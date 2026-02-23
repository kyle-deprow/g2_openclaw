# G2 OpenClaw App — Thin Client Design

## 1. Overview

The G2 OpenClaw App is the thinnest possible bridge between the Even Realities G2 smart glasses and the PC Gateway. It runs inside a WKWebView (`flutter_inappwebview`) on an iPhone, packaged as an EvenHub `.ehpk` app. The app has no intelligence of its own — it captures microphone PCM from the glasses, forwards raw bytes over a single WebSocket to the PC Gateway, receives streamed JSON text responses, and renders them on the 576×288 4-bit greyscale display via the EvenAppBridge SDK (`rebuildPageContainer` / `textContainerUpgrade`). Physical input from the R1 ring and temple gestures drives navigation and mic control.

**The phone does NOT:**

- Talk to Whisper or transcribe audio
- Talk to OpenClaw or manage agent sessions
- Buffer, accumulate, or process audio in any way
- Perform AI/ML inference
- Manage authentication tokens for OpenClaw

All intelligence lives on the PC. The phone is a dumb pipe.

---

## 2. App Structure

```
g2_app/
├── app.json              ← EvenHub manifest (package_id, name, version, entrypoint, etc.)
├── index.html            ← Entry point loaded by WebView
├── src/
│   ├── main.ts           ← App entry: init bridge, connect WS, wire events
│   ├── gateway.ts        ← WebSocket client to PC Gateway (connect, send, receive)
│   ├── display.ts        ← Display manager: container layout, text rendering, page mgmt
│   ├── input.ts          ← Event handler: ring/gesture routing, mic control
│   ├── audio.ts          ← Mic capture: onMicData → forward to gateway
│   └── state.ts          ← Simple state machine (loading, idle, recording, transcribing, thinking, streaming, displaying)
├── tsconfig.json
├── package.json
└── vite.config.ts        ← Bundler (output single JS for WebView)
```

### Module Responsibilities

| Module | Single Responsibility | Key Exports |
|---|---|---|
| **main.ts** | Bootstrap: obtain bridge via `waitForEvenAppBridge()`, connect gateway, register event handlers, create startup page (checking `StartUpPageCreateResult`) | `init()` — async entry point |
| **gateway.ts** | Manage the single WebSocket connection to the PC Gateway. Send binary/JSON frames, receive JSON frames, handle reconnection. | `Gateway` class: `connect()`, `send(data)`, `sendJson(obj)`, `onMessage(cb)`, `disconnect()` |
| **display.ts** | Build and push `ContainerData` layouts to the glasses. Manage text content, page state, and container specs for each app state. Uses `rebuildPageContainer` for layout transitions and `textContainerUpgrade` for content updates. | `Display` class: `showIdle()`, `showRecording()`, `showTranscribing()`, `showThinking()`, `showStreaming(text)`, `showResponse(text)`, `showError(msg)`, `swapPage()` |
| **input.ts** | Route `onEvenHubEvent` callbacks to the correct action based on current app state and event type. Handle all SDK quirks. | `InputHandler` class: `setup(bridge)`, `onAction(cb)` |
| **audio.ts** | Open/close the G2 microphone via `bridge.audioControl()`. Forward each `onMicData` PCM chunk immediately to the gateway as a binary frame. | `AudioCapture` class: `start(bridge, gateway)`, `stop()` |
| **state.ts** | Track the app's display state. Transition between states in response to gateway frames and user input. Emit state change events. | `AppState` enum, `StateMachine` class: `transition(newState)`, `current()`, `onChange(cb)` |

### Data Flow Summary

```
┌──────────┐   BLE/PCM    ┌──────────────┐   binary WS    ┌────────────┐
│ G2       │──────────────►│ audio.ts     │───────────────►│ gateway.ts │──► PC Gateway
│ Glasses  │               │ (no buffer)  │                │            │
│          │◄──────────────│ display.ts   │◄───────────────│            │◄── PC Gateway
│          │ rebuildPage() │ (containers) │   JSON frames  │            │
└──────────┘               └──────────────┘                └────────────┘
                                 ▲
                                 │ state changes
                           ┌─────────────┐
                           │  state.ts   │
                           │  input.ts   │
                           └─────────────┘
```

---

## 3. Display Layout Design

The G2 display is 576×288 pixels, 4-bit greyscale (16 shades of green, `0x0`–`0xF`). Origin `(0,0)` is top-left. All positioning is absolute pixel coordinates. Maximum **4 containers per page**. Exactly **one container must have `isEventCapture: 1`**.

