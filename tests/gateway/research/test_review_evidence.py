from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from gateway.cli import app
from gateway.openclaw_client import OpenClawTransportError
from gateway.research.contracts import (
    AttemptState,
    HypothesisDecision,
    HypothesisSpec,
    ImplementationRecord,
    ReviewRecord,
)
from gateway.research.review_evidence import (
    BundleError,
    ReviewPending,
    ReviewUnresolved,
    acknowledge_review,
    cancel_review,
    collect_review,
    reserve_review,
)
from gateway.research.store import ResearchStore, StoreConflict
from typer.testing import CliRunner

runner = CliRunner()


def _setup(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> tuple[ResearchStore, Path, HypothesisSpec, str, Path]:
    store, source, hypothesis = campaign
    evidence = tmp_path / "tests.json"
    evidence.write_text('{"pytest":"pass"}\n', encoding="utf-8")
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    store.submit_implementation(
        attempt.attempt_id,
        ImplementationRecord(
            attempt.attempt_id,
            commit,
            ("python", "-m", "target"),
            str(evidence),
            "reported-coder",
            "high",
            "standard",
            "2026-01-01T00:00:00Z",
        ),
    )
    bundle = tmp_path / "review-bundle"
    return store, source, hypothesis, attempt.attempt_id, bundle


def _host_fixture(
    store: ResearchStore,
    attempt_id: str,
    bundle: Path,
    tmp_path: Path,
    *,
    status: str = "succeeded",
    verdict: str = "PASS",
) -> tuple[Path, Path, Path]:
    reservation_payload = json.loads(store.evidence(attempt_id, "review_reservation"))
    label = str(reservation_payload["label"])
    reserved_at = datetime.fromisoformat(
        str(reservation_payload["reserved_at"]).replace("Z", "+00:00")
    )
    reserved_ms = int(reserved_at.timestamp() * 1000)
    core = tmp_path / "openclaw.sqlite"
    child_key = "agent:claude:acp:child"
    run_id = "run-1"
    session_uuid = "84785a20-b278-4d4a-9c62-fa35e5bb9928"
    with sqlite3.connect(core) as conn:
        conn.executescript(
            """
            CREATE TABLE task_runs(
              task_id TEXT, runtime TEXT, task_kind TEXT, source_id TEXT,
              requester_session_key TEXT, owner_key TEXT, scope_kind TEXT,
              child_session_key TEXT, agent_id TEXT, requester_agent_id TEXT,
              run_id TEXT, label TEXT, status TEXT, created_at INTEGER,
              started_at INTEGER, ended_at INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO task_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task-1",
                "acp",
                "review",
                None,
                "owner",
                "owner",
                "session",
                child_key,
                "claude",
                None,
                run_id,
                label,
                status,
                reserved_ms + 1000,
                reserved_ms + 2000,
                reserved_ms + 3000,
            ),
        )
        conn.commit()
    sessions = tmp_path / "acpx-sessions"
    sessions.mkdir()
    record_uuid = "10551e01-0503-456f-9e61-6993c912d478"
    record = {
        "schema": "acpx.session.v1",
        "acpx_record_id": f"{child_key}:oneshot:{record_uuid}",
        "acp_session_id": session_uuid,
        "cwd": str(bundle),
        "name": child_key,
        "created_at": (reserved_at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "last_used_at": (reserved_at + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "last_request_id": run_id,
        "closed": True,
        "closed_at": (reserved_at + timedelta(seconds=4)).isoformat().replace("+00:00", "Z"),
        "title": f"review {attempt_id}",
        "messages": [{"User": {"content": []}}],
        "acpx": {
            "desired_config_options": {"effort": "high"},
            "session_options": {"model": "claude-opus-5"},
        },
    }
    record_path = sessions / (quote(f"{child_key}:oneshot:{record_uuid}", safe="") + ".json")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    encoded = "".join(char if char.isalnum() and char.isascii() else "-" for char in str(bundle))
    project = tmp_path / "projects" / encoded
    project.mkdir(parents=True)
    event_time = (reserved_at + timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    verdict_object = {
        "verdict": verdict,
        "attempt_id": attempt_id,
        "commit": str(reservation_payload["commit"]),
        "spec_sha256": str(reservation_payload["hypothesis_spec_sha256"]),
        "findings": [],
    }
    transcript = {
        "type": "assistant",
        "sessionId": session_uuid,
        "cwd": str(bundle),
        "effort": "high",
        "timestamp": event_time,
        "message": {
            "model": "claude-opus-5",
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(verdict_object)}],
        },
    }
    (project / f"{session_uuid}.jsonl").write_text(json.dumps(transcript) + "\n", encoding="utf-8")
    return core, sessions, tmp_path / "projects"


def _transcript_path(projects: Path) -> Path:
    paths = list(projects.rglob("*.jsonl"))
    assert len(paths) == 1
    return paths[0]


def _prepare_review(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    *,
    status: str = "succeeded",
    verdict: str = "PASS",
) -> tuple[ResearchStore, str, Path, Path, Path, Path]:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(
        store, attempt_id, bundle, tmp_path, status=status, verdict=verdict
    )
    return store, attempt_id, bundle, core, sessions, projects


def test_reservation_builds_actual_read_only_bundle_and_ack_replays(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = __import__(
        "gateway.research.review_evidence", fromlist=["reserve_review"]
    ).reserve_review(store, attempt_id, bundle, "owner")
    assert reservation.label.startswith(attempt_id + "-")
    assert (bundle / "source" / "tracked.txt").read_text() == "tracked"
    instructions = (bundle / "instructions.md").read_text()
    assert f"attempt_id={attempt_id}" in instructions
    assert f"spec_sha256={hypothesis.spec_sha256}" in instructions
    assert not (bundle / "source" / "tracked.txt").stat().st_mode & 0o222
    ack = acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    assert (
        acknowledge_review(store, attempt_id, ack.child_session_key, ack.run_id, ack.mode, None)
        == ack
    )
    result = runner.invoke(
        app,
        [
            "research",
            "review-reserve",
            attempt_id,
            "--root",
            str(store.root),
            "--bundle-dir",
            str(bundle),
            "--owner-key",
            "owner",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["effort"] == "high"


def test_reservation_accepts_nested_tracked_source_directories(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    _store, source, _hypothesis = campaign
    source.chmod(0o755)
    nested = source / "fixture" / "nested" / "artifact.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "nested fixture"], cwd=source, check=True)
    source.chmod(0o555)

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = reserve_review(store, attempt_id, bundle, "owner")

    assert (bundle / "source" / "fixture" / "nested" / "artifact.py").read_text() == "VALUE = 1\n"
    assert (
        reserve_review(store, attempt_id, bundle, "owner").bundle_sha256
        == reservation.bundle_sha256
    )


def test_reservation_replay_and_collect_use_frozen_test_evidence_copy(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = reserve_review(store, attempt_id, bundle, "owner")
    implementation = json.loads(store.evidence(attempt_id, "implementation"))
    Path(str(implementation["test_evidence_path"])).unlink()

    replay = reserve_review(store, attempt_id, bundle, "owner")
    assert replay.bundle_sha256 == reservation.bundle_sha256
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)

    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )


def test_collect_requires_terminal_host_evidence_and_is_idempotent(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )
    attempt_dir = store.root / "hypotheses" / "H0001" / "attempts" / attempt_id
    (attempt_dir / "review.json").unlink()
    (attempt_dir / "review_host_evidence.json").unlink()
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )
    assert (attempt_dir / "review.json").is_file()
    assert (attempt_dir / "review_host_evidence.json").is_file()
    assert len([row for row in store.events() if row.kind == "review_collected"]) == 1
    run_dir = store.root / "hypotheses" / "H0001" / "attempts" / attempt_id / "run"
    assert (
        store.queue_run_request(attempt_id, "job-evidence", run_dir, 30, 256).state
        == AttemptState.RUN_QUEUED
    )


def test_collect_nonterminal_is_pending_and_bundle_mutation_is_rejected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path, status="running")
    with pytest.raises(ReviewPending):
        collect_review(store, attempt_id, core, sessions, projects)
    bundle.chmod(0o755)
    (bundle / "instructions.md").chmod(0o644)
    (bundle / "instructions.md").write_text("changed", encoding="utf-8")
    with pytest.raises(BundleError):
        reserve_review(store, attempt_id, bundle, "owner")


def test_collect_wrong_owner_is_unresolved_and_pauses_campaign(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "wrong-owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.campaign()[0] == "PAUSED"


@pytest.mark.parametrize(
    "format_name",
    ("fenced", "prose", "trailing", "duplicate", "extra", "multiple", "tool_call"),
)
def test_collect_rejects_non_bare_or_non_exact_verdicts(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    format_name: str,
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    transcript = _transcript_path(projects)
    event = json.loads(transcript.read_text())
    verdict_text = str(event["message"]["content"][0]["text"])
    if format_name == "fenced":
        verdict_text = f"```json\n{verdict_text}\n```"
    elif format_name == "prose":
        verdict_text = f"Review complete: {verdict_text}"
    elif format_name == "trailing":
        verdict_text += " trailing prose"
    elif format_name == "duplicate":
        verdict_text = verdict_text.replace(
            '"verdict": "PASS"', '"verdict": "PASS", "verdict": "PASS"'
        )
    elif format_name == "extra":
        verdict = json.loads(verdict_text)
        verdict["extra"] = "reject"
        verdict_text = json.dumps(verdict)
    elif format_name == "multiple":
        verdict_text = f"{verdict_text} {verdict_text}"
    else:
        event["message"]["content"] = [{"type": "tool_use", "id": "tool"}]
    if format_name != "tool_call":
        event["message"]["content"] = [{"type": "text", "text": verdict_text}]
    transcript.write_text(json.dumps(event) + "\n")

    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )


@pytest.mark.parametrize("field,value", (("model", "claude-sonnet"), ("effort", "low")))
def test_collect_rejects_wrong_model_or_effort(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    transcript = _transcript_path(projects)
    event = json.loads(transcript.read_text())
    if field == "model":
        event["message"][field] = value
    else:
        event[field] = value
    transcript.write_text(json.dumps(event) + "\n")
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )


def test_collect_rejects_non_terminal_final_assistant_event(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    transcript = _transcript_path(projects)
    event = json.loads(transcript.read_text())
    event["message"]["stop_reason"] = "tool_use"
    transcript.write_text(json.dumps(event) + "\n")
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )


def test_identity_or_transcript_host_error_is_unresolved(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    shutil.rmtree(sessions)
    sessions.mkdir()
    (sessions / "malformed.json").write_text("{}")
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.get_attempt(attempt_id).state == AttemptState.IMPLEMENTED
    assert any(event.kind == "review_unresolved" for event in store.events())


def test_missing_transcript_is_unresolved(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    _transcript_path(projects).unlink()
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.get_attempt(attempt_id).state == AttemptState.IMPLEMENTED


def test_two_matching_tasks_are_unresolved(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    with sqlite3.connect(core) as conn:
        conn.execute("INSERT INTO task_runs SELECT * FROM task_runs")
        conn.commit()
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.campaign()[0] == "PAUSED"


def test_cancel_uses_exact_task_and_unknown_response_stays_pending(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, _sessions, _projects = _host_fixture(
        store, attempt_id, bundle, tmp_path, status="running"
    )
    calls: list[tuple[str, str]] = []

    async def lost_response(task_id: str, reason: str) -> dict[str, object]:
        calls.append((task_id, reason))
        raise OpenClawTransportError("connection lost after send")

    outcome = cancel_review(store, attempt_id, "stop review", core, lost_response)
    assert calls == [("task-1", "stop review")]
    assert outcome.pending is True
    assert outcome.rpc_result == "UNKNOWN_RESPONSE"
    assert json.loads(store.evidence(attempt_id, "review_cancel"))["task_id"] == "task-1"

    replay = cancel_review(store, attempt_id, "stop review", core, lost_response)
    assert replay.pending is True
    assert calls == [("task-1", "stop review")]


def test_cancel_rejection_is_pending_until_terminal_reread(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )

    async def rejected(task_id: str, reason: str) -> dict[str, object]:
        del reason
        return {"taskId": task_id, "status": "rejected"}

    outcome = cancel_review(store, attempt_id, "stop review", core, rejected)
    assert outcome.pending is True
    assert outcome.status == "pending"
    assert outcome.rpc_result == "RPC_REJECTED"


def test_cancel_unexpected_error_uses_terminal_reread(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )

    async def errored(task_id: str, reason: str) -> dict[str, object]:
        del reason
        with sqlite3.connect(core) as conn:
            conn.execute(
                "UPDATE task_runs SET status='cancelled', ended_at=created_at+3000 WHERE task_id=?",
                (task_id,),
            )
            conn.commit()
        raise RuntimeError("unexpected transport boundary")

    outcome = cancel_review(store, attempt_id, "stop review", core, errored)
    assert outcome.pending is False
    assert outcome.status == "cancelled"
    assert outcome.rpc_result == "RPC_ERROR:RuntimeError"


def test_cancel_unexpected_error_replay_stays_pending_without_resend(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )
    calls = 0

    async def errored(task_id: str, reason: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        del task_id, reason
        raise RuntimeError("unexpected transport boundary")

    outcome = cancel_review(store, attempt_id, "stop review", core, errored)
    replay = cancel_review(store, attempt_id, "stop review", core, errored)
    assert outcome.pending is True
    assert replay.pending is True
    assert replay.rpc_result == "RPC_ERROR:RuntimeError"
    assert calls == 1


def test_bundle_mutation_verification_fails_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    bundle.chmod(0o755)
    (bundle / "test-evidence").chmod(0o644)
    (bundle / "test-evidence").write_text("mutated", encoding="utf-8")
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )
    assert (
        json.loads(store.evidence(attempt_id, "review_host_evidence"))["reason"] == "bundle_mutated"
    )


def test_queue_refuses_missing_or_forced_review_host_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, _bundle = _setup(campaign, tmp_path)
    run_dir = store.root / "hypotheses" / hypothesis.hypothesis_id / "attempts" / attempt_id / "run"
    with pytest.raises(StoreConflict):
        store.queue_run_request(attempt_id, "job-missing-review", run_dir, 30, 256)
    attempt = store.get_attempt(attempt_id)
    forced = replace(
        attempt,
        state=AttemptState.REVIEW_PASSED,
        review_verdict="PASS",
        review_commit=attempt.commit,
        review_spec_sha256=hypothesis.spec_sha256,
    )
    store.set_state(forced, event="test_forced_review_pass")
    with pytest.raises(StoreConflict):
        store.queue_run_request(attempt_id, "job-forced-review", run_dir, 30, 256)


def test_queue_refuses_forced_pass_with_fail_host_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(
        campaign, tmp_path, verdict="FAIL"
    )
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )
    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    forced = replace(
        attempt,
        state=AttemptState.REVIEW_PASSED,
        review_verdict="PASS",
        review_commit=attempt.commit,
        review_spec_sha256=hypothesis.spec_sha256,
    )
    store.set_state(forced, event="test_forced_review_pass")
    run_dir = store.root / "hypotheses" / hypothesis.hypothesis_id / "attempts" / attempt_id / "run"
    with pytest.raises(StoreConflict):
        store.queue_run_request(attempt_id, "job-fail-review", run_dir, 30, 256)


def test_self_report_submit_is_a_tombstone(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, _bundle = _setup(campaign, tmp_path)
    attempt = store.get_attempt(attempt_id)
    record = ReviewRecord(
        attempt_id,
        str(attempt.commit),
        hypothesis.spec_sha256,
        "PASS",
        (),
        "claude-opus-5",
        "claude-opus-5",
        "session",
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(StoreConflict, match="review-submit was removed"):
        store.submit_review(attempt_id, record)  # type: ignore[arg-type]


def test_all_decided_wake_requests_next_hypothesis_authoring(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    from gateway.research.wake import compose_wake

    store, _source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.FINISHED, "done")
    plan = compose_wake(store)
    assert plan is not None
    assert plan.state == "ALL_DECIDED"
    assert plan.hypothesis_id == "H0002"
    assert "author" in plan.message
    assert "freeze" in plan.message


def test_implemented_wake_requests_high_effort_review(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, _attempt_id, _bundle = _setup(campaign, tmp_path)
    from gateway.research.wake import compose_wake

    plan = compose_wake(store)
    assert plan is not None
    assert plan.state == "IMPLEMENTED"
    assert "effort=high" in plan.message
