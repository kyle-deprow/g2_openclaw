from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from gateway.research.contracts import (
    AttemptDecision,
    AttemptState,
    HypothesisDecision,
    HypothesisSpec,
    ImplementationRecord,
    RunOutcome,
)
from gateway.research.machine import (
    IllegalTransition,
    close_attempt,
    decide_hypothesis,
    finish_run,
    freeze,
    open_attempt,
    queue_run,
    start_run,
    submit_implementation,
    submit_review,
)
from gateway.research.store import ResearchStore

from tests.gateway.research.conftest import implementation, review


def test_open_requires_frozen_and_closed_siblings(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    _store, source, hypothesis = campaign
    with pytest.raises(IllegalTransition):
        open_attempt(hypothesis, (), str(source), "2026-01-01T00:00:00Z")
    frozen = freeze(hypothesis)
    first = open_attempt(frozen, (), str(source), "2026-01-01T00:00:00Z")
    with pytest.raises(IllegalTransition):
        open_attempt(frozen, (first,), str(source), "2026-01-01T00:00:00Z")


def test_every_run_edge_and_illegal_back_edges(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    _store, source, hypothesis = campaign
    frozen = freeze(hypothesis)
    attempt = open_attempt(frozen, (), str(source), "2026-01-01T00:00:00Z")
    with pytest.raises(IllegalTransition):
        queue_run(attempt, "2026-01-01T00:00:00Z")
    implemented = submit_implementation(
        attempt,
        implementation(attempt.attempt_id, "a" * 40),
        "2026-01-01T00:00:00Z",
    )
    failed = submit_review(
        implemented,
        review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256, "FAIL"),
        hypothesis.spec_sha256,
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(IllegalTransition):
        submit_implementation(
            failed,
            implementation(attempt.attempt_id, "a" * 40),
            "2026-01-01T00:00:00Z",
        )
    closed_failed = close_attempt(failed, AttemptDecision.RETRY, "retry", "2026-01-01T00:00:00Z")
    assert closed_failed.state == AttemptState.CLOSED

    passing_attempt = open_attempt(frozen, (closed_failed,), str(source), "2026-01-01T00:00:00Z")
    passing_impl = submit_implementation(
        passing_attempt,
        implementation(passing_attempt.attempt_id, "b" * 40),
        "2026-01-01T00:00:00Z",
    )
    passing = submit_review(
        passing_impl,
        review(passing_attempt.attempt_id, "b" * 40, hypothesis.spec_sha256),
        hypothesis.spec_sha256,
        "2026-01-01T00:00:00Z",
    )
    queued = queue_run(passing, "2026-01-01T00:00:00Z")
    running = start_run(queued, "job-1", "2026-01-01T00:00:00Z")
    outcome = RunOutcome(
        passing_attempt.attempt_id,
        "job-1",
        0,
        True,
        False,
        True,
        "accepted",
        "driver",
        "/run/result.json",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
    )
    succeeded = finish_run(running, outcome, "2026-01-01T00:00:01Z")
    assert succeeded.state == AttemptState.RUN_SUCCEEDED
    assert (
        close_attempt(succeeded, AttemptDecision.FINISH, "done", "2026-01-01T00:00:02Z").state
        == AttemptState.CLOSED
    )


def test_new_implementation_clears_review_metadata() -> None:
    from gateway.research.contracts import ImplementationRecord

    attempt = open_attempt(
        HypothesisSpec(
            "H0001",
            "t",
            "{}",
            "a" * 64,
            "/p",
            "/r",
            "/e",
            "b" * 64,
            "c" * 64,
            "d" * 64,
            3,
            "e" * 40,
            "2026-01-01T00:00:00Z",
            state=__import__(
                "gateway.research.contracts", fromlist=["HypothesisState"]
            ).HypothesisState.FROZEN,
            dividends_path="/dividends",
            dividends_sha256="f" * 64,
        ),
        (),
        "/tmp/worktree",
        "2026-01-01T00:00:00Z",
    )
    dirty = replace(
        attempt,
        review_verdict="FAIL",
        review_commit="a" * 40,
        review_spec_sha256="a" * 64,
        reported_reviewer_model="untrusted",
    )
    record = ImplementationRecord(
        attempt.attempt_id,
        "f" * 40,
        ("/usr/bin/python", "-m", "module"),
        "/tmp/evidence",
        "coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    updated = submit_implementation(dirty, record, "2026-01-01T00:00:01Z")
    assert updated.state == AttemptState.IMPLEMENTED
    assert (
        updated.review_verdict is None
        and updated.review_commit is None
        and updated.reported_reviewer_model is None
    )


def test_terminal_status_controls_run_success() -> None:
    from gateway.research.contracts import HypothesisState

    hypothesis = HypothesisSpec(
        "H0001",
        "t",
        "{}",
        "a" * 64,
        "/p",
        "/r",
        "/e",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        1,
        "e" * 40,
        "2026-01-01T00:00:00Z",
        state=HypothesisState.FROZEN,
        dividends_path="/dividends",
        dividends_sha256="e" * 64,
    )
    opened = open_attempt(hypothesis, (), "/tmp/worktree", "2026-01-01T00:00:00Z")
    implemented = submit_implementation(
        opened,
        ImplementationRecord(
            opened.attempt_id,
            "f" * 40,
            ("/usr/bin/python3", "-m", "target"),
            "/tmp/evidence",
            "coder",
            "high",
            "standard",
            "2026-01-01T00:00:00Z",
        ),
        "2026-01-01T00:00:00Z",
    )
    passed = submit_review(
        implemented,
        review(implemented.attempt_id, "f" * 40, hypothesis.spec_sha256),
        hypothesis.spec_sha256,
        "2026-01-01T00:00:00Z",
    )
    running = start_run(queue_run(passed, "2026-01-01T00:00:00Z"), "job-1", "2026-01-01T00:00:00Z")
    outcome = RunOutcome(
        running.attempt_id,
        "job-1",
        0,
        True,
        False,
        True,
        "accepted",
        "driver",
        "/run/result.json",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
        "source_mutated",
    )
    assert finish_run(running, outcome, "2026-01-01T00:00:01Z").state == AttemptState.RUN_FAILED


def test_machine_rejects_all_non_edge_state_calls(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    _store, source, hypothesis = campaign
    frozen = freeze(hypothesis)
    opened = open_attempt(frozen, (), str(source), "2026-01-01T00:00:00Z")
    implemented = submit_implementation(
        opened,
        implementation(opened.attempt_id, "a" * 40),
        "2026-01-01T00:00:00Z",
    )
    passed = submit_review(
        implemented,
        review(opened.attempt_id, "a" * 40, hypothesis.spec_sha256),
        hypothesis.spec_sha256,
        "2026-01-01T00:00:00Z",
    )
    queued = queue_run(passed, "2026-01-01T00:00:00Z")
    running = start_run(queued, "job-1", "2026-01-01T00:00:00Z")
    outcome = RunOutcome(
        opened.attempt_id,
        "job-1",
        0,
        True,
        False,
        True,
        "accepted",
        "driver",
        "/run/result.json",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
    )
    with pytest.raises(IllegalTransition):
        freeze(frozen)
    for item in [opened, passed, queued, running]:
        with pytest.raises(IllegalTransition):
            close_attempt(item, AttemptDecision.RETRY, "no", "2026-01-01T00:00:00Z")
    retried = close_attempt(implemented, AttemptDecision.RETRY, "retry", "2026-01-01T00:00:00Z")
    assert retried.state == AttemptState.CLOSED
    assert retried.decision == AttemptDecision.RETRY.value
    for decision in [AttemptDecision.FINISH, AttemptDecision.PAUSE]:
        with pytest.raises(IllegalTransition):
            close_attempt(implemented, decision, "no", "2026-01-01T00:00:00Z")
    with pytest.raises(IllegalTransition):
        submit_review(
            opened,
            review(opened.attempt_id, "a" * 40, hypothesis.spec_sha256),
            hypothesis.spec_sha256,
            "2026-01-01T00:00:00Z",
        )
    for item in [opened, implemented, queued, running]:
        with pytest.raises(IllegalTransition):
            queue_run(item, "2026-01-01T00:00:00Z")
    for item in [opened, implemented, passed, running]:
        with pytest.raises(IllegalTransition):
            start_run(item, "job-1", "2026-01-01T00:00:00Z")
    for item in [opened, implemented, passed, queued]:
        with pytest.raises(IllegalTransition):
            finish_run(item, outcome, "2026-01-01T00:00:01Z")
    with pytest.raises(IllegalTransition):
        decide_hypothesis(frozen, HypothesisDecision.FINISHED, [opened])


def test_review_rejects_commit_and_spec_mismatch(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    _store, source, hypothesis = campaign
    frozen = freeze(hypothesis)
    attempt = open_attempt(frozen, (), str(source), "2026-01-01T00:00:00Z")
    implemented = submit_implementation(
        attempt,
        implementation(attempt.attempt_id, "a" * 40),
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="commit"):
        submit_review(
            implemented,
            review(attempt.attempt_id, "b" * 40, hypothesis.spec_sha256),
            hypothesis.spec_sha256,
            "2026-01-01T00:00:00Z",
        )
    with pytest.raises(ValueError, match="spec digest"):
        submit_review(
            implemented,
            review(attempt.attempt_id, "a" * 40, "f" * 64),
            hypothesis.spec_sha256,
            "2026-01-01T00:00:00Z",
        )


def test_open_attempt_refuses_max_attempts(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    _store, source, hypothesis = campaign
    limited = replace(freeze(hypothesis), max_attempts=1)
    first = open_attempt(limited, (), str(source), "2026-01-01T00:00:00Z")
    closed = replace(first, state=AttemptState.CLOSED)
    with pytest.raises(IllegalTransition, match="MAX_ATTEMPTS"):
        open_attempt(limited, [closed], str(source), "2026-01-01T00:00:00Z")
