# PC Gateway — Detailed Design

## 1. Overview

The PC Gateway is the brain of G2 OpenClaw. It is a unified Python WebSocket service that runs on the user's personal machine (developer workstation, laptop, or home server) and orchestrates the entire voice-to-response pipeline. The iPhone sends raw PCM audio or text commands over a single WebSocket connection; the Gateway transcribes audio locally via `faster-whisper`, forwards the resulting text to OpenClaw for AI processing, and streams response deltas back to the phone in real time. All intelligence, authentication, session state, and error recovery live here — the phone is deliberately kept stateless.

---

## 2. Module Architecture

```
gateway/
├── server.py          ← WebSocket server, connection management, frame routing
├── transcriber.py     ← Whisper integration wrapper
├── openclaw_client.py ← OpenClaw WebSocket client, auth, message relay
├── audio_buffer.py    ← PCM accumulation and numpy conversion
├── config.py          ← Configuration loading (env vars, .env file)
├── protocol.py        ← Frame type definitions, serialization helpers
├── pyproject.toml     ← Dependencies and project metadata
└── README.md          ← Quick-start guide
```

### 2.1 `server.py` — WebSocket Server & Frame Router

**Responsibility:** Accept a single WebSocket connection from the iPhone, classify incoming frames (binary = PCM audio, text = JSON command), drive the state machine, and dispatch work to the transcriber and OpenClaw client.

```python
# Public interface
async def main() -> None
    """Entry point. Loads config, initialises model, starts WS server."""

class GatewaySession:
    """Manages one phone connection's lifecycle."""

    async def handle_connection(self, ws: WebSocketServerProtocol) -> None
        """Top-level handler registered with websockets.serve()."""

    async def handle_text_frame(self, ws, message: str) -> None
        """Parse JSON, dispatch by 'type' field."""

    async def handle_binary_frame(self, ws, data: bytes) -> None
        """Append raw PCM bytes to audio_buffer."""

    async def send_status(self, ws, status: str) -> None
        """Send {type:'status', status:<status>} to phone."""

    async def send_error(self, ws, code: str, detail: str = "") -> None
        """Send {type:'error', code:<code>, detail:<detail>} to phone."""
```

**Dependencies:** `config`, `protocol`, `transcriber`, `openclaw_client`, `audio_buffer`

### 2.2 `transcriber.py` — Whisper Integration

**Responsibility:** Load the `faster-whisper` model once at startup and expose a single async-friendly transcription method that accepts a float32 numpy array and returns text.

```python
class Transcriber:
    def __init__(self, model_name: str, device: str, compute_type: str) -> None
        """Load the CTranslate2 model into memory."""

    async def transcribe(self, audio: np.ndarray, language: str = "en") -> str
        """Run inference in a thread pool executor; return transcription text.
           Raises TranscriptionError on failure or timeout."""
```

**Dependencies:** `faster-whisper`, `numpy`, `asyncio` (for `run_in_executor`)

### 2.3 `openclaw_client.py` — OpenClaw WebSocket Client

**Responsibility:** Maintain a persistent WebSocket connection to the OpenClaw Gateway on localhost, handle the auth handshake, send agent messages with proper request IDs and session keys, and yield streamed response deltas back to the caller.

```python
class OpenClawClient:
    def __init__(self, host: str, port: int, token: str) -> None
        """Store connection params. Connection is lazy."""

    async def ensure_connected(self) -> None
        """Connect and authenticate if not already connected.
           Reconnects with exponential backoff on failure."""

    async def send_message(
        self, text: str, session_key: str = "agent:claw:g2"
    ) -> AsyncIterator[str]:
        """Send an agent request and yield assistant delta strings
           as they arrive. Raises OpenClawError on agent errors."""

    async def close(self) -> None
        """Gracefully close the WebSocket connection."""
```

**Dependencies:** `websockets`, `config`, `protocol`

### 2.4 `audio_buffer.py` — PCM Accumulation & NumPy Conversion

**Responsibility:** Accumulate raw PCM byte chunks, enforce size limits, and convert to a float32 numpy array for Whisper consumption.

```python
class AudioBuffer:
    def __init__(self, sample_rate: int, channels: int, sample_width: int) -> None
        """Initialise with audio format params from start_audio."""

    def append(self, chunk: bytes) -> None
        """Append PCM bytes. Raises BufferOverflow if > MAX_DURATION."""

    def to_numpy(self) -> np.ndarray
        """Convert accumulated PCM bytes to float32 numpy array for Whisper."""

    def reset(self) -> None
        """Clear the buffer for the next recording."""

    @property
    def duration_seconds(self) -> float
        """Estimated duration based on accumulated bytes and format."""

    @property
    def is_empty(self) -> bool

    MAX_DURATION: float = 60.0  # seconds
```

