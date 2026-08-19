"""Deterministic, owner-session-only supervisor for Quantipy autoresearch."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from dotenv import load_dotenv as load_dotenv

from gateway.autoresearch.artifacts import (
    QuantipyExecutionInterruptedEvidence,
    VerificationResultArtifact,
)
from gateway.autoresearch.attestation import (
    require_canonical_verification_dispatch_attestation,
    seal_canonical_verification_dispatch_state_file,
)
from gateway.autoresearch.configuration import (
    load_autoresearch_policy,
)
from gateway.autoresearch.constants import (
    CAMPAIGN_REVIEW_RECOVERY_COMMAND,
    DEFAULT_AUTORESEARCH_LAUNCH_REQUESTS,
    DEFAULT_AUTORESEARCH_STAGE_INBOX,
    DEFAULT_OPENCLAW_CONFIG_PATH,
    DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
    DEFAULT_QUANTIPY_ROOT,
)
from gateway.autoresearch.enums import (
    FinalDecision,
    Phase,
    ResearchMode,
    VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.lifecycle import (
    start_next_iteration,
)
from gateway.autoresearch.manifest_runtime import (
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
)
from gateway.autoresearch.memory import (
    can_write_memory,
    finalize_repeat_memory_state_file,
)
from gateway.autoresearch.persistence import (
    advance_infrastructure_verification_failure,
    consume_stage_submission_inbox,
    load_state_file,
    persist_next_iteration_state,
    provision_quantipy_experiment_runs_root,
)
from gateway.autoresearch.state import (
    AutoresearchState,
    AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference,
)
from gateway.autoresearch.workspace import (
    migrate_legacy_autoresearch_workspace_state_file,
)
from gateway.autoresearch_checkpoint import (
    MemoryWakeAcknowledgement as MemoryWakeAcknowledgement,
)
from gateway.autoresearch_checkpoint import (
    RecoveryRecord as RecoveryRecord,
)
from gateway.autoresearch_checkpoint import (
    SupervisorCheckpoint as SupervisorCheckpoint,
)
from gateway.autoresearch_checkpoint import (
    _optional_float as _optional_float,
)
from gateway.autoresearch_checkpoint import (
    _optional_int as _optional_int,
)
from gateway.autoresearch_checkpoint import (
    _optional_str as _optional_str,
)
from gateway.autoresearch_checkpoint import (
    reset_recovery_checkpoint_for_manual_wake as reset_recovery_checkpoint_for_manual_wake,
)
from gateway.autoresearch_readiness import (
    DEFAULT_PLATFORM_READINESS_PATH,
    load_platform_readiness,
    validate_state_readiness,
)
from gateway.autoresearch_reconciliation import (
    CanonicalTaskStatus as CanonicalTaskStatus,
)
from gateway.autoresearch_reconciliation import (
    ReconciledRunningTasks as ReconciledRunningTasks,
)
from gateway.autoresearch_reconciliation import (
    TaskProvenance as TaskProvenance,
)
from gateway.autoresearch_reconciliation import (
    TaskReconciliationError as TaskReconciliationError,
)
from gateway.autoresearch_reconciliation import (
    _detect_recovery_error_in_object as _detect_recovery_error_in_object,
)
from gateway.autoresearch_reconciliation import (
    _detect_recovery_error_in_text as _detect_recovery_error_in_text,
)
from gateway.autoresearch_reconciliation import (
    _preferred_recovery_error as _preferred_recovery_error,
)
from gateway.autoresearch_reconciliation import (
    _task_id_for_reconciliation as _task_id_for_reconciliation,
)
from gateway.autoresearch_reconciliation import (
    _task_provenance_fingerprint as _task_provenance_fingerprint,
)
from gateway.autoresearch_reconciliation import (
    classify_autoresearch_task as classify_autoresearch_task,
)
from gateway.autoresearch_reconciliation import (
    reconcile_relevant_running_tasks as reconcile_relevant_running_tasks,
)
from gateway.autoresearch_rpc import (
    NativeGatewayRPC as NativeGatewayRPC,
)
from gateway.autoresearch_rpc import (
    OpenClawRPC as OpenClawRPC,
)
from gateway.autoresearch_rpc import (
    ShutdownRequested as ShutdownRequested,
)
from gateway.autoresearch_rpc import (
    TaskGateway as TaskGateway,
)
from gateway.autoresearch_rpc import (
    WakeDeliveryProof as WakeDeliveryProof,
)
from gateway.autoresearch_rpc import (
    _default_task_gateway as _default_task_gateway,
)
from gateway.autoresearch_rpc import (
    _shutdown_not_requested as _shutdown_not_requested,
)
from gateway.autoresearch_rpc import (
    _strict_json_object as _strict_json_object,
)
from gateway.autoresearch_rpc import (
    make_idempotency_key as make_idempotency_key,
)
from gateway.autoresearch_runs import (
    DEFAULT_AUTORESEARCH_RUNS_ROOT,
    AutoresearchRunRecordError,
    ExpectedArtifactAttestationError,
    ExpectedArtifactAttestationStatus,
    RunFailureClassification,
    RunManifest,
    RunRecord,
    RunState,
    complete_run,
    read_run_record,
)
from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_AGENT_ID as AUTORESEARCH_OWNER_AGENT_ID,
)
from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_SESSION_KEY as AUTORESEARCH_OWNER_SESSION_KEY,
)
from gateway.autoresearch_shared import (
    DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS as DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS,
)
from gateway.autoresearch_shared import (
    DEFAULT_TASK_RPC_TIMEOUT_SECONDS as DEFAULT_TASK_RPC_TIMEOUT_SECONDS,
)
from gateway.autoresearch_shared import (
    EXPECTED_STAGE_AGENT_IDS as EXPECTED_STAGE_AGENT_IDS,
)
from gateway.autoresearch_shared import (
    READ_ONLY_TASK_LIST_ATTEMPTS as READ_ONLY_TASK_LIST_ATTEMPTS,
)
from gateway.autoresearch_shared import (
    READ_ONLY_TASK_LIST_RETRY_SECONDS as READ_ONLY_TASK_LIST_RETRY_SECONDS,
)
from gateway.autoresearch_shared import (
    RECOVERY_ERROR_PATTERNS as RECOVERY_ERROR_PATTERNS,
)
from gateway.autoresearch_shared import (
    RELEVANT_AGENT_IDS as RELEVANT_AGENT_IDS,
)
from gateway.autoresearch_shared import (
    REQUIRED_OPENCLAW_VERSION_TEXT as REQUIRED_OPENCLAW_VERSION_TEXT,
)
from gateway.autoresearch_shared import (
    OpenClawUnavailableError as OpenClawUnavailableError,
)
from gateway.autoresearch_shared import (
    RecoveryErrorPattern as RecoveryErrorPattern,
)
from gateway.autoresearch_shared import (
    RecoveryStatus as RecoveryStatus,
)
from gateway.autoresearch_shared import (
    ShutdownInterrupted as ShutdownInterrupted,
)
from gateway.autoresearch_shared import (
    SupervisorCheckpointError as SupervisorCheckpointError,
)
from gateway.autoresearch_shared import (
    SupervisorError as SupervisorError,
)
from gateway.openclaw_client import OpenClawClient as OpenClawClient

logger = logging.getLogger(__name__)

DEFAULT_AUTORESEARCH_DIR = Path.home() / ".openclaw" / "autoresearch"
DEFAULT_STATE_PATH = DEFAULT_AUTORESEARCH_DIR / "quantipy-state.json"
DEFAULT_CHECKPOINT_PATH = DEFAULT_AUTORESEARCH_DIR / "owner-recovery.json"
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
DEFAULT_RENUDGE_ESCALATION_MINUTES = 20.0
DEFAULT_MAX_ESCALATING_RENUDGES = 5
DEFAULT_STALENESS_FORCE_WAKE_MINUTES = 45.0
DEFAULT_WRITER_STALE_MINUTES = 60.0
DEFAULT_LAUNCH_REQUEST_TTL_MINUTES = 30.0
# Implementation, verification, review, and fix stages can spend several
# minutes running tests and backtests without producing an OpenClaw event.
# Keep the supervisor responsive while allowing those legitimate long turns to
# finish before declaring the task stale.
DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS = 900.0
DEFAULT_MAX_RECOVERY_ATTEMPTS = 2
FAILED_DETACHED_RUN_STDERR_TAIL_BYTES = 400
MAX_WAKE_MESSAGE_BYTES = 1500
WATCHDOG_RECOVERY_KEY_PREFIX = "staleness-watchdog:"
MONOTONIC_RECOVERY_KEY_PREFIX = "supervisor-clock:"
LAUNCH_REQUEST_SCHEMA_VERSION = 1
LAUNCH_REQUEST_MAX_BYTES = 4096
LAUNCH_REQUEST_TIMEOUT_SECONDS = 30.0
REQUIRED_OPENCLAW_VERSION = (2026, 7, 1)
WAKE_MESSAGE = (
    "Continue Quantipy autoresearch from the authoritative state. First run exactly: "
    "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json. The supervisor owns "
    "final MemPalace persistence before waking this session; do not write "
    "MemPalace from any model turn."
)
FINALIZED_MEMORY_WAKE_MESSAGE = (
    "Required final MemPalace persistence was completed by the autoresearch "
    "supervisor from authoritative state. Continue Quantipy autoresearch from "
    "the authoritative state. First run exactly: "
    "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
    "/home/dev/.openclaw/autoresearch/quantipy-state.json. Do not write "
    "MemPalace from any model turn."
)
RECOVERY_MESSAGE = (
    "Continue Quantipy autoresearch from the authoritative state. First run exactly: "
    "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
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
    "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
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
MISSING_VERIFICATION_ARTIFACT_REASON = "missing_verification_artifact"
OWNER_SESSION_STORE_UNAVAILABLE_REASON = "owner_session_store_unavailable"
EARLY_OWNER_LIFECYCLE_SHORT_CIRCUIT_PHASES = frozenset({Phase.VERIFICATION, Phase.DECISION_LOG})
TARGET_WRITER_COMMAND_RE = re.compile(
    r"(\bpytest\b|\bpy\.test\b|\bjupyter\b|\bpapermill\b|\bipython\b|"
    r"\bnbconvert\b|\bgenerate_[\w.-]*|notebooks/experiments|"
    r"src/quantipy/alpha|scripts/experiments|tools/experiments)"
)


class WorkspaceEvidenceError(SupervisorError):
    """Raised when an active implementation workspace cannot be verified."""


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
    RENUDGED = "renudged"
    ALERT = "alert"
    FINALIZED = "finalized"
    ERROR = "error"


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
    renudge_escalation_minutes: float = DEFAULT_RENUDGE_ESCALATION_MINUTES
    max_escalating_renudges: int = DEFAULT_MAX_ESCALATING_RENUDGES
    staleness_force_wake_minutes: float = DEFAULT_STALENESS_FORCE_WAKE_MINUTES
    writer_stale_minutes: float = DEFAULT_WRITER_STALE_MINUTES
    launch_request_ttl_minutes: float = DEFAULT_LAUNCH_REQUEST_TTL_MINUTES
    expected_stage_task_stale_seconds: float = DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS
    max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
    stage_inbox_path: Path = DEFAULT_AUTORESEARCH_STAGE_INBOX
    launch_requests_path: Path = DEFAULT_AUTORESEARCH_LAUNCH_REQUESTS

    def __post_init__(self) -> None:
        _require_finite_positive(self.poll_interval_seconds, field_name="poll_interval_seconds")
        _require_finite_positive(self.grace_period_seconds, field_name="grace_period_seconds")
        _require_finite_positive(self.claim_stale_seconds, field_name="claim_stale_seconds")
        _require_finite_positive(
            self.renudge_escalation_minutes,
            field_name="renudge_escalation_minutes",
        )
        if (
            isinstance(self.max_escalating_renudges, bool)
            or not isinstance(self.max_escalating_renudges, int)
            or self.max_escalating_renudges < 1
        ):
            raise SupervisorError("max_escalating_renudges must be a positive integer")
        _require_finite_positive(
            self.staleness_force_wake_minutes,
            field_name="staleness_force_wake_minutes",
        )
        _require_finite_positive(self.writer_stale_minutes, field_name="writer_stale_minutes")
        _require_finite_positive(
            self.launch_request_ttl_minutes,
            field_name="launch_request_ttl_minutes",
        )
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
    renudge_idle_seconds: float | None = None
    escalation_count: int = 0
    forced_by_staleness_watchdog: bool = False
    state_fingerprint: str = ""
    record_key: str | None = None
    ordinary_recovery_cold_start: bool = False


@dataclass(frozen=True, slots=True)
class MalformedRunRecord:
    run_directory: Path
    attempt: int | None
    error: AutoresearchRunRecordError


def memory_wake_acknowledgement_key(state: AutoresearchState) -> str:
    """Stable controller-state key for one finalized memory wake proof."""
    decision = state.final_decision
    receipt = state.memory_verification_receipt
    if decision is None or receipt is None or not state.memory_written:
        raise SupervisorError("memory wake acknowledgement requires finalized memory state")
    if decision.experiment_id != receipt.experiment_id:
        raise SupervisorError("memory wake acknowledgement experiment_id mismatch")
    return (
        "memory-finalized:"
        f"{state.iteration}:{decision.experiment_id}:{receipt.verified_rows_digest}"
    )


def _structured_log(level: int, event: str, **fields: object) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, sort_keys=True, default=str))


def _validate_launch_request_directory_fd(fd: int, *, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SupervisorError(f"{label} must be a plain directory")
    if metadata.st_uid != os.getuid():
        raise SupervisorError(f"{label} must be owned by the current user")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SupervisorError(f"{label} must not be group/world writable")


def _open_launch_request_child_directory(inbox_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=inbox_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise SupervisorError(f"cannot create launch request {name} directory: {exc}") from exc
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=inbox_fd,
        )
    except OSError as exc:
        raise SupervisorError(
            f"launch request {name} path must be a plain directory: {exc}"
        ) from exc
    try:
        _validate_launch_request_directory_fd(child_fd, label=f"launch request {name} directory")
    except BaseException:
        os.close(child_fd)
        raise
    return child_fd


def _move_launch_request_no_replace(
    name: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
    destination_name: str | None = None,
    expected_stat: os.stat_result | None = None,
    source_fd: int | None = None,
) -> None:
    """Atomically move a request without replacing a destination.

    ``os.rename`` is atomic but replaces an existing destination on POSIX.
    Launch requests need the stronger no-replace operation because a stale
    request can race a publisher using the same filename.  Linux exposes the
    required operation as ``renameat2(RENAME_NOREPLACE)``; fail closed when it
    is unavailable rather than falling back to link/unlink or an overwrite.
    """

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise SupervisorError("atomic no-replace rename is unavailable") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int

    owned_source_fd = source_fd
    close_source_fd = False
    if expected_stat is not None and owned_source_fd is None:
        try:
            owned_source_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=src_dir_fd,
            )
        except OSError as exc:
            raise SupervisorError("launch request changed during stale archival") from exc
        close_source_fd = True

    def same_inode(metadata: os.stat_result) -> bool:
        assert expected_stat is not None
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_nlink,
        ) == (
            expected_stat.st_dev,
            expected_stat.st_ino,
            expected_stat.st_mode,
            expected_stat.st_size,
            expected_stat.st_mtime_ns,
            expected_stat.st_nlink,
        )

    def rollback_mismatched_entry(target: str) -> str | None:
        restore_result = renameat2(
            dst_dir_fd,
            os.fsencode(target),
            src_dir_fd,
            os.fsencode(name),
            1,
        )
        if restore_result == 0:
            return None
        restore_errno = ctypes.get_errno()
        # A publisher can recreate the request name between the archival
        # rename and this rollback. Keep that replacement at the request name,
        # but move the entry we moved out of the archive name to a unique
        # non-request name. This preserves no-replace semantics and never
        # leaves the replacement under the stale archive name.
        for rollback_attempt in range(128):
            rollback_name = (
                f".raced-{name}-"
                f"{hashlib.sha256(f'{name}\\n{time.time_ns()}\\n{rollback_attempt}'.encode()).hexdigest()[:16]}"
            )
            rollback_result = renameat2(
                dst_dir_fd,
                os.fsencode(target),
                src_dir_fd,
                os.fsencode(rollback_name),
                1,
            )
            if rollback_result == 0:
                return rollback_name
            if ctypes.get_errno() != errno.EEXIST:
                break
        raise SupervisorError(
            "launch request changed during stale archival and could not be restored"
        ) from OSError(restore_errno, os.strerror(restore_errno))

    try:
        if expected_stat is not None:
            assert owned_source_fd is not None
            if not same_inode(os.fstat(owned_source_fd)):
                raise SupervisorError("launch request changed during stale archival")
            # Also validate the inode CURRENTLY bound to the pathname, not only
            # the descriptor we hold: a publisher can replace the entry after
            # our open, and the rename below moves whatever the name binds NOW.
            # The post-rename check only rolls back if this process survives to
            # run it, so a crash in that window would strand a live request in
            # the archive. Checking the binding first closes the crash window.
            try:
                bound_stat = os.stat(name, dir_fd=src_dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise SupervisorError("launch request changed during stale archival") from exc
            if not same_inode(bound_stat):
                raise SupervisorError("launch request changed during stale archival")

        for attempt in range(128):
            target = destination_name or name
            if attempt:
                digest = hashlib.sha256(
                    f"{target}\n{time.time_ns()}\n{attempt}".encode()
                ).hexdigest()
                target = f"{target}.{digest[:16]}"
            result = renameat2(
                src_dir_fd,
                os.fsencode(name),
                dst_dir_fd,
                os.fsencode(target),
                1,  # RENAME_NOREPLACE
            )
            if result != 0:
                error_number = ctypes.get_errno()
                if error_number == errno.EEXIST:
                    continue
                raise SupervisorError(
                    f"cannot quarantine launch request: {os.strerror(error_number)}"
                )

            if expected_stat is not None:
                try:
                    archived = os.stat(target, dir_fd=dst_dir_fd, follow_symlinks=False)
                except OSError as exc:
                    try:
                        rollback_mismatched_entry(target)
                    except SupervisorError as rollback_exc:
                        raise rollback_exc from exc
                    raise SupervisorError("launch request changed during stale archival") from exc
                if not same_inode(archived):
                    rollback_name = rollback_mismatched_entry(target)
                    if rollback_name is not None:
                        raise SupervisorError(
                            "launch request changed during stale archival; "
                            f"moved entry restored as {rollback_name}"
                        )
                    raise SupervisorError("launch request changed during stale archival")
            return
        raise SupervisorError("cannot quarantine launch request without overwrite")
    finally:
        if close_source_fd and owned_source_fd is not None:
            os.close(owned_source_fd)


class AutoresearchSupervisor:
    """Monitors Quantipy and wakes only its dedicated owner session."""

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] | None = None,
        mtime: Callable[[Path], float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        task_gateway: TaskGateway | None = None,
    ) -> None:
        self.config = config or SupervisorConfig()
        self._now = now
        self._monotonic = monotonic or (time.monotonic if now is time.time else now)
        self._mtime = mtime or (lambda path: path.stat().st_mtime)
        self._sleep = sleep
        self._rpc = OpenClawRPC(task_gateway)
        self._cycle_state: AutoresearchState | None = None
        self._campaign_review_warning_record: str | None = None
        self._stale_writer_warning_identities: set[tuple[int, float]] = set()

    def run_once(
        self, *, shutdown_requested: ShutdownRequested = _shutdown_not_requested
    ) -> SupervisorResult:
        self._cycle_state = None
        try:
            result = self._run_once(shutdown_requested=shutdown_requested)
        except BaseException as exc:
            self._record_cycle_safely(
                outcome=SupervisorOutcome.ERROR.value,
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._record_cycle_safely(outcome=result.outcome.value, detail=result.reason)
        return result

    def _run_once(
        self, *, shutdown_requested: ShutdownRequested = _shutdown_not_requested
    ) -> SupervisorResult:
        try:
            state = self._load_state()
            self._cycle_state = state
            if state.campaign_review_required:
                self._log_campaign_review_advisory(state)
            if state.suspended:
                if (
                    state.final_decision is not None
                    and state.final_decision.decision is FinalDecision.INFRA_BLOCKED
                    and not state.final_decision.continue_loop
                ):
                    raise SupervisorError(
                        "INFRA_BLOCKED final decisions require continue_loop=true"
                    )
                return SupervisorResult(SupervisorOutcome.NO_ACTION, "platform_readiness_suspended")
            if self._is_terminal_state(state):
                return SupervisorResult(SupervisorOutcome.NO_ACTION, "terminal_state")
        except SupervisorCheckpointError as exc:
            _structured_log(
                logging.ERROR,
                "supervisor.checkpoint_corrupt",
                detail=str(exc),
            )
            return SupervisorResult(SupervisorOutcome.ALERT, f"checkpoint_corrupt: {exc}")
        try:
            self._validate_dispatchable_state(state)
        except SupervisorError as exc:
            _structured_log(logging.ERROR, "supervisor.readiness_blocked", detail=str(exc))
            return SupervisorResult(
                SupervisorOutcome.ALERT,
                f"platform_readiness_blocked: {exc}",
            )
        writers = self._active_target_repo_writer_processes(state)
        if writers:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "target_repo_writer_active")
        lifecycle_result = self._prepare_controller_lifecycle(
            state,
            shutdown_requested=shutdown_requested,
        )
        if lifecycle_result is not None:
            return lifecycle_result
        finalization_result = self._finalize_required_memory(
            state,
            shutdown_requested=shutdown_requested,
        )
        if finalization_result is not None:
            return finalization_result
        # Stage submissions must be consumed before terminal-run handling: a
        # failed detached run raises a persistent alert that would otherwise
        # starve consumption of the very submission that resolves it.
        inbox_result = self._consume_stage_submission_inbox(state)
        if inbox_result is not None:
            return inbox_result
        launch_result = self._consume_launch_request_inbox()
        if launch_result is not None:
            return launch_result
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
        owner_task_active = self._active_owner_task(reconciled_tasks)
        force_staleness_wake = self._staleness_watchdog_due(
            state,
            probe,
            owner_task_active=owner_task_active,
        )
        if not force_staleness_wake and self._now() - probe.latest_update_ts < (
            self.config.grace_period_seconds
        ):
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "state_not_stale")
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
            state_fingerprint=probe.fingerprint,
            state_mtime=probe.latest_update_ts,
            owner_task_active=owner_task_active,
            force=force_staleness_wake,
        )
        if isinstance(claim_or_result, SupervisorResult):
            return claim_or_result
        if claim_or_result is None:
            return SupervisorResult(
                SupervisorOutcome.NO_ACTION,
                "recovery_escalation_capped",
                recovery_key,
            )
        claim = claim_or_result
        self._rotate_owner_session_for_wake(phase=state.phase.value)
        wake_message = self._with_current_failed_detached_run_detail(
            state,
            recovery_plan.message,
        )
        if claim.forced_by_staleness_watchdog:
            wake_message = (
                "Staleness watchdog: the authoritative state is stale with no active owner "
                "task or current detached run.\n\n"
                f"{wake_message}"
            )
        if claim.escalation_count:
            wake_message = (
                f"Recovery escalation {claim.escalation_count}/"
                f"{self.config.max_escalating_renudges}: fresh wake after unchanged state.\n\n"
                f"{wake_message}"
            )
        wake_message = self._bounded_wake_message(
            self._with_campaign_review_advisory(state, wake_message)
        )
        try:
            self._rpc.wake(
                message=wake_message,
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
        if claim.forced_by_staleness_watchdog:
            _structured_log(
                logging.WARNING,
                "supervisor.staleness_watchdog_wake",
                recovery_key=recovery_key,
                reason=recovery_plan.reason,
            )
        elif claim.renudge_idle_seconds is None:
            _structured_log(
                logging.WARNING,
                "supervisor.nudged",
                recovery_key=recovery_key,
                reason=recovery_plan.reason,
                detected_error=detected_error.pattern if detected_error is not None else None,
            )
        else:
            _structured_log(
                logging.WARNING,
                "supervisor.renudged",
                recovery_key=recovery_key,
                reason=recovery_plan.reason,
                detected_error=detected_error.pattern if detected_error is not None else None,
                idle_seconds=claim.renudge_idle_seconds,
                escalation_count=claim.escalation_count,
            )
        return SupervisorResult(
            SupervisorOutcome.RENUDGED
            if claim.renudge_idle_seconds is not None and not claim.forced_by_staleness_watchdog
            else SupervisorOutcome.NUDGED,
            recovery_plan.reason,
            recovery_key,
            sent_wake=True,
        )

    def _campaign_review_record_key(self, state: AutoresearchState) -> str:
        if state.campaign_review_history:
            record = state.campaign_review_history[-1]
            return json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
        return json.dumps(
            {
                "reason": state.campaign_review_reason,
                "counters": state.campaign_counters.to_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _log_campaign_review_advisory(self, state: AutoresearchState) -> None:
        record_key = self._campaign_review_record_key(state)
        if record_key == self._campaign_review_warning_record:
            return
        self._campaign_review_warning_record = record_key
        _structured_log(
            logging.WARNING,
            "supervisor.campaign_review_advisory",
            iteration=state.iteration,
            reason=state.campaign_review_reason,
            recovery_command=CAMPAIGN_REVIEW_RECOVERY_COMMAND,
            counters=state.campaign_counters.to_dict(),
            review_record=record_key,
        )

    def _with_campaign_review_advisory(
        self,
        state: AutoresearchState,
        message: str,
    ) -> str:
        if not state.campaign_review_required:
            return message
        counters = state.campaign_counters
        return (
            f"{message}\n\nCampaign review advisory: {state.campaign_review_reason}. "
            "Research continues under the current directive until the operator acknowledges "
            f"the review with {CAMPAIGN_REVIEW_RECOVERY_COMMAND}. Counters: "
            f"consecutive_non_keep={counters.consecutive_non_keep}, "
            f"consecutive_no_consensus={counters.consecutive_no_consensus}, "
            f"iterations_since_last_keep={counters.iterations_since_last_keep}."
        )

    def _record_cycle_safely(self, *, outcome: str, detail: str) -> None:
        try:
            self._persist_cycle(self._cycle_state, outcome=outcome, detail=detail)
        except BaseException as exc:
            _structured_log(
                logging.ERROR,
                "supervisor.cycle_persist_failed",
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _persist_cycle(
        self,
        state: AutoresearchState | None,
        *,
        outcome: str,
        detail: str,
    ) -> None:
        cycle_at = self._now()
        try:
            with self._checkpoint_lock():
                checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
                checkpoint.last_cycle_outcome = outcome
                checkpoint.last_cycle_detail = detail
                checkpoint.last_cycle_at = cycle_at
                checkpoint.save(self.config.checkpoint_path)
        except SupervisorError as exc:
            _structured_log(logging.ERROR, "supervisor.cycle_persist_failed", detail=str(exc))
        _structured_log(
            logging.INFO,
            "supervisor.cycle",
            outcome=outcome,
            detail=detail,
            phase=state.phase.value if state is not None else None,
            iteration=state.iteration if state is not None else None,
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

    def _with_current_failed_detached_run_detail(
        self,
        state: AutoresearchState,
        message: str,
    ) -> str:
        """Append bounded current-run evidence needed by the owner on recovery."""
        if state.phase not in {Phase.IMPLEMENTATION, Phase.VERIFICATION}:
            return message
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
            if state.phase is Phase.VERIFICATION:
                matching = self._matching_verification_runs(
                    iteration=state.iteration,
                    state_reference_sha256=state_reference_sha256,
                    instruction_manifest_sha256=instruction_manifest_sha256,
                )
            else:
                matching = self._matching_current_phase_runs(
                    state,
                    state_reference_sha256=state_reference_sha256,
                    instruction_manifest_sha256=instruction_manifest_sha256,
                )
        except (AutoresearchRunRecordError, AutoresearchValidationError, OSError, ValueError):
            return message
        if not matching:
            return message
        latest = matching[-1]
        if state.phase is Phase.IMPLEMENTATION and state.implementation_result is None:
            succeeded_prewarms = [
                record
                for record in matching
                if record.status.state is RunState.SUCCEEDED
                and "prewarm" in record.manifest.task_label.lower()
            ]
            if succeeded_prewarms:
                latest = succeeded_prewarms[-1]
                return (
                    f"{message}\n\nCurrent succeeded prewarm: "
                    f"outcome=succeeded; run_directory={latest.run_directory.name}; "
                    "reuse its receipts; build "
                    "and submit the implementation"
                )
        if latest.status.state is RunState.SUCCEEDED:
            if state.phase is Phase.VERIFICATION:
                return (
                    f"{message}\n\nCurrent succeeded detached verification run: "
                    f"outcome=succeeded; run_directory={latest.run_directory.name}; "
                    "the run sealed; submit the "
                    "verification artifact referencing it"
                )
            return message
        if latest.status.state is not RunState.FAILED:
            return message
        capture = latest.status.output_capture
        stderr_truncated: bool | None
        if capture is None:
            stderr_tail = "<sealed stderr capture unavailable>"
            stderr_truncated = None
        else:
            stderr_truncated = capture.stderr.truncated
            try:
                stderr_tail = (
                    (latest.run_directory / capture.stderr.relative_path)
                    .read_bytes()[-FAILED_DETACHED_RUN_STDERR_TAIL_BYTES:]
                    .decode("utf-8", errors="replace")
                )
            except OSError:
                stderr_tail = "<sealed stderr capture unavailable>"
        failure_classification = latest.status.failure_classification
        return (
            f"{message}\n\nCurrent failed detached run: "
            f"outcome=failed; run_directory={latest.run_directory.name}; "
            f"exit_code={latest.status.exit_code}; "
            "failure_classification="
            f"{failure_classification.value if failure_classification is not None else 'unknown'}; "
            f"sealed_stderr_truncated={json.dumps(stderr_truncated)}; "
            f"sealed_stderr_tail={json.dumps(stderr_tail)}"
        )

    def _bounded_wake_message(self, message: str) -> str:
        encoded = message.encode("utf-8")
        if len(encoded) <= MAX_WAKE_MESSAGE_BYTES:
            return message

        # The long recovery instructions are useful context, but the run
        # identity and imperative at the end are the information the owner
        # cannot reconstruct safely after a truncation.  Keep those fields as
        # required fragments and spend the remaining budget on prose.
        required: list[str] = []
        for pattern in (
            r"run_directory=[^;\n]+",
            r"outcome=(?:succeeded|failed)",
            r"exit_code=[^;\n]+",
            r"failure_classification=[^;\n]+",
            r"sealed_stderr_truncated=[^;\n]+",
        ):
            required.extend(match.group(0) for match in re.finditer(pattern, message))
        for phrase in (
            "the run sealed; submit the verification artifact referencing it",
            "reuse its receipts; build and submit the implementation",
            "If provider/model/auth/capacity is blocked, surface the control-plane blocker exactly",
            "Otherwise rerun the verification stage from the authoritative state.",
        ):
            if phrase in message:
                required.append(phrase)
        unique_required = tuple(dict.fromkeys(required))
        required_message = "\n".join(unique_required)
        required_bytes = required_message.encode("utf-8")
        if len(required_bytes) >= MAX_WAKE_MESSAGE_BYTES:
            return required_bytes[:MAX_WAKE_MESSAGE_BYTES].decode("utf-8", errors="ignore")
        remaining = MAX_WAKE_MESSAGE_BYTES - len(required_bytes) - 8
        optional_prefix = encoded[: max(0, remaining)].decode("utf-8", errors="ignore")
        if optional_prefix:
            return f"{optional_prefix}\n...\n{required_message}"
        return required_message

    def _matching_current_phase_runs(
        self,
        state: AutoresearchState,
        *,
        state_reference_sha256: str,
        instruction_manifest_sha256: str,
    ) -> tuple[RunRecord, ...]:
        root = self.config.runs_root
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise AutoresearchRunRecordError(f"cannot inspect runs root: {exc}") from exc
        if not metadata or root.is_symlink() or not root.is_dir():
            raise AutoresearchRunRecordError("runs root must be a non-symlink directory")
        records: list[RunRecord] = []
        for directory, child_directories, files in os.walk(root, followlinks=False):
            child_directories.sort()
            files.sort()
            parent = Path(directory)
            if "manifest.json" not in files:
                continue
            try:
                record = read_run_record(run_dir=parent, runs_root=root)
            except AutoresearchRunRecordError:
                continue
            manifest = record.manifest
            if (
                manifest.phase is state.phase
                and manifest.iteration == state.iteration
                and manifest.state_reference_sha256 == state_reference_sha256
                and manifest.instruction_manifest_sha256 == instruction_manifest_sha256
            ):
                records.append(record)
        return tuple(sorted(records, key=lambda record: record.manifest.attempt))

    def _prepare_controller_lifecycle(
        self,
        state: AutoresearchState,
        *,
        shutdown_requested: ShutdownRequested,
    ) -> SupervisorResult | None:
        try:
            readiness = load_platform_readiness(self.config.readiness_manifest_path)
            context = AutoresearchValidationContext.from_readiness(readiness)
            policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
            migrated = migrate_legacy_autoresearch_workspace_state_file(
                self.config.state_path,
                policy=policy,
                validation_context=context,
            )
            if migrated != state:
                return SupervisorResult(SupervisorOutcome.NUDGED, "legacy_workspace_migrated")
            if state.phase is Phase.VERIFICATION and state.implementation_result is not None:
                sealed = seal_canonical_verification_dispatch_state_file(
                    self.config.state_path,
                    policy=policy,
                    validation_context=context,
                )
                require_canonical_verification_dispatch_attestation(
                    self.config.state_path,
                    policy=policy,
                    validation_context=context,
                    expected_state_reference_sha256=build_authoritative_state_reference(
                        sealed,
                        state_path=self.config.state_path,
                    ).sha256(),
                )
                provision_quantipy_experiment_runs_root()
                if sealed != state:
                    return SupervisorResult(
                        SupervisorOutcome.NUDGED,
                        "verification_dispatch_state_sealed",
                    )
            if state.phase is not Phase.REPEAT or state.final_decision is None:
                return None
            if (
                state.final_decision.decision is FinalDecision.INFRA_BLOCKED
                and not state.final_decision.continue_loop
            ):
                raise AutoresearchValidationError(
                    "INFRA_BLOCKED final decisions require continue_loop=true"
                )
            if not state.final_decision.continue_loop:
                return None
            if state.final_decision.memory_write_required and not state.memory_written:
                return None
            if state.final_decision.memory_write_required and not self._memory_wake_acknowledged(
                state
            ):
                return None
            receipts = build_receipt_catalog(
                Path(state.setup.target_repo)
                if state.setup is not None
                else self.config.target_repo
            )
            instruction_manifest_sha256 = expected_instruction_manifest_sha256(
                state,
                policy,
                receipts,
                state_path=self.config.state_path,
            )
            next_state = start_next_iteration(state, readiness=readiness)
            persist_next_iteration_state(
                self.config.state_path,
                self.config.state_path,
                state,
                next_state,
                instruction_manifest_sha256=instruction_manifest_sha256,
                policy=policy,
                receipt_catalog_factory=lambda: build_receipt_catalog(
                    Path(state.setup.target_repo)
                    if state.setup is not None
                    else self.config.target_repo
                ),
            )
            proof = self._rpc.wake(
                message=self._with_campaign_review_advisory(next_state, WAKE_MESSAGE),
                idempotency_key=make_idempotency_key(
                    purpose="repeat-successor",
                    material=build_authoritative_state_reference(
                        next_state,
                        state_path=self.config.state_path,
                    ).sha256(),
                ),
                shutdown_requested=shutdown_requested,
            )
            del proof
            return SupervisorResult(
                SupervisorOutcome.NUDGED,
                "repeat_successor_started",
                sent_wake=True,
            )
        except (AutoresearchValidationError, ValueError, OSError, SupervisorError) as exc:
            self._persistent_control_plane_alert(
                key=f"controller-lifecycle:{state.iteration}:{state.phase.value}",
                reason=f"controller_lifecycle_failed: {exc}",
            )
            return None

    def _finalize_required_memory(
        self,
        state: AutoresearchState,
        *,
        shutdown_requested: ShutdownRequested,
    ) -> SupervisorResult | None:
        if self._memory_wake_acknowledged(state):
            return None
        if not state.memory_written:
            if not can_write_memory(state):
                return None
            try:
                readiness = load_platform_readiness(self.config.readiness_manifest_path)
                policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
                finalized = finalize_repeat_memory_state_file(
                    self.config.state_path,
                    policy=policy,
                    validation_context=AutoresearchValidationContext.from_readiness(readiness),
                )
            except ValueError as exc:
                _structured_log(
                    logging.ERROR, "supervisor.memory_finalization_failed", detail=str(exc)
                )
                return SupervisorResult(
                    SupervisorOutcome.ALERT, f"memory_finalization_failed: {exc}"
                )
        else:
            finalized = state
        receipt = finalized.memory_verification_receipt
        experiment_id = receipt.experiment_id if receipt is not None else "<missing-receipt>"
        try:
            proof = self._rpc.wake(
                message=self._with_campaign_review_advisory(
                    finalized, FINALIZED_MEMORY_WAKE_MESSAGE
                ),
                idempotency_key=make_idempotency_key(
                    purpose="memory-finalized",
                    material=memory_wake_acknowledgement_key(finalized),
                ),
                shutdown_requested=shutdown_requested,
            )
            self._acknowledge_memory_owner_wake(finalized, proof)
        except SupervisorError as exc:
            _structured_log(logging.ERROR, "supervisor.memory_owner_wake_failed", detail=str(exc))
            return SupervisorResult(
                SupervisorOutcome.ALERT, "memory_finalized_owner_wake_retryable"
            )
        _structured_log(
            logging.INFO,
            "supervisor.memory_finalized",
            iteration=finalized.iteration,
            experiment_id=experiment_id,
        )
        return SupervisorResult(
            SupervisorOutcome.FINALIZED,
            "memory_finalized_owner_wake_sent",
            sent_wake=True,
        )

    def _memory_wake_acknowledged(self, state: AutoresearchState) -> bool:
        if not state.memory_written or state.memory_verification_receipt is None:
            return False
        try:
            acknowledgement_key = memory_wake_acknowledgement_key(state)
        except SupervisorError:
            return False
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            return acknowledgement_key in checkpoint.memory_wake_acknowledgements

    def _acknowledge_memory_owner_wake(
        self,
        expected_state: AutoresearchState,
        proof: WakeDeliveryProof,
    ) -> None:
        acknowledgement_key = memory_wake_acknowledgement_key(expected_state)
        with self._checkpoint_lock():
            current_state = load_state_file(self.config.state_path)
            current_decision = current_state.final_decision
            current_receipt = current_state.memory_verification_receipt
            expected_decision = expected_state.final_decision
            expected_receipt = expected_state.memory_verification_receipt
            if current_state.iteration <= expected_state.iteration and (
                current_decision is None
                or current_receipt is None
                or expected_decision is None
                or expected_receipt is None
                or current_state.iteration != expected_state.iteration
                or current_decision.experiment_id != expected_decision.experiment_id
                or current_receipt.experiment_id != expected_receipt.experiment_id
                or current_receipt.verified_rows_digest != expected_receipt.verified_rows_digest
            ):
                raise SupervisorError(
                    "memory owner wake acknowledgement no longer matches the finalized state"
                )
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            checkpoint.memory_wake_acknowledgements[acknowledgement_key] = (
                MemoryWakeAcknowledgement(
                    status=proof.status,
                    acknowledged_at=self._now(),
                    run_id=proof.run_id,
                    cached_terminal=proof.cached_terminal,
                )
            )
            checkpoint.save(self.config.checkpoint_path)

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
                except SupervisorCheckpointError as exc:
                    _structured_log(
                        logging.ERROR,
                        "supervisor.poll_failed_closed",
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
            self._persistent_control_plane_alert(
                key=f"run-record-current-instruction:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=f"cannot_compute_current_instruction_manifest: {exc}",
            )
            return None
        try:
            matching = self._matching_verification_runs(
                iteration=state.iteration,
                state_reference_sha256=state_reference_sha256,
                instruction_manifest_sha256=instruction_manifest_sha256,
            )
        except AutoresearchRunRecordError as exc:
            self._persistent_control_plane_alert(
                key=f"run-record:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=f"invalid_detached_run_record: {exc}",
            )
            return None
        if not matching:
            return None
        latest = matching[-1]
        if latest.status.state is RunState.RUNNING:
            return SupervisorResult(SupervisorOutcome.NO_ACTION, "active_matching_detached_run")
        if latest.status.state is RunState.SUCCEEDED:
            return None
        if latest.status.failure_classification is RunFailureClassification.OPERATOR_STOPPED:
            self._persistent_control_plane_alert(
                key=(
                    "interrupted-detached-verification:"
                    f"{state.iteration}:{state.phase.value}:{state_reference_sha256}"
                ),
                reason="interrupted_detached_verification_requires_operator_recovery",
            )
            return None
        if (
            latest.status.expected_artifact_attestation_status
            is not ExpectedArtifactAttestationStatus.FAILED
            or latest.status.expected_artifact_attestation_error
            is not ExpectedArtifactAttestationError.MISSING
        ):
            self._persistent_control_plane_alert(
                key=f"run-record-attestation:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=(
                    "detached_verification_failure_cannot_form_execution-interrupted evidence: "
                    "expected run.json attestation is not FAILED/MISSING"
                ),
            )
            return None
        if state.mode is not ResearchMode.ALPHA_RESEARCH:
            self._persistent_control_plane_alert(
                key=f"run-record-mode:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=(
                    "detached_verification_failure_cannot_form_strict_artifact: "
                    f"mode={state.mode.value if state.mode is not None else 'null'}"
                ),
            )
            return None
        try:
            artifact = self._verification_failure_artifact(latest, state)
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
                runs_root=self.config.runs_root,
            )
        except (AutoresearchValidationError, ValueError, OSError) as exc:
            self._persistent_control_plane_alert(
                key=f"run-record-advance:{state.iteration}:{state.phase.value}:{state_reference_sha256}",
                reason=f"detached_verification_failure_not_advanced: {exc}",
            )
            return None
        return SupervisorResult(
            SupervisorOutcome.NUDGED,
            "detached_verification_failure_advanced",
        )

    def _consume_launch_request_inbox(self) -> SupervisorResult | None:
        try:
            inbox_fd = os.open(
                self.config.launch_requests_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            _structured_log(
                logging.WARNING,
                "supervisor.launch_request_rejected",
                request="<inbox>",
                reason=f"launch request inbox unavailable: {exc}",
            )
            return None

        rejected_fd: int | None = None
        accepted_fd: int | None = None

        def log_rejection(name: str, reason: str) -> None:
            _structured_log(
                logging.WARNING,
                "supervisor.launch_request_rejected",
                request=name,
                reason=reason,
            )

        def reject(
            name: str,
            reason: str,
            *,
            destination_name: str | None = None,
            expected_stat: os.stat_result | None = None,
            source_fd: int | None = None,
        ) -> bool:
            nonlocal rejected_fd
            try:
                if rejected_fd is None:
                    rejected_fd = _open_launch_request_child_directory(inbox_fd, "rejected")
                _move_launch_request_no_replace(
                    name,
                    src_dir_fd=inbox_fd,
                    dst_dir_fd=rejected_fd,
                    destination_name=destination_name,
                    expected_stat=expected_stat,
                    source_fd=source_fd,
                )
            except (OSError, SupervisorError, ValueError) as exc:
                log_rejection(name, f"{reason}; quarantine failed: {exc}")
                return False
            log_rejection(name, reason)
            return True

        def validate_request(
            name: str,
        ) -> tuple[str | None, Path | None, Path | None]:
            try:
                metadata = os.stat(name, dir_fd=inbox_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None, None, None
            except OSError as exc:
                return f"cannot inspect request: {exc}", None, None
            if not stat.S_ISREG(metadata.st_mode):
                return "request must be a non-symlink regular file", None, None
            if metadata.st_nlink != 1:
                return "request must have exactly one hard link", None, None
            if metadata.st_size > LAUNCH_REQUEST_MAX_BYTES:
                return f"request exceeds {LAUNCH_REQUEST_MAX_BYTES} bytes", None, None
            try:
                request_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=inbox_fd,
                )
            except OSError as exc:
                return f"cannot open request: {exc}", None, None
            try:
                opened = os.fstat(request_fd)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_nlink,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_nlink,
                ):
                    return "request changed during inspection", None, None
                raw_bytes = os.read(request_fd, LAUNCH_REQUEST_MAX_BYTES + 1)
            except OSError as exc:
                return f"cannot read request: {exc}", None, None
            finally:
                os.close(request_fd)
            if len(raw_bytes) > LAUNCH_REQUEST_MAX_BYTES:
                return f"request exceeds {LAUNCH_REQUEST_MAX_BYTES} bytes", None, None
            try:
                raw_request = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return f"request is not valid JSON: {exc}", None, None
            if not isinstance(raw_request, dict):
                return "request JSON must be an object", None, None
            request: dict[str, object] = raw_request
            schema_version = request.get("schema_version")
            if isinstance(schema_version, bool) or schema_version != LAUNCH_REQUEST_SCHEMA_VERSION:
                return "request has an unsupported schema_version", None, None
            run_dir_value = request.get("run_dir")
            runs_root_value = request.get("runs_root")
            if not isinstance(run_dir_value, str) or not isinstance(runs_root_value, str):
                return "request paths must be strings", None, None
            run_dir = Path(run_dir_value)
            runs_root = Path(runs_root_value)
            if not run_dir.is_absolute() or not runs_root.is_absolute():
                return "request paths must be absolute", None, None
            if runs_root != self.config.runs_root:
                return "request runs_root does not match the configured long-runs root", None, None
            try:
                configured_root = self.config.runs_root.resolve(strict=True)
                resolved_run_dir = run_dir.resolve(strict=True)
            except (FileNotFoundError, ValueError):
                return "run directory is missing", None, None
            except OSError as exc:
                return f"cannot resolve run directory: {exc}", None, None
            if (
                resolved_run_dir == configured_root
                or configured_root not in resolved_run_dir.parents
            ):
                return "run directory must be strictly under runs_root", None, None
            try:
                run_metadata = run_dir.stat()
            except (FileNotFoundError, ValueError):
                return "run directory is missing", None, None
            except OSError as exc:
                return f"cannot inspect run directory: {exc}", None, None
            if not stat.S_ISDIR(run_metadata.st_mode):
                return "run directory must be a directory", None, None
            manifest_path = run_dir / "manifest.json"
            try:
                manifest_metadata = manifest_path.stat()
            except (FileNotFoundError, ValueError):
                return "run directory manifest.json is missing", None, None
            except OSError as exc:
                return f"cannot inspect run manifest: {exc}", None, None
            if not stat.S_ISREG(manifest_metadata.st_mode) or manifest_path.is_symlink():
                return "run manifest must be a non-symlink regular file", None, None
            status_path = run_dir / "status.json"
            try:
                status_path.lstat()
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                return f"cannot inspect run status: {exc}", None, None
            else:
                return "run directory already contains status.json", None, None
            return None, run_dir, runs_root

        try:
            _validate_launch_request_directory_fd(inbox_fd, label="launch request inbox")

            def candidate_mtime(name: str) -> float:
                try:
                    return self._mtime(self.config.launch_requests_path / name)
                except (FileNotFoundError, OSError, ValueError):
                    return float("inf")

            candidates = sorted(
                (name for name in os.listdir(inbox_fd) if name.endswith(".json")),
                key=lambda name: (candidate_mtime(name), name),
            )
            for name in candidates:
                request_metadata: os.stat_result | None = None
                request_fd: int | None = None
                try:
                    request_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=inbox_fd,
                    )
                    request_metadata = os.fstat(request_fd)
                    request_age = (
                        self._now() - request_metadata.st_mtime
                        if stat.S_ISREG(request_metadata.st_mode)
                        else None
                    )
                except (FileNotFoundError, OSError, ValueError):
                    request_age = None
                    if request_fd is not None:
                        os.close(request_fd)
                        request_fd = None
                if request_age is not None and request_age > (
                    self.config.launch_request_ttl_minutes * 60.0
                ):
                    stale_reason = (
                        f"stale launch request age_seconds={request_age:.3f}; "
                        f"ttl_minutes={self.config.launch_request_ttl_minutes}"
                    )
                    archived = reject(
                        name,
                        stale_reason,
                        destination_name=f".stale-{name}",
                        expected_stat=request_metadata,
                        source_fd=request_fd,
                    )
                    if request_fd is not None:
                        os.close(request_fd)
                        request_fd = None
                    if not archived:
                        _structured_log(
                            logging.WARNING,
                            "supervisor.launch_request_stale",
                            request=name,
                            age_seconds=request_age,
                            reason=f"{stale_reason}; archive failed",
                        )
                    else:
                        _structured_log(
                            logging.WARNING,
                            "supervisor.launch_request_stale",
                            request=name,
                            age_seconds=request_age,
                            ttl_minutes=self.config.launch_request_ttl_minutes,
                            archived_as=f".stale-{name}",
                        )
                    continue
                if request_fd is not None:
                    os.close(request_fd)
                try:
                    reason, run_dir, runs_root = validate_request(name)
                except (OSError, SupervisorError, ValueError) as exc:
                    log_rejection(name, f"request validation failed: {exc}")
                    continue
                if reason is not None:
                    reject(name, reason)
                    continue
                if run_dir is None or runs_root is None:
                    continue
                repo_root = Path(__file__).resolve().parents[1]
                try:
                    completed = subprocess.run(
                        [
                            "bash",
                            str(repo_root / "scripts" / "run-long-task.sh"),
                            "--launch-prepared",
                            "--run-dir",
                            str(run_dir),
                            "--runs-root",
                            str(runs_root),
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=LAUNCH_REQUEST_TIMEOUT_SECONDS,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    reject_reason = f"launch_request_execution_failed: {exc}"
                    try:
                        if rejected_fd is None:
                            rejected_fd = _open_launch_request_child_directory(inbox_fd, "rejected")
                        _move_launch_request_no_replace(
                            name,
                            src_dir_fd=inbox_fd,
                            dst_dir_fd=rejected_fd,
                        )
                    except (OSError, SupervisorError, ValueError) as quarantine_exc:
                        reject_reason = f"{reject_reason}; quarantine failed: {quarantine_exc}"
                    _structured_log(
                        logging.ERROR,
                        "supervisor.launch_request_failed",
                        request=name,
                        detail=reject_reason,
                    )
                    self._persistent_control_plane_alert(
                        key=f"launch-request:{name}",
                        reason=reject_reason,
                    )
                    continue
                if completed.returncode != 0:
                    stdout = str(completed.stdout or "")[-1000:]
                    stderr = str(completed.stderr or "")[-1000:]
                    reject_reason = (
                        "launch_request_execution_failed: "
                        f"returncode={completed.returncode}; stdout={stdout}; stderr={stderr}"
                    )
                    try:
                        if rejected_fd is None:
                            rejected_fd = _open_launch_request_child_directory(inbox_fd, "rejected")
                        _move_launch_request_no_replace(
                            name,
                            src_dir_fd=inbox_fd,
                            dst_dir_fd=rejected_fd,
                        )
                    except (OSError, SupervisorError, ValueError) as quarantine_exc:
                        reject_reason = f"{reject_reason}; quarantine failed: {quarantine_exc}"
                    _structured_log(
                        logging.ERROR,
                        "supervisor.launch_request_failed",
                        request=name,
                        returncode=completed.returncode,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    self._persistent_control_plane_alert(
                        key=f"launch-request:{name}",
                        reason=reject_reason,
                    )
                    continue
                try:
                    if accepted_fd is None:
                        accepted_fd = _open_launch_request_child_directory(inbox_fd, "accepted")
                    _move_launch_request_no_replace(
                        name,
                        src_dir_fd=inbox_fd,
                        dst_dir_fd=accepted_fd,
                    )
                except (OSError, SupervisorError, ValueError) as exc:
                    log_rejection(name, f"launch executed; acceptance quarantine failed: {exc}")
                    return None
                _structured_log(
                    logging.INFO,
                    "supervisor.launch_request_executed",
                    request=name,
                    run_dir=str(run_dir),
                )
                return SupervisorResult(SupervisorOutcome.NUDGED, "launch_request_executed")
            return None
        except (OSError, SupervisorError, ValueError) as exc:
            _structured_log(
                logging.WARNING,
                "supervisor.launch_request_rejected",
                request="<inbox>",
                reason=f"launch request inbox processing failed: {exc}",
            )
            return None
        finally:
            if accepted_fd is not None:
                os.close(accepted_fd)
            if rejected_fd is not None and rejected_fd != accepted_fd:
                os.close(rejected_fd)
            os.close(inbox_fd)

    def _consume_stage_submission_inbox(
        self,
        state: AutoresearchState,
    ) -> SupervisorResult | None:
        try:
            candidates = [
                entry
                for entry in os.listdir(self.config.stage_inbox_path)
                if entry.endswith(".json")
            ]
        except OSError:
            return None
        if not candidates:
            return None
        try:
            readiness = load_platform_readiness(self.config.readiness_manifest_path)
            context = AutoresearchValidationContext.from_readiness(readiness)
            advanced = consume_stage_submission_inbox(
                state_path=self.config.state_path,
                output_path=self.config.state_path,
                inbox_path=self.config.stage_inbox_path,
                openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
                quantipy_root=(
                    Path(state.setup.target_repo)
                    if state.setup is not None
                    else self.config.target_repo
                ),
                validation_context=context,
            )
        except (AutoresearchValidationError, ValueError, OSError) as exc:
            self._persistent_control_plane_alert(
                key=f"stage-inbox:{state.iteration}:{state.phase.value}",
                reason=f"stage_submission_inbox_invalid: {exc}",
            )
            return None
        if advanced is None:
            return None
        return SupervisorResult(
            SupervisorOutcome.NUDGED,
            "stage_submission_advanced",
        )

    def _rotate_owner_session_for_wake(self, *, phase: str) -> None:
        """Drop the owner session mapping so the next wake starts fresh.

        Session history is disposable by design — everything authoritative
        lives in the state file and every wake begins with autoresearch-next
        — so a fresh thread per wake keeps each turn near minimal context
        instead of replaying the accumulated transcript. Called only from
        wake paths, after the activity and lifecycle guards have proven no
        owner turn is in flight; a lingering "running" record skips rotation
        as a final belt-and-braces check. Store writes follow OpenClaw's
        sessions lock protocol to avoid racing gateway persistence.
        """
        sessions_path = self.config.owner_sessions_path
        if sessions_path.is_symlink():
            _structured_log(
                logging.WARNING,
                "supervisor.owner_session_rotation_skipped",
                reason="owner session store is a symlink",
            )
            return
        lock_path = sessions_path.with_name(sessions_path.name + ".lock")
        lock_fd: int | None = None
        try:
            lock_fd = self._acquire_session_store_lock(lock_path)
            if lock_fd is None:
                _structured_log(
                    logging.WARNING,
                    "supervisor.owner_session_rotation_skipped",
                    reason="session store lock is held",
                )
                return
            try:
                raw = json.loads(
                    sessions_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_strict_json_object,
                )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                _structured_log(
                    logging.WARNING,
                    "supervisor.owner_session_rotation_skipped",
                    reason=f"cannot read owner session store: {exc}",
                )
                return
            if not isinstance(raw, dict) or AUTORESEARCH_OWNER_SESSION_KEY not in raw:
                return
            entry = raw[AUTORESEARCH_OWNER_SESSION_KEY]
            if isinstance(entry, Mapping) and entry.get("status") == "running":
                _structured_log(
                    logging.WARNING,
                    "supervisor.owner_session_rotation_skipped",
                    reason="owner session reports a running turn",
                )
                return
            removed = raw.pop(AUTORESEARCH_OWNER_SESSION_KEY)
            session_id = removed.get("sessionId") if isinstance(removed, Mapping) else None
            try:
                mode = stat.S_IMODE(sessions_path.stat().st_mode)
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{sessions_path.name}.rotate.",
                    dir=sessions_path.parent,
                )
                try:
                    os.write(descriptor, json.dumps(raw, indent=2).encode("utf-8"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.chmod(temp_name, mode)
                os.replace(temp_name, sessions_path)
            except OSError as exc:
                _structured_log(
                    logging.WARNING,
                    "supervisor.owner_session_rotation_skipped",
                    reason=f"cannot rewrite owner session store: {exc}",
                )
                return
            _structured_log(
                logging.INFO,
                "supervisor.owner_session_rotated",
                phase=phase,
                session_id=session_id,
            )
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                with suppress(OSError):
                    lock_path.unlink()

    def _acquire_session_store_lock(self, lock_path: Path) -> int | None:
        """Acquire OpenClaw's exclusive session store lock, or None if held.

        Mirrors the gateway's protocol: O_CREAT|O_EXCL lock file carrying
        {pid, createdAt}, with stale takeover after 30 seconds.
        """
        for _attempt in range(2):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    age = self._now() - lock_path.stat().st_mtime
                except OSError:
                    continue
                if age > 30.0:
                    with suppress(OSError):
                        lock_path.unlink()
                    continue
                return None
            except OSError:
                return None
            with suppress(OSError):
                os.write(
                    fd,
                    json.dumps({"pid": os.getpid(), "createdAt": int(self._now() * 1000)}).encode(
                        "utf-8"
                    ),
                )
            return fd
        return None

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
        status_path = run_dir / "status.json"
        if not status_path.is_symlink() and not status_path.exists():
            # A valid manifest with no status file is a prepared run awaiting
            # its queued launch, not a malformed record.
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

    def _verification_failure_artifact(
        self,
        record: RunRecord,
        state: AutoresearchState,
    ) -> VerificationResultArtifact:
        implementation = state.implementation_result
        if implementation is None:
            raise AutoresearchValidationError(
                "execution-interrupted evidence requires implementation_result"
            )
        from gateway.autoresearch.recovery_receipts import (
            _expected_quantipy_verification_run_id,
        )

        expected_run_id = _expected_quantipy_verification_run_id(
            state,
            implementation.commit_sha,
        )
        expected_run_json_path = (
            DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_run_id / "run.json"
        )
        if record.status.finished_at is None:
            raise AutoresearchValidationError(
                "execution-interrupted evidence requires a terminal finish timestamp"
            )
        started_at = datetime.fromisoformat(record.status.started_at.replace("Z", "+00:00"))
        finished_at = datetime.fromisoformat(record.status.finished_at.replace("Z", "+00:00"))
        status_sha256 = hashlib.sha256(
            json.dumps(record.status.to_dict(), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        capture = record.status.output_capture
        if capture is None:
            raise AutoresearchValidationError(
                "execution-interrupted evidence requires sealed output capture"
            )
        if record.status.exit_code is None:
            raise AutoresearchValidationError(
                "execution-interrupted evidence requires a terminal exit code"
            )
        if record.status.failure_classification is None:
            raise AutoresearchValidationError(
                "execution-interrupted evidence requires a terminal failure classification"
            )
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
        interrupted = QuantipyExecutionInterruptedEvidence(
            expected_run_id=expected_run_id,
            expected_run_json_path=str(expected_run_json_path),
            manifest_path=implementation.experiment_manifest_path,
            manifest_sha256=implementation.experiment_manifest_sha256,
            detached_run_directory=str(record.run_directory),
            detached_manifest_sha256=record.status.manifest_sha256,
            detached_status_sha256=status_sha256,
            exit_code=record.status.exit_code,
            signal_number=record.status.signal_number,
            failure_classification=(
                record.status.failure_classification.value
                if record.status.failure_classification is not None
                else ""
            ),
            timeout_seconds=record.manifest.timeout_seconds,
            wall_seconds_observed=(finished_at - started_at).total_seconds(),
            stdout_sha256=capture.stdout.sha256,
            stdout_bytes_observed=capture.stdout.bytes_observed,
            stdout_truncated=capture.stdout.truncated,
            stderr_sha256=capture.stderr.sha256,
            stderr_bytes_observed=capture.stderr.bytes_observed,
            stderr_truncated=capture.stderr.truncated,
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
            quantipy_execution_interrupted=interrupted,
        )

    def _persistent_control_plane_alert(self, *, key: str, reason: str) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = checkpoint.recovery_records.setdefault(key, RecoveryRecord())
            if record.alerted:
                return
            record.status = RecoveryStatus.EXHAUSTED
            record.last_error = reason[:1000]
            record.alerted = True
            checkpoint.save(self.config.checkpoint_path)
        _structured_log(
            logging.ERROR,
            "supervisor.control_plane_advisory",
            recovery_key=key,
            reason=reason,
        )

    def _is_terminal_state(self, state: AutoresearchState) -> bool:
        decision = state.final_decision
        return (
            state.phase is Phase.REPEAT
            and decision is not None
            and decision.decision is not FinalDecision.INFRA_BLOCKED
            and not decision.continue_loop
            and (
                not decision.memory_write_required
                or (state.memory_written and self._memory_wake_acknowledged(state))
            )
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

    def _active_owner_task(self, tasks: ReconciledRunningTasks) -> bool:
        return any(
            classify_autoresearch_task(task) is TaskProvenance.OWNER_TURN
            for task in tasks.running_tasks
        )

    def _staleness_watchdog_due(
        self,
        state: AutoresearchState,
        probe: StateProbe,
        *,
        owner_task_active: bool,
    ) -> bool:
        if self._is_terminal_state(state) or owner_task_active:
            return False
        if self._now() - probe.latest_update_ts <= (
            self.config.staleness_force_wake_minutes * 60.0
        ):
            return False
        return not self._has_running_detached_run(state.iteration)

    def _has_running_detached_run(self, iteration: int) -> bool:
        root = self.config.runs_root
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if not metadata or root.is_symlink() or not root.is_dir():
            return True
        for directory, child_directories, files in os.walk(root, followlinks=False):
            child_directories.sort()
            files.sort()
            parent = Path(directory)
            if "manifest.json" not in files:
                continue
            try:
                record = read_run_record(run_dir=parent, runs_root=root)
            except AutoresearchRunRecordError:
                continue
            if record.manifest.iteration == iteration and record.status.state is RunState.RUNNING:
                return True
        return False

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
        if status is None:
            # The gateway only writes a top-level status when a run ends
            # cleanly; an aborted turn leaves the key absent, which means no
            # run is active — not a malformed record.
            return None
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
        state_fingerprint: str | None = None,
        state_mtime: float,
        owner_task_active: bool,
        force: bool = False,
    ) -> RecoveryClaim | SupervisorResult | None:
        del state_mtime  # State change is compared by fingerprint, not wall-clock time.
        observed_fingerprint = state_fingerprint or recovery_key
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            condition_record = checkpoint.recovery_records.setdefault(
                recovery_key, RecoveryRecord()
            )
            now = self._now()
            monotonic_now = self._monotonic()

            def elapsed_since(
                record: RecoveryRecord,
                *,
                record_key: str,
                field: str,
            ) -> float:
                anchor = cast(float | None, getattr(record, field))
                if anchor is None:
                    return 0.0
                clock_record = checkpoint.recovery_records.get(
                    f"{MONOTONIC_RECOVERY_KEY_PREFIX}{record_key}"
                )
                monotonic_anchor = (
                    clock_record.woke_at
                    if field == "woke_at" and clock_record is not None
                    else clock_record.failed_at
                    if field == "failed_at" and clock_record is not None
                    else None
                )
                if monotonic_anchor is not None and monotonic_now >= monotonic_anchor:
                    return monotonic_now - monotonic_anchor
                return max(0.0, now - anchor)

            recovered_error = _preferred_recovery_error(
                detected_error,
                _detect_recovery_error_in_text(condition_record.last_error)
                if condition_record.last_error is not None
                else None,
            )
            renudge_idle_seconds: float | None = None
            escalation_count = 0
            ordinary_recovery_cold_start = force and condition_record.woke_at is None

            # A watchdog uses a separate record.  It is intentionally not the
            # ordinary wake record: ordinary 20-minute escalation and watchdog
            # settling are independent clocks.
            watchdog_key = f"{WATCHDOG_RECOVERY_KEY_PREFIX}{recovery_key}"
            ordinary_renudge_due = False
            if condition_record.status is RecoveryStatus.SUCCEEDED:
                last_wake_elapsed = elapsed_since(
                    condition_record,
                    record_key=recovery_key,
                    field="woke_at",
                )
                if last_wake_elapsed >= self.config.grace_period_seconds:
                    last_nudge_elapsed = elapsed_since(
                        condition_record,
                        record_key=recovery_key,
                        field="woke_at",
                    )
                    key_fingerprint = recovery_key.rsplit(":", 1)[-1]
                    unchanged = key_fingerprint == observed_fingerprint
                    ordinary_renudge_due = (
                        not recovered_error
                        and not owner_task_active
                        and unchanged
                        and last_nudge_elapsed >= self.config.renudge_escalation_minutes * 60.0
                    )

            record_key = watchdog_key if force else recovery_key
            record = (
                checkpoint.recovery_records.setdefault(record_key, RecoveryRecord())
                if force
                else condition_record
            )
            watchdog_claim = force
            watchdog_settling_reason: str | None = None
            if force:
                if record.status is RecoveryStatus.SUCCEEDED and record.woke_at is not None:
                    watchdog_elapsed = elapsed_since(
                        record,
                        record_key=record_key,
                        field="woke_at",
                    )
                    if watchdog_elapsed < self.config.staleness_force_wake_minutes * 60.0:
                        watchdog_settling_reason = "staleness_watchdog_settling"
                elif record.status is RecoveryStatus.FAILED and record.failed_at is not None:
                    # A failed watchdog wake settles on the same cadence as a
                    # successful one; without this throttle the watchdog would
                    # retry a failing wake on every poll, and at the cap it
                    # would alert, reset, and retry in the same poll.
                    watchdog_elapsed = elapsed_since(
                        record,
                        record_key=record_key,
                        field="failed_at",
                    )
                    if watchdog_elapsed < self.config.staleness_force_wake_minutes * 60.0:
                        watchdog_settling_reason = "staleness_watchdog_settling"
                if watchdog_settling_reason is not None:
                    watchdog_claim = False
                    if not ordinary_renudge_due:
                        return SupervisorResult(
                            SupervisorOutcome.NO_ACTION,
                            watchdog_settling_reason,
                            recovery_key,
                        )
                elif record.status is RecoveryStatus.FAILED:
                    if record.attempt_count >= self.config.max_recovery_attempts:
                        self._alert(
                            checkpoint,
                            record,
                            record_key,
                            "staleness_watchdog_recovery_capped",
                        )
                        # This advisory is logging-only. Reset the bounded
                        # attempt counter so a persistent outage can be
                        # retried on the next watchdog cadence.
                        record.attempt_count = 0
                        checkpoint.save(self.config.checkpoint_path)
            if not watchdog_claim:
                record_key = recovery_key
                record = condition_record
                if condition_record.status is RecoveryStatus.SUCCEEDED and condition_record.woke_at:
                    elapsed = elapsed_since(
                        condition_record,
                        record_key=recovery_key,
                        field="woke_at",
                    )
                    if elapsed < self.config.grace_period_seconds:
                        return SupervisorResult(
                            SupervisorOutcome.NO_ACTION, "recovery_settling", recovery_key
                        )
                    key_fingerprint = recovery_key.rsplit(":", 1)[-1]
                    unchanged = key_fingerprint == observed_fingerprint
                    idle_seconds = elapsed_since(
                        condition_record,
                        record_key=recovery_key,
                        field="woke_at",
                    )
                    if owner_task_active or not unchanged:
                        return SupervisorResult(
                            SupervisorOutcome.NO_ACTION,
                            "recovery_nudge_deduped",
                            recovery_key,
                        )
                    if recovered_error is None and idle_seconds < (
                        self.config.renudge_escalation_minutes * 60.0
                    ):
                        return SupervisorResult(
                            SupervisorOutcome.NO_ACTION,
                            "recovery_nudge_deduped",
                            recovery_key,
                        )
                    if recovered_error is None:
                        if condition_record.renudge_count >= self.config.max_escalating_renudges:
                            self._renudge_limit_alert(
                                checkpoint,
                                condition_record,
                                recovery_key,
                                state,
                            )
                            return SupervisorResult(
                                SupervisorOutcome.NO_ACTION,
                                "recovery_escalation_capped",
                                recovery_key,
                            )
                        renudge_idle_seconds = idle_seconds
                        escalation_count = condition_record.renudge_count + 1
                    else:
                        # A retained auth/capacity signature gets a retry
                        # cadence, not a permanent exhausted state.
                        if idle_seconds < self.config.grace_period_seconds:
                            return SupervisorResult(
                                SupervisorOutcome.NO_ACTION,
                                "recovery_error_settling",
                                recovery_key,
                            )
                        renudge_idle_seconds = None
                        escalation_count = 0
                else:
                    renudge_idle_seconds = None
                    escalation_count = 0

            if record.status is RecoveryStatus.EXHAUSTED and not watchdog_claim:
                # Older checkpoints used EXHAUSTED for mechanical wake
                # failures. Advisories are not a recovery latch.
                record.status = RecoveryStatus.FAILED
                record.attempt_count = 0
                if record.failed_at is None:
                    record.failed_at = now
                checkpoint.save(self.config.checkpoint_path)

            if record.status is RecoveryStatus.IN_FLIGHT:
                if record.claim_started_at is None or record.claim_pid is None:
                    raise SupervisorError(
                        f"in-flight recovery claim lacks owner metadata: {recovery_key}"
                    )
                age = max(0.0, now - record.claim_started_at)
                if age < self.config.claim_stale_seconds:
                    return SupervisorResult(
                        SupervisorOutcome.NO_ACTION,
                        "recovery_in_flight",
                        recovery_key,
                    )
                if self._claim_owner_alive(record):
                    self._alert(
                        checkpoint,
                        record,
                        record_key,
                        "stale_recovery_claim_owner_alive",
                    )
                    return SupervisorResult(
                        SupervisorOutcome.NO_ACTION,
                        "recovery_in_flight",
                        recovery_key,
                    )

            if (
                record.status is RecoveryStatus.FAILED
                and record.failed_at is not None
                and not watchdog_claim
                and elapsed_since(record, record_key=record_key, field="failed_at")
                < self.config.grace_period_seconds
            ):
                return SupervisorResult(
                    SupervisorOutcome.NO_ACTION,
                    (
                        "recovery_error_settling"
                        if recovered_error is not None
                        else "recovery_failed_settling"
                    ),
                    recovery_key,
                )

            if (
                record.status is RecoveryStatus.FAILED
                and record.attempt_count >= self.config.max_recovery_attempts
                and not watchdog_claim
            ):
                self._alert(
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
                record.attempt_count = 0
                record.status = RecoveryStatus.FAILED
                record.failed_at = now
                clock_record = checkpoint.recovery_records.setdefault(
                    f"{MONOTONIC_RECOVERY_KEY_PREFIX}{record_key}",
                    RecoveryRecord(),
                )
                clock_record.failed_at = self._monotonic()
                checkpoint.save(self.config.checkpoint_path)
                return SupervisorResult(
                    SupervisorOutcome.NO_ACTION,
                    "recovery_escalation_capped",
                    recovery_key,
                )

            pid = os.getpid()
            identity = self._process_identity(pid)
            attempt_number = record.attempt_count + (1 if renudge_idle_seconds is None else 0)
            token = f"{pid}:{identity or 'unknown'}:{attempt_number}:{int(now * 1e9)}"
            record.status = RecoveryStatus.IN_FLIGHT
            if renudge_idle_seconds is None:
                record.attempt_count += 1
            record.claim_token = token
            record.claim_pid = pid
            record.claim_process_identity = identity
            record.claim_started_at = now
            record.failed_at = None
            record.last_error = None
            checkpoint.save(self.config.checkpoint_path)
            return RecoveryClaim(
                recovery_key,
                token,
                renudge_idle_seconds,
                escalation_count,
                watchdog_claim,
                observed_fingerprint,
                record_key,
                ordinary_recovery_cold_start,
            )

    def _complete_recovery_claim(self, claim: RecoveryClaim) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._owned_claim(checkpoint, claim)
            woke_at = self._now()
            monotonic_woke_at = self._monotonic()
            record.status = RecoveryStatus.SUCCEEDED
            record.woke_at = woke_at
            record.last_nudge_at = record.woke_at
            if claim.renudge_idle_seconds is not None and not claim.forced_by_staleness_watchdog:
                record.renudge_count = min(
                    record.renudge_count + 1,
                    self.config.max_escalating_renudges,
                )
            record.last_error = None
            clock_record = checkpoint.recovery_records.setdefault(
                f"{MONOTONIC_RECOVERY_KEY_PREFIX}{claim.record_key or claim.recovery_key}",
                RecoveryRecord(),
            )
            clock_record.status = RecoveryStatus.READY
            clock_record.woke_at = monotonic_woke_at
            clock_record.failed_at = None
            if claim.forced_by_staleness_watchdog and claim.ordinary_recovery_cold_start:
                ordinary_record = checkpoint.recovery_records.setdefault(
                    claim.recovery_key,
                    RecoveryRecord(),
                )
                ordinary_record.status = RecoveryStatus.SUCCEEDED
                ordinary_record.woke_at = woke_at
                ordinary_record.last_nudge_at = woke_at
                ordinary_record.failed_at = None
                ordinary_record.last_error = None
                ordinary_clock_record = checkpoint.recovery_records.setdefault(
                    f"{MONOTONIC_RECOVERY_KEY_PREFIX}{claim.recovery_key}",
                    RecoveryRecord(),
                )
                ordinary_clock_record.status = RecoveryStatus.READY
                ordinary_clock_record.woke_at = monotonic_woke_at
                ordinary_clock_record.failed_at = None
            checkpoint.save(self.config.checkpoint_path)

    def _fail_recovery_claim(self, claim: RecoveryClaim, error: BaseException) -> None:
        with self._checkpoint_lock():
            checkpoint = SupervisorCheckpoint.load(self.config.checkpoint_path)
            record = self._owned_claim(checkpoint, claim)
            record.status = RecoveryStatus.FAILED
            record.failed_at = self._now()
            record.last_error = f"{type(error).__name__}: {error}"[:1000]
            if not claim.forced_by_staleness_watchdog:
                clock_record = checkpoint.recovery_records.setdefault(
                    f"{MONOTONIC_RECOVERY_KEY_PREFIX}{claim.record_key or claim.recovery_key}",
                    RecoveryRecord(),
                )
                clock_record.failed_at = self._monotonic()
            checkpoint.save(self.config.checkpoint_path)

    def _owned_claim(
        self, checkpoint: SupervisorCheckpoint, claim: RecoveryClaim
    ) -> RecoveryRecord:
        record = checkpoint.recovery_records.get(claim.record_key or claim.recovery_key)
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
    ) -> bool:
        if record.alerted:
            return False
        record.alerted = True
        checkpoint.save(self.config.checkpoint_path)
        _structured_log(
            logging.ERROR,
            "supervisor.recovery_advisory",
            recovery_key=recovery_key,
            reason=reason,
        )
        return True

    def _renudge_limit_alert(
        self,
        checkpoint: SupervisorCheckpoint,
        record: RecoveryRecord,
        recovery_key: str,
        state: AutoresearchState,
    ) -> None:
        emitted = self._alert(
            checkpoint,
            record,
            recovery_key,
            (
                f"renudge_escalation_limit_reached: iteration={state.iteration}; "
                f"phase={state.phase.value}; recovery_key={recovery_key}; "
                f"renudge_count={record.renudge_count}; "
                f"max_escalating_renudges={self.config.max_escalating_renudges}"
            ),
        )
        if emitted:
            _structured_log(
                logging.ERROR,
                "supervisor.renudge_limit_reached",
                recovery_key=recovery_key,
                renudge_count=record.renudge_count,
                max_escalating_renudges=self.config.max_escalating_renudges,
            )

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

    def _process_started_at(self, process_dir: Path) -> float | None:
        try:
            raw = (process_dir / "stat").read_text(encoding="utf-8")
            closing = raw.rfind(")")
            fields = raw[closing + 1 :].split() if closing >= 0 else []
            start_ticks = int(fields[19]) if len(fields) > 19 else None
            boot_time = None
            for line in (self.config.proc_root / "stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("btime "):
                    boot_time = float(line.split()[1])
                    break
            if start_ticks is not None and boot_time is not None:
                return boot_time + start_ticks / float(os.sysconf("SC_CLK_TCK"))
        except (FileNotFoundError, OSError, ValueError, IndexError, TypeError):
            pass
        try:
            return self._mtime(process_dir)
        except (FileNotFoundError, OSError, ValueError):
            return None

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
                started_at = self._process_started_at(process_dir)
                age_seconds = self._now() - started_at if started_at is not None else 0.0
                if age_seconds > self.config.writer_stale_minutes * 60.0:
                    if started_at is not None:
                        warning_identity = (pid, started_at)
                        if warning_identity not in self._stale_writer_warning_identities:
                            self._stale_writer_warning_identities.add(warning_identity)
                            _structured_log(
                                logging.WARNING,
                                "supervisor.stale_writer_ignored",
                                pid=pid,
                                command=command[:200],
                                age_minutes=age_seconds / 60.0,
                                writer_stale_minutes=self.config.writer_stale_minutes,
                            )
                    continue
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
                mtime = self._mtime(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SupervisorError(f"failed to stat supervision path {path}: {exc}") from exc
            if mtime > now:
                if path == self.config.state_path:
                    raise SupervisorError("state progress evidence is future-dated")
                continue
            parts.append(f"{path}:{mtime:.9f}:{metadata.st_size}")
            if mtime > latest:
                latest, latest_path = mtime, path
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
        "--renudge-escalation-minutes",
        type=_finite_positive_cli_float,
        default=DEFAULT_RENUDGE_ESCALATION_MINUTES,
    )
    parser.add_argument(
        "--max-escalating-renudges",
        type=int,
        default=DEFAULT_MAX_ESCALATING_RENUDGES,
    )
    parser.add_argument(
        "--staleness-force-wake-minutes",
        type=_finite_positive_cli_float,
        default=DEFAULT_STALENESS_FORCE_WAKE_MINUTES,
    )
    parser.add_argument(
        "--writer-stale-minutes",
        type=_finite_positive_cli_float,
        default=DEFAULT_WRITER_STALE_MINUTES,
    )
    parser.add_argument(
        "--launch-request-ttl-minutes",
        type=_finite_positive_cli_float,
        default=DEFAULT_LAUNCH_REQUEST_TTL_MINUTES,
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
                renudge_escalation_minutes=args.renudge_escalation_minutes,
                max_escalating_renudges=args.max_escalating_renudges,
                staleness_force_wake_minutes=args.staleness_force_wake_minutes,
                writer_stale_minutes=args.writer_stale_minutes,
                launch_request_ttl_minutes=args.launch_request_ttl_minutes,
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
