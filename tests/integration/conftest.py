"""Shared fixtures for integration tests."""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
import websockets
from gateway.config import GatewayConfig
from gateway.server import GatewayServer


class StaticResponseHandler:
    """Explicit test handler for integration tests."""

    async def handle(self, message: str, send_frame: Any) -> None:
        await send_frame({"type": "status", "status": "streaming"})
        for delta in ("This is a ", "test response ", "from the gateway."):
            await send_frame({"type": "assistant", "delta": delta})
        await send_frame({"type": "end"})

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_real_openclaw_state() -> Any:
    """Prevent integration tests from reading real host OpenClaw state."""
    with (
        patch("gateway.server.resolve_session", return_value=None),
        patch("gateway.server.read_task_status", return_value=None),
        patch("gateway.session_history.read_history", return_value=[]),
        patch("gateway.session_history.list_session_summaries", return_value=[]),
    ):
        yield


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
    gw = GatewayServer(config, handler=StaticResponseHandler())
    server = await websockets.serve(gw.handler, config.gateway_host, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw
    finally:
        server.close()
        await server.wait_closed()
