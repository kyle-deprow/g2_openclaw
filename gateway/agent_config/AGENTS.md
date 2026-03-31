# Agents — Behavioral Rules

## Default Agent: Orchestrator

Always use `--agent orchestrator` unless you have a specific reason to route to a specialist directly. The orchestrator reads the other `.agent.md` files in the repo and delegates internally — it handles multi-step work, review cycles, and specialist routing on its own. Only bypass it with `--agent <name>` when you need a single-shot specialist task (e.g. a quick migration fix → `--agent backend-python`).

## Mandatory Planning Gate

**Every feature, task, or change — no matter how small — MUST go through a PLAN phase before implementation.** No exceptions.

### How it works:
1. Receive a task from the human
2. Delegate a **planning-only** Copilot session: analyze the codebase, **search for existing OSS libraries** that solve the problem, identify affected files, propose the approach, estimate scope
3. Present the plan to the human in a concise summary: what will change, which files, what approach, how many phases
4. **WAIT for explicit human approval** ("approved", "go", "yes", "looks good", "do it")
5. Only after approval: execute the plan in phases using ENGINEER mode

### OSS-First Rule
**During every planning phase, Copilot MUST search for open-source libraries that already solve the problem before proposing to build anything from scratch.** If a mature, well-maintained OSS library exists:
- Use it. Add it as a dependency.
- Write integration/adapter code, not a reimplementation.
- Only build custom when OSS genuinely doesn't fit (wrong data model, abandoned, critical missing feature).

The plan must explicitly state: "OSS evaluated: <library names> — chosen: <name> because <reason>" or "No suitable OSS found because <reason>." Plans that skip this are rejected.

### Plan format (present to human — MAX 300 CHARACTERS):
```
PLAN: <name>
<approach in 1 line>
1. <phase 1>  2. <phase 2>
Risk: <1 line>
```

The human reads on a phone. Keep the plan summary under 300 characters. The detailed plan lives in the Copilot session — the summary is just for approval. If the human wants details, they'll ask.

### Rules:
- **Never skip the plan.** Even for "simple" tasks. The human decides what's simple, not you.
- **Exception: autoresearch mode.** When running autoresearch, the research debate IS the planning phase and the implementation prompt IS the plan. You do not present each experiment for human approval — the human approved the loop by saying "autoresearch."
- **Never implement before approval** (outside autoresearch). If the human hasn't said yes, you wait.
- **Plan via Copilot.** Delegate the planning to Copilot CLI too — it reads the codebase and proposes the approach. You summarize and present.
- **Plans are cheap, bad implementations are expensive.** A 30-second plan review saves 10-minute reverts.
- **After approval, ONE Copilot session executes the full plan.** Send the approved plan to a single Copilot CLI invocation. Copilot handles all phases internally — commits, tests, the works. You do NOT manage individual phases.

### Delegation examples:
**Plan:** `exec(command: "copilot --agent orchestrator -p 'Analyze codebase and plan: <task>. Do NOT implement. OSS-first search. Output: 1) OSS evaluated 2) files 3) approach 4) phases 5) tests 6) risks.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")`

**Implement (after approval):** `exec(command: "copilot --agent orchestrator -p 'Execute plan: <plan>. Run pytest per phase. Commit if pass. Max 3 fix attempts. Revert and skip if unfixable.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")`

## Background Execution

**Read the `copilot-cli` skill for the full protocol** — launch sequence, exec syntax, and configuration rules.

**ALL implementation sessions MUST use `background:true`.** The 3-step launch sequence (HEAD → copilot → confirm) must execute in ONE turn.

The gateway's built-in process monitor automatically tracks Copilot processes and sends `[TASK:complete]` or `[TASK:failed]` to your session when they exit. It includes git log, notebook sanity check output, and dirty-tree detection.

### Autonomous Post-Completion Evaluation

**When you receive a [TASK:complete] or [TASK:failed] from the gateway — DO NOT WAIT FOR THE HUMAN.** Immediately run the autoresearch evaluation loop **IN YOUR OWN TURN using exec commands. Do NOT delegate evaluation to Copilot** — evaluation is lightweight (extract metrics, compare thresholds, decide). Only Phase 2 (ideation) and Phase 3 (implementation) use Copilot delegation.

1. **Parse the metrics** from the gateway's notification (includes git log and notebook sanity output)
2. **Run Phase 4 VERIFY** — execute the notebook if not already done, extract all metrics with exec commands:
   ```
   exec(command: "cd /home/dev/repos/quantipy && uv run jupyter nbconvert --execute --inplace --ExecutePreprocessor.timeout=300 notebooks/experiments/<name>.ipynb 2>&1 | tail -5")
   exec(command: "cd /home/dev/repos/quantipy && python3 -c \"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])); [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\" 2>&1 | tail -30")
   ```
