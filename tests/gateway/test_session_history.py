"""Tests for gateway.session_history."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

from gateway.session_history import (
    HistoryEntry,
    _strip_bracket_prefixes,
    read_history,
    resolve_session_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sessions_dir(tmp_path: Path, agent_id: str = "claw") -> Path:
    sessions_dir = tmp_path / agent_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def _write_sessions_json(sessions_dir: Path, session_key: str, session_id: str) -> None:
    store = {session_key: {"sessionId": session_id}}
    (sessions_dir / "sessions.json").write_text(json.dumps(store))


def _write_jsonl(sessions_dir: Path, session_id: str, lines: list[dict[str, Any]]) -> None:
    path = sessions_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _msg(role: str, content: str | list[object], **extra: object) -> dict[str, object]:
    """Build a JSONL message line."""
    msg: dict[str, object] = {"role": role, "content": content, "timestamp": 1700000000000}
    msg.update(extra)
    return {"type": "message", "message": msg}


# ---------------------------------------------------------------------------
# resolve_session_file
# ---------------------------------------------------------------------------


class TestResolveSessionFile:
    def test_returns_path_when_valid(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_abc")
        _write_jsonl(sd, "ses_abc", [_msg("user", "hello")])

        result = resolve_session_file(
            session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path
        )
        assert result is not None
        assert result.name == "ses_abc.jsonl"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        result = resolve_session_file(
            session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path
        )
        assert result is None

    def test_returns_none_when_key_absent(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "other:key", "ses_abc")

        result = resolve_session_file(
            session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path
        )
        assert result is None

    def test_returns_none_when_jsonl_missing(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_abc")
        # Don't create the JSONL file

        result = resolve_session_file(
            session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path
        )
        assert result is None

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        (sd / "sessions.json").write_text("{corrupt json!")

        result = resolve_session_file(
            session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path
        )
        assert result is None


# ---------------------------------------------------------------------------
# read_history
# ---------------------------------------------------------------------------


class TestReadHistory:
    def test_extracts_user_and_assistant(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("user", "What is 2+2?"),
                _msg("assistant", "4"),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 2
        assert entries[0] == HistoryEntry(role="user", text="What is 2+2?", ts=1700000000000)
        assert entries[1] == HistoryEntry(role="assistant", text="4", ts=1700000000000)

    def test_skips_system_and_tool_roles(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("system", "You are an assistant."),
                _msg("user", "hi"),
                _msg("tool", "result"),
                _msg("assistant", "Hello!"),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 2
        assert entries[0].role == "user"
        assert entries[1].role == "assistant"

    def test_skips_errored_assistant(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("user", "hi"),
                _msg("assistant", "Error occurred", stopReason="error"),
                _msg("assistant", "Hello!"),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 2
        assert entries[0].role == "user"
        assert entries[1].text == "Hello!"

    def test_skips_empty_assistant(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("user", "hi"),
                _msg("assistant", ""),
                _msg("assistant", "Real response"),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 2
        assert entries[0].role == "user"
        assert entries[1].text == "Real response"

    def test_strips_timestamp_prefix(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("user", "[2026-03-07 10:00 UTC] What time is it?"),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 1
        assert entries[0].text == "What time is it?"

    def test_extracts_content_blocks(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg(
                    "assistant",
                    [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": "World"},
                    ],
                ),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 1
        assert entries[0].text == "Hello World"

    def test_handles_string_content(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("assistant", "Simple string content"),
            ],
        )

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 1
        assert entries[0].text == "Simple string content"

    def test_respects_limit(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        lines = [_msg("user", f"msg-{i}") for i in range(20)]
        _write_jsonl(sd, "ses_1", lines)

        entries = read_history(
            session_key="agent:claw:g2", agent_id="claw", limit=5, base_path=tmp_path
        )
        assert len(entries) == 5
        # Should be the last 5
        assert entries[0].text == "msg-15"
        assert entries[4].text == "msg-19"

    def test_returns_empty_on_missing_file(self, tmp_path: Path) -> None:
        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert entries == []

    def test_handles_partial_trailing_line(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        # Write JSONL with a partial trailing line (no trailing newline, incomplete JSON)
        jsonl_path = sd / "ses_1.jsonl"
        valid_line = json.dumps(_msg("user", "hello"))
        jsonl_path.write_text(valid_line + '\n{"incomplete')

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 1
        assert entries[0].text == "hello"


# ---------------------------------------------------------------------------
# _strip_bracket_prefixes
# ---------------------------------------------------------------------------


class TestIsoTimestampParsing:
    """read_history parses ISO datetime strings into epoch millis."""

    def test_iso_timestamp_converted_to_epoch_millis(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_iso")
        lines = [
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": "hello",
                    "timestamp": "2026-03-07T10:00:00+00:00",
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": "hi there",
                    "timestamp": "2026-03-07T10:00:05+00:00",
                },
            },
        ]
        _write_jsonl(sd, "ses_iso", lines)

        entries = read_history(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert len(entries) == 2
        # 2026-03-07T10:00:00+00:00 ≈ 1772028800000 ms
        from datetime import datetime

        expected_ts_0 = int(datetime(2026, 3, 7, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)
        expected_ts_1 = int(datetime(2026, 3, 7, 10, 0, 5, tzinfo=UTC).timestamp() * 1000)
        assert entries[0].ts == expected_ts_0
        assert entries[1].ts == expected_ts_1


class TestStripBracketPrefixes:
    def test_single_bracket(self) -> None:
        assert _strip_bracket_prefixes("[2026-03-07 10:00 UTC] Hello") == "Hello"

    def test_multiple_brackets(self) -> None:
        assert (
            _strip_bracket_prefixes("[2024-03-07T12:00:00Z] [Subagent Context] actual message")
            == "actual message"
        )

    def test_no_prefix(self) -> None:
        assert _strip_bracket_prefixes("Hello world") == "Hello world"


# ---------------------------------------------------------------------------
# Reverse-seek read_history (Task 2)
# ---------------------------------------------------------------------------


class TestReadHistoryReverseSeek:
    """read_history() uses reverse-seek for large files."""

    def test_read_history_reverse_seek_large_file(self, tmp_path: Path) -> None:
        """Create a JSONL with 1000+ entries, verify read_history returns last N."""
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_big")

        # Generate 1500 messages (well over 512KB threshold)
        lines: list[dict[str, Any]] = []
        for i in range(1500):
            lines.append(_msg("user", f"message-{i:04d}-" + "x" * 200))

        _write_jsonl(sd, "ses_big", lines)

        entries = read_history(
            session_key="agent:claw:g2", agent_id="claw", limit=10, base_path=tmp_path
        )
        assert len(entries) == 10
        # Should be the last 10
        assert entries[0].text.startswith("message-1490-")
        assert entries[9].text.startswith("message-1499-")

    def test_read_history_small_file_unchanged(self, tmp_path: Path) -> None:
        """Small files (< 512KB) should work identically to before."""
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_sm")
        _write_jsonl(
            sd,
            "ses_sm",
            [
                _msg("user", "hello"),
                _msg("assistant", "world"),
                _msg("user", "foo"),
            ],
        )

        entries = read_history(
            session_key="agent:claw:g2", agent_id="claw", limit=2, base_path=tmp_path
        )
        assert len(entries) == 2
        assert entries[0].text == "world"
        assert entries[1].text == "foo"

    def test_read_history_large_limit_scales_seekback(self, tmp_path: Path) -> None:
        """A large limit scales the seek window so no messages are silently dropped."""
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_scale")

        # Generate 1500+ entries (each ~300 bytes → total ~450KB+)
        lines: list[dict[str, Any]] = []
        for i in range(1500):
            lines.append(_msg("user", f"line-{i:04d}-" + "x" * 200))
        _write_jsonl(sd, "ses_scale", lines)

        entries = read_history(
            session_key="agent:claw:g2", agent_id="claw", limit=500, base_path=tmp_path
        )
        assert len(entries) == 500
        # Should be the last 500 entries
        assert entries[0].text.startswith("line-1000-")
        assert entries[499].text.startswith("line-1499-")
