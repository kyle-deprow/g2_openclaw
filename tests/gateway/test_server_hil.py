"""Tests for HIL (human-in-the-loop) TTS flow in server.py."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from gateway.server import GatewaySession, SessionState

pytestmark = pytest.mark.asyncio


class FakeWebSocket:
    """Fake WebSocket for testing."""

    def __init__(self, messages: list[str | bytes] | None = None) -> None:
        self._messages: list[str | bytes] = messages or []
        self._sent: list[str | bytes] = []

    @property
    def sent_frames(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self._sent if isinstance(m, str)]

    async def send(self, data: str | bytes) -> None:
        self._sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

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
    """Mock transcriber that returns configurable result."""

    def __init__(self, result: str = "test transcription") -> None:
        self._result = result
        self._calls: list[np.ndarray] = []

    async def transcribe(
        self, audio: np.ndarray, language: str = "en", timeout: float = 30.0
    ) -> str:
        self._calls.append(audio)
        return self._result


def _start_audio_frame() -> str:
    return json.dumps(
        {
            "type": "start_audio",
            "sampleRate": 16000,
            "channels": 1,
            "sampleWidth": 2,
        }
    )


def _stop_audio_frame_hil(text: str) -> str:
    return json.dumps({"type": "stop_audio", "hilText": text})


class TestHilTtsFlow:
    """Test HIL flow where stop_audio carries hilText for TTS."""

    async def test_hil_stop_audio_with_text_runs_tts_and_transcription(self) -> None:
        """stop_audio with hilText → TTS → Whisper → transcription → idle."""
        transcriber = MockTranscriber(result="hello world via tts")
        ws = FakeWebSocket(
            messages=[
                _start_audio_frame(),
                _stop_audio_frame_hil("hello world"),
            ]
        )
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        types = [f["type"] for f in frames]

        # Should contain transcription
        assert "transcription" in types
        transcription = next(f for f in frames if f["type"] == "transcription")
        assert transcription["text"] == "hello world via tts"

        # Should have recording → transcribing → idle (not thinking!)
        statuses = [f["status"] for f in frames if f["type"] == "status"]
        assert "recording" in statuses
        assert "transcribing" in statuses
        assert statuses[-1] == "idle"
        assert "thinking" not in statuses

        # Transcriber should have been called
        assert len(transcriber._calls) == 1

    async def test_stop_audio_without_hil_uses_real_buffer(self) -> None:
        """stop_audio without hilText uses the real audio buffer (production path)."""
        transcriber = MockTranscriber(result="real audio result")
        pcm = b"\x00" * 3200
        ws = FakeWebSocket(
            messages=[
                _start_audio_frame(),
                pcm,
                json.dumps({"type": "stop_audio"}),
            ]
        )
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        transcription = next(f for f in frames if f["type"] == "transcription")
        assert transcription["text"] == "real audio result"

        # Should end at idle (not thinking)
        statuses = [f["status"] for f in frames if f["type"] == "status"]
        assert statuses[-1] == "idle"
        assert "thinking" not in statuses

    async def test_stop_audio_returns_to_idle_not_thinking(self) -> None:
        """After transcription, session goes to idle (user must confirm)."""
        transcriber = MockTranscriber(result="confirmed text")
        pcm = b"\x00" * 3200
        ws = FakeWebSocket(
            messages=[
                _start_audio_frame(),
                pcm,
                json.dumps({"type": "stop_audio"}),
            ]
        )
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        # Session should be idle
        assert session._state == SessionState.IDLE

        frames = ws.sent_frames
        statuses = [f["status"] for f in frames if f["type"] == "status"]
        # Last status must be idle
        assert statuses[-1] == "idle"
        # Must NOT contain thinking (that only happens when user confirms with text frame)
        assert "thinking" not in statuses


class TestConfirmFlow:
    """After transcription → idle, user sends text frame to confirm."""

    async def test_text_after_transcription_triggers_openclaw(self) -> None:
        """User confirms by sending text frame → thinking → streaming → idle."""
        transcriber = MockTranscriber(result="hello")
        pcm = b"\x00" * 3200
        ws = FakeWebSocket(
            messages=[
                _start_audio_frame(),
                pcm,
                json.dumps({"type": "stop_audio"}),
                json.dumps({"type": "text", "message": "hello"}),
            ]
        )
        session = GatewaySession(ws, transcriber=transcriber)  # type: ignore[arg-type]
        await session.handle()

        frames = ws.sent_frames
        statuses = [f["status"] for f in frames if f["type"] == "status"]

        # Full flow: idle → recording → transcribing → idle → thinking → streaming → idle
        assert "recording" in statuses
        assert "transcribing" in statuses
        assert "thinking" in statuses
        assert "streaming" in statuses
        assert statuses[-1] == "idle"
