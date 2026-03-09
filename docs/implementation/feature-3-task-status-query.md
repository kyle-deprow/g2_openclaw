# Feature 3: Task Status Query — Implementation Plan

## Overview

Add a `status_request` inbound frame that triggers the Gateway to reply with an enhanced `status` frame containing optional metadata: the current question being processed, elapsed time, and processing phase. This gives the G2 user a quick "what's happening?" mechanism without composing a voice query.

---

## 1. New Protocol Frames

### 1.1 `status_request` (Phone → Gateway)

A zero-payload request frame. No fields beyond `type`.

**Python (`gateway/protocol.py`)**

```python
class StatusRequestFrame(TypedDict):
    type: Literal["status_request"]
```

Add to `InboundFrame` union and `_INBOUND_FIELDS`:

```python
InboundFrame = StartAudioFrame | StopAudioFrame | TextFrame | PongFrame | StatusRequestFrame

_INBOUND_FIELDS: dict[str, list[str]] = {
    ...
    "status_request": [],
}
```

**TypeScript (`g2_app/src/protocol.ts`)**

```typescript
export interface StatusRequestFrame {
  type: 'status_request';
}

export type OutboundFrame = TextFrame | PongFrame | StartAudioFrame | StopAudioFrame | StatusRequestFrame;
```

### 1.2 Enhanced `status` Response (Gateway → Phone)

Add three **optional** fields to the existing `StatusFrame`. This is backward-compatible — the frame is only enriched when sent in response to a `status_request`, or when the Gateway has metadata to attach. Regular state-transition status frames continue to carry just `status`.

**Python (`gateway/protocol.py`)**

```python
class StatusFrame(TypedDict):
    type: Literal["status"]
    status: StatusState
    question: NotRequired[str]       # The user question currently being processed
    elapsedMs: NotRequired[int]      # Milliseconds since processing started
    phase: NotRequired[str]          # Human-readable phase, e.g. "Transcribing audio", "Waiting for OpenClaw"
```

The `question`, `elapsedMs`, and `phase` fields are **only included when non-null**. Existing outbound validation remains relaxed for `NotRequired` fields — `_OUTBOUND_FIELDS` still lists only `["status"]` as required.

**TypeScript (`g2_app/src/protocol.ts`)**

```typescript
export interface StatusFrame {
  type: 'status';
  status: GatewayStatus;
  question?: string;     // Current question being processed (if any)
  elapsedMs?: number;    // Milliseconds since processing began
  phase?: string;        // Human-readable description
}
```

The TypeScript `parseFrame()` function already builds a clean object from known fields. Update the `knownFields` set for `status` frames to also copy `question`, `elapsedMs`, and `phase` when present (see Section 3.1).

---

## 2. Gateway Implementation

### 2.1 Task Tracking State in `GatewaySession`

Add three private fields to `GatewaySession.__init__()` in `gateway/server.py`:

```python
self._current_question: str | None = None
self._task_start: float | None = None  # monotonic time via asyncio loop
```

**Set on entry:**

| Transition | `_current_question` | `_task_start` |
|---|---|---|
| `_handle_text(frame)` — enters THINKING | `frame["message"]` | `loop.time()` |
| `_handle_stop_audio()` — enters TRANSCRIBING | `None` (no question yet) | `loop.time()` |

**Clear on exit to IDLE:**

Every path that sets `self._state = SessionState.IDLE` also sets:

```python
self._current_question = None
self._task_start = None
```

This touches the `finally` block of `_handle_text`, the various error/empty paths in `_handle_stop_audio`, and the timeout/overflow paths in `_handle_binary`.

### 2.2 Phase Descriptions

A simple helper method on `GatewaySession`:

```python
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
```

### 2.3 Building the Enhanced Status Frame

A helper that builds the status dict with optional metadata:

```python
def _build_status_frame(self, *, include_metadata: bool = False) -> dict[str, Any]:
    """Build a status frame, optionally including task metadata."""
    frame: dict[str, Any] = {"type": "status", "status": self._state.value}
    if include_metadata:
        if self._current_question:
            frame["question"] = self._current_question[:200]  # truncate for display
        if self._task_start is not None:
            elapsed = asyncio.get_event_loop().time() - self._task_start
            frame["elapsedMs"] = int(elapsed * 1000)
        phase = self._phase_description()
        if phase:
            frame["phase"] = phase
    return frame
```

### 2.4 Handling `status_request` in `_dispatch`

Add a new branch in `_dispatch()`:

```python
elif frame_type == "status_request":
    await self._handle_status_request()
```

The handler itself:

```python
async def _handle_status_request(self) -> None:
    """Respond with current status and optional task metadata."""
    frame = self._build_status_frame(include_metadata=True)
    await self.send_frame(frame)
```

Key behavior: **`status_request` is valid in ANY state** — it never returns an error. When IDLE, it returns `{"type": "status", "status": "idle"}` with no metadata. When busy, it includes question/elapsed/phase.

### 2.5 Protocol Validation Update

The enhanced status frame has optional extra fields. The existing `validate_outbound()` only checks required fields, so `question`, `elapsedMs`, and `phase` pass through without changes to `_OUTBOUND_FIELDS`. However, add the new field types for type-checking:

```python
_FIELD_TYPES: dict[str, type] = {
    ...
    "question": str,
    "elapsedMs": int,
    "phase": str,
}
```

---

## 3. G2 App Implementation

### 3.1 Protocol Update (`g2_app/src/protocol.ts`)

1. Add `StatusRequestFrame` to `OutboundFrame` union.
2. Add optional fields to `StatusFrame` interface.
3. Update `parseFrame()` to preserve the optional `status` fields.

In `parseFrame()`, after building the clean object for `status` frames, also copy the optional metadata fields if present:

```typescript
// After building clean object...
if (clean.type === 'status') {
  if (typeof frame.question === 'string') clean.question = frame.question;
  if (typeof frame.elapsedMs === 'number') clean.elapsedMs = frame.elapsedMs;
  if (typeof frame.phase === 'string') clean.phase = frame.phase;
}
```

### 3.2 Gateway Class (`g2_app/src/gateway.ts`)

Add a convenience method:

```typescript
requestStatus(): void {
  this.sendJson({ type: 'status_request' });
}
```

### 3.3 Trigger: On Reconnect

In `main.ts`, the `routeEvent` handler for `'connected'` fires when the WebSocket reconnects. After the Gateway sends `connected` + `status:idle`, send a `status_request` to learn if there's in-flight work (relevant when the app reconnects while Gateway was busy with another client or a long-running task):

```typescript
case 'connected':
  break;  // currently a no-op
```

Change to:

```typescript
case 'connected':
  // Query current gateway status in case it's mid-task
  gateway.requestStatus();
  break;
```

Note: The `status_request` will be sent after the WebSocket `onopen` fires. The Gateway may not have sent the `connected` frame yet at that point. That's fine — the `status_request` will be queued and processed after the handshake frames. The response is an enriched `status` frame that routes through the existing `case 'status':` handler in `routeFrame`.

### 3.4 Trigger: On Specific Gesture (Optional, Future)

A triple-tap or long-press gesture could trigger `gateway.requestStatus()`. This is **not required for v1** — the reconnect trigger is sufficient. If added later, wire it in `InputHandler._handleEvent()`:

```typescript
if (eventType === OsEventTypeList.TRIPLE_CLICK_EVENT) {
  this.gateway.requestStatus();
  return;
}
```

### 3.5 Display Handling

The enriched status frame routes through the existing `case 'status':` in `routeFrame`. Two options for displaying the metadata:

**Option A (Minimal, recommended for v1):** Log the metadata but don't change the display. The status bar already shows "Thinking…", "Streaming…", etc. The enriched fields serve as reconnect context — the important thing is that the state machine transitions correctly.

**Option B (Enhanced):** When a `status` frame has `question` and/or `phase`, show them in the footer or as a brief overlay. For example, during `thinking` with a `question` field, the status bar could read `"Thinking: What is the capital of..."` (truncated).

Recommended: **Start with Option A**, iterate to Option B once the protocol is proven.

For Option A, the only display change is in the `case 'status'` → `case 'thinking'` and `case 'streaming'` sub-branches:

```typescript
case 'thinking':
  display.showThinking(frame.question).catch(...);
  break;
case 'streaming':
  conversation.startAssistantStream();
  display.showStreaming(frame.question).catch(...);
  break;
```

Where `showThinking(question?: string)` and `showStreaming(question?: string)` optionally include the question in the status bar text. These are small changes to `DisplayManager` in `display.ts`.

---

## 4. Integration with Feature 1 (History Replay)

If Feature 1 adds a `history_replay` mechanism on reconnect, the `status_request` should be sent **after** history replay completes, so the user sees the conversation context before the current-task overlay.

Sequencing on reconnect:

1. Gateway sends `connected` → `status:idle`
2. App receives frames, transitions to `idle`
3. **(Feature 1)** App requests history replay → Gateway sends past turns
4. App sends `status_request` → Gateway responds with enriched `status`
5. App updates display with current task info (if any)

