# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-22

## Goal

Run a fully autonomous quantitative research pipeline: OpenClaw (PM agent) ideates → delegates to Copilot CLI → evaluates results → decides keep/discard → continues. The human connects via G2 AR glasses for brief strategic steering; the loop runs 24/7 without interaction.

## Critical Rule

**We do not touch quantipy directly.** All code in `~/repos/quantipy` is written by OpenClaw → Copilot CLI. We tune OpenClaw's persona/config and fix infrastructure in this repo.

## Architecture

```
Human (G2 glasses — connect/steer/disconnect)
  ↓
OpenClaw PM (:18789 — autonomous daemon)
  ├─ Graphiti MCP (stdio) → FalkorDB (:6379, Docker, persistent volume)
  ├─ Phase 1: Resume — read RESEARCH_LOG.md + memory + knowledge graph
  ├─ Phase 2: Ideate — Copilot --agent researcher (3-agent debate)
  ├─ Phase 3: Implement — Copilot --agent orchestrator (code + test + notebook)
  ├─ Phase 4: Verify — sanity checks (Sharpe >10 = BUG, OOS >2× IS = unreliable)
  ├─ Phase 4.5: Review — Copilot --agent reviewer (adversarial 8-point audit)
  ├─ Phase 5: Decide — IS walk-forward Sharpe is the primary metric
  ├─ Phase 6-7: Log + reflect + graph_add_memory
  └─ Phase 8: Continue — loop NEVER self-terminates, seek orthogonal strategies
  ↓
Gateway (:8765) — process monitor polls 30s, notifies OpenClaw on Copilot exit
  ↓
Copilot CLI (--yolo --agent <role> --model claude-opus-4.6)
  ↓
~/repos/quantipy — all changes committed by Copilot, reversible via git
```

## Data Available

- **OHLCV**: 1-minute bars for NVDA and AMD (Jan–Jul 2022)
- **Reddit sentiment**: Historical posts from r/wallstreetbets, r/stocks, r/investing with LLM sentiment scores
- **News sentiment**: Articles with sentiment from Massive.com and Polygon.io
- All loaded via `quantipy.prices()` or direct SQL to localhost:5433

## Methodology Guardrails

These were hard-won from 18 prior experiments (all discarded) and are now baked into the agent config:

| Guardrail | Why |
|-----------|-----|
| IS walk-forward Sharpe is the decision metric | OOS on <60 days is meaningless; OOS >2× IS indicates luck |
| Adversarial reviewer runs after every experiment | Caught T8-MSG methodology bugs that inflated Sharpe from -2.76 to +5.9 |
| Sanity checks: Sharpe >10 = BUG | Caught annualization bugs (T8-MSG fix #1 showed IS Sharpe 13.2) |
| Cooldown must match holding period | Per-bar returns without cooldown inflate results massively |
| Loop never stops | "GOAL MET" was premature at OOS 5.9; continuous exploration builds a portfolio |
| Real data only, no synthetic | Early experiments used synthetic data — all were meaningless |
| 3-agent research debate | Researcher, contrarian, explorer/theorist — prevents tunnel vision |

## Copilot Agent Roster (in quantipy)

| Agent | Role |
|-------|------|
| researcher | Orchestrates 3-specialist debate, produces ranked proposals |
| orchestrator | Implements experiments end-to-end (module + tests + notebook) |
| reviewer | Adversarial 8-point audit (OOS reliability, leakage, feature importance, costs) |
| contrarian | Challenges consensus, proposes unconventional directions |
| explorer | Broad creative search, novel feature families |
| theorist | Academically grounded proposals with citations |
| backend-python | Platform infrastructure work |

## Config Files

| File | Location | Purpose |
|------|----------|---------|
| SOUL.md | `gateway/agent_config/` | OpenClaw identity, principles, vibe |
| AGENTS.md | `gateway/agent_config/` | Behavioral rules, verification protocol, phase flow |
| BOOTSTRAP.md | `gateway/agent_config/` | Quantipy context (modules, data, commands) |
| TOOLS.md | `gateway/agent_config/` | Tool reference, exec syntax |
| autoresearch/ | `gateway/agent_config/skills/` | Full 8-phase autonomous loop protocol |
| copilot-cli/ | `gateway/agent_config/skills/` | Copilot delegation, sentinels, resume |
| experiment-data/ | `quantipy .github/skills/` | Data loading, walk-forward, sanity checks |
| graphiti-config.yaml | `gateway/openclaw_config/` | Graphiti MCP server config (graph backend, LLM, embedder, entity types) |
| knowledge-graph/ | `gateway/agent_config/skills/` | Teaches agent when/how to use graph tools in autoresearch |

## Success Criteria

1. Every experiment goes through adversarial review before keep/discard
2. Loop runs continuously — implement → review → decide → next (zero human needed)
3. Build a portfolio of orthogonal strategies, not just one
4. Human's role is strategic steering: connect → get status → "try X next" → disconnect
