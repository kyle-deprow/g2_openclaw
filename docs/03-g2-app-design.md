# G2 OpenClaw App — Thin Client Design

## 1. Overview

The G2 OpenClaw App is the thinnest possible bridge between the Even Realities G2 smart glasses and the PC Gateway. It runs inside a WKWebView (`flutter_inappwebview`) on an iPhone, packaged as an EvenHub `.ehpk` app. The app has no intelligence of its own — it captures microphone PCM from the glasses, forwards raw bytes over a single WebSocket to the PC Gateway, receives streamed JSON text responses, and renders them on the 576×288 4-bit greyscale display via `EvenAppBridge.setLayout()`. Physical input from the R1 ring and temple gestures drives navigation and mic control.

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
├── app.json              ← EvenHub manifest (appId, appName, version, icon, entry)
├── index.html            ← Entry point loaded by WebView
├── src/
│   ├── main.ts           ← App entry: init bridge, connect WS, wire events
│   ├── gateway.ts        ← WebSocket client to PC Gateway (connect, send, receive)
│   ├── display.ts        ← Display manager: container layout, text rendering, page mgmt
│   ├── input.ts          ← Event handler: ring/gesture routing, mic control
│   ├── audio.ts          ← Mic capture: onMicData → forward to gateway
│   └── state.ts          ← Simple state machine (idle, recording, transcribing, thinking, streaming)
├── assets/
│   └── icon.bmp          ← App icon (4-bit greyscale BMP)
├── tsconfig.json
├── package.json
└── vite.config.ts        ← Bundler (output single JS for WebView)
```

### Module Responsibilities

| Module | Single Responsibility | Key Exports |
|---|---|---|
| **main.ts** | Bootstrap: initialize bridge, connect gateway, register event handlers, create startup page | `init()` — async entry point |
| **gateway.ts** | Manage the single WebSocket connection to the PC Gateway. Send binary/JSON frames, receive JSON frames, handle reconnection. | `Gateway` class: `connect()`, `send(data)`, `sendJson(obj)`, `onMessage(cb)`, `disconnect()` |
| **display.ts** | Build and push `ContainerData` layouts to the glasses. Manage text content, page state, and container specs for each app state. | `Display` class: `showIdle()`, `showRecording()`, `showTranscribing()`, `showThinking()`, `showStreaming(text)`, `showResponse(text)`, `showError(msg)`, `flipPage()` |
| **input.ts** | Route `onEvenHubEvent` callbacks to the correct action based on current app state and event type. Handle all SDK quirks. | `InputHandler` class: `setup(bridge)`, `onAction(cb)` |
| **audio.ts** | Open/close the G2 microphone via `bridge.audioControl()`. Forward each `onMicData` PCM chunk immediately to the gateway as a binary frame. | `AudioCapture` class: `start(bridge, gateway)`, `stop()` |
| **state.ts** | Track the app's display state. Transition between states in response to gateway frames and user input. Emit state change events. | `AppState` enum, `StateMachine` class: `transition(newState)`, `current()`, `onChange(cb)` |

### Data Flow Summary

```
┌──────────┐   BLE/PCM    ┌──────────────┐   binary WS    ┌────────────┐
│ G2       │──────────────►│ audio.ts     │───────────────►│ gateway.ts │──► PC Gateway
│ Glasses  │               │ (no buffer)  │                │            │
│          │◄──────────────│ display.ts   │◄───────────────│            │◄── PC Gateway
│          │  setLayout()  │ (containers) │   JSON frames  │            │
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

### 3.1 Idle State (Page 0)

```
┌─────────────────────────── 576px ───────────────────────────┐
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=10
│  │  "OpenClaw Ready"                                    │   │
│  │  containerID: 1  name: "title"   isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=36
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=56
│  │  "Hold ring to speak"                                │   │
│  │  containerID: 2  name: "hint"    isEventCapture: 1   │   │
│  └──────────────────────────────────────────────────────┘   │ h=28
│                                                              │
│                                                              │   288px
│                        (empty space)                         │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=258
│  │  "● Connected"                                       │   │
│  │  containerID: 3  name: "status"  isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=22
└──────────────────────────────────────────────────────────────┘
```

