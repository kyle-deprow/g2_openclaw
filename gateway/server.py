"""WebSocket server for the G2 OpenClaw Gateway."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import sys
import time as _time_module
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import parse_qs, urlparse

import websockets
import websockets.http11
from websockets import ServerConnection

from gateway.audio_buffer import AudioBuffer, BufferOverflow
from gateway.config import GatewayConfig, load_config
from gateway.copilot_sessions import kill_copilot_session, list_copilot_sessions
from gateway.copilot_watcher import CopilotSessionWatcher
from gateway.metrics import gateway_metrics
from gateway.openclaw_client import OpenClawClient, OpenClawError
from gateway.process_monitor import CopilotProcessMonitor
from gateway.protocol import (
    ErrorCode,
    ProtocolError,
    parse_text_frame,
    serialize,
    validate_outbound,
)
from gateway.session_resolver import resolve_session
from gateway.task_status import read_task_status
from gateway.transcriber import Transcriber, TranscriptionError

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


_MAX_RECORDING_SECONDS = 90

_BUFFER_MAX_CHARS = 200_000  # ~200 KB text limit
_BUFFER_TTL_SECONDS = 300  # discard after 5 minutes

_STREAM_LOG = Path(__file__).resolve().parent.parent / "logs" / "openclaw-stream.log"

_NOTIFY_RETRY_DELAYS = (5, 15, 30)


async def _notify_openclaw_with_retry(
    client: OpenClawClient,
    message: str,
    session_key: str,
) -> None:
    """Send notification to OpenClaw with increasing-delay retries."""
    max_attempts = len(_NOTIFY_RETRY_DELAYS) + 1
    for attempt in range(max_attempts):
        try:
            stream = await client.send_message(message, session_key=session_key)
            async for _delta in stream:
                pass  # drain — we don't display the response
            return
        except OpenClawError:
            if attempt >= len(_NOTIFY_RETRY_DELAYS):
                logger.error(
                    "Process monitor: failed to notify OpenClaw after %d attempts",
                    max_attempts,
                    exc_info=True,
                )
                return
            delay = _NOTIFY_RETRY_DELAYS[attempt]
            logger.warning(
                "Process monitor: notify attempt %d/%d failed, retrying in %ds",
                attempt + 1,
                max_attempts,
                delay,
                exc_info=True,
            )
            await client.disconnect()
            await asyncio.sleep(delay)


# --- Auth rate limiting ---
_AUTH_MAX_FAILURES = 5
_AUTH_WINDOW_SECONDS = 60
_auth_failures: dict[str, list[float]] = {}


def _check_auth_rate_limit(remote_ip: str) -> bool:
    """Return True if the IP is rate-limited (too many failed attempts)."""
    now = _time_module.monotonic()
    attempts = _auth_failures.get(remote_ip, [])
    # Prune old attempts outside the window
    attempts = [t for t in attempts if now - t < _AUTH_WINDOW_SECONDS]
    _auth_failures[remote_ip] = attempts
    return len(attempts) >= _AUTH_MAX_FAILURES


def _record_auth_failure(remote_ip: str) -> None:
    """Record a failed auth attempt for rate limiting."""
    now = _time_module.monotonic()
    attempts = _auth_failures.setdefault(remote_ip, [])
    attempts.append(now)
    # Prune to keep only recent attempts
    _auth_failures[remote_ip] = [t for t in attempts if now - t < _AUTH_WINDOW_SECONDS]


def _generate_session_key() -> str:
    """Generate a new unique session key."""
    return f"agent:claw:g2:{int(_time_module.time())}:{uuid.uuid4().hex[:6]}"


def _today_utc() -> str:
    """Return today's date in UTC as YYYY-MM-DD."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass
class InflightBuffer:
    """Holds an in-flight OpenClaw response while the phone is disconnected."""

    user_question: str
    deltas: list[str] = field(default_factory=list)
    complete: bool = False
    error: str | None = None
    error_code: ErrorCode = ErrorCode.OPENCLAW_ERROR
    created_at: float = field(default_factory=_time_module.monotonic)
    _char_count: int = field(default=0, init=False, repr=False)

    @property
    def full_text(self) -> str:
        return "".join(self.deltas)

    @property
    def char_count(self) -> int:
        return self._char_count

    def append_delta(self, delta: str) -> bool:
        """Append *delta* if within the char limit. Return False if limit exceeded."""
        if self._char_count + len(delta) > _BUFFER_MAX_CHARS:
            return False
        self.deltas.append(delta)
        self._char_count += len(delta)
        return True

    @property
    def expired(self) -> bool:
        return (_time_module.monotonic() - self.created_at) > _BUFFER_TTL_SECONDS


class SessionState(StrEnum):
    """Gateway session processing states."""

    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    STREAMING = "streaming"


