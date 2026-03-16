# Quantipy → Autonomous Research Sandbox — Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-03-16 (Two-stage cron sentinel, GPT-5-mini, Round 3 intraday focus)

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
| Autoresearch R3 | Intraday ML strategies — research debate + implementation | **NEXT** |

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

**Completed:**
- Scaffolding deployed by OpenClaw → commit `467f37f`
- Bloat stripped by OpenClaw → Copilot CLI → commit `df3bf61` (55 files, -3793 lines)
  - Removed: airflow/, docker/, docker-compose.yaml, .dockerignore, src/quantipy/api/, src/quantipy/client.py, distributed_rate_limiter.py
  - Updated imports in 4 files, 689 tests pass
- Azure GPT-5.4 working with 128k completion tokens, reasoning_effort=high

**Bugs fixed during launch:**
- Azure auth: preload needed to strip `api-key` header (not just `authorization`)
- exec workdir: tilde `~` not expanded → must use absolute paths
- Copilot CLI quoting: single quotes in `-p` break when prompt has apostrophes
- API version: needed `2025-04-01-preview` for `max_completion_tokens` + `reasoning_effort`
- Parameter rename: model rejects deprecated `max_tokens`, requires `max_completion_tokens`

**Config improvements deployed:**
- TOOLS.md: absolute paths, double-quote quoting, workdir warnings
- AGENTS.md: all examples use absolute `/home/dev/repos/quantipy` paths
- Preload: body interception, parameter rename, reasoning inject, API version bump
- openclaw.json: maxTokens=128000, reasoning=true

---

## Async Autonomous — DONE

**Problem:** Blocking `exec` tied up OpenClaw for entire Copilot sessions (5-30 min). 5-min inflight buffer TTL meant reconnects after short disconnects lost responses. No concept of background work — user saw "Idle" even when Copilot was actively building.

**Solution implemented (3 phases):**

| Phase | What | Files |
|-------|------|-------|
| Config | Taught OpenClaw `background:true`, `[TASK:status]` markers, cron monitoring | SOUL.md, AGENTS.md, TOOLS.md |
| Gateway | Task-aware reconnect — reads JSONL transcript for `[TASK:*]` markers, injects `taskSummary` in `connected` frame | task_status.py (new), server.py, protocol.py |
| G2 App | Shows task indicator on reconnect, deduplicates on flaky connections | protocol.ts, main.ts, display.ts |

**Key design decisions:**
- No separate task database — uses structured `[TASK:status]` markers in OpenClaw session transcript (JSONL)
- Gateway reuses `resolve_session_file()` and `extract_text()` from session_history (no duplication)
- TOOLS.md is single source of truth for task status format; SOUL.md and AGENTS.md reference it
- G2 display transforms `[RUNNING] desc` → `RUNNING: desc` to avoid double-bracket rendering

**What OpenClaw now does differently:**
- Long tasks use `bash background:true` — agent responds immediately, Copilot runs in background
- Posts `[TASK:running]`, `[TASK:complete]`, `[TASK:failed]` markers
- Creates monitoring crons for background tasks
- First message on human reconnect = status briefing

**Tests:** 419 Python (13 new for task_status), 231 TypeScript — all pass. Lint clean.

---

## Iterate — IN PROGRESS

**After launch, our job is to:**
1. Connect to OpenClaw via G2, get status briefing, steer with high-level directions
2. Disconnect — OpenClaw continues autonomously with background tasks and cron monitoring
3. Reconnect later — gateway injects `taskSummary`, G2 shows task status indicator
4. Evaluate quality, approve/reject, tune persona if behavior is suboptimal
5. Redeploy updated persona via `bash scripts/push-openclaw-config.sh` + `openclaw daemon restart`

**Persona tuning triggers:**
| Observed Problem | Fix |
|-----------------|-----|
| OpenClaw writes code directly instead of delegating | Strengthen AGENTS.md delegation rules |
| Copilot makes poor choices | Improve copilot-instructions.md via SCAFFOLD |
| Research is shallow/generic | Improve RESEARCH mode prompts in AGENTS.md |
| Experiments not tracked | Strengthen experiment logging requirements |
| Agent gets stuck in loops | Tune stuck detection thresholds |
| Too much money spent on Opus | Switch default model to sonnet for routine, opus for deep dives |
| Not using background:true for long tasks | Reinforce in AGENTS.md Background Execution section |
| Missing [TASK:*] markers | Reinforce in SOUL.md Async Autonomy section |
| No cron for monitoring | Check AGENTS.md step 3, add explicit cron_create examples |

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

## Round 1 — Scaffolding + Bloat Strip — DONE

**Scaffolding (commit 467f37f):**
- OpenClaw autonomously created `.github/copilot-instructions.md`, `orchestrator.agent.md`, `backend-python.agent.md`
- Quality: accurate tech stack detection, correct project patterns, lean content

