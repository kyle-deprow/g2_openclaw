"""WebSocket server for the G2 OpenClaw Gateway."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import websockets
from websockets import ServerConnection

from gateway.config import GatewayConfig, load_config
from gateway.protocol import (
    ErrorCode,
    ProtocolError,
    parse_text_frame,
    serialize,
    validate_outbound,
)

logger = logging.getLogger(__name__)

_MOCK_DELTAS = [
    "This is a ",
    "mock response ",
    "from the gateway.",
]


class SessionState(StrEnum):
    """Gateway session processing states."""

    IDLE = "idle"
    THINKING = "thinking"
    STREAMING = "streaming"


class ResponseHandler(Protocol):
    """Protocol for handling text messages.

    Phase 2 will provide a real implementation backed by OpenClaw.
    """

    async def handle(
        self, message: str, send_frame: Callable[[dict], Awaitable[None]]
    ) -> None: ...


class MockResponseHandler:
    """Default mock handler that returns canned responses."""

    async def handle(
        self, message: str, send_frame: Callable[[dict], Awaitable[None]]
    ) -> None:
        await asyncio.sleep(0.1)
        await send_frame({"type": "status", "status": "streaming"})
        for delta in _MOCK_DELTAS:
            await send_frame({"type": "assistant", "delta": delta})
        await send_frame({"type": "end"})


class GatewaySession:
    """Manages a single WebSocket connection."""

    def __init__(
        self, ws: ServerConnection, handler: ResponseHandler | None = None
    ) -> None:
        self.ws = ws
        self._state = SessionState.IDLE
        self._handler: ResponseHandler = handler or MockResponseHandler()

    async def send_frame(self, frame: dict) -> None:
        validate_outbound(frame)
        await self.ws.send(serialize(frame))

    async def handle(self) -> None:
        await self.send_frame({"type": "connected", "version": "1.0"})
        await self.send_frame({"type": "status", "status": "idle"})

        async for message in self.ws:
            if isinstance(message, bytes):
                logger.warning("Binary frame received — ignoring (not implemented)")
                continue

            try:
                frame = parse_text_frame(message)
            except ProtocolError as exc:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": str(exc),
                        "code": ErrorCode.INVALID_FRAME,
                    }
                )
                continue

            await self._dispatch(frame)

    async def _dispatch(self, frame: dict) -> None:
        frame_type = frame["type"]

        if frame_type == "text":
            if self._state != SessionState.IDLE:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Cannot process text while session is busy",
                        "code": ErrorCode.INVALID_STATE,
                    }
                )
                return
            await self._handle_text(frame)
        elif frame_type == "pong":
            logger.info("Received pong")
        elif frame_type in ("start_audio", "stop_audio"):
            logger.info("Received %s (not implemented in Phase 1)", frame_type)
        else:
            await self.send_frame(
                {
                    "type": "error",
                    "detail": f"Unhandled frame type: {frame_type}",
                    "code": ErrorCode.INVALID_FRAME,
                }
            )

    async def _handle_text(self, frame: dict) -> None:
        self._state = SessionState.THINKING
        await self.send_frame({"type": "status", "status": "thinking"})
        try:
            self._state = SessionState.STREAMING
            await self._handler.handle(frame["message"], self.send_frame)
        finally:
            self._state = SessionState.IDLE
            await self.send_frame({"type": "status", "status": "idle"})


class GatewayServer:
    """Single-connection WebSocket gateway server."""

    def __init__(self, config: GatewayConfig, handler: ResponseHandler | None = None) -> None:
        self.config = config
        self._handler: ResponseHandler = handler or MockResponseHandler()
        self._current_session: GatewaySession | None = None

    async def handler(self, ws: ServerConnection) -> None:
        """Handle a new WebSocket connection."""
        # --- token auth ---
        if self.config.gateway_token:
            query = parse_qs(urlparse(ws.request.path).query)
            tokens = query.get("token", [])
            if not tokens or tokens[0] != self.config.gateway_token:
                await ws.close(4001, "Unauthorized")
                return

        # --- replace existing session (single-connection model) ---
        session = GatewaySession(ws, self._handler)
        old_session = self._current_session
        self._current_session = session  # claim the slot first
        if old_session is not None:
            logger.info("Replacing existing connection")
            try:
                await old_session.ws.close(1000, "Replaced by new connection")
            except Exception:
                logger.debug("Error closing previous session", exc_info=True)

        try:
            await session.handle()
        except websockets.ConnectionClosed:
            logger.info("Connection closed")
        finally:
            if self._current_session is session:
                self._current_session = None

    async def serve(self) -> None:
        """Start the server and run forever."""
        logger.info(
            "Gateway listening on %s:%s",
            self.config.gateway_host,
            self.config.gateway_port,
        )
        async with websockets.serve(
            self.handler, self.config.gateway_host, self.config.gateway_port
        ):
            await asyncio.Future()  # block forever


async def main() -> None:
    """Entry point: load config and start serving."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    server = GatewayServer(config)
    await server.serve()
