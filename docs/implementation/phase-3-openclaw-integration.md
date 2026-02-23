# Phase 3: OpenClaw Integration

## Phase Goal

Full voice-to-AI-response pipeline: speak into glasses → Gateway transcribes → OpenClaw processes → streamed AI response renders on glasses display in real time.

## Prerequisites

- Phase 2 complete: audio capture, transcription, display layouts for recording/transcribing/thinking all working
- OpenClaw Gateway running on localhost:18789 with a configured agent
- `OPENCLAW_GATEWAY_TOKEN` set in environment / `.env`
- Understanding of OpenClaw wire protocol (doc 01 Appendix B, `docs/openclaw_research/02_agent_architecture_and_context.md`)

## Task Breakdown

### P3.0 — Mock OpenClaw WebSocket Server (review D.6)

- **Description:** Create a minimal WebSocket server (~50 lines) that simulates OpenClaw for offline development and CI. Accepts `connect` auth, accepts `agent` requests, streams back canned responses. Enables Phase 3 G2 app development without a real OpenClaw instance.
- **Owner:** backend-python
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `tests/mocks/mock_openclaw.py`
- **Acceptance criteria:**
  - Listens on configurable port (default 18789)
  - Handles `connect` auth request → responds `{ok:true}`
  - Handles `agent` request → streams 5 canned `assistant` deltas (50ms apart) → `lifecycle` end
  - Supports `MOCK_RESPONSE` env var for custom response text
  - Can be started standalone: `python tests/mocks/mock_openclaw.py`

### P3.1 — OpenClaw Client (`openclaw_client.py`)

- **Description:** Implement the WebSocket client to OpenClaw per doc 02 §2.3 and §5. Lazy connection (only connects on first agent message per doc 02 §5.1). Auth handshake with `connect` method (doc 02 §5.2). Request ID management with monotonic counter (doc 02 §5.3). Send `agent` method with `sessionKey` (doc 02 §5.4-5.5). Parse stream events and yield assistant deltas (doc 02 §5.6). Use `phase` field for lifecycle events per review §2.3. Reconnection with exponential backoff (doc 02 §5.7).
- **Owner:** backend-python
- **Dependencies:** P1.1 (protocol.py), P1.3 (config.py)
- **Complexity:** L
- **Files created:**
  - `src/gateway/openclaw_client.py`
  - `tests/gateway/test_openclaw_client.py`
- **Acceptance criteria:**
  - `OpenClawClient.__init__(host, port, token)` — stores params, no immediate connection
  - `ensure_connected()` — lazy connect + auth handshake; sends `{type:"req", id:1, method:"connect", params:{auth:{token:"..."}}}` and validates `{ok:true}` response (doc 02 §5.2)
  - `send_message(text, session_key="agent:claw:g2") -> AsyncIterator[str]` — sends agent request, yields delta strings as they arrive (doc 02 §5.4)
  - Request ID counter: monotonic int starting at 1, resets on reconnect (doc 02 §5.3)
  - Session key: defaults to `"agent:claw:g2"` (doc 02 §5.5)
  - Parses `{type:"event", event:"agent", payload:{stream:"assistant", delta:"..."}}` → yields delta
  - Parses `{type:"event", event:"agent", payload:{stream:"lifecycle", phase:"end"}}` → signals completion (**note: field is `phase` not `status`** per review §2.3)
  - Parses `{type:"event", event:"agent", payload:{stream:"lifecycle", phase:"error"}}` → raises `OpenClawError`
  - `close()` — gracefully close WebSocket
  - Reconnection: exponential backoff 1s → 2s → 4s → 8s → max 30s with ±20% jitter (doc 02 §5.7)
  - Error handling per doc 02 §5.8: connection refused, auth rejected, agent error, unexpected disconnect, malformed response
  - **Verification checkpoint (review B.4):** Integration test with a real OpenClaw instance confirms lifecycle field name (`phase` vs `status`) before proceeding
  - Tests: mock WebSocket server simulating OpenClaw responses, verify auth handshake, streaming deltas, lifecycle end, error scenarios, reconnection

### P3.2 — Gateway Config: OpenClaw Settings

