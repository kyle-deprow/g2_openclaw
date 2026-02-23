# G2 OpenClaw

A multi-component system that bridges [Even Realities G2](https://www.evenrealities.com/) AR smart glasses to a local AI assistant ([OpenClaw](https://github.com/open-claw/open-claw)) via a PC gateway. Users speak into the G2 glasses, audio is routed through BLE → iPhone → WebSocket to a PC-based gateway that handles transcription and AI inference, then streams responses back to the glasses display.

## Architecture

```
┌──────────┐   BLE    ┌──────────────┐  WebSocket   ┌──────────────┐  localhost  ┌──────────┐
│ G2       │ ◄──────► │ G2 App       │ ◄──────────► │ PC Gateway   │ ◄─────────► │ OpenClaw │
│ Glasses  │          │ (iPhone)     │  port 8765   │ (Python)     │  port 18789 │ (AI)     │
│ firmware │          │ TypeScript   │              │ WebSockets   │             │ Node.js  │
└──────────┘          └──────────────┘              └──────────────┘             └──────────┘
```

**Thin-client model** — the iPhone acts as a "dumb pipe." All intelligence (speech-to-text via Whisper, AI reasoning via OpenClaw, session management) runs on the user's PC. The glasses display is 576×288 pixels, 4-bit greyscale.

### Data Flow

1. User speaks → G2 microphone captures audio
2. Audio streams over BLE to the iPhone G2 App
3. G2 App forwards raw PCM audio (S16LE, 16kHz, mono) via binary WebSocket frames to the PC Gateway
4. PC Gateway runs Whisper transcription (planned — Phase 1 uses mock responses)
5. Transcribed text is sent to OpenClaw for AI inference
6. OpenClaw streams response deltas back through the Gateway
7. Gateway forwards text frames to the G2 App
8. G2 App renders responses on the glasses display

## Repository Structure

```
g2_openclaw/
├── gateway/               # PC Gateway — Python WebSocket server
├── g2_app/                # G2 App — TypeScript thin client for iPhone
├── copilot_bridge/        # Copilot Bridge — GitHub Copilot SDK wrapper
├── infra/                 # Infra CLI (Python) + Azure Bicep IaC
│   ├── main.py            # Typer CLI (deploy, what-if, destroy, lint)
│   ├── azure_ops.py       # Azure CLI wrapper
│   ├── main.bicep         # Root Bicep template
│   ├── modules/           # Modular Bicep templates
│   └── parameters/        # Environment-specific parameters
├── tests/
│   └── gateway/           # Python gateway tests (pytest)
├── tests/
│   ├── gateway/           # Python gateway tests (pytest)
│   ├── integration/       # End-to-end integration tests
│   └── mocks/             # Mock OpenClaw server
├── docs/
│   ├── design/            # Architecture, protocol, component designs
│   ├── guides/            # Getting started, development workflow
│   ├── reference/         # External system docs (OpenClaw, G2 SDK)
│   ├── decisions/         # Architecture Decision Records
│   └── implementation/    # Phase plans and progress tracking
├── pyproject.toml         # Root project config (Infra CLI)
└── .pre-commit-config.yaml
```

## Components

### PC Gateway (`gateway/`)

The heart of the system. A Python WebSocket server that accepts connections from the G2 App, handles audio transcription, communicates with OpenClaw, and streams responses back.

| Module | Purpose |
|--------|---------|
| `server.py` | WebSocket server with token auth, single-connection model, frame routing |
| `protocol.py` | All frame type definitions (TypedDicts), parsing, serialization, error codes |
| `config.py` | Configuration via `.env` and environment variables |
| `__main__.py` | Entry point — `python -m gateway` |

**State machine:** `LOADING → IDLE → RECORDING → TRANSCRIBING → THINKING → STREAMING → IDLE`

**Current status:** Phase 1 complete — WebSocket server, protocol, config, and mock AI responses are implemented. Whisper transcription and OpenClaw integration are planned for later phases.

### G2 App (`g2_app/`)

TypeScript application running on the iPhone via EvenHub. Acts as the thin client between the G2 glasses and the PC Gateway.

| Module | Purpose |
|--------|---------|
| `main.ts` | Application entry point (stub — wiring pending) |
| `gateway.ts` | WebSocket client with auto-reconnect (exponential backoff + jitter) |
| `display.ts` | `DisplayManager` — renders all 9 UI states on the glasses display |
| `state.ts` | Finite state machine with valid transition map and change callbacks |
| `protocol.ts` | TypeScript frame types matching the Python protocol exactly |
| `utils.ts` | Helpers (e.g., `stripMarkdown()` for display-safe text) |

**Display states:** loading, idle, recording, transcribing, thinking, streaming, displaying, error, disconnected — each with a specific layout targeting the 576×288 4-bit greyscale display.

### Copilot Bridge (`copilot_bridge/`)

A TypeScript wrapper around the `@github/copilot-sdk` for integrating GitHub Copilot as an AI backend.

| Module | Purpose |
|--------|---------|
| `client.ts` | `CopilotBridge` class — session management, `runTask()`, `runTaskStreaming()` |
| `config.ts` | Configuration from env vars (`COPILOT_GITHUB_TOKEN`, BYOK settings, etc.) |
| `interfaces.ts` | Interface definitions for the client, sessions, permissions, providers |
| `types.ts` | Type definitions for requests, results, streaming deltas, errors |

Supports BYOK (Bring Your Own Key) providers: OpenAI, Azure OpenAI, Anthropic, Ollama.

### Infrastructure (`infra/`)

Azure Bicep templates and a Python CLI tool for deploying and managing cloud resources.

| File | Purpose |
|------|---------||
| `main.py` | Typer CLI — `deploy`, `what-if`, `destroy`, `validate`, `lint` commands |
| `azure_ops.py` | Wraps Azure CLI (`az`) for deployments, validation, teardown |
| `console.py` | Rich console output helpers |
| `main.bicep` | Root Bicep template |
| `modules/` | Modular Bicep templates |
| `parameters/` | Environment-specific `.bicepparam` files |

Bicep templates provision Azure AI resources:

- **AI Hub** (Azure AI Foundry workspace)
- **AI Project**
- **Azure OpenAI** (with configurable model deployments, default: `gpt-4.1`)
- **Key Vault** (RBAC-based access)
- **Storage Account**
- **Monitoring** (Log Analytics + Application Insights)

Naming convention: `{prefix}-{workload}-{environment}-{location}`

## Prerequisites

### Required

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | ≥ 3.13 | PC Gateway, Infra CLI |
| **Node.js** | ≥ 22 | G2 App, Copilot Bridge, OpenClaw |
| **uv** | latest | Python package/environment manager |
| **npm** | latest (comes with Node.js) | Node.js package manager |

### Optional (for specific components)

| Tool | Version | Purpose |
|------|---------|---------|
| **Azure CLI** (`az`) | latest | Infrastructure deployment via Infra CLI |
| **Azure Bicep CLI** | latest | Bicep template linting/compilation |
| **EvenHub CLI** | latest | G2 App packaging and sideloading |
| **EvenHub Simulator** | latest | Testing G2 App without physical glasses |
| **OpenClaw** | latest | Local AI assistant (target integration) |

### Installing Prerequisites

**Python & uv:**

```bash
# Install uv (recommended method)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Verify
uv --version
python3 --version   # Should be 3.13+
```

**Node.js:**

```bash
# Using nvm (recommended)
nvm install 22
nvm use 22

# Verify
node --version   # Should be ≥ 22
npm --version
```

**Azure CLI (optional, for infrastructure deployment):**

```bash
# Linux
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# macOS
brew install azure-cli

# Verify
az --version
```

## Getting Started

### 1. Clone the repository

```bash
git clone <repo-url>
cd g2_openclaw
```

### 2. Set up the PC Gateway

```bash
cd gateway

# Install dependencies
uv sync --extra dev

# Create a .env file
cat > .env << 'EOF'
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8765
GATEWAY_TOKEN=your-secret-token
EOF

# Run the gateway
uv run python -m gateway
```

The gateway will start listening on `ws://0.0.0.0:8765`. Clients connect with the token as a query parameter: `ws://host:8765?token=your-secret-token`.

### 3. Set up the G2 App

```bash
cd g2_app

# Install dependencies
npm install

# Start development server
npm run dev
```

The Vite dev server starts on `http://0.0.0.0:5173`. For production builds:

```bash
npm run build   # Outputs to dist/
```

To deploy to G2 glasses, package as `.ehpk` using the EvenHub CLI and sideload via QR code.

### 4. Set up the Copilot Bridge

```bash
cd copilot_bridge

# Install dependencies
npm install

# Build
npm run build

# Validate connection (requires COPILOT_GITHUB_TOKEN env var)
npm run validate
```

### 5. Set up the Infra CLI (optional — Azure deployment)

```bash
# From the repo root
uv sync --extra dev

# Verify
uv run azure-infra-cli --help
```

## Development

### Running Tests

**Gateway tests (Python):**

```bash
# From repo root
uv run pytest tests/gateway/ -v
```

**G2 App tests (TypeScript):**

```bash
cd g2_app
npm test
```

**Copilot Bridge tests (TypeScript):**

```bash
cd copilot_bridge
npm test

# Integration tests (requires Copilot token)
npm run test:integration
```

### Linting & Formatting

**Python (ruff):**

```bash
uv run ruff check .    # Lint
uv run ruff format .   # Format
```

**TypeScript — G2 App:** No separate linter configured; TypeScript strict mode enforced via `tsc --noEmit`.

**TypeScript — Copilot Bridge (Biome):**

```bash
cd copilot_bridge
npm run lint      # Check
npm run format    # Fix
```

### Pre-commit Hooks

```bash
# Install hooks
uv run pre-commit install

# Run all hooks manually
uv run pre-commit run --all-files
```

Configured hooks: `ruff` (lint + format) and `mypy` (type checking).

### Type Checking

```bash
# Python
uv run mypy gateway/ infra/

# G2 App
cd g2_app && npm run typecheck

# Copilot Bridge
cd copilot_bridge && npm run typecheck
```

## Infrastructure Deployment

The Infra CLI manages Azure resource deployment via Bicep templates.

```bash
# Preview changes (what-if)
uv run azure-infra-cli what-if --env dev

# Deploy
uv run azure-infra-cli deploy --env dev

# Validate templates without deploying
uv run azure-infra-cli validate --env dev

# Lint Bicep templates
uv run azure-infra-cli lint

# Tear down resources
uv run azure-infra-cli destroy --env dev
```

Deployment parameters are in `infra/parameters/`. Currently, only `dev.bicepparam` is defined.

## WebSocket Protocol

The Gateway and G2 App communicate via a binary + JSON WebSocket protocol.

### Client → Gateway (G2 App sends)

| Frame | Format | Purpose |
|-------|--------|---------|
| `start_audio` | JSON | Begin audio recording session |
| `stop_audio` | JSON | End audio recording session |
| `text` | JSON | Send a text message (bypass speech) |
| `pong` | JSON | Respond to server ping |
| Audio data | Binary | Raw PCM S16LE, 16kHz, mono |

### Gateway → Client (Gateway sends)

| Frame | Format | Purpose |
|-------|--------|---------|
| `connected` | JSON | Auth success, includes Gateway capabilities |
| `status` | JSON | State machine transition notification |
| `transcription` | JSON | Whisper transcription result |
| `assistant_delta` | JSON | Streamed AI response chunk |
| `assistant_end` | JSON | AI response complete |
| `error` | JSON | Error with code and message |
| `ping` | JSON | Keepalive (client must respond with `pong`) |

**Authentication:** Token-based via WebSocket query parameter (`?token=...`).

**Connection model:** Single connection at a time — a new connection replaces the existing one.

## Configuration

### Gateway Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `0.0.0.0` | WebSocket server bind address |
| `GATEWAY_PORT` | `8765` | WebSocket server port |
| `GATEWAY_TOKEN` | *(optional)* | Authentication token for client connections |
| `WHISPER_MODEL` | `base.en` | Whisper model name |
| `WHISPER_DEVICE` | `cpu` | Device for inference (`cpu` or `cuda`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | Compute type for inference |
| `OPENCLAW_HOST` | `127.0.0.1` | OpenClaw Gateway host |
| `OPENCLAW_PORT` | `18789` | OpenClaw Gateway port |
| `OPENCLAW_GATEWAY_TOKEN` | — | OpenClaw auth token (enables real AI; mock without) |
| `AGENT_TIMEOUT` | `120` | Max seconds for AI response cycle |

### Copilot Bridge Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COPILOT_GITHUB_TOKEN` | *(required)* | GitHub token for Copilot API access |
| `COPILOT_BYOK_PROVIDER` | — | BYOK provider: `openai`, `azure`, `anthropic`, `ollama` |
| `COPILOT_BYOK_API_KEY` | — | API key for BYOK provider |
| `COPILOT_BYOK_MODEL` | — | Model name for BYOK provider |
| `COPILOT_BYOK_BASE_URL` | — | Base URL for BYOK provider |
| `COPILOT_LOG_LEVEL` | `info` | Log level |
| `OPENCLAW_HOST` | `localhost` | OpenClaw Gateway host |
| `OPENCLAW_PORT` | `18789` | OpenClaw Gateway port |
| `OPENCLAW_TOKEN` | — | OpenClaw auth token |

### G2 App Gateway URL

The G2 App resolves the Gateway WebSocket URL in priority order:

1. URL hash: `http://app-url#ws://gateway:8765?token=xxx`
2. URL query parameter: `?gateway=ws://gateway:8765?token=xxx`
3. `localStorage` key: `gateway_url`
4. Environment variable at build time

## Dependencies

### Python (Gateway + Infra CLI)

**Runtime — Gateway:**

| Package | Version | Purpose |
|---------|---------|---------|
| `websockets` | ≥ 13.0 | WebSocket server implementation |
| `python-dotenv` | ≥ 1.0 | `.env` file loading |

**Runtime — Infra CLI:**

| Package | Version | Purpose |
|---------|---------|---------|
| `typer` | ≥ 0.12 | CLI framework |
| `rich` | ≥ 13.0 | Terminal formatting and output |
| `azure-identity` | ≥ 1.17 | Azure authentication |
| `azure-mgmt-resource` | ≥ 23.0 | Azure Resource Manager SDK |

**Dev (shared):**

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | ≥ 8.0 | Test framework |
| `pytest-asyncio` | ≥ 0.24 | Async test support (gateway) |
| `ruff` | ≥ 0.5 | Linter and formatter |
| `mypy` | ≥ 1.10 | Static type checker |
| `pre-commit` | ≥ 3.7 | Git hook management |

### TypeScript (G2 App)

**Runtime:**

| Package | Version | Purpose |
|---------|---------|---------|
| `@evenrealities/even_hub_sdk` | ^0.0.7 | G2 glasses SDK (display, input, audio, events) |

**Dev:**

| Package | Version | Purpose |
|---------|---------|---------|
| `typescript` | ^5.5.0 | TypeScript compiler |
| `vite` | ^5.4.0 | Build tool and dev server |
| `vitest` | ^2.0.0 | Test framework |

### TypeScript (Copilot Bridge)

**Runtime:**

| Package | Version | Purpose |
|---------|---------|---------|
| `@github/copilot-sdk` | ^0.1.25 | GitHub Copilot SDK |
| `dotenv` | ^16.4.7 | `.env` file loading |

**Dev:**

| Package | Version | Purpose |
|---------|---------|---------|
| `@biomejs/biome` | ^1.9.4 | Linter and formatter |
| `@types/node` | ^25.3.0 | Node.js type definitions |
| `tsx` | ^4.19.2 | TypeScript execution |
| `typescript` | ^5.7.3 | TypeScript compiler |
| `vitest` | ^3.0.5 | Test framework |

## Documentation

Documentation lives in `docs/` — see [docs/README.md](docs/README.md) for the full index.

| Directory | Contents |
|-----------|----------|
| [docs/design/](docs/design/) | Architecture, protocol, gateway, G2 app, display layouts, copilot bridge, Azure infra |
| [docs/guides/](docs/guides/) | Getting started, development workflow |
| [docs/reference/](docs/reference/) | External system docs — OpenClaw internals, G2 SDK/hardware reference |
| [docs/decisions/](docs/decisions/) | Architecture Decision Records (ADRs) |
| [docs/implementation/](docs/implementation/) | Phase plans (1–4) and progress tracking |

## Project Status

This project follows a phased implementation plan:

- **Phase 1 (Vertical Slice):** ✅ Complete — Gateway WebSocket server, protocol on both sides, G2 App state machine + display + gateway client, full test suites, mock AI responses
- **Phase 2 (Audio Pipeline):** ✅ Complete — Audio buffer, Whisper transcriber, mic capture in G2 App, full audio state machine in Gateway, input handler with tap-to-toggle
- **Phase 3 (OpenClaw Integration):** ✅ Complete — OpenClaw WebSocket client with auth/streaming/lifecycle, agent config (SOUL.md), mock OpenClaw server, delta buffering, response truncation, markdown stripping
- **Phase 4 (Polish):** 🔧 Partial — Ping/pong defined but heartbeat task not started, reconnection backoff not implemented, graceful shutdown pending
- **Copilot Bridge:** ✅ Complete — All 5 phases done: Copilot SDK client, MCP servers (both directions), OpenClaw plugin, task orchestrator, audit logging

## License

See repository for license details.
