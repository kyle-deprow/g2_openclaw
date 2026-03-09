# Feature 4: Session Reset Notification and Control — Implementation Plan

## Summary

Add two capabilities to the G2 OpenClaw system:

- **A) Session reset notification** — Gateway detects when an OpenClaw session has been reset (daily 4 AM or explicit) and pushes a `session_reset` frame to the G2 app, which clears local conversation history and shows a brief notification.
- **B) Explicit session reset** — User triggers a reset from the glasses via a triple-tap gesture in idle state, generating a new session key.

---

## 1. New Protocol Frames

### 1.1 `reset_session` (Phone → Gateway)

Request an explicit session reset.

```json
{"type": "reset_session"}
```

| Field  | Type   | Required | Description                  |
|--------|--------|----------|------------------------------|
| `type` | string | yes      | Always `"reset_session"`     |

No additional fields — the Gateway controls key generation. This frame is only valid in `idle` state.

### 1.2 `session_reset` (Gateway → Phone)

Notification that a session reset has occurred.

```json
{"type": "session_reset", "reason": "daily_reset"}
```

| Field    | Type   | Required | Values                              | Description                        |
|----------|--------|----------|-------------------------------------|------------------------------------|
| `type`   | string | yes      | `"session_reset"`                   | Frame type discriminator           |
| `reason` | string | yes      | `"user_request"` \| `"daily_reset"` | Why the session was reset          |

### 1.3 Protocol File Changes

**`gateway/protocol.py`** — add to inbound and outbound registries:

```python
# New inbound frame
class ResetSessionFrame(TypedDict):
    type: Literal["reset_session"]

# Update InboundFrame union
InboundFrame = StartAudioFrame | StopAudioFrame | TextFrame | PongFrame | ResetSessionFrame

# New outbound frame
class SessionResetFrame(TypedDict):
    type: Literal["session_reset"]
    reason: str  # "user_request" | "daily_reset"

# Add to registries
_INBOUND_FIELDS["reset_session"] = []
_OUTBOUND_FIELDS["session_reset"] = ["reason"]
_FIELD_TYPES["reason"] = str
```

**`g2_app/src/protocol.ts`** — mirror on TS side:

```typescript
// New outbound frame (App → Gateway)
export interface ResetSessionFrame {
  type: 'reset_session';
}

// Update OutboundFrame union
export type OutboundFrame = TextFrame | PongFrame | StartAudioFrame | StopAudioFrame | ResetSessionFrame;

// New inbound frame (Gateway → App)
export interface SessionResetFrame {
  type: 'session_reset';
  reason: 'user_request' | 'daily_reset';
}

// Update InboundFrame union to include SessionResetFrame

// Update INBOUND_TYPES, REQUIRED_FIELDS, FIELD_TYPES
// INBOUND_TYPES: add 'session_reset'
// REQUIRED_FIELDS: { session_reset: ['reason'] }
// FIELD_TYPES: { session_reset: { reason: 'string' } }
```

---

## 2. Session Key Management

### 2.1 Current State

The session key is hardcoded as `"agent:claw:g2"` — passed as the default parameter in `OpenClawClient.send_message()`. The Gateway owns one `OpenClawClient` instance shared across all connections.

### 2.2 New Design

The session key becomes **mutable state on the Gateway**, stored in `GatewayServer` (not `GatewaySession`, since session keys persist across WebSocket reconnections).

```python
# In GatewayServer.__init__()
self._session_key: str = "agent:claw:g2"
self._session_date: str = _today_utc()  # "2026-03-07"
```

#### Key Generation

On explicit reset, append a UTC timestamp to make the key unique:

```python
import time

def _generate_session_key() -> str:
    """Generate a new unique session key."""
    return f"agent:claw:g2:{int(time.time())}"

def _today_utc() -> str:
    """Return today's date in UTC as YYYY-MM-DD."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
```

This follows the OpenClaw key anatomy pattern `agent:<agentId>:<channel>` with an added discriminator. The key is opaque to OpenClaw — it creates a new session if the key has never been seen.

