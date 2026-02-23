"""End-to-end OpenClaw verification tests (P3.9).

Proves the full pipeline: phone WebSocket → Gateway → mock OpenClaw → streamed
response back to phone.  Each test stands up its own mock OpenClaw server and
Gateway so there are no shared-state side effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Callable, Coroutine
from typing import Any, cast

import numpy as np
import pytest
import websockets
import websockets.asyncio.server
from gateway.config import GatewayConfig
from gateway.openclaw_client import OpenClawClient
from gateway.server import GatewayServer, OpenClawResponseHandler
from gateway.transcriber import Transcriber

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIMEOUT = 10.0


async def _recv(ws: websockets.ClientConnection) -> dict[str, Any]:
    """Receive a single JSON frame with a timeout guard."""
    raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
    result: dict[str, Any] = json.loads(raw)
    return result


async def _recv_until(
    ws: websockets.ClientConnection,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float = TIMEOUT,
) -> list[dict[str, Any]]:
    """Receive frames until *predicate* returns True on a frame."""
    frames: list[dict[str, Any]] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2.0))
            frame: dict[str, Any] = json.loads(raw)
            frames.append(frame)
            if predicate(frame):
                return frames
        except TimeoutError:
            break
    return frames


async def _consume_handshake(
    ws: websockets.ClientConnection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    connected = await _recv(ws)
    idle = await _recv(ws)
    return connected, idle


async def _send_text(ws: websockets.ClientConnection, message: str) -> None:
    await ws.send(json.dumps({"type": "text", "message": message}))


async def _send_json(ws: websockets.ClientConnection, frame: dict[str, Any]) -> None:
    await ws.send(json.dumps(frame))


async def _collect_until_idle(ws: websockets.ClientConnection) -> list[dict[str, Any]]:
    """Collect all frames until a status:idle frame is received."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = await _recv(ws)
        frames.append(frame)
        if frame.get("type") == "status" and frame.get("status") == "idle":
            break
    return frames


def _is_final_idle(frame: dict[str, Any]) -> bool:
    return frame.get("type") == "status" and frame.get("status") == "idle"