> **Pixel-level layout specifications** for all application states (idle, recording, transcribing, thinking, streaming, displaying, error, disconnected, loading) are defined in [display-layouts.md](display-layouts.md). This section describes the layout strategy; see [display-layouts.md](display-layouts.md) for exact coordinates, container IDs, font colors, and font sizes.

### 3.1 Delta Buffering During Layout Transitions

When transitioning from thinking to streaming, the app calls `rebuildPageContainer` to swap to the streaming layout. During the BLE round-trip (~50-100ms), incoming assistant deltas must be buffered in a local array. Once `rebuildPageContainer` resolves, flush all buffered deltas via a single `textContainerUpgrade` call, then resume normal per-delta updates.

### 3.2 Markdown Stripping

LLM responses contain markdown (`**bold**`, `` `code` ``, `[links](url)`, `# headings`). Apply a `stripMarkdown()` function to each assistant delta before appending to display text: strip bold/italic markers, convert links to plain text, remove heading markers, strip code fences.

### 3.3 Response Length Handling

The `textContainerUpgrade` method supports up to 2000 characters. If accumulated response text exceeds ~1800 characters, truncate the page 0 display with `"… [double-tap for more]"` and manage page state in the app layer — on double-tap, call `rebuildPageContainer` to swap between a page 0 (truncated) and page 1 (continuation) view.

> **Note:** There is no `setPageFlip` method in the SDK. Page management must be done manually by tracking which page is active and calling `rebuildPageContainer` with the appropriate containers.

> **SDK spelling quirks (use verbatim):** The SDK contains several misspelled identifiers that must be used exactly as-is in code:
> - `borderRdaius` (not `borderRadius`) in `ContainerData` properties
> - `ShutDownContaniner` (not `ShutDownContainer`) in shutdown-related enum values
> - `APP_REQUEST_REBUILD_PAGE_FAILD` (not `FAILED`) in `StartUpPageCreateResult` enum

---

## 4. State Machine (App-Side)

The thin client tracks its own display state locally, transitioning in response to Gateway status frames and user input.

### State Diagram

```
     ┌─────────────────────────────────────────────────────────────────────────┐
     │                                                                         │
     │  ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐       │
     │  │ loading  │────►│   idle   │────►│recording │────►│ transcr. │       │
     │  └──────────┘     └────┬─────┘     └──────────┘     └────┬─────┘       │
     │   Gateway sends        │ ▲              tap               │             │
     │   status:"loading"     │ │           (toggle)             │             │
     │                        │ │                                ▼             │
     │                        │ │         ┌──────────┐     ┌──────────┐       │
     │                        │ │         │displaying│     │ thinking │       │
     │                        │ │         └────┬─────┘     └────┬─────┘       │
     │                        │ │   user taps  │                │             │
     │                        │ └──────────────┘                ▼             │
     │                        │                            ┌──────────┐       │
     │                        │                            │streaming │       │
     │                        │                            └────┬─────┘       │
     │                        │              {type:"end"}       │             │
     │                        │              ─────────────►     │             │
     │                        │                                 ▼             │
     │                        │                           ┌───────────┐       │
     │                        │                           │displaying │       │
     │                        │                           │(app-local)│       │
     │                        │                           └───────────┘       │
     │                        │                                               │
     │  ┌──────────┐          │                                               │
     └─►│  error   │──────────┘  (tap to dismiss)                             │
        └──────────┘                                                          │
        ┌──────────────┐                                                      │
        │ disconnected │──────────────────────────────────────────────────────┘
        └──────────────┘   (reconnect success)
```

**Full state flow:** `loading → idle → recording → transcribing → thinking → streaming → displaying → idle`

> **Note:** The app does NOT blindly map Gateway status frames to app states. The `displaying` state is app-local and ignores the Gateway's `status:'idle'` frame that follows `{type:'end'}`.

### State Definitions

| State | Display | Accepted Input | Expected Gateway Frames |
|---|---|---|---|
| `loading` | "Loading..." | None (tap ignored — Gateway not ready) | `{type:"connected", version:"1.0"}` on initial connect |
| `idle` | "OpenClaw Ready", hint, status dot | Tap → start recording | `{type:"status", status:"loading"}` → back to loading |
| `recording` | "Recording...", level bar, mic indicator | Tap → stop recording | (none — phone is sending) |
| `transcribing` | "Transcribing..." | None (wait) | `{type:"transcription"}`, `{type:"status", status:"thinking"}` |
| `thinking` | "Thinking...", shows transcription preview | None (wait) | `{type:"status", status:"streaming"}`, first `{type:"assistant"}` |
| `streaming` | Live response text, auto-scrolling | Scroll (firmware-native) | `{type:"assistant", delta}` (many), `{type:"end"}` |
| `displaying` | Full response, navigation hints | Tap → dismiss (idle); double-tap → swap page via `rebuildPageContainer` | Ignores `{type:"status", status:"idle"}` from Gateway |
| `error` | Error message | Tap → dismiss (idle) | `{type:"error", code:"...", detail:"..."}` |

