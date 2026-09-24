"""Read-only readiness checks for OpenClaw's native shared auth store."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from .guarded_fs import guard_destination_path_chain, path_owned_by_effective_user

NATIVE_SHARED_SCHEMA_VERSION = 15
NATIVE_AGENT_SCHEMA_VERSION = 19
NATIVE_APP_VERSION = "2026.9.2"
NATIVE_SHARED_ROLE = "global"
NATIVE_AGENT_ROLE = "agent"
SHARED_STORE_OWNERSHIP_KEY = "auth.sharedStore"
SHARED_STORE_KEY = "authProfiles.store"
SHARED_STATE_KEY = "authProfiles.state"
PRIMARY_ROW_KEY = "primary"
SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

NATIVE_INIT_GUIDANCE = (
    "Initialize the store through the public OpenClaw native initialization path for this agent "
    "before publication; never create or mark SQLite schema manually."
)


def _raise_error(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def _validate_database_file(database: str, context: str) -> None:
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        _raise_error(f"Native OpenClaw database {database} is not an existing regular file.")
    if not path_owned_by_effective_user(database):
        _raise_error(f"Native OpenClaw database {database} is not owned by the effective user.")
    try:
        guard_destination_path_chain(database, context)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def _database_uri(database: str) -> str:
    return f"{Path(database).absolute().as_uri()}?mode=ro"


def _open_readonly(database: str) -> sqlite3.Connection:
    connection = sqlite3.connect(_database_uri(database), uri=True)
    connection.execute("PRAGMA query_only = ON;")
    return connection


def _validate_shared_database(database: str) -> dict[str, object]:
    _validate_database_file(database, f"validating native OpenClaw shared database {database}")
    try:
        with _open_readonly(database) as connection:
            row = connection.execute(
                """
                SELECT role, schema_version, agent_id, app_version
                FROM schema_meta
                WHERE meta_key = 'primary'
                """
            ).fetchone()
            if row is None:
                _raise_error(f"Native shared OpenClaw database {database} has no schema metadata.")
            role, schema_version, agent_id, app_version = row
            if (
                role != NATIVE_SHARED_ROLE
                or schema_version != NATIVE_SHARED_SCHEMA_VERSION
                or agent_id is not None
                or app_version != NATIVE_APP_VERSION
            ):
                _raise_error(
                    f"Native shared OpenClaw database {database} has incompatible schema metadata."
                )
            rows = dict(
                connection.execute(
                    """
                    SELECT state_key, value_json
                    FROM config_machine_state
                    WHERE state_key IN (?, ?, ?)
                    """,
                    (SHARED_STORE_OWNERSHIP_KEY, SHARED_STORE_KEY, SHARED_STATE_KEY),
                ).fetchall()
            )
    except SystemExit:
        raise
    except sqlite3.Error as exc:
        _raise_error(f"Native shared OpenClaw database {database} is unreadable ({exc}).")
    ownership = _parse_json_cell(rows.get(SHARED_STORE_OWNERSHIP_KEY), SHARED_STORE_OWNERSHIP_KEY)
    if ownership != {"location": "state-db"}:
        _raise_error(
            f"Native shared OpenClaw database {database} does not declare state-db auth ownership."
        )
    store = _parse_profile_store(
        _parse_json_cell(rows.get(SHARED_STORE_KEY), SHARED_STORE_KEY), SHARED_STORE_KEY
    )
    state_raw = rows.get(SHARED_STATE_KEY)
    if state_raw is not None:
        state = _parse_json_cell(state_raw, SHARED_STATE_KEY)
        if not isinstance(state, dict) or state.get("version") != 1:
            _raise_error(f"Native shared OpenClaw auth state {SHARED_STATE_KEY} is incompatible.")
    return store


def _parse_json_cell(raw: object, key: str) -> object:
    if not isinstance(raw, str):
        _raise_error(f"Native OpenClaw auth state {key} is missing.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _raise_error(f"Native OpenClaw auth state {key} is malformed JSON ({exc.msg}).")


def _parse_profile_store(raw: object, key: str) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        _raise_error(f"Native OpenClaw auth store {key} has an incompatible shape.")
    record = raw
    profiles = record.get("profiles")
    if not isinstance(profiles, dict):
        _raise_error(f"Native OpenClaw auth store {key} has an incompatible profiles map.")
    for profile_id, profile in profiles.items():
        if not isinstance(profile_id, str) or not profile_id:
            _raise_error(f"Native OpenClaw auth store {key} has an unsafe profile id.")
        if not isinstance(profile, dict):
            _raise_error(f"Native OpenClaw auth store {key} has a malformed profile.")
        provider = profile.get("provider")
        profile_type = profile.get("type")
        if not isinstance(provider, str) or not provider:
            _raise_error(f"Native OpenClaw auth store {key} has a profile without a provider.")
        if profile_type not in {"api_key", "oauth", "token"}:
            _raise_error(f"Native OpenClaw auth store {key} has an unsupported profile type.")
        for field in ("access", "refresh", "key", "token", "oauthRef"):
            if field in profile and not isinstance(profile[field], str):
                _raise_error(f"Native OpenClaw auth store {key} has malformed credential metadata.")
        if "expires" in profile and (
            isinstance(profile["expires"], bool)
            or not isinstance(profile["expires"], (int, float))
            or not math.isfinite(float(profile["expires"]))
        ):
            _raise_error(f"Native OpenClaw auth store {key} has malformed expiry metadata.")
    return record


def _has_eligible_openai_oauth(store: dict[str, object]) -> bool:
    profiles = store["profiles"]
    assert isinstance(profiles, dict)
    for profile in profiles.values():
        assert isinstance(profile, dict)
        if profile.get("provider") != "openai" or profile.get("type") != "oauth":
            continue
        access = profile.get("access")
        refresh = profile.get("refresh")
        if (isinstance(access, str) and access.strip()) or (
            isinstance(refresh, str) and refresh.strip()
        ):
            return True
    return False


def _native_agent_store(database: str, agent_id: str) -> dict[str, object] | None:
    _validate_database_file(database, f"validating native OpenClaw agent database {database}")
    try:
        with _open_readonly(database) as connection:
            row = connection.execute(
                """
                SELECT role, schema_version, agent_id, app_version
                FROM schema_meta
                WHERE meta_key = 'primary'
                """
            ).fetchone()
            if row is None or row[0] != NATIVE_AGENT_ROLE or row[1] != NATIVE_AGENT_SCHEMA_VERSION:
                _raise_error(
                    f"Native agent store {database} has incompatible ownership metadata.\n"
                    f"       {NATIVE_INIT_GUIDANCE}"
                )
            if row[2] != agent_id or row[3] != NATIVE_APP_VERSION:
                _raise_error(
                    f"Native agent store {database} does not identify agent {agent_id}.\n"
                    f"       {NATIVE_INIT_GUIDANCE}"
                )
            store_row = connection.execute(
                """
                SELECT store_json
                FROM auth_profile_store
                WHERE store_key = ?
                """,
                (PRIMARY_ROW_KEY,),
            ).fetchone()
    except SystemExit:
        raise
    except sqlite3.Error as exc:
        _raise_error(
            f"Native agent store {database} is unreadable ({exc}).\n       {NATIVE_INIT_GUIDANCE}"
        )
    if store_row is None:
        return None
    store = _parse_json_cell(store_row[0], f"agent auth store {database}")
    return _parse_profile_store(store, f"agent auth store {database}")


def _safe_agent_id(agent_id: object) -> str:
    if not isinstance(agent_id, str) or SAFE_AGENT_ID.fullmatch(agent_id) is None:
        _raise_error(f"unsafe agent id in managed OpenAI roster: {agent_id!r}.")
    return agent_id


def _openai_agent_ids(repo_config: str) -> list[str]:
    try:
        raw: object = json.loads(Path(repo_config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _raise_error(f"Cannot read managed OpenClaw config {repo_config} ({exc}).")
    if not isinstance(raw, dict) or not isinstance(raw.get("agents"), dict):
        _raise_error(f"Managed OpenClaw config {repo_config} has no agents roster.")
    agents = raw["agents"]
    assert isinstance(agents, dict)
    entries = agents.get("entries")
    if not isinstance(entries, dict):
        _raise_error(f"Managed OpenClaw config {repo_config} has no agents entries.")
    result: list[str] = []
    for raw_agent_id, agent in entries.items():
        if not isinstance(agent, dict):
            continue
        model = agent.get("model")
        primary = model.get("primary") if isinstance(model, dict) else None
        if isinstance(primary, str) and primary.startswith("openai/"):
            result.append(_safe_agent_id(raw_agent_id))
    if not result:
        _raise_error(f"No OpenAI/Codex-managed agents found in {repo_config}.")
    return result


def check_managed_agent_codex_auth_readiness(
    push_home: str,
    repo_config: str,
) -> None:
    """Check native shared and agent-local auth state without changing either store."""

    root = os.path.abspath(push_home)
    shared_db = os.path.join(root, "state/openclaw.sqlite")
    shared_store = _validate_shared_database(shared_db)
    if not _has_eligible_openai_oauth(shared_store):
        _raise_error("Native shared OpenClaw auth store has no eligible OpenAI OAuth profile.")

    agent_ids = _openai_agent_ids(repo_config)
    for agent_id in agent_ids:
        database = os.path.join(root, "agents", agent_id, "agent", "openclaw-agent.sqlite")
        local_store = _native_agent_store(database, agent_id)
        if local_store is not None:
            if not _has_eligible_openai_oauth(local_store):
                _raise_error(
                    f"Native local OpenClaw auth store for agent {agent_id} has an incompatible "
                    "local OpenAI OAuth override."
                )
            print(f"  {agent_id}: local OpenAI OAuth override verified")
        else:
            print(f"  {agent_id}: shared read-through")
    print(
        "Native OpenClaw shared Codex OAuth readiness verified for "
        f"{len(agent_ids)} managed agent(s)."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("push_home")
    check_parser.add_argument("repo_config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check":
            check_managed_agent_codex_auth_readiness(
                args.push_home,
                args.repo_config,
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
