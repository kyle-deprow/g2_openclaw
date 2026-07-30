"""Deterministic, owner-session-only supervisor for Quantipy autoresearch."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from gateway.autoresearch_readiness import (
    DEFAULT_PLATFORM_READINESS_PATH,
    load_platform_readiness,
    validate_state_readiness,
)
from gateway.autoresearch_runner import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    DEFAULT_QUANTIPY_ROOT,
    AutoresearchState,
    AutoresearchValidationContext,
    AutoresearchValidationError,
    Phase,
    ResearchMode,
    VerificationResultArtifact,
    VerificationStatus,
    advance_infrastructure_verification_failure,
    build_authoritative_state_reference,
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
    load_autoresearch_policy,
)
from gateway.autoresearch_runs import (
    DEFAULT_AUTORESEARCH_RUNS_ROOT,
    AutoresearchRunRecordError,
    RunFailureClassification,
    RunManifest,
    RunRecord,
    RunState,
    complete_run,
    read_run_record,
)
from gateway.openclaw_client import OpenClawClient, OpenClawError, OpenClawTransportError

logger = logging.getLogger(__name__)

DEFAULT_AUTORESEARCH_DIR = Path.home() / ".openclaw" / "autoresearch"
DEFAULT_STATE_PATH = DEFAULT_AUTORESEARCH_DIR / "quantipy-state.json"
DEFAULT_CHECKPOINT_PATH = DEFAULT_AUTORESEARCH_DIR / "owner-recovery.json"
AUTORESEARCH_OWNER_AGENT_ID = "autoresearch-pm"
AUTORESEARCH_OWNER_SESSION_KEY = "agent:autoresearch-pm:autoresearch:quantipy"
DEFAULT_OWNER_SESSIONS_PATH = (
    Path.home()
    / ".openclaw"
    / "agents"
    / AUTORESEARCH_OWNER_AGENT_ID
    / "sessions"
    / "sessions.json"
)
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_GRACE_PERIOD_SECONDS = 120.0
DEFAULT_CLAIM_STALE_SECONDS = 300.0
# Implementation, verification, review, and fix stages can spend several
# minutes running tests and backtests without producing an OpenClaw event.
# Keep the supervisor responsive while allowing those legitimate long turns to
# finish before declaring the task stale.
DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS = 900.0
DEFAULT_MAX_RECOVERY_ATTEMPTS = 2
DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS = 0.05
READ_ONLY_TASK_LIST_ATTEMPTS = 3
READ_ONLY_TASK_LIST_RETRY_SECONDS = 0.5
REQUIRED_OPENCLAW_VERSION = (2026, 7, 1)
REQUIRED_OPENCLAW_VERSION_TEXT = "2026.7.1-2"
DEFAULT_TASK_RPC_TIMEOUT_SECONDS = 30.0
WAKE_MESSAGE = (
    "Continue Quantipy autoresearch from the authoritative state. First run exactly: "
    "cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json"
)
RECOVERY_MESSAGE = (
    "Continue Quantipy autoresearch from the authoritative state. First run exactly: "
    "cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json. Reconcile only the current "
    "iteration/phase attempt labels from the authoritative state and current task ledger. "
    "Use bounded task metadata; do not enumerate historical sessions or fetch old full "
    "transcripts. Inspect a native Codex transcript only for an exact current label and "
    "only when its terminal artifact is needed before relaunching; "
    "infrastructure recovery only; no research steering. Do not silently switch "
    "provider, runtime, or model. If provider/model/auth/capacity is blocked, "
    "surface the control-plane blocker exactly and do not edit Quantipy experiment "
    "files."
)
MISSING_VERIFICATION_ARTIFACT_RECOVERY_MESSAGE = (
    "Recover Quantipy autoresearch from a stale verification phase with an "
    "implementation_result but no verification_history. First run exactly: "
    "cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json. This is artifact recovery, "
    "not research steering. Do not fabricate verification_result metrics, commands, "
    "coverage, status, or provenance from prose. Inspect the authoritative state, "
    "implementation_result workspace and current task metadata. Do not enumerate historical "
    "sessions or fetch old full transcripts. Inspect a native Codex transcript only for an "
    "exact current label and only when its terminal artifact is needed. "
    "If existing machine-verifiable terminal outputs prove a valid verification_result, "
    "wrap that exact artifact in the strict production envelope from the active "
    "autoresearch-next output: "
    '{"instruction_manifest_sha256":"<source_manifest_sha256>",'
    '"state_reference_sha256":"<state_reference_sha256>","artifact":{...}}. '
    "Never pass a raw unwrapped verification_result. Otherwise rerun "
    "the verification stage from the authoritative state. Do not edit Quantipy experiment "
    "files outside the normal verification/fix workflow. If provider/model/auth/capacity "
    "is blocked, surface the control-plane blocker exactly and do not silently switch "
    "provider, runtime, or model."
)
PROVIDER_BLOCKED_ALERT_REASON = "control_plane_provider_blocked"
MISSING_VERIFICATION_ARTIFACT_REASON = "missing_verification_artifact"
OWNER_SESSION_STORE_UNAVAILABLE_REASON = "owner_session_store_unavailable"
EARLY_OWNER_LIFECYCLE_SHORT_CIRCUIT_PHASES = frozenset({Phase.VERIFICATION, Phase.DECISION_LOG})


@dataclass(frozen=True, slots=True)
class RecoveryErrorPattern:
    pattern: str
    alert_reason: str | None = None


RECOVERY_ERROR_PATTERNS = (
    RecoveryErrorPattern('no api key found for provider "openai"', PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("no api key found for provider", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("selected model is at capacity", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("model is at capacity", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("selected model is overloaded", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("authentication failed", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("auth rejected", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("cli transcript compaction failed"),
    RecoveryErrorPattern("context overflow"),
    RecoveryErrorPattern("prompt too long"),
    RecoveryErrorPattern("maximum context length"),
    RecoveryErrorPattern("maximum context size"),
    RecoveryErrorPattern("too many tokens"),
)
EXPECTED_STAGE_AGENT_IDS: dict[Phase, tuple[str, ...]] = {
    Phase.DEBATE: (
        "debater_microstructure",
        "debater_data",
        "debater_skeptic",
        "debater_theory",
        "debater_implementation",
    ),
    Phase.CONSENSUS: ("consensus_arbiter",),
    Phase.IMPLEMENTATION: ("implementer",),
    Phase.VERIFICATION: (AUTORESEARCH_OWNER_AGENT_ID,),
    Phase.REVIEW: ("reviewer",),
    Phase.FIX_TEST: ("fixer",),
    Phase.DECISION_LOG: (AUTORESEARCH_OWNER_AGENT_ID,),
    Phase.REPEAT: (),
}
RELEVANT_AGENT_IDS = frozenset(
    {
        AUTORESEARCH_OWNER_AGENT_ID,
        "context_curator",
        *(agent_id for agent_ids in EXPECTED_STAGE_AGENT_IDS.values() for agent_id in agent_ids),
    }
)
TARGET_WRITER_COMMAND_RE = re.compile(
    r"(\bpytest\b|\bpy\.test\b|\bjupyter\b|\bpapermill\b|\bipython\b|"
    r"\bnbconvert\b|\bgenerate_[\w.-]*|notebooks/experiments|"
    r"src/quantipy/alpha|scripts/experiments|tools/experiments)"
)


class SupervisorError(RuntimeError):
    """Base failure for strict autoresearch supervision."""


class OpenClawUnavailableError(SupervisorError):
    """A transient OpenClaw control-plane operation exhausted bounded retries."""


class WorkspaceEvidenceError(SupervisorError):
    """Raised when an active implementation workspace cannot be verified."""


class ShutdownInterrupted(Exception):
    """A command failure observed after shutdown was already requested."""


ShutdownRequested = Callable[[], bool]


def _shutdown_not_requested() -> bool:
    return False


class TaskGateway(Protocol):
    """Synchronous boundary for native, authenticated gateway control RPCs."""

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class NativeGatewayRPC:
    """One-shot native WebSocket gateway RPC client; it never launches the CLI."""

    host: str
    port: int
    token: str

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        if shutdown_requested():
            raise ShutdownInterrupted("OpenClaw gateway RPC interrupted during shutdown")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SupervisorError("Native gateway RPC requests require a synchronous caller")
        try:
            return asyncio.run(self._request(method, params, shutdown_requested))
        except OpenClawTransportError as exc:
            detail = f"OpenClaw gateway RPC failed ({method}): {exc}"
            if shutdown_requested():
                raise ShutdownInterrupted(detail) from exc
            raise OpenClawUnavailableError(detail) from exc
        except OpenClawError as exc:
            detail = f"OpenClaw gateway RPC failed ({method}): {exc}"
            if shutdown_requested():
                raise ShutdownInterrupted(detail) from exc
            raise SupervisorError(detail) from exc

    async def _request(
        self,
        method: str,
        params: Mapping[str, object],
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        client = OpenClawClient(self.host, self.port, self.token)
        request = asyncio.create_task(
            client.request_once(
                method,
                params,
                timeout_seconds=DEFAULT_TASK_RPC_TIMEOUT_SECONDS,
                required_server_version=REQUIRED_OPENCLAW_VERSION_TEXT,
            )
        )
        while not request.done():
            if shutdown_requested():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request
                raise ShutdownInterrupted("OpenClaw gateway RPC interrupted during shutdown")
            await asyncio.sleep(DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS)
        return await request


def _default_task_gateway() -> NativeGatewayRPC:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if not token:
        raise SupervisorError("OPENCLAW_GATEWAY_TOKEN is required for native task observation")
    raw_port = os.environ.get("OPENCLAW_PORT", "18789")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SupervisorError(
            "OPENCLAW_PORT must be an integer for native task observation"
        ) from exc
    if not 1 <= port <= 65535:
        raise SupervisorError("OPENCLAW_PORT must be between 1 and 65535")
    host = os.environ.get("OPENCLAW_HOST", "127.0.0.1").strip()
    if not host:
        raise SupervisorError("OPENCLAW_HOST must be non-empty for native task observation")
    return NativeGatewayRPC(host, port, token)


def _require_finite_positive(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SupervisorError(f"{field_name} must be a finite positive number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise SupervisorError(f"{field_name} must be a finite positive number")
    return parsed


def _finite_positive_cli_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return value


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorError(f"duplicate OpenClaw session key: {key}")
        result[key] = value
    return result


def make_idempotency_key(*, purpose: str, material: str) -> str:
    """Produce a stable, bounded key for one logical owner-session request."""
    digest = hashlib.sha256(
        f"{purpose}\n{AUTORESEARCH_OWNER_SESSION_KEY}\n{material}".encode()
    ).hexdigest()
    return f"autoresearch-{purpose}-{digest}"


class OpenClawRPC:
    """Native authenticated gateway RPC boundary shared by owner control surfaces."""

    def __init__(
        self,
        gateway: TaskGateway | None = None,
    ) -> None:
        self._gateway = gateway or _default_task_gateway()

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        return self._gateway.request(method, params, shutdown_requested=shutdown_requested)

    def list_running_tasks(
        self,
        *,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        """Read OpenClaw running tasks with strict bounded retry for RPC failures."""
        last_error: OpenClawUnavailableError | None = None
        for attempt in range(1, READ_ONLY_TASK_LIST_ATTEMPTS + 1):
            try:
                return self._request(
                    "tasks.list",
                    {"status": "running", "limit": 500},
                    shutdown_requested=shutdown_requested,
                )
            except ShutdownInterrupted:
                if last_error is not None:
                    raise OpenClawUnavailableError(
                        "OpenClaw running task list failed before shutdown during retry: "
                        f"{last_error}"
                    ) from last_error
                raise
            except OpenClawUnavailableError as exc:
                if shutdown_requested():
                    raise ShutdownInterrupted(str(exc)) from exc
                last_error = exc
                if attempt >= READ_ONLY_TASK_LIST_ATTEMPTS:
                    break
                logger.warning(
                    "OpenClaw read-only task list failed; retrying (%s/%s): %s",
                    attempt,
                    READ_ONLY_TASK_LIST_ATTEMPTS,
                    exc,
                )
                time.sleep(READ_ONLY_TASK_LIST_RETRY_SECONDS)
                if shutdown_requested():
                    raise OpenClawUnavailableError(
                        "OpenClaw running task list failed before shutdown during retry: "
                        f"{last_error}"
                    ) from last_error
        raise OpenClawUnavailableError(
            "OpenClaw running task list failed after "
            f"{READ_ONLY_TASK_LIST_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def wake(
        self,
        *,
        message: str,
        idempotency_key: str,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> str:
        params = json.dumps(
            {
                "message": message,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "idempotencyKey": idempotency_key,
            },
            separators=(",", ":"),
        )
        payload = self._request(
            "agent",
            json.loads(params, object_pairs_hook=_strict_json_object),
            shutdown_requested=shutdown_requested,
        )
        status = payload.get("status")
        session_key = payload.get("sessionKey")
        run_id = payload.get("runId")
        if status != "accepted" or session_key != AUTORESEARCH_OWNER_SESSION_KEY:
            raise SupervisorError(
                "OpenClaw wake response did not accept the dedicated owner session"
            )
        if not isinstance(run_id, str) or not run_id.strip():
            raise SupervisorError("OpenClaw wake response is missing a non-empty runId")
        return run_id

    def delete_owner_session(self) -> bool:
        params = json.dumps(
            {
                "key": AUTORESEARCH_OWNER_SESSION_KEY,
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "deleteTranscript": False,
            },
            separators=(",", ":"),
        )
        payload = self._request(
            "sessions.delete", json.loads(params, object_pairs_hook=_strict_json_object)
        )
        if payload.get("ok") is not True or payload.get("key") != AUTORESEARCH_OWNER_SESSION_KEY:
            raise SupervisorError(
                "OpenClaw owner-session deletion response is malformed or mismatched"
            )
        if payload.get("deleted") is True:
            return True
        if payload.get("deleted") is False and payload.get("absent") is True:
            return False
        raise SupervisorError(
            "OpenClaw owner-session deletion response did not confirm deletion or absence"
        )

    def abort_owner_run(self, *, run_id: str) -> None:
        params = json.dumps(
            {
                "key": AUTORESEARCH_OWNER_SESSION_KEY,
                "runId": run_id,
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
            },
            separators=(",", ":"),
        )
        payload = self._request(
            "sessions.abort", json.loads(params, object_pairs_hook=_strict_json_object)
        )
        status = payload.get("status")
        aborted_run_id = payload.get("abortedRunId")
        if payload.get("ok") is not True or (
            (status == "aborted" and aborted_run_id != run_id)
            or (status == "no-active-run" and aborted_run_id is not None)
            or status not in {"aborted", "no-active-run"}
        ):
            raise SupervisorError(
                f"OpenClaw owner-run abort response is malformed or mismatched: {run_id}"
            )

    def cancel_task(self, *, task_id: str) -> None:
        params = json.dumps(
            {"taskId": task_id, "reason": "Quantipy autoresearch stopped by operator."},
            separators=(",", ":"),
        )
        payload = self._request(
            "tasks.cancel", json.loads(params, object_pairs_hook=_strict_json_object)
        )
        if payload.get("found") is not True or payload.get("cancelled") is not True:
            raise SupervisorError(
                f"OpenClaw task cancellation response is malformed or mismatched: {task_id}"
            )
        returned_task = payload.get("task")
        if returned_task is None:
            return
        if not isinstance(returned_task, Mapping):
            raise SupervisorError(
                f"OpenClaw task cancellation response has an invalid task: {task_id}"
            )
        returned_task_id = returned_task.get("taskId")
        returned_id = returned_task.get("id")
        if returned_task_id != task_id or (
            returned_id is not None and returned_id != returned_task_id
        ):
            raise SupervisorError(
                f"OpenClaw task cancellation response is malformed or mismatched: {task_id}"
            )

    def get_task(
        self,
        *,
        task_id: str,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        payload = self._request(
            "tasks.get",
            {"taskId": task_id},
            shutdown_requested=shutdown_requested,
        )
        task = payload.get("task")
        if not isinstance(task, Mapping):
            raise SupervisorError(f"OpenClaw tasks.get response has no object task: {task_id}")
        return task


class SupervisorOutcome(StrEnum):
    NO_ACTION = "no_action"
    NUDGED = "nudged"
    ALERT = "alert"


class RecoveryStatus(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"


class TaskProvenance(StrEnum):
    """Whether a public task summary can be attributed to the PM session."""

    UNRELATED = "unrelated"
    OWNER_TURN = "owner_turn"
    STAGE_CHILD = "stage_child"
    CODEX_NATIVE_SUBAGENT = "codex_native_subagent"
    AMBIGUOUS = "ambiguous"


class CanonicalTaskStatus(StrEnum):
    """OpenClaw 2026.7.1-2 gateway task-ledger statuses."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TaskReconciliationError(SupervisorError):
    """A task-list projection cannot be proven to match a canonical task record."""


