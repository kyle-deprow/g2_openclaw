# Feature: Session Menu — G2 App Implementation Plan

> **Scope:** G2 App (TypeScript) changes only — `g2_app/src/`  
> **Date:** 2026-03-07  
> **Status:** Draft

## Overview

Add a session menu overlay to the G2 app so the user can browse all OpenClaw
sessions, select one to view its transcript, create new sessions, and navigate
back to the menu with a double-tap from any transcript view.

---

## 1. State Machine Changes

### 1.1 New State: `menu`

Add `'menu'` to the `AppStatus` union in `protocol.ts` and to the transition
table in `state.ts`.

```
AppStatus = GatewayStatus | 'error' | 'disconnected' | 'confirming' | 'menu'
```

### 1.2 Transition Table

The `menu` state is reachable from `idle` (via a long-press or dedicated
gesture — initially mapped to double-tap-from-idle, replacing the current
`resetSession` binding) and returns to `idle` when a session is selected.

```
┌──────────────────────────────────────────────────────────────────┐
│                      STATE TRANSITIONS                           │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│                    ┌──────────┐                                   │
│    ┌──────────────►│   menu   │◄──────────────────┐              │
│    │ double-tap    └────┬─────┘  double-tap from   │              │
│    │ (from idle)        │        transcript view    │              │
│    │                    │                           │              │
│    │  ┌─────────────────┼────────────────────┐     │              │
│    │  │ tap session     │ tap "New Session"   │     │              │
│    │  ▼                 ▼                     │     │              │
│  ┌─────┐          ┌──────────┐               │     │              │
│  │idle │◄─────────│  idle    │  (new session  │     │              │
│  │(sel)│  loaded   │  (new)   │   created)    │     │              │
│  └─────┘  history  └──────────┘               │     │              │
│    │                                          │     │              │
│    │  (normal app flow continues)             │     │              │
│    ▼                                          │     │              │
│  recording → transcribing → confirming ───────┘     │              │
│  thinking → streaming ──────────────────────────────┘              │
│  error ─────────────────────────────────────────────┘              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Concrete transition table additions** (in `state.ts` `TRANSITIONS` map):

| From          | New allowed target(s) |
|---------------|-----------------------|
| `idle`        | + `menu`              |
| `menu`        | `idle`, `error`, `disconnected` |
| `streaming`   | (unchanged — double-tap → `menu` if desired, or `idle` then `menu`) |
| `error`       | + `menu` (tap dismisses error, then double-tap opens menu) |

> **Design decision:** Enter `menu` only from `idle` to avoid interrupting
> active operations. If the user is streaming/recording, double-tap first
> cancels (existing behaviour), returning to `idle`, *then* a second
> double-tap opens menu. This preserves the existing cancel semantics.

### 1.3 Impact on `StateMachine` Class

No structural changes — only the `TRANSITIONS` record gains the `menu` key and
existing states gain `menu` in their allowed-targets arrays.

---

## 2. Protocol Changes

### 2.1 New Inbound Frames (Gateway → App)

#### `SessionListFrame`

Sent by the gateway in response to a `list_sessions` request.

```typescript
export interface SessionListEntry {
  sessionKey: string;          // e.g. "agent:claw:g2:abc123"
  sessionId: string;           // OpenClaw session ID
  label: string;               // human-readable label (≤64 chars, for list item)
  updatedAt: string;           // ISO-8601 timestamp
  isActive: boolean;           // true if this is the currently active session
}

export interface SessionListFrame {
  type: 'session_list';
  sessions: SessionListEntry[];
}
```

#### `SessionSwitchedFrame`

Confirmation that the gateway switched to the requested session.

```typescript
export interface SessionSwitchedFrame {
  type: 'session_switched';
  sessionId: string;
  sessionKey: string;
}
```

### 2.2 New Outbound Frames (App → Gateway)

```typescript
export interface ListSessionsFrame {
  type: 'list_sessions';
}

export interface SwitchSessionFrame {
  type: 'switch_session';
  sessionKey: string;
}

export interface NewSessionFrame {
  type: 'new_session';
}
```

### 2.3 Frame Union Updates

```typescript
// Add to InboundFrame union:
export type InboundFrame =
  | ... // existing
  | SessionListFrame
  | SessionSwitchedFrame;

