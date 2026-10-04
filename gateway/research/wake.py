"""One-shot owner-session wake delivery and task observation."""

# Human-readable gateway commands are intentionally not line-wrapped.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from gateway.openclaw_client import OpenClawClient, OpenClawTransportError

from .contracts import AttemptDecision, AttemptState, HypothesisState, compute_requirements
from .host_records import HostRecordError, managed_native_databases
from .store import ResearchStore, StoreConflict, canonical_wake_key


class WakeRejected(RuntimeError):
    """The authenticated gateway did not accept the owner wake."""


class WakeUncertain(RuntimeError):
    """The gateway may have accepted a wake before transport failed."""


class OwnerPollUnavailable(RuntimeError):
    """The owner-turn observation failed due to an unavailable transport."""


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
    return canonical_wake_key(hypothesis_id, attempt_id, state, resume_seq)


def _managed_native_review_paths() -> tuple[str, str] | None:
    """Return concrete native stores or refuse to render a review command."""

    configured = os.environ.get("RESEARCH_CORE_DATABASE")
    if not configured:
        return None
    try:
        stores = managed_native_databases(configured)
    except HostRecordError:
        return None
    return str(stores.openclaw_database), str(stores.codex_state_database)


_FREEZE_SEQUENCE = (
    "run `gateway-cli research compute-probe {hid} --root ROOT` (it measures real validate-inputs "
    "and evaluate cost and can take tens of minutes to hours; it is recorded once), copy its "
    "`min_rss_mb` into the spec with `hypothesis-set-compute {hid} --root ROOT --max-rss-mb N "
    "--max-wall-seconds S`, then `hypothesis-freeze {hid} --root ROOT`. A DRAFT that cannot be "
    "frozen (infeasible probe) is abandoned with `hypothesis-decide {hid} --root ROOT --decision "
    "ABANDONED --reason ...`."
)


