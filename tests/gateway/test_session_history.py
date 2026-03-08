"""Tests for gateway.session_history."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

from gateway.session_history import (
    HistoryEntry,
    _strip_timestamp_prefix,
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
# _strip_timestamp_prefix
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


class TestStripTimestampPrefix:
    def test_standard(self) -> None:
        assert _strip_timestamp_prefix("[2026-03-07 10:00 UTC] Hello") == "Hello"

    def test_no_prefix(self) -> None:
        assert _strip_timestamp_prefix("Hello world") == "Hello world"