// Add to OutboundFrame union:
export type OutboundFrame =
  | ... // existing
  | ListSessionsFrame
  | SwitchSessionFrame
  | NewSessionFrame;
```

### 2.4 Parse/Validation Updates

- Add `'session_list'` and `'session_switched'` to `INBOUND_TYPES`.
- Add required-field definitions:
  - `session_list`: `['sessions']` (array validation like `history`)
  - `session_switched`: `['sessionId', 'sessionKey']`
- Add field-type definitions to `FIELD_TYPES`.
- Validate `sessions` array entries: each must have `sessionKey` (string),
  `sessionId` (string), `label` (string), and `updatedAt` (string). `isActive`
  is boolean, defaults to `false` if missing.
- Add `'list_sessions'`, `'switch_session'`, `'new_session'` as accepted
  outbound frame types (if outbound validation exists, otherwise no change).

---

## 3. Gateway Client Changes

### 3.1 New Methods on `Gateway`

```typescript
/** Request the list of available sessions from the gateway. */
requestSessionList(): void {
  this.sendJson({ type: 'list_sessions' });
}

/** Request switching to a different session. */
switchSession(sessionKey: string): void {
  this.sendJson({ type: 'switch_session', sessionKey });
}

/** Request creation of a new session. */
createNewSession(): void {
  this.sendJson({ type: 'new_session' });
}
```

These are thin wrappers — no new state or callbacks needed on `Gateway` itself.
The response frames (`session_list`, `session_switched`) flow through the
existing `onMessage` callback pipeline into `routeFrame()`.

---

## 4. Display Manager Changes

### 4.1 New Display Mode Concept

The `DisplayManager` currently operates in a single mode: **transcript view**
(status bar + scrollable text transcript + footer). The session menu requires a
second mode: **list view** (status bar + scrollable list + footer).

Switching between modes requires a `rebuildPageContainer` call since the
container *type* changes (text → list or list → text). This is acceptable
because mode switches are user-initiated and infrequent.

### 4.2 Container Layout: Menu Mode

```
┌────────────────────── 576 px ───────────────────────┐
│ ID:1 status    "OpenClaw  ● Sessions"    (text, 22px)│ y=4
├─────────────────────────────────────────────────────┤
│ ID:3 menu-list  [ListContainer]          (list,228px)│ y=30
│   ┌─────────────────────────────────────┐           │
│   │ ▸ + New Session                     │           │
│   │   [Session label 1]                 │           │
│   │   [Session label 2]                 │           │
│   │   ...up to 20 items                 │           │
│   └─────────────────────────────────────┘           │
├─────────────────────────────────────────────────────┤
│ ID:4 footer    "Tap to select"           (text, 22px)│ y=262
└─────────────────────────────────────────────────────┘
  containerTotalNum = 3
  isEventCapture: list container (ID:3)
```

**Key constraints respected:**
- Max 4 containers: using 3 (status text + list + footer text) — within limit.
- Exactly one `isEventCapture: 1` — the list container.
- List items 1–20: first item is "✦ New Session", remaining are session labels.
- Item names ≤ 64 chars: `label` field is pre-truncated by the gateway.
- No hidden event-capture list needed (the visible list *is* the capture target).

> **Note:** The current transcript view uses an invisible 1×1
> `ListContainerProperty` for event capture (simulator compat hack). In menu
> mode this is unnecessary since the visible list container takes capture.
> However, on return to transcript mode, the invisible list must be restored.

### 4.3 Container Layout: Transcript Mode (Unchanged)

Remains as-is: status (text) + transcript (text, `isEventCapture:1`) + footer (text) + invisible event-capture list. Total = 4 containers.

### 4.4 New Methods

```typescript
/**
 * Rebuild the display in menu (list) mode.
 * @param sessions — array of session entries from the gateway
 * @param activeIndex — the index of the currently active session (for highlight)
 */
async showSessionMenu(sessions: SessionListEntry[]): Promise<void>

/**
 * Return to transcript mode from menu mode.
 * Triggers a full rebuild to swap list → text containers.
 */
