"""Pure transition functions for the research campaign."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import replace

from .contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    HypothesisDecision,
    HypothesisSpec,
    HypothesisState,
    ImplementationRecord,
    ReviewEvidence,
    ReviewRecord,
    RunOutcome,
)


class IllegalTransition(RuntimeError):
    """A requested state transition is not in the driver graph."""

    def __init__(self, current: str, requested: str) -> None:
        super().__init__(f"illegal transition: {current} -> {requested}")
        self.current = current
        self.requested = requested


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def freeze(hypothesis: HypothesisSpec) -> HypothesisSpec:
    if hypothesis.state != HypothesisState.DRAFT:
        raise IllegalTransition(hypothesis.state.value, HypothesisState.FROZEN.value)
    return replace(hypothesis, state=HypothesisState.FROZEN)


def open_attempt(
    hypothesis: HypothesisSpec,
    attempts: Iterable[Attempt],
    worktree_path: str,
    opened_at: str,
) -> Attempt:
    prior = list(attempts)
    if hypothesis.state != HypothesisState.FROZEN:
        raise IllegalTransition(hypothesis.state.value, AttemptState.OPENED.value)
    if any(item.state != AttemptState.CLOSED for item in prior):
        raise IllegalTransition("UNFINISHED_ATTEMPT", AttemptState.OPENED.value)
    if len(prior) >= hypothesis.max_attempts:
        raise IllegalTransition("MAX_ATTEMPTS", AttemptState.OPENED.value)
    number = len(prior) + 1
    attempt_id = f"{hypothesis.hypothesis_id}-A{number:03d}"
    return Attempt(
        attempt_id=attempt_id,
        hypothesis_id=hypothesis.hypothesis_id,
        number=number,
        state=AttemptState.OPENED,
        worktree_path=worktree_path,
        commit=None,
        implementation_sha256=None,
        review_verdict=None,
        review_commit=None,
        review_spec_sha256=None,
        reported_reviewer_model=None,
        reported_reviewer_actual_model=None,
        reported_coder_model=None,
        coder_effort=None,
        coder_service_tier=None,
        run_job_id=None,
        run_outcome=None,
        decision=None,
        decision_reason=None,
        opened_at=opened_at,
        updated_at=opened_at,
    )


def submit_implementation(
    attempt: Attempt, record: ImplementationRecord, updated_at: str
) -> Attempt:
    if attempt.state != AttemptState.OPENED:
        raise IllegalTransition(attempt.state.value, AttemptState.IMPLEMENTED.value)
    if record.attempt_id != attempt.attempt_id:
        raise ValueError("implementation attempt_id does not match")
    return replace(
        attempt,
        state=AttemptState.IMPLEMENTED,
        commit=record.commit,
        implementation_sha256=_digest(record.to_json()),
        review_verdict=None,
        review_commit=None,
        review_spec_sha256=None,
        reported_reviewer_model=None,
        reported_reviewer_actual_model=None,
        reported_coder_model=record.reported_coder_model,
        coder_effort=record.coder_effort,
        coder_service_tier=record.coder_service_tier,
        updated_at=updated_at,
    )


def submit_review(
    attempt: Attempt,
    record: ReviewEvidence | ReviewRecord,
    spec_sha256: str,
    updated_at: str,
) -> Attempt:
    if attempt.state != AttemptState.IMPLEMENTED:
        raise IllegalTransition(attempt.state.value, "REVIEW_" + record.verdict)
    if record.attempt_id != attempt.attempt_id:
        raise ValueError("review attempt_id does not match")
    if record.commit != attempt.commit:
        raise ValueError("review commit does not match implementation commit")
    if record.spec_sha256 != spec_sha256:
        raise ValueError("review spec digest does not match frozen hypothesis")
    return replace(
        attempt,
        state=AttemptState.REVIEW_PASSED
        if record.verdict == "PASS"
        else AttemptState.REVIEW_FAILED,
        review_verdict=record.verdict,
        review_commit=record.commit,
        review_spec_sha256=record.spec_sha256,
        reported_reviewer_model=record.reported_reviewer_model,
        reported_reviewer_actual_model=record.reported_reviewer_actual_model,
        updated_at=updated_at,
    )


def queue_run(attempt: Attempt, updated_at: str) -> Attempt:
    if attempt.state != AttemptState.REVIEW_PASSED:
        raise IllegalTransition(attempt.state.value, AttemptState.RUN_QUEUED.value)
    return replace(attempt, state=AttemptState.RUN_QUEUED, updated_at=updated_at)


def start_run(attempt: Attempt, job_id: str, updated_at: str) -> Attempt:
    if attempt.state != AttemptState.RUN_QUEUED:
        raise IllegalTransition(attempt.state.value, AttemptState.RUNNING.value)
    return replace(attempt, state=AttemptState.RUNNING, run_job_id=job_id, updated_at=updated_at)


def finish_run(attempt: Attempt, outcome: RunOutcome, updated_at: str) -> Attempt:
    if attempt.state != AttemptState.RUNNING:
        raise IllegalTransition(attempt.state.value, "RUN_FINISHED")
    if outcome.attempt_id != attempt.attempt_id or outcome.job_id != attempt.run_job_id:
        raise ValueError("run outcome does not match attempt job")
    has_result = bool(outcome.result_path)
    # The worker's terminal status is authoritative for the lifecycle verdict.
    # A pure transition function cannot read result.json; the non-empty path
    # is only additional evidence on the success branch.
    succeeded = outcome.status == "succeeded" and outcome.exit_code == 0 and has_result
    return replace(
        attempt,
        state=AttemptState.RUN_SUCCEEDED if succeeded else AttemptState.RUN_FAILED,
        run_outcome=outcome.to_json(),
        updated_at=updated_at,
    )


def close_attempt(
    attempt: Attempt, decision: AttemptDecision, reason: str, updated_at: str
) -> Attempt:
    if not isinstance(decision, AttemptDecision):
        raise ValueError("decision must be an AttemptDecision")
    if not (
        attempt.state == AttemptState.IMPLEMENTED and decision == AttemptDecision.RETRY
    ) and attempt.state not in {
        AttemptState.REVIEW_FAILED,
        AttemptState.RUN_SUCCEEDED,
        AttemptState.RUN_FAILED,
    }:
        raise IllegalTransition(attempt.state.value, AttemptState.CLOSED.value)
    return replace(
        attempt,
        state=AttemptState.CLOSED,
        decision=decision.value,
        decision_reason=reason,
        updated_at=updated_at,
    )


def decide_hypothesis(
    hypothesis: HypothesisSpec,
    decision: HypothesisDecision,
    attempts: Iterable[Attempt] = (),
) -> HypothesisSpec:
    if not isinstance(decision, HypothesisDecision):
        raise ValueError("decision must be a HypothesisDecision")
    if hypothesis.state != HypothesisState.FROZEN:
        raise IllegalTransition(hypothesis.state.value, HypothesisState.DECIDED.value)
    if any(attempt.state != AttemptState.CLOSED for attempt in attempts):
        raise IllegalTransition("UNFINISHED_ATTEMPT", HypothesisState.DECIDED.value)
    return replace(hypothesis, state=HypothesisState.DECIDED)
