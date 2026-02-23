# Phase 1: Vertical Slice (Text-Only, Mock AI)

## Phase Goal

Phone connects to PC Gateway over WebSocket, sends a text message, receives a mock streamed response, and displays it on the G2 glasses simulator — proving end-to-end plumbing before touching audio or AI.

## Prerequisites

- Node.js ≥ 18 and npm installed (for G2 app)
- Python 3.12+ and `uv` installed (for Gateway)
- EvenHub CLI installed (`npm install -g @evenrealities/evenhub-cli`)
- EvenHub simulator running (or real G2 glasses paired)
- No OpenClaw instance required (responses are mocked)
- No faster-whisper model required (no audio in this phase)

## Task Breakdown

### P1.1 — Gateway Protocol Types (`protocol.py`)

- **Description:** Define all JSON frame types (inbound + outbound) as TypedDicts per doc 02 §2.6 and doc 01 Appendix A. Include `parse_text_frame()` and `serialize()` helpers. Resolve all naming conflicts identified in review §2.2: use `message` for TextMessage payload, `detail` for ErrorFrame, include `version` in ConnectedFrame.
- **Owner:** backend-python
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/gateway/protocol.py`
- **Acceptance criteria:**
  - `StartAudio`, `StopAudio`, `TextMessage`, `PongFrame` inbound types defined
  - `StatusFrame`, `TranscriptionFrame`, `AssistantDelta`, `EndFrame`, `ErrorFrame`, `ConnectedFrame`, `PingFrame` outbound types defined
  - `parse_text_frame(raw: str)` parses JSON and returns typed dict; raises `ProtocolError` on invalid input or unknown type
  - `serialize(frame: dict) -> str` returns JSON string
  - Status literal includes all 6 states: `loading`, `recording`, `transcribing`, `thinking`, `streaming`, `idle`
  - Error codes enum: `AUTH_FAILED`, `TRANSCRIPTION_FAILED`, `BUFFER_OVERFLOW`, `OPENCLAW_ERROR`, `INVALID_FRAME`, `INVALID_STATE`, `TIMEOUT`, `INTERNAL_ERROR`
  - Unit tests in `tests/gateway/test_protocol.py` covering parse/serialize round-trips

### P1.2 — App Protocol Types & Shared Constants (`protocol.ts`)

- **Description:** Define TypeScript interfaces mirroring Gateway protocol types. Define frame type constants, status literals, error code literals. This is the app-side contract for all WebSocket communication.
- **Owner:** g2-development
- **Dependencies:** None (can reference doc 01 Appendix A independently of P1.1)
- **Complexity:** S
- **Files created:**
  - `src/g2_app/src/protocol.ts`
- **Acceptance criteria:**
  - Inbound frame interfaces: `StatusFrame`, `TranscriptionFrame`, `AssistantDelta`, `EndFrame`, `ErrorFrame`, `ConnectedFrame`, `PingFrame`
  - Outbound frame interfaces: `StartAudioFrame`, `StopAudioFrame`, `TextFrame`, `PongFrame`
  - `AppStatus` type union: `'loading' | 'idle' | 'recording' | 'transcribing' | 'thinking' | 'streaming' | 'displaying' | 'error' | 'disconnected'`
  - `parseFrame(data: string): InboundFrame` function with type narrowing
  - All field names match P1.1 exactly (coordinated via doc 01 Appendix A)

### P1.2.5 — Markdown Stripping Utility (moved from P3.5 per review C.2)

- **Description:** Implement `stripMarkdown(text: string): string` per doc 04 §6. Zero-dependency pure utility. Available from Phase 1 for mock response testing — makes Phase 1 integration more realistic since real LLM responses contain markdown.
- **Owner:** g2-development
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/g2_app/src/utils.ts`
  - `src/g2_app/src/__tests__/utils.test.ts`
- **Acceptance criteria:**
  - `stripMarkdown()` strips: `**bold**` → `bold`, `*italic*` → `italic`, `` `inline code` `` → `inline code`, `[link text](url)` → `link text`, `# heading` → `heading` (all levels), code fences → content only, `> blockquote` → content
  - Does NOT corrupt normal text, URLs, or special characters
  - Unit tests with variety of markdown inputs (review F.1)

### P1.3 — Gateway Project Scaffold & Config

> **Consolidation note (review C.1):** P1.1 (protocol types) and P1.3 (scaffold + config) share the same owner and have no external dependencies. They can be combined into a single task if the implementing agent prefers. Kept separate here for granularity.

