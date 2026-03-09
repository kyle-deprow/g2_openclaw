# Feature: Session Menu — Protocol & Integration Plan

> **Scope:** New protocol frames, session metadata model, frame flows, validation
> rules, backward compatibility, and integration test plan for the Session Menu
> overlay on the G2 app.

---

## 1. New Frame Definitions

### 1.1 Overview

Five new frame types are introduced. All are JSON text frames on the existing
Phone ↔ Gateway WebSocket connection.

| Frame | Direction | Purpose |
|---|---|---|
| `session_list_request` | Client → Gateway | Request the full list of sessions |
| `session_list` | Gateway → Client | Respond with session metadata array |
| `switch_session` | Client → Gateway | Switch active session to a given key |
| `create_session` | Client → Gateway | Create a new blank session |
| `session_switched` | Gateway → Client | Confirm switch with new session metadata + history |

### 1.2 Python TypedDict Definitions (`gateway/protocol.py`)

```python
# --- Phone → Gateway ---

class SessionListRequestFrame(TypedDict):
    type: Literal["session_list_request"]


class SwitchSessionFrame(TypedDict):
    type: Literal["switch_session"]
    sessionKey: str


class CreateSessionFrame(TypedDict):
    type: Literal["create_session"]
    label: NotRequired[str]  # optional user-given name


# --- Gateway → Phone ---

class SessionEntryDict(TypedDict):
    sessionKey: str
    label: str
    lastActivity: int          # Unix ms (UTC)
    messageCount: int
    preview: str               # last assistant message, truncated to 80 chars
    active: bool               # True if this is the currently active session


class SessionListFrame(TypedDict):
    type: Literal["session_list"]
    sessions: list[SessionEntryDict]


class SessionSwitchedFrame(TypedDict):
    type: Literal["session_switched"]
    sessionKey: str
    sessionId: str
    label: str
    entries: list[HistoryEntryDict]  # reuses existing HistoryEntryDict
```

Register in `_INBOUND_FIELDS`:

```python
_INBOUND_FIELDS["session_list_request"] = []
_INBOUND_FIELDS["switch_session"] = ["sessionKey"]
_INBOUND_FIELDS["create_session"] = []
```

Register in `_OUTBOUND_FIELDS`:

```python
_OUTBOUND_FIELDS["session_list"] = ["sessions"]
_OUTBOUND_FIELDS["session_switched"] = ["sessionKey", "sessionId", "label", "entries"]
```

Add to `_FIELD_TYPES`:

```python
_FIELD_TYPES["sessionKey"] = str   # already present
_FIELD_TYPES["label"] = str
_FIELD_TYPES["sessions"] = list
_FIELD_TYPES["active"] = bool
_FIELD_TYPES["lastActivity"] = int
_FIELD_TYPES["messageCount"] = int
_FIELD_TYPES["preview"] = str
```

Update union types:

```python
InboundFrame = (
    StartAudioFrame | StopAudioFrame | TextFrame
    | PongFrame | StatusRequestFrame | ResetSessionFrame
    | SessionListRequestFrame | SwitchSessionFrame | CreateSessionFrame
)
```

### 1.3 TypeScript Interface & Validation (`g2_app/src/protocol.ts`)

```typescript
// --- Outbound (App → Gateway) ---

export interface SessionListRequestFrame {
  type: 'session_list_request';
}

export interface SwitchSessionFrame {
  type: 'switch_session';
  sessionKey: string;
}

export interface CreateSessionFrame {
  type: 'create_session';
  label?: string;
}

export type OutboundFrame =
  | TextFrame | PongFrame | StartAudioFrame | StopAudioFrame
  | StatusRequestFrame | ResetSessionFrame
  | SessionListRequestFrame | SwitchSessionFrame | CreateSessionFrame;


// --- Inbound (Gateway → App) ---

export interface SessionEntry {
  sessionKey: string;
  label: string;
  lastActivity: number;   // Unix ms
  messageCount: number;
  preview: string;
  active: boolean;
}

export interface SessionListFrame {
  type: 'session_list';
  sessions: SessionEntry[];
}

export interface SessionSwitchedFrame {
  type: 'session_switched';
  sessionKey: string;
  sessionId: string;
  label: string;
  entries: HistoryEntry[];
}

export type InboundFrame =
  | StatusFrame | TranscriptionFrame | AssistantDelta | EndFrame
  | ErrorFrame | ConnectedFrame | PingFrame | HistoryFrame
  | SessionResetFrame
  | SessionListFrame | SessionSwitchedFrame;
```