async exitMenuMode(): Promise<void>
```

### 4.5 Internal State Tracking

Add a private field:

```typescript
private _mode: 'transcript' | 'menu' = 'transcript';
```

Guard all existing `updateStatus`, `updateFooter`, `replaceTranscript`,
`appendToTranscript` methods:

```typescript
if (this._mode !== 'transcript') return; // no-op in menu mode
```

`showSessionMenu()` sets `_mode = 'menu'` and calls `rebuildPageContainer`
with the list layout. `exitMenuMode()` sets `_mode = 'transcript'` and
calls `rebuildPageContainer` to restore the transcript layout.

### 4.6 Session List Building

```typescript
private _buildSessionListItems(sessions: SessionListEntry[]): string[] {
  const items: string[] = ['✦ New Session'];
  for (const s of sessions.slice(0, 19)) { // 1 new + 19 sessions = 20 max
    const prefix = s.isActive ? '● ' : '  ';
    const label = s.label.slice(0, 60); // leave room for prefix
    items.push(`${prefix}${label}`);
  }
  return items;
}
```

### 4.7 Rebuild Strategy

| Trigger | Method | Flicker? |
|---------|--------|----------|
| Enter menu mode | `rebuildPageContainer` (text→list) | Yes — unavoidable, container type change |
| Exit menu mode | `rebuildPageContainer` (list→text) | Yes — unavoidable, container type change |
| Session list refresh (while in menu) | `rebuildPageContainer` (list→list with new items) | Yes — lists cannot be updated in-place |
| All other state changes | `textContainerUpgrade` (existing) | No |

---

## 5. Input Handler Changes

### 5.1 Updated Double-Tap Behaviour

The `_handleDoubleTap()` method currently has three behaviours:
- `idle` → `resetSession()`
- `confirming` → `rejectTranscription()`
- `thinking`/`streaming` → `cancelResponse()`

**New behaviour:**

| Current state | Double-tap action |
|---------------|-------------------|
| `idle`        | **Open session menu** (transition to `menu`, request session list) |
| `menu`        | **Close session menu** (transition back to `idle`, restore transcript) |
| `confirming`  | Reject transcription (unchanged) |
| `thinking`/`streaming` | Cancel response (unchanged) |
| All others    | No-op (unchanged) |

> **Breaking change:** Double-tap in `idle` no longer resets the session.
> Session reset is relocated to the menu (could be a special list item, or
> handled server-side). This is acceptable because:
> 1. The new menu provides a more intentional UX for session management.
> 2. Accidental session resets are eliminated.
> 3. The "New Session" menu item serves the same purpose.

### 5.2 Updated Tap Behaviour

| Current state | Tap action |
|---------------|------------|
| `menu`        | **Select the highlighted session** (read selected index from list event) |
| All others    | Unchanged (start recording, stop recording, confirm, dismiss error, reconnect) |

### 5.3 New Event Routing in Menu Mode

When in `menu` mode, the `isEventCapture` container is a **list**, so events
arrive as `listEvent` (on real hardware) or `sysEvent` (on simulator). The
`currentSelectItemIndex` and `currentSelectItemName` fields identify the
selected session.

```typescript
private _handleMenuTap(event: EvenHubEvent): void {
  // Extract selection from list event
  const index = event.listEvent?.currentSelectItemIndex ?? this._trackedMenuIndex;
  const name = event.listEvent?.currentSelectItemName;

  if (index === 0 || name?.includes('New Session')) {
    // "New Session" item
    this.gateway.createNewSession();
  } else {
    // Session selection — map list index back to session entry
    const sessionIndex = index - 1; // offset for "New Session" item
    const session = this._sessionList?.[sessionIndex];
    if (session) {
      this.gateway.switchSession(session.sessionKey);
    }
  }
}
```

### 5.4 Tracked Menu State

The `InputHandler` needs to store the session list received from the gateway
so it can map a selected list index back to a `sessionKey`:

```typescript
private _sessionList: SessionListEntry[] | null = null;
private _trackedMenuIndex: number = 0;

