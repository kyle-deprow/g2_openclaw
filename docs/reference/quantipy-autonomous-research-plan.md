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

| # | Strategy | IS Sharpe (net) | OOS Sharpe (net) | Verdict | Commit | Reviewer |
|---|----------|-----------------|------------------|---------|--------|----------|
| T1 | GMM Regime Detection | 1.01 | — | DISCARD | d521ac5 | N/A (pre-reviewer) |
| T2 | IF Anomaly-Gated RF | -27.86 | — | DISCARD | ab25745 | N/A |
| E2 | PA Sentiment Gate | — | — | IMPL ONLY | f10dd37 | N/A |
| E3 | CPG-Ridge Ensemble | -143.16 | — | DISCARD | 3bad5ca | N/A |
| E2-HMR | HMM Regime | — | — | DISCARD | 86c48ab | N/A |
| T4-HRE | Hurst Regime | — | — | DISCARD | 614a607 | N/A |
| T7-TFD | Trade Fragmentation | — | — | DISCARD | 37266e4 | N/A |
| T8-LMS | LMSW Vol-Ret Elasticity | — | — | KEPT | add9862 | N/A |
| T8-MSG | Multi-Scale OHLC Geometry | 2.13 | 5.90 | KEPT* | 02192b2 | Needs review |

*T8-MSG: OOS 5.90 on 45 days is unreliable. IS walk-forward 2.13 [1.65, 2.73] is the real result.

---

## Architecture (Current)

```
Human (G2 glasses)
  ↓ "autoresearch" / reconnect → status
OpenClaw PM (:18789)
  ↓ Phase 2: Copilot --agent researcher (ideation)
  ↓ Phase 3: Copilot --agent orchestrator (implementation)
  ↓ Phase 4: OpenClaw verifies (tests, notebook, metrics)
  ↓ Phase 4.5: Copilot --agent reviewer (adversarial review)  ← NEW
  ↓ Phase 5-8: decide → log → reflect → continue
  ↓ Gateway process monitor (polls 30s, notifies on exit)
Copilot CLI (--yolo --model claude-opus-4.6)
~/repos/quantipy (all changes via Copilot)
```

---

## Config State

| File | Location | Status |
|------|----------|--------|
| autoresearch SKILL.md | g2_openclaw agent_config | Phase 4.5 added, needs push |
| reviewer.agent.md | quantipy .github/agents | NEW, needs commit |
| experiment-data SKILL.md | quantipy .github/skills | Updated, needs commit |
| orchestrator.agent.md | quantipy .github/agents | Updated, needs commit |

---

## Next Steps

1. Push OpenClaw config (autoresearch with Phase 4.5)
2. Commit quantipy scaffolding (reviewer agent + updated skills)
3. Run T8-MSG through the reviewer to validate current HEAD
4. Resume autonomous loop from T8-MSG review
5. Monitor reviewer quality

## Success Criteria

1. Every experiment goes through adversarial review before keep/discard
2. GOAL MET requires IS walk-forward Sharpe > 1.5 AND reviewer PASS
3. Loop: implement → review → decide → next (zero human)
