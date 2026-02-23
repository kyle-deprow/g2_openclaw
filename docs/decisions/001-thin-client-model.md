# ADR-001: Thin Client Model

**Date:** 2026-02-22
**Status:** Accepted

---

## Context

The G2 OpenClaw system connects Even Realities G2 AR smart glasses to an
OpenClaw AI assistant running on a personal computer over a local network.
The initial "naive" architecture had the iPhone app communicate directly
with both Whisper (for speech-to-text) and the OpenClaw Gateway — exposing
two separate ports on the PC and requiring the phone to manage auth tokens,
session keys, and audio buffering logic.

This approach creates a larger attack surface (two open ports), forces the
resource-constrained EvenHub WebView to handle complex orchestration, and
scatters session state across two processes with no single source of truth.

An alternative is a **thin-client** model: the iPhone app acts solely as a
transparent pipe between the G2 glasses (BLE) and a unified PC Gateway
(single WebSocket). All intelligence — speech recognition, AI relay,
session management, authentication — runs on the PC.

## Decision

We adopt the thin-client model. The iPhone forwards raw PCM audio chunks
from the G2 microphone to the PC Gateway as binary WebSocket frames and
receives JSON text frames (status updates, transcription results, streamed
assistant deltas) back over the same connection. The phone never talks to
Whisper or OpenClaw directly, never interprets audio, and holds no AI or
session state.

The PC Gateway (`server.py`, port 8765) is the single entry point. It:

- Receives raw PCM binary frames and accumulates them server-side.
- Transcribes via faster-whisper (in-process, localhost).
- Relays transcriptions to OpenClaw Gateway (localhost:18789).
- Streams response deltas back to the phone over the same WebSocket.
- Manages OpenClaw auth and session lifecycle internally.

## Consequences

**Benefits:**

- **Single port exposed** to the phone — port 8765 only. OpenClaw remains
  locked to localhost, eliminating an entire class of security concerns.
- **Simpler phone app** — the EvenHub WebView handles only BLE I/O,
  WebSocket forwarding, gesture events, and display rendering.
- **Centralised session state** — the Gateway owns `sessionKey`, `runId`,
  Whisper model lifecycle, and OpenClaw connection health.
- **Easier operational debugging** — all logs, errors, and timing data
  originate from a single Python process.

**Trade-offs:**

- All audio must be relayed through the phone. There is no direct
  glasses-to-PC path (BLE range limits this to the phone as intermediary).
- The PC Gateway becomes a single point of failure: if it crashes,
  the entire pipeline is down.
- Phone-side latency is added by the extra WebSocket hop, though on a
  local WiFi network this is negligible (~1–2 ms).