- **Description:** Create the Gateway Python project with `pyproject.toml`, `config.py`, and basic project structure. Config loads from env vars / `.env` per doc 02 §6. For Phase 1, only `GATEWAY_HOST`, `GATEWAY_PORT`, and `GATEWAY_TOKEN` are needed (no Whisper or OpenClaw config yet).
- **Owner:** backend-python
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/gateway/pyproject.toml`
  - `src/gateway/config.py`
  - `src/gateway/__init__.py`
  - `src/gateway/.env.example`
- **Acceptance criteria:**
  - `pyproject.toml` declares `websockets>=13.0`, `python-dotenv>=1.0` as deps; dev deps include `pytest>=8.0`, `pytest-asyncio>=0.24`, `ruff>=0.5`
  - `load_config() -> GatewayConfig` reads env vars with sensible defaults
  - `GatewayConfig` is a frozen dataclass with: `gateway_host`, `gateway_port`, `gateway_token`
  - `uv sync` succeeds
  - Unit test validates config loading from env vars

### P1.4 — App Project Scaffold

- **Description:** Create the G2 app project: `package.json`, `tsconfig.json`, `vite.config.ts`, `app.json` manifest, `index.html` entry point. Configure Vite for single-bundle output per doc 03 §9.
- **Owner:** g2-development
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/g2_app/package.json`
  - `src/g2_app/tsconfig.json`
  - `src/g2_app/vite.config.ts`
  - `src/g2_app/app.json`
  - `src/g2_app/index.html`
- **Acceptance criteria:**
  - `package.json` lists `@evenrealities/even_hub_sdk` dependency and `vite` + `typescript` dev deps
  - `vite.config.ts` produces single JS bundle (`inlineDynamicImports: true`) per doc 03 §9
  - `app.json` uses correct g2-dev-toolchain schema with all required fields (NOT `appId`/`appName`/`entry` — those are wrong per review A.1). `package_id` must match `^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$`. Golden reference:
    ```json
    {
      "package_id": "com.g2openclaw.app",
      "edition": "202601",
      "name": "OpenClaw",
      "version": "0.1.0",
      "min_app_version": "0.1.0",
      "tagline": "AI assistant for Even Realities G2 glasses",
      "description": "Voice-controlled AI assistant that connects G2 smart glasses to a local PC gateway running OpenClaw.",
      "author": "G2 OpenClaw Contributors",
      "entrypoint": "index.html",
      "permissions": { "network": ["*"] }
    }
    ```
  - No `icon` field in manifest; no `assets/icon.bmp` in project tree (review A.1)
  - `package.json` includes `vitest` as dev dependency; `"test": "vitest run"` in scripts (review F.1)
  - `npm install && npx vite build` produces `dist/` with `index.html` and bundled JS
  - `npx tsc --noEmit` passes with no type errors

### P1.5 — Gateway WebSocket Server Skeleton (`server.py`)

- **Description:** Implement the core WebSocket server: accept a single connection, authenticate via query param token (if configured), classify frames (binary vs text), dispatch text frames via `parse_text_frame()`, send `{type:"connected", version:"1.0"}` on connect. For Phase 1, binary frames log a warning. Handle `{type:"text"}` by returning a mock streamed response (3 deltas + end frame). Implement single-connection model per doc 02 §10.
- **Owner:** backend-python
- **Dependencies:** P1.1 (protocol.py), P1.3 (config.py)
- **Complexity:** M
- **Files created:**
  - `src/gateway/server.py`
- **Acceptance criteria:**
  - `async def main()` starts WebSocket server on configured host:port
  - `GatewaySession` class with `handle_connection()`, `handle_text_frame()`, `handle_binary_frame()`
  - Token auth: rejects connections with wrong/missing token (if `GATEWAY_TOKEN` set) using close code 4001
  - Sends `{type:"connected", version:"1.0"}` after successful connection
  - Sends `{type:"status", status:"idle"}` after connected frame
  - On `{type:"text", message:"..."}` → sends `status:thinking` → 3 mock `assistant` deltas (100ms apart) → `end` → `status:idle`
  - Single-connection: new connection closes existing one
  - Graceful cleanup on disconnect
  - `uv run pytest tests/gateway/test_server.py` passes with mock WebSocket client tests

### P1.6 — App State Machine (`state.ts`)

- **Description:** Implement the app-side state machine per doc 03 §4. Track current state, validate transitions, emit state change events. The `displaying` state ignores `status:"idle"` from Gateway (review §3.3).
- **Owner:** g2-development
- **Dependencies:** P1.2 (protocol.ts)
- **Complexity:** S
- **Files created:**
  - `src/g2_app/src/state.ts`
