"""Tests for OpenClawResponseHandler and OpenClaw integration wiring in server.py."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from gateway.config import GatewayConfig
from gateway.openclaw_client import OpenClawClient, OpenClawError
from gateway.server import (
    GatewayServer,
    GatewaySession,
    MockResponseHandler,
    OpenClawResponseHandler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _recv_json(ws: websockets.ClientConnection) -> dict:
    return json.loads(await ws.recv())


class _FakeStream:
    """Async iterator that yields predefined deltas."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        for d in self._deltas:
            yield d


class _SlowStream:
    """Async iterator that sleeps forever after first delta — triggers timeout."""

    def __init__(self) -> None:
        pass

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        yield "start…"
        await asyncio.sleep(300)  # will be cancelled by timeout


class _ErrorStream:
    """Async iterator that raises OpenClawError mid-stream."""

    def __init__(self, deltas_before_error: list[str], error_msg: str) -> None:
        self._deltas = deltas_before_error
        self._error_msg = error_msg

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        for d in self._deltas:
            yield d
        raise OpenClawError(self._error_msg)


# ---------------------------------------------------------------------------
# Unit tests: OpenClawResponseHandler
# ---------------------------------------------------------------------------


class TestOpenClawResponseHandler:
    """Test OpenClawResponseHandler in isolation with mocked OpenClawClient."""

    async def test_full_flow_streams_deltas_and_end(self) -> None:
        """Text message → streaming status → 5 deltas → end."""
        deltas = ["one ", "two ", "three ", "four ", "five"]
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.return_value = _FakeStream(deltas)

        handler = OpenClawResponseHandler(client)
        frames: list[dict] = []

        async def capture(frame: dict) -> None:
            frames.append(frame)

        await handler.handle("hello", capture)

        assert frames[0] == {"type": "status", "status": "streaming"}
        for i, d in enumerate(deltas):
            assert frames[1 + i] == {"type": "assistant", "delta": d}
        assert frames[-1] == {"type": "end"}
        assert len(frames) == 1 + len(deltas) + 1  # streaming + deltas + end

    async def test_openclaw_error_during_streaming_propagates(self) -> None:
        """OpenClawError raised in stream propagates to caller."""
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.return_value = _ErrorStream(["ok "], "agent error: model crashed")

        handler = OpenClawResponseHandler(client)
        frames: list[dict] = []

        async def capture(frame: dict) -> None:
            frames.append(frame)

        with pytest.raises(OpenClawError, match="model crashed"):
            await handler.handle("hello", capture)

        # Should have sent streaming + at least 1 delta before error
        assert frames[0] == {"type": "status", "status": "streaming"}
        assert frames[1] == {"type": "assistant", "delta": "ok "}

    async def test_connection_error_propagates(self) -> None:
        """OpenClawError from send_message propagates."""
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.side_effect = OpenClawError("connection refused: boom")

        handler = OpenClawResponseHandler(client)

        with pytest.raises(OpenClawError, match="connection refused"):
            await handler.handle("hello", AsyncMock())


# ---------------------------------------------------------------------------
# Integration tests: _handle_text with OpenClawResponseHandler
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal fake WebSocket for unit-testing GatewaySession."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        await asyncio.sleep(100)
        return ""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed = True

    @property
    def request(self):
        return None

    def frames(self) -> list[dict]:
        return [json.loads(s) for s in self.sent]

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
class TestHandleTextWithOpenClaw:
    """Test _handle_text path with OpenClawResponseHandler wired in."""

    async def test_text_message_full_flow(self) -> None:
        """text → thinking → streaming → deltas → end → idle."""
        deltas = ["This ", "is ", "a ", "test ", "answer"]
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.return_value = _FakeStream(deltas)

        handler = OpenClawResponseHandler(client)
        fake_ws = _FakeWebSocket()
        session = GatewaySession(fake_ws, handler=handler)  # type: ignore[arg-type]

        await session._handle_text({"type": "text", "message": "hello"})

        frames = fake_ws.frames()
        types = [f["type"] for f in frames]

        assert types[0:2] == ["status", "status"]
        assert frames[0]["status"] == "thinking"
        assert frames[1]["status"] == "streaming"

        delta_frames = [f for f in frames if f["type"] == "assistant"]
        assert len(delta_frames) == 5
        assert [d["delta"] for d in delta_frames] == deltas

        assert {"type": "end"} in frames
        assert frames[-1] == {"type": "status", "status": "idle"}

    async def test_openclaw_error_sends_error_frame(self) -> None:
        """OpenClawError mid-stream → OPENCLAW_ERROR frame → idle."""
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.return_value = _ErrorStream([], "agent error: model crashed")

        handler = OpenClawResponseHandler(client)
        fake_ws = _FakeWebSocket()
        session = GatewaySession(fake_ws, handler=handler)  # type: ignore[arg-type]

        await session._handle_text({"type": "text", "message": "hello"})

        frames = fake_ws.frames()
        error_frames = [f for f in frames if f["type"] == "error"]
        assert len(error_frames) == 1
        assert error_frames[0]["code"] == "OPENCLAW_ERROR"
        assert error_frames[0]["detail"] == "Agent communication error"
        assert frames[-1] == {"type": "status", "status": "idle"}

    async def test_agent_timeout_sends_timeout_frame(self) -> None:
        """Handler exceeding timeout → TIMEOUT error frame → idle."""
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.return_value = _SlowStream()

        handler = OpenClawResponseHandler(client)
        fake_ws = _FakeWebSocket()
        session = GatewaySession(fake_ws, handler=handler, timeout=1)

        await session._handle_text({"type": "text", "message": "hello"})

        frames = fake_ws.frames()
        error_frames = [f for f in frames if f["type"] == "error"]
        assert len(error_frames) == 1
        assert error_frames[0]["code"] == "TIMEOUT"
        assert "1s timeout" in error_frames[0]["detail"]
        assert frames[-1] == {"type": "status", "status": "idle"}

    async def test_connection_refused_sends_openclaw_error(self) -> None:
        """OpenClaw not running → OPENCLAW_ERROR frame → idle."""
        client = AsyncMock(spec=OpenClawClient)
        client.send_message.side_effect = OpenClawError("connection refused: [Errno 111]")

        handler = OpenClawResponseHandler(client)
        fake_ws = _FakeWebSocket()
        session = GatewaySession(fake_ws, handler=handler)  # type: ignore[arg-type]

        await session._handle_text({"type": "text", "message": "hello"})

        frames = fake_ws.frames()
        error_frames = [f for f in frames if f["type"] == "error"]
        assert len(error_frames) == 1
        assert error_frames[0]["code"] == "OPENCLAW_ERROR"
        assert error_frames[0]["detail"] == "Agent communication error"
        assert frames[-1] == {"type": "status", "status": "idle"}