Add to `INBOUND_TYPES`, `REQUIRED_FIELDS`, `FIELD_TYPES`:

```typescript
INBOUND_TYPES.add('session_list');
INBOUND_TYPES.add('session_switched');

REQUIRED_FIELDS['session_list'] = ['sessions'];
REQUIRED_FIELDS['session_switched'] = ['sessionKey', 'sessionId', 'label', 'entries'];

FIELD_TYPES['session_list'] = { sessions: 'object' };
FIELD_TYPES['session_switched'] = {
  sessionKey: 'string', sessionId: 'string', label: 'string', entries: 'object',
};
```

### 1.4 `parseFrame` Extensions

Add validation branches in `parseFrame()` for the two new inbound types:

- **`session_list`**: Validate `sessions` is an array. Filter entries: each must
  have `sessionKey` (string), `label` (string), `lastActivity` (number),
  `messageCount` (number), `preview` (string), `active` (boolean). Silently drop
  malformed entries (same pattern as `history.entries`).

- **`session_switched`**: Validate `entries` array with the same logic as the
  existing `history` frame. Validate `sessionKey`, `sessionId`, `label` are
  strings.

---

## 2. Session Metadata Model

### 2.1 Data Available on Disk

OpenClaw stores sessions at `~/.openclaw/agents/<agentId>/sessions/`:

- `sessions.json` — maps session keys → `{ sessionId, updatedAt, ... }`
- `<sessionId>.jsonl` — append-only transcript (one JSON object per line)

The existing `session_resolver.py` reads a single key. The existing
`session_history.py` reads and parses JSONL transcripts.

### 2.2 `SessionEntry` Fields

| Field | Type | Source | Notes |
|---|---|---|---|
| `sessionKey` | `string` | Key in `sessions.json` | e.g. `"agent:claw:g2"`, `"agent:claw:g2:1740000000:a1b2c3"` |
| `label` | `string` | Derived (see §2.3) | Human-readable display name |
| `lastActivity` | `int` (Unix ms) | `updatedAt` in `sessions.json` | UTC timestamp of last message |
| `messageCount` | `int` | Count of `type:"message"` lines in JSONL | Only user + assistant messages |
| `preview` | `string` | Last assistant message text in JSONL | Truncated to 80 characters |
| `active` | `boolean` | Comparison with `GatewayServer._session_key` | `True` for the currently active session |

### 2.3 Display Name Derivation

Session keys follow the pattern `agent:<agentId>:<suffix>`. The label is derived as:

| Key Pattern | Example Key | Derived Label |
|---|---|---|
| `agent:claw:g2` | `agent:claw:g2` | `"Default"` |
| `agent:claw:g2:<timestamp>:<hex>` | `agent:claw:g2:1740000000:a1b2c3` | `"Session Feb 19"` (date from timestamp) |
| `agent:claw:main` | `agent:claw:main` | `"Main"` |
| Any other | `agent:claw:custom` | Tail segment capitalized: `"Custom"` |

Logic belongs in a new function `derive_session_label(session_key: str) -> str` in
`gateway/session_resolver.py`.

### 2.4 New Function: `list_all_sessions()`

Add to `gateway/session_resolver.py`:

```python
def list_all_sessions(
    agent_id: str = _DEFAULT_AGENT_ID,
    active_key: str | None = None,
) -> list[SessionEntry]:
    """Return metadata for all sessions in sessions.json.

    Each entry includes key, label, lastActivity, messageCount,
    preview, and whether it matches the active key.
    Sorted by lastActivity descending (most recent first).
    """
```

