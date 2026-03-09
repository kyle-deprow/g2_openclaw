# Feature 2: In-Flight Response Resumption — Implementation Plan

## 1. Problem Statement

When the phone disconnects mid-stream (BLE drop, app backgrounded, etc.), the
Gateway's `OpenClawResponseHandler.handle()` is iterating through the
`_stream_deltas` generator, calling `send_frame()` on a dead WebSocket.  Those
`send_frame()` calls raise `ConnectionClosed`, which unwinds to `_handle_text`,
which catches the exception and moves to IDLE.  Meanwhile, OpenClaw finishes
processing server-side and the full response lands in its `.jsonl` transcript —
but the Gateway discards all accumulated deltas and the user never sees them.

On reconnect the `GatewaySession` object is destroyed and replaced with a fresh
one, so there is zero continuity.

**Goal:** Buffer the in-flight response inside the long-lived `GatewayServer`
instance and replay it to the phone when it reconnects.

---

## 2. Design Overview

```
                  ┌─────────────────────────────────────────────┐
                  │              GatewayServer                  │
                  │                                             │
                  │   _inflight_buffer: InflightBuffer | None   │
                  │   _inflight_task:   asyncio.Task  | None    │
                  │   _current_session: GatewaySession | None   │
                  │                                             │
                  └─────────────────────────────────────────────┘
                               ▲               ▲
              Phone connected  │               │  Phone disconnected—
              → replay buffer  │               │  stream continues
              → splice live    │               │  into buffer
```

**Key principle:** The `GatewayServer` persists across connections.  The
`InflightBuffer` lives on `GatewayServer`, not on `GatewaySession`.

---

## 3. Data Structures

### 3.1 `InflightBuffer` (new dataclass in `gateway/server.py`)

```python
from dataclasses import dataclass, field
import time

_BUFFER_MAX_CHARS = 200_000          # ~200 KB text limit
_BUFFER_TTL_SECONDS = 300            # discard after 5 minutes

@dataclass
class InflightBuffer:
    """Holds an in-flight OpenClaw response while the phone is disconnected."""

    user_question: str                              # the user's original message
    deltas: list[str] = field(default_factory=list) # accumulated delta strings
    complete: bool = False                          # True after `end` frame
    error: str | None = None                        # non-None if OpenClaw errored
    created_at: float = field(default_factory=time.monotonic)

    @property
    def full_text(self) -> str:
        return "".join(self.deltas)

    @property
    def char_count(self) -> int:
        return sum(len(d) for d in self.deltas)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > _BUFFER_TTL_SECONDS
```

**Why a flat list of deltas?**  It preserves the exact delta boundaries for the
still-streaming splice case (§5).  `full_text` is a derived property for the
completed-response replay.

### 3.2 New fields on `GatewayServer`

```python
class GatewayServer:
    def __init__(self, ...):
        ...
        self._inflight_buffer: InflightBuffer | None = None
        self._inflight_task: asyncio.Task[None] | None = None
```

---

## 4. Gateway-Side Buffering (Disconnect Path)

### 4.1 Detecting the disconnect during streaming

Currently, `GatewaySession.handle()` iterates `async for message in self.ws`,
which exits cleanly when the WebSocket closes.  The cleanup in
`GatewayServer.handler()` runs:

```python
finally:
    session._stop_local_stream()
    if self._current_session is session:
        self._current_session = None
```

When the phone drops mid-stream, `_handle_text()` is still running, blocked
inside `self._handler.handle(message, self.send_frame)`.  The `send_frame()`
call will raise `websockets.ConnectionClosed`.  The exception propagates up
through `OpenClawResponseHandler.handle()` and is caught by `_handle_text`'s
generic `except Exception` block, which calls `await self._handler.close()` —
which in turn calls `await self._client.close()`, killing the OpenClaw
WebSocket and losing the remaining stream.

**That is the core issue.** We must decouple the OpenClaw stream consumption
from the phone WebSocket lifetime.

### 4.2 New approach: `_stream_to_target()` background task

