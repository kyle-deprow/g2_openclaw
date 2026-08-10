---
name: g2-sim-automation
description: Autonomous control of the G2 OpenClaw simulator via the local Dev API on port 5173. Use when starting the sim stack, sending messages to OpenClaw, checking autoresearch status, reading the glasses display, injecting ring taps, or running end-to-end tests through the G2 app.
---

# G2 Simulator Automation

Drive the G2 app inside the EvenHub simulator through HTTP endpoints served by the loopback-only Vite dev server; the plugin in `g2_app/dev-api.ts` bridges HTTP calls to `window.__g2Api` in the simulator webview.

**Canonical reference:** `.agents/skills/g2-sim-automation/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

Use this skill for OpenClaw product state, exact display text, gateway integration, and product journeys. Use `g2-simulator-automation` for simulator `0.8.0`'s native framebuffer/WebView screenshots, console logs, and glasses input API.

## Core rules

- **Start the stack with `make sim`** (gateway on port 8765 + Vite on 127.0.0.1:5173 + simulator); stop with `make stop`. Manual alternative: run `uv run python -m gateway` in the background, `cd g2_app && npm run dev:sim`, then `evenhub-simulator --no-aid http://localhost:5173`.
- **Wait ~5 s after the simulator starts** before issuing Dev API calls; after HMR or restart the webview reloads and state returns to `idle`.
- **Never use broad `pkill` patterns** — use `make stop` then `make sim` for a managed restart scoped to G2 OpenClaw processes.
- **The Dev API is local-only and agent-only.** Base URL `http://localhost:5173`. It accepts a supplied `Origin` only if exactly `http://127.0.0.1:5173` or `http://localhost:5173`; origin-less same-host curl works. Never expose it to a phone, G2 device, LAN host, or remote browser.
- **`npm run dev` and `npm run dev:network` deliberately have NO automation API** — no injected input, telemetry, or session controls. Only `npm run dev:sim` (or `make sim`) serves the Dev API.
- **Endpoints:** `GET /_dev/health` → `{"ok":true}`; `GET /_dev/state`; `GET /_dev/display` (exact glasses text); `GET /_dev/conversation` (JSON array); `POST /_dev/cmd` (fire-and-forget id); `GET /_dev/result/:id` (poll result, blocks up to 30 s).
- **Command payload shape:** `{"cmd": "<name>", "args": [...]}` POSTed to `/_dev/cmd`.
- **Commands:** `sendText ["message"]` (bypasses Whisper), `tap`, `doubleTap` (no-op in idle; dismisses error), `getState`, `getDisplayText`, `getConversation`, `getGatewayConnected`, `getAutoresearchStatus`, `startRecording`, `stopRecording ["hilText"]`, `confirmTranscription`, `rejectTranscription`, `cancelResponse`, `resetSession`, `getSessionId`, `getPendingTranscription`.
- **The app boots straight into `idle`** — the single autoresearch thread view. `sendText` works immediately; there is no session menu. `sendText` returns `false` outside `idle`.
- **State machine:** `LOADING → IDLE → RECORDING → TRANSCRIBING → CONFIRMING → THINKING → STREAMING → IDLE`. After `sendText`, state cycles idle → thinking → streaming → idle automatically (allow ~8 s for a response).
- **`getAutoresearchStatus`** returns the latest `autoresearch_status` frame (phase, iteration, running, supervisor outcome) or null before the first push.
- **Display text is reverse chronological** — newest at top; user messages prefixed `»`, system messages in `[brackets]`.
- **Prefer `/_dev/display` over screenshots** — it returns exact text with no OCR noise. For pixels, use the simulator's native screenshot button (v0.5.0+, RGBA PNG to CWD) or `gnome-screenshot -f /tmp/sim.png && tesseract /tmp/sim.png -` on Wayland.
- **After error/disconnected state** (e.g. OpenClaw daemon restart): run `make sim` to restart the full stack; the app reconnects straight to idle.
- **Troubleshooting:** `{"error":"app not ready"}` → webview not loaded, wait 5 s or restart the simulator; stuck in `loading` → gateway not connected, check `ss -tlnp | grep 8765`; timeouts on convenience endpoints → browser polling script not running, restart the simulator; opaque simulator parse errors → set `RUST_LOG=debug` in the simulator env.

## This repo

- **`g2_app/dev-api.ts`** — the Vite plugin implementing the Dev API bridge to `window.__g2Api`.
- **`g2_app/src/api.ts`** — in-app command surface backing the Dev API.
- **`g2_app/src/state.ts`** — the state machine the `/_dev/state` endpoint reports.
- **`Makefile`** — `sim`, `restart`, `stop`, `launch` targets; `gateway/` — the Python WebSocket server on port 8765 (`uv run python -m gateway ...`).
- **`docs/reference/g2-platform/evenhub_simulator.md`** — full simulator reference.