This function:
1. Reads `sessions.json` to get all keys and their `sessionId` / `updatedAt`.
2. For each session, opens the corresponding JSONL to count messages and
   extract the last assistant message as `preview`.
3. Builds `SessionEntry` dicts, marks the `active` one.
4. Sorts descending by `lastActivity`.

**Performance note:** Reading all JSONL files could be slow for many sessions.
Mitigate by:
- Capping to the most recent 20 sessions (sorted by `updatedAt` before reading
  JSONL).
- Reading only the last 5 lines of each JSONL for the preview (seek to end,
  read backward).

---

## 3. Frame Flow Diagrams

### 3.1 App Startup → Menu Display → Session Selection → Transcript

```
Phone                            Gateway
  │                                │
  ├─── WS connect ────────────────►│
  │                                │
  │◄── connected (v1.0, session) ──┤
  │◄── history (entries[])─────────┤   ← current session history
  │◄── status: idle ──────────────►│
  │                                │
  │  (user double-taps → open session menu)
  │                                │
  ├─── session_list_request ──────►│
  │                                │   Gateway reads sessions.json + JSONLs
  │◄── session_list (sessions[]) ──┤
  │                                │
  │  (G2 app renders session menu overlay)
  │  (user taps a session entry)
  │                                │
  ├─── switch_session ────────────►│   { sessionKey: "agent:claw:g2:17400..." }
  │                                │
  │    Gateway:                    │
  │      1. Update _session_key    │
  │      2. Close old handler      │
  │      3. Read new history       │
  │      4. Rebuild OpenClaw handler with new key
  │                                │
  │◄── session_switched ──────────┤   { sessionKey, sessionId, label, entries[] }
  │                                │
  │  (G2 app:                      │
  │    1. Close menu overlay       │
  │    2. conversation.replayHistory(entries)
  │    3. Update display)          │
  │                                │
```

### 3.2 Double-tap → Back to Menu → Select Different Session

```
Phone                            Gateway
  │                                │
  │  (currently viewing session transcript)
  │  (user double-taps → open session menu)
  │                                │
  ├─── session_list_request ──────►│
  │                                │
  │◄── session_list (sessions[]) ──┤   ← active flag marks current session
  │                                │
  │  (user taps a different session)
  │                                │
  ├─── switch_session ────────────►│
  │                                │
  │◄── session_switched ──────────┤
  │                                │
  │  (G2 app replaces transcript)  │
  │                                │
```

### 3.3 Creating a New Session from Menu

```
Phone                            Gateway
  │                                │
  │  (session menu is displayed)   │
  │  (user scrolls to "＋ New Session" item and taps)
  │                                │
  ├─── create_session ────────────►│   { label?: "..." }   (optional)
  │                                │
  │    Gateway:                    │
  │      1. new_key = _generate_session_key()
  │      2. _session_key = new_key │
  │      3. Close old handler      │
  │                                │
  │◄── session_switched ──────────┤   { sessionKey: new_key, entries: [] }
  │                                │
  │  (G2 app:                      │
  │    1. Close menu overlay       │
  │    2. conversation.clear()     │
  │    3. Show idle with empty transcript)
  │                                │
```

### 3.4 Reconnect with Session Menu State

```
Phone                            Gateway
  │                                │
  │  (WS disconnects)             │
  │  ...                           │
  ├─── WS reconnect ─────────────►│
  │                                │
  │◄── connected ─────────────────┤   ← includes current sessionKey
  │◄── history ───────────────────┤   ← current session's history
  │◄── status: idle ──────────────┤
  │                                │
  │  (App restores to transcript view, NOT session menu)
  │  (Session menu is transient UI — reconnect always lands on
  │   the active session's transcript. User double-taps to reopen menu.)
  │                                │
```

### 3.5 Error: Invalid Session Key on Switch

```
Phone                            Gateway
  │                                │
  ├─── switch_session ────────────►│   { sessionKey: "nonexistent:key" }
  │                                │
  │◄── error ─────────────────────┤   { code: "INVALID_STATE",
  │                                │     detail: "Session key not found" }
  │                                │
  │  (G2 app stays on session menu, shows brief error toast)
  │                                │
```