Replace the synchronous `OpenClawResponseHandler.handle()` call in
`_handle_text` with a **background task** managed by `GatewayServer` that:

1. Consumes the OpenClaw delta stream.
2. For each delta: attempts to send to the phone, **and** appends to the
   `InflightBuffer`.
3. If the phone send fails (`ConnectionClosed`), stops sending to the phone but
   **keeps consuming** into the buffer until `lifecycle:end`.

#### 4.2.1 Changes to `OpenClawResponseHandler`

Refactor `handle()` to return the stream iterator instead of consuming it
internally:

```python
class OpenClawResponseHandler:
    async def start_stream(self, message: str) -> AsyncIterator[str]:
        """Initiate an OpenClaw agent request and return the delta stream."""
        logger.info("Sending to OpenClaw: %s", message[:100])
        return await self._client.send_message(message)

    async def handle(self, message, send_frame):
        """Unchanged — still works for non-buffered path (mock handler etc.)."""
        stream = await self.start_stream(message)
        await send_frame({"type": "status", "status": "streaming"})
        async for delta in stream:
            await send_frame({"type": "assistant", "delta": delta})
        await send_frame({"type": "end"})

    async def close(self) -> None:
        await self._client.close()
```

#### 4.2.2 New method on `GatewayServer`: `_run_inflight_stream()`

```python
async def _run_inflight_stream(
    self,
    stream: AsyncIterator[str],
    buffer: InflightBuffer,
) -> None:
    """Consume the OpenClaw stream into the buffer, forwarding to phone if connected.

    Runs as a background task. If the phone disconnects, continues draining
    into the buffer silently.
    """
    try:
        async for delta in stream:
            # Always buffer
            if buffer.char_count + len(delta) <= _BUFFER_MAX_CHARS:
                buffer.deltas.append(delta)
            else:
                logger.warning("Inflight buffer exceeded %d chars — truncating", _BUFFER_MAX_CHARS)

            # Forward to phone if still connected
            session = self._current_session
            if session is not None:
                try:
                    await session.send_frame({"type": "assistant", "delta": delta})
                except Exception:
                    logger.info("Phone disconnected mid-stream — continuing to buffer")
                    # Don't clear _current_session here; handler() cleanup does that
        buffer.complete = True
    except OpenClawError as exc:
        buffer.error = str(exc)
        logger.error("OpenClaw error during inflight stream: %s", exc)
    except Exception:
        buffer.error = "internal error"
        logger.exception("Unexpected error during inflight stream")
    finally:
        # Send end frame if phone is still connected
        session = self._current_session
        if session is not None and buffer.complete:
            try:
                await session.send_frame({"type": "end"})
                await session.send_frame({"type": "status", "status": "idle"})
            except Exception:
                pass

        # If buffer completed and was delivered, clean up
        if buffer.complete and self._current_session is not None:
            self._inflight_buffer = None
        self._inflight_task = None
```

#### 4.2.3 Modified `_handle_text` flow

Inside `GatewaySession._handle_text` (or better, via a new coordination method
on `GatewayServer`), the flow becomes:

```
1. session._state = THINKING
2. send status:thinking
3. stream = await handler.start_stream(message)  # get iterator
4. send status:streaming
5. Create InflightBuffer(user_question=message)
6. server._inflight_buffer = buffer
7. server._inflight_task = asyncio.create_task(server._run_inflight_stream(stream, buffer))
8. await server._inflight_task  (but wrapped so disconnect doesn't cancel it)
```

**Critical change:** When the phone disconnects, `GatewaySession.handle()` exits
its `async for message in self.ws` loop.  This would normally cancel any
in-progress `_handle_text` work.  We must ensure the inflight task is **not**
cancelled.

The way to achieve this: launch `_inflight_task` as a standalone `asyncio.Task`
on the event loop, not as a coroutine awaited inside `_handle_text`.
`_handle_text` creates the task and then waits on it, but the disconnect
handler in `GatewayServer.handler()` detaches rather than cancelling it.