**Dependencies:** `numpy`

### 2.5 `config.py` — Configuration

**Responsibility:** Load settings from environment variables and an optional `.env` file, validate required values, and expose them as typed attributes.

```python
@dataclass(frozen=True)
class GatewayConfig:
    gateway_host: str
    gateway_port: int
    gateway_token: str | None
    openclaw_host: str
    openclaw_port: int
    openclaw_gateway_token: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    agent_timeout: int
    log_level: str

def load_config() -> GatewayConfig
    """Read from env vars / .env. Raises if OPENCLAW_GATEWAY_TOKEN is missing."""
```

**Dependencies:** `python-dotenv`, `os` (stdlib)

### 2.6 `protocol.py` — Frame Definitions & Serialization

**Responsibility:** Define all JSON message types exchanged with the phone, provide serialization/deserialization helpers, and keep wire-format knowledge out of other modules.

```python
# --- Inbound (phone → gateway) ---
class StartAudio(TypedDict):
    type: Literal["start_audio"]
    sampleRate: int       # e.g. 16000
    channels: int         # e.g. 1
    sampleWidth: int      # e.g. 2 (bytes per sample)

class StopAudio(TypedDict):
    type: Literal["stop_audio"]

class TextMessage(TypedDict):
    type: Literal["text"]
    message: str

# --- Outbound (gateway → phone) ---
class StatusFrame(TypedDict):
    type: Literal["status"]
    status: Literal["loading", "recording", "transcribing", "thinking", "streaming", "idle"]

class TranscriptionFrame(TypedDict):
    type: Literal["transcription"]
    text: str

class AssistantDelta(TypedDict):
    type: Literal["assistant"]
    delta: str

class EndFrame(TypedDict):
    type: Literal["end"]

class ErrorFrame(TypedDict):
    type: Literal["error"]
    code: str
    detail: str

class ConnectedFrame(TypedDict):
    type: Literal["connected"]
    version: str  # = "1.0"

class PingFrame(TypedDict):
    type: Literal["ping"]

class PongFrame(TypedDict):
    type: Literal["pong"]

def parse_text_frame(raw: str) -> StartAudio | StopAudio | TextMessage | PongFrame
    """Parse a JSON text frame. Raises ProtocolError on invalid input."""

def serialize(frame: dict) -> str
    """JSON-serialize an outbound frame."""
```

**Dependencies:** `json` (stdlib), `typing` (stdlib)

---

## 3. State Machine

The Gateway tracks one `ConnectionState` per active phone connection. Every state transition sends a `{type:"status"}` frame to the phone so the app can update its UI.

```
  ┌──────────────────────────────────┐
  │            LOADING               │
  │                                  │
  │  Send {type:"status",            │
  │    status:"loading"} on entry    │
  │  Reject start_audio with error   │
  │  Whisper model loading...        │
  └───────────────┬──────────────────┘
                  │ model loaded
                  ▼
                    ┌─────────────────────────────────────────────────────────────────┐
                    │                                                                 │
                    │  ┌─────────────────────────────────────────────────────────┐    │
                    │  │                        ERROR                            │    │
                    │  │                                                         │    │
                    │  │  • Send {type:"error", code:..., detail:...} to phone   │    │
                    │  │  • Clean up (reset audio buffer, cancel pending tasks)  │    │
                    │  │  • Auto-transition to IDLE                              │    │
                    │  └─────────────────────┬───────────────────────────────────┘    │
                    │                        │ (automatic)                            │
                    │                        ▼                                        │
                ┌───┴────────────────────────────────────────────┐                    │
                │                                                │                    │
     ┌──────────┤                     IDLE                       │                    │
     │          │                                                │                    │
     │          │   {type:"status", status:"idle"} sent on entry │                    │
     │          └──────┬──────────────────────────┬──────────────┘                    │
     │                 │                          │                                   │
     │ on "text"       │ on "start_audio"         │ any failure                       │
     │ message         │                          │ in any state                      │
     │                 ▼                          │                                   │
     │    ┌────────────────────────┐              │                                   │
     │    │       RECORDING        │              │                                   │
     │    │                        │              │                                   │
     │    │  Send {status:         │              │                                   │
     │    │    "recording"}        │──────────────┼───────────────────────────────────┘
     │    │  Accumulate binary     │              │
     │    │  PCM frames            │              │
     │    └───────────┬────────────┘              │
     │                │                           │
     │                │ on "stop_audio"            │
     │                ▼                           │
     │    ┌────────────────────────┐              │
     │    │     TRANSCRIBING       │              │
     │    │                        │              │
     │    │  Send {status:         │              │
     │    │    "transcribing"}     │              │
     │    │  Convert PCM → numpy   │              │
     │    │  Run Whisper inference  │              │
     │    │  Send {type:           │              │
     │    │    "transcription"}    │              │
     │    └───────────┬────────────┘              │
     │                │                           │
     │                │ transcription complete     │
     │                ▼                           │
     │    ┌────────────────────────┐              │
     ├───►│       THINKING         │              │
     │    │                        │              │
     │    │  Send {status:         │              │
     │    │    "thinking"}         │              │
     │    │  Forward text to       │              │
     │    │  OpenClaw agent        │              │
     │    └───────────┬────────────┘              │
     │                │                           │
     │                │ first assistant delta      │
     │                ▼                           │
          ┌────────────────────────┐              │
          │       STREAMING        │              │
          │                        │              │
          │  Send {status:         │              │
          │    "streaming"}        │              │
          │  Forward each delta    │              │
          │  as {type:"assistant"} │              │
          └───────────┬────────────┘              │
                      │                           │
                      │ lifecycle:end             │
                      │ from OpenClaw             │
                      ▼                           │
            Send {type:"end"}                     │
            Transition → IDLE ────────────────────┘
```

