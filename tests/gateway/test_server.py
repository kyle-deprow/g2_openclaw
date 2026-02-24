"""Integration tests for gateway.server."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets
from gateway.server import GatewayServer, SessionState, main

pytestmark = pytest.mark.asyncio


async def _recv_json(ws: websockets.ClientConnection) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(await ws.recv())
    return result


async def _auth_connect(url: str, token: str = "test-token") -> websockets.ClientConnection:
    """Connect and send first-message auth handshake."""
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    return ws


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

            status = await _recv_json(ws)
            assert status == {"type": "status", "status": "idle"}

    async def test_noauth_text_triggers_mock_response(
        self, noauth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = noauth_gateway
        async with websockets.connect(url) as ws:
            await ws.recv()  # connected
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
