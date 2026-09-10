"""OpenClaw-managed Codex auth-store synchronization."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .guarded_fs import (
    guard_destination_path_chain,
    guarded_chmod,
    guarded_cp_file,
    path_owned_by_effective_user,
)

AUTH_PROFILE_QUERY = (
    'select 1 from auth_profile_store where store_json like \'%"provider":"openai"%\' limit 1;'
)
NATIVE_AGENT_SCHEMA_QUERY = """
SELECT role, schema_version, agent_id, app_version
FROM schema_meta
WHERE meta_key = 'primary';
"""
NATIVE_AGENT_SCHEMA_VERSION = 19
NATIVE_AGENT_ROLE = "agent"
NATIVE_INIT_GUIDANCE = (
    "Initialize the store through the public OpenClaw native initialization path for this agent "
    "(the native agent-store path) "
    "before auth sync; never create or mark SQLite schema manually."
)
AUTH_SYNC_SQL_TEMPLATE = """ATTACH DATABASE {source_db} AS source_auth;
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS auth_profile_store (
  store_key TEXT NOT NULL PRIMARY KEY,
  store_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_profile_state (
  state_key TEXT NOT NULL PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
DELETE FROM auth_profile_store;
INSERT INTO auth_profile_store SELECT store_key, store_json, updated_at FROM source_auth.auth_profile_store;
DELETE FROM auth_profile_state;
INSERT INTO auth_profile_state SELECT state_key, state_json, updated_at FROM source_auth.auth_profile_state;
COMMIT;
"""


def quote_sqlite_literal(value: str) -> str:
    """Quote a SQLite string literal for the guarded ATTACH statement."""

    return "'" + value.replace("'", "''") + "'"


def _guarded_copy_file(source: str, destination: str, context: str) -> None:
    if guarded_cp_file(source, destination, context) != 0:
        raise RuntimeError(f"ERROR: Failed to copy managed auth file to {destination}.")


def _guarded_chmod(mode: int, destination: str, context: str) -> None:
    if guarded_chmod(format(mode, "04o"), destination, context) != 0:
        raise RuntimeError(f"ERROR: Failed to chmod managed auth file {destination}.")


def _has_openai_profile(database: str) -> bool:
    try:
        with sqlite3.connect(_native_agent_database_uri(database, "ro"), uri=True) as connection:
            return connection.execute(AUTH_PROFILE_QUERY).fetchone() is not None
    except sqlite3.Error:
        return False


def _native_agent_database_uri(database: str, mode: str) -> str:
    return f"{Path(database).absolute().as_uri()}?mode={mode}"


def _raise_native_store_error(database: str, agent_id: str, detail: str) -> None:
    raise SystemExit(
        f"ERROR: Native agent store {database} is not initialized and owned by agent "
        f"{agent_id}: {detail}.\n"
        f"       {NATIVE_INIT_GUIDANCE}"
    )


def _validate_native_agent_database(
    database: str,
    agent_id: str,
    *,
    mode: str,
    context: str,
) -> None:
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        _raise_native_store_error(database, agent_id, "the existing regular database is missing")
    if not path_owned_by_effective_user(database):
        _raise_native_store_error(
            database,
            agent_id,
            "the existing database is not owned by the effective user",
        )
    try:
        guard_destination_path_chain(
            database,
            context,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        with sqlite3.connect(_native_agent_database_uri(database, mode), uri=True) as connection:
            connection.execute("PRAGMA query_only = ON;")
            row = connection.execute(NATIVE_AGENT_SCHEMA_QUERY).fetchone()
    except sqlite3.Error as exc:
        _raise_native_store_error(database, agent_id, f"schema_meta validation failed ({exc})")
    if row is None:
        _raise_native_store_error(
            database,
            agent_id,
            "schema_meta native ownership metadata row is absent",
        )
    role, schema_version, stored_agent_id, app_version = row
    if (
        role != NATIVE_AGENT_ROLE
        or schema_version != NATIVE_AGENT_SCHEMA_VERSION
        or stored_agent_id != agent_id
        or not isinstance(app_version, str)
        or not app_version
    ):
        _raise_native_store_error(
            database,
            agent_id,
            "schema_meta native ownership metadata does not identify the native agent store",
        )


def _sync_database(target: str, source: str) -> None:
    source_sql = quote_sqlite_literal(_native_agent_database_uri(source, "ro"))
    script = AUTH_SYNC_SQL_TEMPLATE.format(source_db=source_sql)
    try:
        with sqlite3.connect(_native_agent_database_uri(target, "rw"), uri=True) as connection:
            connection.executescript(script)
    except sqlite3.Error as exc:
        raise RuntimeError(str(exc)) from exc


def _openai_agent_ids(repo_config: str) -> list[str]:
    try:
        raw: object = json.loads(Path(repo_config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("agents"), dict):
        return []
    entries = raw["agents"].get("entries")
    if not isinstance(entries, dict):
        return []
    result: list[str] = []
    for agent_id, agent in entries.items():
        if not isinstance(agent_id, str):
            continue
        if not isinstance(agent, dict):
            continue
        model = agent.get("model")
        primary = model.get("primary") if isinstance(model, dict) else None
        if isinstance(primary, str) and primary.startswith("openai/"):
            result.append(agent_id)
    return result


def sync_managed_agent_codex_auth(
    push_home: str,
    repo_config: str,
    openclaw_bin: str,
) -> None:
    """Copy the main OpenAI profile rows to every managed OpenAI agent."""

    source_agent_dir = os.path.join(push_home, "agents/main/agent")
    source_db = os.path.join(source_agent_dir, "openclaw-agent.sqlite")
    source_profiles = os.path.join(source_agent_dir, "auth-profiles.json")

    if not os.path.isfile(source_db):
        raise SystemExit(
            f"ERROR: Missing main OpenClaw auth store {source_db}.\n"
            f"       Run: {openclaw_bin} models auth login --provider openai"
        )
    _validate_native_agent_database(
        source_db,
        "main",
        mode="ro",
        context=f"validating native OpenClaw agent database {source_db}",
    )
    if not _has_openai_profile(source_db):
        raise SystemExit(
            "ERROR: Main OpenClaw auth store has no OpenAI/Codex OAuth profile.\n"
            f"       Run: {openclaw_bin} models auth login --provider openai"
        )

    agent_ids = _openai_agent_ids(repo_config)
    if not agent_ids:
        raise SystemExit(f"ERROR: No OpenAI/Codex-managed agents found in {repo_config}")

    print(f"Syncing OpenClaw-managed Codex OAuth profile to {len(agent_ids)} agent auth stores:")
    for agent_id in agent_ids:
        agent_dir = os.path.join(push_home, f"agents/{agent_id}/agent")
        target_db = os.path.join(agent_dir, "openclaw-agent.sqlite")
        if agent_id == "main":
            print(f"  {agent_id} → {target_db} (source)")
            continue
        _validate_native_agent_database(
            target_db,
            agent_id,
            mode="rw",
            context=f"syncing managed OpenClaw agent auth database {target_db}",
        )
        if os.path.isfile(source_profiles):
            target_profiles = os.path.join(agent_dir, "auth-profiles.json")
            _guarded_copy_file(
                source_profiles,
                target_profiles,
                f"copying managed OpenClaw auth profile to {target_profiles}",
            )
            _guarded_chmod(
                0o600,
                target_profiles,
                f"chmod managed OpenClaw auth profile {target_profiles}",
            )
        _sync_database(target_db, source_db)
        _guarded_chmod(
            0o600,
            target_db,
            f"chmod managed OpenClaw agent auth database {target_db}",
        )
        if not _has_openai_profile(target_db):
            raise SystemExit(f"ERROR: Failed to sync OpenAI/Codex auth into {target_db}")
        print(f"  {agent_id} → {target_db}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("push_home")
    sync_parser.add_argument("repo_config")
    sync_parser.add_argument("openclaw_bin")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "sync":
            sync_managed_agent_codex_auth(args.push_home, args.repo_config, args.openclaw_bin)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
