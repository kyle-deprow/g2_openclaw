"""WebSocket server for the G2 OpenClaw Gateway."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import websockets
from websockets import ServerConnection

from gateway.audio_buffer import AudioBuffer, BufferOverflow
from gateway.config import GatewayConfig, load_config
from gateway.openclaw_client import OpenClawClient, OpenClawError
from gateway.protocol import (
    ErrorCode,
    ProtocolError,
    parse_text_frame,
    serialize,
    validate_outbound,
)
from gateway.transcriber import Transcriber, TranscriptionError

logger = logging.getLogger(__name__)

_MOCK_DELTAS = [
    "This is a ",
    "mock response ",
    "from the gateway.",
]

_MAX_RECORDING_SECONDS = 90


class SessionState(StrEnum):
    """Gateway session processing states."""

    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    STREAMING = "streaming"


class ResponseHandler(Protocol):
    """Protocol for handling text messages.

    Phase 2 will provide a real implementation backed by OpenClaw.
    """

    async def handle(
        self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None: ...

    async def close(self) -> None:
        """Release resources held by this handler."""
        ...


class MockResponseHandler:
    """Default mock handler that returns canned responses."""

    async def handle(
        self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        await asyncio.sleep(0.1)
        await send_frame({"type": "status", "status": "streaming"})
        for delta in _MOCK_DELTAS:
            await send_frame({"type": "assistant", "delta": delta})
        await send_frame({"type": "end"})

    async def close(self) -> None:
        """No-op for mock handler."""


class OpenClawResponseHandler:
    """Response handler that forwards messages to OpenClaw and streams responses."""

    def __init__(self, client: OpenClawClient) -> None:
        self._client = client

    async def handle(
        self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Forward message to OpenClaw, relay streamed deltas, send end frame."""
        stream = await self._client.send_message(message)

        await send_frame({"type": "status", "status": "streaming"})

        async for delta in stream:
            await send_frame({"type": "assistant", "delta": delta})

        await send_frame({"type": "end"})

    async def close(self) -> None:
        """Close the underlying OpenClaw client connection."""
        await self._client.close()


