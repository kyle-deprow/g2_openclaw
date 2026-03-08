"""Tests for in-flight response resumption (Feature 2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import websockets
from gateway.config import GatewayConfig
from gateway.server import (
    _BUFFER_TTL_SECONDS,
    GatewayServer,
    InflightBuffer,
)

from tests.gateway.conftest import auth_connect as _auth_connect
from tests.gateway.conftest import recv_json as _recv_json


async def _consume_handshake(ws: websockets.ClientConnection) -> None:
    """Consume connected + history + idle frames and validate them."""
    connected = json.loads(await ws.recv())
    assert connected["type"] == "connected", f"Expected connected, got {connected}"

    history = json.loads(await ws.recv())
    assert history["type"] == "history", f"Expected history, got {history}"

    status = json.loads(await ws.recv())
    assert (
        status["type"] == "status" and status["status"] == "idle"
    ), f"Expected status:idle, got {status}"


# ---------------------------------------------------------------------------
# Slow-stream handler (duck-typed to match start_stream protocol)
# ---------------------------------------------------------------------------


class _SlowStreamHandler:
    """Mock handler that streams deltas slowly, supporting start_stream()."""

    def __init__(self, deltas: list[str], delay: float = 0.05) -> None:
        self._deltas = deltas
        self._delay = delay

    async def start_stream(self, message: str) -> AsyncIterator[str]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[str]:
        for d in self._deltas:
            await asyncio.sleep(self._delay)
            yield d

    async def handle(self, message: str, send_frame: Any) -> None:
        await send_frame({"type": "status", "status": "streaming"})
        async for delta in self._stream():
            await send_frame({"type": "assistant", "delta": delta})
        await send_frame({"type": "end"})

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def slow_gateway() -> AsyncIterator[tuple[str, GatewayServer, _SlowStreamHandler]]:
    """Start a gateway with a slow-stream handler that supports start_stream."""
    handler = _SlowStreamHandler(
        ["Hello ", "world ", "from ", "OpenClaw!"],
        delay=0.1,
    )
    config = GatewayConfig(
        gateway_host="127.0.0.1",
        gateway_port=0,
        gateway_token="test-token",
    )
    gw = GatewayServer(config, handler=handler)
    server = await websockets.serve(
        gw.handler,
        config.gateway_host,
        0,
        process_request=gw._process_request,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", gw, handler
    finally:
        # Cancel any lingering inflight task before shutdown
        if gw._inflight_task is not None and not gw._inflight_task.done():
            gw._inflight_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await gw._inflight_task
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# Unit tests - InflightBuffer
# ---------------------------------------------------------------------------


class TestInflightBuffer:
    def test_accumulates_deltas(self) -> None:
        buf = InflightBuffer(user_question="test")
        buf.append_delta("hello ")
        buf.append_delta("world")
        assert buf.full_text == "hello world"

    def test_char_count(self) -> None:
        buf = InflightBuffer(user_question="test")
        buf.append_delta("abc")
        buf.append_delta("de")
        assert buf.char_count == 5

    def test_expired(self) -> None:
        buf = InflightBuffer(
            user_question="test",
            created_at=time.monotonic() - _BUFFER_TTL_SECONDS - 1,
        )
        assert buf.expired

    def test_not_expired(self) -> None:
        buf = InflightBuffer(user_question="test")
        assert not buf.expired

    def test_full_text_empty_initially(self) -> None:
        buf = InflightBuffer(user_question="test")
        assert buf.full_text == ""
        assert buf.char_count == 0

    def test_defaults(self) -> None:
        buf = InflightBuffer(user_question="q")
        assert buf.complete is False
        assert buf.error is None
        assert isinstance(buf.deltas, list)
        assert len(buf.deltas) == 0


# ---------------------------------------------------------------------------
# Integration tests - disconnect/reconnect during streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDisconnectDuringStream:
    """Verify that disconnecting mid-stream buffers and replays on reconnect."""

    async def test_disconnect_during_stream_buffers_and_replays_on_reconnect(
        self,
        slow_gateway: tuple[str, GatewayServer, _SlowStreamHandler],
    ) -> None:
        url, gw, _handler = slow_gateway

        # 1. Connect and complete handshake
        ws1 = await _auth_connect(url)
        await _consume_handshake(ws1)

        # 2. Send text message to start streaming
        await ws1.send(json.dumps({"type": "text", "message": "hello"}))

        # Read thinking + streaming status
        thinking = await _recv_json(ws1)
        assert thinking == {"type": "status", "status": "thinking"}
        streaming = await _recv_json(ws1)
        assert streaming == {"type": "status", "status": "streaming"}

        # Read first delta to confirm streaming started
        first_delta = await _recv_json(ws1)
        assert first_delta["type"] == "assistant"

        # 3. Disconnect abruptly (simulating phone disconnect)
        await ws1.close()
        # Poll for stream completion instead of fixed sleep
        for _ in range(50):
            if gw._inflight_buffer and gw._inflight_buffer.complete:
                break
            await asyncio.sleep(0.05)

        # 4. Verify buffer exists and is complete
        assert gw._inflight_buffer is not None
        assert gw._inflight_buffer.complete is True
        assert len(gw._inflight_buffer.full_text) > 0

        # 5. Reconnect
        ws2 = await _auth_connect(url)
        await ws2.recv()  # connected
        await ws2.recv()  # history

        # idle frame
        idle = await _recv_json(ws2)
        assert idle == {"type": "status", "status": "idle"}

        # 6. Replay: streaming → assistant (full text) → end → idle
        replay_streaming = await _recv_json(ws2)
        assert replay_streaming == {"type": "status", "status": "streaming"}

        replay_delta = await _recv_json(ws2)
        assert replay_delta["type"] == "assistant"
        assert "Hello " in replay_delta["delta"]

        replay_end = await _recv_json(ws2)
        assert replay_end == {"type": "end"}

        replay_idle = await _recv_json(ws2)
        assert replay_idle == {"type": "status", "status": "idle"}

        # 7. Buffer should be cleared now
        assert gw._inflight_buffer is None

        await ws2.close()

    async def test_no_buffer_on_normal_completion(
        self,
        slow_gateway: tuple[str, GatewayServer, _SlowStreamHandler],
    ) -> None:
        """When the phone stays connected, no buffer lingers after completion."""
        url, gw, _handler = slow_gateway

        ws = await _auth_connect(url)
        await _consume_handshake(ws)

        await ws.send(json.dumps({"type": "text", "message": "hello"}))

        # Read thinking + streaming + all deltas + end + idle
        thinking = await _recv_json(ws)
        assert thinking["status"] == "thinking"
        streaming = await _recv_json(ws)
        assert streaming["status"] == "streaming"

        deltas: list[str] = []
        while True:
            frame = await asyncio.wait_for(_recv_json(ws), timeout=5.0)
            if frame["type"] == "assistant":
                deltas.append(frame["delta"])
            elif frame["type"] == "end":
                break

        idle = await _recv_json(ws)
        assert idle == {"type": "status", "status": "idle"}

        # Buffer should be cleared since phone stayed connected
        assert gw._inflight_buffer is None
        assert "".join(deltas) == "Hello world from OpenClaw!"

        await ws.close()


@pytest.mark.asyncio
class TestNewMessageClearsBuffer:
    """Sending a new text message discards any stale inflight buffer."""

    async def test_new_message_clears_buffer(
        self,
        slow_gateway: tuple[str, GatewayServer, _SlowStreamHandler],
    ) -> None:
        url, gw, _handler = slow_gateway

        # Connect, start streaming, disconnect
        ws1 = await _auth_connect(url)
        await _consume_handshake(ws1)
        await ws1.send(json.dumps({"type": "text", "message": "first question"}))

        thinking = await _recv_json(ws1)
        assert thinking["status"] == "thinking"
        streaming = await _recv_json(ws1)
        assert streaming["status"] == "streaming"

        # Read one delta then disconnect
        await _recv_json(ws1)
        await ws1.close()
        # Poll for stream completion instead of fixed sleep
        for _ in range(50):
            if gw._inflight_buffer and gw._inflight_buffer.complete:
                break
            await asyncio.sleep(0.05)

        assert gw._inflight_buffer is not None

        # Reconnect
        ws2 = await _auth_connect(url)
        await _consume_handshake(ws2)

        # Replay frames will arrive — consume them
        replay_streaming = await _recv_json(ws2)
        assert replay_streaming == {"type": "status", "status": "streaming"}

        replay_delta = await _recv_json(ws2)
        assert replay_delta["type"] == "assistant"

        replay_end = await _recv_json(ws2)
        assert replay_end["type"] == "end"

        replay_idle = await _recv_json(ws2)
        assert replay_idle == {"type": "status", "status": "idle"}

        # Now send a new message — this should work with no stale buffer
        assert gw._inflight_buffer is None

        await ws2.send(json.dumps({"type": "text", "message": "second question"}))
        thinking2 = await _recv_json(ws2)
        assert thinking2["status"] == "thinking"

        # The new text message should clear any buffer via _discard_inflight
        # (already None here, but the code path is exercised)

        await ws2.close()
        # Poll for stream completion instead of fixed sleep
        for _ in range(50):
            if gw._inflight_buffer and gw._inflight_buffer.complete:
                break
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
class TestExpiredBuffer:
    """Expired buffers are discarded on reconnect."""

    async def test_expired_buffer_discarded(
        self,
        slow_gateway: tuple[str, GatewayServer, _SlowStreamHandler],
    ) -> None:
        url, gw, _handler = slow_gateway

        # Connect, start streaming, disconnect
        ws1 = await _auth_connect(url)
        await _consume_handshake(ws1)
        await ws1.send(json.dumps({"type": "text", "message": "hello"}))

        thinking = await _recv_json(ws1)
        assert thinking["status"] == "thinking"
        streaming = await _recv_json(ws1)
        assert streaming["status"] == "streaming"
        await _recv_json(ws1)  # first delta
        await ws1.close()
        # Poll for stream completion instead of fixed sleep
        for _ in range(50):
            if gw._inflight_buffer and gw._inflight_buffer.complete:
                break
            await asyncio.sleep(0.05)

        # Force the buffer to be expired
        assert gw._inflight_buffer is not None
        gw._inflight_buffer.created_at = time.monotonic() - _BUFFER_TTL_SECONDS - 10

        # Reconnect
        ws2 = await _auth_connect(url)
        await ws2.recv()  # connected
        await ws2.recv()  # history
        idle = await _recv_json(ws2)
        assert idle == {"type": "status", "status": "idle"}

        # No replay — buffer was expired and discarded
        assert gw._inflight_buffer is None

        # Verify we can still send messages normally
        await ws2.send(json.dumps({"type": "text", "message": "new question"}))
        thinking2 = await _recv_json(ws2)
        assert thinking2["status"] == "thinking"

        await ws2.close()
        # Poll for stream completion instead of fixed sleep
        for _ in range(50):
            if gw._inflight_buffer and gw._inflight_buffer.complete:
                break
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
class TestSpliceInflight:
    """Reconnecting while stream is still in progress splices buffered + live data."""

    async def test_reconnect_during_active_stream_splices(
        self,
        slow_gateway: tuple[str, GatewayServer, _SlowStreamHandler],
    ) -> None:
        """Reconnect while the background stream is still producing deltas."""
        url, gw, handler = slow_gateway

        # Use a slower handler so we can reconnect while still streaming
        handler._delay = 0.3  # 4 deltas x 0.3s = 1.2s total
        handler._deltas = ["A", "B", "C", "D"]

        ws1 = await _auth_connect(url)
        await _consume_handshake(ws1)
        await ws1.send(json.dumps({"type": "text", "message": "hello"}))

        thinking = await _recv_json(ws1)
        assert thinking["status"] == "thinking"
        streaming = await _recv_json(ws1)
        assert streaming["status"] == "streaming"

        # Read first delta
        d1 = await _recv_json(ws1)
        assert d1["type"] == "assistant"

        # Disconnect while stream is still active
        await ws1.close()
        await asyncio.sleep(0.15)  # Let ~1 more delta buffer

        # Stream should still be running
        assert gw._inflight_buffer is not None
        assert not gw._inflight_buffer.complete

        # Reconnect — should get splice of buffered content
        ws2 = await _auth_connect(url)
        await ws2.recv()  # connected
        await ws2.recv()  # history
        idle = await _recv_json(ws2)
        assert idle == {"type": "status", "status": "idle"}

        # Splice: streaming status + buffered deltas
        splice_streaming = await _recv_json(ws2)
        assert splice_streaming == {"type": "status", "status": "streaming"}

        # Collect all remaining frames until end + idle
        frames: list[dict[str, Any]] = []
        while True:
            frame = await asyncio.wait_for(_recv_json(ws2), timeout=5.0)
            frames.append(frame)
            if frame.get("type") == "status" and frame.get("status") == "idle":
                break

        # Should have assistant deltas, end, idle
        types = [f["type"] for f in frames]
        assert "assistant" in types
        assert "end" in types
        assert frames[-1] == {"type": "status", "status": "idle"}

        await ws2.close()


@pytest.mark.asyncio
class TestBufferErrorReplay:
    """When the buffer has an error, replay sends the error to the reconnecting client."""

    async def test_error_buffer_replayed_as_error(
        self,
        slow_gateway: tuple[str, GatewayServer, _SlowStreamHandler],
    ) -> None:
        url, gw, _handler = slow_gateway

        # Manually set up an error buffer
        gw._inflight_buffer = InflightBuffer(user_question="test")
        gw._inflight_buffer.error = "upstream timeout"

        ws = await _auth_connect(url)
        await ws.recv()  # connected
        await ws.recv()  # history
        idle = await _recv_json(ws)
        assert idle == {"type": "status", "status": "idle"}

        # Replay should send error
        error = await _recv_json(ws)
        assert error["type"] == "error"
        assert "upstream timeout" in error["detail"]

        # Buffer cleared
        assert gw._inflight_buffer is None

        await ws.close()


@pytest.mark.asyncio
class TestDiscardInflight:
    """_discard_inflight cancels the task and clears the buffer."""

    async def test_discard_clears_buffer_and_task(self) -> None:
        config = GatewayConfig(
            gateway_host="127.0.0.1",
            gateway_port=0,
        )
        gw = GatewayServer(config)

        # Set up fake buffer and task
        gw._inflight_buffer = InflightBuffer(user_question="test")

        async def _noop() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(_noop())
        gw._inflight_task = task

        await gw._discard_inflight()

        assert gw._inflight_buffer is None
        assert gw._inflight_task is None
        assert task.cancelled()

    async def test_discard_inflight_noop_when_empty(self) -> None:
        """Calling _discard_inflight with no buffer/task raises no error."""
        config = GatewayConfig(
            gateway_host="127.0.0.1",
            gateway_port=0,
        )
        gw = GatewayServer(config)

        assert gw._inflight_buffer is None
        assert gw._inflight_task is None

        await gw._discard_inflight()  # should be a no-op

        assert gw._inflight_buffer is None
        assert gw._inflight_task is None

    async def test_discard_inflight_when_task_done(self) -> None:
        """Discarding when the task has already completed cleans up correctly."""
        config = GatewayConfig(
            gateway_host="127.0.0.1",
            gateway_port=0,
        )
        gw = GatewayServer(config)

        async def _instant() -> None:
            return

        task = asyncio.create_task(_instant())
        await task  # ensure it completes

        gw._inflight_buffer = InflightBuffer(user_question="done")
        gw._inflight_task = task

        await gw._discard_inflight()

        assert gw._inflight_buffer is None
        assert gw._inflight_task is None