### Transition Table

| From | Event | To | Phone Frame Sent |
|---|---|---|---|
| `loading` | Whisper model loaded | `idle` | `{type:"status", status:"idle"}` |
| `loading` | Receive `{type:"start_audio"}` | `loading` | `{type:"error", code:"INVALID_STATE", detail:"model loading"}` |
| `idle` | Receive `{type:"start_audio"}` (tap) | `recording` | `{type:"status", status:"recording"}` |
| `idle` | Receive `{type:"text", message:"..."}` | `thinking` | `{type:"status", status:"thinking"}` |
| `recording` | Receive binary frame | `recording` | (none — silent accumulation) |
| `recording` | Receive `{type:"stop_audio"}` (tap) | `transcribing` | `{type:"status", status:"transcribing"}` |
| `transcribing` | Whisper returns text | `thinking` | `{type:"transcription", text:"..."}` then `{type:"status", status:"thinking"}` |
| `thinking` | First assistant delta | `streaming` | `{type:"status", status:"streaming"}` then `{type:"assistant", delta:"..."}` |
| `streaming` | More deltas arrive | `streaming` | `{type:"assistant", delta:"..."}` |
| `streaming` | `lifecycle:end` from OpenClaw | `idle` | `{type:"end"}` then `{type:"status", status:"idle"}` |
| *any* | Receive `{type:"ping"}` | *(same)* | `{type:"pong"}` |
| *any* | Unrecoverable error | `error` → `idle` | `{type:"error", code:"...", detail:"..."}` then `{type:"status", status:"idle"}` |

> **Tap-to-toggle:** The phone uses tap-to-toggle for audio recording. The first tap sends `{type:"start_audio"}`, the second tap sends `{type:"stop_audio"}`. There are no hold/release events.

> **Ping/pong:** The Gateway sends `{type:"ping"}` every 30 seconds. The phone must respond with `{type:"pong"}` within 10 seconds or the connection is considered dead.

> **Note:** The `cancel` frame type is reserved for v2 and not handled in v1.

---

## 4. Audio Pipeline

### 4.1 End-to-End Flow

```
┌──────────┐   {type:"start_audio",     ┌──────────────┐
│          │    sampleRate:16000,        │              │
│  iPhone  │    channels:1,              │  PC Gateway  │
│  (thin   │    sampleWidth:2}           │              │
│  client) │ ──────────────────────────► │  1. Create   │
│          │                             │     AudioBuffer
│          │   binary frame (PCM bytes)  │              │
│          │ ──────────────────────────► │  2. Append   │
│          │   binary frame (PCM bytes)  │     chunks   │
│          │ ──────────────────────────► │              │
│          │   ... more binary frames    │              │
│          │ ──────────────────────────► │              │
│          │                             │              │
│          │   {type:"stop_audio"}       │  3. Finalize │
│          │ ──────────────────────────► │     buffer   │
│          │                             │              │
│          │   ◄── {status:              │ 4. to_numpy()│
│          │        "transcribing"}      │    → float32 │
│          │                             │    numpy     │
│          │                             │    array     │
│          │                             │              │
│          │                             │  5. Whisper  │
│          │                             │     inference│
│          │                             │              │
│          │   ◄── {type:               │  6. Return   │
│          │        "transcription",     │     text     │
│          │        text:"hello world"}  │              │
└──────────┘                             └──────────────┘
```

