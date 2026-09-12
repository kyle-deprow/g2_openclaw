from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.research.contracts import (
    AttemptDecision,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisSpec,
)
from gateway.research.store import OwnerLockHeld, ResearchStore, StoreConflict

from tests.gateway.research.conftest import implementation, review, run_plan, verified_review


def test_store_evidence_is_idempotent_and_repairs_projection(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    record = implementation(attempt.attempt_id, "a" * 40)
    # The fixture's actual commit is the implementation identity.
    record = record.__class__(
        attempt.attempt_id,
        store.attempts_for(hypothesis.hypothesis_id)[0].commit or "a" * 40,
        record.targets_argv,
        record.test_evidence_path,
        record.reported_coder_model,
        record.coder_effort,
        record.coder_service_tier,
        record.submitted_at,
    )
    plan = run_plan(store, attempt, record)
    store.submit_implementation(attempt.attempt_id, record, run_plan=plan)
    projection = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "implementation.json"
    )
    projection.unlink()
    assert (
        store.submit_implementation(attempt.attempt_id, record, run_plan=plan).state
        == AttemptState.IMPLEMENTED
    )
    assert projection.is_file()
    with pytest.raises(StoreConflict):
        store.submit_implementation(
            attempt.attempt_id,
            record.__class__(
                attempt.attempt_id,
                record.commit,
                ("different",),
                record.test_evidence_path,
                record.reported_coder_model,
                record.coder_effort,
                record.coder_service_tier,
                record.submitted_at,
            ),
            run_plan=plan,
        )


def test_submit_requires_plan_and_rejects_primary_or_unbound_spec_binding(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    record = implementation(attempt.attempt_id, commit)
    with pytest.raises(StoreConflict, match="run plan evidence"):
        store.submit_implementation(attempt.attempt_id, record)

    plan = run_plan(store, attempt, record)
    changed_record = replace(record, targets_argv=("python", "-m", "different"))
    changed_plan = replace(
        plan,
        implementation_sha256=hashlib.sha256(changed_record.to_json().encode()).hexdigest(),
    )
    with pytest.raises(StoreConflict, match="primary scenario argv"):
        store.submit_implementation(attempt.attempt_id, changed_record, run_plan=changed_plan)

    unbound_plan = replace(
        plan,
        scenarios=(
            plan.scenarios[0].__class__(
                plan.scenarios[0].scenario_id,
                plan.scenarios[0].targets_argv,
                "c999",
                "d" * 64,
            ),
        ),
    )
    with pytest.raises(StoreConflict, match="scenario spec does not match evidence"):
        store.submit_implementation(attempt.attempt_id, record, run_plan=unbound_plan)


def test_frozen_hypothesis_and_closed_attempt_are_sqlite_immutable(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    with pytest.raises(sqlite3.DatabaseError), store._connect() as conn:
        conn.execute("UPDATE hypotheses SET spec_json='changed' WHERE hypothesis_id='H0001'")
    with pytest.raises(sqlite3.DatabaseError), store._connect() as conn:
        conn.execute(
            "UPDATE hypotheses SET panel_sha256=? WHERE hypothesis_id='H0001'", ("f" * 64,)
        )


def test_owner_lock_and_closed_attempt_trigger(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    second = ResearchStore(store.root)
    store.acquire_owner_lock()
    with pytest.raises(OwnerLockHeld):
        second.acquire_owner_lock()
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    impl = implementation(attempt.attempt_id, "a" * 40)
    store.submit_implementation(attempt.attempt_id, impl, run_plan=run_plan(store, attempt, impl))
    verified_review(
        store,
        review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256, "FAIL"),
    )
    store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")
    with pytest.raises(sqlite3.DatabaseError), store._connect() as conn:
        conn.execute(
            "UPDATE attempts SET decision='FINISH' WHERE attempt_id=?", (attempt.attempt_id,)
        )
    store.release_owner_lock()


def test_review_is_idempotent_repairs_projection_and_is_insert_only(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    impl = implementation(attempt.attempt_id, "a" * 40)
    store.submit_implementation(attempt.attempt_id, impl, run_plan=run_plan(store, attempt, impl))
    record = review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256)
    verified_review(store, record)
    projection = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "review.json"
    )
    projection.unlink()
    verified_review(store, record)
    assert projection.is_file()
    assert any(event.kind == "projection_repaired" for event in store.events())
    with pytest.raises(StoreConflict):
        verified_review(
            store,
            record.__class__(
                record.attempt_id,
                record.commit,
                record.spec_sha256,
                record.verdict,
                ("different",),
                record.reported_reviewer_model,
                record.reported_reviewer_actual_model,
                record.acp_session_id,
                record.submitted_at,
            ),
        )
    with pytest.raises(sqlite3.DatabaseError), store._connect() as conn:
        conn.execute(
            "DELETE FROM attempt_evidence WHERE attempt_id=? AND kind='review'",
            (attempt.attempt_id,),
        )


def test_reconcile_repairs_missing_projection_without_launching(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    record = implementation(attempt.attempt_id, "a" * 40)
    store.submit_implementation(
        attempt.attempt_id, record, run_plan=run_plan(store, attempt, record)
    )
    projection = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "implementation.json"
    )
    projection.unlink()
    assert store.repair_projections() == 2
    assert projection.is_file()


def test_historical_hypothesis_remains_readable_without_spec_set_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    set_projection = (
        store.root / "hypotheses" / hypothesis.hypothesis_id / "evaluation-spec-set.json"
    )
    with store._connect() as conn:
        conn.execute("DROP TRIGGER immutable_hypothesis_evidence_delete")
        conn.execute(
            "DELETE FROM hypothesis_evidence WHERE hypothesis_id=? AND kind='evaluation_spec_set'",
            (hypothesis.hypothesis_id,),
        )
        conn.execute(
            """
            CREATE TRIGGER immutable_hypothesis_evidence_delete
            BEFORE DELETE ON hypothesis_evidence
            BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END
            """
        )
        conn.commit()
    set_projection.unlink()
    assert store.get_hypothesis(hypothesis.hypothesis_id).hypothesis_id == hypothesis.hypothesis_id
    with pytest.raises(ValueError):
        store.evaluation_spec_set(hypothesis.hypothesis_id)


def test_run_reservation_is_atomic_before_worker_launch(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    record = implementation(attempt.attempt_id, "a" * 40)
    plan = run_plan(store, attempt, record)
    store.submit_implementation(attempt.attempt_id, record, run_plan=plan)
    verified_review(store, review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256))
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    store.queue_run_request(attempt.attempt_id, "job-reserved", run_dir, 30, 256, run_plan=plan)
    store.acquire_run_lock()
    try:
        running = store.claim_queued_job(attempt.attempt_id, "job-reserved")
    finally:
        store.release_run_lock()
    assert running.state == AttemptState.RUNNING
    row = store.job_for(attempt.attempt_id)
    assert row is not None
    assert json.loads(row["payload_json"])["state"] == "LAUNCH_RESERVED"


def test_frozen_payload_authority_cannot_be_rewritten(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    frozen = store.freeze(hypothesis.hypothesis_id)
    with store._connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM hypotheses WHERE hypothesis_id=?",
                (frozen.hypothesis_id,),
            ).fetchone()[0]
        )
        payload["title"] = "tampered"
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(text.encode()).hexdigest()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE hypotheses SET payload_json=?,payload_sha256=? WHERE hypothesis_id=?",
                (text, digest, frozen.hypothesis_id),
            )
    assert store.get_hypothesis(frozen.hypothesis_id).title == hypothesis.title


