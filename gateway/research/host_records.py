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

Installed host shape (OpenClaw 2026.9.2 / Codex 0.153.4) that these readers
model: a native child has a Codex ``threads`` row and a spawn edge but no
OpenClaw session row or session event; the owner thread rotates between runs;
the plaintext spawn prompt is stored nowhere (the rollout ``message`` is
ciphertext); and a completion callback appears in the owner store only as a
run named ``announce:codex-native:<spawning-thread>:<child-thread>:<status>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
# The owner rollout grows by 0.9-3 MB per turn and 16 rollouts on the installed
# host already exceed 8 MiB; a native implementer child was 3.3 MB.
MAX_PARENT_ROLLOUT_BYTES = 512 * 1024 * 1024
MAX_CHILD_ROLLOUT_BYTES = 64 * 1024 * 1024
_ROLLOUT_CHUNK_BYTES = 1024 * 1024
_NATIVE_TASK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TERMINAL_MARKERS = frozenset({"task_complete", "task_failed", "turn_aborted"})
_ANNOUNCE_PREFIX = "announce:codex-native:"

TerminalState = Literal["succeeded", "failed", "cancelled", "pending"]


class HostRecordError(RuntimeError):
    """Host metadata is missing, malformed, ambiguous, or inconsistent."""


class HostRecordPending(HostRecordError):
    """The exact host record exists but has not reached its official end."""


@dataclass(frozen=True, slots=True)
class NativeSpawnCall:
    """The one native ``collaboration.spawn_agent`` call that spawned a reservation.

    Only the fields needed for correlation are retained.  The spawn ``message``
    is ciphertext in the installed rollout, so the prompt is neither exposed
    nor hashed here; binding rests on the plaintext task name.
    """

    call_id: str
    agent_type: str
    task_name: str
    result_task_name: str | None
    observed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class NativeParentRollout:
    """Prefix snapshot of an owner rollout: thread identity and reserved spawn calls."""

    path: Path
    thread_id: str
    spawn_calls: tuple[NativeSpawnCall, ...]


@dataclass(frozen=True, slots=True)
class NativeOwnerHostRecord:
    """Official OpenClaw owner run/thread binding from read-only runtime events."""

    owner_session_key: str
    session_id: str
    run_id: str
    thread_id: str
    model: str
    provider: str
    started_at_ms: int
    ended_at_ms: int | None


@dataclass(frozen=True, slots=True)
class NativeManagedDatabases:
    """The two official native runtime stores for one managed installation."""

    openclaw_database: Path
    codex_state_database: Path


