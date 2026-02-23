"""Tests for OpenClaw WebSocket client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets
from gateway.openclaw_client import OpenClawClient, OpenClawError
from websockets import ServerConnection
from websockets.asyncio.server import Server

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Mock OpenClaw server helpers
# ---------------------------------------------------------------------------


async def _mock_openclaw_handler(
    ws: ServerConnection,
    *,
    auth_ok: bool = True,
    deltas: list[str] | None = None,
    error_on_agent: bool = False,
    disconnect_mid_stream: bool = False,
) -> None:
    """Simple handler that mimics OpenClaw protocol."""
    deltas = deltas or ["Hello ", "from ", "OpenClaw."]

    async for raw in ws:
        msg = json.loads(raw)
        if msg["method"] == "connect":
            if auth_ok:
                await ws.send(
                    json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                )
            else:
                await ws.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": msg["id"],
                            "ok": False,
                            "error": "bad token",
                        }
                    )
                )
                return
        elif msg["method"] == "agent":
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

            if error_on_agent:
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {
                                "stream": "lifecycle",
                                "phase": "error",
                                "error": "model crashed",
                            },
                        }
                    )
                )
                return

            if disconnect_mid_stream:
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {"stream": "assistant", "delta": deltas[0]},
                        }
                    )
                )
                await ws.close()
                return

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


async def _start_mock_server(**kwargs: Any) -> tuple[Server, int]:
    """Start a mock OpenClaw server on an ephemeral port. Returns (server, port)."""

    async def handler(ws: ServerConnection) -> None:
        await _mock_openclaw_handler(ws, **kwargs)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_connect_auth_and_stream_deltas(self) -> None:
        server, port = await _start_mock_server(deltas=["Hello ", "world!"])
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            stream = await client.send_message("Hi")
            collected = [d async for d in stream]
            assert collected == ["Hello ", "world!"]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_multiple_sequential_messages(self) -> None:
        """Send two agent requests on the same connection; both get deltas."""
        server, port = await _start_mock_server(deltas=["A", "B"])
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")

            stream1 = await client.send_message("first")
            collected1 = [d async for d in stream1]
            assert collected1 == ["A", "B"]

            stream2 = await client.send_message("second")
            collected2 = [d async for d in stream2]
            assert collected2 == ["A", "B"]

            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_request_ids_increment(self) -> None:
        """Request IDs should monotonically increase across calls."""
        server, port = await _start_mock_server(deltas=["x"])
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            # After ensure_connected, auth used id=1
            await client.ensure_connected()
            assert client._next_id == 2  # 1 consumed by auth

            stream = await client.send_message("msg1")
            _ = [d async for d in stream]
            assert client._next_id == 3  # 2 consumed by agent

            stream2 = await client.send_message("msg2")
            _ = [d async for d in stream2]
            assert client._next_id == 4

            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAuthErrors:
    async def test_auth_rejected(self) -> None:
        server, port = await _start_mock_server(auth_ok=False)
        try:
            client = OpenClawClient("127.0.0.1", port, "bad-token")
            with pytest.raises(OpenClawError, match="auth rejected"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAgentErrors:
    async def test_agent_error_event(self) -> None:
        server, port = await _start_mock_server(error_on_agent=True)
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            stream = await client.send_message("Hi")
            with pytest.raises(OpenClawError, match="agent error"):
                async for _ in stream:
                    pass
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestConnectionErrors:
    async def test_connection_refused(self) -> None:
        # Use ephemeral port to avoid conflicts
        import socket as _socket

        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
            _s.bind(("127.0.0.1", 0))
            ephemeral_port = _s.getsockname()[1]
        client = OpenClawClient("127.0.0.1", ephemeral_port, "test-token")
        with pytest.raises(OpenClawError, match="connection refused"):
            await client.send_message("Hi")

    async def test_disconnect_mid_stream(self) -> None:
        server, port = await _start_mock_server(disconnect_mid_stream=True)
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            stream = await client.send_message("Hi")
            with pytest.raises(OpenClawError, match="disconnected"):
                collected = []
                async for d in stream:
                    collected.append(d)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestClose:
    async def test_graceful_close(self) -> None:
        server, port = await _start_mock_server()
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            await client.ensure_connected()
            assert client._connected
            await client.close()
            assert not client._connected
            assert client._ws is None
        finally:
            server.close()
            await server.wait_closed()


class TestMalformedAgentAcceptance:
    """M-9: Malformed (non-JSON) agent acceptance response."""

    async def test_non_json_agent_response(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    await ws.send("<<<not json>>>")

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            with pytest.raises(OpenClawError, match="no response to agent request"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAgentAcceptanceIdMismatch:
    """M-10: Agent acceptance response has wrong request ID."""

    async def test_wrong_id_in_agent_response(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"] + 999,
                                "ok": True,
                                "payload": {},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = OpenClawClient("127.0.0.1", port, "test-token")
            with pytest.raises(OpenClawError, match="unexpected agent response"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
