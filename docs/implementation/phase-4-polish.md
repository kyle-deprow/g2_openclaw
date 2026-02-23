# Phase 4: Polish & Edge Cases

## Phase Goal

Production-ready system with comprehensive error handling, reconnection logic, heartbeat keep-alive, all display states, page 1 detail view, graceful shutdown, configuration documentation, build packaging, and a quick-start guide.

## Prerequisites

- Phase 3 complete: full voice-to-AI-response pipeline working end-to-end
- All happy-path flows verified: voice input, text input, streaming display, sequential queries
- OpenClaw running with a configured agent
- Real G2 hardware available for final testing (simulator used for development)

## Task Breakdown

### P4.1 — Ping/Pong Heartbeat (Gateway)

- **Description:** Implement application-level heartbeat per review §3.1 and [architecture.md](../design/architecture.md) Appendix A. Gateway sends `{type:"ping"}` every 30 seconds. Phone must respond with `{type:"pong"}` within 10 seconds or connection is considered dead. This catches silent half-close scenarios (phone walks out of WiFi range, router drops idle TCP).
- **Owner:** backend-python
- **Dependencies:** Phase 3 server.py
- **Complexity:** S
- **Files created/modified:**
  - `gateway/server.py` (modify — add ping/pong loop)
  - `tests/gateway/test_heartbeat.py`
- **Acceptance criteria:**
  - Background asyncio task sends `{type:"ping"}` every 30 seconds
  - Tracks last pong received timestamp
  - If no pong within 10s of last ping: close WebSocket, clean up session, log "phone heartbeat timeout"
  - Pong handling already exists from Phase 1 (protocol.py parses pong frames)
  - Ping task starts after `connected` frame sent, stops on disconnect
  - Does NOT send pings during `loading` state
  - Test: mock client that responds/doesn't respond to pings

### P4.2 — Ping/Pong Heartbeat (App)

- **Description:** Ensure app responds to `{type:"ping"}` frames from Gateway with `{type:"pong"}` immediately. Already partially implemented in Phase 1 `gateway.ts` — verify and harden. No state change occurs on ping/pong.
- **Owner:** g2-development
- **Dependencies:** P1.7 (gateway.ts)
- **Complexity:** S
- **Files created/modified:**
  - `g2_app/src/gateway.ts` (verify/modify — ensure pong response is reliable)
- **Acceptance criteria:**
  - On receiving `{type:"ping"}` frame: immediately send `{type:"pong"}` regardless of app state
  - Pong sent even during recording, streaming, or any other state
  - No display or state machine changes on ping/pong
  - Test: Gateway sends pings on schedule, app responds consistently

### P4.3 — Gateway → OpenClaw Reconnection

- **Description:** Harden the OpenClaw client reconnection per [gateway.md](../design/gateway.md) §5.7. If OpenClaw disconnects mid-stream, the current request fails with an error frame but subsequent requests trigger automatic reconnection. Exponential backoff with jitter.
- **Owner:** backend-python
- **Dependencies:** P3.1 (openclaw_client.py)
- **Complexity:** M
- **Files created/modified:**
  - `gateway/openclaw_client.py` (modify — harden reconnection)
  - `tests/gateway/test_openclaw_reconnect.py`
- **Acceptance criteria:**
  - On `ConnectionClosedError` during stream: clear connection state, raise `OpenClawError` for current request
  - On next `send_message()` call: `ensure_connected()` attempts reconnect with backoff
  - Backoff: 1s → 2s → 4s → 8s → max 30s, ±20% jitter per [gateway.md](../design/gateway.md) §5.7
  - Resets backoff on successful connection
  - Re-authenticates on each new connection (request ID counter also resets)
  - Max attempts: unlimited (keep trying as long as phone is connected)
  - Test: simulate OpenClaw disconnect, verify reconnection on next request

### P4.4 — Phone → Gateway Reconnection Hardening

- **Description:** Harden the app-side reconnection per [g2-app.md](../design/g2-app.md) §5. On disconnect, display "Connecting to Gateway..." with countdown. On reconnect, restore to idle state. Handle edge cases: disconnect during recording, disconnect during streaming.
- **Owner:** g2-development
- **Dependencies:** P1.7 (gateway.ts), P2.5 (input.ts)
- **Complexity:** M
- **Files created/modified:**
  - `g2_app/src/gateway.ts` (modify)
  - `g2_app/src/main.ts` (modify — reconnection state handling)