#### Key Propagation

The `OpenClawResponseHandler` must receive the current session key from the server. Two options:

**Option A (recommended): Pass key at call time.** Modify `OpenClawResponseHandler.handle()` to accept the session key, and thread it through to `OpenClawClient.send_message()`:

```python
class OpenClawResponseHandler:
    def __init__(self, client: OpenClawClient, get_session_key: Callable[[], str]) -> None:
        self._client = client
        self._get_session_key = get_session_key

    async def handle(self, message: str, send_frame: ...) -> None:
        stream = await self._client.send_message(message, session_key=self._get_session_key())
        ...
```

**Option B: Session key on the client.** Store it as mutable state on `OpenClawClient` and update it before each call. This couples state to the client unnecessarily — Option A is cleaner.

---

## 3. Gateway Implementation

### 3.1 Handling `reset_session` Requests

In `GatewaySession._dispatch()`, add a new branch:

```python
elif frame_type == "reset_session":
    if self._state != SessionState.IDLE:
        await self.send_frame({
            "type": "error",
            "detail": "Cannot reset session while busy",
            "code": ErrorCode.INVALID_STATE,
        })
        return
    await self._server.reset_session("user_request")
```

Note: `GatewaySession` needs a back-reference to `GatewayServer` to call `reset_session()`. Add `server: GatewayServer` to `GatewaySession.__init__()`.

In `GatewayServer`:

```python
async def reset_session(self, reason: str) -> None:
    """Generate a new session key and notify the connected client."""
    old_key = self._session_key
    self._session_key = _generate_session_key()
    self._session_date = _today_utc()
    logger.info("Session reset (%s): %s → %s", reason, old_key, self._session_key)

    # Close the existing OpenClaw connection so next message uses new key
    await self._handler.close()

    # Notify the phone
    if self._current_session is not None:
        await self._current_session.send_frame({
            "type": "session_reset",
            "reason": reason,
        })
```

### 3.2 Daily Reset Detection

The Gateway detects a daily reset by comparing the stored `_session_date` against the current UTC date. Check at two points:

1. **On new WebSocket connection** — in `GatewayServer.handler()`, before creating the session
2. **Before each `text` frame dispatch** — in `GatewaySession._handle_text()`, right before sending to OpenClaw

```python
# In GatewayServer
async def _check_daily_reset(self) -> None:
    """Check if the date has rolled over since the last interaction."""
    today = _today_utc()
    if today != self._session_date:
        logger.info("Date rolled over: %s → %s — triggering daily reset", self._session_date, today)
        await self.reset_session("daily_reset")
```

Call `_check_daily_reset()` from:
- `GatewayServer.handler()` after authentication, before `session.handle()`
- `GatewaySession._handle_text()` at the start (before transitioning to THINKING)

This is simple and robust — no filesystem probing or OpenClaw API polling needed. The date comparison in UTC catches the boundary regardless of when OpenClaw's internal reset fires (4 AM local time is a detail we don't need to match exactly; a fresh key on the next interaction is sufficient).

### 3.3 Modified File Inventory

| File | Changes |
|------|---------|
| `gateway/protocol.py` | Add `ResetSessionFrame`, `SessionResetFrame`, registry entries |
| `gateway/server.py` | Add `_session_key`, `_session_date` to `GatewayServer`; add `reset_session()`, `_check_daily_reset()`; wire `GatewaySession` to dispatch `reset_session`; pass session key to handler |
| `gateway/openclaw_client.py` | No changes needed — already accepts `session_key` parameter |

---

## 4. G2 App Implementation

### 4.1 Gesture: Triple-Tap in Idle

**Rationale:** Single-tap in idle starts recording. Double-tap is used for page toggle / reject / cancel. A **triple-tap** is the natural escalation and easy to discover once mentioned. Long-press is not exposed by the G2 SDK's `OsEventTypeList`.