---

## 5. WebSocket Client (`gateway.ts`)

### Connection

Single persistent WebSocket to the PC Gateway:

```
ws://<PC_IP>:8765?token=<shared_secret>
```

The connection opens on app launch and stays alive. All audio and control traffic flows over this one socket.

### Auto-Reconnect

Exponential backoff on disconnect:

```
Attempt 1: wait 1s
Attempt 2: wait 2s
Attempt 3: wait 4s
Attempt 4: wait 8s
Attempt 5: wait 16s
Attempt 6+: wait 30s (max)
```

On successful reconnect, reset backoff to 1s. During reconnect, display shows "Connecting to Gateway..." on the glasses.

### Frame Handling

#### Outbound: Phone → Gateway

| Frame Type | Format | When Sent |
|---|---|---|
| **Binary** (PCM audio) | Raw bytes (`ArrayBuffer`) | Each `onMicData` chunk during recording |
| `start_audio` | `{type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2}` | User taps ring (from idle or displaying) |
| `stop_audio` | `{type:"stop_audio"}` | User taps ring again (from recording) |
| `text` | `{type:"text", message:"..."}` | Text input (future: keyboard/voice-to-text) |
| `pong` | `{type:"pong"}` | Immediately, in response to Gateway `{type:"ping"}` |

#### Inbound: Gateway → Phone

| `type` | Key Fields | App Action |
|---|---|---|
| `connected` | `version` (`"1.0"`) | Confirm connection, transition to idle, display "Ready" |
| `status` | `status` (string) | Transition state machine to matching state, update display. **Exception:** ignore `status:"idle"` when in `displaying` state. |
| `transcription` | `text` | Store transcription, show "You said: ..." in preview |
| `assistant` | `delta` | Strip markdown, append delta to response buffer, update display text |
| `end` | — | Finalize response display, transition to `displaying` state |
| `error` | `code`, `detail` | Show error on display, transition to `error` state |
| `ping` | — | Immediately respond with `{type:"pong"}` |

**Ping/pong keep-alive:** When the Gateway sends `{type:'ping'}`, the client immediately responds with `{type:'pong'}`. This is handled transparently — no state change occurs.

**Authentication:** The shared secret is passed as a query parameter on the WebSocket URL (`?token=<secret>`). There is no first-frame auth handshake.

### Connection Lifecycle

```typescript
// Pseudocode — gateway.ts
class Gateway {
  private ws: WebSocket | null = null;
  private url: string;
  private backoff = 1000;

  connect(): void {
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.backoff = 1000;  // reset on success
      this.emit('connected');
    };

    this.ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        const frame = JSON.parse(event.data);
        this.emit('message', frame);
      }
      // Binary frames are not expected inbound
    };

    this.ws.onclose = () => this.reconnect();
    this.ws.onerror = () => this.ws?.close();
  }

  send(data: ArrayBuffer): void {
    // Binary frame — raw PCM
    this.ws?.send(data);
  }

  sendJson(obj: Record<string, unknown>): void {
    // JSON text frame
    this.ws?.send(JSON.stringify(obj));
  }

  private reconnect(): void {
    setTimeout(() => this.connect(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, 30_000);
    this.emit('reconnecting');
  }
}
```

### Gateway URL Configuration

The WebSocket URL is resolved in this order:

1. **URL hash override:** `index.html#gateway=192.168.1.100:8765&token=mysecret`
2. **Query param override:** `index.html?gateway=192.168.1.100:8765&token=mysecret`
3. **localStorage persistence:** Previously configured value
4. **Build-time default:** Baked in via Vite `import.meta.env.VITE_GATEWAY_URL`

On first successful connection, the resolved URL and token are persisted to `localStorage` for subsequent launches.

---

## 6. Mic Capture (`audio.ts`)

### Audio Flow