```python
# In _handle_text, after creating the task:
try:
    await self._server._inflight_task
except asyncio.CancelledError:
    pass  # session closed — task continues independently
```

Wait — the task is on the server, not the session.  The session's `handle()`
method runs inside `GatewayServer.handler()`, which has a `try/finally` that
sets `_current_session = None`.  The inflight task uses `self._current_session`
to check if a phone is connected, so setting it to `None` is the signal.

**Simpler approach:** `_handle_text` starts the background task and immediately
returns.  The session state stays `STREAMING`.  The background task handles
sending `end` and transitioning to `IDLE` when done.  If the phone disconnects,
the session is destroyed, but the task keeps running.

### 4.3 Preventing task cancellation on disconnect

In `GatewayServer.handler()`'s `finally` block:

```python
finally:
    session._stop_local_stream()
    if self._current_session is session:
        self._current_session = None
    # NOTE: Do NOT cancel _inflight_task here — it must finish draining
```

The inflight task continues running.  It notices `_current_session is None` and
stops trying to send frames, but it keeps consuming the OpenClaw stream into
the buffer.

---

## 5. Reconnect Delivery

### 5.1 Completed response (buffer.complete == True)

When a new phone connects and `GatewayServer.handler()` creates a new
`GatewaySession`, check for a pending buffer **after** sending `connected`
and `status:idle`:

```python
async def handler(self, ws):
    # ... auth ...
    session = GatewaySession(ws, ...)
    self._current_session = session

    # Check for pending inflight buffer BEFORE entering message loop
    await self._replay_inflight_if_pending(session)

    try:
        await session.handle()
    ...
```

But `session.handle()` sends `connected` and `status:idle` at the start.  The
replay must happen **after** those frames.  Two options:

**Option A (chosen):** Add a hook in `GatewaySession.handle()` that calls back
to the server after sending the initial frames:

```python
# GatewaySession.handle()
async def handle(self) -> None:
    await self.send_frame({"type": "connected", "version": "1.0"})
    await self.send_frame({"type": "status", "status": "idle"})

    # Allow server to replay buffered response
    if self._on_ready:
        await self._on_ready()

    async for message in self.ws:
        ...
```

The callback is set by `GatewayServer`:

```python
session._on_ready = lambda: self._replay_inflight(session)
```

#### `_replay_inflight()` method on `GatewayServer`:

```python
async def _replay_inflight(self, session: GatewaySession) -> None:
    """Replay a buffered inflight response to the reconnected phone."""
    buf = self._inflight_buffer
    if buf is None or buf.expired:
        self._inflight_buffer = None
        return

    if buf.error:
        # Deliver the error
        await session.send_frame({
            "type": "error",
            "detail": f"Previous response failed: {buf.error}",
            "code": ErrorCode.OPENCLAW_ERROR,
        })
        self._inflight_buffer = None
        return

    if buf.complete:
        # Deliver completed response as: status:streaming → assistant(full) → end → status:idle
        await session.send_frame({"type": "status", "status": "streaming"})
        session._state = SessionState.STREAMING
        await session.send_frame({"type": "assistant", "delta": buf.full_text})
        await session.send_frame({"type": "end"})
        session._state = SessionState.IDLE
        await session.send_frame({"type": "status", "status": "idle"})
        self._inflight_buffer = None
        return

    # Still streaming — splice (see §5.2)
    await self._splice_inflight(session, buf)
```

**Why send as a single delta?** The G2 display renders text character-by-character
anyway.  Sending the accumulated text as one `assistant` frame is simpler and
faster than replaying individual deltas with artificial delays.  The phone's
`ConversationHistory.appendToLastAssistant()` simply concatenates — it doesn't
care about delta boundaries.

### 5.2 Still-streaming response (buffer.complete == False, task still running)

If `_inflight_task` is still running when the phone reconnects:

```python
async def _splice_inflight(self, session: GatewaySession, buf: InflightBuffer) -> None:
    """Splice buffered deltas with the live stream for a reconnecting phone."""
    # 1. Send what we have so far
    await session.send_frame({"type": "status", "status": "streaming"})
    session._state = SessionState.STREAMING

    if buf.full_text:
        await session.send_frame({"type": "assistant", "delta": buf.full_text})

    # 2. The background task (_run_inflight_stream) is still running.
    #    It checks self._current_session on each delta — now that we've
    #    set _current_session to the new session, new deltas will be
    #    forwarded to it automatically.
    #
    # 3. The task will send `end` and `status:idle` when it finishes.
    #    Nothing more to do here.
```

This works because `_run_inflight_stream()` already checks
`self._current_session` on every delta.  Once `handler()` sets
`_current_session` to the new session, new deltas flow to the new phone
automatically.  The splice is seamless.

**Timing detail:** There's a brief window where the task might try to send a
delta to the old (dead) session right as the new session is being set.  This is
safe because:
- The old session's `send_frame()` will raise `ConnectionClosed`.
- The task catches this and continues buffering.
- On the next delta, `_current_session` is the new session and delivery resumes.

---

## 6. Cleanup

Discard the buffer when:

| Trigger | Action |
|---------|--------|
| Successful replay of completed buffer | `_inflight_buffer = None` in `_replay_inflight()` |
| Buffer TTL expires (5 min) | Checked at replay time; also checked before splice |
| New user message arrives | Clear buffer in `_handle_text` before starting new request |
| OpenClaw error during buffering | Keep buffer with error set; deliver error on reconnect, then clear |
| Server shutdown | Implicit — process dies |

In `GatewaySession._handle_text` (or the new coordination point):

```python
# Clear any stale buffer before starting a new request
self._server._discard_inflight()
```

```python
def _discard_inflight(self) -> None:
    """Cancel any in-flight stream and discard the buffer."""
    if self._inflight_task is not None and not self._inflight_task.done():
        self._inflight_task.cancel()
    self._inflight_task = None
    self._inflight_buffer = None
```

---

## 7. State Machine Impact

### 7.1 Gateway-side (`SessionState`)

No new states needed.  The state machine operates per-session:

| Scenario | Session 1 (disconnecting) | GatewayServer | Session 2 (reconnecting) |
|----------|--------------------------|---------------|--------------------------|
| Phone drops mid-stream | STREAMING → destroyed | buffer accumulating | — |
| Phone reconnects, response done | — | buffer complete | IDLE → STREAMING → IDLE (replay) |
| Phone reconnects, still streaming | — | buffer in progress | IDLE → STREAMING (splice) |

The new session starts in IDLE (after `connected` + `status:idle`), then
transitions to STREAMING during replay/splice, then back to IDLE on `end`.

### 7.2 G2 App-side (`StateMachine`)

No changes needed.  The app already handles:
- `connected` → transition to `idle`
- `status:streaming` → `conversation.startAssistantStream()`, display update
- `assistant` deltas → append
- `end` → transition to `idle`

The replayed sequence (`connected → idle → streaming → assistant → end → idle`)
follows the same frame sequence as a normal response.  The app is stateless
about whether this is a fresh or replayed response.

---

## 8. Edge Cases

### 8.1 Multiple disconnects during the same response

The inflight task runs independently of sessions.  Each reconnect:
1. Creates a new `GatewaySession`
2. Sets `_current_session` to the new session
3. Calls `_replay_inflight()` / `_splice_inflight()`

If the phone disconnects again mid-replay, the task is still running and
continues buffering.  The next reconnect picks up where it left off.

### 8.2 Buffer size limit

`_BUFFER_MAX_CHARS = 200_000` (~200 KB).  If exceeded, new deltas are dropped
but the stream continues draining (to reach `lifecycle:end` and close the
OpenClaw WebSocket cleanly).  The buffer is marked complete but truncated.

### 8.3 OpenClaw errors during disconnect

`_run_inflight_stream` catches `OpenClawError` and sets `buffer.error`.  On
reconnect, `_replay_inflight` delivers the error frame and clears the buffer.