- **Acceptance criteria:**
  - On WebSocket close: transition to `disconnected` state, display disconnected layout
  - Backoff timer countdown shown in footer: "Retry in {n}s..." per [display-layouts.md](../design/display-layouts.md) §4.8
  - Countdown updates every second via `textContainerUpgrade`
  - Tap during disconnected: force immediate reconnect attempt
  - On reconnect success: send no auth first-frame (token in query param), receive `connected` frame, transition to idle
  - Disconnect during recording: stop mic, clean up audio state
  - Disconnect during streaming: discard partial response, clean up
  - `FOREGROUND_ENTER_EVENT`: trigger reconnect if disconnected

### P4.5 — Remaining Display States: Error + Disconnected + Loading (consolidated from P4.5/P4.6/P4.7 per review C.1)

- **Description:** Implement 3 remaining display layouts in one pass: error ([display-layouts.md](../design/display-layouts.md) §4.7), disconnected ([display-layouts.md](../design/display-layouts.md) §4.8), loading ([display-layouts.md](../design/display-layouts.md) §4.9). Each is ~15 lines of container definitions. Consolidating saves 2 agent calls of overhead.
- **Owner:** g2-development
- **Dependencies:** P1.8 (display.ts)
- **Complexity:** M
- **Files created/modified:**
  - `g2_app/src/display.ts` (modify — add `showError()`, `showDisconnected()`, `showLoading()`, `updateRetryCountdown()`)
- **Acceptance criteria:**
  - **Error** — `showError(code: string, detail: string)` — 4 containers matching [display-layouts.md](../design/display-layouts.md) §4.7:
    - Title: `"OpenClaw"`, Badge: `"✕ Error"` (0xF), Content: `"Error\n\n{message}\n\n{hint}"` (scrollable), Footer: `"Tap to dismiss"` (0xA)
    - Error code → user-friendly message + recovery hint mapping for all 8 error codes (TRANSCRIPTION_FAILED, BUFFER_OVERFLOW, OPENCLAW_ERROR, TIMEOUT, INVALID_FRAME, INVALID_STATE, AUTH_FAILED, INTERNAL_ERROR)
    - Tap to dismiss → idle; error clears any in-progress audio
  - **Disconnected** — `showDisconnected(retryIn: number)` — 4 containers matching [display-layouts.md](../design/display-layouts.md) §4.8:
    - Badge: `"○ Offline"` (0x6 muted), Content: WiFi troubleshooting hints, Footer: `"Retry in {n}s..."` (0x6)
    - `updateRetryCountdown(n)` — `textContainerUpgrade` on footer each second
    - Tap → immediate reconnect
  - **Loading** — `showLoading()` — 4 containers matching [display-layouts.md](../design/display-layouts.md) §4.9:
    - Badge: `"● loading"` (0xC accent), Content: `"Starting up..."` (24px, 0xA), Footer: `"Initializing speech model"` (0x6)
    - Ring taps ignored (already handled by input.ts)
    - Auto-transitions to idle on `status:idle`
  - Calls `bridge.shutDownPageContainer(0)` on `ABNORMAL_EXIT_EVENT` for graceful display cleanup (review A.7)
  - **SDK note (Phase 0 spike):** the SDK method is actually `ShutDownContaniner` (sic — typo in SDK), and the rebuild failure event is `APP_REQUEST_REBUILD_PAGE_FAILD` (sic). Use these exact misspelled names.
  - Optional: subscribe to `onDeviceStatusChanged` for battery level and wearing-state logging (review A.8) — not required for MVP but useful for diagnostics

### P4.8 — Page 1 Detail View (app-layer page management)

- **Description:** Implement page 1 layout per [display-layouts.md](../design/display-layouts.md) §5. Shows full response text, metadata (model, elapsed time, session key). Accessible via double-tap from displaying state. Return to page 0 via double-tap. **NOTE:** `setPageFlip()` does NOT exist in the SDK (Phase 0 spike finding). Page navigation is implemented via app-layer state + full container rebuild.
- **Owner:** g2-development
- **Dependencies:** P1.8 (display.ts), P3.7 (truncation stores full response)
- **Complexity:** M
- **Files created/modified:**
  - `g2_app/src/display.ts` (modify — add `currentPage` state, `rebuildPageContainer()`, `showPage0()`, `showPage1()`)