### 4.2 PCM Format Expectations

The G2 microphone produces audio via the `onMicData` callback as `Uint8Array` chunks. The expected format (and the default values if `start_audio` omits them):

| Parameter | Value | Notes |
|---|---|---|
| Sample rate | 16,000 Hz | Standard for speech recognition |
| Channels | 1 (mono) | G2 has a single microphone |
| Sample width | 2 bytes (16-bit) | Signed little-endian (PCM S16LE) |
| Byte rate | 32,000 bytes/sec | 16000 × 1 × 2 |

### 4.3 Buffer Size Limits

To prevent unbounded memory growth from a stuck recording:

- **Maximum duration:** 60 seconds
- **Maximum bytes:** 60 × 32,000 = 1,920,000 bytes (~1.83 MB)
- **Enforcement:** `AudioBuffer.append()` raises `BufferOverflow` if the accumulated size would exceed the limit
- **On overflow:** Gateway transitions to `error`, sends `{type:"error", code:"BUFFER_OVERFLOW"}`, resets buffer, returns to `idle`

### 4.4 PCM → NumPy Conversion

`faster-whisper` accepts numpy arrays directly. The Gateway converts accumulated PCM bytes to a float32 numpy array, bypassing WAV header construction entirely:

```python
def to_numpy(self) -> np.ndarray:
    """Convert accumulated PCM bytes to float32 numpy array for Whisper."""
    samples = np.frombuffer(bytes(self._buffer), dtype=np.int16)
    return samples.astype(np.float32) / 32768.0
```

This is a zero-copy-friendly operation that normalizes 16-bit signed integers to the `[-1.0, 1.0]` float range expected by Whisper. No WAV header construction is needed.

### 4.5 Whisper Model Selection

| Model | Size | Speed (CPU int8) | Use Case |
|---|---|---|---|
| `tiny.en` | 39M | ~0.5s for 5s audio | Ultra-low latency, lower accuracy |
| `base.en` | 74M | ~1.0s for 5s audio | **Default.** Best speed/accuracy tradeoff for English |
| `small.en` | 244M | ~3.0s for 5s audio | Higher accuracy, still reasonable latency |
| `small` | 244M | ~3.5s for 5s audio | Multilingual support |
| `medium.en` | 769M | ~8.0s for 5s audio | High accuracy, noticeable latency |

> Speed estimates are rough, on a modern x86 CPU with int8 quantization. GPU (`cuda` + `float16`) is 5–10× faster.

**Recommended defaults:**
- English-only users: `base.en` with `int8`
- Multilingual users: `small` with `int8`

### 4.6 Inference Parameters

```python
segments, _info = self._model.transcribe(
    audio_array,              # float32 numpy array from AudioBuffer.to_numpy()
    beam_size=1,              # greedy decoding — fastest
    best_of=1,                # no sampling alternatives
    temperature=0.0,          # deterministic
    condition_on_previous_text=False,  # each utterance is independent
    vad_filter=True,          # skip silence — faster inference
    vad_parameters=dict(
        min_silence_duration_ms=500,
    ),
    language="en",            # skip language detection for English-only models
)
transcription = " ".join(segment.text.strip() for segment in segments)
```

### 4.7 Error Handling

| Scenario | Detection | Action |
|---|---|---|
| Empty buffer on `stop_audio` | `audio_buffer.is_empty` | Skip Whisper, send error `TRANSCRIPTION_FAILED`, return to idle |
| Buffer overflow (>60s) | `BufferOverflow` exception in `append()` | Send error `BUFFER_OVERFLOW`, reset buffer, return to idle |
| Whisper inference timeout | `asyncio.wait_for()` with 30s timeout | Cancel task, send error `TRANSCRIPTION_FAILED`, return to idle |
| Whisper model failure | Exception from `faster-whisper` | Log traceback, send error `TRANSCRIPTION_FAILED`, return to idle |
| Whisper returns empty text | Transcription is whitespace-only | Send error `TRANSCRIPTION_FAILED` with detail "empty transcription", return to idle |

---

## 5. OpenClaw Integration

### 5.1 Connection Lifecycle