**Bloat strip (commit df3bf61):**
- Removed 17 files (-3793 lines): Airflow, Docker, FastAPI, distributed rate limiter
- 689 tests pass. Required 3 exec attempts due to tilde/quoting bugs (now fixed).

## Round 2 — Backtesting Integration — DONE

**6 commits, 721 LOC source + 1891 LOC tests:**
- backtesting.py engine adapter, DataBridge, QuantiPyStrategy base, SMA crossover POC, BacktestRunner, CLI command
- **854 total tests pass, 0 failures**
- OpenClaw used exec directly (not background:true) — works but slow. Needed nudges between phases.

## Autoresearch Round 1 — DONE (Disappointing)

**First autonomous end-to-end research loop.** OpenClaw spawned orchestrator → researcher (PID 4032410), monitored via cron, extracted results.

**4 proposals, all rule-based (no ML):**
- Contrarian Sentiment Divergence (hand-crafted threshold)
- Momentum Regime Detector (hardcoded rules)
- Trend-Following with Sentiment Filter (moving average + heuristic)
- Mean Reversion with Volume Confirmation (Bollinger bands + volume rules)

**Root causes of poor quality:**
1. Contrarian agent had anti-ML bias ("simple is better")
2. Prompts too generic — no tech stack awareness
3. No minimum complexity requirement — rule-based proposals accepted
4. Feasibility-dominated scoring let trivial strategies win

## ML Agent Upgrade — DONE (commits bcebd22, d19c219, 388bf4d)

**Rewrote all 4 Copilot CLI research agents:**
- `researcher.agent.md`: HARD REJECT for no-ML proposals, weighted scoring (novelty×2 + feasibility + persistence×1.5)
- `contrarian.agent.md`: "If it has no learned parameters, it's not a strategy" — Minimum Complexity Tier
- `explorer.agent.md`: ML-first philosophy, experiment templates, academic references
- `theorist.agent.md`: Theory→features→model→target→validation pipeline mandatory
- All have Tech Stack Awareness (scikit-learn, pandas, numpy, backtesting.py)

**SKILL.md v4 + AGENTS.md**: ML mandatory filters, weighted scoring, HARD REJECT rule-based.

## Autoresearch Round 2 — DONE (Strong Results)

**9 ML-grade proposals.** All have learned parameters as required.

**Top proposals:**
| Rank | ID | Name | Composite | Model |
|------|-----|------|-----------|-------|
| 1 | C2 | Adversarial Sentiment Crowding Detector | 19.0 | IsolationForest → XGBoost |
| 2 | T3 | Bayesian Hierarchical Sector Momentum | 19.5 | Bayesian hierarchical model |
| 3 | T2 | Sentiment-Price Divergence Classifier | 19.0 | RandomForest cross-validated |
| 4 | E3 | HMM Market Regime Detector | 18.0 | Hidden Markov Model |

**Winner: C2 — Adversarial Sentiment Crowding Detector**
- Phase 1: IsolationForest anomaly detection on 18-dim feature vector
- Phase 2: XGBClassifier for mean-reversion prediction after crowding events
- Features: Reddit sentiment/engagement, price/volume patterns, Reddit-news divergence
- Validation: walk-forward CV (8-week train → 1-week validate → 7-day embargo, 52 periods)
- Deps: scikit-learn + xgboost (feasible in current stack)

**Implementation roadmap:** C2 (2-3wk) → E3 (1-2wk) → T2 (2wk) → T3 (3-4wk)

## Memory System Fix — DONE (commit 95a3808)

**Problem:** No memory config at all. Cron ticks hit ENOENT on `memory/YYYY-MM-DD.md`. Skill doc had wrong schema (used `memory.enabled`, `memory.vectorSearch` — invalid in v2026.3.2).

**Root cause:** Top-level `memory` only accepts `backend`/`citations`/`qmd`. Memory search config goes under `agents.defaults.memorySearch` per the Zod schema (`MemorySearchSchema`).

**Fix:**
- `agents.defaults.memorySearch`: local embeddings, SQLite vector store, hybrid search (BM25 0.7 weight), MMR reranking, temporal decay (30-day half-life)
- `agents.defaults.compaction.memoryFlush`: enabled, softThresholdTokens=4000
- `memory.backend`: "builtin"
- Push script: force-set `memory` section (like `tools`) to prevent deep merge drift
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

**Pipeline improvements since Round 2:**
- Memory config fixed (correct Zod schema for v2026.3.2)
- Reflection hooks (Phase 7 REFLECT with pattern analysis + scaffolding updates)
- Notebook enforcement (mandatory Jupyter output for every experiment)
- Planning gate exemption (no human approval during autoresearch loop)
- Intraday constraints injected at every delegation point
- **Two-stage Copilot Process Sentinel** — mini model for cheap 5-min monitoring, GPT-5.4 only for evaluation
- **GPT-5-mini added to openclaw.json** — `azure-oai-g2-mini/gpt-5-mini` (capacity 200, GlobalStandard)
- **Generalized sentinel template** — reusable for any Copilot background process