- **Acceptance criteria:**
  - Maintain `currentPage` variable (0 or 1) in module state
  - Page 0 = summary (truncated response) — existing `showResponse()` layout
  - Page 1 layout: 3 containers per [display-layouts.md](../design/display-layouts.md) §5:
    - `p1_header`: `"Response Details         [Page 2]"` (24px, 0xF)
    - `p1_content`: `"{response}\n\n━━━━━━━━━━━━━━━\nModel: {model}\nTime: {elapsed}s\nSession: {session}"` (18px, 0xF, scrollable)
    - `p1_footer`: `"Double-tap: back · Tap: dismiss"` (18px, 0x6)
  - `showPage1()` — calls `bridge.buildPageContainer(0, page1Containers)` to fully rebuild display with page 1 content (no native page flip)
  - `showPage0()` — calls `bridge.buildPageContainer(0, page0Containers)` to rebuild display with original summary content
  - Double-tap (`DOUBLE_CLICK_EVENT`) in displaying state: toggle `currentPage`, call `showPage1()` or `showPage0()` accordingly
  - Double-tap on page 1 → `showPage0()` (rebuild with summary)
  - Tap on page 1 → dismiss → idle
  - Page 1 content populated during `showResponse()` using stored full response text
  - When response was truncated on page 0, page 1 shows full untruncated text
  - Page navigation triggers a full container rebuild, not a native page flip
  - Metadata includes elapsed time (time from thinking to end) and session key (`agent:claw:g2`)

### P4.9 — Graceful Shutdown (Gateway)

- **Description:** Handle Ctrl+C and SIGTERM gracefully per review §7.5. Drain active connections, send a close frame to phone, cancel pending tasks.
- **Owner:** backend-python
- **Dependencies:** Phase 3 server.py
- **Complexity:** S
- **Files created/modified:**
  - `gateway/server.py` (modify — add signal handlers)
- **Acceptance criteria:**
  - Register signal handlers for SIGINT and SIGTERM
  - On shutdown signal: log "Shutting down..."
  - If phone connected: send `{type:"error", code:"INTERNAL_ERROR", detail:"Gateway shutting down"}`, close WebSocket
  - Cancel pending asyncio tasks (Whisper inference, OpenClaw stream)
  - Close OpenClaw client connection
  - Exit cleanly with code 0
  - Test: start Gateway, connect client, send SIGINT, verify clean disconnect and exit

### P4.10 — Complete Configuration & .env Documentation

- **Description:** Finalize all configuration options with full documentation. Create `.env.example` with all variables documented. Validate all config at startup with clear error messages.
- **Owner:** backend-python
- **Dependencies:** P3.2 (config with OpenClaw fields)
- **Complexity:** S
- **Files created/modified:**
  - `gateway/config.py` (modify — add LOG_LEVEL, final validation)
  - `gateway/.env.example` (update)
