"""Read-only host records for the OpenClaw ``claude-cli`` runtime.

The Claude profile has no Codex rollout or thread store.  Durable truth lives in:

* ``<state>/state/openclaw.sqlite``: ``task_runs`` (status, timing, linkage) and
  ``subagent_runs`` (child spawn payload);
* ``<state>/agents/<agent_id>/agent/openclaw-agent.sqlite``: ``session_nodes`` (model,
  explicit thinking level, ``claudeCliSessionId``, workspace);
* the Claude Code transcript
  ``<claude_config_dir>/projects/<cwd-slug>/<claudeCliSessionId>.jsonl``, whose
  ``api-request`` records carry the authoritative ``params.model`` and
  ``params.output_config.effort``.

Everything here is read-only and fails closed: malformed records raise
``ClaudeRecordError``.  Missing optional evidence (no agent database, no session node,
no transcript) is reported as ``None``/empty sets so a caller comparing against a pin
refuses rather than guesses.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gateway.agent_profiles import AgentProfileError, claude_config_dir, deployed_profile_path

from .host_records import HostRecordError, _readonly_database

MAX_CLAUDE_TRANSCRIPT_BYTES = 256 * 1024 * 1024
MAX_CLAUDE_TRANSCRIPT_LINE_BYTES = 32 * 1024 * 1024
MAX_CLAUDE_JSON_CELL_BYTES = 8 * 1024 * 1024

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_API_REQUEST_MARKERS = (b'"type":"api-request"', b'"type": "api-request"')
_OUTCOMES: dict[str, Literal["ok", "error", "lost", "pending"]] = {
    "succeeded": "ok",
    "failed": "error",
    "error": "error",
    "cancelled": "error",
    "timed_out": "error",
    "lost": "lost",
    "running": "pending",
    "queued": "pending",
}

_TASK_COLUMNS = (
    "task_id, runtime, requester_session_key, child_session_key, owner_key, agent_id, "
    "run_id, task, status, created_at, started_at, ended_at"
)


class ClaudeRecordError(HostRecordError):
    """A Claude-profile host record is missing, malformed, ambiguous, or inconsistent."""


class ClaudeRecordNotRecorded(ClaudeRecordError):
    """The gateway has no ``task_runs`` row for the requested run."""


@dataclass(frozen=True, slots=True)
class ClaudeRun:
    run_id: str
    agent_id: str
    session_key: str
    requester_session_key: str | None
    runtime: str
    status: str
    started_at_ms: int | None
    ended_at_ms: int | None
    model: str | None
    thinking_level: str | None
    claude_session_id: str | None
    transcript_models: frozenset[str]
    transcript_efforts: frozenset[str]
    task_text: str | None
    cwd: str | None
    # Added for AP3b: the child's final reply text (subagent_runs completion), else None.
    result_text: str | None = None


@dataclass(frozen=True, slots=True)
class ClaudeStores:
    openclaw_state_db: Path
    agents_dir: Path
    claude_config_dir: Path


def claude_stores(state_dir: Path | None = None) -> ClaudeStores:
    """Derive the Claude-profile stores from the OpenClaw state dir (env or ``~/.openclaw``)."""
    try:
        base = deployed_profile_path(state_dir).parent
        config = claude_config_dir(state_dir)
    except AgentProfileError as exc:
        raise ClaudeRecordError(str(exc)) from exc
    return ClaudeStores(
        openclaw_state_db=base / "state" / "openclaw.sqlite",
        agents_dir=base / "agents",
        claude_config_dir=config,
    )


@dataclass(frozen=True, slots=True)
class _TaskRow:
    task_id: str
    runtime: str
    requester_session_key: str | None
    child_session_key: str | None
    owner_key: str
    agent_id: str
    run_id: str
    task: str
    status: str
    created_at_ms: int
    started_at_ms: int | None
    ended_at_ms: int | None


def _opt_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ClaudeRecordError(f"{field} must be a non-empty string when present")
    return value


def _text(value: object, field: str) -> str:
    text = _opt_text(value, field)
    if text is None:
        raise ClaudeRecordError(f"{field} is required")
    return text


def _opt_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClaudeRecordError(f"{field} must be a non-negative integer when present")
    return value


def _task_row(row: sqlite3.Row) -> _TaskRow:
    created = _opt_int(row["created_at"], "task_runs.created_at")
    if created is None:
        raise ClaudeRecordError("task_runs.created_at is required")
    task = row["task"]
    if not isinstance(task, str):
        raise ClaudeRecordError("task_runs.task must be text")
    return _TaskRow(
        task_id=_text(row["task_id"], "task_runs.task_id"),
        runtime=_text(row["runtime"], "task_runs.runtime"),
        requester_session_key=_opt_text(
            row["requester_session_key"], "task_runs.requester_session_key"
        ),
        child_session_key=_opt_text(row["child_session_key"], "task_runs.child_session_key"),
        owner_key=_text(row["owner_key"], "task_runs.owner_key"),
        agent_id=_text(row["agent_id"], "task_runs.agent_id"),
        run_id=_text(row["run_id"], "task_runs.run_id"),
        task=task,
        status=_text(row["status"], "task_runs.status"),
        created_at_ms=created,
        started_at_ms=_opt_int(row["started_at"], "task_runs.started_at"),
        ended_at_ms=_opt_int(row["ended_at"], "task_runs.ended_at"),
    )


@contextmanager
def _state_connection(stores: ClaudeStores) -> Iterator[sqlite3.Connection]:
    try:
        with _readonly_database(stores.openclaw_state_db) as connection:
            yield connection
    except ClaudeRecordError:
        raise
    except HostRecordError as exc:
        raise ClaudeRecordError(f"openclaw state database: {exc}") from exc
    except sqlite3.Error as exc:
        raise ClaudeRecordError("openclaw state database read failed") from exc


def _json_object(raw: object, field: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ClaudeRecordError(f"{field} must be JSON text")
    if len(raw) > MAX_CLAUDE_JSON_CELL_BYTES:
        raise ClaudeRecordError(f"{field} exceeds the size limit")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeRecordError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ClaudeRecordError(f"{field} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _bare_model(value: object, field: str) -> str | None:
    text = _opt_text(value, field)
    if text is None:
        return None
    return text.split("/", 1)[1] if "/" in text else text


def _result_text(payload: dict[str, object]) -> str | None:
    completion = payload.get("completion")
    if not isinstance(completion, dict):
        return None
    text = completion.get("resultText")
    if isinstance(text, str):
        return text
    reply = completion.get("terminalReply")
    if isinstance(reply, dict) and isinstance(reply.get("text"), str):
        return str(reply["text"])
    return None


def _subagent_payload(connection: sqlite3.Connection, run_id: str) -> dict[str, object] | None:
    rows = connection.execute(
        "SELECT payload_json FROM subagent_runs WHERE run_id = ?", (run_id,)
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ClaudeRecordError("subagent_runs run_id is ambiguous")
    return _json_object(rows[0]["payload_json"], "subagent_runs.payload_json")


def _session_node(
    stores: ClaudeStores, agent_id: str, session_key: str, run_id: str, *, finished: bool
) -> dict[str, object] | None:
    """The agent's ``session_nodes`` entry, only when it still describes ``run_id``.

    A finished run must be named by ``lastRunId``; an in-flight run may predate its write.
    """
    if not _SAFE_ID.fullmatch(agent_id):
        raise ClaudeRecordError("task_runs.agent_id is not a safe agent id")
    database = stores.agents_dir / agent_id / "agent" / "openclaw-agent.sqlite"
    if not os.path.lexists(database):
        return None
    try:
        with _readonly_database(database) as connection:
            rows = connection.execute(
                "SELECT entry_json FROM session_nodes WHERE session_key = ?", (session_key,)
            ).fetchall()
    except ClaudeRecordError:
        raise
    except HostRecordError as exc:
        raise ClaudeRecordError(f"agent database: {exc}") from exc
    except sqlite3.Error as exc:
        raise ClaudeRecordError("agent database read failed") from exc
    if not rows:
        return None
    if len(rows) != 1:
        raise ClaudeRecordError("session_nodes session_key is ambiguous")
    entry = _json_object(rows[0]["entry_json"], "session_nodes.entry_json")
    last_run = entry.get("lastRunId")
    named = isinstance(last_run, str) and bool(last_run)
    if (named and last_run != run_id) or (finished and not named):
        # The session was reused by a later run (for example an owner announce turn) or
        # does not name this run; its model, effort and Claude session do not describe it.
        return None
    return entry


@dataclass(frozen=True, slots=True)
class _Transcript:
    models: frozenset[str]
    efforts: frozenset[str]
    cwd: str | None


_EMPTY_TRANSCRIPT = _Transcript(frozenset(), frozenset(), None)


def _find_transcript(config_dir: Path, session_id: str) -> Path | None:
    if not _SAFE_ID.fullmatch(session_id):
        raise ClaudeRecordError("claudeCliSessionId is not a safe session id")
    projects = config_dir / "projects"
    try:
        entries = sorted(os.scandir(projects), key=lambda entry: entry.name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ClaudeRecordError("claude projects directory is unreadable") from exc
    found: list[Path] = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        candidate = Path(entry.path) / f"{session_id}.jsonl"
        if os.path.lexists(candidate):
            found.append(candidate)
    if len(found) > 1:
        raise ClaudeRecordError("claude transcript is ambiguous across project directories")
    return found[0] if found else None


def _iter_lines(path: Path) -> Iterator[tuple[bytes, bool]]:
    """Yield ``(line, terminated)`` from a bounded regular, non-symlink file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ClaudeRecordError("claude transcript is unreadable") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ClaudeRecordError("claude transcript is not a regular file")
        if info.st_size > MAX_CLAUDE_TRANSCRIPT_BYTES:
            raise ClaudeRecordError("claude transcript exceeds the size limit")
        consumed = 0
        while True:
            line = handle.readline(MAX_CLAUDE_TRANSCRIPT_LINE_BYTES + 1)
            if not line:
                return
            consumed += len(line)
            if consumed > MAX_CLAUDE_TRANSCRIPT_BYTES:
                raise ClaudeRecordError("claude transcript exceeds the size limit")
            terminated = line.endswith(b"\n")
            if len(line) > MAX_CLAUDE_TRANSCRIPT_LINE_BYTES and not terminated:
                raise ClaudeRecordError("claude transcript line exceeds the size limit")
            yield line, terminated