@dataclass(frozen=True, slots=True)
class ReconciledRunningTasks:
    """Canonical running tasks and evidence that a projected task has ended."""

    running_tasks: tuple[Mapping[str, object], ...]
    terminal_task_seen: bool
    observed_error: RecoveryErrorPattern | None = None


def _detect_recovery_error_in_text(text: str) -> RecoveryErrorPattern | None:
    lowered = text.lower()
    for pattern in RECOVERY_ERROR_PATTERNS:
        if pattern.pattern in lowered:
            return pattern
    return None


def _detect_recovery_error_in_object(value: object) -> RecoveryErrorPattern | None:
    try:
        rendered = json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        rendered = str(value)
    return _detect_recovery_error_in_text(rendered)


def _preferred_recovery_error(
    *candidates: RecoveryErrorPattern | None,
) -> RecoveryErrorPattern | None:
    preferred: RecoveryErrorPattern | None = None
    for candidate in candidates:
        if candidate is None:
            continue
        if preferred is None or (
            preferred.alert_reason is None and candidate.alert_reason is not None
        ):
            preferred = candidate
    return preferred


def classify_autoresearch_task(task: Mapping[str, object]) -> TaskProvenance:
    """Classify the two exact OpenClaw 2026.7.1-2 task-list projections."""
    # The CLI emits raw TaskRecord.requesterSessionKey. Gateway tasks.list maps
    # that same field to TaskSummary.sessionKey. If both appear they must agree.
    raw_requester_key = task.get("requesterSessionKey")
    summary_session_key = task.get("sessionKey")
    if (
        raw_requester_key is not None
        and summary_session_key is not None
        and raw_requester_key != summary_session_key
    ):
        return TaskProvenance.AMBIGUOUS
    requester_key = raw_requester_key if raw_requester_key is not None else summary_session_key
    owner_key = task.get("ownerKey")
    owns_one_key = (
        requester_key == AUTORESEARCH_OWNER_SESSION_KEY
        or owner_key == AUTORESEARCH_OWNER_SESSION_KEY
    )
    has_native_codex_marker = task.get("taskKind") is not None
    if (
        requester_key != AUTORESEARCH_OWNER_SESSION_KEY
        or owner_key != AUTORESEARCH_OWNER_SESSION_KEY
    ):
        if has_native_codex_marker and not owns_one_key:
            return TaskProvenance.UNRELATED
        if owns_one_key or task.get("agentId") == AUTORESEARCH_OWNER_AGENT_ID:
            return TaskProvenance.AMBIGUOUS
        return TaskProvenance.UNRELATED

    agent_id = task.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return TaskProvenance.AMBIGUOUS
    runtime = task.get("runtime")
    task_kind = task.get("taskKind")
    run_id = task.get("runId")
    if has_native_codex_marker:
        if (
            runtime == "subagent"
            and task_kind == "codex-native"
            and isinstance(run_id, str)
            and run_id.startswith("codex-thread:")
        ):
            return TaskProvenance.CODEX_NATIVE_SUBAGENT
        return TaskProvenance.AMBIGUOUS
    child_session_key = task.get("childSessionKey")
    if agent_id == AUTORESEARCH_OWNER_AGENT_ID:
        if child_session_key is None or child_session_key == AUTORESEARCH_OWNER_SESSION_KEY:
            return TaskProvenance.OWNER_TURN
        return TaskProvenance.AMBIGUOUS

    stage_agent_ids = RELEVANT_AGENT_IDS - {AUTORESEARCH_OWNER_AGENT_ID}
    if agent_id not in stage_agent_ids:
        return TaskProvenance.AMBIGUOUS
    if not isinstance(child_session_key, str):
        return TaskProvenance.AMBIGUOUS
    child_parts = child_session_key.split(":", 2)
    if (
        len(child_parts) != 3
        or child_parts[0] != "agent"
        or child_parts[1] != agent_id
        or not child_parts[2]
    ):
        return TaskProvenance.AMBIGUOUS
    return TaskProvenance.STAGE_CHILD


