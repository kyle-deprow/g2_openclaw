"""Claude (claude-cli) reviewer reservation, correlation and collection.

The Claude host-record readers are faked at the ``review_evidence`` import boundary; these
tests exercise the acceptance rules, not the record parsing.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from gateway.agent_profiles import AgentProfile, load_profile
from gateway.research import review_evidence
from gateway.research.contracts import Attempt, AttemptState, HypothesisSpec
from gateway.research.host_records import HostRecordError
from gateway.research.host_records_claude import (
    ClaudeRecordError,
    ClaudeRecordNotRecorded,
    ClaudeRun,
    ClaudeStores,
)
from gateway.research.review_evidence import (
    ReviewAck,
    ReviewEvidenceError,
    ReviewPending,
    ReviewReservation,
    ReviewUnresolved,
    cancel_review,
    collect_review,
    reconcile_review,
    reserve_review,
)
from gateway.research.store import ResearchStore, StoreConflict, canonical_wake_key

from tests.gateway.research.native_review_fixtures import OWNER_RUN_ID, OWNER_SESSION
from tests.gateway.research.native_review_fixtures import _setup as fixture_setup

Outcome = Literal["ok", "error", "lost", "pending"]
PROFILE: AgentProfile = load_profile("claude")
REVIEWER = PROFILE.role("reviewer")
CHILD_RUN_ID = "child-run-1"
CHILD_SESSION = "agent:research-reviewer:subagent:11111111"


class FakeClaudeHost:
    """In-memory stand-in for AP3a's ``read_run``/``child_runs``/``run_outcome``."""

    def __init__(self) -> None:
        self.runs: dict[str, ClaudeRun] = {}
        self.outcomes: dict[str, Outcome] = {}
        self.created: dict[str, int] = {}

    def add(
        self, run: ClaudeRun, outcome: Outcome = "ok", *, created_at_ms: int | None = None
    ) -> None:
        self.runs[run.run_id] = run
        self.outcomes[run.run_id] = outcome
        if created_at_ms is not None:
            self.created[run.run_id] = created_at_ms

    def read_run(self, stores: ClaudeStores, run_id: str) -> ClaudeRun:
        if run_id not in self.runs:
            raise ClaudeRecordNotRecorded(run_id)
        return self.runs[run_id]

    def child_runs(
        self,
        stores: ClaudeStores,
        requester_session_key: str,
        *,
        since_ms: int,
        agent_id: str | None = None,
    ) -> tuple[ClaudeRun, ...]:
        """Like AP3a: subagent runs of the requester, created at or after ``since_ms``."""

        return tuple(
            run
            for run in self.runs.values()
            if run.runtime == "subagent"
            and run.requester_session_key == requester_session_key
            and self.created.get(run.run_id, 2**62) >= since_ms
            and (agent_id is None or run.agent_id == agent_id)
        )

    def run_outcome(self, stores: ClaudeStores, run_id: str) -> Outcome:
        if run_id not in self.runs:
            raise ClaudeRecordNotRecorded(run_id)
        return self.outcomes[run_id]


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeClaudeHost:
    fake = FakeClaudeHost()
    monkeypatch.setattr(review_evidence, "read_run", fake.read_run)
    monkeypatch.setattr(review_evidence, "child_runs", fake.child_runs)
    monkeypatch.setattr(review_evidence, "run_outcome", fake.run_outcome)
    return fake


def _stores(tmp_path: Path) -> ClaudeStores:
    state = tmp_path / "state-dir"
    return ClaudeStores(
        state / "state" / "openclaw.sqlite", state / "agents", state / "claude-code"
    )


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _owner_run(started_at_ms: int, *, ended_at_ms: int | None = None) -> ClaudeRun:
    return ClaudeRun(
        run_id=OWNER_RUN_ID,
        agent_id="research-orchestrator",
        session_key=OWNER_SESSION,
        requester_session_key=OWNER_SESSION,
        runtime="cli",
        status="running" if ended_at_ms is None else "succeeded",
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        model="claude-opus-5-5",
        thinking_level=None,
        claude_session_id="owner-claude-session",
        transcript_models=frozenset({"claude-opus-5-5"}),
        transcript_efforts=frozenset({"high"}),
        task_text=None,
        cwd=None,
    )