- **Acceptance criteria:**
  - `AppState` enum: `loading`, `idle`, `recording`, `transcribing`, `thinking`, `streaming`, `displaying`, `error`, `disconnected`
  - `StateMachine` class with `transition(newState)`, `current`, `onChange(cb)` per doc 03 §2
  - Transition validation: rejects invalid transitions (e.g., `loading` → `streaming`)
  - `displaying` state ignores `idle` status frame per review §3.3
  - State change callbacks fire on valid transitions
  - Unit tests in `src/g2_app/src/__tests__/state.test.ts` covering all valid/invalid transitions (review F.1)

### P1.7 — App WebSocket Client (`gateway.ts`)

- **Description:** Implement the Gateway WebSocket client per doc 03 §5. Connect to `ws://<PC_IP>:8765?token=<secret>`, send binary/JSON frames, receive and parse JSON frames, handle auto-reconnect with exponential backoff. URL resolved from hash > query > localStorage > build-time env per doc 03 §8.
- **Owner:** g2-development
- **Dependencies:** P1.2 (protocol.ts), P1.4 (project scaffold)
- **Complexity:** M
- **Files created:**
  - `src/g2_app/src/gateway.ts`
- **Acceptance criteria:**
  - `Gateway` class with `connect()`, `send(data: ArrayBuffer)`, `sendJson(obj)`, `disconnect()`
  - `onMessage(cb)` callback receives parsed `InboundFrame` objects
  - Auto-reconnect: 1s → 2s → 4s → 8s → 16s → 30s max backoff per doc 03 §5
  - Emits `'connected'`, `'disconnected'`, `'reconnecting'` events
  - URL resolution from hash → query → localStorage → env per doc 03 §8
  - Persists resolved URL/token to localStorage on first successful connection
  - Responds to `{type:"ping"}` with `{type:"pong"}` immediately

### P1.8 — Display Manager: Idle + Streaming + Displaying (`display.ts`)

- **Description:** Implement the Display class with layouts for 3 states: idle (doc 04 §4.1), streaming (doc 04 §4.5), and response complete/displaying (doc 04 §4.6). Use the `text()` helper and `rebuild()` wrapper from doc 04 §7.1. Implement `textContainerUpgrade` for delta appending per doc 04 §7.3.
- **Owner:** g2-development
- **Dependencies:** P1.2 (protocol.ts), P1.4 (project scaffold)
- **Complexity:** M
- **Files created:**
  - `src/g2_app/src/display.ts`
- **Acceptance criteria:**
  - `text()` helper builds `TextContainerProperty` with all fields including `borderRdaius` typo (doc 04 §7.1)
  - `rebuild()` wrapper calls `bridge.rebuildPageContainer()` with correct `RebuildPageContainer` structure; handle `APP_REQUEST_REBUILD_PAGE_FAILD` (sic — SDK typo) in error path
  - `showIdle()` — 4 containers matching doc 04 §4.1 spec exactly (positions, font sizes, colors)
  - `showStreaming(query: string)` — 4 containers matching doc 04 §4.5 spec; content container scrollable
  - `showResponse(query: string, response: string)` — 4 containers matching doc 04 §4.6 spec
  - `appendDelta(delta: string)` — uses `textContainerUpgrade` to append text at correct offset (doc 04 §7.3)
  - Streaming cursor `█` shown during stream, stripped on completion

### P1.9 — App Entry Point & Wiring (`main.ts`)

- **Description:** Bootstrap the app: initialize `EvenAppBridge`, call `createStartUpPageContainer`, connect Gateway, wire state machine to display updates and gateway frame handlers. On `{type:"text"}` mock flow: show thinking → streaming → displaying. Handle `connected` frame by transitioning to idle and showing idle display.
- **Owner:** g2-development
- **Dependencies:** P1.6 (state.ts), P1.7 (gateway.ts), P1.8 (display.ts)
- **Complexity:** M
- **Files created:**
  - `src/g2_app/src/main.ts`
