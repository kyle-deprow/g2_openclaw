# Agents — Behavioral Rules

## Two Modes of Copilot Delegation

### RESEARCH Mode
Delegate a question to Copilot CLI with web access. Always structure the prompt:
- What you're looking for (indicator, strategy, data source, technique)
- Constraints (must work with our data: 1-min OHLCV, Reddit sentiment, news sentiment, volume indicators)
- What to return (name, formula, data requirements, complexity, references)

```
bash pty:true workdir:~/repos/quantipy command:"copilot -p 'Search the web for <topic>. Return: name, formula, data requirements, references.' --yolo --model gpt-5.4 --no-auto-update"
```

### ENGINEER Mode
Delegate implementation to Copilot CLI. Always structure the prompt:
- Exact files to create/modify
- Existing patterns to follow (reference specific files in the repo)
- Tech requirements: async Python, SQLAlchemy, pytest TDD, ruff, type hints
- Instruction: run `uv run pytest` after — all tests must pass

```
bash pty:true workdir:~/repos/quantipy command:"copilot -p 'In ~/repos/quantipy, <task>. Follow pattern in <file>. Run uv run pytest after.' --yolo --model gpt-5.4 --no-auto-update"
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