```
┌────────────────┐          ┌────────────────────────┐
│   PC Gateway   │          │  OpenClaw Gateway      │
│                │          │  localhost:18789        │
│                │          │                        │
│  [First agent  │  connect │                        │
│   message      │────────►│                        │
│   triggers     │          │                        │
│   lazy connect]│  auth    │                        │
│                │────────►│  {type:"req",           │
│                │          │   method:"connect",    │
│                │          │   params:{auth:{       │
│                │          │     token:"..."}}}     │
│                │          │                        │
│                │  ◄───────│  {ok:true, ...}        │
│                │          │                        │
│  [Authenticated│          │                        │
│   — ready]     │          │                        │
│                │  agent   │                        │
│                │────────►│  {type:"req",           │
│                │          │   method:"agent",      │
│                │          │   params:{             │
│                │          │     message:"hello",   │
│                │          │     sessionKey:         │
│                │          │       "agent:claw:g2"}} │
│                │          │                        │
│                │  ◄───────│  {ok:true, payload:{   │
│                │          │   runId, acceptedAt}}  │
│                │          │                        │
│                │  ◄───────│  {type:"event",        │
│                │          │   event:"agent",       │
│                │          │   payload:{stream:     │
│                │          │   "assistant",         │
│                │          │   delta:"The "}}       │
│                │  ◄───────│  ...more deltas...     │
│                │          │                        │
│                │  ◄───────│  {type:"event",        │
│                │          │   event:"agent",       │
│                │          │   payload:{stream:     │
│                │          │   "lifecycle",         │
│                │          │   phase:"end"}}        │
└────────────────┘          └────────────────────────┘
```

The connection is **lazy** — the Gateway only opens the WebSocket to OpenClaw when the first agent message needs to be sent. This avoids startup failures if OpenClaw is not yet running.

### 5.2 Auth Handshake

The first message after WebSocket connect must be the `connect` request:

```json
{
  "type": "req",
  "id": 1,
  "method": "connect",
  "params": {
    "auth": {
      "token": "<OPENCLAW_GATEWAY_TOKEN from env>"
    }
  }
}
```

Expected response:

```json
{
  "type": "res",
  "id": 1,
  "ok": true,
  "payload": { ... }
}
```

If `ok` is `false`, the Gateway logs the error, closes the connection, and reports `OPENCLAW_ERROR` to the phone.

### 5.3 Request ID Management

Each request sent to OpenClaw carries a unique `id` field. The Gateway uses a monotonically increasing integer counter, starting at `1` and incrementing per request. This counter resets when the connection is re-established (since request IDs only need to be unique within a single WebSocket session).

```python
self._next_id: int = 1

def _get_next_id(self) -> int:
    rid = self._next_id
    self._next_id += 1
    return rid
```

### 5.4 Sending Agent Messages

```json
{
  "type": "req",
  "id": 2,
  "method": "agent",
  "params": {
    "message": "What is the capital of France?",
    "sessionKey": "agent:claw:g2"
  }
}
```

### 5.5 Session Key Strategy

The Gateway uses a single, persistent session key: `"agent:claw:g2"`. This means:

- **Conversation continuity:** All messages within a session share context in OpenClaw's memory
- **Simplicity:** No session management logic needed on the phone side
- **Reset:** If the user wants a fresh conversation, a future protocol extension (e.g., `{type:"reset_session"}`) can generate a new session key

### 5.6 Receiving & Parsing Stream Events

OpenClaw streams responses as a series of events on the WebSocket:

| Event Payload | Meaning | Gateway Action |
|---|---|---|
| `{stream:"assistant", delta:"..."}` | Text chunk from the AI | Forward `{type:"assistant", delta:"..."}` to phone |
| `{stream:"lifecycle", phase:"end"}` | Agent run complete | Send `{type:"end"}` then `{type:"status", status:"idle"}` to phone |
| `{stream:"lifecycle", phase:"error", ...}` | Agent run errored | Send `{type:"error", code:"OPENCLAW_ERROR"}` to phone |
| `{stream:"tool", ...}` | Tool invocation (informational) | Optionally forward for display; otherwise ignore |

### 5.7 Reconnection Strategy

```
  Connection attempt fails or drops
          │
          ▼
  Wait 1s   ── fail ──► Wait 2s  ── fail ──► Wait 4s  ── fail ──► Wait 8s
          │                │                    │                    │
        success          success              success              success
          │                │                    │                    │
          ▼                ▼                    ▼                    ▼
    Re-authenticate   Re-authenticate    Re-authenticate    Re-authenticate
```

- **Base delay:** 1 second
- **Multiplier:** 2× per attempt
- **Max delay:** 30 seconds
- **Max attempts:** unlimited (Gateway keeps trying as long as the phone is connected)
- **Jitter:** ±20% random jitter on each delay to avoid thundering herd
- **Reset:** Backoff resets to 1s after a successful connection

### 5.8 Error Handling

