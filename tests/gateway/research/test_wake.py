from __future__ import annotations

from pathlib import Path

import pytest
from gateway.research.contracts import AttemptDecision, AttemptState, HypothesisSpec
from gateway.research.machine import queue_run, start_run
from gateway.research.store import ResearchStore
from gateway.research.wake import (
    OpenClawWakeSender,
    WakeRejected,
    WakeUncertain,
    compose_wake,
    deliver,
    poll_owner_turn,
)

from tests.gateway.research.conftest import implementation, review, verified_review


class Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, message: str, session_key: str, idempotency_key: str) -> str:
        self.calls.append((message, session_key, idempotency_key))
        return "run-1"

    def owner_task_status(self, _run_id: str) -> dict[str, object]:
        return {"status": "error"}


def test_wake_delivery_is_one_shot_until_resume(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    sender = Sender()
    plan = compose_wake(store)
    assert plan is not None
    assert deliver(store, sender, plan, "agent:owner") == "run-1"
    assert deliver(store, sender, plan, "agent:owner") is None
    assert len(sender.calls) == 1
    store.resume("test")
    next_plan = compose_wake(store)
    assert next_plan is not None and next_plan.pending_key != plan.pending_key


def test_wake_composes_each_state_and_failed_owner_turn_is_recorded(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    initial = compose_wake(store)
    assert initial is not None
    sender = Sender()
    deliver(store, sender, initial, "owner")
    poll_owner_turn(store, sender, required=True)
    assert store.wake_rows()[-1]["turn_status"] == "error"
    assert any(event.kind == "owner_turn_failed" for event in store.events())
    store.freeze(hypothesis.hypothesis_id)
    assert compose_wake(store).state == "FROZEN"  # type: ignore[union-attr]
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    assert compose_wake(store).state == "OPENED"  # type: ignore[union-attr]
    impl = implementation(attempt.attempt_id, "a" * 40)
    implemented = store.submit_implementation(attempt.attempt_id, impl)
    assert implemented.state == AttemptState.IMPLEMENTED
    assert compose_wake(store).state == "IMPLEMENTED"  # type: ignore[union-attr]
    passed = verified_review(store, review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256))
    assert passed.state == AttemptState.REVIEW_PASSED
    assert compose_wake(store).state == "REVIEW_PASSED"  # type: ignore[union-attr]
    running = store.set_state(
        __import__("gateway.research.machine", fromlist=["queue_run"]).start_run(
            __import__("gateway.research.machine", fromlist=["queue_run"]).queue_run(
                passed, "2026-01-01T00:00:00Z"
            ),
            "run-job",
            "2026-01-01T00:00:00Z",
        ),
        event="run_started",
    )
    assert running.state == AttemptState.RUNNING
    assert compose_wake(store) is None
    store.resume("again")
    plan = compose_wake(store)
    assert plan is None


def test_wake_delivery_failure_releases_reservation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    plan = compose_wake(store)
    assert plan is not None

    class FailingSender:
        def send(self, _message: str, _session: str, _key: str) -> str:
            raise RuntimeError("uncertain")

    with pytest.raises(RuntimeError):
        deliver(store, FailingSender(), plan, "owner")
    assert store.wake_row(plan.pending_key) is None


def test_uncertain_ack_keeps_reservation_for_same_key(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    plan = compose_wake(store)
    assert plan is not None

    class UncertainSender:
        def send(self, _message: str, _session: str, _key: str) -> str:
            raise WakeUncertain("transport timeout after acceptance")

    with pytest.raises(WakeUncertain):
        deliver(store, UncertainSender(), plan, "owner")
    row = store.wake_row(plan.pending_key)
    assert row is not None and row["run_id"] == "PENDING"

    class RecoverySender:
        def __init__(self) -> None:
            self.key: str | None = None

        def send(self, _message: str, _session: str, key: str) -> str:
            self.key = key
            return "run-recovered"

    recovery = RecoverySender()
    assert deliver(store, recovery, plan, "owner") == "run-recovered"
    assert recovery.key == plan.pending_key


def test_reserved_wake_after_crash_is_delivered_once(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    plan = compose_wake(store)
    assert plan is not None
    assert store.reserve_wake(plan.pending_key, plan.attempt_id, plan.state, plan.resume_seq)

    sender = Sender()
    assert deliver(store, sender, plan, "owner") == "run-1"
    assert len(sender.calls) == 1


def test_owner_poll_without_mapping_is_explicit_runtime_gate(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    plan = compose_wake(store)
    assert plan is not None
    deliver(store, Sender(), plan, "owner")

    class NoPoll:
        def send(self, _message: str, _session: str, _key: str) -> str:
            return "unused"

    poll_owner_turn(store, NoPoll())
    assert store.wake_rows()[-1]["turn_status"] == "pending_runtime_gate"


def test_unknown_owner_turn_status_is_persisted_without_renudge(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    plan = compose_wake(store)
    assert plan is not None
    deliver(store, Sender(), plan, "owner")

    class Unknown:
        def send(self, _message: str, _session: str, _key: str) -> str:
            return "unused"

        def owner_task_status(self, _run_id: str) -> dict[str, object]:
            return {"status": "future-status"}

    poll_owner_turn(store, Unknown())
    assert store.wake_rows()[-1]["turn_status"] == "future-status"
    assert not any(event.kind == "owner_turn_failed" for event in store.events())


def test_real_wake_and_wait_validate_session_and_run_id(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _source, _hypothesis = campaign
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def __init__(self, *_args: object) -> None:
            pass

        async def request_once(
            self, method: str, params: dict[str, object], *, timeout_seconds: float
        ) -> dict[str, object]:
            calls.append((method, params))
            if method == "agent":
                return {"status": "accepted", "runId": "run-actual", "sessionKey": "owner"}
            return {"runId": "run-actual", "status": "timeout"}

    monkeypatch.setattr("gateway.research.wake.OpenClawClient", Client)
    sender = OpenClawWakeSender("127.0.0.1", 1, "token")
    assert sender.send("message", "owner", "key") == "run-actual"
    assert sender.owner_task_status("run-actual")["status"] == "timeout"
    assert calls == [
        (
            "agent",
            {"message": "message", "sessionKey": "owner", "idempotencyKey": "key"},
        ),
        ("agent.wait", {"runId": "run-actual", "timeoutMs": 0}),
    ]

    class MismatchClient(Client):
        async def request_once(
            self, method: str, params: dict[str, object], *, timeout_seconds: float
        ) -> dict[str, object]:
            if method == "agent":
                return {"status": "accepted", "runId": "run-actual", "sessionKey": "other"}
            return {"runId": "different", "status": "pending"}

    monkeypatch.setattr("gateway.research.wake.OpenClawClient", MismatchClient)
    with pytest.raises(WakeRejected, match="owner session"):
        sender.send("message", "owner", "key")
    with pytest.raises(WakeRejected, match="runId"):
        sender.owner_task_status("run-actual")


def test_wake_rejects_missing_run_id_and_releases_reservation(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    plan = compose_wake(store)
    assert plan is not None

    class EmptySender:
        def send(self, _message: str, _session: str, _key: str) -> str:
            return ""

    with pytest.raises(WakeRejected, match="runId"):
        deliver(store, EmptySender(), plan, "owner")
    assert store.wake_row(plan.pending_key) is None


def test_wake_compose_terminal_states_requests_close_decision(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    store.submit_implementation(attempt.attempt_id, implementation(attempt.attempt_id, "a" * 40))
    verified_review(store, review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256, "FAIL"))
    assert compose_wake(store).state == "REVIEW_FAILED"  # type: ignore[union-attr]
    closed = store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, "retry")
    assert closed.state.value == "CLOSED"
    reopened = store.open_attempt(hypothesis.hypothesis_id, source)
    store.submit_implementation(reopened.attempt_id, implementation(reopened.attempt_id, "b" * 40))
    passed = verified_review(store, review(reopened.attempt_id, "b" * 40, hypothesis.spec_sha256))
    queued = queue_run(passed, "2026-01-01T00:00:00Z")
    running = start_run(queued, "job-x", "2026-01-01T00:00:00Z")
    store.set_state(running, event="run_started")
    assert compose_wake(store) is None
