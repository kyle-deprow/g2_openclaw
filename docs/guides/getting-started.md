# Getting Started

## What You'll Build

G2 OpenClaw is a pipeline that connects Even Realities G2 AR smart glasses to a local AI assistant: audio and text flow from the glasses through an iPhone companion app over WebSocket to a PC-based Python gateway, which handles transcription and AI inference via OpenClaw. This guide walks you through setting up every component so you can send a message from a WebSocket client and receive an AI response end-to-end.

## Prerequisites

### Required

| Tool | Version | Install | Purpose |
|------|---------|---------|---------|
| **Python** | ≥ 3.13 | [python.org](https://www.python.org/) or your OS package manager | PC Gateway, Infra CLI |
| **Node.js** | ≥ 22 | `nvm install 22` ([nvm](https://github.com/nvm-sh/nvm)) | G2 App, Copilot Bridge |
| **uv** | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | Python package/environment manager |
| **npm** | latest | Comes with Node.js | Node.js package manager |

### Optional

| Tool | Install | Purpose |
|------|---------|---------|
| **Azure CLI** (`az`) | [Install Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Infrastructure deployment via Infra CLI |
| **Azure Bicep CLI** | Bundled with Azure CLI or `az bicep install` | Bicep template linting/compilation |
| **EvenHub CLI** | [Even Realities developer portal](https://www.evenrealities.com/) | G2 App packaging (`.ehpk`) and sideloading to glasses |
| **EvenHub Simulator** | Even Realities developer portal | Test the G2 App without physical glasses |
| **OpenClaw** | [github.com/open-claw/open-claw](https://github.com/open-claw/open-claw) | Local AI assistant (the AI backend) |

## Step-by-Step Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd g2_openclaw
```

---

### 2. PC Gateway (required)

The gateway is the core of the system — a Python WebSocket server that accepts connections, routes messages, and (eventually) handles transcription and AI.

```bash
# Install all dependencies (including dev tools)
uv sync --extra dev
```

Create a `.env` file in the **repo root**:

```bash
cat > .env << 'EOF'
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8765
GATEWAY_TOKEN=your-secret-token
EOF
```

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `0.0.0.0` | Bind address |
| `GATEWAY_PORT` | `8765` | Listen port |
| `GATEWAY_TOKEN` | *(none)* | Shared secret for `?token=` auth. If unset, auth is disabled |

Start the gateway:

```bash
uv run python -m gateway
```

You should see:

```
Gateway listening on 0.0.0.0:8765
```

---

### 3. G2 App (required for glasses, optional for gateway-only testing)

The G2 App is a TypeScript thin client that runs on the iPhone via EvenHub. It bridges BLE communication with the glasses to the PC Gateway over WebSocket.

```bash
cd g2_app

# Install dependencies
npm install

# Start the Vite dev server
npm run dev
```

The dev server starts on `http://0.0.0.0:5173`.

**Production build:**

```bash
npm run build    # Outputs to dist/
```

**Deploy to glasses:** Package the production build as an `.ehpk` file using the EvenHub CLI, then sideload via QR code. See the [EvenHub CLI notes](../g2_dev_notes/evenhub_cli.md) for details.

```bash
cd ../..   # Back to repo root
```

---

### 4. OpenClaw (optional — real AI responses)

Without OpenClaw the gateway returns mock (hardcoded) responses. To get real AI responses:

1. **Install OpenClaw** following [its README](https://github.com/open-claw/open-claw).

2. **Set the gateway token** so the gateway can authenticate with OpenClaw. Add to your `.env`:

   ```
   OPENCLAW_HOST=127.0.0.1
   OPENCLAW_PORT=18789
   OPENCLAW_GATEWAY_TOKEN=your-openclaw-token
   ```

3. **Copy the agent config** into your OpenClaw setup. The directory `gateway/agent_config/` contains:

   - `SOUL.md` — a system prompt optimised for the G2's tiny display (short, plain-text answers)
   - `README.md` — notes on session keys and which OpenClaw tools to disable

   Configure your OpenClaw agent to use the `SOUL.md` prompt and the session key `agent:claw:g2`. The gateway does **not** inject the prompt at runtime — it must be set on the OpenClaw side.

4. **Start OpenClaw** on port `18789` (default), then restart the gateway.

---

### 5. Copilot Bridge (optional — GitHub Copilot integration)

An alternative AI backend that wraps the GitHub Copilot SDK.

```bash
cd copilot_bridge

# Install dependencies
npm install

# Build
npm run build
```

Set the required environment variable:

```bash
export COPILOT_GITHUB_TOKEN=ghp_your_token_here
```

Validate the connection:

```bash
npm run validate
```

The bridge also supports BYOK (Bring Your Own Key) providers — OpenAI, Azure OpenAI, Anthropic, and Ollama. See the root [README](../../README.md#copilot-bridge-environment-variables) for the full list of `COPILOT_BYOK_*` variables.

```bash
cd ../..   # Back to repo root
```

---

### 6. Azure Infrastructure (optional — cloud AI resources)

Deploy Azure AI resources (OpenAI, Key Vault, monitoring, etc.) using the Infra CLI:

```bash
# Login to Azure
az login

# Preview what will be deployed
uv run azure-infra-cli what-if --env dev

# Deploy
uv run azure-infra-cli deploy --env dev
```

See `infra/parameters/dev.bicepparam` for the deployment parameters.

---

## Verify It Works

### Quick smoke test (gateway only)

1. **Start the gateway** (if not already running):

   ```bash
   uv run python -m gateway
   ```

2. **Connect via WebSocket.** Use any WebSocket client (e.g., `websocat`, Postman, or a browser console):

   ```bash
   # Using websocat
   websocat "ws://localhost:8765?token=your-secret-token"
   ```

3. **You'll receive a `connected` frame** immediately:

   ```json
   {"type": "connected", "version": "0.1.0"}
   ```

4. **Send a text message:**

   ```json
   {"type": "text", "message": "Hello"}
   ```

5. **Expect a response sequence** — a `status` frame (thinking), one or more `assistant` delta frames with the response text, and an `end` frame:

   ```json
   {"type": "status", "status": "thinking"}
   {"type": "assistant", "delta": "This is a mock response..."}
   {"type": "end"}
   {"type": "status", "status": "idle"}
   ```

   Without OpenClaw, you'll get hardcoded mock responses. With OpenClaw running, you'll get real AI answers.

### Run the test suites

```bash
# Gateway unit tests
uv run pytest tests/gateway/ -v

# Gateway integration tests (full WebSocket round-trip)
uv run pytest tests/integration/ -v

# G2 App tests
cd g2_app && npm test && cd ..

# Copilot Bridge tests
cd copilot_bridge && npm test && cd ..
```

## Configuration Reference

All gateway environment variables are defined in [gateway/config.py](../../gateway/config.py). The full table:

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `0.0.0.0` | WebSocket server bind address |
| `GATEWAY_PORT` | `8765` | WebSocket server port |
| `GATEWAY_TOKEN` | *(none)* | Client authentication token |
| `WHISPER_MODEL` | `base.en` | Whisper model name (Phase 2) |
| `WHISPER_DEVICE` | `cpu` | Whisper inference device (Phase 2) |
| `WHISPER_COMPUTE_TYPE` | `int8` | Whisper compute type (Phase 2) |
| `OPENCLAW_HOST` | `127.0.0.1` | OpenClaw server address |
| `OPENCLAW_PORT` | `18789` | OpenClaw server port |
| `OPENCLAW_GATEWAY_TOKEN` | *(none)* | Token for OpenClaw authentication |
| `AGENT_TIMEOUT` | `120` | Agent response timeout in seconds |

For deeper design context see:

- [Architecture Overview](../design/architecture.md)
- [PC Gateway Design](../design/gateway.md)
- [G2 App Design](../design/g2-app.md)
- [Display Layouts](../design/display-layouts.md)
- [Protocol Design](../design/protocol.md)

## What's Next

- **[Development workflow](development.md)** — linting, formatting, testing, pre-commit hooks, type checking
- **[Architecture Overview](../design/architecture.md)** — full system design, data flow, security model
- **[PC Gateway Design](../design/gateway.md)** — state machine, module breakdown, error handling
- **[G2 App Design](../design/g2-app.md)** — display system, reconnection, input model
- **[OpenClaw Research](../openclaw_research/README.md)** — 8 research docs on OpenClaw internals and capabilities
- **[Implementation Phases](../implementation/phase-1-vertical-slice.md)** — what's done and what's planned
