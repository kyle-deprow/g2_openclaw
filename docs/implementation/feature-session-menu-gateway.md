# Feature: Session Menu — Gateway Changes

> **Scope:** Python gateway only. G2 app (TypeScript) changes are out of scope.
> **Status:** Plan — no code changes yet.
> **Date:** 2026-03-07

---

## 1. Overview

The G2 app needs a session menu overlay where the user can browse all OpenClaw
sessions, switch between them, view transcripts, and create new sessions. The
gateway currently operates with a single hardcoded session key
(`agent:claw:g2`) set at startup and rotated only on daily reset or explicit
`reset_session`. This plan adds the protocol frames, resolver logic, and server
orchestration needed to support multi-session browsing and switching.

### Current Architecture (Relevant Pieces)

| Component | Role | Key constraint |
|---|---|---|
| `GatewayServer._session_key` | Active session key, used for all OpenClaw requests | Single string, server-wide |
| `GatewaySession._session_key` | Copy of server key, used for history + connected frame | Set once at session creation |
| `OpenClawResponseHandler._get_session_key` | Lambda returning server's current key | Evaluated per `send_message` call |
| `session_resolver.resolve_session()` | Reads `sessions.json`, returns metadata for **one** key | No list-all capability |
| `session_history.read_history()` | Reads JSONL transcript for **one** key | Works with any key |
| `protocol.py` | Frame schemas — inbound and outbound field registries | Must register new frames |

### Session Storage on Disk

```
~/.openclaw/agents/claw/sessions/
  sessions.json          ← { "session_key": { "sessionId": "...", "updatedAt": ... }, ... }
  <sessionId>.jsonl      ← Append-only transcript
```

`sessions.json` already stores **all** sessions as a flat key→metadata dict.
The resolver just needs a new function to return everything instead of looking
up one key.

---

## 2. New Protocol Frames

### 2.1 Inbound: `session_list_request` (Phone → Gateway)

Request the full list of sessions. No parameters needed.

```json
{ "type": "session_list_request" }
```

**Schema:**

```python
class SessionListRequestFrame(TypedDict):
    type: Literal["session_list_request"]
```

**Registration in `_INBOUND_FIELDS`:**

```python
"session_list_request": [],
```

### 2.2 Outbound: `session_list` (Gateway → Phone)

Returns an array of session summaries, sorted by `updatedAt` descending (most
recent first). Includes the currently active session key.

```json
{
  "type": "session_list",
  "sessions": [
    {
      "sessionKey": "agent:claw:g2:1741300000:a1b2c3",
      "sessionId": "ses_abc123",
      "updatedAt": "2026-03-07T10:00:00+00:00",
      "preview": "What is the capital of France?",
      "messageCount": 24
    },
    {
      "sessionKey": "agent:claw:g2",
      "sessionId": "ses_def456",
      "updatedAt": "2026-03-06T15:30:00+00:00",
      "preview": "Summarize the project status",
      "messageCount": 8
    }
  ],
  "activeSessionKey": "agent:claw:g2:1741300000:a1b2c3"
}
```

**Schema:**

```python
class SessionSummaryDict(TypedDict):
    sessionKey: str
    sessionId: str
    updatedAt: str          # ISO-8601 UTC
    preview: str            # First user message or last user message (truncated)
    messageCount: int       # Total user+assistant messages


class SessionListFrame(TypedDict):
    type: Literal["session_list"]
    sessions: list[SessionSummaryDict]
    activeSessionKey: str
```

**Registration in `_OUTBOUND_FIELDS`:**

```python
"session_list": ["sessions", "activeSessionKey"],
```

**Registration in `_FIELD_TYPES`:**

```python
"sessions": list,
"activeSessionKey": str,
"messageCount": int,
"preview": str,
```

### 2.3 Inbound: `session_switch` (Phone → Gateway)

Switch the active session to an existing session key.

```json
{ "type": "session_switch", "sessionKey": "agent:claw:g2:1741300000:a1b2c3" }
```

**Schema:**

```python
class SessionSwitchFrame(TypedDict):
    type: Literal["session_switch"]
    sessionKey: str
```

**Registration in `_INBOUND_FIELDS`:**

```python
"session_switch": ["sessionKey"],
```

### 2.4 Inbound: `session_create` (Phone → Gateway)

Create a new session and switch to it.

```json
{ "type": "session_create" }
```

**Schema:**

```python
class SessionCreateFrame(TypedDict):
    type: Literal["session_create"]
```