def _task_id_for_reconciliation(task: Mapping[str, object], *, source: str) -> str:
    task_id = task.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskReconciliationError(f"{source} task is missing a non-empty taskId")
    legacy_id = task.get("id")
    if legacy_id is not None and (
        not isinstance(legacy_id, str) or not legacy_id.strip() or legacy_id != task_id
    ):
        raise TaskReconciliationError(f"{source} task id must agree with canonical taskId")
    return task_id


def _task_provenance_fingerprint(task: Mapping[str, object]) -> tuple[object, ...]:
    provenance = classify_autoresearch_task(task)
    if provenance not in {
        TaskProvenance.OWNER_TURN,
        TaskProvenance.STAGE_CHILD,
        TaskProvenance.CODEX_NATIVE_SUBAGENT,
    }:
        raise TaskReconciliationError("canonical task has invalid autoresearch provenance")
    requester_key = task.get("requesterSessionKey")
    if requester_key is None:
        requester_key = task.get("sessionKey")
    return (
        provenance,
        task.get("agentId"),
        requester_key,
        task.get("ownerKey"),
        task.get("childSessionKey"),
        task.get("runtime"),
        task.get("taskKind"),
        task.get("runId"),
    )


def reconcile_relevant_running_tasks(
    rpc: OpenClawRPC,
    tasks: Sequence[Mapping[str, object]],
    *,
    shutdown_requested: ShutdownRequested = _shutdown_not_requested,
) -> ReconciledRunningTasks:
    """Resolve relevant task-list projections through canonical gateway task records."""
    running_tasks: list[Mapping[str, object]] = []
    terminal_task_seen = False
    observed_error: RecoveryErrorPattern | None = None
    terminal_statuses = {
        CanonicalTaskStatus.COMPLETED,
        CanonicalTaskStatus.FAILED,
        CanonicalTaskStatus.TIMED_OUT,
        CanonicalTaskStatus.CANCELLED,
    }
    for projected_task in tasks:
        provenance = classify_autoresearch_task(projected_task)
        if provenance is TaskProvenance.UNRELATED:
            continue
        if provenance is TaskProvenance.AMBIGUOUS:
            raise TaskReconciliationError(
                "task-list projection has ambiguous autoresearch provenance"
            )
        task_id = _task_id_for_reconciliation(projected_task, source="task-list")
        try:
            canonical_task = rpc.get_task(
                task_id=task_id,
                shutdown_requested=shutdown_requested,
            )
        except SupervisorError as exc:
            raise TaskReconciliationError(
                f"tasks.get RPC failed during reconciliation: {task_id}"
            ) from exc
        if _task_id_for_reconciliation(canonical_task, source="tasks.get") != task_id:
            raise TaskReconciliationError("tasks.get taskId does not match task-list projection")
        if _task_provenance_fingerprint(canonical_task) != _task_provenance_fingerprint(
            projected_task
        ):
            raise TaskReconciliationError(
                "tasks.get provenance does not match task-list projection"
            )
        status_raw = canonical_task.get("status")
        if not isinstance(status_raw, str):
            raise TaskReconciliationError("tasks.get response is missing a string status")
        try:
            status = CanonicalTaskStatus(status_raw)
        except ValueError as exc:
            raise TaskReconciliationError(
                f"tasks.get response has unsupported status: {status_raw}"
            ) from exc
        if status is CanonicalTaskStatus.RUNNING:
            running_tasks.append(canonical_task)
            observed_error = _preferred_recovery_error(
                observed_error,
                _detect_recovery_error_in_object(canonical_task),
            )
        elif status in terminal_statuses:
            terminal_task_seen = True
            observed_error = _preferred_recovery_error(
                observed_error,
                _detect_recovery_error_in_object(canonical_task),
            )
        else:
            raise TaskReconciliationError(
                f"tasks.get response has non-terminal non-running status: {status.value}"
            )
    return ReconciledRunningTasks(tuple(running_tasks), terminal_task_seen, observed_error)


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    state_path: Path = DEFAULT_STATE_PATH
    readiness_manifest_path: Path = DEFAULT_PLATFORM_READINESS_PATH
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH
    autoresearch_dir: Path = DEFAULT_AUTORESEARCH_DIR
    owner_sessions_path: Path = DEFAULT_OWNER_SESSIONS_PATH
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS
    target_repo: Path = DEFAULT_QUANTIPY_ROOT
    proc_root: Path = Path("/proc")
    claim_stale_seconds: float = DEFAULT_CLAIM_STALE_SECONDS
    expected_stage_task_stale_seconds: float = DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS
    max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT

    def __post_init__(self) -> None:
        _require_finite_positive(self.poll_interval_seconds, field_name="poll_interval_seconds")
        _require_finite_positive(self.grace_period_seconds, field_name="grace_period_seconds")
        _require_finite_positive(self.claim_stale_seconds, field_name="claim_stale_seconds")
        _require_finite_positive(
            self.expected_stage_task_stale_seconds,
            field_name="expected_stage_task_stale_seconds",
        )
        if self.max_recovery_attempts < 1:
            raise SupervisorError("max_recovery_attempts must be at least one")