class Fixture:
    def __init__(
        self,
        store: ResearchStore,
        attempt_id: str,
        bundle: Path,
        stores: ClaudeStores,
        host: FakeClaudeHost,
    ) -> None:
        self.store = store
        self.attempt_id = attempt_id
        self.bundle = bundle
        self.stores = stores
        self.host = host
        self.reservation: ReviewReservation | None = None

    def reserve(self) -> ReviewReservation:
        _status, resume_seq = self.store.campaign()
        wake_key = canonical_wake_key("H0001", self.attempt_id, "IMPLEMENTED", resume_seq)
        if self.store.wake_row(wake_key) is None:
            assert self.store.reserve_wake(wake_key, self.attempt_id, "IMPLEMENTED", resume_seq)
            self.store.complete_wake(wake_key, OWNER_RUN_ID)
        wake = self.store.wake_row(wake_key)
        assert wake is not None
        started = _epoch_ms(str(wake["sent_at"])) + 1
        self.host.add(_owner_run(started), "pending")
        time.sleep(0.01)
        self.reservation = reserve_review(
            self.store,
            self.attempt_id,
            self.bundle,
            OWNER_SESSION,
            wake_pending_key=wake_key,
            openclaw_database=self.stores.openclaw_state_db,
            claude_stores=self.stores,
            profile=PROFILE,
        )
        return self.reservation

    def child(self, **overrides: object) -> ClaudeRun:
        reservation = self.reservation
        assert reservation is not None
        run = ClaudeRun(
            run_id=CHILD_RUN_ID,
            agent_id="research-reviewer",
            session_key=CHILD_SESSION,
            requester_session_key=OWNER_SESSION,
            runtime="subagent",
            status="succeeded",
            started_at_ms=_epoch_ms(reservation.reserved_at) + 50,
            ended_at_ms=_epoch_ms(reservation.reserved_at) + 5000,
            model=REVIEWER.model_id,
            thinking_level=REVIEWER.effort,
            claude_session_id="child-claude-session",
            transcript_models=frozenset({REVIEWER.model_id}),
            transcript_efforts=frozenset({str(REVIEWER.effort)}),
            task_text=reservation.spawn_arguments["task"],
            cwd=reservation.bundle_dir,
            result_text=self.verdict("PASS"),
        )
        return replace(run, **overrides)  # type: ignore[arg-type]

    def verdict(self, verdict: str) -> str:
        reservation = self.reservation
        assert reservation is not None
        return json.dumps(
            {
                "verdict": verdict,
                "attempt_id": self.attempt_id,
                "commit": reservation.commit,
                "spec_sha256": reservation.hypothesis_spec_sha256,
                "findings": [] if verdict == "PASS" else ["bad"],
            }
        )

    def reconcile(self) -> ReviewAck | None:
        return reconcile_review(
            self.store,
            self.attempt_id,
            self.stores.openclaw_state_db,
            claude_stores=self.stores,
            profile=PROFILE,
        )

    def collect(self) -> Attempt:
        return collect_review(
            self.store,
            self.attempt_id,
            self.stores.openclaw_state_db,
            claude_stores=self.stores,
            profile=PROFILE,
        )


@pytest.fixture
def fx(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, host: FakeClaudeHost
) -> Fixture:
    store, _source, _hypothesis, attempt_id, bundle = fixture_setup(campaign, tmp_path)
    return Fixture(store, attempt_id, bundle, _stores(tmp_path), host)


def test_reserve_builds_a_research_reviewer_sessions_spawn(fx: Fixture) -> None:
    reservation = fx.reserve()
    arguments = reservation.spawn_arguments
    assert reservation.is_claude
    assert set(arguments) == {"agentId", "cwd", "model", "task", "taskName", "thinking"}
    assert arguments["agentId"] == "research-reviewer"
    assert arguments["model"] == REVIEWER.model_ref
    assert arguments["thinking"] == REVIEWER.effort
    assert arguments["cwd"] == reservation.bundle_dir
    assert reservation.bundle_dir in arguments["task"]
    assert f"Review label: {arguments['taskName']}" in arguments["task"]
    # The stored payload loads back and a second reserve returns the stored reservation.
    assert fx.reserve() == reservation


