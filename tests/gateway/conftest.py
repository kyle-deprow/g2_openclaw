"""Shared fixtures for gateway integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
import websockets
from gateway.config import GatewayConfig
from gateway.server import GatewayServer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def recv_json(ws: websockets.ClientConnection) -> dict[str, Any]:
    """Receive and parse a JSON frame from the WebSocket."""
    result: dict[str, Any] = json.loads(await ws.recv())
    return result


async def auth_connect(url: str, token: str = "test-token") -> websockets.ClientConnection:
    """Connect and send first-message auth handshake."""
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    return ws


@pytest.fixture(autouse=True)
def _no_real_session_resolver() -> Iterator[None]:
    """Prevent tests from reading the real ~/.openclaw sessions.json."""
    with (
        patch("gateway.server.resolve_session", return_value=None),
        patch("gateway.session_history.read_history", return_value=[]),
    ):
        yield


@pytest_asyncio.fixture
async def auth_gateway() -> AsyncIterator[tuple[str, GatewayServer]]:
    """Start a gateway server with token auth, yield (url, server_instance)."""
    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token="test-token",
    )
    gw = GatewayServer(config)
    server = await websockets.serve(
        gw.handler,
        config.gateway_host,
        0,
        process_request=gw._process_request,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw
    finally:
        if gw._inflight_task and not gw._inflight_task.done():
            gw._inflight_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gw._inflight_task
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def noauth_gateway() -> AsyncIterator[tuple[str, GatewayServer]]:
    """Start a gateway server without token auth, yield (url, server_instance)."""
    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token=None,
    )
    gw = GatewayServer(config)
    server = await websockets.serve(
        gw.handler,
        config.gateway_host,
        0,
        process_request=gw._process_request,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw
    finally:
        if gw._inflight_task and not gw._inflight_task.done():
            gw._inflight_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gw._inflight_task
        server.close()
        await server.wait_closed()
