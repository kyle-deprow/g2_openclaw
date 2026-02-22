# OpenClaw — Overview & Installation

## What is OpenClaw?

OpenClaw (🦞) is a **free, open-source personal AI assistant** that runs on your own devices. It connects to messaging channels you already use (WhatsApp, Telegram, Slack, Discord, Google Chat, Signal, iMessage, Microsoft Teams, Matrix, WebChat) and can see your screen, use your apps, and do real work for you.

- **GitHub**: [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw) — 217k+ stars, 40.9k forks, 723+ contributors
- **License**: MIT
- **Languages**: TypeScript (84.6%), Swift (11.4%), Kotlin (1.5%), Shell, JS, CSS
- **Creator**: Peter Steinberger (@steipete) and the community
- **Mascot**: Molty, a space lobster 🦞
- **Tagline**: "EXFOLIATE! EXFOLIATE!"

## Key Features

- **Multi-channel inbox**: WhatsApp, Telegram, Slack, Discord, Google Chat, Signal, BlueBubbles (iMessage), iMessage (legacy), Microsoft Teams, Matrix, Zalo, WebChat
- **Voice**: Voice Wake + Talk Mode on macOS/iOS/Android with ElevenLabs
- **Live Canvas**: Agent-driven visual workspace with A2UI
- **Browser control**: Dedicated managed Chrome/Chromium with CDP control
- **First-class tools**: Browser, canvas, nodes, cron, sessions, messaging
- **Companion apps**: macOS menu bar app, iOS/Android nodes
- **Skills platform**: Bundled, managed, and workspace skills with ClawHub registry
- **Multi-agent routing**: Route channels/accounts/peers to isolated agents
- **Local-first**: Gateway runs on your machine, data stays local

## System Requirements

- **Runtime**: Node.js ≥ 22
- **OS**: macOS, Linux, Windows (via WSL2, strongly recommended)
- **Package managers**: npm, pnpm, or bun

## Installation Methods

### Method 1: Install Script (Recommended)

```bash
# macOS/Linux
curl -fsSL https://openclaw.ai/install.sh | bash

# Windows (PowerShell) — use WSL2
```

### Method 2: npm Global Install

```bash
npm install -g openclaw@latest
# or
pnpm add -g openclaw@latest
```

### Method 3: From Source (Development)

```bash
git clone https://github.com/openclaw/openclaw.git
cd openclaw

pnpm install
pnpm ui:build    # auto-installs UI deps on first run
pnpm build

pnpm openclaw onboard --install-daemon
```

### Method 4: Docker

See [Docker docs](https://docs.openclaw.ai/install/docker).

### Method 5: Nix

See [Nix mode](https://github.com/openclaw/nix-openclaw) for declarative config.

### Method 6: MyClaw (Hosted)

[myclaw.ai](https://myclaw.ai/) provides managed, hosted OpenClaw instances:
- **Lite**: $19/mo — 2 vCPU, 4 GB RAM, 40 GB SSD
- **Pro**: $39/mo — 4 vCPU, 8 GB RAM, 80 GB SSD
- **Max**: $79/mo — 8 vCPU, 16 GB RAM, 160 GB SSD

## Quick Start

### 1. Install & Onboard

```bash
openclaw onboard --install-daemon
```

The wizard configures:
- Auth (Anthropic, OpenAI, etc.)
- Gateway settings
- Optional channels (WhatsApp, Telegram, etc.)
- Installs the Gateway daemon (launchd/systemd user service)

### 2. Check Gateway Status

```bash
openclaw gateway status
```

### 3. Open the Control UI

```bash
openclaw dashboard
# Or open http://127.0.0.1:18789/ in browser
```

### 4. Send a Test Message

```bash
openclaw message send --to +1234567890 --message "Hello from OpenClaw"
```

### 5. Talk to the Assistant

```bash
openclaw agent --message "Ship checklist" --thinking high
```

## Development Channels

- **stable**: Tagged releases (`vYYYY.M.D`), npm dist-tag `latest`
- **beta**: Prerelease tags (`vYYYY.M.D-beta.N`), npm dist-tag `beta`
- **dev**: Moving head of `main`, npm dist-tag `dev`

Switch channels:
```bash
openclaw update --channel stable|beta|dev
```

## Useful Environment Variables

| Variable | Purpose |
|---|---|
| `OPENCLAW_HOME` | Home directory for internal path resolution |
| `OPENCLAW_STATE_DIR` | Override state directory |
| `OPENCLAW_CONFIG_PATH` | Override config file path |
| `OPENCLAW_GATEWAY_TOKEN` | Auth token for Gateway WebSocket |
| `OPENCLAW_GATEWAY_PORT` | Override Gateway port (default: 18789) |

## Health Check

```bash
openclaw doctor    # Surface misconfigurations
openclaw status    # Show store path and recent sessions
```

## References

- [Official Docs](https://docs.openclaw.ai/)
- [Getting Started](https://docs.openclaw.ai/start/getting-started)
- [GitHub README](https://github.com/openclaw/openclaw)
- [MyClaw.ai](https://myclaw.ai/)
- [Vision Document](https://github.com/openclaw/openclaw/blob/main/VISION.md)
- [DeepWiki](https://deepwiki.com/openclaw/openclaw)
