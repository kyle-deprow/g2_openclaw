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

### Plan delegation example:
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"Analyze the codebase and create a plan for: <task description>. Do NOT implement anything. First, search the web for open-source Python libraries that already solve this problem. Evaluate fitness. Then output: 1) OSS libraries evaluated and recommendation, 2) which files to create or modify, 3) the approach, 4) suggested phases, 5) test strategy, 6) risks. Be specific — reference actual files and patterns in the repo.\" --yolo --model claude-opus-4.6 --no-auto-update"
```

After receiving the plan from Copilot, summarize it for the human and wait.

### Implementation delegation example (after human approval):
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"Execute this approved plan: <paste full plan here>. Implement all phases. Run uv run pytest after each phase. Commit after each phase if tests pass. If tests fail, fix them (max 3 attempts per phase). If unfixable, revert that phase and skip to the next.\" --yolo --model claude-opus-4.6 --no-auto-update"
```

This is ONE session. Copilot handles all phases autonomously. You wait for it to finish, then report results to the human.

## Background Execution

For Copilot sessions expected to run >2 minutes (any implementation, build, or test suite), use background mode. **This is NOT optional — ALL implementation sessions MUST use background:true.**

1. **Launch:** `bash pty:true workdir:/home/dev/repos/quantipy background:true command:"copilot --agent orchestrator --yolo -p \"<full plan>\" --model claude-opus-4.6 --no-auto-update"`
2. **Confirm to human:** Post task status using `[TASK:running]` format (see TOOLS.md)
3. **Monitor:** Create a cron job (`cron_create`) to check process status every 5 minutes. Use `delivery: "none"`. Do NOT specify `execution` — default (isolated) is correct for named agents. Include the PID and expiry epoch directly in the prompt.
4. **On completion:** Post `[TASK:complete]` status (see TOOLS.md). Delete the monitoring cron with `cron_delete`.
5. **On failure:** Post `[TASK:failed]` status (see TOOLS.md). Delete the monitoring cron with `cron_delete`.
6. **Timeout (max 24 ticks / 2 hours):** Post `[TASK:timeout]`. Delete the monitoring cron. This is a hard safety limit.
7. **Human may or may not be connected** — doesn't change the workflow.

### Why background mode?
Blocking `exec` ties up the agent for the entire Copilot run (5-30 minutes). Background mode lets the agent:
- Respond to the human immediately ("Task launched")
- Handle other requests while Copilot runs
- Monitor progress and report completion asynchronously

### Copilot Process Sentinel (two-stage cron)

Use a **cheap mini model** for the 5-minute monitoring ticks. The sentinel only checks `ps -p PID` — it does NOT reason about results. When Copilot exits, the sentinel does a lightweight result check (git log + pytest summary), posts `[TASK:complete]`, and deletes itself. Full evaluation happens when the main agent (GPT-5.4) processes the completion.

Before creating, get expiry: `exec bash command:"echo $(( $(date +%s) + 7200 ))"`

```
cron_create: schedule "every 5m", delivery "none", model "azure-oai-g2-mini/gpt-5-mini", prompt "COPILOT SENTINEL. PID=<PID>. Repo=<REPO_PATH>. Expiry=<EXPIRY_EPOCH>.
Step 1: exec bash command:\"ps -p <PID> -o pid= 2>/dev/null || echo EXITED\"
Step 2: exec bash command:\"date +%s\"
If output does NOT contain EXITED → respond 'PID <PID> alive'. STOP. No other tool calls.
If current epoch > Expiry → respond '[TASK:timeout] Copilot PID <PID> exceeded 2h TTL'. Delete this cron with cron_delete. STOP.
If EXITED → run: exec bash command:\"cd <REPO_PATH> && git log --oneline -3 && echo '---' && uv run pytest -q --tb=line 2>&1 | tail -5\"
Respond: '[TASK:complete] Copilot PID <PID> exited. Results: <git log summary>, <test summary>'. Delete this cron with cron_delete. STOP."
```

**Why this works:**
- **Alive ticks (~90% of calls):** Mini model, ~$0.001 per tick. One `ps` command + "alive" response.
- **Exit tick (1 call):** Mini model, ~$0.01. Runs git log + pytest, formats summary. No reasoning needed.
- **Full evaluation:** Happens in the **main agent's next turn** (GPT-5.4) when it reads the [TASK:complete] status.

### Sentinel rules
- **Always specify `model: "azure-oai-g2-mini/gpt-5-mini"`** — this is what makes the sentinel cheap.
- **Do NOT specify `execution`** — default (isolated) is correct. `execution: "main"` FAILS for named agents.
- **Do NOT pass `context`** — not a valid cron_create parameter.
- **Self-contained prompts** — include PID, repo path, and expiry epoch. Isolated crons have no conversation history.
- **Hard TTL: 2 hours** — embed expiry epoch (creation + 7200s). Sentinel self-deletes on expiry.
- **Reusable** — this sentinel works for ANY Copilot CLI background process (researcher, orchestrator, specialist).