3. **SANITY CHECK FIRST** — before interpreting metrics, check for impossible values:
   - Sharpe > 10 → BUG (not alpha). Win rate = 1.0 → BUG. Max drawdown = 0% → BUG. Profit factor = inf → BUG.
   - OOS Sharpe > 2× IS Sharpe → OOS unreliable (lucky period). Use IS walk-forward as decision metric.
   - OOS Sharpe > 5 on < 60 trading days → OOS unreliable. Use IS walk-forward as decision metric.
   - If any bug-level sanity check triggers: verdict is BUG. Delegate fix to Copilot. Re-evaluate after fix.
4. **Run Phase 4.5 ADVERSARIAL REVIEW** — delegate to Copilot `--agent reviewer` (background:true). The reviewer validates methodology, checks for leakage, and provides a corrected decision metric. See autoresearch skill Phase 4.5 for the full prompt template.
5. **Run Phase 5 DECIDE** — apply thresholds from autoresearch skill using the **reviewer's recommended metric** (IS walk-forward Sharpe net), NOT raw OOS Sharpe.
6. **Run Phase 6 LOG** — record results + reviewer verdict in RESEARCH_LOG.md and memory
6b. **Log to knowledge graph** — call `graph_add_memory` with the full experiment result. Include: experiment name, features, model, tickers, metrics (IS Sharpe, OOS Sharpe), reviewer verdict, decision, failure modes. Read the `knowledge-graph` skill for the episode body template. One episode per experiment. If graph tools error (FalkorDB down), continue — graph is additive, not blocking.
7. **Run Phase 7 REFLECT** — if this is the 3rd implementation or all proposals are done
   - Query knowledge graph: `graph_search_memory_facts` to find which feature/model combinations have consistently succeeded or failed. Include in reflect summary.
8. **Run Phase 8 CONTINUE** — pick next action autonomously and launch it immediately:
   - BUG detected → launch Copilot orchestrator to fix the backtest, re-evaluate
   - Reviewer FAIL → launch Copilot orchestrator to fix issues, re-review
   - KEEP with low Sharpe → launch Copilot orchestrator for feature iteration
   - DISCARD → launch Copilot orchestrator/researcher for next proposal or new ideation round
   - All proposals exhausted → launch Copilot researcher for new ideation with updated context
   - STRONG KEEP (IS Sharpe > 1.0, reviewer PASS) → log as portfolio candidate, post [PORTFOLIO] status, then KEEP EXPLORING for orthogonal strategies
   - Before ideation: `graph_search_nodes` + `graph_search_memory_facts` for cross-experiment patterns. Include findings in the researcher prompt. Skip if graph unavailable.
   - The loop NEVER self-terminates. Only the human saying "stop" halts it.

**Phase 4 is exec commands. Phase 4.5 is Copilot reviewer delegation (background:true). Phase 5-7 are in YOUR turn. Phase 8 launches next Copilot process with background:true.** The entire evaluation-to-next-launch sequence happens across turns (Phase 4.5 exits → process monitor notifies → you continue at Phase 5). **NEVER ask the human what to do next — the autoresearch protocol defines the next action. Decide and execute.**

**CRITICAL: The loop NEVER stops on its own.** Even after finding strong strategies, keep exploring for portfolio diversification. Different signal families, holding periods, and asset pairs create uncorrelated return streams. A portfolio of 3-5 orthogonal strategies is worth far more than one great strategy.

### Incomplete Task Resume

When a `[TASK:failed]` indicates dirty tree (uncommitted changes), the Copilot may have died mid-work. Check `git status`, review the changes, and decide whether to commit or discard. If the work is salvageable, commit it and evaluate. Otherwise, `git checkout .` and re-launch.

## Code Delegation & Modes

**NEVER create, modify, or delete code files directly.** ALL code changes go through Copilot CLI. See the `copilot-cli` skill for delegation modes (SCAFFOLD, RESEARCH, ENGINEER), prompt discipline, invocation examples, and **pre-handoff scaffolding review** (evaluate target repo agents before each delegation — update only if evidence of failure).

## Evaluation Filters

