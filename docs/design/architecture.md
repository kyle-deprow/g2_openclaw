# G2 OpenClaw — Architecture Overview

## 1. Project Purpose

G2 OpenClaw bridges Even Realities G2 AR smart glasses to an OpenClaw AI assistant running on a personal computer over the local network. The system follows a **thin-client** model: the iPhone app acts solely as a transparent pipe between the G2 glasses (BLE) and a unified PC Gateway (WebSocket). All intelligence — speech recognition, AI processing, session management — runs on the PC. The user speaks a question through the G2 microphone; raw PCM audio is relayed through the iPhone directly to the PC Gateway, which transcribes it locally via Whisper and forwards the text to OpenClaw. The streamed AI response flows back over the same WebSocket and is rendered in real time on the glasses' 576×288 micro-LED display. Physical input from the R1 ring and temple gestures provides navigation, selection, and page control — creating a fully hands-free, eyes-up AI assistant experience with no cloud dependency.

---

## 2. System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   LOCAL AREA NETWORK (WiFi)                                  │
│                                                                                              │
│  ┌──────────────────────┐         ┌──────────────────────────┐                               │
│  │   G2 Smart Glasses   │   BLE   │  iPhone (THIN CLIENT)    │       single WebSocket        │
│  │                       │◄───────►│                          │◄──────────────────────────┐   │
│  │  576×288 micro-LED    │         │  G2 OpenClaw App:        │                           │   │
│  │  4-bit greyscale      │         │                          │                           │   │
│  │  (16 shades of green) │         │  • Capture mic PCM       │                           │   │
│  │                       │         │    from onMicData()      │                           │   │
│  │  Inputs:              │         │  • Forward raw PCM bytes │                           │   │
│  │   • R1 Ring (BLE)     │         │    to PC Gateway (binary │                           │   │
│  │   • Temple gestures   │         │    WebSocket frames)     │                           │   │
│  │   • Microphone (PCM)  │         │  • Receive streamed text │                           │   │
│  │                       │         │    responses (JSON text   │                           │   │
│  │  No camera            │         │    frames)               │                           │   │
│  │  No speaker           │         │  • Render on glasses via │                           │   │
│  │  No GPS               │         │    bridge.setLayout()    │                           │   │
│  │                       │         │  • Handle ring/gesture   │                           │   │
│  │                       │         │    input for navigation  │                           │   │
│  └──────────────────────┘         │                          │                           │   │
│                                    │  Does NOT:               │                           │   │
│                                    │  • Talk to Whisper       │                           │   │
│                                    │  • Talk to OpenClaw      │                           │   │
│                                    │  • Buffer/process audio  │                           │   │
│                                    │  • Do any AI/ML work     │                           │   │
│                                    │  • Manage sessions/auth  │                           │   │
│                                    └──────────────────────────┘                           │   │
│                                                                                           │   │
│  ┌────────────────────────────────────────────────────────────────────────────────────┐   │   │
│  │                              PC (Developer Machine)                                │   │   │
│  │                                                                                    │   │   │
│  │  ┌──────────────────────────────────────────────────────────────────────────────┐  │   │   │
│  │  │                    PC Gateway (Python)  :8765                                │  │◄──┘   │
│  │  │                                                                              │  │       │
│  │  │  • Accepts single WebSocket from iPhone                                      │  │       │
│  │  │  • Receives raw PCM audio (binary frames) OR text (JSON frames)              │  │       │
│  │  │  • Transcribes audio via Whisper (faster-whisper, localhost)                  │  │       │
│  │  │  • Forwards transcript to OpenClaw Gateway (localhost:18789)                  │  │       │
│  │  │  • Streams OpenClaw response deltas back to phone over same WebSocket        │  │       │
│  │  │  • Handles OpenClaw auth/connect handshake internally                        │  │       │
│  │  │  • Sends status updates (recording, transcribing, thinking, streaming, idle) │  │       │
│  │  │                                                                              │  │       │
│  │  │         ┌────────────────────┐          ┌───────────────────────────────┐     │  │       │
│  │  │         │  faster-whisper    │          │  OpenClaw Gateway             │     │  │       │
│  │  │         │  (in-process or    │          │  localhost:18789              │     │  │       │
│  │  │         │   localhost API)   │          │                               │     │  │       │
│  │  │         │                    │          │  Wire Protocol:               │     │  │       │
│  │  │         │  PCM → text        │          │  • WS JSON text frames        │     │  │       │
│  │  │         │  transcription     │          │  • connect → agent → stream   │     │  │       │
│  │  │         │                    │          │  • HTTP POST /hooks/agent alt  │     │  │       │
│  │  │         └────────────────────┘          │                               │     │  │       │
│  │  │                                         │  Auth: token-based            │     │  │       │
│  │  │                                         │  Config: ~/.openclaw/config   │     │  │       │
│  │  │                                         └───────────────────────────────┘     │  │       │
│  │  └──────────────────────────────────────────────────────────────────────────────┘  │       │
│  └────────────────────────────────────────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Key design principle:** The iPhone is a "dumb pipe." It never interprets audio, never talks to Whisper or OpenClaw directly, and holds no AI state. All intelligence lives on the PC.