### 8.4 Phone reconnects after response is fully complete

`buffer.complete == True` — the entire response is replayed as a single delta.
This is the most common case: brief network glitch, response finishes while
disconnected, phone comes back.

### 8.5 New connection replaces existing connection (not a disconnect)

`GatewayServer.handler()` already handles this: it closes the old session
before setting up the new one.  The inflight task should **not** be cancelled in
this case — the new session should receive the splice/replay.

### 8.6 Phone sends a new text message while buffer is pending

`_handle_text` calls `_discard_inflight()` first.  The old buffer is discarded,
the old task is cancelled, and the new request starts fresh.  This is correct:
the user has moved on.

### 8.7 OpenClaw WebSocket closes unexpectedly during buffering

The `_stream_deltas` generator in `openclaw_client.py` raises `OpenClawError`
on `ConnectionClosed`.  This is caught by `_run_inflight_stream`, which sets
`buffer.error`.

### 8.8 Inflight task is still running when `_discard_inflight` is called

The task is cancelled via `task.cancel()`.  The `_stream_deltas` generator
receives `CancelledError`, its `finally` block runs `_close_ws()`, and the
OpenClaw connection is cleaned up.

---

## 9. Files Changed

### 9.1 `gateway/server.py` — Primary changes

| Change | Details |
|--------|---------|
| Add `InflightBuffer` dataclass | New class, ~25 lines |
| Add `_inflight_buffer`, `_inflight_task` to `GatewayServer` | 2 new fields |
| Add `GatewayServer._run_inflight_stream()` | New async method, ~40 lines |
| Add `GatewayServer._replay_inflight()` | New async method, ~35 lines |
| Add `GatewayServer._splice_inflight()` | New async method, ~15 lines |
| Add `GatewayServer._discard_inflight()` | New method, ~8 lines |
| Modify `GatewayServer.handler()` | Wire `_on_ready` callback, don't cancel inflight task |
| Add `GatewaySession._on_ready` callback | Optional callback field + invocation in `handle()` |
| Modify `OpenClawResponseHandler` | Add `start_stream()` method returning iterator |
| Modify `GatewaySession._handle_text()` | Use `start_stream()` + create background task for OpenClaw handler; keep existing path for mock handler |

### 9.2 `gateway/openclaw_client.py` — Minor change

| Change | Details |
|--------|---------|
| No changes needed | `send_message()` already returns `AsyncIterator[str]`. The `_close_ws()` in `_stream_deltas`' `finally` block still runs when the generator is garbage-collected or exhausted. |

Actually — one issue: `_stream_deltas()` has a `finally: await self._close_ws()`
that closes the OpenClaw WebSocket after the stream ends.  This is fine: the
inflight task consumes the entire stream, so the finally block runs at end.
If the task is cancelled, `CancelledError` propagates through the async
generator and triggers the finally block — also fine.

### 9.3 `gateway/protocol.py` — No changes

The existing frame types (`status`, `assistant`, `end`, `error`, `connected`)
are sufficient.  No new frame types needed.

### 9.4 `g2_app/src/main.ts` — No changes

The app already handles the replayed frame sequence correctly.

### 9.5 `g2_app/src/conversation.ts` — No changes

`startAssistantStream()` + `appendToLastAssistant()` work regardless of whether
the delta contains the full text or partial chunks.

### 9.6 `g2_app/src/gateway.ts` — No changes

Reconnection logic is already implemented with exponential backoff.

---

## 10. Detailed Pseudocode for Modified `_handle_text`