@dataclass(frozen=True, slots=True)
class StateProbe:
    fingerprint: str
    latest_update_ts: float
    latest_update_path: Path


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    outcome: SupervisorOutcome
    reason: str
    recovery_key: str | None = None
    sent_wake: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    reason: str
    message: str
    key_prefix: str


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    recovery_key: str
    token: str


@dataclass(frozen=True, slots=True)
class MalformedRunRecord:
    run_directory: Path
    attempt: int | None
    error: AutoresearchRunRecordError


@dataclass(slots=True)
class RecoveryRecord:
    status: RecoveryStatus = RecoveryStatus.READY
    attempt_count: int = 0
    claim_token: str | None = None
    claim_pid: int | None = None
    claim_process_identity: str | None = None
    claim_started_at: float | None = None
    woke_at: float | None = None
    failed_at: float | None = None
    last_error: str | None = None
    alerted: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RecoveryRecord:
        status_raw = raw.get("status", RecoveryStatus.READY.value)
        if not isinstance(status_raw, str):
            raise SupervisorError(f"invalid recovery status: {status_raw!r}")
        try:
            status = RecoveryStatus(status_raw)
        except ValueError as exc:
            raise SupervisorError(f"invalid recovery status: {status_raw!r}") from exc
        attempt_count = raw.get("attempt_count", 0)
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise SupervisorError("recovery attempt_count must be a non-negative integer")
        return cls(
            status=status,
            attempt_count=attempt_count,
            claim_token=_optional_str(raw.get("claim_token")),
            claim_pid=_optional_int(raw.get("claim_pid")),
            claim_process_identity=_optional_str(raw.get("claim_process_identity")),
            claim_started_at=_optional_float(raw.get("claim_started_at")),
            woke_at=_optional_float(raw.get("woke_at")),
            failed_at=_optional_float(raw.get("failed_at")),
            last_error=_optional_str(raw.get("last_error")),
            alerted=raw.get("alerted") is True,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SupervisorCheckpoint:
    recovery_records: dict[str, RecoveryRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SupervisorCheckpoint:
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_strict_json_object
            )
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            raise SupervisorError(f"failed to read supervisor checkpoint {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SupervisorError(f"invalid supervisor checkpoint JSON: {path}") from exc
        if not isinstance(raw, Mapping):
            raise SupervisorError(f"invalid supervisor checkpoint payload: {path}")
        records_raw = raw.get("recovery_records", {})
        if not isinstance(records_raw, Mapping):
            raise SupervisorError("recovery_records must be an object")
        records: dict[str, RecoveryRecord] = {}
        for key, value in records_raw.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                raise SupervisorError("recovery_records entries must be keyed objects")
            records[key] = RecoveryRecord.from_dict(value)
        return cls(recovery_records=records)

    def save(self, path: Path) -> None:
        serialized = json.dumps(
            {
                "recovery_records": {
                    key: record.to_dict() for key, record in self.recovery_records.items()
                }
            },
            indent=2,
            sort_keys=True,
        )
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except OSError as exc:
            raise SupervisorError(
                f"failed to atomically save supervisor checkpoint {path}: {exc}"
            ) from exc
        finally:
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()


def reset_recovery_checkpoint_for_manual_wake(path: Path, *, iteration: int, phase: str) -> None:
    """Allow an explicit operator wake to retry an exhausted recovery key.

    Recovery attempt limits protect the autonomous supervisor from retry loops,
    but an operator-initiated wake is a deliberate new recovery window. Remove
    only failed or exhausted records for the current state phase; successful
    records remain bounded history and must not be reset implicitly.
    """
    checkpoint_path = path.expanduser()
    lock_path = checkpoint_path.with_name(f"{checkpoint_path.name}.lock")
    prefixes = (
        f"stale_state:{iteration}:{phase}:",
        f"missing_verification_artifact:{iteration}:{phase}:",
    )
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                checkpoint = SupervisorCheckpoint.load(checkpoint_path)
                removed = [
                    key
                    for key, record in checkpoint.recovery_records.items()
                    if key.startswith(prefixes)
                    and record.status in {RecoveryStatus.FAILED, RecoveryStatus.EXHAUSTED}
                ]
                if removed:
                    for key in removed:
                        del checkpoint.recovery_records[key]
                    checkpoint.save(checkpoint_path)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise SupervisorError(
            f"failed to reset supervisor checkpoint {checkpoint_path}: {exc}"
        ) from exc


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _structured_log(level: int, event: str, **fields: object) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, sort_keys=True, default=str))


