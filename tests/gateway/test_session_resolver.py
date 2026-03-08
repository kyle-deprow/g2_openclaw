"""Tests for gateway.session_resolver."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from gateway.session_resolver import SessionMeta, resolve_session


def _write_sessions(tmp_path: Path, data: Any, agent_id: str = "claw") -> Path:
    """Write a sessions.json fixture and return the file path."""
    sessions_dir = tmp_path / ".openclaw" / "agents" / agent_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / "sessions.json"
    path.write_text(json.dumps(data))
    return path


class TestResolveSession:
    """resolve_session reads and parses sessions.json correctly."""

    def test_returns_session_meta_from_valid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:claw:g2": {
                "sessionId": "ses_abc123",
                "updatedAt": "2026-03-07T10:00:00Z",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        result = resolve_session()

        assert result is not None
        assert result == SessionMeta(
            session_id="ses_abc123",
            session_key="agent:claw:g2",
            updated_at="2026-03-07T10:00:00Z",
        )

    def test_returns_none_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nonexistent" / "sessions.json"
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": missing
        )

        assert resolve_session() is None

    def test_returns_none_when_session_key_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:claw:other": {
                "sessionId": "ses_other",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        assert resolve_session() is None

    def test_returns_none_on_corrupt_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions_dir = tmp_path / ".openclaw" / "agents" / "claw" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / "sessions.json"
        path.write_text("{not valid json!!")
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        assert resolve_session() is None

    def test_returns_none_when_session_id_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:claw:g2": {
                "updatedAt": "2026-03-07T10:00:00Z",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        assert resolve_session() is None

    def test_returns_none_when_session_id_is_not_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:claw:g2": {
                "sessionId": 12345,
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        assert resolve_session() is None

    def test_custom_agent_id_reads_correct_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:claw:g2": {
                "sessionId": "ses_custom",
            }
        }
        path = _write_sessions(tmp_path, sessions, agent_id="myagent")
        mock_path_fn = MagicMock(return_value=path)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path",
            mock_path_fn,
        )

        result = resolve_session(agent_id="myagent")

        assert result is not None
        assert result.session_id == "ses_custom"
        mock_path_fn.assert_called_once_with("myagent")

    def test_updated_at_is_none_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:claw:g2": {
                "sessionId": "ses_no_ts",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        result = resolve_session()

        assert result is not None
        assert result.updated_at is None

    def test_numeric_updated_at_converted_to_iso(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A numeric updatedAt (Unix timestamp) is converted to an ISO 8601 string."""
        sessions = {
            "agent:claw:g2": {
                "sessionId": "ses_numeric_ts",
                "updatedAt": 1772028800,
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="claw": path
        )

        result = resolve_session()

        assert result is not None
        assert result.session_id == "ses_numeric_ts"
        # Must be an ISO 8601 string, not a number
        assert isinstance(result.updated_at, str)
        assert "T" in result.updated_at  # basic ISO 8601 check