---

## 4. Validation Rules

### 4.1 Python Inbound Validation (`parse_text_frame`)

| Frame | Field | Rule |
|---|---|---|
| `session_list_request` | *(none)* | No required fields |
| `switch_session` | `sessionKey` | Must be a non-empty string, max 200 chars. Must match the pattern `^agent:[a-z0-9_]+:` (loose prefix check). |
| `create_session` | `label` (optional) | If present: string, max 100 chars, stripped of control characters. |

Add to the `parse_text_frame` function after existing `text` length check:

```python
if frame_type == "switch_session":
    key = data.get("sessionKey", "")
    if not isinstance(key, str) or not key.strip():
        raise ProtocolError("switch_session requires a non-empty sessionKey")
    if len(key) > 200:
        raise ProtocolError("sessionKey too long (max 200)")

if frame_type == "create_session":
    label = data.get("label")
    if label is not None:
        if not isinstance(label, str) or len(label) > 100:
            raise ProtocolError("create_session label must be a string ≤100 chars")
```

### 4.2 Python Outbound Validation (`validate_outbound`)

Existing `_check_fields` + `_OUTBOUND_FIELDS` handles required field presence
and type checking. The `sessions` and `entries` fields are validated as `list`
type.

### 4.3 TypeScript Inbound Validation (in `parseFrame`)

**`session_list` validation block:**

```typescript
if (clean.type === 'session_list') {
  if (!Array.isArray(frame.sessions)) {
    throw new Error('session_list.sessions must be an array');
  }
  clean.sessions = (frame.sessions as unknown[]).filter(
    (s): s is Record<string, unknown> => {
      if (typeof s !== 'object' || s === null) return false;
      const e = s as Record<string, unknown>;
      return (
        typeof e.sessionKey === 'string' &&
        typeof e.label === 'string' &&
        typeof e.lastActivity === 'number' &&
        typeof e.messageCount === 'number' &&
        typeof e.preview === 'string' &&
        typeof e.active === 'boolean'
      );
    }
  ).map(s => {
    const e = s as Record<string, unknown>;
    return {
      sessionKey: e.sessionKey as string,
      label: e.label as string,
      lastActivity: e.lastActivity as number,
      messageCount: e.messageCount as number,
      preview: (e.preview as string).slice(0, 80),
      active: e.active as boolean,
    };
  });
}
```

**`session_switched` validation block:**

Reuse the existing `history` entries validation logic for the `entries` field.
Validate `sessionKey`, `sessionId`, `label` are strings.

### 4.4 Edge Cases

| Scenario | Behavior |
|---|---|
| **Empty session list** | `session_list` frame with `sessions: []`. G2 app shows "No sessions — tap to create one". |
| **Active session not in list** | Should not happen — `list_all_sessions()` includes the active key. If it does, app shows list without any `active: true` entry. |
| **Switch to already-active session** | Gateway detects `sessionKey === _session_key`, responds with `session_switched` without reinitializing handler (no-op switch). |
| **Switch while recording/streaming** | Gateway responds with `error { code: "INVALID_STATE", detail: "Cannot switch session while processing" }`. Session menu should only be openable from idle state; this is a defense-in-depth guard. |
| **Create session while recording** | Same as above — `INVALID_STATE` error. |
| **Concurrent session_list_request** | Idempotent read operation — multiple requests in flight are safe, each gets a response. |
| **sessions.json missing or unreadable** | `list_all_sessions()` returns `[]`. Client gets an empty session list. |
| **JSONL file missing for a session** | That session entry gets `messageCount: 0`, `preview: ""`. Still included in the list. |

---

## 5. Backward Compatibility

### 5.1 Strategy: Additive-Only Protocol Extension

All changes are **additive** — no existing frames are modified or removed.