def test_reserve_refuses_an_unrecorded_owner_run(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()  # records the wake
    host.runs.clear()
    with pytest.raises(ReviewUnresolved, match="not recorded"):
        _again(fx)


def _again(fx: Fixture) -> None:
    _status, resume_seq = fx.store.campaign()
    wake_key = canonical_wake_key("H0001", fx.attempt_id, "IMPLEMENTED", resume_seq)
    reserve_review(
        fx.store,
        fx.attempt_id,
        fx.bundle,
        OWNER_SESSION,
        wake_pending_key=wake_key,
        openclaw_database=fx.stores.openclaw_state_db,
        claude_stores=fx.stores,
        profile=PROFILE,
    )


def test_reserve_requires_a_claude_profile(fx: Fixture) -> None:
    codex = load_profile("codex")
    with pytest.raises(ReviewEvidenceError, match="claude-cli"):
        review_evidence._claude_reviewer(codex, fx.stores)


def test_accept_and_idempotent_recollect(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child())
    ack = fx.reconcile()
    assert ack is not None
    assert fx.reconcile() == ack
    attempt = fx.collect()
    assert attempt.state is AttemptState.REVIEW_PASSED
    stored = fx.store.get_attempt(fx.attempt_id)
    assert stored.reported_reviewer_model == REVIEWER.model_ref
    assert stored.reported_reviewer_actual_model == REVIEWER.model_ref
    evidence = json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence"))
    assert evidence["child_thread_id"] == CHILD_RUN_ID
    assert evidence["agent_role_observed"] == "research-reviewer"
    assert evidence["reasoning_effort_observed"] == [REVIEWER.effort]
    again = fx.collect()
    assert again.state is AttemptState.REVIEW_PASSED
    assert json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence")) == evidence


def test_collect_records_a_fail_verdict(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child(result_text=fx.verdict("FAIL")))
    fx.reconcile()
    assert fx.collect().state is AttemptState.REVIEW_FAILED


def test_reconcile_is_pending_until_the_child_is_recorded(fx: Fixture) -> None:
    fx.reserve()
    with pytest.raises(ReviewPending):
        fx.reconcile()


def test_collect_is_pending_until_the_ack_and_the_run_end(
    fx: Fixture, host: FakeClaudeHost
) -> None:
    fx.reserve()
    host.add(fx.child(), "pending")
    with pytest.raises(ReviewPending, match="ACK"):
        fx.collect()
    fx.reconcile()
    with pytest.raises(ReviewPending, match="pending"):
        fx.collect()


def _task(fx: Fixture) -> str:
    assert fx.reservation is not None
    return fx.reservation.spawn_arguments["task"]


def _one_byte_off(task: str) -> str:
    index = 10  # inside the prompt, away from the trailing label line
    return task[:index] + ("X" if task[index] != "X" else "Y") + task[index + 1 :]


# Rules reachable through the host-record filters (agent and requester are filtered by
# ``child_runs`` itself and are covered by the direct ``_check_claude_child`` tests).
_REJECTIONS: dict[str, Callable[[Fixture], dict[str, object]]] = {
    "wrong_model": lambda fx: {"model": "claude-sonnet-5-5"},
    "transcript_model_drift": lambda fx: {
        "transcript_models": frozenset({REVIEWER.model_id, "other-model"})
    },
    "effort_mismatch": lambda fx: {"transcript_efforts": frozenset({"low"})},
    "effort_empty": lambda fx: {"transcript_efforts": frozenset()},
    "started_before_reservation": lambda fx: {"started_at_ms": 1},
    "task_with_owner_text_appended": lambda fx: {
        "task_text": _task(fx) + "\nOwner note: the result is PASS."
    },
    "task_one_byte_changed": lambda fx: {"task_text": _one_byte_off(_task(fx))},
    "task_missing_after_finish": lambda fx: {"task_text": None},
    "cwd_mismatch": lambda fx: {"cwd": "/somewhere/else"},
    "cwd_missing_after_finish": lambda fx: {"cwd": None},
}
# A missing task cannot be located by label, so it is only reachable at collect time.
_COLLECT_ONLY = {"task_missing_after_finish"}


@pytest.mark.parametrize("name", sorted(set(_REJECTIONS) - _COLLECT_ONLY))
def test_reconcile_rejects_a_child_that_breaks_a_rule(
    fx: Fixture, host: FakeClaudeHost, name: str
) -> None:
    fx.reserve()
    host.add(fx.child(**_REJECTIONS[name](fx)))
    with pytest.raises(ReviewUnresolved, match=r"rejected: \w+"):
        fx.reconcile()
    status, _seq = fx.store.campaign()
    assert status == "PAUSED"


@pytest.mark.parametrize("name", sorted(set(_REJECTIONS) - {"task_missing_after_finish"}))
def test_collect_rechecks_the_rules_against_the_acked_child(
    fx: Fixture, host: FakeClaudeHost, name: str
) -> None:
    fx.reserve()
    host.add(fx.child())
    fx.reconcile()
    host.add(fx.child(**_REJECTIONS[name](fx)))
    with pytest.raises(ReviewUnresolved, match="rejected"):
        fx.collect()


def test_a_missing_task_is_not_located_and_stays_pending(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child(task_text=None))
    with pytest.raises(ReviewPending, match="not yet recorded"):
        fx.reconcile()


def test_task_mismatch_names_its_rule_and_does_not_ack(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child(task_text=_task(fx) + "\nextra"))
    with pytest.raises(ReviewUnresolved, match="task_mismatch"):
        fx.reconcile()
    assert fx.store.campaign()[0] == "PAUSED"
    with pytest.raises(ReviewPending, match="ACK"):
        fx.collect()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"agent_id": "research-implementer"}, "wrong_agent"),
        ({"requester_session_key": "agent:other:session"}, "wrong_requester"),
        ({"requester_session_key": None}, "wrong_requester"),
    ],
)
def test_check_rejects_the_filtered_identity_rules_directly(
    fx: Fixture, overrides: dict[str, object], reason: str
) -> None:
    fx.reserve()
    assert fx.reservation is not None
    with pytest.raises(review_evidence._ClaudeChildRejected) as raised:
        review_evidence._check_claude_child(fx.child(**overrides), fx.reservation, REVIEWER, "ok")
    assert raised.value.reason == reason


@pytest.mark.parametrize(
    "overrides",
    [
        {"started_at_ms": None},
        {"transcript_models": frozenset()},
        {"transcript_efforts": frozenset()},
        {"model": None},
        {"cwd": None},
    ],
)
def test_incomplete_evidence_of_a_running_child_is_pending_not_rejected(
    fx: Fixture, host: FakeClaudeHost, overrides: dict[str, object]
) -> None:
    fx.reserve()
    host.add(fx.child(status="running", ended_at_ms=None, **overrides), "pending")
    with pytest.raises(ReviewPending, match="incomplete"):
        fx.reconcile()
    assert fx.store.campaign()[0] != "PAUSED"


def test_a_running_child_with_a_positive_mismatch_is_still_rejected(
    fx: Fixture, host: FakeClaudeHost
) -> None:
    fx.reserve()
    host.add(fx.child(status="running", ended_at_ms=None, model="claude-sonnet-5-5"), "pending")
    with pytest.raises(ReviewUnresolved, match="wrong_model"):
        fx.reconcile()


def test_a_child_created_before_the_reservation_is_not_found(
    fx: Fixture, host: FakeClaudeHost
) -> None:
    reservation = fx.reserve()
    host.add(fx.child(), created_at_ms=_epoch_ms(reservation.reserved_at) - 1)
    with pytest.raises(ReviewPending, match="not yet recorded"):
        fx.reconcile()


def test_the_located_child_must_be_the_acknowledged_run(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child())
    fx.reconcile()
    # The acked run stops carrying the label; another run claims it.
    host.add(fx.child(task_text="unrelated"))
    host.add(fx.child(run_id="child-run-2", session_key=CHILD_SESSION + "2"))
    with pytest.raises(ReviewUnresolved, match="differs from ACK"):
        fx.collect()
    assert fx.store.campaign()[0] == "PAUSED"


@pytest.mark.parametrize("outcome", ["error", "lost"])
def test_outcome_not_ok_records_a_fail_with_the_outcome_as_reason(
    fx: Fixture, host: FakeClaudeHost, outcome: Outcome
) -> None:
    fx.reserve()
    host.add(fx.child(status="failed"), outcome)
    fx.reconcile()
    assert fx.collect().state is AttemptState.REVIEW_FAILED
    evidence = json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence"))
    assert evidence["reason"] == outcome
    assert evidence["verdict"] == "FAIL"


def test_missing_result_text_fails_closed(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child(result_text=None))
    fx.reconcile()
    assert fx.collect().state is AttemptState.REVIEW_FAILED


