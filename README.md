# G2 OpenClaw

Bridges [Even Realities G2](https://www.evenrealities.com/) AR smart glasses to a local [OpenClaw](https://github.com/open-claw/open-claw) AI assistant via a PC gateway. Speak into the glasses, get AI responses rendered on the display — fully local, no cloud dependency. The iPhone acts as a transparent BLE-to-WebSocket pipe; all intelligence (Whisper STT, OpenClaw inference, session management) runs on the PC.

## Architecture

```
┌──────────┐   BLE    ┌──────────────┐  WebSocket   ┌──────────────┐  WebSocket  ┌──────────┐
│ G2       │ ◄──────► │ G2 App       │ ◄──────────► │ PC Gateway   │ ◄─────────► │ OpenClaw │
│ Glasses  │          │ (iPhone)     │  port 8765   │ (Python)     │  port 18789 │ (AI)     │
│ firmware │          │ TypeScript   │              │ Whisper STT  │             │ Node.js  │
└──────────┘          └──────────────┘              └──────────────┘             └──────────┘
                                                           │
                                                     ┌─────┴──────┐
                                                     │  Copilot   │
                                                     │  Bridge    │
                                                     │ (MCP/Plugin)│
                                                     └────────────┘
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
│   ├── tts.py             # Text-to-speech via espeak-ng
│   ├── cli.py             # CLI: init-env, launch, stop, push-config
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
├── copilot_bridge/        # GitHub Copilot ↔ OpenClaw bridge
│   └── src/
│       ├── client.ts      # Copilot SDK wrapper, session pool (LRU, max 8)
│       ├── mcp-server.ts  # MCP: exposes Copilot to OpenClaw
│       ├── mcp-openclaw.ts # MCP: exposes OpenClaw memory to Copilot
│       ├── plugin.ts      # Native OpenClaw plugin (alternative to MCP)
│       ├── hooks.ts       # Permission gates, secret redaction, audit logging
│       ├── config.ts      # Bridge configuration from env vars
│       └── types.ts       # Shared type definitions
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
| `cli.py` | CLI commands: `init-env`, `launch`, `stop`, `push-config` |

**Server-side state machine:** `IDLE → RECORDING → TRANSCRIBING → IDLE (confirmation) → THINKING → STREAMING → IDLE`

**Key features:**

- Whisper transcription via faster-whisper (CUDA or CPU); falls back to mock when token unset
- OpenClaw integration with Ed25519 auth; falls back to `MockResponseHandler`
- Session management: list, switch, create; daily auto-reset on date rollover
- Inflight response buffering: captures deltas during disconnect, replays on reconnect (200KB cap, 5min TTL)
- Auth: HMAC token, rate limiting (5 failures/60s/IP), weak-token warning
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

### Copilot Bridge (`copilot_bridge/`)

Bidirectional bridge between GitHub Copilot and OpenClaw, available as MCP servers or a native OpenClaw plugin.

| Module | Purpose |
|--------|---------|
| `client.ts` | Copilot SDK wrapper, session pool (LRU eviction, max 8 concurrent) |
| `mcp-server.ts` | MCP server: exposes Copilot capabilities to OpenClaw |
| `mcp-openclaw.ts` | MCP server: exposes OpenClaw memory/prefs to Copilot |
| `plugin.ts` | Native OpenClaw plugin (alternative to MCP) |
| `hooks.ts` | Pre/post tool-use permission gating, secret redaction, JSONL audit logs |
| `config.ts` | Configuration from env vars |
| `types.ts` | Shared type definitions |

**Key features:** BYOK support (OpenAI, Azure OpenAI, Anthropic, Ollama), cycle detection (MAX_CALL_DEPTH=3), path restriction policies.

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
| **Node.js** | ≥ 22 | G2 App, Copilot Bridge |
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
uv sync --extra dev

# 2. Generate .env with GPU detection + model selection
uv run python -m gateway init-env

# 3. Push OpenClaw agent/provider config
bash scripts/push-openclaw-config.sh

# 4. Install G2 App dependencies
cd g2_app && npm install && cd ..

# 5. Install Copilot Bridge dependencies
cd copilot_bridge && npm install && cd ..
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

The gateway listens on `ws://127.0.0.1:8765`. Clients connect with `?token=<GATEWAY_TOKEN>`.

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
cd copilot_bridge && npm test             # 216 tests
```

**Total: ~777 tests** across the monorepo.

### Linting & Formatting

```bash
make lint       # Lint all components
make format     # Format all components
```

Or individually:

```bash
uv run ruff check .                       # Python lint
uv run ruff format .                      # Python format
cd copilot_bridge && npm run lint         # Biome lint
cd copilot_bridge && npm run format       # Biome format
```

### Type Checking

```bash
make typecheck

# Or individually
uv run mypy gateway/ infra/               # Python (strict mode)
cd g2_app && npm run typecheck            # TypeScript
cd copilot_bridge && npm run typecheck    # TypeScript
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
| `make test` | Run all tests (gateway + G2 + bridge) |
| `make lint` | Lint all components |
| `make format` | Format all components |
| `make typecheck` | Type-check all components |
| `make cold-start` | Full setup: deps, env, security, smoke tests |
| `make push-config` | Push OpenClaw config to `~/.openclaw/` |
| `make clean` | Remove caches, dist/, logs/, node_modules/ |
| `make help` | Show all targets |

## Configuration

### Gateway Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `127.0.0.1` | Bind address |
| `GATEWAY_PORT` | `8765` | Bind port |
| `GATEWAY_TOKEN` | — | Auth token (required for non-loopback) |
| `WHISPER_MODEL` | `base.en` | faster-whisper model |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | Inference precision |
| `OPENCLAW_HOST` | `127.0.0.1` | OpenClaw host |
| `OPENCLAW_PORT` | `18789` | OpenClaw port |
| `OPENCLAW_GATEWAY_TOKEN` | — | OpenClaw auth token (unset = mock mode) |
| `AGENT_TIMEOUT` | `120` | Max seconds for AI response |
| `AUTH_TIMEOUT` | `5.0` | Auth handshake timeout |
| `ALLOWED_ORIGINS` | — | Comma-separated origins |
| `G2_LOCAL_AUDIO` | `false` | Use local mic instead of WebSocket audio |
| `HISTORY_LIMIT` | `10` | History entries sent on connect |
| `OPENCLAW_AGENT_ID` | `claw` | Agent ID |

### Copilot Bridge Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COPILOT_GITHUB_TOKEN` | *(required)* | GitHub token for Copilot API access |
| `COPILOT_BYOK_PROVIDER` | — | BYOK provider: `openai`, `azure`, `anthropic`, `ollama` |
| `COPILOT_BYOK_API_KEY` | — | API key for BYOK provider |
| `COPILOT_BYOK_MODEL` | — | Model name for BYOK provider |
| `COPILOT_BYOK_BASE_URL` | — | Base URL for BYOK provider |
| `COPILOT_LOG_LEVEL` | `info` | Log level |
| `OPENCLAW_HOST` | `localhost` | OpenClaw host |
| `OPENCLAW_PORT` | `18789` | OpenClaw port |
| `OPENCLAW_TOKEN` | — | OpenClaw auth token |

### G2 App Gateway URL

Resolved in priority order:

1. URL hash: `http://app-url#ws://gateway:8765?token=xxx`
2. Query parameter: `?gateway=ws://gateway:8765?token=xxx`
3. `localStorage` key: `gateway_url`
4. Build-time environment variable

## WebSocket Protocol

Binary + JSON protocol between Gateway and G2 App.

**Authentication:** Token via query parameter (`?token=...`). Single connection at a time — new connections replace existing ones.

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
| [docs/design/](docs/design/) | Architecture, protocol, gateway, G2 app, display layouts, copilot bridge |
| [docs/guides/](docs/guides/) | Getting started, development workflow |
| [docs/reference/](docs/reference/) | OpenClaw internals, G2 SDK/hardware reference |
| [docs/decisions/](docs/decisions/) | Architecture Decision Records (ADRs) |
| [docs/implementation/](docs/implementation/) | Implementation plans and feature specs |

## License

See repository for license details.