| Concern | Mitigation |
|---|---|
| **Old client, new gateway** | Old clients never send `session_list_request`, `switch_session`, or `create_session` frames — they simply don't have the menu feature. The gateway never sends `session_list` or `session_switched` unprompted. Existing `connected` → `history` → `status:idle` flow is unchanged. |
| **New client, old gateway** | If the client sends `session_list_request` to an old gateway, the gateway will respond with an `error { code: "INVALID_FRAME", detail: "Unknown frame type: session_list_request" }`. The G2 app should handle this gracefully: show a "Session menu not supported — update gateway" message and close the overlay. |
| **Protocol version negotiation** | The `connected` frame already includes a `version` field (currently `"1.0"`). Bump to `"1.1"` when session menu support is present. The G2 app can check `version >= "1.1"` before enabling the double-tap → session menu gesture. |

### 5.2 Version Gating in G2 App

```typescript
// In main.ts, on connected frame:
const supportsSessionMenu = frame.version >= '1.1';
input.setSessionMenuEnabled(supportsSessionMenu);
```

If disabled, double-tap retains its current behavior (reject transcription in
confirming state, or no-op in other states).

### 5.3 Gateway Version Bump

In `GatewaySession.handle()`, change:

```python
connected_frame: dict[str, Any] = {"type": "connected", "version": "1.1"}
```

This is safe — old clients ignore unknown fields and don't parse the version
string programmatically.

---

## 6. Gateway Handler Changes (Sketch)

### 6.1 `GatewaySession._handle_text_frame` additions

Add three new case branches in the text frame dispatch:

```python
case "session_list_request":
    await self._handle_session_list_request()

case "switch_session":
    await self._handle_switch_session(data)

case "create_session":
    await self._handle_create_session(data)
```

### 6.2 `GatewaySession._handle_session_list_request`

```
1. Call list_all_sessions(agent_id, active_key=self._session_key)
2. Build SessionListFrame
3. send_frame(session_list_frame)
```

Allowed in **any** state (read-only operation).

### 6.3 `GatewaySession._handle_switch_session`

```
1. Guard: state must be IDLE (else → INVALID_STATE error)
2. Validate sessionKey exists in sessions.json
3. If sessionKey == current key → send session_switched with current history (no-op)
4. Else:
   a. server.switch_session(new_key)  → updates _session_key, closes handler
   b. Read history for new key
   c. Build SessionSwitchedFrame with metadata + entries
   d. send_frame(session_switched_frame)
```

### 6.4 `GatewaySession._handle_create_session`

```
1. Guard: state must be IDLE
2. new_key = _generate_session_key()
3. server.switch_to_key(new_key)  → updates _session_key, closes handler
4. Build SessionSwitchedFrame with new key, empty entries
5. send_frame(session_switched_frame)
```

### 6.5 `GatewayServer.switch_to_key`

New method on `GatewayServer` (similar to existing `reset_session` but takes an
explicit target key rather than generating one):

```python
async def switch_to_key(self, new_key: str) -> None:
    """Switch the active session to new_key."""
    old_key = self._session_key
    self._session_key = new_key
    logger.info("Session switch: %s → %s", old_key, new_key)
    await self._discard_inflight()
    await self._handler.close()
    # Re-initialize handler with new session key
    # (OpenClawResponseHandler gets new key via the lambda)
```

---

## 7. G2 App Changes (Sketch)

### 7.1 State Machine

Add a new `AppStatus` value: `'menu'`.

```typescript
export type AppStatus = GatewayStatus | 'error' | 'disconnected' | 'confirming' | 'menu';
```

Transitions:
- `idle → menu` (double-tap)
- `menu → idle` (session selected or menu dismissed)
- `menu → disconnected` (connection lost)

### 7.2 Input Handler

- **Double-tap in `idle` state**: Send `session_list_request`, transition to `menu`.
- **Tap in `menu` state**: Forward to session list UI (select highlighted item).
- **Double-tap in `menu` state**: Close menu, transition back to `idle`.
- **Scroll in `menu` state**: Navigate session list up/down.

### 7.3 Display Manager

New methods:
- `showSessionMenu(sessions: SessionEntry[])` — Render a list overlay with session names, timestamps, and a "＋ New" footer item.
- `updateSessionMenu(sessions: SessionEntry[])` — Refresh the list (e.g. after error).

