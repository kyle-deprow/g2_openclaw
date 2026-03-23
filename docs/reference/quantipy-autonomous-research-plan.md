# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-22 (FULL RESET — clean slate with sentiment data backfill)

## Goal

Get OpenClaw running a fully autonomous research loop: ideate → implement → backtest → evaluate → decide (keep/discard) → continue — **with zero human interaction between iterations**. The human connects briefly via G2 glasses, gets a status briefing, optionally steers, disconnects. OpenClaw runs 24/7.

## Critical Rule

**WE DO NOT TOUCH QUANTIPY DIRECTLY.** All code in `~/repos/quantipy` is written by OpenClaw → Copilot CLI. We tune OpenClaw's persona/config and fix infrastructure.

## Architecture

```
Human (G2 glasses)
  ↓ "autoresearch" / reconnect → status
OpenClaw PM (:18789)
  ↓ Phase 1: Resume check (read RESEARCH_LOG.md + memory)
  ↓ Phase 2: Copilot --agent researcher (3-agent debate → proposals)
  ↓ Phase 3: Copilot --agent orchestrator (implement + test + notebook)
  ↓ Phase 4: OpenClaw verifies (tests, notebook, metrics, sanity checks)
  ↓ Phase 4.5: Copilot --agent reviewer (adversarial review)
  ↓ Phase 5-8: decide → log → reflect → continue (NEVER stops)
  ↓ Gateway process monitor (polls 30s, notifies on exit)
Copilot CLI (--yolo --model claude-opus-4.6)
~/repos/quantipy (all changes via Copilot)
```

---

## Current State: CLEAN SLATE

**All experiment artifacts deleted.** Starting fresh with:
- Sentiment data being backfilled for Jan-Jul 2022 (Reddit + news)
- All methodology improvements retained (adversarial reviewer, continuous portfolio mode, sanity checks)
- OpenClaw memory wiped — no stale experiment records

### What works (proven in prior rounds):
- [x] "autoresearch" trigger → full autonomous loop
- [x] Researcher 3-agent debate → ranked proposals → winner selected
- [x] Orchestrator implements module + tests + notebook → commits
- [x] Adversarial reviewer catches methodology bugs (proven on T8-MSG)
- [x] Continuous portfolio exploration — loop never self-terminates
- [x] Process monitor detects Copilot exit, notifies OpenClaw within 30s
- [x] IS walk-forward Sharpe as primary decision metric (not OOS)
- [x] Sanity checks: Sharpe >10 = BUG, OOS >2× IS = unreliable

### Key lessons from prior rounds (18 experiments, all discarded):
- Pure OHLCV features exhausted after 18 attempts — need sentiment data channel
- Per-bar returns without cooldown inflate Sharpe massively (T8-MSG bug)
- OOS on <60 days is statistically meaningless
- All 3 research agents converged on sentiment as the untapped channel

---

## Strategy Results

(empty — fresh start)

---

## Config State

| File | Location | Status |
|------|----------|--------|
| BOOTSTRAP.md | g2_openclaw agent_config | Updated — fresh start, no banned list |
| autoresearch SKILL.md | g2_openclaw agent_config | Updated — dynamic data counts |
| AGENTS.md | g2_openclaw agent_config | Current — reviewer flow, continuous mode |
| SOUL.md | g2_openclaw agent_config | Current |
| TOOLS.md | g2_openclaw agent_config | Current |
| reviewer.agent.md | quantipy .github/agents | Current |
| experiment-data SKILL.md | quantipy .github/skills | Current |
| All 7 Copilot agents | quantipy .github/agents | Current |
| All 5 Copilot skills | quantipy .github/skills | Current |

---

## Next Steps

1. Sentiment data backfill completes (in progress)
2. Push OpenClaw config + restart daemon
3. Trigger "autoresearch" → fresh Round 1 with sentiment data available
4. Monitor first experiment through full adversarial review cycle

## Success Criteria

1. Every experiment goes through adversarial review before keep/discard
2. Sentiment data is used in at least one experiment (the whole point of reset)
3. Loop runs continuously — implement → review → decide → next (zero human)
4. Build a portfolio of orthogonal strategies, not just one