class GatewaySession:
    """Manages a single WebSocket connection."""

    def __init__(
        self,
        ws: ServerConnection,
        handler: ResponseHandler | None = None,
        transcriber: Transcriber | None = None,
        timeout: int = 120,
    ) -> None:
        self.ws = ws
        self._state = SessionState.IDLE
        self._handler: ResponseHandler = handler or MockResponseHandler()
        self._transcriber = transcriber
        self._audio_buffer: AudioBuffer | None = None
        self._timeout = timeout
        self._recording_start: float | None = None

    async def send_frame(self, frame: dict[str, Any]) -> None:
        validate_outbound(frame)
        await self.ws.send(serialize(frame))

    async def handle(self) -> None:
        await self.send_frame({"type": "connected", "version": "1.0"})
        await self.send_frame({"type": "status", "status": "idle"})

        async for message in self.ws:
            if isinstance(message, bytes):
                await self._handle_binary(message)
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

    async def _dispatch(self, frame: dict[str, Any]) -> None:
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
        elif frame_type == "start_audio":
            if self._state != SessionState.IDLE:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Cannot start audio while session is busy",
                        "code": ErrorCode.INVALID_STATE,
                    }
                )
                return
            await self._handle_start_audio(frame)
        elif frame_type == "stop_audio":
            if self._state != SessionState.RECORDING:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Cannot stop audio — not recording",
                        "code": ErrorCode.INVALID_STATE,
                    }
                )
                return
            await self._handle_stop_audio()
        else:
            await self.send_frame(
                {
                    "type": "error",
                    "detail": f"Unhandled frame type: {frame_type}",
                    "code": ErrorCode.INVALID_FRAME,
                }
            )

    async def _handle_binary(self, data: bytes) -> None:
        """Handle binary frame (PCM audio data)."""
        if self._state != SessionState.RECORDING:
            logger.warning("Binary frame received while not recording — ignoring")
            return
        if self._audio_buffer is None:
            logger.warning("Binary frame received but no audio buffer — ignoring")
            return
        # M-4: recording timeout
        if self._recording_start is not None:
            elapsed = asyncio.get_event_loop().time() - self._recording_start
            if elapsed > _MAX_RECORDING_SECONDS:
                logger.warning(
                    "Recording exceeded %ss limit — auto-stopping", _MAX_RECORDING_SECONDS
                )
                self._audio_buffer.reset()
                self._recording_start = None
                self._state = SessionState.IDLE
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": f"Recording exceeded {_MAX_RECORDING_SECONDS}s limit",
                        "code": ErrorCode.BUFFER_OVERFLOW,
                    }
                )
                await self.send_frame({"type": "status", "status": "idle"})
                return
        try:
            self._audio_buffer.append(data)
        except BufferOverflow as exc:
            logger.error("Audio buffer overflow: %s", exc)
            self._audio_buffer.reset()
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Audio buffer overflow",
                    "code": ErrorCode.BUFFER_OVERFLOW,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
        except ValueError as exc:
            logger.error("Invalid PCM data: %s", exc)
            if self._audio_buffer is not None:
                self._audio_buffer.reset()
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Invalid audio data format",
                    "code": ErrorCode.INVALID_FRAME,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})

    async def _handle_start_audio(self, frame: dict[str, Any]) -> None:
        """Start recording audio after validating format parameters."""
        sample_rate = frame["sampleRate"]
        channels = frame["channels"]
        sample_width = frame["sampleWidth"]

        if sample_width != 2:
            await self.send_frame(
                {
                    "type": "error",
                    "detail": (
                        f"Unsupported sample width: {sample_width} (only 16-bit PCM supported)"
                    ),
                    "code": ErrorCode.INVALID_FRAME,
                }
            )
            return
        if not (8_000 <= sample_rate <= 48_000):
            await self.send_frame(
                {
                    "type": "error",
                    "detail": f"Invalid sample rate: {sample_rate} (expected 8000-48000)",
                    "code": ErrorCode.INVALID_FRAME,
                }
            )
            return
        if channels not in (1, 2):
            await self.send_frame(
                {
                    "type": "error",
                    "detail": f"Invalid channels: {channels} (must be 1 or 2)",
                    "code": ErrorCode.INVALID_FRAME,
                }
            )
            return

        self._audio_buffer = AudioBuffer(
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )
        self._recording_start = asyncio.get_event_loop().time()
        self._state = SessionState.RECORDING
        await self.send_frame({"type": "status", "status": "recording"})

    async def _handle_stop_audio(self) -> None:
        """Stop recording and run transcription pipeline."""
        self._state = SessionState.TRANSCRIBING
        self._recording_start = None
        await self.send_frame({"type": "status", "status": "transcribing"})

        buf = self._audio_buffer
        self._audio_buffer = None

        if buf is None or buf.is_empty:
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "No audio data received",
                    "code": ErrorCode.TRANSCRIPTION_FAILED,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return

        if self._transcriber is None:
            logger.warning("No transcriber configured — skipping transcription")
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Transcriber not configured",
                    "code": ErrorCode.TRANSCRIPTION_FAILED,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return

        try:
            audio_array = buf.to_numpy()
            text = await self._transcriber.transcribe(audio_array)
        except TranscriptionError as exc:
            logger.error("Transcription failed: %s", exc)
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Transcription failed",
                    "code": ErrorCode.TRANSCRIPTION_FAILED,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return
        except TimeoutError:
            logger.error("Transcription timed out")
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Transcription timed out",
                    "code": ErrorCode.TIMEOUT,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return
        except Exception:
            logger.exception("Unexpected transcription error")
            self._state = SessionState.IDLE
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Internal transcription error",
                    "code": ErrorCode.INTERNAL_ERROR,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return

        # Send transcription to client
        await self.send_frame({"type": "transcription", "text": text})

        # Now handle the transcribed text as if it were a text message
        await self._handle_text({"type": "text", "message": text})

    async def _handle_text(self, frame: dict[str, Any]) -> None:
        self._state = SessionState.THINKING
        await self.send_frame({"type": "status", "status": "thinking"})
        try:
            await asyncio.wait_for(
                self._handler.handle(frame["message"], self.send_frame),
                timeout=self._timeout,
            )
        except TimeoutError:
            logger.error("Agent cycle timed out after %ss", self._timeout)
            await self._handler.close()
            try:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": f"Agent cycle exceeded {self._timeout}s timeout",
                        "code": ErrorCode.TIMEOUT,
                    }
                )
            except Exception:
                logger.debug("Failed to send timeout error frame", exc_info=True)
        except OpenClawError as exc:
            logger.error("OpenClaw error: %s", exc)
            await self._handler.close()
            try:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Agent communication error",
                        "code": ErrorCode.OPENCLAW_ERROR,
                    }
                )
            except Exception:
                logger.debug("Failed to send OpenClaw error frame", exc_info=True)
        except Exception:
            logger.exception("Response handler error")
            await self._handler.close()
            try:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Response processing failed",
                        "code": ErrorCode.OPENCLAW_ERROR,
                    }
                )
            except Exception:
                logger.debug("Failed to send handler error frame", exc_info=True)
        finally:
            self._state = SessionState.IDLE
            try:
                await self.send_frame({"type": "status", "status": "idle"})
            except Exception:
                logger.debug("Failed to send idle status on cleanup", exc_info=True)


