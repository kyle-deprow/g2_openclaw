"""Tests for gateway.task_status."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from gateway.task_status import TaskInfo, read_task_status


def _write_transcript(directory: Path, lines: list[dict[str, object]]) -> Path:
    path = directory / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _msg(role: str, content: str) -> dict[str, object]:
    return {"type": "message", "message": {"role": role, "content": content}}


_RESOLVE_PATCH = "gateway.task_status.resolve_session_file"


class TestReadTaskStatus:
    """Tests for read_task_status()."""

    def test_no_session_file(self) -> None:
        with patch(_RESOLVE_PATCH, return_value=None):
            assert read_task_status("agent:claw:g2") is None

    def test_session_file_missing_on_disk(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.jsonl"
        with patch(_RESOLVE_PATCH, return_value=missing):
            assert read_task_status("agent:claw:g2") is None

    def test_no_task_markers(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path,
            [
                _msg("user", "Run a backtest"),
                _msg("assistant", "Sure, I'll plan that for you."),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            assert read_task_status("agent:claw:g2") is None

    def test_running_task(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path,
            [
                _msg("user", "Run backtest on SPY"),
                _msg("assistant", "[TASK:running] SPY momentum backtest | started: 14:30 UTC"),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "running"
            assert "SPY momentum backtest" in result.description

    def test_complete_task(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path,
            [
                _msg("user", "Run backtest on SPY"),
                _msg("assistant", "[TASK:running] SPY backtest | started: 14:30 UTC"),
                _msg(
                    "assistant", "[TASK:complete] SPY backtest | duration: 12m | result: Sharpe 1.2"
                ),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "complete"
            assert "Sharpe 1.2" in result.description

    def test_failed_task(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path,
            [
                _msg("assistant", "[TASK:running] SPY backtest | started: 14:30 UTC"),
                _msg("assistant", "[TASK:failed] SPY backtest | error: pytest failures"),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "failed"
            assert "pytest failures" in result.description

    def test_latest_marker_wins(self, tmp_path: Path) -> None:
        """When multiple task markers exist, the last one wins."""
        path = _write_transcript(
            tmp_path,
            [
                _msg("assistant", "[TASK:running] First task | started: 10:00 UTC"),
                _msg("assistant", "[TASK:complete] First task | duration: 5m | result: done"),
                _msg("assistant", "[TASK:running] Second task | started: 11:00 UTC"),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "running"
            assert "Second task" in result.description

    def test_content_block_format(self, tmp_path: Path) -> None:
        """Handle ContentBlock format (list of blocks) instead of plain string."""
        block_text = "[TASK:running] Block format test | started: 15:00 UTC"
        path = _write_transcript(
            tmp_path,
            [
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": block_text}],
                    },
                },
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "running"
            assert "Block format test" in result.description

    def test_user_messages_ignored(self, tmp_path: Path) -> None:
        """Only assistant messages are checked for task markers."""
        path = _write_transcript(
            tmp_path,
            [
                _msg("user", "[TASK:running] User should not be detected"),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            assert read_task_status("agent:claw:g2") is None

    def test_malformed_jsonl_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        lines = [
            "not valid json",
            json.dumps(_msg("assistant", "[TASK:running] Valid task | started: 12:00")),
            "{broken json",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "running"

    def test_case_insensitive_status(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path,
            [
                _msg("assistant", "[TASK:Running] Case test | started: 12:00"),
            ],
        )
        with patch(_RESOLVE_PATCH, return_value=path):
            result = read_task_status("agent:claw:g2")
            assert result is not None
            assert result.status == "running"


class TestTaskInfo:
    """Tests for the TaskInfo dataclass."""

    def test_frozen(self) -> None:
        info = TaskInfo(status="running", description="test")
        with pytest.raises(AttributeError):
            info.status = "complete"  # type: ignore[misc]

    def test_slots(self) -> None:
        info = TaskInfo(status="running", description="test")
        assert not hasattr(info, "__dict__")
