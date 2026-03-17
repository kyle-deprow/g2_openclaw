# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-17 (model-router fix, C1 implementation launched autonomously)

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
- [x] "autoresearch" trigger → OpenClaw reads memory, picks up where it left off
- [x] Researcher 3-agent debate → 9 proposals → winner selected
- [x] OpenClaw launches Copilot orchestrator (background:true) with detailed prompt
- [x] Orchestrator implements module + tests + notebook → commits
- [x] Sentinel delivery: `announce` + `channel "g2"` (isolated sessions need explicit channel)
- [x] No model in sentinel template (avoids auth errors in isolated cron sessions)
- [x] Preload caps gpt-5-mini max_tokens at 16384
- [x] AGENTS.md compressed to ~17k (under 20k bootstrap truncation limit)
- [x] Per-agent models.json fixed: direct deployment URL (not model-router), no apiKeys
- [x] Push script auto-corrects per-agent models.json on every push
- [x] GPT-5.4 hitting correct endpoint: oai-ss.../gpt-5-4 (verified via preload debug)

### Working but not yet verified e2e:
- [ ] **Sentinel announces [TASK:complete] to main agent** — correct config deployed but not yet tested through a full cycle
- [ ] **OpenClaw evaluates metrics autonomously after sentinel announcement** — should work per AGENTS.md but needs cycle to confirm
- [ ] **OpenClaw continues to next iteration without human** — observed partially (T2→E2) but never full sentinel→eval→launch cycle

### Known issues:
- **Content filter** — Azure GPT-5.4 flags financial context after several turns. Mitigated by session reset.
- **Orchestrator context limits** — sometimes exits before committing. Fix: smaller prompts or `--continue`.

---

## Strategy Results

| # | Strategy | Sharpe | Accuracy | Trades | Verdict | Commit |
|---|----------|--------|----------|--------|---------|--------|
| T1 | GMM Regime Detection | 1.01 | 51.3% | 1 | DISCARD | d521ac5 |
| T2 | IF Anomaly-Gated RF | -27.86 | 44.8% | 131 | DISCARD | ab25745 |
| E2 | PA Sentiment Gate | — | — | — | IMPL ONLY | f10dd37 |
| E3 | CPG-Ridge Ensemble | -143.16 | — | — | DISCARD | 3bad5ca |

**Root cause (T4 researcher):** All experiments ran on synthetic data with zero alpha. T4 winner (C1: Kyle DGP + Cross-Sectional GBM) fixes this with a realistic data generator.

**Round 4 ideation:** Complete (commit `1f76b41`). Winner: **C1 Kyle DGP + Cross-Sectional HistGBT**.

**C1 implementation:** ACTIVE — Copilot orchestrator PID 42628, sentinel `0746cd52` (0 errors, announce/g2). Launched autonomously after "autoresearch" trigger with correct model (oai-ss.../gpt-5-4).

**Quantipy:** 28 unpushed commits.

---

## Config State

| File | Chars | Deployed | Notes |
|------|-------|----------|-------|
| AGENTS.md | 17,426 | Yes | Under 20k bootstrap limit |
| SKILL.md | 25,216 | Yes | Loaded on-demand per skill |
| preload.cjs | — | Yes | gpt-5-mini cap 16384 only |
| openclaw.json | — | Yes | GPT-5.4 128k |
| per-agent models.json | — | Yes | Fixed: oai-ss URL, no apiKeys |
| push-openclaw-config.sh | — | Yes | Auto-corrects per-agent models.json |

---

## Next Steps

1. **Monitor C1 implementation** — Copilot PID 42628 active, sentinel watching
2. **Verify sentinel fires correctly** — sentinel announces [TASK:complete] with metrics
3. **Verify OpenClaw evaluates autonomously** — reads metrics, decides keep/discard
4. **Verify OpenClaw continues to next iteration** — launches C2 or new ideation
5. **Push quantipy commits** once loop stable
6. **Disable preload debug** — `AZURE_PRELOAD_DEBUG=1` is noisy, disable after confirmed

## Success Criteria

OpenClaw completes without human interaction:
1. Launch implementation with sentinel
2. Sentinel detects exit + reports metrics
3. Evaluate in own turn
4. Decide keep/discard
5. Launch next iteration
6. Repeat