```
┌─────────────┐     ┌────────────────┐     ┌──────────────┐     ┌─────────────┐
│ G2 Glasses  │ BLE │ EvenAppBridge  │     │  audio.ts    │     │ gateway.ts  │
│ Microphone  │────►│ onEvenHubEvent │────►│              │────►│ send(chunk) │──► PC
│             │     │ audioEvent     │     │ (no buffer!) │     │             │
└─────────────┘     └────────────────┘     └──────────────┘     └─────────────┘
```

### Step-by-Step

1. **User taps ring (from idle or displaying state)** → `input.ts` detects CLICK_EVENT in `idle` or `displaying` state.
2. **App sends control frame:** `gateway.sendJson({type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2})`.
3. **App opens microphone:** `await bridge.audioControl(true)`.
4. **PCM chunks arrive:** `onEvenHubEvent` fires with `audioEvent.audioPcm` (`Uint8Array`).
   - Real hardware: 40 bytes per frame (10ms, 16kHz, S16LE, mono).
   - Simulator: 3,200 bytes per frame (100ms).
5. **Each chunk forwarded immediately:** `gateway.send(chunk.buffer)` — no buffering, no accumulation.
6. **User taps ring again (from recording state)** → `input.ts` detects second tap.
7. **App closes microphone:** `await bridge.audioControl(false)`.
8. **App sends stop frame:** `gateway.sendJson({type:"stop_audio"})`.
9. **App updates display:** Transition to `transcribing` state, show "Transcribing..." on glasses.

### PCM Format Clarification

The `Uint8Array` from `onMicData` is a byte-level view of 16-bit PCM S16LE data — each sample spans 2 consecutive bytes. Send the raw bytes as-is; the PC Gateway handles conversion.

### Critical Design Constraint

**No buffering on the phone.** Each PCM chunk is forwarded as-is, the moment it arrives. The PC Gateway handles accumulation and passes the complete audio to Whisper for transcription. The phone never holds more than one chunk at a time.

### PCM Format Reference

| Parameter | Value |
|---|---|
| Sample rate | 16,000 Hz (16 kHz) |
| Frame duration | 10 ms (real hardware), 100 ms (simulator) |
| Bytes per frame | 40 (real hardware), 3,200 (simulator) |
| Encoding | PCM S16LE (signed 16-bit little-endian) |
| Channels | Mono |

### Prerequisite

`bridge.audioControl()` requires `createStartUpPageContainer` to have been called first. The app must set up the initial display layout before opening the microphone.

> **Important:** Always check the `StartUpPageCreateResult` returned by `createStartUpPageContainer`:
> - `0` = success
> - `1` = invalid parameters
> - `2` = container oversize
> - `3` = out of memory
>
> Proceed with mic capture and event registration only on success (`0`).

> **SDK initialization:** Use `await waitForEvenAppBridge()` to obtain the bridge instance — do **not** use `EvenAppBridge.getInstance()`, which is unreliable and may return before the bridge is ready.

---

## 7. Input Mapping

### Complete Event-to-Action Table

| Event | In State | Action |
|---|---|---|
| TAP (CLICK_EVENT / undefined) | `loading` | Ignore (Gateway not ready) |
| TAP | `idle` | Start mic, send `start_audio`, → `recording` |
| TAP | `recording` | Stop mic, send `stop_audio`, → `transcribing` |
| TAP | `transcribing` | Ignore |
| TAP | `thinking` | Ignore |
| TAP | `streaming` | Ignore |
| TAP | `displaying` | Dismiss, → `idle` |
| TAP | `error` | Dismiss, → `idle` |
| TAP | `disconnected` | Trigger reconnect |
| DOUBLE_CLICK_EVENT (3) | `displaying` | Swap page view via `rebuildPageContainer` (app-managed page state) |
| DOUBLE_CLICK_EVENT (3) | other | Ignore |
| SCROLL_BOTTOM (2) | `displaying` | End of content indicator |
| SCROLL_TOP (1) | `displaying` | Top of content indicator |
| FOREGROUND_ENTER (4) | any | Reconnect WS if needed |
| FOREGROUND_EXIT (5) | `recording` | Stop mic, send `stop_audio` |
| FOREGROUND_EXIT (5) | other | Maintain WS |
| ABNORMAL_EXIT (6) | any | Call `bridge.shutDownPageContainer(0)`, close mic, reset state |

### CLICK_EVENT = 0 / undefined Bug Workaround

The SDK deserializes `CLICK_EVENT` (value `0`) as `undefined` due to a `fromJson` bug. Additionally, the simulator sends click events through `sysEvent` while real hardware sends them through `textEvent` or `listEvent` depending on which container has `isEventCapture: 1`.

