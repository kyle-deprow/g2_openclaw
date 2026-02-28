"""Tests for OpenClaw WebSocket client."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import pytest
import websockets
from gateway.device_identity import DeviceIdentity, _generate_identity
from gateway.openclaw_client import OpenClawClient, OpenClawError
from websockets import ServerConnection
from websockets.asyncio.server import Server

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_identity() -> DeviceIdentity:
    """Generate a throwaway device identity for testing."""
    return _generate_identity()


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
    send_challenge: bool = True,
) -> None:
    """Simple handler that mimics OpenClaw protocol.

    When *send_challenge* is ``True`` (default), the handler sends the
    ``connect.challenge`` event immediately after the WebSocket opens,
    matching real OpenClaw server behaviour.
    """
    deltas = deltas or ["Hello ", "from ", "OpenClaw."]

    # Phase 1 — send challenge nonce
    if send_challenge:
        nonce = secrets.token_urlsafe(16)
        await ws.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": nonce},
                }
            )
        )

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
                                "data": {"phase": "error", "error": "model crashed"},
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
                            "payload": {"stream": "assistant", "data": {"delta": deltas[0]}},
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
                            "payload": {"stream": "assistant", "data": {"delta": d}},
                        }
                    )
                )
                await asyncio.sleep(0.01)

            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {"stream": "lifecycle", "data": {"phase": "end"}},
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


def _make_client(port: int, token: str = "test-token") -> OpenClawClient:
    """Create a client with a test device identity (no disk I/O)."""
    return OpenClawClient("127.0.0.1", port, token, device_identity=_make_test_identity())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_connect_auth_and_stream_deltas(self) -> None:
        server, port = await _start_mock_server(deltas=["Hello ", "world!"])
        try:
            client = _make_client(port)
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
            client = _make_client(port)

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
        """Request IDs reset per connection (each send_message reconnects)."""
        server, port = await _start_mock_server(deltas=["x"])
        try:
            client = _make_client(port)
            # After ensure_connected, auth used id=1
            await client.ensure_connected()
            assert client._next_id == 2  # 1 consumed by auth

            stream = await client.send_message("msg1")
            _ = [d async for d in stream]
            assert client._next_id == 3  # 2 consumed by agent (ws closed)

            # Second message triggers reconnect → IDs reset
            stream2 = await client.send_message("msg2")
            _ = [d async for d in stream2]
            assert client._next_id == 3  # auth=1, agent=2 on fresh conn

            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_connect_sends_device_block_and_scopes(self) -> None:
        """The connect request includes device identity, role, and scopes."""
        captured: dict[str, Any] = {}

        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    captured.update(msg["params"])
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            await client.ensure_connected()

            # Verify device block present
            assert "device" in captured
            device = captured["device"]
            assert "id" in device
            assert "publicKey" in device
            assert "signature" in device
            assert "signedAt" in device
            assert "nonce" in device

            # Verify scopes and role
            assert captured["scopes"] == ["operator.admin"]
            assert captured["role"] == "operator"

            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAuthErrors:
    async def test_auth_rejected(self) -> None:
        server, port = await _start_mock_server(auth_ok=False)
        try:
            client = _make_client(port, "bad-token")
            with pytest.raises(OpenClawError, match="auth rejected"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestBufferedEvents:
    """Events arriving before the res frame must not be lost."""

    async def test_deltas_before_res_are_buffered(self) -> None:
        """If the server sends agent delta events BEFORE the res ack,
        the client should still yield them."""

        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    # Send deltas BEFORE the res frame
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "assistant", "data": {"delta": "early1 "}},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "assistant", "data": {"delta": "early2 "}},
                            }
                        )
                    )
                    # Now send the res ack
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"],
                                "ok": True,
                                "payload": {"runId": "mock-run-1"},
                            }
                        )
                    )
                    # Then more deltas after res
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "assistant", "data": {"delta": "late "}},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "lifecycle", "data": {"phase": "end"}},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            collected = [d async for d in stream]
            assert collected == ["early1 ", "early2 ", "late "]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_lifecycle_end_in_buffer(self) -> None:
        """If lifecycle end arrives before the res frame, stream ends cleanly."""

        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    # Send delta + lifecycle end BEFORE res
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "assistant", "data": {"delta": "fast!"}},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": "agent",
                                "payload": {"stream": "lifecycle", "data": {"phase": "end"}},
                            }
                        )
                    )
                    # Res comes after (client already got everything)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"],
                                "ok": True,
                                "payload": {"runId": "mock-run-2"},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            collected = [d async for d in stream]
            assert collected == ["fast!"]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAgentErrors:
    async def test_agent_error_event(self) -> None:
        server, port = await _start_mock_server(error_on_agent=True)
        try:
            client = _make_client(port)
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
        client = _make_client(ephemeral_port)
        with pytest.raises(OpenClawError, match="connection refused"):
            await client.send_message("Hi")

    async def test_disconnect_mid_stream(self) -> None:
        server, port = await _start_mock_server(disconnect_mid_stream=True)
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            with pytest.raises(OpenClawError, match="disconnected"):
                collected = []
                async for d in stream:
                    collected.append(d)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_missing_challenge_nonce(self) -> None:
        """Connection fails if server sends no challenge event."""
        server, port = await _start_mock_server(send_challenge=False)
        try:
            client = _make_client(port)
            with pytest.raises(OpenClawError, match="challenge"):
                await client.send_message("Hi")
        finally:
            server.close()
            await server.wait_closed()


class TestClose:
    async def test_graceful_close(self) -> None:
        server, port = await _start_mock_server()
        try:
            client = _make_client(port)
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
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
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
            client = _make_client(port)
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
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
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
                                "id": msg["id"] + "_wrong",
                                "ok": True,
                                "payload": {},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            with pytest.raises(OpenClawError, match="unexpected agent response"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