- **Description:** Extend `config.py` with OpenClaw connection settings per doc 02 §6. `OPENCLAW_GATEWAY_TOKEN` is required — Gateway refuses to start without it. Add `AGENT_TIMEOUT` for full cycle timeout.
- **Owner:** backend-python
- **Dependencies:** P1.3 (config.py exists)
- **Complexity:** S
- **Files created/modified:**
  - `src/gateway/config.py` (modify — add OpenClaw fields)
  - `tests/gateway/test_config.py` (modify)
- **Acceptance criteria:**
  - `GatewayConfig` gains: `openclaw_host` (default `127.0.0.1`), `openclaw_port` (default `18789`), `openclaw_gateway_token` (required), `agent_timeout` (default `120`)
  - `load_config()` raises error if `OPENCLAW_GATEWAY_TOKEN` not set
  - Config logs redact token values
  - Test: verify required token check, verify defaults

### P3.2.5 — OpenClaw Agent Configuration (review A.9)

- **Description:** Create the G2-specific OpenClaw agent identity. Without a concise-response system prompt, LLM answers will routinely exceed 2000 chars and every response will be truncated on page 0. Also restrict tools that are useless on glasses (browser, canvas, etc.).
- **Owner:** backend-python
- **Dependencies:** None (can be done early, just config files)
- **Complexity:** S
- **Files created:**
  - `src/gateway/agent_config/SOUL.md` (system prompt: "keep responses under 150 words")
  - `src/gateway/agent_config/README.md` (documents the agent setup)
- **Acceptance criteria:**
  - System prompt instructs: concise answers (50-150 words), plain text only (no markdown), no code blocks unless explicitly asked
  - Session key: `agent:claw:g2` (doc 02 §5.5)
  - Documents which OpenClaw tools are useful vs. useless for G2 (browser/canvas → deny)
  - Notes on response length control for the constrained display

### P3.3 — Gateway Wiring: Transcribe → OpenClaw → Stream

- **Description:** Replace the mock response in `server.py` with real OpenClaw integration. After transcription (or text input), forward to OpenClaw via `openclaw_client.send_message()`, relay each yielded delta back to phone as `{type:"assistant", delta:"..."}`. Implement agent timeout per doc 02 §7.2 #14. Handle all OpenClaw error scenarios.
- **Owner:** backend-python
- **Dependencies:** P3.1 (openclaw_client), P3.2 (config), P2.3 (server with audio FSM)
- **Complexity:** L
- **Files created/modified:**
  - `src/gateway/server.py` (modify — replace mock response with OpenClaw relay)
  - `tests/gateway/test_server_openclaw.py`