**Registration in `_INBOUND_FIELDS`:**

```python
"session_create": [],
```

### 2.5 Outbound: `session_switched` (Gateway → Phone)

Confirms a session switch or creation completed. Followed by a `history` frame
with the new session's transcript and an `idle` status.

```json
{
  "type": "session_switched",
  "sessionKey": "agent:claw:g2:1741300000:a1b2c3",
  "sessionId": "ses_abc123",
  "sessionStartedAt": "2026-03-07T10:00:00+00:00"
}
```

**Schema:**

```python
class SessionSwitchedFrame(TypedDict):
    type: Literal["session_switched"]
    sessionKey: str
    sessionId: NotRequired[str]
    sessionStartedAt: NotRequired[str]
```

**Registration in `_OUTBOUND_FIELDS`:**

```python
"session_switched": ["sessionKey"],
```

---

## 3. Session Resolver Changes (`session_resolver.py`)

### 3.1 New Function: `list_sessions`

```python
def list_sessions(
    agent_id: str = _DEFAULT_AGENT_ID,
) -> list[SessionMeta]:
    """Return metadata for ALL sessions in sessions.json.

    Results are sorted by updatedAt descending (most recent first).
    Returns an empty list if the file is missing or unreadable.
    """
```

**Implementation notes:**

- Read `sessions.json` once (same path logic as `resolve_session`).
- Iterate all key→value pairs, skip entries without a valid `sessionId`.
- Parse `updatedAt` the same way `resolve_session` does (handle ms timestamps
  and ISO strings).
- Sort by `updatedAt` descending, with `None` values last.
- Return `list[SessionMeta]`.

### 3.2 `SessionMeta` Extension

Add an optional `label` or keep it as-is. The current `SessionMeta` dataclass
(`session_id`, `session_key`, `updated_at`) is sufficient for the list. The
`preview` and `messageCount` fields will be populated by session_history, not
the resolver.

**No changes to `SessionMeta` needed.**

---

## 4. Session History Changes (`session_history.py`)

### 4.1 New Function: `session_summary`

```python
@dataclass(frozen=True)
class SessionSummary:
    """Lightweight summary of a session for the session list."""
    session_key: str
    session_id: str
    updated_at: str | None
    preview: str            # Truncated first or last user message
    message_count: int      # Total user+assistant messages


def session_summary(
    session_key: str,
    agent_id: str = _DEFAULT_AGENT_ID,
    base_path: Path | None = None,
    preview_max_len: int = 80,
) -> SessionSummary | None:
    """Build a lightweight summary for one session.

    Reads the JSONL transcript to count messages and extract a preview.
    Returns None if the transcript file is missing.
    """
```

**Implementation notes:**

- Resolve the JSONL path using existing `resolve_session_file`.
- Stream through the JSONL counting `user` and `assistant` messages.
- Capture the **first user message** as the preview (truncated to
  `preview_max_len` chars). This gives a stable "topic" label.
- Lightweight — does not load full message content into memory.

### 4.2 New Function: `list_session_summaries`

```python
def list_session_summaries(
    agent_id: str = _DEFAULT_AGENT_ID,
    base_path: Path | None = None,
    preview_max_len: int = 80,
) -> list[SessionSummary]:
    """Return summaries for all sessions, sorted by updatedAt descending."""
```

**Implementation notes:**

- Call `list_sessions()` from session_resolver to get all `SessionMeta`.
- For each, call `session_summary()` to populate preview and count.
- Filter out sessions with no transcript file (stale entries).
- Return sorted list.

---

## 5. Server Changes (`server.py`)

### 5.1 New Dispatch Entries in `GatewaySession._dispatch`

Add three new `elif` branches:

```python
elif frame_type == "session_list_request":
    await self._handle_session_list_request()

elif frame_type == "session_switch":
    if self._state != SessionState.IDLE:
        await self.send_frame({
            "type": "error",
            "detail": "Cannot switch session while busy",
            "code": ErrorCode.INVALID_STATE,
        })
        return
    await self._handle_session_switch(frame)

elif frame_type == "session_create":
    if self._state != SessionState.IDLE:
        await self.send_frame({
            "type": "error",
            "detail": "Cannot create session while busy",
            "code": ErrorCode.INVALID_STATE,
        })
        return
    await self._handle_session_create()
```

### 5.2 `GatewaySession._handle_session_list_request`

