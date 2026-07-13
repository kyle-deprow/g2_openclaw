"""Deterministic, owner-session-only supervisor for Quantipy autoresearch."""

from __future__ import annotations

import argparse
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
from typing import NoReturn

from gateway.autoresearch_runner import (
    DEFAULT_QUANTIPY_ROOT,
    AutoresearchState,
    AutoresearchValidationError,
    Phase,
    normalize_autoresearch_state,
)

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
DEFAULT_OPENCLAW_BIN = Path.home() / ".local" / "share" / "pnpm" / "openclaw"
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_GRACE_PERIOD_SECONDS = 120.0
DEFAULT_CLAIM_STALE_SECONDS = 300.0
DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS = 300.0
DEFAULT_MAX_RECOVERY_ATTEMPTS = 2
READ_ONLY_TASK_LIST_ATTEMPTS = 3
READ_ONLY_TASK_LIST_RETRY_SECONDS = 0.5
REQUIRED_OPENCLAW_VERSION = (2026, 6, 11)
WAKE_MESSAGE = (
    "Continue Quantipy autoresearch from the authoritative state. First run exactly: "
    "cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json"
)
RECOVERY_MESSAGE = (
    "Continue Quantipy autoresearch from the authoritative state. First run exactly: "
    "cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json. Reconcile terminal stage "
    "outputs from task and child-session records before waiting or relaunching; "
    "infrastructure recovery only; no research steering."
)
RECOVERY_ERROR_PATTERNS = (
    "cli transcript compaction failed",
    'no api key found for provider "openai"',
    "context overflow",
    "prompt too long",
    "maximum context length",
    "maximum context size",
    "too many tokens",
)
EXPECTED_STAGE_AGENT_IDS: dict[Phase, tuple[str, ...]] = {
    Phase.DEBATE: (
        "debater-microstructure",
        "debater-data",
        "debater-skeptic",
        "debater-theory",
        "debater-implementation",
    ),
    Phase.CONSENSUS: ("consensus-arbiter",),
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
        "context-curator",
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


class OpenClawResolutionError(SupervisorError):
    """Raised when the pinned executable cannot be resolved safely."""


class OpenClawVersionError(SupervisorError):
    """Raised when the pinned executable version is unsupported."""


class WorkspaceEvidenceError(SupervisorError):
    """Raised when an active implementation workspace cannot be verified."""


class ShutdownInterrupted(Exception):
    """A command failure observed after shutdown was already requested."""


ShutdownRequested = Callable[[], bool]


def _shutdown_not_requested() -> bool:
    return False


def _raise_command_failure(
    detail: str,
    *,
    shutdown_requested: ShutdownRequested,
    cause: Exception | None = None,
    error_type: type[SupervisorError] = SupervisorError,
) -> NoReturn:
    """Classify a failed OpenClaw process before constructing its domain error."""
    if shutdown_requested():
        if cause is None:
            raise ShutdownInterrupted(detail)
        raise ShutdownInterrupted(detail) from cause
    if cause is None:
        raise error_type(detail)
    raise error_type(detail) from cause


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
    """Pinned-executable RPC boundary shared by owner control surfaces."""

    def __init__(
        self,
        default_openclaw_bin: Path,
        *,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._default_openclaw_bin = default_openclaw_bin
        self._run_command = run_command

    def require_binary(
        self, *, shutdown_requested: ShutdownRequested = _shutdown_not_requested
    ) -> Path:
        executable = self._resolve_executable()
        try:
            result = self._run_command(
                [str(executable), "--version"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            _raise_command_failure(
                f"failed to execute OpenClaw at {executable}: {exc}",
                shutdown_requested=shutdown_requested,
                cause=exc,
                error_type=OpenClawResolutionError,
            )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        version = self._parse_version(output)
        if result.returncode != 0 or version is None:
            _raise_command_failure(
                f"OpenClaw version check failed for {executable}: {output or 'no output'}",
                shutdown_requested=shutdown_requested,
                error_type=OpenClawVersionError,
            )
        if version != REQUIRED_OPENCLAW_VERSION:
            required = ".".join(str(part) for part in REQUIRED_OPENCLAW_VERSION)
            raise OpenClawVersionError(
                f"OpenClaw {version[0]}.{version[1]}.{version[2]} at {executable} is "
                f"unsupported; need exactly {required}."
            )
        return executable

    def run_json(
        self,
        executable: Path,
        args: Sequence[str],
        *,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        command = [str(executable), *args]
        try:
            result = self._run_command(command, check=False, capture_output=True, text=True)
        except OSError as exc:
            _raise_command_failure(
                f"failed to execute {' '.join(command)}: {exc}",
                shutdown_requested=shutdown_requested,
                cause=exc,
            )
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            _raise_command_failure(
                f"OpenClaw command failed ({' '.join(command)}): {output or 'no output'}",
                shutdown_requested=shutdown_requested,
            )
        try:
            parsed = json.loads(output, object_pairs_hook=_strict_json_object)
        except json.JSONDecodeError as exc:
            raise SupervisorError(
                f"OpenClaw command returned invalid JSON ({' '.join(command)})"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise SupervisorError(
                f"OpenClaw command returned non-object JSON ({' '.join(command)})"
            )
        return parsed

    def list_running_tasks(
        self,
        executable: Path,
        *,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        """Read OpenClaw running tasks with strict bounded retry for CLI crashes."""
        args = ["tasks", "list", "--status", "running", "--json"]
        last_error: SupervisorError | None = None
        for attempt in range(1, READ_ONLY_TASK_LIST_ATTEMPTS + 1):
            try:
                return self.run_json(
                    executable,
                    args,
                    shutdown_requested=shutdown_requested,
                )
            except ShutdownInterrupted:
                if last_error is not None:
                    raise SupervisorError(
                        "OpenClaw running task list failed before shutdown during retry: "
                        f"{last_error}"
                    ) from last_error
                raise
            except SupervisorError as exc:
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
                    raise SupervisorError(
                        "OpenClaw running task list failed before shutdown during retry: "
                        f"{last_error}"
                    ) from last_error
        raise SupervisorError(
            "OpenClaw running task list failed after "
            f"{READ_ONLY_TASK_LIST_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def wake(
        self,
        executable: Path,
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
        payload = self.run_json(
            executable,
            ["gateway", "call", "agent", "--json", "--params", params, "--timeout", "30000"],
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

    def delete_owner_session(self, executable: Path) -> bool:
        params = json.dumps(
            {
                "key": AUTORESEARCH_OWNER_SESSION_KEY,
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "deleteTranscript": False,
            },
            separators=(",", ":"),
        )
        payload = self.run_json(
            executable,
            [
                "gateway",
                "call",
                "sessions.delete",
                "--json",
                "--params",
                params,
                "--timeout",
                "30000",
            ],
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

    def abort_owner_run(self, executable: Path, *, run_id: str) -> None:
        params = json.dumps(
            {
                "key": AUTORESEARCH_OWNER_SESSION_KEY,
                "runId": run_id,
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
            },
            separators=(",", ":"),
        )
        payload = self.run_json(
            executable,
            [
                "gateway",
                "call",
                "sessions.abort",
                "--json",
                "--params",
                params,
                "--timeout",
                "30000",
            ],
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

    def cancel_task(self, executable: Path, *, task_id: str) -> None:
        params = json.dumps(
            {"taskId": task_id, "reason": "Quantipy autoresearch stopped by operator."},
            separators=(",", ":"),
        )
        payload = self.run_json(
            executable,
            ["gateway", "call", "tasks.cancel", "--json", "--params", params, "--timeout", "30000"],
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

    def show_task(
        self,
        executable: Path,
        *,
        task_id: str,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        return self.run_json(
            executable,
            ["tasks", "show", task_id, "--json"],
            shutdown_requested=shutdown_requested,
        )

    def _resolve_executable(self) -> Path:
        override = os.environ.get("OPENCLAW_BIN")
        candidate = Path(override).expanduser() if override else self._default_openclaw_bin
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        source = "OPENCLAW_BIN" if override else "the configured pinned path"
        raise OpenClawResolutionError(f"{source} is missing or non-executable: {candidate}")

    def _parse_version(self, output: str) -> tuple[int, int, int] | None:
        for token in output.replace("(", " ").replace(")", " ").split():
            bits = token.strip().split(".")
            if len(bits) == 3 and all(bit.isdigit() for bit in bits):
                return int(bits[0]), int(bits[1]), int(bits[2])
        return None


class SupervisorOutcome(StrEnum):
    NO_ACTION = "no_action"
    NUDGED = "nudged"
    ALERT = "alert"


class RecoveryStatus(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class TaskProvenance(StrEnum):
    """Whether a public task summary can be attributed to the PM session."""

    UNRELATED = "unrelated"
    OWNER_TURN = "owner_turn"
    STAGE_CHILD = "stage_child"
    AMBIGUOUS = "ambiguous"


class CanonicalTaskStatus(StrEnum):
    """OpenClaw 2026.6.11 task-record statuses accepted by this supervisor."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    LOST = "lost"


class TaskReconciliationError(SupervisorError):
    """A task-list projection cannot be proven to match a canonical task record."""


@dataclass(frozen=True, slots=True)
class ReconciledRunningTasks:
    """Canonical running tasks and evidence that a projected task has ended."""

    running_tasks: tuple[Mapping[str, object], ...]
    terminal_task_seen: bool


def classify_autoresearch_task(task: Mapping[str, object]) -> TaskProvenance:
    """Classify the two exact OpenClaw 2026.6.11 task-list projections."""
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
    if (
        requester_key != AUTORESEARCH_OWNER_SESSION_KEY
        or owner_key != AUTORESEARCH_OWNER_SESSION_KEY
    ):
        if owns_one_key or task.get("agentId") == AUTORESEARCH_OWNER_AGENT_ID:
            return TaskProvenance.AMBIGUOUS
        return TaskProvenance.UNRELATED

    agent_id = task.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
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
    if provenance not in {TaskProvenance.OWNER_TURN, TaskProvenance.STAGE_CHILD}:
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
    )


def reconcile_relevant_running_tasks(
    rpc: OpenClawRPC,
    executable: Path,
    tasks: Sequence[Mapping[str, object]],
    *,
    shutdown_requested: ShutdownRequested = _shutdown_not_requested,
) -> ReconciledRunningTasks:
    """Resolve relevant task-list projections through canonical ``tasks show`` records."""
    running_tasks: list[Mapping[str, object]] = []
    terminal_task_seen = False
    terminal_statuses = {
        CanonicalTaskStatus.SUCCEEDED,
        CanonicalTaskStatus.FAILED,
        CanonicalTaskStatus.TIMED_OUT,
        CanonicalTaskStatus.CANCELLED,
        CanonicalTaskStatus.LOST,
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
            canonical_task = rpc.show_task(
                executable,
                task_id=task_id,
                shutdown_requested=shutdown_requested,
            )
        except SupervisorError as exc:
            raise TaskReconciliationError(
                f"task-show command failed during reconciliation: {task_id}"
            ) from exc
        if _task_id_for_reconciliation(canonical_task, source="task-show") != task_id:
            raise TaskReconciliationError("task-show taskId does not match task-list projection")
        if _task_provenance_fingerprint(canonical_task) != _task_provenance_fingerprint(
            projected_task
        ):
            raise TaskReconciliationError(
                "task-show provenance does not match task-list projection"
            )
        status_raw = canonical_task.get("status")
        if not isinstance(status_raw, str):
            raise TaskReconciliationError("task-show response is missing a string status")
        try:
            status = CanonicalTaskStatus(status_raw)
        except ValueError as exc:
            raise TaskReconciliationError(
                f"task-show response has unsupported status: {status_raw}"
            ) from exc
        if status is CanonicalTaskStatus.RUNNING:
            running_tasks.append(canonical_task)
        elif status in terminal_statuses:
            terminal_task_seen = True
        else:
            raise TaskReconciliationError(
                f"task-show response has non-terminal non-running status: {status.value}"
            )
    return ReconciledRunningTasks(tuple(running_tasks), terminal_task_seen)


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    state_path: Path = DEFAULT_STATE_PATH
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH
    autoresearch_dir: Path = DEFAULT_AUTORESEARCH_DIR
    owner_sessions_path: Path = DEFAULT_OWNER_SESSIONS_PATH
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS
    default_openclaw_bin: Path = DEFAULT_OPENCLAW_BIN
    target_repo: Path = DEFAULT_QUANTIPY_ROOT
    proc_root: Path = Path("/proc")
    claim_stale_seconds: float = DEFAULT_CLAIM_STALE_SECONDS
    expected_stage_task_stale_seconds: float = DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS
    max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS

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
class RecoveryClaim:
    recovery_key: str
    token: str


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
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.config = config or SupervisorConfig()
        self._now = now
        self._sleep = sleep
        self._rpc = OpenClawRPC(self.config.default_openclaw_bin, run_command=run_command)

    def run_once(
        self, *, shutdown_requested: ShutdownRequested = _shutdown_not_requested
    ) -> SupervisorResult:
        executable = self._rpc.require_binary(shutdown_requested=shutdown_requested)
        state = self._load_state()
        if self._is_terminal_state(state):
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "terminal_state")
        try:
            reconciled_tasks = self._reconciled_running_tasks(
                executable, shutdown_requested=shutdown_requested
            )
        except TaskReconciliationError:
            return SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")
        activity = self._activity_guard(state, reconciled_tasks)
        if activity is not None:
            return activity
        probe = self._build_state_probe(state)
        if self._now() - probe.latest_update_ts < self.config.grace_period_seconds:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "state_not_stale")
        try:
            reconciled_tasks = self._reconciled_running_tasks(
                executable, shutdown_requested=shutdown_requested
            )
        except TaskReconciliationError:
            return SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")
        activity = self._activity_guard(state, reconciled_tasks)
        if activity is not None:
            return activity
        writers = self._active_target_repo_writer_processes(state)
        if writers:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "target_repo_writer_active")
        recovery_key = f"{state.iteration}:{state.phase.value}:{probe.fingerprint}"
        claim_or_result = self._claim_recovery(state, recovery_key)
        if isinstance(claim_or_result, SupervisorResult):
            return claim_or_result
        claim = claim_or_result
        detected_error = self._detect_owner_error()
        try:
            self._rpc.wake(
                executable,
                message=RECOVERY_MESSAGE,
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
        reason = (
            "owner_session_error_recovery"
            if detected_error is not None
            else "recovery_message_sent"
        )
        _structured_log(
            logging.WARNING, "supervisor.nudged", recovery_key=recovery_key, reason=reason
        )
        return SupervisorResult(SupervisorOutcome.NUDGED, reason, recovery_key, sent_wake=True)

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
            return normalize_autoresearch_state(AutoresearchState.from_dict(raw))
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

    def _is_terminal_state(self, state: AutoresearchState) -> bool:
        decision = state.final_decision
        return (
            state.phase is Phase.REPEAT
            and decision is not None
            and not decision.continue_loop
            and (not decision.memory_write_required or state.memory_written)
        )

    def _running_tasks(
        self, executable: Path, *, shutdown_requested: ShutdownRequested
    ) -> list[dict[str, object]]:
        payload = self._rpc.list_running_tasks(
            executable,
            shutdown_requested=shutdown_requested,
        )
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, str | bytes):
            raise SupervisorError("OpenClaw tasks JSON missing tasks array")
        if not all(isinstance(task, Mapping) for task in raw_tasks):
            raise SupervisorError("OpenClaw tasks JSON contains a non-object task")
        return [dict(task) for task in raw_tasks if isinstance(task, Mapping)]

    def _reconciled_running_tasks(
        self, executable: Path, *, shutdown_requested: ShutdownRequested
    ) -> ReconciledRunningTasks:
        return reconcile_relevant_running_tasks(
            self._rpc,
            executable,
            self._running_tasks(executable, shutdown_requested=shutdown_requested),
            shutdown_requested=shutdown_requested,
        )

    def _expected_stage_agent_ids(self, state: AutoresearchState) -> tuple[str, ...]:
        if state.phase is Phase.SETUP_CONTEXT:
            return (AUTORESEARCH_OWNER_AGENT_ID,) if state.setup is None else ("context-curator",)
        return EXPECTED_STAGE_AGENT_IDS[state.phase]

    def _activity_guard(
        self, state: AutoresearchState, reconciled_tasks: ReconciledRunningTasks
    ) -> SupervisorResult | None:
        running_tasks = reconciled_tasks.running_tasks
        expected = [
            task
            for task in running_tasks
            if task.get("agentId") in self._expected_stage_agent_ids(state)
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
        if not reconciled_tasks.terminal_task_seen:
            lifecycle_result = self._owner_lifecycle_guard(state)
            if lifecycle_result is not None:
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
        store = self._load_owner_session_store()
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
        }

    def _claim_recovery(
        self, state: AutoresearchState, recovery_key: str
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
                return self._alert(checkpoint, record, recovery_key, "recovery_attempts_exhausted")
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

    def _detect_owner_error(self) -> str | None:
        lifecycle = self._load_owner_session_store().get(AUTORESEARCH_OWNER_SESSION_KEY)
        if not isinstance(lifecycle, Mapping):
            return None
        haystacks = [json.dumps(lifecycle, sort_keys=True)]
        session_file = lifecycle.get("sessionFile")
        if isinstance(session_file, str):
            haystacks.append(self._tail_text(Path(session_file), bytes_limit=128_000))
        for haystack in haystacks:
            lowered = haystack.lower()
            for pattern in RECOVERY_ERROR_PATTERNS:
                if pattern in lowered:
                    return pattern
        return None

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
        paths = [self.config.state_path, *self._git_marker_paths(self._target_repo_root(state))]
        if self.config.autoresearch_dir.exists():
            prefix = f"iteration-{state.iteration}-"
            paths.extend(
                path
                for path in self.config.autoresearch_dir.iterdir()
                if path.is_file()
                and (path.name.startswith(prefix) or path.name == "current-next.json")
            )
        latest = 0.0
        latest_path = self.config.state_path
        parts: list[str] = []
        for path in paths:
            try:
                metadata = path.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SupervisorError(f"failed to stat supervision path {path}: {exc}") from exc
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
    values: list[int] = []
    for field_name in ("updatedAt", "lastInteractionAt", "startedAt"):
        value = lifecycle.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SupervisorError(f"running owner lifecycle entry is missing integer {field_name}")
        values.append(value)
    return max(values)


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
    try:
        if args.once:
            supervisor.run_once()
            return 0
        return supervisor.run_forever()
    except SupervisorError as exc:
        _structured_log(logging.ERROR, "supervisor.failure", detail=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