def test_two_children_with_the_label_are_ambiguous(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child())
    host.add(fx.child(run_id="child-run-2", session_key=CHILD_SESSION + "x"))
    with pytest.raises(ReviewUnresolved, match="correlation"):
        fx.reconcile()


def test_child_that_vanishes_after_the_ack_is_unresolved(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child())
    fx.reconcile()
    del host.runs[CHILD_RUN_ID]
    with pytest.raises(ReviewUnresolved, match="vanished"):
        fx.collect()


def test_cancel_reports_a_terminal_child(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child())

    async def never(*args: object, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("a finished child needs no chat.abort")

    outcome = cancel_review(
        fx.store,
        fx.attempt_id,
        "operator",
        fx.stores.openclaw_state_db,
        never,
        claude_stores=fx.stores,
        profile=PROFILE,
    )
    assert outcome.pending is False
    assert outcome.status == "succeeded"
    assert outcome.task_id == CHILD_RUN_ID


def test_cancel_aborts_the_owner_run_of_a_running_child(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child(status="running", ended_at_ms=None), "pending")
    calls: list[tuple[str, str, str]] = []

    async def abort(session_key: str, agent_id: str, run_id: str) -> Mapping[str, object]:
        calls.append((session_key, agent_id, run_id))
        await asyncio.sleep(0)
        return {"ok": True, "aborted": False, "runIds": []}

    outcome = cancel_review(
        fx.store,
        fx.attempt_id,
        "operator",
        fx.stores.openclaw_state_db,
        abort,
        claude_stores=fx.stores,
        profile=PROFILE,
    )
    assert calls == [(OWNER_SESSION, "research-orchestrator", OWNER_RUN_ID)]
    assert outcome.pending is True
    assert outcome.rpc_result == review_evidence.CANCEL_OWNER_INACTIVE


def test_codex_reservation_is_not_claude(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    from tests.gateway.research.test_review_evidence import _reserve_native

    store, _source, _hypothesis, attempt_id, bundle = fixture_setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path)
    assert not reservation.is_claude
    assert reservation.spawn_arguments["agent_type"] == review_evidence.REVIEW_AGENT


def test_host_record_errors_during_reconcile_pause(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.reserve()

    def boom(*args: object, **kwargs: object) -> tuple[ClaudeRun, ...]:
        raise HostRecordError("sqlite unreadable")

    monkeypatch.setattr(review_evidence, "child_runs", boom)
    with pytest.raises(ReviewUnresolved, match="correlation"):
        fx.reconcile()


@pytest.fixture(autouse=False)
def fake_claude_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.research import cli as research_cli

    def make(state_dir: Path | None = None) -> ClaudeStores:
        assert state_dir is not None
        return ClaudeStores(
            state_dir / "state" / "openclaw.sqlite",
            state_dir / "agents",
            state_dir / "claude-code",
        )

    monkeypatch.setattr(research_cli, "claude_stores", make)


def _cli_args(fx: Fixture, command: str, *extra: str) -> list[str]:
    return ["research", command, fx.attempt_id, "--root", str(fx.store.root), *extra]


def test_cli_reserve_collect_use_the_state_dir_under_claude(
    fx: Fixture, host: FakeClaudeHost, fake_claude_stores: None
) -> None:
    from gateway.agent_profiles import write_deployed_profile
    from gateway.cli import app
    from typer.testing import CliRunner

    state = fx.store.root.resolve().parent
    write_deployed_profile(PROFILE, state)
    stores = ClaudeStores(
        state / "state" / "openclaw.sqlite", state / "agents", state / "claude-code"
    )
    fx.stores = stores
    _status, resume_seq = fx.store.campaign()
    wake_key = canonical_wake_key("H0001", fx.attempt_id, "IMPLEMENTED", resume_seq)
    assert fx.store.reserve_wake(wake_key, fx.attempt_id, "IMPLEMENTED", resume_seq)
    fx.store.complete_wake(wake_key, OWNER_RUN_ID)
    wake = fx.store.wake_row(wake_key)
    assert wake is not None
    host.add(_owner_run(_epoch_ms(str(wake["sent_at"])) + 1), "pending")
    time.sleep(0.01)
    runner = CliRunner()
    reserve = runner.invoke(
        app,
        _cli_args(
            fx,
            "review-reserve",
            "--bundle-dir",
            str(fx.bundle),
            "--owner-key",
            OWNER_SESSION,
            "--wake-key",
            wake_key,
            "--openclaw-state-dir",
            str(state),
        ),
    )
    assert reserve.exit_code == 0, reserve.output
    payload = json.loads(reserve.output)
    assert payload["model"] == REVIEWER.model_ref
    assert payload["effort"] == REVIEWER.effort
    assert payload["spawn_arguments"]["agentId"] == "research-reviewer"
    fx.reservation = review_evidence._reservation(fx.store, fx.attempt_id)
    host.add(fx.child())
    for command in ("review-reconcile", "review-collect"):
        result = runner.invoke(app, _cli_args(fx, command, "--openclaw-state-dir", str(state)))
        assert result.exit_code == 0, result.output
    assert fx.store.get_attempt(fx.attempt_id).state is AttemptState.REVIEW_PASSED


def _claude_root(fx: Fixture) -> Path:
    from gateway.agent_profiles import write_deployed_profile

    state = fx.store.root.resolve().parent
    write_deployed_profile(PROFILE, state)
    return state


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--openclaw-state-dir", "{foreign}"], "managed OpenClaw state"),
        (["--openclaw-database", "{foreign}/x.sqlite"], "not valid under the claude profile"),
        (["--codex-state-database", "{foreign}/x.sqlite"], "not valid under the claude profile"),
    ],
)
def test_cli_claude_rejects_foreign_state_and_codex_options(
    fx: Fixture, tmp_path: Path, fake_claude_stores: None, extra: list[str], message: str
) -> None:
    from gateway.cli import app
    from typer.testing import CliRunner

    _claude_root(fx)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    args = [item.format(foreign=foreign) for item in extra]
    result = CliRunner().invoke(app, _cli_args(fx, "review-collect", *args))
    assert result.exit_code == 1
    assert message in result.output


