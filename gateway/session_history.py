"""Read conversation history from OpenClaw session transcript files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_ID = "claw"
_OPENCLAW_BASE = Path.home() / ".openclaw" / "agents"
DEFAULT_HISTORY_LIMIT = 10


@dataclass(frozen=True)
class HistoryEntry:
    """A single user or assistant message from the session transcript."""

    role: str  # "user" | "assistant"
    text: str  # plain-text content
    ts: int  # Unix milliseconds


def _extract_text(content: object) -> str:
    """Extract plain text from an OpenClaw message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def _strip_timestamp_prefix(text: str) -> str:
    """Remove the OpenClaw-injected timestamp prefix from user messages."""
    return re.sub(r"^\[.*?\]\s*", "", text, count=1)


def resolve_session_file(
    session_key: str = "agent:claw:g2",
    agent_id: str = _DEFAULT_AGENT_ID,
    base_path: Path | None = None,
) -> Path | None:
    """Resolve the JSONL transcript path for a session key."""
    base = base_path or _OPENCLAW_BASE
    sessions_dir = base / agent_id / "sessions"
    store_file = sessions_dir / "sessions.json"

    if not store_file.exists():
        logger.debug("sessions.json not found at %s", store_file)
        return None

    try:
        store = json.loads(store_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read sessions.json: %s", exc)
        return None

    session_meta = store.get(session_key)
    if not isinstance(session_meta, dict):
        logger.debug("Session key %r not found in sessions.json", session_key)
        return None

    session_id = session_meta.get("sessionId")
    if not session_id:
        return None

    jsonl_path = sessions_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        logger.debug("JSONL file not found: %s", jsonl_path)
        return None

    return jsonl_path


def read_history(
    session_key: str = "agent:claw:g2",
    agent_id: str = _DEFAULT_AGENT_ID,
    limit: int = DEFAULT_HISTORY_LIMIT,
    base_path: Path | None = None,
) -> list[HistoryEntry]:
    """Read the last ``limit`` user/assistant turns from a session transcript."""
    jsonl_path = resolve_session_file(
        session_key=session_key,
        agent_id=agent_id,
        base_path=base_path,
    )
    if jsonl_path is None:
        return []

    entries: list[HistoryEntry] = []
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "message":
                    continue

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue

                content = msg.get("content", "")
                text = _extract_text(content).strip()

                if role == "assistant" and not text:
                    continue

                if role == "assistant" and msg.get("stopReason") == "error":
                    continue

                if role == "user":
                    text = _strip_timestamp_prefix(text)

                ts = msg.get("timestamp", 0)
                if isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts)
                        ts = int(dt.timestamp() * 1000)
                    except ValueError:
                        ts = 0

                entries.append(HistoryEntry(role=role, text=text, ts=int(ts)))
    except OSError as exc:
        logger.warning("Failed to read JSONL transcript: %s", exc)
        return []

    return entries[-limit:]
