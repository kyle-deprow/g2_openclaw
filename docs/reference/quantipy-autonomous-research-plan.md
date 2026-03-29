# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-28

## Goal

Run a fully autonomous quantitative research pipeline: OpenClaw (PM agent) ideates → delegates to Copilot CLI → evaluates results → decides keep/discard → continues. The human connects via G2 AR glasses for brief strategic steering; the loop runs 24/7 without interaction.

## Critical Rule

**We do not touch quantipy directly.** All code in `~/repos/quantipy` is written by OpenClaw → Copilot CLI. We tune OpenClaw's persona/config and fix infrastructure in this repo.

## Architecture

```
Human (G2 glasses — connect/steer/disconnect)
  ↓
OpenClaw PM (:18789 — autonomous daemon, GPT-5.4 reasoning:high)
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

## Current Phase (2026-03-28)

**Status: Data range fix applied, relaunching loop.** First cycle revealed all experiments used only 6 months of NVDA/AMD 2022 data — root cause was hardcoded constraints in quantipy's experiment-data skill. Fixed across all config layers (AGENTS.md, BOOTSTRAP.md, experiment-data skill). Worktrees cleaned. Ready for second cycle with full 2021-2026 data mandate.

**Objective:** Identify, backtest, and validate multiple profitable intraday strategies across diverse asset classes. Start with low-to-mid cap equities, expand freely.

**Monitoring strategy:** Human proxy (Copilot agent in g2_openclaw) monitors via Dev API (`/_dev/display`, `/_dev/conversation`, `/_dev/state`). Intervenes only on: stuck loops, dead processes, protocol violations. Improves OpenClaw via skills/agent config when deviations detected.

**Validation target:** At least 2+ strategies with IS walk-forward Sharpe > 0.5 (net of costs), passing adversarial review.

## Data Available

- **OHLCV**: Any ticker, any timeframe (down to 1-min bars) via Massive.com subscription. Not limited to what's on disk — the agent can pull any data it needs. Period: 2021–2026.
- **Reddit sentiment**: 2021–2026 historical posts from r/wallstreetbets, r/stocks, r/investing with LLM sentiment scores
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
| 95% data range coverage | Experiments limited to 6mo caused useless OOS; use full 2021-2026 range |
| Min 20 walk-forward folds | 10 folds on 6mo data was too few; 3+ years enables 70+ folds |
| Min 120-day OOS holdout | <60 days OOS has Sharpe SE of ±1-3, making estimates meaningless |
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
5. At least 2+ validated profitable strategies (IS Sharpe > 0.5 net, reviewer PASS)

## Monitoring Log

| Timestamp | Event | Action | Outcome |
|-----------|-------|--------|---------|
| 2026-03-28 03:00 | Launch | Fresh start, graph enabled, autoresearch sent | Loop started |
| 2026-03-28 03:15 | T9-HRA | Copilot implemented Hurst Regime Adaptive | Tests failed → reverted |
| 2026-03-28 03:30 | T9-IFA | Copilot implemented Isolation Forest Adaptive | Notebook not executed, incomplete |
| 2026-03-28 03:45 | Resume | OpenClaw self-healed with --continue | T9-IFA resumed |
| 2026-03-28 04:00 | DATA RANGE VIOLATION | All experiments used only 6mo NVDA/AMD 2022 | Stopped loop for fix |
| 2026-03-28 04:30 | Config fix | Updated AGENTS.md, BOOTSTRAP.md, experiment-data skill | 95% coverage rule, 3yr train, 120d OOS, 20+ folds |
| 2026-03-28 04:45 | Restart | Pushed config, restarted OpenClaw, cleaned worktrees | Ready for new cycle |
