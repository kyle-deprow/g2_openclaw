from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research.contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    Event,
    HypothesisDecision,
    HypothesisState,
    RunOutcome,
)
from gateway.research.status import read_status
from gateway.research.store import ResearchStore, StoreConflict
from typer.testing import CliRunner

from tests.gateway.research.test_cli_scenario import (
    _canonical_terminal_fixture,
    _rewrite_terminal_evidence,
)

JOB = "job-terminal-canonical"
OPERATOR = "operator-ticket-1"
REASON = "host success verifier accepted the real worker evidence schema"
runner = CliRunner()


def _snapshot(run_dir: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(run_dir)): path.read_bytes() if path.is_file() else None
        for path in sorted(run_dir.rglob("*"))
    }


def _events(store: ResearchStore, attempt_id: str, kind: str) -> list[Event]:
    return [e for e in store.events() if e.attempt_id == attempt_id and e.kind == kind]


class _Case:
    def __init__(self, campaign: tuple[ResearchStore, Path, Any]) -> None:
        (
            self.store,
            self.attempt,
            self.plan,
            self.run_dir,
            self.evidence,
            self.terminal,
        ) = _canonical_terminal_fixture(campaign)
        self.attempt_id = self.attempt.attempt_id
        (self.run_dir / "terminal.json").write_text(json.dumps(self.terminal), encoding="utf-8")

    def running(self) -> None:
        self.store.claim_queued_job(self.attempt_id, JOB)

    def fail(self, status: str = "run_evidence_mismatch") -> Attempt:
        """Reproduce the live shape: terminal succeeded on disk, host recorded a mismatch."""
        self.running()
        outcome = research_cli._run_evidence_mismatch_outcome(
            self.attempt_id, self.store.get_attempt(self.attempt_id), self.terminal
        )
        failed = self.store.finish_run(self.attempt_id, replace(outcome, status=status))
        research_cli._mark_job_state(self.store, self.attempt_id, "EXITED")
        assert failed.state is AttemptState.RUN_FAILED
        return failed

    def recomputed(self) -> RunOutcome:
        terminal = json.loads((self.run_dir / "terminal.json").read_text(encoding="utf-8"))
        return research_cli._terminal_outcome(
            self.store, self.attempt_id, terminal, origin="terminal.json"
        )

    def digests(self) -> tuple[str, str]:
        return (
            hashlib.sha256((self.run_dir / "terminal.json").read_bytes()).hexdigest(),
            hashlib.sha256((self.run_dir / "run-evidence.json").read_bytes()).hexdigest(),
        )

    def reverify(
        self,
        *,
        job_id: str = JOB,
        reference: str = OPERATOR,
        outcome: RunOutcome | None = None,
        digests: tuple[str, str] | None = None,
    ) -> Attempt:
        terminal_sha, evidence_sha = digests or self.digests()
        return self.store.reverify_run_outcome(
            self.attempt_id,
            job_id,
            outcome or self.recomputed(),
            REASON,
            reference,
            terminal_sha,
            evidence_sha,
        )

    def cli(self, *, job_id: str = JOB, reference: str = OPERATOR) -> Any:
        return runner.invoke(
            app,
            [
                "research",
                "run-reverify",
                self.attempt_id,
                "--root",
                str(self.store.root),
                "--job-id",
                job_id,
                "--reason",
                REASON,
                "--operator-reference",
                reference,
            ],
        )

    def state(self) -> AttemptState:
        return self.store.get_attempt(self.attempt_id).state


@pytest.fixture
def case(campaign: tuple[ResearchStore, Path, Any]) -> _Case:
    return _Case(campaign)


def test_reverify_happy_path_via_cli_preserves_old_outcome_and_run_files(case: _Case) -> None:
    old = case.fail()
    assert case.recomputed().status == "succeeded"
    before = _snapshot(case.run_dir)
    other_events = [e for e in case.store.events() if e.attempt_id != case.attempt_id]

    result = case.cli()

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert f"reverified {JOB} state=RUN_SUCCEEDED" in result.output
    updated = case.store.get_attempt(case.attempt_id)
    assert updated.state is AttemptState.RUN_SUCCEEDED
    assert updated.run_job_id == JOB
    assert updated.run_outcome is not None
    new = RunOutcome.from_json(updated.run_outcome)
    primary = case.run_dir / "scenarios" / "s000" / "evaluator-stage" / "out" / "result.json"
    assert new.status == "succeeded" and new.exit_code == 0
    assert new.result_path == str(primary)
    assert new.compliant is True
    assert _snapshot(case.run_dir) == before
    assert [e for e in case.store.events() if e.attempt_id != case.attempt_id] == other_events

    (event,) = _events(case.store, case.attempt_id, "run_reverified")
    assert event.actor == "operator"
    detail = json.loads(event.detail)
    assert detail["job_id"] == JOB
    assert detail["reason"] == REASON
    assert detail["operator_reference"] == OPERATOR
    assert detail["old_outcome"] == json.loads(old.run_outcome or "")
    assert detail["old_outcome"]["status"] == "run_evidence_mismatch"
    old_digest = hashlib.sha256((old.run_outcome or "").encode()).hexdigest()
    assert detail["old_outcome_sha256"] == old_digest
    assert detail["new_outcome_sha256"] == hashlib.sha256(updated.run_outcome.encode()).hexdigest()
    assert (
        detail["terminal_sha256"]
        == hashlib.sha256((case.run_dir / "terminal.json").read_bytes()).hexdigest()
    )
    assert (
        detail["run_evidence_sha256"]
        == hashlib.sha256((case.run_dir / "run-evidence.json").read_bytes()).hexdigest()
    )