The session menu uses a `ListContainerProperty` (the G2 SDK's built-in
scrollable list container) overlaid on the transcript area.

### 7.4 Frame Routing (`main.ts`)

Add cases in `routeFrame`:

```typescript
case 'session_list':
  if (sm.current === 'menu') {
    display.showSessionMenu(frame.sessions);
  }
  break;

case 'session_switched':
  conversation.replayHistory(frame.entries);
  sm.transition('idle');
  display.showIdle();
  // Update stored session ID
  try { localStorage.setItem(SESSION_ID_KEY, frame.sessionId); } catch {}
  break;
```

---

## 8. Integration Test Plan

### 8.1 Unit Tests — Python (`tests/gateway/`)

| Test | File | Description |
|---|---|---|
| `test_parse_session_list_request` | `test_protocol.py` | Parse `{"type":"session_list_request"}` — succeeds with no required fields |
| `test_parse_switch_session_valid` | `test_protocol.py` | Parse `{"type":"switch_session","sessionKey":"agent:claw:g2"}` — succeeds |
| `test_parse_switch_session_missing_key` | `test_protocol.py` | Missing `sessionKey` → `ProtocolError` |
| `test_parse_switch_session_empty_key` | `test_protocol.py` | Empty string `sessionKey` → `ProtocolError` |
| `test_parse_create_session_no_label` | `test_protocol.py` | Parse `{"type":"create_session"}` — succeeds |
| `test_parse_create_session_with_label` | `test_protocol.py` | Parse with `label: "Work"` — succeeds |
| `test_parse_create_session_label_too_long` | `test_protocol.py` | Label > 100 chars → `ProtocolError` |
| `test_validate_session_list_frame` | `test_protocol.py` | Outbound validation of `session_list` with entries |
| `test_validate_session_switched_frame` | `test_protocol.py` | Outbound validation of `session_switched` |
| `test_list_all_sessions_empty` | `test_session_resolver.py` | No `sessions.json` → returns `[]` |
| `test_list_all_sessions_multiple` | `test_session_resolver.py` | 3 sessions → returns sorted by `lastActivity` desc |
| `test_list_all_sessions_active_flag` | `test_session_resolver.py` | Active key is flagged correctly |
| `test_list_all_sessions_missing_jsonl` | `test_session_resolver.py` | Session exists in JSON but JSONL missing → `messageCount: 0` |
| `test_derive_session_label` | `test_session_resolver.py` | Various key patterns → expected label strings |
| `test_session_list_request_handler` | `test_server.py` | Send `session_list_request` → receive `session_list` frame |
| `test_switch_session_idle` | `test_server.py` | In idle state, send `switch_session` → receive `session_switched` |
| `test_switch_session_while_streaming` | `test_server.py` | In streaming state → receive `error` with `INVALID_STATE` |
| `test_switch_session_invalid_key` | `test_server.py` | Non-existent key → receive `error` |
| `test_switch_to_same_session` | `test_server.py` | Switch to already-active key → `session_switched` (no-op) |
| `test_create_session` | `test_server.py` | Send `create_session` → receive `session_switched` with new key, empty entries |

### 8.2 Unit Tests — TypeScript (`g2_app/src/__tests__/`)

| Test | File | Description |
|---|---|---|
| `parseFrame: session_list` | `protocol.test.ts` | Valid `session_list` frame → parsed correctly, malformed entries filtered |
| `parseFrame: session_list empty` | `protocol.test.ts` | `sessions: []` → valid frame |
| `parseFrame: session_switched` | `protocol.test.ts` | Valid frame → parsed with `entries` validated |
| `parseFrame: session_switched missing field` | `protocol.test.ts` | Missing `sessionKey` → throws |
| `OutboundFrame types` | `protocol.test.ts` | Verify new outbound frames type-check correctly |

### 8.3 Integration Tests (`tests/integration/`)