**Detection strategy:** The G2 SDK does not provide a native `TRIPLE_CLICK_EVENT`. We detect it by counting rapid taps. Track consecutive taps within a time window:

```typescript
// In InputHandler
private _tapCount = 0;
private _tapTimer: ReturnType<typeof setTimeout> | null = null;
private static readonly TRIPLE_TAP_WINDOW = 600; // ms
```

Replace the current immediate `_handleTap()` dispatch with a counting mechanism:

```typescript
private _onTap(): void {
  this._tapCount++;

  if (this._tapTimer) clearTimeout(this._tapTimer);

  // If we already have 3, fire immediately
  if (this._tapCount >= 3) {
    this._handleTripleTap();
    this._tapCount = 0;
    return;
  }

  // Wait to see if more taps come
  this._tapTimer = setTimeout(() => {
    const count = this._tapCount;
    this._tapCount = 0;
    if (count === 1) {
      this._handleTap();
    } else if (count === 2) {
      this._handleDoubleTap();
    }
  }, InputHandler.TRIPLE_TAP_WINDOW);
}
```

> **Important note:** This replaces the current direct dispatch of CLICK_EVENT → `_handleTap()` and DOUBLE_CLICK_EVENT → `_handleDoubleTap()`. Since the G2 SDK already debounces single vs double clicks (the firmware distinguishes them and sends the appropriate event type), an alternative approach is:

**Preferred simpler approach:** Use the SDK's existing DOUBLE_CLICK_EVENT detection and count consecutive double-taps:

Actually, the cleanest approach given the SDK already distinguishes click vs double-click:

**Final recommended approach — Double-tap + hold pattern is unavailable; use a dedicated gesture sequence:**

Since the SDK provides `CLICK_EVENT` and `DOUBLE_CLICK_EVENT` as distinct events (firmware-classified), we can detect triple-tap as **CLICK_EVENT arriving within 400ms of a DOUBLE_CLICK_EVENT** (i.e., the firmware sends double-click, then immediately another click):

```typescript
private _lastDoubleTapTime = 0;
private static readonly TRIPLE_TAP_GRACE = 500; // ms after double-tap

// In _handleEvent:
if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
  this._lastDoubleTapTime = Date.now();
  this._handleDoubleTap();
  return;
}

if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
  const now = Date.now();
  if (now - this._lastDoubleTapTime < InputHandler.TRIPLE_TAP_GRACE) {
    // This click immediately follows a double-tap → treat as triple-tap
    this._lastDoubleTapTime = 0;
    this._handleTripleTap();
    return;
  }
  this._handleTap();
  return;
}
```

> **However**, this has a risk: the third tap triggers both the double-tap action AND the triple-tap. To avoid this side-effect, the double-tap action must be delayed by the grace period. This adds latency to double-tap.

**Simplest viable approach (recommended):** Given the complexity of tap-counting in the face of firmware-level gesture classification, use a **state-specific reinterpretation of double-tap**:

- **In idle state**, double-tap means "reset session" (currently double-tap in idle has no assigned action — it does nothing useful since page toggle only matters during streaming/post-stream states).
- In streaming/thinking, double-tap remains "cancel".
- In confirming, double-tap remains "reject".

Check the current `_handleDoubleTap()`:
```typescript
private _handleDoubleTap(): void {
  const state = this.sm.current;
  if (state === 'confirming') {
    this.rejectTranscription();
  } else if (state === 'thinking' || state === 'streaming') {
    this.cancelResponse();
  }
  // idle → no action currently!
}
```

**Double-tap in idle is currently unused.** This is the ideal slot for session reset — no new gesture detection needed, no latency added.

### 4.2 Final Gesture Map

| State        | Single Tap      | Double Tap           |
|--------------|-----------------|----------------------|
| idle         | Start recording | **Reset session** ←  |
| recording    | Stop recording  | *(no action)*        |
| transcribing | *(ignored)*     | *(ignored)*          |
| confirming   | Confirm         | Reject               |
| thinking     | *(ignored)*     | Cancel               |
| streaming    | *(ignored)*     | Cancel               |
| error        | Dismiss         | *(no action)*        |
| disconnected | Reconnect       | *(no action)*        |

