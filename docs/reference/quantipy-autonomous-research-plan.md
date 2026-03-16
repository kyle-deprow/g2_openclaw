# Quantipy → Autonomous Research Sandbox — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-16 (Round 3 T2 ideation complete — Isolation Forest Anomaly-Gated RF winner)

## Critical Rule

**WE DO NOT TOUCH QUANTIPY DIRECTLY.** All code changes to ~/repos/quantipy happen through OpenClaw → Copilot CLI. Our job is to:
1. Maintain and tune the OpenClaw PM agent (persona, tools, skills, config)
2. Interact with OpenClaw via G2 glasses / DM as a human would
3. Steer OpenClaw toward building a robust financial research sandbox
4. Evaluate OpenClaw's decisions and tune its persona based on observed behavior

If we edit quantipy files directly, the entire exercise is pointless — we're testing whether an autonomous agent can do this work.

## Architecture

```
Human (via G2 glasses — connects/disconnects freely)
  ↓ natural language instructions, disconnect, reconnect hours later
OpenClaw PM (daemon :18789, runs autonomously 24/7)
  ↓ delegates via exec bash background:true
Copilot CLI (--yolo --agent orchestrator --model claude-opus-4.6)
  ↓ reads .github/copilot-instructions.md + .github/agents/*.agent.md
~/repos/quantipy (the research workspace — ALL changes happen here via Copilot)
```

**OpenClaw** = Research PM. Runs autonomously. Uses `background:true` for long tasks, cron for monitoring, `[TASK:status]` markers for status tracking.
**Copilot CLI** = Engineering agent. Implements, tests, backtests, reports metrics.
**Gateway** = Reads `[TASK:*]` markers from transcript on reconnect, injects `taskSummary` into `connected` frame.
**G2 App** = Shows task status indicator on reconnect (`● Task Running` / `✓ Task Done` / `✗ Task Failed`).
**Us** = Human supervisor. Connect briefly, get status, steer, disconnect. OpenClaw continues working.

## What We CAN Touch

| Allowed | Where |
|---------|-------|
| OpenClaw persona files | `gateway/agent_config/{SOUL,AGENTS,TOOLS,BOOTSTRAP}.md` |
| OpenClaw skills | `gateway/agent_config/skills/*/SKILL.md` |
| OpenClaw config | `gateway/openclaw_config/openclaw.json` |
| AI scaffolding repo | `~/repos/ai_scaffolding/` (agent templates, reusable .agent.md files) |
| G2 gateway / app code | `gateway/`, `g2_app/` (the communication layer) |
| This plan | `docs/reference/quantipy-autonomous-research-plan.md` |

## What We CANNOT Touch

| Forbidden | Why |
|-----------|-----|
| `~/repos/quantipy/**` | OpenClaw + Copilot do ALL the work |
| Writing copilot-instructions.md for quantipy directly | OpenClaw must create/manage these via Copilot CLI |
| Any code changes to quantipy | This is the whole point of the exercise |

---

## Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| Infra | Copilot bridge removal, CLI setup, session mgmt, autoresearch | **DONE** |
| Persona | OpenClaw PM persona for research mode | **DONE** |
| Scaffolding | SCAFFOLD mode in AGENTS.md, orchestrator default, agent templates | **DONE** |
| Launch | First interaction — tell OpenClaw to start | **DONE** |
| Async | Background exec, task status on reconnect, cron monitoring | **DONE** |
| Iterate | Steer, evaluate, tune persona | **IN PROGRESS** |
| Memory | Durable memory for cross-session context | **DONE** |
| ML Agents | Upgrade research agents to enforce ML minimum complexity | **DONE** |
| Autoresearch R1 | First research round — rule-based proposals (disappointing) | **DONE** |
| Autoresearch R2 | Second research round — ML-grade proposals (not intraday-focused) | **DONE (archived)** |
| Repo Revert | Quantipy restored to full complexity (API, Docker, Airflow) | **DONE** |
| Intraday Focus | Agent config updated for intraday-only strategies | **DONE** |
| Autoresearch R3 | Intraday ML strategies — research debate + implementation | **IN PROGRESS** |

---

## Infra — DONE