def test_reverified_attempt_can_be_closed_and_hypothesis_decided(case: _Case) -> None:
    case.fail()
    case.reverify()

    closed = case.store.close_attempt(case.attempt_id, AttemptDecision.FINISH, "reverified run")
    decided = case.store.decide_hypothesis(
        case.attempt.hypothesis_id, HypothesisDecision.FINISHED, "done"
    )

    assert closed.state is AttemptState.CLOSED
    assert closed.decision == "FINISH"
    assert decided.state is HypothesisState.DECIDED


def test_reverify_refuses_wrong_state(case: _Case) -> None:
    case.running()
    outcome = case.recomputed()
    assert case.state() is AttemptState.RUNNING
    with pytest.raises(StoreConflict, match="not RUN_FAILED"):
        case.reverify(outcome=outcome)
    assert case.state() is AttemptState.RUNNING
    assert not _events(case.store, case.attempt_id, "run_reverified")


def test_reverify_refuses_wrong_job(case: _Case) -> None:
    case.fail()
    with pytest.raises(StoreConflict, match="does not match the requested job"):
        case.reverify(job_id="job-other")
    assert case.cli(job_id="job-other").exit_code == 1
    assert case.state() is AttemptState.RUN_FAILED


def test_reverify_refuses_stored_status_other_than_run_evidence_mismatch(case: _Case) -> None:
    case.fail(status="scenario_failed")
    with pytest.raises(StoreConflict, match="not a run_evidence_mismatch"):
        case.reverify()
    assert case.cli().exit_code == 1
    assert case.state() is AttemptState.RUN_FAILED


@pytest.mark.parametrize("job_state", ["ATTACHED", "CANCELLED", "TIMED_OUT", "ORPHANED"])
def test_reverify_requires_exited_job(case: _Case, job_state: str) -> None:
    case.fail()
    row = case.store.job_for(case.attempt_id)
    assert row is not None
    case.store.update_job({**json.loads(row["payload_json"]), "state": job_state}, case.attempt_id)
    with pytest.raises(StoreConflict, match="not in a terminal state"):
        case.reverify()
    assert case.cli().exit_code == 1
    assert case.state() is AttemptState.RUN_FAILED
    assert not _events(case.store, case.attempt_id, "run_reverified")


@pytest.mark.parametrize("which", ["terminal", "evidence", "both"])
def test_reverify_refuses_digests_that_differ_from_the_files(case: _Case, which: str) -> None:
    case.fail()
    terminal_sha, evidence_sha = case.digests()
    bad = ("0" * 64) if which in {"terminal", "both"} else terminal_sha
    bad_evidence = ("0" * 64) if which in {"evidence", "both"} else evidence_sha
    with pytest.raises(StoreConflict, match="changed after verification"):
        case.reverify(digests=(bad, bad_evidence))
    assert case.state() is AttemptState.RUN_FAILED
    assert not _events(case.store, case.attempt_id, "run_reverified")


def test_reverify_refuses_files_changed_between_verification_and_commit(case: _Case) -> None:
    case.fail()
    outcome = case.recomputed()
    digests = case.digests()
    # A byte-different but still-valid terminal.json written after the CLI verified.
    (case.run_dir / "terminal.json").write_text(
        json.dumps({**case.terminal, "finished_at": "2026-01-01T00:00:09Z"}), encoding="utf-8"
    )
    with pytest.raises(StoreConflict, match="changed after verification"):
        case.reverify(outcome=outcome, digests=digests)
    assert case.state() is AttemptState.RUN_FAILED