/** Called by main.ts when a session_list frame arrives. */
setSessionList(sessions: SessionListEntry[]): void {
  this._sessionList = sessions;
  this._trackedMenuIndex = 0;
}
```

> **SDK Quirk handling:** `currentSelectItemIndex` may be `undefined` when
> index === 0 (Quirk 2). The `_trackedMenuIndex` fallback covers this. On
> scroll events, update `_trackedMenuIndex` from the event payload if available.

### 5.5 New Public Methods

```typescript
/** Open the session menu. */
openSessionMenu(): boolean {
  if (this.sm.current !== 'idle') return false;
  this.sm.transition('menu');
  this.gateway.requestSessionList();
  this.display.showSessionMenu([]).catch(/**/); // empty until list arrives
  return true;
}

/** Close the session menu and return to idle transcript view. */
closeSessionMenu(): boolean {
  if (this.sm.current !== 'menu') return false;
  this._sessionList = null;
  this.sm.transition('idle');
  this.display.exitMenuMode().catch(/**/);
  this.display.showIdle().catch(/**/);
  return true;
}
```

### 5.6 Revised `_handleEvent` for Menu Mode

The existing `_handleEvent` examines `eventType` from a unified extraction.
In `menu` state, we need to *also* read `listEvent` details. The handler
must branch early:

```typescript
private _handleEvent(eventType: number | undefined, event?: EvenHubEvent): void {
  if (this.sm.current === 'menu') {
    if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
      this._handleMenuTap(event);
    } else if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      this.closeSessionMenu();
    }
    // Scroll events handled natively by firmware list — no app action needed.
    return;
  }
  // ... existing handling ...
}
```

> **Implementation note:** The `onEvenHubEvent` callback must pass the full
> `EvenHubEvent` object (not just `eventType`) to `_handleEvent`. This is a
> small signature change. Currently `_handleEvent` receives only `eventType`;
> add the raw event as a second optional parameter.

---

## 6. Conversation Model Changes

### 6.1 Session Switch: Clear and Reload

When the user selects a different session from the menu, the `ConversationHistory`
must be cleared and repopulated from the new session's history. This happens
through the existing mechanism:

1. Gateway sends `session_switched` frame.
2. `routeFrame()` in `main.ts` calls `conversation.clear()`.
3. Gateway sends `history` frame with the new session's entries.
4. `routeFrame()` calls `conversation.replayHistory(entries)`.

**No changes to `ConversationHistory` class itself.** The clear-and-replay
flow already exists (used by `session_reset` handling).

### 6.2 New Session: Same Flow

Creating a new session follows the same pattern — the gateway responds with
`session_switched` (new empty session) followed by a `history` frame with
zero entries.

---

## 7. Main.ts — Frame Routing Additions

### 7.1 New Frame Handlers in `routeFrame()`

```typescript
case 'session_list': {
  console.log(`[Main] Session list: ${frame.sessions.length} sessions`);
  input.setSessionList(frame.sessions);
  if (sm.current === 'menu') {
    display.showSessionMenu(frame.sessions)
      .catch(err => console.error('[Main] Display error:', err));
  }
  break;
}

