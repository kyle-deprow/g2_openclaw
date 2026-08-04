"""Pure task classification and canonical task-list reconciliation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from gateway.autoresearch_rpc import (
    OpenClawRPC as OpenClawRPC,
)
from gateway.autoresearch_rpc import (
    ShutdownRequested as ShutdownRequested,
)
from gateway.autoresearch_rpc import (
    _shutdown_not_requested as _shutdown_not_requested,
)
from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_AGENT_ID as AUTORESEARCH_OWNER_AGENT_ID,
)
from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_SESSION_KEY as AUTORESEARCH_OWNER_SESSION_KEY,
)
from gateway.autoresearch_shared import (
    RECOVERY_ERROR_PATTERNS as RECOVERY_ERROR_PATTERNS,
)
from gateway.autoresearch_shared import (
    RELEVANT_AGENT_IDS as RELEVANT_AGENT_IDS,
)
from gateway.autoresearch_shared import (
    RecoveryErrorPattern as RecoveryErrorPattern,
)
from gateway.autoresearch_shared import (
    SupervisorError as SupervisorError,
)


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
