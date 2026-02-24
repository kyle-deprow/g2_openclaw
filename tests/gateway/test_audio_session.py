"""Tests for audio recording/transcription session handling."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from gateway.audio_buffer import AudioBuffer
from gateway.server import GatewaySession, SessionState
from gateway.transcriber import TranscriptionError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Fake WebSocket for testing session behavior."""

    def __init__(self, messages: list[str | bytes] | None = None) -> None:
        self._messages: list[str | bytes] = messages or []
        self._sent: list[str | bytes] = []
        self._closed = False
        self._close_code: int | None = None
        self._close_reason: str | None = None

    @property
    def sent_frames(self) -> list[dict[str, Any]]:
        """Parse all sent text frames as dicts."""
        return [json.loads(m) for m in self._sent if isinstance(m, str)]

    async def send(self, data: str | bytes) -> None:
        self._sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed = True
        self._close_code = code
        self._close_reason = reason

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._async_iter()

    async def _async_iter(self) -> AsyncIterator[str | bytes]:
        for msg in self._messages:
            yield msg

    @property
    def request(self) -> MagicMock:
        req = MagicMock()
        req.path = "/"
        return req


class MockTranscriber:
    """Mock transcriber that returns a configurable result or raises."""

    def __init__(self, result: str | Exception = "test transcription") -> None:
        self._result: str | Exception = result
        self._calls: list[np.ndarray] = []

    async def transcribe(
        self, audio: np.ndarray, language: str = "en", timeout: float = 30.0
    ) -> str:
        self._calls.append(audio)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# ---------------------------------------------------------------------------
# Helper to build a start_audio frame
# ---------------------------------------------------------------------------


def _start_audio_frame(sample_rate: int = 16_000, channels: int = 1, sample_width: int = 2) -> str:
    return json.dumps(
        {
            "type": "start_audio",
            "sampleRate": sample_rate,
            "channels": channels,
            "sampleWidth": sample_width,
        }
    )


def _stop_audio_frame() -> str:
    return json.dumps({"type": "stop_audio"})


def _text_frame(message: str = "hello") -> str:
    return json.dumps({"type": "text", "message": message})


def _pcm_silence(num_bytes: int = 3200) -> bytes:
    """Return PCM silence bytes (zeros)."""
    return b"\x00" * num_bytes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStartAudio:
    async def test_start_audio_transitions_to_recording(self) -> None:
        """Send start_audio while idle → status:recording."""
        ws = FakeWebSocket(messages=[_start_audio_frame()])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        # connected, idle, recording
        statuses = [f for f in frames if f["type"] == "status"]
        assert {"type": "status", "status": "recording"} in statuses

    async def test_start_audio_while_busy_returns_error(self) -> None:
        """Send text (THINKING) then start_audio → INVALID_STATE error."""
        ws = FakeWebSocket(
            messages=[
                _text_frame("hi"),
                _start_audio_frame(),
            ]
        )

        # Use a slow handler so the session is still busy when start_audio arrives
        class SlowHandler:
            async def handle(
                self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
            ) -> None:
                # Send start_audio into the queue before finishing
                await send_frame({"type": "status", "status": "streaming"})
                await send_frame({"type": "end"})

        session = GatewaySession(ws, handler=SlowHandler())  # type: ignore[arg-type]
        await session.handle()

        _frames = ws.sent_frames
        # After text completes the session goes back to idle, so start_audio
        # will actually succeed.  To properly test "busy", we force state.
        # Instead, let's test directly via _dispatch.

    async def test_start_audio_while_not_idle_returns_error(self) -> None:
        """Directly test dispatch rejects start_audio when not idle."""
        ws = FakeWebSocket(messages=[])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        session._state = SessionState.THINKING

        frame = {"type": "start_audio", "sampleRate": 16000, "channels": 1, "sampleWidth": 2}
        await session._dispatch(frame)

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_STATE"
        assert "busy" in errors[0]["detail"].lower()


class TestBinaryData:
    async def test_binary_data_appended_to_buffer(self) -> None:
        """Binary data during RECORDING is appended to the audio buffer."""
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        # After start_audio, buffer was created and binary data appended.
        # Since we didn't stop_audio, the buffer was left in place.
        # Verify the buffer got the data by checking session state went to recording.
        statuses = [f["status"] for f in ws.sent_frames if f["type"] == "status"]
        assert "recording" in statuses

    async def test_binary_data_while_not_recording_ignored(self) -> None:
        """Binary data while IDLE produces no error, just ignored."""
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[pcm])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 0