- **Acceptance criteria:**
  - After transcription: `status:thinking` → call `openclaw_client.send_message(text)` → on first delta: `status:streaming` → relay each delta as `{type:"assistant", delta:"..."}` → on lifecycle end: `{type:"end"}` + `status:idle`
  - Text input path: `{type:"text"}` → skip transcription → `status:thinking` → OpenClaw → stream back
  - Agent timeout: `asyncio.wait_for()` wraps the entire agent cycle with `AGENT_TIMEOUT` seconds (default 120); on timeout: `{type:"error", code:"TIMEOUT"}` → idle (doc 02 §7.2 #14)
  - Lazy connection: first agent request triggers `ensure_connected()`
  - OpenClaw errors mapped to phone error frames per doc 02 §5.8:
    - Connection refused → `{code:"OPENCLAW_ERROR", detail:"connection refused"}`
    - Auth rejected → `{code:"OPENCLAW_ERROR", detail:"auth rejected"}`
    - Agent error → `{code:"OPENCLAW_ERROR", detail:"agent error"}`
    - Disconnected mid-stream → `{code:"OPENCLAW_ERROR", detail:"disconnected"}`
  - Tests: mock OpenClaw server, verify full flow: text → thinking → streaming → end, verify timeout, verify error forwarding

### P3.4 — Thinking State Display

- **Description:** Add thinking state layout to `display.ts` per doc 04 §4.4. Shows user query as confirmation + "Thinking..." indicator with animated dots.
- **Owner:** g2-development
- **Dependencies:** P1.8 (display.ts base)
- **Complexity:** S
- **Files created/modified:**
  - `src/g2_app/src/display.ts` (modify — add `showThinking(query)`)
- **Acceptance criteria:**
  - `showThinking(query: string)` — 4 containers matching doc 04 §4.4:
    - Title: `"OpenClaw"` (24px, 0xF)
    - Badge: `"● Thinking"` (18px, 0xC accent)
    - Content: `"You: {query}\n\n━━━━━━━━━━━━━━━\nThinking..."` (18px, 0xF) — query truncated at ~3 lines
    - Footer: `"Waiting for response"` (18px, 0x6)
  - `rebuildPageContainer` on entry
  - Animated dots via 500ms `textContainerUpgrade` cycle (match transcribing pattern)

### ~~P3.5~~ — Markdown Stripping Utility → **Moved to Phase 1 as P1.2.5** (review C.2)

Zero-dependency pure utility. Available from Phase 1 for mock response testing.

### P3.6 — Delta Buffering During Layout Transitions

- **Description:** Implement delta buffering per doc 04 §4.5 and review §3.5. When transitioning from thinking to streaming, `rebuildPageContainer` is async (BLE round-trip ~50-100ms). During this time, incoming deltas must be buffered and flushed after rebuild completes.
- **Owner:** g2-development
- **Dependencies:** P1.8 (display.ts)
- **Complexity:** M
- **Files created/modified:**
  - `src/g2_app/src/display.ts` (modify — add delta buffer logic)
- **Acceptance criteria:**
  - `Display` class gains a `_pendingDeltas: string[]` buffer and `_layoutPending: boolean` flag
  - On thinking → streaming transition: set `_layoutPending = true`, start `rebuildPageContainer`, buffer all incoming deltas in `_pendingDeltas`
  - When `rebuildPageContainer` resolves: set `_layoutPending = false`, flush all buffered deltas via single `textContainerUpgrade` call, resume normal per-delta updates
  - No deltas lost during transition — verified by accumulating expected vs actual text
  - Works correctly even if 0 deltas arrive during transition (edge case)

### P3.7 — Response Length Truncation

- **Description:** Handle the 2000-character `textContainerUpgrade` limit per doc 04 §4.6 and review §3.4. Truncate page 0 display at ~1800 chars with `"… [double-tap for more]"`. Full response accessible on page 1. Streaming cursor (`█`) management.
- **Owner:** g2-development
- **Dependencies:** P1.8 (display.ts), P3.6 (delta buffering)
- **Complexity:** M
- **Files created/modified:**
  - `src/g2_app/src/display.ts` (modify — add truncation logic)
- **Acceptance criteria:**
  - Track total accumulated response length
  - At ~1800 characters: stop appending to page 0 content, show `"… [double-tap for more]"` at end
  - Full untruncated response stored in memory for page 1
  - Streaming cursor `█` appended during stream, stripped on `{type:"end"}`
  - Delta batching: use a **100ms debounce timer** that flushes accumulated deltas, not a fixed count (review C.5 — count-based batching causes variable latency; timer gives consistent 100ms update frequency regardless of delta arrival rate)

### P3.8 — Wiring OpenClaw Flow into App

- **Description:** Update `main.ts` to handle the full OpenClaw response flow. Wire thinking display on `status:thinking`, handle streaming transition with delta buffering, apply markdown stripping to each delta, handle `end` frame to finalize display.
- **Owner:** g2-development
- **Dependencies:** P3.4, P3.6, P3.7, P2.7 (main.ts with audio) _(P3.5 moved to Phase 1 as P1.2.5)_
- **Complexity:** M
- **Files created/modified:**
  - `src/g2_app/src/main.ts` (modify — integrate OpenClaw response flow)
- **Acceptance criteria:**
  - `status:thinking` + `transcription` → `display.showThinking(transcription)`
  - First `assistant` delta → trigger thinking-to-streaming transition with delta buffering
  - Each `assistant` delta → `stripMarkdown(delta)` → `display.appendDelta()`
  - `{type:"end"}` → `stateMachine.transition('displaying')` → `display.showResponse()` (strip cursor, update badge/footer)
  - Displaying state: tap dismisses, double-tap flips page
  - Error during streaming → cleanup and show error
  - Agent timeout error → display appropriate message

### P3.9 — End-to-End OpenClaw Verification

- **Description:** Full pipeline test with real OpenClaw instance. Voice input → transcription → AI response on glasses. Test with various query types and response lengths.
- **Owner:** both
- **Dependencies:** P3.3, P3.8 (all Phase 3 tasks)
- **Complexity:** L
- **Files created:**
  - `tests/integration/test_openclaw_e2e.py`
- **Acceptance criteria:**
  - Full voice flow: tap → record → tap → transcribe → OpenClaw responds → streamed response appears on glasses
  - Text flow: send `{type:"text", message:"What is 2+2?"}` → response streams correctly
  - Long response: verify truncation at ~1800 chars with "double-tap for more" hint
  - Markdown response: `**bold**` text renders as `bold` (no raw markdown on display)
  - Multiple sequential queries: first response → dismiss → new recording → new response
  - OpenClaw not running: `OPENCLAW_ERROR: connection refused` displayed, auto-retry on next request
  - OpenClaw slow response: verify timeout at 120s
  - Gateway logs show: transcription time, OpenClaw response time, total latency
  - Latency measured: time from `stop_audio` to first displayed delta < 10s on CPU

## Parallel Execution Plan

```
── Time →

Agent A (backend-python):
  [P3.2 config OpenClaw] ──→ [P3.1 openclaw_client.py] ──────────────→ [P3.3 server.py OpenClaw wiring] ──→ [P3.9 e2e]
        ~0.5h                          ~3h                                        ~2.5h                        ~1.5h

Agent B (g2-development):
  [P3.8 main.ts OpenClaw flow wiring] ─────────────────────────────────────────────────────────────────→ [P3.9 e2e]
        ~2.5h  (blocked until Agent C completes P3.4+P3.6+P3.7)                                              ~1.5h

Agent C (g2-development):
  [P3.4 thinking display] ──┐
        ~1h                  ├──→ [P3.7 response truncation] ──→ (feeds into P3.8)
  [P3.6 delta buffering]  ──┘           ~1.5h
        ~1.5h
```

**Parallelization notes:**
- P3.1 (OpenClaw client), P3.4 (thinking display), and P3.6 (delta buffering) all start immediately in parallel _(P3.5 moved to Phase 1)_
- P3.2 is a quick config change that unblocks P3.1
- P3.6 (delta buffering) starts in parallel with P3.4 — no dependency between them (review B.2: the buffer is independent of the thinking layout)
- P3.7 (truncation) depends on P3.4 + P3.6 (builds on both display and buffering infrastructure)
- P3.3 (server wiring) depends on P3.1 (needs OpenClaw client)
- P3.8 (app wiring) depends on P3.4 + P3.6 + P3.7 (all display features; P3.5 already in Phase 1)
- P3.9 needs everything — joint verification with real OpenClaw instance

## Integration Checkpoint

Before moving to Phase 4, verify:

1. **OpenClaw client connects and authenticates** — test with real OpenClaw instance on localhost:18789
2. **Lifecycle event parsing** — confirm `phase` field (not `status`) terminates the stream correctly
3. **Full voice-to-response pipeline** — speak → transcribe → AI response → display, end-to-end
4. **Streaming display** — deltas appear smoothly, no gaps from layout transition buffering
5. **Markdown stripped** — common markdown patterns cleaned from display text
6. **Long responses truncated** — > 1800 chars shows truncation hint, page 1 has full text available
7. **Text input path** — `{type:"text"}` skips transcription, goes directly to OpenClaw
8. **Error recovery** — OpenClaw errors surface as readable error frames, Gateway returns to idle
9. **Sequential queries** — dismiss response → new recording → new response works cleanly
10. **Agent timeout** — 120s timeout fires and recovers correctly

## Definition of Done

- [x] `uv run pytest tests/gateway/` — all tests pass including OpenClaw client and server integration tests
- [x] OpenClaw client connects, authenticates, and streams deltas from a real OpenClaw instance
- [ ] Full voice pipeline: tap → record → tap → transcribe → AI thinking → streamed response → display *(needs real hardware/simulator)*
- [x] Text input: `{type:"text"}` → AI response displayed
- [x] Markdown stripped from displayed responses
- [x] Long responses (> 1800 chars) truncated with hint on page 0
- [x] Delta buffering works during thinking→streaming layout transition (no lost deltas)
- [x] Agent timeout (120s) triggers error and recovery
- [x] OpenClaw connection refused → error displayed, retry on next request
- [x] Multiple sequential interactions work without state leaks
- [ ] Gateway logs show latency: transcription time, OpenClaw time, first-delta time *(Phase 4 P4.11)*