def _api_request(line: bytes, terminated: bool, session_id: str) -> tuple[str, str | None] | None:
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if not terminated:
            return None  # a record still being written
        raise ClaudeRecordError("claude transcript api-request record is not valid JSON") from exc
    if not isinstance(record, dict):
        raise ClaudeRecordError("claude transcript api-request record is not an object")
    if record.get("type") != "api-request":
        return None
    if record.get("sessionId") != session_id:
        raise ClaudeRecordError("claude transcript api-request has a missing or foreign sessionId")
    params = record.get("params")
    if not isinstance(params, dict):
        raise ClaudeRecordError("claude transcript api-request has no params object")
    model = params.get("model")
    if not isinstance(model, str) or not model:
        raise ClaudeRecordError("claude transcript api-request has no model")
    output_config = params.get("output_config")
    effort: str | None = None
    if output_config is not None:
        if not isinstance(output_config, dict):
            raise ClaudeRecordError("claude transcript api-request output_config is malformed")
        raw_effort = output_config.get("effort")
        if raw_effort is not None:
            if not isinstance(raw_effort, str) or not raw_effort:
                raise ClaudeRecordError("claude transcript api-request effort is malformed")
            effort = raw_effort
    return model, effort


MAX_CLAUDE_SUBAGENT_FILES = 4096
MAX_CLAUDE_SUBAGENT_DEPTH = 8


