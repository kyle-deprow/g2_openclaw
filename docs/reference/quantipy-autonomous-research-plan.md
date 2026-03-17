# Autonomous Research Loop — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-17 (E2 implemented, autonomous loop debugging)

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
  ↓ cron sentinel (gpt-5-mini) monitors PID every 5 min
  ↓ sentinel reports [TASK:complete] with metrics
  ↓ OpenClaw evaluates metrics in own turn (no Copilot needed)
  ↓ OpenClaw autonomously decides: keep/discard → next iteration
Copilot CLI (--yolo --agent orchestrator/researcher --model claude-opus-4.6)
  ↓ implements, tests, backtests, commits
~/repos/quantipy (all changes via Copilot)
```

---

## Autonomous Loop Status

### What works e2e:
- [x] Human sends "autoresearch" → OpenClaw launches Copilot researcher
- [x] Researcher runs 3-agent debate → 9 proposals → winner selected
- [x] OpenClaw launches Copilot orchestrator for implementation (background:true)
- [x] Orchestrator implements module + tests + notebook → commits
- [x] OpenClaw can evaluate results and decide DISCARD autonomously (seen once)
- [x] OpenClaw can launch next implementation without human (E2 after T2 DISCARD)
- [x] Preload caps gpt-5-mini max_tokens at 16384 (sentinel fix)

### What's broken (blocking full autonomy):
- [ ] **Sentinel reliability**: OpenClaw frequently skips sentinel creation despite config emphasis. Without sentinel, it can't detect when background Copilot exits.
- [ ] **Sentinel max_tokens**: Fixed in preload but UNTESTED since daemon restart. Need to verify sentinel fires successfully with the cap.
- [ ] **Post-sentinel continuation**: Even when sentinel reports [TASK:complete], OpenClaw doesn't always continue to Phase 4-8 autonomously. Config says to — behavior doesn't match.
- [ ] **Orchestrator incomplete exits**: E2 orchestrator created files but didn't commit (ran out of context). Need to handle partial completions.
- [ ] **No tee/stdout capture**: OpenClaw doesn't consistently pipe Copilot output for debugging.
- [ ] **Gateway stale after daemon restart**: Must restart full stack, not just daemon.

### What's been tried to fix autonomy:
- AGENTS.md: "Steps 1-4 are ONE ATOMIC SEQUENCE" (sentinel before response) — partially working
- AGENTS.md: "Autonomous Post-Completion Evaluation" section — OpenClaw followed it once (T2→E2)
- SOUL.md: Autoresearch section says "DO NOT wait for human approval between iterations"
- SKILL.md: Phase 4-8 are exec commands, not Copilot delegation
- Preload: MODEL_MAX_TOKENS cap for gpt-5-mini (16384)

---

## Strategy Results (Round 3)

| # | Strategy | Source | Sharpe | Accuracy | Trades | Status | Commit |
|---|----------|--------|--------|----------|--------|--------|--------|
| T1 | GMM Regime Detection | R3 ideation | 1.01 | 51.3% | 1 | DISCARD | d521ac5 |
| T2 | Isolation Forest Anomaly-Gated RF | T2 ideation | -27.86 | 44.8% | 131 | DISCARD | ab25745, e318dab |
| E2 | PA Sentiment Gate | T2 runner-up | ? | ? | ? | IMPLEMENTED (not backtested) | f10dd37 |

8 unpushed commits in quantipy (d521ac5 through f10dd37).

---

## Infra Done

- Copilot CLI v1.0.5, copilot_bridge removed (-15,900 lines)
- Azure GPT-5.4 (128k maxTokens) + GPT-5-mini (16384) via Entra auth preload
- Autoresearch skill v5.0.0, 4 Copilot research agents deployed
- Dev API: `_dev/sendText`, `_dev/cmd`, `_dev/state` endpoints
- Push script: auto-generates models allowlist, propagates API keys
- Async: background exec, [TASK:status] markers, task-aware reconnect

## Config Files

| File | Lines | Purpose |
|------|-------|---------|
| SOUL.md | 108 | PM identity, autonomous after approval, autoresearch triggers |
| AGENTS.md | 337 | Delegation modes, sentinel template, evaluation filters, post-completion eval |
| SKILL.md | 396 | 8-phase autoresearch loop, Copilot prompt templates |
| openclaw.json | 166 | GPT-5.4 + GPT-5-mini providers, memory, compaction |
| preload.cjs | 287 | Entra auth, max_tokens→max_completion_tokens, model caps |

## Known Issues

1. **Sentinel skipped by OpenClaw** — despite "Steps 1-4 ATOMIC" in AGENTS.md, OpenClaw frequently launches Copilot without creating sentinel. Root cause unclear — may be context window limitation or instruction buried too deep.
2. **exec quoting fragile** — OpenClaw misformats nested quotes in `-p` prompts 2/3 times before self-recovering.
3. **Gateway stale WS** — after daemon restart, gateway holds connection to old daemon. Must restart full stack.

---

## Next Steps (in order)

1. **Restart full stack** (gateway is stale from last daemon restart)
2. **Test sentinel max_tokens fix** — launch a dummy Copilot process, create sentinel, verify it fires without 400 error
3. **Send "continue autoresearch"** — let OpenClaw evaluate E2 state and continue autonomously
4. **Monitor**: Does OpenClaw create a sentinel? Does sentinel fire? Does OpenClaw evaluate + continue?
5. **Fix failures as they arise** — tune AGENTS.md/SOUL.md/SKILL.md for each observed behavior gap
6. **Push quantipy commits** when tests are stable

## Success Criteria

The loop is working when OpenClaw can:
1. Launch implementation (with sentinel and tee)
2. Sentinel detects exit and reports metrics
3. OpenClaw evaluates metrics without human input
4. OpenClaw decides keep/discard
5. OpenClaw launches next iteration (or new ideation)
6. Repeat steps 1-5 without human connected

All 6 steps happening autonomously = loop complete.