```python
# GatewaySession (receives a reference to GatewayServer as self._server)

async def _handle_text(self, frame: dict[str, Any]) -> None:
    # Clear any stale inflight buffer from a previous request
    self._server._discard_inflight()

    self._state = SessionState.THINKING
    await self.send_frame({"type": "status", "status": "thinking"})

    # If handler supports start_stream (OpenClaw), use the buffered path
    if isinstance(self._handler, OpenClawResponseHandler):
        try:
            stream = await asyncio.wait_for(
                self._handler.start_stream(frame["message"]),
                timeout=self._timeout,
            )
        except TimeoutError:
            logger.error("OpenClaw request timed out")
            await self.send_frame({"type": "error", ...})
            self._state = SessionState.IDLE
            await self.send_frame({"type": "status", "status": "idle"})
            return
        except OpenClawError as exc:
            logger.error("OpenClaw error: %s", exc)
            await self.send_frame({"type": "error", ...})
            self._state = SessionState.IDLE
            await self.send_frame({"type": "status", "status": "idle"})
            return

        self._state = SessionState.STREAMING
        await self.send_frame({"type": "status", "status": "streaming"})

        buffer = InflightBuffer(user_question=frame["message"])
        self._server._inflight_buffer = buffer
        self._server._inflight_task = asyncio.create_task(
            self._server._run_inflight_stream(stream, buffer),
            name="inflight-stream",
        )
        # Don't await the task here — let session.handle() continue
        # listening for frames. The task runs independently.
        # The session state will be set back to IDLE by the task when done.
        return

    # Fallback: original synchronous path for mock/other handlers
    try:
        await asyncio.wait_for(
            self._handler.handle(frame["message"], self.send_frame),
            timeout=self._timeout,
        )
    except ...:
        ...
    finally:
        self._state = SessionState.IDLE
        await self.send_frame({"type": "status", "status": "idle"})
```

**Important:** After starting the background task, `_handle_text` returns
immediately.  The session stays in `STREAMING` state, which means:
- New `text` frames are rejected (`INVALID_STATE`) — correct.
- New `start_audio` frames are rejected — correct.
- The session continues processing other frame types (e.g., `pong`).

The background task transitions the session back to `IDLE` when done.

---

## 11. Constructor Wiring

`GatewaySession` needs a reference to `GatewayServer` for the inflight buffer
coordination.  Add a `server` parameter:

```python
class GatewaySession:
    def __init__(self, ws, handler, transcriber, timeout, local_audio, server=None):
        ...
        self._server: GatewayServer | None = server
```

And in `GatewayServer.handler()`:

```python
session = GatewaySession(
    ws, self._handler, self._transcriber,
    timeout=self.config.agent_timeout,
    local_audio=self.config.local_audio,
    server=self,
)
```

For the `_on_ready` callback:

```python
session._on_ready = lambda: self._replay_inflight(session)
```

---

## 12. Testing Strategy

### 12.1 New test file: `tests/gateway/test_server_resumption.py`

All tests use the existing `conftest.py` fixtures (`auth_gateway`,
`noauth_gateway`) with `GatewayServer` configured to use a mock handler that
simulates slow/fast OpenClaw streams.

#### Unit tests for `InflightBuffer`

| Test | Description |
|------|-------------|
| `test_buffer_accumulates_deltas` | Append deltas, verify `full_text` |
| `test_buffer_char_count` | Verify `char_count` property |
| `test_buffer_expired` | Create buffer, mock `time.monotonic` to exceed TTL |
| `test_buffer_not_expired` | Verify fresh buffer is not expired |

#### Integration tests: Completed response replay

| Test | Description |
|------|-------------|
| `test_disconnect_during_stream_buffers_response` | Connect, send text, disconnect mid-stream, verify buffer on server |
| `test_reconnect_replays_completed_response` | Disconnect mid-stream, wait for completion, reconnect, verify: `connected → idle → streaming → assistant(full) → end → idle` |
| `test_replayed_response_contains_all_deltas` | Verify the replayed assistant delta matches all original deltas concatenated |
| `test_buffer_cleared_after_replay` | After replay, verify `server._inflight_buffer is None` |

#### Integration tests: Still-streaming splice

| Test | Description |
|------|-------------|
| `test_reconnect_during_stream_splices` | Connect, send text, disconnect, reconnect before stream completes, verify buffered deltas + live deltas arrive |
| `test_splice_delivers_all_deltas` | Verify that the sum of replayed + live deltas equals the full response |

