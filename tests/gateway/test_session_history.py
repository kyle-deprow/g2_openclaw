"""Tests for gateway.session_history."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

from gateway.session_history import (
    HistoryEntry,
    _strip_bracket_prefixes,
    list_session_summaries,
    read_history,
    resolve_session_file,
    session_summary,
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
# session_summary
# ---------------------------------------------------------------------------


class TestSessionSummary:
    def test_returns_preview_and_count(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg("user", "What is the capital of France?"),
                _msg("assistant", "Paris"),
                _msg("user", "And Germany?"),
                _msg("assistant", "Berlin"),
            ],
        )

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
            session_id="ses_1",
            updated_at="2026-03-07T10:00:00Z",
        )
        assert result is not None
        assert result.preview == "What is the capital of France?"
        assert result.message_count == 4
        assert result.session_id == "ses_1"
        assert result.updated_at == "2026-03-07T10:00:00Z"

    def test_truncates_long_preview(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        long_text = "A" * 200
        _write_jsonl(
            sd,
            "ses_1",
            [_msg("user", long_text)],
        )

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
            preview_max_len=80,
        )
        assert result is not None
        assert len(result.preview) == 80

    def test_returns_none_for_missing_transcript(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_missing")
        # Not creating the JSONL file

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
        )
        assert result is None

    def test_empty_transcript_returns_zero_count(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_empty")
        _write_jsonl(sd, "ses_empty", [])

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
            session_id="ses_empty",
        )
        assert result is not None
        assert result.preview == ""
        assert result.message_count == 0

    def test_strips_timestamp_prefix_from_preview(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [_msg("user", "[2026-03-07 10:00 UTC] What time?")],
        )

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
        )
        assert result is not None
        assert result.preview == "What time?"

    def test_skips_system_injected_user_messages(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [
                _msg(
                    "user",
                    "[2026-03-07T10:00:00Z] [Subagent Context] You are running as a subagent...",
                ),
                _msg("user", "What is the weather today?"),
                _msg("assistant", "Sunny!"),
            ],
        )

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
        )
        assert result is not None
        assert result.preview == "What is the weather today?"
        assert result.message_count == 3

    def test_strips_multiple_bracket_prefixes_from_preview(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_1")
        _write_jsonl(
            sd,
            "ses_1",
            [_msg("user", "[2026-03-07 10:00 UTC] [Context] What time?")],
        )

        result = session_summary(
            session_key="agent:claw:g2",
            agent_id="claw",
            base_path=tmp_path,
        )
        assert result is not None
        assert result.preview == "What time?"


# ---------------------------------------------------------------------------
# list_session_summaries
# ---------------------------------------------------------------------------


class TestListSessionSummaries:
    def test_combines_resolver_and_history_data(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)

        # Write sessions.json with two entries
        store = {
            "agent:claw:g2": {
                "sessionId": "ses_1",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
            "agent:claw:g2:2": {
                "sessionId": "ses_2",
                "updatedAt": "2026-03-07T12:00:00Z",
            },
        }
        (sd / "sessions.json").write_text(json.dumps(store))

        _write_jsonl(
            sd,
            "ses_1",
            [_msg("user", "Hello"), _msg("assistant", "Hi")],
        )
        _write_jsonl(
            sd,
            "ses_2",
            [_msg("user", "Goodbye")],
        )

        from unittest.mock import patch

        from gateway.session_resolver import SessionMeta

        metas = [
            SessionMeta(
                session_id="ses_2",
                session_key="agent:claw:g2:2",
                updated_at="2026-03-07T12:00:00Z",
            ),
            SessionMeta(
                session_id="ses_1",
                session_key="agent:claw:g2",
                updated_at="2026-03-07T10:00:00Z",
            ),
        ]
        with patch("gateway.session_resolver.list_sessions", return_value=metas):
            result = list_session_summaries(agent_id="claw", base_path=tmp_path)

        assert len(result) == 2
        assert result[0].session_key == "agent:claw:g2:2"
        assert result[0].preview == "Goodbye"
        assert result[0].message_count == 1
        assert result[1].session_key == "agent:claw:g2"
        assert result[1].preview == "Hello"
        assert result[1].message_count == 2

    def test_filters_stale_sessions(self, tmp_path: Path) -> None:
        """Sessions with no JSONL file are excluded from the list."""
        sd = _make_sessions_dir(tmp_path)

        store = {
            "agent:claw:g2": {
                "sessionId": "ses_exists",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
            "agent:claw:stale": {
                "sessionId": "ses_stale",
                "updatedAt": "2026-03-06T10:00:00Z",
            },
        }
        (sd / "sessions.json").write_text(json.dumps(store))

        # Only create JSONL for ses_exists
        _write_jsonl(sd, "ses_exists", [_msg("user", "Hello")])

        from unittest.mock import patch

        from gateway.session_resolver import SessionMeta

        metas = [
            SessionMeta(
                session_id="ses_exists",
                session_key="agent:claw:g2",
                updated_at="2026-03-07T10:00:00Z",
            ),
            SessionMeta(
                session_id="ses_stale",
                session_key="agent:claw:stale",
                updated_at="2026-03-06T10:00:00Z",
            ),
        ]
        with patch("gateway.session_resolver.list_sessions", return_value=metas):
            result = list_session_summaries(agent_id="claw", base_path=tmp_path)

        assert len(result) == 1
        assert result[0].session_key == "agent:claw:g2"
