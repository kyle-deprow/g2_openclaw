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

**Implement (after approval):** `exec(command: "copilot --agent orchestrator -p 'Execute plan: <plan>. Run pytest per phase. Commit if pass. Max 3 fix attempts. Revert and skip if unfixable.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, background: true, workdir: "/home/dev/repos/quantipy")`

## Background Execution

For Copilot sessions expected to run >2 minutes (any implementation, build, or test suite), use background mode. **This is NOT optional — ALL implementation sessions MUST use background:true.**

**IMPORTANT — steps must be executed IN THIS ORDER. Do NOT skip or reorder.**

1. **Get expiry epoch FIRST:** `exec(command: "echo $(( $(date +%s) + 7200 ))")`
2. **Launch Copilot:** `exec(command: "copilot --agent orchestrator --yolo -p '<full plan>' --model claude-opus-4.6 --no-auto-update", pty: true, background: true, workdir: "/home/dev/repos/quantipy")` — note the PID from the output.
3. **MANDATORY — Create cron sentinel (same turn, no exceptions):** Call `cron_create` with the PID from step 2, expiry from step 1, delivery `announce`. Use the Copilot Process Sentinel template below.
4. **Confirm to human:** Post task status using `[TASK:running]` format (see TOOLS.md). This is your response to the human.

**CRITICAL: You MUST execute ALL 4 steps in ONE turn.** exec(epoch) → exec(copilot) → cron_create → text response. If you respond with text before calling cron_create, the task runs blind with NO monitoring — there is NO way to learn when Copilot finishes. This is the #1 most common failure mode. NEVER skip cron_create, even if sentinels failed in prior sessions. Prior sentinel errors were caused by specifying a model (now fixed). The default model works.

5. **On completion (from sentinel):** Post `[TASK:complete]` status (see TOOLS.md). Delete the monitoring cron with `cron_delete`.
6. **On failure:** Post `[TASK:failed]` status (see TOOLS.md). Delete the monitoring cron with `cron_delete`.
7. **Timeout (max 24 ticks / 2 hours):** Post `[TASK:timeout]`. Delete the monitoring cron. This is a hard safety limit.
8. **Human may or may not be connected** — doesn't change the workflow.

### Why background mode?
Blocking `exec` ties up the agent for the entire Copilot run (5-30 minutes). Background mode lets the agent:
- Respond to the human immediately ("Task launched")
- Handle other requests while Copilot runs
- Monitor progress and report completion asynchronously

### exec tool parameter syntax
The `exec` tool takes NAMED PARAMETERS, not inline bash flags. Correct:
```
exec(command: "copilot ...", pty: true, background: true, workdir: "/home/dev/repos/quantipy")
```
WRONG (will cause "command not found"):
```
exec(command: "pty:true workdir:/foo copilot ...")
```
**Never put `pty:true`, `background:true`, or `workdir:` inside the command string.** They are separate tool parameters.

### Copilot Process Sentinel (two-stage cron)

Use a **cheap mini model** for the 5-minute monitoring ticks. The sentinel only checks `ps -p PID` — it does NOT reason about results. When Copilot exits, the sentinel captures: git log, test summary, AND notebook backtest metrics. This is critical — without metrics, you cannot evaluate the strategy.

Before creating, get expiry: `exec bash command:"echo $(( $(date +%s) + 7200 ))"`

```
cron_create: schedule "every 5m", delivery "announce", prompt "COPILOT SENTINEL. PID=<PID>. Repo=<REPO_PATH>. Expiry=<EXPIRY_EPOCH>.
Step 1: exec bash command:\"ps -p <PID> -o pid= 2>/dev/null || echo EXITED\"
Step 2: exec bash command:\"date +%s\"
If output does NOT contain EXITED → respond 'PID <PID> alive'. STOP. No other tool calls.
If current epoch > Expiry → respond '[TASK:timeout] Copilot PID <PID> exceeded 2h TTL'. Delete this cron with cron_delete. STOP.
If EXITED → run THREE commands:
  exec bash command:\"cd <REPO_PATH> && git log --oneline -3\"
  exec bash command:\"cd <REPO_PATH> && uv run pytest -q --tb=line 2>&1 | tail -5\"
  exec bash command:\"cd <REPO_PATH> && python3 -c \\\"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])) if nbs else {}; [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\\\" 2>&1 | tail -20\"
Respond: '[TASK:complete] Copilot PID <PID> exited. Commits: <git log>. Tests: <test summary>. Notebook metrics: <extracted metrics>'. Delete this cron with cron_delete. STOP."
```

**Why this works:**
- **Alive ticks (~90% of calls):** One `ps` command + "alive" response. Announced to channel but main agent ignores.
- **Exit tick (1 call):** Runs git log + pytest + notebook metric extraction. Announces [TASK:complete] to channel.
- **Full evaluation:** Happens in the **main agent's next turn** (GPT-5.4) when it reads the [TASK:complete] status with metrics included.

### Autonomous Post-Completion Evaluation