| Container | ID | Name | x | y | w | h | fontColor | borderWidth | isEventCapture |
|---|---|---|---|---|---|---|---|---|---|
| Title | 1 | `title` | 10 | 10 | 556 | 36 | `0xF` (bright) | 0 | 0 |
| Hint | 2 | `hint` | 10 | 56 | 556 | 28 | `0x8` (mid-grey) | 0 | 1 |
| Status | 3 | `status` | 10 | 258 | 556 | 22 | `0x6` (dim) | 0 | 0 |

**Container count:** 3. `containerTotalNum: 3`.

### 3.2 Recording State (Page 0)

```
┌─────────────────────────── 576px ───────────────────────────┐
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=10
│  │  "Recording..."                                      │   │
│  │  containerID: 1  name: "title"   isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=36
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=100
│  │  "━━━━━━━━━━━━━━━━"   (level bar — greyscale fill)   │   │
│  │  containerID: 2  name: "level"   isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=30
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=258
│  │  "◉ Mic active — release to send"                    │   │
│  │  containerID: 3  name: "status"  isEventCapture: 1   │   │
│  └──────────────────────────────────────────────────────┘   │ h=22
└──────────────────────────────────────────────────────────────┘
```

| Container | ID | Name | x | y | w | h | fontColor | borderWidth | isEventCapture |
|---|---|---|---|---|---|---|---|---|---|
| Title | 1 | `title` | 10 | 10 | 556 | 36 | `0xF` | 0 | 0 |
| Level | 2 | `level` | 60 | 100 | 456 | 30 | `0xA` | 1 (border `0x4`) | 0 |
| Status | 3 | `status` | 10 | 258 | 556 | 22 | `0xC` | 0 | 1 |

**Level bar:** Built using Unicode block characters (`━`) repeated proportionally to the mic level. Updated via `textContainerUpgrade` on each audio chunk (throttled to max 10 updates/sec to avoid flicker).

### 3.3 Transcribing / Thinking State (Page 0)

```
┌─────────────────────────── 576px ───────────────────────────┐
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=10
│  │  "Transcribing..."  OR  "Thinking..."                │   │
│  │  containerID: 1  name: "title"   isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=36
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=56
│  │  "You said: <transcription text>"                    │   │
│  │  (only shown during thinking state)                  │   │
│  │  containerID: 2  name: "preview" isEventCapture: 1   │   │
│  └──────────────────────────────────────────────────────┘   │ h=190
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=258
│  │  "⏳ Processing..."                                   │   │
│  │  containerID: 3  name: "status"  isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=22
└──────────────────────────────────────────────────────────────┘
```

During **transcribing**, the preview container shows "Listening..." or is empty. When the Gateway sends `{type:"transcription", text:"..."}`, update the preview with the transcribed text, then the title changes to "Thinking..." as the `thinking` status arrives.

### 3.4 Streaming Response State (Page 0)

```
┌─────────────────────────── 576px ───────────────────────────┐
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=0
│  │                                                      │   │
│  │  "The capital of France is Paris. It has been the    │   │
│  │   capital since the 10th century and is the most     │   │
│  │   populous city in France with over 2 million        │   │
│  │   inhabitants in the city proper..."                 │   │
│  │                                                      │   │
│  │  containerID: 1  name: "response" isEventCapture: 1  │   │
│  │  (firmware auto-scrolls as text grows)               │   │
│  └──────────────────────────────────────────────────────┘   │ h=264
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │ y=268
│  │  "▼ streaming..."                                    │   │
│  │  containerID: 2  name: "status"  isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=18
└──────────────────────────────────────────────────────────────┘
```

| Container | ID | Name | x | y | w | h | fontColor | borderWidth | isEventCapture |
|---|---|---|---|---|---|---|---|---|---|
| Response | 1 | `response` | 4 | 0 | 568 | 264 | `0xF` | 0 | 1 |
| Status | 2 | `status` | 10 | 268 | 556 | 18 | `0x6` | 0 | 0 |

**Streaming strategy:**