class AutoresearchSupervisor:
    """Monitors Quantipy and wakes only its dedicated owner session."""

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        task_gateway: TaskGateway | None = None,
    ) -> None:
        self.config = config or SupervisorConfig()
        self._now = now
        self._sleep = sleep
        self._rpc = OpenClawRPC(task_gateway)

    def run_once(
        self, *, shutdown_requested: ShutdownRequested = _shutdown_not_requested
    ) -> SupervisorResult:
        state = self._load_state()
        if state.suspended:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "platform_readiness_suspended")
        if self._is_terminal_state(state):
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "terminal_state")
        try:
            self._validate_dispatchable_state(state)
        except SupervisorError as exc:
            _structured_log(logging.ERROR, "supervisor.readiness_blocked", detail=str(exc))
            return SupervisorResult(
                SupervisorOutcome.ALERT,
                f"platform_readiness_blocked: {exc}",
            )
        run_record_result = self._consume_terminal_verification_run(state)
        if run_record_result is not None:
            return run_record_result
        if state.phase in EARLY_OWNER_LIFECYCLE_SHORT_CIRCUIT_PHASES:
            lifecycle_result = self._owner_lifecycle_guard(state)
            if lifecycle_result is not None and lifecycle_result.reason == "active_owner_session":
                return lifecycle_result
        try:
            reconciled_tasks = self._reconciled_running_tasks(shutdown_requested=shutdown_requested)
        except TaskReconciliationError:
            return SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")
        activity = self._activity_guard(state, reconciled_tasks)
        if activity is not None:
            return activity
        try:
            probe = self._build_state_probe(state)
        except SupervisorError as exc:
            return SupervisorResult(SupervisorOutcome.ALERT, f"invalid_progress_evidence: {exc}")
        if self._now() - probe.latest_update_ts < self.config.grace_period_seconds:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "state_not_stale")
        try:
            reconciled_tasks = self._reconciled_running_tasks(shutdown_requested=shutdown_requested)
        except TaskReconciliationError:
            return SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")
        activity = self._activity_guard(state, reconciled_tasks)
        if activity is not None:
            return activity
        writers = self._active_target_repo_writer_processes(state)
        if writers:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "target_repo_writer_active")
        recovery_plan = self._recovery_plan(state)
        recovery_key = (
            f"{recovery_plan.key_prefix}:{state.iteration}:{state.phase.value}:{probe.fingerprint}"
        )
        detected_error = _preferred_recovery_error(
            reconciled_tasks.observed_error,
            self._detect_owner_error(),
        )
        claim_or_result = self._claim_recovery(
            state,
            recovery_key,
            detected_error=detected_error,
        )
        if isinstance(claim_or_result, SupervisorResult):
            return claim_or_result
        claim = claim_or_result
        try:
            self._rpc.wake(
                message=recovery_plan.message,
                idempotency_key=make_idempotency_key(
                    purpose="recovery",
                    material=f"{recovery_key}\nclaim={claim.token}",
                ),
                shutdown_requested=shutdown_requested,
            )
        except BaseException as exc:
            self._fail_recovery_claim(claim, exc)
            raise
        self._complete_recovery_claim(claim)
        _structured_log(
            logging.WARNING,
            "supervisor.nudged",
            recovery_key=recovery_key,
            reason=recovery_plan.reason,
            detected_error=detected_error.pattern if detected_error is not None else None,
        )
        return SupervisorResult(
            SupervisorOutcome.NUDGED,
            recovery_plan.reason,
            recovery_key,
            sent_wake=True,
        )

    def _recovery_plan(self, state: AutoresearchState) -> RecoveryPlan:
        if (
            state.phase is Phase.VERIFICATION
            and state.implementation_result is not None
            and not state.verification_history
        ):
            return RecoveryPlan(
                reason=MISSING_VERIFICATION_ARTIFACT_REASON,
                message=MISSING_VERIFICATION_ARTIFACT_RECOVERY_MESSAGE,
                key_prefix=MISSING_VERIFICATION_ARTIFACT_REASON,
            )
        return RecoveryPlan(
            reason="recovery_message_sent",
            message=RECOVERY_MESSAGE,
            key_prefix="stale_state",
        )

    def run_forever(self) -> int:
        stop_requested = False

        def request_stop(signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True
            _structured_log(logging.INFO, "supervisor.signal", signum=signum)

        previous_int = signal.signal(signal.SIGINT, request_stop)
        previous_term = signal.signal(signal.SIGTERM, request_stop)
        try:
            while not stop_requested:
                try:
                    self.run_once(shutdown_requested=lambda: stop_requested)
                except ShutdownInterrupted as exc:
                    _structured_log(
                        logging.INFO,
                        "supervisor.shutdown_interrupted",
                        detail=str(exc),
                    )
                    return 0
                except OpenClawUnavailableError as exc:
                    if stop_requested:
                        _structured_log(
                            logging.INFO,
                            "supervisor.shutdown_interrupted",
                            detail=str(exc),
                        )
                        return 0
                    _structured_log(
                        logging.ERROR,
                        "supervisor.poll_failed",
                        detail=str(exc),
                    )
                deadline = self._now() + self.config.poll_interval_seconds
                while not stop_requested and self._now() < deadline:
                    self._sleep(min(0.5, deadline - self._now()))
            return 0
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)

    def _load_state(self) -> AutoresearchState:
        try:
            raw = json.loads(self.config.state_path.read_text(encoding="utf-8"))
            return AutoresearchState.from_dict(raw)
        except FileNotFoundError as exc:
            raise SupervisorError(
                f"missing autoresearch state file: {self.config.state_path}"
            ) from exc
        except OSError as exc:
            raise SupervisorError(
                f"failed to read autoresearch state file: {self.config.state_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SupervisorError(
                f"invalid autoresearch state JSON: {self.config.state_path}"
            ) from exc
        except AutoresearchValidationError as exc:
            raise SupervisorError(f"invalid autoresearch state: {exc}") from exc

    def _validate_dispatchable_state(self, state: AutoresearchState) -> None:
        """Require a current operator readiness receipt before recovery wake."""
        try:
            readiness = load_platform_readiness(self.config.readiness_manifest_path)
            validate_state_readiness(state.platform_readiness, readiness)
        except ValueError as exc:
            raise SupervisorError(f"cannot wake autoresearch: {exc}") from exc

    def _consume_terminal_verification_run(
        self, state: AutoresearchState
    ) -> SupervisorResult | None:
        if state.phase is not Phase.VERIFICATION:
            return None
        state_reference_sha256 = build_authoritative_state_reference(
            state,
            state_path=self.config.state_path,
        ).sha256()
        try:
            policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
            quantipy_root = (
                Path(state.setup.target_repo) if state.setup is not None else DEFAULT_QUANTIPY_ROOT
            )
            receipts = build_receipt_catalog(quantipy_root)
            instruction_manifest_sha256 = expected_instruction_manifest_sha256(
                state,
                policy,
                receipts,
                state_path=self.config.state_path,
            )
        except (AutoresearchValidationError, ValueError, OSError) as exc:
            return self._persistent_control_plane_alert(
                key=f"run-record-current-instruction:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=f"cannot_compute_current_instruction_manifest: {exc}",
            )
        try:
            matching = self._matching_verification_runs(
                iteration=state.iteration,
                state_reference_sha256=state_reference_sha256,
                instruction_manifest_sha256=instruction_manifest_sha256,
            )
        except AutoresearchRunRecordError as exc:
            return self._persistent_control_plane_alert(
                key=f"run-record:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=f"invalid_detached_run_record: {exc}",
            )
        if not matching:
            return None
        latest = matching[-1]
        if latest.status.state is RunState.RUNNING:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "active_matching_detached_run")
        if latest.status.state is RunState.SUCCEEDED:
            return None
        if latest.status.failure_classification is RunFailureClassification.OPERATOR_STOPPED:
            return self._persistent_control_plane_alert(
                key=(
                    "interrupted-detached-verification:"
                    f"{state.iteration}:{state.phase.value}:{state_reference_sha256}"
                ),
                reason="interrupted_detached_verification_requires_operator_recovery",
            )
        if state.mode is not ResearchMode.ALPHA_RESEARCH:
            return self._persistent_control_plane_alert(
                key=f"run-record-mode:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=(
                    "detached_verification_failure_cannot_form_strict_artifact: "
                    f"mode={state.mode.value if state.mode is not None else 'null'}"
                ),
            )
        artifact = self._verification_failure_artifact(latest)
        try:
            readiness = load_platform_readiness(self.config.readiness_manifest_path)
            context = AutoresearchValidationContext.from_readiness(readiness)
            advance_infrastructure_verification_failure(
                state_path=self.config.state_path,
                state_reference_sha256=state_reference_sha256,
                instruction_manifest_sha256=instruction_manifest_sha256,
                artifact=artifact,
                policy=policy,
                receipts=receipts,
                validation_context=context,
            )
        except (AutoresearchValidationError, ValueError, OSError) as exc:
            return self._persistent_control_plane_alert(
                key=f"run-record-advance:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=f"detached_verification_failure_not_advanced: {exc}",
            )
        return SupervisorResult(
            SupervisorOutcome.NUDGED,
            "detached_verification_failure_advanced",
        )

    def _matching_verification_runs(
        self,
        *,
        iteration: int,
        state_reference_sha256: str,
        instruction_manifest_sha256: str,
    ) -> tuple[RunRecord, ...]:
        root = self.config.runs_root
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise AutoresearchRunRecordError(f"cannot inspect runs root: {exc}") from exc
        if not root_metadata:
            return ()
        if root.is_symlink() or not root.is_dir():
            raise AutoresearchRunRecordError("runs root must be a non-symlink directory")
        records: list[RunRecord] = []
        stale_records: list[RunRecord] = []
        malformed_records: list[MalformedRunRecord] = []
        attempts: set[int] = set()
        for directory, child_directories, files in os.walk(root, followlinks=False):
            child_directories.sort()
            files.sort()
            parent = Path(directory)
            for child_directory in child_directories:
                if (parent / child_directory).is_symlink():
                    raise AutoresearchRunRecordError("symlinked detached run directory")
            if "manifest.json" not in files:
                continue
            try:
                record = read_run_record(run_dir=parent, runs_root=root)
            except AutoresearchRunRecordError as exc:
                malformed = self._malformed_relevant_verification_run(
                    parent,
                    iteration=iteration,
                    error=exc,
                )
                if malformed is not None:
                    malformed_records.append(malformed)
                continue
            manifest = record.manifest
            if manifest.phase is not Phase.VERIFICATION or manifest.iteration != iteration:
                continue
            if manifest.state_reference_sha256 != state_reference_sha256:
                stale_records.append(record)
                continue
            if manifest.instruction_manifest_sha256 != instruction_manifest_sha256:
                stale_records.append(record)
                continue
            if manifest.attempt in attempts:
                raise AutoresearchRunRecordError("duplicate detached run attempt")
            if record.status.state is RunState.RUNNING:
                record = self._recover_terminal_systemd_run(record)
            attempts.add(manifest.attempt)
            records.append(record)
        latest_matching_attempt = max((record.manifest.attempt for record in records), default=0)
        latest_stale_attempt = max((record.manifest.attempt for record in stale_records), default=0)
        latest_malformed = max(
            malformed_records,
            key=lambda malformed: malformed.attempt if malformed.attempt is not None else 10**12,
            default=None,
        )
        if latest_malformed is not None:
            malformed_attempt = latest_malformed.attempt
            if malformed_attempt is None or malformed_attempt >= max(
                latest_matching_attempt,
                latest_stale_attempt,
            ):
                raise AutoresearchRunRecordError(
                    f"malformed latest detached run record: {latest_malformed.error}"
                )
        if latest_stale_attempt > latest_matching_attempt:
            _structured_log(
                logging.WARNING,
                "supervisor.ignored_stale_detached_run_history",
                iteration=iteration,
                phase=Phase.VERIFICATION.value,
                latest_stale_attempt=latest_stale_attempt,
                latest_matching_attempt=latest_matching_attempt,
            )
        return tuple(sorted(records, key=lambda record: record.manifest.attempt))

    def _malformed_relevant_verification_run(
        self,
        run_dir: Path,
        *,
        iteration: int,
        error: AutoresearchRunRecordError,
    ) -> MalformedRunRecord | None:
        try:
            raw = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8"),
                object_pairs_hook=_strict_json_object,
            )
            manifest = RunManifest.from_dict(raw)
        except (OSError, json.JSONDecodeError, AutoresearchRunRecordError):
            return MalformedRunRecord(run_dir, None, error)
        if manifest.phase is not Phase.VERIFICATION or manifest.iteration != iteration:
            return None
        return MalformedRunRecord(run_dir, manifest.attempt, error)

    def _recover_terminal_systemd_run(self, record: RunRecord) -> RunRecord:
        unit = record.status.systemd_unit
        if unit is None:
            return record
        try:
            properties = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--no-pager",
                    "--property=Result",
                    "--property=ActiveState",
                    "--property=ExecMainStatus",
                    "--property=MemoryPeak",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return record
        if properties.returncode != 0:
            if self._detached_run_process_alive(record.status.pid):
                return record
            return self._terminalize_disappeared_detached_run(record)
        parsed: dict[str, str] = {}
        for line in properties.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                parsed[key] = value
        if parsed.get("Result") != "oom-kill":
            active_state = parsed.get("ActiveState")
            if active_state not in {"inactive", "failed"}:
                return record
            if self._detached_run_process_alive(record.status.pid):
                return record
            return self._terminalize_disappeared_detached_run(
                record,
                peak_rss_bytes=(
                    int(parsed["MemoryPeak"]) if parsed.get("MemoryPeak", "").isdigit() else None
                ),
            )
        peak_rss_bytes = None
        if parsed.get("MemoryPeak", "").isdigit():
            peak_rss_bytes = int(parsed["MemoryPeak"])
        status_value = parsed.get("ExecMainStatus", "")
        signal_number = int(status_value) if status_value.isdigit() and int(status_value) > 0 else 9
        exit_code = 128 + signal_number
        complete_run(
            run_dir=record.run_directory,
            runs_root=self.config.runs_root,
            exit_code=exit_code,
            signal_number=signal_number,
            peak_rss_bytes=peak_rss_bytes,
            failure_classification=RunFailureClassification.RESOURCE_EXHAUSTED,
        )
        return read_run_record(run_dir=record.run_directory, runs_root=self.config.runs_root)

    def _detached_run_process_alive(self, pid: int | None) -> bool:
        """Return whether the recorded child is still a non-zombie process."""
        if pid is None or pid < 1:
            return False
        try:
            raw = (self.config.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SupervisorError(f"failed to inspect detached run process {pid}: {exc}") from exc
        closing = raw.rfind(")")
        fields = raw[closing + 1 :].split() if closing >= 0 else []
        if not fields:
            raise SupervisorError(f"malformed process stat for detached run pid {pid}")
        return fields[0] != "Z"

    def _terminalize_disappeared_detached_run(
        self, record: RunRecord, *, peak_rss_bytes: int | None = None
    ) -> RunRecord:
        """Close a running record after its worker and systemd unit disappeared."""
        _structured_log(
            logging.WARNING,
            "supervisor.terminalized_disappeared_detached_run",
            pid=record.status.pid,
            run_directory=str(record.run_directory),
            systemd_unit=record.status.systemd_unit,
        )
        complete_run(
            run_dir=record.run_directory,
            runs_root=self.config.runs_root,
            exit_code=1,
            signal_number=None,
            peak_rss_bytes=peak_rss_bytes,
            failure_classification=RunFailureClassification.PROCESS_ERROR,
        )
        return read_run_record(run_dir=record.run_directory, runs_root=self.config.runs_root)

    def _verification_failure_artifact(self, record: RunRecord) -> VerificationResultArtifact:
        evidence = json.dumps(
            {
                "exit_code": record.status.exit_code,
                "failure_classification": record.status.failure_classification.value
                if record.status.failure_classification is not None
                else None,
                "finished_at": record.status.finished_at,
                "peak_rss_bytes": record.status.resource_usage.peak_rss_bytes,
                "run_directory": str(record.run_directory),
                "signal_number": record.status.signal_number,
                "started_at": record.status.started_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return VerificationResultArtifact(
            status=VerificationStatus.TEST_FAILURE,
            is_walk_forward_sharpe_net=None,
            oos_sharpe_net=None,
            max_drawdown_pct=None,
            win_rate=None,
            trade_count=None,
            trades_per_day=None,
            oos_trading_days=None,
            feature_importances_summary=evidence,
            null_test_summary=evidence,
            bug_signals=(),
            tests_passed=False,
            commands_run=(),
            data_coverage=None,
        )

    def _persistent_control_plane_alert(self, *, key: str, reason: str) -> SupervisorResult:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = checkpoint.recovery_records.setdefault(key, RecoveryRecord())
            if record.alerted:
                return SupervisorResult(SupervisorOutcome.NO_ACTION, "alert_already_emitted", key)
            record.status = RecoveryStatus.EXHAUSTED
            record.last_error = reason[:1000]
            record.alerted = True
            checkpoint.save(self.config.checkpoint_path)
        return SupervisorResult(SupervisorOutcome.ALERT, reason, key)

    def _is_terminal_state(self, state: AutoresearchState) -> bool:
        decision = state.final_decision
        return (
            state.phase is Phase.REPEAT
            and decision is not None
            and not decision.continue_loop
            and (not decision.memory_write_required or state.memory_written)
        )

    def _running_tasks(self, *, shutdown_requested: ShutdownRequested) -> list[dict[str, object]]:
        payload = self._rpc.list_running_tasks(
            shutdown_requested=shutdown_requested,
        )
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, str | bytes):
            raise SupervisorError("OpenClaw tasks JSON missing tasks array")
        if not all(isinstance(task, Mapping) for task in raw_tasks):
            raise SupervisorError("OpenClaw tasks JSON contains a non-object task")
        return [dict(task) for task in raw_tasks if isinstance(task, Mapping)]

    def _reconciled_running_tasks(
        self, *, shutdown_requested: ShutdownRequested
    ) -> ReconciledRunningTasks:
        return reconcile_relevant_running_tasks(
            self._rpc,
            self._running_tasks(shutdown_requested=shutdown_requested),
            shutdown_requested=shutdown_requested,
        )

    def _expected_stage_agent_ids(self, state: AutoresearchState) -> tuple[str, ...]:
        if state.phase is Phase.SETUP_CONTEXT:
            return (AUTORESEARCH_OWNER_AGENT_ID,) if state.setup is None else ("context_curator",)
        return EXPECTED_STAGE_AGENT_IDS[state.phase]

    def _activity_guard(
        self, state: AutoresearchState, reconciled_tasks: ReconciledRunningTasks
    ) -> SupervisorResult | None:
        running_tasks = reconciled_tasks.running_tasks
        expected = [
            task
            for task in running_tasks
            if (
                classify_autoresearch_task(task) is TaskProvenance.CODEX_NATIVE_SUBAGENT
                or task.get("agentId") in self._expected_stage_agent_ids(state)
            )
            and self._is_relevant_task(task)
        ]
        if expected:
            now_ms = int(self._now() * 1000)
            stale_ms = int(self.config.expected_stage_task_stale_seconds * 1000)
            if any(
                (timestamp := _task_last_event_ms(task)) is None or now_ms - timestamp > stale_ms
                for task in expected
            ):
                return SupervisorResult(SupervisorOutcome.ALERT, "stale_expected_stage_task")
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "active_expected_stage_task")
        lifecycle_result = self._owner_lifecycle_guard(state)
        if lifecycle_result is not None:
            if lifecycle_result.reason == OWNER_SESSION_STORE_UNAVAILABLE_REASON:
                return lifecycle_result
            if (
                lifecycle_result.reason == "active_owner_session"
                or not reconciled_tasks.terminal_task_seen
            ):
                return lifecycle_result
        fresh = [
            task
            for task in running_tasks
            if self._is_relevant_task(task)
            and (timestamp := _task_last_event_ms(task)) is not None
            and int(self._now() * 1000) - timestamp <= int(self.config.grace_period_seconds * 1000)
        ]
        if fresh:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "fresh_relevant_task")
        return None

    def _owner_lifecycle_guard(self, state: AutoresearchState) -> SupervisorResult | None:
        try:
            store = self._load_owner_session_store()
        except SupervisorError:
            return SupervisorResult(SupervisorOutcome.ALERT, OWNER_SESSION_STORE_UNAVAILABLE_REASON)
        lifecycle = store.get(AUTORESEARCH_OWNER_SESSION_KEY)
        if lifecycle is None:
            return None
        if not isinstance(lifecycle, Mapping):
            return SupervisorResult(SupervisorOutcome.ALERT, "invalid_owner_session_lifecycle")
        status = lifecycle.get("status")
        if not isinstance(status, str):
            return SupervisorResult(SupervisorOutcome.ALERT, "invalid_owner_session_lifecycle")
        if status != "running":
            return None
        if lifecycle.get("endedAt") is not None or lifecycle.get("abortedLastRun") is True:
            return SupervisorResult(SupervisorOutcome.ALERT, "contradictory_running_owner_session")
        try:
            timestamp = _running_lifecycle_last_event_ms(lifecycle)
        except SupervisorError:
            return SupervisorResult(SupervisorOutcome.ALERT, "invalid_owner_session_lifecycle")
        now_ms = int(self._now() * 1000)
        if timestamp > now_ms:
            return SupervisorResult(SupervisorOutcome.ALERT, "contradictory_running_owner_session")
        if now_ms - timestamp <= int(self.config.expected_stage_task_stale_seconds * 1000):
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "active_owner_session")
        if AUTORESEARCH_OWNER_AGENT_ID in self._expected_stage_agent_ids(state):
            if self._active_target_repo_writer_processes(state):
                return SupervisorResult(SupervisorOutcome.NO_ACTION, "active_owner_process")
            return SupervisorResult(SupervisorOutcome.ALERT, "stale_running_owner_session")
        return None

    def _is_relevant_task(self, task: Mapping[str, object]) -> bool:
        return classify_autoresearch_task(task) in {
            TaskProvenance.OWNER_TURN,
            TaskProvenance.STAGE_CHILD,
            TaskProvenance.CODEX_NATIVE_SUBAGENT,
        }

    def _claim_recovery(
        self,
        state: AutoresearchState,
        recovery_key: str,
        *,
        detected_error: RecoveryErrorPattern | None,
    ) -> RecoveryClaim | SupervisorResult:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = checkpoint.recovery_records.setdefault(recovery_key, RecoveryRecord())
            if record.status is RecoveryStatus.SUCCEEDED and record.woke_at is not None:
                elapsed = self._now() - record.woke_at
                if 0 <= elapsed < self.config.grace_period_seconds:
                    return SupervisorResult(
                        SupervisorOutcome.NO_ACTION, "recovery_settling", recovery_key
                    )
            if record.status is RecoveryStatus.IN_FLIGHT:
                if record.claim_started_at is None or record.claim_pid is None:
                    raise SupervisorError(
                        f"in-flight recovery claim lacks owner metadata: {recovery_key}"
                    )
                age = self._now() - record.claim_started_at
                if age < self.config.claim_stale_seconds:
                    return SupervisorResult(
                        SupervisorOutcome.NO_ACTION, "recovery_in_flight", recovery_key
                    )
                if self._claim_owner_alive(record):
                    return self._alert(
                        checkpoint, record, recovery_key, "stale_recovery_claim_owner_alive"
                    )
            if record.attempt_count >= self.config.max_recovery_attempts:
                record.status = RecoveryStatus.EXHAUSTED
                return self._alert(
                    checkpoint,
                    record,
                    recovery_key,
                    (
                        f"{self._exhausted_recovery_reason(record, detected_error)}: "
                        f"iteration={state.iteration}; phase={state.phase.value}; "
                        f"recovery_key={recovery_key}; "
                        f"last_failure_reason={record.last_error or 'none'}"
                    ),
                )
            pid = os.getpid()
            identity = self._process_identity(pid)
            token = (
                f"{pid}:{identity or 'unknown'}:{record.attempt_count + 1}:{int(self._now() * 1e9)}"
            )
            record.status = RecoveryStatus.IN_FLIGHT
            record.attempt_count += 1
            record.claim_token = token
            record.claim_pid = pid
            record.claim_process_identity = identity
            record.claim_started_at = self._now()
            record.failed_at = None
            record.last_error = None
            checkpoint.save(self.config.checkpoint_path)
            return RecoveryClaim(recovery_key, token)

    def _complete_recovery_claim(self, claim: RecoveryClaim) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._owned_claim(checkpoint, claim)
            record.status = RecoveryStatus.SUCCEEDED
            record.woke_at = self._now()
            record.last_error = None
            checkpoint.save(self.config.checkpoint_path)

    def _fail_recovery_claim(self, claim: RecoveryClaim, error: BaseException) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._owned_claim(checkpoint, claim)
            record.status = RecoveryStatus.FAILED
            record.failed_at = self._now()
            record.last_error = f"{type(error).__name__}: {error}"[:1000]
            checkpoint.save(self.config.checkpoint_path)

    def _owned_claim(
        self, checkpoint: SupervisorCheckpoint, claim: RecoveryClaim
    ) -> RecoveryRecord:
        record = checkpoint.recovery_records.get(claim.recovery_key)
        if (
            record is None
            or record.status is not RecoveryStatus.IN_FLIGHT
            or record.claim_token != claim.token
        ):
            raise SupervisorError(
                f"recovery claim ownership changed unexpectedly: {claim.recovery_key}"
            )
        return record

    def _alert(
        self,
        checkpoint: SupervisorCheckpoint,
        record: RecoveryRecord,
        recovery_key: str,
        reason: str,
    ) -> SupervisorResult:
        if record.alerted:
            return SupervisorResult(
                SupervisorOutcome.NO_ACTION, "alert_already_emitted", recovery_key
            )
        record.alerted = True
        checkpoint.save(self.config.checkpoint_path)
        return SupervisorResult(SupervisorOutcome.ALERT, reason, recovery_key)

    def _exhausted_recovery_reason(
        self, record: RecoveryRecord, detected_error: RecoveryErrorPattern | None
    ) -> str:
        matched_error = _preferred_recovery_error(
            detected_error,
            _detect_recovery_error_in_text(record.last_error)
            if record.last_error is not None
            else None,
        )
        if matched_error is not None and matched_error.alert_reason is not None:
            return matched_error.alert_reason
        return "recovery_attempts_exhausted"

    @contextmanager
    def _checkpoint_lock(self) -> Iterator[None]:
        lock_path = self.config.checkpoint_path.with_name(
            f"{self.config.checkpoint_path.name}.lock"
        )
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise SupervisorError(
                f"failed to lock supervisor checkpoint {lock_path}: {exc}"
            ) from exc

    def _claim_owner_alive(self, record: RecoveryRecord) -> bool:
        if record.claim_pid is None or record.claim_pid <= 0:
            raise SupervisorError("recovery claim has an invalid owner pid")
        if record.claim_pid == os.getpid():
            return True
        current = self._process_identity(record.claim_pid)
        return current is not None and (
            record.claim_process_identity is None or current == record.claim_process_identity
        )

    def _process_identity(self, pid: int) -> str | None:
        try:
            raw = (self.config.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SupervisorError(
                f"failed to inspect process identity for pid {pid}: {exc}"
            ) from exc
        closing = raw.rfind(")")
        fields = raw[closing + 1 :].split() if closing >= 0 else []
        if len(fields) <= 19:
            raise SupervisorError(f"malformed process stat for pid {pid}")
        return fields[19]

    def _load_owner_session_store(self) -> Mapping[str, object]:
        try:
            raw = json.loads(
                self.config.owner_sessions_path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_json_object,
            )
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SupervisorError(f"failed to read owner session store: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SupervisorError("invalid owner session store JSON") from exc
        if not isinstance(raw, Mapping):
            raise SupervisorError("invalid owner session store payload")
        return raw

    def _detect_owner_error(self) -> RecoveryErrorPattern | None:
        lifecycle = self._load_owner_session_store().get(AUTORESEARCH_OWNER_SESSION_KEY)
        if not isinstance(lifecycle, Mapping):
            return None
        detected = _detect_recovery_error_in_object(lifecycle)
        session_file = lifecycle.get("sessionFile")
        if isinstance(session_file, str):
            detected = _preferred_recovery_error(
                detected,
                _detect_recovery_error_in_text(
                    self._tail_text(Path(session_file), bytes_limit=128_000)
                ),
            )
        return detected

    def _tail_text(self, path: Path, *, bytes_limit: int) -> str:
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > bytes_limit:
                    handle.seek(size - bytes_limit)
                    handle.readline()
                return handle.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise SupervisorError(f"failed to read owner transcript tail {path}: {exc}") from exc

    def _active_target_repo_writer_processes(self, state: AutoresearchState) -> tuple[str, ...]:
        roots = self._target_writer_roots(state)
        if not self.config.proc_root.is_dir():
            raise SupervisorError(f"process filesystem is unavailable: {self.config.proc_root}")
        writers: list[str] = []
        for process_dir in self.config.proc_root.glob("[0-9]*"):
            try:
                pid = int(process_dir.name)
                command = (
                    (process_dir / "cmdline")
                    .read_bytes()
                    .replace(b"\x00", b" ")
                    .decode("utf-8", "replace")
                )
                cwd = (process_dir / "cwd").resolve()
            except (OSError, ValueError):
                continue
            if pid in {os.getpid(), os.getppid()} or not TARGET_WRITER_COMMAND_RE.search(command):
                continue
            if any(cwd == root or root in cwd.parents for root in roots):
                writers.append(f"{pid}:{command[:200]}")
        return tuple(writers)

    def _target_repo_root(self, state: AutoresearchState) -> Path:
        return (
            Path(state.setup.target_repo).expanduser().resolve()
            if state.setup is not None
            else self.config.target_repo.expanduser().resolve()
        )

    def _target_writer_roots(self, state: AutoresearchState) -> tuple[Path, ...]:
        repo_root = self._target_repo_root(state)
        implementation_result = state.implementation_result
        if implementation_result is None:
            return (repo_root,)
        workspace_path = implementation_result.workspace_path
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            raise WorkspaceEvidenceError(
                "implementation_result workspace_path must be a non-empty string"
            )
        candidate = Path(workspace_path).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceEvidenceError("implementation_result workspace_path must be absolute")
        try:
            workspace_root = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceEvidenceError(
                "implementation_result workspace_path cannot be resolved strictly"
            ) from exc
        return (repo_root,) if workspace_root == repo_root else (repo_root, workspace_root)

    def _build_state_probe(self, state: AutoresearchState) -> StateProbe:
        # The authoritative state was parsed and normalized by _load_state. Repository
        # mtimes are neither signed nor bound to this iteration, so a stray touch must
        # not postpone recovery.
        paths = [self.config.state_path]
        latest = 0.0
        latest_path = self.config.state_path
        parts: list[str] = []
        now = self._now()
        for path in paths:
            try:
                metadata = path.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SupervisorError(f"failed to stat supervision path {path}: {exc}") from exc
            if metadata.st_mtime > now:
                if path == self.config.state_path:
                    raise SupervisorError("state progress evidence is future-dated")
                continue
            parts.append(f"{path}:{metadata.st_mtime_ns}:{metadata.st_size}")
            if metadata.st_mtime > latest:
                latest, latest_path = metadata.st_mtime, path
        if latest == 0.0:
            raise SupervisorError("could not determine any autoresearch progress timestamps")
        return StateProbe(
            hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest(), latest, latest_path
        )

    def _git_marker_paths(self, repo_root: Path) -> list[Path]:
        git_root = repo_root / ".git"
        return [git_root / "HEAD", git_root / "index", git_root / "logs" / "HEAD"]


def _task_last_event_ms(task: Mapping[str, object]) -> int | None:
    for field_name in ("lastEventAt", "updatedAt", "startedAt", "createdAt"):
        value = task.get(field_name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
        ):
            continue
        return int(value)
    return None


def _running_lifecycle_last_event_ms(lifecycle: Mapping[str, object]) -> int:
    # OpenClaw's sessions.json uses updatedAt for record writes; lastInteractionAt
    # is the only lifecycle field that evidences activity for a running session.
    value = lifecycle.get("lastInteractionAt")
    if isinstance(value, bool) or not isinstance(value, int):
        raise SupervisorError("running owner lifecycle entry is missing integer lastInteractionAt")
    return value


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--interval", type=_finite_positive_cli_float, default=DEFAULT_POLL_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--grace", type=_finite_positive_cli_float, default=DEFAULT_GRACE_PERIOD_SECONDS
    )
    parser.add_argument(
        "--expected-stage-task-stale",
        type=_finite_positive_cli_float,
        default=DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS,
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--checkpoint-path", type=Path, default=DEFAULT_CHECKPOINT_PATH)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO))
    try:
        supervisor = AutoresearchSupervisor(
            SupervisorConfig(
                state_path=args.state_path,
                checkpoint_path=args.checkpoint_path,
                autoresearch_dir=args.state_path.parent,
                poll_interval_seconds=args.interval,
                grace_period_seconds=args.grace,
                expected_stage_task_stale_seconds=args.expected_stage_task_stale,
            )
        )
        if args.once:
            supervisor.run_once()
            return 0
        return supervisor.run_forever()
    except SupervisorError as exc:
        _structured_log(logging.ERROR, "supervisor.failure", detail=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