---

## 3. Component Inventory

| Component | Runs On | Language / Runtime | Port | Protocol | Purpose |
|---|---|---|---|---|---|
| **G2 Smart Glasses** | Worn on face | Firmware (no user code) | — | BLE to iPhone | Micro-LED display (576×288, 4-bit greyscale), microphone, R1 ring + temple gesture input |
| **G2 OpenClaw App** (thin client) | iPhone WebView (WKWebView) | TypeScript / JavaScript | — | BLE (to glasses), single WS (to PC Gateway) | Captures mic PCM via `onMicData`, forwards raw bytes to PC Gateway, receives streamed text, renders on glasses, handles gesture input |
| **PC Gateway** | PC (LAN) | Python 3.12+ | `8765` | WebSocket (to phone), in-process (to Whisper), WS (to OpenClaw) | Unified entry point — receives audio/text from phone, transcribes via Whisper, relays to OpenClaw, streams responses back |
| **OpenClaw Gateway** | PC (localhost) | Node.js ≥ 22 / TypeScript | `18789` | WebSocket (+ HTTP hooks) | Local-first AI assistant — receives user messages, runs agent, streams response deltas. Accessed only by the PC Gateway, never by the phone. |

### What Changed from the Naive Architecture

| Aspect | Old (Wrong) | New (Thin Client) |
|---|---|---|
| Phone → Whisper | Phone sends HTTP POST with PCM to Whisper on PC | Phone sends raw PCM over WebSocket to PC Gateway; Gateway transcribes locally |
| Phone → OpenClaw | Phone opens its own WebSocket to OpenClaw on PC | Phone never talks to OpenClaw; Gateway handles it |
| Open ports to phone | 2 (Whisper :8765, OpenClaw :18789) | 1 (PC Gateway :8765) |
| Audio buffering | Phone accumulates and sends complete PCM blob | Phone streams individual chunks as they arrive |
| OpenClaw auth | Phone manages token and connect handshake | PC Gateway manages token and handshake internally |
| Session management | Phone tracks sessionKey and runId | PC Gateway owns session state |

---

## 4. Data Flow: Voice-to-Response

### Step-by-Step Walkthrough

| Step | Actor | Action | Detail |
|---:|---|---|---|
| 1 | **User** | Speaks | Voice enters G2 glasses microphone |
| 2 | **G2 Glasses** | Streams PCM | Raw PCM Uint8Array chunks sent over BLE to iPhone |
| 3 | **G2 App** | Receives `onMicData` | Callback fires per audio chunk |
| 4 | **G2 App** | Forwards chunk immediately | Sends raw PCM bytes as a binary WebSocket frame to PC Gateway — no buffering, no processing |
| 5 | **PC Gateway** | Receives binary frame | Accumulates PCM chunks in a server-side buffer |
| 6 | **G2 App** | Signals end of speech | User taps ring again → app sends `{type:"stop_audio"}` JSON text frame |
| 7 | **PC Gateway** | Transcribes | Sends `{type:"status", status:"transcribing"}` to phone. Passes accumulated PCM to faster-whisper for inference |
| 8 | **PC Gateway** | Sends transcription | Sends `{type:"transcription", text:"what user said"}` to phone (for optional display) |
| 9 | **PC Gateway** | Relays to OpenClaw | Sends WS frame to OpenClaw (localhost:18789): `{type:"req", method:"agent", params:{message:"...", sessionKey:"agent:claw:g2"}}`. Sends `{type:"status", status:"thinking"}` to phone |
| 10 | **OpenClaw** | Accepts | Returns `{runId, acceptedAt}` — begins agent processing |
| 11 | **OpenClaw** | Streams response | Emits `{type:"event", event:"agent", payload:{stream:"assistant", delta:"..."}}` text chunks to PC Gateway |
| 12 | **PC Gateway** | Relays deltas | Forwards each delta to phone as `{type:"assistant", delta:"..."}`. Sends `{type:"status", status:"streaming"}` on first delta |
| 13 | **G2 App** | Buffers deltas | Accumulates streamed text, incrementally updating display content |
| 14 | **G2 App** | Renders on glasses | Calls `bridge.setLayout()` with `ContainerData[]` to push text to the 576×288 display |
| 15 | **OpenClaw** | Signals completion | Emits `{type:"event", event:"agent", payload:{stream:"lifecycle", phase:"end"}}` |
| 16 | **PC Gateway** | Signals end | Sends `{type:"end"}` and `{type:"status", status:"idle"}` to phone |
| 17 | **G2 App** | Finalizes display | Final render pass — may show full response, enable scroll, or switch pages |

### Sequence Diagram