### 4.3 Input Handler Changes

In `InputHandler._handleDoubleTap()`:

```typescript
private _handleDoubleTap(): void {
  const state = this.sm.current;
  if (state === 'idle') {
    this.resetSession();
  } else if (state === 'confirming') {
    this.rejectTranscription();
  } else if (state === 'thinking' || state === 'streaming') {
    this.cancelResponse();
  }
}

/** Request a session reset from the gateway. */
resetSession(): boolean {
  if (this.sm.current !== 'idle') {
    console.warn('[Input] Cannot reset session — state is', this.sm.current);
    return false;
  }
  this.gateway.sendJson({ type: 'reset_session' } as any);
  return true;
}
```

The `as any` cast is needed because `OutboundFrame` union will be extended to include `ResetSessionFrame`.

### 4.4 Frame Routing in `main.ts`

Add a case in `routeFrame()`:

```typescript
case 'session_reset': {
  const reason = (frame as SessionResetFrame).reason;
  console.log('[Main] Session reset (%s)', reason);
  conversation.clear();
  const label = reason === 'daily_reset' ? 'New day, new session' : 'Session reset';
  conversation.addSystem(label);
  // Ensure we're in idle before showing notification
  if (sm.current !== 'idle') sm.transition('idle');
  display.showSessionReset(label).catch(err => console.error('[Main] Display error:', err));
  break;
}
```

### 4.5 Display Notification

Add `showSessionReset(label: string)` to `DisplayManager`:

```typescript
async showSessionReset(label: string): Promise<void> {
  // Show the notification in the transcript area
  await this._updateStatus('OpenClaw  ○ New Session');
  await this._updateTranscript(label);
  await this._updateFooter('Double-tap to reset · Tap to talk');

  // After 2 seconds, revert to normal idle display
  setTimeout(() => {
    this.showIdle().catch(err => console.error('[Display] Error reverting from reset:', err));
  }, 2000);
}
```

This uses the existing `_updateStatus`, `_updateTranscript`, `_updateFooter` upgrade helpers. The notification is brief — 2 seconds — then returns to idle. No full rebuild needed.

### 4.6 Conversation Clear

`ConversationHistory.clear()` already exists and empties the entries array. After reset, `addSystem()` inserts a single "[New day, new session]" or "[Session reset]" entry so the transcript isn't completely blank.

### 4.7 Gateway.ts — OutboundFrame Update

Extend `OutboundFrame` and `sendJson` to allow the new frame type. Since `sendJson` just calls `JSON.stringify`, the main change is the type union:

```typescript
export type OutboundFrame = TextFrame | PongFrame | StartAudioFrame | StopAudioFrame | ResetSessionFrame;
```

### 4.8 Modified File Inventory

| File | Changes |
|------|---------|
| `g2_app/src/protocol.ts` | Add `SessionResetFrame`, `ResetSessionFrame`, update unions & validation maps |
| `g2_app/src/main.ts` | Add `session_reset` case to `routeFrame()` |
| `g2_app/src/input.ts` | Add `resetSession()`, update `_handleDoubleTap()` for idle state |
| `g2_app/src/display.ts` | Add `showSessionReset()` method |
| `g2_app/src/conversation.ts` | No changes (`clear()` already exists) |
| `g2_app/src/gateway.ts` | No structural changes (sendJson already handles arbitrary objects) |
| `g2_app/src/state.ts` | No changes (idle → idle is a no-op, which is fine) |

---

## 5. Session Reset Detection — Strategy

### 5.1 Date-Based Comparison (Recommended)

The Gateway stores `_session_date` (UTC date string). On each new WebSocket connection and before each agent message, compare against `_today_utc()`. If the date has changed, trigger `reset_session("daily_reset")`.

