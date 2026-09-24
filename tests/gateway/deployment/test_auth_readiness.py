"""Regression tests for native OpenClaw shared-auth readiness."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTH_READINESS_MODULE = "gateway.deployment.auth_readiness"


def _oauth_profile(*, expires: float | None = None) -> dict[str, object]:
    profile: dict[str, object] = {
        "provider": "openai",
        "type": "oauth",
        "access": "access-sentinel",
        "refresh": "refresh-sentinel",
    }
    if expires is not None:
        profile["expires"] = expires
    return profile


def _create_shared_database(
    path: Path,
    *,
    profiles: object | None = None,
    store_json: str | None = None,
    ownership: object = None,
    schema_role: str = "global",
    schema_version: int = 15,
    schema_agent_id: str | None = None,
    app_version: str = "2026.9.2",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (
                meta_key TEXT NOT NULL PRIMARY KEY,
                role TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                agent_id TEXT,
                app_version TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE config_machine_state (
                state_key TEXT NOT NULL PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL
            ) STRICT;
            """
        )
        connection.execute(
            "INSERT INTO schema_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("primary", schema_role, schema_version, schema_agent_id, app_version, 1, 1),
        )
        if ownership is not None:
            connection.execute(
                "INSERT INTO config_machine_state VALUES (?, ?, ?)",
                ("auth.sharedStore", json.dumps(ownership), 1),
            )
        if store_json is None and profiles is not None:
            store_json = json.dumps({"version": 1, "profiles": profiles})
        if store_json is not None:
            connection.execute(
                "INSERT INTO config_machine_state VALUES (?, ?, ?)",
                ("authProfiles.store", store_json, 1),
            )
        connection.execute(
            "INSERT INTO config_machine_state VALUES (?, ?, ?)",
            ("authProfiles.state", json.dumps({"version": 1}), 1),
        )


