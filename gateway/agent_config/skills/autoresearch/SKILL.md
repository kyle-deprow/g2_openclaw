---
name: autoresearch
description: Autonomous research loop using OpenClaw Codex subagents for ideation, implementation, and adversarial review. PM evaluates metrics, logs to MemPalace, and continues without human intervention after approval.
version: 6.0.0
---

# Autoresearch - Autonomous Iteration Protocol v6

Autoresearch is a behavioral mode for autonomous, metric-driven iteration. The
PM agent runs the loop, delegates substantial research and implementation work
to OpenClaw Codex subagents, evaluates results mechanically, logs outcomes, and
continues until the human explicitly says `stop`.

## Execution Model

You run the loop in your own turn using OpenClaw tools.

- Phase 1, 4, 5, 6, and 7 are PM responsibilities.
- Phase 2 delegates ideation to the `researcher` subagent, which coordinates
  `contrarian`, `explorer`, and `theorist`.
- Phase 3 delegates implementation to the `orchestrator` subagent.
- Phase 4.5 delegates methodology review to the `reviewer` subagent.
- Long ideation, implementation, and review tasks run in the background.
- Do not silently switch providers or runtimes. If the Codex runtime or selected
  model is unavailable, fail closed and report the blocker.

## When to Activate

- User says `autoresearch`, `iterate autonomously`, `keep improving`,
  `run overnight`, or `research loop`.
- A task requires repeated modify -> verify -> measure cycles with a mechanical
  metric.
- The user wants hands-off improvement of code, tests, performance, or a
  quantitative target.

## Setup Phase

Do once at the start of a loop:

1. Read in-scope files and repo instructions.
2. Define the mechanical goal and metric.
3. Define scope: writable files, read-only files, and target repo.
4. Define metric direction.
5. Establish baseline and record iteration 0.
6. Search memory for previous attempts.
7. Check MemPalace status and recent diary entries.
8. Confirm target-repo subagent scaffolding exists for `researcher`,
   `contrarian`, `explorer`, `theorist`, `orchestrator`, and `reviewer`.

## The Loop

```text
LOOP until user says stop:
  Phase 1 - REVIEW
  Phase 2 - IDEATE
  Phase 3 - IMPLEMENT
  Phase 4 - VERIFY
  Phase 4.5 - ADVERSARIAL REVIEW
  Phase 5 - DECIDE
  Phase 6 - LOG
  Phase 7 - REFLECT when due
  Phase 8 - CONTINUE
```

## Phase 1 - Review

Build situational awareness:

- Read current in-scope files.
- Read recent `RESEARCH_LOG.md` entries.
- Run `git log --oneline -20`.
- Search memory for related experiments and research rounds.
- Query MemPalace for prior experiments, failure modes, and successful
  feature/model combinations.
- Identify unimplemented proposals from the current research round.
- If an unimplemented proposal exists, skip Phase 2 and implement the next
  ranked proposal.

## Phase 2 - Ideate

Delegate the research debate to the `researcher` subagent.

Prompt requirements:

- Current best metric and baseline.
- Last 10 experiment log entries.
- Strategies already tried and rejected.
- Available data sources in Quantipy.
- MemPalace findings on prior failures and successful features.
- Human's current research direction.
- Required output: ranked proposals with scores, rationale, universe, data split,
  transaction cost model, walk-forward design, and rejection reasons for losers.

Quantipy constraints:

- Intraday trading on small/mid cap equities ($500M-$20B market cap).
- Large caps may be signal sources but never traded positions.
- Reddit/news sentiment may be used for feature generation or conditioning.
- Universe selection is part of the research.
- All experiments use real OHLCV data from the database.
- Use 2021-2026 data and at least 95% of available trading days.
- No overnight holding periods.
- No synthetic data.
- Simple indicators are the core thesis: Moving Averages, Bollinger Bands, OBV,
  VWAP, volume profiles, plus optional sentiment conditioning.
- Hyperparameter tuning must use a time-series-aware split.

