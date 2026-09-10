"""White-box tests for managed Codex auth-store synchronization."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTH_SYNC_MODULE = "gateway.deployment.auth_sync"


def _create_auth_database(
    path: Path,
    *,
    profile_json: str | None,
    state_json: str,
    agent_id: str = "reviewer",
    native_schema: bool = True,
    native_role: str = "agent",
    native_schema_version: int = 19,
    wal: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        if wal:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.executescript(
            """
            CREATE TABLE schema_meta (
                meta_key TEXT NOT NULL PRIMARY KEY,
                role TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                app_version TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE auth_profile_store (
                store_key TEXT NOT NULL PRIMARY KEY,
                store_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE auth_profile_state (
                state_key TEXT NOT NULL PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        if native_schema:
            connection.execute(
                "INSERT INTO schema_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("primary", native_role, native_schema_version, agent_id, "2026.8.1", 1, 1),
            )
        if profile_json is not None:
            connection.execute(
                "INSERT INTO auth_profile_store VALUES (?, ?, ?)",
                ("openai:test", profile_json, 1),
            )
        connection.execute(
            "INSERT INTO auth_profile_state VALUES (?, ?, ?)",
            ("state", state_json, 2),
        )


def _write_config(path: Path, *agent_ids: str) -> None:
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "ownership": "explicit",
                    "entries": {
                        agent_id: {"model": {"primary": "openai/gpt-5.4"}} for agent_id in agent_ids
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def _run_sync(push_home: Path, config: Path, openclaw_bin: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            AUTH_SYNC_MODULE,
            "sync",
            str(push_home),
            str(config),
            openclaw_bin,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def test_sync_managed_agent_codex_auth_copies_rows_and_preserves_wal(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    source_profile = '{"provider":"openai","mode":"oauth"}'
    _create_auth_database(
        source_db,
        profile_json=source_profile,
        state_json='{"source":true}',
        agent_id="main",
    )
    (source_db.parent / "auth-profiles.json").write_text('{"profiles":[]}', encoding="utf-8")

    target_db = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    _create_auth_database(
        target_db,
        profile_json='{"provider":"azure"}',
        state_json='{"old":true}',
        agent_id="reviewer",
        wal=True,
    )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Syncing OpenClaw-managed Codex OAuth profile to 2 agent auth stores:" in result.stdout
    with sqlite3.connect(target_db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute(
            "SELECT store_key, store_json, updated_at FROM auth_profile_store"
        ).fetchall() == [("openai:test", source_profile, 1)]
        assert connection.execute(
            "SELECT state_key, state_json, updated_at FROM auth_profile_state"
        ).fetchall() == [("state", '{"source":true}', 2)]
    assert (target_db.parent / "auth-profiles.json").read_text(encoding="utf-8") == (
        '{"profiles":[]}'
    )
    assert (target_db.stat().st_mode & 0o777) == 0o600
    assert (target_db.parent / "auth-profiles.json").stat().st_mode & 0o777 == 0o600


def test_sync_guard_failure_has_frozen_message_without_traceback(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json='{"provider":"openai"}',
        state_json="{}",
        agent_id="main",
    )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")

    target_db = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    alias_db = tmp_path / "external.sqlite"
    _create_auth_database(
        alias_db,
        profile_json='{"provider":"azure"}',
        state_json="{}",
        agent_id="reviewer",
    )
    target_db.parent.mkdir(parents=True)
    target_db.hardlink_to(alias_db)
    original = alias_db.read_bytes()

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert result.stdout == (
        "Syncing OpenClaw-managed Codex OAuth profile to 2 agent auth stores:\n"
        f"  main → {push_home / 'agents/main/agent/openclaw-agent.sqlite'} (source)\n"
    )
    assert result.stderr == (
        f"ERROR: Destination path is a hard-linked regular file while syncing managed OpenClaw "
        f"agent auth database {target_db}: {target_db} (link count 2).\n"
        "       Refusing before mutation to avoid modifying external hard-link aliases.\n"
    )
    assert "Traceback" not in result.stderr
    assert alias_db.read_bytes() == original


def test_sync_missing_openai_profile_has_frozen_message(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json=None,
        state_json="{}",
        agent_id="main",
    )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main")

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "ERROR: Main OpenClaw auth store has no OpenAI/Codex OAuth profile.\n"
        "       Run: /opt/openclaw models auth login --provider openai\n"
    )
    assert "Traceback" not in result.stderr


def test_sync_refuses_missing_native_target_before_creating_database(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json='{"provider":"openai"}',
        state_json="{}",
        agent_id="main",
    )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")
    target_db = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert not target_db.exists()
    assert "Native agent store" in result.stderr
    assert "public OpenClaw native initialization" in result.stderr


def test_sync_refuses_target_without_native_ownership_metadata(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json='{"provider":"openai"}',
        state_json="{}",
        agent_id="main",
    )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")
    target_db = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    _create_auth_database(
        target_db,
        profile_json='{"provider":"azure"}',
        state_json='{"old":true}',
        agent_id="unexpected",
    )
    prior_bytes = target_db.read_bytes()

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert target_db.read_bytes() == prior_bytes
    assert "native ownership metadata" in result.stderr
    assert "public OpenClaw native initialization" in result.stderr


def test_sync_refuses_source_without_native_ownership_metadata(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json='{"provider":"openai"}',
        state_json="{}",
        agent_id="main",
        native_schema=False,
    )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main")

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert "native ownership metadata" in result.stderr
    assert "public OpenClaw native initialization" in result.stderr


@pytest.mark.parametrize(
    ("native_role", "native_schema_version"),
    [("owner", 19), ("agent", 18)],
)
def test_sync_refuses_target_with_wrong_native_schema(
    tmp_path: Path,
    native_role: str,
    native_schema_version: int,
) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json='{"provider":"openai"}',
        state_json="{}",
        agent_id="main",
    )
    target_db = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    _create_auth_database(
        target_db,
        profile_json='{"provider":"azure"}',
        state_json='{"old":true}',
        agent_id="reviewer",
        native_role=native_role,
        native_schema_version=native_schema_version,
    )
    prior_bytes = target_db.read_bytes()
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert target_db.read_bytes() == prior_bytes
    assert "native ownership metadata" in result.stderr
    assert "public OpenClaw native initialization" in result.stderr


@pytest.mark.parametrize("target_kind", ["symlink", "directory", "corrupt"])
def test_sync_refuses_non_native_target_shapes(tmp_path: Path, target_kind: str) -> None:
    push_home = tmp_path / "openclaw"
    source_db = push_home / "agents/main/agent/openclaw-agent.sqlite"
    _create_auth_database(
        source_db,
        profile_json='{"provider":"openai"}',
        state_json="{}",
        agent_id="main",
    )
    target_db = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    target_db.parent.mkdir(parents=True)
    if target_kind == "symlink":
        alias_db = tmp_path / "external.sqlite"
        _create_auth_database(
            alias_db,
            profile_json='{"provider":"azure"}',
            state_json="{}",
            agent_id="reviewer",
        )
        target_db.symlink_to(alias_db)
    elif target_kind == "directory":
        target_db.mkdir()
    else:
        target_db.write_bytes(b"not a sqlite database")
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")

    result = _run_sync(push_home, config, "/opt/openclaw")

    assert result.returncode == 1
    assert "Native agent store" in result.stderr
    assert "public OpenClaw native initialization" in result.stderr
