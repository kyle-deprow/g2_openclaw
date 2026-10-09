"""One-shot owner-session wake delivery and task observation."""

# Human-readable gateway commands are intentionally not line-wrapped.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from gateway.openclaw_client import OpenClawClient, OpenClawError, OpenClawTransportError

from .contracts import AttemptDecision, AttemptState, HypothesisState, compute_requirements
from .host_records import HostRecordError, managed_native_databases, read_exact_native_owner
from .lost_runs import GATEWAY_RESTART_STATUS, UNKNOWN_RUN_STATUS, lost_wake_key
from .store import ResearchStore, StoreConflict, canonical_wake_key


class WakeRejected(RuntimeError):
    """The authenticated gateway did not accept the owner wake."""


class WakeUncertain(RuntimeError):
    """The gateway may have accepted a wake before transport failed."""


class OwnerPollUnavailable(RuntimeError):
    """The owner-turn observation failed due to an unavailable transport."""


_GATEWAY_UNIT = "openclaw-gateway.service"
_run = subprocess.run


@dataclass(frozen=True, slots=True)
class GatewayInstance:
    """One gateway process: systemd's per-start InvocationID and its wall-clock start."""

    invocation_id: str | None
    started_at: datetime | None


def _parse_systemd_utc(value: str) -> datetime | None:
    """Parse ``Fri 2026-10-09 21:56:26.935517 UTC`` (``--timestamp=us+utc``)."""
    parts = value.split()
    if len(parts) != 4 or parts[3] != "UTC":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(f"{parts[1]} {parts[2]}", fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


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

    def gateway_instance(self) -> GatewayInstance | None:
        """Identify the running gateway process, or None when systemd cannot say."""
        try:
            result = _run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    _GATEWAY_UNIT,
                    "--timestamp=us+utc",
                    "-p",
                    "ActiveState",
                    "-p",
                    "ActiveEnterTimestamp",
                    "-p",
                    "InvocationID",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        fields = dict(line.partition("=")[::2] for line in result.stdout.splitlines())
        if fields.get("ActiveState") != "active":
            return None
        invocation = fields.get("InvocationID") or None
        started = _parse_systemd_utc(fields.get("ActiveEnterTimestamp", ""))
        if invocation is None and started is None:
            return None
        return GatewayInstance(invocation, started)


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


_REFUSAL_REASON_LIMIT = 300
_CHARTER_STOP_SENTENCE = (
    "If the campaign charter's stop condition is met (for example, consecutive underpowered "
    "refusals), pause with `gateway-cli research pause --root ROOT --owner --reason TEXT`; "
    "otherwise redesign - do not resubmit the same design unchanged."
)


def _create_refusal_suffix(count: int, reason: str | None) -> str:
    """Wake text appended only after refused creates; empty (byte-identical) otherwise."""
    if count == 0:
        return ""
    shown = " ".join((reason or "no reason recorded").split())
    if len(shown) > _REFUSAL_REASON_LIMIT:
        shown = shown[: _REFUSAL_REASON_LIMIT - 3] + "..."
    return (
        f" {count} hypothesis-create refusal(s) of any reason since the last create, decide or "
        f"resume; latest: {shown}. {_CHARTER_STOP_SENTENCE}"
    )


def _create_refusal_key(hypothesis_id: str, state: str, resume_seq: int, count: int) -> str:
    # Zero refusals keeps the historical key; each new refusal yields exactly one new key.
    return _key(hypothesis_id, f"create_refusals:{count}" if count else None, state, resume_seq)


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


_LOST_RUN_SUFFIX = (
    " The previous owner turn for this state was interrupted by a gateway restart and its run was "
    "lost; resume from your durable notes and the recorded campaign state instead of assuming "
    "that turn finished its work."
)


def compose_wake(store: ResearchStore) -> WakePlan | None:
    """Compose the next owner wake, salting its key once per lost owner run since the resume."""
    plan = _compose_base_wake(store)
    if plan is None:
        return None
    lost, latest = store.lost_owner_runs()
    if lost == 0:
        return plan
    key = lost_wake_key(plan.pending_key, lost)
    message = plan.message.replace(plan.pending_key, key)
    if latest == (plan.attempt_id, plan.state):
        message += _LOST_RUN_SUFFIX
    return replace(plan, message=message, pending_key=key)


def _compose_base_wake(store: ResearchStore) -> WakePlan | None:
    status, resume_seq = store.campaign()
    if status == "PAUSED":
        return None
    hypotheses = store.hypotheses()
    refusal_count, refusal_reason = (0, None)
    if not hypotheses or all(item.state == HypothesisState.DECIDED for item in hypotheses):
        refusal_count, refusal_reason = store.create_refusals()
    refusal_suffix = _create_refusal_suffix(refusal_count, refusal_reason)
    frozen = next((item for item in hypotheses if item.state == HypothesisState.FROZEN), None)
    if frozen is None:
        if not hypotheses:
            return WakePlan(
                "H0001",
                None,
                "NO_HYPOTHESIS",
                f"Astra: create one hypothesis with `gateway-cli research hypothesis-create --root ROOT ...`, then {_FREEZE_SEQUENCE.format(hid='H0001')}{refusal_suffix}",
                _create_refusal_key("H0001", "NO_HYPOTHESIS", resume_seq, refusal_count),
                resume_seq,
            )
        if all(item.state == HypothesisState.DECIDED for item in hypotheses):
            next_number = max(int(item.hypothesis_id[1:]) for item in hypotheses) + 1
            next_id = f"H{next_number:04d}"
            return WakePlan(
                next_id,
                None,
                "ALL_DECIDED",
                f"Astra: author the next hypothesis {next_id} (all existing hypotheses are DECIDED), then {_FREEZE_SEQUENCE.format(hid=next_id)}{refusal_suffix}",
                _create_refusal_key(next_id, "ALL_DECIDED", resume_seq, refusal_count),
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
    # Read before sending: a restart in between then reads as a (bounded) lost run, never a
    # silently missed one.
    instance_of = getattr(sender, "gateway_instance", None)
    instance = instance_of() if callable(instance_of) else None
    invocation_id = instance.invocation_id if isinstance(instance, GatewayInstance) else None
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
    store.complete_wake(plan.pending_key, run_id, invocation_id)
    return run_id


_TERMINAL_STATUSES = frozenset({"ok", "error", "lost", "superseded"})
_UNKNOWN_RUN_STATUSES = frozenset(
    {"unknown", "unknown_run", "not_found", "notfound", "missing", "no_such_run"}
)
# OpenClaw rejects agent.wait for a run it does not know with "agent run was not found".
_UNKNOWN_RUN_ERROR = re.compile(
    r"agent run was not found|unknown run|run not found|no such run", re.IGNORECASE
)


def poll_owner_turn(
    store: ResearchStore,
    sender: WakeSender,
    *,
    required: bool = False,
    session_key: str | None = None,
) -> None:
    """Observe the newest owner turn through the installed ``agent.wait`` RPC.

    Only the newest delivered wake row of the current resume can be observed or declared lost;
    older unfinished rows are closed as ``superseded`` (no failure event, no lost count).
    """
    del required
    poll = getattr(sender, "owner_task_status", None)
    delivered = [row for row in store.wake_rows() if row["run_id"] != "PENDING"]
    if not callable(poll):
        if delivered:
            store.update_wake_status(str(delivered[-1]["pending_key"]), "pending_runtime_gate")
        return
    if not delivered:
        return
    row = delivered[-1]
    for older in delivered[:-1]:
        if older["turn_status"] not in _TERMINAL_STATUSES:
            store.supersede_wake(str(older["pending_key"]))
    if row["turn_status"] in _TERMINAL_STATUSES:
        return
    pending_key = str(row["pending_key"])
    if int(row["resume_seq"]) != store.campaign()[1]:
        # Delivered before the last operator resume: that turn can no longer be re-woken.
        store.supersede_wake(pending_key)
        return
    try:
        result = poll(str(row["run_id"]))
    except (OpenClawTransportError, WakeUncertain) as exc:
        raise OwnerPollUnavailable(f"owner poll unavailable: {exc}") from exc
    except OpenClawError as exc:
        if _UNKNOWN_RUN_ERROR.search(str(exc)) is None:
            raise
        _declare_lost(
            store, row, session_key, UNKNOWN_RUN_STATUS, f"gateway rejected agent.wait: {exc}"
        )
        return
    status = (
        result.get("status", result.get("state", "unknown"))
        if isinstance(result, Mapping)
        else "unknown"
    )
    explicit = result.get("status", result.get("state")) if isinstance(result, Mapping) else None
    if isinstance(explicit, str) and explicit.lower() in _UNKNOWN_RUN_STATUSES:
        _declare_lost(
            store,
            row,
            session_key,
            UNKNOWN_RUN_STATUS,
            f"gateway no longer knows the run (status={status})",
        )
        return
    if str(status) not in _TERMINAL_STATUSES:
        # The incident case: after a restart the gateway keeps answering "timeout" for the
        # run it no longer knows, so only the process identity can reveal the loss.
        restarted = _restart_after_send(sender, row)
        if restarted is not None:
            _declare_lost(store, row, session_key, GATEWAY_RESTART_STATUS, restarted)
            return
    store.update_wake_status(pending_key, str(status))


def _declare_lost(
    store: ResearchStore, row: sqlite3.Row, session_key: str | None, failure: str, reason: str
) -> None:
    """Record a lost run unless the durable host record proves how the run actually ended.

    "Ended" is not "succeeded": a gateway restart aborts in-flight runs and writes their end
    event with status ``interrupted``, which stays a lost run.
    """
    pending_key = str(row["pending_key"])
    outcome = (
        _host_record_outcome(session_key, str(row["run_id"]), str(row["sent_at"]))
        if session_key is not None
        else None
    )
    if outcome is not None:
        store.update_wake_status(pending_key, outcome)
        return
    store.mark_wake_lost(pending_key, failure, reason)


def _host_record_outcome(session_key: str, run_id: str, sent_at: str) -> str | None:
    """``ok`` or ``error`` when the native end event proves a finished run, else None (lost)."""
    configured = os.environ.get("RESEARCH_CORE_DATABASE")
    if not configured:
        return None
    try:
        sent_ms = int(datetime.fromisoformat(sent_at.replace("Z", "+00:00")).timestamp() * 1000)
        owner = read_exact_native_owner(
            managed_native_databases(configured).openclaw_database,
            session_key,
            sent_ms,
            expected_run_id=run_id,
        )
    except (HostRecordError, OSError, ValueError, sqlite3.Error):
        return None
    if owner.ended_at_ms is None or owner.ended_aborted is not False:
        return None
    if owner.ended_status == "success":
        return "ok"
    if owner.ended_status == "error":
        return "error"
    return None  # "interrupted", unknown or missing status


def _restart_after_send(sender: WakeSender, row: sqlite3.Row) -> str | None:
    """Return a reason when the gateway process is not the one that accepted the wake.

    Limitation: an in-process restart (SIGUSR1) keeps systemd's InvocationID and start time,
    so it is invisible here; only a unit restart or the run-unknown responses reveal a loss.
    """
    instance_of = getattr(sender, "gateway_instance", None)
    instance = instance_of() if callable(instance_of) else None
    if not isinstance(instance, GatewayInstance):
        return None
    recorded = row["gateway_invocation_id"]
    if recorded and instance.invocation_id:
        if recorded == instance.invocation_id:
            return None
        return (
            f"gateway invocation changed from {recorded} to {instance.invocation_id} after the "
            "wake was sent; the in-flight run was lost in the restart"
        )
    sent_at = str(row["sent_at"])
    if instance.started_at is None:
        return None
    try:
        sent = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    if instance.started_at <= sent:
        return None
    return (
        f"gateway process started at {instance.started_at.isoformat()}, after the wake was sent "
        f"at {sent_at}; the in-flight run was lost in the restart"
    )