After completion:

- Extract the winning idea and all ranked proposals.
- Write the proposals to `RESEARCH_LOG.md` and memory.
- This ranked list is the implementation queue.

## Phase 3 - Implement

Pick the top-ranked unimplemented proposal from `RESEARCH_LOG.md` and delegate
to the `orchestrator` subagent.

Implementation prompt requirements:

- Proposal name, description, and ML model type.
- Feature engineering pipeline from raw data to model input.
- Universe and traded tickers with justification.
- Data loading via `qp.prices()` or direct SQL from PostgreSQL.
- Walk-forward parameters from the proposal; if absent, use the target repo's
  experiment-data skill defaults.
- Transaction cost model.
- Module path: `src/quantipy/alpha/<strategy_name>/`.
- Notebook path: `notebooks/experiments/<strategy_name>.ipynb`.
- Unit test path.
- Verification commands.
- Commit requirement only after tests and notebook execution pass.

Notebook requirements:

1. Data inventory with actual DB rows and date ranges.
2. Hypothesis and universe choice.
3. Data loading from real OHLCV.
4. Feature engineering with distributions and correlations.
5. Hyperparameter tuning.
6. Walk-forward backtest with purge/embargo where applicable.
7. Transaction costs with gross and net Sharpe.
8. OOS evaluation with at least 120 trading days.
9. Null tests.
10. Conclusion with keep/iterate/discard recommendation.

Critical backtest rule: holding period must match prediction horizon. If the
model predicts a 15-bar forward return, the backtest must hold for 15 bars and
must not recalculate a new overlapping position every bar.

## Phase 4 - Verify

Evaluation is PM work. Do not delegate basic metric extraction.

If the task notification already includes sufficient metrics, use them. If not,
run focused read-only commands in the target repo:

```bash
uv run pytest -q --tb=short --ignore=tests/integration
uv run jupyter nbconvert --execute --inplace --ExecutePreprocessor.timeout=300 notebooks/experiments/<strategy_name>.ipynb
```

Extract:

- IS walk-forward Sharpe net.
- OOS Sharpe net.
- Max drawdown.
- Win rate.
- Trade count and trades/day.
- OOS trading days.
- Feature importances.
- Null test results.

Sanity checks before interpreting metrics:

- Sharpe > 10 means BUG.
- Win rate = 1.0 means BUG.
- Max drawdown = 0.00% means BUG.
- Profit factor = inf means BUG.
- Accuracy < 50% with Sharpe > 5 means BUG.
- OOS Sharpe > 2x IS Sharpe means OOS is unreliable; use IS walk-forward Sharpe.

If any sanity check triggers, delegate a targeted bug fix to `orchestrator`,
then rerun Phase 4.

## Phase 4.5 - Adversarial Review

Every experiment gets reviewed by `reviewer` before any keep/discard decision.

Reviewer prompt requirements:

- Notebook path.
- Module code path.
- Tests path.
- Extracted metrics from Phase 4.
- Request all methodology checks: leakage, OOS reliability, feature importance,
  holding period alignment, transaction costs, data coverage, null tests, and
  reproducibility.
- Required output: verdict (`PASS`, `CONDITIONAL PASS`, `FAIL`), recommended
  decision metric, issues by severity, and recommended action.

The reviewer's recommended metric overrides raw OOS claims.

## Phase 5 - Decide

Use the reviewer's recommended metric. If review is unavailable after a
reasonable retry, use IS walk-forward Sharpe net and log the missing review.

Thresholds:

- Tests fail: CRASH.
- Critical review issue: BUG FIX.
- Decision Sharpe <= -0.5: DISCARD.
- Decision Sharpe improves baseline: KEEP.
- Decision Sharpe > 0.5: SIGNIFICANT KEEP.
- Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP.
- Max drawdown >= 30%: DISCARD regardless of Sharpe.

Actions:

- KEEP: keep commit, record metrics and reviewer verdict.
- DISCARD: revert the experiment commit, record why, then move on.
- CRASH: attempt fix up to 3 times, then revert and log.
- BUG FIX: delegate fix, rerun Phase 4 and Phase 4.5.

## Phase 6 - Log

Log every result:

- Append to `results/experiments.jsonl` or the target repo's equivalent log.
- Update `RESEARCH_LOG.md` with proposal status and metrics.
- Write a memory note with strategy name, outcome, Sharpe, and lesson.
- If SIGNIFICANT KEEP or STRONG KEEP, update durable memory.
- Add a MemPalace drawer with the full experiment result.
- Add KG facts for experiment -> feature, experiment -> metric,
  experiment -> model, experiment -> decision, and experiment -> failure mode.

MemPalace errors are additive failures. Continue the loop and mention the
logging gap.

## Phase 7 - Reflect

Run after every 3 implementations or when all proposals in a round are exhausted.

- Query MemPalace for experiment results, recurring failure modes, and features
  used in successful experiments.
- Read `RESEARCH_LOG.md`.
- Analyze keep/discard/crash rate.
- Check whether high-ranked proposals outperform low-ranked proposals.
- Identify which subagent source produced the best ideas.
- Identify model and feature families that work or fail.
- Update memory with scoring adjustments for the next ideation round.
- Write a MemPalace diary entry.

If target-repo agent scaffolding caused repeated failures, delegate a focused
scaffolding update to `orchestrator` before the next implementation.

## Phase 8 - Continue

Do not stop after evaluating a single experiment.

Decision tree:

- Backtesting bug: delegate bug fix and re-evaluate.
- KEEP with Sharpe < 1.0: try one optimization or feature iteration.
- STRONG KEEP: log as a portfolio candidate, post a `[PORTFOLIO]` status, then
  continue to the next orthogonal proposal.
- DISCARD: move to the next ranked proposal.
- All proposals exhausted: run Phase 7, then Phase 2 with updated context.

The loop continues until the human says `stop`.

## Critical Rules

1. Loop until stopped by the human.
2. Read before write.
3. One focused change per iteration.
4. Use mechanical metrics only.
5. Revert failed changes.
6. Never retry failed ideas without new evidence.
7. Prefer simple working strategies over complex fragile ones.
8. Commit every kept change.
9. If stuck, re-read context and try a materially different direction.

## Results Logging

Track every iteration in `results/experiments.jsonl` or the project-specific
equivalent. One JSON object per line:

```jsonl
{"meta": true, "metric_direction": "higher_is_better", "metric_name": "is_walk_forward_sharpe_net", "goal": ">0.5"}
{"iteration": 0, "commit": "a1b2c3d", "metric": 0.0, "delta": 0.0, "status": "baseline", "description": "initial baseline"}
{"iteration": 1, "commit": "b2c3d4e", "metric": 0.7, "delta": 0.7, "status": "keep", "description": "sentiment-gated VWAP reversion"}
{"iteration": 2, "commit": "-", "metric": -0.8, "delta": -1.5, "status": "discard", "description": "OBV breakout failed after costs"}
```

Every 10 iterations, post:

```text
=== Autoresearch Progress (iteration 20) ===
Baseline: 0.00 -> Current best: 0.92 (+0.92)
Keeps: 8 | Discards: 10 | Crashes: 2
```

## Loop Recovery

On reconnect or session resume:

1. Check whether autoresearch was active.
2. Check running background OpenClaw tasks.
3. If no task is running and the loop was active, re-enter at Phase 1.
4. If a task is running, monitor it.
5. When a `[TASK:*]` message arrives, evaluate and continue. Never acknowledge
   it and stop.

## Status Reporting

- Post `[TASK:running] autoresearch iteration N` after each launch.
- Post `[TASK:complete] autoresearch - N iterations, metric: X -> Y` only when
  the human stops the loop or a declared finite goal is complete.
- Every 10 iterations, post a progress summary.
- Write significant findings to memory and MemPalace.