#### Integration tests: Cleanup

| Test | Description |
|------|-------------|
| `test_new_message_clears_buffer` | Disconnect/reconnect, send new text before replay intent, verify old buffer discarded |
| `test_expired_buffer_discarded` | Set TTL to 0, reconnect, verify no replay |
| `test_error_during_stream_replays_error` | OpenClaw errors mid-stream during disconnect, reconnect, verify error frame delivered |

#### Integration tests: Edge cases

| Test | Description |
|------|-------------|
| `test_multiple_disconnects_same_response` | Disconnect, reconnect, disconnect again during replay, reconnect again — full response eventually delivered |
| `test_buffer_size_limit` | Stream exceeds `_BUFFER_MAX_CHARS`, verify truncation and no crash |
| `test_no_buffer_on_normal_completion` | Complete a response without disconnect, verify no lingering buffer |

### 12.2 Mock helpers needed

```python
class _SlowFakeStream:
    """Yields deltas with configurable delays, allowing disconnect simulation."""

    def __init__(self, deltas: list[str], delay: float = 0.1):
        self._deltas = deltas
        self._delay = delay
        self._gate = asyncio.Event()  # can pause/resume
        self._gate.set()

    def __aiter__(self): return self._iter()

    async def _iter(self):
        for d in self._deltas:
            await self._gate.wait()
            await asyncio.sleep(self._delay)
            yield d


class _BufferingOpenClawHandler:
    """OpenClawResponseHandler-like mock that supports start_stream()."""

    def __init__(self, stream: AsyncIterator[str]):
        self._stream = stream

    async def start_stream(self, message: str) -> AsyncIterator[str]:
        return self._stream

    async def handle(self, message, send_frame):
        await send_frame({"type": "status", "status": "streaming"})
        async for delta in self._stream:
            await send_frame({"type": "assistant", "delta": delta})
        await send_frame({"type": "end"})

    async def close(self): pass
```

### 12.3 Existing tests — verify no regressions

Run the full `tests/gateway/` suite.  The mock handler path (`MockResponseHandler`)
is unchanged, so existing tests should pass without modification.

The only risk is the new `server` parameter on `GatewaySession.__init__()`.
It defaults to `None`, so existing tests that construct `GatewaySession` directly
(if any) won't break.

### 12.4 G2 app tests

No G2 app test changes needed.  The app receives the same frame types in the
same order.  Existing `g2_app` tests (conversation, state machine) remain valid.

---

## 13. Implementation Order

1. **Add `InflightBuffer` dataclass** to `gateway/server.py`
2. **Add `start_stream()` to `OpenClawResponseHandler`** — extract iterator return
3. **Add `_server` reference to `GatewaySession`** — constructor + wiring
4. **Add `_on_ready` callback to `GatewaySession.handle()`**
5. **Add inflight methods to `GatewayServer`**: `_run_inflight_stream`, `_replay_inflight`, `_splice_inflight`, `_discard_inflight`
6. **Modify `GatewayServer.handler()`**: wire callback, don't cancel task
7. **Modify `_handle_text()`**: branch on handler type, create background task
8. **Write tests** in `tests/gateway/test_server_resumption.py`
9. **Run full test suite**, fix regressions
10. **Manual end-to-end test**: launch gateway + app, mid-stream disconnect/reconnect

---

## 14. Risk Assessment

| Risk | Mitigation |
|------|------------|
| Background task leaks if never cancelled | TTL on buffer; `_discard_inflight` on new request; task self-terminates after stream ends |
| Race between old session teardown and new session setup | `_current_session` is set atomically; inflight task checks it per-delta |
| `MockResponseHandler` doesn't support `start_stream()` | `_handle_text` checks `isinstance(handler, OpenClawResponseHandler)`; mock path unchanged |
| `_stream_deltas` finally block closes OpenClaw WS | This is correct — runs when generator exhausts or is cancelled |
| Buffer replay arrives before app is ready to display | Replay happens after `connected` + `status:idle`, matching normal flow |