This is **pragmatic and sufficient** because:
- OpenClaw's daily reset creates a new `sessionId` for the same key — but the old messages are gone from context anyway
- A new session key guarantees a clean context regardless of OpenClaw internals
- No dependency on OpenClaw session file paths or API probing
- Works even if OpenClaw's reset hour changes (we just reset on next use after midnight UTC)

### 5.2 Why Not Probe OpenClaw?

- OpenClaw has no public "session status" API — only `agent` and `send` RPC methods
- Checking session file timestamps requires knowledge of OpenClaw's internal directory layout (`~/.openclaw/agents/claw/sessions/`) which is an implementation detail
- The date comparison is simpler, has zero network overhead, and covers the use case

### 5.3 Timing Behavior

| Scenario | Behavior |
|----------|----------|
| User sends message at 3:59 AM | Uses current session key |
| User sends message at 4:01 AM | Gateway sees date changed → `daily_reset` → new key → message uses new key |
| User reconnects after midnight | `handler()` checks date → reset on connection |
| Gateway restarts at 2 PM | `_session_date` initialises to today → no immediate reset |
| Gateway restarts after midnight | `_session_date` = new day → first connection of old day that reconnects won't stale |

**Note:** The 4 AM reset from OpenClaw and the UTC midnight check from the Gateway may not align exactly. This is fine: if OpenClaw resets at 4 AM local but the Gateway doesn't generate a new key until the next message, the first message goes to the old (now-empty) session. With the new key generated on date roll, the user gets a fresh session within one message of the boundary. If exact alignment is needed later, `_session_date` tracking can be changed to compare against local 4 AM instead of UTC midnight.

---

## 6. Edge Cases

### 6.1 Reset During Active Streaming

If `reset_session` arrives while state is STREAMING or THINKING:

- **Gateway side:** Return `INVALID_STATE` error. The user must wait for the current cycle to complete.
- **App side:** `resetSession()` checks `sm.current !== 'idle'` and returns false.
- **Daily reset during streaming:** `_check_daily_reset()` is called before `_handle_text()`. If a stream is already in progress, the check happens before the *next* message, not mid-stream. So this is safe — the reset applies to the next interaction.

### 6.2 Reset When Disconnected

- **App side:** `sendJson` is a no-op when WebSocket is not open. Double-tap in disconnected state triggers reconnect, not reset.
- **Gateway side:** On reconnection, `handler()` checks `_check_daily_reset()` and sends `session_reset` if appropriate, right after the `connected` frame.

### 6.3 Multiple Rapid Resets

- Each reset generates a new unique key (timestamp-based), so rapid resets are idempotent from a state perspective.
- The Gateway logs each reset for debugging.
- The G2 app clears conversation and shows notification each time. Back-to-back resets just re-trigger the same 2-second notification (harmless).

### 6.4 Reset Races with Authentication

The `session_reset` frame is sent after the `connected` + `status:idle` frames. Ordering:
```
connected → status:idle → session_reset (if daily reset detected)
```
The app handles `session_reset` in any state ≥ idle. The `routeFrame()` `session_reset` case forces a transition to idle if not already there.

### 6.5 Gateway Restart

On restart, `_session_key` resets to `"agent:claw:g2"` and `_session_date` initialises to today. If the user was previously using a timestamped key, this effectively creates a new session (different key). This is acceptable — a Gateway restart is already a clean break.

**Optional improvement (future):** Persist `_session_key` to a small JSON file at the project root (e.g., `gateway/.session_state.json`). Skip for v1.

### 6.6 OpenClaw Unavailable

Session reset is purely a Gateway-side key management operation. It does not contact OpenClaw. If OpenClaw is down, the reset still succeeds — the next message will fail with `OPENCLAW_ERROR` as usual, but the session key will be fresh.

---

## 7. Testing Strategy

### 7.1 Python Gateway Tests (`tests/gateway/`)

#### `test_protocol.py` — Frame Validation