- **Acceptance criteria:**
  - All 11 env vars from [gateway.md](../design/gateway.md) §6 supported: `GATEWAY_HOST`, `GATEWAY_PORT`, `GATEWAY_TOKEN`, `OPENCLAW_HOST`, `OPENCLAW_PORT`, `OPENCLAW_GATEWAY_TOKEN`, `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `AGENT_TIMEOUT`, `LOG_LEVEL`
  - `load_config()` validates all values (port ranges, valid model names, valid device/compute types)
  - Missing required config → clear error message with env var name
  - `.env.example` has all vars with descriptions and example values
  - Config logged at startup with tokens redacted (first 4 chars + `***`)

### P4.11 — Structured Logging (Gateway)

- **Description:** Set up structured logging with configurable level. Log key events: connection, disconnection, state transitions, transcription timing, OpenClaw timing, errors. Per review §7.4.
- **Owner:** backend-python
- **Dependencies:** P4.10 (LOG_LEVEL config)
- **Complexity:** S
- **Files created/modified:**
  - `gateway/server.py` (modify — add structured logging throughout)
  - `gateway/openclaw_client.py` (modify)
  - `gateway/transcriber.py` (modify)
- **Acceptance criteria:**
  - Python `logging` configured with `LOG_LEVEL` from config
  - Log format: `%(asctime)s %(levelname)-5s %(message)s`
  - Key events logged:
    - INFO: startup, config, phone connect/disconnect, state transitions, transcription result + timing, OpenClaw connect, first delta, end
    - DEBUG: individual frame types, PCM chunk sizes, delta text, buffer sizes
    - WARNING: unknown frame types, state mismatches, reconnection attempts
    - ERROR: all error scenarios with full context
  - Timing: log Whisper inference time, OpenClaw first-delta time, total request time

### P4.12 — Build & Packaging (G2 App)

- **Description:** Set up the full build and packaging pipeline per [g2-app.md](../design/g2-app.md) §9. Produce a working `.ehpk` file for sideloading via QR code.
- **Owner:** g2-development
- **Dependencies:** Phase 3 app complete
- **Complexity:** M
- **Files created/modified:**
  - `g2_app/assets/icon.bmp` (create — placeholder 4-bit greyscale BMP)
  - `g2_app/package.json` (modify — add build scripts)
  - `g2_app/README.md` (create)
- **Acceptance criteria:**
  - `npm run build` produces `dist/` with bundled app
  - `app.json` and `assets/` copied to `dist/`
  - `app.json` validated against **g2-dev-toolchain skill schema** (NOT design [g2-app.md](../design/g2-app.md) §9 which has wrong field names — review E.2):
    - Required fields: `package_id`, `name`, `entrypoint`, `edition`, `min_app_version`, `tagline`, `description`, `author`, `permissions`
    - `package_id` must match `^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$` (e.g. `com.openclaw.g2`)
    - Correct field names: `package_id` (not `appId`), `name` (not `appName`), `entrypoint` (not `entry`)
  - `evenhub pack app.json dist -o openclaw.ehpk` produces `.ehpk` file (correct syntax per g2-dev-toolchain skill)
  - `evenhub qr --url "http://<LAN_IP>:5173"` generates working QR code for dev workflow
  - `icon.bmp` is a valid 4-bit greyscale BMP (can be a simple "OC" text icon)
  - Package scripts in `package.json`: `build`, `dev`, `pack`, `qr`
  - README documents: prerequisites, dev workflow, build steps, QR sideloading

### P4.13 — Gateway README & Quick-Start Guide

- **Description:** Write comprehensive documentation for the Gateway: installation, configuration, running, troubleshooting. Per review §7.3 and [gateway.md](../design/gateway.md) module architecture.
- **Owner:** backend-python
- **Dependencies:** P4.10 (config finalized)
- **Complexity:** S
- **Files created/modified:**
  - `gateway/README.md` (create or update)
- **Acceptance criteria:**
  - Prerequisites: Python 3.12+, uv, OpenClaw running on localhost:18789
  - Install: `cd gateway && uv sync`
  - Configure: copy `.env.example` → `.env`, set `OPENCLAW_GATEWAY_TOKEN`
  - Run: `uv run python -m gateway.server`
  - Troubleshooting: common errors (missing token, port in use, OpenClaw not running, Whisper model download)
  - Architecture: brief description of modules and data flow
  - Configuration reference: all env vars with defaults and descriptions

### P4.14 — Root README & Quick-Start

- **Description:** Write a project-level README that explains the full system, how to set up both components, and how to do a first voice interaction.
- **Owner:** both
- **Dependencies:** P4.12, P4.13
- **Complexity:** S
- **Files created:**
  - `README.md` or `src/README.md`
- **Acceptance criteria:**
  - System overview with architecture diagram
  - Prerequisites checklist (hardware, software, network)
  - Quick-start: step-by-step from clone to first voice query
  - Component README links
  - Known limitations / FAQ

## Parallel Execution Plan

```
── Time →

Agent A (backend-python):
  [P4.1 ping/pong GW] → [P4.3 OpenClaw reconnect] → [P4.9 graceful shutdown] → [P4.10 config] → [P4.11 logging] → [P4.13 README]
        ~1h                     ~2h                         ~1h                      ~1h              ~1.5h              ~1h

Agent B (g2-development):
  [P4.2 ping/pong app] → [P4.4 reconnect harden] → [P4.5 error+disconnected+loading] → [P4.8 page 1 detail] → [P4.12 build/pack] → [P4.14 README]
        ~0.5h                   ~2h                            ~2h                            ~2h                    ~1.5h              ~1h
