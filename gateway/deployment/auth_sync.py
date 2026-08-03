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
    guarded_mkdir_p,
)

AUTH_PROFILE_QUERY = (
    'select 1 from auth_profile_store where store_json like \'%"provider":"openai"%\' limit 1;'
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
    """Quote a SQLite string using the shell helper's exact convention."""

    return "'" + value.replace("'", "''") + "'"


def _guarded_mkdir(path: str, context: str) -> None:
    if guarded_mkdir_p(path, context) != 0:
        raise RuntimeError(f"ERROR: Failed to create managed directory {path}.")


def _guarded_copy_file(source: str, destination: str, context: str) -> None:
    if guarded_cp_file(source, destination, context) != 0:
        raise RuntimeError(f"ERROR: Failed to copy managed auth file to {destination}.")


def _guarded_chmod(mode: int, destination: str, context: str) -> None:
    if guarded_chmod(format(mode, "04o"), destination, context) != 0:
        raise RuntimeError(f"ERROR: Failed to chmod managed auth file {destination}.")


def _has_openai_profile(database: str) -> bool:
    try:
        with sqlite3.connect(database) as connection:
            return connection.execute(AUTH_PROFILE_QUERY).fetchone() is not None
    except sqlite3.Error:
        return False


def _sync_database(target: str, source: str) -> None:
    source_sql = quote_sqlite_literal(source)
    script = AUTH_SYNC_SQL_TEMPLATE.format(source_db=source_sql)
    try:
        with sqlite3.connect(target) as connection:
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
    agents = raw["agents"].get("list")
    if not isinstance(agents, list):
        return []
    result: list[str] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        model = agent.get("model")
        primary = model.get("primary") if isinstance(model, dict) else None
        if isinstance(primary, str) and primary.startswith("openai/"):
            agent_id = agent.get("id")
            if isinstance(agent_id, str):
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
        _guarded_mkdir(
            agent_dir,
            f"creating managed OpenClaw agent auth directory {agent_dir}",
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
        guard_destination_path_chain(
            target_db,
            f"syncing managed OpenClaw agent auth database {target_db}",
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