def _draft_wake_text(store: ResearchStore, hypothesis_id: str) -> str:
    try:
        probe = store.compute_probe(hypothesis_id)
    except (StoreConflict, ValueError):
        probe = None
    if probe is None:
        return f"Astra: DRAFT hypothesis {hypothesis_id} has no compute probe; {_FREEZE_SEQUENCE.format(hid=hypothesis_id)}"
    required = compute_requirements(probe)
    return (
        f"Astra: DRAFT hypothesis {hypothesis_id} has a recorded compute probe "
        f"(min_rss_mb={required.min_rss_mb}, min_scenario_timeout_seconds="
        f"{required.min_scenario_timeout_seconds}); set `compute.max_rss_mb` >= min_rss_mb with "
        f"`gateway-cli research hypothesis-set-compute {hypothesis_id} --root ROOT --max-rss-mb N "
        f"--max-wall-seconds S`, then `hypothesis-freeze {hypothesis_id} --root ROOT`. If the "
        f"requirements are infeasible, abandon with `hypothesis-decide {hypothesis_id} --root ROOT "
        f"--decision ABANDONED --reason ...`."
    )


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
                f"Astra: create one hypothesis with `gateway-cli research hypothesis-create --root ROOT ...`, then {_FREEZE_SEQUENCE.format(hid='H0001')}",
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
                f"Astra: author the next hypothesis {next_id} (all existing hypotheses are DECIDED), then {_FREEZE_SEQUENCE.format(hid=next_id)}",
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
            _draft_wake_text(store, draft.hypothesis_id),
            _key(draft.hypothesis_id, None, draft.state.value, resume_seq),
            resume_seq,
        )
    attempts = store.attempts_for(frozen.hypothesis_id)
    attempt = next((item for item in attempts if item.state != AttemptState.CLOSED), None)
    if attempt is None:
        if attempts and attempts[-1].decision == AttemptDecision.FINISH.value:
            last_attempt = attempts[-1]
            return WakePlan(
                frozen.hypothesis_id,
                None,
                frozen.state.value,
                f"Astra: decide whether to finish or abandon {frozen.hypothesis_id} with `gateway-cli research hypothesis-decide {frozen.hypothesis_id} --root ROOT --decision FINISHED|ABANDONED --reason TEXT`; do not reopen another attempt.",
                _key(
                    frozen.hypothesis_id,
                    last_attempt.attempt_id,
                    "HYPOTHESIS_DECISION",
                    resume_seq,
                ),
                resume_seq,
            )
        refusal = store.latest_admission_refusal(frozen.hypothesis_id)
        refusal_suffix = ""
        if refusal is not None:
            reason, detail = refusal
            refusal_suffix = f" admission_refusal={reason}: {detail or 'typed admission refused'}."
        pending_key = _key(
            frozen.hypothesis_id,
            attempts[-1].attempt_id if attempts else None,
            "ATTEMPT_OPEN" if attempts else frozen.state.value,
            resume_seq,
        )
        return WakePlan(
            frozen.hypothesis_id,
            None,
            frozen.state.value,
            f"Astra: open an attempt for {frozen.hypothesis_id} with `gateway-cli research attempt-open {frozen.hypothesis_id} --root ROOT --worktree PATH`.{refusal_suffix}",
            pending_key,
            resume_seq,
        )
    state = attempt.state.value
    if attempt.state in {AttemptState.RUN_QUEUED, AttemptState.RUNNING}:
        return None
    if attempt.state == AttemptState.OPENED:
        action = (
            f"dispatch coder; submit with `gateway-cli research implementation-submit "
            f"{attempt.attempt_id} --root ROOT --file impl.json --run-plan run-plan.json "
            "--provenance-evidence provenance.json`; on the completion callback, "
            "verify the submitted implementation and stop this owner turn. Do not "
            "reserve or spawn review from this OPENED callback; wait for the next "
            "canonical IMPLEMENTED wake."
        )
    elif attempt.state == AttemptState.IMPLEMENTED:
        wake_key = _key(frozen.hypothesis_id, attempt.attempt_id, state, resume_seq)
        native_paths = _managed_native_review_paths()
        if native_paths is None:
            action = (
                "native review reviewer model=gpt-5.6-sol effort=xhigh is unavailable: "
                "RESEARCH_CORE_DATABASE must resolve to the managed "
                "<root>/state/openclaw.sqlite with validated OpenClaw and Codex stores; pause "
                "instead of rendering a guessed database path; do not invoke spawn_agent"
            )
        else:
            openclaw_database, codex_database = native_paths
            action = (
                f"reserve a committed review bundle with `gateway-cli research review-reserve {attempt.attempt_id} --root ROOT --bundle-dir BUNDLE --wake-key {wake_key} --owner-key OWNER --openclaw-database {openclaw_database}`, "
                "then invoke the returned exact `spawn_arguments` through the native OpenClaw "
                "collaboration.spawn_agent reviewer call (model=gpt-5.6-sol, effort=xhigh is "
                "observed from the child runtime, not self-reported). Reconcile the official child "
                "only from the completion callback after this owner turn has ended; yield/stop "
                "immediately after spawn and never respawn from the same turn. "
                f"ACK with `gateway-cli research review-reconcile {attempt.attempt_id} --root ROOT --openclaw-database {openclaw_database} --codex-state-database {codex_database}`, "
                "then collect host evidence with "
                f"`gateway-cli research review-collect {attempt.attempt_id} --root ROOT --openclaw-database {openclaw_database} --codex-state-database {codex_database}`"
            )
    elif attempt.state == AttemptState.REVIEW_PASSED:
        action = f"run with `gateway-cli research run {attempt.attempt_id} --root ROOT`"
    else:
        action = f"ask Astra for close decision using `gateway-cli research attempt-close {attempt.attempt_id} --root ROOT --decision RETRY|FINISH|PAUSE --reason TEXT`"
    refusal = store.latest_admission_refusal(frozen.hypothesis_id)
    refusal_suffix = ""
    if refusal is not None:
        reason, detail = refusal
        refusal_suffix = f" admission_refusal={reason}: {detail or 'typed admission refused'}."
    message = f"Astra owner action: hypothesis_id={frozen.hypothesis_id}; attempt_id={attempt.attempt_id}; state={state}; paths={attempt.worktree_path}; {action}.{refusal_suffix}"
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
    try:
        result = poll(str(row["run_id"]))
    except (OpenClawTransportError, WakeUncertain) as exc:
        raise OwnerPollUnavailable(f"owner poll unavailable: {exc}") from exc
    status = (
        result.get("status", result.get("state", "unknown"))
        if isinstance(result, Mapping)
        else "unknown"
    )
    store.update_wake_status(str(row["pending_key"]), str(status))
