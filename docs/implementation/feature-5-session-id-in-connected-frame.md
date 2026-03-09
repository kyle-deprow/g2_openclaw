# Feature 5: Session ID in Connected Frame — Implementation Plan

## Summary

Extend the `connected` frame with optional session metadata (`sessionId`, `sessionKey`, `sessionStartedAt`) so the G2 app can detect when the OpenClaw session has been reset since last connection.

**Scope:** Protocol extension (backward-compatible), Gateway session resolver, G2 app localStorage comparison + conversation clear.

---

## 1. Protocol Changes

### 1.1 Gateway — `ConnectedFrame` TypedDict

**File:** `gateway/protocol.py`

```python
# Before
class ConnectedFrame(TypedDict):
    type: Literal["connected"]
    version: str

# After
class ConnectedFrame(TypedDict):
    type: Literal["connected"]
    version: str
    sessionId: NotRequired[str]
    sessionKey: NotRequired[str]
    sessionStartedAt: NotRequired[str]   # ISO 8601 UTC
```

All three new fields use `NotRequired` — backward compatible. Existing tests that assert `{"type": "connected", "version": "1.0"}` continue to pass unmodified because `validate_outbound` only checks *required* fields.

**No changes needed** to `_OUTBOUND_FIELDS`, `_FIELD_TYPES`, or `validate_outbound` — the new fields are optional and already string-typed (matching existing `_FIELD_TYPES` patterns). We *do* add them to `_FIELD_TYPES` so that `_check_fields` validates their types when present:

```python
# Add to _FIELD_TYPES dict:
"sessionId": str,
"sessionKey": str,
"sessionStartedAt": str,
```

### 1.2 G2 App — `ConnectedFrame` interface

**File:** `g2_app/src/protocol.ts`

```typescript
// Before
export interface ConnectedFrame {
  type: 'connected';
  version: string;
}

// After
export interface ConnectedFrame {
  type: 'connected';
  version: string;
  sessionId?: string;
  sessionKey?: string;
  sessionStartedAt?: string;   // ISO 8601 UTC
}
```

**`parseFrame()` changes:** The `REQUIRED_FIELDS` map stays the same (`connected: ['version']`). The optional fields are *not* required. However, the current `parseFrame` builds a clean object with only `knownFields` (the required list), which means optional fields get stripped. Fix this by adding an `OPTIONAL_FIELDS` map and including those fields in the clean object when present:

```typescript
const OPTIONAL_FIELDS: Record<string, string[]> = {
  connected: ['sessionId', 'sessionKey', 'sessionStartedAt'],
};

// In parseFrame(), after building clean from required fields:
const optionalFields = OPTIONAL_FIELDS[frame.type as string] ?? [];
for (const f of optionalFields) {
  if (f in frame) { clean[f] = frame[f]; }
}
```

Also add type checks for the optional fields:

```typescript
// Extend FIELD_TYPES
connected: { version: 'string', sessionId: 'string', sessionKey: 'string', sessionStartedAt: 'string' },
```

Since `_check_fields`-equivalent logic only validates fields that are *present*, and the optional fields are all strings, this is safe. The existing type-check loop iterates over keys in `FIELD_TYPES[frameType]` but should only check if the field is present — verify this is already the case (it checks `typeof frame[field]` which returns `'undefined'` for absent fields; we need a guard):

```typescript
// Adjust type validation to skip absent optional fields:
if (typeChecks) {
  for (const [field, expectedType] of Object.entries(typeChecks)) {
    if (field in frame && typeof frame[field] !== expectedType) {
      throw new Error(`Field "${field}" must be ${expectedType}, got ${typeof frame[field]}`);
    }
  }
}
```

---

## 2. Gateway Implementation — Session Resolver

### 2.1 New module: `gateway/session_resolver.py`

Resolves the current OpenClaw session metadata for a given session key by reading from the on-disk session store. The Gateway and OpenClaw run on the same machine, so we can read `~/.openclaw/agents/<agentId>/sessions/sessions.json` directly.

