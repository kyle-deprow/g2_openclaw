"""WebSocket client for communicating with the OpenClaw Gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import websockets
from websockets import ClientConnection

logger = logging.getLogger(__name__)


class OpenClawError(Exception):
    """Raised when OpenClaw returns an error or communication fails."""


class OpenClawClient:
    """Async WebSocket client for the OpenClaw Gateway.

    Lazy connection: only connects on first agent message.
    Auth handshake with monotonic request IDs.
    """

    def __init__(self, host: str, port: int, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._ws: ClientConnection | None = None
        self._next_id: int = 1
        self._connected: bool = False

    def _get_next_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    @property
    def url(self) -> str:
        return f"ws://{self._host}:{self._port}"

    async def ensure_connected(self) -> None:
        """Connect and authenticate if not already connected."""
        if self._connected and self._ws is not None:
            return

        # Close any stale socket before reconnecting
        if self._ws is not None:
            await self._close_ws()

        # Reset request ID counter on new connection
        self._next_id = 1

        try:
            self._ws = await websockets.connect(self.url)
        except (OSError, websockets.WebSocketException) as exc:
            raise OpenClawError(f"connection refused: {exc}") from exc

        # Auth handshake
        auth_id = self._get_next_id()
        auth_req = {
            "type": "req",
            "id": auth_id,
            "method": "connect",
            "params": {"auth": {"token": self._token}},
        }
        try:
            await self._ws.send(json.dumps(auth_req))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            resp = json.loads(raw)
        except (TimeoutError, websockets.WebSocketException, json.JSONDecodeError) as exc:
            await self._close_ws()
            raise OpenClawError(f"auth handshake failed: {exc}") from exc

        if resp.get("type") != "res" or resp.get("id") != auth_id:
            await self._close_ws()
            raise OpenClawError(f"unexpected auth response: {resp}")

        if not resp.get("ok"):
            await self._close_ws()
            error = resp.get("error", "unknown error")
            raise OpenClawError(f"auth rejected: {error}")

        self._connected = True
        logger.info("Connected and authenticated to OpenClaw at %s", self.url)

    async def send_message(
        self,
        text: str,
        session_key: str = "agent:claw:g2",
    ) -> AsyncIterator[str]:
        """Send an agent request and yield assistant delta strings.

        Raises OpenClawError on agent errors or communication failures.
        """
        await self.ensure_connected()
        if self._ws is None:
            raise OpenClawError("not connected")

        req_id = self._get_next_id()
        agent_req = {
            "type": "req",
            "id": req_id,
            "method": "agent",
            "params": {
                "message": text,
                "sessionKey": session_key,
            },
        }

        try:
            await self._ws.send(json.dumps(agent_req))
        except websockets.WebSocketException as exc:
            self._connected = False
            raise OpenClawError(f"failed to send agent request: {exc}") from exc

        # Read the initial response (acceptance)
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            resp = json.loads(raw)
        except (TimeoutError, websockets.WebSocketException, json.JSONDecodeError) as exc:
            self._connected = False
            raise OpenClawError(f"no response to agent request: {exc}") from exc

        if resp.get("type") != "res" or resp.get("id") != req_id:
            self._connected = False
            raise OpenClawError(f"unexpected agent response: {resp}")

        if not resp.get("ok"):
            error = resp.get("error", "unknown error")
            raise OpenClawError(f"agent request rejected: {error}")

        # Now yield assistant deltas from the event stream
        async def _stream_deltas() -> AsyncIterator[str]:
            if self._ws is None:
                raise OpenClawError("not connected")
            _clean_exit = False
            try:
                async for raw_msg in self._ws:
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning("Malformed OpenClaw message: %s", raw_msg[:100])
                        continue

                    if msg.get("type") != "event" or msg.get("event") != "agent":
                        continue

                    payload = msg.get("payload", {})
                    stream = payload.get("stream")

                    if stream == "assistant":
                        delta = payload.get("delta", "")
                        if delta:
                            yield delta
                    elif stream == "lifecycle":
                        phase = payload.get("phase")
                        if phase == "end":
                            _clean_exit = True
                            return
                        elif phase == "error":
                            _clean_exit = True
                            detail = payload.get("error", "agent error")
                            raise OpenClawError(f"agent error: {detail}")
                    # Ignore tool and other streams
            except websockets.ConnectionClosed as exc:
                self._connected = False
                raise OpenClawError(f"disconnected: {exc}") from exc
            finally:
                if not _clean_exit:
                    self._connected = False

            # If we exit the async for loop without a lifecycle end,
            # the connection was closed prematurely.
            self._connected = False
            raise OpenClawError("disconnected: connection closed before lifecycle end")

        return _stream_deltas()

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._connected = False
        await self._close_ws()

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected = False