```
 ┌──────┐       ┌──────────┐       ┌─────────────┐       ┌──────────────┐       ┌──────────┐
 │ User │       │ G2       │       │ iPhone      │       │ PC Gateway   │       │ OpenClaw │
 │      │       │ Glasses  │       │ (thin app)  │       │ :8765        │       │ :18789   │
 └──┬───┘       └────┬─────┘       └──────┬──────┘       └──────┬───────┘       └────┬─────┘
    │                 │                    │                      │                    │
    │  speaks         │                    │                      │                    │
    │────────────────►│                    │                      │                    │
    │                 │                    │                      │                    │
    │                 │  PCM chunk (BLE)   │                      │                    │
    │                 │──────────────────►│                      │                    │
    │                 │                    │                      │                    │
    │                 │                    │  binary WS frame     │                    │
    │                 │                    │  (raw PCM bytes)     │                    │
    │                 │                    │─────────────────────►│                    │
    │                 │                    │                      │                    │
    │                 │  more PCM chunks   │                      │  [buffer PCM]      │
    │                 │──────────────────►│──────────────────────►│                    │
    │                 │                    │                      │                    │
    │  taps ring      │                    │                      │                    │
    │  (to stop)      │                    │                      │                    │
    │────────────────►│                    │                      │                    │
    │                 │  TAP (BLE)         │                      │                    │
    │                 │──────────────────►│                      │                    │
    │                 │                    │                      │                    │
    │                 │                    │  {type:"stop_audio"} │                    │
    │                 │                    │─────────────────────►│                    │
    │                 │                    │                      │                    │
    │                 │                    │  {type:"status",     │                    │
    │                 │                    │   status:            │                    │
    │                 │                    │   "transcribing"}    │                    │
    │                 │                    │◄─────────────────────│                    │
    │                 │                    │                      │                    │
    │                 │                    │                      │  [Whisper inference │
    │                 │                    │                      │   on accumulated   │
    │                 │                    │                      │   PCM — local,     │
    │                 │                    │                      │   in-process]       │
    │                 │                    │                      │                    │
    │                 │                    │  {type:              │                    │
    │                 │                    │   "transcription",   │                    │
    │                 │                    │   text:"hello"}      │                    │
    │                 │                    │◄─────────────────────│                    │
    │                 │                    │                      │                    │
    │                 │                    │  {type:"status",     │                    │
    │                 │                    │   status:"thinking"} │                    │
    │                 │                    │◄─────────────────────│                    │
    │                 │                    │                      │                    │
    │                 │                    │                      │  WS: {type:"req",  │
    │                 │                    │                      │   method:"agent",  │
    │                 │                    │                      │   params:{message: │
    │                 │                    │                      │   "hello",         │
    │                 │                    │                      │   sessionKey:      │
    │                 │                    │                      │   "agent:claw:g2"}}│
    │                 │                    │                      │───────────────────►│
    │                 │                    │                      │                    │
    │                 │                    │                      │  WS: {ok:true,     │
    │                 │                    │                      │   payload:{runId,  │
    │                 │                    │                      │   acceptedAt}}     │
    │                 │                    │                      │◄───────────────────│
    │                 │                    │                      │                    │
    │                 │                    │  {type:"status",     │                    │
    │                 │                    │   status:"streaming"}│                    │
    │                 │                    │◄─────────────────────│                    │
    │                 │                    │                      │  WS: {stream:      │
    │                 │                    │  {type:"assistant",  │   "assistant",     │
    │                 │                    │   delta:"The "}      │   delta:"The "}    │
    │                 │                    │◄─────────────────────│◄───────────────────│
    │                 │                    │                      │                    │
    │                 │  setLayout(        │                      │                    │
    │                 │  ContainerData[])  │                      │                    │
    │                 │◄──────────────────│                      │                    │
    │                 │                    │                      │                    │
    │  reads display  │                    │  {type:"assistant",  │  WS: {stream:     │
    │◄────────────────│                    │   delta:"answer "}   │   "assistant",    │
    │                 │                    │◄─────────────────────│◄──────────────────│
    │                 │                    │                      │                    │
    │                 │                    │        ...more deltas...                  │
    │                 │                    │                      │                    │
    │                 │                    │  {type:"end"}        │  WS: {stream:     │
    │                 │                    │◄─────────────────────│   "lifecycle",    │
    │                 │                    │                      │   phase:"end"}    │
    │                 │                    │  {type:"status",     │◄──────────────────│
    │                 │                    │   status:"idle"}     │                    │
    │                 │                    │◄─────────────────────│                    │
    │                 │                    │                      │                    │
    │                 │  setLayout(final)  │                      │                    │
    │                 │◄──────────────────│                      │                    │
    │                 │                    │                      │                    │
    │  reads final    │                    │                      │                    │
    │◄────────────────│                    │                      │                    │
    │                 │                    │                      │                    │
```

---

## 5. Data Flow: Ring / Gesture Interaction

### Input Sources

The G2 platform has two physical input devices that produce **identical event types** through the SDK:

| Device | Connection | Gestures |
|---|---|---|
| **R1 Ring** | BLE (to iPhone, relayed to glasses) | Tap (CLICK), double-tap (DOUBLE_CLICK), trackpad scroll |
| **G2 Temple Strips** | Built-in capacitive touch | Tap, double-tap, swipe (scroll) |

### Event Types (`OsEventTypeList`)

| Event | Value | Trigger |
|---|---|---|
| `CLICK_EVENT` | 0 | Single tap / select |
| `SCROLL_TOP_EVENT` | 1 | Firmware scroll reached top boundary |
| `SCROLL_BOTTOM_EVENT` | 2 | Firmware scroll reached bottom boundary |
| `DOUBLE_CLICK_EVENT` | 3 | Double tap |
| `FOREGROUND_ENTER_EVENT` | 4 | App enters foreground |
| `FOREGROUND_EXIT_EVENT` | 5 | App moves to background |
| `ABNORMAL_EXIT_EVENT` | 6 | Unexpected disconnect |

> **Critical quirk:** `CLICK_EVENT = 0` deserializes as `undefined` due to an SDK bug.
> Always check `eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined`.

### Interaction Mapping

All gesture handling is local to the phone app — gestures do **not** travel to the PC Gateway (except for mic tap-to-toggle which drives the audio flow).

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         G2 OpenClaw Interaction Map                              │
├─────────────────────┬───────────────────────────────────────────────────────────┤
│                     │                                                           │
│  ┌───────────────┐  │  State: IDLE (waiting for input)                          │
│  │   R1 Ring     │  │  ─────────────────────────────────                        │
│  │               │  │                                                           │
│  │  TAP (idle)   │──┼──► Open microphone, send start_audio, stream PCM          │
│  │  TAP (rec.)   │──┼──► Close mic, send {type:"stop_audio"}; await response    │
│  │  TAP (disp.)  │──┼──► Dismiss response / return to idle                      │
│  │  DOUBLE TAP   │──┼──► Toggle page (app-layer swap via rebuildPageContainer)   │
│  │  SCROLL       │──┼──► Firmware scrolls list/text natively                    │
│  │               │  │                                                           │
│  └───────────────┘  │  State: RECORDING (mic active, streaming to Gateway)      │
│                     │  ──────────────────────────────────────────────            │
│  ┌───────────────┐  │                                                           │
│  │   Temple      │  │  TAP (idle) → start recording, stream to Gateway          │
│  │   Gestures    │  │  TAP (rec.) → send {type:"stop_audio"}, await response    │
│  │               │  │                                                           │
│  │  TAP          │──┼──► Same as ring TAP                                       │
│  │  DOUBLE TAP   │──┼──► Same as ring DOUBLE TAP                               │
│  │  SWIPE FWD    │──┼──► Scroll down (firmware handles natively)                │
│  │  SWIPE BACK   │──┼──► Scroll up (firmware handles natively)                  │
│  └───────────────┘  │                                                           │
│                     │  State: DISPLAYING RESPONSE                               │
│                     │  ─────────────────────────────                             │
│                     │                                                           │
│                     │  SCROLL → firmware auto-scrolls long text/list            │
│                     │  TAP → dismiss / return to idle                           │
│                     │  DOUBLE TAP → swap page content (via rebuildPageContainer)│
│                     │  SCROLL_BOTTOM_EVENT → load next page or auto-advance     │
│                     │                                                           │
└─────────────────────┴───────────────────────────────────────────────────────────┘
```

### Event Routing Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    G2 Event Flow                                     │
│                                                                      │
│   ┌────────┐     ┌───────────┐     ┌─────────────────────────────┐  │
│   │  R1    │     │ G2 Temple │     │    G2 Glasses Firmware      │  │
│   │  Ring  │     │ Strips    │     │                             │  │
│   └───┬────┘     └─────┬─────┘     │  • Scroll position mgmt    │  │
│       │                │           │  • Selection highlighting   │  │
│       │   BLE          │  touch    │  • Boundary detection       │  │
│       ▼                ▼           │                             │  │
│   ┌────────────────────────────┐   └──────────────┬──────────────┘  │
│   │   Hardware Event           │                  │                  │
│   │   (gesture detected)       │                  │ BLE event       │
│   └────────────┬───────────────┘                  │                  │
│                │                                  ▼                  │
│                │                   ┌──────────────────────────────┐  │
│                │                   │  Flutter Host (EvenHub iOS)  │  │
│                │                   │  window._listenEvenAppMessage│  │
│                │                   └──────────────┬───────────────┘  │
│                │                                  │                  │
│                │                                  ▼                  │
│                │                   ┌──────────────────────────────┐  │
│                │                   │  EvenAppBridge SDK           │  │
│                │                   │  bridge.onEvenHubEvent(cb)   │  │
│                │                   └──────────────┬───────────────┘  │
│                │                                  │                  │
│                │                                  ▼                  │
│                │                   ┌──────────────────────────────┐  │
│                │                   │  EvenHubEvent                │  │
│                │                   │  ┌──────────┬──────────┐     │  │
│                │                   │  │listEvent │textEvent │     │  │
│                │                   │  │          │          │     │  │
│                │                   │  │sysEvent  │audioEvent│     │  │
│                │                   │  └──────────┴──────────┘     │  │
│                │                   └──────────────┬───────────────┘  │
│                │                                  │                  │
│                │                                  ▼                  │
│                │                   ┌──────────────────────────────┐  │
│                │                   │  G2 OpenClaw App Handler     │  │
│                │                   │                              │  │
│                │                   │  CLICK → select / confirm    │  │
│                │                   │  DOUBLE_CLICK_EVENT → swap   │  │
│                │                   │  SCROLL_TOP → prev page      │  │
│                │                   │  SCROLL_BOTTOM → next page   │  │
│                │                   │  audioEvent → forward PCM    │  │
│                │                   │    chunk to PC Gateway       │  │
│                └──────────────────►│                              │  │
│                                    └──────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### Page Management

The G2 display supports up to **4 containers** per layout. There is no native `setPageFlip` API — dual-page display must be **simulated at the application layer** by calling `bridge.rebuildPageContainer()` to swap container content on double-tap.

| "Page" | Suggested Use | Implementation |
|---|---|---|
| Page 0 (default) | Primary view — conversation, current response | Initial `setLayout()` call |
| Page 1 (detail) | Full response, tools used, status | `rebuildPageContainer()` replaces containers on double-tap |

Double-tap (`DOUBLE_CLICK_EVENT`) is the recommended gesture for flipping between logical pages.

---

## 6. Network Requirements

### Topology

```
┌───────────────────────────────────────────────────────────────────┐
│                        WiFi Router / AP                           │
│                    (192.168.x.x subnet)                           │
│                                                                   │
│     ┌──────────────────┐              ┌───────────────────────┐   │
│     │  iPhone           │              │  PC                   │   │
│     │  192.168.x.A      │─── WiFi ────│  192.168.x.B          │   │
│     │                   │              │                       │   │
│     │  G2 App WebView   │              │  :8765  PC Gateway    │   │
│     │  connects to:     │              │   └─► Whisper (local) │   │
│     │  • PC_IP:8765     │              │   └─► OpenClaw :18789 │   │
│     │    (only port)    │              │       (localhost only) │   │
│     └────────┬──────────┘              └───────────────────────┘   │
│              │ BLE                                                 │
│     ┌────────▼──────────┐                                         │
│     │  G2 Glasses       │                                         │
│     │  (no WiFi)        │                                         │
│     └───────────────────┘                                         │
└───────────────────────────────────────────────────────────────────┘
```

### Requirements Checklist

| # | Requirement | Detail |
|--:|---|---|
| 1 | **Same WiFi network** | iPhone and PC must be on the same local subnet. The G2 glasses have no WiFi — they connect only via BLE to the iPhone. |
| 2 | **PC LAN IP reachable** | The iPhone must be able to reach the PC's internal IP (e.g., `192.168.1.100`). No NAT traversal or public IP needed. |
| 3 | **Port 8765 open on PC** | PC Gateway listens on `0.0.0.0:8765`. Firewall must allow inbound TCP on this port from the LAN. This is the **only port** the phone connects to. |
| 4 | **Port 18789 localhost only** | OpenClaw Gateway listens on `127.0.0.1:18789`. It does **not** need to be exposed to the LAN — only the PC Gateway (running on the same machine) connects to it. |
| 5 | **No internet required** | All communication is LAN-only. Whisper runs locally (faster-whisper with CPU inference). OpenClaw runs locally. No cloud APIs in the critical path. |
| 6 | **BLE pairing** | G2 glasses must be paired to the iPhone via the EvenHub iOS app. R1 ring must be paired separately via BLE. |
| 7 | **WebView HTTP** | The G2 app is served to the WebView. Only one outbound WebSocket connection is made to `ws://<PC_IP>:8765`. |
| 8 | **Low latency** | WiFi latency is typically <5ms on LAN. Audio streaming and response rendering should feel near-realtime. |

