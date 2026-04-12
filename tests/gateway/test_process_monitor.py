"""Tests for gateway.process_monitor."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from gateway.process_monitor import (
    CopilotProcessMonitor,
    DeathReport,
    OrphanReaper,
    ReapResult,
    _OrphanCandidate,
)


@pytest.fixture()
def mock_notify() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def monitor(mock_notify: AsyncMock) -> CopilotProcessMonitor:
    return CopilotProcessMonitor(
        notify_callback=mock_notify,
        target_dirs=["/home/dev/repos/quantipy"],
    )


class TestTracking:
    """Tests for process discovery and death detection."""

    @pytest.mark.asyncio()
    async def test_discovers_target_process(
        self, monitor: CopilotProcessMonitor, mock_notify: AsyncMock
    ) -> None:
        """Should track a Copilot session working on a target repo."""
        fake_session = MagicMock()
        fake_session.pid = 12345
        fake_session.session_id = "abc-123"
        fake_session.cwd = "/home/dev/repos/quantipy"
        fake_session.summary = "Fix backtest"

        with patch(
            "gateway.process_monitor.list_copilot_sessions",
            return_value=[fake_session],
        ):
            await monitor._poll()

        assert 12345 in monitor.tracked_pids
        mock_notify.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ignores_non_target_process(
        self, monitor: CopilotProcessMonitor, mock_notify: AsyncMock
    ) -> None:
        """Should NOT track Copilot sessions in other repos."""
        fake_session = MagicMock()
        fake_session.pid = 99999
        fake_session.session_id = "xyz"
        fake_session.cwd = "/home/dev/repos/other_project"
        fake_session.summary = "Something else"

        with patch(
            "gateway.process_monitor.list_copilot_sessions",
            return_value=[fake_session],
        ):
            await monitor._poll()

        assert 99999 not in monitor.tracked_pids

    @pytest.mark.asyncio()
    async def test_notifies_on_death(
        self, monitor: CopilotProcessMonitor, mock_notify: AsyncMock
    ) -> None:
        """Should send notification when tracked process disappears."""
        fake_session = MagicMock()
        fake_session.pid = 12345
        fake_session.session_id = "abc-123"
        fake_session.cwd = "/home/dev/repos/quantipy"
        fake_session.summary = "Fix backtest"

        # First poll: discover
        with patch(
            "gateway.process_monitor.list_copilot_sessions",
            return_value=[fake_session],
        ):
            await monitor._poll()

        assert 12345 in monitor.tracked_pids

        # Second poll: process gone
        with (
            patch(
                "gateway.process_monitor.list_copilot_sessions",
                return_value=[],
            ),
            patch.object(
                monitor,
                "_build_report",
                return_value=DeathReport(
                    pid=12345,
                    cwd="/home/dev/repos/quantipy",
                    summary="Fix backtest",
                    recent_commits="abc1234 fix something",
                    sanity_output="",
                    has_uncommitted=False,
                ),
            ),
        ):
            await monitor._poll()

        assert 12345 not in monitor.tracked_pids
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][0]
        assert "[TASK:complete]" in msg
        assert "12345" in msg

    @pytest.mark.asyncio()
    async def test_dirty_tree_reports_failed(
        self, monitor: CopilotProcessMonitor, mock_notify: AsyncMock
    ) -> None:
        """Dirty tree should produce [TASK:failed] instead of complete."""
        fake_session = MagicMock()
        fake_session.pid = 55555
        fake_session.session_id = "xyz"
        fake_session.cwd = "/home/dev/repos/quantipy"
        fake_session.summary = "Add feature"

        # Discover
        with patch(
            "gateway.process_monitor.list_copilot_sessions",
            return_value=[fake_session],
        ):
            await monitor._poll()

        # Die with dirty tree
        with (
            patch(
                "gateway.process_monitor.list_copilot_sessions",
                return_value=[],
            ),
            patch.object(
                monitor,
                "_build_report",
                return_value=DeathReport(
                    pid=55555,
                    cwd="/home/dev/repos/quantipy",
                    summary="Add feature",
                    recent_commits="",
                    sanity_output="",
                    has_uncommitted=True,
                ),
            ),
        ):
            await monitor._poll()

        msg = mock_notify.call_args[0][0]
        assert "[TASK:failed]" in msg
        assert "uncommitted" in msg.lower()


class TestFormatMessage:
    """Tests for death report formatting."""

    def test_clean_exit_format(self, monitor: CopilotProcessMonitor) -> None:
        report = DeathReport(
            pid=1000,
            cwd="/home/dev/repos/quantipy",
            summary="Fix E1-LAG backtest",
            recent_commits="abc1234 fix: horizon mismatch",
            sanity_output="",
            has_uncommitted=False,
        )
        msg = monitor._format_message(report)
        assert "[TASK:complete]" in msg
        assert "1000" in msg
        assert "Fix E1-LAG backtest" in msg
        assert "abc1234" in msg
        assert "autoresearch" in msg.lower()

    def test_sanity_output_included(self, monitor: CopilotProcessMonitor) -> None:
        report = DeathReport(
            pid=2000,
            cwd="/x",
            summary="",
            recent_commits="",
            sanity_output="BUG: Sharpe > 10",
            has_uncommitted=False,
        )
        msg = monitor._format_message(report)
        assert "BUG: Sharpe > 10" in msg


class _EmptyAsyncIter:
    """Async iterator that yields nothing — simulates an empty stream."""

    def __aiter__(self) -> _EmptyAsyncIter:
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration


class TestNotifyRetry:
    """Tests for _notify_openclaw_with_retry in gateway.server."""

    @pytest.mark.asyncio()
    async def test_succeeds_on_first_attempt(self) -> None:
        """Should send message and drain stream without retrying."""
        from gateway.server import _notify_openclaw_with_retry

        client = AsyncMock()
        client.send_message = AsyncMock(return_value=_EmptyAsyncIter())

        await _notify_openclaw_with_retry(client, "hello", "session:key")

        client.send_message.assert_called_once_with("hello", session_key="session:key")
        client.disconnect.assert_not_called()

    @pytest.mark.asyncio()
    async def test_retries_on_transient_failure(self) -> None:
        """Should retry after OpenClawError and succeed on second attempt."""
        from gateway.openclaw_client import OpenClawError
        from gateway.server import _notify_openclaw_with_retry

        client = AsyncMock()
        client.send_message = AsyncMock(
            side_effect=[OpenClawError("connection refused"), _EmptyAsyncIter()]
        )

        with patch("gateway.server.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await _notify_openclaw_with_retry(client, "msg", "s:k")

        assert client.send_message.call_count == 2
        client.disconnect.assert_called_once()
        mock_sleep.assert_called_once_with(5)

    @pytest.mark.asyncio()
    async def test_retries_exhaust_all_attempts(self) -> None:
        """Should try all attempts then give up without raising."""
        from gateway.openclaw_client import OpenClawError
        from gateway.server import _notify_openclaw_with_retry

        client = AsyncMock()
        client.send_message = AsyncMock(side_effect=OpenClawError("always fails"))

        with patch("gateway.server.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await _notify_openclaw_with_retry(client, "msg", "s:k")

        # 1 initial + 3 retries = 4 total attempts
        assert client.send_message.call_count == 4
        assert client.disconnect.call_count == 3
        assert mock_sleep.call_args_list == [
            ((5,),),
            ((15,),),
            ((30,),),
        ]

    @pytest.mark.asyncio()
    async def test_succeeds_on_third_attempt(self) -> None:
        """Should succeed after two failures."""
        from gateway.openclaw_client import OpenClawError
        from gateway.server import _notify_openclaw_with_retry

        client = AsyncMock()
        client.send_message = AsyncMock(
            side_effect=[
                OpenClawError("refused"),
                OpenClawError("restart"),
                _EmptyAsyncIter(),
            ]
        )

        with patch("gateway.server.asyncio.sleep", new_callable=AsyncMock):
            await _notify_openclaw_with_retry(client, "msg", "s:k")

        assert client.send_message.call_count == 3
        assert client.disconnect.call_count == 2


# ---------------------------------------------------------------------------
# OrphanReaper tests
# ---------------------------------------------------------------------------


def _ps_line(pid: int, ppid: int, etimes: int, rss: int, args: str) -> str:
    return f"  {pid}   {ppid}      {etimes}  {rss} {args}"


class TestFindOrphans:
    """Tests for OrphanReaper._find_orphans."""

    def test_finds_loky_orphan_with_ppid_1(self) -> None:
        """Loky worker with ppid=1 and age > 120s should be detected."""
        ps_output = _ps_line(9001, 1, 600, 512000, "/usr/bin/python -m loky.backend.popen")
        result = MagicMock(returncode=0, stdout=ps_output)

        with patch("gateway.process_monitor.subprocess.run", return_value=result):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 1
        assert orphans[0].pid == 9001
        assert orphans[0].rss_kb == 512000

    def test_finds_joblib_orphan_with_systemd_parent(self) -> None:
        """Joblib worker under systemd --user parent should be detected."""
        ps_output = _ps_line(9002, 2129, 300, 256000, "python -c from joblib import ...")
        result = MagicMock(returncode=0, stdout=ps_output)
        parent_result = MagicMock(returncode=0, stdout="/usr/lib/systemd/systemd --user\n")

        with patch(
            "gateway.process_monitor.subprocess.run",
            side_effect=[result, parent_result],
        ):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 1
        assert orphans[0].pid == 9002

    def test_finds_ipykernel_orphan(self) -> None:
        """ipykernel worker with ppid=1 should be detected."""
        ps_output = _ps_line(9003, 1, 200, 100000, "python -m ipykernel_launcher -f conn.json")
        result = MagicMock(returncode=0, stdout=ps_output)

        with patch("gateway.process_monitor.subprocess.run", return_value=result):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 1
        assert orphans[0].pid == 9003

    def test_ignores_young_process(self) -> None:
        """Processes running < 120s should not be reaped."""
        ps_output = _ps_line(9004, 1, 60, 512000, "python -m loky.backend.popen")
        result = MagicMock(returncode=0, stdout=ps_output)

        with patch("gateway.process_monitor.subprocess.run", return_value=result):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 0

    def test_ignores_non_orphan(self) -> None:
        """Loky worker with a normal parent should not be reaped."""
        ps_output = _ps_line(9005, 5000, 600, 512000, "python -m loky.backend.popen")
        ps_result = MagicMock(returncode=0, stdout=ps_output)
        parent_result = MagicMock(returncode=0, stdout="python run_pipeline.py\n")

        with patch(
            "gateway.process_monitor.subprocess.run",
            side_effect=[ps_result, parent_result],
        ):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 0

    def test_ignores_unrelated_process(self) -> None:
        """Process without loky/joblib/ipykernel in args should be ignored."""
        ps_output = _ps_line(9006, 1, 600, 512000, "/usr/bin/vim some_file.py")
        result = MagicMock(returncode=0, stdout=ps_output)

        with patch("gateway.process_monitor.subprocess.run", return_value=result):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 0

    def test_caches_systemd_parent_lookup(self) -> None:
        """Multiple orphans with the same ppid should only look up parent once."""
        lines = "\n".join(
            [
                _ps_line(9010, 2129, 300, 100000, "python loky worker 1"),
                _ps_line(9011, 2129, 400, 200000, "python loky worker 2"),
            ]
        )
        ps_result = MagicMock(returncode=0, stdout=lines)
        parent_result = MagicMock(returncode=0, stdout="/usr/lib/systemd/systemd --user\n")

        with patch(
            "gateway.process_monitor.subprocess.run",
            side_effect=[ps_result, parent_result],
        ) as mock_run:
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 2
        # ps called twice: once for listing, once for parent lookup (cached for second orphan)
        assert mock_run.call_count == 2

    def test_handles_ps_failure(self) -> None:
        """Should return empty list when ps fails."""
        result = MagicMock(returncode=1, stdout="")

        with patch("gateway.process_monitor.subprocess.run", return_value=result):
            orphans = OrphanReaper._find_orphans()

        assert orphans == []

    def test_skips_malformed_ps_lines(self) -> None:
        """Lines with fewer than 5 fields or non-numeric PID should be silently skipped."""
        ps_output = "\n".join(
            [
                "bad line",
                "abc 1 600 512000 python loky worker",
                _ps_line(9001, 1, 600, 512000, "python -m loky.backend.popen"),
            ]
        )
        result = MagicMock(returncode=0, stdout=ps_output)

        with patch("gateway.process_monitor.subprocess.run", return_value=result):
            orphans = OrphanReaper._find_orphans()

        assert len(orphans) == 1
        assert orphans[0].pid == 9001


class TestKillProcess:
    """Tests for OrphanReaper._kill_process."""

    @pytest.mark.asyncio()
    async def test_sigterm_then_sigkill(self) -> None:
        """Should send SIGTERM, wait, then SIGKILL."""
        with (
            patch("gateway.process_monitor.os.kill") as mock_kill,
            patch("gateway.process_monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await OrphanReaper._kill_process(9001)

        assert result is True
        assert mock_kill.call_args_list == [
            call(9001, signal.SIGTERM),
            call(9001, signal.SIGKILL),
        ]
        mock_sleep.assert_called_once_with(5)

    @pytest.mark.asyncio()
    async def test_process_already_gone(self) -> None:
        """Should return False if process doesn't exist at SIGTERM time."""
        with patch(
            "gateway.process_monitor.os.kill",
            side_effect=ProcessLookupError,
        ):
            result = await OrphanReaper._kill_process(9999)

        assert result is False

    @pytest.mark.asyncio()
    async def test_no_permission(self) -> None:
        """Should return False if we can't kill the process."""
        with patch(
            "gateway.process_monitor.os.kill",
            side_effect=PermissionError,
        ):
            result = await OrphanReaper._kill_process(9001)

        assert result is False

    @pytest.mark.asyncio()
    async def test_sigterm_sufficient(self) -> None:
        """Should succeed if SIGTERM kills it and SIGKILL gets ProcessLookupError."""
        effects = [None, ProcessLookupError]

        with (
            patch("gateway.process_monitor.os.kill", side_effect=effects) as mock_kill,
            patch("gateway.process_monitor.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await OrphanReaper._kill_process(9001)

        assert result is True
        assert mock_kill.call_args_list == [
            call(9001, signal.SIGTERM),
            call(9001, signal.SIGKILL),
        ]

    @pytest.mark.asyncio()
    async def test_sigterm_ok_sigkill_permission_error(self) -> None:
        """Should return True when SIGTERM succeeds but SIGKILL raises PermissionError."""
        effects = [None, PermissionError]

        with (
            patch("gateway.process_monitor.os.kill", side_effect=effects) as mock_kill,
            patch("gateway.process_monitor.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await OrphanReaper._kill_process(9001)

        assert result is True
        assert mock_kill.call_args_list == [
            call(9001, signal.SIGTERM),
            call(9001, signal.SIGKILL),
        ]


class TestReapCycle:
    """Tests for OrphanReaper._reap end-to-end."""

    @pytest.mark.asyncio()
    async def test_reap_returns_summary(self) -> None:
        """Should return killed count and freed MB."""
        candidates = [
            _OrphanCandidate(pid=9001, ppid=1, etimes=600, rss_kb=512000, args="loky worker"),
            _OrphanCandidate(pid=9002, ppid=1, etimes=300, rss_kb=256000, args="joblib worker"),
        ]
        reaper = OrphanReaper()

        with (
            patch.object(reaper, "_find_orphans", return_value=candidates),
            patch.object(reaper, "_kill_process", new_callable=AsyncMock, return_value=True),
        ):
            result = await reaper._reap()

        assert result == ReapResult(killed=2, freed_mb=(512000 + 256000) // 1024)

    @pytest.mark.asyncio()
    async def test_reap_partial_failure(self) -> None:
        """Should count only successfully killed processes."""
        candidates = [
            _OrphanCandidate(pid=9001, ppid=1, etimes=600, rss_kb=512000, args="loky worker"),
            _OrphanCandidate(pid=9002, ppid=1, etimes=300, rss_kb=256000, args="joblib worker"),
        ]
        reaper = OrphanReaper()

        with (
            patch.object(reaper, "_find_orphans", return_value=candidates),
            patch.object(
                reaper, "_kill_process", new_callable=AsyncMock, side_effect=[True, False]
            ),
        ):
            result = await reaper._reap()

        assert result == ReapResult(killed=1, freed_mb=512000 // 1024)

    @pytest.mark.asyncio()
    async def test_reap_no_orphans(self) -> None:
        """Should return zeros when nothing found."""
        reaper = OrphanReaper()

        with patch.object(reaper, "_find_orphans", return_value=[]):
            result = await reaper._reap()

        assert result == ReapResult(killed=0, freed_mb=0)


class TestCopilotMonitorReaperIntegration:
    """Tests that CopilotProcessMonitor creates and manages OrphanReaper."""

    def test_monitor_has_reaper(self, monitor: CopilotProcessMonitor) -> None:
        """Monitor should create an OrphanReaper in __init__."""
        assert isinstance(monitor._reaper, OrphanReaper)

    def test_stop_stops_reaper(self, monitor: CopilotProcessMonitor) -> None:
        """stop() should propagate to the reaper."""
        monitor.stop()
        assert monitor._reaper._running is False