def test_pause_close_commits_attempt_and_campaign_together(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    record = implementation(attempt.attempt_id, "a" * 40)
    store.submit_implementation(
        attempt.attempt_id, record, run_plan=run_plan(store, attempt, record)
    )
    verified_review(
        store,
        review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256, "FAIL"),
    )
    closed = store.close_attempt(attempt.attempt_id, AttemptDecision.PAUSE, "stop")
    assert closed.state == AttemptState.CLOSED
    assert store.campaign()[0] == "PAUSED"
    assert [event.kind for event in store.events()][-2:] == [
        "attempt_closed",
        "campaign_paused",
    ]


def test_hypothesis_create_requires_decided_previous_and_reconcile_repairs(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    with pytest.raises(TypeError):
        store.create_hypothesis(  # type: ignore[call-arg]
            "second",
            Path(hypothesis.evaluation_spec_path),
            Path(hypothesis.panel_path),
            Path(hypothesis.receipt_path),
            Path(hypothesis.evaluation_spec_path),
            "a" * 40,
        )
    store.freeze(hypothesis.hypothesis_id)
    store.decide_hypothesis(
        hypothesis.hypothesis_id,
        __import__(
            "gateway.research.contracts", fromlist=["HypothesisDecision"]
        ).HypothesisDecision.FINISHED,
        "done",
    )
    spec_file = store.root / "second-spec.json"
    spec_file.write_text('{"second": true}', encoding="utf-8")
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
        "b" * 40,
        dividends=Path(hypothesis.dividends_path),
        evaluation_spec_set=second_set,
    )
    assert second.hypothesis_id == "H0002"
