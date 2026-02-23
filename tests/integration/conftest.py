"""Shared fixtures for integration tests."""

from collections.abc import AsyncIterator

import pytest_asyncio
import websockets
from gateway.config import GatewayConfig
from gateway.server import GatewayServer


@pytest_asyncio.fixture
async def auth_gateway() -> AsyncIterator[tuple[str, GatewayServer]]:
    """Start a gateway server with token auth on an ephemeral port.

    Yields (ws_url, GatewayServer).
    """
    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token="integration-token",
        auth_timeout=1.0,
    )
    gw = GatewayServer(config)
    server = await websockets.serve(gw.handler, config.gateway_host, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def noauth_gateway() -> AsyncIterator[tuple[str, GatewayServer]]:
    """Start a gateway server without token auth on an ephemeral port.

    Yields (ws_url, GatewayServer).
    """
    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token=None,
    )
    gw = GatewayServer(config)
    server = await websockets.serve(gw.handler, config.gateway_host, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw
    finally:
        server.close()
        await server.wait_closed()
