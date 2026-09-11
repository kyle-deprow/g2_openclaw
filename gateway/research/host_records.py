"""Read-only correlation of OpenClaw task records and Codex rollouts.

This module deliberately does not own review state, spawn work, route lineage,
or copy rollout text.  Callers provide exact host paths and correlation values.
SQLite is opened with an absolute ``mode=ro`` URI and ``query_only``; reading
an existing WAL database may still materialize SQLite ``-wal``/``-shm``
coordination sidecars when the same-user host directory permits it.  Missing
databases never cause a parent directory or database to be created.  These
checks are for trusted same-user host metadata, not hostile-root security.
Rollout service and billing tier remain unknown unless a future reader is
given separate host evidence; this reader never labels observations verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote
from uuid import UUID

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "timed_out", "cancelled", "lost"})


class HostRecordError(RuntimeError):
    """Host metadata is missing, malformed, ambiguous, or inconsistent."""


@dataclass(frozen=True, slots=True)
class TaskRunHostRecord:
    """The selected task-run identity and lifecycle fields."""

    task_id: str
    runtime: str
    task_kind: str | None
    source_id: str | None
    requester_session_key: str | None
    owner_key: str
    scope_kind: str
    child_session_key: str
    agent_id: str
    requester_agent_id: str | None
    run_id: str
    label: str
    status: str
    created_at_ms: int
    started_at_ms: int | None
    ended_at_ms: int | None

    @property
    def is_terminal(self) -> bool:
        """Whether the host has recorded one of the known terminal states."""

        return self.status in TERMINAL_TASK_STATUSES

    @property
    def is_pending(self) -> bool:
        """Whether the host has not recorded a terminal task status."""

        return not self.is_terminal


@dataclass(frozen=True, slots=True)
class AcpIdentityHostRecord:
    """The exact resolved ACP identity and Claude session binding."""

    session_key: str
    core_session_id: str | None
    backend: str
    agent: str
    runtime_session_name: str | None
    identity_state: str
    agent_session_id: str | None
    acpx_session_id: str | None
    canonical_session_uuid: str
    mode: str
    row_cwd: str | None
    runtime_options_cwd: str | None
    effective_cwd: str
    host_state: str | None
    claude_session_id: str


@dataclass(frozen=True, slots=True)
class ClaudeTranscriptRecord:
    """The exact transcript bytes selected by one canonical ACP UUID."""

    path: Path
    cwd: str
    session_uuid: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class RolloutUsage:
    """The latest observed token-count snapshot from one rollout.

    ``model_context_window`` is optional because rollout evidence can place it
    inside the selected usage snapshot or beside it in ``token_count.info``.
    Only live rollout evidence can confirm which placement a deployment uses;
    this reader does not infer a value when neither location reports one.
    """

    source: Literal["last", "total"]
    total_tokens: int
    model_context_window: int | None


@dataclass(frozen=True, slots=True)
class RolloutHostRecord:
    """Observed Codex rollout metadata; service and billing tier are unknown.

    The role is read only from the runtime's nested subagent metadata when it
    is present.  It is never inferred from a caller-supplied label or path.
    """

    path: Path
    thread_id: str
    agent_role: str | None
    model: str
    reasoning_effort: str
    terminal_state: Literal["succeeded", "failed", "pending"]
    latest_usage: RolloutUsage | None
    sha256: str


_TASK_RUN_QUERY = """
SELECT task_id, runtime, task_kind, source_id, requester_session_key,
       owner_key, scope_kind, child_session_key, agent_id,
       requester_agent_id, run_id, label, status, created_at,
       started_at, ended_at
  FROM task_runs
 WHERE owner_key = ? AND label = ?
   AND runtime = ? AND scope_kind = ? AND agent_id = ?
"""

_ACP_SESSION_QUERY = """
SELECT session_key, session_id, backend, agent, runtime_session_name,
       identity_json, mode, runtime_options_json, cwd, state
  FROM acp_sessions
 WHERE session_key = ?