# ---------------------------------------------------------------------------
# GatewayServer auto-handler selection
# ---------------------------------------------------------------------------


class TestAutoHandlerSelection:
    """GatewayServer picks the correct handler based on config."""

    def test_no_token_defaults_to_mock(self) -> None:
        """Without openclaw_gateway_token, MockResponseHandler is used."""
        config = GatewayConfig(openclaw_gateway_token=None)
        server = GatewayServer(config)
        assert isinstance(server._handler, MockResponseHandler)

    def test_explicit_handler_overrides_config(self) -> None:
        """Explicitly passed handler wins regardless of token."""
        config = GatewayConfig(openclaw_gateway_token="some-token")
        mock = MockResponseHandler()
        server = GatewayServer(config, handler=mock)
        assert server._handler is mock

    def test_token_creates_openclaw_handler(self) -> None:
        """With openclaw_gateway_token set, OpenClawResponseHandler is created."""
        config = GatewayConfig(
            openclaw_gateway_token="test-token",
            openclaw_host="10.0.0.1",
            openclaw_port=9999,
            agent_timeout=60,
        )
        server = GatewayServer(config)
        assert isinstance(server._handler, OpenClawResponseHandler)
        assert server._handler._client._host == "10.0.0.1"
        assert server._handler._client._port == 9999


# ---------------------------------------------------------------------------
# Full WebSocket integration: Gateway + mock OpenClaw server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFullWebSocketIntegration:
    """End-to-end tests using a real WebSocket gateway backed by mock OpenClaw."""

    async def test_text_via_openclaw_mock_server(self) -> None:
        """Full round-trip: gateway → mock OpenClaw → streamed response to client."""

        # Inline minimal mock OpenClaw handler (same protocol as tests/mocks)
        async def mock_oc_handler(ws: websockets.ServerConnection) -> None:
            deltas = ["Hello ", "from ", "mock ", "Open", "Claw."]
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("method") == "connect":
                    await ws.send(
                        json.dumps(
                            {"type": "res", "id": msg["id"], "ok": True, "payload": {}}
                        )
                    )
                elif msg.get("method") == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"],
                                "ok": True,
                                "payload": {
                                    "runId": "mock-run-1",
                                    "acceptedAt": "2026-01-01T00:00:00Z",
                                },
                            }
                        )
                    )
                    for d in deltas:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "event",
                                    "event": "agent",
                                    "payload": {"stream": "assistant", "delta": d},
                                }
                            )
                        )
                        await asyncio.sleep(0.01)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "lifecycle", "phase": "end"},
                            }
                        )
                    )

        # Start mock OpenClaw server
        oc_server = await websockets.serve(mock_oc_handler, "127.0.0.1", 0)
        oc_port = oc_server.sockets[0].getsockname()[1]

        try:
            # Build gateway wired to the mock OpenClaw
            client = OpenClawClient(
                host="127.0.0.1",
                port=oc_port,
                token="test-token",
            )
            oc_handler = OpenClawResponseHandler(client)
            config = GatewayConfig(
                gateway_host="127.0.0.1",
                gateway_port=0,
                gateway_token=None,
            )
            gw = GatewayServer(config, handler=oc_handler)
            gw_server = await websockets.serve(gw.handler, "127.0.0.1", 0)
            gw_port = gw_server.sockets[0].getsockname()[1]

            try:
                async with websockets.connect(f"ws://127.0.0.1:{gw_port}") as ws:
                    # Handshake
                    connected = await _recv_json(ws)
                    assert connected["type"] == "connected"
                    idle = await _recv_json(ws)
                    assert idle == {"type": "status", "status": "idle"}

                    # Send text
                    await ws.send(json.dumps({"type": "text", "message": "hello"}))

                    thinking = await _recv_json(ws)
                    assert thinking == {"type": "status", "status": "thinking"}

                    streaming = await _recv_json(ws)
                    assert streaming == {"type": "status", "status": "streaming"}

                    # Collect assistant deltas until end frame
                    deltas: list[str] = []
                    while True:
                        frame = await _recv_json(ws)
                        if frame["type"] == "assistant":
                            deltas.append(frame["delta"])
                        elif frame["type"] == "end":
                            break

                    # Mock server returns 5 deltas
                    assert len(deltas) >= 1
                    full_text = "".join(deltas)
                    assert "openclaw" in full_text.lower()

                    final_idle = await _recv_json(ws)
                    assert final_idle == {"type": "status", "status": "idle"}
            finally:
                gw_server.close()
                await gw_server.wait_closed()
                await client.close()
        finally:
            oc_server.close()
            await oc_server.wait_closed()
