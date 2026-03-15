"""Tests for gateway.copilot_sessions — Copilot CLI session discovery."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from gateway.copilot_sessions import (
    _find_lock_pid,
    _parse_workspace_yaml,
    _pid_alive,
    invalidate_copilot_cache,
    kill_copilot_session,
    list_copilot_sessions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """Create a temporary session-state directory."""
    d = tmp_path / "session-state"
    d.mkdir()
    return d


def _make_session(
    state_dir: Path,
    session_id: str = "abc-123",
    cwd: str = "/home/dev/repos/myproject",
    branch: str = "main",
    summary: str = "Implement feature X",
    lock_pid: int | None = None,
    created_at: str = "2026-03-15T01:00:00.000Z",
    updated_at: str = "2026-03-15T02:00:00.000Z",
    repository: str = "user/myproject",
) -> Path:
    """Helper to create a fake session directory with workspace.yaml and optional lock."""
    session_dir = state_dir / session_id
    session_dir.mkdir(exist_ok=True)

    ws_yaml = (
        f"id: {session_id}\n"
        f"cwd: {cwd}\n"
        f"git_root: {cwd}\n"
        f"repository: {repository}\n"
        f"branch: {branch}\n"
        f"summary: {summary}\n"
        f"created_at: {created_at}\n"
        f"updated_at: {updated_at}\n"
    )
    (session_dir / "workspace.yaml").write_text(ws_yaml, encoding="utf-8")

    if lock_pid is not None:
        (session_dir / f"inuse.{lock_pid}.lock").write_text(str(lock_pid), encoding="utf-8")

    return session_dir


# ---------------------------------------------------------------------------
# _pid_alive
# ---------------------------------------------------------------------------


class TestPidAlive:
    def test_current_process_is_alive(self) -> None:
        assert _pid_alive(os.getpid()) is True

    def test_nonexistent_pid(self) -> None:
        # PID 4_000_000 almost certainly doesn't exist
        assert _pid_alive(4_000_000) is False


# ---------------------------------------------------------------------------
# _find_lock_pid
# ---------------------------------------------------------------------------


class TestFindLockPid:
    def test_no_lock_file(self, state_dir: Path) -> None:
        session = state_dir / "sess-1"
        session.mkdir()
        assert _find_lock_pid(session) is None

    def test_finds_lock_pid(self, state_dir: Path) -> None:
        session = state_dir / "sess-1"
        session.mkdir()
        (session / "inuse.12345.lock").write_text("12345")
        assert _find_lock_pid(session) == 12345

    def test_ignores_non_lock_files(self, state_dir: Path) -> None:
        session = state_dir / "sess-1"
        session.mkdir()
        (session / "workspace.yaml").write_text("id: sess-1")
        (session / "events.jsonl").write_text("")
        assert _find_lock_pid(session) is None


# ---------------------------------------------------------------------------
# _parse_workspace_yaml
# ---------------------------------------------------------------------------


class TestParseWorkspaceYaml:
    def test_parses_valid_yaml(self, state_dir: Path) -> None:
        session = _make_session(state_dir, session_id="test-sess")
        result = _parse_workspace_yaml(session)
        assert result is not None
        assert result["id"] == "test-sess"
        assert result["cwd"] == "/home/dev/repos/myproject"
        assert result["branch"] == "main"
        assert result["repository"] == "user/myproject"

    def test_missing_yaml_returns_none(self, state_dir: Path) -> None:
        session = state_dir / "no-yaml"
        session.mkdir()
        assert _parse_workspace_yaml(session) is None

    def test_corrupt_yaml_returns_none(self, state_dir: Path) -> None:
        session = state_dir / "corrupt"
        session.mkdir()
        (session / "workspace.yaml").write_text("][][invalid yaml{{{", encoding="utf-8")
        assert _parse_workspace_yaml(session) is None

    def test_non_dict_yaml_returns_none(self, state_dir: Path) -> None:
        session = state_dir / "bad-type"
        session.mkdir()
        (session / "workspace.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert _parse_workspace_yaml(session) is None


# ---------------------------------------------------------------------------
# list_copilot_sessions
# ---------------------------------------------------------------------------


class TestListCopilotSessions:
    def test_empty_dir(self, state_dir: Path) -> None:
        assert list_copilot_sessions(state_dir=state_dir) == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert list_copilot_sessions(state_dir=tmp_path / "does-not-exist") == []

    def test_running_session_with_live_pid(self, state_dir: Path) -> None:
        """A session with a lock file pointing to our own PID should be found."""
        my_pid = os.getpid()
        _make_session(
            state_dir,
            session_id="live-sess",
            cwd="/home/dev/repos/quantipy",
            branch="master",
            summary="Build backtest engine",
            lock_pid=my_pid,
        )

        sessions = list_copilot_sessions(state_dir=state_dir)
        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_id == "live-sess"
        assert s.cwd == "/home/dev/repos/quantipy"
        assert s.dir_name == "quantipy"
        assert s.branch == "master"
        assert s.summary == "Build backtest engine"
        assert s.pid == my_pid
        assert s.is_running is True

    def test_skips_dead_pid_when_running_only(self, state_dir: Path) -> None:
        """Sessions with a stale lock (dead PID) are excluded by default."""
        _make_session(
            state_dir,
            session_id="dead-sess",
            lock_pid=4_000_000,  # unlikely to be alive
        )

        sessions = list_copilot_sessions(state_dir=state_dir, running_only=True)
        assert len(sessions) == 0

    def test_includes_dead_pid_when_running_only_false(self, state_dir: Path) -> None:
        _make_session(
            state_dir,
            session_id="dead-sess",
            lock_pid=4_000_000,
        )

        sessions = list_copilot_sessions(state_dir=state_dir, running_only=False)
        assert len(sessions) == 1
        assert sessions[0].is_running is False

    def test_skips_session_without_lock(self, state_dir: Path) -> None:
        """Sessions without any lock file are skipped (running_only=True)."""
        _make_session(state_dir, session_id="no-lock", lock_pid=None)

        sessions = list_copilot_sessions(state_dir=state_dir, running_only=True)
        assert len(sessions) == 0

    def test_includes_no_lock_when_running_only_false(self, state_dir: Path) -> None:
        _make_session(state_dir, session_id="no-lock", lock_pid=None)

        sessions = list_copilot_sessions(state_dir=state_dir, running_only=False)
        assert len(sessions) == 1
        assert sessions[0].pid == 0
        assert sessions[0].is_running is False

    def test_sorts_by_updated_at_descending(self, state_dir: Path) -> None:
        my_pid = os.getpid()
        _make_session(
            state_dir,
            session_id="older",
            updated_at="2026-03-15T01:00:00.000Z",
            lock_pid=my_pid,
        )
        _make_session(
            state_dir,
            session_id="newer",
            updated_at="2026-03-15T03:00:00.000Z",
            lock_pid=my_pid,
        )

        sessions = list_copilot_sessions(state_dir=state_dir)
        assert len(sessions) == 2
        assert sessions[0].session_id == "newer"
        assert sessions[1].session_id == "older"

    def test_skips_missing_workspace_yaml(self, state_dir: Path) -> None:
        """Directories without workspace.yaml are silently skipped."""
        my_pid = os.getpid()
        bad = state_dir / "no-yaml-sess"
        bad.mkdir()
        (bad / f"inuse.{my_pid}.lock").write_text(str(my_pid))

        sessions = list_copilot_sessions(state_dir=state_dir)
        assert len(sessions) == 0

    def test_truncates_long_summary(self, state_dir: Path) -> None:
        my_pid = os.getpid()
        long_summary = "x" * 500
        _make_session(
            state_dir,
            session_id="long-summary",
            summary=long_summary,
            lock_pid=my_pid,
        )

        sessions = list_copilot_sessions(state_dir=state_dir)
        assert len(sessions) == 1
        assert len(sessions[0].summary) == 200

    def test_dataclass_is_frozen(self, state_dir: Path) -> None:
        my_pid = os.getpid()
        _make_session(state_dir, session_id="frozen-test", lock_pid=my_pid)
        sessions = list_copilot_sessions(state_dir=state_dir)
        with pytest.raises(AttributeError):
            sessions[0].cwd = "/other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# kill_copilot_session
# ---------------------------------------------------------------------------


class TestKillCopilotSession:
    def test_nonexistent_session_returns_false(self, state_dir: Path) -> None:
        assert kill_copilot_session("no-such-session", state_dir=state_dir) is False

    def test_no_lock_file_returns_false(self, state_dir: Path) -> None:
        _make_session(state_dir, session_id="no-lock", lock_pid=None)
        assert kill_copilot_session("no-lock", state_dir=state_dir) is False

    def test_dead_pid_returns_false(self, state_dir: Path) -> None:
        _make_session(state_dir, session_id="dead", lock_pid=4_000_000)
        assert kill_copilot_session("dead", state_dir=state_dir) is False

    def test_kills_live_subprocess(self, state_dir: Path) -> None:
        """Spawn a real subprocess, then kill it via kill_copilot_session."""
        import subprocess

        proc = subprocess.Popen(["sleep", "60"])
        _make_session(state_dir, session_id="live", lock_pid=proc.pid)

        result = kill_copilot_session("live", state_dir=state_dir)
        assert result is True

        # Wait for the process to actually die
        proc.wait(timeout=5)
        assert proc.returncode is not None  # process exited

    def test_nonexistent_dir_returns_false(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus-state"
        assert kill_copilot_session("x", state_dir=bogus) is False


# ---------------------------------------------------------------------------
# Copilot session cache (Task 3)
# ---------------------------------------------------------------------------


class TestCopilotSessionCache:
    """list_copilot_sessions() caches results with a TTL."""

    def setup_method(self) -> None:
        invalidate_copilot_cache()

    def teardown_method(self) -> None:
        invalidate_copilot_cache()

    def test_list_uses_cache_within_ttl(self, state_dir: Path) -> None:
        """Two calls within TTL return the same list object (cached)."""
        my_pid = os.getpid()
        _make_session(state_dir, session_id="cached-s1", lock_pid=my_pid)

        r1 = list_copilot_sessions(state_dir=state_dir)
        assert len(r1) == 1

        # Add a second session — should NOT appear if cache is used
        _make_session(
            state_dir,
            session_id="cached-s2",
            lock_pid=my_pid,
            updated_at="2026-03-15T04:00:00.000Z",
        )

        r2 = list_copilot_sessions(state_dir=state_dir)
        assert len(r2) == 1  # still cached
        assert r2 is r1  # same object identity

    def test_list_refreshes_after_ttl(self, state_dir: Path) -> None:
        """After TTL expires, cache is refreshed with fresh filesystem scan."""
        my_pid = os.getpid()
        _make_session(state_dir, session_id="ttl-s1", lock_pid=my_pid)

        # Patch TTL to something tiny so we can test expiration
        with patch("gateway.copilot_sessions._COPILOT_CACHE_TTL", 0.05):
            r1 = list_copilot_sessions(state_dir=state_dir)
            assert len(r1) == 1

            # Add another session
            _make_session(
                state_dir,
                session_id="ttl-s2",
                lock_pid=my_pid,
                updated_at="2026-03-15T04:00:00.000Z",
            )

            time.sleep(0.1)  # exceed TTL

            r2 = list_copilot_sessions(state_dir=state_dir)
            assert len(r2) == 2  # refreshed

    def test_kill_invalidates_cache(self, state_dir: Path) -> None:
        """After kill, next list call scans fresh."""
        import subprocess

        proc = subprocess.Popen(["sleep", "60"])
        _make_session(state_dir, session_id="kill-s1", lock_pid=proc.pid)

        r1 = list_copilot_sessions(state_dir=state_dir)
        assert len(r1) == 1

        # Kill the session — this should invalidate the cache
        kill_copilot_session("kill-s1", state_dir=state_dir)
        proc.wait(timeout=5)

        # Next call should scan fresh (process is now dead, running_only=True)
        r2 = list_copilot_sessions(state_dir=state_dir)
        assert len(r2) == 0  # session is dead now

    def test_invalidate_copilot_cache(self, state_dir: Path) -> None:
        """Explicit invalidation forces a fresh scan."""
        my_pid = os.getpid()
        _make_session(state_dir, session_id="inv-s1", lock_pid=my_pid)

        r1 = list_copilot_sessions(state_dir=state_dir)
        assert len(r1) == 1

        # Add another session
        _make_session(
            state_dir,
            session_id="inv-s2",
            lock_pid=my_pid,
            updated_at="2026-03-15T04:00:00.000Z",
        )

        # Without invalidation, still cached
        r2 = list_copilot_sessions(state_dir=state_dir)
        assert len(r2) == 1

        # Invalidate and re-list
        invalidate_copilot_cache()
        r3 = list_copilot_sessions(state_dir=state_dir)
        assert len(r3) == 2
