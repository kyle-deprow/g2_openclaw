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
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote
from uuid import UUID

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_ACPX_CANDIDATES = 8
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
    source: Literal["task_runs", "subagent_runs"]

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
    """The exact ACPX retained record and Claude transcript binding."""

    session_key: str
    acp_session_id: str
    backend: str
    agent: str
    canonical_session_uuid: str
    mode: str
    effective_cwd: str
    claude_session_id: str
    acpx_record_id: str
    acpx_record_path: Path
    acpx_record_sha256: str
    acpx_candidate_count: int
    last_request_id: str
    created_at_ms: int
    last_used_at_ms: int
    closed_at_ms: int
    model: str
    effort: str


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

_SUBAGENT_RUN_QUERY = """
SELECT run_id, child_session_key, controller_session_key, requester_session_key,
       created_at, payload_json
  FROM subagent_runs
 WHERE requester_session_key = ? AND json_extract(payload_json, '$.label') = ?
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


def _finalize_task_run(
    *,
    task_id: str,
    runtime: str,
    task_kind: str | None,
    source_id: str | None,
    requester_session_key: str | None,
    owner_key: str,
    scope_kind: str,
    child_session_key: str,
    agent_id: str,
    requester_agent_id: str | None,
    run_id: str,
    label: str,
    status: str,
    created_at_ms: int,
    started_at_ms: int | None,
    ended_at_ms: int | None,
    reserved_at_ms: int,
    ack_child_session_key: str | None,
    ack_run_id: str | None,
    source: Literal["task_runs", "subagent_runs"],
) -> TaskRunHostRecord:
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
        owner_key=owner_key,
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
        source=source,
    )


def _task_runs_record(
    row: sqlite3.Row,
    *,
    owner_key: str,
    reservation_label: str,
    reserved_at_ms: int,
    expected_runtime: str,
    expected_scope_kind: str,
    expected_agent_id: str,
    ack_child_session_key: str | None,
    ack_run_id: str | None,
) -> TaskRunHostRecord:
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

    return _finalize_task_run(
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
        reserved_at_ms=reserved_at_ms,
        ack_child_session_key=ack_child_session_key,
        ack_run_id=ack_run_id,
        source="task_runs",
    )


def _json_object_value(value: object, field: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HostRecordError(f"{field} is not valid JSON") from exc
    elif isinstance(value, bytes):
        raw = value
    else:
        raise HostRecordError(f"{field} is not valid JSON")
    return _json_object_bytes(raw, field)


def _subagent_runs_record(
    row: sqlite3.Row,
    *,
    owner_key: str,
    reservation_label: str,
    reserved_at_ms: int,
    expected_runtime: str,
    expected_scope_kind: str,
    expected_agent_id: str,
    ack_child_session_key: str | None,
    ack_run_id: str | None,
) -> TaskRunHostRecord:
    payload = _json_object_value(_row_value(row, "payload_json"), "host subagent payload")
    row_run_id = _required_text(_row_value(row, "run_id"), "subagent.run_id")
    row_child_session_key = _required_text(
        _row_value(row, "child_session_key"), "subagent.child_session_key"
    )
    row_controller_session_key = _optional_text(
        _row_value(row, "controller_session_key"), "subagent.controller_session_key"
    )
    row_requester_session_key = _required_text(
        _row_value(row, "requester_session_key"), "subagent.requester_session_key"
    )
    row_created_at_ms = _required_epoch_ms(_row_value(row, "created_at"), "subagent.created_at")

    payload_run_id = _required_text(payload.get("runId"), "subagent.payload.runId")
    payload_task_run_id = _required_text(payload.get("taskRunId"), "subagent.payload.taskRunId")
    payload_child_session_key = _required_text(
        payload.get("childSessionKey"), "subagent.payload.childSessionKey"
    )
    payload_requester_session_key = _required_text(
        payload.get("requesterSessionKey"), "subagent.payload.requesterSessionKey"
    )
    payload_controller_session_key = _optional_text(
        payload.get("controllerSessionKey"), "subagent.payload.controllerSessionKey"
    )
    payload_label = _required_text(payload.get("label"), "subagent.payload.label")
    spawn_mode = _required_text(payload.get("spawnMode"), "subagent.payload.spawnMode")
    payload_created_at_ms = _required_epoch_ms(
        payload.get("createdAt"), "subagent.payload.createdAt"
    )
    requester_agent_id = _optional_nonempty_text(
        payload.get("requesterAgentId"), "subagent.payload.requesterAgentId"
    )

    if payload_run_id != payload_task_run_id or payload_task_run_id != row_run_id:
        raise HostRecordError("host subagent run ids do not match")
    if payload_child_session_key != row_child_session_key:
        raise HostRecordError("host subagent child session does not match")
    if payload_requester_session_key != owner_key or row_requester_session_key != owner_key:
        raise HostRecordError("host subagent requester session does not match owner_key")
    if payload_controller_session_key is not None and payload_controller_session_key != owner_key:
        raise HostRecordError("host subagent controller session does not match owner_key")
    if row_controller_session_key is not None and row_controller_session_key != owner_key:
        raise HostRecordError("host subagent controller session does not match owner_key")
    if payload_label != reservation_label:
        raise HostRecordError("host subagent label does not match the exact lookup")
    if spawn_mode != "run":
        raise HostRecordError("host subagent spawn mode is not run")
    if payload_created_at_ms != row_created_at_ms:
        raise HostRecordError("host subagent created timestamps do not match")
    if not row_child_session_key.startswith(f"agent:{expected_agent_id}:acp:"):
        raise HostRecordError("host subagent child session does not bind the expected ACP agent")

    execution = payload.get("execution")
    if not isinstance(execution, dict):
        raise HostRecordError("host subagent execution must be an object")
    if execution.get("status") != "terminal":
        status = "running"
        started_at_ms = _optional_epoch_ms(
            execution.get("startedAt"), "subagent.execution.startedAt"
        )
        ended_at_ms = None
    else:
        outcome = execution.get("outcome")
        if not isinstance(outcome, dict):
            raise HostRecordError("host subagent execution outcome must be an object")
        outcome_status = outcome.get("status")
        status_by_outcome = {
            "ok": "succeeded",
            "error": "failed",
            "timeout": "timed_out",
            "cancelled": "cancelled",
        }
        if not isinstance(outcome_status, str) or outcome_status not in status_by_outcome:
            raise HostRecordError("host subagent outcome status is unknown")
        status = status_by_outcome[outcome_status]
        started_at_ms = _required_epoch_ms(
            execution.get("startedAt"), "subagent.execution.startedAt"
        )
        ended_at_ms = _required_epoch_ms(execution.get("endedAt"), "subagent.execution.endedAt")

    return _finalize_task_run(
        task_id=payload_task_run_id,
        runtime=expected_runtime,
        task_kind="subagent",
        source_id=None,
        requester_session_key=owner_key,
        owner_key=owner_key,
        scope_kind=expected_scope_kind,
        child_session_key=row_child_session_key,
        agent_id=expected_agent_id,
        requester_agent_id=requester_agent_id,
        run_id=row_run_id,
        label=payload_label,
        status=status,
        created_at_ms=row_created_at_ms,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        reserved_at_ms=reserved_at_ms,
        ack_child_session_key=ack_child_session_key,
        ack_run_id=ack_run_id,
        source="subagent_runs",
    )


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
        if len(rows) > 1:
            raise HostRecordError(
                "host task lookup requires exactly one matching row after identity filters; "
                f"found {len(rows)}"
            )
        if len(rows) == 1:
            task_rows = rows
            subagent_rows: list[sqlite3.Row] = []
        else:
            try:
                subagent_rows = connection.execute(
                    _SUBAGENT_RUN_QUERY, (owner_key, reservation_label)
                ).fetchall()
            except sqlite3.Error as exc:
                raise HostRecordError("host subagent metadata could not be read") from exc
            if len(subagent_rows) != 1:
                raise HostRecordError(
                    "host subagent lookup requires exactly one matching row after "
                    "identity filters; "
                    f"found {len(subagent_rows)}"
                )
            task_rows = []

    if task_rows:
        return _task_runs_record(
            task_rows[0],
            owner_key=owner_key,
            reservation_label=reservation_label,
            reserved_at_ms=reserved_at_ms,
            expected_runtime=expected_runtime,
            expected_scope_kind=expected_scope_kind,
            expected_agent_id=expected_agent_id,
            ack_child_session_key=ack_child_session_key,
            ack_run_id=ack_run_id,
        )
    return _subagent_runs_record(
        subagent_rows[0],
        owner_key=owner_key,
        reservation_label=reservation_label,
        reserved_at_ms=reserved_at_ms,
        expected_runtime=expected_runtime,
        expected_scope_kind=expected_scope_kind,
        expected_agent_id=expected_agent_id,
        ack_child_session_key=ack_child_session_key,
        ack_run_id=ack_run_id,
    )


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


_ACPX_RECORD_NAME = re.compile(
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json"
)
_ACPX_LIFECYCLE_TOLERANCE_MS = 60_000


def _record_time(value: object, field: str) -> int:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostRecordError(f"host record {field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise HostRecordError(f"host record {field} must be UTC")
    result = int(parsed.timestamp() * 1000)
    if result < 0:
        raise HostRecordError(f"host record {field} must not be before the epoch")
    return result


def _open_acpx_sessions_dir(path: Path | str) -> int:
    directory = Path(path)
    if not directory.is_absolute() or directory.is_symlink():
        raise HostRecordError("ACPX sessions directory must be an absolute non-symlink directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
        directory_stat = os.fstat(descriptor)
    except OSError as exc:
        raise HostRecordError("ACPX sessions directory is missing or unreadable") from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        os.close(descriptor)
        raise HostRecordError("ACPX sessions path is not a regular directory")
    return descriptor


def _read_acpx_candidate(directory_fd: int, name: str) -> tuple[dict[str, object], bytes]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise HostRecordError("ACPX candidate is missing or path-unsafe") from exc
    try:
        initial_stat = os.fstat(descriptor)
        if not stat.S_ISREG(initial_stat.st_mode):
            raise HostRecordError("ACPX candidate must be a regular non-symlink file")
        if initial_stat.st_size > MAX_METADATA_BYTES:
            raise HostRecordError("ACPX candidate exceeds the bounded size limit")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_METADATA_BYTES:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_METADATA_BYTES - total + 1))
            except OSError as exc:
                raise HostRecordError("ACPX candidate could not be read") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_METADATA_BYTES:
                raise HostRecordError("ACPX candidate exceeds the bounded size limit")
        final_stat = os.fstat(descriptor)
        if final_stat.st_size != total:
            raise HostRecordError("ACPX candidate changed while it was being read")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    return _json_object_bytes(raw, "ACPX retained session record"), raw


def _acpx_record_candidate(
    record: dict[str, object],
    raw: bytes,
    name: str,
    child_session_key: str,
    expected_cwd: str,
    *,
    expected_backend: str,
    expected_agent: str,
    expected_mode: str,
    expected_run_id: str,
    reservation_at_ms: int | None,
    task_started_at_ms: int | None,
    task_ended_at_ms: int | None,
) -> AcpIdentityHostRecord:
    match = _ACPX_RECORD_NAME.fullmatch(name[-41:])
    if match is None:
        raise HostRecordError("ACPX candidate filename does not contain one canonical UUID")
    record_uuid = match.group("uuid")
    record_id = _required_text(record.get("acpx_record_id"), "acpx_record_id")
    expected_record_id = f"{child_session_key}:oneshot:{record_uuid}"
    if record_id != expected_record_id:
        raise HostRecordError("ACPX record id does not match its bounded filename and child key")
    name_field = _required_text(record.get("name"), "name")
    if name_field != child_session_key:
        raise HostRecordError("ACPX record name does not match the exact child session")
    schema = _required_text(record.get("schema"), "schema")
    if schema != "acpx.session.v1":
        raise HostRecordError("ACPX retained session record has an unexpected schema")
    acp_session_id = _canonical_uuid(record.get("acp_session_id"), "acp_session_id")
    if acp_session_id is None:
        raise HostRecordError("ACPX record has no canonical ACP session UUID")
    cwd = _validate_canonical_cwd(_required_text(record.get("cwd"), "cwd"), "cwd")
    if cwd != expected_cwd:
        raise HostRecordError("ACPX record cwd does not match the expected bundle cwd")
    last_request_id = _required_text(record.get("last_request_id"), "last_request_id")
    if last_request_id != expected_run_id:
        raise HostRecordError("ACPX record last_request_id does not match the spawn ACK run id")
    if record.get("closed") is not True:
        raise HostRecordError("ACPX record lifecycle is not closed")
    created_at_ms = _record_time(record.get("created_at"), "created_at")
    last_used_at_ms = _record_time(record.get("last_used_at"), "last_used_at")
    closed_at_ms = _record_time(record.get("closed_at"), "closed_at")
    if not created_at_ms <= last_used_at_ms <= closed_at_ms:
        raise HostRecordError("ACPX record lifecycle timestamps are not ordered")
    if reservation_at_ms is not None and created_at_ms < reservation_at_ms:
        raise HostRecordError("ACPX record was created before the review reservation")
    if task_started_at_ms is not None and closed_at_ms < task_started_at_ms:
        raise HostRecordError("ACPX record closed before the task started")
    if (
        task_ended_at_ms is not None
        and closed_at_ms > task_ended_at_ms + _ACPX_LIFECYCLE_TOLERANCE_MS
    ):
        raise HostRecordError("ACPX record closed outside the task lifecycle window")

    acpx = record.get("acpx")
    if not isinstance(acpx, dict):
        raise HostRecordError("ACPX retained session record is missing acpx metadata")
    config_options = acpx.get("config_options")
    session_options = acpx.get("session_options")
    if not isinstance(config_options, list) or not isinstance(session_options, dict):
        raise HostRecordError("ACPX retained session record has incomplete runtime options")
    effort_options = [
        option
        for option in config_options
        if isinstance(option, dict) and option.get("id") == "effort"
    ]
    if len(effort_options) != 1:
        raise HostRecordError("ACPX retained session record effort option is ambiguous")
    effort_value = effort_options[0].get("currentValue")
    model_value = session_options.get("model")
    if (
        not isinstance(effort_value, str)
        or not effort_value
        or not isinstance(model_value, str)
        or not model_value
    ):
        raise HostRecordError("ACPX retained session record has incomplete runtime options")
    effort = effort_value
    model = model_value
    if model != "claude-opus-5" or effort != "high":
        raise HostRecordError("ACPX retained session record model or effort is not Opus 5/high")
    title = _required_text(record.get("title"), "title")
    del title  # Title is required record evidence; task identity remains task_runs authority.
    return AcpIdentityHostRecord(
        session_key=child_session_key,
        acp_session_id=acp_session_id,
        backend=expected_backend,
        agent=expected_agent,
        canonical_session_uuid=acp_session_id,
        mode=expected_mode,
        effective_cwd=cwd,
        claude_session_id=acp_session_id,
        acpx_record_id=record_id,
        acpx_record_path=Path(name),
        acpx_record_sha256=hashlib.sha256(raw).hexdigest(),
        acpx_candidate_count=1,
        last_request_id=last_request_id,
        created_at_ms=created_at_ms,
        last_used_at_ms=last_used_at_ms,
        closed_at_ms=closed_at_ms,
        model=model,
        effort=effort,
    )


def read_exact_acpx_identity(
    acpx_sessions_dir: Path | str,
    child_session_key: str,
    expected_cwd: str,
    *,
    expected_backend: str,
    expected_agent: str,
    expected_mode: str,
    expected_run_id: str,
    reservation_at_ms: int | None = None,
    task_started_at_ms: int | None = None,
    task_ended_at_ms: int | None = None,
) -> AcpIdentityHostRecord:
    """Resolve exactly one retained ACPX record by a bounded encoded prefix.

    The transient core ACP projection is intentionally not consulted.  A child
    key plus ACK run ID is insufficient
    to derive the random ACPX oneshot UUID, so every canonical-UUID candidate
    under the configured sessions directory is bounded, read safely, and
    validated before accepting exactly one.
    """

    child_session_key = _validate_lookup_text(child_session_key, "child_session_key")
    if any(character in child_session_key for character in ("\x00", "/", "\\", "%")):
        raise HostRecordError("child_session_key contains an unsafe path character")
    expected_cwd = _validate_canonical_cwd(expected_cwd, "expected_cwd")
    expected_backend = _validate_lookup_text(expected_backend, "expected_backend")
    expected_agent = _validate_lookup_text(expected_agent, "expected_agent")
    expected_mode = _validate_lookup_text(expected_mode, "expected_mode")
    if expected_backend != "acpx" or expected_mode != "oneshot":
        raise HostRecordError("ACPX identity has unexpected backend or mode")
    expected_run_id = _validate_lookup_text(expected_run_id, "expected_run_id")
    if "\x00" in expected_run_id:
        raise HostRecordError("expected_run_id contains a NUL byte")
    if not child_session_key.startswith(f"agent:{expected_agent}:acp:"):
        raise HostRecordError("child_session_key does not bind the expected ACP agent")
    for value, field in (
        (reservation_at_ms, "reservation_at_ms"),
        (task_started_at_ms, "task_started_at_ms"),
        (task_ended_at_ms, "task_ended_at_ms"),
    ):
        if value is not None:
            _required_epoch_ms(value, field)
    if (
        task_started_at_ms is not None
        and task_ended_at_ms is not None
        and task_ended_at_ms < task_started_at_ms
    ):
        raise HostRecordError("task ended before it started")

    encoded_prefix = quote(child_session_key + ":oneshot:", safe="")
    directory_fd = _open_acpx_sessions_dir(acpx_sessions_dir)
    try:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise HostRecordError("ACPX sessions directory could not be listed") from exc
        matching: list[str] = []
        for name in names:
            if not isinstance(name, str) or not name.startswith(encoded_prefix):
                continue
            matching.append(name)
        if not matching:
            raise HostRecordError("no ACPX retained record matches the exact child prefix")
        if len(matching) > MAX_ACPX_CANDIDATES:
            raise HostRecordError("ACPX retained record candidate set exceeds the bounded limit")
        matching.sort()
        records: list[AcpIdentityHostRecord] = []
        for name in matching:
            suffix = name[len(encoded_prefix) :]
            if _ACPX_RECORD_NAME.fullmatch(suffix) is None:
                raise HostRecordError("ACPX child-prefix candidate has an unsafe filename")
            candidate_record, raw = _read_acpx_candidate(directory_fd, name)
            last_request_id = candidate_record.get("last_request_id")
            if last_request_id is None or (
                isinstance(last_request_id, str) and last_request_id != expected_run_id
            ):
                # ACPX creates a retained record before the first request.  It
                # has the same child prefix but cannot bind this ACK; retain
                # its parse/path/size safety checks while excluding it before
                # full identity and cardinality validation.
                continue
            records.append(
                _acpx_record_candidate(
                    candidate_record,
                    raw,
                    name,
                    child_session_key,
                    expected_cwd,
                    expected_backend=expected_backend,
                    expected_agent=expected_agent,
                    expected_mode=expected_mode,
                    expected_run_id=expected_run_id,
                    reservation_at_ms=reservation_at_ms,
                    task_started_at_ms=task_started_at_ms,
                    task_ended_at_ms=task_ended_at_ms,
                )
            )
        if len(records) != 1:
            raise HostRecordError(
                "ACPX retained record resolution is ambiguous; expected exactly one candidate"
            )
        resolved_record = records[0]
        return replace(
            resolved_record,
            acpx_record_path=Path(acpx_sessions_dir) / resolved_record.acpx_record_path.name,
            acpx_candidate_count=len(matching),
        )
    finally:
        os.close(directory_fd)


def _json_object_bytes(raw: bytes, field: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(raw, object_pairs_hook=_strict_json_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HostRecordError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise HostRecordError(f"{field} must contain a JSON object")
    return {key: cast(object, value) for key, value in decoded.items()}


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostRecordError("ACPX retained session record contains duplicate JSON keys")
        result[key] = value
    return result


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