```python
# New test cases
def test_inbound_reset_session_valid():
    """reset_session parses with no extra fields."""
    frame = parse_text_frame('{"type":"reset_session"}')
    assert frame == {"type": "reset_session"}

def test_outbound_session_reset_valid():
    """session_reset validates with reason field."""
    validate_outbound({"type": "session_reset", "reason": "daily_reset"})

def test_outbound_session_reset_missing_reason():
    """session_reset without reason raises ProtocolError."""
    with pytest.raises(ProtocolError):
        validate_outbound({"type": "session_reset"})
```

#### `test_server.py` — Session Reset Flow

```python
class TestSessionReset:
    async def test_reset_session_in_idle_sends_session_reset(self, auth_gateway):
        """reset_session in idle generates new key and notifies client."""
        url, server = auth_gateway
        async with await _auth_connect(url) as ws:
            await ws.recv()  # connected
            await ws.recv()  # status:idle
            await ws.send(json.dumps({"type": "reset_session"}))
            frame = await _recv_json(ws)
            assert frame == {"type": "session_reset", "reason": "user_request"}

    async def test_reset_session_while_busy_returns_error(self, auth_gateway):
        """reset_session during non-idle state returns INVALID_STATE."""
        url, server = auth_gateway
        async with await _auth_connect(url) as ws:
            await ws.recv()  # connected
            await ws.recv()  # status:idle
            # Start a text request to enter THINKING state
            await ws.send(json.dumps({"type": "text", "message": "hello"}))
            await ws.recv()  # status:thinking
            await ws.send(json.dumps({"type": "reset_session"}))
            frame = await _recv_json(ws)
            assert frame["type"] == "error"
            assert frame["code"] == "INVALID_STATE"

    async def test_daily_reset_on_reconnect(self, auth_gateway):
        """Date rollover triggers daily_reset on next connection."""
        url, server = auth_gateway
        # Simulate date rollover
        server._session_date = "2026-03-06"  # yesterday
        async with await _auth_connect(url) as ws:
            await ws.recv()  # connected
            await ws.recv()  # status:idle
            frame = await _recv_json(ws)
            assert frame == {"type": "session_reset", "reason": "daily_reset"}

    async def test_session_key_changes_on_reset(self, auth_gateway):
        """Session key is different after reset."""
        _, server = auth_gateway
        old_key = server._session_key
        await server.reset_session("user_request")
        assert server._session_key != old_key
        assert server._session_key.startswith("agent:claw:g2:")
```

#### `test_session_key_generation.py` — Unit Tests

```python
def test_generate_session_key_format():
    key = _generate_session_key()
    assert key.startswith("agent:claw:g2:")
    # Timestamp portion is a valid integer
    ts = key.split(":")[-1]
    assert ts.isdigit()

def test_generate_session_key_unique():
    k1 = _generate_session_key()
    time.sleep(0.01)
    k2 = _generate_session_key()
    assert k1 != k2

def test_today_utc_format():
    result = _today_utc()
    assert len(result) == 10  # YYYY-MM-DD
    datetime.strptime(result, "%Y-%m-%d")
```

### 7.2 TypeScript G2 App Tests (`g2_app/src/__tests__/`)

#### `protocol.test.ts` — Frame Parsing

```typescript
it('parses session_reset frame', () => {
  const frame = parseFrame('{"type":"session_reset","reason":"daily_reset"}');
  expect(frame).toEqual({ type: 'session_reset', reason: 'daily_reset' });
});

it('rejects session_reset without reason', () => {
  expect(() => parseFrame('{"type":"session_reset"}')).toThrow('missing required field');
});
```

#### `input.test.ts` — Gesture Handling

```typescript
describe('session reset', () => {
  it('double-tap in idle sends reset_session', () => {
    sm._current = 'idle';
    handler._handleEvent(DOUBLE_CLICK);
    expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'reset_session' });
  });

  it('double-tap in recording does NOT send reset_session', () => {
    sm._current = 'recording';
    handler._handleEvent(DOUBLE_CLICK);
    expect(gateway.sendJson).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'reset_session' })
    );
  });

  it('double-tap in confirming still rejects transcription', () => {
    sm._current = 'confirming';
    handler._handleEvent(DOUBLE_CLICK);
    // Should reject, not reset
    expect(sm.transition).toHaveBeenCalledWith('idle');
  });
});
```

