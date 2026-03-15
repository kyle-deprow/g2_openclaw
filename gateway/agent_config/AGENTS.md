# Agents — Behavioral Rules

## Default Agent: Orchestrator

Always use `--agent orchestrator` unless you have a specific reason to route to a specialist directly. The orchestrator reads the other `.agent.md` files in the repo and delegates internally — it handles multi-step work, review cycles, and specialist routing on its own. Only bypass it with `--agent <name>` when you need a single-shot specialist task (e.g. a quick migration fix → `--agent backend-python`).

## Three Modes of Copilot Delegation

### SCAFFOLD Mode
Manage the coding environment that Copilot CLI works within.

- **Template library**: `~/repos/ai_scaffolding/` contains reusable `.agent.md` files (in `agents/`) and skill files (in `skills/`). Check this repo for relevant templates before setting up a new project.
- **Deploy scaffolding**: Before first delegating work to Copilot in a target repo, ensure `.github/copilot-instructions.md` exists with project-specific conventions, and `.github/agents/*.agent.md` files are deployed for the relevant specialist agents.
- **Tailor the orchestrator**: After deploying `orchestrator.agent.md` to a repo, update its routing table to list only the agents actually present in that repo's `.github/agents/`. The template references generic agents — the deployed copy must match reality.
- **Continuous improvement**: After each research/engineering cycle, assess whether scaffolding is still accurate. If Copilot repeatedly makes the same mistakes, update the relevant `.agent.md` or `copilot-instructions.md`. If a skill or agent file is never triggered, remove it.
- **No bloat**: Only deploy agent files and instructions relevant to the current project. A Python-only repo doesn't need React agent files. Prune aggressively.
- **Spot improvements**: When you discover better patterns or new conventions during work, update templates in `~/repos/ai_scaffolding/` so future projects benefit.

```
bash pty:true workdir:~/repos/<project> command:"copilot -p 'Read .github/copilot-instructions.md and .github/agents/. Assess if the instructions match current project conventions. Fix any stale references, add missing patterns you observe in the codebase, remove irrelevant rules. Keep it lean.' --yolo --model claude-opus-4.6 --no-auto-update"
```

### RESEARCH Mode
Delegate a question to Copilot CLI with web access. Always structure the prompt:
- What you're looking for (indicator, strategy, data source, technique)
- Constraints (must work with our data: 1-min OHLCV, Reddit sentiment, news sentiment, volume indicators)
- What to return (name, formula, data requirements, complexity, references)

```
bash pty:true workdir:~/repos/quantipy command:"copilot --agent orchestrator -p 'Search the web for <topic>. Return: name, formula, data requirements, references.' --yolo --model claude-opus-4.6 --no-auto-update"
```

### ENGINEER Mode
Delegate implementation to Copilot CLI. Always structure the prompt:
- Exact files to create/modify
- Existing patterns to follow (reference specific files in the repo)
- Tech requirements: async Python, SQLAlchemy, pytest TDD, ruff, type hints
- Instruction: run `uv run pytest` after — all tests must pass

```
bash pty:true workdir:~/repos/quantipy command:"copilot --agent orchestrator -p 'In ~/repos/quantipy, <task>. Follow pattern in <file>. Run uv run pytest after.' --yolo --model claude-opus-4.6 --no-auto-update"
```

For single-shot specialist tasks, bypass the orchestrator:
```
bash pty:true workdir:~/repos/quantipy command:"copilot --agent backend-python -p '<narrow task>' --yolo --model claude-opus-4.6 --no-auto-update"
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

- **Before implementation:** Research must pass all 4 evaluation filters
- **Before keeping changes:** Tests must pass AND metrics must not degrade
- **Before re-trying:** Check `memory_search` — if the idea was already tried and failed, skip it
- **Before first delegation to a new repo:** SCAFFOLD mode must have run — `.github/copilot-instructions.md` must exist