def test_cli_claude_state_dir_defaults_to_the_root_and_checks_the_core_database(
    fx: Fixture, tmp_path: Path, fake_claude_stores: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.cli import app
    from typer.testing import CliRunner

    _claude_root(fx)
    monkeypatch.setenv(
        "RESEARCH_CORE_DATABASE", str(tmp_path / "other" / "state" / "openclaw.sqlite")
    )
    result = CliRunner().invoke(app, _cli_args(fx, "review-collect"))
    assert result.exit_code == 1
    assert "RESEARCH_CORE_DATABASE does not match" in result.output
    monkeypatch.delenv("RESEARCH_CORE_DATABASE")
    # Without the flag the root's own state directory is used (no reservation yet).
    result = CliRunner().invoke(app, _cli_args(fx, "review-collect"))
    assert result.exit_code == 1
    assert "reservation is missing" in result.output


def test_cli_codex_still_requires_the_native_databases(fx: Fixture) -> None:
    from gateway.cli import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, _cli_args(fx, "review-collect"))
    assert result.exit_code == 1
    assert "--codex-state-database" in result.output


def test_cli_codex_rejects_the_claude_state_dir_option(fx: Fixture, tmp_path: Path) -> None:
    from gateway.cli import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app, _cli_args(fx, "review-collect", "--openclaw-state-dir", str(tmp_path))
    )
    assert result.exit_code == 1
    assert "only valid under the claude agent profile" in result.output


def test_cli_service_tier_is_required_under_codex_and_optional_under_claude(fx: Fixture) -> None:
    from gateway.research import cli as research_cli

    with pytest.raises(ValueError, match="coder_service_tier is required"):
        research_cli._require_service_tier(fx.store.root, None)
    research_cli._require_service_tier(fx.store.root, "fast")
    _claude_root(fx)
    research_cli._require_service_tier(fx.store.root, None)