#### `main.test.ts` — Frame Routing

```typescript
it('session_reset clears conversation and shows notification', () => {
  // Simulate receiving session_reset
  routeFrame({ type: 'session_reset', reason: 'daily_reset' } as any);
  expect(conversation.clear).toHaveBeenCalled();
  expect(display.showSessionReset).toHaveBeenCalledWith('New day, new session');
});
```

### 7.3 Integration Test

Add to `tests/integration/`:

```python
async def test_full_reset_cycle():
    """End-to-end: connect, chat, reset, verify new session."""
    # 1. Connect to gateway
    # 2. Send text message, receive response (verifies old session works)
    # 3. Send reset_session
    # 4. Receive session_reset with reason "user_request"
    # 5. Send another text message (verifies new session works)
```

---

## 8. Implementation Order

1. **Protocol frames** — `gateway/protocol.py`, `g2_app/src/protocol.ts` + tests
2. **Session key management** — `GatewayServer._session_key`, `_generate_session_key()`, `_today_utc()` + unit tests
3. **Gateway dispatch** — `reset_session` handling in `server.py`, `reset_session()` method, `_check_daily_reset()` + server tests
4. **Handler key propagation** — Update `OpenClawResponseHandler` to use `get_session_key` callback
5. **G2 App protocol** — `protocol.ts` parsing + tests
6. **G2 App display** — `showSessionReset()` in `display.ts`
7. **G2 App input** — `resetSession()` + double-tap-in-idle in `input.ts` + tests
8. **G2 App routing** — `session_reset` case in `main.ts` + tests
9. **Integration test** — Full cycle test
10. **Protocol doc update** — Add new frames to `docs/design/protocol.md`

Steps 1–3 can proceed in parallel with steps 5–7 (Python + TypeScript are independent).

---

## 9. Doc Updates

- [docs/design/protocol.md](docs/design/protocol.md) — Add `reset_session` and `session_reset` to frame tables in §1.1 and §1.2
- [docs/design/gateway.md](docs/design/gateway.md) — Document session key lifecycle in §2.3
- [g2_app/src/input.ts](g2_app/src/input.ts) — Update JSDoc header with new gesture mapping

---

## Appendix: Message Sequence Diagrams

### A. User-Initiated Reset

```
   G2 Glasses          iPhone App           Gateway             OpenClaw
       │                   │                   │                   │
       │  double-tap       │                   │                   │
       │──────────────────►│                   │                   │
       │                   │  reset_session    │                   │
       │                   │──────────────────►│                   │
       │                   │                   │─ generate key     │
       │                   │                   │─ close OC conn    │
       │                   │  session_reset    │                   │
       │                   │◄──────────────────│                   │
       │                   │─ clear history    │                   │
       │  "Session reset"  │                   │                   │
       │◄──────────────────│                   │                   │
       │  (2s) idle        │                   │                   │
       │◄──────────────────│                   │                   │
```

### B. Daily Reset Detection

```
   G2 Glasses          iPhone App           Gateway             OpenClaw
       │                   │                   │                   │
       │   (4 AM passes — OpenClaw resets internally)              │
       │                   │                   │                   │
       │  tap (next day)   │                   │                   │
       │──────────────────►│                   │                   │
       │                   │  start_audio      │                   │
       │                   │──────────────────►│                   │
       │                   │                   │─ check date       │
       │                   │                   │─ date changed!    │
       │                   │                   │─ generate key     │
       │                   │  session_reset    │                   │
       │                   │◄──────────────────│  (reason:daily)   │
       │                   │─ clear history    │                   │
       │  "New session"    │                   │                   │
       │◄──────────────────│                   │                   │
       │                   │  status:recording │                   │
       │                   │◄──────────────────│                   │
       │                   │  ... normal flow  │                   │
```
