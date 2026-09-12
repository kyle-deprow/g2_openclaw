from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from gateway.research.contracts import (
    Attempt,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    ImplementationRecord,
    ReviewEvidence,
    RunPlan,
)
from gateway.research.control import ControlError, OwnerControl, ReviewCancellation, StartResult
from gateway.research.jobs import JobRecord
from gateway.research.jobs import cancel as cancel_job
from gateway.research.status import ResearchStatus, build_status_frame, read_status
from gateway.research.store import ResearchStore

from tests.gateway.research.conftest import run_plan


def _db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "state.sqlite3")
    conn.executescript(
        """
        CREATE TABLE hypotheses (
          hypothesis_id TEXT PRIMARY KEY, state TEXT, created_at TEXT
        );
        CREATE TABLE attempts (
          attempt_id TEXT PRIMARY KEY, hypothesis_id TEXT, state TEXT, updated_at TEXT
        );
        CREATE TABLE events (
          seq INTEGER PRIMARY KEY, at TEXT, hypothesis_id TEXT, attempt_id TEXT,
          kind TEXT, detail TEXT, actor TEXT
        );
        CREATE TABLE campaign (singleton INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO campaign VALUES (1, 'ACTIVE');
        """
    )
    return conn


def test_missing_status_does_not_create_files(tmp_path: Path) -> None:
    root = tmp_path / "missing"

    status = read_status(root, unit_state=lambda _: "inactive")

    assert status.available is False
    assert status.unavailable_reason == "research state database is missing"
    assert not root.exists()


def test_empty_initialized_campaign_is_available_idle(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path)

    status = read_status(tmp_path, unit_state=lambda _: "inactive")

    assert status.available is True
    assert status.stage == "idle"
    assert status.campaign_status == "ACTIVE"
    assert status.hypothesis_id is None
    assert store.campaign() == ("ACTIVE", 0)


def test_status_read_only_uri_handles_special_characters_in_root(tmp_path: Path) -> None:
    root = tmp_path / "research#status?root"
    ResearchStore(root)

    status = read_status(root, unit_state=lambda _: "inactive")

    assert status.available is True
    assert status.stage == "idle"