```python
"""Resolve OpenClaw session metadata from the local session store."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default agent ID used by the OpenClaw agent backing the G2 Gateway.
# This should match the agent configured in openclaw.json / the OpenClaw server.
_DEFAULT_AGENT_ID = "claw"

@dataclass(frozen=True)
class SessionMeta:
    """Resolved session metadata."""
    session_id: str
    session_key: str
    updated_at: str | None = None  # ISO 8601 from sessions.json "updatedAt"


def _sessions_json_path(agent_id: str = _DEFAULT_AGENT_ID) -> Path:
    """Return the path to the OpenClaw sessions.json file."""
    return Path.home() / ".openclaw" / "agents" / agent_id / "sessions" / "sessions.json"


def resolve_session(
    session_key: str = "agent:claw:g2",
    agent_id: str = _DEFAULT_AGENT_ID,
) -> SessionMeta | None:
    """Read sessions.json and return metadata for the given session key.

    Returns None if the file doesn't exist, is unreadable, or the key
    is not present. This is intentionally best-effort — the Gateway
    should not fail to connect just because session metadata is unavailable.
    """
    path = _sessions_json_path(agent_id)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError) as exc:
        logger.debug("Could not read sessions.json at %s: %s", path, exc)
        return None

    entry = data.get(session_key)
    if not isinstance(entry, dict):
        logger.debug("Session key %r not found in sessions.json", session_key)
        return None

    session_id = entry.get("sessionId")
    if not session_id or not isinstance(session_id, str):
        return None

    return SessionMeta(
        session_id=session_id,
        session_key=session_key,
        updated_at=entry.get("updatedAt"),
    )
```

**Design notes:**
- Pure function, no caching needed at this layer — called once per connection.
- Reads `sessions.json` synchronously (tiny file, local disk) which is fine for the connection handshake path.
- Gracefully returns `None` on any failure — the connected frame just omits the session fields.
- `_DEFAULT_AGENT_ID` is `"claw"` based on the session key pattern `agent:claw:g2`. If this needs to be configurable, add it to `GatewayConfig` later.

### 2.2 Wire it into `GatewaySession.handle()`

**File:** `gateway/server.py`

Currently at line 151:
```python
async def handle(self) -> None:
    await self.send_frame({"type": "connected", "version": "1.0"})
```

Change to:
```python
from gateway.session_resolver import resolve_session

async def handle(self) -> None:
    connected_frame: dict[str, Any] = {"type": "connected", "version": "1.0"}

    meta = resolve_session()
    if meta is not None:
        connected_frame["sessionId"] = meta.session_id
        connected_frame["sessionKey"] = meta.session_key
        if meta.updated_at:
            connected_frame["sessionStartedAt"] = meta.updated_at

    await self.send_frame(connected_frame)
    await self.send_frame({"type": "status", "status": "idle"})
```

**Import placement:** Add `from gateway.session_resolver import resolve_session` at the top of `server.py` with the other gateway imports.

### 2.3 Config extension (optional, defer to follow-up)

If the agent ID needs to vary, add `openclaw_agent_id: str = "claw"` to `GatewayConfig` and pass it through. For now, hardcode `"claw"` since the session key `agent:claw:g2` is also hardcoded.

---

## 3. G2 App Implementation — Session Change Detection

### 3.1 Session storage keys

```typescript
// localStorage keys
const SESSION_ID_KEY = 'g2_last_session_id';
```

### 3.2 Handling in `routeFrame()` — `main.ts`

**File:** `g2_app/src/main.ts`, inside the `case 'connected':` block:

```typescript
case 'connected': {
  console.log(`[Main] Gateway connected (server v${frame.version})`);

  // Session change detection
  if (frame.sessionId) {
    const lastSessionId = localStorage.getItem(SESSION_ID_KEY);

    if (lastSessionId && lastSessionId !== frame.sessionId) {
      console.log('[Main] Session changed: %s → %s', lastSessionId, frame.sessionId);
      conversation.clear();
      conversation.addSystem('New session started');
    }

    try {
      localStorage.setItem(SESSION_ID_KEY, frame.sessionId);
    } catch { /* localStorage full or unavailable — non-fatal */ }
  }

  sm.transition('idle');
  display.showIdle().catch(err => console.error('[Main] Display error:', err));
  break;
}
```