- **Acceptance criteria:**
  - `init()` async entry — calls `await waitForEvenAppBridge()` (NOT `getInstance()` — review A.5), then `createStartUpPageContainer` **with return value check** (reject if result !== `StartUpPageCreateResult.success` — review A.6), instantiates Gateway/Display/StateMachine
  - Registers `gateway.onMessage()` handler that routes frames to state machine + display
  - `status` frame → `stateMachine.transition()` → `display.show<State>()`
  - `assistant` delta → strip markdown → `display.appendDelta()`
  - `end` frame → `stateMachine.transition('displaying')` → `display.showResponse()`
  - `connected` frame → transition to idle → `display.showIdle()`
  - `error` frame → basic error display (refined in Phase 4)
  - App boots, connects to Gateway, and renders idle screen on simulator

### P1.10 — Integration Verification

- **Description:** End-to-end smoke test: start Gateway with mock responses, start G2 app on simulator (or Vite dev server), verify text input → mock response displayed on glasses. Write a simple WS test client script for automated verification.
- **Owner:** both (backend-python starts Gateway, g2-development runs app)
- **Dependencies:** P1.5, P1.9 (all previous tasks complete)
- **Complexity:** M
- **Files created:**
  - `tests/integration/test_vertical_slice.py` (Python WS client simulating app)
  - `src/gateway/README.md` (quick-start instructions)
- **Acceptance criteria:**
  - Gateway starts: `uv run python -m gateway.server`
  - App builds and serves: `cd src/g2_app && npx vite --host 0.0.0.0`
  - Test client connects, sends `{type:"text", message:"hello"}`, receives `connected` → `status:idle` → `status:thinking` → `status:streaming` → `assistant` deltas → `end` → `status:idle`
  - On simulator: idle screen visible → text input triggers response → response text renders in content area → footer shows "Tap to dismiss"
  - No errors in Gateway logs or browser console

## Parallel Execution Plan

```
── Time →

Agent A (backend-python):
  [P1.1 protocol.py] ──→ [P1.3 config + scaffold] ──→ [P1.5 server.py skeleton] ──→ [P1.10 integration]
        ~1h                     ~0.5h                        ~2h                        ~1h

Agent B (g2-development):
  [P1.2 protocol.ts] ──→ [P1.4 app scaffold] ──→ [P1.7 gateway.ts] ──→ [P1.9 main.ts wiring] ──→ [P1.10 integration]
        ~1h                    ~1h                    ~2h                    ~2h                      ~1h

Agent C (g2-development):
                         [P1.4 app scaffold] ──→ [P1.6 state.ts] ──→ [P1.8 display.ts] ──→ (feeds into P1.9)
                               ~1h                    ~1h                  ~2h
```

**Parallelization notes:**
- P1.1 and P1.2 run fully in parallel (both derive from doc 01 Appendix A)
- P1.3 and P1.4 run in parallel (independent project scaffolds)
- P1.6, P1.7, P1.8 can all start once P1.2 + P1.4 are done — Agent B takes gateway.ts, Agent C takes state.ts then display.ts
- P1.5 depends on P1.1 + P1.3 (Agent A's prior tasks)
- P1.9 is the merge point — needs state.ts + gateway.ts + display.ts
- P1.10 needs everything — both agents collaborate

## Integration Checkpoint

Before moving to Phase 2, verify:

1. **Gateway accepts WebSocket connections** — test client can connect with token auth
2. **Frame protocol works** — text frames parsed correctly, status frames sent in correct order
3. **Mock response flows end-to-end** — `text` message → `thinking` → `streaming` (3 deltas) → `end` → `idle`
4. **App renders on simulator** — idle screen shows, streaming text appears character-by-character, response complete screen shows with correct footer
5. **State machine transitions are correct** — `displaying` state ignores `status:"idle"`, tap to dismiss works
6. **Single-connection model works** — second connection replaces first
7. **Reconnection works** — kill Gateway, app shows "Connecting...", restart Gateway, app reconnects and shows idle

## Definition of Done

- [x] `uv run pytest tests/gateway/` — all tests pass (protocol, config, server)
- [x] `cd src/g2_app && npx tsc --noEmit` — no type errors
- [x] `cd src/g2_app && npx vite build` — produces working bundle
- [ ] `cd src/g2_app && evenhub pack app.json dist -o openclaw.ehpk` — produces valid `.ehpk` package *(Phase 4 P4.12)*
- [x] Gateway starts with `uv run python -m gateway.server` from `src/gateway/`
- [x] App loads on EvenHub simulator and shows "OpenClaw" idle screen
- [x] Sending `{type:"text", message:"test"}` via WebSocket produces visible streamed response on simulator display
- [x] Auto-reconnect recovers from Gateway restart within 30s
- [x] Token auth rejects unauthorized connections
- [x] Integration test script passes end-to-end