**Required pattern — handle all three event sources:**

```typescript
bridge.onEvenHubEvent((event: EvenHubEvent) => {
  const eventType =
    event.textEvent?.eventType ??
    event.listEvent?.eventType ??
    event.sysEvent?.eventType;

  if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
    if (eventType === undefined) {
      console.warn('Undefined eventType — treating as tap (SDK bug)');
    }
    handleTap();
  } else if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
    handleDoubleClick();
  } else if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
    handleScrollBoundary('top');
  } else if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
    handleScrollBoundary('bottom');
  } else if (eventType === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
    handleForegroundEnter();
  } else if (eventType === OsEventTypeList.FOREGROUND_EXIT_EVENT) {
    handleForegroundExit();
  } else if (eventType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
    handleAbnormalExit();
  }

  // Audio events arrive separately
  if (event.audioEvent) {
    handleAudioChunk(event.audioEvent.audioPcm);
  }
});
```

### Tap-to-Toggle Input Model

The G2 SDK does not provide distinct HOLD/RELEASE events. All ring/temple interactions fire `CLICK_EVENT`. The app uses a **tap-to-toggle** model:

| State | Tap Action |
|---|---|
| `idle` | Start recording → mic opens, display updates, → `recording` |
| `recording` | Stop recording → mic closes, send `stop_audio`, → `transcribing` |
| `displaying` | Dismiss response → return to `idle` |
| `error` | Dismiss error → return to `idle` |
| `loading` / `transcribing` / `thinking` / `streaming` | Ignored |

Double-tap in `displaying` state swaps between page 0 and page 1 views by calling `rebuildPageContainer` with the appropriate layout.


### Scroll Throttling

Apply a 300ms cooldown on scroll boundary events to prevent rapid-fire page changes from fast swipes:

```typescript
let lastScrollTime = 0;
function handleScrollBoundary(direction: 'top' | 'bottom'): void {
  const now = Date.now();
  if (now - lastScrollTime < 300) return;
  lastScrollTime = now;
  // Process scroll boundary
}
```

---

## 8. Configuration

### Config Resolution Order

The gateway URL and token are resolved with a priority chain:

1. **URL fragment** — `index.html#gateway=<ip:port>&token=<secret>` (highest priority)
2. **Query parameter** — `index.html?gateway=<ip:port>&token=<secret>`
3. **localStorage** — Persisted from a previous session
4. **Build-time env** — `VITE_GATEWAY_URL` and `VITE_GATEWAY_TOKEN` (lowest priority)

### Configuration Table

| Config | Source | Default | Example |
|---|---|---|---|
| `GATEWAY_URL` | Build-time env `VITE_GATEWAY_URL` / URL hash / query param | `ws://192.168.1.100:8765` | `ws://10.0.0.5:8765` |
| `GATEWAY_TOKEN` | Build-time env `VITE_GATEWAY_TOKEN` / URL hash / query param | (none — auth disabled) | `my_shared_secret` |

### Persistence

On first successful `{type:"connected"}` response from the Gateway, the app writes the resolved URL and token to `localStorage`:

```typescript
localStorage.setItem('g2_gateway_url', resolvedUrl);
localStorage.setItem('g2_gateway_token', resolvedToken);
```

On subsequent launches, these values are read as fallback when no hash/query override is present.

### Vite Build-Time Configuration

```bash
# .env (or .env.local)
VITE_GATEWAY_URL=ws://192.168.1.100:8765
VITE_GATEWAY_TOKEN=my_secret
```

Accessed in code via `import.meta.env.VITE_GATEWAY_URL`.

---

## 9. Build & Deploy

### Prerequisites

```bash
node --version   # >= 18
npm --version    # >= 9
npm install -g @evenrealities/evenhub-cli
```

### Build Steps

```bash
# 1. Install dependencies
cd g2_app
npm install

# 2. Build for production (outputs dist/ with single HTML+JS bundle)
npx vite build

# 3. Copy manifest into dist/
cp app.json dist/

# 4. Package as .ehpk
evenhub pack app.json dist -o openclaw.ehpk

# 5. Generate QR code for sideloading (use machine's LAN IP)
evenhub qr --url "http://192.168.1.100:5173"
#   or for the packed app served from a local HTTP server:
#   npx serve dist -l 3000
#   evenhub qr --ip 192.168.1.100 --port 3000
```

### Development Workflow