**Behavior summary:**
| Scenario | `lastSessionId` | `frame.sessionId` | Action |
|---|---|---|---|
| First connection ever | `null` | `"abc123"` | Store ID, no clear |
| Reconnect, same session | `"abc123"` | `"abc123"` | No-op |
| Reconnect, session reset | `"abc123"` | `"def456"` | Clear conversation, show system message, store new ID |
| Gateway has no session info | any | `undefined` | Skip entirely (backward compat) |

### 3.3 `ConversationHistory.clear()` already exists

The `clear()` method at [conversation.ts](g2_app/src/conversation.ts#L117) already resets `entries = []`. No changes needed to the conversation module.

---

## 4. Integration with Other Features

### 4.1 Feature 1 — History Replay

History replay will send past conversation turns after the `connected` frame. The `sessionId` tells the G2 app whether replay data belongs to the *current* session:
- If `sessionId` matches localStorage → replay is a continuation, append to existing conversation.
- If `sessionId` is new → conversation was already cleared (by this feature), replay populates a fresh history.

The G2 app can also use `sessionStartedAt` to display "Session started at ..." in a system message during replay.

### 4.2 Feature 4 — Reset Notification

When a session reset is detected (`lastSessionId !== frame.sessionId`), the G2 app can:
- Show a brief glasses notification ("Session reset — new context") via `DisplayManager.showNotification()` (to be built in Feature 4).
- The detection logic from Section 3.2 above provides the `if` branch where Feature 4 hooks its notification call.

### 4.3 Protocol extensibility

The `NotRequired` / optional pattern means future fields can be added to `ConnectedFrame` without breaking older clients. The G2 app's `parseFrame()` with `OPTIONAL_FIELDS` makes this straightforward.

---

## 5. Edge Cases

| Edge Case | Gateway Behavior | G2 App Behavior |
|---|---|---|
| **First connection ever** | `resolve_session()` returns metadata if OpenClaw has been used before; `None` if sessions.json doesn't exist yet | `lastSessionId` is `null` → stores the ID, no conversation clear |
| **OpenClaw not running** | `sessions.json` may still exist on disk from a previous run → session metadata is available. If the file doesn't exist, `resolve_session()` returns `None` → connected frame omits session fields | `frame.sessionId` is `undefined` → session detection skipped entirely |
| **Gateway restart** | No cached state needed — `resolve_session()` reads from disk on every connection. Stateless by design | No impact — the sessionId comparison works across Gateway restarts |
| **sessions.json unreadable** (permissions, corrupt) | `resolve_session()` catches exceptions, returns `None`, logs at `DEBUG` level | Session fields omitted, G2 app skips detection |
| **Session reset between reconnects** (daily reset, `/new` command) | `sessions.json` now has a new `sessionId` for the same key | `lastSessionId !== frame.sessionId` → conversation cleared |
| **Multiple G2 apps connecting** | Each phone has its own localStorage — independent session tracking | Each phone independently detects session changes |
| **Agent ID mismatch** | If agent ID isn't `"claw"`, `resolve_session()` reads the wrong path → returns `None` | Session fields omitted, no harm |

---

## 6. Testing Strategy

### 6.1 Unit Tests — `tests/gateway/test_session_resolver.py` (new file)

```
test_resolve_session_returns_meta_from_valid_file
    → Write a temp sessions.json, assert SessionMeta fields match
test_resolve_session_returns_none_when_file_missing
    → Point at nonexistent path, assert None
test_resolve_session_returns_none_when_key_absent
    → Valid JSON but missing the target session key
test_resolve_session_returns_none_on_corrupt_json
    → Write invalid JSON, assert None (no exception raised)
test_resolve_session_returns_none_when_session_id_missing
    → Entry exists but has no sessionId field
test_resolve_session_with_custom_agent_id
    → Verify different agent_id reads from correct path
```

### 6.2 Unit Tests — `tests/gateway/test_protocol.py` (extend)

```
test_outbound_connected_with_session_fields_validates
    → validate_outbound({"type": "connected", "version": "1.0",
        "sessionId": "abc", "sessionKey": "agent:claw:g2",
        "sessionStartedAt": "2026-03-07T..."})
test_outbound_connected_without_optional_fields_still_validates
    → validate_outbound({"type": "connected", "version": "1.0"})
    (existing test, confirm it still passes)
test_outbound_connected_rejects_wrong_type_session_id
    → validate_outbound({"type":"connected","version":"1.0","sessionId":123})
    → ProtocolError
```

### 6.3 Unit Tests — `tests/gateway/test_server.py` (extend)

```
test_connected_frame_includes_session_meta_when_available
    → mock resolve_session() to return SessionMeta
    → connect, assert connected frame contains sessionId, sessionKey, sessionStartedAt
test_connected_frame_omits_session_fields_when_unavailable
    → mock resolve_session() to return None
    → connect, assert connected == {"type": "connected", "version": "1.0"}
    (existing test effectively covers this)
```

### 6.4 G2 App Tests — `g2_app/src/__tests__/protocol.test.ts` (extend)

```
it('parses connected frame with optional session fields')
    → parseFrame with sessionId, sessionKey, sessionStartedAt
    → assert all fields present on returned object
it('parses connected frame without optional fields (backward compat)')
    → existing test, confirm it passes
it('rejects non-string sessionId')
    → parseFrame with sessionId: 123 → throws
```

### 6.5 G2 App Tests — `g2_app/src/__tests__/main.test.ts` (new or extend)

```
it('clears conversation when sessionId changes')
    → stub localStorage with old sessionId
    → call routeFrame with connected frame containing new sessionId
    → assert conversation.clear() called, localStorage updated
it('does not clear conversation on first connect')
    → localStorage returns null
    → call routeFrame → conversation.clear() NOT called
it('does not clear conversation when sessionId matches')
    → same sessionId in localStorage and frame → no clear
it('skips session detection when sessionId absent')
    → connected frame without sessionId → no localStorage interaction
```

### 6.6 Integration Test (optional)

In `tests/integration/`, a test that starts the Gateway with a fixture `sessions.json`, connects via WebSocket, and asserts the connected frame contains the expected session metadata.

---

## 7. Implementation Order

1. **`gateway/session_resolver.py`** — new module + tests (`test_session_resolver.py`)
2. **`gateway/protocol.py`** — extend `ConnectedFrame` TypedDict + `_FIELD_TYPES` + protocol tests
3. **`gateway/server.py`** — wire `resolve_session()` into `handle()` + server tests
4. **`g2_app/src/protocol.ts`** — extend interface + `parseFrame()` optional fields + tests
5. **`g2_app/src/main.ts`** — session change detection in `routeFrame()` + tests
6. **Integration test** (optional)

Steps 1–3 are Gateway-side (Python). Steps 4–5 are G2 App (TypeScript). They can proceed in parallel.

---

## 8. Files Modified / Created

| File | Action |
|---|---|
| `gateway/session_resolver.py` | **Create** |
| `gateway/protocol.py` | Extend `ConnectedFrame`, add to `_FIELD_TYPES` |
| `gateway/server.py` | Import `resolve_session`, enrich connected frame in `handle()` |
| `g2_app/src/protocol.ts` | Extend `ConnectedFrame`, add `OPTIONAL_FIELDS`, adjust type validation |
| `g2_app/src/main.ts` | Session change detection in `routeFrame()` `connected` case |
| `tests/gateway/test_session_resolver.py` | **Create** |
| `tests/gateway/test_protocol.py` | Add connected frame with optional fields tests |
| `tests/gateway/test_server.py` | Add session metadata tests with mocked resolver |
| `g2_app/src/__tests__/protocol.test.ts` | Add optional session field tests |