If history replay is not yet implemented, step 3 is skipped and `status_request` fires immediately on reconnect via `routeEvent('connected')` as described in 3.3.

**No conflict**: `status_request` is a read-only query — it doesn't mutate Gateway state. It can be sent at any time, even during an active stream. The response is a standard `status` frame that the app already knows how to handle.

---

## 5. Edge Cases

| Scenario | Behavior |
|---|---|
| **IDLE — nothing happening** | Response: `{"type":"status","status":"idle"}`. No metadata fields. |
| **RECORDING** | Response includes `elapsedMs` (time since recording started) and `phase: "Recording audio"`. No `question` (not known yet). |
| **TRANSCRIBING** | Response includes `elapsedMs` and `phase: "Transcribing audio"`. No `question`. |
| **THINKING** | Response includes `question` (the user's text), `elapsedMs`, and `phase: "Waiting for OpenClaw"`. |
| **STREAMING** | Response includes `question`, `elapsedMs`, and `phase: "Streaming response"`. |
| **Rapid status_request spam** | No rate-limiting needed — the response is trivially cheap (reads local state, no I/O). If abuse is a concern later, add a simple cooldown. |
| **status_request during disconnect** | The app can't send frames while disconnected. If sent right at the boundary, the WebSocket library will error and the Gateway ignores it. No special handling. |
| **status_request before auth** | Impossible in the current single-connection model — the Gateway won't dispatch frames until auth succeeds. If the frame arrives pre-auth, it's consumed by the auth handshake and rejected (wrong type). |
| **Multiple clients** | The Gateway is single-connection. Reconnecting replaces the session. The new session starts IDLE, so `status_request` returns idle. The old session's in-flight task context is lost, which is correct — the new connection owns the session. |
| **Session replacement while STREAMING** | Gateway replaces session, closes old handler. New session starts IDLE. `status_request` returns idle. |

---

## 6. Testing Strategy

### 6.1 Python — Gateway Tests

**File: `tests/gateway/test_protocol.py`**

Add to `TestRoundTripFrames.test_inbound_round_trip` parametrize list:
```python
{"type": "status_request"},
```

Add test for enhanced status frame validation:
```python
def test_enhanced_status_frame_validates(self) -> None:
    frame = {"type": "status", "status": "thinking", "question": "hello", "elapsedMs": 1234, "phase": "Waiting for OpenClaw"}
    validate_outbound(frame)
```

**File: `tests/gateway/test_server.py`** — New test class `TestStatusRequest`:

| Test | Description |
|---|---|
| `test_status_request_when_idle` | Send `status_request` in IDLE → receive `{"type":"status","status":"idle"}` with no metadata. |
| `test_status_request_during_thinking` | Send `text` message, then immediately send `status_request` before response completes → receive enriched status with `question`, `elapsedMs`, `phase`. |
| `test_status_request_during_streaming` | Use a slow mock handler, send `status_request` mid-stream → verify `question` and `phase: "Streaming response"`. |
| `test_status_request_multiple_times` | Send `status_request` twice rapidly → receive two valid status frames. |
| `test_status_request_does_not_change_state` | Verify `_state` is unchanged after handling `status_request`. |

Implementation pattern (matches existing test style):

```python
class TestStatusRequest:
    async def test_status_request_when_idle(
        self, auth_gateway: tuple[str, GatewayServer]
    ) -> None:
        url, _ = auth_gateway
        ws = await _auth_connect(url)
        async with ws:
            await ws.recv()  # connected
            await ws.recv()  # status:idle

            await ws.send(json.dumps({"type": "status_request"}))
            resp = await _recv_json(ws)
            assert resp == {"type": "status", "status": "idle"}
```

For the "during thinking" test, use a `SlowMockHandler` that sleeps long enough to send a `status_request` mid-processing:

```python
class SlowMockHandler:
    async def handle(self, message, send_frame):
        await send_frame({"type": "status", "status": "streaming"})
        await asyncio.sleep(5)  # long enough to query status
        await send_frame({"type": "assistant", "delta": "done"})
        await send_frame({"type": "end"})

    async def close(self):
        pass
```

### 6.2 Python — Unit Tests

**File: `tests/gateway/test_server.py`** or new `tests/gateway/test_status_request.py`:

Unit-test `_build_status_frame()` and `_phase_description()` directly on a `GatewaySession` instance with a mocked WebSocket:

```python
def test_build_status_frame_idle_no_metadata():
    session = GatewaySession(ws=MagicMock(), ...)
    frame = session._build_status_frame(include_metadata=True)
    assert frame == {"type": "status", "status": "idle"}
    assert "question" not in frame
    assert "elapsedMs" not in frame

def test_build_status_frame_thinking_with_metadata():
    session = GatewaySession(ws=MagicMock(), ...)
    session._state = SessionState.THINKING
    session._current_question = "What is 2+2?"
    session._task_start = asyncio.get_event_loop().time() - 1.5  # 1.5s ago
    frame = session._build_status_frame(include_metadata=True)
    assert frame["question"] == "What is 2+2?"
    assert frame["elapsedMs"] >= 1400  # ~1500ms with tolerance
    assert frame["phase"] == "Waiting for OpenClaw"
```

### 6.3 TypeScript — G2 App Tests

**File: `g2_app/src/__tests__/protocol.test.ts`**

```typescript
test('parseFrame accepts status with optional metadata', () => {
  const raw = '{"type":"status","status":"thinking","question":"hello","elapsedMs":1234,"phase":"Waiting for OpenClaw"}';
  const frame = parseFrame(raw);
  expect(frame).toEqual({
    type: 'status',
    status: 'thinking',
    question: 'hello',
    elapsedMs: 1234,
    phase: 'Waiting for OpenClaw',
  });
});

test('parseFrame accepts status without metadata', () => {
  const raw = '{"type":"status","status":"idle"}';
  const frame = parseFrame(raw);
  expect(frame).toEqual({ type: 'status', status: 'idle' });
});
```

**File: `g2_app/src/__tests__/gateway.test.ts`**

```typescript
test('requestStatus sends status_request frame', () => {
  // ... setup mock WebSocket
  gateway.requestStatus();
  expect(mockWs.send).toHaveBeenCalledWith(JSON.stringify({ type: 'status_request' }));
});
```

**File: `g2_app/src/__tests__/main.test.ts`**

```typescript
test('reconnect event triggers status request', () => {
  // simulate routeEvent('connected')
  // verify gateway.requestStatus() was called
});
```

### 6.4 Integration Test (Optional)

**File: `tests/integration/test_status_query.py`**

End-to-end: connect WebSocket client → send text → send `status_request` during processing → verify enriched response → verify normal flow completes.

---

## 7. Implementation Order

| Step | Component | Files | Effort |
|---|---|---|---|
| 1 | Protocol frames (Python) | `gateway/protocol.py` | ~15 min |
| 2 | Protocol tests (Python) | `tests/gateway/test_protocol.py` | ~10 min |
| 3 | Task tracking + handler | `gateway/server.py` | ~30 min |
| 4 | Server tests | `tests/gateway/test_server.py` | ~30 min |
| 5 | Protocol frames (TypeScript) | `g2_app/src/protocol.ts` | ~10 min |
| 6 | Gateway method + reconnect trigger | `g2_app/src/gateway.ts`, `g2_app/src/main.ts` | ~15 min |
| 7 | TypeScript tests | `g2_app/src/__tests__/protocol.test.ts`, `gateway.test.ts` | ~20 min |
| 8 | Display enhancement (Option B, if desired) | `g2_app/src/display.ts`, `g2_app/src/main.ts` | ~20 min |

**Total estimate: ~2.5 hours**

---

## 8. Files Changed Summary

| File | Change |
|---|---|
| `gateway/protocol.py` | Add `StatusRequestFrame`, add to `InboundFrame` union, update `_INBOUND_FIELDS`, add field types |
| `gateway/server.py` | Add `_current_question`, `_task_start` to `GatewaySession.__init__()`. Add `_phase_description()`, `_build_status_frame()`, `_handle_status_request()`. Wire in `_dispatch()`. Set/clear tracking fields in `_handle_text()` and `_handle_stop_audio()`. |
| `g2_app/src/protocol.ts` | Add `StatusRequestFrame`, add to `OutboundFrame` union, add optional fields to `StatusFrame`, update `parseFrame()` |
| `g2_app/src/gateway.ts` | Add `requestStatus()` method |
| `g2_app/src/main.ts` | Call `gateway.requestStatus()` on reconnect event |
| `g2_app/src/display.ts` | *(Optional)* Accept optional `question` param in `showThinking()` / `showStreaming()` |
| `tests/gateway/test_protocol.py` | Add round-trip test for `status_request`, enhanced status validation |
| `tests/gateway/test_server.py` | Add `TestStatusRequest` class with 5 tests |
| `g2_app/src/__tests__/protocol.test.ts` | Add parse tests for enhanced status frame |
| `g2_app/src/__tests__/gateway.test.ts` | Add `requestStatus` test |
| `g2_app/src/__tests__/main.test.ts` | Add reconnect-triggers-status-request test |