```bash
# Start Vite dev server (hot reload)
npx vite --host 0.0.0.0 --port 5173

# Generate QR code pointing to dev server
evenhub qr --url "http://192.168.1.100:5173"

# Scan QR with EvenHub iOS app → app loads in WebView
```

### `app.json` Manifest

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
  "permissions": {
    "network": ["*"]
  }
}
```

| Field | Description |
|---|---|
| `package_id` | Reverse-domain unique identifier. Lowercase letters + digits only, each segment starts with a letter. Regex: `^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$` |
| `edition` | Release edition string (e.g. `"202601"`) |
| `name` | Display name in EvenHub app launcher |
| `version` | Semantic version string |
| `min_app_version` | Minimum EvenHub app version required to run this app |
| `tagline` | Short one-line description shown in the app store |
| `description` | Full description of the app |
| `author` | Author or organization name |
| `entrypoint` | HTML file loaded by the WebView |
| `permissions` | Required permissions (e.g. `{"network": ["*"]}` for WebSocket access) |

> **Note:** The `icon` field is **not** part of the manifest schema and is silently ignored if present. Do not include it.

### Vite Configuration

```typescript
// vite.config.ts
import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        // Single JS bundle for WebView compatibility
        manualChunks: undefined,
        inlineDynamicImports: true,
      },
    },
  },
  server: {
    host: '0.0.0.0',  // Expose on LAN for QR dev workflow
    port: 5173,
  },
});
```

### QR Code Sideloading

1. Ensure iPhone and dev machine are on the same WiFi network.
2. Run `evenhub qr --url "http://<LAN_IP>:5173"`.
3. Open the EvenHub iOS app on the iPhone.
4. Scan the QR code — the app loads in the WebView.
5. The G2 glasses display the app UI immediately.

**Important:** Use the machine's LAN IP (`192.168.x.x`), never `localhost` — the iPhone must reach the dev server over the network.

---

## 10. Error Handling

### Error Recovery Matrix

| Error | Detection | Recovery | Glasses Display |
|---|---|---|---|
| **Gateway unreachable** | `WebSocket.onerror` / `onclose` fires | Auto-reconnect with exponential backoff (1→2→4→8→16→30s max) | "⚠ Connecting to Gateway..." |
| **Gateway auth failure** | `{type:"error", code:"AUTH_FAILED"}` | Display error, do not reconnect (bad token) | "✗ Auth failed — check token" |
| **Gateway error frame** | `{type:"error", code:"...", detail:"..."}` | Display `detail` from frame, transition → `error` state, tap to dismiss | Show `detail` from frame |
| **Transcription failed** | `{type:"error", code:"TRANSCRIPTION_FAILED"}` | Return to idle, user can retry | "✗ Could not transcribe — try again" |
| **OpenClaw error** | `{type:"error", code:"OPENCLAW_ERROR"}` | Return to idle | "✗ AI error — try again" |
| **Mic not available** | `audioControl(true)` fails or no `audioEvent` within 2s | Show error, return to idle | "✗ Mic unavailable" |
| **BLE disconnect** | `ABNORMAL_EXIT_EVENT` (6) | Close mic, reset state, wait for reconnect | "⚠ Glasses disconnected" |
| **Foreground exit during recording** | `FOREGROUND_EXIT_EVENT` (5) while `recording` | Close mic, send `stop_audio`, maintain WS | (app backgrounded — no display) |
| **WebSocket send failure** | `ws.send()` throws (WS not open) | Queue frame or drop, trigger reconnect | "⚠ Reconnecting..." |
| **Loading state error** | `{type:"error"}` while in `loading` state | Display error, transition → `error`, tap to dismiss | Show error detail |

### Error Display Layout

```
┌─────────────────────────── 576px ───────────────────────────┐
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=10
│  │  "⚠ Error"                                           │   │
│  │  containerID: 1  name: "title"   isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=36
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=56
│  │  "<error message from Gateway>"                      │   │
│  │  containerID: 2  name: "errmsg"  isEventCapture: 1   │   │
│  └──────────────────────────────────────────────────────┘   │ h=170
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=258
│  │  "Tap to dismiss"                                    │   │
│  │  containerID: 3  name: "status"  isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=22
└──────────────────────────────────────────────────────────────┘
```

### Reconnection Display

While the WebSocket is disconnected and reconnecting, the app shows a persistent connection status on the glasses. The backoff timer is displayed so the user knows the app is working:

```
"⚠ Connecting to Gateway... (retry in 4s)"
```

On successful reconnect, the display returns to the idle state.
