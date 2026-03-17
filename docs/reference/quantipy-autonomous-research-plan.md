# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-17 (post-crash recovery, Round 4 ideation done, E3 DISCARD)

## Goal

Get OpenClaw running a fully autonomous research loop: ideate → implement → backtest → evaluate → decide (keep/discard) → continue — **with zero human interaction between iterations**. The human connects briefly via G2 glasses, gets a status briefing, optionally steers, disconnects. OpenClaw runs 24/7.

## Critical Rule

**WE DO NOT TOUCH QUANTIPY DIRECTLY.** All code in `~/repos/quantipy` is written by OpenClaw → Copilot CLI. We tune OpenClaw's persona/config and fix infrastructure.

## Architecture

```
Human (G2 glasses, connects/disconnects freely)
  ↓ "autoresearch" / "focus on X" / reconnect → status briefing
OpenClaw PM (daemon :18789, autonomous 24/7)
  ↓ exec bash background:true → Copilot CLI
  ↓ cron sentinel monitors PID every 5 min (delivery: announce, channel: g2)
  ↓ sentinel reports [TASK:complete] with metrics
  ↓ OpenClaw evaluates metrics in own turn (no Copilot needed)
  ↓ OpenClaw autonomously decides: keep/discard → next iteration
Copilot CLI (--yolo --agent orchestrator/researcher --model claude-opus-4.6)
  ↓ implements, tests, backtests, commits
~/repos/quantipy (all changes via Copilot)
```

---

## Autonomous Loop Status

### What works:
- [x] "autoresearch" trigger → OpenClaw launches Copilot researcher
- [x] Researcher 3-agent debate → 9 proposals → winner selected
- [x] OpenClaw launches Copilot orchestrator (background:true)
- [x] Orchestrator implements module + tests + notebook → commits
- [x] OpenClaw evaluates DISCARD autonomously (observed for T2→E2, T2→E3)
- [x] OpenClaw launches next implementation without human approval
- [x] Preload caps gpt-5-mini max_tokens at 16384
- [x] AGENTS.md compressed to ~17k (under 20k bootstrap truncation limit)
- [x] Sentinel delivery: `announce` + `channel "g2"` (isolated sessions need explicit channel)
- [x] No model in sentinel (avoids auth errors in isolated cron sessions)

### What's still broken:
- [ ] **Sentinel creation reliability** — OpenClaw skips sentinel ~50% of launches. Without sentinel, it can't detect Copilot exit.
- [ ] **Sentinel max_tokens cap untested post-restart** — preload.cjs caps gpt-5-mini but hasn't been verified e2e.
- [ ] **Content filter on financial context** — Azure GPT-5.4 content filter triggers after several turns. Requires session reset.
- [ ] **Orchestrator incomplete exits** — sometimes runs out of context before committing.

---

## Strategy Results

| # | Strategy | Sharpe | Accuracy | Trades | Verdict | Commit |
|---|----------|--------|----------|--------|---------|--------|
| T1 | GMM Regime Detection | 1.01 | 51.3% | 1 | DISCARD | d521ac5 |
| T2 | IF Anomaly-Gated RF | -27.86 | 44.8% | 131 | DISCARD | ab25745 |
| E2 | PA Sentiment Gate | — | — | — | IMPL ONLY | f10dd37 |
| E3 | CPG-Ridge Ensemble | -143.16 | — | — | DISCARD | 3bad5ca |

**Root cause (T4 researcher):** All experiments ran on synthetic data with zero alpha. T4 winner (C1: Kyle DGP + Cross-Sectional GBM) fixes this with a realistic data generator.

**Round 4 ideation:** Complete (commit `1f76b41`). Winner: **C1 Kyle DGP + Cross-Sectional GBM**. Not yet implemented.

**Quantipy:** 28 unpushed commits. Uncommitted: executed conformal_ridge.ipynb + 4 PNGs.

---

## Config State

| File | Chars | Deployed |
|------|-------|----------|
| AGENTS.md | 17,426 | Yes (07cb2fd) |
| SKILL.md | 25,216 | Yes (07cb2fd) |
| SOUL.md | ~7,000 | Yes |
| openclaw.json | GPT-5.4 128k + mini 16384 | Yes |
| preload.cjs | gpt-5-mini cap 16384 | Yes |

---

## Next Steps

1. **Restart full stack** — gateway + vite + simulator (daemon auto-started at boot)
2. **Fresh session** — reset to avoid content filter from prior financial context
3. **Relay state** — tell OpenClaw: E3 DISCARD, Round 4 ideation done, winner C1, implement it
4. **Monitor sentinel** — verify creation + `channel "g2"` + no max_tokens error
5. **Monitor autonomous continuation** — eval → decide → next launch without human
6. **Fix failures** — tune config per observed behavior gap
7. **Push quantipy commits** once loop stable

## Success Criteria

OpenClaw completes without human interaction:
1. Launch implementation with sentinel
2. Sentinel detects exit + reports metrics
3. Evaluate in own turn
4. Decide keep/discard
5. Launch next iteration
6. Repeat
