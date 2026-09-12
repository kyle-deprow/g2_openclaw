from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research.contracts import (
    AnalysisPlan,
    Attempt,
    AttemptDecision,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisDecision,
    ImplementationRecord,
    JobState,
    RunPlan,
    RunScenario,
)
from gateway.research.jobs import JobError, JobRecord
from gateway.research.store import ResearchStore, StoreConflict
from typer.testing import CliRunner

from tests.gateway.research.conftest import review, verified_review
from tests.gateway.research.conftest import run_plan as fixture_run_plan
from tests.gateway.research.test_admission import _admit, _document, _payload
from tests.gateway.research.test_readiness import configure_real_readiness


def _ready(
    store: ResearchStore,
    source: Path,
    hypothesis: Any,
    *,
    run_plan_factory: Callable[[Attempt, ImplementationRecord], RunPlan] | None = None,
) -> Attempt:
    if store.get_hypothesis(hypothesis.hypothesis_id).state.value == "DRAFT":
        store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    implementation_record = ImplementationRecord(
        attempt.attempt_id,
        commit,
        (sys.executable, "-m", "fixture_target"),
        "/tmp/evidence.json",
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    run_plan = (
        run_plan_factory(attempt, implementation_record) if run_plan_factory is not None else None
    )
    if run_plan is None:
        run_plan = fixture_run_plan(store, attempt, implementation_record)
    store.submit_implementation(attempt.attempt_id, implementation_record, run_plan=run_plan)
    verified_review(store, review(attempt.attempt_id, commit, hypothesis.spec_sha256))
    return store.get_attempt(attempt.attempt_id)


def _insert_verified(store: ResearchStore, attempt_id: str, kind: str) -> None:
    payload = json.dumps({"verified": True}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
            (attempt_id, kind, payload, digest),
        )
        conn.commit()


def _install_dispatch_admission(
    store: ResearchStore, attempt: Attempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_set_digest = hashlib.sha256(
        store.evaluation_spec_set(attempt.hypothesis_id).to_json().encode()
    ).hexdigest()
    decision = _admit(_document(_payload()), evaluation_spec_set_sha256=spec_set_digest)
    store.insert_admission_decision(attempt.attempt_id, decision)
    monkeypatch.setattr(research_cli, "_admission_for_hypothesis", lambda *_args: decision)


def _queue(store: ResearchStore, attempt_id: str, *, job_id: str = "job-queue-test") -> Attempt:
    attempt = store.get_attempt(attempt_id)
    run_dir = store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id / "run"
    plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    return store.queue_run_request(attempt_id, job_id, run_dir, 30, 256, run_plan=plan)


def test_run_cli_only_queues_and_does_not_spawn(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"worker launch from runner: {args!r} {kwargs!r}")

    monkeypatch.setattr(research_cli, "launch", forbidden)
    result = CliRunner().invoke(
        app,
        ["research", "run", attempt.attempt_id, "--root", str(store.root), "--no-wait"],
    )
    assert result.exit_code == 0, result.output
    assert "state=QUEUED" in result.output
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_QUEUED
    row = store.job_for(attempt.attempt_id)
    assert row is not None and row["state"] == JobState.QUEUED.value
    assert not (Path(json.loads(row["payload_json"])["run_dir"]) / "job.json").exists()


def test_queue_limits_reject_nonfinite_negative_and_ceiling(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    for index, timeout in enumerate((math.nan, math.inf, -1.0), start=1):
        with pytest.raises(ValueError):
            store.queue_run_request(
                attempt.attempt_id, f"job-invalid-{index}", run_dir, timeout, 256
            )
    with pytest.raises(ValueError):
        store.queue_run_request(attempt.attempt_id, "job-invalid-rss", run_dir, 30, 8193)


def test_queue_rejects_run_plan_stage_timeout_above_job_timeout(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign

    def long_plan(attempt: Attempt, record: ImplementationRecord) -> RunPlan:
        base = fixture_run_plan(store, attempt, record)
        return replace(base, scenario_timeout_seconds=20, analysis_timeout_seconds=20)

    attempt = _ready(store, source, hypothesis, run_plan_factory=long_plan)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))

    with pytest.raises(StoreConflict, match="timeouts"):
        store.queue_run_request(attempt.attempt_id, "job-timeout-cap", run_dir, 10, 256, plan)


def test_queue_replay_is_idempotent_and_conflicting_payload_refused(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    first = _queue(store, attempt.attempt_id)
    second = _queue(store, attempt.attempt_id)
    assert second == first
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    with pytest.raises(StoreConflict):
        store.queue_run_request(
            attempt.attempt_id,
            first.run_job_id or "job-queue-test",
            store.root / "other-run",
            31,
            256,
            run_plan=plan,
        )


def test_host_dispatch_rejects_tampered_run_plan_before_launch(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    row = store.job_for(attempt.attempt_id)
    assert row is not None
    payload = json.loads(str(row["payload_json"]))
    payload["run_plan_sha256"] = "0" * 64
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET payload_json=?,payload_sha256=? WHERE job_id=?",
            (text, hashlib.sha256(text.encode()).hexdigest(), payload["job_id"]),
        )
        conn.commit()
    monkeypatch.setattr(research_cli, "launch", lambda *_a, **_k: pytest.fail("spawn"))
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == "run_plan_mismatch"
    finally:
        store.release_owner_lock()


def test_competing_queue_requests_have_one_winner(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)

    def enqueue(job_id: str) -> str:
        try:
            _queue(store, attempt.attempt_id, job_id=job_id)
            return "won"
        except StoreConflict:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, ("job-race-a", "job-race-b")))
    assert sorted(results) == ["lost", "won"]
    assert len(store.queued_jobs()) == 1


def test_dispatch_requires_owner_and_explicit_host_native_gates(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _insert_verified(store, attempt.attempt_id, "host_execution")
    _insert_verified(store, attempt.attempt_id, "native_execution")
    with pytest.raises(JobError, match="owner lock"):
        research_cli._dispatch_queued_job(store)
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == (
            "native_execution_unavailable:native_core_database_unset"
        )
    finally:
        store.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    row = store.job_for(attempt.attempt_id)
    assert row is not None and row["state"] == JobState.EXITED.value


def test_dispatch_rejects_new_launch_without_admission_evidence(
    campaign: tuple[ResearchStore, Path, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directly-created legacy attempt must not reach a fresh worker launch."""
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    _insert_verified(store, attempt.attempt_id, "host_execution")
    _insert_verified(store, attempt.attempt_id, "native_execution")
    configure_real_readiness(store, store.root.parent, monkeypatch)
    monkeypatch.setattr(research_cli, "launch", lambda *_a, **_k: pytest.fail("spawn"))

    store.acquire_owner_lock()
    try:
        result = research_cli._dispatch_queued_job(store)
    finally:
        store.release_owner_lock()

    assert result is not None and result.startswith("ADMISSION_")
    assert store.get_attempt(attempt.attempt_id).state is AttemptState.REVIEW_PASSED
    row = store.job_for(attempt.attempt_id)
    assert row is not None and row["state"] == "ADMISSION_REFUSED"


def test_budget_gate_is_explicit_when_host_and_native_are_verified(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _insert_verified(store, attempt.attempt_id, "host_execution")
    _insert_verified(store, attempt.attempt_id, "native_execution")
    configure_real_readiness(store, store.root.parent, monkeypatch, policy=False)
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == (
            "budget_unavailable:budget_campaign_policy_unset"
        )
    finally:
        store.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED


def test_pause_before_claim_leaves_queue_untouched(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    store.pause("operator pause")
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) is None
    finally:
        store.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_QUEUED
    assert store.job_for(attempt.attempt_id)["state"] == JobState.QUEUED.value  # type: ignore[index]


def test_dispatch_claims_once_with_injected_worker_boundary(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _insert_verified(store, attempt.attempt_id, "host_execution")
    _insert_verified(store, attempt.attempt_id, "native_execution")
    configure_real_readiness(store, store.root.parent, monkeypatch)
    launches = 0

    def fake_launch(attempt_dir: Path, *_args: object, **kwargs: object) -> JobRecord:
        nonlocal launches
        launches += 1
        job_id = str(kwargs["job_id"])
        return JobRecord(job_id, attempt.attempt_id, os.getpid(), 1, str(attempt_dir / "run"))

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == "job-queue-test"
        assert research_cli._dispatch_queued_job(store) is None
    finally:
        store.release_owner_lock()
    assert launches == 1
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUNNING
    assert store.job_for(attempt.attempt_id)["state"] == JobState.LAUNCHED.value  # type: ignore[index]


def test_queued_cancel_writes_terminal_without_signalling(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    monkeypatch.setattr(research_cli, "cancel_job", lambda *_args, **_kwargs: pytest.fail("signal"))
    result = CliRunner().invoke(
        app,
        ["research", "cancel", attempt.attempt_id, "--root", str(store.root)],
    )
    assert result.exit_code == 0, result.output
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    row = store.job_for(attempt.attempt_id)
    assert row is not None and row["state"] == JobState.CANCELLED.value
    terminal = Path(json.loads(row["payload_json"])["run_dir"]) / "terminal.json"
    assert json.loads(terminal.read_text())["status"] == "cancelled"


def test_running_cancel_reports_already_completed_outcome_honestly(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    store.acquire_run_lock()
    try:
        store.claim_queued_job(attempt.attempt_id, "job-queue-test")
    finally:
        store.release_run_lock()

    row = store.job_for(attempt.attempt_id)
    assert row is not None
    payload = json.loads(str(row["payload_json"]))
    worker_pid = os.getpid()
    worker_starttime = int(Path(f"/proc/{worker_pid}/stat").read_text().split()[21])
    payload.update(
        {
            "worker_pid": worker_pid,
            "worker_starttime": worker_starttime,
            "state": "LAUNCHED",
        }
    )
    store.update_job(payload, attempt.attempt_id)
    run_dir = Path(str(payload["run_dir"]))
    result_path = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text('{"compliant":true}', encoding="utf-8")
    result_digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    plan_digest = hashlib.sha256(plan.to_json().encode()).hexdigest()
    validated_inputs = run_dir / "validated-inputs.json"
    validated_inputs.write_text('{"fixture":true}', encoding="utf-8")
    analysis_path = run_dir / "analysis-stage" / "analysis" / "result.json"
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text("{}", encoding="utf-8")
    terminal = run_dir / "terminal.json"
    evidence = run_dir / "run-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "contract": "research-run-evidence-v1",
                "status": "succeeded",
                "job_id": payload["job_id"],
                "attempt_id": attempt.attempt_id,
                "run_plan_sha256": plan_digest,
                "evaluation_spec_set_sha256": plan.evaluation_spec_set_sha256,
                "primary_scenario_id": "s000",
                "scenarios": {
                    "s000": {
                        "status": "succeeded",
                        "spec_id": plan.scenarios[0].spec_id,
                        "spec_sha256": plan.scenarios[0].evaluation_spec_sha256,
                        "result_path": str(result_path),
                        "result_sha256": result_digest,
                    }
                },
                "completed_scenarios": ["s000"],
                "validated_inputs_sha256": hashlib.sha256(
                    validated_inputs.read_bytes()
                ).hexdigest(),
                "analysis": {
                    "analysis/result.json": hashlib.sha256(analysis_path.read_bytes()).hexdigest()
                },
                "checks": [{"name": "source_before", "value": "fixture", "ok": True}],
            }
        ),
        encoding="utf-8",
    )
    terminal.write_text(
        json.dumps(
            {
                "job_id": payload["job_id"],
                "worker_pid": worker_pid,
                "worker_starttime": worker_starttime,
                "status": "succeeded",
                "evaluator_exit": 0,
                "run_evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["research", "cancel", attempt.attempt_id, "--root", str(store.root)],
    )
    assert result.exit_code == 0, result.output
    assert "ALREADY_FINISHED status=succeeded" in result.output
    assert "CANCELLED" not in result.output
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_SUCCEEDED
    assert not (run_dir / "cancel.json").exists()


def test_running_cancel_publishes_cancel_sidecar_without_worker_evidence(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running cancel must reconcile its host sidecar as canonical cancellation."""
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    store.acquire_run_lock()
    try:
        store.claim_queued_job(attempt.attempt_id, "job-queue-test")
    finally:
        store.release_run_lock()
    row = store.job_for(attempt.attempt_id)
    assert row is not None
    payload = json.loads(str(row["payload_json"]))
    payload.update(worker_pid=4242, worker_starttime=77, state="LAUNCHED")
    store.update_job(payload, attempt.attempt_id)

    def publish_cancel(job: JobRecord) -> str:
        Path(job.run_dir).mkdir(parents=True, exist_ok=True)
        (Path(job.run_dir) / "cancel.json").write_text(
            json.dumps(
                {
                    "job_id": job.job_id,
                    "attempt_id": job.attempt_id,
                    "worker_pid": job.worker_pid,
                    "worker_starttime": job.worker_starttime,
                    "status": "cancelled",
                    "targets_exit": -15,
                    "evaluator_exit": None,
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:00:01Z",
                }
            ),
            encoding="utf-8",
        )
        return "CANCELLED"

    monkeypatch.setattr(research_cli, "cancel_job", publish_cancel)
    result = CliRunner().invoke(
        app,
        ["research", "cancel", attempt.attempt_id, "--root", str(store.root)],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "CANCELLED"
    stored = store.get_attempt(attempt.attempt_id)
    assert stored.state == AttemptState.RUN_FAILED
    assert stored.run_outcome is not None
    assert json.loads(stored.run_outcome)["status"] == "cancelled"


def test_owner_lock_does_not_block_queue_or_cancel(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    owner = ResearchStore(store.root)
    owner.acquire_owner_lock()
    try:
        _call = CliRunner().invoke(
            app,
            [
                "research",
                "run",
                attempt.attempt_id,
                "--root",
                str(store.root),
                "--no-wait",
            ],
        )
        assert _call.exit_code == 0, _call.output
        cancelled = CliRunner().invoke(
            app,
            ["research", "cancel", attempt.attempt_id, "--root", str(store.root)],
        )
        assert cancelled.exit_code == 0, cancelled.output
    finally:
        owner.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED


def test_reserved_pid_zero_restart_is_explicit_failure_without_signal(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)
    store.acquire_owner_lock()
    try:
        store.acquire_run_lock()
        try:
            store.claim_queued_job(attempt.attempt_id, "job-queue-test")
        finally:
            store.release_run_lock()
        research_cli._reconcile_jobs(store)
    finally:
        store.release_owner_lock()
    updated = store.get_attempt(attempt.attempt_id)
    assert updated.state == AttemptState.RUN_FAILED
    assert '"status":"interrupted_before_launch"' in (updated.run_outcome or "")


def test_three_attempt_retry_and_second_hypothesis_queue_lifecycle(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    for number in range(1, 4):
        attempt = _ready(store, source, hypothesis)
        _queue(store, attempt.attempt_id, job_id=f"job-retry-{number}")
        result = CliRunner().invoke(
            app,
            ["research", "cancel", attempt.attempt_id, "--root", str(store.root)],
        )
        assert result.exit_code == 0, result.output
        decision = AttemptDecision.FINISH if number == 3 else AttemptDecision.RETRY
        store.close_attempt(attempt.attempt_id, decision, f"attempt {number}")
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.FINISHED, "three attempts")
    spec_file = store.root / "second-spec.json"
    spec_file.write_text('{"second":true}', encoding="utf-8")
    second_eval = Path(hypothesis.evaluation_spec_path)
    second_set = store.root / "second-evaluation-spec-set.json"
    second_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0002",
            "c000",
            (
                EvaluationSpecEntry(
                    "c000",
                    str(second_eval),
                    hashlib.sha256(second_eval.read_bytes()).hexdigest(),
                ),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    second = store.create_hypothesis(
        "second",
        spec_file,
        Path(hypothesis.panel_path),
        Path(hypothesis.receipt_path),
        second_eval,
        hypothesis.base_commit,
        dividends=Path(hypothesis.dividends_path),
        evaluation_spec_set=second_set,
    )

    def second_run_plan(attempt: Attempt, implementation: ImplementationRecord) -> RunPlan:
        return RunPlan(
            "research-run-plan-v1",
            attempt.attempt_id,
            implementation.commit,
            hashlib.sha256(implementation.to_json().encode()).hexdigest(),
            hashlib.sha256(
                store.evaluation_spec_set(second.hypothesis_id).to_json().encode()
            ).hexdigest(),
            "s000",
            (
                RunScenario(
                    "s000",
                    implementation.targets_argv,
                    "c000",
                    hashlib.sha256(second_eval.read_bytes()).hexdigest(),
                ),
            ),
            AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
            10,
            10,
        )

    attempt = _ready(store, source, second, run_plan_factory=second_run_plan)
    queued = _queue(store, attempt.attempt_id, job_id="job-second")
    assert queued.state == AttemptState.RUN_QUEUED
    assert len(store.attempts_for(hypothesis.hypothesis_id)) == 3
    assert len(store.hypotheses()) == 2
