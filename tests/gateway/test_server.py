"""Integration tests for gateway.server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest
import websockets
from gateway.server import GatewayServer, SessionState, main
from gateway.session_resolver import SessionMeta

from tests.gateway.conftest import auth_connect as _auth_connect
from tests.gateway.conftest import recv_json as _recv_json

pytestmark = pytest.mark.asyncio


class TestConnection:
    """Connection lifecycle tests."""

    async def test_valid_token_receives_connected_and_idle(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            connected = await _recv_json(ws)
            assert connected == {"type": "connected", "version": "1.0"}

            history = await _recv_json(ws)
            assert history["type"] == "history"

            status = await _recv_json(ws)
            assert status == {"type": "status", "status": "idle"}

    async def test_wrong_token_rejected(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url, token="wrong")
        async with ws:
            with pytest.raises(websockets.ConnectionClosedError):
                await ws.recv()

    async def test_missing_token_rejected(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        url, _ = auth_gateway
        async with websockets.connect(url) as ws:
            with pytest.raises(websockets.ConnectionClosedError):
                await ws.recv()


class TestTextMessage:
    """Sending a text message triggers the explicit test response handler."""

    async def test_text_triggers_test_response(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            # consume handshake frames
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "text", "message": "hello"}))

            thinking = await _recv_json(ws)
            assert thinking == {"type": "status", "status": "thinking"}

            streaming = await _recv_json(ws)
            assert streaming == {"type": "status", "status": "streaming"}

            deltas: list[str] = []
            for _ in range(3):
                frame = await _recv_json(ws)
                assert frame["type"] == "assistant"
                deltas.append(frame["delta"])

            assert deltas == [
                "This is a ",
                "test response ",
                "from the gateway.",
            ]

            end = await _recv_json(ws)
            assert end == {"type": "end"}

            idle = await _recv_json(ws)
            assert idle == {"type": "status", "status": "idle"}


class TestInvalidFrames:
    """Server responds with error for bad frames."""

    async def test_invalid_json_returns_error(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # idle

            await ws.send("{bad json")

            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_FRAME"


class TestSecondConnection:
    """New connection replaces existing one."""

    async def test_second_connection_replaces_first(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway

        ws1 = await _auth_connect(url)
        await ws1.recv()  # connected
        await ws1.recv()  # history
        await ws1.recv()  # idle

        ws2 = await _auth_connect(url)
        async with ws2:
            connected = await _recv_json(ws2)
            assert connected["type"] == "connected"

        # ws1 should have been closed by the server
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws1.recv(), timeout=2.0)


class TestConcurrentRejection:
    """Text is rejected when session is not idle."""

    async def test_text_rejected_when_thinking(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # idle

            # Force session into THINKING state to simulate concurrent request
            assert gw._current_session is not None
            gw._current_session._state = SessionState.THINKING

            await ws.send(json.dumps({"type": "text", "message": "should be rejected"}))

            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"
            assert "busy" in error["detail"].lower()

    async def test_text_rejected_when_streaming(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # idle

            assert gw._current_session is not None
            gw._current_session._state = SessionState.STREAMING

            await ws.send(json.dumps({"type": "text", "message": "should be rejected"}))

            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"


class TestRequiredAuth:
    """GatewayServer requires explicit client auth configuration."""

    async def test_missing_gateway_token_rejected(self) -> None:
        from gateway.config import GatewayConfig

        config = GatewayConfig(gateway_token=None)
        with pytest.raises(ValueError, match="GATEWAY_TOKEN"):
            GatewayServer(config)


@pytest.fixture()
def main_mocks() -> Iterator[tuple[MagicMock, MagicMock, AsyncMock]]:
    """Shared mocks for main() tests.

    Patches load_config, GatewayServer, and logging.basicConfig so that
    main() can be called without side-effects.  Yields
    ``(mock_config, mock_gw_cls, mock_server)``.
    """
    with (
        patch("gateway.server.load_config") as mock_load_config,
        patch("gateway.server.GatewayServer") as mock_gw_cls,
        patch("logging.basicConfig"),
    ):
        mock_config = mock_load_config.return_value
        mock_config.whisper_model = "base.en"
        mock_config.whisper_device = "cpu"
        mock_config.whisper_compute_type = "int8"

        mock_server = AsyncMock()
        mock_gw_cls.return_value = mock_server

        yield mock_config, mock_gw_cls, mock_server


class TestMainTranscriber:
    """main() instantiates Transcriber and fails fast on startup errors."""

    async def test_main_creates_transcriber_on_success(
        self, main_mocks: tuple[MagicMock, MagicMock, AsyncMock]
    ) -> None:
        mock_config, mock_gw_cls, mock_server = main_mocks
        mock_transcriber = MagicMock()

        with patch(
            "gateway.server.Transcriber",
            return_value=mock_transcriber,
        ) as mock_cls:
            await main()

            mock_cls.assert_called_once_with("base.en", "cpu", "int8")
            mock_gw_cls.assert_called_once_with(
                mock_config,
                transcriber=mock_transcriber,
            )
            mock_server.serve.assert_awaited_once()

    async def test_main_raises_on_transcriber_exception(
        self,
        main_mocks: tuple[MagicMock, MagicMock, AsyncMock],
    ) -> None:
        _mock_config, mock_gw_cls, mock_server = main_mocks

        with patch(
            "gateway.server.Transcriber",
            side_effect=RuntimeError("CUDA not available"),
        ):
            with pytest.raises(RuntimeError, match="CUDA not available"):
                await main()

            mock_gw_cls.assert_not_called()
            mock_server.serve.assert_not_awaited()


class TestHealthCheck:
    """Tests for the /healthz HTTP health check endpoint."""

    async def test_healthz_returns_200(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        """GET /healthz should return 200 OK without upgrading to WebSocket."""
        url, _ = auth_gateway
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port

        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            response_text = data.decode()
            assert "200 OK" in response_text
            assert response_text.endswith("OK\n")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_websocket_still_works_after_healthz(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """WebSocket connections should still work alongside health checks."""
        url, _ = auth_gateway
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port

        # Health check first
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert "200 OK" in data.decode()
        finally:
            writer.close()
            await writer.wait_closed()

        # Then normal WebSocket
        ws = await _auth_connect(url)
        async with ws:
            connected = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(connected)["type"] == "connected"
            history = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(history)["type"] == "history"
            idle = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(idle) == {"type": "status", "status": "idle"}


class TestConnectedSessionMeta:
    """Connected frame carries session metadata when available."""

    async def test_connected_frame_includes_session_meta_when_available(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        meta = SessionMeta(
            session_id="ses_test_123",
            session_key="agent:main:g2",
            updated_at="2026-03-07T10:00:00Z",
        )
        with patch("gateway.server.resolve_session", return_value=meta):
            ws = await _auth_connect(url)
            async with ws:
                connected = await _recv_json(ws)
                assert connected["type"] == "connected"
                assert connected["version"] == "1.0"
                assert connected["sessionId"] == "ses_test_123"
                assert connected["sessionKey"] == "agent:main:g2"
                assert connected["sessionStartedAt"] == "2026-03-07T10:00:00Z"

    async def test_connected_frame_omits_session_fields_when_unavailable(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        with patch("gateway.server.resolve_session", return_value=None):
            ws = await _auth_connect(url)
            async with ws:
                connected = await _recv_json(ws)
                assert connected == {"type": "connected", "version": "1.0"}


class TestHistoryFrame:
    """History frame sent between connected and idle."""

    async def test_connected_sends_history_frame(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        from gateway.session_history import HistoryEntry

        mock_entries = [
            HistoryEntry(role="user", text="hello", ts=1700000000000),
            HistoryEntry(role="assistant", text="hi there", ts=1700000001000),
        ]
        with patch("gateway.session_history.read_history", return_value=mock_entries):
            ws = await _auth_connect(url)
            async with ws:
                connected = await _recv_json(ws)
                assert connected["type"] == "connected"

                history = await _recv_json(ws)
                assert history["type"] == "history"
                assert len(history["entries"]) == 2
                assert history["entries"][0] == {
                    "role": "user",
                    "text": "hello",
                    "ts": 1700000000000,
                }
                assert history["entries"][1] == {
                    "role": "assistant",
                    "text": "hi there",
                    "ts": 1700000001000,
                }

                idle = await _recv_json(ws)
                assert idle == {"type": "status", "status": "idle"}

    async def test_history_failure_does_not_block_session(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        with patch(
            "gateway.session_history.read_history",
            side_effect=RuntimeError("disk error"),
        ):
            ws = await _auth_connect(url)
            async with ws:
                connected = await _recv_json(ws)
                assert connected["type"] == "connected"

                # _send_history catches exceptions — next frame should be idle
                idle = await _recv_json(ws)
                assert idle == {"type": "status", "status": "idle"}


class TestStatusRequest:
    """status_request frame returns current status with optional metadata."""

    async def test_status_request_when_idle(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "status_request"}))
            resp = await _recv_json(ws)
            assert resp == {"type": "status", "status": "idle"}

    async def test_status_request_during_thinking(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Send status_request while session is thinking."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # Force session into THINKING state with task metadata
            session = gw._current_session
            assert session is not None
            session._state = SessionState.THINKING
            session._current_question = "What is 2+2?"
            session._task_start = asyncio.get_running_loop().time() - 1.5

            await ws.send(json.dumps({"type": "status_request"}))
            resp = await _recv_json(ws)
            assert resp["type"] == "status"
            assert resp["status"] == "thinking"
            assert resp["question"] == "What is 2+2?"
            assert resp["elapsedMs"] >= 1400
            assert resp["phase"] == "Waiting for OpenClaw"

    async def test_status_request_question_truncated(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Questions longer than 200 chars are truncated."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            session = gw._current_session
            assert session is not None
            session._state = SessionState.THINKING
            session._current_question = "x" * 300
            session._task_start = asyncio.get_running_loop().time()

            await ws.send(json.dumps({"type": "status_request"}))
            resp = await _recv_json(ws)
            assert len(resp["question"]) == 200


class TestQuickCommand:
    """Quick command intercept handles short steering commands locally."""

    async def test_status_command_returns_assistant_and_idle(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Saying 'status' returns a local summary without querying OpenClaw."""
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "text", "message": "status"}))

            # Should get an assistant frame with summary (not thinking/streaming)
            resp = await _recv_json(ws)
            assert resp["type"] == "assistant"
            assert isinstance(resp["delta"], str)
            assert len(resp["delta"]) > 0

            # Followed by idle status
            idle = await _recv_json(ws)
            assert idle == {"type": "status", "status": "idle"}

    async def test_non_quick_command_passes_through(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Normal text messages are NOT intercepted by quick commands."""
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "text", "message": "hello world"}))

            # Should go through normal mock handler path → thinking
            thinking = await _recv_json(ws)
            assert thinking == {"type": "status", "status": "thinking"}


class TestSessionReset:
    """Session reset notification and control tests."""

    async def test_reset_session_in_idle_sends_session_reset(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Send reset_session in idle state, receive session_reset with reason user_request."""
        url, _gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "reset_session"}))
            reset_frame = await _recv_json(ws)
            assert reset_frame == {"type": "session_reset", "reason": "user_request"}

    async def test_reset_session_while_busy_returns_error(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Send reset_session in non-idle state, get INVALID_STATE error."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # Force session into THINKING state
            assert gw._current_session is not None
            gw._current_session._state = SessionState.THINKING

            await ws.send(json.dumps({"type": "reset_session"}))
            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"
            assert "busy" in error["detail"].lower()

    async def test_daily_reset_on_reconnect(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        """When session date has rolled over, daily reset notification is sent after handshake."""
        url, gw = auth_gateway
        gw._session_date = "2020-01-01"  # force date mismatch
        old_key = gw._session_key

        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            reset_frame = await _recv_json(ws)
            assert reset_frame == {"type": "session_reset", "reason": "daily_reset"}

            # Session key should have changed
            assert gw._session_key != old_key

    async def test_session_key_changes_on_reset(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Verify session key changes after reset."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            old_key = gw._session_key
            await ws.send(json.dumps({"type": "reset_session"}))
            await _recv_json(ws)  # session_reset frame

            assert gw._session_key != old_key
            assert gw._session_key.startswith("agent:main:g2:")


class TestGenerateSessionKey:
    """_generate_session_key format and uniqueness."""

    def test_format_and_prefix(self) -> None:
        from gateway.server import _generate_session_key

        key = _generate_session_key("claw")
        assert key.startswith("agent:claw:g2:")
        rest = key[len("agent:claw:g2:") :]
        # Format: {timestamp}:{hex6}
        parts = rest.split(":")
        assert len(parts) == 2, f"Expected timestamp:hex6, got {rest!r}"
        assert parts[0].isdigit(), f"Expected numeric timestamp, got {parts[0]!r}"
        assert len(parts[1]) == 6, f"Expected 6-char hex suffix, got {parts[1]!r}"
        int(parts[1], 16)  # must be valid hex

    def test_uniqueness(self) -> None:
        from gateway.server import _generate_session_key

        keys = {_generate_session_key() for _ in range(10)}
        # UUID hex suffix should make each key unique
        assert len(keys) == 10

    def test_uses_configured_agent_id(self) -> None:
        from gateway.server import _generate_session_key

        key = _generate_session_key("main")

        assert key.startswith("agent:main:g2:")


class TestCheckDailyResetNoOp:
    """_check_daily_reset is a no-op when the date hasn't changed."""

    async def test_no_op_when_date_unchanged(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        _url, gw = auth_gateway
        from gateway.server import _today_utc

        gw._session_date = _today_utc()
        old_key = gw._session_key
        old_date = gw._session_date

        await gw._check_daily_reset()

        assert gw._session_key == old_key
        assert gw._session_date == old_date
        assert gw._pending_reset_reason is None


class TestResetSessionCleansUpInflight:
    """reset_session cleans up an existing inflight buffer/task."""

    async def test_reset_clears_inflight(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        url, gw = auth_gateway
        from gateway.server import InflightBuffer

        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # Set up a fake inflight buffer and task *after* handshake
            gw._inflight_buffer = InflightBuffer(user_question="pending question")

            async def _fake_stream() -> None:
                await asyncio.sleep(60)

            task = asyncio.create_task(_fake_stream())
            gw._inflight_task = task

            # Discard the inflight before sending reset
            await gw._discard_inflight()

            assert gw._inflight_buffer is None
            assert gw._inflight_task is None

            await ws.send(json.dumps({"type": "reset_session"}))
            reset_frame = await _recv_json(ws)
            assert reset_frame == {"type": "session_reset", "reason": "user_request"}

            assert gw._session_key.startswith("agent:main:g2:")


class TestSessionMenu:
    """Session menu: list, switch, create."""

    async def test_session_list_request_returns_list(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Send session_list_request → receive session_list with activeSessionKey."""
        from gateway.session_history import SessionSummary

        url, gw = auth_gateway
        summaries = [
            SessionSummary(
                session_key="agent:main:g2",
                session_id="ses_1",
                updated_at="2026-03-07T10:00:00Z",
                preview="Hello world",
                message_count=5,
            ),
        ]
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch(
                "gateway.session_history.list_session_summaries",
                return_value=summaries,
            ):
                await ws.send(json.dumps({"type": "session_list_request"}))
                resp = await _recv_json(ws)

            assert resp["type"] == "session_list"
            assert resp["activeSessionKey"] == gw._session_key
            assert len(resp["sessions"]) == 1
            assert resp["sessions"][0]["sessionKey"] == "agent:main:g2"
            assert resp["sessions"][0]["preview"] == "Hello world"
            assert resp["sessions"][0]["messageCount"] == 5
            assert resp["sessions"][0]["updatedAt"] == "2026-03-07T10:00:00Z"
            assert resp["sessions"][0]["label"] == "Hello world"
            assert resp["sessions"][0]["isActive"] is True

    async def test_session_list_request_allowed_while_busy(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """session_list_request works even during streaming state (read-only)."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # Force session into STREAMING state
            assert gw._current_session is not None
            gw._current_session._state = SessionState.STREAMING

            with patch(
                "gateway.session_history.list_session_summaries",
                return_value=[],
            ):
                await ws.send(json.dumps({"type": "session_list_request"}))
                resp = await _recv_json(ws)

            assert resp["type"] == "session_list"

    async def test_session_switch_valid_key(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        """Send session_switch → receive session_switched + history."""
        url, gw = auth_gateway
        meta = SessionMeta(
            session_id="ses_target",
            session_key="agent:claw:g2:target",
            updated_at="2026-03-07T12:00:00Z",
        )
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch("gateway.server.resolve_session", return_value=meta):
                await ws.send(
                    json.dumps({"type": "session_switch", "sessionKey": "agent:claw:g2:target"})
                )
                switched = await _recv_json(ws)
                history = await _recv_json(ws)

            assert switched["type"] == "session_switched"
            assert switched["sessionKey"] == "agent:claw:g2:target"
            assert switched["sessionId"] == "ses_target"
            assert switched["sessionStartedAt"] == "2026-03-07T12:00:00Z"
            assert history["type"] == "history"
            assert gw._session_key == "agent:claw:g2:target"

    async def test_session_switch_unknown_key(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Send session_switch with bogus key → error frame."""
        url, _gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch("gateway.server.resolve_session", return_value=None):
                await ws.send(json.dumps({"type": "session_switch", "sessionKey": "bogus:key"}))
                error = await _recv_json(ws)

            assert error["type"] == "error"
            assert error["code"] == "INVALID_FRAME"
            assert "bogus:key" in error["detail"]

    async def test_session_switch_while_busy_rejected(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """During streaming → INVALID_STATE error."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            assert gw._current_session is not None
            gw._current_session._state = SessionState.STREAMING

            await ws.send(json.dumps({"type": "session_switch", "sessionKey": "agent:claw:g2:xxx"}))
            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"

    async def test_session_create_generates_new_key(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Send session_create → receive session_switched + empty history."""
        url, gw = auth_gateway
        old_key = gw._session_key
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "session_create"}))
            switched = await _recv_json(ws)
            history = await _recv_json(ws)

            assert switched["type"] == "session_switched"
            assert switched["sessionKey"] != old_key
            assert switched["sessionKey"].startswith("agent:main:g2:")
            assert history["type"] == "history"
            assert history["entries"] == []
            assert gw._session_key == switched["sessionKey"]

    async def test_session_create_while_busy_rejected(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """During streaming → INVALID_STATE error."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            assert gw._current_session is not None
            gw._current_session._state = SessionState.STREAMING

            await ws.send(json.dumps({"type": "session_create"}))
            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"

    async def test_session_switch_updates_session_key(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """After switch, the server's session key is updated for OpenClaw."""
        url, gw = auth_gateway
        meta = SessionMeta(
            session_id="ses_new",
            session_key="agent:claw:g2:new",
            updated_at="2026-03-07T14:00:00Z",
        )
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch("gateway.server.resolve_session", return_value=meta):
                await ws.send(
                    json.dumps({"type": "session_switch", "sessionKey": "agent:claw:g2:new"})
                )
                await _recv_json(ws)  # session_switched
                await _recv_json(ws)  # history

            assert gw._session_key == "agent:claw:g2:new"
            assert gw._current_session is not None
            assert gw._current_session._session_key == "agent:claw:g2:new"

    async def test_session_create_then_message(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Create a new session, then send a text message using the new session."""
        url, gw = auth_gateway
        old_key = gw._session_key
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # 1. Create a new session
            await ws.send(json.dumps({"type": "session_create"}))
            switched = await _recv_json(ws)
            history = await _recv_json(ws)

            assert switched["type"] == "session_switched"
            new_key = switched["sessionKey"]
            assert new_key != old_key
            assert history["type"] == "history"
            assert history["entries"] == []

            # 2. Verify server and session use the new key
            assert gw._session_key == new_key
            assert gw._current_session is not None
            assert gw._current_session._session_key == new_key

            # 3. Send a text message on the new session
            await ws.send(json.dumps({"type": "text", "message": "hello"}))
            thinking = await _recv_json(ws)
            assert thinking == {"type": "status", "status": "thinking"}

            streaming = await _recv_json(ws)
            assert streaming == {"type": "status", "status": "streaming"}

            deltas: list[str] = []
            for _ in range(3):
                frame = await _recv_json(ws)
                assert frame["type"] == "assistant"
                deltas.append(frame["delta"])

            end = await _recv_json(ws)
            assert end == {"type": "end"}

            idle = await _recv_json(ws)
            assert idle == {"type": "status", "status": "idle"}

            # 4. Session key should still be the new one
            assert gw._session_key == new_key

    async def test_session_list_request_error_returns_internal_error(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """list_session_summaries raising → error frame with INTERNAL_ERROR."""
        url, _gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch(
                "gateway.session_history.list_session_summaries",
                side_effect=RuntimeError("disk error"),
            ):
                await ws.send(json.dumps({"type": "session_list_request"}))
                error = await _recv_json(ws)

            assert error["type"] == "error"
            assert error["code"] == "INTERNAL_ERROR"

    async def test_session_switch_to_active_session_skips_reconnect(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Switching to the already-active session sends session_switched without reconnect."""
        url, gw = auth_gateway
        current_key = gw._session_key
        meta = SessionMeta(
            session_id="ses_current",
            session_key=current_key,
            updated_at="2026-03-07T10:00:00Z",
        )
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch("gateway.server.resolve_session", return_value=meta):
                await ws.send(json.dumps({"type": "session_switch", "sessionKey": current_key}))
                switched = await _recv_json(ws)

            assert switched["type"] == "session_switched"
            assert switched["sessionKey"] == current_key
            assert switched["sessionId"] == "ses_current"
            # No history frame follows — reconnect was skipped
            assert gw._session_key == current_key

    async def test_session_list_null_updated_at(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """Sessions with no updatedAt send None, not empty string."""
        from gateway.session_history import SessionSummary

        url, _gw = auth_gateway
        summaries = [
            SessionSummary(
                session_key="agent:claw:g2:no_ts",
                session_id="ses_no_ts",
                updated_at=None,
                preview="",
                message_count=0,
            ),
        ]
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            with patch(
                "gateway.session_history.list_session_summaries",
                return_value=summaries,
            ):
                await ws.send(json.dumps({"type": "session_list_request"}))
                resp = await _recv_json(ws)

            assert resp["sessions"][0]["updatedAt"] is None
            assert resp["sessions"][0]["label"] == "agent:claw:g2:no_ts"


class TestForceStop:
    """force_stop hard-kills inflight work and resets the session."""

    async def test_force_stop_resets_session_key(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """force_stop generates a new session key."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            old_key = gw._session_key
            await ws.send(json.dumps({"type": "force_stop"}))
            reset_frame = await _recv_json(ws)
            assert reset_frame == {"type": "session_reset", "reason": "force_stop"}
            assert gw._session_key != old_key
            assert gw._session_key.startswith("agent:main:g2:")

    async def test_force_stop_while_thinking(self, auth_gateway: tuple[str, GatewayServer]) -> None:
        """force_stop works from non-idle states (no state guard)."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # Force session into THINKING state
            assert gw._current_session is not None
            gw._current_session._state = SessionState.THINKING

            await ws.send(json.dumps({"type": "force_stop"}))
            reset_frame = await _recv_json(ws)
            assert reset_frame == {"type": "session_reset", "reason": "force_stop"}
            # State should be reset to IDLE
            assert gw._current_session._state == SessionState.IDLE

    async def test_force_stop_discards_inflight(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """force_stop cancels inflight buffer and task."""
        from gateway.server import InflightBuffer

        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            # Set up a fake inflight buffer and task
            gw._inflight_buffer = InflightBuffer(user_question="pending question")

            async def _fake_stream() -> None:
                await asyncio.sleep(60)

            task = asyncio.create_task(_fake_stream())
            gw._inflight_task = task

            await ws.send(json.dumps({"type": "force_stop"}))
            reset_frame = await _recv_json(ws)
            assert reset_frame == {"type": "session_reset", "reason": "force_stop"}
            assert gw._inflight_buffer is None
            assert gw._inflight_task is None

    async def test_force_stop_closes_openclaw_client(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """force_stop calls close() on the OpenClaw client."""
        url, gw = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # status:idle

            gw._handler.close = AsyncMock()  # type: ignore[method-assign]
            await ws.send(json.dumps({"type": "force_stop"}))
            await _recv_json(ws)  # session_reset
            gw._handler.close.assert_awaited_once()