```python
async def _handle_session_list_request(self) -> None:
    """Return the full session list to the client."""
    try:
        from gateway.session_history import list_session_summaries

        summaries = list_session_summaries(agent_id=self._agent_id)
        await self.send_frame({
            "type": "session_list",
            "sessions": [
                {
                    "sessionKey": s.session_key,
                    "sessionId": s.session_id,
                    "updatedAt": s.updated_at or "",
                    "preview": s.preview,
                    "messageCount": s.message_count,
                }
                for s in summaries
            ],
            "activeSessionKey": self._session_key,
        })
    except Exception:
        logger.warning("Failed to list sessions", exc_info=True)
        await self.send_frame({
            "type": "error",
            "detail": "Failed to list sessions",
            "code": ErrorCode.INTERNAL_ERROR,
        })
```

**Design note:** `session_list_request` is allowed in **any** state (even while
streaming) because it's read-only. The user might open the menu while a
response is in progress.

### 5.3 `GatewaySession._handle_session_switch`

Delegates to `GatewayServer.switch_session()`.

```python
async def _handle_session_switch(self, frame: dict[str, Any]) -> None:
    """Switch to an existing session."""
    target_key = frame["sessionKey"]
    if self._server is not None:
        await self._server.switch_session(target_key)
```

### 5.4 `GatewaySession._handle_session_create`

Delegates to `GatewayServer.create_session()`.

```python
async def _handle_session_create(self) -> None:
    """Create a new session and switch to it."""
    if self._server is not None:
        await self._server.create_session()
```

### 5.5 `GatewayServer.switch_session`

```python
async def switch_session(self, target_key: str) -> None:
    """Switch the active session to an existing session key.

    Validates the key exists in sessions.json before switching.
    Discards any inflight buffer, closes the OpenClaw client connection
    (forcing reconnect on next message), sends session_switched + history.
    """
    # Validate the session exists
    from gateway.session_resolver import resolve_session
    meta = resolve_session(session_key=target_key, agent_id=self.config.openclaw_agent_id)
    if meta is None:
        if self._current_session is not None:
            await self._current_session.send_frame({
                "type": "error",
                "detail": f"Session not found: {target_key}",
                "code": ErrorCode.INVALID_FRAME,
            })
        return

    old_key = self._session_key
    self._session_key = target_key
    logger.info("Session switch: %s → %s", old_key, target_key)

    # Discard inflight work from the old session
    await self._discard_inflight()
    await self._handler.close()

    # Update the GatewaySession's copy of the session key
    session = self._current_session
    if session is not None:
        session._session_key = target_key

        # Notify client
        switched_frame: dict[str, Any] = {
            "type": "session_switched",
            "sessionKey": meta.session_key,
        }
        if meta.session_id:
            switched_frame["sessionId"] = meta.session_id
        if meta.updated_at:
            switched_frame["sessionStartedAt"] = meta.updated_at
        await session.send_frame(switched_frame)

        # Send the new session's history
        await session._send_history()
```

### 5.6 `GatewayServer.create_session`

```python
async def create_session(self) -> None:
    """Generate a new session key and switch to it.

    The session won't appear in sessions.json until the first OpenClaw
    message is sent (OpenClaw creates the entry on first use).
    """
    new_key = _generate_session_key()
    old_key = self._session_key
    self._session_key = new_key
    logger.info("Session created: %s (was %s)", new_key, old_key)

    await self._discard_inflight()
    await self._handler.close()

    session = self._current_session
    if session is not None:
        session._session_key = new_key
        await session.send_frame({
            "type": "session_switched",
            "sessionKey": new_key,
        })
        # No history to send for a brand new session
        await session.send_frame({
            "type": "history",
            "entries": [],
        })
```

---

## 6. OpenClaw Client Changes (`openclaw_client.py`)

### No Structural Changes Required

The `OpenClawClient.send_message()` method already accepts a `session_key`
parameter, and the `OpenClawResponseHandler` already uses a dynamic
`_get_session_key` lambda that reads `GatewayServer._session_key`. When the
server updates `self._session_key`, the next `send_message` call will
automatically use the new key.

**One consideration:** After a session switch, `self._handler.close()` is called
to tear down the current OpenClaw WebSocket. This is already the pattern used by
`reset_session()`. The next message will trigger `ensure_connected()` and
establish a fresh connection — this is correct because OpenClaw may have
different agent state per session.

---

## 7. Config Changes (`config.py`)

### 7.1 New Option: `session_menu_enabled`

```python
session_menu_enabled: bool = True
```