**What was built:**
- Copilot CLI v1.0.5 installed, authenticated via `gh auth`
- copilot_bridge removed (-15,900 lines), replaced by `exec bash pty:true` delegation
- TOOLS.md: Copilot CLI invocation, session management (`--continue`, `--resume=<uuid>`)
- Default model: `claude-opus-4.6`
- Autoresearch skill v2.0.0 deployed to `~/.openclaw/skills/`
- Push script copies bootstrap files + skills

**Key commits:**
- `46e9018` — remove copilot_bridge
- `f458e1a` — session management + autoresearch skill

---

## Persona — DONE

OpenClaw's agent config (`gateway/agent_config/`) is configured for quantipy research:
- **SOUL.md** — Research PM identity
- **AGENTS.md** — RESEARCH + ENGINEER delegation modes, evaluation filters, verification protocol
- **TOOLS.md** — Copilot CLI invocation with session management
- **BOOTSTRAP.md** — Quantipy context (data sources, indicators, instruments)

---

## Scaffolding — DONE

**Completed (commit 3d9feb2):**
- AGENTS.md: SCAFFOLD mode added as first delegation mode (before RESEARCH/ENGINEER)
- AGENTS.md: Orchestrator is the default agent — all invocations use `--agent orchestrator`
- AGENTS.md: SCAFFOLD includes template library, deploy scaffolding, tailor orchestrator, continuous improvement, no bloat, spot improvements
- TOOLS.md: `--agent <name>` flag documented, default invocation uses `--agent orchestrator`
- human-proxy.agent.md: Persona file for the human proxy role in `.github/agents/`
- Orchestrator routing table updated with human-proxy route
- ai_scaffolding repo: Already has agents/ (orchestrator, backend-python, composition-patterns, react-best-practices, react-native-skills) and skills/ (23 skill directories)
- Gate added: "Before first delegation to a new repo: SCAFFOLD mode must have run"

---

## Launch — DONE

Scaffolding deployed → bloat stripped (-3793 lines, 689 tests). Azure GPT-5.4 128k tokens working. Key bug fixes: Azure auth preload (strip api-key header), exec workdir (no tilde), Copilot quoting, API version `2025-04-01-preview`.

---

## Async Autonomous — DONE

Background exec with `background:true`, `[TASK:status]` markers in JSONL transcript, cron monitoring, task-aware reconnect (gateway reads markers, injects `taskSummary` in `connected` frame). G2 shows task indicator on reconnect. Tests: 419 Python (13 new), 231 TypeScript.

---

## Iterate — IN PROGRESS

Connect → get status → steer → disconnect → OpenClaw continues. Tune persona when behavior is suboptimal. Push via `bash scripts/push-openclaw-config.sh` + `openclaw daemon restart`.

---

## Known Issues

1. **LLM costs**: GPT-5.4 with reasoning = ~$3-8 per research round. Mitigated by mini sentinel for cron.
2. **exec quoting fragile**: OpenClaw sometimes misformats nested quotes in `-p` prompts.
3. **Local embeddings slow on first run**: sqlite-vec + local model downloads may timeout on first memory search.

---

## Two-Stage Cron: Copilot Process Sentinel

**Problem:** Cron monitoring ticks used the full GPT-5.4 model (~$0.10-0.30/tick) to run `ps -p PID`. At 5-min intervals over a 30-min Copilot run = 6× full model invocations for trivial health checks. Prior run also specified `model: "claude-opus-4.6"` (a Copilot CLI model name, not valid in OpenClaw) causing error + exponential backoff.

**Solution:** Two-stage cost optimization using GPT-5-mini for sentinel ticks.

| Stage | Model | Cost/tick | What happens |
|-------|-------|-----------|-------------|
| Alive check (~90% of ticks) | azure-oai-g2-mini/gpt-5-mini | ~$0.001 | `ps -p PID` → "alive" |
| Exit detection (1 tick) | azure-oai-g2-mini/gpt-5-mini | ~$0.01 | git log + pytest summary → [TASK:complete] |
| Full evaluation (next turn) | azure-oai-g2/gpt-5.4 | ~$0.10-0.30 | Main agent continues autoresearch loop |

**Config:**
- Added `azure-oai-g2-mini` provider to `openclaw.json` (deployment: `gpt-5-mini`, model: `gpt-5-mini`, version: `2025-08-07`, capacity: 200, GlobalStandard)
- Sentinel template in AGENTS.md, TOOLS.md, and SKILL.md all specify `model: "azure-oai-g2-mini/gpt-5-mini"`
- Sentinel is GENERIC — reusable for any Copilot CLI background process (researcher, orchestrator, specialist)