### Port Summary

| Port | Service | Protocol | Direction | Bind Address |
|---:|---|---|---|---|
| `8765` | PC Gateway | WebSocket | iPhone → PC | `0.0.0.0` |
| `18789` | OpenClaw Gateway | WebSocket | PC Gateway → localhost | `127.0.0.1` (default, no change needed) |

**Simplification over old architecture:** The phone only needs to know one address (`PC_IP:8765`). OpenClaw stays locked to localhost, reducing the attack surface.

---

## 7. Security Considerations

### Threat Model

This system operates on a trusted home/office LAN with no cloud exposure. The thin-client model concentrates the security surface on the PC Gateway.

| Surface | Risk | Mitigation |
|---|---|---|
| **PC Gateway (:8765)** | Unauthorized WebSocket connections from LAN devices could send audio or text for AI processing | The Gateway validates a shared secret (`GATEWAY_TOKEN`) via query parameter on connection. Since this is the only externally-reachable port, hardening it secures the entire system. |
| **OpenClaw Gateway (:18789)** | Unauthorized access to the AI agent | Bound to `127.0.0.1` by default — not reachable from the network. Only the PC Gateway connects to it. Token-based auth (`OPENCLAW_GATEWAY_TOKEN`) adds a second layer. |
| **BLE (Glasses ↔ iPhone)** | BLE sniffing could intercept mic audio or display content | BLE 5.0+ with bonded pairing provides link-layer encryption. The EvenHub app manages pairing securely. Practical BLE interception requires physical proximity (<10m) and specialized hardware. |
| **WiFi (iPhone ↔ PC)** | Network sniffing could intercept audio bytes and AI responses | Use WPA3 or WPA2 with a strong passphrase. For additional protection, the PC Gateway could serve WSS (WebSocket over TLS) — requires configuring a local TLS cert. |
| **PCM Audio** | Voice data is transmitted as raw bytes over the WebSocket on the LAN | Encrypted WiFi mitigates passive sniffing. For sensitive environments, enable WSS on the PC Gateway. |
| **OpenClaw config** | `~/.openclaw/config.json` contains tokens and model API keys | File permissions should be `600`. Do not commit to version control. |
| **Session persistence** | OpenClaw maintains conversation history per `sessionKey` | The PC Gateway uses a stable key (e.g., `agent:claw:g2`) internally. Session data is stored locally by OpenClaw. |