**Purpose:** Feature flag to disable the session menu for deployments that want
to keep the single-session model. When `False`, the three new inbound frame
types (`session_list_request`, `session_switch`, `session_create`) return an
`INVALID_FRAME` error.

**Env var:** `G2_SESSION_MENU` (default `"true"`).

This is a low-priority addition. The feature can ship without the flag and it
can be added later if needed.

---

## 8. Protocol Spec Update (`docs/design/protocol.md`)

Add a new section **§9 — Session Menu Frames** documenting:

- `session_list_request` (inbound)
- `session_list` (outbound)
- `session_switch` (inbound)
- `session_create` (inbound)
- `session_switched` (outbound)
- Sequence diagram for each flow

Update:
- §1.1 inbound frame table — add three new rows
- §1.2 outbound frame table — add two new rows
- §4 status states — note that session switch resets to idle

---

## 9. Test Plan

### 9.1 Unit Tests: `tests/gateway/test_protocol.py`

| Test | What it validates |
|---|---|
| `test_session_list_request_round_trip` | Parse + serialize `session_list_request` |
| `test_session_switch_round_trip` | Parse + serialize `session_switch` with `sessionKey` |
| `test_session_create_round_trip` | Parse + serialize `session_create` |
| `test_session_list_outbound_valid` | `validate_outbound` accepts `session_list` frame |
| `test_session_switched_outbound_valid` | `validate_outbound` accepts `session_switched` frame |
| `test_session_switch_missing_key_raises` | `session_switch` without `sessionKey` → `ProtocolError` |

### 9.2 Unit Tests: `tests/gateway/test_session_resolver.py`

| Test | What it validates |
|---|---|
| `test_list_sessions_returns_all_entries` | All valid entries returned from sessions.json |
| `test_list_sessions_skips_invalid_entries` | Entries without `sessionId` are excluded |
| `test_list_sessions_sorted_by_updated_at` | Most recent first |
| `test_list_sessions_empty_file` | Returns `[]` for empty `{}` |
| `test_list_sessions_file_missing` | Returns `[]` when sessions.json doesn't exist |
| `test_list_sessions_handles_ms_timestamps` | JS `Date.now()` style timestamps parsed correctly |

### 9.3 Unit Tests: `tests/gateway/test_session_history.py`

| Test | What it validates |
|---|---|
| `test_session_summary_returns_preview_and_count` | Preview is first user message, count is correct |
| `test_session_summary_truncates_long_preview` | Preview capped at `preview_max_len` |
| `test_session_summary_returns_none_for_missing` | No JSONL file → `None` |
| `test_list_session_summaries_combines_data` | Resolver + history data are merged |
| `test_list_session_summaries_filters_stale` | Sessions with no JSONL are excluded |

### 9.4 Integration Tests: `tests/gateway/test_server.py`

| Test | What it validates |
|---|---|
| `test_session_list_request_returns_list` | Send `session_list_request` → receive `session_list` with `activeSessionKey` |
| `test_session_list_request_allowed_while_busy` | Send during `streaming` state → still receives response (no `INVALID_STATE`) |
| `test_session_switch_valid_key` | Send `session_switch` → receive `session_switched` + `history` |
| `test_session_switch_unknown_key` | Send `session_switch` with bogus key → `error` frame |
| `test_session_switch_while_busy_rejected` | During streaming → `INVALID_STATE` |
| `test_session_create_generates_new_key` | Send `session_create` → receive `session_switched` with new key + empty `history` |
| `test_session_create_while_busy_rejected` | During streaming → `INVALID_STATE` |
| `test_session_switch_updates_openclaw_key` | After switch, text message uses the new session key for OpenClaw |
| `test_session_create_then_message` | Create + send text → OpenClaw receives the new key |

### 9.5 Test Infrastructure

The existing `conftest.py` patches `resolve_session` and `read_history` globally.
New tests for session list/switch will need to:

- Patch `list_sessions` and `list_session_summaries` with fixture data.
- Patch `resolve_session` to return `SessionMeta` for valid switch targets.
- Use `tmp_path` fixtures for session_resolver and session_history unit tests
  (same pattern as existing tests).

---

## 10. Sequence Diagrams

### 10.1 Session List Flow

```
Phone                          Gateway
  │                               │
  │──session_list_request────────►│
  │                               │── read sessions.json
  │                               │── read JSONL files (summaries)
  │◄──session_list────────────────│
  │                               │
```

### 10.2 Session Switch Flow

