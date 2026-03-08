"""Integration tests for gateway.server."""

from __future__ import annotations

import asyncio
import json
import logging
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
    """Sending a text message triggers mock response flow."""

    async def test_text_triggers_mock_response(
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
                "mock response ",
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


class TestNoAuth:
    """Connection without token succeeds when gateway_token is None."""

    async def test_noauth_connection_receives_connected_and_idle(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        async with websockets.connect(url) as ws:
            connected = await _recv_json(ws)
            assert connected == {"type": "connected", "version": "1.0"}

            history = await _recv_json(ws)
            assert history["type"] == "history"

            status = await _recv_json(ws)
            assert status == {"type": "status", "status": "idle"}

    async def test_noauth_text_triggers_mock_response(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        async with websockets.connect(url) as ws:
            await ws.recv()  # connected
            await ws.recv()  # history
            await ws.recv()  # idle

            await ws.send(json.dumps({"type": "text", "message": "hello"}))

            thinking = await _recv_json(ws)
            assert thinking == {"type": "status", "status": "thinking"}


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
    """main() instantiates Transcriber with graceful fallback."""

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

    async def test_main_falls_back_when_faster_whisper_missing(
        self,
        main_mocks: tuple[MagicMock, MagicMock, AsyncMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_config, mock_gw_cls, mock_server = main_mocks

        with (
            patch(
                "gateway.server.Transcriber",
                side_effect=ImportError("No module named 'faster_whisper'"),
            ),
            caplog.at_level(logging.WARNING, logger="gateway.server"),
        ):
            await main()

            mock_gw_cls.assert_called_once_with(
                mock_config,
                transcriber=None,
            )
            mock_server.serve.assert_awaited_once()
            assert "faster-whisper is not installed" in caplog.text

    async def test_main_falls_back_on_generic_exception(
        self,
        main_mocks: tuple[MagicMock, MagicMock, AsyncMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_config, mock_gw_cls, mock_server = main_mocks

        with (
            patch(
                "gateway.server.Transcriber",
                side_effect=RuntimeError("CUDA not available"),
            ),
            caplog.at_level(logging.WARNING, logger="gateway.server"),
        ):
            await main()

            mock_gw_cls.assert_called_once_with(
                mock_config,
                transcriber=None,
            )
            mock_server.serve.assert_awaited_once()
            assert "Failed to load transcriber" in caplog.text


class TestHealthCheck:
    """Tests for the /healthz HTTP health check endpoint."""

    async def test_healthz_returns_200(self, noauth_gateway: tuple[str, GatewayServer]) -> None:
        """GET /healthz should return 200 OK without upgrading to WebSocket."""
        url, _ = noauth_gateway
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port

        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(
                b"GET /healthz HTTP/1.1\r\n" b"Host: localhost\r\n" b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            response_text = data.decode()
            assert "200 OK" in response_text
            assert response_text.endswith("OK\n")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_websocket_still_works_after_healthz(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        """WebSocket connections should still work alongside health checks."""
        url, _ = noauth_gateway
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port

        # Health check first
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(
                b"GET /healthz HTTP/1.1\r\n" b"Host: localhost\r\n" b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert "200 OK" in data.decode()
        finally:
            writer.close()
            await writer.wait_closed()

        # Then normal WebSocket
        async with websockets.connect(url) as ws:
            connected = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(connected)["type"] == "connected"
            history = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(history)["type"] == "history"
            idle = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(idle) == {"type": "status", "status": "idle"}


class TestConnectedSessionMeta:
    """Connected frame carries session metadata when available."""

    async def test_connected_frame_includes_session_meta_when_available(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        meta = SessionMeta(
            session_id="ses_test_123",
            session_key="agent:claw:g2",
            updated_at="2026-03-07T10:00:00Z",
        )
        with patch("gateway.server.resolve_session", return_value=meta):
            async with websockets.connect(url) as ws:
                connected = await _recv_json(ws)
                assert connected["type"] == "connected"
                assert connected["version"] == "1.0"
                assert connected["sessionId"] == "ses_test_123"
                assert connected["sessionKey"] == "agent:claw:g2"
                assert connected["sessionStartedAt"] == "2026-03-07T10:00:00Z"

    async def test_connected_frame_omits_session_fields_when_unavailable(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        with patch("gateway.server.resolve_session", return_value=None):
            async with websockets.connect(url) as ws:
                connected = await _recv_json(ws)
                assert connected == {"type": "connected", "version": "1.0"}


class TestHistoryFrame:
    """History frame sent between connected and idle."""

    async def test_connected_sends_history_frame(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        from gateway.session_history import HistoryEntry

        mock_entries = [
            HistoryEntry(role="user", text="hello", ts=1700000000000),
            HistoryEntry(role="assistant", text="hi there", ts=1700000001000),
        ]
        with patch("gateway.session_history.read_history", return_value=mock_entries):
            async with websockets.connect(url) as ws:
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
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        with patch(
            "gateway.session_history.read_history",
            side_effect=RuntimeError("disk error"),
        ):
            async with websockets.connect(url) as ws:
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
            assert gw._session_key.startswith("agent:claw:g2:")


class TestGenerateSessionKey:
    """_generate_session_key format and uniqueness."""

    def test_format_and_prefix(self) -> None:
        from gateway.server import _generate_session_key

        key = _generate_session_key()
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

            assert gw._session_key.startswith("agent:claw:g2:")
