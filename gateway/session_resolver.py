"""Resolve OpenClaw session metadata from the local session store."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_ID = "main"


@dataclass(frozen=True)
class SessionMeta:
    """Resolved session metadata."""

    session_id: str
    session_key: str
    updated_at: str | None = None


def _sessions_json_path(agent_id: str = _DEFAULT_AGENT_ID) -> Path:
    """Return the path to the OpenClaw sessions.json file."""
    return Path.home() / ".openclaw" / "agents" / agent_id / "sessions" / "sessions.json"


def resolve_session(
    session_key: str = "agent:main:g2",
    agent_id: str = _DEFAULT_AGENT_ID,
) -> SessionMeta | None:
    """Read sessions.json and return metadata for the given session key.

    Returns None if the file doesn't exist, is unreadable, or the key
    is not present.
    """
    path = _sessions_json_path(agent_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError) as exc:
        logger.debug("Could not read sessions.json at %s: %s", path, exc)
        return None

    entry = data.get(session_key)
    if not isinstance(entry, dict):
        logger.debug("Session key %r not found in sessions.json", session_key)
        return None

    session_id = entry.get("sessionId")
    if not session_id or not isinstance(session_id, str):
        return None

    updated_at = _parse_updated_at(entry.get("updatedAt"))

    return SessionMeta(
        session_id=session_id,
        session_key=session_key,
        updated_at=updated_at,
    )


def _parse_updated_at(raw: object) -> str | None:
    """Parse an updatedAt value into an ISO 8601 string (or None)."""
    if raw is None:
        return None
    if isinstance(raw, int | float):
        ts_seconds = raw / 1000 if raw > 1e12 else raw
        return datetime.fromtimestamp(ts_seconds, tz=UTC).isoformat()
    return str(raw)


def list_sessions(
    agent_id: str = _DEFAULT_AGENT_ID,
) -> list[SessionMeta]:
    """Return metadata for ALL sessions in sessions.json.

    Results are sorted by updatedAt descending (most recent first).
    Returns an empty list if the file is missing or unreadable.
    """
    path = _sessions_json_path(agent_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError) as exc:
        logger.debug("Could not read sessions.json at %s: %s", path, exc)
        return []

    if not isinstance(data, dict):
        return []

    results: list[SessionMeta] = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("sessionId")
        if not session_id or not isinstance(session_id, str):
            continue
        updated_at = _parse_updated_at(entry.get("updatedAt"))
        results.append(
            SessionMeta(
                session_id=session_id,
                session_key=key,
                updated_at=updated_at,
            )
        )

    # Sort by updatedAt descending; entries without a timestamp go last
    results.sort(key=lambda m: m.updated_at or "", reverse=True)
    return results