```
Phone                          Gateway                     OpenClaw
  │                               │                           │
  │──session_switch───────────────►│                           │
  │  { sessionKey: "..." }        │                           │
  │                               │── validate key exists     │
  │                               │── discard inflight        │
  │                               │── close OpenClaw WS ─────►│ (tear down)
  │                               │── update _session_key     │
  │◄──session_switched────────────│                           │
  │◄──history─────────────────────│ (new session transcript)  │
  │                               │                           │
  │──text─────────────────────────►│                           │
  │                               │── ensure_connected() ────►│ (new WS)
  │                               │── agent req (new key) ───►│
  │                               │                           │
```

### 10.3 Session Create Flow

```
Phone                          Gateway                     OpenClaw
  │                               │                           │
  │──session_create──────────────►│                           │
  │                               │── generate new key        │
  │                               │── discard inflight        │
  │                               │── close OpenClaw WS ─────►│
  │◄──session_switched────────────│                           │
  │  { sessionKey: "<new>" }      │                           │
  │◄──history─────────────────────│ (empty entries)           │
  │                               │                           │
```

---

## 11. Dependency Graph & Ordering

Implementation should proceed in this order, each step independently testable:

```
Step 1 ─── protocol.py           Register 5 new frame types
    │                             (no runtime behavior, just schema)
    │
Step 2 ─┬─ session_resolver.py   Add list_sessions()
    │   └─ session_history.py    Add session_summary() + list_session_summaries()
    │                             (pure functions, no server changes)
    │
Step 3 ─── server.py             Add dispatch + handler methods + GatewayServer
    │                             switch_session() / create_session()
    │
Step 4 ─── protocol.md           Update wire protocol documentation
    │
Step 5 ─── config.py             (Optional) Add session_menu_enabled flag
```

**Steps 1 and 2 are independent** of each other and can be done in parallel.
Step 3 depends on both. Steps 4 and 5 are documentation/polish and can happen
any time after Step 3.

---

## 12. Edge Cases & Design Decisions

### 12.1 Switching While Streaming

Session switch is **rejected** if the gateway is not idle. The user must wait
for the current response to finish (or the G2 app could cancel via future
cancel frame). `session_list_request` is always allowed (read-only).

### 12.2 New Session Doesn't Exist in `sessions.json` Yet

When a user creates a new session, the key is generated locally but doesn't
appear in `sessions.json` until the first message is sent to OpenClaw. The
gateway handles this gracefully — `resolve_session` returns `None` for the new
key, and `session_switched` omits `sessionId` and `sessionStartedAt`.

### 12.3 Stale Sessions

Sessions may exist in `sessions.json` but have no transcript file (e.g., key
was created but no messages were sent). `list_session_summaries` filters these
out. `session_switch` still succeeds for them (the server-side key is valid
even without a transcript).

### 12.4 Large Session Lists

No pagination in v1. `sessions.json` is expected to contain at most ~100
entries for a personal assistant. If this becomes a problem, add a `limit`
field to `session_list_request` later.

### 12.5 Session Key Syncing

After a switch, `GatewaySession._session_key` must be updated to match
`GatewayServer._session_key` so that `_send_history` and the `connected` frame
(on reconnect) use the correct key. This update happens in
`GatewayServer.switch_session()`.

### 12.6 Inflight Buffer on Switch

Any in-progress OpenClaw response is discarded on session switch (same behavior
as `reset_session`). The user explicitly chose to leave the session, so losing
the partial response is acceptable.

---

## 13. Files Changed (Summary)

| File | Change type | Description |
|---|---|---|
| `gateway/protocol.py` | **Modified** | Add 5 new frame TypedDicts + field registrations |
| `gateway/session_resolver.py` | **Modified** | Add `list_sessions()` function |
| `gateway/session_history.py` | **Modified** | Add `SessionSummary`, `session_summary()`, `list_session_summaries()` |
| `gateway/server.py` | **Modified** | Add dispatch + 3 handler methods + 2 GatewayServer methods |
| `gateway/config.py` | **Modified** (optional) | Add `session_menu_enabled` flag |
| `docs/design/protocol.md` | **Modified** | Document new frame types |
| `tests/gateway/test_protocol.py` | **Modified** | Add frame round-trip + validation tests |
| `tests/gateway/test_session_resolver.py` | **Modified** | Add `list_sessions` tests |
| `tests/gateway/test_session_history.py` | **Modified** | Add summary tests |
| `tests/gateway/test_server.py` | **Modified** | Add session menu integration tests |