1. Accumulate `assistant` delta strings in a local buffer.
2. On each delta, call `textContainerUpgrade` to append text in-place (using `contentOffset = existingLength`, `contentLength = 0`).
3. Firmware auto-scrolls the text container because it has `isEventCapture: 1`.
4. Throttle display updates to max **15/sec** to avoid overwhelming the BLE link.
5. If accumulated text exceeds **1000 chars** (the `rebuildPageContainer` limit), switch to `textContainerUpgrade` which allows 2000 chars.

### 3.5 Full Response State (Page 0 + Page 1)

After `{type:"end"}` arrives:

**Page 0 — Response summary:**

```
┌─────────────────────────── 576px ───────────────────────────┐
│  ┌──────────────────────────────────────────────────────┐   │ y=0
│  │  <Full response text — scrollable>                   │   │
│  │  containerID: 1  name: "response" isEventCapture: 1  │   │
│  └──────────────────────────────────────────────────────┘   │ h=264
│  ┌──────────────────────────────────────────────────────┐   │ y=268
│  │  "✓ Done — tap to dismiss, double-tap for details"   │   │
│  │  containerID: 2  name: "status"  isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=18
└──────────────────────────────────────────────────────────────┘
```

**Page 1 — Detail / metadata (optional):**

```
┌─────────────────────────── 576px ───────────────────────────┐
│  ┌──────────────────────────────────────────────────────┐   │ y=0
│  │  "You said: <transcription>"                         │   │
│  │  containerID: 1  name: "query"   isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=60
│  ┌──────────────────────────────────────────────────────┐   │ y=66
│  │  <Response continuation or full text>                │   │
│  │  containerID: 2  name: "detail"  isEventCapture: 1   │   │
│  └──────────────────────────────────────────────────────┘   │ h=198
│  ┌──────────────────────────────────────────────────────┐   │ y=268
│  │  "Page 2/2 — double-tap to go back"                  │   │
│  │  containerID: 3  name: "footer"  isEventCapture: 0   │   │
│  └──────────────────────────────────────────────────────┘   │ h=18
└──────────────────────────────────────────────────────────────┘
```

Page flip is triggered by double-tap via `bridge.setPageFlip()`.

---

## 4. State Machine (App-Side)

The thin client tracks its own display state locally, transitioning in response to Gateway status frames and user input. The state machine mirrors the Gateway's status updates.

### State Diagram

```
                         ┌────────────────────┐
                    ┌────│       idle         │◄───────────────────────────┐
                    │    │                    │                            │
                    │    │  Display: "Ready"  │                            │
                    │    │  Input: ring hold  │                            │
                    │    └────────┬───────────┘                            │
                    │             │                                        │
                    │             │  user holds ring                       │
                    │             │  → audioControl(true)                  │
                    │             │  → send {type:"start_audio"}           │
                    │             ▼                                        │
                    │    ┌────────────────────┐                            │
                    │    │    recording       │                            │
                    │    │                    │                            │
                    │    │  Display: "Rec..." │                            │
                    │    │  Input: ring       │                            │
                    │    │    release only    │                            │
                    │    └────────┬───────────┘                            │
                    │             │                                        │
                    │             │  user releases ring                    │
                    │             │  → audioControl(false)                 │
                    │             │  → send {type:"stop_audio"}            │
                    │             ▼                                        │
                    │    ┌────────────────────┐                            │
                    │    │   transcribing     │                            │
                    │    │                    │                            │
                    │    │  Display: "Trans.."│                            │
                    │    │  Input: none       │                            │
                    │    └────────┬───────────┘                            │
                    │             │                                        │
                    │             │  Gateway: {type:"status",              │
                    │             │    status:"thinking"}                  │
                    │             ▼                                        │
                    │    ┌────────────────────┐                            │
                    │    │     thinking       │                            │
                    │    │                    │                            │
                    │    │  Display: user     │                            │
                    │    │   query + wait     │                            │
                    │    │  Input: none       │                            │
                    │    └────────┬───────────┘                            │
                    │             │                                        │
                    │             │  Gateway: first {type:"assistant"}     │
                    │             ▼                                        │
                    │    ┌────────────────────┐                            │
                    │    │    streaming       │                            │
                    │    │                    │                            │
                    │    │  Display: live     │                            │
                    │    │   response text    │                            │
                    │    │  Input: scroll     │                            │
                    │    └────────┬───────────┘                            │
                    │             │                                        │
                    │             │  Gateway: {type:"end"}                 │
                    │             │  → {type:"status", status:"idle"}      │
                    │             ▼                                        │
                    │    ┌────────────────────┐                            │
                    │    │   displaying       │                            │
                    │    │                    │                            │
                    │    │  Display: full     │                            │
                    │    │   response + nav   │                            │
                    │    │  Input: tap=dismiss│                            │
                    │    │   dbl-tap=flip pg  │                            │
                    │    └────────┬───────────┘                            │
                    │             │                                        │
                    │             │  user taps to dismiss                  │
                    │             │  OR user starts new recording          │
                    │             └────────────────────────────────────────┘
                    │
                    │    ┌────────────────────┐
                    └───►│      error         │
                         │                    │
                         │  Display: error    │
                         │   message          │
                         │  Input: tap=dismiss│
                         └────────┬───────────┘
                                  │  tap to dismiss
                                  │  → return to idle
                                  └───────────────────────────────────────►idle
```

