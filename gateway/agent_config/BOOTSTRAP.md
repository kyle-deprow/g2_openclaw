# Bootstrap — Project Context

## g2_openclaw

Polyglot monorepo bridging Even Realities G2 AR smart glasses to OpenClaw via a PC gateway.

**Stack:** Python 3.13+ (uv), TypeScript/Node 22+ (npm), Azure Bicep

**Layout:**
- `gateway/` — Python WebSocket server, Whisper transcription, OpenClaw relay
- `g2_app/` — TypeScript thin client for iPhone / G2 glasses
- `copilot_bridge/` — MCP server wrapping GitHub Copilot SDK
- `infra/` — Azure Bicep infrastructure-as-code
- `tests/` — pytest, mirrors gateway structure

**Conventions:** TDD, typed dataclasses, clean architecture. Python uses ruff + mypy. TypeScript uses biome.

**User context:** The user wears G2 AR smart glasses. Responses via that channel are kept brief by the Gateway — not your concern.