### Recommendations

1. **Authenticate the phone-to-Gateway WebSocket** — use `GATEWAY_TOKEN` as a query parameter (`ws://host:port?token=secret`). No first-frame auth.
2. **Keep OpenClaw on localhost** — never bind it to `0.0.0.0`.
3. **Use a dedicated WiFi network** or VLAN for the glasses setup if in a shared/public space.
4. **Consider WSS** for the PC Gateway if audio contains sensitive information (medical, legal, etc.).
5. **Always set `OPENCLAW_GATEWAY_TOKEN`** — never run OpenClaw without auth, even when only accessed from localhost.

---

## 8. Technology Stack Summary

| Layer | Technology | Version | Purpose |
|---|---|---|---|
| **G2 App** | TypeScript / JavaScript | ES2020+ | Thin client in iPhone WebView — mic forwarding + display rendering |
| **G2 SDK** | `@evenrealities/even_hub_sdk` | latest | Bridge singleton for display rendering, input events, mic access |
| **WebView Host** | `flutter_inappwebview` (WKWebView) | — | Hosts the web app on iPhone, bridges JS ↔ native ↔ BLE |
| **PC Gateway** | Python | 3.12+ | Unified WebSocket gateway — audio receive, transcription, OpenClaw relay |
| **WebSocket Lib** | `websockets` (Python) | latest | Async WebSocket server for phone connection |
| **Whisper Engine** | `faster-whisper` | latest | CTranslate2-based Whisper inference (CPU, int8) — called in-process by Gateway |
| **OpenClaw Gateway** | Node.js / TypeScript | ≥ 22 | Local AI assistant daemon — agent runtime, tool execution, session management |
| **OpenClaw Protocol** | WebSocket (JSON text frames) | RFC 6455 | PC Gateway ↔ OpenClaw communication (localhost only) |
| **Phone Protocol** | WebSocket (binary + JSON text) | RFC 6455 | Single connection: phone ↔ PC Gateway |
| **BLE** | Bluetooth Low Energy 5.0+ | — | G2 glasses ↔ iPhone connection (display, input, mic) |
| **Display** | Micro-LED (dual panel) | — | 576×288 px, 4-bit greyscale (16 shades), green-only |
| **Package Manager** | `uv` (Python) | — | Python dependency management for PC Gateway |

### File Structure

```
g2_openclaw/
├── src/
│   ├── gateway/                       ← PC Gateway (Python WebSocket server)
│   │   ├── server.py                  ← WebSocket server, frame routing, state machine
│   │   ├── protocol.py                ← Frame type definitions, parse/serialize
│   │   ├── config.py                  ← Configuration (env vars / .env)
│   │   ├── transcriber.py             ← Whisper integration (faster-whisper)
│   │   ├── openclaw_client.py         ← OpenClaw WebSocket client
│   │   ├── audio_buffer.py            ← PCM accumulation & numpy conversion
│   │   └── agent_config/              ← OpenClaw agent persona (SOUL.md)
│   ├── g2_app/                        ← G2 App (TypeScript thin client for iPhone)
│   │   ├── src/                       ← main.ts, gateway.ts, display.ts, state.ts, etc.
│   │   └── app.json                   ← EvenHub manifest
│   ├── copilot_bridge/                ← Copilot Bridge (OpenClaw ↔ GitHub Copilot SDK)
│   │   └── src/                       ← client.ts, plugin.ts, orchestrator.ts, MCP servers
├── infra/                             ← Azure Bicep IaC + Infra CLI (Typer + Rich)
│   ├── main.bicep
│   ├── modules/                       ← AI Hub, OpenAI, Key Vault, Storage, Monitoring
│   └── parameters/                    ← Environment-specific parameters
├── tests/
│   ├── gateway/                       ← Python gateway tests (pytest)
│   ├── integration/                   ← End-to-end integration tests
│   └── mocks/                         ← Mock OpenClaw server
├── docs/
│   ├── design/                        ← Architecture, protocol, component design docs
│   ├── guides/                        ← Getting started, development workflow
│   ├── reference/                     ← External system docs (OpenClaw, G2 SDK)
│   ├── decisions/                     ← Architecture Decision Records (ADRs)
│   └── implementation/                ← Phase plans and progress tracking
└── .github/
    └── skills/                        ← Copilot skill definitions
```

