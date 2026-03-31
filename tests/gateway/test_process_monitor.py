"""Tests for gateway.process_monitor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gateway.process_monitor import CopilotProcessMonitor, DeathReport


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
