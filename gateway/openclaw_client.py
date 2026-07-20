"""WebSocket client for communicating with the OpenClaw Gateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import websockets
from websockets import ClientConnection

from gateway.device_identity import (
    DeviceIdentity,
    build_device_connect_block,
    load_or_create_device_identity,
)

try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

logger = logging.getLogger(__name__)


def _span(name: str, **attributes: str | int | float | bool):  # type: ignore[no-untyped-def]
    """Create an OTel span context manager, or a no-op if OTel is unavailable."""
    if not _HAS_OTEL:
        return contextlib.nullcontext()
    return trace.get_tracer(__name__).start_as_current_span(name, attributes=attributes)


def _record_span_error(exc: BaseException) -> None:
    """Record an exception on the current active span, if any."""
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    span.set_status(StatusCode.ERROR, str(exc))
    span.record_exception(exc)


# Defaults matching the JS GatewayClient SDK
_DEFAULT_CLIENT_ID = "gateway-client"
_DEFAULT_CLIENT_MODE = "backend"
_DEFAULT_CLIENT_VERSION = "dev"
_DEFAULT_ROLE = "operator"
_DEFAULT_SCOPES = ["operator.admin"]
_OPENCLAW_PROTOCOL_VERSION = 4

# Timeout for the server's ``connect.challenge`` event after WS open
_CHALLENGE_TIMEOUT_S = 5.0

# Sentinel used by _process_agent_event to signal stream end


class _StopSentinel:
    """Type-safe sentinel for stream-end detection."""


_STOP: _StopSentinel = _StopSentinel()


class OpenClawError(Exception):
    """Raised when OpenClaw returns an error or communication fails."""


class OpenClawTransportError(OpenClawError):
    """Raised for retryable OpenClaw connection, timeout, or socket failures."""


def _process_agent_event(msg: dict[str, object]) -> str | _StopSentinel | None:
    """Extract a delta string from an agent event message.

    Returns:
        - A ``str`` delta to yield to the caller.
        - ``_STOP`` if the lifecycle ended cleanly.
        - ``None`` if the message should be skipped.

    Raises ``OpenClawError`` on agent error events.
    """
    if msg.get("type") != "event" or msg.get("event") != "agent":
        return None

    payload = msg.get("payload", {})
    if not isinstance(payload, dict):
        return None

    stream = payload.get("stream")
    logger.info(
        "_stream_deltas agent event: stream=%s payload=%s",
        stream,
        json.dumps(payload)[:200],
    )

    # The real payload fields live inside payload.data (not top-level payload)
    data = payload.get("data", {})
    if not isinstance(data, dict):
        data = {}

    if stream == "assistant":
        delta = data.get("delta", "") or payload.get("delta", "")
        if delta:
            return str(delta)
        return None
    elif stream == "lifecycle":
        phase = data.get("phase") or payload.get("phase")
        if phase == "end":
            return _STOP
        elif phase == "error":
            detail = data.get("error") or payload.get("error", "agent error")
            raise OpenClawError(f"agent error: {detail}")
    # Ignore tool and other streams
    return None


class OpenClawClient:
    """Async WebSocket client for the OpenClaw Gateway.

    Lazy connection: only connects on first agent message.
    Auth handshake with device identity, challenge-response nonce, and scopes.

    The OpenClaw server requires a device identity (Ed25519 keypair) bound to
    the ``connect`` request in order to grant operator scopes.  Without it,
    scopes are silently cleared even though the connection succeeds.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        ssl_context: ssl.SSLContext | None = None,
        *,
        device_identity: DeviceIdentity | None = None,
        identity_path: Path | None = None,
        role: str = _DEFAULT_ROLE,
        scopes: list[str] | None = None,
        client_id: str = _DEFAULT_CLIENT_ID,
        client_mode: str = _DEFAULT_CLIENT_MODE,
        client_version: str = _DEFAULT_CLIENT_VERSION,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._ssl_context = ssl_context
        self._ws: ClientConnection | None = None
        self._next_id: int = 1
        self._connected: bool = False
        self._server_version: str | None = None

        # Device identity — generate or load once
        self._identity = device_identity or load_or_create_device_identity(identity_path)

        # Connect metadata
        self._role = role
        self._scopes = scopes if scopes is not None else list(_DEFAULT_SCOPES)
        self._client_id = client_id
        self._client_mode = client_mode
        self._client_version = client_version

    def _get_next_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    @property
    def url(self) -> str:
        # Use wss:// for remote hosts, ws:// for localhost
        scheme = "ws"
        if self._host not in ("127.0.0.1", "localhost", "::1"):
            scheme = "wss"
            logger.info("Using wss:// for remote OpenClaw host %s", self._host)
        return f"{scheme}://{self._host}:{self._port}"

    async def ensure_connected(self) -> None:
        """Connect and authenticate if not already connected.

        The OpenClaw handshake is a two-phase process:

        1. **Challenge** — immediately after the WebSocket opens the server
           pushes a ``connect.challenge`` event containing a one-time ``nonce``.
        2. **Connect** — the client sends a ``connect`` request that includes
           the ``auth`` credentials, ``role``, ``scopes``, and a ``device``
           block with an Ed25519 signature over the canonical payload
           (incorporating the nonce).  This binds the scopes to a verified
           device identity so the server preserves them.
        """
        with _span("openclaw.connect"):
            try:
                await self._ensure_connected_inner()
            except OpenClawError as exc:
                _record_span_error(exc)
                raise

    async def _ensure_connected_inner(self) -> None:
        if self._connected and self._ws is not None:
            return

        # Close any stale socket before reconnecting
        if self._ws is not None:
            await self._close_ws()

        # Reset request ID counter on new connection
        self._next_id = 1

        try:
            connect_kwargs: dict[str, object] = {}
            if self._ssl_context is not None:
                connect_kwargs["ssl"] = self._ssl_context
            self._ws = await websockets.connect(self.url, **connect_kwargs)  # type: ignore[arg-type]
        except (OSError, websockets.WebSocketException) as exc:
            raise OpenClawTransportError(f"connection refused: {exc}") from exc

        # Phase 1: wait for the server's connect.challenge event containing nonce
        nonce: str | None = None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_CHALLENGE_TIMEOUT_S)
            challenge = json.loads(raw)
            if challenge.get("type") == "event" and challenge.get("event") == "connect.challenge":
                payload = challenge.get("payload", {})
                nonce = payload.get("nonce") if isinstance(payload, dict) else None
        except (TimeoutError, websockets.WebSocketException) as exc:
            await self._close_ws()
            raise OpenClawTransportError(f"connect challenge failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            await self._close_ws()
            raise OpenClawError(f"connect challenge failed: {exc}") from exc

        if not nonce or not isinstance(nonce, str) or not nonce.strip():
            await self._close_ws()
            raise OpenClawError("connect challenge missing nonce")

        nonce = nonce.strip()

        # Phase 2: send the connect request with device identity + signed payload
        device_block, _signed_at = build_device_connect_block(
            self._identity,
            client_id=self._client_id,
            client_mode=self._client_mode,
            role=self._role,
            scopes=self._scopes,
            token=self._token,
            nonce=nonce,
        )

        auth_id = self._get_next_id()
        auth_req = {
            "type": "req",
            "id": str(auth_id),
            "method": "connect",
            "params": {
                "minProtocol": _OPENCLAW_PROTOCOL_VERSION,
                "maxProtocol": _OPENCLAW_PROTOCOL_VERSION,
                "client": {
                    "id": self._client_id,
                    "version": self._client_version,
                    "platform": "python",
                    "mode": self._client_mode,
                },
                "auth": {"token": self._token},
                "role": self._role,
                "scopes": self._scopes,
                "device": device_block,
            },
        }
        try:
            await self._ws.send(json.dumps(auth_req))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            resp = json.loads(raw)
        except (TimeoutError, websockets.WebSocketException) as exc:
            await self._close_ws()
            raise OpenClawTransportError(f"auth handshake failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            await self._close_ws()
            raise OpenClawError(f"auth handshake failed: {exc}") from exc

        if resp.get("type") != "res" or resp.get("id") != str(auth_id):
            await self._close_ws()
            raise OpenClawError(f"unexpected auth response: {resp}")

        if not resp.get("ok"):
            await self._close_ws()
            error = resp.get("error", "unknown error")
            raise OpenClawError(f"auth rejected: {error}")

        payload = resp.get("payload")
        if isinstance(payload, Mapping):
            server = payload.get("server")
            if isinstance(server, Mapping):
                version = server.get("version")
                self._server_version = version if isinstance(version, str) else None

        self._connected = True
        logger.info("Connected and authenticated to OpenClaw at %s", self.url)

    async def request_once(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float,
        required_server_version: str | None = None,
    ) -> Mapping[str, object]:
        """Make one authenticated RPC request and always close its socket."""
        if not method.strip():
            raise OpenClawError("RPC method must be non-empty")
        if timeout_seconds <= 0:
            raise OpenClawError("RPC timeout must be positive")
        try:
            await self.ensure_connected()
            if self._ws is None:
                raise OpenClawError("not connected")
            if (
                required_server_version is not None
                and self._server_version != required_server_version
            ):
                raise OpenClawError(
                    "OpenClaw gateway server version mismatch: "
                    f"expected {required_server_version}, got {self._server_version!r}"
                )

            request_id = str(self._get_next_id())
            await self._ws.send(
                json.dumps(
                    {"type": "req", "id": request_id, "method": method, "params": dict(params)}
                )
            )
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise OpenClawTransportError(f"timed out waiting for {method} response")
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                message = json.loads(raw)
                if not isinstance(message, Mapping) or message.get("type") == "event":
                    continue
                if message.get("type") != "res" or message.get("id") != request_id:
                    raise OpenClawError(f"unexpected {method} response")
                if message.get("ok") is not True:
                    raise OpenClawError(
                        f"{method} request rejected: {message.get('error', 'unknown error')}"
                    )
                result = message.get("payload")
                if not isinstance(result, Mapping):
                    raise OpenClawError(f"{method} response has a non-object payload")
                return result
        except (TimeoutError, websockets.WebSocketException) as exc:
            raise OpenClawTransportError(f"{method} request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OpenClawError(f"{method} request failed: {exc}") from exc
        finally:
            await self.close()

    async def send_message(
        self,
        text: str,
        session_key: str = "agent:main:g2",
    ) -> AsyncIterator[str]:
        """Send an agent request and yield assistant delta strings.

        Raises OpenClawError on agent errors or communication failures.
        """
        with _span("openclaw.send_message_init", session_key=session_key):
            return await self._send_message_inner(text, session_key)

    async def _send_message_inner(
        self,
        text: str,
        session_key: str = "agent:main:g2",
    ) -> AsyncIterator[str]:
        await self.ensure_connected()
        if self._ws is None:
            raise OpenClawError("not connected")

        req_id = self._get_next_id()
        agent_req = {
            "type": "req",
            "id": str(req_id),
            "method": "agent",
            "params": {
                "message": text,
                "sessionKey": session_key,
                "idempotencyKey": str(uuid.uuid4()),
            },
        }

        try:
            await self._ws.send(json.dumps(agent_req))
        except websockets.WebSocketException as exc:
            self._connected = False
            raise OpenClawError(f"failed to send agent request: {exc}") from exc

        # Read the initial response (acceptance), buffering interleaved events
        resp: dict[str, object] | None = None
        buffered_events: list[dict[str, object]] = []
        deadline = asyncio.get_event_loop().time() + 10.0
        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for agent response")
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                # Buffer events (they may contain agent deltas) — we only want the res frame
                if msg.get("type") == "event":
                    logger.info(
                        "send_message buffered event: %s",
                        json.dumps(msg)[:200],
                    )
                    buffered_events.append(msg)
                    continue
                resp = msg
                break
            logger.info("agent res frame: %s", json.dumps(resp)[:500])
        except (TimeoutError, websockets.WebSocketException, json.JSONDecodeError) as exc:
            self._connected = False
            raise OpenClawError(f"no response to agent request: {exc}") from exc

        if resp is None or resp.get("type") != "res" or resp.get("id") != str(req_id):
            self._connected = False
            raise OpenClawError(f"unexpected agent response: {resp}")

        if not resp.get("ok"):
            error = resp.get("error", "unknown error")
            raise OpenClawError(f"agent request rejected: {error}")

        # Now yield assistant deltas from the event stream
        async def _stream_deltas(
            pre_buffered: list[dict[str, object]],
        ) -> AsyncIterator[str]:
            if self._ws is None:
                raise OpenClawError("not connected")
            logger.info("_stream_deltas: starting (buffered=%d)", len(pre_buffered))
            _clean_exit = False
            try:
                # First, drain any events that arrived before the res frame
                for msg in pre_buffered:
                    logger.info("_stream_deltas buffered: %s", json.dumps(msg)[:200])
                    result = _process_agent_event(msg)
                    if isinstance(result, _StopSentinel):
                        _clean_exit = True
                        return
                    if result is not None:
                        yield result

                # Then continue reading live from the WebSocket
                async for raw_msg in self._ws:
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning("Malformed OpenClaw message: %s", raw_msg[:100])
                        continue

                    logger.info("_stream_deltas raw: %s", json.dumps(msg)[:200])
                    logger.info("wire recv: %s", json.dumps(msg)[:300])
                    result = _process_agent_event(msg)
                    if isinstance(result, _StopSentinel):
                        _clean_exit = True
                        return
                    if result is not None:
                        yield result
            except websockets.ConnectionClosed as exc:
                self._connected = False
                raise OpenClawError(f"disconnected: {exc}") from exc
            finally:
                # Always close the WebSocket after streaming completes to
                # prevent stale events from carrying over to the next
                # send_message() call.  The next call will reconnect fresh.
                await self._close_ws()

            # If we exit the async for loop without a lifecycle end,
            # the connection was closed prematurely.
            self._connected = False
            raise OpenClawError("disconnected: connection closed before lifecycle end")

        logger.info("send_message: accepted, buffered_events=%d", len(buffered_events))
        return _stream_deltas(buffered_events)

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._connected = False
        await self._close_ws()

    async def disconnect(self) -> None:
        """Reset connection state so the next call reconnects."""
        await self._close_ws()

    async def _close_ws(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._connected = False