def test_reverify_refuses_terminal_not_bound_to_run_evidence(case: _Case) -> None:
    case.fail()
    outcome = case.recomputed()
    (case.run_dir / "terminal.json").write_text(
        json.dumps({**case.terminal, "run_evidence_sha256": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(StoreConflict, match="does not bind"):
        case.reverify(outcome=outcome)
    assert case.state() is AttemptState.RUN_FAILED


@pytest.mark.parametrize("variant", ["secondary", "outside", "relative", "parent_dir"])
def test_reverify_refuses_non_canonical_result_path(case: _Case, variant: str) -> None:
    case.fail()
    outcome = case.recomputed()
    canonical = Path(outcome.result_path)
    path = {
        "secondary": str(canonical).replace("/s000/", "/s001/"),
        "outside": str(case.run_dir / "result.json"),
        "relative": "scenarios/s000/evaluator-stage/out/result.json",
        "parent_dir": str(
            case.run_dir
            / "scenarios"
            / "s000"
            / ".."
            / "s000"
            / "evaluator-stage"
            / "out"
            / "result.json"
        ),
    }[variant]
    with pytest.raises(StoreConflict, match="canonical primary result"):
        case.reverify(outcome=replace(outcome, result_path=path))
    assert case.state() is AttemptState.RUN_FAILED
    assert not _events(case.store, case.attempt_id, "run_reverified")


def test_status_clears_run_evidence_mismatch_after_reverify(case: _Case) -> None:
    case.fail()
    before = read_status(case.store.root, unit_state=lambda _: "inactive")
    assert before.boundary_failure == "run_evidence_mismatch"

    case.reverify()
    after = read_status(case.store.root, unit_state=lambda _: "inactive")

    assert after.attempt_state == "RUN_SUCCEEDED"
    assert after.boundary_failure is None


@pytest.mark.parametrize("tamper", ["status", "job_id", "missing", "not_json"])
def test_reverify_refuses_terminal_that_is_not_succeeded_for_the_job(
    case: _Case, tamper: str
) -> None:
    case.fail()
    outcome = case.recomputed()
    digests = case.digests()
    path = case.run_dir / "terminal.json"
    match = "terminal.json"
    if tamper == "status":
        path.write_text(json.dumps({**case.terminal, "status": "scenario_failed"}))
    elif tamper == "job_id":
        path.write_text(json.dumps({**case.terminal, "job_id": "job-other"}))
    elif tamper == "missing":
        path.unlink()
    else:
        path.write_text("not json")
    if tamper != "missing":
        digests = case.digests()  # the digests describe the tampered file: only the guard refuses
    with pytest.raises(StoreConflict, match=match):
        case.reverify(outcome=outcome, digests=digests)
    assert case.state() is AttemptState.RUN_FAILED
    assert not _events(case.store, case.attempt_id, "run_reverified")


def test_reverify_refuses_when_recomputed_outcome_is_still_a_mismatch(case: _Case) -> None:
    case.fail()
    analysis = case.evidence["analysis"]
    assert isinstance(analysis, dict)
    analysis["analysis/result.json"] = "0" * 64
    _rewrite_terminal_evidence(case.run_dir, case.evidence, case.terminal)
    (case.run_dir / "run-evidence.json").write_text(json.dumps(case.evidence), encoding="utf-8")
    (case.run_dir / "terminal.json").write_text(json.dumps(case.terminal), encoding="utf-8")
    assert case.recomputed().status == "run_evidence_mismatch"

    with pytest.raises(StoreConflict, match="not a verified success"):
        case.reverify()
    refused = case.cli()

    assert refused.exit_code == 1
    assert case.state() is AttemptState.RUN_FAILED
    assert not _events(case.store, case.attempt_id, "run_reverified")


def test_reverify_refuses_second_reverification(case: _Case) -> None:
    old = case.fail()
    case.reverify()
    assert case.state() is AttemptState.RUN_SUCCEEDED

    with pytest.raises(StoreConflict, match="not RUN_FAILED"):
        case.reverify()
    assert case.cli().exit_code == 1

    # Even if the attempt were somehow back in RUN_FAILED, the event blocks a repeat.
    case.store.set_state(old, event="test_reset", actor="operator")
    assert case.state() is AttemptState.RUN_FAILED
    with pytest.raises(StoreConflict, match="already reverified"):
        case.reverify()
    assert case.cli().exit_code == 1
    assert case.state() is AttemptState.RUN_FAILED
    assert len(_events(case.store, case.attempt_id, "run_reverified")) == 1


@pytest.mark.parametrize("reference", ["", None])
def test_reverify_requires_operator_reference(case: _Case, reference: str | None) -> None:
    case.fail()
    with pytest.raises(ValueError, match="operator reference"):
        case.reverify(reference=reference)  # type: ignore[arg-type]
    assert case.cli(reference="").exit_code == 1
    assert case.state() is AttemptState.RUN_FAILED


def test_reverify_requires_reason(case: _Case) -> None:
    case.fail()
    with pytest.raises(ValueError, match="reason"):
        case.store.reverify_run_outcome(
            case.attempt_id, JOB, case.recomputed(), "", OPERATOR, *case.digests()
        )
    assert case.state() is AttemptState.RUN_FAILED
