from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.research.admission import AdmissionDecision
from gateway.research.contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisSpec,
    ImplementationRecord,
)
from gateway.research.machine import IllegalTransition
from gateway.research.store import OwnerLockHeld, ResearchStore, StoreConflict

from tests.gateway.research.conftest import implementation, review, run_plan, verified_review
from tests.gateway.research.test_admission import _admit, _document, _payload


def _implemented_attempt(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    *,
    admission: AdmissionDecision | None = None,
) -> tuple[ResearchStore, Path, HypothesisSpec, Attempt, ImplementationRecord]:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source, admission=admission)
    record = implementation(attempt.attempt_id, "a" * 40)
    attempt = store.submit_implementation(
        attempt.attempt_id, record, run_plan=run_plan(store, attempt, record)
    )
    return store, source, hypothesis, attempt, record


def _admission_for(store: ResearchStore, hypothesis: HypothesisSpec) -> AdmissionDecision:
    spec_set_sha256 = hashlib.sha256(
        store.evaluation_spec_set(hypothesis.hypothesis_id).to_json().encode()
    ).hexdigest()
    return _admit(_document(_payload()), evaluation_spec_set_sha256=spec_set_sha256)


def _insert_raw_evidence(store: ResearchStore, attempt_id: str, kind: str) -> None:
    payload = json.dumps({"kind": kind}, sort_keys=True, separators=(",", ":"))
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
            (attempt_id, kind, payload, hashlib.sha256(payload.encode()).hexdigest()),
        )
        conn.commit()


def test_pre_review_retry_closes_implemented_and_preserves_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    admission = _admission_for(store, hypothesis)
    store, source, hypothesis, attempt, record = _implemented_attempt(campaign, admission=admission)
    implementation_payload = store.evidence(attempt.attempt_id, "implementation")
    run_plan_payload = store.evidence(attempt.attempt_id, "run_plan")
    admission_payload = store.evidence(attempt.attempt_id, "admission_decision")
    frozen_hypothesis = store.get_hypothesis(hypothesis.hypothesis_id)
    campaign_before = dict(store._campaign_row())
    before = store.get_attempt(attempt.attempt_id)
    store.record_review_event(
        attempt.attempt_id,
        "execution_guard_refused",
        {"guard": "review_bundle", "reason": "BUNDLE_TOO_LARGE"},
    )
    events_before = store.events()

    closed = store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "bundle too large")

    assert closed.state == AttemptState.CLOSED
    assert closed.decision == AttemptDecision.RETRY.value
    assert closed.decision_reason == "bundle too large"
    assert closed.commit == before.commit == record.commit
    assert closed.implementation_sha256 == before.implementation_sha256
    assert closed.run_job_id is None
    assert closed.run_outcome is None
    assert store.evidence(attempt.attempt_id, "implementation") == implementation_payload
    assert store.evidence(attempt.attempt_id, "run_plan") == run_plan_payload
    assert store.evidence(attempt.attempt_id, "admission_decision") == admission_payload
    assert store.get_hypothesis(hypothesis.hypothesis_id) == frozen_hypothesis
    assert dict(store._campaign_row()) == campaign_before
    events_after = store.events()
    assert events_after[:-1] == events_before
    assert (
        sum(event.kind == "attempt_closed" for event in events_after)
        == sum(event.kind == "attempt_closed" for event in events_before) + 1
    )
    attempt_events = [event for event in store.events() if event.attempt_id == attempt.attempt_id]
    assert attempt_events[-1].kind == "attempt_closed"
    assert attempt_events[-1].actor == "astra"
    assert json.loads(attempt_events[-1].detail) == {
        "decision": "RETRY",
        "reason": "bundle too large",
    }

    reopened = store.open_attempt(hypothesis.hypothesis_id, source)

    assert reopened.number == attempt.number + 1