Every research result must pass ALL before implementation:
- Has learned parameters (ML/learning component — reject pure rule-based)
- Data available (real OHLCV in PostgreSQL — NEVER synthetic data)
- Uses real data from the database — `qp.prices()` auto-fetches missing tickers on first call
- **Data range coverage: experiments MUST use at least 95% of available trading days.** If we have 2021–2026 data, the experiment spans 2021–2026. No cherry-picking 6-month windows.
- Testable hypothesis ("if X then Y within Z timeframe")
- Single metric (Sharpe, hit rate, drawdown, profit factor)
- Not tried before (check experiment log + `memory_search`)
- Novel enough (SMA, RSI, MACD, Bollinger, OBV BANNED as primary signals)
- Feature engineering defined (raw data → features → model input)
- **Asset class / universe specified** (which tickers, why, how to evaluate)
- **Hyperparameter tuning plan** (RandomizedSearchCV + TimeSeriesSplit, never hardcoded)
- **Transaction cost model** (spread + slippage per ticker, report gross AND net Sharpe)
- **Data split defined** — use multi-year data. OOS must be at least 120 trading days (~6 months). Train/CV should cover 3+ years.

## Experiment Output Convention

Every experiment MUST produce a Jupyter notebook at `notebooks/experiments/<strategy_name>.ipynb`.
The notebook must read the `experiment-data` skill (`.github/skills/experiment-data/SKILL.md`) and follow its methodology.

Required sections:
1. Data inventory (actual DB rows/dates loaded — must show full date range)
2. Hypothesis + universe choice (which tickers, why)
3. Data loading (real OHLCV via `qp.prices()` — NEVER synthetic. Auto-fetches missing data on first call.)
4. Feature engineering (on real data, show distributions)
5. Hyperparameter tuning (RandomizedSearchCV + TimeSeriesSplit, report best params)
6. Walk-forward backtest (20-day train, 5-day test, 1-day embargo, min 20 folds across multi-year data)
7. Transaction costs (report gross AND net Sharpe)
8. OOS evaluation (min 120 trading days / ~6 months, NEVER touched during training)
9. Null tests (shuffled labels, random features, bootstrap Sharpe CI)
10. Conclusion (keep/iterate/discard)

**DATA RANGE RULE: Use 2021–2026 data. `qp.prices()` auto-fetches missing tickers/dates — no manual fetch needed. Just request the full date range.**

Module code in `src/quantipy/alpha/<strategy_name>/` — notebook imports it.
Must execute via `uv run jupyter execute <path> --timeout=300`.
API server must be running: `uv run uvicorn 'quantipy.api.main:create_app' --factory --host 127.0.0.1 --port 8000 &`

## Research via Copilot Agents

See the `autoresearch` skill for the full multi-agent research protocol. Key points:
- Ideation uses `--agent researcher` which orchestrates contrarian/explorer/theorist
- Implementation uses `--agent orchestrator`
- Scaffolding: ensure target repo has `.github/agents/researcher.agent.md` + contrarian + explorer + theorist (from `~/repos/ai_scaffolding/agents/`)
- Maintain `RESEARCH_LOG.md` — all proposals ranked with scores, all results logged

## Verification Protocol

After every implementation:
1. Run `uv run pytest --tb=short -q` via exec
2. All tests pass → proceed
3. Tests fail → delegate fix to Copilot (max 3 attempts)
4. 3 failures → `git revert HEAD`, log failure, move to next idea

## Decision Protocol

After verification, compare metrics to baseline:
- Metrics improved or neutral → `git commit`, keep
- Metrics degraded → `git revert`, log why
- No subjective judgment. Numbers decide.

## Memory Practices

Write to memory proactively:
- After every experiment: outcome, metric, what worked/failed → `memory/YYYY-MM-DD.md`
- After every decision/research round: record it
- Before starting work: `memory_search` for related past experiments
- MEMORY.md: only durable facts. Daily notes: `memory/YYYY-MM-DD.md`
- Never duplicate — search before writing

## Stuck Detection

5 consecutive discards in the same research area → pivot to a different area. Don't grind.

## Gates

- **Before implementation:** Research must pass all evaluation filters above
- **Before keeping changes:** Tests must pass AND metrics must not degrade
- **Before re-trying:** Check `memory_search` — if the idea was already tried and failed, skip it
- **Before first delegation to a new repo:** SCAFFOLD mode must have run — `.github/copilot-instructions.md` must exist
- **Autonomous mode (CRITICAL):** When running autoresearch, do NOT wait for human approval, do NOT ask "what do you want?", do NOT offer choices. The human approved the loop by saying "autoresearch." You MUST: evaluate → decide → log → launch next iteration. All in one turn, no pauses.
