"""Deterministic infrastructure-only supervisor for Quantipy autoresearch."""

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
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from gateway.autoresearch_runner import (
    DEFAULT_QUANTIPY_ROOT,
    AutoresearchState,
    AutoresearchValidationError,
    Phase,
)

logger = logging.getLogger(__name__)

DEFAULT_AUTORESEARCH_DIR = Path.home() / ".openclaw" / "autoresearch"
DEFAULT_STATE_PATH = DEFAULT_AUTORESEARCH_DIR / "quantipy-state.json"
DEFAULT_CHECKPOINT_PATH = DEFAULT_AUTORESEARCH_DIR / "supervisor-state.json"
DEFAULT_MAIN_SESSIONS_PATH = (
    Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
)
DEFAULT_OPENCLAW_BIN = Path.home() / ".local" / "share" / "pnpm" / "openclaw"
DEFAULT_DEV_API_BASE = "http://127.0.0.1:5173"
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_GRACE_PERIOD_SECONDS = 120.0
DEFAULT_CLAIM_STALE_SECONDS = 300.0
DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS = 300.0
DEFAULT_MAX_RECOVERY_ATTEMPTS = 2
REQUIRED_OPENCLAW_VERSION = (2026, 6, 11)
RECOVERY_MESSAGE = (
    "continue from authoritative state; run deterministic runner; "
    "infrastructure recovery only; no research steering/fallback"
)
RELEVANT_AGENT_IDS = frozenset(
    {
        "main",
        "context-curator",
        "debater-microstructure",
        "debater-data",
        "debater-skeptic",
        "debater-theory",
        "debater-implementation",
        "consensus-arbiter",
        "implementer",
        "reviewer",
        "fixer",
    }
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
MAIN_G2_SESSION_KEY = "agent:main:g2"
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
    Phase.VERIFICATION: ("main",),
    Phase.REVIEW: ("reviewer",),
    Phase.FIX_TEST: ("fixer",),
    Phase.DECISION_LOG: ("main",),
    Phase.REPEAT: (),
}
TARGET_WRITER_COMMAND_RE = re.compile(
    r"(\bpytest\b|\bpy\.test\b|\bjupyter\b|\bpapermill\b|\bipython\b|"
    r"\bnbconvert\b|\bgenerate_[\w.-]*|notebooks/experiments|"
    r"src/quantipy/alpha|scripts/experiments|tools/experiments)"
)


def _is_main_g2_session_key(key: object) -> bool:
    return isinstance(key, str) and (
        key == MAIN_G2_SESSION_KEY or key.startswith(f"{MAIN_G2_SESSION_KEY}:")
    )


class SupervisorError(RuntimeError):
    """Base failure for strict autoresearch supervision."""


class OpenClawResolutionError(SupervisorError):
    """Raised when the OpenClaw binary cannot be resolved safely."""


class OpenClawVersionError(SupervisorError):
    """Raised when the OpenClaw binary is not the repo-supported version."""


class DevAPIError(SupervisorError):
    """Raised when the G2 Dev API is unavailable or returns an error."""


class WorkspaceEvidenceError(SupervisorError):
    """Raised when implementation workspace evidence cannot be trusted."""


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


class SupervisorOutcome(StrEnum):
    NO_ACTION = "no_action"
    NUDGED = "nudged"
    ALERT = "alert"