| Error | Detection | Recovery | Phone Message |
|---|---|---|---|
| Connection refused | `ConnectionRefusedError` | Retry with backoff | `{type:"error", code:"OPENCLAW_ERROR", detail:"connection refused"}` |
| Auth rejected | `{ok:false}` response to `connect` | Log, close connection, return idle | `{type:"error", code:"OPENCLAW_ERROR", detail:"auth rejected"}` |
| Agent error event | `{stream:"lifecycle", phase:"error"}` | Return to idle | `{type:"error", code:"OPENCLAW_ERROR", detail:"agent error"}` |
| Unexpected disconnect | `ConnectionClosedError` | Clear connection state, retry on next message | `{type:"error", code:"OPENCLAW_ERROR", detail:"disconnected"}` |
| Malformed response | JSON decode failure | Log warning, ignore frame | (none — internal) |

---

## 6. Configuration

All configuration is loaded from environment variables, with optional `.env` file support via `python-dotenv`. The `.env` file is looked for in the working directory.

### Environment Variables

| Variable | Default | Required | Description |
|---|---|---|---|
| `GATEWAY_HOST` | `0.0.0.0` | No | Bind address for the WebSocket server |
| `GATEWAY_PORT` | `8765` | No | Listen port for the WebSocket server |
| `GATEWAY_TOKEN` | *(none)* | No | Shared secret for phone authentication via WebSocket query parameter (`?token=...`). If set, the phone must include the token in the connection URL. If unset, any connection is accepted. |
| `OPENCLAW_HOST` | `127.0.0.1` | No | OpenClaw Gateway host |
| `OPENCLAW_PORT` | `18789` | No | OpenClaw Gateway port |
| `OPENCLAW_GATEWAY_TOKEN` | *(none)* | **Yes** | Token for the OpenClaw auth handshake. Gateway will refuse to start without it. |
| `WHISPER_MODEL` | `base.en` | No | `faster-whisper` model name (see §4.5 for options) |
| `WHISPER_DEVICE` | `cpu` | No | Compute device: `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | No | Quantization type: `int8`, `float16`, or `float32` |
| `AGENT_TIMEOUT` | `120` | No | Maximum seconds for the full agent request cycle (thinking + streaming). Gateway sends timeout error if exceeded. |
| `LOG_LEVEL` | `INFO` | No | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Example `.env` File

```bash
# PC Gateway Configuration
OPENCLAW_GATEWAY_TOKEN=your-openclaw-gateway-token-here

