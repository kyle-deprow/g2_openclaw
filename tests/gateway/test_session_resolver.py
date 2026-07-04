"""Tests for gateway.session_resolver."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from gateway.session_resolver import SessionMeta, list_sessions, resolve_session


def _write_sessions(tmp_path: Path, data: Any, agent_id: str = "main") -> Path:
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
            "agent:main:g2": {
                "sessionId": "ses_abc123",
                "updatedAt": "2026-03-07T10:00:00Z",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = resolve_session()

        assert result is not None
        assert result == SessionMeta(
            session_id="ses_abc123",
            session_key="agent:main:g2",
            updated_at="2026-03-07T10:00:00Z",
        )

    def test_returns_none_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nonexistent" / "sessions.json"
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": missing
        )

        assert resolve_session() is None

    def test_returns_none_when_session_key_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:main:other": {
                "sessionId": "ses_other",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        assert resolve_session() is None

    def test_returns_none_on_corrupt_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / "sessions.json"
        path.write_text("{not valid json!!")
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        assert resolve_session() is None

    def test_returns_none_when_session_id_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:main:g2": {
                "updatedAt": "2026-03-07T10:00:00Z",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        assert resolve_session() is None

    def test_returns_none_when_session_id_is_not_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:main:g2": {
                "sessionId": 12345,
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        assert resolve_session() is None

    def test_custom_agent_id_reads_correct_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "agent:main:g2": {
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
            "agent:main:g2": {
                "sessionId": "ses_no_ts",
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = resolve_session()

        assert result is not None
        assert result.updated_at is None

    def test_numeric_updated_at_converted_to_iso(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A numeric updatedAt (Unix timestamp) is converted to an ISO 8601 string."""
        sessions = {
            "agent:main:g2": {
                "sessionId": "ses_numeric_ts",
                "updatedAt": 1772028800,
            }
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = resolve_session()

        assert result is not None
        assert result.session_id == "ses_numeric_ts"
        # Must be an ISO 8601 string, not a number
        assert isinstance(result.updated_at, str)
        assert "T" in result.updated_at  # basic ISO 8601 check


class TestListSessions:
    """list_sessions returns all valid entries from sessions.json."""

    def test_returns_all_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions = {
            "agent:main:g2": {
                "sessionId": "ses_1",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
            "agent:main:g2:123:abc": {
                "sessionId": "ses_2",
                "updatedAt": "2026-03-07T12:00:00Z",
            },
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = list_sessions()
        assert len(result) == 2
        # Most recent first
        assert result[0].session_id == "ses_2"
        assert result[1].session_id == "ses_1"

    def test_skips_invalid_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions: dict[str, Any] = {
            "agent:main:g2": {
                "sessionId": "ses_valid",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
            "agent:main:no_id": {
                "updatedAt": "2026-03-07T11:00:00Z",
            },
            "agent:main:bad_id": {
                "sessionId": 12345,
            },
            "agent:main:not_dict": "just_a_string",
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = list_sessions()
        assert len(result) == 1
        assert result[0].session_id == "ses_valid"

    def test_sorted_by_updated_at_descending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "key:old": {
                "sessionId": "ses_old",
                "updatedAt": "2026-03-01T00:00:00Z",
            },
            "key:new": {
                "sessionId": "ses_new",
                "updatedAt": "2026-03-07T00:00:00Z",
            },
            "key:mid": {
                "sessionId": "ses_mid",
                "updatedAt": "2026-03-04T00:00:00Z",
            },
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = list_sessions()
        assert [r.session_id for r in result] == ["ses_new", "ses_mid", "ses_old"]

    def test_returns_empty_for_empty_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_sessions(tmp_path, {})
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = list_sessions()
        assert result == []

    def test_returns_empty_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nonexistent" / "sessions.json"
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": missing
        )

        result = list_sessions()
        assert result == []

    def test_handles_ms_timestamps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """JS Date.now() style timestamps (milliseconds) are parsed correctly."""
        sessions = {
            "agent:main:g2": {
                "sessionId": "ses_ms",
                "updatedAt": 1772028800000,  # ms timestamp
            },
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = list_sessions()
        assert len(result) == 1
        assert result[0].session_id == "ses_ms"
        assert isinstance(result[0].updated_at, str)
        assert "T" in result[0].updated_at

    def test_none_updated_at_sorted_last(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions: dict[str, Any] = {
            "key:with_ts": {
                "sessionId": "ses_ts",
                "updatedAt": "2026-03-07T10:00:00Z",
            },
            "key:no_ts": {
                "sessionId": "ses_no_ts",
            },
        }
        path = _write_sessions(tmp_path, sessions)
        monkeypatch.setattr(
            "gateway.session_resolver._sessions_json_path", lambda agent_id="main": path
        )

        result = list_sessions()
        assert len(result) == 2
        assert result[0].session_id == "ses_ts"
        assert result[1].session_id == "ses_no_ts"