def _create_agent_database(
    path: Path,
    agent_id: str,
    *,
    profiles: object | None = None,
    store_json: str | None = None,
    schema_role: str = "agent",
    schema_version: int = 19,
    stored_agent_id: str | None = None,
    app_version: str = "2026.9.2",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
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
            ) STRICT;
            CREATE TABLE auth_profile_store (
                store_key TEXT NOT NULL PRIMARY KEY,
                store_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE auth_profile_state (
                state_key TEXT NOT NULL PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            ) STRICT;
            """
        )
        connection.execute(
            "INSERT INTO schema_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "primary",
                schema_role,
                schema_version,
                stored_agent_id or agent_id,
                app_version,
                1,
                1,
            ),
        )
        if store_json is None and profiles is not None:
            store_json = json.dumps({"version": 1, "profiles": profiles})
        if store_json is not None:
            connection.execute(
                "INSERT INTO auth_profile_store VALUES (?, ?, ?)",
                ("primary", store_json, 1),
            )
        connection.execute(
            "INSERT INTO auth_profile_state VALUES (?, ?, ?)",
            ("primary", json.dumps({"version": 1}), 1),
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


def _run_readiness(
    push_home: Path,
    config: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            AUTH_READINESS_MODULE,
            "check",
            "--",
            str(push_home),
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def _make_valid_home(tmp_path: Path, *, local_profiles: object | None = None) -> tuple[Path, Path]:
    push_home = tmp_path / "openclaw"
    _create_shared_database(
        push_home / "state/openclaw.sqlite",
        profiles={"openai:shared": _oauth_profile()},
        ownership={"location": "state-db"},
    )
    _create_agent_database(
        push_home / "agents/main/agent/openclaw-agent.sqlite",
        "main",
        profiles=local_profiles,
    )
    _create_agent_database(
        push_home / "agents/reviewer/agent/openclaw-agent.sqlite",
        "reviewer",
        profiles=local_profiles,
    )
    for agent_id in ("main", "reviewer"):
        (push_home / f"agents/{agent_id}/agent/auth-profiles.json").write_text(
            '{"profiles":[],"marker":"preserve"}', encoding="utf-8"
        )
    config = tmp_path / "openclaw.json"
    _write_config(config, "main", "reviewer")
    return push_home, config


def test_readiness_accepts_shared_oauth_with_empty_local_stores_and_does_not_mutate(
    tmp_path: Path,
) -> None:
    push_home, config = _make_valid_home(tmp_path)
    before = {
        path: path.read_bytes()
        for path in [
            push_home / "state/openclaw.sqlite",
            *push_home.glob("agents/*/agent/openclaw-agent.sqlite"),
            *push_home.glob("agents/*/agent/auth-profiles.json"),
        ]
    }

    result = _run_readiness(push_home, config)

    assert result.returncode == 0, result.stderr
    assert "shared read-through" in result.stdout
    assert "sync" not in result.stdout.lower()
    assert "readiness verified" in result.stdout
    assert result.stderr == ""
    assert {path: path.read_bytes() for path in before} == before
    assert "access-sentinel" not in result.stdout + result.stderr


def test_readiness_accepts_valid_local_openai_override_without_writing_it(tmp_path: Path) -> None:
    push_home, config = _make_valid_home(
        tmp_path,
        local_profiles={"openai:local": _oauth_profile(expires=1)},
    )
    before = {
        path: path.read_bytes()
        for path in [
            push_home / "state/openclaw.sqlite",
            *push_home.glob("agents/*/agent/openclaw-agent.sqlite"),
            *push_home.glob("agents/*/agent/auth-profiles.json"),
        ]
    }

    result = _run_readiness(push_home, config)

    assert result.returncode == 0, result.stderr
    assert "local OpenAI OAuth override" in result.stdout
    assert "readiness verified" in result.stdout
    assert {path: path.read_bytes() for path in before} == before
    assert "access-sentinel" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "store_json",
    [None, "not-json", json.dumps({"version": 1, "profiles": []})],
)
def test_readiness_rejects_missing_or_malformed_shared_store(
    tmp_path: Path, store_json: str | None
) -> None:
    push_home = tmp_path / "openclaw"
    _create_shared_database(
        push_home / "state/openclaw.sqlite",
        store_json=store_json,
        ownership={"location": "state-db"},
    )
    _create_agent_database(push_home / "agents/main/agent/openclaw-agent.sqlite", "main")
    config = tmp_path / "openclaw.json"
    _write_config(config, "main")
    before = (push_home / "state/openclaw.sqlite").read_bytes()

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "authProfiles.store" in result.stderr
    assert (push_home / "state/openclaw.sqlite").read_bytes() == before


@pytest.mark.parametrize(
    "profile",
    [
        {"provider": "azure", "type": "oauth", "access": "x", "refresh": "y"},
        {"provider": "openai", "type": "token", "token": "x"},
        {"provider": "openai", "type": "oauth"},
    ],
)
def test_readiness_rejects_shared_wrong_provider_type_or_credential(
    tmp_path: Path, profile: dict[str, object]
) -> None:
    push_home = tmp_path / "openclaw"
    _create_shared_database(
        push_home / "state/openclaw.sqlite",
        profiles={"candidate": profile},
        ownership={"location": "state-db"},
    )
    _create_agent_database(push_home / "agents/main/agent/openclaw-agent.sqlite", "main")
    config = tmp_path / "openclaw.json"
    _write_config(config, "main")

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "no eligible OpenAI OAuth" in result.stderr


def test_readiness_rejects_incompatible_local_openai_override_without_fallback(
    tmp_path: Path,
) -> None:
    push_home, config = _make_valid_home(
        tmp_path,
        local_profiles={"openai:local": {"provider": "openai", "type": "token", "token": "x"}},
    )
    before = {
        path: path.read_bytes()
        for path in [
            push_home / "state/openclaw.sqlite",
            *push_home.glob("agents/*/agent/openclaw-agent.sqlite"),
            *push_home.glob("agents/*/agent/auth-profiles.json"),
        ]
    }

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "local OpenAI OAuth override" in result.stderr
    assert "shared read-through" not in result.stderr
    assert {path: path.read_bytes() for path in before} == before
    assert "readiness verified" not in result.stdout
    assert "access-sentinel" not in result.stdout + result.stderr


def test_readiness_rejects_wrong_shared_ownership_and_native_schema(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    _create_shared_database(
        push_home / "state/openclaw.sqlite",
        profiles={"openai:shared": _oauth_profile()},
        ownership={"location": "legacy-main"},
    )
    _create_agent_database(push_home / "agents/main/agent/openclaw-agent.sqlite", "main")
    config = tmp_path / "openclaw.json"
    _write_config(config, "main")

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "state-db" in result.stderr


def test_readiness_rejects_unsafe_agent_id_before_path_access(tmp_path: Path) -> None:
    push_home = tmp_path / "openclaw"
    _create_shared_database(
        push_home / "state/openclaw.sqlite",
        profiles={"openai:shared": _oauth_profile()},
        ownership={"location": "state-db"},
    )
    _create_agent_database(push_home / "agents/main/agent/openclaw-agent.sqlite", "main")
    config = tmp_path / "openclaw.json"
    _write_config(config, "../escape")

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "unsafe agent id" in result.stderr
    assert not (tmp_path / "escape").exists()


def test_readiness_rejects_symlinked_shared_database(tmp_path: Path) -> None:
    push_home, config = _make_valid_home(tmp_path)
    source = push_home / "state/openclaw.sqlite"
    alias = tmp_path / "external.sqlite"
    alias.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(alias)

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "not an existing regular file" in result.stderr


def test_readiness_rejects_missing_local_without_creating_database(tmp_path: Path) -> None:
    push_home, config = _make_valid_home(tmp_path)
    missing = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    missing.unlink()
    before = {
        path: path.read_bytes()
        for path in [
            push_home / "state/openclaw.sqlite",
            push_home / "agents/main/agent/openclaw-agent.sqlite",
            *push_home.glob("agents/*/agent/auth-profiles.json"),
        ]
    }

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "not an existing regular file" in result.stderr
    assert not missing.exists()
    assert {path: path.read_bytes() for path in before} == before
    assert "readiness verified" not in result.stdout


@pytest.mark.parametrize("target_kind", ["symlink", "directory", "corrupt"])
def test_readiness_rejects_non_native_local_database_shapes_without_writes(
    tmp_path: Path, target_kind: str
) -> None:
    push_home, config = _make_valid_home(tmp_path)
    target = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    before = {
        path: path.read_bytes()
        for path in [
            push_home / "state/openclaw.sqlite",
            push_home / "agents/main/agent/openclaw-agent.sqlite",
            target,
            *push_home.glob("agents/*/agent/auth-profiles.json"),
        ]
    }
    if target_kind == "symlink":
        alias = tmp_path / "external-agent.sqlite"
        alias.write_bytes(before[target])
        target.unlink()
        target.symlink_to(alias)
    elif target_kind == "directory":
        target.unlink()
        target.mkdir()
    else:
        target.write_bytes(b"not a sqlite database")

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    if target_kind == "corrupt":
        assert "Native agent store" in result.stderr
        assert "unreadable" in result.stderr
    else:
        assert "not an existing regular file" in result.stderr
    assert "readiness verified" not in result.stdout
    if target_kind == "symlink":
        assert alias.read_bytes() == before[target]
    elif target_kind == "directory":
        assert target.is_dir()
    else:
        assert target.read_bytes() == b"not a sqlite database"
    untouched = {path: path.read_bytes() for path in before if path != target}
    assert untouched == {path: before[path] for path in before if path != target}


@pytest.mark.parametrize(
    ("schema_role", "schema_version", "schema_agent_id", "app_version"),
    [
        ("agent", 15, None, "2026.9.2"),
        ("global", 14, None, "2026.9.2"),
        ("global", 15, "main", "2026.9.2"),
        ("global", 15, None, "2026.9.1"),
    ],
)
def test_readiness_rejects_wrong_shared_native_identity_without_writes(
    tmp_path: Path,
    schema_role: str,
    schema_version: int,
    schema_agent_id: str | None,
    app_version: str,
) -> None:
    push_home = tmp_path / "openclaw"
    _create_shared_database(
        push_home / "state/openclaw.sqlite",
        profiles={"openai:shared": _oauth_profile()},
        ownership={"location": "state-db"},
        schema_role=schema_role,
        schema_version=schema_version,
        schema_agent_id=schema_agent_id,
        app_version=app_version,
    )
    _create_agent_database(push_home / "agents/main/agent/openclaw-agent.sqlite", "main")
    config = tmp_path / "openclaw.json"
    _write_config(config, "main")
    shared = push_home / "state/openclaw.sqlite"
    before = shared.read_bytes()

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "incompatible schema metadata" in result.stderr
    assert shared.read_bytes() == before
    assert "readiness verified" not in result.stdout


@pytest.mark.parametrize(
    ("schema_role", "schema_version", "stored_agent_id", "app_version"),
    [
        ("owner", 19, "reviewer", "2026.9.2"),
        ("agent", 18, "reviewer", "2026.9.2"),
        ("agent", 19, "wrong-agent", "2026.9.2"),
        ("agent", 19, "reviewer", "2026.9.1"),
    ],
)
def test_readiness_rejects_wrong_local_native_identity_without_writes(
    tmp_path: Path,
    schema_role: str,
    schema_version: int,
    stored_agent_id: str,
    app_version: str,
) -> None:
    push_home, config = _make_valid_home(tmp_path)
    target = push_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    target.unlink()
    _create_agent_database(
        target,
        "reviewer",
        schema_role=schema_role,
        schema_version=schema_version,
        stored_agent_id=stored_agent_id,
        app_version=app_version,
    )
    before = {
        path: path.read_bytes()
        for path in [
            push_home / "state/openclaw.sqlite",
            *push_home.glob("agents/*/agent/openclaw-agent.sqlite"),
            *push_home.glob("agents/*/agent/auth-profiles.json"),
        ]
    }

    result = _run_readiness(push_home, config)

    assert result.returncode == 1
    assert "native" in result.stderr.lower()
    assert "readiness verified" not in result.stdout
    assert {path: path.read_bytes() for path in before} == before


def test_readiness_module_has_no_retired_synchronization_or_credential_fallback() -> None:
    source = (REPO_ROOT / "gateway/deployment/auth_readiness.py").read_text(encoding="utf-8")
    for retired in (
        "ATTACH DATABASE",
        "DELETE FROM",
        "INSERT INTO",
        "guarded_cp_file",
        "guarded_chmod",
        "auth-profiles.json",
        "models auth login",
    ):
        assert retired not in source