def _free_port() -> int:
    """Return a TCP port that is currently unused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


# ---------------------------------------------------------------------------
# Fake transcriber
# ---------------------------------------------------------------------------


class MockTranscriber:
    """Transcriber stub that returns fixed text — no Whisper needed."""

    def __init__(self, result: str = "hello world") -> None:
        self._result = result

    async def transcribe(
        self, audio: np.ndarray[Any, Any], language: str = "en", timeout: float = 30.0
    ) -> str:
        await asyncio.sleep(0.02)
        return self._result


# ---------------------------------------------------------------------------
# Mock OpenClaw handlers (per-test variants)
# ---------------------------------------------------------------------------


async def _standard_oc_handler(ws: websockets.ServerConnection) -> None:
    """Standard mock: connect ack + agent with 2 deltas + lifecycle end."""
    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "connect":
            await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}}))
        elif method == "agent":
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg["id"],
                        "ok": True,
                        "payload": {
                            "runId": "run-1",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )
            for delta in ["Hello ", "world"]:
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {"stream": "assistant", "delta": delta},
                        }
                    )
                )
                await asyncio.sleep(0.01)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {"stream": "lifecycle", "phase": "end"},
                    }
                )
            )


async def _long_response_oc_handler(ws: websockets.ServerConnection) -> None:
    """Sends 100 deltas of 50 chars each (5 000 chars total)."""
    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "connect":
            await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}}))
        elif method == "agent":
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg["id"],
                        "ok": True,
                        "payload": {
                            "runId": "run-long",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )
            for i in range(100):
                delta = f"D{i:03d}-" + "x" * 45  # exactly 50 chars
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {"stream": "assistant", "delta": delta},
                        }
                    )
                )
                # Small sleep every 10 deltas to avoid overwhelming the loop
                if i % 10 == 9:
                    await asyncio.sleep(0.01)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {"stream": "lifecycle", "phase": "end"},
                    }
                )
            )


async def _hanging_oc_handler(ws: websockets.ServerConnection) -> None:
    """Accepts agent request but never sends deltas — simulates a hang."""
    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "connect":
            await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}}))
        elif method == "agent":
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg["id"],
                        "ok": True,
                        "payload": {
                            "runId": "run-hang",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )
            # Hang forever — never send deltas or lifecycle
            await asyncio.sleep(300)


async def _error_mid_stream_oc_handler(ws: websockets.ServerConnection) -> None:
    """Sends 2 deltas then a lifecycle error event."""
    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "connect":
            await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}}))
        elif method == "agent":
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg["id"],
                        "ok": True,
                        "payload": {
                            "runId": "run-err",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )
            for delta in ["partial ", "response"]:
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {"stream": "assistant", "delta": delta},
                        }
                    )
                )
                await asyncio.sleep(0.01)
            # Lifecycle error
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {
                            "stream": "lifecycle",
                            "phase": "error",
                            "error": "model exploded",
                        },
                    }
                )
            )


# ---------------------------------------------------------------------------
# Fixture factory
# ---------------------------------------------------------------------------


async def _make_openclaw_gateway(
    oc_handler: Callable[[websockets.ServerConnection], Coroutine[Any, Any, None]],
    *,
    gateway_token: str = "e2e-token",
    agent_timeout: int = 120,
    transcriber: Transcriber | MockTranscriber | None = None,
) -> tuple[str, websockets.asyncio.server.Server, websockets.asyncio.server.Server, OpenClawClient]:
    """Start a mock OpenClaw server and a Gateway pointing to it.

    Returns (gw_url, gw_server, oc_server, openclaw_client).
    """
    oc_server = await websockets.serve(oc_handler, "127.0.0.1", 0)
    oc_port = oc_server.sockets[0].getsockname()[1]

    client = OpenClawClient(
        host="127.0.0.1",
        port=oc_port,
        token="oc-token",
    )
    oc_response_handler = OpenClawResponseHandler(client)

    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token=gateway_token,
        openclaw_host="127.0.0.1",
        openclaw_port=oc_port,
        openclaw_gateway_token="oc-token",
        agent_timeout=agent_timeout,
    )
    gw = GatewayServer(
        config,
        handler=oc_response_handler,
        transcriber=cast(Transcriber | None, transcriber),
    )
    gw_server = await websockets.serve(gw.handler, "127.0.0.1", 0)
    gw_port = gw_server.sockets[0].getsockname()[1]

    return (
        f"ws://127.0.0.1:{gw_port}",
        gw_server,
        oc_server,
        client,
    )


async def _cleanup(
    gw_server: websockets.asyncio.server.Server,
    oc_server: websockets.asyncio.server.Server,
    client: OpenClawClient,
) -> None:
    """Shutdown servers and client.

    Close the OpenClaw *client* first so the mock handler's ``async for``
    loop exits with ``ConnectionClosed``.  Then close both servers with a
    bounded wait so that mock handlers that intentionally hang (e.g. the
    timeout test) don't block test teardown.
    """
    await client.close()
    gw_server.close()
    oc_server.close()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(gw_server.wait_closed(), timeout=2.0)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(oc_server.wait_closed(), timeout=2.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFullTextFlow:
    """1. Send text → receive full status/delta/end sequence via mock OpenClaw."""

    async def test_full_text_e2e(self) -> None:
        url, gw_server, oc_server, client = await _make_openclaw_gateway(_standard_oc_handler)
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                # Handshake
                connected, idle = await _consume_handshake(ws)
                assert connected == {"type": "connected", "version": "1.0"}
                assert idle == {"type": "status", "status": "idle"}

                # Send text
                await _send_text(ws, "What is 2+2?")

                # Collect everything through final idle
                frames = await _collect_until_idle(ws)

                statuses = [f["status"] for f in frames if f["type"] == "status"]

                # Status progression
                assert statuses[0] == "thinking"
                assert statuses[1] == "streaming"
                assert statuses[-1] == "idle"

                # Assistant deltas
                deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
                assert deltas == ["Hello ", "world"]

                # End before final idle
                assert frames[-2] == {"type": "end"}
                assert frames[-1] == {"type": "status", "status": "idle"}

                # No more frames
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
        finally:
            await _cleanup(gw_server, oc_server, client)


@pytest.mark.asyncio
class TestFullVoiceFlow:
    """2. Voice → text pipeline via mock transcriber + mock OpenClaw."""

    async def test_voice_to_text_e2e(self) -> None:
        transcriber = MockTranscriber(result="hello world")
        url, gw_server, oc_server, client = await _make_openclaw_gateway(
            _standard_oc_handler, transcriber=transcriber
        )
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
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

                # Send PCM data (0.1s of 16kHz mono 16-bit)
                pcm_data = b"\x00\x80" * 1600  # 3200 bytes
                await ws.send(pcm_data)

                # Stop audio
                await _send_json(ws, {"type": "stop_audio"})

                # Collect through final idle
                frames = await _collect_until_idle(ws)

                # Must have transcribing status
                assert {"type": "status", "status": "transcribing"} in frames
                # Must have transcription
                assert {"type": "transcription", "text": "hello world"} in frames
                # Thinking
                assert {"type": "status", "status": "thinking"} in frames
                # Streaming
                assert {"type": "status", "status": "streaming"} in frames
                # Deltas from OpenClaw
                deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
                assert deltas == ["Hello ", "world"]
                # End
                assert {"type": "end"} in frames
                # Final idle
                assert frames[-1] == {"type": "status", "status": "idle"}

                # Ordering: transcribing < transcription < thinking < streaming < end < idle
                idx_transcribing = next(
                    i
                    for i, f in enumerate(frames)
                    if f.get("type") == "status" and f.get("status") == "transcribing"
                )
                idx_transcription = next(
                    i for i, f in enumerate(frames) if f.get("type") == "transcription"
                )
                idx_thinking = next(
                    i
                    for i, f in enumerate(frames)
                    if f.get("type") == "status" and f.get("status") == "thinking"
                )
                idx_end = next(i for i, f in enumerate(frames) if f.get("type") == "end")
                assert idx_transcribing < idx_transcription < idx_thinking < idx_end
        finally:
            await _cleanup(gw_server, oc_server, client)


@pytest.mark.asyncio
class TestLongResponseTruncation:
    """3. 100 deltas x 50 chars -- all arrive at the phone (no server-side truncation)."""

    async def test_100_deltas_all_arrive(self) -> None:
        url, gw_server, oc_server, client = await _make_openclaw_gateway(_long_response_oc_handler)
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                await _consume_handshake(ws)
                await _send_text(ws, "give me a long answer")

                frames = await _collect_until_idle(ws)

                deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
                assert len(deltas) == 100

                # Each delta is exactly 50 chars
                for d in deltas:
                    assert len(d) == 50

                total_chars = sum(len(d) for d in deltas)
                assert total_chars == 5000

                assert frames[-2] == {"type": "end"}
                assert frames[-1] == {"type": "status", "status": "idle"}
        finally:
            await _cleanup(gw_server, oc_server, client)


@pytest.mark.asyncio
class TestMultipleSequentialQueries:
    """4. Two sequential queries — no state leaks between them."""

    async def test_two_queries_no_state_leak(self) -> None:
        url, gw_server, oc_server, client = await _make_openclaw_gateway(_standard_oc_handler)
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                await _consume_handshake(ws)

                # --- First query ---
                await _send_text(ws, "first query")
                frames_1 = await _collect_until_idle(ws)

                assert frames_1[0] == {"type": "status", "status": "thinking"}
                deltas_1 = [f["delta"] for f in frames_1 if f["type"] == "assistant"]
                assert deltas_1 == ["Hello ", "world"]
                assert frames_1[-1] == {"type": "status", "status": "idle"}

                # --- Second query ---
                await _send_text(ws, "second query")
                frames_2 = await _collect_until_idle(ws)

                assert frames_2[0] == {"type": "status", "status": "thinking"}
                deltas_2 = [f["delta"] for f in frames_2 if f["type"] == "assistant"]
                assert deltas_2 == ["Hello ", "world"]
                assert frames_2[-1] == {"type": "status", "status": "idle"}
        finally:
            await _cleanup(gw_server, oc_server, client)


@pytest.mark.asyncio
class TestOpenClawNotRunning:
    """5. OpenClaw unreachable → OPENCLAW_ERROR → gateway recovers."""

    async def test_openclaw_not_running(self) -> None:
        # Get a port where nothing listens
        dead_port = _free_port()

        client = OpenClawClient(
            host="127.0.0.1",
            port=dead_port,
            token="oc-token",
        )
        oc_handler = OpenClawResponseHandler(client)

        config = GatewayConfig(
            gateway_host="127.0.0.1",
            gateway_port=0,
            gateway_token="e2e-token",
            openclaw_host="127.0.0.1",
            openclaw_port=dead_port,
            openclaw_gateway_token="oc-token",
            agent_timeout=10,
        )
        gw = GatewayServer(config, handler=oc_handler)
        gw_server = await websockets.serve(gw.handler, "127.0.0.1", 0)
        gw_port = gw_server.sockets[0].getsockname()[1]

        try:
            async with websockets.connect(f"ws://127.0.0.1:{gw_port}?token=e2e-token") as ws:
                await _consume_handshake(ws)

                await _send_text(ws, "hello?")

                frames = await _collect_until_idle(ws)

                # Must have thinking → error → idle
                assert frames[0] == {"type": "status", "status": "thinking"}

                error_frames = [f for f in frames if f["type"] == "error"]
                assert len(error_frames) >= 1
                assert error_frames[0]["code"] == "OPENCLAW_ERROR"

                assert frames[-1] == {"type": "status", "status": "idle"}
        finally:
            await client.close()
            gw_server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(gw_server.wait_closed(), timeout=2.0)


@pytest.mark.asyncio
class TestAgentTimeout:
    """6. Mock OpenClaw hangs after accepting agent → TIMEOUT error after ~2s."""

    async def test_agent_timeout(self) -> None:
        url, gw_server, oc_server, client = await _make_openclaw_gateway(
            _hanging_oc_handler, agent_timeout=2
        )
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                await _consume_handshake(ws)

                t0 = asyncio.get_event_loop().time()
                await _send_text(ws, "this will hang")

                # Use _collect_until_idle (10s per-frame timeout) so we don't
                # race with the 2s agent timeout.
                frames = await _collect_until_idle(ws)

                elapsed = asyncio.get_event_loop().time() - t0

                # Should have timed out in roughly 2s (give generous margin)
                assert elapsed < 10.0, f"Took too long: {elapsed:.1f}s"

                error_frames = [f for f in frames if f["type"] == "error"]
                assert len(error_frames) >= 1
                assert error_frames[0]["code"] == "TIMEOUT"
                assert "2s timeout" in error_frames[0]["detail"]

                # Must end with idle
                assert frames[-1] == {"type": "status", "status": "idle"}
        finally:
            await _cleanup(gw_server, oc_server, client)


@pytest.mark.asyncio
class TestOpenClawErrorDuringStreaming:
    """7. Mock OpenClaw sends 2 deltas then lifecycle error."""

    async def test_error_mid_stream(self) -> None:
        url, gw_server, oc_server, client = await _make_openclaw_gateway(
            _error_mid_stream_oc_handler
        )
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                await _consume_handshake(ws)

                await _send_text(ws, "this will error mid-stream")

                frames = await _collect_until_idle(ws)

                # Should have received the 2 assistant deltas
                deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
                assert deltas == ["partial ", "response"]

                # Should have an OPENCLAW_ERROR
                error_frames = [f for f in frames if f["type"] == "error"]
                assert len(error_frames) >= 1
                assert error_frames[0]["code"] == "OPENCLAW_ERROR"

                # streaming status before deltas
                assert {"type": "status", "status": "streaming"} in frames
                # Final idle
                assert frames[-1] == {"type": "status", "status": "idle"}
        finally:
            await _cleanup(gw_server, oc_server, client)


async def _empty_response_oc_handler(ws: websockets.ServerConnection) -> None:
    """Accepts agent request and immediately sends lifecycle end — zero deltas."""
    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "connect":
            await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}}))
        elif method == "agent":
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg["id"],
                        "ok": True,
                        "payload": {
                            "runId": "run-empty",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )
            # No deltas — immediate lifecycle end
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {"stream": "lifecycle", "phase": "end"},
                    }
                )
            )


@pytest.mark.asyncio
class TestEmptyResponse:
    """8. Mock OpenClaw sends lifecycle end with zero deltas."""

    async def test_empty_response(self) -> None:
        url, gw_server, oc_server, client = await _make_openclaw_gateway(_empty_response_oc_handler)
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                await _consume_handshake(ws)
                await _send_text(ws, "give me an empty response")

                frames = await _collect_until_idle(ws)

                # No assistant deltas
                deltas = [f["delta"] for f in frames if f["type"] == "assistant"]
                assert deltas == []

                # Should still have thinking → streaming → end → idle
                assert frames[0] == {"type": "status", "status": "thinking"}
                assert {"type": "status", "status": "streaming"} in frames
                assert {"type": "end"} in frames
                assert frames[-1] == {"type": "status", "status": "idle"}
        finally:
            await _cleanup(gw_server, oc_server, client)


@pytest.mark.asyncio
class TestRecoveryAfterError:
    """9. First query gets lifecycle error, second query succeeds."""

    async def test_recovery_after_error(self) -> None:
        agent_calls = [0]

        async def oc_handler(ws: websockets.ServerConnection) -> None:
            async for raw in ws:
                msg = json.loads(raw)
                method = msg.get("method")
                if method == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif method == "agent":
                    agent_calls[0] += 1
                    current = agent_calls[0]
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"],
                                "ok": True,
                                "payload": {
                                    "runId": f"run-{current}",
                                    "acceptedAt": "2026-01-01T00:00:00Z",
                                },
                            }
                        )
                    )
                    if current == 1:
                        # First request: lifecycle error
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "event",
                                    "event": "agent",
                                    "payload": {
                                        "stream": "lifecycle",
                                        "phase": "error",
                                        "error": "temporary failure",
                                    },
                                }
                            )
                        )
                    else:
                        # Subsequent requests: normal response
                        for delta in ["Recovered ", "OK"]:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "event",
                                        "event": "agent",
                                        "payload": {
                                            "stream": "assistant",
                                            "delta": delta,
                                        },
                                    }
                                )
                            )
                            await asyncio.sleep(0.01)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "event",
                                    "event": "agent",
                                    "payload": {
                                        "stream": "lifecycle",
                                        "phase": "end",
                                    },
                                }
                            )
                        )

        url, gw_server, oc_server, client = await _make_openclaw_gateway(oc_handler)
        try:
            async with websockets.connect(f"{url}?token=e2e-token") as ws:
                await _consume_handshake(ws)

                # --- First query: errors ---
                await _send_text(ws, "first query")
                frames_1 = await _collect_until_idle(ws)
                error_frames = [f for f in frames_1 if f["type"] == "error"]
                assert len(error_frames) >= 1
                assert error_frames[0]["code"] == "OPENCLAW_ERROR"
                assert frames_1[-1] == {"type": "status", "status": "idle"}

                # --- Second query: succeeds ---
                await _send_text(ws, "second query")
                frames_2 = await _collect_until_idle(ws)
                deltas = [f["delta"] for f in frames_2 if f["type"] == "assistant"]
                assert deltas == ["Recovered ", "OK"]
                assert {"type": "end"} in frames_2
                assert frames_2[-1] == {"type": "status", "status": "idle"}
        finally:
            await _cleanup(gw_server, oc_server, client)