| Scenario | Description |
|---|---|
| **Session list round-trip** | Start gateway with fixture `sessions.json` containing 3 sessions. Connect client, send `session_list_request`, assert response contains all 3 entries sorted by `lastActivity`, with correct `active` flag. |
| **Switch session round-trip** | Start gateway, send `switch_session` with a valid key. Assert `session_switched` response has correct `sessionKey`, `sessionId`, and `entries` matching the JSONL. Assert subsequent messages use the new session key (verify via OpenClaw mock). |
| **Create session round-trip** | Start gateway, send `create_session`. Assert `session_switched` response has a new unique key and empty `entries`. Assert the gateway's internal `_session_key` has changed. |
| **Switch during streaming → error** | Start a long-running mock response, send `switch_session` mid-stream. Assert `error` with `INVALID_STATE`. Assert original session continues unaffected. |
| **Backward compat: old gateway** | Connect to a gateway running v1.0 (without session menu). Send `session_list_request`. Assert `error` with `INVALID_FRAME`. Verify app doesn't crash. |
| **Reconnect after switch** | Switch session, disconnect, reconnect. Assert `connected` frame has the switched session's `sessionKey` and `sessionId`. Assert `history` frame matches the switched session. |
| **Create then list** | Create a new session, then send `session_list_request`. Assert the new session appears in the list with `active: true` and `messageCount: 0`. |

### 8.4 Manual Test Script (Simulator)

1. Boot gateway + G2 app + simulator.
2. Send a few messages → verify transcript.
3. Double-tap → session menu appears with current session highlighted.
4. Tap "＋ New Session" → menu closes, transcript is empty, status is idle.
5. Send a message in the new session → verify transcript.
6. Double-tap → session menu now shows 2 sessions.
7. Tap the previous session → transcript shows the old conversation.
8. Double-tap → back to menu. Double-tap again → menu closes (stays on same session).
9. Disconnect gateway → reconnect → lands on last active session (menu is not shown).

---

## 9. Files to Modify

| File | Changes |
|---|---|
| `gateway/protocol.py` | Add 3 inbound + 2 outbound TypedDicts; register in field maps; update `InboundFrame` union |
| `gateway/session_resolver.py` | Add `list_all_sessions()`, `derive_session_label()` |
| `gateway/server.py` | Add `_handle_session_list_request`, `_handle_switch_session`, `_handle_create_session` to `GatewaySession`; add `switch_to_key` to `GatewayServer`; bump version to `"1.1"` |
| `g2_app/src/protocol.ts` | Add 3 outbound + 2 inbound interfaces; update unions; add `parseFrame` validation blocks |
| `g2_app/src/state.ts` | Add `'menu'` to `AppStatus`, add transitions |
| `g2_app/src/input.ts` | Double-tap in idle → open menu; tap/scroll in menu → navigate; double-tap in menu → close |
| `g2_app/src/display.ts` | Add `showSessionMenu()`, `updateSessionMenu()` |
| `g2_app/src/main.ts` | Add `session_list` and `session_switched` cases in `routeFrame`; version-gate menu feature |
| `g2_app/src/conversation.ts` | No changes (existing `replayHistory` and `clear` suffice) |
| `docs/design/protocol.md` | Document new frames in §1.1/§1.2 tables; add §9 "Session Management Frames" |
| `tests/gateway/test_protocol.py` | New test cases per §8.1 |
| `tests/gateway/test_session_resolver.py` | New test cases per §8.1 |
| `tests/gateway/test_server.py` | New test cases per §8.1 |
| `g2_app/src/__tests__/protocol.test.ts` | New test cases per §8.2 |

---

## 10. Open Questions / Future Work

1. **Session naming UI**: Should the G2 app allow renaming sessions, or is auto-derived labeling sufficient? (Recommendation: start with auto-derived labels only — the tiny G2 display makes text input impractical.)

2. **Session deletion**: Not included in this plan. Could be added later as a `delete_session` frame with a confirmation flow.

3. **Session list pagination**: The plan caps at 20 sessions. If users accumulate hundreds of sessions over months, pagination or a "recent N" approach may be needed.

4. **Concurrent client support**: The current gateway is single-connection. If multi-client support is ever added, session switching semantics will need locking.

5. **Session labels from OpenClaw**: OpenClaw may support user-defined session names in the future. When available, prefer those over derived labels.