# Optional overrides
# GATEWAY_PORT=8765
# GATEWAY_TOKEN=your-phone-auth-token
# WHISPER_MODEL=small.en
# WHISPER_DEVICE=cuda
# WHISPER_COMPUTE_TYPE=float16
# AGENT_TIMEOUT=120
# LOG_LEVEL=DEBUG
```

---

## 7. Error Handling Strategy

### 7.1 Error Classification

Errors fall into three categories:

1. **Recoverable — retry:** Transient failures (OpenClaw disconnect, network blip). Gateway retries automatically.
2. **Recoverable — return to idle:** Non-transient but non-fatal (empty buffer, Whisper timeout). Gateway notifies the phone and resets.
3. **Fatal:** Configuration errors at startup (missing `OPENCLAW_GATEWAY_TOKEN`). Gateway refuses to start.

### 7.2 Error Scenario Matrix

| # | Error | Detection | Recovery | Phone Message |
|--:|---|---|---|---|
| 1 | Empty audio buffer | `stop_audio` received with 0 bytes in buffer | Reset buffer, return to idle | `{type:"error", code:"TRANSCRIPTION_FAILED", detail:"empty audio"}` |
| 2 | Audio buffer overflow | `BufferOverflow` in `append()` | Reset buffer, return to idle | `{type:"error", code:"BUFFER_OVERFLOW"}` |
| 3 | Whisper inference timeout | `asyncio.wait_for()` exceeds 30s | Cancel inference task, return to idle | `{type:"error", code:"TRANSCRIPTION_FAILED", detail:"timeout"}` |
| 4 | Whisper model crash | Unhandled exception from `faster-whisper` | Log traceback, return to idle | `{type:"error", code:"TRANSCRIPTION_FAILED", detail:"model error"}` |
| 5 | Whisper empty result | Transcription is whitespace-only | Return to idle | `{type:"error", code:"TRANSCRIPTION_FAILED", detail:"empty transcription"}` |
| 6 | OpenClaw connection refused | `ConnectionRefusedError` | Exponential backoff retry | `{type:"error", code:"OPENCLAW_ERROR", detail:"connection refused"}` |
| 7 | OpenClaw auth rejected | `{ok:false}` in connect response | Log error, close WS, return to idle | `{type:"error", code:"OPENCLAW_ERROR", detail:"auth rejected"}` |
| 8 | OpenClaw agent error | Error event in stream | Return to idle | `{type:"error", code:"OPENCLAW_ERROR", detail:"agent error"}` |
| 9 | OpenClaw unexpected disconnect | `ConnectionClosedError` mid-stream | Attempt reconnect on next message | `{type:"error", code:"OPENCLAW_ERROR", detail:"disconnected"}` |
| 10 | Phone disconnect | WebSocket close event | Clean up: reset buffer, cancel pending tasks, await reconnect | *(no phone to send to)* |
| 11 | Invalid JSON frame | `json.JSONDecodeError` | Log warning, ignore frame | `{type:"error", code:"INVALID_FRAME", detail:"malformed JSON"}` |
| 12 | Unknown frame type | Valid JSON but unrecognized `type` | Log warning, ignore frame | `{type:"error", code:"INVALID_FRAME", detail:"unknown type"}` |
| 13 | Frame in wrong state | e.g., `stop_audio` while in `idle` | Log warning, ignore | `{type:"error", code:"INVALID_STATE", detail:"unexpected frame in current state"}` |
| 14 | Agent cycle timeout | Elapsed time > `AGENT_TIMEOUT` (default 120s) | Send error, return to idle | `{type:"error", code:"TIMEOUT", detail:"agent cycle exceeded 120s"}` |

### 7.3 Error Frame Format

All errors sent to the phone follow a consistent structure:

```json
{
  "type": "error",
  "code": "TRANSCRIPTION_FAILED",
  "detail": "Whisper inference timed out after 30s"
}
```

Error codes are a fixed enum:

| Code | Category |
|---|---|
| `TRANSCRIPTION_FAILED` | Audio/Whisper pipeline errors |
| `BUFFER_OVERFLOW` | Recording too long |
| `OPENCLAW_ERROR` | OpenClaw connection, auth, or agent errors |
| `INVALID_FRAME` | Phone sent an unparseable or unknown frame |
| `INVALID_STATE` | Phone sent a valid frame but in the wrong state |
| `TIMEOUT` | Agent cycle exceeded the configured time limit |
| `INTERNAL_ERROR` | Unexpected server-side failure |

---

## 8. Dependencies

```toml
[project]
name = "g2-openclaw-gateway"
version = "0.1.0"
description = "PC Gateway for G2 OpenClaw — transcription, AI relay, and session management"
requires-python = ">=3.12"
dependencies = [
    "websockets>=13.0",
    "faster-whisper>=1.0",
    "python-dotenv>=1.0",
    "numpy>=1.26",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.5",
]