@dataclass(frozen=True, slots=True)
class NativeChildHostRecord:
    """Official child thread identity and native rollout evidence.

    The child is identified by its Codex thread id: the installed host writes
    no OpenClaw session row or session event for a native child.
    """

    owner_session_key: str
    owner_run_id: str
    parent_thread_id: str
    child_thread_id: str
    task_name: str
    model: str
    reasoning_effort: str
    agent_role: str
    rollout_path: Path
    rollout_sha256: str
    terminal_state: TerminalState
    last_agent_message: str | None
    spawn_call_id: str
    announce_run_id: str | None
    announce_status: Literal["succeeded", "failed"] | None


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
    ``terminal_state`` maps ``task_complete`` without an error to
    ``succeeded``, ``task_complete`` with an error and ``task_failed`` to
    ``failed``, and ``turn_aborted`` to ``cancelled``.
    """

    path: Path
    thread_id: str
    agent_role: str | None
    model: str
    reasoning_effort: str
    terminal_state: TerminalState
    latest_usage: RolloutUsage | None
    sha256: str
    parent_thread_id: str | None = None
    originator: str | None = None
    last_agent_message: str | None = None
    unterminated_tail: bool = False


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostRecordError(f"host record {field} must be a non-empty string")
    return value


def _required_epoch_ms(value: object, field: str) -> int:
    if type(value) is not int or value < 0:  # bool is not a valid timestamp here.
        raise HostRecordError(f"host record {field} must be a non-negative epoch-ms integer")
    return value


def _optional_timestamp_ms(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise HostRecordError(f"host record {field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostRecordError(f"host record {field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise HostRecordError(f"host record {field} must be UTC")
    return int(parsed.timestamp() * 1000)


def _validate_lookup_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostRecordError(f"{field} must be a non-empty string")
    return value


@contextmanager
def _readonly_database(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    if not path.is_absolute():
        raise HostRecordError("host metadata database path must be absolute")
    # SQLite's URI ``mode=ro`` follows symlinks.  A managed runtime database
    # must therefore be a regular file reached only through regular
    # directories; otherwise a caller can silently redirect correlation to an
    # unrelated database.  Check every component before opening it.
    try:
        database_stat = path.lstat()
    except OSError as exc:
        raise HostRecordError("host metadata database is missing or not a regular file") from exc
    if stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode):
        raise HostRecordError("host metadata database is missing or not a regular file")
    parent = path.parent
    while True:
        try:
            parent_stat = parent.lstat()
        except OSError as exc:
            raise HostRecordError("host metadata database parent is missing") from exc
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise HostRecordError("host metadata database has an unsafe parent path")
        if parent == parent.parent:
            break
        parent = parent.parent
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


def _sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise HostRecordError("host metadata schema could not be read") from exc


def managed_native_databases(core_database: Path | str) -> NativeManagedDatabases:
    """Derive and validate native stores from the managed core database.

    ``RESEARCH_CORE_DATABASE`` is an admission anchor, not an agent database.
    Only ``<managed-root>/state/openclaw.sqlite`` is accepted; all derived
    paths are canonical, non-symlink regular files with the installed native
    schemas.  This helper is shared by wake construction and cancellation so
    neither path can drift to a global or caller-guessed database.
    """

    core = Path(core_database)
    if not core.is_absolute() or core.name != "openclaw.sqlite" or core.parent.name != "state":
        raise HostRecordError("managed core database must be <root>/state/openclaw.sqlite")
    managed_root = core.parent.parent
    openclaw = managed_root / "agents" / "research-orchestrator" / "agent" / "openclaw-agent.sqlite"
    codex = (
        managed_root
        / "agents"
        / "research-orchestrator"
        / "agent"
        / "codex-home"
        / "state_5.sqlite"
    )
    # Open the exact paths read-only here.  Besides rejecting symlinks in all
    # parents, this confirms both files expose the stores needed by native
    # correlation before a wake or abort command is rendered.
    with _readonly_database(core) as connection:
        if "task_runs" not in _sqlite_tables(connection):
            raise HostRecordError("managed core database has no installed task_runs schema")
    with _readonly_database(openclaw) as connection:
        if not {"session_nodes", "trajectory_runtime_events"}.issubset(_sqlite_tables(connection)):
            raise HostRecordError("managed OpenClaw database has no native runtime schema")
    with _readonly_database(codex) as connection:
        if not {"threads", "thread_spawn_edges"}.issubset(_sqlite_tables(connection)):
            raise HostRecordError("managed Codex database has no native thread schema")
    return NativeManagedDatabases(openclaw, codex)


def _native_event(value: object, field: str) -> dict[str, object]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise HostRecordError(f"{field} is not valid JSON")
    return _json_object_bytes(raw, field)


def _native_run_id(value: object, field: str) -> str:
    return _required_text(value, field)


def _boundary_event(
    row: sqlite3.Row, owner_session_key: str
) -> tuple[int, str, str, dict[str, object], str]:
    """Parse one bound-run boundary row and enforce its session/run identity."""

    created_at = _required_epoch_ms(row[3], "native runtime event created_at")
    event = _native_event(row[2], "native runtime event")
    event_type = str(event.get("type"))
    event_session_id = _required_text(event.get("sessionId"), "native boundary session_id")
    event_session_key = _required_text(event.get("sessionKey"), "native boundary session_key")
    event_run_id = _native_run_id(event.get("runId"), "native boundary run_id")
    if event_session_id != str(row[0]) or event_session_key != owner_session_key:
        raise HostRecordError("native boundary event identity does not match its row")
    if event_run_id != str(row[1]):
        raise HostRecordError("native boundary event run_id does not match its row")
    data = event.get("data")
    if not isinstance(data, dict):
        raise HostRecordError("native boundary event thread identity is malformed")
    _required_text(data.get("threadId"), "native boundary thread_id")
    return created_at, event_session_id, event_run_id, event, event_type


def read_exact_native_owner(
    database_path: Path | str,
    owner_session_key: str,
    reserved_at_ms: int,
    *,
    expected_run_id: str | None = None,
    expected_thread_id: str | None = None,
) -> NativeOwnerHostRecord:
    """Resolve one owner run/thread from OpenClaw's official runtime events.

    The old ``task_runs`` projection is deliberately not consulted.  The
    owner run is selected by its exact session key and run id; its interval is
    that run's own ``session.started`` and ``session.ended`` events.  Owner
    threads rotate between runs on the installed host, so the thread is bound
    only through those two events and later runs are never consulted.  A run
    without a ``session.ended`` event has an open interval.
    """

    owner_session_key = _validate_lookup_text(owner_session_key, "owner_session_key")
    reserved_at_ms = _required_epoch_ms(reserved_at_ms, "reserved_at_ms")
    if expected_run_id is not None:
        expected_run_id = _validate_lookup_text(expected_run_id, "expected_run_id")
    if expected_thread_id is not None:
        expected_thread_id = _validate_lookup_text(expected_thread_id, "expected_thread_id")

    with _readonly_database(database_path) as connection:
        tables = _sqlite_tables(connection)
        if not {"session_nodes", "trajectory_runtime_events"}.issubset(tables):
            raise HostRecordError("native OpenClaw database has no runtime event schema")
        try:
            node_rows = connection.execute(
                "SELECT session_key, current_session_id, entry_valid "
                "FROM session_nodes WHERE session_key = ?",
                (owner_session_key,),
            ).fetchall()
            parameters: list[object] = []
            run_filter = ""
            if expected_run_id is not None:
                run_filter = "run_id = ? AND "
                parameters.append(expected_run_id)
            parameters.extend(
                [str(node_rows[0][1]) if len(node_rows) == 1 else "", owner_session_key]
            )
            # Only boundary events are selected: the owner session holds
            # thousands of large trace events that this reader never needs.
            rows = connection.execute(
                "SELECT session_id, run_id, event_json, created_at "
                "FROM trajectory_runtime_events "
                f"WHERE {run_filter}"
                "(session_id = ? OR json_extract(event_json, '$.sessionKey') = ?) "
                "AND json_extract(event_json, '$.type') IN ('session.started', 'session.ended') "
                "ORDER BY created_at, seq",
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise HostRecordError("native OpenClaw runtime events could not be read") from exc
    if len(node_rows) != 1:
        raise HostRecordError("native owner session identity is missing or ambiguous")
    if node_rows[0][0] != owner_session_key or node_rows[0][2] != 1:
        raise HostRecordError("native owner session node is not valid")

    starts: dict[str, list[tuple[int, str, dict[str, object]]]] = {}
    ends: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for row in rows:
        created_at, session_id, run_id, event, event_type = _boundary_event(row, owner_session_key)
        if event_type == "session.started":
            starts.setdefault(run_id, []).append((created_at, session_id, event))
        else:
            ends.setdefault(run_id, []).append((created_at, event))

    candidates: list[tuple[str, int, str, dict[str, object], int | None]] = []
    for run_id, started in starts.items():
        start_data = cast(dict[str, object], started[0][2]["data"])
        thread_id = str(start_data["threadId"])
        if expected_thread_id is not None and thread_id != expected_thread_id:
            continue
        ended = ends.get(run_id, [])
        if expected_run_id is None and any(entry[0] > reserved_at_ms for entry in started):
            # Without a bound run id, only runs started before the reservation
            # can own it; the rest are later turns of the same session.
            continue
        candidates.append(
            (
                run_id,
                started[0][0],
                started[0][1],
                started[0][2],
                ended[0][0] if ended else None,
            )
        )
    if not candidates:
        raise HostRecordError("native owner run/thread correlation is unresolved")
    if len(candidates) != 1:
        raise HostRecordError("native owner run/thread correlation is ambiguous")
    run_id, started_at, session_id, start_event, ended_at = candidates[0]
    # Only the bound run's own events are held to identity rules: duplicates
    # of its started/ended events and a conflicting thread on either one.
    if len(starts[run_id]) != 1:
        raise HostRecordError("native owner run/thread correlation is ambiguous")
    if len(ends.get(run_id, [])) > 1:
        raise HostRecordError("native owner run/thread correlation is ambiguous")
    start_data = cast(dict[str, object], start_event["data"])
    thread_id = str(start_data["threadId"])
    for _created_at, end_event in ends.get(run_id, []):
        end_data = cast(dict[str, object], end_event["data"])
        if end_data.get("threadId") != thread_id:
            raise HostRecordError("native owner end event thread does not match owner")
    provider = _required_text(start_event.get("provider"), "native owner provider")
    model = _required_text(start_event.get("modelId"), "native owner model")
    # Only the bound run's own start is checked: earlier history on the same
    # session legitimately contains other routes.
    if provider != "openai" or model != "gpt-6-astra":
        raise HostRecordError("native owner runtime is not the observed OpenAI Astra route")
    return NativeOwnerHostRecord(
        owner_session_key,
        session_id,
        run_id,
        thread_id,
        model,
        provider,
        started_at,
        ended_at,
    )


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
            raise HostRecordError("host record contains duplicate JSON keys")
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


class _RolloutPrefix:
    """A regular, non-symlink rollout opened once with its size snapshotted.

    The host appends to rollouts while they are read (collection runs inside a
    callback turn that can share the owner thread), so only the first ``size``
    bytes are read, line by line, and later appends are ignored.
    """

    def __init__(self, descriptor: int, size: int, label: str) -> None:
        self._descriptor = descriptor
        self._size = size
        self._label = label

    def lines(self) -> Iterator[tuple[bytes, bool]]:
        """Yield ``(line, newline_terminated)`` for the snapshotted prefix."""

        remaining = self._size
        buffer = bytearray()
        while remaining > 0:
            chunk = os.read(self._descriptor, min(_ROLLOUT_CHUNK_BYTES, remaining))
            if not chunk:
                raise HostRecordError(f"{self._label} changed while it was being read")
            remaining -= len(chunk)
            buffer.extend(chunk)
            start = 0
            while True:
                end = buffer.find(b"\n", start)
                if end < 0:
                    break
                yield bytes(buffer[start:end]), True
                start = end + 1
            del buffer[:start]
        if buffer:
            yield bytes(buffer), False


@contextmanager
def _open_rollout_prefix(path: Path, max_bytes: int, label: str) -> Iterator[_RolloutPrefix]:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise HostRecordError(f"{label} byte limit must be positive")
    if max_bytes > MAX_PARENT_ROLLOUT_BYTES:
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
            yield _RolloutPrefix(file_descriptor, initial_stat.st_size, label)
        except HostRecordError:
            raise
        except OSError as exc:
            raise HostRecordError(f"{label} could not be read") from exc
    finally:
        os.close(file_descriptor)


def read_exact_rollout(
    rollout_path: Path | str,
    *,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    unterminated_tail: Literal["parse", "ignore"] = "parse",
    digest_through_terminal: bool = False,
) -> RolloutHostRecord:
    """Read one absolute caller-selected Codex rollout without retaining text.

    The file is read as a streamed prefix of its size at open.  More than one
    terminal marker is anomalous and fails closed.  With ``unterminated_tail``
    set to ``ignore`` a final line without a newline is treated as a writer
    that is mid-append: it is not parsed and the rollout is reported
    ``pending``.  ``digest_through_terminal`` limits ``sha256`` to the prefix
    up to and including the terminal marker line.
    """

    path = Path(rollout_path)
    if not path.is_absolute():
        raise HostRecordError("Codex rollout path must be absolute")
    hasher = hashlib.sha256()
    terminal_digest: str | None = None
    session_meta: dict[str, object] | None = None
    latest_usage: RolloutUsage | None = None
    observations: list[tuple[object, object]] = []
    markers: list[dict[str, object]] = []
    tail_unterminated = False

    with _open_rollout_prefix(path, max_bytes, "Codex rollout") as prefix:
        for index, (raw, terminated) in enumerate(prefix.lines(), start=1):
            if not terminated and unterminated_tail == "ignore":
                tail_unterminated = True
                continue
            hasher.update(raw)
            if terminated:
                hasher.update(b"\n")
            if not raw.strip():
                continue
            event = _json_object_bytes(raw, f"Codex rollout line {index}")
            event_kind = event.get("type")
            if event_kind == "session_meta":
                if session_meta is not None:
                    raise HostRecordError("Codex rollout contains multiple session_meta records")
                meta_payload = event.get("payload")
                if not isinstance(meta_payload, dict):
                    raise HostRecordError("Codex rollout session_meta payload must be an object")
                session_meta = meta_payload
            elif event_kind == "turn_context":
                context_payload = event.get("payload")
                if not isinstance(context_payload, dict):
                    raise HostRecordError("Codex rollout turn_context payload must be an object")
                observations.append(
                    (
                        context_payload.get("model"),
                        context_payload.get("effort", context_payload.get("reasoning_effort")),
                    )
                )
            elif event_kind == "event_msg":
                message_payload = event.get("payload")
                if not isinstance(message_payload, dict):
                    raise HostRecordError("Codex rollout event_msg payload must be an object")
                message_type = message_payload.get("type")
                if message_type == "token_count":
                    # ``info`` is null on rate-limit-only snapshots; keep the
                    # latest real usage snapshot in that case.
                    if message_payload.get("info") is not None:
                        latest_usage = _rollout_usage(message_payload.get("info"))
                elif message_type in _TERMINAL_MARKERS:
                    markers.append(message_payload)
                    terminal_digest = hasher.copy().hexdigest()

    if session_meta is None:
        raise HostRecordError("Codex rollout is missing session_meta")
    thread_id = _required_text(session_meta.get("id"), "rollout.thread_id")
    agent_role: str | None = None
    parent_thread_id: str | None = None
    originator: str | None = None
    source = session_meta.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            thread_spawn = subagent.get("thread_spawn")
            if isinstance(thread_spawn, dict):
                raw_role = thread_spawn.get("agent_role")
                if raw_role is not None:
                    agent_role = _required_text(raw_role, "rollout.agent_role")
                raw_parent = thread_spawn.get("parent_thread_id")
                if raw_parent is not None:
                    parent_thread_id = _required_text(raw_parent, "rollout.parent_thread_id")
                raw_originator = thread_spawn.get("originator")
                if raw_originator is not None:
                    originator = _required_text(raw_originator, "rollout.originator")
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

    for observed_model, observed_effort in observations:
        model = merge_observation(model, observed_model, "rollout.model")
        reasoning_effort = merge_observation(
            reasoning_effort, observed_effort, "rollout.reasoning_effort"
        )
    if model is None:
        raise HostRecordError("host record rollout.model must be a non-empty string")
    if reasoning_effort is None:
        raise HostRecordError("host record rollout.reasoning_effort must be a non-empty string")

    if len(markers) > 1:
        raise HostRecordError("Codex rollout contains multiple terminal markers")
    terminal_state: TerminalState = "pending"
    last_agent_message: str | None = None
    for marker in markers:
        marker_thread_id = marker.get("thread_id")
        if marker_thread_id is not None and marker_thread_id != thread_id:
            raise HostRecordError("Codex rollout terminal marker has the wrong thread id")
        marker_type = marker.get("type")
        if marker_type == "turn_aborted":
            terminal_state = "cancelled"
        elif marker_type == "task_failed" or marker.get("error") is not None:
            terminal_state = "failed"
        else:
            terminal_state = "succeeded"
            message = marker.get("last_agent_message", marker.get("lastAgentMessage"))
            if message is not None:
                if not isinstance(message, str) or not message:
                    raise HostRecordError("Codex rollout task_complete message is malformed")
                last_agent_message = message
    if tail_unterminated:
        # The writer may be mid-append: nothing after the last newline is
        # evidence, so the rollout is not yet terminal.
        terminal_state = "pending"
        last_agent_message = None

    return RolloutHostRecord(
        path=path,
        thread_id=thread_id,
        agent_role=agent_role,
        model=model,
        reasoning_effort=reasoning_effort,
        terminal_state=terminal_state,
        latest_usage=latest_usage,
        sha256=(
            terminal_digest
            if digest_through_terminal and terminal_digest is not None
            else hasher.hexdigest()
        ),
        parent_thread_id=parent_thread_id,
        originator=originator,
        last_agent_message=last_agent_message,
        unterminated_tail=tail_unterminated,
    )


def read_native_parent_rollout(
    rollout_path: Path | str,
    task_name: str,
    *,
    agent_type: str = "reviewer",
    max_bytes: int = MAX_PARENT_ROLLOUT_BYTES,
) -> NativeParentRollout:
    """Find the reserved ``spawn_agent`` call in a growing owner rollout.

    Only the snapshotted prefix is read and a final line without a newline is
    ignored (the writer may be mid-append).  The owner rollout holds spawn
    calls for other roles and tasks; those are skipped and never make the
    rollout unreadable.  Strict checks apply only to calls whose ``task_name``
    equals the reservation's: the exact argument key set, the expected
    ``agent_type`` (default ``reviewer``), ``fork_turns`` ``none``, one call and
    one output.  The spawn ``message`` is ciphertext on the installed host, so it is not examined.
    Lines that cannot contain a session header, a spawn call, or an output for
    a matching call are skipped without parsing to bound the cost of a large
    rollout.
    """

    path = Path(rollout_path)
    if not path.is_absolute():
        raise HostRecordError("Codex rollout path must be absolute")
    task_name = _validate_lookup_text(task_name, "task_name")
    thread_id: str | None = None
    other_call_ids: set[str] = set()
    matching: dict[str, tuple[str, int | None]] = {}
    outputs: dict[str, str | None] = {}

    with _open_rollout_prefix(path, max_bytes, "Codex parent rollout") as prefix:
        for index, (raw, terminated) in enumerate(prefix.lines(), start=1):
            if not terminated:
                continue
            wants_meta = b'"session_meta"' in raw
            wants_call = b'"spawn_agent"' in raw
            wants_output = any(call_id.encode("utf-8") in raw for call_id in matching)
            if not (wants_meta or wants_call or wants_output):
                continue
            event = _json_object_bytes(raw, f"Codex rollout line {index}")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "session_meta":
                if thread_id is not None:
                    raise HostRecordError("Codex rollout contains multiple session_meta records")
                thread_id = _required_text(payload.get("id"), "rollout.thread_id")
                continue
            if event.get("type") != "response_item":
                continue
            item_type = payload.get("type")
            if (
                item_type == "function_call"
                and payload.get("namespace") == "collaboration"
                and payload.get("name") == "spawn_agent"
            ):
                _record_spawn_call(event, payload, task_name, agent_type, other_call_ids, matching)
            elif item_type == "function_call_output":
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id in matching:
                    if call_id in outputs:
                        raise HostRecordError("rollout.spawn_agent contains duplicate outputs")
                    outputs[call_id] = _spawn_output_task_name(payload.get("output"))

    if thread_id is None:
        raise HostRecordError("Codex rollout is missing session_meta")
    calls = tuple(
        NativeSpawnCall(call_id, agent_type, task_name, outputs[call_id], observed_at_ms)
        for call_id, (agent_type, observed_at_ms) in matching.items()
        if call_id in outputs
    )
    return NativeParentRollout(path=path, thread_id=thread_id, spawn_calls=calls)


def _record_spawn_call(
    event: Mapping[str, object],
    payload: Mapping[str, object],
    task_name: str,
    expected_agent_type: str,
    other_call_ids: set[str],
    matching: dict[str, tuple[str, int | None]],
) -> None:
    arguments = payload.get("arguments")
    argument_text = arguments if isinstance(arguments, str) else ""
    args: dict[str, object] | None = None
    if argument_text:
        try:
            args = _json_object_bytes(
                argument_text.encode("utf-8"), "rollout.spawn_agent arguments"
            )
        except HostRecordError:
            args = None
    if args is None and task_name in argument_text:
        # Malformed arguments are skipped only when they cannot be ours.
        raise HostRecordError("rollout.spawn_agent arguments are malformed")
    call_task = args.get("task_name") if args is not None else None
    raw_call_id = payload.get("call_id")
    if call_task != task_name:
        if isinstance(raw_call_id, str):
            if raw_call_id in matching:
                raise HostRecordError("rollout.spawn_agent contains duplicate call ids")
            other_call_ids.add(raw_call_id)
        return
    if args is None:
        raise HostRecordError("rollout.spawn_agent arguments are malformed")
    call_id = _required_text(raw_call_id, "rollout.spawn_agent.call_id")
    if call_id in matching or call_id in other_call_ids:
        raise HostRecordError("rollout.spawn_agent contains duplicate call ids")
    if matching:
        raise HostRecordError("rollout.spawn_agent has multiple calls for the reserved task name")
    if set(args) != {"agent_type", "fork_turns", "task_name", "message"}:
        raise HostRecordError("rollout.spawn_agent arguments have an unexpected key set")
    agent_type = _required_text(args.get("agent_type"), "rollout.spawn_agent.agent_type")
    fork_turns = _required_text(args.get("fork_turns"), "rollout.spawn_agent.fork_turns")
    _required_text(args.get("message"), "rollout.spawn_agent.message")
    if agent_type != expected_agent_type or fork_turns != "none":
        raise HostRecordError("rollout.spawn_agent role or fork_turns is not canonical")
    matching[call_id] = (
        agent_type,
        _optional_timestamp_ms(event.get("timestamp"), "rollout.spawn_agent.timestamp"),
    )


def _spawn_output_task_name(output: object) -> str | None:
    if not isinstance(output, str):
        return None
    try:
        output_obj = _json_object_bytes(output.encode("utf-8"), "rollout.spawn_agent output")
    except HostRecordError:
        return None
    candidate = output_obj.get("task_name")
    if candidate is None:
        return None
    return _required_text(candidate, "rollout.spawn_agent.result.task_name")


def _managed_rollout_path(value: object, codex_home: Path, label: str) -> Path:
    """Return a rollout path only if it is the official path under ``sessions/``."""

    path = Path(_required_text(value, label))
    sessions = codex_home / "sessions"
    if not path.is_absolute():
        raise HostRecordError(f"{label} must be absolute")
    try:
        relative = path.relative_to(sessions)
    except ValueError as exc:
        raise HostRecordError(f"{label} is outside the managed Codex sessions directory") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise HostRecordError(f"{label} is outside the managed Codex sessions directory")
    current = sessions
    for component in (None, *relative.parts[:-1]):
        if component is not None:
            current = current / component
        try:
            component_stat = current.lstat()
        except OSError as exc:
            raise HostRecordError(f"{label} has a missing parent directory") from exc
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
            raise HostRecordError(f"{label} has an unsafe parent path")
    try:
        final_stat = path.lstat()
    except OSError as exc:
        raise HostRecordError(f"{label} is missing or unreadable") from exc
    if stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISREG(final_stat.st_mode):
        raise HostRecordError(f"{label} must be a regular non-symlink file")
    return path


def _native_thread_source(value: object, field: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            raw = json.loads(value, object_pairs_hook=_strict_json_pairs)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HostRecordError(f"{field} is not valid JSON") from exc
    elif isinstance(value, dict):
        raw = value
    else:
        raise HostRecordError(f"{field} is not an object")
    if not isinstance(raw, dict):
        raise HostRecordError(f"{field} is not an object")
    return cast(dict[str, object], raw)


def _native_child_origin_matches(
    *,
    source: Mapping[str, object],
    owner_session_key: str,
    owner_thread_id: str,
) -> bool:
    """Require parent/origin metadata from the official child records."""

    spawn = source.get("subagent")
    if not isinstance(spawn, dict):
        return False
    thread_spawn = spawn.get("thread_spawn")
    if not isinstance(thread_spawn, dict):
        return False
    parent_thread = thread_spawn.get("parent_thread_id")
    if parent_thread != owner_thread_id:
        return False
    parent_session = thread_spawn.get("parent_session_key", thread_spawn.get("parentSessionKey"))
    # The Codex ``threads`` row carries the parent thread edge only; the
    # installed host writes no OpenClaw session row for a native child, so a
    # parent session key is checked only when the child source carries one.
    return parent_session is None or parent_session == owner_session_key


def _read_native_announce(
    openclaw_database: Path | str,
    owner_session_key: str,
    owner_thread_id: str,
    child_thread_id: str,
) -> tuple[str, Literal["succeeded", "failed"]] | None:
    """Find the completion-callback run for one child in the owner store.

    The host records a native completion callback only as an owner-session
    run named ``announce:codex-native:<spawning-thread>:<child>:<status>``.
    The callback runs on whatever owner thread is current, so only its run id
    and session key are bound.  Zero callbacks is ``None`` (still pending);
    more than one is ambiguous.
    """

    prefix = f"{_ANNOUNCE_PREFIX}{owner_thread_id}:{child_thread_id}:"
    with _readonly_database(openclaw_database) as connection:
        if "trajectory_runtime_events" not in _sqlite_tables(connection):
            raise HostRecordError("native OpenClaw database has no runtime event schema")
        try:
            rows = connection.execute(
                "SELECT session_id, run_id, event_json, created_at "
                "FROM trajectory_runtime_events "
                "WHERE substr(run_id, 1, ?) = ? "
                "AND json_extract(event_json, '$.type') = 'session.started' "
                "ORDER BY created_at, seq",
                (len(prefix), prefix),
            ).fetchall()
        except sqlite3.Error as exc:
            raise HostRecordError("native completion callback could not be read") from exc
    found: list[tuple[str, Literal["succeeded", "failed"]]] = []
    for row in rows:
        event = _native_event(row[2], "native completion callback event")
        if event.get("sessionKey") != owner_session_key:
            continue
        run_id = _required_text(row[1], "native completion callback run_id")
        if event.get("runId") != run_id or event.get("sessionId") != str(row[0]):
            raise HostRecordError("native completion callback identity does not match its row")
        status = run_id[len(prefix) :]
        if status == "succeeded":
            found.append((run_id, "succeeded"))
        elif status == "failed":
            found.append((run_id, "failed"))
        else:
            raise HostRecordError("native completion callback status is not recognized")
    if len(found) > 1:
        raise HostRecordError("native completion callback is ambiguous")
    return found[0] if found else None


def read_exact_native_child(
    openclaw_database: Path | str,
    codex_state_database: Path | str,
    owner_session_key: str,
    owner_run_id: str,
    owner_thread_id: str,
    task_name: str,
    reserved_at_ms: int,
    *,
    expected_agent_role: str = "reviewer",
    expected_model: str = "gpt-5.6-sol",
    expected_effort: str = "xhigh",
    require_completion_callback: bool = False,
) -> NativeChildHostRecord:
    """Correlate one native child across OpenClaw and Codex official stores.

    This function intentionally does not consult ``task_runs`` or thread-edge
    status (an edge stays ``open`` after completion).  It requires the
    owner's exact spawn call/result, a parent-bound child thread whose
    plaintext task name matches everywhere the host records it, and the
    child's official rollout.  The spawn prompt is ciphertext in the host
    records, so it cannot be compared with the reserved prompt digest; binding
    rests on the 96-bit-nonce task name plus host role/model/effort and the
    caller's strict verdict binding.

    With ``require_completion_callback`` the owner store must also hold the
    child's ``announce:codex-native`` callback run: a missing callback keeps
    the child ``pending``, ``failed`` maps to ``failed``/``cancelled``, and
    ``succeeded`` is honoured only with a successful terminal rollout.
    """

    owner_session_key = _validate_lookup_text(owner_session_key, "owner_session_key")
    owner_run_id = _validate_lookup_text(owner_run_id, "owner_run_id")
    owner_thread_id = _validate_lookup_text(owner_thread_id, "owner_thread_id")
    task_name = _validate_lookup_text(task_name, "task_name")
    if _NATIVE_TASK_NAME.fullmatch(task_name) is None:
        raise HostRecordError("native task name is not canonical")
    reserved_at_ms = _required_epoch_ms(reserved_at_ms, "reserved_at_ms")
    expected_agent_role = _validate_lookup_text(expected_agent_role, "expected_agent_role")
    expected_model = _validate_lookup_text(expected_model, "expected_model")
    expected_effort = _validate_lookup_text(expected_effort, "expected_effort")

    # Bind the reservation to the owner's official runtime event first.  The
    # old task_runs projection is empty on current installs and is deliberately
    # not consulted by this native path.
    owner = read_exact_native_owner(
        openclaw_database,
        owner_session_key,
        reserved_at_ms,
        expected_run_id=owner_run_id,
        expected_thread_id=owner_thread_id,
    )
    if owner.ended_at_ms is None:
        raise HostRecordPending("native owner interval is still open")

    codex_home = Path(codex_state_database).parent
    runtime_task_name = f"/root/{task_name}"
    with _readonly_database(codex_state_database) as connection:
        tables = _sqlite_tables(connection)
        if not {"threads", "thread_spawn_edges"}.issubset(tables):
            raise HostRecordError("Codex state database has no native thread schema")
        try:
            parent_rows = connection.execute(
                "SELECT id, rollout_path FROM threads WHERE id = ?", (owner_thread_id,)
            ).fetchall()
            child_rows = connection.execute(
                "SELECT t.*, e.parent_thread_id "
                "FROM threads AS t JOIN thread_spawn_edges AS e ON e.child_thread_id = t.id "
                "WHERE e.parent_thread_id = ? AND t.agent_path = ?",
                (owner_thread_id, runtime_task_name),
            ).fetchall()
        except sqlite3.Error as exc:
            raise HostRecordError("Codex native thread metadata could not be read") from exc
    if len(parent_rows) != 1:
        raise HostRecordError("native parent thread is missing or ambiguous")
    parent_rollout = read_native_parent_rollout(
        _managed_rollout_path(parent_rows[0][1], codex_home, "native parent rollout path"),
        task_name,
        agent_type=expected_agent_role,
    )
    if parent_rollout.thread_id != owner_thread_id:
        raise HostRecordError("native parent rollout thread id does not match owner")
    # The spawn call timestamp comes from the function_call event, not its output.
    matching_calls = [
        call
        for call in parent_rollout.spawn_calls
        if call.agent_type == expected_agent_role
        and call.task_name == task_name
        and call.observed_at_ms is not None
        and owner.started_at_ms <= call.observed_at_ms < owner.ended_at_ms
        and owner.started_at_ms <= reserved_at_ms <= call.observed_at_ms
    ]
    if len(matching_calls) != 1:
        raise HostRecordError("native parent spawn call correlation is missing or ambiguous")
    matching_call = matching_calls[0]
    if matching_call.result_task_name != runtime_task_name:
        raise HostRecordError("native spawn result does not bind the exact task name")

    candidates: list[sqlite3.Row] = []
    for row in child_rows:
        if row["thread_source"] != "subagent" or row["model_provider"] != "openai":
            continue
        # ``name`` is NULL for every installed native child; a non-null name
        # must still be the runtime task name.
        if row["name"] is not None and row["name"] != runtime_task_name:
            continue
        source = _native_thread_source(row["source"], "native child source")
        if not _native_child_origin_matches(
            source=source,
            owner_session_key=owner_session_key,
            owner_thread_id=owner_thread_id,
        ):
            continue
        subagent = cast(dict[str, object], source["subagent"])
        thread_spawn = cast(dict[str, object], subagent["thread_spawn"])
        if (
            row["agent_path"] != runtime_task_name
            or thread_spawn.get("agent_path") != runtime_task_name
            or thread_spawn.get("agent_role") != expected_agent_role
            or thread_spawn.get("parent_thread_id") != owner_thread_id
            or row["agent_role"] != expected_agent_role
        ):
            continue
        if row["model"] != expected_model or row["reasoning_effort"] != expected_effort:
            raise HostRecordError("native child model or effort does not match reservation")
        candidates.append(row)
    if len(candidates) != 1:
        raise HostRecordError("native child thread correlation is missing or ambiguous")
    child_row = candidates[0]
    child_thread_id = _required_text(child_row["id"], "native child thread id")
    if (
        "created_at_ms" in set(child_row.keys())
        and child_row["created_at_ms"] is not None
        and _required_epoch_ms(child_row["created_at_ms"], "native child created_at_ms")
        < reserved_at_ms
    ):
        raise HostRecordError("native child thread was created before reservation")
    # A child reviewer is one-shot evidence.  Multiple terminal markers are
    # not a later turn to merge; they are an immutable corruption/refusal.
    child_rollout_path = _managed_rollout_path(
        child_row["rollout_path"], codex_home, "native child rollout path"
    )
    rollout = read_exact_rollout(
        child_rollout_path,
        max_bytes=MAX_CHILD_ROLLOUT_BYTES,
        unterminated_tail="ignore",
        digest_through_terminal=True,
    )
    if rollout.thread_id != child_thread_id:
        raise HostRecordError("native child rollout thread id does not match thread store")
    if rollout.parent_thread_id != owner_thread_id:
        raise HostRecordError("native child rollout parent does not match owner")
    if rollout.agent_role != expected_agent_role:
        raise HostRecordError("native child rollout role does not match reservation")
    if rollout.model != expected_model or rollout.reasoning_effort != expected_effort:
        raise HostRecordError("native child rollout model or effort does not match reservation")

    terminal_state: TerminalState = rollout.terminal_state
    announce: tuple[str, Literal["succeeded", "failed"]] | None = None
    if require_completion_callback:
        announce = _read_native_announce(
            openclaw_database, owner_session_key, owner_thread_id, child_thread_id
        )
        if announce is None:
            terminal_state = "pending"
        elif announce[1] == "failed":
            terminal_state = "cancelled" if rollout.terminal_state == "cancelled" else "failed"
        elif rollout.terminal_state == "pending":
            terminal_state = "pending"
    return NativeChildHostRecord(
        owner_session_key=owner_session_key,
        owner_run_id=owner_run_id,
        parent_thread_id=owner_thread_id,
        child_thread_id=child_thread_id,
        task_name=runtime_task_name,
        model=expected_model,
        reasoning_effort=expected_effort,
        agent_role=expected_agent_role,
        rollout_path=child_rollout_path,
        rollout_sha256=rollout.sha256,
        terminal_state=terminal_state,
        last_agent_message=(rollout.last_agent_message if terminal_state == "succeeded" else None),
        spawn_call_id=matching_call.call_id,
        announce_run_id=announce[0] if announce is not None else None,
        announce_status=announce[1] if announce is not None else None,
    )