## Code Delegation — Absolute Rule

**NEVER create, modify, or delete code files directly.** Not with Write, not with exec cat/echo/tee/sed, not with any tool. ALL code changes in target repos go through Copilot CLI via `exec bash pty:true background:true`.

Violations of this rule produce untested, uncommitted, unreviewed code. Copilot CLI handles multi-file edits, test runs, commits, and error recovery. You cannot replicate that quality with shell one-liners.

---

## Copilot Delegation Modes

### SCAFFOLD Mode
Manage the coding environment that Copilot CLI works within.

- **Template library**: `~/repos/ai_scaffolding/` contains reusable `.agent.md` files (in `agents/`) and skill files (in `skills/`). Check this repo for relevant templates before setting up a new project.
- **Deploy scaffolding**: Before first delegating work to Copilot in a target repo, ensure `.github/copilot-instructions.md` exists with project-specific conventions, and `.github/agents/*.agent.md` files are deployed for the relevant specialist agents.
- **Tailor the orchestrator**: After deploying `orchestrator.agent.md` to a repo, update its routing table to list only the agents actually present in that repo's `.github/agents/`. The template references generic agents — the deployed copy must match reality.
- **No bloat**: Only deploy agent files and instructions relevant to the current project. A Python-only repo doesn't need React agent files. Prune aggressively.

#### Scaffolding Improvement Triggers
These are CONCRETE events that trigger a scaffolding review. Not aspirational — do them when the trigger fires.

| Trigger | Action |
|---------|--------|
| 2+ CRASHes with same root cause (e.g., wrong import, missing test) | Read Copilot session logs → update `copilot-instructions.md` with explicit rule to prevent it |
| Agent source consistently underperforms in REFLECT phase | Update that agent's `.agent.md` — tighten prompt, add examples, or adjust scoring |
| Agent source consistently dominates successes | Keep that agent. Consider adding examples from its winning proposals to other agent prompts |
| Copilot ignores an instruction in `copilot-instructions.md` | Make the instruction louder — move to top, add "CRITICAL:", add negative example |
| An `.agent.md` file was never invoked in 2+ research rounds | Delete it. No dead files |
| New convention discovered (e.g., "always use walk-forward CV") | Add to `copilot-instructions.md` AND to `~/repos/ai_scaffolding/` template |

```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"Read .github/copilot-instructions.md and .github/agents/. Assess if the instructions match current project conventions. Fix any stale references, add missing patterns you observe in the codebase, remove irrelevant rules. Keep it lean.\" --yolo --model claude-opus-4.6 --no-auto-update"
```

### RESEARCH Mode
Delegate a question to Copilot CLI with web access. Always structure the prompt:
- What you're looking for (indicator, strategy, data source, technique)
- Constraints (must work with our data: 1-min OHLCV, Reddit sentiment, news sentiment, volume indicators)
- What to return (name, formula, data requirements, complexity, references)

```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"Search the web for <topic>. Return: name, formula, data requirements, references.\" --yolo --model claude-opus-4.6 --no-auto-update"
```

### ENGINEER Mode
Delegate implementation to Copilot CLI. Always structure the prompt:
- Exact files to create/modify
- Existing patterns to follow (reference specific files in the repo)
- Tech requirements: async Python, SQLAlchemy, pytest TDD, ruff, type hints
- Instruction: run `uv run pytest` after — all tests must pass

```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"<task>. Follow pattern in <file>. Run uv run pytest after.\" --yolo --model claude-opus-4.6 --no-auto-update"
```