case 'session_switched': {
  console.log(`[Main] Switched to session: ${frame.sessionId}`);
  conversation.clear();
  try {
    localStorage.setItem(SESSION_ID_KEY, frame.sessionId);
  } catch { /* non-fatal */ }
  // Gateway will follow up with a 'history' frame.
  // Transition from menu → idle.
  if (sm.current === 'menu') {
    input.closeSessionMenu();
  }
  sm.transition('idle');
  display.showIdle().catch(err => console.error('[Main] Display error:', err));
  break;
}
```

### 7.2 Dev Hook Updates

Add `openSessionMenu` and `closeSessionMenu` to the `__g2Dev` object for
simulator/HIL testing.

---

## 8. File-by-File Change Summary

| File | Change type | Description |
|------|-------------|-------------|
| `protocol.ts` | Add types | `SessionListEntry`, `SessionListFrame`, `SessionSwitchedFrame`, `ListSessionsFrame`, `SwitchSessionFrame`, `NewSessionFrame`; update unions; update parse logic |
| `state.ts` | Modify | Add `'menu'` to `AppStatus`, add transition rules |
| `gateway.ts` | Add methods | `requestSessionList()`, `switchSession()`, `createNewSession()` |
| `display.ts` | Add methods + mode | `_mode` field, `showSessionMenu()`, `exitMenuMode()`, list-building helpers; guards on text-mode-only methods |
| `input.ts` | Modify + add | Revised `_handleDoubleTap()`, new `_handleMenuTap()`, `openSessionMenu()`, `closeSessionMenu()`, `setSessionList()`, pass raw event to `_handleEvent` |
| `main.ts` | Add cases | Route `session_list` and `session_switched` frames; update dev hooks |
| `conversation.ts` | No changes | Existing `clear()` and `replayHistory()` suffice |
| `utils.ts` | No changes | — |

---

## 9. Test Plan

### 9.1 `protocol.test.ts` — New Frame Parsing

| Test | Description |
|------|-------------|
| `parseFrame: session_list with valid sessions` | Parses correctly, filters malformed entries |
| `parseFrame: session_list with empty array` | Returns `{ type: 'session_list', sessions: [] }` |
| `parseFrame: session_list missing sessions field` | Throws |
| `parseFrame: session_switched valid` | Parses `sessionId` and `sessionKey` |
| `parseFrame: session_switched missing sessionId` | Throws |
| `parseFrame: unknown outbound types accepted` | `list_sessions`, `switch_session`, `new_session` don't break serialisation |

### 9.2 `state.test.ts` — Menu Transitions

| Test | Description |
|------|-------------|
| `idle → menu is valid` | Transition succeeds |
| `menu → idle is valid` | Transition succeeds |
| `menu → error is valid` | Transition succeeds |
| `menu → disconnected is valid` | Transition succeeds |
| `recording → menu is rejected` | Cannot open menu while recording |
| `streaming → menu is rejected` | Cannot open menu while streaming |
| `thinking → menu is rejected` | Cannot open menu while thinking |

### 9.3 `display.test.ts` — Menu Mode Display

| Test | Description |
|------|-------------|
| `showSessionMenu: calls rebuildPageContainer with list` | Verify list container has correct `itemName` array |
| `showSessionMenu: respects max 20 items` | Truncates to 19 sessions + 1 "New Session" |
| `showSessionMenu: marks active session with ● prefix` | Check item name formatting |
| `showSessionMenu: sets _mode to 'menu'` | Internal mode tracking |
| `exitMenuMode: restores transcript layout` | Rebuilds with text containers |
| `exitMenuMode: sets _mode to 'transcript'` | Mode tracking reset |
| `text methods no-op in menu mode` | `updateStatus`, `replaceTranscript`, etc. return early |
| `showSessionMenu: empty list shows only "New Session"` | Single-item list |

### 9.4 `input.test.ts` — Menu Interaction

| Test | Description |
|------|-------------|
| `double-tap in idle opens menu` | Triggers `openSessionMenu()`, transitions to `menu` |
| `double-tap in menu closes menu` | Triggers `closeSessionMenu()`, transitions to `idle` |
| `tap in menu selects session` | Calls `gateway.switchSession()` with correct key |
| `tap on "New Session" creates session` | Calls `gateway.createNewSession()` |
| `menu tap with undefined index (Quirk 2)` | Falls back to `_trackedMenuIndex` |
| `setSessionList stores sessions` | Internal state updated correctly |
| `double-tap in confirming still rejects` | Existing behaviour preserved |
| `double-tap in streaming still cancels` | Existing behaviour preserved |
| `tap in menu with no session list` | Graceful no-op |

### 9.5 `gateway.test.ts` — New Methods

| Test | Description |
|------|-------------|
| `requestSessionList sends correct frame` | `sendJson({ type: 'list_sessions' })` |
| `switchSession sends correct frame` | `sendJson({ type: 'switch_session', sessionKey })` |
| `createNewSession sends correct frame` | `sendJson({ type: 'new_session' })` |

### 9.6 `main.test.ts` — Frame Routing

| Test | Description |
|------|-------------|
| `session_list frame updates input and display` | Calls `setSessionList` and `showSessionMenu` |
| `session_list frame ignored when not in menu` | No display update |
| `session_switched frame clears conversation` | `conversation.clear()` called |
| `session_switched frame transitions menu → idle` | State machine transition |
| `session_switched frame updates localStorage` | Session ID persisted |

---

## 10. Dependencies & Ordering

Tasks are ordered to minimise integration risk — protocol first, then model
changes, then UI, then wiring.

```
Phase 1: Protocol & Types (no runtime impact)
  ├── 1a. Add frame types to protocol.ts
  ├── 1b. Add 'menu' to AppStatus in protocol.ts
  ├── 1c. Add parse/validation for new inbound frames
  └── 1d. Write protocol.test.ts cases

