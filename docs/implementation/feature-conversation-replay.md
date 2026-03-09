# Feature: Conversation Replay on Reconnect

## Implementation Plan

**Goal:** When the G2 app reconnects to the Gateway (after app restart or
network drop), the Gateway sends the last N conversation turns from the
OpenClaw session transcript so the glasses display shows recent conversation
context. This happens automatically after the `connected` frame.

---

## 1. New Protocol Frame: `history`

### 1.1 Frame Definition (Gateway → Phone)

A single `history` frame carries an ordered array of recent conversation turns.
One frame rather than per-entry replay avoids timing races, reduces round-trips
to one, and lets the G2 app render the entire history atomically before showing
the idle screen.

```jsonc
{
  "type": "history",
  "entries": [
    { "role": "user",      "text": "What is 2+2?",     "ts": 1772910239636 },
    { "role": "assistant", "text": "The answer is 4.",  "ts": 1772910240500 }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `"history"` | Frame discriminant |
| `entries` | `Array<HistoryEntry>` | Ordered oldest-first. May be empty (`[]`) on first-ever session. |

Each `HistoryEntry`:

| Field | Type | Description |
|---|---|---|
| `role` | `"user" \| "assistant"` | Only user/assistant turns — system/tool entries are filtered out |
| `text` | `string` | Plain-text content (markdown stripped by Gateway for assistant messages) |
| `ts` | `number` | Unix-ms timestamp from the JSONL `message.timestamp` field |

### 1.2 Design Rationale

- **Single frame, not multiple:** The G2 display does an atomic rebuild after
  receiving history. Streaming multiple per-entry frames would cause visual
  flicker and require a "history complete" sentinel.
- **`entries` may be empty:** First-connection or after daily session reset.
  The G2 app just shows `"Ready."` as today.
- **No `system` or `toolResult` roles:** These are internal to the agent context
  and meaningless to the glasses user.
- **Plain text only:** The Gateway strips markdown from assistant content before
  sending. The G2 app already calls `stripMarkdown()` — doing it server-side
  keeps the frame payload small and display-ready.

---

## 2. Gateway Changes

### 2.1 New Module: `gateway/session_history.py`

Reads OpenClaw's on-disk JSONL transcripts directly (same machine, no RPC
needed). This is simpler, faster, and avoids adding a new RPC method to
OpenClaw's wire protocol.

```python
# gateway/session_history.py
"""Read conversation history from OpenClaw session transcript files."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default agent ID used by the G2 Gateway
_DEFAULT_AGENT_ID = "claw"

# OpenClaw session store location
_OPENCLAW_BASE = Path.home() / ".openclaw" / "agents"

# Max entries to return (last N user+assistant turns)
DEFAULT_HISTORY_LIMIT = 10


@dataclass(frozen=True)
class HistoryEntry:
    """A single user or assistant message from the session transcript."""

    role: str       # "user" | "assistant"
    text: str       # plain-text content
    ts: int         # Unix milliseconds


def _extract_text(content: object) -> str:
    """Extract plain text from an OpenClaw message content field.

    Content can be:
    - A string (simple text)
    - A list of content blocks: [{"type": "text", "text": "..."}, ...]
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def _strip_timestamp_prefix(text: str) -> str:
    """Remove the OpenClaw-injected timestamp prefix from user messages.

    OpenClaw prepends timestamps like '[Sat 2026-03-07 13:03 CST] ' to user
    messages. Strip this for the glasses display since it's noise.
    """
    import re
    # Pattern: [Day YYYY-MM-DD HH:MM TZ]<space>
    return re.sub(r"^\[.*?\]\s*", "", text, count=1)


