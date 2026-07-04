# G2 OpenClaw

Bridges [Even Realities G2](https://www.evenrealities.com/) AR smart glasses to a local [OpenClaw](https://github.com/open-claw/open-claw) AI assistant via a PC gateway. Speak into the glasses, get AI responses rendered on the display — fully local, no cloud dependency. The iPhone acts as a transparent BLE-to-WebSocket pipe; all intelligence (Whisper STT, OpenClaw inference, session management) runs on the PC.

## Architecture

```
┌──────────┐   BLE    ┌──────────────┐  WebSocket   ┌──────────────┐  WebSocket  ┌──────────┐
│ G2       │ ◄──────► │ G2 App       │ ◄──────────► │ PC Gateway   │ ◄─────────► │ OpenClaw │
│ Glasses  │          │ (iPhone)     │  port 8765   │ (Python)     │  port 18789 │ (AI)     │
│ firmware │          │ TypeScript   │              │ Whisper STT  │             │ Node.js  │
└──────────┘          └──────────────┘              └──────────────┘             └──────────┘
```

Glasses display: 576×288 px, 4-bit greyscale micro-LED.

### Data Flow

1. User speaks → G2 mic captures audio
2. Audio streams BLE → iPhone → binary WebSocket (S16LE, 16kHz, mono) to Gateway
3. Gateway runs faster-whisper transcription (CUDA-accelerated or CPU)
4. User confirms/rejects transcription on glasses (tap / double-tap)
5. Confirmed text sent to OpenClaw for AI inference
6. OpenClaw streams response deltas back through Gateway
7. Gateway forwards text frames to G2 App
8. G2 App renders in reverse chronological order on glasses (newest first — no scroll API)

## Repository Structure

```
g2_openclaw/
├── gateway/               # PC Gateway — Python WebSocket server + Whisper STT
│   ├── server.py          # WebSocket server, session management, frame routing
│   ├── protocol.py        # Frame type definitions (9 inbound, 11 outbound)
│   ├── config.py          # Configuration from env vars / .env
│   ├── audio_buffer.py    # PCM accumulation, numpy conversion for Whisper
│   ├── transcriber.py     # faster-whisper async wrapper with VAD
│   ├── openclaw_client.py # WebSocket client with Ed25519 auth
│   ├── device_identity.py # Ed25519 keypair management for OpenClaw handshake
│   ├── session_resolver.py # Session metadata from OpenClaw store
│   ├── session_history.py # Conversation history from JSONL transcripts
│   ├── tts.py             # Text-to-speech via espeak-ng│   ├── otel_setup.py      # OpenTelemetry initialization + logging configuration
│   ├── metrics.py         # Custom application metrics (connections, durations, errors)│   ├── cli.py             # CLI: init-env, launch, stop, push-config
│   ├── agent_config/      # OpenClaw agent persona (SOUL.md, AGENTS.md, etc.)
│   └── openclaw_config/   # OpenClaw daemon config (provider, model, etc.)
├── g2_app/                # G2 App — TypeScript thin client for iPhone / G2 glasses
│   └── src/
│       ├── main.ts        # Boot-to-menu, frame routing, module wiring
│       ├── state.ts       # 10-state machine with validated transitions
│       ├── display.ts     # Dual-mode display (transcript + session menu)
│       ├── conversation.ts # History model, reverse chronological formatting
│       ├── input.ts       # Tap-to-toggle, double-tap menu, scroll throttling
│       ├── gateway.ts     # WebSocket client with auto-reconnect + jitter
│       ├── protocol.ts    # Frame types with runtime validation
│       └── utils.ts       # stripMarkdown() for display-safe text
├── infra/                 # Azure IaC — Bicep templates + Python CLI
│   ├── main.py            # Typer CLI: deploy, what-if, destroy, validate, lint
│   ├── main.bicep         # Root template
│   ├── modules/           # AI Hub, AI Services, OpenAI, KeyVault, Storage, Monitoring
│   └── parameters/        # Environment params (dev.bicepparam)
├── tests/
│   ├── gateway/           # 334 pytest tests (14 test files)
│   ├── integration/       # E2E tests (audio, OpenClaw, vertical slice)
│   └── mocks/             # Mock OpenClaw server
├── scripts/
│   ├── bootstrap.sh       # One-shot repo setup (prereqs, deps, config, tests)
│   └── push-openclaw-config.sh  # Merge agent/provider config into ~/.openclaw/
├── docs/                  # Design docs, guides, ADRs, reference material
├── docker-compose.otel.yml  # OTel observability stack (collector, Jaeger, Prometheus, Loki, Grafana)
├── otel/                    # OTel collector, Grafana, Prometheus configuration
├── Makefile               # 25+ targets for build, test, lint, sim, infra
└── pyproject.toml         # Root config (Python 3.13+, uv-managed)
```

## Components

### PC Gateway (`gateway/`)

Python WebSocket server that accepts G2 App connections, runs Whisper transcription, communicates with OpenClaw, and streams responses back.

| Module | Purpose |
|--------|---------|
| `server.py` | WebSocket server, session management, session menu handlers, frame routing |
| `protocol.py` | Frame definitions (9 inbound, 11 outbound), session menu types, error codes |
| `config.py` | Configuration via `.env` and environment variables |
| `audio_buffer.py` | PCM validation (16-bit, 8–48kHz), 60s/5MB cap, numpy conversion |
| `transcriber.py` | faster-whisper async wrapper with VAD (CUDA or CPU) |
| `openclaw_client.py` | WebSocket client with Ed25519 challenge/response auth |
| `device_identity.py` | Ed25519 keypair generation and management |
| `session_resolver.py` | Session metadata resolution from OpenClaw local store |
| `session_history.py` | Conversation history from JSONL transcript files |
| `tts.py` | Text-to-speech via espeak-ng |
| `otel_setup.py` | OpenTelemetry initialization, logging configuration, graceful degradation |
| `metrics.py` | Custom application metrics (connections, durations, errors) |
| `cli.py` | CLI commands: `init-env`, `launch`, `stop`, `push-config` |

**Server-side state machine:** `IDLE → RECORDING → TRANSCRIBING → IDLE (confirmation) → THINKING → STREAMING → IDLE`

**Key features:**

- Whisper transcription via faster-whisper (CUDA or CPU); startup fails if the model cannot load
- OpenClaw integration with Ed25519 auth; `OPENCLAW_GATEWAY_TOKEN` is required
- Session management: list, switch, create; daily auto-reset on date rollover
- Inflight response buffering: captures deltas during disconnect, replays on reconnect (200KB cap, 5min TTL)
- Auth: first-message HMAC token handshake, rate limiting (5 failures/60s/IP), weak-token rejection
- Health endpoint: `/healthz` → HTTP 200
- Local audio capture mode (`--local-audio`)
- CUDA library pre-loading for GPU inference

### G2 App (`g2_app/`)

TypeScript thin client running on iPhone via EvenHub. Bridges G2 glasses (BLE) to the PC Gateway (WebSocket).

| Module | Purpose |
|--------|---------|
| `main.ts` | Boot-to-menu flow, frame routing, module wiring |
| `state.ts` | 10-state machine with validated transitions and change callbacks |
| `display.ts` | Dual-mode display manager (transcript mode + session menu mode) |
| `conversation.ts` | History model, `formatReverse()`, `removeLastUser()` via splice |
| `input.ts` | Tap-to-toggle recording, double-tap menu, menu tap navigation |
| `gateway.ts` | WebSocket client with auto-reconnect (1s→30s backoff, ±20% jitter) |
| `protocol.ts` | Frame types with runtime validation |
| `utils.ts` | `stripMarkdown()` for display-safe text |

**State machine (10 states):** `LOADING → MENU → IDLE → RECORDING → TRANSCRIBING → CONFIRMING → THINKING → STREAMING → IDLE` (+ `ERROR`, `DISCONNECTED` reachable from most states)

**Key features:**

- Boots to session menu (session picker), not idle
- Session menu via `ListContainerProperty`: tap to select/create, double-tap to go back
- Reverse chronological display — newest messages at top
- Streaming delta display with 100ms debounced batching
- Display layout: 576×288 canvas — status bar (y=2, 24px), content (y=34, 212px), footer (y=256, 26px)

### Coding Tasks

Coding tasks (code generation, refactoring, etc.) run through OpenClaw's Codex
runtime and subagent model. Authenticate once with
`openclaw models auth login --provider openai`, then push the repo-managed
OpenClaw config.

### Infrastructure (`infra/`)

Azure Bicep templates + Python Typer CLI for deploying AI resources (AI Hub, AI Services, OpenAI, KeyVault, Storage, Monitoring).

```bash
uv run azure-infra-cli deploy --env dev    # Deploy
uv run azure-infra-cli what-if --env dev   # Preview changes
uv run azure-infra-cli destroy --env dev   # Tear down
```

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | ≥ 3.13 | Gateway, Infra CLI |
| **Node.js** | ≥ 22 | G2 App |
| **uv** | latest | Python package/env manager (**not** pip/poetry) |
| **npm** | latest | Node.js package manager |

**Optional:** Azure CLI (infra deployment), EvenHub CLI + Simulator (G2 app dev/testing), OpenClaw (AI backend).

Dependencies are declared in [pyproject.toml](pyproject.toml) (Python) and per-component `package.json` files. Use `uv sync` and `npm install` respectively — lockfiles handle pinning.

## Quick Start

### Automated Setup

```bash
./scripts/bootstrap.sh   # Install deps, generate .env, run smoke tests
```

### Manual Setup

```bash
# 1. Install Python dependencies
uv sync                    # runtime deps (Whisper, CUDA, gateway)
uv sync --extra dev        # + dev tools (ruff, pytest, mypy)

# 2. Generate .env with GPU detection + model selection
uv run python -m gateway init-env

# 3. Push OpenClaw agent/provider config
bash scripts/push-openclaw-config.sh

# 4. Install G2 App dependencies
cd g2_app && npm install && cd ..
```

MemPalace is a strict startup dependency for OpenClaw research memory. Cold
start, config push, and the CLI launcher validate that the active palace uses
the configured local embedding model and a cosine Chroma index. The expected
runtime cache is durable, not `/tmp`:

```bash
export FASTEMBED_CACHE_PATH="$HOME/.cache/fastembed"
make mempalace-health
```

### Running

```bash
# Start everything (gateway + Vite dev server + simulator)
make sim

# Or start the gateway alone
uv run python -m gateway

# Or use the CLI launcher
uv run python -m gateway launch              # gateway + vite + simulator
uv run python -m gateway launch --no-simulator  # gateway + vite only
```

The gateway listens on `ws://127.0.0.1:8765`. Clients must send
`{"type":"auth","token":"<GATEWAY_TOKEN>"}` as the first WebSocket frame.

To push OpenClaw config changes:

```bash
make push-config                # or: bash scripts/push-openclaw-config.sh
uv run python -m gateway push-config  # push + restart daemon
```

## Development

### Testing

```bash
# All tests
make test

# By component
uv run pytest tests/gateway/ -v          # 334 tests
cd g2_app && npm test                     # 227 tests
```

**Total: ~561 tests** across the monorepo.

### Linting & Formatting

```bash
make lint       # Lint all components
make format     # Format all components
```

Or individually:

```bash
uv run ruff check .                       # Python lint
uv run ruff format .                      # Python format
```

### Type Checking

```bash
make typecheck

# Or individually
uv run mypy gateway/ infra/               # Python (strict mode)
cd g2_app && npm run typecheck            # TypeScript
```

### Pre-commit Hooks

```bash
uv run pre-commit install                 # Install hooks
uv run pre-commit run --all-files         # Run manually
```

Configured hooks: ruff (lint + format), mypy, detect-secrets.

### Sim Stack

`make sim` is the primary dev workflow command. It kills any running services, then starts:

1. PC Gateway (`uv run python -m gateway`)
2. Vite dev server (`npm run dev` in `g2_app/`)
3. EvenHub Simulator pointed at the Vite server

| Target | Description |
|--------|-------------|
| `make sim` | Kill all + restart gateway + Vite + simulator |
| `make stop` | Kill all running services |
| `make test` | Run all tests (gateway + G2) |
| `make lint` | Lint all components |
| `make format` | Format all components |
| `make typecheck` | Type-check all components |
| `make cold-start` | Full setup: deps, env, security, smoke tests |
| `make push-config` | Push OpenClaw config to `~/.openclaw/` |
| `make mempalace-health` | Validate MemPalace embedding and index invariants |
| `make clean` | Remove caches, dist/, logs/, node_modules/ |
| `make sim-lite` | Start gateway only (no OTel Docker stack) |
| `make otel-up` | Start OTel observability Docker services |
| `make otel-down` | Stop OTel observability Docker services |
| `make otel-status` | Check OTel service health |
| `make help` | Show all targets |

## Observability

The gateway integrates OpenTelemetry for traces, metrics, and log export. By default, `make sim` starts the full observability stack.

### OTel Stack

```bash
make sim          # Start OTel stack + gateway + Vite + simulator
make sim-lite     # Start gateway only (no OTel stack)
make otel-up      # Start just the OTel Docker services
make otel-down    # Stop the OTel Docker services
make otel-status  # Check OTel service health
```

Five Docker services run via `docker-compose.otel.yml`:

| Service | Port | Purpose |
|---------|------|---------|
| OTel Collector | 4317 (gRPC), 4318 (HTTP) | Receives OTLP telemetry, routes to backends |
| Jaeger | 16686 | Distributed trace viewer |
| Prometheus | 9090 | Metrics storage and querying |
| Loki | 3100 | Log aggregation |
| Grafana | 3000 | Dashboards (auto-provisions all datasources) |

Default Grafana credentials: `admin` / `admin`.

### What's Instrumented

**Traces** (visible in Jaeger at `http://localhost:16686`):
- `gateway.ws_connection` — Full WebSocket session lifecycle
- `gateway.handle_text` — Text message processing
- `gateway.transcribe_audio` — Audio transcription pipeline
- `gateway.inflight_stream` — Background OpenClaw streaming
- `openclaw.connect` — OpenClaw auth handshake
- `openclaw.send_message_init` — OpenClaw agent request initiation
- `whisper.transcribe` — Whisper model inference
- `tts.synthesize` — Text-to-speech synthesis

**Metrics** (visible in Prometheus at `http://localhost:9090`):
- `gateway.ws.connections_active` — Active WebSocket connections (UpDownCounter)
- `gateway.transcription.duration_seconds` — Whisper transcription time (Histogram)
- `gateway.openclaw.request_duration_seconds` — OpenClaw request time (Histogram)
- `gateway.openclaw.errors_total` — OpenClaw error count (Counter)

**Logs** (visible in Loki via Grafana):
All stdlib `logging` calls are bridged to OTel via `LoggingInstrumentor`. Log records include trace context (trace_id, span_id) for correlation.

### Disabling OTel

Set `OTEL_EXPORTER_OTLP_ENDPOINT=none` in `.env` or environment to disable all telemetry export. The gateway falls back to console + `RotatingFileHandler` (logs/gateway.log).

## Configuration

### Gateway Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `127.0.0.1` | Bind address |
| `GATEWAY_PORT` | `8765` | Bind port |
| `GATEWAY_TOKEN` | — | Required G2 app auth token |
| `WHISPER_MODEL` | `base.en` | faster-whisper model |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | Inference precision |
| `OPENCLAW_HOST` | `127.0.0.1` | OpenClaw host |
| `OPENCLAW_PORT` | `18789` | OpenClaw port |
| `OPENCLAW_GATEWAY_TOKEN` | — | Required OpenClaw gateway auth token |
| `AGENT_TIMEOUT` | `120` | Max seconds for AI response |
| `AUTH_TIMEOUT` | `5.0` | Auth handshake timeout |
| `ALLOWED_ORIGINS` | — | Comma-separated origins |
| `G2_LOCAL_AUDIO` | `false` | Use local mic instead of WebSocket audio |
| `HISTORY_LIMIT` | `10` | History entries sent on connect |
| `OPENCLAW_AGENT_ID` | `main` | OpenClaw agent ID used for G2 sessions |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP HTTP endpoint. Set to `none` or empty to disable OTel |

### G2 App Gateway URL

Resolved in priority order:

1. URL hash: `http://app-url#ws://gateway:8765?token=xxx`
2. Query parameter: `?gateway=ws://gateway:8765?token=xxx`
3. `localStorage` key: `gateway_url`
4. Build-time environment variable

## WebSocket Protocol

Binary + JSON protocol between Gateway and G2 App.

**Authentication:** first client frame must be `{"type":"auth","token":"..."}` when `GATEWAY_TOKEN` is configured. The G2 app may receive the token in its URL, but it strips the token before opening the WebSocket and sends this auth frame. Single connection at a time — new connections replace existing ones.

### Client → Gateway (9 frame types)

| Frame | Format | Purpose |
|-------|--------|---------|
| `start_audio` | JSON | Begin audio recording session |
| `stop_audio` | JSON | End audio recording session |
| `text` | JSON | Send text message (bypass speech) |
| `pong` | JSON | Respond to server ping |
| `status_request` | JSON | Request current status |
| `reset_session` | JSON | Reset the current session |
| `session_list_request` | JSON | Request available sessions |
| `session_switch` | JSON | Switch to a different session |
| `session_create` | JSON | Create a new session |
| *(binary)* | S16LE PCM | Raw audio (16kHz, mono) |

### Gateway → Client (11 frame types)

| Frame | Format | Purpose |
|-------|--------|---------|
| `connected` | JSON | Auth success + capabilities + session ID |
| `status` | JSON | State machine transition |
| `transcription` | JSON | Whisper transcription result |
| `assistant` | JSON | Streamed AI response delta |
| `end` | JSON | AI response complete |
| `error` | JSON | Error with code and message |
| `ping` | JSON | Keepalive |
| `history` | JSON | Conversation history on connect |
| `session_reset` | JSON | Session reset confirmation |
| `session_list` | JSON | Available sessions |
| `session_switched` | JSON | Session switch confirmation |

### Error Codes

`AUTH_FAILED` · `TRANSCRIPTION_FAILED` · `BUFFER_OVERFLOW` · `OPENCLAW_ERROR` · `INVALID_FRAME` · `INVALID_STATE` · `TIMEOUT` · `INTERNAL_ERROR`

## Documentation

Deep-dive docs live in `docs/` — see [docs/README.md](docs/README.md) for the full index.

| Directory | Contents |
|-----------|----------|
| [docs/design/](docs/design/) | Architecture, protocol, gateway, G2 app, display layouts |
| [docs/guides/](docs/guides/) | Getting started, development workflow |
| [docs/reference/](docs/reference/) | OpenClaw internals, G2 SDK/hardware reference |
| [docs/decisions/](docs/decisions/) | Architecture Decision Records (ADRs) |
| [docs/implementation/](docs/implementation/) | Implementation plans and feature specs |

## License

See repository for license details.
