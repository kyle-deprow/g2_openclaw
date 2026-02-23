# G2 OpenClaw — Documentation

G2 OpenClaw bridges [Even Realities G2](https://www.evenrealities.com/) AR smart glasses to a local [OpenClaw](https://github.com/open-claw/open-claw) AI assistant via a PC gateway. The system follows a thin-client model: the iPhone app acts as a transparent pipe between glasses (BLE) and a Python WebSocket gateway that handles transcription and AI inference — fully local, no cloud dependency.

## Quick Links

| What | Where |
|------|-------|
| System architecture | [design/architecture.md](design/architecture.md) |
| WebSocket protocol spec | [design/protocol.md](design/protocol.md) |
| Onboarding guide | [guides/getting-started.md](guides/getting-started.md) |
| Dev workflow & testing | [guides/development.md](guides/development.md) |
| OpenClaw research | [reference/openclaw/](reference/openclaw/) |

## Directory Structure

```
docs/
├── README.md                          ← You are here
├── design/                            # Design: what we're building
│   ├── architecture.md                # System architecture & data-flow diagrams
│   ├── protocol.md                    # Canonical WebSocket protocol spec (message types, frames)
│   ├── gateway.md                     # PC Gateway design (Python, Whisper, session mgmt)
│   ├── g2-app.md                      # G2 App thin-client design (TypeScript, BLE relay)
│   ├── display-layouts.md             # Pixel-level display specs (576×288, 4-bit greyscale)
│   ├── copilot-bridge.md              # Copilot Bridge design (GitHub Copilot SDK wrapper)
│   └── azure-infrastructure.md        # Azure infra design (Bicep modules, AI Hub, KeyVault)
├── guides/                            # How-to guides
│   ├── getting-started.md             # Zero-to-working onboarding for new developers
│   └── development.md                 # Dev workflow: testing, linting, uv commands
├── reference/                         # Reference material
│   ├── openclaw/                      # OpenClaw research (8 files covering agents, tools, memory, etc.)
│   └── g2-platform/                   # G2 hardware constraints & EvenHub SDK reference
├── decisions/                         # Architecture Decision Records (ADRs)
└── archive/                           # Historical: completed spikes & old reviews
    ├── spikes/                        # Phase-0 spike results and findings
    └── reviews/                       # Past architecture reviews
```

## Reading Order

New to the project? Read these in order:

1. **[design/architecture.md](design/architecture.md)** — Understand the overall system: glasses → iPhone → gateway → OpenClaw.
2. **[design/protocol.md](design/protocol.md)** — Learn the WebSocket message types that glue components together.
3. **[design/gateway.md](design/gateway.md)** — Dive into the PC Gateway (the brains of the system).
4. **[design/g2-app.md](design/g2-app.md)** — See how the iPhone thin client relays audio and renders text.
5. **[design/display-layouts.md](design/display-layouts.md)** — Understand the 576×288 micro-LED display constraints.
6. **[guides/getting-started.md](guides/getting-started.md)** — Get a local dev environment running.

For deeper context, browse [reference/openclaw/](reference/openclaw/) for OpenClaw internals and [reference/g2-platform/](reference/g2-platform/) for G2 hardware details.

## Archive

The `archive/` folder contains historical material — completed spike results, phase reviews, and superseded design notes. These are kept for provenance but are **not** actively maintained. Prefer the `design/` docs for current truth.