"""


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostRecordError(f"host record {field} must be a non-empty string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HostRecordError(f"host record {field} must be a string or null")
    return value


def _optional_nonempty_text(value: object, field: str) -> str | None:
    result = _optional_text(value, field)
    if result == "":
        raise HostRecordError(f"host record {field} must not be empty")
    return result


def _required_epoch_ms(value: object, field: str) -> int:
    if type(value) is not int or value < 0:  # bool is not a valid timestamp here.
        raise HostRecordError(f"host record {field} must be a non-negative epoch-ms integer")
    return value


def _optional_epoch_ms(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _required_epoch_ms(value, field)


def _row_value(row: sqlite3.Row, field: str) -> object:
    return cast(object, row[field])


def _validate_lookup_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostRecordError(f"{field} must be a non-empty string")
    return value


def _validate_canonical_cwd(value: str, field: str) -> str:
    value = _validate_lookup_text(value, field)
    if "\x00" in value or not value.startswith("/") or value.startswith("//"):
        raise HostRecordError(f"{field} must be an absolute canonical path")
    if os.path.normpath(value) != value:
        raise HostRecordError(f"{field} must be an absolute canonical path")
    return value


def _optional_canonical_cwd(value: object, field: str) -> str | None:
    text = _optional_nonempty_text(value, field)
    return _validate_canonical_cwd(text, field) if text is not None else None


def _validate_ack(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _validate_lookup_text(value, field)


@contextmanager
def _readonly_database(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    if not path.is_absolute():
        raise HostRecordError("host metadata database path must be absolute")
    if not path.is_file():
        raise HostRecordError("host metadata database is missing or not a regular file")

    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise HostRecordError("host metadata database is unreadable") from exc

    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or query_only[0] != 1:
            raise HostRecordError("host metadata database did not enter query-only mode")
        yield connection
    except HostRecordError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HostRecordError("host metadata database read failed") from exc
    finally:
        connection.close()


def read_exact_task_run(
    database_path: Path | str,
    owner_key: str,
    reservation_label: str,
    reserved_at_ms: int,
    *,
    expected_runtime: str,
    expected_scope_kind: str,
    expected_agent_id: str,
    ack_child_session_key: str | None = None,
    ack_run_id: str | None = None,
) -> TaskRunHostRecord:
    """Read one task reserved by an exact owner and caller-supplied identity.

    ``reserved_at_ms`` is the trusted reservation wall-clock lower bound.  A
    task created before it is invalid even if all other fields match.
    """

    owner_key = _validate_lookup_text(owner_key, "owner_key")
    reservation_label = _validate_lookup_text(reservation_label, "reservation_label")
    reserved_at_ms = _required_epoch_ms(reserved_at_ms, "reserved_at_ms")
    expected_runtime = _validate_lookup_text(expected_runtime, "expected_runtime")
    expected_scope_kind = _validate_lookup_text(expected_scope_kind, "expected_scope_kind")
    expected_agent_id = _validate_lookup_text(expected_agent_id, "expected_agent_id")
    ack_child_session_key = _validate_ack(ack_child_session_key, "ack_child_session_key")
    ack_run_id = _validate_ack(ack_run_id, "ack_run_id")

    with _readonly_database(database_path) as connection:
        try:
            rows = connection.execute(
                _TASK_RUN_QUERY,
                (
                    owner_key,
                    reservation_label,
                    expected_runtime,
                    expected_scope_kind,
                    expected_agent_id,
                ),
            ).fetchall()
        except sqlite3.Error as exc:
            raise HostRecordError("host task metadata could not be read") from exc

    if len(rows) != 1:
        raise HostRecordError(
            "host task lookup requires exactly one matching row after identity filters; "
            f"found {len(rows)}"
        )
    row = rows[0]

    task_id = _required_text(_row_value(row, "task_id"), "task_id")
    runtime = _required_text(_row_value(row, "runtime"), "runtime")
    task_kind = _optional_nonempty_text(_row_value(row, "task_kind"), "task_kind")
    source_id = _optional_nonempty_text(_row_value(row, "source_id"), "source_id")
    requester_session_key = _optional_text(
        _row_value(row, "requester_session_key"), "requester_session_key"
    )
    stored_owner_key = _required_text(_row_value(row, "owner_key"), "owner_key")
    scope_kind = _required_text(_row_value(row, "scope_kind"), "scope_kind")
    child_session_key = _required_text(_row_value(row, "child_session_key"), "child_session_key")
    agent_id = _required_text(_row_value(row, "agent_id"), "agent_id")
    requester_agent_id = _optional_nonempty_text(
        _row_value(row, "requester_agent_id"), "requester_agent_id"
    )
    run_id = _required_text(_row_value(row, "run_id"), "run_id")
    label = _required_text(_row_value(row, "label"), "label")
    status = _required_text(_row_value(row, "status"), "status")
    created_at_ms = _required_epoch_ms(_row_value(row, "created_at"), "created_at")
    started_at_ms = _optional_epoch_ms(_row_value(row, "started_at"), "started_at")
    ended_at_ms = _optional_epoch_ms(_row_value(row, "ended_at"), "ended_at")

    if stored_owner_key != owner_key or label != reservation_label:
        raise HostRecordError("host task identity does not match the exact lookup")
    if requester_session_key and requester_session_key != owner_key:
        raise HostRecordError("host task requester session does not match owner_key")
    if (
        runtime != expected_runtime
        or scope_kind != expected_scope_kind
        or agent_id != expected_agent_id
    ):
        raise HostRecordError("host task identity has an unexpected runtime, scope, or agent")
    if created_at_ms < reserved_at_ms:
        raise HostRecordError("host task was created before the reservation")
    if started_at_ms is not None and started_at_ms < created_at_ms:
        raise HostRecordError("host task started before it was created")
    if ended_at_ms is not None:
        if ended_at_ms < created_at_ms:
            raise HostRecordError("host task ended before it was created")
        if started_at_ms is not None and ended_at_ms < started_at_ms:
            raise HostRecordError("host task ended before it started")
    if ack_child_session_key is not None and child_session_key != ack_child_session_key:
        raise HostRecordError("host task child session conflicts with the spawn ACK")
    if ack_run_id is not None and run_id != ack_run_id:
        raise HostRecordError("host task run id conflicts with the spawn ACK")

    return TaskRunHostRecord(
        task_id=task_id,
        runtime=runtime,
        task_kind=task_kind,
        source_id=source_id,
        requester_session_key=requester_session_key,
        owner_key=stored_owner_key,
        scope_kind=scope_kind,
        child_session_key=child_session_key,
        agent_id=agent_id,
        requester_agent_id=requester_agent_id,
        run_id=run_id,
        label=label,
        status=status,
        created_at_ms=created_at_ms,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
    )


def _json_object(raw: object, field: str, *, allow_null: bool = False) -> dict[str, object] | None:
    if raw is None and allow_null:
        return None
    if not isinstance(raw, str) or not raw:
        raise HostRecordError(f"host record {field} must contain a JSON object")
    try:
        decoded: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HostRecordError(f"host record {field} is not valid JSON") from exc
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise HostRecordError(f"host record {field} must contain a JSON object")
    return {key: cast(object, value) for key, value in decoded.items()}


def _canonical_uuid(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise HostRecordError(f"host record {field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise HostRecordError(f"host record {field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise HostRecordError(f"host record {field} must be a canonical UUID")
    return value


def read_exact_acp_identity(
    database_path: Path | str,
    child_session_key: str,
    claude_sessions_path: Path | str,
    expected_cwd: str,
    *,
    expected_backend: str,
    expected_agent: str,
    expected_mode: str,
) -> AcpIdentityHostRecord:
    """Read one resolved ACP row with caller-supplied runtime expectations."""

    child_session_key = _validate_lookup_text(child_session_key, "child_session_key")
    expected_cwd = _validate_canonical_cwd(expected_cwd, "expected_cwd")
    expected_backend = _validate_lookup_text(expected_backend, "expected_backend")
    expected_agent = _validate_lookup_text(expected_agent, "expected_agent")
    expected_mode = _validate_lookup_text(expected_mode, "expected_mode")

    with _readonly_database(database_path) as connection:
        try:
            rows = connection.execute(_ACP_SESSION_QUERY, (child_session_key,)).fetchall()
        except sqlite3.Error as exc:
            raise HostRecordError("ACP session metadata could not be read") from exc

    if len(rows) != 1:
        raise HostRecordError(
            f"ACP session lookup requires exactly one matching row; found {len(rows)}"
        )
    row = rows[0]
    session_key = _required_text(_row_value(row, "session_key"), "session_key")
    core_session_id = _optional_nonempty_text(_row_value(row, "session_id"), "session_id")
    backend = _required_text(_row_value(row, "backend"), "backend")
    agent = _required_text(_row_value(row, "agent"), "agent")
    runtime_session_name = _optional_nonempty_text(
        _row_value(row, "runtime_session_name"), "runtime_session_name"
    )
    identity = _json_object(_row_value(row, "identity_json"), "identity_json")
    if identity is None:  # Narrowing for the strict type checker.
        raise HostRecordError("ACP identity_json must contain a JSON object")
    identity_state = _required_text(identity.get("state"), "identity_json.state")
    if identity_state != "resolved":
        raise HostRecordError("ACP identity_json.state is not resolved")
    agent_session_id = _canonical_uuid(identity.get("agentSessionId"), "agentSessionId")
    acpx_session_id = _canonical_uuid(identity.get("acpxSessionId"), "acpxSessionId")
    if agent_session_id is None and acpx_session_id is None:
        raise HostRecordError("ACP identity has no canonical session UUID")
    if (
        agent_session_id is not None
        and acpx_session_id is not None
        and agent_session_id != acpx_session_id
    ):
        raise HostRecordError("ACP identity session UUIDs conflict")
    canonical_session_uuid = agent_session_id or acpx_session_id
    if canonical_session_uuid is None:  # Narrowing for the strict type checker.
        raise HostRecordError("ACP identity has no canonical session UUID")
    mode = _required_text(_row_value(row, "mode"), "mode")
    row_cwd = _optional_canonical_cwd(_row_value(row, "cwd"), "cwd")
    runtime_options = _json_object(
        _row_value(row, "runtime_options_json"), "runtime_options_json", allow_null=True
    )
    runtime_options_cwd = (
        _optional_canonical_cwd(runtime_options.get("cwd"), "runtime_options_json.cwd")
        if runtime_options is not None and "cwd" in runtime_options
        else None
    )
    effective_cwd = runtime_options_cwd or row_cwd
    if effective_cwd is None:
        raise HostRecordError("ACP session has no effective cwd")
    host_state = _optional_nonempty_text(_row_value(row, "state"), "state")

    if session_key != child_session_key:
        raise HostRecordError("ACP session key does not match the exact child session")
    if backend != expected_backend or agent != expected_agent or mode != expected_mode:
        raise HostRecordError("ACP identity has unexpected backend, agent, or mode")
    if effective_cwd != expected_cwd:
        raise HostRecordError("ACP session cwd does not match the expected bundle cwd")

    sessions_bytes = _read_regular_bounded(
        Path(claude_sessions_path), MAX_METADATA_BYTES, "Claude sessions metadata"
    )
    sessions = _json_object_bytes(sessions_bytes, "Claude sessions metadata")
    entry = sessions.get(child_session_key)
    if not isinstance(entry, dict):
        raise HostRecordError("Claude sessions metadata has no exact child session key")
    claude_session_id = _required_text(entry.get("sessionId"), "Claude sessionId")
    if core_session_id is not None and core_session_id != claude_session_id:
        raise HostRecordError("ACP core session_id conflicts with Claude sessions sessionId")

    return AcpIdentityHostRecord(
        session_key=session_key,
        core_session_id=core_session_id,
        backend=backend,
        agent=agent,
        runtime_session_name=runtime_session_name,
        identity_state=identity_state,
        agent_session_id=agent_session_id,
        acpx_session_id=acpx_session_id,
        canonical_session_uuid=canonical_session_uuid,
        mode=mode,
        row_cwd=row_cwd,
        runtime_options_cwd=runtime_options_cwd,
        effective_cwd=effective_cwd,
        host_state=host_state,
        claude_session_id=claude_session_id,
    )


def _json_object_bytes(raw: bytes, field: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HostRecordError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise HostRecordError(f"{field} must contain a JSON object")
    return {key: cast(object, value) for key, value in decoded.items()}


def _required_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise HostRecordError(f"host record {field} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _required_nonnegative_int(value, field)


def _rollout_usage(info: object) -> RolloutUsage:
    if not isinstance(info, dict):
        raise HostRecordError("Codex rollout token_count info must be an object")
    if "last_token_usage" in info:
        source: Literal["last", "total"] = "last"
        snapshot = info["last_token_usage"]
    elif "total_token_usage" in info:
        source = "total"
        snapshot = info["total_token_usage"]
    else:
        raise HostRecordError("Codex rollout token_count has no usage snapshot")
    if not isinstance(snapshot, dict):
        raise HostRecordError("Codex rollout token_count has no usage snapshot")
    snapshot_context = (
        _optional_nonnegative_int(snapshot["model_context_window"], "rollout.model_context_window")
        if "model_context_window" in snapshot
        else None
    )
    sibling_context = (
        _optional_nonnegative_int(info["model_context_window"], "rollout.model_context_window")
        if "model_context_window" in info
        else None
    )
    if (
        snapshot_context is not None
        and sibling_context is not None
        and snapshot_context != sibling_context
    ):
        raise HostRecordError("Codex rollout context window values disagree")
    return RolloutUsage(
        source=source,
        total_tokens=_required_nonnegative_int(
            snapshot.get("total_tokens"), "rollout.total_tokens"
        ),
        model_context_window=snapshot_context if snapshot_context is not None else sibling_context,
    )


def read_exact_rollout(rollout_path: Path | str) -> RolloutHostRecord:
    """Read one absolute caller-selected Codex rollout without retaining text.

    A second terminal marker is anomalous for this oneshot evidence and fails
    closed rather than guessing which terminal observation should win.
    """

    path = Path(rollout_path)
    if not path.is_absolute():
        raise HostRecordError("Codex rollout path must be absolute")
    content = _read_regular_bounded(path, MAX_TRANSCRIPT_BYTES, "Codex rollout")
    events = tuple(
        _json_object_bytes(line, f"Codex rollout line {index}")
        for index, line in enumerate(content.split(b"\n"), start=1)
        if line.strip()
    )
    session_meta: dict[str, object] | None = None
    latest_usage: RolloutUsage | None = None
    terminal_state: Literal["succeeded", "failed", "pending"] = "pending"

    for event in events:
        if event.get("type") == "session_meta":
            if session_meta is not None:
                raise HostRecordError("Codex rollout contains multiple session_meta records")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise HostRecordError("Codex rollout session_meta payload must be an object")
            session_meta = payload

    if session_meta is None:
        raise HostRecordError("Codex rollout is missing session_meta")
    thread_id = _required_text(session_meta.get("id"), "rollout.thread_id")
    agent_role: str | None = None
    source = session_meta.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            thread_spawn = subagent.get("thread_spawn")
            if isinstance(thread_spawn, dict):
                raw_role = thread_spawn.get("agent_role")
                if raw_role is not None:
                    agent_role = _required_text(raw_role, "rollout.agent_role")
    direct_role = session_meta.get("agent_role")
    if direct_role is not None:
        direct_role = _required_text(direct_role, "rollout.agent_role")
        if agent_role is not None and direct_role != agent_role:
            raise HostRecordError("Codex rollout role metadata values disagree")
        agent_role = direct_role
    model = (
        _required_text(session_meta["model"], "rollout.model")
        if session_meta.get("model") is not None
        else None
    )
    reasoning_effort = (
        _required_text(session_meta["reasoning_effort"], "rollout.reasoning_effort")
        if session_meta.get("reasoning_effort") is not None
        else None
    )

    def merge_observation(current: str | None, value: object, field: str) -> str | None:
        if value is None:
            return current
        observed = _required_text(value, field)
        if current is not None and current != observed:
            raise HostRecordError(f"Codex rollout {field} values disagree")
        return observed

    for event in events:
        if event.get("type") != "turn_context":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise HostRecordError("Codex rollout turn_context payload must be an object")
        model = merge_observation(model, payload.get("model"), "rollout.model")
        reasoning_effort = merge_observation(
            reasoning_effort,
            payload.get("effort", payload.get("reasoning_effort")),
            "rollout.reasoning_effort",
        )
    if model is None:
        raise HostRecordError("host record rollout.model must be a non-empty string")
    if reasoning_effort is None:
        raise HostRecordError("host record rollout.reasoning_effort must be a non-empty string")

    for event in events:
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise HostRecordError("Codex rollout event_msg payload must be an object")
        event_type = payload.get("type")
        if event_type == "token_count":
            latest_usage = _rollout_usage(payload.get("info"))
        elif event_type in {"task_complete", "task_failed"}:
            marker_thread_id = payload.get("thread_id")
            if marker_thread_id is not None and marker_thread_id != thread_id:
                raise HostRecordError("Codex rollout terminal marker has the wrong thread id")
            if terminal_state != "pending":
                raise HostRecordError("Codex rollout contains multiple terminal markers")
            terminal_state = "succeeded" if event_type == "task_complete" else "failed"

    return RolloutHostRecord(
        path=path,
        thread_id=thread_id,
        agent_role=agent_role,
        model=model,
        reasoning_effort=reasoning_effort,
        terminal_state=terminal_state,
        latest_usage=latest_usage,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _read_regular_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise HostRecordError(f"{label} byte limit must be positive")
    if max_bytes > MAX_TRANSCRIPT_BYTES:
        raise HostRecordError(f"{label} byte limit exceeds the safety maximum")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        file_descriptor = os.open(path, flags)
    except OSError as exc:
        raise HostRecordError(f"{label} is missing or unreadable") from exc

    try:
        try:
            initial_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(initial_stat.st_mode):
                raise HostRecordError(f"{label} must be a regular non-symlink file")
            if initial_stat.st_size > max_bytes:
                raise HostRecordError(f"{label} exceeds the bounded size limit")

            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(file_descriptor, min(1024 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise HostRecordError(f"{label} exceeds the bounded size limit")
            final_stat = os.fstat(file_descriptor)
            if final_stat.st_size != total:
                raise HostRecordError(f"{label} changed while it was being read")
            return b"".join(chunks)
        except HostRecordError:
            raise
        except OSError as exc:
            raise HostRecordError(f"{label} could not be read") from exc
    finally:
        os.close(file_descriptor)


def _encode_claude_project_cwd(cwd: str) -> str:
    return "".join(
        character
        if ("A" <= character <= "Z") or ("a" <= character <= "z") or ("0" <= character <= "9")
        else "-"
        for character in cwd
    )


def _validate_canonical_session_uuid(session_uuid: str) -> str:
    value = _canonical_uuid(session_uuid, "session_uuid")
    if value is None:  # The signature is non-null, but preserve fail-closed behavior.
        raise HostRecordError("session_uuid must be a canonical UUID")
    return value


def derive_claude_transcript_path(projects_root: Path | str, cwd: str, session_uuid: str) -> Path:
    """Derive exactly one Claude project transcript path; never scan a directory."""

    cwd = _validate_canonical_cwd(cwd, "cwd")
    canonical_uuid = _validate_canonical_session_uuid(session_uuid)
    encoded_cwd = _encode_claude_project_cwd(cwd)
    return Path(projects_root) / encoded_cwd / f"{canonical_uuid}.jsonl"


def read_exact_claude_transcript(
    projects_root: Path | str,
    cwd: str,
    session_uuid: str,
    *,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
) -> ClaudeTranscriptRecord:
    """Read one bounded, regular, non-symlink Claude transcript exactly once."""

    path = derive_claude_transcript_path(projects_root, cwd, session_uuid)
    content = _read_regular_bounded(path, max_bytes, "Claude transcript")
    return ClaudeTranscriptRecord(
        path=path,
        cwd=cwd,
        session_uuid=_validate_canonical_session_uuid(session_uuid),
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