def test_decided_hypothesis_with_closed_attempt_is_idle(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("INSERT INTO hypotheses VALUES ('H0001','DECIDED','2026-09-06T00:00:00Z')")
    conn.execute(
        "INSERT INTO attempts VALUES ('H0001-A001','H0001','CLOSED','2026-09-06T00:01:00Z')"
    )
    conn.commit()
    conn.close()

    assert read_status(tmp_path, unit_state=lambda _: "inactive").stage == "idle"


@pytest.mark.parametrize(
    ("attempt_state", "expected_stage"),
    [
        ("OPENED", "coding"),
        ("IMPLEMENTED", "review"),
        ("REVIEW_PASSED", "run_queued"),
        ("RUN_QUEUED", "run_queued"),
        ("RUNNING", "running"),
        ("REVIEW_FAILED", "awaiting_decision"),
        ("RUN_SUCCEEDED", "awaiting_decision"),
        ("RUN_FAILED", "awaiting_decision"),
        ("CLOSED", "awaiting_decision"),
    ],
)
def test_status_maps_actual_attempt_states(
    tmp_path: Path, attempt_state: str, expected_stage: str
) -> None:
    conn = _db(tmp_path)
    conn.execute("INSERT INTO hypotheses VALUES ('H0001','FROZEN','2026-09-06T00:00:00Z')")
    conn.execute(
        "INSERT INTO attempts VALUES ('H0001-A001','H0001',?, '2026-09-06T00:01:00Z')",
        (attempt_state,),
    )
    conn.commit()
    conn.close()

    status = read_status(tmp_path, unit_state=lambda _: "inactive")

    assert status.stage == expected_stage
    assert status.attempt_state == attempt_state


def test_status_surfaces_review_and_run_failure_evidence(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("INSERT INTO hypotheses VALUES ('H0001','FROZEN','2026-09-06T00:00:00Z')")
    conn.execute(
        "INSERT INTO attempts VALUES ('H0001-A001','H0001','RUN_FAILED','2026-09-06T00:01:00Z')"
    )
    conn.execute(
        "INSERT INTO events VALUES (1,'2026-09-06T00:02:00Z','H0001','H0001-A001',"
        '\'run_finished\',\'{"exit_code":1,"status":"failed","state":"RUN_FAILED"}\',\'driver\')'
    )
    conn.commit()
    conn.close()

    status = read_status(tmp_path, unit_state=lambda _: "inactive")

    assert status.boundary_failure == "failed"
    assert status.last_event_at == "2026-09-06T00:02:00Z"


def test_status_keeps_owner_failure_visible_when_attempt_id_is_null(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("INSERT INTO hypotheses VALUES ('H0001','FROZEN','2026-09-06T00:00:00Z')")
    conn.execute(
        "INSERT INTO attempts VALUES ('H0001-A001','H0001','RUNNING','2026-09-06T00:01:00Z')"
    )
    conn.execute(
        "INSERT INTO events VALUES (1,'2026-09-06T00:02:00Z','H0001',NULL,"
        "'owner_turn_failed','{\"status\":\"owner_unavailable\"}','owner')"
    )
    conn.commit()
    conn.close()

    status = read_status(tmp_path, unit_state=lambda _: "inactive")

    assert status.boundary_failure == "owner_unavailable"


def test_status_without_owner_probe_is_unknown_unless_lock_is_held(tmp_path: Path) -> None:
    ResearchStore(tmp_path)

    assert read_status(tmp_path, unit_state=None).owner_state == "unknown"

    lock = (tmp_path / "owner.lock").open("w", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert read_status(tmp_path, unit_state=None).owner_state == "active"
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def test_owner_state_requires_unit_and_held_existing_lock(tmp_path: Path) -> None:
    ResearchStore(tmp_path)
    lock = (tmp_path / "owner.lock").open("w", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert read_status(tmp_path, unit_state=lambda _: "active").owner_state == "active"
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    assert read_status(tmp_path, unit_state=lambda _: "active").owner_state == "unknown"


class _FakeUnit:
    def __init__(
        self,
        state: str = "inactive",
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self.current = state
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.calls: list[tuple[str, str]] = []

    def state(self, unit: str) -> str:
        self.calls.append(("state", unit))
        return self.current

    def start(self, unit: str) -> None:
        self.calls.append(("start", unit))
        if self.fail_start:
            raise RuntimeError("start failed")
        self.current = "active"

    def stop(self, unit: str) -> None:
        self.calls.append(("stop", unit))
        if self.fail_stop:
            raise RuntimeError("stop failed")
        self.current = "inactive"


def _configured_store(root: Path) -> ResearchStore:
    store = ResearchStore(root)
    shared_python = Path(sys.executable)
    evaluator = shared_python.parent / "pytest"
    snapshot = root / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text(
        "VERSION = 'fixture'\n", encoding="utf-8"
    )
    (snapshot / "pyproject.toml").write_text("[project]\nname = 'quantipy'\n", encoding="utf-8")
    (snapshot / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    universe = root / "universe.json"
    universe.write_text('{"contract":"trusted-universe-v2"}', encoding="utf-8")
    store.configure(shared_python, evaluator, snapshot, universe)
    return store


def _attempt_fixture(root: Path, *, review: bool = True) -> tuple[ResearchStore, Attempt]:
    """Build one durable attempt using only the public store transitions."""
    store = _configured_store(root)
    spec_file = root / "spec.json"
    panel = root / "panel.json"
    receipt = root / "receipt.json"
    eval_spec = root / "evaluation.json"
    dividends = root / "dividends.json"
    spec_file.write_text('{"objective":"test"}', encoding="utf-8")
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    eval_spec.write_text("evaluation", encoding="utf-8")
    dividends.write_text('{"contract":"trusted-dividends-v2"}', encoding="utf-8")
    spec_set = root / "evaluation-spec-set.json"
    spec_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0001",
            "c000",
            (
                EvaluationSpecEntry(
                    "c000", str(eval_spec), hashlib.sha256(eval_spec.read_bytes()).hexdigest()
                ),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    hypothesis = store.create_hypothesis(
        "control fixture",
        spec_file,
        panel,
        receipt,
        eval_spec,
        "a" * 40,
        dividends,
        evaluation_spec_set=spec_set,
    )
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, root / "worktree")
    implementation = ImplementationRecord(
        attempt_id=attempt.attempt_id,
        commit="b" * 40,
        targets_argv=("/usr/bin/python3", "-m", "fixture.target"),
        test_evidence_path=str(root / "tests.json"),
        reported_coder_model="fixture-coder",
        coder_effort="high",
        coder_service_tier="default",
        submitted_at="2026-09-06T00:02:00Z",
    )
    (root / "tests.json").write_text("{}", encoding="utf-8")
    attempt = store.submit_implementation(
        attempt.attempt_id, implementation, run_plan=run_plan(store, attempt, implementation)
    )
    if review:
        review_record = ReviewEvidence(
            attempt_id=attempt.attempt_id,
            commit="b" * 40,
            spec_sha256=hypothesis.spec_sha256,
            verdict="PASS",
            findings=("fixture pass",),
            reported_reviewer_model="fixture-reviewer",
            reported_reviewer_actual_model="fixture-reviewer",
            acp_session_id="fixture-session",
            submitted_at="2026-09-06T00:03:00Z",
        )
        host_payload = json.dumps(
            {
                "task_id": "fixture-task",
                "task_status": "succeeded",
                "task_started_at": 1,
                "task_ended_at": 2,
                "acp_session_uuid": review_record.acp_session_id,
                "transcript_path": "",
                "transcript_sha256": "",
                "assistant_events": 1,
                "models_seen": ["claude-opus-5"],
                "efforts_seen": ["high"],
                "verdict_json": review_record.to_json(),
                "bound_commit": review_record.commit,
                "bound_spec_sha256": review_record.spec_sha256,
                "bound_run_plan_sha256": hashlib.sha256(
                    store.evidence(attempt.attempt_id, "run_plan").encode()
                ).hexdigest(),
                "collected_at": review_record.submitted_at,
                "verdict": review_record.verdict,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        attempt = store.collect_review_evidence(attempt.attempt_id, review_record, host_payload)
    return store, attempt


def _queue_running_job(store: ResearchStore, attempt: Attempt, job_id: str) -> Attempt:
    run_dir = (
        store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt.attempt_id / "run"
    )
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    store.queue_run_request(attempt.attempt_id, job_id, run_dir, 30, 256, run_plan=plan)
    store.acquire_run_lock()
    try:
        running = store.claim_queued_job(attempt.attempt_id, job_id)
    finally:
        store.release_run_lock()
    row = store.job_for(attempt.attempt_id)
    assert row is not None
    payload = json.loads(str(row["payload_json"]))
    payload["worker_pid"] = 0
    payload["worker_starttime"] = 0
    payload["state"] = "LAUNCHED"
    store.update_job(payload, attempt.attempt_id)
    return running


def test_start_allows_configured_empty_campaign(tmp_path: Path) -> None:
    _configured_store(tmp_path)
    unit = _FakeUnit()

    result = OwnerControl(tmp_path, unit=unit, readiness_gate=lambda _: None).start()

    assert isinstance(result, StartResult)
    assert result.started is True
    assert result.resumed is False
    assert unit.calls[-1] == ("start", "research-owner.service")


def test_start_refuses_unconfigured_or_missing_owner_unit(tmp_path: Path) -> None:
    with pytest.raises(ControlError, match="missing or not configured"):
        OwnerControl(tmp_path, unit=_FakeUnit()).start()
    _configured_store(tmp_path)
    with pytest.raises(ControlError, match="not installed"):
        OwnerControl(tmp_path, unit=_FakeUnit("missing")).start()


def test_start_failed_unit_keeps_campaign_paused(tmp_path: Path) -> None:
    store = _configured_store(tmp_path)
    unit = _FakeUnit(fail_start=True)

    with pytest.raises(ControlError, match="start failed"):
        OwnerControl(tmp_path, unit=unit, readiness_gate=lambda _: None).start()

    assert store.campaign()[0] == "PAUSED"


def test_start_refuses_until_readiness_gate_is_integrated(tmp_path: Path) -> None:
    _configured_store(tmp_path)

    with pytest.raises(ControlError, match="readiness gate is not integrated"):
        OwnerControl(tmp_path, unit=_FakeUnit()).start()


def test_start_refuses_gate_before_resuming_or_starting_owner(tmp_path: Path) -> None:
    store = _configured_store(tmp_path)
    store.pause("test")
    unit = _FakeUnit("inactive")

    with pytest.raises(ControlError, match="campaign ceiling is not approved"):
        OwnerControl(
            tmp_path,
            unit=unit,
            readiness_gate=lambda _: "campaign ceiling is not approved",
        ).start()

    assert store.campaign()[0] == "PAUSED"
    assert not [call for call in unit.calls if call[0] == "start"]


def test_stop_pauses_before_stopping_owner_only(tmp_path: Path) -> None:
    store = _configured_store(tmp_path)
    unit = _FakeUnit("active")

    result = OwnerControl(tmp_path, unit=unit).stop()

    assert result.completed is True
    assert result.paused is True
    assert unit.calls[-1] == ("stop", "research-owner.service")
    assert store.campaign()[0] == "PAUSED"


def test_stop_cancels_exact_running_job_after_pause(tmp_path: Path) -> None:
    store, attempt = _attempt_fixture(tmp_path)
    attempt = _queue_running_job(store, attempt, "job-exact")
    unit = _FakeUnit("active")
    cancelled: list[tuple[str, str, int, int]] = []

    def cancel_exact(job: JobRecord) -> str:
        cancelled.append((job.job_id, job.attempt_id, job.worker_pid, job.worker_starttime))
        assert store.campaign()[0] == "PAUSED"
        return "CANCELLED"

    result = OwnerControl(tmp_path, unit=unit, job_cancel=cancel_exact).stop()

    assert result.completed is True
    assert result.job_cancelled is True
    assert cancelled == [("job-exact", attempt.attempt_id, 0, 0)]
    assert unit.calls[-1] == ("stop", "research-owner.service")
    assert store.job_for(attempt.attempt_id)["state"] == "CANCELLED"  # type: ignore[index]


def test_stop_refuses_wrong_attempt_identity_without_cancel_signal(tmp_path: Path) -> None:
    store, attempt = _attempt_fixture(tmp_path)
    _queue_running_job(store, attempt, "job-identity")
    row = store.job_for(attempt.attempt_id)
    assert row is not None
    payload = json.loads(str(row["payload_json"]))
    payload["attempt_id"] = "H0001-A999"
    store.update_job(payload, attempt.attempt_id)
    unit = _FakeUnit("active")
    cancel_calls: list[str] = []

    def must_not_cancel(_job: JobRecord) -> str:
        cancel_calls.append("signal")
        return "CANCELLED"

    result = OwnerControl(tmp_path, unit=unit, job_cancel=must_not_cancel).stop()

    assert cancel_calls == []
    assert result.owner_stopped is True
    assert result.completed is False
    assert any("identity" in error for error in result.errors)


def test_stop_refuses_stale_pid_and_still_stops_owner(tmp_path: Path) -> None:
    store, attempt = _attempt_fixture(tmp_path)
    _queue_running_job(store, attempt, "job-stale")
    unit = _FakeUnit("active")

    result = OwnerControl(tmp_path, unit=unit, job_cancel=cancel_job).stop()

    assert result.job_cancelled is False
    assert result.owner_stopped is True
    assert result.completed is False
    assert any("stale_job_identity" in error for error in result.errors)


def test_stop_cancellation_error_does_not_skip_owner_stop(tmp_path: Path) -> None:
    store, attempt = _attempt_fixture(tmp_path)
    _queue_running_job(store, attempt, "job-error")
    unit = _FakeUnit("active")

    def fail_cancel(_job: JobRecord) -> str:
        raise RuntimeError("cancel adapter failed")

    result = OwnerControl(tmp_path, unit=unit, job_cancel=fail_cancel).stop()

    assert result.owner_stopped is True
    assert result.completed is False
    assert any("cancellation incomplete" in error for error in result.errors)


def test_stop_cancels_queued_job_without_signalling(tmp_path: Path) -> None:
    store, attempt = _attempt_fixture(tmp_path)
    run_dir = (
        tmp_path / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt.attempt_id / "run"
    )
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    queued = store.queue_run_request(
        attempt.attempt_id, "job-queued", run_dir, 30, 256, run_plan=plan
    )
    unit = _FakeUnit("active")

    result = OwnerControl(tmp_path, unit=unit).stop()

    assert result.owner_stopped is True
    assert result.job_cancelled is True
    assert result.completed is True
    assert store.get_attempt(attempt.attempt_id).state is AttemptState.REVIEW_PASSED
    row = store.job_for(queued.attempt_id)
    assert row is not None and row["state"] == "CANCELLED"


@pytest.mark.parametrize("adapter_state", ["cancelled", "pending", "unknown"])
def test_stop_reports_structured_review_cancellation_outcome(
    tmp_path: Path, adapter_state: str
) -> None:
    _store, attempt = _attempt_fixture(tmp_path, review=False)
    unit = _FakeUnit("active")
    calls: list[str] = []

    class Canceller:
        def cancel(self, received: Attempt, root: Path, reason: str) -> ReviewCancellation:
            calls.append(f"{received.attempt_id}:{root.name}:{reason}")
            return ReviewCancellation(adapter_state)

    result = OwnerControl(tmp_path, unit=unit, review_canceller=Canceller()).stop()

    expected = "cancelled" if adapter_state == "cancelled" else f"incomplete_{adapter_state}"
    assert result.review_cancellation == expected
    assert result.owner_stopped is True
    assert result.completed == (adapter_state == "cancelled")
    assert calls == [f"{attempt.attempt_id}:{tmp_path.name}:operator stop"]


def test_stop_review_without_adapter_is_explicitly_incomplete(tmp_path: Path) -> None:
    _store, _attempt = _attempt_fixture(tmp_path, review=False)

    result = OwnerControl(tmp_path, unit=_FakeUnit("active")).stop()

    assert result.review_cancellation == "incomplete_adapter"
    assert result.owner_stopped is True
    assert result.completed is False


def test_stop_review_adapter_error_keeps_owner_stop_and_incomplete(tmp_path: Path) -> None:
    _store, _attempt = _attempt_fixture(tmp_path, review=False)

    class FailingCanceller:
        def cancel(self, attempt: Attempt, root: Path, reason: str) -> ReviewCancellation:
            raise RuntimeError("review unavailable")

    result = OwnerControl(
        tmp_path,
        unit=_FakeUnit("active"),
        review_canceller=FailingCanceller(),
    ).stop()

    assert result.review_cancellation == "incomplete_error"
    assert result.owner_stopped is True
    assert result.completed is False
    assert any("pending review cancellation failed" in error for error in result.errors)


def test_stop_owner_failure_is_reported_after_pause_and_reconciliation(tmp_path: Path) -> None:
    store, attempt = _attempt_fixture(tmp_path)
    _queue_running_job(store, attempt, "job-owner-failure")
    result = OwnerControl(
        tmp_path,
        unit=_FakeUnit("active", fail_stop=True),
        job_cancel=lambda _job: "CANCELLED",
    ).stop()

    assert result.paused is True
    assert result.job_cancelled is True
    assert result.owner_stopped is False
    assert result.completed is False
    assert any("owner unit stop failed" in error for error in result.errors)


def test_status_frame_has_new_camel_case_fields() -> None:
    frame = build_status_frame(
        ResearchStatus(
            hypothesis_id=None,
            hypothesis_state=None,
            attempt_id=None,
            attempt_state=None,
            stage="idle",
            last_astra_decision=None,
            campaign_status="ACTIVE",
            boundary_failure=None,
            last_event_at=None,
            owner_state="inactive",
            updated_at=None,
        )
    )
    assert set(frame) == {
        "type",
        "hypothesisId",
        "hypothesisState",
        "attemptId",
        "attemptState",
        "stage",
        "lastAstraDecision",
        "campaignStatus",
        "boundaryFailure",
        "lastEventAt",
        "ownerState",
        "updatedAt",
        "available",
        "unavailableReason",
    }