class ResponseHandler(Protocol):
    """Protocol for handling text messages."""

    async def handle(
        self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None: ...

    async def close(self) -> None:
        """Release resources held by this handler."""
        ...


class OpenClawResponseHandler:
    """Response handler that forwards messages to OpenClaw and streams responses."""

    def __init__(
        self,
        client: OpenClawClient,
        get_session_key: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._get_session_key = get_session_key or (lambda: "agent:claw:g2")

    async def start_stream(self, message: str) -> AsyncIterator[str]:
        """Initiate an OpenClaw agent request and return the delta stream."""
        logger.info("Sending to OpenClaw: %s", message[:100])
        return await self._client.send_message(message, session_key=self._get_session_key())

    async def handle(
        self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Forward message to OpenClaw, relay streamed deltas, send end frame."""
        stream = await self.start_stream(message)

        await send_frame({"type": "status", "status": "streaming"})

        async for delta in stream:
            logger.debug("OpenClaw delta: %s", delta[:50] if delta else "")
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
        local_audio: bool = False,
        history_limit: int = 10,
        session_key: str = "agent:claw:g2",
        agent_id: str = "claw",
        server: GatewayServer | None = None,
    ) -> None:
        self.ws = ws
        self._state = SessionState.IDLE
        if handler is None:
            raise ValueError("GatewaySession requires an explicit response handler")
        self._handler: ResponseHandler = handler
        self._transcriber = transcriber
        self._audio_buffer: AudioBuffer | None = None
        self._timeout = timeout
        self._recording_start: float | None = None
        self._local_audio = local_audio
        self._local_stream: Any = None  # sounddevice.InputStream when active
        self._history_limit = history_limit
        self._session_key = session_key
        self._agent_id = agent_id
        self._current_question: str | None = None
        self._task_start: float | None = None
        self._server: GatewayServer | None = server
        self._on_ready: Callable[[], Awaitable[None]] | None = None

    async def send_frame(self, frame: dict[str, Any]) -> None:
        validate_outbound(frame)
        await self.ws.send(serialize(frame))

    def _stop_local_stream(self) -> None:
        """Stop and close the local sounddevice stream if active."""
        if self._local_stream is not None:
            try:
                self._local_stream.stop()
                self._local_stream.close()
                logger.info("Local audio capture stopped")
            except Exception:
                logger.debug("Error stopping local audio stream", exc_info=True)
            finally:
                self._local_stream = None

    async def _send_history(self) -> None:
        """Send recent conversation history. Best-effort."""
        try:
            from gateway.session_history import read_history

            entries = read_history(
                session_key=self._session_key,
                agent_id=self._agent_id,
                limit=self._history_limit,
            )
            await self.send_frame(
                {
                    "type": "history",
                    "entries": [
                        {"role": e.role, "text": e.text[:2000], "ts": e.ts} for e in entries
                    ],
                }
            )
            logger.info("Sent %d history entries to client", len(entries))
        except Exception:
            logger.warning("Failed to send history — continuing without it", exc_info=True)

    def _phase_description(self) -> str | None:
        """Return a human-readable description of the current processing phase."""
        match self._state:
            case SessionState.IDLE:
                return None
            case SessionState.RECORDING:
                return "Recording audio"
            case SessionState.TRANSCRIBING:
                return "Transcribing audio"
            case SessionState.THINKING:
                return "Waiting for OpenClaw"
            case SessionState.STREAMING:
                return "Streaming response"

    def _build_status_frame(self, *, include_metadata: bool = False) -> dict[str, Any]:
        """Build a status frame, optionally including task metadata."""
        frame: dict[str, Any] = {"type": "status", "status": self._state.value}
        if include_metadata:
            if self._current_question:
                frame["question"] = self._current_question[:200]
            if self._task_start is not None:
                elapsed = asyncio.get_running_loop().time() - self._task_start
                frame["elapsedMs"] = int(elapsed * 1000)
            phase = self._phase_description()
            if phase:
                frame["phase"] = phase
        return frame

    # ── Quick commands ──────────────────────────────────────────────
    # Short voice commands handled locally without an OpenClaw round-trip.
    # Keeps the G2 UX responsive for common steering actions.

    _QUICK_COMMANDS: ClassVar[dict[str, str]] = {
        "status": "status",
        "what's happening": "status",
        "what is happening": "status",
        "update": "status",
        "progress": "status",
    }

    async def _try_quick_command(self, message: str) -> bool:
        """Intercept short steering commands and handle locally.

        Returns True if the command was handled, False to pass through to OpenClaw.
        """
        normalised = message.strip().lower().rstrip("?.!")
        cmd = self._QUICK_COMMANDS.get(normalised)
        if cmd is None:
            return False

        logger.info("Quick command: %r → %s", message, cmd)

        if cmd == "status":
            await self._quick_status()
            return True

        return False

    async def _quick_status(self) -> None:
        """Build and send a quick local status summary without querying OpenClaw."""
        import subprocess

        parts: list[str] = []

        # 1. Task status from transcript
        task_info = read_task_status(
            session_key=self._session_key,
            agent_id=self._agent_id,
        )
        if task_info:
            parts.append(f"[{task_info.status.upper()}] {task_info.description[:80]}")

        # 2. Copilot processes
        try:
            result = subprocess.run(
                ["pgrep", "-fa", "copilot.*-p"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            copilot_lines = [
                line
                for line in result.stdout.strip().splitlines()
                if "tsserver" not in line and "vscode" not in line
            ]
            if copilot_lines:
                parts.append(f"Copilot: {len(copilot_lines)} running")
            else:
                parts.append("Copilot: idle")
        except Exception:
            parts.append("Copilot: unknown")

        # 3. Process monitor tracked PIDs
        if self._server and self._server._process_monitor:
            tracked = self._server._process_monitor.tracked_pids
            if tracked:
                parts.append(f"Monitored: {len(tracked)} PIDs")

        # 4. Git status of quantipy
        quantipy_dir = "/home/dev/repos/quantipy"
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=quantipy_dir,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts.append(f"Last commit: {result.stdout.strip()[:60]}")
        except Exception:
            pass

        summary = " | ".join(parts) if parts else "No active tasks"
        await self.send_frame({"type": "assistant", "delta": summary})
        await self.send_frame({"type": "status", "status": "idle"})

    async def _handle_status_request(self) -> None:
        """Respond with current status and optional task metadata."""
        frame = self._build_status_frame(include_metadata=True)
        await self.send_frame(frame)

    async def _handle_session_list_request(self) -> None:
        """Return the full session list to the client."""
        try:
            from gateway.session_history import list_session_summaries

            summaries = list_session_summaries(agent_id=self._agent_id)
            logger.info("Session list: %d sessions found", len(summaries))
            await self.send_frame(
                {
                    "type": "session_list",
                    "sessions": [
                        {
                            "sessionKey": s.session_key,
                            "sessionId": s.session_id,
                            "updatedAt": s.updated_at,
                            "preview": s.preview,
                            "messageCount": s.message_count,
                            "label": s.preview[:40] if s.preview else s.session_key,
                            "isActive": s.session_key == self._session_key,
                        }
                        for s in summaries
                    ],
                    "activeSessionKey": self._session_key,
                }
            )
            logger.info("Session list sent successfully")
        except Exception:
            logger.warning("Failed to list sessions", exc_info=True)
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Failed to list sessions",
                    "code": ErrorCode.INTERNAL_ERROR,
                }
            )

    async def _handle_copilot_session_list(self) -> None:
        """Return running Copilot CLI sessions to the client."""
        try:
            sessions = list_copilot_sessions(running_only=True)
            logger.info("Copilot session list: %d sessions found", len(sessions))
            await self.send_frame(
                {
                    "type": "copilot_session_list",
                    "sessions": [
                        {
                            "sessionId": s.session_id,
                            "cwd": s.cwd,
                            "dirName": s.dir_name,
                            "repository": s.repository or "",
                            "branch": s.branch or "",
                            "summary": s.summary,
                            "updatedAt": s.updated_at,
                            "isRunning": s.is_running,
                        }
                        for s in sessions
                    ],
                }
            )
        except Exception:
            logger.warning("Failed to list Copilot sessions", exc_info=True)
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Failed to list Copilot sessions",
                    "code": ErrorCode.INTERNAL_ERROR,
                }
            )

    async def _handle_copilot_kill(self, frame: dict[str, Any]) -> None:
        """Kill a running Copilot CLI session by sending SIGTERM to its PID."""
        session_id = frame["sessionId"]
        try:
            success = kill_copilot_session(session_id)
            await self.send_frame(
                {
                    "type": "copilot_killed",
                    "sessionId": session_id,
                    "success": success,
                }
            )
            if success:
                logger.info("Killed Copilot session %s", session_id)
            else:
                logger.warning("Failed to kill Copilot session %s", session_id)
        except Exception:
            logger.warning("Error killing Copilot session %s", session_id, exc_info=True)
            await self.send_frame(
                {
                    "type": "copilot_killed",
                    "sessionId": session_id,
                    "success": False,
                }
            )

    async def _handle_session_switch(self, frame: dict[str, Any]) -> None:
        """Switch to an existing session."""
        target_key = frame["sessionKey"]
        if self._server is None:
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Session management not available",
                    "code": ErrorCode.INTERNAL_ERROR,
                }
            )
            return
        await self._server.switch_session(target_key)

    async def _handle_session_create(self) -> None:
        """Create a new session and switch to it."""
        if self._server is None:
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Session management not available",
                    "code": ErrorCode.INTERNAL_ERROR,
                }
            )
            return
        await self._server.create_session()

    async def handle(self) -> None:
        connected_frame: dict[str, Any] = {"type": "connected", "version": "1.0"}
        meta = resolve_session(
            session_key=self._session_key,
            agent_id=self._agent_id,
        )
        if meta is not None:
            connected_frame["sessionId"] = meta.session_id
            connected_frame["sessionKey"] = meta.session_key
            if meta.updated_at:
                connected_frame["sessionStartedAt"] = meta.updated_at

        # Inject task status for async reconnect awareness
        task_info = read_task_status(
            session_key=self._session_key,
            agent_id=self._agent_id,
        )
        if task_info is not None:
            connected_frame["taskSummary"] = f"[{task_info.status.upper()}] {task_info.description}"

        await self.send_frame(connected_frame)

        await self._send_history()

        await self.send_frame({"type": "status", "status": "idle"})

        # Allow server to replay buffered response
        if self._on_ready:
            await self._on_ready()

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
        logger.info("Dispatch: %s", frame_type)

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
            await self._handle_stop_audio(frame)
        elif frame_type == "status_request":
            await self._handle_status_request()
        elif frame_type == "reset_session":
            if self._state != SessionState.IDLE:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Cannot reset session while busy",
                        "code": ErrorCode.INVALID_STATE,
                    }
                )
                return
            if self._server is not None:
                await self._server.reset_session("user_request")
        elif frame_type == "session_list_request":
            await self._handle_session_list_request()
        elif frame_type == "session_switch":
            if self._state != SessionState.IDLE:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Cannot switch session while busy",
                        "code": ErrorCode.INVALID_STATE,
                    }
                )
                return
            await self._handle_session_switch(frame)
        elif frame_type == "session_create":
            if self._state != SessionState.IDLE:
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "Cannot create session while busy",
                        "code": ErrorCode.INVALID_STATE,
                    }
                )
                return
            await self._handle_session_create()
        elif frame_type == "force_stop":
            if self._server is not None:
                await self._server.force_stop()
        elif frame_type == "copilot_session_list_request":
            await self._handle_copilot_session_list()
        elif frame_type == "copilot_watch":
            if self._server is not None:
                await self._server.handle_copilot_watch(frame["sessionId"])
        elif frame_type == "copilot_unwatch":
            if self._server is not None:
                await self._server.handle_copilot_unwatch()
        elif frame_type == "copilot_kill":
            await self._handle_copilot_kill(frame)
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
        if self._local_audio:
            # In local-audio mode the mic stream feeds the buffer directly —
            # silently discard binary frames from the WebSocket.
            return
        if self._state != SessionState.RECORDING:
            logger.warning("Binary frame received while not recording — ignoring")
            return
        if self._audio_buffer is None:
            logger.warning("Binary frame received but no audio buffer — ignoring")
            return
        # M-4: recording timeout
        if self._recording_start is not None:
            elapsed = asyncio.get_running_loop().time() - self._recording_start
            if elapsed > _MAX_RECORDING_SECONDS:
                logger.warning(
                    "Recording exceeded %ss limit — auto-stopping", _MAX_RECORDING_SECONDS
                )
                self._audio_buffer.reset()
                self._recording_start = None
                self._state = SessionState.IDLE
                self._current_question = None
                self._task_start = None
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
            self._current_question = None
            self._task_start = None
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
            self._current_question = None
            self._task_start = None
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

        logger.info(
            "start_audio: sample_rate=%d, channels=%d, sample_width=%d",
            sample_rate,
            channels,
            sample_width,
        )
        self._audio_buffer = AudioBuffer(
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )
        self._recording_start = asyncio.get_running_loop().time()
        self._state = SessionState.RECORDING

        # Start local mic capture when --local-audio is enabled
        if self._local_audio:
            try:
                import sounddevice as sd  # type: ignore[import-not-found,import-untyped,unused-ignore]  # lazy, optional dep

                def _audio_callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                    if status:
                        logger.warning("Local audio stream status: %s", status)
                    if self._audio_buffer is not None:
                        self._audio_buffer.append(indata.tobytes())

                self._local_stream = sd.InputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                    blocksize=sample_rate // 10,  # 100 ms chunks
                    callback=_audio_callback,
                )
                self._local_stream.start()
                logger.info(
                    "Local audio capture started (rate=%d, ch=%d, blocksize=%d)",
                    sample_rate,
                    channels,
                    sample_rate // 10,
                )
            except Exception:
                logger.exception("Failed to start local audio capture")
                self._local_stream = None

        await self.send_frame({"type": "status", "status": "recording"})

    async def _handle_stop_audio(self, frame: dict[str, Any] | None = None) -> None:
        """Stop recording and run transcription pipeline."""
        with _span("gateway.transcribe_audio"):
            await self._handle_stop_audio_inner(frame)

    async def _handle_stop_audio_inner(self, frame: dict[str, Any] | None = None) -> None:
        """Inner implementation of stop audio handling."""
        # Stop local mic stream if active
        self._stop_local_stream()

        self._task_start = asyncio.get_running_loop().time()
        self._state = SessionState.TRANSCRIBING
        self._recording_start = None
        await self.send_frame({"type": "status", "status": "transcribing"})

        # HIL mode: synthesize text → fill buffer for transcription
        hil_text = frame.get("hilText") if frame else None
        if hil_text:
            try:
                from gateway.tts import synthesize_pcm

                logger.info("HIL TTS: synthesizing %d chars", len(hil_text))
                pcm_bytes, tts_rate = await synthesize_pcm(hil_text)
                # Create a fresh buffer and fill it with the TTS audio
                from gateway.audio_buffer import AudioBuffer

                self._audio_buffer = AudioBuffer(
                    sample_rate=tts_rate,
                    channels=1,
                    sample_width=2,
                )
                self._audio_buffer.append(pcm_bytes)
                logger.info(
                    "HIL TTS: buffer filled (%.1fs of audio)",
                    self._audio_buffer.duration_seconds,
                )
            except Exception:
                logger.exception("HIL TTS synthesis failed")
                self._state = SessionState.IDLE
                self._current_question = None
                self._task_start = None
                await self.send_frame(
                    {
                        "type": "error",
                        "detail": "TTS synthesis failed",
                        "code": ErrorCode.INTERNAL_ERROR,
                    }
                )
                await self.send_frame({"type": "status", "status": "idle"})
                return

        buf = self._audio_buffer
        self._audio_buffer = None

        if buf is None or buf.is_empty:
            self._state = SessionState.IDLE
            self._current_question = None
            self._task_start = None
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
            self._current_question = None
            self._task_start = None
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Transcriber not configured",
                    "code": ErrorCode.TRANSCRIPTION_FAILED,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return

        _transcription_start = _time_module.monotonic()
        try:
            audio_array = buf.to_numpy()
            try:
                import numpy as _np

                rms = float(_np.sqrt(_np.mean(audio_array**2)))
                peak = float(_np.max(_np.abs(audio_array)))
                logger.info(
                    "Pre-transcription audio: samples=%d, RMS=%.4f, Peak=%.4f",
                    len(audio_array),
                    rms,
                    peak,
                )
                # Save diagnostic WAV for debugging audio issues
                import time as _time
                import wave as _wave

                _diag_path = f"/tmp/gateway_audio_{int(_time.time())}.wav"
                with _wave.open(_diag_path, "wb") as _wf:
                    _wf.setnchannels(buf.channels)
                    _wf.setsampwidth(buf.sample_width)
                    _wf.setframerate(buf.sample_rate)
                    _wf.writeframes((audio_array * 32768).astype(_np.int16).tobytes())
                logger.info(
                    "Diagnostic WAV saved: %s (sr=%d ch=%d)",
                    _diag_path,
                    buf.sample_rate,
                    buf.channels,
                )
            except Exception:
                pass  # diagnostic only — never block transcription
            text = await self._transcriber.transcribe(audio_array)
        except TranscriptionError as exc:
            _record_span_error(exc)
            logger.error("Transcription failed: %s", exc)
            self._state = SessionState.IDLE
            self._current_question = None
            self._task_start = None
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Transcription failed",
                    "code": ErrorCode.TRANSCRIPTION_FAILED,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return
        except TimeoutError as exc:
            _record_span_error(exc)
            logger.error("Transcription timed out")
            self._state = SessionState.IDLE
            self._current_question = None
            self._task_start = None
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
            self._current_question = None
            self._task_start = None
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "Internal transcription error",
                    "code": ErrorCode.INTERNAL_ERROR,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return

        # Skip OpenClaw when transcription is empty (silence / no speech)
        if not text.strip():
            logger.debug("Empty transcription — no speech detected")
            self._state = SessionState.IDLE
            self._current_question = None
            self._task_start = None
            await self.send_frame({"type": "transcription", "text": ""})
            await self.send_frame(
                {
                    "type": "error",
                    "detail": "No speech detected — try again",
                    "code": ErrorCode.TRANSCRIPTION_FAILED,
                }
            )
            await self.send_frame({"type": "status", "status": "idle"})
            return

        gateway_metrics.record_transcription_duration(
            _time_module.monotonic() - _transcription_start
        )

        # Send transcription to client and return to idle for user confirmation
        await self.send_frame({"type": "transcription", "text": text})
        self._state = SessionState.IDLE
        await self.send_frame({"type": "status", "status": "idle"})

    async def _handle_text(self, frame: dict[str, Any]) -> None:
        with _span("gateway.handle_text", message_length=len(frame["message"])):
            await self._handle_text_inner(frame)

    async def _handle_text_inner(self, frame: dict[str, Any]) -> None:
        # Quick command intercept — handle short steering commands locally
        quick = await self._try_quick_command(frame["message"])
        if quick:
            return

        self._current_question = frame["message"]
        self._task_start = asyncio.get_running_loop().time()
        _openclaw_start = _time_module.monotonic()

        # Clear any stale inflight buffer from a previous request
        if self._server is not None:
            await self._server._discard_inflight()

        self._state = SessionState.THINKING
        await self.send_frame({"type": "status", "status": "thinking"})

        # If handler supports start_stream, use the buffered background path
        if self._server is not None and hasattr(self._handler, "start_stream"):
            try:
                stream = await asyncio.wait_for(
                    self._handler.start_stream(frame["message"]),
                    timeout=self._timeout,
                )
            except TimeoutError as exc:
                _record_span_error(exc)
                gateway_metrics.record_openclaw_error()
                logger.error("OpenClaw request timed out")
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
                self._state = SessionState.IDLE
                self._current_question = None
                self._task_start = None
                try:
                    await self.send_frame({"type": "status", "status": "idle"})
                except Exception:
                    logger.debug("Failed to send idle status on cleanup", exc_info=True)
                return
            except OpenClawError as exc:
                _record_span_error(exc)
                gateway_metrics.record_openclaw_error()
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
                self._state = SessionState.IDLE
                self._current_question = None
                self._task_start = None
                try:
                    await self.send_frame({"type": "status", "status": "idle"})
                except Exception:
                    logger.debug("Failed to send idle status on cleanup", exc_info=True)
                return
            except Exception as exc:
                _record_span_error(exc)
                gateway_metrics.record_openclaw_error()
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
                self._state = SessionState.IDLE
                self._current_question = None
                self._task_start = None
                try:
                    await self.send_frame({"type": "status", "status": "idle"})
                except Exception:
                    logger.debug("Failed to send idle status on cleanup", exc_info=True)
                return

            # Stream obtained — transition to streaming and start background task
            self._state = SessionState.STREAMING
            await self.send_frame({"type": "status", "status": "streaming"})

            buffer = InflightBuffer(user_question=frame["message"])
            self._server._inflight_buffer = buffer
            self._server._inflight_task = asyncio.create_task(
                self._server._run_inflight_stream(stream, buffer, _openclaw_start),
                name="inflight-stream",
            )
            # Don't await — task runs independently. Session stays in STREAMING.
            # The background task will transition to IDLE when done.
            return

        # Fallback: original synchronous path for mock/other handlers
        try:
            await asyncio.wait_for(
                self._handler.handle(frame["message"], self.send_frame),
                timeout=self._timeout,
            )
        except TimeoutError:
            gateway_metrics.record_openclaw_error()
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
            gateway_metrics.record_openclaw_error()
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
            gateway_metrics.record_openclaw_error()
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
            gateway_metrics.record_openclaw_request_duration(
                _time_module.monotonic() - _openclaw_start
            )
            self._state = SessionState.IDLE
            self._current_question = None
            self._task_start = None
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
        self._inflight_buffer: InflightBuffer | None = None
        self._inflight_task: asyncio.Task[None] | None = None
        self._session_key: str = "agent:claw:g2"
        self._session_date: str = _today_utc()
        self._pending_reset_reason: str | None = None
        self._copilot_watcher: CopilotSessionWatcher | None = None
        self._copilot_watch_task: asyncio.Task[None] | None = None
        self._process_monitor: CopilotProcessMonitor | None = None
        self._process_monitor_task: asyncio.Task[None] | None = None

        if config.gateway_token is None:
            raise ValueError("GatewayServer requires GATEWAY_TOKEN")

        if handler is not None:
            self._handler: ResponseHandler = handler
        elif config.openclaw_gateway_token:
            client = OpenClawClient(
                host=config.openclaw_host,
                port=config.openclaw_port,
                token=config.openclaw_gateway_token,
            )
            self._handler = OpenClawResponseHandler(
                client, get_session_key=lambda: self._session_key
            )
        else:
            raise ValueError(
                "GatewayServer requires OPENCLAW_GATEWAY_TOKEN or an explicit test handler"
            )

    async def handler(self, ws: ServerConnection) -> None:
        """Handle a new WebSocket connection."""
        gateway_metrics.connection_opened()
        try:
            with _span("gateway.ws_connection"):
                await self._handler_inner(ws)
        finally:
            gateway_metrics.connection_closed()

    async def _handler_inner(self, ws: ServerConnection) -> None:
        """Inner implementation of WebSocket connection handler."""
        # --- token auth ---
        if self.config.gateway_token:
            # Rate limit check
            remote_ip = ws.remote_address[0] if ws.remote_address else "unknown"
            if _check_auth_rate_limit(remote_ip):
                logger.warning("Auth rate limit exceeded for %s", remote_ip)
                await ws.close(4029, "Too Many Requests")
                return

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
                _record_auth_failure(remote_ip)
                logger.warning("Auth failed for %s", remote_ip)
                await ws.close(4001, "Unauthorized")
                return

        # --- replace existing session (single-connection model) ---
        # Check for daily session reset before creating session
        await self._check_daily_reset()

        session = GatewaySession(
            ws,
            self._handler,
            self._transcriber,
            timeout=self.config.agent_timeout,
            local_audio=self.config.local_audio,
            history_limit=self.config.history_limit,
            session_key=self._session_key,
            agent_id=self.config.openclaw_agent_id,
            server=self,
        )
        session._on_ready = lambda: self._on_session_ready(session)
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
            # Clean up local audio stream on disconnect
            session._stop_local_stream()
            await self._stop_copilot_watch()
            if self._current_session is session:
                self._current_session = None
            # NOTE: Do NOT cancel _inflight_task — it must finish draining

    async def reset_session(self, reason: str) -> None:
        """Generate a new session key and notify the connected client."""
        old_key = self._session_key
        self._session_key = _generate_session_key()
        self._session_date = _today_utc()
        logger.info("Session reset (%s): %s → %s", reason, old_key, self._session_key)

        await self._discard_inflight()
        await self._handler.close()

        if self._current_session is not None:
            self._current_session._session_key = self._session_key
            try:
                await self._current_session.send_frame(
                    {
                        "type": "session_reset",
                        "reason": reason,
                    }
                )
            except Exception:
                logger.debug("Failed to send session_reset frame", exc_info=True)

    async def handle_copilot_watch(self, session_id: str) -> None:
        """Start watching a Copilot CLI session's transcript."""
        if "/" in session_id or ".." in session_id:
            session = self._current_session
            if session is not None:
                await session.send_frame(
                    {
                        "type": "error",
                        "detail": "Invalid session ID",
                        "code": ErrorCode.INVALID_FRAME,
                    }
                )
            return

        await self._stop_copilot_watch()

        session_dir = Path.home() / ".copilot" / "session-state" / session_id
        if not session_dir.exists():
            session = self._current_session
            if session is not None:
                await session.send_frame(
                    {
                        "type": "error",
                        "detail": f"Copilot session not found: {session_id}",
                        "code": ErrorCode.INTERNAL_ERROR,
                    }
                )
            return

        watcher = CopilotSessionWatcher(session_dir)
        self._copilot_watcher = watcher

        # Send initial history
        history = watcher.read_recent_history(limit=20)
        session = self._current_session
        if session is not None:
            await session.send_frame(
                {
                    "type": "copilot_history",
                    "sessionId": session_id,
                    "entries": history,
                }
            )

        # Start background tail task
        self._copilot_watch_task = asyncio.create_task(
            self._run_copilot_stream(watcher, session_id)
        )

    async def handle_copilot_unwatch(self) -> None:
        """Stop watching the current Copilot CLI session."""
        await self._stop_copilot_watch()

    async def _run_copilot_stream(self, watcher: CopilotSessionWatcher, session_id: str) -> None:
        """Background task: tail events.jsonl, forward events to client."""
        try:
            async for event in watcher.stream_events():
                session = self._current_session
                if session is None:
                    break
                await session.send_frame(
                    {
                        "type": "copilot_transcript",
                        "sessionId": session_id,
                        "delta": event["text"],
                        "role": event["role"],
                    }
                )
        except asyncio.CancelledError:
            pass
        finally:
            session = self._current_session
            if session is not None:
                try:
                    await session.send_frame(
                        {
                            "type": "copilot_transcript_end",
                            "sessionId": session_id,
                        }
                    )
                except Exception:
                    logger.debug("Failed to send copilot_transcript_end", exc_info=True)

    async def _stop_copilot_watch(self) -> None:
        """Stop any active copilot watcher and cancel the tail task."""
        if self._copilot_watcher is not None:
            self._copilot_watcher.stop()
        if self._copilot_watch_task is not None and not self._copilot_watch_task.done():
            self._copilot_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._copilot_watch_task
        self._copilot_watcher = None
        self._copilot_watch_task = None

    async def force_stop(self) -> None:
        """Hard-kill: abort inflight stream, close OpenClaw connection, reset session."""
        logger.info("Force stop requested")
        await self._discard_inflight()
        await self._handler.close()
        old_key = self._session_key
        self._session_key = _generate_session_key()
        self._session_date = _today_utc()
        logger.info("Force stop: session reset %s → %s", old_key, self._session_key)

        if self._current_session is not None:
            self._current_session._session_key = self._session_key
            self._current_session._state = SessionState.IDLE
            self._current_session._current_question = None
            self._current_session._task_start = None
            try:
                await self._current_session.send_frame(
                    {
                        "type": "session_reset",
                        "reason": "force_stop",
                    }
                )
            except Exception:
                logger.debug("Failed to send session_reset after force_stop", exc_info=True)

    async def switch_session(self, target_key: str) -> None:
        """Switch the active session to an existing session key.

        Validates the key exists in sessions.json before switching.
        Discards any inflight buffer, closes the OpenClaw client connection
        (forcing reconnect on next message), sends session_switched + history.
        """
        # Early return if already on this session — skip expensive reconnect
        if target_key == self._session_key:
            session = self._current_session
            if session is not None:
                meta = resolve_session(
                    session_key=target_key, agent_id=self.config.openclaw_agent_id
                )
                switched_frame: dict[str, Any] = {
                    "type": "session_switched",
                    "sessionKey": target_key,
                }
                if meta is not None:
                    if meta.session_id:
                        switched_frame["sessionId"] = meta.session_id
                    if meta.updated_at:
                        switched_frame["sessionStartedAt"] = meta.updated_at
                await session.send_frame(switched_frame)
            return

        meta = resolve_session(session_key=target_key, agent_id=self.config.openclaw_agent_id)
        if meta is None:
            if self._current_session is not None:
                await self._current_session.send_frame(
                    {
                        "type": "error",
                        "detail": f"Session not found: {target_key}",
                        "code": ErrorCode.INVALID_FRAME,
                    }
                )
            return

        old_key = self._session_key
        self._session_key = target_key
        logger.info("Session switch: %s → %s", old_key, target_key)

        await self._discard_inflight()
        await self._handler.close()

        session = self._current_session
        if session is not None:
            session._session_key = target_key

            sw_frame: dict[str, Any] = {
                "type": "session_switched",
                "sessionKey": meta.session_key,
            }
            if meta.session_id:
                sw_frame["sessionId"] = meta.session_id
            if meta.updated_at:
                sw_frame["sessionStartedAt"] = meta.updated_at
            await session.send_frame(sw_frame)

            await session._send_history()

    async def create_session(self) -> None:
        """Generate a new session key and switch to it.

        The session won't appear in sessions.json until the first OpenClaw
        message is sent (OpenClaw creates the entry on first use).
        """
        new_key = _generate_session_key()
        old_key = self._session_key
        self._session_key = new_key
        logger.info("Session created: %s (was %s)", new_key, old_key)

        await self._discard_inflight()
        await self._handler.close()

        session = self._current_session
        if session is not None:
            session._session_key = new_key
            await session.send_frame(
                {
                    "type": "session_switched",
                    "sessionKey": new_key,
                }
            )
            await session.send_frame(
                {
                    "type": "history",
                    "entries": [],
                }
            )

    async def _check_daily_reset(self) -> None:
        """Check if the date has rolled over since the last interaction."""
        today = _today_utc()
        if today != self._session_date:
            logger.info(
                "Date rolled over: %s → %s — triggering daily reset",
                self._session_date,
                today,
            )
            old_key = self._session_key
            self._session_key = _generate_session_key()
            self._session_date = today
            await self._handler.close()
            self._pending_reset_reason = "daily_reset"
            logger.info("Daily session reset: %s → %s", old_key, self._session_key)

    async def _on_session_ready(self, session: GatewaySession) -> None:
        """Called after session sends connected+history+idle."""
        if session is not self._current_session:
            return

        # Send pending reset notification
        if self._pending_reset_reason is not None:
            try:
                await session.send_frame(
                    {
                        "type": "session_reset",
                        "reason": self._pending_reset_reason,
                    }
                )
            except Exception:
                logger.debug("Failed to send pending session_reset", exc_info=True)
            self._pending_reset_reason = None

        # Replay inflight buffer
        try:
            await self._replay_inflight(session)
        except Exception:
            logger.exception("Failed to replay inflight buffer")
            self._inflight_buffer = None

    async def _run_inflight_stream(
        self,
        stream: AsyncIterator[str],
        buffer: InflightBuffer,
        openclaw_start: float,
    ) -> None:
        """Consume the OpenClaw stream into buffer, forwarding to phone if connected.

        Safety: all state mutations here are safe because asyncio.create_task
        runs on the same event loop — there are no true concurrent accesses,
        only interleaved execution at await points.
        """
        with _span("gateway.inflight_stream"):
            await self._run_inflight_stream_inner(stream, buffer, openclaw_start)

    async def _run_inflight_stream_inner(
        self,
        stream: AsyncIterator[str],
        buffer: InflightBuffer,
        openclaw_start: float,
    ) -> None:
        try:
            _STREAM_LOG.parent.mkdir(exist_ok=True)
            with _STREAM_LOG.open("w") as f:
                f.write(
                    f"--- {buffer.user_question} "
                    f"[{_time_module.strftime('%Y-%m-%dT%H:%M:%SZ', _time_module.gmtime())}] ---\n"
                )
            stream_iter = aiter(stream)
            while True:
                remaining = self.config.agent_timeout - (_time_module.monotonic() - openclaw_start)
                if remaining <= 0:
                    raise TimeoutError(f"Agent cycle exceeded {self.config.agent_timeout}s timeout")
                try:
                    delta = await asyncio.wait_for(anext(stream_iter), timeout=remaining)
                except StopAsyncIteration:
                    break

                if not buffer.append_delta(delta):
                    logger.warning(
                        "Inflight buffer exceeded %d chars — truncating", _BUFFER_MAX_CHARS
                    )

                with _STREAM_LOG.open("a") as f:
                    f.write(delta)

                session = self._current_session
                if session is not None:
                    try:
                        await session.send_frame({"type": "assistant", "delta": delta})
                    except Exception:
                        logger.info("Phone disconnected mid-stream — continuing to buffer")
            buffer.complete = True
        except TimeoutError as exc:
            gateway_metrics.record_openclaw_error()
            buffer.error_code = ErrorCode.TIMEOUT
            buffer.error = str(exc) or f"Agent cycle exceeded {self.config.agent_timeout}s timeout"
            logger.error("OpenClaw inflight stream timed out: %s", buffer.error)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._handler.close(), timeout=1.0)
        except OpenClawError as exc:
            gateway_metrics.record_openclaw_error()
            buffer.error = str(exc)
            buffer.error_code = ErrorCode.OPENCLAW_ERROR
            logger.error("OpenClaw error during inflight stream: %s", exc)
        except asyncio.CancelledError:
            raise  # let cancellation propagate
        except Exception:
            gateway_metrics.record_openclaw_error()
            buffer.error = "internal error"
            logger.exception("Unexpected error during inflight stream")
        finally:
            gateway_metrics.record_openclaw_request_duration(
                _time_module.monotonic() - openclaw_start
            )
            with contextlib.suppress(OSError), _STREAM_LOG.open("a") as f:
                f.write("\n--- END ---\n")
            session = self._current_session
            if session is not None and buffer.complete and self._inflight_buffer is buffer:
                try:
                    await session.send_frame({"type": "end"})
                    session._state = SessionState.IDLE
                    session._current_question = None
                    session._task_start = None
                    await session.send_frame({"type": "status", "status": "idle"})
                except Exception:
                    pass
            elif session is not None and buffer.error and self._inflight_buffer is buffer:
                try:
                    await session.send_frame(
                        {
                            "type": "error",
                            "detail": f"Agent error: {buffer.error}",
                            "code": buffer.error_code,
                        }
                    )
                    session._state = SessionState.IDLE
                    session._current_question = None
                    session._task_start = None
                    await session.send_frame({"type": "status", "status": "idle"})
                except Exception:
                    pass

            if (
                session is not None
                and (buffer.complete or buffer.error)
                and self._inflight_buffer is buffer
            ):
                self._inflight_buffer = None
            self._inflight_task = None

    async def _replay_inflight(self, session: GatewaySession) -> None:
        """Replay a buffered inflight response to the reconnected phone."""
        buf = self._inflight_buffer
        if buf is None or buf.expired:
            self._inflight_buffer = None
            return

        if buf.error:
            await session.send_frame(
                {
                    "type": "error",
                    "detail": f"Previous response failed: {buf.error}",
                    "code": ErrorCode.OPENCLAW_ERROR,
                }
            )
            session._state = SessionState.IDLE
            session._current_question = None
            session._task_start = None
            self._inflight_buffer = None
            return

        if buf.complete:
            await session.send_frame({"type": "status", "status": "streaming"})
            session._state = SessionState.STREAMING
            await session.send_frame({"type": "assistant", "delta": buf.full_text})
            await session.send_frame({"type": "end"})
            session._state = SessionState.IDLE
            session._current_question = None
            session._task_start = None
            await session.send_frame({"type": "status", "status": "idle"})
            self._inflight_buffer = None
            return

        # Still streaming — splice
        await self._splice_inflight(session, buf)

    async def _splice_inflight(self, session: GatewaySession, buf: InflightBuffer) -> None:
        """Splice buffered deltas with the live stream for a reconnecting phone."""
        await session.send_frame({"type": "status", "status": "streaming"})
        session._state = SessionState.STREAMING
        session._current_question = buf.user_question
        session._task_start = asyncio.get_running_loop().time()
        if buf.full_text:
            await session.send_frame({"type": "assistant", "delta": buf.full_text})
        # The background task will forward new deltas to the new _current_session

    async def _discard_inflight(self) -> None:
        """Cancel any in-flight stream and discard the buffer.

        Clears references BEFORE cancelling to prevent the cancelled task's
        finally block from clobbering a replacement task.
        """
        old_task = self._inflight_task
        self._inflight_task = None
        self._inflight_buffer = None
        if old_task is not None and not old_task.done():
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_task

    async def _process_request(
        self,
        connection: ServerConnection,
        request: websockets.http11.Request,
    ) -> websockets.http11.Response | None:
        """Handle HTTP requests before WebSocket upgrade.

        Returns an HTTP response for /healthz; returns None for
        all other paths to let the WebSocket handshake proceed.
        """
        if request.path == "/healthz":
            return connection.respond(200, "OK\n")
        if request.path == "/stream":
            buf = self._inflight_buffer
            body = buf.full_text if buf is not None else "No active stream"
            return connection.respond(200, body)
        if request.path == "/abort":
            had_stream = self._inflight_buffer is not None
            if had_stream:
                await self._discard_inflight()
                session = self._current_session
                if session is not None:
                    session._state = SessionState.IDLE
                    session._current_question = None
                    session._task_start = None
                    try:
                        await session.send_frame(
                            {
                                "type": "error",
                                "detail": "Stream aborted via /abort",
                                "code": ErrorCode.OPENCLAW_ERROR,
                            }
                        )
                        await session.send_frame({"type": "status", "status": "idle"})
                    except Exception:
                        pass
                return connection.respond(200, "Aborted\n")
            return connection.respond(200, "No active stream\n")
        return None

    async def serve(self) -> None:
        """Start the server and run forever."""
        logger.info(
            "Gateway listening on %s:%s",
            self.config.gateway_host,
            self.config.gateway_port,
        )
        serve_kwargs: dict[str, object] = {
            "max_size": 2**16,  # 64 KiB max frame size
            "process_request": self._process_request,
        }
        if self.config.allowed_origins is not None:
            serve_kwargs["origins"] = self.config.allowed_origins
        async with websockets.serve(
            self.handler,
            self.config.gateway_host,
            self.config.gateway_port,
            **serve_kwargs,  # type: ignore[arg-type]
        ):
            await self._start_process_monitor()
            try:
                await asyncio.Future()  # block forever
            finally:
                await self._stop_process_monitor()

    async def _start_process_monitor(self) -> None:
        """Start the background Copilot process monitor."""
        if not self.config.openclaw_gateway_token:
            raise RuntimeError("OPENCLAW_GATEWAY_TOKEN is required for process monitoring")

        monitor_client = OpenClawClient(
            host=self.config.openclaw_host,
            port=self.config.openclaw_port,
            token=self.config.openclaw_gateway_token,
        )

        async def notify(message: str) -> None:
            """Send a notification to OpenClaw with retry on failure."""
            key = self._session_key  # snapshot before retries
            await _notify_openclaw_with_retry(monitor_client, message, key)

        self._process_monitor = CopilotProcessMonitor(notify_callback=notify)
        self._process_monitor_task = asyncio.create_task(self._process_monitor.run())
        logger.info("Copilot process monitor started")

    async def _stop_process_monitor(self) -> None:
        """Stop the background process monitor."""
        if self._process_monitor is not None:
            self._process_monitor.stop()
        if self._process_monitor_task is not None:
            self._process_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._process_monitor_task


def _setup_cuda_library_paths() -> None:
    """Pre-load CUDA shared libraries so ctranslate2/faster-whisper can find them.

    Setting LD_LIBRARY_PATH after process start has no effect on dlopen(),
    so we use ctypes.CDLL with RTLD_GLOBAL to make the symbols available
    before ctranslate2 is imported.
    """
    import ctypes

    search_roots = [
        # 1. Current venv site-packages
        *sorted(Path(sys.prefix).glob("lib/python*/site-packages/nvidia")),
        # 2. uv cache
        Path.home() / ".cache" / "uv",
        # 3. System CUDA installs
        *sorted(Path("/usr/local").glob("cuda*/lib64")),
        # 4. Distro multiarch lib dir
        Path("/usr/lib/x86_64-linux-gnu"),
    ]

    # Libraries to pre-load (order matters — cublas before cudnn)
    targets = ["libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9"]

    for lib_name in targets:
        # Check if already loadable
        try:
            ctypes.CDLL(lib_name)
            logger.info("CUDA lib %s: already loadable", lib_name)
            continue
        except OSError:
            pass

        # Search for the library file
        found = False
        for root in search_roots:
            if not root.exists() or root.is_file():
                continue
            matches = list(root.rglob(lib_name))
            if matches:
                lib_path = matches[0]
                try:
                    ctypes.CDLL(str(lib_path), mode=ctypes.RTLD_GLOBAL)
                    logger.info("CUDA lib %s pre-loaded from %s", lib_name, lib_path)
                    found = True
                except OSError as exc:
                    logger.warning(
                        "CUDA lib %s found at %s but failed to load: %s", lib_name, lib_path, exc
                    )
                break

        if not found:
            logger.warning("CUDA lib %s not found — GPU transcription may fail", lib_name)


async def main() -> None:
    """Entry point: load config, initialise transcriber, and start serving."""
    # Logging is configured by init_otel() (called from cli.py).
    # If running directly (not via CLI), set up basic logging.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    config = load_config()

    if config.local_audio:
        logger.info("Local audio capture mode ENABLED — mic audio captured by gateway")

    _setup_cuda_library_paths()

    transcriber = Transcriber(
        config.whisper_model, config.whisper_device, config.whisper_compute_type
    )
    logger.info("Transcriber loaded (model=%s)", config.whisper_model)

    server = GatewayServer(config, transcriber=transcriber)
    await server.serve()
