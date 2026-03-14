# Autoresearch — Autonomous Research Loop

Adapted from Karpathy's autoresearch principle: constraint + mechanical metric + autonomous iteration = compounding gains.

## When to Activate

- User says "start researching", "run the research loop", "iterate on quantipy", "improve the platform"
- User says "autoresearch", "keep improving", "run overnight"
- Any task requiring repeated research → implement → verify → measure cycles

## The Loop

```
LOOP (until interrupted or N iterations):
  1. REVIEW: Read repo state, test status, experiment log, git history
  2. RESEARCH: Delegate a specific question to Copilot (RESEARCH mode)
  3. EVALUATE: Apply the 4 mechanical filters (see AGENTS.md)
  4. IMPLEMENT: Delegate to Copilot (ENGINEER mode) — one focused change
  5. VERIFY: Run tests via exec — pass/fail, no subjective judgment
  6. MEASURE: Run backtest if available — record Sharpe, hit rate, drawdown
  7. DECIDE: Improved → git commit + keep. Degraded → git revert + log.
  8. LOG: Record in experiment tracker. Store learnings in memory.
  9. REPEAT: Go to step 1. Do NOT stop. Do NOT ask "should I continue?"
```

## Setup (First Iteration Only)

1. Inspect `~/repos/quantipy` — glob files, read key modules, run tests
2. Check if experiment tracker exists (`src/quantipy/experiment/`). If not, build it first.
3. Check if backtester exists (`src/quantipy/backtest/`). If not, build it first.
4. Establish baseline metrics — test count, test pass rate, coverage (if measurable)
5. Begin the loop

## Critical Rules

1. **One change per iteration** — Atomic changes. If it breaks, you know exactly why.
2. **Commit before verify** — `git commit` the change, then verify. This way `git revert` is trivial.
3. **Mechanical verification only** — Tests pass/fail, metric numbers. Not "looks good."
4. **Automatic rollback** — Failed → revert. No "maybe if we tweak it." Move on.
5. **Never stop** — Unless interrupted by the user or loop count reached, keep iterating.
6. **Never re-try failed ideas** — Check `memory_search` and experiment log before proposing.
7. **Simplicity wins** — Equal metrics + less code → keep. Marginal gain + complexity → discard.

## Iteration Logging

Each iteration is logged with:
- Iteration number
- Hypothesis (what you tried)
- Outcome: `keep` / `discard` / `error`
- Metrics: before and after
- Reason (why kept or discarded)
- Git commit hash (if kept)

## Research Areas (Rotate When Stuck)

If 5 consecutive discards in one area, move to the next:
1. Technical indicators (momentum, trend, mean-reversion)
2. Signal generation (combining indicators into tradeable signals)
3. Sentiment-price correlation (does Reddit/news sentiment predict moves?)
4. Data pipeline improvements (faster collection, better coverage)
5. Backtest infrastructure (more sophisticated metrics, walk-forward)

## Adaptation

This loop works on ANY measurable improvement task in the repo — not just adding indicators. Examples:
- Increase test coverage
- Reduce code complexity
- Add new data sources
- Improve backtest Sharpe ratio
- Optimize query performance
