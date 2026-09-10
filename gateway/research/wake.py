"""One-shot owner-session wake delivery and task observation."""

# Human-readable gateway commands are intentionally not line-wrapped.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from gateway.openclaw_client import OpenClawClient, OpenClawTransportError

from .contracts import AttemptState, HypothesisState
from .review_evidence import REVIEW_EFFORT
from .store import ResearchStore


class WakeRejected(RuntimeError):
    """The authenticated gateway did not accept the owner wake."""


class WakeUncertain(RuntimeError):
    """The gateway may have accepted a wake before transport failed."""


class WakeSender(Protocol):
    def send(self, message: str, session_key: str, idempotency_key: str) -> str: ...


@dataclass(frozen=True, slots=True)
class WakePlan:
    hypothesis_id: str
    attempt_id: str | None
    state: str
    message: str
    pending_key: str
    resume_seq: int


class OpenClawWakeSender:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.host, self.port, self.token = host, port, token

    def send(self, message: str, session_key: str, idempotency_key: str) -> str:
        async def request() -> str:
            payload = await OpenClawClient(self.host, self.port, self.token).request_once(
                "agent",
                {"message": message, "sessionKey": session_key, "idempotencyKey": idempotency_key},
                timeout_seconds=30,
            )
            if payload.get("status") not in {"accepted", "in_flight"}:
                raise WakeRejected(f"wake rejected: status={payload.get('status')!r}")
            response_session = payload.get("sessionKey")
            if response_session is not None and response_session != session_key:
                raise WakeRejected("wake response targeted a different owner session")
            run_id = payload.get("runId")
            if not isinstance(run_id, str) or not run_id:
                raise WakeRejected("wake response has no runId")
            return run_id

        try:
            return asyncio.run(request())
        except OpenClawTransportError as exc:
            raise WakeUncertain(str(exc)) from exc

    def owner_task_status(self, run_id: str) -> Mapping[str, object]:
        async def request() -> Mapping[str, object]:
            payload = await OpenClawClient(self.host, self.port, self.token).request_once(
                "agent.wait", {"runId": run_id, "timeoutMs": 0}, timeout_seconds=30
            )
            response_run_id = payload.get("runId")
            if response_run_id != run_id:
                raise WakeRejected("agent.wait response runId does not match requested runId")
            return payload

        return asyncio.run(request())


def _key(hypothesis_id: str, attempt_id: str | None, state: str, resume_seq: int) -> str:
    return hashlib.sha256(
        f"{hypothesis_id}|{attempt_id or ''}|{state}|{resume_seq}".encode()
    ).hexdigest()


