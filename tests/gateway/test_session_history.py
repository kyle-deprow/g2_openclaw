"""Tests for gateway.session_history."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gateway.session_history import (
    HistoryEntry,
    _strip_bracket_prefixes,
    _summary_cache,
    clear_summary_cache,
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


# ---------------------------------------------------------------------------
# Summary cache (Task 1)
# ---------------------------------------------------------------------------


class TestSummaryCacheMtime:
    """session_summary() caches by file mtime and clear_summary_cache() resets it."""

    def setup_method(self) -> None:
        clear_summary_cache()

    def teardown_method(self) -> None:
        clear_summary_cache()

    def test_session_summary_uses_cache_on_same_mtime(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_c1")
        _write_jsonl(sd, "ses_c1", [_msg("user", "Hello")])

        r1 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r1 is not None
        assert r1.preview == "Hello"

        # Overwrite the file content but keep the same mtime
        jsonl_path = sd / "ses_c1.jsonl"
        original_stat = jsonl_path.stat()
        jsonl_path.write_text(json.dumps(_msg("user", "Changed")) + "\n")
        # Restore original mtime so the cache still hits
        os.utime(jsonl_path, (original_stat.st_atime, original_stat.st_mtime))

        r2 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r2 is not None
        # Should return the cached value (old preview) because mtime unchanged
        assert r2.preview == "Hello"
        assert r2 is r1  # same object identity

    def test_session_summary_invalidates_on_mtime_change(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_c2")
        _write_jsonl(sd, "ses_c2", [_msg("user", "First")])

        r1 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r1 is not None
        assert r1.preview == "First"

        # Modify the file (new content AND new mtime)
        jsonl_path = sd / "ses_c2.jsonl"
        time.sleep(0.05)  # ensure mtime changes
        jsonl_path.write_text(
            json.dumps(_msg("user", "Second"))
            + "\n"
            + json.dumps(_msg("assistant", "Reply"))
            + "\n"
        )

        r2 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r2 is not None
        assert r2.preview == "Second"
        assert r2.message_count == 2

    def test_clear_summary_cache(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)
        _write_sessions_json(sd, "agent:claw:g2", "ses_c3")
        _write_jsonl(sd, "ses_c3", [_msg("user", "Original")])

        r1 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r1 is not None
        assert r1.preview == "Original"

        # Overwrite but keep mtime identical
        jsonl_path = sd / "ses_c3.jsonl"
        original_stat = jsonl_path.stat()
        jsonl_path.write_text(json.dumps(_msg("user", "Replaced")) + "\n")
        os.utime(jsonl_path, (original_stat.st_atime, original_stat.st_mtime))

        # Without clearing, cache still returns old data
        r2 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r2 is not None
        assert r2.preview == "Original"

        # After clearing, it re-reads
        clear_summary_cache()
        r3 = session_summary(session_key="agent:claw:g2", agent_id="claw", base_path=tmp_path)
        assert r3 is not None
        assert r3.preview == "Replaced"


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


# ---------------------------------------------------------------------------
# Summary cache pruning (Must-Fix #1)
# ---------------------------------------------------------------------------


class TestSummaryCachePruning:
    """list_session_summaries() prunes stale entries from _summary_cache."""

    def setup_method(self) -> None:
        clear_summary_cache()

    def teardown_method(self) -> None:
        clear_summary_cache()

    def test_summary_cache_prunes_stale_entries(self, tmp_path: Path) -> None:
        sd = _make_sessions_dir(tmp_path)

        # Create two sessions
        store = {
            "agent:claw:keep": {
                "sessionId": "ses_keep",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
            "agent:claw:remove": {
                "sessionId": "ses_remove",
                "updatedAt": "2026-03-07T09:00:00Z",
            },
        }
        (sd / "sessions.json").write_text(json.dumps(store))
        _write_jsonl(sd, "ses_keep", [_msg("user", "Hello")])
        _write_jsonl(sd, "ses_remove", [_msg("user", "Goodbye")])

        from gateway.session_resolver import SessionMeta

        metas_both = [
            SessionMeta(
                session_id="ses_keep",
                session_key="agent:claw:keep",
                updated_at="2026-03-07T10:00:00Z",
            ),
            SessionMeta(
                session_id="ses_remove",
                session_key="agent:claw:remove",
                updated_at="2026-03-07T09:00:00Z",
            ),
        ]
        with patch("gateway.session_resolver.list_sessions", return_value=metas_both):
            result = list_session_summaries(agent_id="claw", base_path=tmp_path)

        assert len(result) == 2
        assert "agent:claw:keep" in _summary_cache
        assert "agent:claw:remove" in _summary_cache

        # Now remove one session from sessions.json
        store2 = {
            "agent:claw:keep": {
                "sessionId": "ses_keep",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
        }
        (sd / "sessions.json").write_text(json.dumps(store2))

        metas_one = [
            SessionMeta(
                session_id="ses_keep",
                session_key="agent:claw:keep",
                updated_at="2026-03-07T10:00:00Z",
            ),
        ]
        with patch("gateway.session_resolver.list_sessions", return_value=metas_one):
            result2 = list_session_summaries(agent_id="claw", base_path=tmp_path)

        assert len(result2) == 1
        # The deleted session's key should be pruned from the cache
        assert "agent:claw:keep" in _summary_cache
        assert "agent:claw:remove" not in _summary_cache
