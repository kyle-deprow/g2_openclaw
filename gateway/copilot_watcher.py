"""Tail a Copilot CLI events.jsonl and yield structured transcript events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Event types we surface in the transcript
_DISPLAYABLE_TYPES = frozenset({"user.message", "assistant.message", "tool.execution_start"})

_POLL_INTERVAL = 0.5  # seconds
_MAX_HISTORY_BYTES = 512_000  # ~500 KB look-back for recent history


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """A single displayable transcript event."""

    role: str  # "user" | "assistant" | "system"
    text: str
    ts: int  # epoch millis


def _parse_event_line(line: str) -> TranscriptEvent | None:
    """Parse a single JSONL line into a TranscriptEvent, or None if not displayable."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    event_type = data.get("type", "")
    if event_type not in _DISPLAYABLE_TYPES:
        return None

    ts = _extract_ts(data)

    if event_type == "user.message":
        content = data.get("content", "")
        if isinstance(content, str) and content.strip():
            return TranscriptEvent(role="user", text=content.strip(), ts=ts)

    elif event_type == "assistant.message":
        content = data.get("content", "")
        if isinstance(content, str) and content.strip():
            return TranscriptEvent(role="assistant", text=content.strip(), ts=ts)

    elif event_type == "tool.execution_start":
        tool_name = data.get("toolName", data.get("tool_name", "tool"))
        return TranscriptEvent(role="system", text=f"[🔧 {tool_name}]", ts=ts)

    return None


def _extract_ts(data: dict[str, object]) -> int:
    """Extract a timestamp in epoch millis from an event dict."""
    for key in ("timestamp", "ts", "created_at"):
        val = data.get(key)
        if isinstance(val, int | float):
            # Auto-detect seconds vs millis
            return int(val * 1000) if val < 1e12 else int(val)
    return 0


class CopilotSessionWatcher:
    """Tail a Copilot CLI events.jsonl and yield structured transcript events."""

    def __init__(self, session_dir: Path) -> None:
        self._events_path = session_dir / "events.jsonl"
        self._session_dir = session_dir
        self._offset: int = 0
        self._running: bool = False

    def read_recent_history(self, limit: int = 20) -> list[dict[str, str | int]]:
        """Read the last N displayable events from events.jsonl.

        Sets ``self._offset`` to end of file for subsequent streaming.

        Returns:
            List of ``{"role": ..., "text": ..., "ts": ...}`` dicts.
        """
        if not self._events_path.exists():
            return []

        try:
            file_size = self._events_path.stat().st_size
        except OSError:
            return []

        # Seek near end for efficiency
        start_pos = max(0, file_size - _MAX_HISTORY_BYTES)

        events: list[TranscriptEvent] = []
        try:
            with self._events_path.open("r", encoding="utf-8", errors="replace") as f:
                if start_pos > 0:
                    f.seek(start_pos)
                    f.readline()  # skip partial line

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    evt = _parse_event_line(line)
                    if evt is not None:
                        events.append(evt)

                self._offset = f.tell()
        except OSError:
            logger.debug("Failed to read events.jsonl", exc_info=True)
            return []

        # Return last `limit` events
        trimmed = events[-limit:] if len(events) > limit else events
        return [{"role": e.role, "text": e.text, "ts": e.ts} for e in trimmed]

    async def stream_events(self) -> AsyncIterator[dict[str, str | int]]:
        """Yield new transcript events by polling events.jsonl.

        Polls every 500ms for new bytes. Yields ``{"role", "text", "ts"}`` dicts.
        Stops when ``stop()`` is called, the lock file disappears, or cancelled.
        """
        self._running = True

        while self._running:
            try:
                new_events = self._read_new_lines()
                for evt in new_events:
                    yield {"role": evt.role, "text": evt.text, "ts": evt.ts}

                # Check if session is still running (lock file present)
                if not self._has_lock_file():
                    logger.info("Copilot session lock file gone — stopping watcher")
                    break

                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        """Signal the stream_events loop to stop."""
        self._running = False

    def _read_new_lines(self) -> list[TranscriptEvent]:
        """Read new lines from events.jsonl since last offset."""
        if not self._events_path.exists():
            return []

        try:
            file_size = self._events_path.stat().st_size
        except OSError:
            return []

        if file_size <= self._offset:
            return []

        events: list[TranscriptEvent] = []
        try:
            with self._events_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                remainder = ""
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    remainder += chunk

                lines = remainder.split("\n")
                # Last element may be a partial line — keep it for next read
                if remainder.endswith("\n"):
                    complete_lines = lines[:-1]  # last is empty string
                    self._offset = f.tell()
                else:
                    complete_lines = lines[:-1]
                    # Don't advance offset past partial line
                    partial_len = len(lines[-1].encode("utf-8"))
                    self._offset = f.tell() - partial_len

                for line in complete_lines:
                    line = line.strip()
                    if not line:
                        continue
                    evt = _parse_event_line(line)
                    if evt is not None:
                        events.append(evt)
        except OSError:
            logger.debug("Failed to read new events", exc_info=True)

        return events

    def _has_lock_file(self) -> bool:
        """Check if any inuse.*.lock file exists in the session directory."""
        try:
            for entry in self._session_dir.iterdir():
                if entry.name.startswith("inuse.") and entry.name.endswith(".lock"):
                    return True
        except OSError:
            pass
        return False