> **Protocol Reference:** The canonical WebSocket protocol specification is in
> [protocol.md](protocol.md). All frame types, field names, and error codes are
> defined there as the single source of truth.

---

## Appendix A: PC Gateway WebSocket Protocol

This section defines the WebSocket frame types exchanged between the iPhone thin client and the PC Gateway over the single `ws://<PC_IP>:8765` connection.

### Connection

```
ws://<PC_IP>:8765?token=<shared_secret>
```

The phone opens one WebSocket connection on app launch and keeps it alive. The `token` query parameter authenticates the client (configured via `GATEWAY_TOKEN` env var). The Gateway rejects connections with an invalid or missing token (if auth is enabled).

### Frame Types: Phone → Gateway

#### Binary Frames (Audio)

Raw PCM audio bytes are sent as **binary WebSocket frames** — no JSON wrapping, no base64 encoding. Each `onMicData` chunk is forwarded as-is.

| Frame Type | Format | Description |
|---|---|---|
| Raw PCM | Binary frame (bytes) | Uint8Array audio chunk from G2 microphone, forwarded directly |

#### JSON Text Frames (Control)

| `type` | Fields | Description |
|---|---|---|
| `start_audio` | `{type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2}` | Signals that audio streaming is beginning. Gateway starts accumulating PCM. |
| `stop_audio` | `{type:"stop_audio"}` | Signals end of speech. Gateway finalizes buffer and begins transcription. |
| `text` | `{type:"text", message:"typed input"}` | Text input (e.g., if the user types instead of speaking). Gateway skips transcription and sends directly to OpenClaw. |
| `pong` | `{type:"pong"}` | Response to a Gateway `ping`. Phone must respond within 10 seconds. |

> **`cancel` — Reserved for v2.** Not implemented in v1.

### Frame Types: Gateway → Phone

All Gateway-to-phone frames are **JSON text frames**.

| `type` | Fields | Description |
|---|---|---|
| `status` | `{type:"status", status:"<state>"}` | State machine update. States: `loading`, `recording`, `transcribing`, `thinking`, `streaming`, `idle`, `error` |
| `transcription` | `{type:"transcription", text:"what user said"}` | The transcribed text, sent after Whisper completes. Phone may display this as a "You said:" preview. |
| `assistant` | `{type:"assistant", delta:"streamed chunk"}` | A chunk of the AI assistant's response. Accumulate these to build the full response. |
| `end` | `{type:"end"}` | Signals completion of the current response cycle. All deltas have been sent. |
| `error` | `{type:"error", detail:"description", code:"ERROR_CODE"}` | An error occurred. Codes: `AUTH_FAILED`, `TRANSCRIPTION_FAILED`, `OPENCLAW_ERROR`, `INTERNAL_ERROR` |
| `connected` | `{type:"connected", version:"1.0"}` | Sent immediately after successful WebSocket connection and authentication. |
| `ping` | `{type:"ping"}` | Sent every 30 seconds. Phone must respond with `{type:"pong"}` within 10 seconds or be disconnected. |

### Protocol Invariants

- `status` frames always precede their corresponding data frames (e.g., `status:"transcribing"` before `transcription`, `status:"streaming"` before first `assistant` delta)
- `end` frame is always sent after all `assistant` delta frames
- `error` frame may arrive at any point and always transitions to idle
- Frame delivery order matches send order (TCP/WebSocket guarantee)
- All binary frames in v1 are raw PCM audio. Future binary types will use a 1-byte type prefix.

### State Machine

