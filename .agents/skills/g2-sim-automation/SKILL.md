---
name: g2-sim-automation
description:
  Autonomous control of the G2 OpenClaw simulator via the Dev API. Use when
  an agent needs to start the sim stack, send messages, navigate menus, read
  the glasses display, inject ring commands, or take screenshots. Triggers on
  tasks involving simulator interaction, dev API calls, or end-to-end testing
  through the G2 app.
---

# G2 Simulator Automation

Control the G2 app running inside the EvenHub simulator through HTTP endpoints
served by the loopback-only Vite simulator server on **port 5173**. The API plugin
(`g2_app/dev-api.ts`) bridges external HTTP calls to `window.__g2Api` inside the
simulator's webview. This control API is for the user or Codex only. It is not a
phone/G2 feature and must never be exposed to another machine.

## Starting the Stack

```bash
# Option A: all-in-one (gateway + vite + simulator)
make sim

# Option B: manual (gives you individual control)
> logs/gateway.log && uv run python -m gateway 2>>logs/gateway.log &  # port 8765
cd g2_app && npm run dev:sim 2>/dev/null &                  # 127.0.0.1:5173 only
sleep 3 && evenhub-simulator --no-aid http://localhost:5173 &
```

Wait ~5 s after starting the simulator before issuing Dev API calls.

Do not use broad `pkill` patterns. If a managed restart is required, use
`make stop` followed by `make sim`; this scopes cleanup to G2 OpenClaw
processes. `npm run dev` and `npm run dev:network` intentionally have no
automation API or injected input, telemetry, or session controls.

## Dev API Endpoints

Base URL: `http://localhost:5173`

The API accepts a supplied `Origin` only when it is exactly
`http://127.0.0.1:5173` or `http://localhost:5173` (using the active Vite port).
Origin-less same-host curl/Codex requests remain supported. Never send the API
to a phone, G2 device, LAN host, or remote browser.

| Endpoint              | Method | Purpose                                  |
|-----------------------|--------|------------------------------------------|
| `/_dev/health`        | GET    | Liveness check → `{"ok":true}`           |
| `/_dev/state`         | GET    | Current state machine state              |
| `/_dev/display`       | GET    | Exact text rendered on the G2 glasses    |
| `/_dev/conversation`  | GET    | Full conversation history (JSON array)   |
| `/_dev/cmd`           | POST   | Execute any command (fire-and-forget id) |
| `/_dev/result/:id`    | GET    | Poll for a command's result              |

### Available Commands (POST to `/_dev/cmd`)

```json
{"cmd": "<name>", "args": [<arg1>, ...]}
```

| Command                | Args              | Description                        |
|------------------------|-------------------|------------------------------------|
| `sendText`             | `["message"]`     | Send text to OpenClaw (bypasses Whisper) |
| `tap`                  | `[]`              | Simulate ring tap                  |
| `doubleTap`            | `[]`              | Open session menu                  |
| `selectSession`        | `[index]`         | Pick session from menu (0-based). **Index 0 = "New Session"; existing sessions start at index 1.** |
| `getState`             | `[]`              | Returns state string               |
| `getDisplayText`       | `[]`              | Returns glasses display text       |
| `getConversation`      | `[]`              | Returns conversation entries       |
| `getGatewayConnected`  | `[]`              | Returns boolean                    |
| `startRecording`       | `[]`              | Begin audio capture                |
| `stopRecording`        | `["hilText"]`     | Stop capture (optional HIL text)   |
| `confirmTranscription` | `[]`              | Accept pending transcription       |
| `rejectTranscription`  | `[]`              | Reject pending transcription       |
| `openSessionMenu`      | `[]`              | Open menu from idle                |
| `closeSessionMenu`     | `[]`              | Dismiss menu                       |
| `cancelResponse`       | `[]`              | Cancel in-flight response          |
| `resetSession`         | `[]`              | Reset the current OpenClaw session |
| `getSessionId`         | `[]`              | Get active session ID from localStorage |
| `getPendingTranscription` | `[]`           | Get text awaiting confirmation     |

Convenience endpoints and `GET /_dev/result/:id` block up to 30 s waiting for the browser to execute the command.

## Typical Agent Workflow

### 1. Verify the stack is alive

```bash
curl -s --max-time 5 http://localhost:5173/_dev/health
# → {"ok":true}
```

### 2. Boot → select a session

App boots into **menu** state. Index 0 is always "✦ New Session"; existing sessions start at index 1.

```bash
curl -s --max-time 10 http://localhost:5173/_dev/state
# → {"id":"...","result":"menu","ts":...}

# Select first EXISTING session (index 1). Use index 0 to create a new session.
curl -s -X POST http://localhost:5173/_dev/cmd \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"selectSession","args":[1]}'
sleep 3

curl -s --max-time 10 http://localhost:5173/_dev/state
# → {"id":"...","result":"idle","ts":...}
```

### 3. Send a message and read the response

```bash
curl -s -X POST http://localhost:5173/_dev/cmd \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"sendText","args":["What is 2+2?"]}'
sleep 8   # wait for OpenClaw to respond

curl -s --max-time 10 http://localhost:5173/_dev/display
# → {"result":"4\n─ ─ ─ ─ ─ ─ ─ ─\n» What is 2+2?","ts":...}
```

Display is **reverse chronological** — newest at top. User `»`, system `[brackets]`.

### 4. Check structured conversation

```bash
curl -s --max-time 10 http://localhost:5173/_dev/conversation
# → {"result":[{"role":"user","text":"...","timestamp":...},
#              {"role":"assistant","text":"...","timestamp":...}]}
```

### 5. Screenshots

**Native simulator screenshot (v0.5.0+):** The simulator supports built-in screenshot export — click the screenshot button in the simulator window. Saves an RGBA PNG to the current working directory with a timestamp filename. Path is logged to simulator stdout (warn level). No OS-level tools needed.

**OS-level alternative (Wayland):**

```bash
gnome-screenshot -f /tmp/sim.png && tesseract /tmp/sim.png - 2>/dev/null
```

Prefer `/_dev/display` over screenshots — returns exact text without OCR noise.

## State Machine

```
LOADING → MENU → IDLE → RECORDING → TRANSCRIBING → CONFIRMING → THINKING → STREAMING → IDLE
```

- **menu**: Boot default. Must `selectSession` to reach idle.
- **idle**: Ready. `tap` starts recording, `doubleTap` opens menu.
- **After sendText**: State cycles idle → thinking → streaming → idle automatically.
- **After HMR/restart**: Simulator webview reloads. Wait 5 s, state returns to menu.
- **After error/disconnected** (e.g. daemon restart): Run `make sim` to kill and restart the full stack, then re-select a session.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `{"error":"app not ready"}` | Simulator webview hasn't loaded yet. Wait 5 s or restart simulator. |
| State stuck in `menu` | Gateway not connected. Check `ss -tlnp \| grep 8765`. |
| `sendText` returns `false` | Not in `idle` state. Check `/_dev/state` first. |
| Timeout on convenience endpoints | Browser polling script not running. Restart simulator. |
| Simulator parse error, no payload detail | Set `RUST_LOG=debug` in the simulator env to log original JSON payloads on parse errors. |