### State Definitions

| State | Display | Accepted Input | Expected Gateway Frames |
|---|---|---|---|
| `idle` | "OpenClaw Ready", hint, status dot | Ring hold → start recording; double-tap → page flip | `{type:"connected"}` on initial connect |
| `recording` | "Recording...", level bar, mic indicator | Ring release → stop recording | (none — phone is sending) |
| `transcribing` | "Transcribing..." | None (wait) | `{type:"transcription"}`, `{type:"status", status:"thinking"}` |
| `thinking` | "Thinking...", shows transcription preview | None (wait) | `{type:"status", status:"streaming"}`, first `{type:"assistant"}` |
| `streaming` | Live response text, auto-scrolling | Scroll (firmware-native) | `{type:"assistant", delta}` (many), `{type:"end"}` |
| `displaying` | Full response, navigation hints | Tap → dismiss (idle); double-tap → page flip; ring hold → new recording | `{type:"status", status:"idle"}` (already received) |
| `error` | Error message | Tap → dismiss (idle) | `{type:"error"}` |

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
| `start_audio` | `{type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2}` | User begins recording (ring hold) |
| `stop_audio` | `{type:"stop_audio"}` | User stops recording (ring release) |
| `text` | `{type:"text", message:"..."}` | Text input (future: keyboard/voice-to-text) |
| `cancel` | `{type:"cancel"}` | Cancel current operation |

#### Inbound: Gateway → Phone

| `type` | Key Fields | App Action |
|---|---|---|
| `connected` | `version` | Confirm connection, transition to idle, display "Ready" |
| `status` | `status` (string) | Transition state machine to matching state, update display |
| `transcription` | `text` | Store transcription, show "You said: ..." in preview |
| `assistant` | `delta` | Append delta to response buffer, update display text |
| `end` | — | Finalize response display, transition to `displaying` state |
| `error` | `message`, `code` | Show error on display, transition to `error` state |

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

1. **User holds ring button** → `input.ts` detects CLICK/HOLD event in `idle` state.
2. **App sends control frame:** `gateway.sendJson({type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2})`.
3. **App opens microphone:** `await bridge.audioControl(true)`.
4. **PCM chunks arrive:** `onEvenHubEvent` fires with `audioEvent.audioPcm` (Uint8Array).
   - Real hardware: 40 bytes per frame (10ms, 16kHz, S16LE, mono).
   - Simulator: 3,200 bytes per frame (100ms).
5. **Each chunk forwarded immediately:** `gateway.send(chunk.buffer)` — no buffering, no accumulation.
6. **User releases ring button** → `input.ts` detects release.
7. **App closes microphone:** `await bridge.audioControl(false)`.
8. **App sends stop frame:** `gateway.sendJson({type:"stop_audio"})`.
9. **App updates display:** Transition to `transcribing` state, show "Transcribing..." on glasses.

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

---

## 7. Input Mapping

### Complete Event-to-Action Table