def test_pre_review_retry_preserves_hypothesis_attempt_cap(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    limited = replace(hypothesis, max_attempts=1)
    payload = limited.to_json()
    with store._connect() as conn:
        conn.execute(
            "UPDATE hypotheses SET max_attempts=?,payload_json=?,payload_sha256=? "
            "WHERE hypothesis_id=?",
            (
                limited.max_attempts,
                payload,
                hashlib.sha256(payload.encode()).hexdigest(),
                hypothesis.hypothesis_id,
            ),
        )
        conn.commit()

    _store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    with pytest.raises(IllegalTransition, match="MAX_ATTEMPTS"):
        store.open_attempt(hypothesis.hypothesis_id, source)
    assert store.get_hypothesis(hypothesis.hypothesis_id).max_attempts == 1


def test_pre_review_retry_preserves_campaign_attempt_cap(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.set_campaign_policy(1, None, "retry-cap-test")
    _store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    campaign_before = dict(store._campaign_row())

    store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    with pytest.raises(StoreConflict, match="campaign attempt cap exceeded"):
        store.open_attempt(hypothesis.hypothesis_id, source)
    assert dict(store._campaign_row()) == campaign_before


@pytest.mark.parametrize("decision", (AttemptDecision.FINISH, AttemptDecision.PAUSE))
def test_store_rejects_pre_review_non_retry_without_mutation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], decision: AttemptDecision
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    before = store.get_attempt(attempt.attempt_id)
    campaign_before = dict(store._campaign_row())
    events_before = store.events()
    evidence_before = {
        kind: store.evidence(attempt.attempt_id, kind) for kind in ("implementation", "run_plan")
    }

    with pytest.raises(IllegalTransition):
        store.close_attempt(attempt.attempt_id, decision, "not a retry")

    assert store.get_attempt(attempt.attempt_id) == before
    assert dict(store._campaign_row()) == campaign_before
    assert store.events() == events_before
    assert {
        kind: store.evidence(attempt.attempt_id, kind) for kind in evidence_before
    } == evidence_before


@pytest.mark.parametrize(
    "kind",
    ("review_reservation", "review_ack", "review", "review_host_evidence", "review_cancel"),
)
def test_pre_review_retry_refuses_review_lifecycle_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], kind: str
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    if kind in {"review_reservation", "review_ack", "review_cancel"}:
        payload = json.dumps({"kind": kind})
        store.insert_review_evidence(attempt.attempt_id, kind, payload, "review_test")
    else:
        _insert_raw_evidence(store, attempt.attempt_id, kind)

    with pytest.raises(StoreConflict):
        store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    assert store.get_attempt(attempt.attempt_id).state == AttemptState.IMPLEMENTED


@pytest.mark.parametrize("blocker", ("run_job_id", "job_row"))
def test_pre_review_retry_refuses_existing_run_binding(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], blocker: str
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    if blocker == "run_job_id":
        store.set_state(replace(attempt, run_job_id="job-existing"), event="test_run_bound")
    else:
        store.save_job("job-existing", attempt.attempt_id, "CANCELLED", {"state": "CANCELLED"})

    with pytest.raises(StoreConflict):
        store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    assert store.get_attempt(attempt.attempt_id).state == AttemptState.IMPLEMENTED


def test_pre_review_retry_refuses_dispatch_indicating_review_event(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    store.record_review_event(
        attempt.attempt_id, "review_dispatch_requested", {"task_id": "unknown"}
    )

    with pytest.raises(StoreConflict):
        store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    assert store.get_attempt(attempt.attempt_id).state == AttemptState.IMPLEMENTED


def test_reservation_wins_against_pre_review_retry_close(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    payload = '{"reservation":true}'

    store.insert_review_evidence(
        attempt.attempt_id, "review_reservation", payload, "review_reserved"
    )

    with pytest.raises(StoreConflict):
        store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    assert store.get_attempt(attempt.attempt_id).state == AttemptState.IMPLEMENTED


def test_close_wins_against_new_review_reservation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    payload = '{"reservation":true}'
    store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    with pytest.raises(StoreConflict):
        store.insert_review_evidence(
            attempt.attempt_id, "review_reservation", payload, "review_reserved"
        )

    assert store.get_attempt(attempt.attempt_id).state == AttemptState.CLOSED


def test_existing_review_payload_replay_repairs_projection_after_closed_transition(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    payload = '{"reservation":true}'
    store.insert_review_evidence(
        attempt.attempt_id, "review_reservation", payload, "review_reserved"
    )
    projection = (
        store.root
        / "hypotheses"
        / attempt.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "review_reservation.json"
    )
    projection.unlink()
    store.set_state(
        replace(
            attempt,
            state=AttemptState.CLOSED,
            decision=AttemptDecision.RETRY.value,
            decision_reason="later",
        ),
        event="test_closed",
    )

    store.insert_review_evidence(
        attempt.attempt_id, "review_reservation", payload, "review_reserved"
    )

    assert projection.is_file()


def test_new_review_reservation_after_closed_attempt_is_refused(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis, attempt, _record = _implemented_attempt(campaign)
    store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")

    with pytest.raises(StoreConflict):
        store.insert_review_evidence(
            attempt.attempt_id,
            "review_reservation",
            '{"reservation":true}',
            "review_reserved",
        )


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