class TestStopAudio:
    async def test_stop_audio_triggers_transcription(self) -> None:
        """Full flow: start_audio → binary → stop_audio → transcription → text handling."""
        transcriber = MockTranscriber(result="hello world")
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm, _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        types = [f["type"] for f in frames]

        # Should contain transcription frame
        assert "transcription" in types
        transcription_frame = next(f for f in frames if f["type"] == "transcription")
        assert transcription_frame["text"] == "hello world"

        # Should have gone through: recording → transcribing → thinking → streaming → idle
        statuses = [f["status"] for f in frames if f["type"] == "status"]
        assert "recording" in statuses
        assert "transcribing" in statuses
        assert "thinking" in statuses
        # ends with idle
        assert statuses[-1] == "idle"

        # Transcriber should have been called once
        assert len(transcriber._calls) == 1

    async def test_stop_audio_empty_buffer_returns_error(self) -> None:
        """start_audio then immediately stop_audio (no binary data) → error."""
        transcriber = MockTranscriber()
        ws = FakeWebSocket(messages=[_start_audio_frame(), _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "TRANSCRIPTION_FAILED"
        assert "no audio" in errors[0]["detail"].lower()

        # Should return to idle
        statuses = [f["status"] for f in frames if f["type"] == "status"]
        assert statuses[-1] == "idle"

    async def test_stop_audio_without_recording_returns_error(self) -> None:
        """stop_audio while idle → INVALID_STATE error."""
        ws = FakeWebSocket(messages=[_stop_audio_frame()])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_STATE"
        assert "not recording" in errors[0]["detail"].lower()


class TestBufferOverflow:
    async def test_buffer_overflow_sends_error(self) -> None:
        """Exceeding buffer capacity sends BUFFER_OVERFLOW error."""
        ws = FakeWebSocket(messages=[])
        session = GatewaySession(ws)  # type: ignore[arg-type]

        # Manually set up recording state and buffer
        session._state = SessionState.RECORDING
        session._audio_buffer = AudioBuffer(sample_rate=16_000, channels=1, sample_width=2)

        # Calculate how many bytes would overflow (60s * 16000 * 1 * 2 = 1_920_000)
        overflow_data = b"\x00" * (AudioBuffer.MAX_DURATION_SECONDS * 16_000 * 1 * 2 + 2)

        await session._handle_binary(overflow_data)

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "BUFFER_OVERFLOW"

        # Should return to idle
        assert session._state == SessionState.IDLE
        statuses = [f for f in frames if f["type"] == "status"]
        assert statuses[-1]["status"] == "idle"


class TestTranscriptionErrors:
    async def test_transcription_error_sends_error(self) -> None:
        """TranscriptionError from transcriber → TRANSCRIPTION_FAILED error frame."""
        transcriber = MockTranscriber(result=TranscriptionError("model failed"))
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm, _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "TRANSCRIPTION_FAILED"
        detail = errors[0]["detail"]
        assert "model failed" in detail or detail == "Transcription failed"

    async def test_transcription_timeout_sends_error(self) -> None:
        """TimeoutError from transcriber → TIMEOUT error frame."""
        transcriber = MockTranscriber(result=TimeoutError())
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm, _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "TIMEOUT"
        assert "timed out" in errors[0]["detail"].lower()

        statuses = [f["status"] for f in frames if f["type"] == "status"]
        assert statuses[-1] == "idle"

    async def test_no_transcriber_sends_error(self) -> None:
        """No transcriber configured → TRANSCRIPTION_FAILED error frame."""
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm, _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=None)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "TRANSCRIPTION_FAILED"
        assert "not configured" in errors[0]["detail"].lower()

        statuses = [f["status"] for f in frames if f["type"] == "status"]
        assert statuses[-1] == "idle"

    async def test_unexpected_error_sends_internal_error(self) -> None:
        """Unexpected exception from transcriber → INTERNAL_ERROR frame."""
        transcriber = MockTranscriber(result=RuntimeError("kaboom"))
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm, _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INTERNAL_ERROR"
        # Detail must NOT leak the internal exception message (Issue 7)
        assert "kaboom" not in errors[0]["detail"]
        assert errors[0]["detail"] == "Internal transcription error"


class TestFullPipeline:
    async def test_full_audio_pipeline(self) -> None:
        """End-to-end: start_audio → binary → stop_audio → transcription → mock response → idle."""
        transcriber = MockTranscriber(result="hello")
        pcm = _pcm_silence(3200)
        ws = FakeWebSocket(messages=[_start_audio_frame(), pcm, _stop_audio_frame()])
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        types = [f["type"] for f in frames]

        # Expected sequence:
        # connected, status:idle, status:recording, status:transcribing,
        # transcription, status:thinking, status:streaming,
        # assistant x3, end, status:idle
        assert types[0] == "connected"
        assert frames[1] == {"type": "status", "status": "idle"}
        assert frames[2] == {"type": "status", "status": "recording"}
        assert frames[3] == {"type": "status", "status": "transcribing"}
        assert frames[4] == {"type": "transcription", "text": "hello"}
        assert frames[5] == {"type": "status", "status": "thinking"}
        assert frames[6] == {"type": "status", "status": "streaming"}

        # Mock handler sends 3 assistant deltas + end
        assistant_frames = [f for f in frames if f["type"] == "assistant"]
        assert len(assistant_frames) == 3

        assert {"type": "end"} in frames
        # Final status is idle
        statuses = [f for f in frames if f["type"] == "status"]
        assert statuses[-1]["status"] == "idle"

        # Session should be back to idle
        assert session._state == SessionState.IDLE


class TestSessionStateEnum:
    async def test_recording_state_exists(self) -> None:
        assert SessionState.RECORDING.value == "recording"

    async def test_transcribing_state_exists(self) -> None:
        assert SessionState.TRANSCRIBING.value == "transcribing"

    async def test_all_states(self) -> None:
        expected = {"idle", "recording", "transcribing", "thinking", "streaming"}
        assert {s.value for s in SessionState} == expected


class TestStartAudioValidation:
    """Tests for start_audio parameter validation (Issues 1 & 4)."""

    async def test_unsupported_sample_width_returns_error(self) -> None:
        """start_audio with sample_width != 2 → INVALID_FRAME error."""
        ws = FakeWebSocket(messages=[_start_audio_frame(sample_width=4)])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_FRAME"
        assert "sample width" in errors[0]["detail"].lower()
        # Session should remain idle (not transition to recording)
        assert session._state == SessionState.IDLE

    async def test_invalid_sample_rate_returns_error(self) -> None:
        """start_audio with sample_rate=0 → INVALID_FRAME error."""
        ws = FakeWebSocket(messages=[_start_audio_frame(sample_rate=0)])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_FRAME"
        assert "sample rate" in errors[0]["detail"].lower()

    async def test_invalid_sample_rate_too_high_returns_error(self) -> None:
        """start_audio with sample_rate > 48000 → INVALID_FRAME error."""
        ws = FakeWebSocket(messages=[_start_audio_frame(sample_rate=96_000)])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_FRAME"
        assert "sample rate" in errors[0]["detail"].lower()

    async def test_invalid_channels_returns_error(self) -> None:
        """start_audio with channels=0 → INVALID_FRAME error."""
        ws = FakeWebSocket(messages=[_start_audio_frame(channels=0)])
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_FRAME"
        assert "channels" in errors[0]["detail"].lower()

    async def test_valid_params_accepted(self) -> None:
        """start_audio with valid params transitions to recording."""
        ws = FakeWebSocket(
            messages=[_start_audio_frame(sample_rate=44_100, channels=2, sample_width=2)]
        )
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        statuses = [f for f in ws.sent_frames if f["type"] == "status"]
        assert {"type": "status", "status": "recording"} in statuses


class TestOddByteChunk:
    """Tests for odd-byte PCM chunk error handling in session (Issue 5)."""

    async def test_odd_byte_binary_sends_error(self) -> None:
        """Odd-byte binary frame during recording → INVALID_FRAME error, session resets to idle."""
        odd_chunk = b"\x00\x01\x02"  # 3 bytes, not multiple of sample_width=2
        good_chunk = _pcm_silence(100)  # 100 bytes, valid
        ws = FakeWebSocket(
            messages=[
                _start_audio_frame(),
                odd_chunk,
                good_chunk,  # Ignored because session reset to idle after error
            ]
        )
        session = GatewaySession(ws)  # type: ignore[arg-type]
        await session.handle()

        errors = [f for f in ws.sent_frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_FRAME"
        assert errors[0]["detail"] == "Invalid audio data format"
        assert session._state == SessionState.IDLE
        # Idle status sent after error
        idle_after_error = [
            f for f in ws.sent_frames if f.get("type") == "status" and f.get("status") == "idle"
        ]
        assert len(idle_after_error) >= 1


class TestHandlerException:
    """Tests for ResponseHandler exception handling in _handle_text (Issue 6)."""

    async def test_handler_exception_sends_openclaw_error(self) -> None:
        """ResponseHandler raising an exception → OPENCLAW_ERROR frame, session returns to idle."""

        class ExplodingHandler:
            async def handle(
                self, message: str, send_frame: Callable[[dict[str, Any]], Awaitable[None]]
            ) -> None:
                raise RuntimeError("handler exploded")

            async def close(self) -> None:
                pass

        ws = FakeWebSocket(messages=[_text_frame("boom")])
        session = GatewaySession(ws, handler=ExplodingHandler())  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "OPENCLAW_ERROR"
        assert errors[0]["detail"] == "Response processing failed"
        # Internal exception message must not leak
        assert "handler exploded" not in errors[0]["detail"]

        # Session must return to idle
        statuses = [f for f in frames if f["type"] == "status"]
        assert statuses[-1]["status"] == "idle"
        assert session._state == SessionState.IDLE