Phase 2: State Machine
  ├── 2a. Add 'menu' transitions to state.ts
  └── 2b. Write state.test.ts cases

Phase 3: Gateway Client
  ├── 3a. Add requestSessionList, switchSession, createNewSession
  └── 3b. Write gateway.test.ts cases

Phase 4: Display Manager
  ├── 4a. Add _mode field and guards
  ├── 4b. Implement showSessionMenu() with list layout
  ├── 4c. Implement exitMenuMode() transcript restore
  └── 4d. Write display.test.ts cases

Phase 5: Input Handler
  ├── 5a. Pass raw EvenHubEvent to _handleEvent (signature change)
  ├── 5b. Add menu state handling in _handleEvent
  ├── 5c. Add openSessionMenu / closeSessionMenu / setSessionList
  ├── 5d. Remap double-tap-in-idle from resetSession to openSessionMenu
  └── 5e. Write input.test.ts cases

Phase 6: Main Wiring
  ├── 6a. Add session_list and session_switched to routeFrame()
  ├── 6b. Update dev hooks
  └── 6c. Write main.test.ts cases

Phase 7: Integration Testing (simulator)
  └── 7a. Manual sim test: open menu → select → view transcript → back
```

### Dependency Graph

```
protocol.ts ──┬──► state.ts ──► input.ts ──► main.ts
               │                    ▲
               ├──► gateway.ts ─────┘
               │                    ▲
               └──► display.ts ─────┘
```

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Gateway doesn't implement `list_sessions` yet | Menu shows empty | Show "Loading..." then "No sessions" after a 3s timeout. Server-side is out of scope for this plan but must be built in parallel. |
| List container doesn't fire `listEvent` on simulator | Can't test selection | Simulator sends `sysEvent` for clicks. Existing unified event extraction (`listEvent ?? textEvent ?? sysEvent`) covers this, but `currentSelectItemIndex` is only on `listEvent`. Fallback to `_trackedMenuIndex`. |
| `currentSelectItemIndex` = 0 → undefined (Quirk 2) | First session always selected wrong | Track index in app state; default to 0 when `undefined`. |
| More than 20 sessions | Truncation | Cap at 19 sessions + "New Session" = 20 items. Show newest first (sorted by `updatedAt` descending — sorting is done gateway-side). |
| Rebuild flicker when switching modes | Brief visual flash | Acceptable — mode switches are user-initiated. Show brief "Loading..." during rebuild. |
| Double-tap-in-idle no longer resets session | Users lose quick-reset | "New Session" in the menu serves the same purpose with one extra tap. Document the UX change. |

---

## 12. Open Questions

1. **Session labels:** What does the gateway use as the `label` field? Options:
   first user message (truncated), session key suffix, or date+time. The gateway
   should compute this from the first message in the JSONL transcript.

2. **Session ordering:** Should sessions be sorted by `updatedAt` descending
   (most recent first) or by creation date? Recommend `updatedAt` descending.

3. **Session limit:** The list supports 20 items (minus 1 for "New Session" = 19).
   If the user has more than 19 sessions, should the menu paginate or just show
   the 19 most recent? Recommend: show 19 most recent, no pagination in v1.

4. **Active session indicator on reconnect:** When the gateway sends
   `session_list`, should the currently active session be highlighted differently?
   Plan assumes `isActive: true` field, rendered with `●` prefix.

5. **Gateway protocol finalisation:** The gateway server does not yet handle
   `list_sessions`, `switch_session`, or `new_session` frames. A companion
   gateway plan is needed. This G2 app plan is designed to be gateway-agnostic —
   the app just sends/receives the frames defined above.
