"""Black-box tests for read-only host review-record correlation."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from gateway.research.host_records import (
    MAX_METADATA_BYTES,
    MAX_TRANSCRIPT_BYTES,
    AcpIdentityHostRecord,
    HostRecordError,
    TaskRunHostRecord,
    derive_claude_transcript_path,
    read_exact_acp_identity,
    read_exact_claude_transcript,
    read_exact_rollout,
    read_exact_task_run,
)

OWNER: Final = "agent:research-orchestrator:autoresearch:quantipy-v2"
CHILD_KEY: Final = "agent:claude:acp:child-001"
TASK_ID: Final = "task-001"
RUN_ID: Final = "run-001"
LABEL: Final = "H0001-A001-review-nonce-001"
RESERVED_AT: Final = 1_756_000_000_000
UUID: Final = "84785a20-b278-4d4a-9c62-fa35e5bb9928"
OPENCLAW_SESSION_ID: Final = "openclaw-child-session-001"
EXPECTED_CWD: Final = "/tmp/review worktree/H0001-A001"
ACP_BACKEND: Final = "acpx"
ACP_AGENT: Final = "claude"
ACP_MODE: Final = "oneshot"
ROLLOUT_THREAD_ID: Final = "thread-rollout-001"
ROLLOUT_MODEL: Final = "gpt-5.6-luna"
ROLLOUT_EFFORT: Final = "xhigh"


def _read_task(
    database_path: Path | str,
    owner_key: str = OWNER,
    reservation_label: str = LABEL,
    reserved_at_ms: int = RESERVED_AT,
    *,
    expected_runtime: str = "acp",
    expected_scope_kind: str = "session",
    expected_agent_id: str = "claude",
    ack_child_session_key: str | None = None,
    ack_run_id: str | None = None,
) -> TaskRunHostRecord:
    return read_exact_task_run(
        database_path,
        owner_key,
        reservation_label,
        reserved_at_ms,
        expected_runtime=expected_runtime,
        expected_scope_kind=expected_scope_kind,
        expected_agent_id=expected_agent_id,
        ack_child_session_key=ack_child_session_key,
        ack_run_id=ack_run_id,
    )


def _read_acp(
    database_path: Path | str,
    child_session_key: str,
    claude_sessions_path: Path | str,
    expected_cwd: str,
    *,
    expected_backend: str = ACP_BACKEND,
    expected_agent: str = ACP_AGENT,
    expected_mode: str = ACP_MODE,
) -> AcpIdentityHostRecord:
    return read_exact_acp_identity(
        database_path,
        child_session_key,
        claude_sessions_path,
        expected_cwd,
        expected_backend=expected_backend,
        expected_agent=expected_agent,
        expected_mode=expected_mode,
    )


def _rollout_bytes(*events: Mapping[str, object]) -> bytes:
    return (
        b"\n".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
            for event in events
        )
        + b"\n"
    )


def _session_meta() -> dict[str, object]:
    return {
        "type": "session_meta",
        "payload": {
            "id": ROLLOUT_THREAD_ID,
            "model": ROLLOUT_MODEL,
            "reasoning_effort": ROLLOUT_EFFORT,
        },
    }


def _token_count(
    total_tokens: int,
    model_context_window: int | None,
    *,
    source: str = "last",
    sibling_context: int | None = None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {"total_tokens": total_tokens}
    if model_context_window is not None:
        snapshot["model_context_window"] = model_context_window
    info: dict[str, object] = {f"{source}_token_usage": snapshot}
    if sibling_context is not None:
        info["model_context_window"] = sibling_context
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": info,
        },
    }


@pytest.fixture()
def host_fixture(tmp_path: Path) -> dict[str, Path | str]:
    db = tmp_path / "state" / "openclaw.sqlite"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE task_runs (
              task_id TEXT NOT NULL, runtime TEXT NOT NULL, task_kind TEXT,
              source_id TEXT, requester_session_key TEXT, owner_key TEXT NOT NULL,
              scope_kind TEXT NOT NULL, child_session_key TEXT, agent_id TEXT,
              requester_agent_id TEXT, run_id TEXT, label TEXT, status TEXT,
              created_at INTEGER, started_at INTEGER, ended_at INTEGER, error TEXT
            );
            CREATE TABLE acp_sessions (
              session_key TEXT PRIMARY KEY, session_id TEXT, backend TEXT,
              agent TEXT, runtime_session_name TEXT, identity_json TEXT,
              mode TEXT, runtime_options_json TEXT, cwd TEXT, state TEXT,
              last_activity_at INTEGER, updated_at INTEGER
            );
            """
        )
        connection.execute(
            """
            INSERT INTO task_runs (
              task_id, runtime, task_kind, source_id, requester_session_key,
              owner_key, scope_kind, child_session_key, agent_id,
              requester_agent_id, run_id, label, status, created_at,
              started_at, ended_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                TASK_ID,
                "acp",
                "spawn",
                "source-001",
                OWNER,
                OWNER,
                "session",
                CHILD_KEY,
                "claude",
                "research-orchestrator",
                RUN_ID,
                LABEL,
                "running",
                RESERVED_AT + 100,
                RESERVED_AT + 200,
                None,
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO acp_sessions (
              session_key, session_id, backend, agent, runtime_session_name,
              identity_json, mode, runtime_options_json, cwd, state,
              last_activity_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                CHILD_KEY,
                OPENCLAW_SESSION_ID,
                "acpx",
                "claude",
                "claude",
                json.dumps(
                    {
                        "state": "resolved",
                        "agentSessionId": UUID,
                        "acpxSessionId": UUID,
                    }
                ),
                "oneshot",
                json.dumps({"cwd": EXPECTED_CWD}),
                "/wrong/row-cwd",
                "active",
                RESERVED_AT + 300,
                RESERVED_AT + 300,
            ),
        )
    sessions = tmp_path / "claude-sessions.json"
    sessions.write_text(
        json.dumps({CHILD_KEY: {"sessionId": OPENCLAW_SESSION_ID, "updatedAt": RESERVED_AT}}),
        encoding="utf-8",
    )
    projects = tmp_path / "claude-projects" / "projects"
    transcript = derive_claude_transcript_path(projects, EXPECTED_CWD, UUID)
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"assistant","bytes":[0,255]}\n')
    return {"db": db, "sessions": sessions, "projects": projects, "transcript": transcript}


def test_task_lookup_returns_exact_row_and_pending_status(
    host_fixture: dict[str, Path | str],
) -> None:
    record = _read_task(
        host_fixture["db"],
        ack_child_session_key=CHILD_KEY,
        ack_run_id=RUN_ID,
    )

    assert record.task_id == TASK_ID
    assert record.child_session_key == CHILD_KEY
    assert record.status == "running"
    assert record.is_pending


@pytest.mark.parametrize("status", ["queued", "running", "finalizing"])
def test_nonterminal_task_status_is_pending(
    host_fixture: dict[str, Path | str], status: str
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE task_runs SET status = ?", (status,))

    record = _read_task(host_fixture["db"])

    assert record.status == status
    assert record.is_pending


@pytest.mark.parametrize("status", ["succeeded", "failed", "timed_out", "cancelled", "lost"])
def test_terminal_task_status_remains_distinguishable(
    host_fixture: dict[str, Path | str], status: str
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE task_runs SET status = ?", (status,))

    record = _read_task(host_fixture["db"])

    assert record.status == status
    assert record.is_terminal
    assert not record.is_pending


@pytest.mark.parametrize(
    ("field", "value"),
    [("owner_key", "other-owner"), ("label", "other-label")],
)
def test_task_lookup_requires_exact_owner_and_label(
    host_fixture: dict[str, Path | str], field: str, value: str
) -> None:
    kwargs = {"owner_key": OWNER, "reservation_label": LABEL}
    kwargs["owner_key" if field == "owner_key" else "reservation_label"] = value

    with pytest.raises(HostRecordError, match="exactly one"):
        _read_task(
            host_fixture["db"],
            owner_key=kwargs["owner_key"],
            reservation_label=kwargs["reservation_label"],
        )


@pytest.mark.parametrize("requester_session_key", [None, ""])
def test_task_lookup_accepts_null_or_empty_requester_session_key(
    host_fixture: dict[str, Path | str], requester_session_key: str | None
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE task_runs SET requester_session_key = ?",
            (requester_session_key,),
        )

    record = _read_task(host_fixture["db"])

    assert record.requester_session_key == requester_session_key


def test_task_lookup_rejects_nonempty_requester_session_key_for_other_owner(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE task_runs SET requester_session_key = ?",
            ("agent:other-owner",),
        )

    with pytest.raises(HostRecordError, match="requester session"):
        _read_task(host_fixture["db"])


def test_task_lookup_rejects_multiple_rows_and_conflicting_ack(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            """
            INSERT INTO task_runs
            SELECT task_id || '-2', runtime, task_kind, source_id,
                   requester_session_key, owner_key, scope_kind,
                   child_session_key, agent_id, requester_agent_id,
                   run_id || '-2', label, status, created_at, started_at,
                   ended_at, error
              FROM task_runs
            """
        )

    with pytest.raises(HostRecordError, match="exactly one"):
        _read_task(host_fixture["db"])

    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("DELETE FROM task_runs WHERE task_id != ?", (TASK_ID,))

    with pytest.raises(HostRecordError, match="ACK"):
        _read_task(host_fixture["db"], ack_run_id="wrong-run")


def test_task_lookup_rejects_task_created_before_reservation(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE task_runs SET created_at = ?", (RESERVED_AT - 1,))

    with pytest.raises(HostRecordError, match="reservation"):
        _read_task(host_fixture["db"])


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_task_lookup_rejects_timestamp_before_creation(
    host_fixture: dict[str, Path | str], field: str
) -> None:
    statements = {
        "started_at": "UPDATE task_runs SET started_at = ?",
        "ended_at": "UPDATE task_runs SET ended_at = ?",
    }
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(statements[field], (RESERVED_AT,))

    with pytest.raises(HostRecordError, match="before"):
        _read_task(host_fixture["db"])


def test_task_lookup_rejects_end_before_start(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE task_runs SET started_at = ?, ended_at = ?",
            (RESERVED_AT + 500, RESERVED_AT + 400),
        )

    with pytest.raises(HostRecordError, match="before it started"):
        _read_task(host_fixture["db"])


@pytest.mark.parametrize(
    ("column", "value"),
    [("runtime", "subagent"), ("scope_kind", "global"), ("agent_id", "codex")],
)
def test_task_lookup_rejects_wrong_identity(
    host_fixture: dict[str, Path | str], column: str, value: str
) -> None:
    statements = {
        "runtime": "UPDATE task_runs SET runtime = ?",
        "scope_kind": "UPDATE task_runs SET scope_kind = ?",
        "agent_id": "UPDATE task_runs SET agent_id = ?",
    }
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(statements[column], (value,))

    with pytest.raises(HostRecordError, match="identity"):
        _read_task(host_fixture["db"])


@pytest.mark.parametrize(
    ("field", "value"),
    [("runtime", "subagent"), ("scope_kind", "global"), ("agent_id", "codex")],
)
def test_task_lookup_rejects_wrong_expected_identity_without_mutating_row(
    host_fixture: dict[str, Path | str], field: str, value: str
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        before = connection.execute(
            "SELECT runtime, scope_kind, agent_id FROM task_runs"
        ).fetchone()

    with pytest.raises(HostRecordError, match="identity"):
        if field == "runtime":
            _read_task(host_fixture["db"], expected_runtime=value)
        elif field == "scope_kind":
            _read_task(host_fixture["db"], expected_scope_kind=value)
        else:
            _read_task(host_fixture["db"], expected_agent_id=value)

    with sqlite3.connect(host_fixture["db"]) as connection:
        after = connection.execute("SELECT runtime, scope_kind, agent_id FROM task_runs").fetchone()
    assert after == before


def test_task_lookup_accepts_non_acp_expected_identity_without_fixture_mutation(
    host_fixture: dict[str, Path | str], tmp_path: Path
) -> None:
    database = tmp_path / "non-acp.sqlite"
    shutil.copyfile(host_fixture["db"], database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE task_runs SET runtime = ?, scope_kind = ?, agent_id = ?",
            ("subagent", "session", "codex"),
        )

    record = _read_task(
        database,
        expected_runtime="subagent",
        expected_scope_kind="session",
        expected_agent_id="codex",
    )

    assert (record.runtime, record.scope_kind, record.agent_id) == (
        "subagent",
        "session",
        "codex",
    )


def test_missing_database_is_read_only_and_does_not_create_anything(tmp_path: Path) -> None:
    db = tmp_path / "missing-root" / "openclaw.sqlite"

    with pytest.raises(HostRecordError, match="database"):
        _read_task(db)

    assert not db.parent.exists()
    assert not db.exists()
    assert not db.with_name("openclaw.sqlite-wal").exists()
    assert not db.with_name("openclaw.sqlite-shm").exists()


def test_readonly_database_can_be_read_without_writes(
    host_fixture: dict[str, Path | str],
) -> None:
    before = Path(host_fixture["db"]).read_bytes()
    Path(host_fixture["db"]).chmod(0o444)

    record = _read_task(host_fixture["db"])

    assert record.task_id == TASK_ID
    assert Path(host_fixture["db"]).read_bytes() == before
    assert not Path(host_fixture["db"]).with_name("openclaw.sqlite-wal").exists()
    assert not Path(host_fixture["db"]).with_name("openclaw.sqlite-shm").exists()


def test_database_uri_handles_encoded_absolute_paths(
    host_fixture: dict[str, Path | str], tmp_path: Path
) -> None:
    database = tmp_path / "state with spaces?" / "openclaw.sqlite"
    database.parent.mkdir()
    shutil.copyfile(host_fixture["db"], database)

    record = _read_task(database)

    assert record.task_id == TASK_ID


def test_wal_database_read_keeps_main_contents_unchanged(
    host_fixture: dict[str, Path | str],
) -> None:
    db = Path(host_fixture["db"])
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("UPDATE task_runs SET status = 'queued'")
    before = db.read_bytes()
    with sqlite3.connect(db) as connection:
        before_rows = connection.execute(
            "SELECT task_id, status FROM task_runs ORDER BY task_id"
        ).fetchall()

    record = _read_task(db)

    assert record.status == "queued"
    with sqlite3.connect(db) as connection:
        after_rows = connection.execute(
            "SELECT task_id, status FROM task_runs ORDER BY task_id"
        ).fetchall()
    assert after_rows == before_rows
    assert db.read_bytes() == before


def test_acp_identity_uses_runtime_options_cwd_and_exact_child_session(
    host_fixture: dict[str, Path | str],
) -> None:
    record = _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)

    assert record.canonical_session_uuid == UUID
    assert record.claude_session_id == OPENCLAW_SESSION_ID
    assert record.effective_cwd == EXPECTED_CWD


@pytest.mark.parametrize(
    ("column", "value"),
    [("backend", "other"), ("agent", "codex"), ("mode", "run")],
)
def test_acp_identity_rejects_wrong_runtime_identity(
    host_fixture: dict[str, Path | str], column: str, value: str
) -> None:
    statements = {
        "backend": "UPDATE acp_sessions SET backend = ?",
        "agent": "UPDATE acp_sessions SET agent = ?",
        "mode": "UPDATE acp_sessions SET mode = ?",
    }
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(statements[column], (value,))

    with pytest.raises(HostRecordError, match="ACP identity"):
        _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)


@pytest.mark.parametrize(
    ("field", "value"),
    [("backend", "other"), ("agent", "codex"), ("mode", "run")],
)
def test_acp_identity_rejects_wrong_expected_values_without_mutating_row(
    host_fixture: dict[str, Path | str], field: str, value: str
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        before = connection.execute("SELECT backend, agent, mode FROM acp_sessions").fetchone()

    with pytest.raises(HostRecordError, match="ACP identity"):
        if field == "backend":
            _read_acp(
                host_fixture["db"],
                CHILD_KEY,
                host_fixture["sessions"],
                EXPECTED_CWD,
                expected_backend=value,
            )
        elif field == "agent":
            _read_acp(
                host_fixture["db"],
                CHILD_KEY,
                host_fixture["sessions"],
                EXPECTED_CWD,
                expected_agent=value,
            )
        else:
            _read_acp(
                host_fixture["db"],
                CHILD_KEY,
                host_fixture["sessions"],
                EXPECTED_CWD,
                expected_mode=value,
            )

    with sqlite3.connect(host_fixture["db"]) as connection:
        after = connection.execute("SELECT backend, agent, mode FROM acp_sessions").fetchone()
    assert after == before


@pytest.mark.parametrize(
    "identity",
    [
        {"state": "pending", "agentSessionId": UUID},
        {"state": "resolved", "agentSessionId": UUID, "acpxSessionId": "wrong"},
        {"state": "resolved", "agentSessionId": "not-a-uuid"},
    ],
)
def test_acp_identity_rejects_unresolved_or_conflicting_uuid(
    host_fixture: dict[str, Path | str], identity: dict[str, str]
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE acp_sessions SET identity_json = ?", (json.dumps(identity),))

    with pytest.raises(HostRecordError, match=r"(identity|UUID)"):
        _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)


def test_acp_identity_accepts_nullable_core_session_id(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE acp_sessions SET session_id = NULL")

    record = _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)

    assert record.core_session_id is None


def test_acp_identity_uses_acpx_uuid_when_agent_uuid_is_absent(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE acp_sessions SET identity_json = ?",
            (json.dumps({"state": "resolved", "acpxSessionId": UUID}),),
        )

    record = _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)

    assert record.agent_session_id is None
    assert record.acpx_session_id == UUID
    assert record.canonical_session_uuid == UUID


def test_acp_identity_rejects_two_different_valid_uuids(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE acp_sessions SET identity_json = ?",
            (
                json.dumps(
                    {
                        "state": "resolved",
                        "agentSessionId": UUID,
                        "acpxSessionId": "10551e01-0503-456f-9e61-6993c912d478",
                    }
                ),
            ),
        )

    with pytest.raises(HostRecordError, match="conflict"):
        _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)


def test_acp_identity_rejects_stale_sessions_mapping_or_wrong_cwd(
    host_fixture: dict[str, Path | str],
) -> None:
    Path(host_fixture["sessions"]).write_text(
        json.dumps({CHILD_KEY: {"sessionId": "stale-session"}}), encoding="utf-8"
    )
    with pytest.raises(HostRecordError, match="session"):
        _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD)

    Path(host_fixture["sessions"]).write_text(
        json.dumps({CHILD_KEY: {"sessionId": OPENCLAW_SESSION_ID}}), encoding="utf-8"
    )
    with pytest.raises(HostRecordError, match="cwd"):
        _read_acp(host_fixture["db"], CHILD_KEY, host_fixture["sessions"], "/wrong/bundle")


def test_transcript_path_encodes_every_non_ascii_alphanumeric_character(
    tmp_path: Path,
) -> None:
    cwd = "/tmp/a b:" + "\N{GREEK SMALL LETTER ALPHA}" + "/project_1"
    path = derive_claude_transcript_path(tmp_path, cwd, UUID)

    assert path == tmp_path / "-tmp-a-b---project-1" / f"{UUID}.jsonl"


def test_transcript_path_matches_known_sdk_probe_location_without_reading_it() -> None:
    cwd = "/home/dev/autoresearch-migration-20260906.wul9vD/sdk-probe.hgmhMJ"
    path = derive_claude_transcript_path(Path("/home/dev/.claude/projects"), cwd, UUID)

    assert path == Path(
        "/home/dev/.claude/projects/"
        "-home-dev-autoresearch-migration-20260906-"
        "wul9vD-sdk-probe-hgmhMJ/"  # pragma: allowlist secret (synthetic fixture)
        f"{UUID}.jsonl"
    )


def test_transcript_returns_exact_bytes_and_hash(
    host_fixture: dict[str, Path | str],
) -> None:
    record = read_exact_claude_transcript(host_fixture["projects"], EXPECTED_CWD, UUID)
    content = b'{"type":"assistant","bytes":[0,255]}\n'

    assert record.path == host_fixture["transcript"]
    assert record.content == content
    assert record.sha256 == hashlib.sha256(content).hexdigest()


def test_rollout_returns_observed_metadata_latest_usage_and_byte_hash(tmp_path: Path) -> None:
    older_usage = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "total_tokens": 3,
                    "model_context_window": 100,
                }
            },
        },
    }
    terminal = {
        "type": "event_msg",
        "payload": {"type": "task_complete", "completed_at": "2026-09-09T12:00:00Z"},
    }
    content = _rollout_bytes(_session_meta(), older_usage, _token_count(9, 200), terminal)
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(content)

    record = read_exact_rollout(path)

    assert record.path == path
    assert record.thread_id == ROLLOUT_THREAD_ID
    assert record.model == ROLLOUT_MODEL
    assert record.reasoning_effort == ROLLOUT_EFFORT
    assert record.terminal_state == "succeeded"
    assert record.latest_usage is not None
    assert record.latest_usage.source == "last"
    assert record.latest_usage.total_tokens == 9
    assert record.latest_usage.model_context_window == 200
    assert record.sha256 == hashlib.sha256(content).hexdigest()
    assert not hasattr(record, "verified")
    assert not hasattr(record, "service_tier")


def test_rollout_reads_model_and_effort_from_official_turn_context(tmp_path: Path) -> None:
    session_meta = _session_meta()
    session_meta["payload"] = {
        "id": ROLLOUT_THREAD_ID,
        "model": None,
        "reasoning_effort": None,
        "source": {"subagent": {"thread_spawn": {"agent_role": "implementer"}}},
    }
    turn_context = {
        "type": "turn_context",
        "payload": {"model": ROLLOUT_MODEL, "effort": ROLLOUT_EFFORT},
    }
    path = tmp_path / "turn-context-rollout.jsonl"
    path.write_bytes(_rollout_bytes(session_meta, turn_context))

    record = read_exact_rollout(path)

    assert record.model == ROLLOUT_MODEL
    assert record.reasoning_effort == ROLLOUT_EFFORT
    assert record.agent_role == "implementer"


@pytest.mark.parametrize(
    ("usage_event", "source", "context_window"),
    [
        (_token_count(4, 120), "last", 120),
        (_token_count(5, None, source="total", sibling_context=240), "total", 240),
        (_token_count(6, 360, sibling_context=360), "last", 360),
    ],
)
def test_rollout_usage_records_source_and_observed_context_placement(
    tmp_path: Path,
    usage_event: dict[str, object],
    source: str,
    context_window: int,
) -> None:
    path = tmp_path / f"usage-{source}.jsonl"
    path.write_bytes(_rollout_bytes(_session_meta(), usage_event))

    record = read_exact_rollout(path)

    assert record.latest_usage is not None
    assert record.latest_usage.source == source
    assert record.latest_usage.model_context_window == context_window


def test_rollout_usage_prefers_last_snapshot_when_both_appear_in_one_event(
    tmp_path: Path,
) -> None:
    event = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": 3},
                "last_token_usage": {"total_tokens": 9},
            },
        },
    }
    path = tmp_path / "usage-both.jsonl"
    path.write_bytes(_rollout_bytes(_session_meta(), event))

    record = read_exact_rollout(path)

    assert record.latest_usage is not None
    assert record.latest_usage.source == "last"
    assert record.latest_usage.total_tokens == 9


def test_rollout_usage_requires_agreeing_context_window_placements(tmp_path: Path) -> None:
    path = tmp_path / "usage-conflict.jsonl"
    path.write_bytes(
        _rollout_bytes(
            _session_meta(),
            _token_count(6, 120, sibling_context=240),
        )
    )

    with pytest.raises(HostRecordError, match="context window"):
        read_exact_rollout(path)


@pytest.mark.parametrize(
    ("marker", "state"),
    [(None, "pending"), ("task_complete", "succeeded"), ("task_failed", "failed")],
)
def test_rollout_terminal_marker_projects_only_observed_state(
    tmp_path: Path, marker: str | None, state: str
) -> None:
    events = [_session_meta()]
    if marker is not None:
        events.append({"type": "event_msg", "payload": {"type": marker}})
    path = tmp_path / f"{state}.jsonl"
    path.write_bytes(_rollout_bytes(*events))

    record = read_exact_rollout(path)

    assert record.terminal_state == state


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"id": ROLLOUT_THREAD_ID, "model": ROLLOUT_MODEL},
        {"id": ROLLOUT_THREAD_ID, "reasoning_effort": ROLLOUT_EFFORT},
    ],
)
def test_rollout_rejects_missing_or_malformed_session_metadata(
    tmp_path: Path, metadata: object
) -> None:
    events: list[dict[str, object]] = []
    if metadata is not None:
        events.append({"type": "session_meta", "payload": metadata})
    path = tmp_path / "malformed.jsonl"
    path.write_bytes(_rollout_bytes(*events))

    with pytest.raises(HostRecordError, match=r"session_meta|rollout\."):
        read_exact_rollout(path)


def test_rollout_rejects_duplicate_session_metadata(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-meta.jsonl"
    path.write_bytes(_rollout_bytes(_session_meta(), _session_meta()))

    with pytest.raises(HostRecordError, match="multiple session_meta"):
        read_exact_rollout(path)


def test_rollout_rejects_non_object_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "non-object.jsonl"
    path.write_bytes(b"[]\n")

    with pytest.raises(HostRecordError, match="must contain a JSON object"):
        read_exact_rollout(path)


def test_rollout_rejects_stale_terminal_marker(tmp_path: Path) -> None:
    stale = {
        "type": "event_msg",
        "payload": {"type": "task_complete", "thread_id": "other-thread"},
    }
    path = tmp_path / "stale.jsonl"
    path.write_bytes(_rollout_bytes(_session_meta(), stale))

    with pytest.raises(HostRecordError, match="wrong thread id"):
        read_exact_rollout(path)


def test_rollout_rejects_multiple_terminal_markers(tmp_path: Path) -> None:
    path = tmp_path / "multiple-terminal.jsonl"
    path.write_bytes(
        _rollout_bytes(
            _session_meta(),
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "event_msg", "payload": {"type": "task_failed"}},
        )
    )

    with pytest.raises(HostRecordError, match="multiple terminal"):
        read_exact_rollout(path)


@pytest.mark.parametrize("kind", ["symlink", "oversized"])
def test_rollout_rejects_symlink_and_oversized_inputs(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(_rollout_bytes(_session_meta()))
    if kind == "symlink":
        path.unlink()
        target = tmp_path / "outside.jsonl"
        target.write_bytes(b"outside")
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * (MAX_TRANSCRIPT_BYTES + 1))

    with pytest.raises(HostRecordError, match="Codex rollout"):
        read_exact_rollout(path)


def test_rollout_requires_an_absolute_exact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(HostRecordError, match="absolute"):
        read_exact_rollout(Path("relative-rollout.jsonl"))


def test_rollout_uses_only_the_exact_path_not_directory_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(_rollout_bytes(_session_meta()))

    def forbidden(*_args: object, **_kwargs: object) -> list[Path]:
        raise AssertionError("directory scanning is not allowed")

    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)

    assert read_exact_rollout(path).thread_id == ROLLOUT_THREAD_ID


def test_transcript_uses_only_the_exact_path_not_directory_scanning(
    host_fixture: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> list[Path]:
        raise AssertionError("directory scanning is not allowed")

    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)

    assert read_exact_claude_transcript(host_fixture["projects"], EXPECTED_CWD, UUID).content


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "oversized"])
def test_transcript_rejects_unsafe_exact_input(
    host_fixture: dict[str, Path | str], kind: str, tmp_path: Path
) -> None:
    path = Path(host_fixture["transcript"])
    if kind == "missing":
        path.unlink()
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "symlink":
        path.unlink()
        target = tmp_path / "other.jsonl"
        target.write_bytes(b"not the selected transcript")
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * (MAX_TRANSCRIPT_BYTES + 1))

    with pytest.raises(HostRecordError, match="transcript"):
        read_exact_claude_transcript(host_fixture["projects"], EXPECTED_CWD, UUID)


def test_sessions_json_rejects_symlink_without_following(
    host_fixture: dict[str, Path | str], tmp_path: Path
) -> None:
    sessions = Path(host_fixture["sessions"])
    sessions.unlink()
    target = tmp_path / "target-sessions.json"
    target.write_text("{}", encoding="utf-8")
    sessions.symlink_to(target)

    with pytest.raises(HostRecordError, match="sessions"):
        _read_acp(host_fixture["db"], CHILD_KEY, sessions, EXPECTED_CWD)


def test_sessions_json_rejects_oversized_input(
    host_fixture: dict[str, Path | str],
) -> None:
    sessions = Path(host_fixture["sessions"])
    sessions.write_bytes(b"x" * (MAX_METADATA_BYTES + 1))

    with pytest.raises(HostRecordError, match="sessions"):
        _read_acp(host_fixture["db"], CHILD_KEY, sessions, EXPECTED_CWD)


def test_database_uri_rejects_relative_path_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(HostRecordError, match="absolute"):
        _read_task(Path("relative.sqlite"))

    assert not (tmp_path / "relative.sqlite").exists()


@pytest.mark.parametrize("cwd", ["relative/worktree", "/tmp/../worktree", "/tmp/worktree/"])
def test_transcript_rejects_noncanonical_cwd(tmp_path: Path, cwd: str) -> None:
    with pytest.raises(HostRecordError, match="canonical"):
        derive_claude_transcript_path(tmp_path, cwd, UUID)