**Cost comparison (typical 30-min research run, 5-min intervals):**
| Approach | Ticks | Cost |
|----------|-------|------|
| Old (GPT-5.4 every 5m) | 6 × $0.20 | ~$1.20 |
| Old (GPT-5.4 every 15m) | 2 × $0.20 | ~$0.40 |
| New sentinel (mini every 5m) | 5 × $0.001 + 1 × $0.01 + 1 × $0.20 | ~$0.22 |

---

## Prior Rounds Summary

| Round | Outcome | Key Commits |
|-------|---------|-------------|
| R1 Scaffold + Bloat Strip | Scaffolding deployed (467f37f), bloat stripped -3793 lines (df3bf61), 689 tests pass | DONE |
| R2 Backtesting | backtesting.py adapter + BacktestRunner + SMA POC (721 LOC src + 1891 LOC tests), 854 tests pass | DONE |
| Autoresearch R1 | 4 proposals, all rule-based (no ML) — disappointing. Root cause: anti-ML bias in prompts | DONE |
| ML Agent Upgrade | All 4 agents rewritten with HARD REJECT for no-ML, weighted scoring, min complexity tier (bcebd22, d19c219, 388bf4d) | DONE |
| Autoresearch R2 | 9 ML-grade proposals. Winner: C2 Adversarial Sentiment Crowding Detector. Archived — not intraday-focused | DONE |
| Memory Fix | Local embeddings, SQLite vec, hybrid search. Push script force-sets memory config (95a3808) | DONE |
- Skill doc: corrected Quick Config Template with warning
- MEMORY.md seeded in workspace-claw with architecture + active experiments

---

## Next Step — Intraday Research Round 3

**Goal:** Fully autonomous intraday strategy research + implementation loop.

**What changed:**
1. Quantipy reverted to full complexity (commit f2b9933) — API, Docker, Airflow restored. 939 tests pass.
2. Round 2 proposals archived — they weren't intraday-focused.
3. Agent config updated: SOUL.md (intraday identity), BOOTSTRAP.md (intraday focus section), SKILL.md (intraday constraints in researcher/orchestrator prompts), AGENTS.md (planning gate exemption for autoresearch).
4. RESEARCH_LOG.md reset for Round 3 with intraday focus.

**What OpenClaw will do when activated:**
1. Read RESEARCH_LOG.md — see Round 3 PENDING, no unimplemented proposals
2. Phase 2 IDEATE — delegate to Copilot researcher with intraday constraints
3. Researcher runs debate: contrarian/explorer/theorist propose 6-9 intraday ML strategies
4. OpenClaw picks winner, writes all ranked proposals to RESEARCH_LOG.md
5. Phase 3 IMPLEMENT — delegate to Copilot orchestrator (background:true)
6. Phase 4-5 VERIFY + DECIDE — tests, backtest metrics, keep/discard
7. Phase 6-7 LOG + REFLECT — record results, update scaffolding if needed
8. Phase 8 CONTINUE — next proposal or new ideation round

**Activation:** Send "autoresearch" via G2/simulator. OpenClaw runs autonomously from there.

## Autoresearch Round 3 — IN PROGRESS

**Ideation phase (completed):**
- Copilot researcher (session `236e33a3`, PID 289036) ran structured debate
- 21,365-char research report, 535 events
- Winner: **T1 Microstructure Regime Detection via GMM on Volume-Price Impact Features**

**T1 Implementation (completed — commit `d521ac5` in quantipy):**
- Copilot orchestrator (PID 339530) ran ~15 minutes, clean exit
- **1,615 LOC** across 9 files:
  - `src/quantipy/technical_indicators/regime_detection/`: schemas.py (92), features.py (247), gmm.py (74), classifier.py (261), service.py (129), __init__.py (59)
  - `tests/unit/test_regime_detection.py` (608 lines, 45 tests)
  - `pyproject.toml` + `uv.lock` (scikit-learn, scipy deps)
- **45 tests pass** in 8.76s
- Two-stage pipeline: GMM(K=2-4, BIC selected) on 7 microstructure features → Per-regime Logistic Regression with L2 reg
- Walk-forward CV: 20-day train, 5-day test, 5-day slide, 30-bar purged gap
- Target: binary sign(return next 30 min), 0.02% threshold