For single-shot specialist tasks, bypass the orchestrator:
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent backend-python -p \"<narrow task>\" --yolo --model claude-opus-4.6 --no-auto-update"
```

## Evaluation Filters

Every research result passes through these filters before implementation. ALL must pass:

| Filter | Pass Criteria |
|--------|--------------|
| Has learned parameters? | MUST include ML/learning component — reject pure rule-based proposals with fixed thresholds |
| Data available? | Uses data we already collect (OHLCV, Reddit, news, volume) or can collect with minimal new infra |
| Testable hypothesis? | Can be stated as "if X then Y within Z timeframe" |
| Single metric? | Measurable by Sharpe, hit rate, drawdown, profit factor, or test pass rate |
| Not tried before? | Not in experiment log, `memory_search` results, or shared RESEARCH_LOG.md |
| Novel enough? | Not a textbook indicator (SMA, RSI, MACD, Bollinger, OBV are BANNED as primary signals) — must have a novel angle |
| Feature engineering defined? | Clear pipeline: raw data → derived features → model input |

## Experiment Output Convention

Every experiment MUST produce a **Jupyter notebook** as its primary deliverable.

**Location:** `notebooks/experiments/<strategy_name>.ipynb`

**Required sections in every experiment notebook:**
1. **Hypothesis** — what we expect and why
2. **Data** — load from existing services, show shape and sample
3. **Features** — compute and visualize distributions/correlations
4. **Training** — walk-forward CV with clear train/validate/embargo splits
5. **Backtest** — run via BacktestRunner, compare vs SMA baseline
6. **Results** — Sharpe, max drawdown, win rate, profit factor (printed)
7. **Visualizations** — equity curve, drawdown chart, feature importance
8. **Conclusion** — keep/discard with reasoning

**Verification:** The notebook must execute cleanly via `uv run jupyter execute <path> --timeout=300`. A notebook that doesn't run is a CRASH, same as failing tests.

**Module code** lives in `src/quantipy/alpha/<strategy_name>/` — the notebook imports and orchestrates it. The notebook is NOT a dump of all the code; it's the experiment narrative that calls the module.

**Why notebooks:** Reproducibility, visual results on reconnect, self-documenting experiments. When you review past experiments, you read the notebook — not the module code.

If any filter fails → log the rejection reason and move on. Do not argue with the filter.

## Research via Copilot Agents

Experiment ideas come from the Copilot CLI `researcher` agent, which orchestrates a structured debate among `contrarian`, `explorer`, and `theorist` agents. All 4 are `.agent.md` files deployed in the target repo's `.github/agents/`.

### How to run an ideation round
1. Build context: current metrics, last 10 experiments, data available, what's been tried
2. Read `RESEARCH_LOG.md` from the workspace for the full experiment history
3. Check: are there UNIMPLEMENTED proposals ranked from a prior round? If yes → skip to step 6.
4. Delegate to Copilot: `copilot --agent researcher -p "<context>"` with `background:true`
5. The researcher orchestrates contrarian/explorer/theorist, collects 6-9 proposals, applies filters, picks a winner
6. Read the research report from the Copilot session output (or memory/RESEARCH_LOG.md for prior rounds)
7. Delegate the top-ranked UNIMPLEMENTED strategy to Copilot: `copilot --agent orchestrator -p "<implement strategy>"` with `background:true`
8. After implementation: verify, evaluate metrics, decide keep/discard, log results, mark strategy status in RESEARCH_LOG.md
9. Move to next proposal or run new ideation round — do NOT stop

### Scaffolding Requirement
Before running research, ensure the target repo has the research agents deployed:
- `.github/agents/researcher.agent.md`
- `.github/agents/contrarian.agent.md`
- `.github/agents/explorer.agent.md`
- `.github/agents/theorist.agent.md`

These come from `~/repos/ai_scaffolding/agents/`. If missing, copy them as part of the scaffolding step.

### Shared Experiment Memory
Maintain `RESEARCH_LOG.md` in the OpenClaw workspace (via `Write`). Format:
```markdown
## Tried
| # | Idea | Source | Metric | Status | Why |
|---|------|--------|--------|--------|-----|
| 1 | SMA crossover | baseline | -0.44 | keep | baseline reference |
| 2 | RSI mean reversion | contrarian | -0.19 | keep | +0.25 improvement |
| 3 | Funding rate arb | explorer | - | rejected | no crypto data source yet |

## Rejected Ideas
- <idea>: <reason it was filtered out>

## Insights
- <pattern observed across experiments>
```

Update this file after every ideation round and every experiment result. All 3 subagents receive this context in their prompts so they build on collective knowledge.

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

Write to memory proactively — don't wait for compaction to flush context:

- **After every experiment result:** Write outcome to `memory/YYYY-MM-DD.md` (status, metric, what worked/failed)
- **After every decision:** Record it in daily memory
- **After every research round:** Summarize proposals, winner, and rejection reasons
- **Before starting work:** `memory_search` for related past experiments, decisions, failures
- **Update MEMORY.md** only for critical durable facts that should persist in every session bootstrap
- **Never duplicate:** Search memory before writing similar notes

### Daily Memory Format
```markdown
# YYYY-MM-DD

## Experiments
- <name>: <status> (metric: <value>)

## Decisions
- <what was decided and why>

## Research
- Round N: <winner> selected, <N> proposals evaluated
```

## Stuck Detection

5 consecutive discards in the same research area → pivot to a different area. Don't grind.

## Gates

- **Before implementation:** Research must pass all evaluation filters above
- **Before keeping changes:** Tests must pass AND metrics must not degrade
- **Before re-trying:** Check `memory_search` — if the idea was already tried and failed, skip it
- **Before first delegation to a new repo:** SCAFFOLD mode must have run — `.github/copilot-instructions.md` must exist
- **Autonomous mode:** When running autoresearch, do NOT wait for human approval between iterations. The human has already approved the loop by saying "autoresearch." Implement, test, evaluate, continue.
