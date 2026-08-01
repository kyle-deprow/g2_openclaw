# G2 OpenClaw — Documentation

G2 OpenClaw bridges [Even Realities G2](https://www.evenrealities.com/) AR smart glasses to a local [OpenClaw](https://github.com/open-claw/open-claw) AI assistant via a PC gateway. The system follows a thin-client model: the iPhone app acts as a transparent pipe between glasses (BLE) and a Python WebSocket gateway that handles transcription and AI inference — fully local, no cloud dependency.

## Quick Links

| What | Where |
|------|-------|
| System architecture & data flow | [../README.md](../README.md) (root README) |
| Agent instructions & repo rules | [../AGENTS.md](../AGENTS.md), [../CLAUDE.md](../CLAUDE.md) |
| OpenClaw platform reference | [reference/openclaw/](reference/openclaw/) |
| G2 hardware & EvenHub SDK reference | [reference/g2-platform/](reference/g2-platform/) |
| Quantipy autonomous research plan | [reference/quantipy-autonomous-research-plan.md](reference/quantipy-autonomous-research-plan.md) |

## Directory Structure

```
docs/
├── README.md                          ← You are here
└── reference/                         # Reference material
    ├── openclaw/                      # OpenClaw research (agents, context, personas, tools/MCP)
    ├── g2-platform/                   # G2 hardware constraints & EvenHub CLI/SDK/simulator reference
    └── quantipy-autonomous-research-plan.md  # The autoresearch loop plan of record
```

Design/how-to knowledge that used to live in separate docs now lives closer to where agents consume it:

- **Architecture and data flow** — root [README.md](../README.md).
- **Display, input, SDK, and toolchain rules** — repo skills under [`.agents/skills/g2-*`](../.agents/skills/) (distilled Claude mirrors in [`.claude/skills/`](../.claude/skills/)).
- **OpenClaw operations, sessions, memory, automation** — repo skills under [`.agents/skills/openclaw-*`](../.agents/skills/).
- **Runtime agent behavior** (PM persona, autoresearch protocol, data contracts) — [`gateway/agent_config/`](../gateway/agent_config/) and its `skills/`.
- **Deployment checkpoint** — `.archive/OPENCLAW_DEPLOYMENT_STATUS.md`.

## Reading Order

New to the project? Read these in order:

1. **[../README.md](../README.md)** — the overall system: glasses → iPhone → gateway → OpenClaw, plus the autoresearch loop summary.
2. **[../AGENTS.md](../AGENTS.md)** — stack, layout, rules, and guardrails for working in this repo.
3. **[reference/g2-platform/g2_reference_guide.md](reference/g2-platform/g2_reference_guide.md)** — G2 hardware constraints.
4. **[reference/openclaw/](reference/openclaw/)** — OpenClaw internals (overview, agent architecture, personas, tools/MCP).
5. **[reference/quantipy-autonomous-research-plan.md](reference/quantipy-autonomous-research-plan.md)** — the autonomous research loop design.
