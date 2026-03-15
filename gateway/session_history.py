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
_DEFAULT_PREVIEW_MAX_LEN = 80

# Reverse-seek: only read the tail of large JSONL files (~500KB)
_HISTORY_TAIL_BYTES: int = 512_000

# Summary cache: session_key → (mtime, SessionSummary)
_summary_cache: dict[str, tuple[float, SessionSummary]] = {}


def clear_summary_cache() -> None:
    """Clear the in-memory session summary cache."""
    _summary_cache.clear()


@dataclass(frozen=True)
class HistoryEntry:
    """A single user or assistant message from the session transcript."""

    role: str  # "user" | "assistant"
    text: str  # plain-text content
    ts: int  # Unix milliseconds


def extract_text(content: object) -> str:
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


# Known system-injected prefixes to skip when picking session previews
_SYSTEM_PREFIXES = (
    "you are running as a subagent",
    "subagent context",
    "system context",
    "context:",
)


def _strip_bracket_prefixes(text: str) -> str:
    """Remove all leading [...] bracket tags from user messages.

    OpenClaw injects timestamp and context prefixes like:
      [2024-03-07T12:00:00Z] [Subagent Context] actual message...
    """
    return re.sub(r"^(\[.*?\]\s*)+", "", text)


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
        file_size = jsonl_path.stat().st_size
        with jsonl_path.open(encoding="utf-8") as f:
            # Estimate bytes needed: each JSONL line is typically ~300-500 bytes
            estimated_tail = max(_HISTORY_TAIL_BYTES, limit * 500)
            if file_size > estimated_tail:
                # Reverse-seek: jump near the end of the file
                f.seek(file_size - estimated_tail)
                f.readline()  # skip partial first line

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
                text = extract_text(content).strip()

                if role == "assistant" and not text:
                    continue

                if role == "assistant" and msg.get("stopReason") == "error":
                    continue

                if role == "user":
                    text = _strip_bracket_prefixes(text)

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


# ---------------------------------------------------------------------------
# Session summaries for the session menu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    """Lightweight summary of a session for the session list."""

    session_key: str
    session_id: str
    updated_at: str | None
    preview: str
    message_count: int


def session_summary(
    session_key: str,
    agent_id: str = _DEFAULT_AGENT_ID,
    base_path: Path | None = None,
    preview_max_len: int = _DEFAULT_PREVIEW_MAX_LEN,
    *,
    session_id: str | None = None,
    updated_at: str | None = None,
) -> SessionSummary | None:
    """Build a lightweight summary for one session.

    Reads the JSONL transcript to count messages and extract a preview.
    Uses an mtime-based cache to avoid re-parsing unchanged files.
    Returns None if the transcript file is missing.
    """
    jsonl_path = resolve_session_file(
        session_key=session_key,
        agent_id=agent_id,
        base_path=base_path,
    )
    if jsonl_path is None:
        return None

    # Check mtime cache
    try:
        current_mtime = jsonl_path.stat().st_mtime
    except OSError:
        return None

    cached = _summary_cache.get(session_key)
    if cached is not None:
        cached_mtime, cached_summary = cached
        # Float comparison: safe for local filesystems; on NFS/FUSE mtime
        # granularity may mask sub-second changes (acceptable for local-only use).
        if cached_mtime == current_mtime:
            return cached_summary

    first_user_text: str | None = None
    message_count = 0

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
                text = extract_text(content).strip()

                if role == "assistant" and not text:
                    continue
                if role == "assistant" and msg.get("stopReason") == "error":
                    continue

                message_count += 1

                if role == "user" and first_user_text is None:
                    text = _strip_bracket_prefixes(text)
                    # Skip system-injected context messages
                    if text and not text.lower().startswith(_SYSTEM_PREFIXES):
                        first_user_text = text
    except OSError as exc:
        logger.warning("Failed to read JSONL transcript for summary: %s", exc)
        return None

    preview = first_user_text or ""
    if len(preview) > preview_max_len:
        preview = preview[:preview_max_len]

    # Resolve session_id from sessions.json if not provided
    resolved_session_id = session_id
    if resolved_session_id is None:
        # Fallback: extract from the jsonl filename
        resolved_session_id = jsonl_path.stem

    result = SessionSummary(
        session_key=session_key,
        session_id=resolved_session_id,
        updated_at=updated_at,
        preview=preview,
        message_count=message_count,
    )

    # Store in cache
    _summary_cache[session_key] = (current_mtime, result)

    return result


def list_session_summaries(
    agent_id: str = _DEFAULT_AGENT_ID,
    base_path: Path | None = None,
    preview_max_len: int = _DEFAULT_PREVIEW_MAX_LEN,
) -> list[SessionSummary]:
    """Return summaries for all sessions, sorted by updatedAt descending."""
    from gateway.session_resolver import list_sessions

    metas = list_sessions(agent_id=agent_id)
    summaries: list[SessionSummary] = []
    for meta in metas:
        summary = session_summary(
            session_key=meta.session_key,
            agent_id=agent_id,
            base_path=base_path,
            preview_max_len=preview_max_len,
            session_id=meta.session_id,
            updated_at=meta.updated_at,
        )
        if summary is not None:
            summaries.append(summary)

    # Prune stale cache entries for sessions that no longer exist
    current_keys = {m.session_key for m in metas}
    stale = set(_summary_cache.keys()) - current_keys
    for k in stale:
        del _summary_cache[k]

    return summaries
