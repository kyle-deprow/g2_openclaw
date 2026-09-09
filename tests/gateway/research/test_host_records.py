"""Black-box tests for read-only host review-record correlation."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Final

import pytest
from gateway.research.host_records import (
    MAX_METADATA_BYTES,
    MAX_TRANSCRIPT_BYTES,
    HostRecordError,
    derive_claude_transcript_path,
    read_exact_acp_identity,
    read_exact_claude_transcript,
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
    record = read_exact_task_run(
        host_fixture["db"],
        owner_key=OWNER,
        reservation_label=LABEL,
        reserved_at_ms=RESERVED_AT,
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

    record = read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)

    assert record.status == status
    assert record.is_pending


@pytest.mark.parametrize("status", ["succeeded", "failed", "timed_out", "cancelled", "lost"])
def test_terminal_task_status_remains_distinguishable(
    host_fixture: dict[str, Path | str], status: str
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE task_runs SET status = ?", (status,))

    record = read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)

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
        read_exact_task_run(host_fixture["db"], reserved_at_ms=RESERVED_AT, **kwargs)


@pytest.mark.parametrize("requester_session_key", [None, ""])
def test_task_lookup_accepts_null_or_empty_requester_session_key(
    host_fixture: dict[str, Path | str], requester_session_key: str | None
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE task_runs SET requester_session_key = ?",
            (requester_session_key,),
        )

    record = read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)

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
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)


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
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)

    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("DELETE FROM task_runs WHERE task_id != ?", (TASK_ID,))

    with pytest.raises(HostRecordError, match="ACK"):
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT, ack_run_id="wrong-run")


def test_task_lookup_rejects_task_created_before_reservation(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE task_runs SET created_at = ?", (RESERVED_AT - 1,))

    with pytest.raises(HostRecordError, match="reservation"):
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)


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
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)


def test_task_lookup_rejects_end_before_start(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE task_runs SET started_at = ?, ended_at = ?",
            (RESERVED_AT + 500, RESERVED_AT + 400),
        )

    with pytest.raises(HostRecordError, match="before it started"):
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)


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
        read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)


def test_missing_database_is_read_only_and_does_not_create_anything(tmp_path: Path) -> None:
    db = tmp_path / "missing-root" / "openclaw.sqlite"

    with pytest.raises(HostRecordError, match="database"):
        read_exact_task_run(db, OWNER, LABEL, RESERVED_AT)

    assert not db.parent.exists()
    assert not db.exists()
    assert not db.with_name("openclaw.sqlite-wal").exists()
    assert not db.with_name("openclaw.sqlite-shm").exists()


def test_readonly_database_can_be_read_without_writes(
    host_fixture: dict[str, Path | str],
) -> None:
    before = Path(host_fixture["db"]).read_bytes()
    Path(host_fixture["db"]).chmod(0o444)

    record = read_exact_task_run(host_fixture["db"], OWNER, LABEL, RESERVED_AT)

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

    record = read_exact_task_run(database, OWNER, LABEL, RESERVED_AT)

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

    record = read_exact_task_run(db, OWNER, LABEL, RESERVED_AT)

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
    record = read_exact_acp_identity(
        host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
    )

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
        read_exact_acp_identity(
            host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
        )


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
        read_exact_acp_identity(
            host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
        )


def test_acp_identity_accepts_nullable_core_session_id(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute("UPDATE acp_sessions SET session_id = NULL")

    record = read_exact_acp_identity(
        host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
    )

    assert record.core_session_id is None


def test_acp_identity_uses_acpx_uuid_when_agent_uuid_is_absent(
    host_fixture: dict[str, Path | str],
) -> None:
    with sqlite3.connect(host_fixture["db"]) as connection:
        connection.execute(
            "UPDATE acp_sessions SET identity_json = ?",
            (json.dumps({"state": "resolved", "acpxSessionId": UUID}),),
        )

    record = read_exact_acp_identity(
        host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
    )

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
        read_exact_acp_identity(
            host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
        )


def test_acp_identity_rejects_stale_sessions_mapping_or_wrong_cwd(
    host_fixture: dict[str, Path | str],
) -> None:
    Path(host_fixture["sessions"]).write_text(
        json.dumps({CHILD_KEY: {"sessionId": "stale-session"}}), encoding="utf-8"
    )
    with pytest.raises(HostRecordError, match="session"):
        read_exact_acp_identity(
            host_fixture["db"], CHILD_KEY, host_fixture["sessions"], EXPECTED_CWD
        )

    Path(host_fixture["sessions"]).write_text(
        json.dumps({CHILD_KEY: {"sessionId": OPENCLAW_SESSION_ID}}), encoding="utf-8"
    )
    with pytest.raises(HostRecordError, match="cwd"):
        read_exact_acp_identity(
            host_fixture["db"], CHILD_KEY, host_fixture["sessions"], "/wrong/bundle"
        )


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
        read_exact_acp_identity(host_fixture["db"], CHILD_KEY, sessions, EXPECTED_CWD)


def test_sessions_json_rejects_oversized_input(
    host_fixture: dict[str, Path | str],
) -> None:
    sessions = Path(host_fixture["sessions"])
    sessions.write_bytes(b"x" * (MAX_METADATA_BYTES + 1))

    with pytest.raises(HostRecordError, match="sessions"):
        read_exact_acp_identity(host_fixture["db"], CHILD_KEY, sessions, EXPECTED_CWD)


def test_database_uri_rejects_relative_path_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(HostRecordError, match="absolute"):
        read_exact_task_run(Path("relative.sqlite"), OWNER, LABEL, RESERVED_AT)

    assert not (tmp_path / "relative.sqlite").exists()


@pytest.mark.parametrize("cwd", ["relative/worktree", "/tmp/../worktree", "/tmp/worktree/"])
def test_transcript_rejects_noncanonical_cwd(tmp_path: Path, cwd: str) -> None:
    with pytest.raises(HostRecordError, match="canonical"):
        derive_claude_transcript_path(tmp_path, cwd, UUID)