```
                    ┌──────────────┐
                    │   loading    │  (Whisper model initializing)
                    └──────┬───────┘
                           │  model ready
                           ▼
                    ┌──────────────┐
               ┌────│    idle      │◄──────────────────────────────┐
               │    └──────┬───────┘                                │
               │           │  phone sends start_audio               │
               │           ▼                                        │
               │    ┌──────────────┐                                │
               │    │  recording   │  (phone streaming PCM chunks)  │
               │    └──────┬───────┘                                │
               │           │  phone sends stop_audio                │
               │           ▼                                        │
               │    ┌──────────────┐                                │
               │    │ transcribing │  (Whisper inference running)   │
               │    └──────┬───────┘                                │
               │           │  transcription complete                │
               │           ▼                                        │
               │    ┌──────────────┐                                │
               │    │  thinking    │  (waiting for OpenClaw)        │
               │    └──────┬───────┘                                │
               │           │  first delta arrives                   │
               │           ▼                                        │
               │    ┌──────────────┐                                │
               │    │  streaming   │  (relaying assistant deltas)   │
               │    └──────┬───────┘                                │
               │           │  lifecycle:end from OpenClaw           │
               │           ▼                                        │
               │    ┌──────────────┐                                │
               │    │    idle      │                                │
               │    └──────────────┘───────────────────────────────►│
               │                                                    │
               │    ┌──────────────┐                                │
               └───►│    error     │  (any stage can error)         │
                    └──────┬───────┘                                │
                           │  auto-recovers or phone reconnects     │
                           └────────────────────────────────────────┘
```

> **Note:** `cancel` is reserved for v2 and has no effect in the v1 state machine.

### Example Session

```
── Phone connects ──────────────────────────────────────────────────

Phone:    ws://<PC_IP>:8765?token=my_secret
Gateway:  {type:"connected", version:"1.0"}

── Voice interaction ───────────────────────────────────────────────

Phone:    {type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2}
Gateway:  {type:"status", status:"recording"}

Phone:    <binary frame: 4096 bytes of PCM>
Phone:    <binary frame: 4096 bytes of PCM>
Phone:    <binary frame: 4096 bytes of PCM>
...

Phone:    {type:"stop_audio"}
Gateway:  {type:"status", status:"transcribing"}
Gateway:  {type:"transcription", text:"What is the capital of France?"}
Gateway:  {type:"status", status:"thinking"}

                ── Gateway internally ──
                Gateway → OpenClaw :18789
                  {type:"req", method:"connect", params:{auth:{token:"..."}}}
                  {type:"req", method:"agent", params:{message:"What is the capital of France?", sessionKey:"agent:claw:g2"}}
                OpenClaw → Gateway
                  {type:"res", ok:true, payload:{runId:"run_abc", acceptedAt:"..."}}
                  {type:"event", event:"agent", payload:{stream:"assistant", delta:"The capital"}}
                  {type:"event", event:"agent", payload:{stream:"assistant", delta:" of France"}}
                  {type:"event", event:"agent", payload:{stream:"assistant", delta:" is Paris."}}
                  {type:"event", event:"agent", payload:{stream:"lifecycle", phase:"end"}}
                ── end internal ──

Gateway:  {type:"status", status:"streaming"}
Gateway:  {type:"assistant", delta:"The capital"}
Gateway:  {type:"assistant", delta:" of France"}
Gateway:  {type:"assistant", delta:" is Paris."}
Gateway:  {type:"end"}
Gateway:  {type:"status", status:"idle"}

── Text input (alternative) ────────────────────────────────────────

Phone:    {type:"text", message:"What is 2+2?"}
Gateway:  {type:"status", status:"thinking"}
Gateway:  {type:"status", status:"streaming"}
Gateway:  {type:"assistant", delta:"4"}
Gateway:  {type:"end"}
Gateway:  {type:"status", status:"idle"}

── Error case ──────────────────────────────────────────────────────

Phone:    {type:"stop_audio"}
Gateway:  {type:"status", status:"transcribing"}
Gateway:  {type:"error", detail:"Whisper failed: empty audio buffer", code:"TRANSCRIPTION_FAILED"}
Gateway:  {type:"status", status:"idle"}
```

---

## Appendix B: OpenClaw Wire Protocol Quick Reference

This is an **internal protocol** used by the PC Gateway to communicate with OpenClaw on `localhost:18789`. The phone never sees these frames.

### Connection Handshake

```json
→  {"type":"req", "id":"1", "method":"connect", "params":{"auth":{"token":"<OPENCLAW_GATEWAY_TOKEN>"}}}
←  {"type":"res", "id":"1", "ok":true, "payload":{...}}
```

### Sending a Message

```json
→  {"type":"req", "id":"2", "method":"agent", "params":{"message":"What is 2+2?", "sessionKey":"agent:claw:g2"}}
←  {"type":"res", "id":"2", "ok":true, "payload":{"runId":"run_abc123", "acceptedAt":"2026-02-22T10:00:00Z"}}
```

### Streaming Response Events

```json
←  {"type":"event", "event":"agent", "payload":{"stream":"assistant", "delta":"The answer"}}
←  {"type":"event", "event":"agent", "payload":{"stream":"assistant", "delta":" is 4."}}
←  {"type":"event", "event":"agent", "payload":{"stream":"lifecycle", "phase":"end"}}
```

### Alternative: Synchronous HTTP

```
POST http://localhost:18789/hooks/agent
Content-Type: application/json

{"message":"What is 2+2?", "sessionKey":"agent:claw:g2", "wait": true}
```

Returns the complete response in a single HTTP response (no streaming). Simpler but higher latency. Used for testing only.