```

**Parallelization notes (updated per review C.1):**
- P4.5 now consolidates error + disconnected + loading display into one task (saves 2 agent calls)
- P4.1 (GW ping) and P4.2 (app ping) run in parallel — two halves of one feature
- P4.3 (OpenClaw reconnect) can run alongside P4.4 (app reconnect) and P4.5 (error states)
- ~~P4.6/P4.7 removed~~ — consolidated into P4.5 (see above)
- P4.8 (page 1) can start once P4.5 is done (needs the full display module stable)
- P4.9 (graceful shutdown), P4.10 (config), P4.11 (logging) are sequential on Agent A post-reconnection work
- P4.12 (build) and P4.13/P4.14 (READMEs) are the final polish tasks, parallelizable across agents

## Integration Checkpoint

Final system verification before release:

1. **Full voice pipeline** — tap → record → tap → transcribe → AI response → display → dismiss → repeat
2. **Text input** — alternative path skipping audio
3. **Error recovery** — each of the 13 error scenarios from [gateway.md](../design/gateway.md) §7.2 triggers correct display and recovers to idle
4. **Reconnection (phone→GW)** — kill Gateway, app shows "Connecting...", restart Gateway, app recovers to idle
5. **Reconnection (GW→OpenClaw)** — restart OpenClaw, next query triggers reconnect, response arrives
6. **Heartbeat** — Gateway detects zombie connection (phone killed without clean close) within 40s
7. **Loading state** — fresh Gateway start shows "Starting up..." → "Ready" after model loads
8. **Long response** — 2000+ char response truncated on page 0, full text on page 1
9. **Page 1 detail** — double-tap shows full response + metadata, double-tap returns
10. **Graceful shutdown** — Ctrl+C sends error frame to phone, clean exit
11. **Sequential queries** — 5+ back-to-back voice queries with no state leaks
12. **Real hardware** — test on actual G2 glasses + R1 ring (not just simulator)
13. **Build** — `evenhub pack` produces valid `.ehpk`, QR sideloading works
14. **BLE bandwidth** — verify display updates respect 100ms debounce timer to avoid saturating BLE link (review D.3)

## Definition of Done

- [ ] All unit tests pass: `uv run pytest tests/gateway/`, `npx tsc --noEmit`, and `npx vitest run` (review F.1 — vitest set up in Phase 1)
- [ ] All 13 error scenarios ([gateway.md](../design/gateway.md) §7.2) handled with correct error frames and user-friendly display
- [ ] Ping/pong heartbeat detects dead connections within 40 seconds
- [ ] Phone auto-reconnects after Gateway restart (max time: 30s)
- [ ] Gateway reconnects to OpenClaw after OpenClaw restart (transparent to user)
- [ ] Loading state displayed during Whisper initialization
- [ ] Disconnected state with countdown and tap-to-retry
- [ ] Error state with contextual recovery hints
- [ ] Page 1 detail view: full response + metadata, accessible via double-tap
- [ ] Response truncation at ~1800 chars with page 1 overflow
- [ ] Graceful shutdown: clean disconnect + error frame to phone
- [ ] Structured logging with configurable level
- [ ] `.env.example` with all 11 config variables documented
- [ ] Gateway README with install, config, run, troubleshoot
- [ ] G2 app README with dev workflow, build, pack, QR sideloading
- [ ] Root README with system overview and quick-start
- [ ] `.ehpk` package builds successfully
- [ ] Full end-to-end test on EvenHub simulator passes
- [ ] (Stretch) Full end-to-end test on real G2 hardware passes
- [ ] CI workflow (`.github/workflows/ci.yml`) passes on push/PR (review F.2)
- [ ] Display updates enforce 100ms debounce to respect BLE bandwidth (review D.3)
### P4.15 — CI Workflow (review F.2)

- **Description:** Create a GitHub Actions workflow that runs on push/PR: Gateway Python tests, TypeScript type-check, TypeScript tests, Vite build.
- **Owner:** both
- **Dependencies:** P4.14
- **Complexity:** S
- **Files created:**
  - `.github/workflows/ci.yml`
- **Acceptance criteria:**
  - Runs `uv run pytest tests/gateway/` for Gateway
  - Runs `npx tsc --noEmit` and `npx vitest run` for app
  - Runs `npx vite build` for bundle verification
  - Fails on any test/type/build failure