**When you receive a [TASK:complete] from the sentinel — DO NOT WAIT FOR THE HUMAN.** Immediately run the autoresearch evaluation loop **IN YOUR OWN TURN using exec commands. Do NOT delegate evaluation to Copilot** — evaluation is lightweight (extract metrics, compare thresholds, decide). Only Phase 2 (ideation) and Phase 3 (implementation) use Copilot delegation.

1. **Parse the metrics** from the sentinel's [TASK:complete] message (Sharpe, accuracy, drawdown, return, trade count)
2. **Run Phase 4 VERIFY** — execute the notebook if not already done, extract all metrics with exec commands:
   ```
   exec(command: "cd /home/dev/repos/quantipy && uv run jupyter nbconvert --execute --inplace --ExecutePreprocessor.timeout=300 notebooks/experiments/<name>.ipynb 2>&1 | tail -5")
   exec(command: "cd /home/dev/repos/quantipy && python3 -c \"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])); [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\" 2>&1 | tail -30")
   ```
3. **Run Phase 5 DECIDE** — apply the hard thresholds from the autoresearch skill (Sharpe > -0.5? > 0.5? > 1.0? Max DD < 30%?)
4. **Run Phase 6 LOG** — record results in RESEARCH_LOG.md and memory
5. **Run Phase 7 REFLECT** — if this is the 3rd implementation or all proposals are done
6. **Run Phase 8 CONTINUE** — pick next action autonomously and launch it immediately:
   - KEEP with low Sharpe → launch Copilot orchestrator for feature iteration
   - DISCARD → launch Copilot orchestrator/researcher for next proposal or new ideation round
   - All proposals exhausted → launch Copilot researcher for new ideation with updated context
   - Goal met → post [TASK:complete] final summary

**Phase 4-5 are exec commands in YOUR turn. Phase 6-7 are write/memory operations in YOUR turn. Phase 8 launches a new Copilot process with background:true + sentinel.** The entire evaluation-to-next-launch sequence happens in ONE turn. You do not stop between phases. **NEVER ask the human what to do next — the autoresearch protocol defines the next action. Decide and execute.**

### Sentinel rules
- **Always specify `delivery: "announce"`** — so the main agent sees [TASK:complete] when Copilot exits.
- **Do NOT specify `model`** — the default model works. Specifying a model causes auth errors in isolated cron sessions.
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
Setup coding environment for Copilot CLI. Templates in `~/repos/ai_scaffolding/`. Before first delegation, ensure `.github/copilot-instructions.md` + `.github/agents/*.agent.md` exist in target repo.

**Triggers for scaffolding review:** 2+ CRASHes with same root cause → update instructions. Agent underperforms → update its `.agent.md`. Agent never invoked in 2+ rounds → delete it. New convention found → add to instructions + template.

```
exec(command: "copilot --agent orchestrator -p 'Read .github/copilot-instructions.md and .github/agents/. Fix stale refs, add missing patterns, remove irrelevant rules. Keep lean.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

### RESEARCH Mode
Delegate a question to Copilot CLI with web access. Always structure the prompt:
- What you're looking for (indicator, strategy, data source, technique)
- Constraints (must work with our data: 1-min OHLCV, Reddit sentiment, news sentiment, volume indicators)
- What to return (name, formula, data requirements, complexity, references)

```
exec(command: "copilot --agent orchestrator -p 'Search the web for <topic>. Return: name, formula, data requirements, references.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

### ENGINEER Mode
Delegate implementation to Copilot CLI. Always structure the prompt:
- Exact files to create/modify
- Existing patterns to follow (reference specific files in the repo)
- Tech requirements: async Python, SQLAlchemy, pytest TDD, ruff, type hints
- Instruction: run `uv run pytest` after — all tests must pass

```
exec(command: "copilot --agent orchestrator -p '<task>. Follow pattern in <file>. Run uv run pytest after.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

For single-shot specialist tasks, bypass the orchestrator:
```
exec(command: "copilot --agent backend-python -p '<narrow task>' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

## Evaluation Filters

Every research result must pass ALL before implementation:
- Has learned parameters (ML/learning component — reject pure rule-based)
- Data available (OHLCV, Reddit, news, volume — or minimal new infra)
- Testable hypothesis ("if X then Y within Z timeframe")
- Single metric (Sharpe, hit rate, drawdown, profit factor)
- Not tried before (check experiment log + `memory_search`)
- Novel enough (SMA, RSI, MACD, Bollinger, OBV BANNED as primary signals)
- Feature engineering defined (raw data → features → model input)

## Experiment Output Convention

Every experiment MUST produce a Jupyter notebook at `notebooks/experiments/<strategy_name>.ipynb`. Required sections: Hypothesis, Data, Features, Training (walk-forward CV), Backtest (vs SMA baseline), Results (Sharpe/DD/win rate printed), Visualizations, Conclusion. Must execute via `uv run jupyter execute <path> --timeout=300`. Module code in `src/quantipy/alpha/<strategy_name>/` — notebook imports it.

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