class GatewayServer:
    """Single-connection WebSocket gateway server."""

    def __init__(
        self,
        config: GatewayConfig,
        handler: ResponseHandler | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self.config = config
        self._transcriber = transcriber
        self._current_session: GatewaySession | None = None

        if handler is not None:
            self._handler: ResponseHandler = handler
        elif config.openclaw_gateway_token:
            client = OpenClawClient(
                host=config.openclaw_host,
                port=config.openclaw_port,
                token=config.openclaw_gateway_token,
            )
            self._handler = OpenClawResponseHandler(client)
        else:
            self._handler = MockResponseHandler()

    async def handler(self, ws: ServerConnection) -> None:
        """Handle a new WebSocket connection."""
        # --- token auth ---
        if self.config.gateway_token:
            # Deprecation warning for query-string token
            if ws.request is not None:
                query = parse_qs(urlparse(ws.request.path).query)
                if query.get("token"):
                    logger.warning(
                        "Client attempted query-string token auth (deprecated and disabled). "
                        "Use the first-message auth handshake instead."
                    )

            # First-message auth handshake (only supported method)
            authenticated = False
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.config.auth_timeout)
                if isinstance(raw, str):
                    auth_frame = json.loads(raw)
                    if (
                        isinstance(auth_frame, dict)
                        and auth_frame.get("type") == "auth"
                        and isinstance(auth_frame.get("token"), str)
                        and hmac.compare_digest(auth_frame["token"], self.config.gateway_token)
                    ):
                        authenticated = True
            except (TimeoutError, json.JSONDecodeError, websockets.ConnectionClosed):
                pass

            if not authenticated:
                await ws.close(4001, "Unauthorized")
                return

        # --- replace existing session (single-connection model) ---
        session = GatewaySession(
            ws, self._handler, self._transcriber, timeout=self.config.agent_timeout
        )
        old_session = self._current_session
        self._current_session = session  # claim the slot first
        if old_session is not None:
            logger.info("Replacing existing connection")
            await self._handler.close()
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
        serve_kwargs: dict[str, object] = {
            "max_size": 2**16,  # 64 KiB max frame size
        }
        if self.config.allowed_origins is not None:
            serve_kwargs["origins"] = self.config.allowed_origins
        async with websockets.serve(
            self.handler,
            self.config.gateway_host,
            self.config.gateway_port,
            **serve_kwargs,  # type: ignore[arg-type]
        ):
            await asyncio.Future()  # block forever


async def main() -> None:
    """Entry point: load config, initialise transcriber, and start serving."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()

    transcriber: Transcriber | None = None
    try:
        transcriber = Transcriber(
            config.whisper_model, config.whisper_device, config.whisper_compute_type
        )
        logger.info("Transcriber loaded (model=%s)", config.whisper_model)
    except ImportError:
        logger.warning(
            "faster-whisper is not installed — audio transcription disabled. "
            "Install with: uv sync --extra whisper"
        )
    except Exception:
        logger.warning("Failed to load transcriber — audio transcription disabled", exc_info=True)

    server = GatewayServer(config, transcriber=transcriber)
    await server.serve()
