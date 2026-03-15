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
- **Never implement before approval.** If the human hasn't said yes, you wait.
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

For Copilot sessions expected to run >2 minutes (any implementation, build, or test suite), use background mode:

1. **Launch:** `bash pty:true workdir:/home/dev/repos/quantipy background:true command:"copilot --agent orchestrator --yolo -p \"<full plan>\" --model claude-opus-4.6 --no-auto-update"`
2. **Confirm to human:** Post task status using `[TASK:running]` format (see TOOLS.md)
3. **Monitor:** Create a cron job (`cron_create`) to check process status every 5 minutes. Use `delivery: "none"` for silent monitoring, `mode: "main"` for shared context.
4. **On completion:** Post `[TASK:complete]` status (see TOOLS.md). Delete the monitoring cron.
5. **On failure:** Post `[TASK:failed]` status (see TOOLS.md). Delete the monitoring cron.
6. **Human may or may not be connected** — doesn't change the workflow.

### Why background mode?
Blocking `exec` ties up the agent for the entire Copilot run (5-30 minutes). Background mode lets the agent:
- Respond to the human immediately ("Task launched")
- Handle other requests while Copilot runs
- Monitor progress and report completion asynchronously

---

## Copilot Delegation Modes

### SCAFFOLD Mode
Manage the coding environment that Copilot CLI works within.

- **Template library**: `~/repos/ai_scaffolding/` contains reusable `.agent.md` files (in `agents/`) and skill files (in `skills/`). Check this repo for relevant templates before setting up a new project.
- **Deploy scaffolding**: Before first delegating work to Copilot in a target repo, ensure `.github/copilot-instructions.md` exists with project-specific conventions, and `.github/agents/*.agent.md` files are deployed for the relevant specialist agents.
- **Tailor the orchestrator**: After deploying `orchestrator.agent.md` to a repo, update its routing table to list only the agents actually present in that repo's `.github/agents/`. The template references generic agents — the deployed copy must match reality.
- **Continuous improvement**: After each research/engineering cycle, assess whether scaffolding is still accurate. If Copilot repeatedly makes the same mistakes, update the relevant `.agent.md` or `copilot-instructions.md`. If a skill or agent file is never triggered, remove it.
- **No bloat**: Only deploy agent files and instructions relevant to the current project. A Python-only repo doesn't need React agent files. Prune aggressively.
- **Spot improvements**: When you discover better patterns or new conventions during work, update templates in `~/repos/ai_scaffolding/` so future projects benefit.

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
| Data available? | Uses data we already collect (OHLCV, Reddit, news, volume) or can collect with minimal new infra |
| Testable hypothesis? | Can be stated as "if X then Y within Z timeframe" |
| Single metric? | Measurable by Sharpe, hit rate, drawdown, profit factor, or test pass rate |
| Not tried before? | Not in experiment log or `memory_search` results |

If any filter fails → log the rejection reason and move on. Do not argue with the filter.

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

## Stuck Detection

5 consecutive discards in the same research area → pivot to a different area. Don't grind.

## Gates

- **Before ANY implementation:** A plan must be created and approved by the human. No plan → no code.
- **Before implementation:** Research must pass all 4 evaluation filters
- **Before keeping changes:** Tests must pass AND metrics must not degrade
- **Before re-trying:** Check `memory_search` — if the idea was already tried and failed, skip it
- **Before first delegation to a new repo:** SCAFFOLD mode must have run — `.github/copilot-instructions.md` must exist