| Event | Value | In State | Action |
|---|---|---|---|
| Ring/Temple HOLD | CLICK (0/undefined) | `idle` | Start mic, send `start_audio`, transition → `recording` |
| Ring/Temple HOLD | CLICK (0/undefined) | `displaying` | Start mic (new question), transition → `recording` |
| Ring/Temple RELEASE | — | `recording` | Stop mic, send `stop_audio`, transition → `transcribing` |
| Ring/Temple TAP | CLICK (0/undefined) | `idle` | No-op (or show connectivity info) |
| Ring/Temple TAP | CLICK (0/undefined) | `streaming` | Ignored (do not interrupt stream) |
| Ring/Temple TAP | CLICK (0/undefined) | `displaying` | Dismiss response, transition → `idle` |
| Ring/Temple TAP | CLICK (0/undefined) | `error` | Dismiss error, transition → `idle` |
| DOUBLE_CLICK | 3 | `displaying` | Toggle page flip (page 0 ↔ page 1) |
| DOUBLE_CLICK | 3 | `idle` | Toggle page flip (if page 1 has content) |
| DOUBLE_CLICK | 3 | `streaming` | Ignored |
| SCROLL (firmware) | — | `streaming` / `displaying` | Firmware handles natively via `isEventCapture` container |
| SCROLL_BOTTOM | 2 | `displaying` | Indicate end of content (flash status bar) |
| SCROLL_TOP | 1 | `displaying` | Indicate top of content |
| FOREGROUND_ENTER | 4 | any | Reconnect WS if closed, refresh display for current state |
| FOREGROUND_EXIT | 5 | any | Close mic if recording (send `stop_audio`), maintain WS connection |
| ABNORMAL_EXIT | 6 | any | Clean up: close mic, close WS, reset state |

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
    handleClick();
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

### Hold vs Tap Detection

The G2 SDK does not provide distinct HOLD/RELEASE events. The CLICK_EVENT fires on tap. To implement hold-to-record:

**Strategy:** Use CLICK as a toggle. First tap starts recording, second tap stops. Alternatively, use DOUBLE_CLICK to cancel. The recommended approach:

| Gesture | Action |
|---|---|
| Single tap (idle) | Start recording → mic opens, display updates |
| Single tap (recording) | Stop recording → mic closes, send `stop_audio` |
| Single tap (displaying) | Dismiss response → return to idle |
| Double tap | Page flip (when response is displayed) |

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

# 3. Copy manifest and assets into dist/
cp app.json dist/
cp -r assets dist/

# 4. Package as .ehpk
cd dist
evenhub pack

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
  "appId": "com.g2openclaw.app",
  "appName": "OpenClaw",
  "version": "1.0.0",
  "icon": "assets/icon.bmp",
  "entry": "index.html"
}
```

| Field | Description |
|---|---|
| `appId` | Reverse-domain unique identifier |
| `appName` | Display name in EvenHub app launcher |
| `version` | Semantic version string |
| `icon` | Path to 4-bit greyscale BMP icon |
| `entry` | HTML file loaded by the WebView |

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
| **Gateway error frame** | `{type:"error", message:"..."}` | Display error message, transition → `error` state, tap to dismiss | Show `message` from frame |
| **Transcription failed** | `{type:"error", code:"TRANSCRIPTION_FAILED"}` | Return to idle, user can retry | "✗ Could not transcribe — try again" |
| **OpenClaw error** | `{type:"error", code:"OPENCLAW_ERROR"}` | Return to idle | "✗ AI error — try again" |
| **Mic not available** | `audioControl(true)` fails or no `audioEvent` within 2s | Show error, return to idle | "✗ Mic unavailable" |
| **BLE disconnect** | `ABNORMAL_EXIT_EVENT` (6) | Close mic, reset state, wait for reconnect | "⚠ Glasses disconnected" |
| **Foreground exit during recording** | `FOREGROUND_EXIT_EVENT` (5) while `recording` | Close mic, send `stop_audio`, maintain WS | (app backgrounded — no display) |
| **WebSocket send failure** | `ws.send()` throws (WS not open) | Queue frame or drop, trigger reconnect | "⚠ Reconnecting..." |

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