[project.scripts]
g2-gateway = "gateway.server:main"
```

### Dependency Rationale

| Package | Why |
|---|---|
| `websockets` | Async WebSocket server and client. Lightweight, well-maintained, supports binary + text frames natively. |
| `faster-whisper` | CTranslate2-based Whisper implementation. 4× faster than OpenAI's `whisper` on CPU, supports int8 quantization, accepts file-like objects. |
| `python-dotenv` | Load `.env` files into `os.environ`. Zero-config, no magic. |
| `numpy` | Convert raw PCM bytes to float32 arrays for direct Whisper ingestion. Negligible conversion overhead. |
| `pytest` + `pytest-asyncio` | Async-native test framework for the Gateway's `asyncio` code. |
| `ruff` | Fast linter + formatter, consistent with the broader project toolchain. |

---

## 9. Startup Sequence

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         PC Gateway Startup                                   │
│                                                                              │
│  Step 1 ─── Load Configuration                                              │
│              │                                                               │
│              ├── Read .env file (if present)                                 │
│              ├── Read environment variables                                   │
│              ├── Validate OPENCLAW_GATEWAY_TOKEN is set                      │
│              └── Log effective config (redact tokens)                        │
│              │                                                               │
│  Step 2 ─── Start WebSocket Server (state = LOADING)                         │
│              │                                                               │
│              ├── Bind to {GATEWAY_HOST}:{GATEWAY_PORT}                       │
│              ├── Register connection handler                                 │
│              ├── Log "Gateway listening — ws://0.0.0.0:8765 (loading)"      │
│              └── Early connections receive {type:"status", status:"loading"} │
│              │                                                               │
│  Step 3 ─── Initialize Whisper Model                                         │
│              │                                                               │
│              ├── Load faster-whisper model: {WHISPER_MODEL}                  │
│              ├── Device: {WHISPER_DEVICE}, Compute: {WHISPER_COMPUTE_TYPE}   │
│              ├── ⏱  This may take 5-30s depending on model size              │
│              └── Log "Whisper model loaded: base.en (cpu/int8)"             │
│              │                                                               │
│  Step 4 ─── Transition to IDLE                                               │
│              │                                                               │
│              ├── Send {type:"status", status:"idle"} to connected phones     │
│              └── Log "Gateway ready — accepting commands"                    │
│              │                                                               │
│  Step 5 ─── Phone Connects                                                   │
│              │                                                               │
│              ├── (Optional) Validate GATEWAY_TOKEN via query parameter       │
│              │     ├── Phone connects to ws://host:port/?token=...           │
│              │     ├── Match query param against GATEWAY_TOKEN               │
│              │     └── Reject with 4001 close code if mismatch              │
│              ├── Send {type:"connected", version:"1.0"} to phone            │
│              ├── Initialize GatewaySession with current state               │
│              └── Log "Phone connected from <ip>:<port>"                     │
│              │                                                               │
│  Step 6 ─── Begin Frame Loop                                                 │
│              │                                                               │
│              ├── async for message in websocket:                             │
│              │     ├── Binary frame → handle_binary_frame()                  │
│              │     └── Text frame   → handle_text_frame()                   │
│              └── On disconnect → clean up, await reconnect                   │
│              │                                                               │
│  Step 7 ─── Lazy Connect to OpenClaw (on first agent message)               │
│              │                                                               │
│              ├── Open WebSocket to {OPENCLAW_HOST}:{OPENCLAW_PORT}           │
│              ├── Send connect + auth handshake                               │
│              ├── Verify {ok:true} response                                   │
│              └── Log "Connected to OpenClaw"                                │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Startup Log Output (example)

```
2026-02-22 10:30:00 INFO  Loading configuration...
2026-02-22 10:30:00 INFO  Config: host=0.0.0.0 port=8765 whisper=base.en device=cpu compute=int8
2026-02-22 10:30:00 INFO  Gateway listening — ws://0.0.0.0:8765 (loading)
2026-02-22 10:30:00 INFO  Loading Whisper model: base.en (cpu/int8)...
2026-02-22 10:30:06 INFO  Whisper model loaded in 6.2s
2026-02-22 10:30:06 INFO  Gateway ready — accepting commands (state=idle)
2026-02-22 10:30:15 INFO  Phone connected from 192.168.1.42:51823
2026-02-22 10:30:15 INFO  Session initialized (state=idle)
```

---

## 10. Single-Connection Model

The Gateway is designed for **exactly one phone connection at a time**. This is a deliberate simplification:

- **Why:** A G2 + iPhone + PC triple is a personal setup. There is no multi-user scenario.
- **Behavior:** If a second phone connects while one is active, the Gateway closes the existing connection and accepts the new one (last-writer-wins).
- **State isolation:** All session state (`AudioBuffer`, connection state, OpenClaw client) is scoped to the single `GatewaySession` instance. When the phone disconnects, all state is discarded.

```
Phone A connects     → Session created, state = idle
Phone A disconnects  → Session destroyed, all state cleared
Phone A reconnects   → Fresh session, no carryover

Phone A connected    → Active session
Phone B connects     → Phone A force-closed, new session for B
```

---

## 11. Threading Model

The Gateway is fully **async** (`asyncio`), running on a single event loop thread. The only blocking operation is Whisper inference, which is offloaded to a thread pool:

```
┌─────────────────────────────────────────────────────────────┐
│                     asyncio Event Loop                       │
│                     (single thread)                          │
│                                                             │
│  ┌─────────────────┐  ┌──────────────────┐  ┌───────────┐  │
│  │  WebSocket       │  │  OpenClaw WS     │  │  Status   │  │
│  │  Server          │  │  Client          │  │  Updates  │  │
│  │  (phone ↔ gw)    │  │  (gw ↔ openclaw) │  │           │  │
│  └─────────────────┘  └──────────────────┘  └───────────┘  │
│                                                             │
│           │ await run_in_executor(...)                       │
│           ▼                                                 │
│  ┌─────────────────────────────────────┐                    │
│  │  ThreadPoolExecutor (default)       │                    │
│  │                                     │                    │
│  │  ┌───────────────────────────────┐  │                    │
│  │  │  Whisper inference            │  │                    │
│  │  │  (CPU-bound, blocking)        │  │                    │
│  │  └───────────────────────────────┘  │                    │
│  └─────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

This ensures that:
- WebSocket I/O (both phone and OpenClaw) remains responsive during transcription
- Status messages can be sent while Whisper is processing
- No GIL contention issues since only one CPU-bound task runs at a time

> **Note:** The numpy PCM→float32 conversion (`AudioBuffer.to_numpy()`) is a negligible-cost operation (sub-millisecond for 60s of audio) and runs inline on the event loop — no thread pool offload is needed for it.