class RecoveryStatus(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Runtime configuration for the autoresearch supervisor."""

    state_path: Path = DEFAULT_STATE_PATH
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH
    autoresearch_dir: Path = DEFAULT_AUTORESEARCH_DIR
    main_sessions_path: Path = DEFAULT_MAIN_SESSIONS_PATH
    dev_api_base: str = DEFAULT_DEV_API_BASE
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS
    default_openclaw_bin: Path = DEFAULT_OPENCLAW_BIN
    target_repo: Path = DEFAULT_QUANTIPY_ROOT
    proc_root: Path = Path("/proc")
    claim_stale_seconds: float = DEFAULT_CLAIM_STALE_SECONDS
    expected_stage_task_stale_seconds: float = DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS
    max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS
    dev_api_timeout_seconds: float = 10.0
    menu_wait_seconds: float = 5.0
    session_ready_wait_seconds: float = 10.0

    def __post_init__(self) -> None:
        _require_finite_positive(
            self.grace_period_seconds,
            field_name="grace_period_seconds",
        )
        _require_finite_positive(
            self.expected_stage_task_stale_seconds,
            field_name="expected_stage_task_stale_seconds",
        )


@dataclass(frozen=True, slots=True)
class StateProbe:
    """Current persisted-state fingerprint and most recent related update."""

    fingerprint: str
    latest_update_ts: float
    latest_update_path: Path


@dataclass(frozen=True, slots=True)
class G2Snapshot:
    """Current G2 app state plus any detected fatal session text."""

    state: str
    display_text: str
    detected_error_text: str | None


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    """One-shot supervision result."""

    outcome: SupervisorOutcome
    reason: str
    recovery_key: str | None = None
    rotated_session: bool = False
    sent_nudge: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    """Exclusive durable claim for one recovery replay."""

    recovery_key: str
    token: str
    rotation_reasons: tuple[str, ...]
    rotated_before: bool


@dataclass(slots=True)
class RecoveryRecord:
    """Checkpointed dedupe state for one iteration/phase/fingerprint."""

    status: RecoveryStatus = RecoveryStatus.READY
    attempt_count: int = 0
    claim_token: str | None = None
    claim_pid: int | None = None
    claim_process_identity: str | None = None
    claim_started_at: float | None = None
    rotated: bool = False
    alerted: bool = False
    rotated_at: float | None = None
    nudged_at: float | None = None
    failed_at: float | None = None
    last_error: str | None = None
    alert_at: float | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RecoveryRecord:
        status_raw = raw.get("status")
        if isinstance(status_raw, str):
            try:
                status = RecoveryStatus(status_raw)
            except ValueError as exc:
                raise SupervisorError(f"invalid recovery status: {status_raw!r}") from exc
        elif bool(raw.get("nudged", False)):
            status = RecoveryStatus.SUCCEEDED
        elif bool(raw.get("replay_attempted", False)):
            status = RecoveryStatus.FAILED
        else:
            status = RecoveryStatus.READY
        return cls(
            status=status,
            attempt_count=_nonnegative_int(
                raw.get("attempt_count"),
                default=int(status is not RecoveryStatus.READY),
            ),
            claim_token=_optional_str(raw.get("claim_token")),
            claim_pid=_optional_int(raw.get("claim_pid")),
            claim_process_identity=_optional_str(raw.get("claim_process_identity")),
            claim_started_at=_optional_float(raw.get("claim_started_at")),
            rotated=bool(raw.get("rotated", False)),
            alerted=bool(raw.get("alerted", False)),
            rotated_at=_optional_float(raw.get("rotated_at")),
            nudged_at=_optional_float(raw.get("nudged_at")),
            failed_at=_optional_float(raw.get("failed_at")),
            last_error=_optional_str(raw.get("last_error")),
            alert_at=_optional_float(raw.get("alert_at")),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SupervisorCheckpoint:
    """Persisted replay and iteration-boundary rotation state."""

    setup_iteration_rotations: dict[str, float] = field(default_factory=dict)
    recovery_records: dict[str, RecoveryRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SupervisorCheckpoint:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            raise SupervisorError(f"failed to read supervisor checkpoint {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SupervisorError(f"invalid supervisor checkpoint JSON: {path}") from exc
        if not isinstance(raw, Mapping):
            raise SupervisorError(f"invalid supervisor checkpoint payload: {path}")
        rotations_raw = raw.get("setup_iteration_rotations", {})
        if not isinstance(rotations_raw, Mapping):
            raise SupervisorError("setup_iteration_rotations must be an object")
        records_raw = raw.get("recovery_records", {})
        if not isinstance(records_raw, Mapping):
            raise SupervisorError("recovery_records must be an object")
        checkpoint = cls()
        for key, value in rotations_raw.items():
            if not isinstance(key, str):
                raise SupervisorError("setup iteration rotation keys must be strings")
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SupervisorError("setup iteration rotation timestamps must be numeric")
            checkpoint.setup_iteration_rotations[key] = float(value)
        for key, value in records_raw.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                raise SupervisorError("recovery_records entries must be keyed objects")
            checkpoint.recovery_records[key] = RecoveryRecord.from_dict(value)
        return checkpoint

    def save(self, path: Path) -> None:
        payload = {
            "setup_iteration_rotations": self.setup_iteration_rotations,
            "recovery_records": {
                key: record.to_dict() for key, record in self.recovery_records.items()
            },
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise SupervisorError(
                f"failed to atomically save supervisor checkpoint {path}: {exc}"
            ) from exc
        finally:
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _nonnegative_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SupervisorError("recovery attempt_count must be a non-negative integer")
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _structured_log(level: int, event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, sort_keys=True, default=str))


class AutoresearchSupervisor:
    """Monitors Quantipy autoresearch and nudges only for deterministic recovery."""

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.config = config or SupervisorConfig()
        self._now = now
        self._sleep = sleep
        self._run_command = run_command
        self._urlopen = urlopen

    def run_once(self) -> SupervisorResult:
        """Execute one deterministic supervision check."""
        openclaw_bin = self._require_openclaw_binary()
        state = self._load_state()
        if self._is_terminal_state(state):
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="terminal_state",
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="terminal_state",
            )

        grace_seconds = self.config.grace_period_seconds
        running_tasks = self._running_tasks(openclaw_bin)
        activity_result = self._activity_guard(
            state,
            running_tasks,
            openclaw_bin,
            grace_seconds,
        )
        if activity_result is not None:
            return activity_result

        state_probe = self._build_state_probe(state)
        idle_seconds = max(0.0, self._now() - state_probe.latest_update_ts)
        if idle_seconds < grace_seconds:
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="state_not_stale",
                idle_seconds=round(idle_seconds, 3),
                latest_update_path=str(state_probe.latest_update_path),
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="state_not_stale",
            )

        g2_snapshot = self._read_g2_snapshot()
        recovery_key = f"{state.iteration}:{state.phase.value}:{state_probe.fingerprint}"
        single_flight_result = self._single_flight_guard(
            openclaw_bin,
            state,
            grace_seconds,
        )
        if single_flight_result is not None:
            return single_flight_result

        claim_or_result = self._claim_recovery(state, g2_snapshot, recovery_key)
        if isinstance(claim_or_result, SupervisorResult):
            return claim_or_result
        claim = claim_or_result

        rotated = False
        try:
            if claim.rotation_reasons:
                self._rotate_g2_session(g2_snapshot.state)
                self._record_claim_rotation(claim, state)
                rotated = True
                _structured_log(
                    logging.WARNING,
                    "supervisor.session_rotated",
                    reasons=claim.rotation_reasons,
                    recovery_key=recovery_key,
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
            self._send_recovery_message()
        except BaseException as exc:
            self._fail_recovery_claim(claim, exc)
            raise
        self._complete_recovery_claim(claim)
        _structured_log(
            logging.WARNING,
            "supervisor.nudged",
            recovery_key=recovery_key,
            rotated_session=rotated or claim.rotated_before,
            phase=state.phase.value,
            iteration=state.iteration,
            detected_error_text=g2_snapshot.detected_error_text,
        )
        return SupervisorResult(
            outcome=SupervisorOutcome.NUDGED,
            reason="recovery_message_sent",
            recovery_key=recovery_key,
            rotated_session=rotated or claim.rotated_before,
            sent_nudge=True,
        )

    def run_forever(self) -> int:
        """Run the daemon loop until signaled or a strict failure occurs."""
        stop_requested = False

        def _request_stop(signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True
            _structured_log(logging.INFO, "supervisor.signal", signum=signum)

        previous_int = signal.signal(signal.SIGINT, _request_stop)
        previous_term = signal.signal(signal.SIGTERM, _request_stop)
        try:
            while not stop_requested:
                self.run_once()
                deadline = self._now() + self.config.poll_interval_seconds
                while not stop_requested and self._now() < deadline:
                    self._sleep(min(0.5, max(0.0, deadline - self._now())))
            _structured_log(logging.INFO, "supervisor.shutdown")
            return 0
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)

    def _require_openclaw_binary(self) -> Path:
        executable = self._resolve_openclaw_executable()
        try:
            result = self._run_command(
                [str(executable), "--version"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise OpenClawResolutionError(
                f"failed to execute OpenClaw at {executable}: {exc}"
            ) from exc
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode != 0:
            raise OpenClawVersionError(
                f"OpenClaw version check failed for {executable}: {output or 'no output'}"
            )
        version = self._parse_openclaw_version(output)
        if version is None:
            raise OpenClawVersionError(
                f"could not parse OpenClaw version from {executable}: {output or 'no output'}"
            )
        if version != REQUIRED_OPENCLAW_VERSION:
            required = ".".join(str(part) for part in REQUIRED_OPENCLAW_VERSION)
            relation = "too old" if version < REQUIRED_OPENCLAW_VERSION else "too new"
            raise OpenClawVersionError(
                f"OpenClaw {version[0]}.{version[1]}.{version[2]} at {executable} is "
                f"{relation}; need exactly {required}."
            )
        return executable

    def _resolve_openclaw_executable(self) -> Path:
        override = os.environ.get("OPENCLAW_BIN")
        if override:
            candidate = Path(override).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
            raise OpenClawResolutionError(
                f"OPENCLAW_BIN points to a missing or non-executable path: {candidate}"
            )
        candidate = self.config.default_openclaw_bin
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise OpenClawResolutionError(
            "OpenClaw executable not found. Checked only the explicit OPENCLAW_BIN override "
            f"and the user pnpm path {candidate}."
        )

    def _parse_openclaw_version(self, output: str) -> tuple[int, int, int] | None:
        parts: list[int] = []
        for token in output.replace("(", " ").replace(")", " ").split():
            raw = token.strip()
            bits = raw.split(".")
            if len(bits) != 3:
                continue
            if all(bit.isdigit() for bit in bits):
                parts = [int(bit) for bit in bits]
                break
        if len(parts) != 3:
            return None
        return parts[0], parts[1], parts[2]

    def _load_state(self) -> AutoresearchState:
        try:
            raw_text = self.config.state_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SupervisorError(
                f"missing autoresearch state file: {self.config.state_path}"
            ) from exc
        except OSError as exc:
            raise SupervisorError(
                f"failed to read autoresearch state file: {self.config.state_path}"
            ) from exc
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SupervisorError(
                f"invalid autoresearch state JSON: {self.config.state_path}"
            ) from exc
        try:
            return AutoresearchState.from_dict(raw)
        except AutoresearchValidationError as exc:
            raise SupervisorError(f"invalid autoresearch state: {exc}") from exc

    def _is_terminal_state(self, state: AutoresearchState) -> bool:
        decision = state.final_decision
        if state.phase is not Phase.REPEAT or decision is None:
            return False
        if decision.continue_loop:
            return False
        return not decision.memory_write_required or state.memory_written

    def _running_tasks(
        self,
        openclaw_bin: Path,
    ) -> list[dict[str, object]]:
        payload = self._run_openclaw_json(
            openclaw_bin,
            ["tasks", "list", "--status", "running", "--json"],
        )
        tasks_raw = payload.get("tasks")
        if not isinstance(tasks_raw, Sequence) or isinstance(tasks_raw, str | bytes):
            raise SupervisorError("OpenClaw tasks JSON missing tasks array")
        tasks: list[dict[str, object]] = []
        for task in tasks_raw:
            if isinstance(task, Mapping):
                tasks.append(dict(task))
        return tasks

    def _fresh_relevant_tasks(
        self,
        tasks: Sequence[Mapping[str, object]],
        grace_seconds: float,
    ) -> list[dict[str, object]]:
        fresh: list[dict[str, object]] = []
        now_ms = int(self._now() * 1000)
        grace_ms = int(grace_seconds * 1000)
        for task in tasks:
            if not self._is_relevant_task(task):
                continue
            last_event_at = _task_last_event_ms(task)
            if last_event_at is None:
                continue
            if now_ms - last_event_at <= grace_ms:
                fresh.append(dict(task))
        return fresh

    def _active_expected_stage_tasks(
        self,
        state: AutoresearchState,
        tasks: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        expected_agent_ids = self._expected_stage_agent_ids(state)
        if not expected_agent_ids:
            return []
        return [
            dict(task)
            for task in tasks
            if task.get("agentId") in expected_agent_ids and self._is_relevant_task(task)
        ]

    def _expected_stage_agent_ids(self, state: AutoresearchState) -> tuple[str, ...]:
        if state.phase is Phase.SETUP_CONTEXT:
            return ("main",) if state.setup is None else ("context-curator",)
        return EXPECTED_STAGE_AGENT_IDS[state.phase]

    def _single_flight_guard(
        self,
        openclaw_bin: Path,
        state: AutoresearchState,
        grace_seconds: float,
    ) -> SupervisorResult | None:
        running_tasks = self._running_tasks(openclaw_bin)
        activity_result = self._activity_guard(
            state,
            running_tasks,
            openclaw_bin,
            grace_seconds,
        )
        if activity_result is not None:
            return activity_result

        writers = self._active_target_repo_writer_processes(state)
        if not writers:
            return None
        _structured_log(
            logging.WARNING,
            "supervisor.no_action",
            reason="target_repo_writer_active",
            count=len(writers),
            processes=writers,
            phase=state.phase.value,
            iteration=state.iteration,
        )
        return SupervisorResult(
            outcome=SupervisorOutcome.NO_ACTION,
            reason="target_repo_writer_active",
        )

    def _claim_recovery(
        self,
        state: AutoresearchState,
        g2_snapshot: G2Snapshot,
        recovery_key: str,
    ) -> RecoveryClaim | SupervisorResult:
        if self.config.claim_stale_seconds <= 0:
            raise SupervisorError("claim_stale_seconds must be positive")
        if self.config.max_recovery_attempts < 1:
            raise SupervisorError("max_recovery_attempts must be at least one")

        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = checkpoint.recovery_records.setdefault(recovery_key, RecoveryRecord())
            if record.status is RecoveryStatus.SUCCEEDED:
                nudged_at = record.nudged_at
                if nudged_at is None:
                    raise SupervisorError(
                        f"succeeded recovery is missing nudged_at: {recovery_key}"
                    )
                if not math.isfinite(nudged_at):
                    raise SupervisorError(
                        f"succeeded recovery has invalid nudged_at: {recovery_key}"
                    )
                elapsed_seconds = self._now() - nudged_at
                if not math.isfinite(elapsed_seconds):
                    raise SupervisorError(
                        f"succeeded recovery has invalid elapsed time: {recovery_key}"
                    )
                if elapsed_seconds < 0:
                    raise SupervisorError(
                        f"succeeded recovery has future nudged_at: {recovery_key}"
                    )
                if elapsed_seconds < self.config.grace_period_seconds:
                    _structured_log(
                        logging.INFO,
                        "supervisor.no_action",
                        reason="recovery_settling",
                        recovery_key=recovery_key,
                        elapsed_seconds=round(elapsed_seconds, 3),
                        phase=state.phase.value,
                        iteration=state.iteration,
                    )
                    return SupervisorResult(
                        outcome=SupervisorOutcome.NO_ACTION,
                        reason="recovery_settling",
                        recovery_key=recovery_key,
                    )

            retrying_unobserved_success = record.status is RecoveryStatus.SUCCEEDED

            if record.status is RecoveryStatus.IN_FLIGHT:
                if record.claim_started_at is None or record.claim_pid is None:
                    raise SupervisorError(
                        f"in-flight recovery claim is missing owner metadata: {recovery_key}"
                    )
                claim_age = max(0.0, self._now() - record.claim_started_at)
                if claim_age < self.config.claim_stale_seconds:
                    _structured_log(
                        logging.INFO,
                        "supervisor.no_action",
                        reason="recovery_in_flight",
                        recovery_key=recovery_key,
                        claim_age_seconds=round(claim_age, 3),
                        claim_pid=record.claim_pid,
                        phase=state.phase.value,
                        iteration=state.iteration,
                    )
                    return SupervisorResult(
                        outcome=SupervisorOutcome.NO_ACTION,
                        reason="recovery_in_flight",
                        recovery_key=recovery_key,
                    )
                if self._claim_owner_alive(record):
                    return self._checkpoint_alert_result(
                        checkpoint,
                        record,
                        state,
                        recovery_key,
                        reason="stale_recovery_claim_owner_alive",
                    )
                _structured_log(
                    logging.WARNING,
                    "supervisor.reclaiming_stale_claim",
                    recovery_key=recovery_key,
                    claim_age_seconds=round(claim_age, 3),
                    previous_claim_pid=record.claim_pid,
                    phase=state.phase.value,
                    iteration=state.iteration,
                )

            if record.attempt_count >= self.config.max_recovery_attempts:
                return self._checkpoint_alert_result(
                    checkpoint,
                    record,
                    state,
                    recovery_key,
                    reason="recovery_attempts_exhausted",
                )

            attempt = record.attempt_count + 1
            claim_pid = os.getpid()
            claim_identity = self._process_identity(claim_pid)
            claimed_at = self._now()
            token = f"{claim_pid}:{claim_identity or 'unknown'}:{attempt}:{int(claimed_at * 1e9)}"
            record.status = RecoveryStatus.IN_FLIGHT
            record.attempt_count = attempt
            record.claim_token = token
            record.claim_pid = claim_pid
            record.claim_process_identity = claim_identity
            record.claim_started_at = claimed_at
            record.failed_at = None
            record.last_error = None
            record.alerted = False
            record.alert_at = None
            rotation_reasons: tuple[str, ...] = ()
            if not record.rotated:
                reasons = self._rotation_reasons(state, g2_snapshot, checkpoint)
                if retrying_unobserved_success:
                    reasons.append("unobserved_recovery_progress")
                rotation_reasons = tuple(reasons)
            checkpoint.save(self.config.checkpoint_path)
            _structured_log(
                logging.INFO,
                "supervisor.recovery_claimed",
                recovery_key=recovery_key,
                attempt=attempt,
                claim_pid=claim_pid,
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return RecoveryClaim(
                recovery_key=recovery_key,
                token=token,
                rotation_reasons=rotation_reasons,
                rotated_before=record.rotated,
            )

    def _record_claim_rotation(
        self,
        claim: RecoveryClaim,
        state: AutoresearchState,
    ) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._require_owned_claim(checkpoint, claim)
            record.rotated = True
            record.rotated_at = self._now()
            if state.phase is Phase.SETUP_CONTEXT:
                checkpoint.setup_iteration_rotations[str(state.iteration)] = record.rotated_at
            checkpoint.save(self.config.checkpoint_path)

    def _complete_recovery_claim(self, claim: RecoveryClaim) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._require_owned_claim(checkpoint, claim)
            record.status = RecoveryStatus.SUCCEEDED
            record.nudged_at = self._now()
            record.failed_at = None
            record.last_error = None
            checkpoint.save(self.config.checkpoint_path)

    def _fail_recovery_claim(
        self,
        claim: RecoveryClaim,
        error: BaseException,
    ) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._require_owned_claim(checkpoint, claim)
            record.status = RecoveryStatus.FAILED
            record.failed_at = self._now()
            record.last_error = f"{type(error).__name__}: {error}"[:1000]
            checkpoint.save(self.config.checkpoint_path)
        _structured_log(
            logging.ERROR,
            "supervisor.recovery_failed",
            recovery_key=claim.recovery_key,
            error_type=type(error).__name__,
            detail=str(error),
        )

    def _require_owned_claim(
        self,
        checkpoint: SupervisorCheckpoint,
        claim: RecoveryClaim,
    ) -> RecoveryRecord:
        record = checkpoint.recovery_records.get(claim.recovery_key)
        if record is None:
            raise SupervisorError(f"recovery claim disappeared: {claim.recovery_key}")
        if record.status is not RecoveryStatus.IN_FLIGHT or record.claim_token != claim.token:
            raise SupervisorError(
                f"recovery claim ownership changed unexpectedly: {claim.recovery_key}"
            )
        return record

    def _checkpoint_alert_result(
        self,
        checkpoint: SupervisorCheckpoint,
        record: RecoveryRecord,
        state: AutoresearchState,
        recovery_key: str,
        *,
        reason: str,
    ) -> SupervisorResult:
        if record.alerted:
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="alert_already_emitted",
                prior_reason=reason,
                recovery_key=recovery_key,
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="alert_already_emitted",
                recovery_key=recovery_key,
            )
        record.alerted = True
        record.alert_at = self._now()
        checkpoint.save(self.config.checkpoint_path)
        _structured_log(
            logging.ERROR,
            "supervisor.alert",
            reason=reason,
            recovery_key=recovery_key,
            phase=state.phase.value,
            iteration=state.iteration,
        )
        return SupervisorResult(
            outcome=SupervisorOutcome.ALERT,
            reason=reason,
            recovery_key=recovery_key,
        )

    @contextmanager
    def _checkpoint_lock(self) -> Iterator[None]:
        checkpoint_path = self.config.checkpoint_path
        lock_path = checkpoint_path.with_name(f"{checkpoint_path.name}.lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise SupervisorError(
                f"failed to lock supervisor checkpoint {lock_path}: {exc}"
            ) from exc

    def _claim_owner_alive(self, record: RecoveryRecord) -> bool:
        claim_pid = record.claim_pid
        if claim_pid is None or claim_pid <= 0:
            raise SupervisorError("recovery claim has an invalid owner pid")
        if claim_pid == os.getpid():
            return True
        current_identity = self._process_identity(claim_pid)
        if current_identity is None:
            return False
        if record.claim_process_identity is None:
            return True
        return current_identity == record.claim_process_identity

    def _process_identity(self, pid: int) -> str | None:
        stat_path = self.config.proc_root / str(pid) / "stat"
        try:
            raw = stat_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SupervisorError(
                f"failed to inspect process identity for pid {pid}: {exc}"
            ) from exc
        command_end = raw.rfind(")")
        if command_end < 0:
            raise SupervisorError(f"malformed process stat for pid {pid}")
        fields = raw[command_end + 1 :].split()
        if len(fields) <= 19:
            raise SupervisorError(f"process stat missing start time for pid {pid}")
        return fields[19]

    def _activity_guard(
        self,
        state: AutoresearchState,
        running_tasks: Sequence[Mapping[str, object]],
        openclaw_bin: Path,
        grace_seconds: float,
    ) -> SupervisorResult | None:
        expected_tasks = self._active_expected_stage_tasks(state, running_tasks)
        if expected_tasks:
            now_ms = int(self._now() * 1000)
            stale_ms = int(self.config.expected_stage_task_stale_seconds * 1000)
            stale_tasks = [
                task
                for task in expected_tasks
                if (last_event_at := _expected_task_last_event_ms(task)) is None
                or now_ms - last_event_at > stale_ms
            ]
            if stale_tasks:
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="stale_expected_stage_task",
                    count=len(stale_tasks),
                    expected_agent_ids=self._expected_stage_agent_ids(state),
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="stale_expected_stage_task",
                )
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="active_expected_stage_task",
                count=len(expected_tasks),
                expected_agent_ids=self._expected_stage_agent_ids(state),
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="active_expected_stage_task",
            )

        expected_main_session_result = self._expected_main_session_guard(state)
        if expected_main_session_result is not None:
            return expected_main_session_result

        fresh_sessions = self._fresh_main_g2_sessions(openclaw_bin, grace_seconds)
        if len(fresh_sessions) > 1:
            session_keys = [session.get("key") for session in fresh_sessions]
            _structured_log(
                logging.ERROR,
                "supervisor.alert",
                reason="multiple_fresh_main_g2_sessions",
                count=len(fresh_sessions),
                session_keys=session_keys,
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.ALERT,
                reason="multiple_fresh_main_g2_sessions",
            )
        if fresh_sessions:
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="fresh_main_g2_session",
                count=1,
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="fresh_main_g2_session",
            )

        fresh_tasks = self._fresh_relevant_tasks(running_tasks, grace_seconds)
        if fresh_tasks:
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="fresh_relevant_task",
                count=len(fresh_tasks),
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="fresh_relevant_task",
            )
        return None

    def _expected_main_session_guard(
        self,
        state: AutoresearchState,
    ) -> SupervisorResult | None:
        if self._expected_stage_agent_ids(state) != ("main",):
            return None
        store = self._load_main_session_store_payload()

        fresh_running_session_keys: list[str] = []
        stale_running_session_keys: list[str] = []
        now_ms = int(self._now() * 1000)
        stale_ms = int(self.config.expected_stage_task_stale_seconds * 1000)
        for key, value in store.items():
            if not _is_main_g2_session_key(key):
                continue
            if not isinstance(value, Mapping):
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="invalid_expected_main_session_store",
                    session_key=key,
                    detail="main lifecycle entry must be an object",
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="invalid_expected_main_session_store",
                )
            status = value.get("status")
            if not isinstance(status, str):
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="invalid_expected_main_session_store",
                    session_key=key,
                    detail="main lifecycle entry is missing a string status",
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="invalid_expected_main_session_store",
                )
            if status != "running":
                continue
            if value.get("endedAt") is not None or value.get("abortedLastRun") is True:
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="contradictory_running_expected_main_session",
                    session_key=key,
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="contradictory_running_expected_main_session",
                )
            try:
                last_event_at = _running_expected_main_session_last_event_ms(value)
            except SupervisorError as exc:
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="invalid_expected_main_session_store",
                    session_key=key,
                    detail=str(exc),
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="invalid_expected_main_session_store",
                )
            if last_event_at > now_ms:
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="contradictory_running_expected_main_session",
                    session_key=key,
                    detail="running main lifecycle timestamp is in the future",
                    last_event_at=last_event_at,
                    now_ms=now_ms,
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="contradictory_running_expected_main_session",
                )
            if now_ms - last_event_at > stale_ms:
                stale_running_session_keys.append(key)
            else:
                fresh_running_session_keys.append(key)

        if len(stale_running_session_keys) == 1 and not fresh_running_session_keys:
            try:
                writers = self._active_target_repo_writer_processes(state)
            except WorkspaceEvidenceError as exc:
                _structured_log(
                    logging.ERROR,
                    "supervisor.alert",
                    reason="invalid_expected_main_workspace",
                    detail=str(exc),
                    session_key=stale_running_session_keys[0],
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.ALERT,
                    reason="invalid_expected_main_workspace",
                )
            if writers:
                _structured_log(
                    logging.INFO,
                    "supervisor.no_action",
                    reason="active_expected_main_process",
                    session_key=stale_running_session_keys[0],
                    count=len(writers),
                    processes=writers,
                    phase=state.phase.value,
                    iteration=state.iteration,
                )
                return SupervisorResult(
                    outcome=SupervisorOutcome.NO_ACTION,
                    reason="active_expected_main_process",
                )

        if stale_running_session_keys:
            _structured_log(
                logging.ERROR,
                "supervisor.alert",
                reason="stale_running_expected_main_session",
                count=len(stale_running_session_keys),
                session_keys=stale_running_session_keys,
                lease_seconds=self.config.expected_stage_task_stale_seconds,
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.ALERT,
                reason="stale_running_expected_main_session",
            )
        if len(fresh_running_session_keys) > 1:
            _structured_log(
                logging.ERROR,
                "supervisor.alert",
                reason="multiple_running_expected_main_sessions",
                count=len(fresh_running_session_keys),
                session_keys=fresh_running_session_keys,
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.ALERT,
                reason="multiple_running_expected_main_sessions",
            )
        if fresh_running_session_keys:
            _structured_log(
                logging.INFO,
                "supervisor.no_action",
                reason="active_expected_main_session",
                session_key=fresh_running_session_keys[0],
                phase=state.phase.value,
                iteration=state.iteration,
            )
            return SupervisorResult(
                outcome=SupervisorOutcome.NO_ACTION,
                reason="active_expected_main_session",
            )
        return None

    def _fresh_main_g2_sessions(
        self,
        openclaw_bin: Path,
        grace_seconds: float,
    ) -> list[dict[str, object]]:
        active_minutes = max(1, math.ceil(grace_seconds / 60.0))
        payload = self._run_openclaw_json(
            openclaw_bin,
            ["sessions", "--agent", "main", "--active", str(active_minutes), "--json"],
        )
        sessions_raw = payload.get("sessions")
        if not isinstance(sessions_raw, Sequence) or isinstance(sessions_raw, str | bytes):
            raise SupervisorError("OpenClaw sessions JSON missing sessions array")
        fresh_cli_sessions: list[Mapping[str, object]] = []
        now_ms = int(self._now() * 1000)
        grace_ms = int(grace_seconds * 1000)
        for session in sessions_raw:
            if not isinstance(session, Mapping):
                continue
            key = session.get("key")
            updated_at = session.get("updatedAt")
            if not _is_main_g2_session_key(key):
                continue
            if isinstance(updated_at, bool) or not isinstance(updated_at, int | float):
                raise SupervisorError(f"invalid fresh main G2 CLI session updatedAt: {key}")
            parsed_updated_at = float(updated_at)
            if not math.isfinite(parsed_updated_at):
                raise SupervisorError(f"invalid fresh main G2 CLI session updatedAt: {key}")
            if now_ms - int(parsed_updated_at) <= grace_ms:
                fresh_cli_sessions.append(session)

        if not fresh_cli_sessions:
            return []

        lifecycle_store = self._load_main_session_store_payload()
        fresh: list[dict[str, object]] = []
        for session in fresh_cli_sessions:
            key = session["key"]
            assert isinstance(key, str)
            lifecycle = lifecycle_store.get(key)
            if lifecycle is None:
                raise SupervisorError(
                    f"missing authoritative lifecycle entry for fresh main G2 session: {key}"
                )
            if not isinstance(lifecycle, Mapping):
                raise SupervisorError(
                    "authoritative lifecycle entry must be an object for fresh main G2 "
                    f"session: {key}"
                )
            status = lifecycle.get("status")
            if not isinstance(status, str):
                raise SupervisorError(
                    "authoritative lifecycle entry is missing string status for fresh main G2 "
                    f"session: {key}"
                )
            if status != "running":
                continue
            if lifecycle.get("endedAt") is not None or lifecycle.get("abortedLastRun") is True:
                raise SupervisorError(
                    "contradictory running authoritative lifecycle entry for fresh main "
                    f"G2 session: {key}"
                )
            last_event_at = _running_expected_main_session_last_event_ms(lifecycle)
            if last_event_at > now_ms:
                raise SupervisorError(
                    "authoritative lifecycle timestamp is in the future for fresh main "
                    f"G2 session: {key}"
                )
            if now_ms - last_event_at <= grace_ms:
                fresh.append(dict(session))
        return fresh

    def _run_openclaw_json(self, openclaw_bin: Path, args: Sequence[str]) -> Mapping[str, object]:
        command = [str(openclaw_bin), *args]
        try:
            result = self._run_command(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise SupervisorError(f"failed to execute {' '.join(command)}: {exc}") from exc
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            raise SupervisorError(
                f"OpenClaw command failed ({' '.join(command)}): {output or 'no output'}"
            )
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise SupervisorError(
                f"OpenClaw command returned invalid JSON ({' '.join(command)})"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise SupervisorError(
                f"OpenClaw command returned non-object JSON ({' '.join(command)})"
            )
        return parsed

    def _is_relevant_task(self, task: Mapping[str, object]) -> bool:
        agent_id = task.get("agentId")
        requester_agent_id = task.get("requesterAgentId")
        if agent_id not in RELEVANT_AGENT_IDS and requester_agent_id not in RELEVANT_AGENT_IDS:
            return False
        keys = [
            task.get("requesterSessionKey"),
            task.get("ownerKey"),
            task.get("childSessionKey"),
        ]
        session_match = any(_is_main_g2_session_key(key) for key in keys)
        text = str(task.get("task", "")).lower()
        keyword_match = "autoresearch" in text or "quantipy" in text
        return session_match or keyword_match

    def _active_target_repo_writer_processes(
        self,
        state: AutoresearchState,
    ) -> tuple[str, ...]:
        target_roots = self._target_writer_roots(state)
        proc_root = self.config.proc_root
        if not proc_root.is_dir():
            raise SupervisorError(f"process filesystem is unavailable: {proc_root}")
        exclude_pids = {os.getpid(), os.getppid()}
        offenders: list[str] = []
        for proc_dir in proc_root.glob("[0-9]*"):
            try:
                pid = int(proc_dir.name)
            except ValueError:
                continue
            if pid in exclude_pids:
                continue
            try:
                raw = (proc_dir / "cmdline").read_bytes()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SupervisorError(
                    f"failed to inspect process {pid} command line: {exc}"
                ) from exc
            if not raw:
                continue
            argv = tuple(
                argument.decode("utf-8", errors="replace")
                for argument in raw.split(b"\x00")
                if argument
            )
            cmdline = " ".join(argv).strip()
            if not cmdline or TARGET_WRITER_COMMAND_RE.search(cmdline) is None:
                continue
            if self._process_touches_target_repos(proc_dir, target_roots, argv):
                offenders.append(f"{pid} {cmdline}")
        return tuple(offenders)

    def _process_touches_target_repos(
        self,
        proc_dir: Path,
        target_roots: Sequence[Path],
        argv: Sequence[str],
    ) -> bool:
        try:
            cwd = (proc_dir / "cwd").resolve(strict=True)
        except FileNotFoundError:
            cwd = None
        except OSError as exc:
            raise SupervisorError(
                f"failed to inspect process {proc_dir.name} working directory: {exc}"
            ) from exc
        if cwd is not None and self._path_is_within_target_roots(cwd, target_roots):
            return True
        return any(
            argument_path is not None
            and self._path_is_within_target_roots(argument_path, target_roots)
            for argument in argv
            if (argument_path := self._resolved_process_argument_path(argument, cwd)) is not None
        )

    def _resolved_process_argument_path(
        self,
        argument: str,
        cwd: Path | None,
    ) -> Path | None:
        try:
            candidate = Path(argument).expanduser()
        except (OSError, RuntimeError, ValueError):
            return None
        if not candidate.is_absolute():
            if cwd is None:
                return None
            candidate = cwd / candidate
        try:
            return candidate.resolve(strict=True)
        except FileNotFoundError:
            return None
        except (OSError, RuntimeError) as exc:
            raise SupervisorError(
                f"failed to resolve process filesystem argument {argument!r}: {exc}"
            ) from exc

    def _path_is_within_target_roots(
        self,
        path: Path,
        target_roots: Sequence[Path],
    ) -> bool:
        return any(
            path == target_root or target_root in path.parents for target_root in target_roots
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
        try:
            workspace_candidate = Path(workspace_path).expanduser()
        except (OSError, ValueError) as exc:
            raise WorkspaceEvidenceError(
                "implementation_result workspace_path is not a valid filesystem path"
            ) from exc
        if not workspace_candidate.is_absolute():
            raise WorkspaceEvidenceError("implementation_result workspace_path must be absolute")
        try:
            workspace_root = workspace_candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceEvidenceError(
                "implementation_result workspace_path cannot be resolved strictly: "
                f"{workspace_candidate}"
            ) from exc
        if workspace_root == repo_root:
            return (repo_root,)
        return repo_root, workspace_root

    def _target_repo_root(self, state: AutoresearchState) -> Path:
        if state.setup is not None:
            return Path(state.setup.target_repo).expanduser().resolve()
        return self.config.target_repo.expanduser().resolve()

    def _build_state_probe(self, state: AutoresearchState) -> StateProbe:
        paths = [self.config.state_path]
        iteration_prefix = f"iteration-{state.iteration}-"
        if self.config.autoresearch_dir.exists():
            for path in sorted(self.config.autoresearch_dir.iterdir()):
                if not path.is_file():
                    continue
                if path.name.startswith(iteration_prefix) or path.name == "current-next.json":
                    paths.append(path)
        repo_root = self._target_repo_root(state)
        paths.extend(self._git_marker_paths(repo_root))
        latest_update_ts = 0.0
        latest_update_path = self.config.state_path
        fingerprint_parts: list[str] = []
        for path in paths:
            try:
                stat_result = path.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SupervisorError(f"failed to stat supervision path {path}: {exc}") from exc
            fingerprint_parts.append(f"{path}:{stat_result.st_mtime_ns}:{stat_result.st_size}")
            if stat_result.st_mtime > latest_update_ts:
                latest_update_ts = stat_result.st_mtime
                latest_update_path = path
        if latest_update_ts == 0.0:
            raise SupervisorError("could not determine any autoresearch progress timestamps")
        fingerprint = _sha256_text("\n".join(fingerprint_parts))
        return StateProbe(
            fingerprint=fingerprint,
            latest_update_ts=latest_update_ts,
            latest_update_path=latest_update_path,
        )

    def _git_marker_paths(self, repo_root: Path) -> list[Path]:
        git_root = repo_root / ".git"
        if git_root.is_file():
            try:
                content = git_root.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SupervisorError(
                    f"failed to read gitdir pointer at {git_root}: {exc}"
                ) from exc
            prefix = "gitdir:"
            if not content.startswith(prefix):
                raise SupervisorError(f"invalid gitdir pointer at {git_root}")
            resolved = content[len(prefix) :].strip()
            git_root = (repo_root / resolved).resolve()
        markers = [
            git_root / "HEAD",
            git_root / "index",
            git_root / "logs" / "HEAD",
        ]
        refs_dir = git_root / "refs" / "heads"
        if refs_dir.exists():
            markers.extend(path for path in refs_dir.rglob("*") if path.is_file())
        return markers

    def _read_g2_snapshot(self) -> G2Snapshot:
        state_value = self._dev_get_state()
        display_payload = self._dev_http_json("GET", "/_dev/display")
        display_result = display_payload.get("result")
        if display_result is None:
            display_text = ""
        elif isinstance(display_result, str):
            display_text = display_result
        else:
            display_text = json.dumps(display_result, sort_keys=True)
        detected_error = self._detect_recent_error_text(display_text)
        return G2Snapshot(
            state=state_value,
            display_text=display_text,
            detected_error_text=detected_error,
        )

    def _detect_recent_error_text(self, display_text: str) -> str | None:
        lowered_display = display_text.lower()
        for pattern in RECOVERY_ERROR_PATTERNS:
            if pattern in lowered_display:
                return pattern
        sessions = self._load_main_session_store()
        main_g2_entries = sorted(
            (
                (key, value)
                for key, value in sessions.items()
                if _is_main_g2_session_key(key) and isinstance(value, Mapping)
            ),
            key=lambda item: _mapping_timestamp_ms(item[1]),
            reverse=True,
        )
        for key, entry in main_g2_entries[:3]:
            haystacks = [json.dumps(entry, sort_keys=True)]
            session_file = entry.get("sessionFile")
            if isinstance(session_file, str):
                haystacks.append(self._tail_text(Path(session_file), bytes_limit=128_000))
            for haystack in haystacks:
                lowered = haystack.lower()
                for pattern in RECOVERY_ERROR_PATTERNS:
                    if pattern in lowered:
                        _structured_log(
                            logging.WARNING,
                            "supervisor.detected_error_text",
                            pattern=pattern,
                            session_key=key,
                        )
                        return pattern
        return None

    def _load_main_session_store_payload(self) -> Mapping[str, object]:
        try:
            raw = json.loads(
                self.config.main_sessions_path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_json_object,
            )
        except FileNotFoundError as exc:
            raise SupervisorError(
                f"missing OpenClaw main sessions store: {self.config.main_sessions_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SupervisorError(
                f"invalid OpenClaw main sessions JSON: {self.config.main_sessions_path}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise SupervisorError(
                f"invalid OpenClaw main sessions payload: {self.config.main_sessions_path}"
            )
        return raw

    def _load_main_session_store(self) -> dict[str, Mapping[str, object]]:
        raw = self._load_main_session_store_payload()
        result: dict[str, Mapping[str, object]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, Mapping):
                result[key] = value
        return result

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
            raise SupervisorError(f"failed to read session transcript tail {path}: {exc}") from exc

    def _rotation_reasons(
        self,
        state: AutoresearchState,
        g2_snapshot: G2Snapshot,
        checkpoint: SupervisorCheckpoint,
    ) -> list[str]:
        reasons: list[str] = []
        if (
            state.phase is Phase.SETUP_CONTEXT
            and str(state.iteration) not in checkpoint.setup_iteration_rotations
        ):
            reasons.append("setup_iteration_boundary")
        if g2_snapshot.state == "error":
            reasons.append("g2_error_state")
        if g2_snapshot.detected_error_text is not None:
            reasons.append(f"g2_error_text:{g2_snapshot.detected_error_text}")
        return reasons

    def _rotate_g2_session(self, g2_state: str) -> None:
        if g2_state == "error":
            self._dev_command("doubleTap")
        elif g2_state == "idle":
            self._dev_command("openSessionMenu")
        elif g2_state == "menu":
            pass
        else:
            raise DevAPIError(f"cannot rotate G2 session from state {g2_state!r}")

        session_list: object = None
        deadline = self._now() + self.config.menu_wait_seconds
        while self._now() < deadline:
            current_state = self._dev_get_state()
            session_list = self._dev_command("getSessionList")
            if current_state == "menu" and isinstance(session_list, list):
                break
            self._sleep(0.25)
        if not isinstance(session_list, list):
            raise DevAPIError("session menu did not become ready for fresh-session creation")
        old_session_key = self._active_g2_session_key(session_list)

        self._dev_command("selectSession", [0])

        ready_deadline = self._now() + self.config.session_ready_wait_seconds
        while self._now() < ready_deadline:
            current_state = self._dev_get_state()
            if current_state == "idle":
                break
            self._sleep(0.25)
        else:
            raise DevAPIError("fresh G2 session did not return to idle in time")

        if old_session_key is not None:
            self._retire_g2_session(old_session_key)

    def _active_g2_session_key(self, session_list: Sequence[object]) -> str | None:
        active_keys: list[str] = []
        for index, entry in enumerate(session_list):
            if not isinstance(entry, Mapping):
                raise DevAPIError(f"G2 session menu entry {index} must be an object")
            session_key = entry.get("sessionKey")
            if not isinstance(session_key, str) or not session_key:
                raise DevAPIError(f"G2 session menu entry {index} has an invalid sessionKey")
            is_active = entry.get("isActive")
            if not isinstance(is_active, bool):
                raise DevAPIError(f"G2 session menu entry {index} has an invalid isActive flag")
            if is_active:
                if not _is_main_g2_session_key(session_key):
                    raise DevAPIError(
                        f"G2 session menu active key is not a main G2 session: {session_key}"
                    )
                active_keys.append(session_key)

        if len(active_keys) > 1:
            raise DevAPIError("G2 session menu has multiple active sessions")
        return active_keys[0] if active_keys else None

    def _retire_g2_session(self, old_session_key: str) -> None:
        if not _is_main_g2_session_key(old_session_key):
            raise SupervisorError(f"refusing to retire a non-main G2 session: {old_session_key}")
        openclaw_bin = self._require_openclaw_binary()
        params = json.dumps(
            {
                "key": old_session_key,
                "agentId": "main",
                "deleteTranscript": False,
            },
            separators=(",", ":"),
        )
        payload = self._run_openclaw_json(
            openclaw_bin,
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
        if (
            payload.get("ok") is not True
            or payload.get("deleted") is not True
            or payload.get("key") != old_session_key
        ):
            raise SupervisorError(
                "OpenClaw session retirement response did not confirm deletion of "
                f"{old_session_key}"
            )

    def _send_recovery_message(self) -> None:
        payload = self._dev_http_json("POST", "/_dev/sendText", {"text": RECOVERY_MESSAGE})
        result = payload.get("result")
        if result is False:
            raise DevAPIError("G2 Dev API rejected recovery message send")

    def _dev_get_state(self) -> str:
        payload = self._dev_http_json("GET", "/_dev/state")
        result = payload.get("result")
        if not isinstance(result, str) or not result:
            raise DevAPIError("G2 Dev API returned an invalid state payload")
        return result

    def _dev_command(self, command: str, args: Sequence[object] | None = None) -> object:
        payload = self._dev_http_json(
            "POST",
            "/_dev/cmd",
            {
                "cmd": command,
                "args": list(args or ()),
            },
        )
        command_id = payload.get("id")
        if not isinstance(command_id, str) or not command_id:
            raise DevAPIError(f"G2 Dev API did not return a command id for {command}")
        result_payload = self._dev_http_json("GET", f"/_dev/result/{command_id}")
        return result_payload.get("result")

    def _dev_http_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        url = f"{self.config.dev_api_base}{path}"
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=self.config.dev_api_timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise DevAPIError(f"G2 Dev API request failed for {url}: {exc}") from exc
        except TimeoutError as exc:
            raise DevAPIError(f"G2 Dev API request timed out for {url}") from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DevAPIError(f"G2 Dev API returned invalid JSON for {url}") from exc
        if not isinstance(parsed, Mapping):
            raise DevAPIError(f"G2 Dev API returned non-object JSON for {url}")
        error = parsed.get("error")
        if isinstance(error, str) and error:
            raise DevAPIError(f"G2 Dev API error for {url}: {error}")
        return parsed


def _mapping_timestamp_ms(raw: Mapping[str, object]) -> int:
    for field_name in ("updatedAt", "lastInteractionAt", "startedAt"):
        value = raw.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        return int(value)
    return 0


def _task_last_event_ms(task: Mapping[str, object]) -> int | None:
    for field_name in ("lastEventAt", "updatedAt", "startedAt", "createdAt"):
        value = task.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        return int(value)
    return None


def _expected_task_last_event_ms(task: Mapping[str, object]) -> int | None:
    for field_name in ("lastEventAt", "updatedAt", "startedAt", "createdAt"):
        if field_name not in task:
            continue
        value = task.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return int(parsed)
    return None


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorError(f"duplicate OpenClaw main sessions key: {key}")
        result[key] = value
    return result


def _running_expected_main_session_last_event_ms(session: Mapping[str, object]) -> int:
    timestamps: list[int] = []
    for field_name in ("updatedAt", "lastInteractionAt", "startedAt"):
        if field_name not in session:
            raise SupervisorError(f"running main lifecycle entry is missing integer {field_name}")
        value = session.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SupervisorError(f"running main lifecycle entry has non-integer {field_name}")
        timestamps.append(value)
    return max(timestamps)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one supervision check and exit.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Daemon poll interval in seconds (default: {DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--grace",
        type=float,
        default=DEFAULT_GRACE_PERIOD_SECONDS,
        help=f"Idle grace period in seconds (default: {DEFAULT_GRACE_PERIOD_SECONDS}).",
    )
    parser.add_argument(
        "--expected-stage-task-stale",
        type=_finite_positive_cli_float,
        default=DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS,
        help=(
            "Expected-stage task stale threshold in seconds "
            f"(default: {DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS})."
        ),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Autoresearch state path (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help=f"Supervisor checkpoint path (default: {DEFAULT_CHECKPOINT_PATH}).",
    )
    parser.add_argument(
        "--dev-api-base",
        default=DEFAULT_DEV_API_BASE,
        help=f"G2 Dev API base URL (default: {DEFAULT_DEV_API_BASE}).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default: INFO).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO))
    config = SupervisorConfig(
        state_path=args.state_path,
        checkpoint_path=args.checkpoint_path,
        autoresearch_dir=args.state_path.parent,
        dev_api_base=args.dev_api_base,
        poll_interval_seconds=float(args.interval),
        grace_period_seconds=float(args.grace),
        expected_stage_task_stale_seconds=float(args.expected_stage_task_stale),
    )
    supervisor = AutoresearchSupervisor(config)
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
