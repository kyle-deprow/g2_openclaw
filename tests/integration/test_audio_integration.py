"""End-to-end audio integration tests for the G2 OpenClaw Gateway.

P2.8 — Proves: start_audio → binary PCM → stop_audio → transcription → response pipeline.
Uses a FakeTranscriber to avoid real Whisper dependency.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
import websockets

from gateway.config import GatewayConfig
from gateway.server import GatewayServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIMEOUT = 5.0


async def _recv(ws: websockets.ClientConnection) -> dict:
    """Receive a single JSON frame with a timeout guard."""
    raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
    return json.loads(raw)


async def _consume_handshake(ws: websockets.ClientConnection) -> tuple[dict, dict]:
    """Receive and return the (connected, status:idle) handshake pair."""
    connected = await _recv(ws)
    idle = await _recv(ws)
    return connected, idle


async def _send_json(ws: websockets.ClientConnection, frame: dict) -> None:
    """Send a JSON text frame."""
    await ws.send(json.dumps(frame))


async def _collect_until_idle(ws: websockets.ClientConnection) -> list[dict]:
    """Collect all frames until a status:idle frame is received."""
    frames: list[dict] = []
    while True:
        frame = await _recv(ws)
        frames.append(frame)
        if frame.get("type") == "status" and frame.get("status") == "idle":
            break
    return frames


# ---------------------------------------------------------------------------
# Fake transcriber
# ---------------------------------------------------------------------------


class FakeTranscriber:
    """Fake transcriber for integration tests — no real Whisper needed."""

    def __init__(self, result: str = "Hello world") -> None:
        self._result = result

    async def transcribe(
        self, audio, language: str = "en", timeout: float = 30.0  # noqa: ANN001
    ) -> str:
        await asyncio.sleep(0.05)
        return self._result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_TEXT = "Hello world"


@pytest_asyncio.fixture
async def audio_gateway():
    """Start a gateway server with a FakeTranscriber on an ephemeral port.

    Yields (ws_url, GatewayServer).
    """
    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token="audio-token",
    )
    fake_transcriber = FakeTranscriber(result=FAKE_TEXT)
    gw = GatewayServer(config, transcriber=fake_transcriber)
    server = await websockets.serve(gw.handler, config.gateway_host, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw
    finally:
        server.close()
        await server.wait_closed()


def _ws_url(base: str) -> str:
    return f"{base}?token=audio-token"


# Fake PCM: 200 bytes of 16-bit silence-ish data
FAKE_PCM = b"\x00\x01" * 100


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullAudioPipeline:
    """Happy-path: start_audio → binary PCM → stop_audio → full response sequence."""

    async def test_full_audio_pipeline(self, audio_gateway: tuple) -> None:
        url, _ = audio_gateway
        async with websockets.connect(_ws_url(url)) as ws:
            # Handshake
            connected, idle = await _consume_handshake(ws)
            assert connected == {"type": "connected", "version": "1.0"}
            assert idle == {"type": "status", "status": "idle"}

            # Start audio
            await _send_json(
                ws,
                {
                    "type": "start_audio",
                    "sampleRate": 16000,
                    "channels": 1,
                    "sampleWidth": 2,
                },
            )
            recording = await _recv(ws)
            assert recording == {"type": "status", "status": "recording"}

            # Send several binary frames of fake PCM
            for _ in range(5):
                await ws.send(FAKE_PCM)

            # Stop audio
            await _send_json(ws, {"type": "stop_audio"})

            # Collect all frames through status:idle
            frames = await _collect_until_idle(ws)

            # Extract frame types
            types = [f["type"] for f in frames]

            # Must see transcribing status
            assert {"type": "status", "status": "transcribing"} in frames
            # Must see transcription with fake text
            assert {"type": "transcription", "text": FAKE_TEXT} in frames
            # Must see thinking
            assert {"type": "status", "status": "thinking"} in frames
            # Must see streaming (from MockResponseHandler)
            assert {"type": "status", "status": "streaming"} in frames
            # Must see assistant deltas
            deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
            assert deltas == [
                "This is a ",
                "mock response ",
                "from the gateway.",
            ]
            # Must see end
            assert {"type": "end"} in frames
            # Last frame must be status:idle
            assert frames[-1] == {"type": "status", "status": "idle"}

            # Verify ordering: transcribing < transcription < thinking < streaming < end < idle
            idx_transcribing = types.index("transcription") - 1
            assert frames[idx_transcribing] == {
                "type": "status",
                "status": "transcribing",
            }
            idx_transcription = types.index("transcription")
            idx_thinking = next(
                i
                for i, f in enumerate(frames)
                if f.get("type") == "status" and f.get("status") == "thinking"
            )
            idx_end = next(i for i, f in enumerate(frames) if f.get("type") == "end")
            assert idx_transcription < idx_thinking < idx_end

            # Connection stays open
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)


class TestStartAudioWhileRecording:
    """Sending start_audio while already recording returns INVALID_STATE."""

    async def test_start_audio_while_recording_returns_error(
        self, audio_gateway: tuple
    ) -> None:
        url, _ = audio_gateway
        async with websockets.connect(_ws_url(url)) as ws:
            await _consume_handshake(ws)

            # First start_audio — should succeed
            await _send_json(
                ws,
                {
                    "type": "start_audio",
                    "sampleRate": 16000,
                    "channels": 1,
                    "sampleWidth": 2,
                },
            )
            recording = await _recv(ws)
            assert recording == {"type": "status", "status": "recording"}

            # Second start_audio — should error
            await _send_json(
                ws,
                {
                    "type": "start_audio",
                    "sampleRate": 16000,
                    "channels": 1,
                    "sampleWidth": 2,
                },
            )
            error = await _recv(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"


class TestStopAudioWithoutRecording:
    """Sending stop_audio when not recording returns INVALID_STATE."""

    async def test_stop_audio_without_recording_returns_error(
        self, audio_gateway: tuple
    ) -> None:
        url, _ = audio_gateway
        async with websockets.connect(_ws_url(url)) as ws:
            await _consume_handshake(ws)

            # Send stop_audio without ever starting
            await _send_json(ws, {"type": "stop_audio"})
            error = await _recv(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"


class TestStopAudioWithNoData:
    """stop_audio immediately after start_audio (no binary data) → TRANSCRIPTION_FAILED."""

    async def test_stop_audio_with_no_data_returns_error(
        self, audio_gateway: tuple
    ) -> None:
        url, _ = audio_gateway
        async with websockets.connect(_ws_url(url)) as ws:
            await _consume_handshake(ws)

            # Start audio
            await _send_json(
                ws,
                {
                    "type": "start_audio",
                    "sampleRate": 16000,
                    "channels": 1,
                    "sampleWidth": 2,
                },
            )
            recording = await _recv(ws)
            assert recording == {"type": "status", "status": "recording"}

            # Immediately stop — no PCM data sent
            await _send_json(ws, {"type": "stop_audio"})

            # Should get transcribing status, then error, then idle
            transcribing = await _recv(ws)
            assert transcribing == {"type": "status", "status": "transcribing"}

            error = await _recv(ws)
            assert error["type"] == "error"
            assert error["code"] == "TRANSCRIPTION_FAILED"
            assert "No audio data" in error["detail"]

            idle = await _recv(ws)
            assert idle == {"type": "status", "status": "idle"}


class TestTextWhileRecording:
    """Sending a text frame while recording returns INVALID_STATE."""

    async def test_text_while_recording_returns_error(
        self, audio_gateway: tuple
    ) -> None:
        url, _ = audio_gateway
        async with websockets.connect(_ws_url(url)) as ws:
            await _consume_handshake(ws)

            # Start audio
            await _send_json(
                ws,
                {
                    "type": "start_audio",
                    "sampleRate": 16000,
                    "channels": 1,
                    "sampleWidth": 2,
                },
            )
            recording = await _recv(ws)
            assert recording == {"type": "status", "status": "recording"}

            # Send text while recording
            await _send_json(ws, {"type": "text", "message": "hello"})
            error = await _recv(ws)
            assert error["type"] == "error"
            assert error["code"] == "INVALID_STATE"


class TestBinaryDataWhileIdle:
    """Binary data sent while idle is silently ignored — no error, session stays functional."""

    async def test_binary_data_while_idle_ignored(
        self, audio_gateway: tuple
    ) -> None:
        url, _ = audio_gateway
        async with websockets.connect(_ws_url(url)) as ws:
            await _consume_handshake(ws)

            # Send binary data without start_audio — should be silently ignored
            await ws.send(FAKE_PCM)

            # Send a normal text frame — should still work
            await _send_json(ws, {"type": "text", "message": "still works"})
            frames = await _collect_until_idle(ws)

            assert frames[0] == {"type": "status", "status": "thinking"}
            assert frames[1] == {"type": "status", "status": "streaming"}
            deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
            assert deltas == [
                "This is a ",
                "mock response ",
                "from the gateway.",
            ]
            assert frames[-1] == {"type": "status", "status": "idle"}
