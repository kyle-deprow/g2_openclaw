# WebSocket Protocol Specification

> **Canonical reference.** This document is the single source of truth for the
> wire protocols used by the G2 OpenClaw system. It supersedes protocol details
> previously scattered across `architecture.md` (Appendix A/B),
> `gateway.md` (§2.6), and the gateway README.

---

## 1. Phone ↔ Gateway Protocol

The phone (G2 thin client) maintains a single WebSocket connection to the PC
Gateway at:

```
ws://<PC_IP>:8765?token=<shared_secret>
```

All control messages are **JSON text frames**. Audio is sent as **binary
WebSocket frames** (raw PCM — no JSON wrapping, no base64).

### 1.1 Inbound Frames (Phone → Gateway)

#### Binary Frames — Raw PCM Audio

Each microphone chunk is forwarded as a binary WebSocket frame. See
[§7 — PCM Audio Format](#7-pcm-audio-format) for encoding details.

#### JSON Text Frames

| `type` | Required Fields | Description |
|---|---|---|
| `start_audio` | `sampleRate` (int), `channels` (int), `sampleWidth` (int) | Begin audio streaming. Gateway starts accumulating PCM. |
| `stop_audio` | *(none)* | End of speech. Gateway finalises buffer and begins transcription. |
| `text` | `message` (string) | Text input — skips transcription, sent directly to OpenClaw. |
| `pong` | *(none)* | Response to a gateway `ping`. Must arrive within 10 s. |

**Examples:**

```json
{"type":"start_audio","sampleRate":16000,"channels":1,"sampleWidth":2}
{"type":"stop_audio"}
{"type":"text","message":"What is the capital of France?"}
{"type":"pong"}
```

### 1.2 Outbound Frames (Gateway → Phone)

All outbound frames are **JSON text frames**.

| `type` | Required Fields | Description |
|---|---|---|
| `connected` | `version` (string) | Sent immediately after successful authentication. |
| `status` | `status` (string — see [§4](#4-status-states)) | State-machine update. |
| `transcription` | `text` (string) | Transcribed user speech (after Whisper completes). |
| `assistant` | `delta` (string) | Streamed chunk of the AI assistant's response. Accumulate to build the full answer. |
| `end` | *(none)* | All `assistant` deltas have been sent; response cycle complete. |
| `error` | `detail` (string), `code` (string — see [§5](#5-error-codes)) | An error occurred. Always transitions session to idle. |
| `ping` | *(none)* | Heartbeat probe. Phone must reply with `pong` within 10 s. |

**Examples:**

```json
{"type":"connected","version":"1.0"}
{"type":"status","status":"recording"}
{"type":"transcription","text":"What is the capital of France?"}
{"type":"assistant","delta":"The capital"}
{"type":"assistant","delta":" of France is Paris."}
{"type":"end"}
{"type":"error","detail":"Whisper failed: empty audio buffer","code":"TRANSCRIPTION_FAILED"}
{"type":"ping"}
```

---

## 2. Authentication

| Aspect | Detail |
|---|---|
| Mechanism | Query-string token: `?token=<secret>` |
| Server config | `GATEWAY_TOKEN` env var. When unset, auth is disabled. |
| Validation | Constant-time compare (`hmac.compare_digest`). |
| Rejection | WebSocket closed with code **4001** and reason `"Unauthorized"`. |

On success the gateway sends a `connected` frame:

```json
{"type":"connected","version":"1.0"}
```

---

## 3. Connection Model

- **Single connection per gateway.** Only one phone may be connected at a
  time. A new connection **replaces** the previous one (the old socket is
  closed).
- The phone opens the WebSocket on app launch and keeps it alive for the
  duration of the session.
- All communication is full-duplex over this single socket.

---

## 4. Status States

The gateway tracks session state and sends `status` frames on every
transition so the phone can update its UI.

| State | Meaning |
|---|---|
| `loading` | Whisper model is initialising. `start_audio` is rejected. |
| `idle` | Ready for a new voice or text interaction. |
| `recording` | Phone is streaming PCM audio chunks. |
| `transcribing` | Whisper inference in progress. |
| `thinking` | Waiting for OpenClaw to begin responding. |
| `streaming` | Relaying `assistant` delta frames to the phone. |

**State machine:**

```
loading ──(model ready)──► idle ◄────────────────────────────┐
                            │                                 │
              ┌─────────────┼──────────────┐                  │
              │ start_audio │ text         │                  │
              ▼             │              │                  │
          recording         │              │                  │
              │             │              │                  │
          stop_audio        │              │                  │
              ▼             │              │                  │
         transcribing       │              │                  │
              │             │              │                  │
              ▼             ▼              │                  │
           thinking ◄───────┘              │                  │
              │                            │                  │
         first delta                       │                  │
              ▼                            │                  │
          streaming                        │                  │
              │                            │                  │
          lifecycle:end                    │                  │
              └────────────────────────────┘                  │
                                                              │
          (any state) ──► error ──(auto)──► idle ─────────────┘
```

### Invariants

- A `status` frame always precedes its corresponding data frames (e.g.
  `status:"transcribing"` before `transcription`; `status:"streaming"` before
  the first `assistant` delta).
- An `end` frame is always sent after all `assistant` deltas.
- An `error` frame may arrive at any point and always transitions to `idle`.
- Frame delivery order matches send order (TCP/WebSocket guarantee).

---

## 5. Error Codes

| Code | When |
|---|---|
| `AUTH_FAILED` | Invalid or missing authentication token. |
| `TRANSCRIPTION_FAILED` | Whisper transcription failed (e.g. empty buffer). |
| `BUFFER_OVERFLOW` | Audio buffer exceeded maximum size. |
| `OPENCLAW_ERROR` | OpenClaw returned an error or is unreachable. |
| `INVALID_FRAME` | Malformed JSON or unknown frame type received. |
| `INVALID_STATE` | Frame is valid but not allowed in the current state (e.g. `stop_audio` while idle). |
| `TIMEOUT` | An operation (transcription, OpenClaw response) timed out. |
| `INTERNAL_ERROR` | Catch-all for unexpected server errors. |

---

## 6. Heartbeat

| Parameter | Value |
|---|---|
| Direction | Gateway → Phone (`ping`), Phone → Gateway (`pong`) |
| Interval | Every **30 seconds** |
| Timeout | Phone must respond within **10 seconds** |
| Consequence | Failure to respond may result in disconnection. |

```
Gateway:  {"type":"ping"}
Phone:    {"type":"pong"}
```

---

## 7. PCM Audio Format

Binary WebSocket frames carry raw PCM audio with the following encoding:

| Parameter | Value |
|---|---|
| Encoding | Signed 16-bit little-endian (S16LE) |
| Sample rate | 16 000 Hz |
| Channels | 1 (mono) |
| Sample width | 2 bytes |
| Framing | Each `onMicData` chunk is one binary WebSocket frame |

The `start_audio` control frame echoes these parameters so both sides can
verify agreement:

```json
{"type":"start_audio","sampleRate":16000,"channels":1,"sampleWidth":2}
```

> **Future:** All binary frames in v1 are raw PCM audio. Future binary types
> will use a 1-byte type prefix to discriminate.

---

## 8. Gateway ↔ OpenClaw Protocol

This is an **internal protocol** between the PC Gateway and OpenClaw running on
`localhost:18789`. The phone never sees these frames.

### 8.1 Connection & Authentication

The gateway connects via WebSocket and authenticates with a `connect` method
call. Request IDs are monotonically increasing integers.

```json
→  {"type":"req","id":1,"method":"connect","params":{"auth":{"token":"<OPENCLAW_GATEWAY_TOKEN>"}}}
←  {"type":"res","id":1,"ok":true,"payload":{…}}
```

| Config | Env Var | Default |
|---|---|---|
| OpenClaw host | `OPENCLAW_HOST` | `localhost` |
| OpenClaw port | `OPENCLAW_PORT` | `18789` |
| Auth token | `OPENCLAW_GATEWAY_TOKEN` | `None` |

### 8.2 Agent Requests

The gateway sends an `agent` method request with the user's message and a
stable session key:

```json
→  {"type":"req","id":2,"method":"agent","params":{"message":"What is 2+2?","sessionKey":"agent:claw:g2"}}
←  {"type":"res","id":2,"ok":true,"payload":{"runId":"run_abc123","acceptedAt":"2026-02-22T10:00:00Z"}}
```

### 8.3 Streaming Response Events

After acceptance, OpenClaw streams events. The gateway listens for
`event`-type messages where `event` is `"agent"`:

```json
←  {"type":"event","event":"agent","payload":{"stream":"assistant","delta":"The answer"}}
←  {"type":"event","event":"agent","payload":{"stream":"assistant","delta":" is 4."}}
←  {"type":"event","event":"agent","payload":{"stream":"lifecycle","phase":"end"}}
```

| `stream` | `payload` fields | Description |
|---|---|---|
| `assistant` | `delta` (string) | A chunk of the assistant response. Forwarded to the phone as an `assistant` frame. |
| `lifecycle` | `phase` (string) | Lifecycle signal. `phase:"end"` ends the response. `phase:"error"` carries an `error` field. |
| *(other)* | — | Tool calls and other streams are ignored by the gateway. |

> **Note:** Lifecycle events use the `phase` field (not `status`) to avoid
> confusion with the phone-protocol `status` frames.

### 8.4 Error Handling

- If `ok` is `false` in a `res` frame, the `error` field contains the reason.
- A `lifecycle` event with `phase:"error"` includes an `error` string in the
  payload.
- A closed WebSocket before `lifecycle:end` is treated as a connection failure.

### 8.5 Synchronous HTTP (Testing Only)

For testing, OpenClaw also exposes a synchronous HTTP endpoint:

```
POST http://localhost:18789/hooks/agent
Content-Type: application/json

{"message":"What is 2+2?","sessionKey":"agent:claw:g2","wait":true}
```

Returns the complete response in a single HTTP response (no streaming). Higher
latency — used only for integration tests.

---

## Example Session (Full)

```
── Phone connects ──────────────────────────────────────────────

Phone:    ws://192.168.1.42:8765?token=my_secret
Gateway:  {"type":"connected","version":"1.0"}

── Voice interaction ───────────────────────────────────────────

Phone:    {"type":"start_audio","sampleRate":16000,"channels":1,"sampleWidth":2}
Gateway:  {"type":"status","status":"recording"}

Phone:    <binary: 4096 bytes PCM>
Phone:    <binary: 4096 bytes PCM>
Phone:    <binary: 4096 bytes PCM>

Phone:    {"type":"stop_audio"}
Gateway:  {"type":"status","status":"transcribing"}
Gateway:  {"type":"transcription","text":"What is the capital of France?"}
Gateway:  {"type":"status","status":"thinking"}
Gateway:  {"type":"status","status":"streaming"}
Gateway:  {"type":"assistant","delta":"The capital"}
Gateway:  {"type":"assistant","delta":" of France"}
Gateway:  {"type":"assistant","delta":" is Paris."}
Gateway:  {"type":"end"}
Gateway:  {"type":"status","status":"idle"}

── Text input (alternative) ────────────────────────────────────

Phone:    {"type":"text","message":"What is 2+2?"}
Gateway:  {"type":"status","status":"thinking"}
Gateway:  {"type":"status","status":"streaming"}
Gateway:  {"type":"assistant","delta":"4"}
Gateway:  {"type":"end"}
Gateway:  {"type":"status","status":"idle"}

── Heartbeat ───────────────────────────────────────────────────

Gateway:  {"type":"ping"}
Phone:    {"type":"pong"}

── Error case ──────────────────────────────────────────────────

Phone:    {"type":"stop_audio"}
Gateway:  {"type":"error","detail":"Cannot stop audio — not recording","code":"INVALID_STATE"}
Gateway:  {"type":"status","status":"idle"}
```