def compose_wake(store: ResearchStore) -> WakePlan | None:
    status, resume_seq = store.campaign()
    if status == "PAUSED":
        return None
    hypotheses = store.hypotheses()
    frozen = next((item for item in hypotheses if item.state == HypothesisState.FROZEN), None)
    if frozen is None:
        if not hypotheses:
            return WakePlan(
                "H0001",
                None,
                "NO_HYPOTHESIS",
                "Astra: create and freeze one hypothesis with `gateway-cli research hypothesis-create --root ROOT ...`.",
                _key("H0001", None, "NO_HYPOTHESIS", resume_seq),
                resume_seq,
            )
        if all(item.state == HypothesisState.DECIDED for item in hypotheses):
            next_number = max(int(item.hypothesis_id[1:]) for item in hypotheses) + 1
            next_id = f"H{next_number:04d}"
            return WakePlan(
                next_id,
                None,
                "ALL_DECIDED",
                f"Astra: author and freeze the next hypothesis {next_id}; all existing hypotheses are DECIDED.",
                _key(next_id, None, "ALL_DECIDED", resume_seq),
                resume_seq,
            )
        draft = next(
            (item for item in hypotheses if item.state != HypothesisState.DECIDED), hypotheses[-1]
        )
        return WakePlan(
            draft.hypothesis_id,
            None,
            draft.state.value,
            f"Astra: freeze hypothesis {draft.hypothesis_id} with `gateway-cli research hypothesis-freeze {draft.hypothesis_id} --root ROOT`.",
            _key(draft.hypothesis_id, None, draft.state.value, resume_seq),
            resume_seq,
        )
    attempts = store.attempts_for(frozen.hypothesis_id)
    attempt = next((item for item in attempts if item.state != AttemptState.CLOSED), None)
    if attempt is None:
        return WakePlan(
            frozen.hypothesis_id,
            None,
            frozen.state.value,
            f"Astra: open an attempt for {frozen.hypothesis_id} with `gateway-cli research attempt-open {frozen.hypothesis_id} --root ROOT --worktree PATH`.",
            _key(frozen.hypothesis_id, None, frozen.state.value, resume_seq),
            resume_seq,
        )
    state = attempt.state.value
    if attempt.state in {AttemptState.RUN_QUEUED, AttemptState.RUNNING}:
        return None
    if attempt.state == AttemptState.OPENED:
        action = f"dispatch coder; submit with `gateway-cli research implementation-submit {attempt.attempt_id} --root ROOT --file impl.json`"
    elif attempt.state == AttemptState.IMPLEMENTED:
        action = (
            f"reserve a committed review bundle with `gateway-cli research review-reserve {attempt.attempt_id} --root ROOT --bundle-dir BUNDLE --owner-key OWNER`, "
            "spawn the exact ACP reviewer (runtime=acp, agentId=claude, mode=run, thread=false, "
            f"cwd=BUNDLE, model=claude-opus-5, effort={REVIEW_EFFORT}, label=LABEL), record its child/run ACK with "
            f"`gateway-cli research review-ack {attempt.attempt_id} --root ROOT --child-session-key CHILD --run-id RUN --mode run`, "
            "then collect host evidence with "
            f"`gateway-cli research review-collect {attempt.attempt_id} --root ROOT --core-database DB --claude-sessions SESSIONS --claude-projects PROJECTS`"
        )
    elif attempt.state == AttemptState.REVIEW_PASSED:
        action = f"run with `gateway-cli research run {attempt.attempt_id} --root ROOT`"
    else:
        action = f"ask Astra for close decision using `gateway-cli research attempt-close {attempt.attempt_id} --root ROOT --decision RETRY|FINISH|PAUSE --reason TEXT`"
    message = f"Astra owner action: hypothesis_id={frozen.hypothesis_id}; attempt_id={attempt.attempt_id}; state={state}; paths={attempt.worktree_path}; {action}."
    return WakePlan(
        frozen.hypothesis_id,
        attempt.attempt_id,
        state,
        message,
        _key(frozen.hypothesis_id, attempt.attempt_id, state, resume_seq),
        resume_seq,
    )


def deliver(
    store: ResearchStore, sender: WakeSender, plan: WakePlan, session_key: str
) -> str | None:
    if not store.reserve_wake(plan.pending_key, plan.attempt_id, plan.state, plan.resume_seq):
        row = store.wake_row(plan.pending_key)
        if row is None or row["run_id"] != "PENDING":
            return None
    try:
        run_id = sender.send(plan.message, session_key, plan.pending_key)
    except WakeUncertain:
        # Keep the reservation and idempotency key: a timed-out request may
        # already have been accepted by the gateway.
        raise
    except Exception:
        store.release_wake(plan.pending_key)
        raise
    if not isinstance(run_id, str) or not run_id:
        store.release_wake(plan.pending_key)
        raise WakeRejected("wake sender returned no runId")
    store.complete_wake(plan.pending_key, run_id)
    return run_id


def poll_owner_turn(store: ResearchStore, sender: WakeSender, *, required: bool = False) -> None:
    """Observe the latest owner turn through the installed ``agent.wait`` RPC."""
    del required
    poll = getattr(sender, "owner_task_status", None)
    if not callable(poll):
        rows = [row for row in store.wake_rows() if row["run_id"] != "PENDING"]
        if rows:
            store.update_wake_status(str(rows[-1]["pending_key"]), "pending_runtime_gate")
        return
    terminal_statuses = {
        "ok",
        "error",
    }
    rows = [
        row
        for row in store.wake_rows()
        if row["run_id"] != "PENDING" and row["turn_status"] not in terminal_statuses
    ]
    if not rows:
        return
    row = rows[-1]
    result = poll(str(row["run_id"]))
    status = (
        result.get("status", result.get("state", "unknown"))
        if isinstance(result, Mapping)
        else "unknown"
    )
    store.update_wake_status(str(row["pending_key"]), str(status))