def test_serve_passes_its_session_key_to_the_dispatcher(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.research import cli as research_cli

    seen: list[str | None] = []
    monkeypatch.setattr(research_cli, "OpenClawWakeSender", lambda *args: object())
    monkeypatch.setattr(
        research_cli, "_dispatch_queued_job", lambda store, key=None: seen.append(key)
    )
    monkeypatch.setattr(research_cli, "compose_wake", lambda store: None)
    monkeypatch.setattr(research_cli, "poll_owner_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_cli, "_reconcile_jobs", lambda store: None)
    from gateway.cli import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app,
        [
            "research",
            "serve",
            "--session-key",
            OWNER_SESSION,
            "--root",
            str(fx.store.root),
            "--once",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [OWNER_SESSION]


def test_native_execution_ready_receives_the_owner_session_key(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.research import cli as research_cli

    calls: list[tuple[object, ...]] = []

    def ready(
        store: ResearchStore, attempt_id: str | None, owner_session_key: str | None = None
    ) -> None:
        calls.append((store, attempt_id, owner_session_key))

    monkeypatch.setattr(research_cli, "native_execution_ready", ready)
    assert research_cli._native_execution_ready(fx.store, "A", OWNER_SESSION) is None
    assert research_cli._native_execution_ready(fx.store, "A") is None
    assert calls[0][2] == OWNER_SESSION
    assert calls[1][2] is None


def test_implementation_record_service_tier_is_optional() -> None:
    from gateway.research.contracts import ImplementationRecord

    def record(tier: str | None) -> ImplementationRecord:
        return ImplementationRecord(
            "H0001-A001",
            "a" * 40,
            ("python", "-m", "target"),
            "/tmp/tests.json",
            "anthropic/claude-sonnet-5-5",
            "xhigh",
            tier,
            "2026-01-01T00:00:00Z",
        )

    claude = record(None)
    assert ImplementationRecord.from_json(claude.to_json()) == claude
    assert json.loads(claude.to_json())["coder_service_tier"] is None
    # A codex record serializes exactly as before.
    codex = record("fast")
    assert json.loads(codex.to_json())["coder_service_tier"] == "fast"
    assert ImplementationRecord.from_json(codex.to_json()) == codex
    with pytest.raises(ValueError, match="coder_service_tier"):
        record("")


def _prepare_wake(fx: Fixture) -> tuple[str, int]:
    _status, resume_seq = fx.store.campaign()
    wake_key = canonical_wake_key("H0001", fx.attempt_id, "IMPLEMENTED", resume_seq)
    if fx.store.wake_row(wake_key) is None:
        assert fx.store.reserve_wake(wake_key, fx.attempt_id, "IMPLEMENTED", resume_seq)
        fx.store.complete_wake(wake_key, OWNER_RUN_ID)
    wake = fx.store.wake_row(wake_key)
    assert wake is not None
    return wake_key, _epoch_ms(str(wake["sent_at"]))


def _reserve_with(fx: Fixture, owner: ClaudeRun, outcome: Outcome = "pending") -> ReviewReservation:
    wake_key, _sent = _prepare_wake(fx)
    fx.host.add(owner, outcome)
    time.sleep(0.01)
    return reserve_review(
        fx.store,
        fx.attempt_id,
        fx.bundle,
        OWNER_SESSION,
        wake_pending_key=wake_key,
        openclaw_database=fx.stores.openclaw_state_db,
        claude_stores=fx.stores,
        profile=PROFILE,
    )


def test_reserve_rejects_an_owner_run_of_another_session(fx: Fixture) -> None:
    _wake_key, sent = _prepare_wake(fx)
    owner = replace(_owner_run(sent + 1), session_key="agent:research-orchestrator:other")
    with pytest.raises(ReviewUnresolved, match="different session"):
        _reserve_with(fx, owner)


def test_reserve_rejects_an_owner_run_that_already_ended(fx: Fixture) -> None:
    _wake_key, sent = _prepare_wake(fx)
    with pytest.raises(ReviewUnresolved, match="already ended"):
        _reserve_with(fx, _owner_run(sent + 1, ended_at_ms=sent + 2), "ok")


def test_reserve_rejects_an_owner_run_that_is_not_pending(fx: Fixture) -> None:
    _wake_key, sent = _prepare_wake(fx)
    with pytest.raises(ReviewUnresolved, match="not the active running writer"):
        _reserve_with(fx, _owner_run(sent + 1), "error")


def test_reserve_rejects_an_owner_run_that_starts_after_the_reservation(fx: Fixture) -> None:
    _wake_key, _sent = _prepare_wake(fx)
    future = int(time.time() * 1000) + 3_600_000
    with pytest.raises(ReviewUnresolved, match="starts after the reservation"):
        _reserve_with(fx, _owner_run(future))


def test_reserve_replay_conflicts_when_the_owner_run_differs(fx: Fixture) -> None:
    fx.reserve()
    other = replace(_owner_run(1), run_id="owner-run-2")
    fx.host.add(other, "pending")
    with pytest.raises(StoreConflict, match="differs from stored reservation"):
        review_evidence._reserve_claude(
            fx.store,
            fx.attempt_id,
            fx.bundle,
            OWNER_SESSION,
            instructions=None,
            owner_run_id="owner-run-2",
            stores=fx.stores,
            spec=REVIEWER,
        )


def _stored_payload(fx: Fixture) -> dict[str, object]:
    reservation = fx.reserve()
    payload: dict[str, object] = json.loads(reservation.to_json())
    assert review_evidence._reservation_from_payload(payload) == reservation
    return payload


def _tamper(payload: dict[str, object], key: str, value: object) -> dict[str, object]:
    arguments = dict(payload["spawn_arguments"])  # type: ignore[call-overload]
    arguments[key] = value
    return {**payload, "spawn_arguments": arguments}


def test_tampered_claude_reservations_conflict(fx: Fixture) -> None:
    payload = _stored_payload(fx)
    arguments = payload["spawn_arguments"]
    assert isinstance(arguments, dict)
    cases: list[dict[str, object]] = [
        _tamper(payload, "agentId", "research-implementer"),
        _tamper(payload, "taskName", "review_other"),
        _tamper(payload, "cwd", "/elsewhere"),
        _tamper(payload, "task", str(arguments["task"]) + "x"),  # digest no longer matches
        {**payload, "spawn_arguments": {**arguments, "extra": "key"}},
        {**payload, "owner_thread_id": "thread-1"},
        {**payload, "owner_run_id": None},
        {**payload, "prompt_sha256": "0" * 64},
    ]
    for tampered in cases:
        with pytest.raises(StoreConflict):
            review_evidence._reservation_from_payload(tampered)


def test_claude_reservation_without_stores_pauses_on_reconcile_and_is_unresolved_on_cancel(
    fx: Fixture,
) -> None:
    fx.reserve()
    with pytest.raises(ReviewUnresolved, match="identity is unresolved"):
        reconcile_review(fx.store, fx.attempt_id, fx.stores.openclaw_state_db)
    assert fx.store.campaign()[0] == "PAUSED"

    async def never(*args: object, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("no abort without a verified child")

    outcome = cancel_review(fx.store, fx.attempt_id, "operator", fx.stores.openclaw_state_db, never)
    assert (outcome.status, outcome.pending, outcome.rpc_result) == (
        "unresolved",
        True,
        "not_requested",
    )


def test_a_codex_reservation_is_refused_by_the_claude_verifier(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, host: FakeClaudeHost
) -> None:
    from tests.gateway.research.test_review_evidence import _reserve_native

    store, _source, _hypothesis, attempt_id, bundle = fixture_setup(campaign, tmp_path)
    _reserve_native(store, attempt_id, bundle, tmp_path)
    with pytest.raises(ReviewEvidenceError, match="not a Claude spawn"):
        review_evidence.verify_claude_review(store, attempt_id, _stores(tmp_path), PROFILE)


@pytest.mark.parametrize(
    ("make_text", "reason"),
    [
        (lambda fx: "Here is my verdict: " + fx.verdict("PASS"), "bare JSON"),
        (lambda fx: fx.verdict("PASS")[:-1] + ',"verdict":"PASS"}', "duplicate keys"),
        (lambda fx: fx.verdict("PASS")[:-1] + ',"extra":1}', "unexpected keys"),
        (
            lambda fx: json.dumps({**json.loads(fx.verdict("PASS")), "commit": "b" * 40}),
            "verdict_binding",
        ),
        (
            lambda fx: json.dumps({**json.loads(fx.verdict("PASS")), "spec_sha256": "c" * 64}),
            "verdict_binding",
        ),
        (
            lambda fx: json.dumps({**json.loads(fx.verdict("PASS")), "attempt_id": "H0001-A999"}),
            "verdict_binding",
        ),
        (lambda fx: json.dumps({**json.loads(fx.verdict("PASS")), "findings": [""]}), "malformed"),
    ],
)
def test_unparseable_or_misbound_verdicts_fail_closed(
    fx: Fixture,
    host: FakeClaudeHost,
    make_text: Callable[[Fixture], str],
    reason: str,
) -> None:
    fx.reserve()
    host.add(fx.child(result_text=make_text(fx)))
    fx.reconcile()
    assert fx.collect().state is AttemptState.REVIEW_FAILED
    evidence = json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence"))
    assert evidence["verdict"] == "FAIL"
    assert reason in evidence["reason"]


def test_a_modified_bundle_fails_the_review(fx: Fixture, host: FakeClaudeHost) -> None:
    reservation = fx.reserve()
    host.add(fx.child())
    fx.reconcile()
    bundle = Path(reservation.bundle_dir)
    bundle.chmod(0o755)
    (bundle / "extra.txt").write_text("changed", encoding="utf-8")
    assert fx.collect().state is AttemptState.REVIEW_FAILED
    evidence = json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence"))
    assert evidence["reason"].startswith("bundle_invalid") or evidence["reason"] == "bundle_mutated"


def test_cancel_replay_does_not_resend_the_abort(fx: Fixture, host: FakeClaudeHost) -> None:
    fx.reserve()
    host.add(fx.child(status="running", ended_at_ms=None), "pending")
    calls: list[str] = []

    async def abort(session_key: str, agent_id: str, run_id: str) -> Mapping[str, object]:
        calls.append(run_id)
        return {"ok": True, "aborted": False, "runIds": []}

    def cancel() -> review_evidence.CancelOutcome:
        return cancel_review(
            fx.store,
            fx.attempt_id,
            "operator",
            fx.stores.openclaw_state_db,
            abort,
            claude_stores=fx.stores,
            profile=PROFILE,
        )

    first = cancel()
    second = cancel()
    assert calls == [OWNER_RUN_ID]
    assert (first.pending, second.pending) == (True, True)
    assert second.task_id == CHILD_RUN_ID
    host.add(fx.child(status="cancelled"), "error")
    third = cancel()
    assert calls == [OWNER_RUN_ID]
    assert (third.pending, third.status) == (False, "cancelled")


def test_cancel_aborts_a_correlated_child_even_if_its_model_is_wrong(
    fx: Fixture, host: FakeClaudeHost
) -> None:
    fx.reserve()
    host.add(fx.child(status="running", ended_at_ms=None, model="claude-sonnet-5-5"), "pending")
    calls: list[str] = []

    async def abort(session_key: str, agent_id: str, run_id: str) -> Mapping[str, object]:
        calls.append(run_id)
        return {"ok": True, "aborted": True, "runIds": [run_id]}

    outcome = cancel_review(
        fx.store,
        fx.attempt_id,
        "operator",
        fx.stores.openclaw_state_db,
        abort,
        claude_stores=fx.stores,
        profile=PROFILE,
    )
    assert calls == [OWNER_RUN_ID]
    assert outcome.rpc_result == review_evidence.CANCEL_ABORTED


@pytest.mark.parametrize("overrides", [{"task_text_suffix": True}, {"cwd": "/elsewhere"}])
def test_cancel_refuses_a_child_whose_task_or_cwd_differs(
    fx: Fixture, host: FakeClaudeHost, overrides: dict[str, object]
) -> None:
    fx.reserve()
    changed = dict(overrides)
    if changed.pop("task_text_suffix", None):
        changed["task_text"] = _task(fx) + "\nowner text"
    host.add(fx.child(status="running", ended_at_ms=None, **changed), "pending")

    async def never(*args: object, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("an uncorrelated child must not trigger an abort")

    outcome = cancel_review(
        fx.store,
        fx.attempt_id,
        "operator",
        fx.stores.openclaw_state_db,
        never,
        claude_stores=fx.stores,
        profile=PROFILE,
    )
    assert (outcome.status, outcome.pending) == ("unresolved", True)


def test_claude_bundle_instructions_name_the_cwd_and_codex_bytes_are_unchanged(
    fx: Fixture,
) -> None:
    reservation = fx.reserve()
    text = (Path(reservation.bundle_dir) / "instructions.md").read_text(encoding="utf-8")
    assert "the spawn cwd is this bundle directory" in text
    assert "no cwd" not in text


def test_codex_bundle_instructions_keep_the_no_cwd_wording(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    from tests.gateway.research.test_review_evidence import _reserve_native

    store, _source, _hypothesis, attempt_id, bundle = fixture_setup(campaign, tmp_path)
    reservation = _reserve_native(store, attempt_id, bundle, tmp_path)
    text = (Path(reservation.bundle_dir) / "instructions.md").read_text(encoding="utf-8")
    assert "the native spawn has no cwd, so use that absolute path exactly." in text


def test_host_evidence_records_the_session_thinking_and_transcript_digest(
    fx: Fixture, host: FakeClaudeHost
) -> None:
    import hashlib

    fx.reserve()
    host.add(fx.child(thinking_level="xhigh"))
    projects = fx.stores.claude_config_dir / "projects" / "-slug"
    (projects / "child-claude-session" / "subagents").mkdir(parents=True)
    main = projects / "child-claude-session.jsonl"
    sub = projects / "child-claude-session" / "subagents" / "agent-1.jsonl"
    main.write_bytes(b'{"type":"api-request"}\n')
    sub.write_bytes(b'{"type":"api-request","sub":1}\n')
    fx.reconcile()
    fx.collect()
    evidence = json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence"))
    assert evidence["claude_session_id"] == "child-claude-session"
    assert evidence["thinking_level"] == "xhigh"
    outer = hashlib.sha256()
    for path in (main, sub):
        inner = hashlib.sha256(path.read_bytes()).hexdigest()
        outer.update(f"-slug/{path.relative_to(projects).as_posix()}\0{inner}\n".encode())
    assert evidence["transcript_sha256"] == outer.hexdigest()


def _write_transcripts(fx: Fixture, slug: str = "-slug") -> dict[str, Path]:
    projects = fx.stores.claude_config_dir / "projects" / slug
    nested = projects / "child-claude-session" / "workflows" / "wf-1"
    nested.mkdir(parents=True)
    files = {
        "main": projects / "child-claude-session.jsonl",
        "journal": nested / "journal.jsonl",
    }
    files["main"].write_bytes(b'{"type":"api-request"}\n')
    files["journal"].write_bytes(b'{"type":"workflow"}\n')
    return files


def test_digest_covers_a_nested_workflow_journal_and_is_stable(fx: Fixture) -> None:
    import hashlib

    files = _write_transcripts(fx)
    digest, unavailable = review_evidence._transcript_digest(fx.stores, "child-claude-session")
    assert unavailable is None
    outer = hashlib.sha256()
    for relative, path in (
        ("-slug/child-claude-session.jsonl", files["main"]),
        ("-slug/child-claude-session/workflows/wf-1/journal.jsonl", files["journal"]),
    ):
        outer.update(f"{relative}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    assert digest == outer.hexdigest()
    assert review_evidence._transcript_digest(fx.stores, "child-claude-session") == (digest, None)
    files["journal"].write_bytes(b'{"type":"workflow","changed":true}\n')
    changed, _reason = review_evidence._transcript_digest(fx.stores, "child-claude-session")
    assert changed != digest


def test_digest_is_unavailable_with_a_reason_for_ambiguous_transcripts(fx: Fixture) -> None:
    _write_transcripts(fx, "-slug-a")
    _write_transcripts(fx, "-slug-b")
    digest, reason = review_evidence._transcript_digest(fx.stores, "child-claude-session")
    assert digest is None
    assert reason is not None and "ambiguous" in reason


def test_digest_is_unavailable_with_a_reason_for_symlinks(fx: Fixture, tmp_path: Path) -> None:
    files = _write_transcripts(fx)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"{}\n")
    (files["journal"].parent / "link.jsonl").symlink_to(outside)
    digest, reason = review_evidence._transcript_digest(fx.stores, "child-claude-session")
    assert digest is None
    assert reason is not None and "symlink" in reason


@pytest.mark.parametrize(
    ("session", "reason"),
    [
        (None, "no_claude_session_id"),
        ("no-such-session", "transcript_not_found"),
        ("../escape", "host_record_error"),
    ],
)
def test_digest_reports_why_it_is_unavailable(
    fx: Fixture, session: str | None, reason: str
) -> None:
    digest, why = review_evidence._transcript_digest(fx.stores, session)
    assert digest is None
    assert why is not None and why.startswith(reason)


def test_collect_records_the_unavailable_reason_instead_of_a_silent_none(
    fx: Fixture, host: FakeClaudeHost
) -> None:
    fx.reserve()
    host.add(fx.child())
    fx.reconcile()
    fx.collect()
    evidence = json.loads(fx.store.evidence(fx.attempt_id, "review_host_evidence"))
    assert evidence["transcript_sha256"] is None
    assert evidence["transcript_digest_unavailable"] == "transcript_not_found"


def test_codex_reviewer_constants_are_the_profile_values() -> None:
    assert review_evidence.REVIEW_MODEL == "gpt-5.6-sol"
    assert review_evidence.REVIEW_EFFORT == "xhigh"


def _claude_control_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, write_profile: bool = True
) -> Path:
    from gateway.agent_profiles import write_deployed_profile

    from tests.gateway.research.test_readiness import _owner_env_file

    state = tmp_path / "ctl-state"
    (state / "state").mkdir(parents=True)
    core = state / "state" / "openclaw.sqlite"
    core.write_bytes(b"")
    core.chmod(0o600)
    if write_profile:
        write_deployed_profile(PROFILE, state)
    for name in ("OPENCLAW_HOST", "OPENCLAW_PORT", "OPENCLAW_GATEWAY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    monkeypatch.delenv("RESEARCH_V2_ROOT", raising=False)
    env_file = _owner_env_file(tmp_path / "owner.env", core, state / "research-v2")
    monkeypatch.setenv("G2_OWNER_ENV_FILE", str(env_file))
    return state


def test_production_control_uses_claude_stores_under_the_claude_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.research import control

    state = _claude_control_env(tmp_path, monkeypatch)
    seen: list[Path | None] = []

    def make(state_dir: Path | None = None) -> ClaudeStores:
        seen.append(state_dir)
        assert state_dir is not None
        return ClaudeStores(
            state_dir / "state" / "openclaw.sqlite", state_dir / "agents", state_dir
        )

    monkeypatch.setattr(control, "claude_stores", make)
    owner = control.production_owner_control(state / "research-v2")
    assert isinstance(owner._review_canceller, control.OpenClawReviewCanceller)
    assert seen == [state]


def test_production_control_refuses_a_broken_agent_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.agent_profiles import deployed_profile_path
    from gateway.research import control

    state = _claude_control_env(tmp_path, monkeypatch)
    deployed_profile_path(state).write_text("{not json", encoding="utf-8")
    owner = control.production_owner_control(state / "research-v2")
    assert owner._review_canceller is None
    gate = owner._readiness_gate
    assert gate is not None
    assert "agent_profile:AgentProfileError" in str(gate(cast(Any, None)))


def test_production_control_refuses_unusable_claude_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.research import control

    state = _claude_control_env(tmp_path, monkeypatch)

    def broken(state_dir: Path | None = None) -> ClaudeStores:
        raise ClaudeRecordError("no stores")

    monkeypatch.setattr(control, "claude_stores", broken)
    owner = control.production_owner_control(state / "research-v2")
    assert owner._review_canceller is None
    assert owner._readiness_gate is not None
    assert "agent_profile:ClaudeRecordError" in str(owner._readiness_gate(cast(Any, None)))
