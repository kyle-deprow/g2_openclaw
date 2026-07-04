# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-07-03

## Goal

Run a fully autonomous quantitative research pipeline: OpenClaw (PM agent)
ideates with Codex subagents, delegates implementation/review to Codex
subagents, evaluates results, decides keep/discard, and continues. The human
connects via G2 AR glasses for brief strategic steering; the loop runs 24/7
without interaction.

## Critical Rule

**We do not touch quantipy directly.** All code in `~/repos/quantipy` is written
through OpenClaw Codex subagents. We tune OpenClaw's persona/config and fix
infrastructure in this repo.

## Architecture

```
Human (G2 glasses — connect/steer/disconnect)
  ↓
OpenClaw PM (:18789 - autonomous daemon, openai/gpt-5.5 high via Codex runtime)
  ├─ MemPalace MCP (stdio) → local palace
  ├─ Phase 1: Context — PM + context-curator read RESEARCH_LOG.md and MemPalace
  ├─ Phase 2: Debate — five OpenClaw agents, require 3-of-5 majority
  ├─ Phase 3: Consensus — consensus-arbiter emits one implementation brief
  ├─ Phase 4: Implement — implementer subagent (code + test + notebook)
  ├─ Phase 5: Verify — sanity checks (Sharpe >10 = BUG, OOS >2x IS = unreliable)
  ├─ Phase 6: Review — single openai/gpt-5.5 high reviewer
  ├─ Phase 7: Fix/test — fixer handles concrete defects only
  ├─ Phase 8: Decide/log — PM writes final experiment outcome to MemPalace
  └─ Continue — loop NEVER self-terminates, seek orthogonal strategies
  ↓
Gateway (:8765) — G2 transport, reconnect briefing, and task status display
  ↓
OpenClaw Codex runtime (codex plugin, OpenAI auth)
  ↓
~/repos/quantipy — all kept changes committed by implementation subagents
```

## Current Phase (2026-03-28)

**Status: Data range fix applied, relaunching loop.** First cycle revealed all experiments used only 6 months of NVDA/AMD 2022 data — root cause was hardcoded constraints in quantipy's experiment-data skill. Fixed across all config layers (AGENTS.md, BOOTSTRAP.md, experiment-data skill). Worktrees cleaned. Ready for second cycle with full 2021-2026 data mandate.

**Objective:** Identify, backtest, and validate multiple profitable intraday strategies across diverse asset classes. Start with low-to-mid cap equities, expand freely.

**Monitoring strategy:** Human proxy monitors via Dev API (`/_dev/display`,
`/_dev/conversation`, `/_dev/state`). Intervenes only on stuck loops, dead
processes, and protocol violations. Improves OpenClaw via skills/agent config
when deviations are detected.

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
| 5-agent research debate | Three gpt-5.5 high and two gpt-5.4 high debaters prevent tunnel vision |

## OpenClaw Stage Roster

| Agent | Role |
|-------|------|
| main | PM; openai/gpt-5.5 high; only agent with write-capable `mempalace` skill and MemPalace mutation tools |
| context-curator | Read-only MemPalace and `RESEARCH_LOG.md` context packet |
| debater-microstructure | Market mechanics theory |
| debater-data | Data availability, coverage, and target construction |
| debater-skeptic | Leakage, overfit, and cherry-picking pressure |
| debater-theory | Statistical and finance rationale |
| debater-implementation | Buildability and verification cost |
| consensus-arbiter | 3-of-5 majority decision and implementation brief |
| implementer | End-to-end implementation |
| reviewer | Single openai/gpt-5.5 high methodology review |
| fixer | Concrete fixes only |

## Config Files

| File | Location | Purpose |
|------|----------|---------|
| SOUL.md | `gateway/agent_config/` | OpenClaw identity, principles, vibe |
| AGENTS.md | `gateway/agent_config/` | Behavioral rules, verification protocol, phase flow |
| BOOTSTRAP.md | `gateway/agent_config/` | Quantipy context (modules, data, commands) |
| TOOLS.md | `gateway/agent_config/` | Tool reference, exec syntax |
| autoresearch/ | `gateway/agent_config/skills/` | Full 8-phase autonomous loop protocol |
| experiment-data/ | `quantipy .agents/skills/` | Data loading, walk-forward, sanity checks |
| mempalace/ | `gateway/agent_config/skills/` | PM-only MemPalace writes for completed experiment decisions |
| mempalace-readonly/ | `gateway/agent_config/skills/` | Non-PM MemPalace search, diary reads, traversal, and KG queries |

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
| 2026-03-28 03:15 | T9-HRA | Implementation subagent built Hurst Regime Adaptive | Tests failed → reverted |
| 2026-03-28 03:30 | T9-IFA | Implementation subagent built Isolation Forest Adaptive | Notebook not executed, incomplete |
| 2026-03-28 03:45 | Resume | OpenClaw self-healed with --continue | T9-IFA resumed |
| 2026-03-28 04:00 | DATA RANGE VIOLATION | All experiments used only 6mo NVDA/AMD 2022 | Stopped loop for fix |
| 2026-03-28 04:30 | Config fix | Updated AGENTS.md, BOOTSTRAP.md, experiment-data skill | 95% coverage rule, 3yr train, 120d OOS, 20+ folds |
| 2026-03-28 04:45 | Restart | Pushed config, restarted OpenClaw, cleaned worktrees | Ready for new cycle |
