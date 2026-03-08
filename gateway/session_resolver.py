"""Resolve OpenClaw session metadata from the local session store."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_ID = "claw"


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
    session_key: str = "agent:claw:g2",
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

    raw_updated = entry.get("updatedAt")
    if raw_updated is None:
        updated_at = None
    elif isinstance(raw_updated, int | float):
        # OpenClaw stores timestamps in milliseconds (JS Date.now())
        ts_seconds = raw_updated / 1000 if raw_updated > 1e12 else raw_updated
        updated_at = datetime.fromtimestamp(ts_seconds, tz=UTC).isoformat()
    else:
        updated_at = str(raw_updated)

    return SessionMeta(
        session_id=session_id,
        session_key=session_key,
        updated_at=updated_at,
    )