**Issues observed:**
1. **OpenClaw skipped cron sentinel** — launched Copilot but didn't create the monitoring cron. Fixed in AGENTS.md (mandatory reinforcement).
2. **exec pty:true syntax error** — OpenClaw put `pty:true` inside the bash command string instead of as a named parameter. Self-recovered after 2 failures. Fixed in AGENTS.md (explicit syntax guide).
3. **LLM timeout after daemon restart** — accumulated context caused 408 timeout from Azure. Resolved by daemon restart + fresh session.
4. **Dev API sendText was 404** — no convenience POST endpoint existed. Added `_dev/sendText` and `_dev/ttsRecord` convenience routes (commit `56e5cef`).
5. **Gateway stale WS after daemon restart** — gateway connected to old daemon; messages silently dropped. Requires gateway restart after daemon restart.

**Notebook + Backtest (completed — commits `38d4128`, `519afa1` in quantipy):**
- Copilot orchestrator (PID 364524) ran ~17 min, clean exit
- **2,893 LOC** across 10 files:
  - `src/quantipy/alpha/microstructure_regime_detection/`: schemas.py, data_generator.py, signal_generator.py, strategy.py, __init__.py (346 LOC)
  - `notebooks/experiments/microstructure_regime_detection.ipynb` (703 LOC, 25 cells, 16 code)
  - `tests/unit/test_microstructure_regime_strategy.py` (314 LOC, 25 tests)
  - jupyter, matplotlib, nbclient, ipykernel dev deps
- **70 total tests pass** (45 regime detection + 25 alpha strategy) in 12.76s
- Notebook executed successfully — all 16 code cells produce output

**T1 Evaluation: DISCARD** — 1 trade/1399 days, 51.3% accuracy, underperforms B&H by 9.47pp. GMM+features infra reusable, signal mapping failed.

**T2 Ideation (completed — commit `db64324` in quantipy):**
- Copilot researcher (PID 429216, `--yolo`, stdout → `/tmp/researcher-output-2.txt`) ran ~9 min
- 3-agent debate: contrarian, explorer, theorist → 9 proposals
- Winner: **T2-ISOGATE — Isolation Forest Anomaly-Gated Random Forest** (score 37.0/50)
- Runner-up: E2 Passive-Aggressive Online + Sentiment Gate (score 34.5)
- Full report: `notebooks/experiments/RESEARCH_DEBATE_T2.md`

**T2-ISOGATE Design:**
- Two-stage: (1) Isolation Forest anomaly detection on joint microstructure+sentiment features, (2) Random Forest direction prediction ONLY on anomalous bars
- 19 features: 7 existing micro + 3 new micro + 3 sentiment timing + 4 temporal + 2 cross-modal
- Ternary target: UP >+10bps, DOWN <-10bps, FLAT within 6-bar horizon
- Walk-forward: 60-bar train / 20-bar test / 10-bar slide / 2-bar purge
- Key insight: T1 predicted ALL bars → noise. T2 filters for ~10-15% high-info bars first
- Expected: 56-62% accuracy (anomalous bars only), 5-10 trades/week, Sharpe 1.2-1.8

**TODO:**
- [ ] T2 implementation via OpenClaw → Copilot orchestrator
- [ ] Update RESEARCH_LOG.md with T1 DISCARD + T2 ideation results
- [ ] Push quantipy commits to origin (4 unpushed: d521ac5, 38d4128, 519afa1, db64324)

**Pipeline improvements since Round 2:**
- Two-stage Copilot Process Sentinel (mini model for 5-min monitoring)
- GPT-5-mini provider added (`azure-oai-g2-mini/gpt-5-mini`)
- Generalized sentinel template (reusable for any Copilot background process)
- `--yolo` MANDATORY for all Copilot CLI invocations (file ops fail without it)
- Stdout capture via `2>&1 | tee /tmp/<output>.txt` for background processes
- AGENTS.md sentinel steps reordered: sentinel BEFORE response, 4-step atomic
- exec syntax guide added to AGENTS.md (named params, not inline flags)

**Known bugs:**
- OpenClaw ignores per-model `maxTokens` from config, sends internal default ~32000. Causes 400 errors on 16384-capped models (gpt-5-mini, gpt-5-4). No config fix — OpenClaw core issue.
- Sentinel cron consistently errors due to maxTokens bug. Still functions but with error backoff delays.
