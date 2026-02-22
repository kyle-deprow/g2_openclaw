"""Integration tests for gateway.server."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from gateway.server import SessionState

pytestmark = pytest.mark.asyncio


async def _recv_json(ws: websockets.ClientConnection) -> dict:
    return json.loads(await ws.recv())


class TestConnection:
    """Connection lifecycle tests."""

    async def test_valid_token_receives_connected_and_idle(self, auth_gateway: tuple) -> None:
        url, _ = auth_gateway
        async with websockets.connect(f"{url}?token=test-token") as ws:
            connected = await _recv_json(ws)
            assert connected == {"type": "connected", "version": "1.0"}

            status = await _recv_json(ws)
            assert status == {"type": "status", "status": "idle"}

    async def test_wrong_token_rejected(self, auth_gateway: tuple) -> None:
        url, _ = auth_gateway
        async with websockets.connect(f"{url}?token=wrong") as ws:
            with pytest.raises(websockets.ConnectionClosedError):
                await ws.recv()

    async def test_missing_token_rejected(self, auth_gateway: tuple) -> None:
        url, _ = auth_gateway
        async with websockets.connect(url) as ws:
            with pytest.raises(websockets.ConnectionClosedError):
                await ws.recv()


class TestTextMessage:
    """Sending a text message triggers mock response flow."""

    async def test_text_triggers_mock_response(self, auth_gateway: tuple) -> None:
        url, _ = auth_gateway
        async with websockets.connect(f"{url}?token=test-token") as ws:
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

    async def test_invalid_json_returns_error(self, auth_gateway: tuple) -> None:
        url, _ = auth_gateway
        async with websockets.connect(f"{url}?token=test-token") as ws:
            await ws.recv()  # connected
            await ws.recv()  # idle

            await ws.send("{bad json")

            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_FRAME"


class TestSecondConnection:
    """New connection replaces existing one."""

    async def test_second_connection_replaces_first(self, auth_gateway: tuple) -> None:
        url, _ = auth_gateway

        ws1 = await websockets.connect(f"{url}?token=test-token")
        await ws1.recv()  # connected
        await ws1.recv()  # idle

        async with websockets.connect(f"{url}?token=test-token") as ws2:
            connected = await _recv_json(ws2)
            assert connected["type"] == "connected"

        # ws1 should have been closed by the server
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws1.recv(), timeout=2.0)


class TestConcurrentRejection:
    """Text is rejected when session is not idle."""

    async def test_text_rejected_when_thinking(self, auth_gateway: tuple) -> None:
        url, gw = auth_gateway
        async with websockets.connect(f"{url}?token=test-token") as ws:
            await ws.recv()  # connected
            await ws.recv()  # idle

            # Force session into THINKING state to simulate concurrent request
            gw._current_session._state = SessionState.THINKING

            await ws.send(json.dumps({"type": "text", "message": "should be rejected"}))

            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"
            assert "busy" in error["detail"].lower()

    async def test_text_rejected_when_streaming(self, auth_gateway: tuple) -> None:
        url, gw = auth_gateway
        async with websockets.connect(f"{url}?token=test-token") as ws:
            await ws.recv()  # connected
            await ws.recv()  # idle

            gw._current_session._state = SessionState.STREAMING

            await ws.send(json.dumps({"type": "text", "message": "should be rejected"}))

            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"


class TestNoAuth:
    """Connection without token succeeds when gateway_token is None."""

    async def test_noauth_connection_receives_connected_and_idle(
        self, noauth_gateway: tuple
    ) -> None:
        url, _ = noauth_gateway
        async with websockets.connect(url) as ws:
            connected = await _recv_json(ws)
            assert connected == {"type": "connected", "version": "1.0"}

            status = await _recv_json(ws)
            assert status == {"type": "status", "status": "idle"}

    async def test_noauth_text_triggers_mock_response(
        self, noauth_gateway: tuple
    ) -> None:
        url, _ = noauth_gateway
        async with websockets.connect(url) as ws:
            await ws.recv()  # connected
            await ws.recv()  # idle

            await ws.send(json.dumps({"type": "text", "message": "hello"}))

            thinking = await _recv_json(ws)
            assert thinking == {"type": "status", "status": "thinking"}
