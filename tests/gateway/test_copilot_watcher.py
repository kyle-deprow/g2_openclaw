"""Tests for gateway.copilot_watcher — events.jsonl tail + transcript parsing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from gateway.copilot_watcher import (
    CopilotSessionWatcher,
    TranscriptEvent,
    _extract_ts,
    _parse_event_line,
)

# ---------------------------------------------------------------------------
# _parse_event_line
# ---------------------------------------------------------------------------


class TestParseEventLine:
    """Tests for individual JSONL line parsing."""

    def test_user_message(self) -> None:
        line = json.dumps(
            {"type": "user.message", "content": "Hello world", "timestamp": 1700000000}
        )
        evt = _parse_event_line(line)
        assert evt is not None
        assert evt.role == "user"
        assert evt.text == "Hello world"
        assert evt.ts == 1700000000000

    def test_assistant_message(self) -> None:
        line = json.dumps(
            {"type": "assistant.message", "content": "I can help", "timestamp": 1700000001}
        )
        evt = _parse_event_line(line)
        assert evt is not None
        assert evt.role == "assistant"
        assert evt.text == "I can help"

    def test_tool_execution_start(self) -> None:
        line = json.dumps(
            {"type": "tool.execution_start", "toolName": "bash", "timestamp": 1700000002}
        )
        evt = _parse_event_line(line)
        assert evt is not None
        assert evt.role == "system"
        assert "bash" in evt.text

    def test_tool_execution_start_with_tool_name_key(self) -> None:
        line = json.dumps(
            {"type": "tool.execution_start", "tool_name": "read_file", "timestamp": 1700000003}
        )
        evt = _parse_event_line(line)
        assert evt is not None
        assert "read_file" in evt.text

    def test_unknown_type_returns_none(self) -> None:
        line = json.dumps({"type": "response.completed", "timestamp": 1700000004})
        assert _parse_event_line(line) is None

    def test_empty_content_returns_none(self) -> None:
        line = json.dumps({"type": "user.message", "content": "  ", "timestamp": 1700000005})
        assert _parse_event_line(line) is None

    def test_invalid_json_returns_none(self) -> None:
        assert _parse_event_line("not json") is None

    def test_non_dict_returns_none(self) -> None:
        assert _parse_event_line('"just a string"') is None

    def test_missing_type_returns_none(self) -> None:
        line = json.dumps({"content": "no type field"})
        assert _parse_event_line(line) is None


# ---------------------------------------------------------------------------
# _extract_ts
# ---------------------------------------------------------------------------


class TestExtractTs:
    """Tests for timestamp extraction."""

    def test_seconds_converted_to_millis(self) -> None:
        assert _extract_ts({"timestamp": 1700000000}) == 1700000000000

    def test_millis_kept_as_is(self) -> None:
        assert _extract_ts({"timestamp": 1700000000000}) == 1700000000000

    def test_ts_key(self) -> None:
        assert _extract_ts({"ts": 1700000000}) == 1700000000000

    def test_created_at_key(self) -> None:
        assert _extract_ts({"created_at": 1700000000}) == 1700000000000

    def test_no_timestamp_returns_zero(self) -> None:
        assert _extract_ts({}) == 0

    def test_float_timestamp(self) -> None:
        assert _extract_ts({"timestamp": 1700000000.5}) == 1700000000500


# ---------------------------------------------------------------------------
# CopilotSessionWatcher.read_recent_history
# ---------------------------------------------------------------------------


class TestReadRecentHistory:
    """Tests for reading recent transcript history from events.jsonl."""

    def test_empty_file(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        (session_dir / "events.jsonl").write_text("")
        watcher = CopilotSessionWatcher(session_dir)
        assert watcher.read_recent_history() == []

    def test_missing_file(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        watcher = CopilotSessionWatcher(session_dir)
        assert watcher.read_recent_history() == []

    def test_reads_displayable_events(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        lines = [
            json.dumps({"type": "user.message", "content": "Hello", "timestamp": 1700000000}),
            json.dumps({"type": "response.completed"}),  # not displayable
            json.dumps(
                {"type": "assistant.message", "content": "Hi there", "timestamp": 1700000001}
            ),
        ]
        (session_dir / "events.jsonl").write_text("\n".join(lines) + "\n")

        watcher = CopilotSessionWatcher(session_dir)
        history = watcher.read_recent_history()

        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["text"] == "Hello"
        assert history[1]["role"] == "assistant"
        assert history[1]["text"] == "Hi there"

    def test_respects_limit(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        lines = [
            json.dumps({"type": "user.message", "content": f"msg {i}", "timestamp": 1700000000 + i})
            for i in range(30)
        ]
        (session_dir / "events.jsonl").write_text("\n".join(lines) + "\n")

        watcher = CopilotSessionWatcher(session_dir)
        history = watcher.read_recent_history(limit=5)

        assert len(history) == 5
        # Should be the last 5 messages
        assert history[0]["text"] == "msg 25"
        assert history[4]["text"] == "msg 29"

    def test_sets_offset_for_streaming(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        content = (
            json.dumps({"type": "user.message", "content": "test", "timestamp": 1700000000}) + "\n"
        )
        (session_dir / "events.jsonl").write_text(content)

        watcher = CopilotSessionWatcher(session_dir)
        watcher.read_recent_history()
        assert watcher._offset > 0


# ---------------------------------------------------------------------------
# CopilotSessionWatcher._read_new_lines
# ---------------------------------------------------------------------------


class TestReadNewLines:
    """Tests for incremental line reading."""

    def test_reads_new_lines_after_offset(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        events_path = session_dir / "events.jsonl"

        # Write initial content
        line1 = (
            json.dumps({"type": "user.message", "content": "first", "timestamp": 1700000000}) + "\n"
        )
        events_path.write_text(line1)

        watcher = CopilotSessionWatcher(session_dir)
        watcher.read_recent_history()  # sets offset

        # Append new content
        line2 = (
            json.dumps({"type": "assistant.message", "content": "second", "timestamp": 1700000001})
            + "\n"
        )
        with events_path.open("a") as f:
            f.write(line2)

        new_events = watcher._read_new_lines()
        assert len(new_events) == 1
        assert new_events[0].role == "assistant"
        assert new_events[0].text == "second"

    def test_no_new_content_returns_empty(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        content = (
            json.dumps({"type": "user.message", "content": "test", "timestamp": 1700000000}) + "\n"
        )
        (session_dir / "events.jsonl").write_text(content)

        watcher = CopilotSessionWatcher(session_dir)
        watcher.read_recent_history()
        assert watcher._read_new_lines() == []

    def test_non_displayable_lines_skipped(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        events_path = session_dir / "events.jsonl"
        events_path.write_text("")

        watcher = CopilotSessionWatcher(session_dir)
        watcher._offset = 0

        # Append non-displayable event
        line = json.dumps({"type": "response.completed"}) + "\n"
        events_path.write_text(line)

        assert watcher._read_new_lines() == []


# ---------------------------------------------------------------------------
# CopilotSessionWatcher._has_lock_file
# ---------------------------------------------------------------------------


class TestHasLockFile:
    """Tests for lock file detection."""

    def test_returns_true_when_lock_exists(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        (session_dir / "inuse.1234.lock").write_text("1234")

        watcher = CopilotSessionWatcher(session_dir)
        assert watcher._has_lock_file() is True

    def test_returns_false_when_no_lock(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()

        watcher = CopilotSessionWatcher(session_dir)
        assert watcher._has_lock_file() is False

    def test_ignores_non_lock_files(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        (session_dir / "workspace.yaml").write_text("id: test")

        watcher = CopilotSessionWatcher(session_dir)
        assert watcher._has_lock_file() is False


# ---------------------------------------------------------------------------
# CopilotSessionWatcher.stream_events (async)
# ---------------------------------------------------------------------------


class TestStreamEvents:
    """Tests for the async event streaming loop."""

    @pytest.mark.asyncio
    async def test_yields_new_events(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        events_path = session_dir / "events.jsonl"
        events_path.write_text("")
        # Create lock file so watcher doesn't immediately stop
        (session_dir / "inuse.9999.lock").write_text("9999")

        watcher = CopilotSessionWatcher(session_dir)
        watcher._offset = 0

        # Schedule writing an event after a short delay
        async def write_event() -> None:
            await asyncio.sleep(0.1)
            line = (
                json.dumps(
                    {
                        "type": "user.message",
                        "content": "streaming test",
                        "timestamp": 1700000000,
                    }
                )
                + "\n"
            )
            with events_path.open("a") as f:
                f.write(line)
            await asyncio.sleep(0.7)
            watcher.stop()

        task = asyncio.create_task(write_event())

        events = []
        async for event in watcher.stream_events():
            events.append(event)

        await task
        assert len(events) >= 1
        assert events[0]["text"] == "streaming test"

    @pytest.mark.asyncio
    async def test_stops_when_lock_file_removed(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        events_path = session_dir / "events.jsonl"
        events_path.write_text("")
        lock_file = session_dir / "inuse.9999.lock"
        lock_file.write_text("9999")

        watcher = CopilotSessionWatcher(session_dir)
        watcher._offset = 0

        # Remove lock file after a short delay
        async def remove_lock() -> None:
            await asyncio.sleep(0.1)
            lock_file.unlink()

        task = asyncio.create_task(remove_lock())

        events = []
        async for event in watcher.stream_events():
            events.append(event)

        await task
        # Should have stopped — no events expected
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_stop_method_terminates_loop(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        events_path = session_dir / "events.jsonl"
        events_path.write_text("")
        (session_dir / "inuse.9999.lock").write_text("9999")

        watcher = CopilotSessionWatcher(session_dir)
        watcher._offset = 0

        async def stop_after_delay() -> None:
            await asyncio.sleep(0.1)
            watcher.stop()

        task = asyncio.create_task(stop_after_delay())

        events = []
        async for event in watcher.stream_events():
            events.append(event)

        await task
        # Loop should have terminated cleanly
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# TranscriptEvent dataclass
# ---------------------------------------------------------------------------


class TestTranscriptEvent:
    """Tests for the TranscriptEvent dataclass."""

    def test_frozen(self) -> None:
        evt = TranscriptEvent(role="user", text="hello", ts=1700000000000)
        with pytest.raises(AttributeError):
            evt.role = "assistant"  # type: ignore[misc]

    def test_fields(self) -> None:
        evt = TranscriptEvent(role="assistant", text="response", ts=123)
        assert evt.role == "assistant"
        assert evt.text == "response"
        assert evt.ts == 123
