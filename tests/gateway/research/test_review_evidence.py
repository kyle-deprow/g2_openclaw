"""Native Sol review evidence, immutable bundle, and state-transition tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Literal

import pytest
from gateway.openclaw_client import OpenClawTransportError
from gateway.research import review_evidence
from gateway.research.contracts import AttemptState, HypothesisSpec, ReviewEvidence, RunPlan
from gateway.research.review_evidence import (
    BundleError,
    ReviewEvidenceError,
    ReviewPending,
    ReviewReservation,
    ReviewUnresolved,
    acknowledge_review,
    cancel_review,
    collect_review,
    reconcile_review,
    reserve_review,
)
from gateway.research.store import ResearchStore, StoreConflict, canonical_wake_key

from tests.gateway.research.native_review_fixtures import (
    OWNER_RUN_ID,
    OWNER_SESSION,
    OWNER_THREAD_ID,
    ROTATED_OWNER_THREAD_ID,
    add_announce,
    create_native_stores,
    create_owner_database,
    end_owner_run,
    flush_owner_run,
    insert_owner_event,
    owner_started,
    prepare_review,
    set_owner_node,
)


def _setup(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> tuple[ResearchStore, Path, HypothesisSpec, str, Path]:
    from tests.gateway.research.native_review_fixtures import _setup as fixture_setup

    return fixture_setup(campaign, tmp_path)


def _queue(store: ResearchStore, attempt_id: str, job_id: str) -> object:
    attempt = store.get_attempt(attempt_id)
    run_dir = store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id / "run"
    plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    return store.queue_run_request(attempt_id, job_id, run_dir, 30, 256, run_plan=plan)


def _commit_source(source: Path, message: str) -> None:
    source.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=source, check=True)
    source.chmod(0o555)


def _owner_run_flushed(path: Path) -> bool:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM trajectory_runtime_events WHERE run_id=?", (OWNER_RUN_ID,)
            ).fetchone()
            is not None
        )


def _wake_started_at_ms(store: ResearchStore, attempt_id: str) -> int:
    """The owner run starts just after its wake was sent (the real order)."""

    _status, resume_seq = store.campaign()
    wake_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", resume_seq)
    if store.wake_row(wake_key) is None:
        assert store.reserve_wake(wake_key, attempt_id, "IMPLEMENTED", resume_seq)
        store.complete_wake(wake_key, OWNER_RUN_ID)
    wake = store.wake_row(wake_key)
    assert wake is not None
    sent = datetime.fromisoformat(str(wake["sent_at"]).replace("Z", "+00:00"))
    return int(sent.timestamp() * 1000) + 1


def _reserve_native(
    store: ResearchStore,
    attempt_id: str,
    bundle: Path,
    tmp_path: Path,
    *,
    flush: Literal["after", "before", "never"] = "after",
) -> ReviewReservation:
    """Reserve like the live owner turn: its own run is not yet flushed.

    ``after`` (default) flushes and ends the owner run once the reservation
    exists so a later reconcile can resolve it, ``before`` models a run whose
    start was already recorded (still open) at reserve time and ``never``
    leaves the run unflushed (its session node stays the running writer).
    """

    started_at_ms = _wake_started_at_ms(store, attempt_id)
    time.sleep(0.01)
    owner_database = create_owner_database(
        tmp_path / "owner.sqlite",
        started_at_ms=started_at_ms,
        owner_flushed=flush == "before",
        owner_ended_event=False,
    )
    _status, resume_seq = store.campaign()
    wake_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", resume_seq)
    reservation = reserve_review(
        store,
        attempt_id,
        bundle,
        OWNER_SESSION,
        wake_pending_key=wake_key,
        openclaw_database=owner_database,
    )
    if flush == "after" and not _owner_run_flushed(owner_database):
        flush_owner_run(owner_database, started_at_ms=started_at_ms)
    return reservation


def test_bundle_limits_are_unchanged() -> None:
    assert review_evidence.MAX_BUNDLE_BYTES == 256 * 1024 * 1024
    assert review_evidence.MAX_BUNDLE_FILE_BYTES == 8 * 1024 * 1024
    assert review_evidence.MAX_TEST_EVIDENCE_BYTES == 8 * 1024 * 1024


def test_bundle_digest_rejects_aggregate_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 4)
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", 8)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "diff.patch").write_bytes(b"patch!")
    (bundle / "instructions.md").write_bytes(b"iii")
    with pytest.raises(BundleError, match="review bundle exceeds its size limit"):
        review_evidence._bundle_digest(bundle)


def test_bundle_digest_enforces_per_file_limit_and_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 4)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "instructions.md").write_bytes(b"large")
    with pytest.raises(BundleError, match="bounded regular file"):
        review_evidence._bundle_digest(bundle)
    (bundle / "instructions.md").unlink()
    target = tmp_path / "target"
    target.write_bytes(b"safe")
    (bundle / "instructions.md").symlink_to(target)
    with pytest.raises(BundleError, match="symlink"):
        review_evidence._bundle_digest(bundle)


def test_reserved_bundle_rejects_untracked_nested_and_excluded_tampering(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _attempt_id, bundle, native = prepare_review(campaign, tmp_path)
    source = bundle / "source"
    bundle.chmod(0o755)
    source.chmod(0o755)
    source.mkdir(exist_ok=True)
    (source / "nested").mkdir()
    (source / "nested" / "untracked.py").write_text("forged\n", encoding="utf-8")
    (source / "nested" / "untracked.py").chmod(0o444)
    (source / "nested").chmod(0o555)
    source.chmod(0o555)
    bundle.chmod(0o555)
    result = collect_review(
        store, _attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED


def test_reserved_bundle_rejects_frozen_test_evidence_and_run_plan_mutation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, bundle, native = prepare_review(campaign, tmp_path)
    for relative, content in (
        ("test-evidence", b"mutated\n"),
        ("run-plan.json", b"{}\n"),
    ):
        path = bundle / relative
        path.chmod(0o644)
        path.write_bytes(content)
        path.chmod(0o444)
        bundle.chmod(0o555)
        result = collect_review(
            store, attempt_id, native.openclaw_database, native.codex_state_database
        )
        assert result.state is AttemptState.REVIEW_FAILED
        break


def test_failed_terminal_review_evidence_is_immutable_and_not_recollected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, verdict="FAIL", findings=("finding",)
    )
    first = collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert first.state is AttemptState.REVIEW_FAILED
    host = store.evidence(attempt_id, "review_host_evidence")
    child = native.child_rollout
    lines = child.read_text().splitlines()
    event = json.loads(lines[-1])
    event["payload"]["last_agent_message"] = event["payload"]["last_agent_message"].replace(
        '"FAIL"', '"PASS"'
    )
    lines[-1] = json.dumps(event)
    child.write_text("\n".join(lines) + "\n")
    second = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert second.state is AttemptState.REVIEW_FAILED
    assert store.evidence(attempt_id, "review_host_evidence") == host


@pytest.mark.parametrize(
    "relative,content",
    [("spec.json", b"{}\n"), ("containment-provenance.json", b"{}\n")],
)
def test_reserved_bundle_rejects_frozen_spec_and_provenance_mutation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    relative: str,
    content: bytes,
) -> None:
    store, attempt_id, bundle, native = prepare_review(campaign, tmp_path)
    path = bundle / relative
    bundle.chmod(0o755)
    path.chmod(0o644)
    path.write_bytes(content)
    path.chmod(0o444)
    bundle.chmod(0o555)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED


def test_reservation_builds_frozen_bundle_and_exact_native_spawn_arguments(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path)
    assert reservation.spawn_arguments["agent_type"] == "reviewer"
    assert reservation.spawn_arguments["fork_turns"] == "none"
    assert reservation.spawn_arguments["task_name"] == reservation.task_name
    assert (
        reservation.prompt_sha256
        == __import__("hashlib").sha256(reservation.spawn_arguments["message"].encode()).hexdigest()
    )
    assert json.loads((bundle / "spec.json").read_text()) == json.loads(hypothesis.spec_json)
    assert not (bundle / "source" / "tracked.txt").stat().st_mode & 0o222
    assert "containment-provenance.json" in (bundle / "instructions.md").read_text()
    assert _reserve_native(store, attempt_id, bundle, tmp_path) == reservation


def test_reservation_owner_replay_rejects_different_run_or_thread(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path)
    with pytest.raises(ReviewUnresolved, match="canonical"):
        reserve_review(
            store,
            attempt_id,
            bundle,
            OWNER_SESSION,
            wake_pending_key="wrong-wake",
            openclaw_database=tmp_path / "owner.sqlite",
        )


def test_reserve_requires_exact_completed_implemented_wake_context(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _status, resume_seq = store.campaign()
    wake_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", resume_seq)
    assert store.reserve_wake(wake_key, attempt_id, "IMPLEMENTED", resume_seq)
    store.complete_wake(wake_key, OWNER_RUN_ID)
    owner_database = create_owner_database(tmp_path / "owner.sqlite", owner_ended_event=False)
    reservation = reserve_review(
        store,
        attempt_id,
        bundle,
        OWNER_SESSION,
        wake_pending_key=wake_key,
        openclaw_database=owner_database,
    )
    assert reservation.owner_run_id == OWNER_RUN_ID
    assert reservation.owner_thread_id == OWNER_THREAD_ID


def test_reserve_rejects_completed_wake_from_prior_resume_sequence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _status, prior_resume_seq = store.campaign()
    prior_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", prior_resume_seq)
    assert store.reserve_wake(prior_key, attempt_id, "IMPLEMENTED", prior_resume_seq)
    store.complete_wake(prior_key, OWNER_RUN_ID)
    current_resume_seq = store.resume("new-owner-turn")
    assert current_resume_seq == prior_resume_seq + 1
    with pytest.raises(ReviewUnresolved, match="current canonical key"):
        reserve_review(
            store,
            attempt_id,
            bundle,
            OWNER_SESSION,
            wake_pending_key=prior_key,
            openclaw_database=create_owner_database(tmp_path / "owner.sqlite"),
        )


def test_reserve_rejects_an_owner_run_that_starts_after_the_reservation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    owner_database = create_owner_database(
        tmp_path / "owner.sqlite", started_at_ms=int(time.time() * 1000) + 10_000
    )
    _status, resume_seq = store.campaign()
    wake_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", resume_seq)
    assert store.reserve_wake(wake_key, attempt_id, "IMPLEMENTED", resume_seq)
    store.complete_wake(wake_key, OWNER_RUN_ID)
    with pytest.raises(ReviewUnresolved, match="starts after the reservation"):
        reserve_review(
            store,
            attempt_id,
            bundle,
            OWNER_SESSION,
            wake_pending_key=wake_key,
            openclaw_database=owner_database,
        )
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_reservation")


def test_reserve_defers_the_owner_thread_while_the_owner_run_is_unflushed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert reservation.owner_run_id == OWNER_RUN_ID
    assert reservation.owner_thread_id is None
    stored = json.loads(store.evidence(attempt_id, "review_reservation"))
    assert stored["owner_run_id"] == OWNER_RUN_ID and stored["owner_thread_id"] is None
    # The exact native spawn arguments are the same as for a flushed owner run.
    assert reservation.spawn_arguments["agent_type"] == "reviewer"
    assert reservation.spawn_arguments["fork_turns"] == "none"
    assert reservation.spawn_arguments["task_name"] == reservation.task_name
    assert set(reservation.spawn_arguments) == {"agent_type", "fork_turns", "message", "task_name"}
    assert str(bundle.resolve()) in reservation.spawn_arguments["message"]
    assert review_evidence._reservation(store, attempt_id) == reservation


def test_reserve_binds_the_thread_when_the_owner_run_is_already_flushed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="before")
    assert reservation.owner_run_id == OWNER_RUN_ID
    assert reservation.owner_thread_id == OWNER_THREAD_ID
    assert _reserve_native(store, attempt_id, bundle, tmp_path, flush="before") == reservation


def test_deferred_reservation_replays_idempotently_once_the_run_is_flushed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert reservation.owner_thread_id is None
    assert _reserve_native(store, attempt_id, bundle, tmp_path, flush="never") == reservation
    flush_owner_run(tmp_path / "owner.sqlite", started_at_ms=int(time.time() * 1000) - 500)
    events = store.events()
    replay = _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert replay == reservation
    assert replay.owner_thread_id is None
    assert store.events() == events


def test_resolved_reservation_replay_conflicts_when_the_thread_differs(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="before")
    assert reservation.owner_thread_id == OWNER_THREAD_ID
    with sqlite3.connect(tmp_path / "owner.sqlite") as connection:
        for row in connection.execute(
            "SELECT session_id, seq, event_json FROM trajectory_runtime_events WHERE run_id=?",
            (OWNER_RUN_ID,),
        ).fetchall():
            event = json.loads(row[2])
            event["data"]["threadId"] = "a-different-owner-thread"
            connection.execute(
                "UPDATE trajectory_runtime_events SET event_json=? WHERE session_id=? AND seq=?",
                (json.dumps(event), row[0], row[1]),
            )
    with pytest.raises(StoreConflict, match="owner run/thread differs"):
        _reserve_native(store, attempt_id, bundle, tmp_path, flush="before")


def test_resolved_reservation_replay_fails_closed_if_the_run_disappears(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path, flush="before")
    with sqlite3.connect(tmp_path / "owner.sqlite") as connection:
        connection.execute("DELETE FROM trajectory_runtime_events WHERE run_id=?", (OWNER_RUN_ID,))
    with pytest.raises(StoreConflict, match="owner run/thread differs"):
        _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")


def _reserve_expecting_refusal(
    store: ResearchStore, attempt_id: str, bundle: Path, tmp_path: Path
) -> None:
    with pytest.raises(ReviewUnresolved, match="owner run/thread is unresolved"):
        _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_reservation")
    assert not bundle.exists()


def test_reserve_rejects_a_present_owner_start_with_the_wrong_model(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    insert_owner_event(
        database,
        run_id=OWNER_RUN_ID,
        event=owner_started(OWNER_THREAD_ID, model="gpt-5.4"),
        created_at_ms=int(time.time() * 1000) - 1_000,
    )
    _reserve_expecting_refusal(store, attempt_id, bundle, tmp_path)


def test_reserve_rejects_duplicate_owner_start_events(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    for offset in (1_000, 900):
        insert_owner_event(
            database,
            run_id=OWNER_RUN_ID,
            event=owner_started(OWNER_THREAD_ID),
            created_at_ms=int(time.time() * 1000) - offset,
        )
    _reserve_expecting_refusal(store, attempt_id, bundle, tmp_path)


def test_reserve_rejects_an_owner_end_without_a_start(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    insert_owner_event(
        database,
        run_id=OWNER_RUN_ID,
        event={
            "type": "session.ended",
            "data": {"threadId": OWNER_THREAD_ID, "status": "success"},
        },
        created_at_ms=int(time.time() * 1000) - 500,
    )
    _reserve_expecting_refusal(store, attempt_id, bundle, tmp_path)


def test_reserve_rejects_a_conflicting_thread_between_owner_start_and_end(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    now = int(time.time() * 1000)
    insert_owner_event(
        database,
        run_id=OWNER_RUN_ID,
        event=owner_started(OWNER_THREAD_ID),
        created_at_ms=now - 1_000,
    )
    insert_owner_event(
        database,
        run_id=OWNER_RUN_ID,
        event={"type": "session.ended", "data": {"threadId": "another-thread"}},
        created_at_ms=now - 500,
    )
    _reserve_expecting_refusal(store, attempt_id, bundle, tmp_path)


def test_reserve_rejects_opened_wake_even_if_it_has_a_run_id(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _status, resume_seq = store.campaign()
    wake_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", resume_seq)
    assert store.reserve_wake(wake_key, attempt_id, "OPENED", resume_seq)
    store.complete_wake(wake_key, OWNER_RUN_ID)
    with pytest.raises(ReviewUnresolved, match="does not bind the IMPLEMENTED"):
        reserve_review(
            store,
            attempt_id,
            bundle,
            OWNER_SESSION,
            wake_pending_key=wake_key,
            openclaw_database=create_owner_database(tmp_path / "owner.sqlite"),
        )


def test_source_and_spec_mutation_is_rejected_on_reservation_replay(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path)
    bundle.chmod(0o755)
    (bundle / "spec.json").chmod(0o644)
    (bundle / "spec.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BundleError):
        _reserve_native(store, attempt_id, bundle, tmp_path)


def test_blocked_source_changes_fail_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / ".env").write_text("secret\n", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "dump.db").write_bytes(b"secret")
    _commit_source(source, "add blocked files")
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    with pytest.raises(BundleError, match="changed data or credential path"):
        _reserve_native(store, attempt_id, bundle, tmp_path)


@pytest.mark.parametrize(
    "campaign",
    [((".env.example", "EXAMPLE=1\n"), ("data/README.md", "fixture data docs\n"))],
    indirect=True,
)
def test_unchanged_blocked_baseline_is_excluded_but_excluded_tampering_fails(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, bundle, native = prepare_review(campaign, tmp_path)
    excluded = bundle / "source" / "EXCLUDED"
    assert excluded.is_file()
    bundle.chmod(0o755)
    excluded.chmod(0o644)
    excluded.write_text("forged\n", encoding="utf-8")
    excluded.chmod(0o444)
    bundle.chmod(0o555)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED


@pytest.mark.parametrize("relative", [".env.example", "data/README.md"])
def test_changed_or_executable_blocked_baseline_is_refused(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    relative: str,
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    path = source / relative
    if relative.startswith("data/"):
        path.parent.mkdir(parents=True, exist_ok=True)
        (source / "tracked.txt").rename(path)
    else:
        path.write_text("blocked\n", encoding="utf-8")
        path.chmod(0o755)
    _commit_source(source, f"tamper blocked {relative}")
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    with pytest.raises(BundleError, match="changed data or credential path"):
        _reserve_native(store, attempt_id, bundle, tmp_path)


def test_native_collect_pass_is_idempotent_and_records_host_identity(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, announce="auto")
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_PASSED
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert host["model_observed"] == "gpt-5.6-sol"
    assert host["reasoning_effort_observed"] == "xhigh"
    assert host["child_thread_id"] == native.child_thread_id
    assert host["announce_run_id"] == native.announce_run_id
    assert host["announce_status"] == "succeeded"
    assert host["spawn_call_id"] == "call-reviewer-0"
    assert "child_session_key" not in host and "child_run_id" not in host
    events = store.events()
    assert (
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
        == result
    )
    assert store.events() == events


def test_native_collect_pending_until_official_child_terminal(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")
    with pytest.raises(ReviewPending):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.get_attempt(attempt_id).state is AttemptState.IMPLEMENTED


@pytest.mark.parametrize("field,value", (("model", "gpt-5.4"), ("effort", "high")))
def test_native_collect_rejects_wrong_model_or_effort(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, field: str, value: str
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    column = "model" if field == "model" else "reasoning_effort"
    with sqlite3.connect(native.codex_state_database) as connection:
        connection.execute(
            f"UPDATE threads SET {column}=? WHERE thread_source='subagent'", (value,)
        )
    with pytest.raises(ReviewUnresolved, match="correlation"):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"


def test_native_collect_records_bundle_mutation_as_failed_review(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, bundle, native = prepare_review(campaign, tmp_path)
    bundle.chmod(0o755)
    (bundle / "instructions.md").chmod(0o644)
    (bundle / "instructions.md").write_text("mutated", encoding="utf-8")
    (bundle / "instructions.md").chmod(0o444)
    bundle.chmod(0o555)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED
    assert json.loads(store.evidence(attempt_id, "review"))["findings"] == ["bundle_mutated"]


@pytest.mark.parametrize("suffix", [" trailing", "\nprose"])
def test_native_collect_rejects_non_bare_verdict(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, suffix: str
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    rollout = native.child_rollout
    lines = rollout.read_text().splitlines()
    event = json.loads(lines[-1])
    event["payload"]["last_agent_message"] += suffix
    lines[-1] = json.dumps(event)
    rollout.write_text("\n".join(lines) + "\n")
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED


def test_native_collect_rejects_duplicate_verdict_keys(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    rollout = native.child_rollout
    lines = rollout.read_text().splitlines()
    event = json.loads(lines[-1])
    event["payload"]["last_agent_message"] = '{"verdict":"PASS","verdict":"PASS"}'
    lines[-1] = json.dumps(event)
    rollout.write_text("\n".join(lines) + "\n")
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED


def test_native_reconcile_recovers_exact_official_child_ack(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path)
    # The ACK needs no completion callback: the child is still running here.
    native = create_native_stores(store, attempt_id, bundle, tmp_path, status="running")
    ack = reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert ack is not None
    assert ack.child_thread_id == native.child_thread_id
    assert ack.child_session_key is None and ack.run_id is None
    assert ack.owner_run_id == reservation.owner_run_id
    stored = json.loads(store.evidence(attempt_id, "review_ack"))
    assert set(stored) == {
        "mode",
        "run_timeout_seconds",
        "acked_at",
        "owner_session_key",
        "owner_run_id",
        "owner_thread_id",
        "child_thread_id",
    }
    with pytest.raises(ReviewEvidenceError):
        acknowledge_review(store, attempt_id, "run", None, child_thread_id="forged-child")


def test_native_collect_without_completion_callback_proceeds_on_rollout_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    # In-turn collection: the callback run cannot see its own events yet.
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path)
    native = create_native_stores(store, attempt_id, bundle, tmp_path, announce=None)
    reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_PASSED
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert host["announce_run_id"] is None and host["announce_status"] is None
    assert host["owner_thread_id"] == OWNER_THREAD_ID
    assert host["child_thread_id"] == native.child_thread_id
    # A later-arriving, consistent callback does not change the stored verdict.
    add_announce(
        native.openclaw_database,
        owner_thread_id=OWNER_THREAD_ID,
        child_thread_id=native.child_thread_id,
        status="succeeded",
        at_ms=int(time.time() * 1000),
    )
    assert (
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
        == result
    )


def test_native_collect_without_callback_still_pends_on_a_running_child(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, status="running", announce=None
    )
    with pytest.raises(ReviewPending):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.get_attempt(attempt_id).state is AttemptState.IMPLEMENTED
    assert store.campaign()[0] != "PAUSED"


def test_native_collect_accepts_a_consistent_callback(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_PASSED
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert host["announce_run_id"] == native.announce_run_id


@pytest.mark.parametrize(
    "setup",
    ["status_mismatch", "parent_thread_mismatch", "duplicate"],
)
def test_native_collect_rejects_an_inconsistent_or_ambiguous_callback(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, setup: str
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, announce={"status_mismatch": "failed"}.get(setup, "auto")
    )
    if setup == "parent_thread_mismatch":
        with sqlite3.connect(native.openclaw_database) as connection:
            connection.execute(
                "UPDATE trajectory_runtime_events SET run_id=replace(run_id, ?, ?) "
                "WHERE run_id LIKE 'announce:%'",
                (f":{OWNER_THREAD_ID}:", ":some-other-owner-thread:"),
            )
            for row in connection.execute(
                "SELECT session_id, seq, event_json FROM trajectory_runtime_events "
                "WHERE run_id LIKE 'announce:%'"
            ).fetchall():
                event = json.loads(row[2])
                event["runId"] = event["runId"].replace(
                    f":{OWNER_THREAD_ID}:", ":some-other-owner-thread:"
                )
                connection.execute(
                    "UPDATE trajectory_runtime_events SET event_json=? "
                    "WHERE session_id=? AND seq=?",
                    (json.dumps(event), row[0], row[1]),
                )
    if setup == "duplicate":
        add_announce(
            native.openclaw_database,
            owner_thread_id=OWNER_THREAD_ID,
            child_thread_id=native.child_thread_id,
            status="failed",
            at_ms=int(time.time() * 1000),
        )
    with pytest.raises(ReviewUnresolved, match="correlation"):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"
    assert store.get_attempt(attempt_id).state is AttemptState.IMPLEMENTED


def test_terminal_fail_stays_immutable_when_a_callback_later_disagrees(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, status="failed", announce=None
    )
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED
    add_announce(
        native.openclaw_database,
        owner_thread_id=OWNER_THREAD_ID,
        child_thread_id=native.child_thread_id,
        status="succeeded",
        at_ms=int(time.time() * 1000),
    )
    assert (
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
        == result
    )
    assert json.loads(store.evidence(attempt_id, "review"))["verdict"] == "FAIL"


def test_reconcile_resolves_a_deferred_thread_from_the_flushed_run(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="after")
    assert reservation.owner_thread_id is None
    native = create_native_stores(store, attempt_id, bundle, tmp_path, status="running")
    ack = reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert ack is not None
    assert ack.owner_run_id == OWNER_RUN_ID
    assert ack.owner_thread_id == OWNER_THREAD_ID
    assert json.loads(store.evidence(attempt_id, "review_ack"))["owner_thread_id"] == (
        OWNER_THREAD_ID
    )
    # The immutable reservation is never rewritten with the resolved thread.
    assert json.loads(store.evidence(attempt_id, "review_reservation"))["owner_thread_id"] is None
    assert review_evidence._reservation(store, attempt_id).owner_thread_id is None
    assert (
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
        == ack
    )


def test_reconcile_is_pending_while_the_deferred_owner_run_is_unflushed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert reservation.owner_thread_id is None
    native = create_native_stores(store, attempt_id, bundle, tmp_path)
    with pytest.raises(ReviewPending):
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] != "PAUSED"
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_ack")
    # Once the host flushes the (ended) run, reconciliation completes.
    flush_owner_run(native.openclaw_database, started_at_ms=int(time.time() * 1000) - 500)
    ack = reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert ack is not None and ack.owner_thread_id == OWNER_THREAD_ID


def test_reconcile_is_pending_while_the_deferred_owner_run_is_open(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    native = create_native_stores(store, attempt_id, bundle, tmp_path)
    flush_owner_run(
        native.openclaw_database,
        started_at_ms=int(time.time() * 1000) - 500,
        owner_ended_event=False,
    )
    with pytest.raises(ReviewPending):
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] != "PAUSED"


def test_reconcile_pauses_when_the_deferred_owner_run_has_the_wrong_model(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    native = create_native_stores(store, attempt_id, bundle, tmp_path)
    insert_owner_event(
        native.openclaw_database,
        run_id=OWNER_RUN_ID,
        event=owner_started(OWNER_THREAD_ID, model="gpt-5.4"),
        created_at_ms=int(time.time() * 1000) - 500,
    )
    with pytest.raises(ReviewUnresolved):
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"


def test_collect_is_unresolved_when_the_owner_run_vanishes_after_the_ack(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    assert review_evidence._reservation(store, attempt_id).owner_thread_id is None
    store.evidence(attempt_id, "review_ack")
    with sqlite3.connect(native.openclaw_database) as connection:
        connection.execute("DELETE FROM trajectory_runtime_events WHERE run_id=?", (OWNER_RUN_ID,))
    with pytest.raises(ReviewUnresolved, match="vanished"):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"
    assert store.get_attempt(attempt_id).state is AttemptState.IMPLEMENTED


def test_reconcile_with_a_resolved_thread_is_unresolved_when_the_run_vanishes(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="before")
    assert reservation.owner_thread_id == OWNER_THREAD_ID
    native = create_native_stores(store, attempt_id, bundle, tmp_path, status="running")
    with sqlite3.connect(native.openclaw_database) as connection:
        connection.execute("DELETE FROM trajectory_runtime_events WHERE run_id=?", (OWNER_RUN_ID,))
    with pytest.raises(ReviewUnresolved, match="vanished"):
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"


def test_cancel_resolves_a_deferred_thread_and_is_unresolved_while_unrecorded(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    native = create_native_stores(store, attempt_id, bundle, tmp_path, status="running")
    calls: list[str] = []

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        calls.append(run_id)
        return {"ok": True, "aborted": True, "runIds": [run_id]}

    unrecorded = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert unrecorded.pending and unrecorded.rpc_result == "not_requested"
    assert unrecorded.status == "unresolved"
    assert calls == []
    flush_owner_run(native.openclaw_database, started_at_ms=int(time.time() * 1000) - 500)
    flushed = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert flushed.pending is True
    assert flushed.task_id == native.child_thread_id
    assert calls == [OWNER_RUN_ID]


def _reserve_with_node(
    store: ResearchStore,
    attempt_id: str,
    bundle: Path,
    tmp_path: Path,
    **node: object,
) -> ReviewReservation:
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    set_owner_node(database, **node)  # type: ignore[arg-type]
    return _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")


def _assert_reserve_refused_cleanly(
    store: ResearchStore, attempt_id: str, bundle: Path, tmp_path: Path, match: str, **node: object
) -> None:
    with pytest.raises(ReviewUnresolved, match=match):
        _reserve_with_node(store, attempt_id, bundle, tmp_path, **node)
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_reservation")
    assert not bundle.exists()


def test_reserve_defers_only_for_the_running_active_writer_of_the_wake_run(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_with_node(
        store, attempt_id, bundle, tmp_path, status="running", active_writer_run_id=OWNER_RUN_ID
    )
    assert reservation.owner_thread_id is None
    assert reservation.owner_run_id == OWNER_RUN_ID


def test_reserve_refuses_an_unrecorded_run_that_is_not_the_active_writer(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _assert_reserve_refused_cleanly(
        store,
        attempt_id,
        bundle,
        tmp_path,
        "not the active running writer",
        status="running",
        active_writer_run_id="some-other-run",
    )
    _assert_reserve_refused_cleanly(
        store,
        attempt_id,
        bundle,
        tmp_path,
        "not the active running writer",
        status="running",
        active_writer_run_id=None,
    )


@pytest.mark.parametrize("status", ["done", "failed", "killed", "timeout"])
def test_reserve_refuses_an_unrecorded_run_whose_session_is_not_running(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    status: str,
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _assert_reserve_refused_cleanly(
        store,
        attempt_id,
        bundle,
        tmp_path,
        "not the active running writer",
        status=status,
        active_writer_run_id=OWNER_RUN_ID,
    )


@pytest.mark.parametrize("entry_json", [None, "not json", "[]", '{"status":"running"}x'])
def test_reserve_refuses_an_unrecorded_run_with_a_missing_or_malformed_node_entry(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    entry_json: str | None,
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE session_nodes SET entry_json=?", (entry_json,))
    with pytest.raises(ReviewUnresolved, match="owner writer is unresolved"):
        _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_reservation")
    assert not bundle.exists()


def test_reserve_refuses_a_recorded_owner_run_that_has_already_ended(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    started_at_ms = _wake_started_at_ms(store, attempt_id)
    time.sleep(0.01)
    database = create_owner_database(
        tmp_path / "owner.sqlite", started_at_ms=started_at_ms, owner_ended_event=False
    )
    end_owner_run(database, ended_at_ms=started_at_ms + 5)
    with pytest.raises(ReviewUnresolved, match="already ended"):
        _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_reservation")
    assert not bundle.exists()


def test_ended_owner_run_does_not_break_replay_of_an_existing_reservation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="after")
    # The run is now flushed and ended; its node is done.  Replay is idempotent.
    assert _reserve_native(store, attempt_id, bundle, tmp_path, flush="never") == reservation


def test_deferred_run_that_later_starts_after_the_reservation_pauses_at_reconcile(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert reservation.owner_thread_id is None
    native = create_native_stores(store, attempt_id, bundle, tmp_path)
    late = int(time.time() * 1000) + 60_000
    flush_owner_run(native.openclaw_database, started_at_ms=late)
    with pytest.raises(ReviewUnresolved):
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_ack")


def test_deferred_run_that_appears_only_under_another_session_pauses_at_reconcile(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert reservation.owner_thread_id is None
    native = create_native_stores(store, attempt_id, bundle, tmp_path)
    insert_owner_event(
        native.openclaw_database,
        run_id=OWNER_RUN_ID,
        event=owner_started(OWNER_THREAD_ID),
        created_at_ms=int(time.time() * 1000) - 500,
        session_id="foreign-session",
        session_key="agent:foreign:session",
    )
    with pytest.raises(ReviewUnresolved):
        reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.campaign()[0] == "PAUSED"


def test_reserve_refuses_a_wake_run_already_recorded_only_under_another_session(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    database = create_owner_database(tmp_path / "owner.sqlite", owner_flushed=False)
    insert_owner_event(
        database,
        run_id=OWNER_RUN_ID,
        event=owner_started(OWNER_THREAD_ID),
        created_at_ms=int(time.time() * 1000) - 500,
        session_id="foreign-session",
        session_key="agent:foreign:session",
    )
    with pytest.raises(ReviewUnresolved, match="owner run/thread is unresolved"):
        _reserve_native(store, attempt_id, bundle, tmp_path, flush="never")
    assert not bundle.exists()


def test_full_review_flow_with_the_owner_run_flushed_at_reserve(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, owner_flushed_at_reserve=True, announce="auto"
    )
    reservation = review_evidence._reservation(store, attempt_id)
    assert reservation.owner_thread_id == OWNER_THREAD_ID
    ack = json.loads(store.evidence(attempt_id, "review_ack"))
    assert ack["owner_thread_id"] == OWNER_THREAD_ID
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_PASSED
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert host["owner_thread_id"] == OWNER_THREAD_ID
    assert host["announce_run_id"] == native.announce_run_id


def test_cancel_with_the_owner_run_flushed_at_reserve(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, status="running", owner_flushed_at_reserve=True
    )
    assert review_evidence._reservation(store, attempt_id).owner_thread_id == OWNER_THREAD_ID
    calls: list[str] = []

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        calls.append(run_id)
        return {"ok": True, "aborted": False, "runIds": []}

    outcome = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert outcome.pending and outcome.task_id == native.child_thread_id
    assert calls == [OWNER_RUN_ID]


def test_default_collect_models_the_in_turn_case_with_no_callback_run_yet(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    assert native.announce_run_id is None
    with sqlite3.connect(native.openclaw_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM trajectory_runtime_events WHERE run_id LIKE 'announce:%'"
        ).fetchone() == (0,)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_PASSED


def test_native_collect_failed_announce_without_a_terminal_marker_is_a_host_failure(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, status="running", announce="failed"
    )
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert host["reason"] == "failed"
    assert host["announce_status"] == "failed"
    assert host["child_terminal_state"] == "failed"


def test_native_collect_failed_announce_with_a_mid_line_tail_stays_pending(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, status="running", announce="failed"
    )
    with native.child_rollout.open("a") as stream:
        stream.write('{"type":"event_msg","payload":{"type":"token_cou')
    with pytest.raises(ReviewPending):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    assert store.get_attempt(attempt_id).state is AttemptState.IMPLEMENTED


def test_native_collect_succeeded_announce_never_completes_a_missing_marker(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(
        campaign, tmp_path, status="running", announce="succeeded"
    )
    with pytest.raises(ReviewPending):
        collect_review(store, attempt_id, native.openclaw_database, native.codex_state_database)


def test_native_collect_failed_child_is_a_host_failure_without_a_parsed_verdict(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path)
    native = create_native_stores(store, attempt_id, bundle, tmp_path, status="failed")
    reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_FAILED
    review = json.loads(store.evidence(attempt_id, "review"))
    assert review["findings"] == ["failed"]
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert host["task_status"] == "unresolved"
    assert host["verdict_json"] == ""
    assert host["reason"] == "failed"
    assert host["announce_status"] == "failed"
    assert host["child_thread_id"] == native.child_thread_id


def test_native_collect_after_rotated_owner_thread_callback(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, announce="auto")
    assert native.announce_run_id is not None
    assert ROTATED_OWNER_THREAD_ID != OWNER_THREAD_ID
    result = collect_review(
        store, attempt_id, native.openclaw_database, native.codex_state_database
    )
    assert result.state is AttemptState.REVIEW_PASSED


def test_native_cancel_calls_chat_abort_once_and_pending_when_child_still_running(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")
    calls: list[tuple[str, str, str]] = []

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        calls.append((owner_session, agent_id, run_id))
        raise OpenClawTransportError("lost after send")

    first = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    replay = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert first.pending is True and replay.pending is True
    assert calls == [(OWNER_SESSION, "research-orchestrator", OWNER_RUN_ID)]


def test_native_cancel_malformed_or_mismatched_reply_is_pending_without_resend(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")
    calls: list[tuple[str, str, str]] = []

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        calls.append((owner_session, agent_id, run_id))
        return {"status": "accepted", "taskId": "wrong-child"}

    first = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    replay = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert first.pending and replay.pending
    assert calls == [(OWNER_SESSION, "research-orchestrator", OWNER_RUN_ID)]


def test_native_cancel_inactive_owner_run_reports_no_supported_child_cancel(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")
    calls: list[tuple[str, str, str]] = []

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        calls.append((owner_session, agent_id, run_id))
        return {"ok": True, "aborted": False, "runIds": []}

    args = (store, attempt_id, "stop review", native.openclaw_database, request)
    first = cancel_review(*args, native.codex_state_database)
    replay = cancel_review(*args, native.codex_state_database)
    assert first.pending and replay.pending
    assert first.status == "pending"
    assert first.rpc_result == review_evidence.CANCEL_OWNER_INACTIVE
    assert replay.rpc_result == review_evidence.CANCEL_OWNER_INACTIVE
    assert calls == [(OWNER_SESSION, "research-orchestrator", OWNER_RUN_ID)]
    assert store.campaign()[0] == "PAUSED"
    assert store.get_attempt(attempt_id).state is AttemptState.IMPLEMENTED


def test_native_cancel_aborted_owner_run_stays_pending_until_child_terminal(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        return {"ok": True, "aborted": True, "runIds": [run_id]}

    outcome = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert outcome.pending is True
    assert outcome.rpc_result == review_evidence.CANCEL_ABORTED


def test_native_cancel_malformed_chat_abort_shape_is_pending(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")

    async def request(owner_session: str, agent_id: str, run_id: str) -> dict[str, object]:
        return {"ok": True, "aborted": "yes"}

    outcome = cancel_review(
        store,
        attempt_id,
        "stop review",
        native.openclaw_database,
        request,
        native.codex_state_database,
    )
    assert outcome.pending is True
    assert outcome.rpc_result == "MALFORMED_RESPONSE"


def test_historical_reservation_is_readable_but_not_reconciled(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    review_evidence.build_review_bundle(store, attempt_id, bundle)
    payload = {
        "attempt_id": attempt_id,
        "commit": store.get_attempt(attempt_id).commit,
        "hypothesis_spec_sha256": store.get_hypothesis("H0001").spec_sha256,
        "bundle_dir": str(bundle.resolve()),
        "bundle_sha256": review_evidence._bundle_digest(bundle),
        "reserved_at": "2026-01-01T00:00:00Z",
        "owner_session_key": OWNER_SESSION,
        "label": "historical",
        "reservation_nonce": "historical",
    }
    store.insert_review_evidence(
        attempt_id, "review_reservation", json.dumps(payload), "review_reserved"
    )
    with pytest.raises(ReviewUnresolved, match="historical ACP"):
        reconcile_review(
            store, attempt_id, tmp_path / "missing.sqlite", tmp_path / "missing-state.sqlite"
        )


def test_queue_refuses_forced_review_pass_without_native_host_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, _bundle = _setup(campaign, tmp_path)
    with pytest.raises(StoreConflict):
        _queue(store, attempt_id, "job-missing-review")
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
        _queue(store, attempt_id, "job-forced-review")


def test_self_report_submit_remains_a_tombstone(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, _bundle = _setup(campaign, tmp_path)
    attempt = store.get_attempt(attempt_id)
    record = ReviewEvidence(
        attempt_id,
        str(attempt.commit),
        hypothesis.spec_sha256,
        "PASS",
        (),
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        "child-session",
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(StoreConflict, match="review-submit was removed"):
        store.submit_review(attempt_id, record)


def test_wake_asks_for_native_sol_xhigh_reviewer(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, _attempt_id, _bundle = _setup(campaign, tmp_path)
    from gateway.research.wake import compose_wake

    plan = compose_wake(store)
    assert plan is not None
    assert "gpt-5.6-sol" in plan.message
    assert "xhigh" in plan.message
    assert "spawn_agent" in plan.message