def resolve_session_file(
    session_key: str = "agent:claw:g2",
    agent_id: str = _DEFAULT_AGENT_ID,
    base_path: Path | None = None,
) -> Path | None:
    """Resolve the JSONL transcript path for a session key.

    Reads sessions.json to map session_key → sessionId, then returns
    the path to <sessionId>.jsonl.

    Returns None if sessions.json is missing, the key doesn't exist,
    or the JSONL file doesn't exist.
    """
    base = base_path or _OPENCLAW_BASE
    sessions_dir = base / agent_id / "sessions"
    store_file = sessions_dir / "sessions.json"

    if not store_file.exists():
        logger.debug("sessions.json not found at %s", store_file)
        return None

    try:
        store = json.loads(store_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read sessions.json: %s", exc)
        return None

    session_meta = store.get(session_key)
    if not isinstance(session_meta, dict):
        logger.debug("Session key %r not found in sessions.json", session_key)
        return None

    session_id = session_meta.get("sessionId")
    if not session_id:
        return None

    jsonl_path = sessions_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        logger.debug("JSONL file not found: %s", jsonl_path)
        return None

    return jsonl_path


def read_history(
    session_key: str = "agent:claw:g2",
    agent_id: str = _DEFAULT_AGENT_ID,
    limit: int = DEFAULT_HISTORY_LIMIT,
    base_path: Path | None = None,
) -> list[HistoryEntry]:
    """Read the last `limit` user/assistant turns from a session transcript.

    Returns an ordered list (oldest first) of up to `limit` entries.
    Returns an empty list on any error or if no transcript exists.
    """
    jsonl_path = resolve_session_file(
        session_key=session_key,
        agent_id=agent_id,
        base_path=base_path,
    )
    if jsonl_path is None:
        return []

    entries: list[HistoryEntry] = []
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "message":
                    continue

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue  # skip toolResult, system, etc.

                content = msg.get("content", "")
                text = _extract_text(content).strip()

                # Skip empty assistant messages (errors, tool-only turns)
                if role == "assistant" and not text:
                    continue

                # Skip errored assistant messages
                if role == "assistant" and msg.get("stopReason") == "error":
                    continue

                # Strip timestamp prefix from user messages
                if role == "user":
                    text = _strip_timestamp_prefix(text)

                ts = msg.get("timestamp", 0)
                if isinstance(ts, str):
                    # ISO timestamp fallback — parse to epoch ms
                    from datetime import datetime, timezone
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts = int(dt.timestamp() * 1000)
                    except ValueError:
                        ts = 0

                entries.append(HistoryEntry(role=role, text=text, ts=int(ts)))
    except OSError as exc:
        logger.warning("Failed to read JSONL transcript: %s", exc)
        return []

    # Return the last `limit` entries
    return entries[-limit:]
```

**Key decisions:**
- Reads JSONL directly from `~/.openclaw/agents/claw/sessions/` — no RPC round-trip
- Filters to `type: "message"` lines with `role: "user" | "assistant"` only
- Skips `stopReason: "error"` assistant entries (timeouts, API errors)
- Strips OpenClaw's `[Day YYYY-MM-DD HH:MM TZ]` prefix from user messages
- Extracts text from content blocks (the `[{type: "text", text: "..."}]` format)
- Returns at most `limit` entries (default 10 = last 5 exchanges)
- Fully synchronous file I/O — acceptable because the JSONL is local and small (typically <100 lines)

### 2.2 Config Changes: `gateway/config.py`

Add two optional config fields:

```python
@dataclass(frozen=True)
class GatewayConfig:
    # ... existing fields ...
    history_limit: int = 10                # max entries in history frame
    openclaw_agent_id: str = "claw"        # agent ID for session file lookup
```

New env vars (with defaults that match current behavior):
- `HISTORY_LIMIT` → `int`, default `10`
- `OPENCLAW_AGENT_ID` → `str`, default `"claw"`

### 2.3 Protocol Changes: `gateway/protocol.py`

Add the `history` frame type to the outbound definitions:

```python
class HistoryEntryDict(TypedDict):
    role: Literal["user", "assistant"]
    text: str
    ts: int


class HistoryFrame(TypedDict):
    type: Literal["history"]
    entries: list[HistoryEntryDict]
```

Register in `_OUTBOUND_FIELDS`:

```python
_OUTBOUND_FIELDS: dict[str, list[str]] = {
    # ... existing entries ...
    "history": ["entries"],
}
```

Add to `_FIELD_TYPES`:

```python
_FIELD_TYPES: dict[str, type] = {
    # ... existing entries ...
    "entries": list,
}
```

### 2.4 Server Changes: `gateway/server.py`

In `GatewaySession.handle()`, send the history frame between `connected` and
the first `status: idle`:

```python
async def handle(self) -> None:
    await self.send_frame({"type": "connected", "version": "1.0"})

    # Send conversation history replay (non-blocking, best-effort)
    await self._send_history()

    await self.send_frame({"type": "status", "status": "idle"})

    async for message in self.ws:
        # ... existing dispatch loop ...
```

New method on `GatewaySession`:

```python
async def _send_history(self) -> None:
    """Send recent conversation history from OpenClaw's session transcript.

    Best-effort: logs and continues on any error.
    """
    try:
        from gateway.session_history import read_history

        entries = read_history(
            session_key=self._session_key,
            agent_id=self._agent_id,
            limit=self._history_limit,
        )
        # Always send the frame (even if entries is empty) so the G2 app
        # knows history replay is complete
        await self.send_frame({
            "type": "history",
            "entries": [
                {"role": e.role, "text": e.text, "ts": e.ts}
                for e in entries
            ],
        })
        logger.info("Sent %d history entries to client", len(entries))
    except Exception:
        logger.warning("Failed to send history — continuing without it", exc_info=True)
```

Constructor changes — pass config values through:

```python
class GatewaySession:
    def __init__(
        self,
        ws: ServerConnection,
        handler: ResponseHandler | None = None,
        transcriber: Transcriber | None = None,
        timeout: int = 120,
        local_audio: bool = False,
        history_limit: int = 10,            # NEW
        session_key: str = "agent:claw:g2", # NEW
        agent_id: str = "claw",             # NEW
    ) -> None:
        # ... existing init ...
        self._history_limit = history_limit
        self._session_key = session_key
        self._agent_id = agent_id
```

In `GatewayServer.handler()`, pass config to session:

```python
session = GatewaySession(
    ws,
    self._handler,
    self._transcriber,
    timeout=self.config.agent_timeout,
    local_audio=self.config.local_audio,
    history_limit=self.config.history_limit,         # NEW
    agent_id=self.config.openclaw_agent_id,          # NEW
)
```

### 2.5 Connection Sequence (After Changes)

```
Phone connects → auth handshake
Gateway sends:  {"type":"connected","version":"1.0"}
Gateway sends:  {"type":"history","entries":[...]}    ← NEW
Gateway sends:  {"type":"status","status":"idle"}
Phone renders history, shows idle
```

---

## 3. G2 App Changes

### 3.1 Protocol Changes: `g2_app/src/protocol.ts`

Add the new frame type:

```typescript
// In frame type definitions:
export interface HistoryEntry {
  role: 'user' | 'assistant';
  text: string;
  ts: number;
}

export interface HistoryFrame {
  type: 'history';
  entries: HistoryEntry[];
}

// Update InboundFrame union:
export type InboundFrame =
  | StatusFrame
  | TranscriptionFrame
  | AssistantDelta
  | EndFrame
  | ErrorFrame
  | ConnectedFrame
  | PingFrame
  | HistoryFrame;    // ← NEW
```

Update parsing infrastructure:

```typescript
// Add to INBOUND_TYPES:
const INBOUND_TYPES = new Set([
  'status', 'transcription', 'assistant', 'end', 'error', 'connected', 'ping',
  'history',  // ← NEW
]);

// Add to REQUIRED_FIELDS:
const REQUIRED_FIELDS: Record<string, string[]> = {
  // ... existing ...
  history: ['entries'],
};

// Add to FIELD_TYPES:
const FIELD_TYPES: Record<string, Record<string, string>> = {
  // ... existing ...
  history: { entries: 'object' },  // arrays are 'object' in typeof
};
```

In `parseFrame()`, add `entries` to the known-field clean copy:

```typescript
// After the clean copy loop, special-case history:
if (clean.type === 'history') {
  // Validate entries is an array
  if (!Array.isArray(frame.entries)) {
    throw new Error('history.entries must be an array');
  }
  clean.entries = frame.entries;
}
```

### 3.2 ConversationHistory Changes: `g2_app/src/conversation.ts`

Add a method to bulk-load history entries:

```typescript
/** Replay history entries received from the Gateway.
 *  Replaces any existing entries (called on reconnect).
 */
replayHistory(entries: Array<{ role: 'user' | 'assistant'; text: string; ts: number }>): void {
  this.clear();
  for (const entry of entries) {
    this.entries.push({
      role: entry.role,
      text: entry.text,
      timestamp: entry.ts,
    });
  }
  this._trim();
}
```

**No persistence needed.** The Gateway is the source of truth (via OpenClaw's
JSONL files). On every reconnect the Gateway re-sends history. The G2 app
remains a pure thin client with no local storage requirements.

### 3.3 Main Routing Changes: `g2_app/src/main.ts`

Add a `history` case to `routeFrame()`:

```typescript
case 'history': {
  console.log(`[Main] History replay: ${frame.entries.length} entries`);
  conversation.replayHistory(frame.entries);
  if (frame.entries.length > 0) {
    display.showTranscript().catch(err => console.error('[Main] Display error:', err));
  }
  break;
}
```

### 3.4 Display Changes: `g2_app/src/display.ts`

Add a `showTranscript()` method that renders the current conversation
transcript (likely already exists as part of `showIdle` logic). If the idle
screen already displays the transcript tail, then `showTranscript()` can just
delegate to the existing transcript-rendering logic:

```typescript
/** Re-render the transcript after history replay. */
async showTranscript(): Promise<void> {
  const text = this.conversation.formatTail(UPGRADE_CHAR_LIMIT);
  await this._upgradeTranscript(text);
}
```

This ensures that after history is loaded, if the subsequent `status: idle`
frame arrives, it will show the populated transcript rather than "Ready."

---

## 4. OpenClaw Interaction

### 4.1 Approach: Direct File Read (No RPC)

The Gateway reads session files directly from the filesystem. This is the
simplest approach because:

1. **Same machine** — Gateway and OpenClaw both run on `localhost`
2. **No new protocol methods** — Avoids modifying OpenClaw's wire protocol
3. **No auth complexity** — File access is just POSIX permissions
4. **OpenClaw exposes `sessions_history`** as an agent tool, but invoking it
   requires a full agent RPC cycle (connect → auth → agent request → stream →
   close) which is heavyweight for a simple history fetch

### 4.2 File Layout Reference

```
~/.openclaw/agents/claw/sessions/
  ├── sessions.json          # { "agent:claw:g2": { "sessionId": "8ecfdf8b-...", ... } }
  └── 8ecfdf8b-....jsonl     # Line-delimited JSON transcript
```

### 4.3 JSONL Entry Format (Messages We Care About)

```jsonc
// User message
{
  "type": "message",
  "message": {
    "role": "user",
    "content": [{ "type": "text", "text": "[Sat 2026-03-07 13:03 CST] Hello" }],
    "timestamp": 1772910239636
  }
}

// Assistant message (successful)
{
  "type": "message",
  "message": {
    "role": "assistant",
    "content": [{ "type": "text", "text": "Hi! How can I help?" }],
    "stopReason": "stop",
    "timestamp": 1772910240500
  }
}

// Assistant message (errored — skip these)
{
  "type": "message",
  "message": {
    "role": "assistant",
    "content": [],
    "stopReason": "error",
    "errorMessage": "Request timed out."
  }
}
```

We **only** extract `type: "message"` lines where `message.role` is `"user"` or
`"assistant"` and `stopReason` is not `"error"`.

### 4.4 Race Condition: File Being Written To

OpenClaw appends to the JSONL file during agent runs. Our read is safe because:
- We read line-by-line and skip malformed JSON (`json.loads` in a try/except)
- A partially-written trailing line is simply skipped
- We only need the last N entries, so a missing partial line at the end doesn't matter

---

## 5. Edge Cases

### 5.1 First Connection (No History)

- `sessions.json` won't have the `"agent:claw:g2"` key, or the JSONL file won't
  exist yet.
- `read_history()` returns `[]`.
- Gateway sends `{"type":"history","entries":[]}`.
- G2 app calls `replayHistory([])` → conversation remains empty → idle shows
  "Ready." as before.

### 5.2 Daily Session Reset

OpenClaw resets sessions at 4:00 AM local time (default). After reset:
- `sessions.json` is updated with a new `sessionId`.
- The old JSONL is renamed to `<id>.jsonl.deleted.<timestamp>`.
- A new JSONL starts fresh.
- `read_history()` reads the new (empty or small) JSONL → few or no entries.

**This is correct behavior** — after a daily reset, conversation context is
fresh and the user sees little/no history.

### 5.3 Large Transcripts

- 90-line JSONL files (observed in current session) take <1ms to parse.
- With `DEFAULT_HISTORY_LIMIT = 10`, we always return at most 10 entries
  regardless of file size.
- Even a 10,000-line transcript would parse in <100ms on the local filesystem.
- The `history` frame payload is bounded by `limit × ~200 chars per entry` ≈ 2KB.

### 5.4 Timing: History Frame vs User Interaction

The frame sequence is strictly ordered:
```
connected → history → status:idle
```
The G2 app won't accept user input until it reaches `idle` state (the
`StateMachine` starts in `loading`). So there's no race between history
rendering and user interaction.

### 5.5 Reconnect During Active Agent Response

If the phone reconnects while the Gateway was mid-stream to the _previous_
connection:
- The old session is closed (Gateway single-connection model)
- The new session starts with `connected → history → idle`
- The interrupted agent response was already committed to the JSONL (OpenClaw
  writes as it streams), so it will appear in the history replay

### 5.6 OpenClaw Not Running / Not Installed

If `~/.openclaw/agents/claw/sessions/sessions.json` doesn't exist,
`read_history()` returns `[]`. The `_send_history()` method catches all
exceptions. The Gateway continues to work with mock mode or without history.

### 5.7 Agent ID Mismatch

Configurable via `OPENCLAW_AGENT_ID` env var (default `"claw"`). If changed,
only need to set the env var — no code changes.

---

## 6. Testing Strategy

### 6.1 Gateway Tests

#### `tests/gateway/test_session_history.py` (New File)

Unit tests for the `session_history` module:

```python
class TestResolveSessionFile:
    """Test session file resolution from sessions.json."""

    def test_returns_path_when_key_exists(self, tmp_path):
        """sessions.json has the key → returns JSONL path."""

    def test_returns_none_when_key_missing(self, tmp_path):
        """sessions.json exists but key is absent → None."""

    def test_returns_none_when_file_missing(self, tmp_path):
        """sessions.json missing entirely → None."""

    def test_returns_none_when_jsonl_missing(self, tmp_path):
        """sessions.json points to non-existent JSONL → None."""

    def test_returns_none_on_corrupt_json(self, tmp_path):
        """sessions.json has invalid JSON → None (log warning)."""


class TestReadHistory:
    """Test JSONL transcript parsing."""

    def test_extracts_user_and_assistant_messages(self, tmp_path):
        """Parses message entries, returns HistoryEntry list."""

    def test_skips_tool_result_and_system_roles(self, tmp_path):
        """Only user + assistant are included."""

    def test_skips_errored_assistant_messages(self, tmp_path):
        """stopReason: 'error' entries are excluded."""

    def test_skips_empty_assistant_content(self, tmp_path):
        """Empty content[] assistant messages are excluded."""

    def test_strips_timestamp_prefix_from_user(self, tmp_path):
        """'[Mon 2026-02-23 23:34 CST] Hello' → 'Hello'."""

    def test_extracts_text_from_content_blocks(self, tmp_path):
        """content: [{type:"text", text:"..."}] → plain text."""

    def test_handles_string_content(self, tmp_path):
        """content: "plain string" → returned as-is."""

    def test_respects_limit(self, tmp_path):
        """With limit=3, returns only last 3 entries."""

    def test_returns_empty_on_missing_file(self, tmp_path):
        """No JSONL → empty list."""

    def test_handles_partial_trailing_line(self, tmp_path):
        """Malformed last line is skipped gracefully."""

    def test_returns_empty_on_no_messages(self, tmp_path):
        """JSONL with only session/model_change entries → empty."""


class TestStripTimestampPrefix:
    """Test timestamp prefix removal."""

    def test_standard_format(self):
        """'[Mon 2026-02-23 23:34 CST] Hello' → 'Hello'."""

    def test_no_prefix(self):
        """'Hello world' → unchanged."""

    def test_empty_after_prefix(self):
        """'[Mon 2026-02-23 23:34 CST] ' → ''."""
```

#### `tests/gateway/test_server.py` (Additions)

Add test to the existing `TestConnection` class:

```python
async def test_connected_sends_history_frame(self, auth_gateway):
    """After connected, receives a history frame before idle."""
    url, _ = auth_gateway
    ws = await _auth_connect(url)
    async with ws:
        connected = await _recv_json(ws)
        assert connected["type"] == "connected"

        history = await _recv_json(ws)
        assert history["type"] == "history"
        assert isinstance(history["entries"], list)

        idle = await _recv_json(ws)
        assert idle == {"type": "status", "status": "idle"}
```

```python
async def test_history_failure_does_not_block_session(self, auth_gateway):
    """If history reading fails, session continues normally."""
```

#### `tests/gateway/test_protocol.py` (Additions)

```python
def test_validate_history_frame():
    """History frame passes outbound validation."""
    validate_outbound({"type": "history", "entries": []})
    validate_outbound({
        "type": "history",
        "entries": [{"role": "user", "text": "hi", "ts": 123}],
    })

def test_history_frame_missing_entries():
    """History frame without entries raises ProtocolError."""
```

### 6.2 G2 App Tests

#### `g2_app/src/__tests__/protocol.test.ts` (Additions)

```typescript
it('parses a history frame', () => {
  const frame = parseFrame(JSON.stringify({
    type: 'history',
    entries: [
      { role: 'user', text: 'Hello', ts: 1000 },
      { role: 'assistant', text: 'Hi!', ts: 2000 },
    ],
  }));
  expect(frame.type).toBe('history');
  expect((frame as HistoryFrame).entries).toHaveLength(2);
});

it('rejects history frame without entries', () => {
  expect(() => parseFrame('{"type":"history"}')).toThrow(/entries/);
});
```

#### `g2_app/src/__tests__/main.test.ts` (Additions)

```typescript
it('routes history frame to conversation replay', async () => {
  // Fire a history frame and verify conversation.replayHistory is called
  // and display.showTranscript is called when entries > 0
});

it('routes empty history frame gracefully', async () => {
  // Empty entries → replayHistory([]) → no display update
});
```

#### `g2_app/src/__tests__/conversation.test.ts` (New or Addition)

```typescript
describe('replayHistory', () => {
  it('replaces existing entries with replayed history', () => {
    const history = new ConversationHistory();
    history.addUser('old message');
    history.replayHistory([
      { role: 'user', text: 'Hello', ts: 1000 },
      { role: 'assistant', text: 'Hi!', ts: 2000 },
    ]);
    expect(history.length).toBe(2);
    expect(history.format()).toContain('Hello');
    expect(history.format()).not.toContain('old message');
  });

  it('handles empty history', () => {
    const history = new ConversationHistory();
    history.replayHistory([]);
    expect(history.length).toBe(0);
    expect(history.format()).toBe('Ready.');
  });
});
```

---

## 7. File Change Summary

| File | Change Type | What Changes |
|---|---|---|
| `gateway/session_history.py` | **NEW** | `resolve_session_file()`, `read_history()`, `HistoryEntry`, `_extract_text()`, `_strip_timestamp_prefix()` |
| `gateway/protocol.py` | MODIFY | Add `HistoryFrame`, `HistoryEntryDict` TypedDicts; register `"history"` in `_OUTBOUND_FIELDS` and `"entries"` in `_FIELD_TYPES` |
| `gateway/config.py` | MODIFY | Add `history_limit: int = 10`, `openclaw_agent_id: str = "claw"` fields; add `HISTORY_LIMIT`, `OPENCLAW_AGENT_ID` env var loading |
| `gateway/server.py` | MODIFY | `GatewaySession.__init__` accepts `history_limit`, `session_key`, `agent_id`; new `_send_history()` method; `handle()` calls `_send_history()` between `connected` and `idle`; `GatewayServer.handler()` passes config to session |
| `g2_app/src/protocol.ts` | MODIFY | Add `HistoryEntry`, `HistoryFrame` interfaces; add to `InboundFrame` union; register in `INBOUND_TYPES`, `REQUIRED_FIELDS`, `FIELD_TYPES`; handle in `parseFrame()` |
| `g2_app/src/conversation.ts` | MODIFY | Add `replayHistory()` method |
| `g2_app/src/main.ts` | MODIFY | Add `case 'history'` to `routeFrame()` |
| `g2_app/src/display.ts` | MODIFY | Add `showTranscript()` method (may be trivial if idle already renders transcript) |
| `tests/gateway/test_session_history.py` | **NEW** | Full test suite for session_history module |
| `tests/gateway/test_server.py` | MODIFY | Add history frame connection test |
| `tests/gateway/test_protocol.py` | MODIFY | Add history frame validation tests |
| `g2_app/src/__tests__/protocol.test.ts` | MODIFY | Add history frame parse tests |
| `g2_app/src/__tests__/main.test.ts` | MODIFY | Add history routing tests |

---

## 8. Implementation Order

1. **`gateway/session_history.py`** + tests — pure function, no dependencies on
   other changes. Can be developed and tested in isolation.
2. **`gateway/protocol.py`** — register the new frame type.
3. **`gateway/config.py`** — add config fields.
4. **`gateway/server.py`** — wire history into session lifecycle.
5. **`g2_app/src/protocol.ts`** — add frame type + parsing.
6. **`g2_app/src/conversation.ts`** — add `replayHistory()`.
7. **`g2_app/src/main.ts`** — add routing case.
8. **`g2_app/src/display.ts`** — add `showTranscript()` if needed.
9. **Integration test** — end-to-end with real JSONL fixture.