def _subagent_transcripts(path: Path, session_id: str) -> list[Path]:
    """Every ``*.jsonl`` under ``<slug>/<sessionId>/`` (subagents, workflow journals, ...).

    The walk is bounded in depth and file count and refuses any symlink or non-plain entry.
    """
    root = path.parent / session_id
    try:
        info = root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ClaudeRecordError("claude session directory is unreadable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ClaudeRecordError("claude session directory is not a plain directory")
    found: list[Path] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_CLAUDE_SUBAGENT_DEPTH:
            raise ClaudeRecordError("claude session directory nesting exceeds the limit")
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ClaudeRecordError("claude session directory is unreadable") from exc
        for entry in entries:
            if entry.is_symlink():
                raise ClaudeRecordError("claude session directory contains a symlink")
            if entry.is_dir(follow_symlinks=False):
                pending.append((Path(entry.path), depth + 1))
            elif entry.name.endswith(".jsonl"):
                found.append(Path(entry.path))
                if len(found) > MAX_CLAUDE_SUBAGENT_FILES:
                    raise ClaudeRecordError("claude session directory has too many transcripts")
    return sorted(found)


def transcript_files(stores: ClaudeStores, session_id: str) -> tuple[Path, ...]:
    """Every transcript file this module reads for ``session_id``: main first, then the rest.

    Uses the same lookup (``_find_transcript``) and walk (``_subagent_transcripts``) as
    ``read_run``, so a caller digesting evidence covers exactly what was parsed.  Returns
    ``()`` when the session has no main transcript; raises ``ClaudeRecordError`` when the
    main transcript is ambiguous, a symlink is found, or a directory is unreadable.
    """
    main = _find_transcript(stores.claude_config_dir, session_id)
    if main is None:
        return ()
    return (main, *_subagent_transcripts(main, session_id))


def _read_transcript(path: Path, session_id: str) -> _Transcript:
    main = _scan_transcript(path, session_id, want_cwd=True)
    models = set(main.models)
    efforts = set(main.efforts)
    for sub in _subagent_transcripts(path, session_id):
        scanned = _scan_transcript(sub, session_id, want_cwd=False)
        models |= scanned.models
        efforts |= scanned.efforts
    return _Transcript(frozenset(models), frozenset(efforts), main.cwd)


def _scan_transcript(path: Path, session_id: str, *, want_cwd: bool) -> _Transcript:
    models: set[str] = set()
    efforts: set[str] = set()
    cwd: str | None = None
    for line, terminated in _iter_lines(path):
        if any(marker in line for marker in _API_REQUEST_MARKERS):
            parsed = _api_request(line, terminated, session_id)
            if parsed is not None:
                models.add(parsed[0])
                if parsed[1] is not None:
                    efforts.add(parsed[1])
                continue
        if want_cwd and cwd is None and b'"cwd"' in line and len(line) <= 1024 * 1024:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # unknown or partial non-api-request record
            candidate = record.get("cwd") if isinstance(record, dict) else None
            if isinstance(candidate, str) and candidate:
                cwd = candidate
    return _Transcript(frozenset(models), frozenset(efforts), cwd)


def _build_run(stores: ClaudeStores, connection: sqlite3.Connection, task: _TaskRow) -> ClaudeRun:
    session_key = task.child_session_key or task.requester_session_key or task.owner_key
    payload = _subagent_payload(connection, task.run_id) if task.runtime == "subagent" else None
    task_text: str | None = None
    payload_model: str | None = None
    result_text: str | None = None
    if payload is not None:
        result_text = _result_text(payload)
        raw_task = payload.get("task")
        task_text = raw_task if isinstance(raw_task, str) else None
        payload_model = _bare_model(payload.get("model"), "subagent_runs.payload.model")
    elif task.runtime == "subagent":
        task_text = task.task
    entry = _session_node(
        stores,
        task.agent_id,
        session_key,
        task.run_id,
        finished=_OUTCOMES.get(task.status) not in (None, "pending"),
    )
    node_model: str | None = None
    thinking: str | None = None
    claude_session: str | None = None
    cwd: str | None = None
    if entry is not None:
        node_model = _bare_model(entry.get("model"), "session_nodes.model")
        thinking = _opt_text(entry.get("thinkingLevel"), "session_nodes.thinkingLevel")
        claude_session = _opt_text(
            entry.get("claudeCliSessionId"), "session_nodes.claudeCliSessionId"
        )
        # ``spawnedCwd`` is the sessions_spawn ``cwd`` argument; ``spawnedWorkspaceDir`` is
        # always the target agent's own workspace.  The former is what the run executed in.
        cwd = _opt_text(entry.get("spawnedCwd"), "session_nodes.spawnedCwd") or _opt_text(
            entry.get("spawnedWorkspaceDir"), "session_nodes.spawnedWorkspaceDir"
        )
    transcript = _EMPTY_TRANSCRIPT
    if claude_session is not None:
        try:
            path = _find_transcript(stores.claude_config_dir, claude_session)
            if path is not None:
                transcript = _read_transcript(path, claude_session)
        except OSError as exc:
            # Fail closed and typed: callers must never see a raw OSError from the walk.
            raise ClaudeRecordError(
                f"claude transcript walk failed: {exc.strerror or exc}"
            ) from exc
    if node_model is not None and payload_model is not None and node_model != payload_model:
        raise ClaudeRecordError("session_nodes model disagrees with the subagent_runs model")
    if (
        cwd is not None
        and transcript.cwd is not None
        and os.path.realpath(cwd) != os.path.realpath(transcript.cwd)
    ):
        raise ClaudeRecordError("claude transcript cwd disagrees with the session node cwd")
    return ClaudeRun(
        run_id=task.run_id,
        agent_id=task.agent_id,
        session_key=session_key,
        requester_session_key=task.requester_session_key,
        runtime=task.runtime,
        status=task.status,
        started_at_ms=task.started_at_ms,
        ended_at_ms=task.ended_at_ms,
        model=node_model or payload_model,
        thinking_level=thinking,
        claude_session_id=claude_session,
        transcript_models=transcript.models,
        transcript_efforts=transcript.efforts,
        task_text=task_text,
        cwd=cwd or transcript.cwd,
        result_text=result_text,
    )


def read_run(stores: ClaudeStores, run_id: str) -> ClaudeRun:
    """Join one run's ``task_runs`` row with its agent session and Claude transcript."""
    if not isinstance(run_id, str) or not run_id:
        raise ClaudeRecordError("run_id must be a non-empty string")
    with _state_connection(stores) as connection:
        try:
            rows = connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchall()
        except sqlite3.Error as exc:
            raise ClaudeRecordError("task_runs could not be read") from exc
        if not rows:
            raise ClaudeRecordNotRecorded(f"no task_runs row for run {run_id}")
        if len(rows) != 1:
            raise ClaudeRecordError(f"task_runs run_id {run_id} is ambiguous")
        return _build_run(stores, connection, _task_row(rows[0]))


def child_runs(
    stores: ClaudeStores,
    requester_session_key: str,
    *,
    since_ms: int,
    agent_id: str | None = None,
) -> tuple[ClaudeRun, ...]:
    """Subagent runs requested by ``requester_session_key`` created at or after ``since_ms``.

    ``agent_id`` filters before any session or transcript is read, so an unrelated child's
    malformed record cannot block a caller that only cares about one agent.
    """
    if not isinstance(requester_session_key, str) or not requester_session_key:
        raise ClaudeRecordError("requester_session_key must be a non-empty string")
    if isinstance(since_ms, bool) or not isinstance(since_ms, int) or since_ms < 0:
        raise ClaudeRecordError("since_ms must be a non-negative integer")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id):
        raise ClaudeRecordError("agent_id must be a non-empty string")
    with _state_connection(stores) as connection:
        try:
            rows = connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM task_runs WHERE runtime = 'subagent' "
                "AND requester_session_key = ? AND created_at >= ? "
                "AND (? IS NULL OR agent_id = ?) ORDER BY created_at, task_id",
                (requester_session_key, since_ms, agent_id, agent_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ClaudeRecordError("task_runs could not be read") from exc
        return tuple(_build_run(stores, connection, _task_row(row)) for row in rows)


def run_outcome(stores: ClaudeStores, run_id: str) -> Literal["ok", "error", "lost", "pending"]:
    """Map the durable ``task_runs.status`` to an outcome; unknown statuses fail closed."""
    if not isinstance(run_id, str) or not run_id:
        raise ClaudeRecordError("run_id must be a non-empty string")
    with _state_connection(stores) as connection:
        try:
            rows = connection.execute(
                "SELECT status FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchall()
        except sqlite3.Error as exc:
            raise ClaudeRecordError("task_runs could not be read") from exc
    if not rows:
        raise ClaudeRecordNotRecorded(f"no task_runs row for run {run_id}")
    if len(rows) != 1:
        raise ClaudeRecordError(f"task_runs run_id {run_id} is ambiguous")
    status = rows[0]["status"]
    outcome = _OUTCOMES.get(status) if isinstance(status, str) else None
    if outcome is None:
        raise ClaudeRecordError(f"unknown task_runs status {status!r}")
    return outcome
