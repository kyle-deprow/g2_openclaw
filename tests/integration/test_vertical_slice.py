"""End-to-end integration tests for the G2 OpenClaw Gateway vertical slice.

Proves: WebSocket connect → text message → streamed response → proper frame sequence.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIMEOUT = 5.0


async def _recv(ws: websockets.ClientConnection) -> dict[str, Any]:
    """Receive a single JSON frame with a timeout guard."""
    raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
    result: dict[str, Any] = json.loads(raw)
    return result


async def _consume_handshake(
    ws: websockets.ClientConnection,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Receive and return the (connected, history, status:idle) handshake frames."""
    connected = await _recv(ws)
    history = await _recv(ws)
    idle = await _recv(ws)
    return connected, history, idle


async def _auth_connect(url: str, token: str = "integration-token") -> websockets.ClientConnection:
    """Connect to the gateway and send first-message auth."""
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    return ws


async def _send_text(ws: websockets.ClientConnection, message: str) -> None:
    """Send a text frame."""
    await ws.send(json.dumps({"type": "text", "message": message}))


async def _collect_response(ws: websockets.ClientConnection) -> list[dict[str, Any]]:
    """Collect all frames from thinking through the final status:idle."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = await _recv(ws)
        frames.append(frame)
        if frame.get("type") == "status" and frame.get("status") == "idle":
            break
    return frames


# ---------------------------------------------------------------------------
# Full happy path WITH auth
# ---------------------------------------------------------------------------


class TestHappyPathAuth:
    """Full vertical slice with token authentication."""

    async def test_full_sequence(self, auth_gateway: tuple[str, object]) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            # Handshake
            connected, history, idle = await _consume_handshake(ws)
            assert connected == {"type": "connected", "version": "1.0"}
            assert history["type"] == "history"
            assert idle == {"type": "status", "status": "idle"}

            # Send text
            await _send_text(ws, "hello world")

            # Collect response
            frames = await _collect_response(ws)

            assert frames[0] == {"type": "status", "status": "thinking"}
            assert frames[1] == {"type": "status", "status": "streaming"}

            deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
            assert deltas == [
                "This is a ",
                "test response ",
                "from the gateway.",
            ]

            assert frames[-2] == {"type": "end"}
            assert frames[-1] == {"type": "status", "status": "idle"}

            # Connection should stay open — no more frames arrive
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)


# ---------------------------------------------------------------------------
# Multiple sequential requests on the same connection
# ---------------------------------------------------------------------------


class TestSequentialRequests:
    """Send multiple requests on one connection, verifying idle between them."""

    async def test_two_sequential_requests(self, auth_gateway: tuple[str, object]) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await _consume_handshake(ws)

            # --- First request ---
            await _send_text(ws, "first request")
            frames_1 = await _collect_response(ws)
            assert frames_1[0] == {"type": "status", "status": "thinking"}
            deltas_1 = [f["delta"] for f in frames_1 if f["type"] == "assistant"]
            assert len(deltas_1) == 3
            assert frames_1[-1] == {"type": "status", "status": "idle"}

            # --- Second request ---
            await _send_text(ws, "second request")
            frames_2 = await _collect_response(ws)
            assert frames_2[0] == {"type": "status", "status": "thinking"}
            deltas_2 = [f["delta"] for f in frames_2 if f["type"] == "assistant"]
            assert len(deltas_2) == 3
            assert frames_2[-1] == {"type": "status", "status": "idle"}


# ---------------------------------------------------------------------------
# Connection replacement
# ---------------------------------------------------------------------------


class TestConnectionReplacement:
    """New connection replaces the existing one (single-connection model)."""

    async def test_client_b_replaces_client_a(self, auth_gateway: tuple[str, object]) -> None:
        url, _ = auth_gateway

        # Client A connects and completes handshake
        ws_a = await _auth_connect(url)
        await _consume_handshake(ws_a)

        # Client B connects and completes handshake
        ws_b = await _auth_connect(url)
        async with ws_b:
            connected_b, history_b, idle_b = await _consume_handshake(ws_b)
            assert connected_b["type"] == "connected"
            assert history_b["type"] == "history"
            assert idle_b == {"type": "status", "status": "idle"}

        # Client A should have been closed by the server
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws_a.recv(), timeout=TIMEOUT)

        await ws_a.close()


# ---------------------------------------------------------------------------
# Error recovery
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    """Session recovers from errors and continues processing."""

    async def test_invalid_json_then_valid_text(self, auth_gateway: tuple[str, object]) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await _consume_handshake(ws)

            # Send invalid JSON
            await ws.send("{bad json")
            error = await _recv(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_FRAME"

            # Session should still be idle — send valid text
            await _send_text(ws, "recover from error")
            frames = await _collect_response(ws)

            assert frames[0] == {"type": "status", "status": "thinking"}
            deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
            assert deltas == [
                "This is a ",
                "test response ",
                "from the gateway.",
            ]
            assert frames[-1] == {"type": "status", "status": "idle"}


# ---------------------------------------------------------------------------
# Auth rejection
# ---------------------------------------------------------------------------


class TestAuthRejection:
    """Invalid or missing tokens are rejected with close code 4001."""

    async def test_wrong_token_closed_4001(self, auth_gateway: tuple[str, object]) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url, token="wrong-token")
        async with ws:
            with pytest.raises(websockets.ConnectionClosedError) as exc_info:
                await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
            assert exc_info.value.rcvd.code == 4001  # type: ignore[union-attr]

    async def test_no_token_closed_4001(self, auth_gateway: tuple[str, object]) -> None:
        url, _ = auth_gateway
        async with websockets.connect(url) as ws:
            with pytest.raises(websockets.ConnectionClosedError) as exc_info:
                await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
            assert exc_info.value.rcvd.code == 4001  # type: ignore[union-attr]
