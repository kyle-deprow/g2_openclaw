---
name: autoresearch
description: PM-owned autonomous research loop for Quantipy using MemPalace, five-agent debate, Codex implementation, and a single high-reasoning reviewer.
version: 7.0.0
---

# Autoresearch

Autoresearch is a PM-owned loop. The PM keeps the state, chooses the next
stage, verifies metrics, logs to MemPalace, and repeats until the human says
`stop`.

Durable research memory is MemPalace only. Do not use `memory_search`,
`memory_get`, flat daily memory files, or OpenClaw memory flush for research
continuity. Only the PM loads the write-capable `mempalace` skill; all stage
agents load `mempalace-readonly`.

## Loop

```text
context-curator
  -> 5-agent debate
  -> consensus
  -> implement
  -> single reviewer
  -> fix/test
  -> decide/log
  -> repeat
```

## Model Policy

- Every gpt-5.4 stage must run with high reasoning.
- The PM agent `main` must run on `openai/gpt-5.5` with high reasoning.
- At least half of the five debate agents must be `openai/gpt-5.5` with high
  reasoning. The configured panel uses three.
- The reviewer is exactly one stage: `reviewer` on `openai/gpt-5.5` with high
  reasoning. Do not run a reviewer panel.
- Spawn by configured agent ID only. Do not use generic/default agents,
  inherited parent models, or per-spawn model overrides for autoresearch stages.
- Do not silently switch provider, runtime, model, or reasoning level. If the
  configured route is unavailable, fail closed and report the blocker.

## Stage Agents

Use the configured agents by ID. Their model bindings are part of the repo
config and are validated by the push script. Prior target-repo Codex roles may
inform prompt content, but they are not OpenClaw stage names.

| Stage | Agent | Model intent |
|-------|-------|--------------|
| Context | `context-curator` | `openai/gpt-5.4`, high |
| Debate 1 | `debater-microstructure` | `openai/gpt-5.5`, high |
| Debate 2 | `debater-data` | `openai/gpt-5.5`, high |
| Debate 3 | `debater-skeptic` | `openai/gpt-5.5`, high |
| Debate 4 | `debater-theory` | `openai/gpt-5.4`, high |
| Debate 5 | `debater-implementation` | `openai/gpt-5.4`, high |
| Consensus | `consensus-arbiter` | `openai/gpt-5.4`, high |
| Implement | `implementer` | `openai/gpt-5.4`, high |
| Review | `reviewer` | `openai/gpt-5.5`, high |
| Fix | `fixer` | `openai/gpt-5.4`, high |

Every stage agent except `main` loads `mempalace-readonly` and
`quantipy-methodology`. The methodology skill requires stage agents to read the
current Quantipy source-of-truth files from `/home/dev/repos/quantipy`
(`AGENTS.md`, relevant `.agents/skills`, and relevant `.codex/agents`) before
context, debate, consensus, implementation, review, or fix work. Do not copy
those target-repo files into G2 OpenClaw.

## Setup

Do once before the first iteration:

1. Define the mechanical goal, metric, metric direction, target repo, and
   writable scope.
2. Establish the baseline and record iteration 0.
3. As PM, read `RESEARCH_LOG.md`, recent git history, current in-scope files,
   and MemPalace prior experiments with `mempalace_status`,
   `mempalace_diary_read`, `mempalace_search`, and `mempalace_kg_query`.
4. Confirm MemPalace tools are available. If unavailable, pause the loop and
   report the infrastructure blocker.

## 1. Context Curator

Spawn `context-curator` with read-only MemPalace access to produce a compact
packet for the debate:

- Current best metric, baseline, and last 10 experiment outcomes.
- Prior MemPalace findings: failures, keeps, feature families, model families,
  data coverage issues, and reviewer objections.
- Open proposals from `RESEARCH_LOG.md`, marked as prior context only.
- Hard constraints and available data sources.

Do not skip debate because a prior proposal exists. The debate must consider
prior proposals, current metrics, and MemPalace history before selecting the
single next theory.

## 2. Five-Agent Debate

Spawn the five configured debate agent IDs with the same context packet and ask
each for one theory, a vote on the strongest theory family, and objections to
likely failure modes. Do not substitute a generic debater or override the
configured model.

Every proposal must include:

- Hypothesis and why the signal should exist.
- Universe and traded tickers; large caps may be sources but not traded names.
- Feature pipeline from raw OHLCV/sentiment to model input.
- Model type and hyperparameter search plan.
- Walk-forward split, purge/embargo if applicable, and OOS holdout.
- Transaction cost model.
- Full data coverage plan using 2021-2026 and at least 95% of available trading
  days.
- Rejection criteria.

Quantipy constraints:

- Intraday small/mid cap equities only, $500M-$20B market cap.
- No overnight holds; flat by the target repo's close-out rule.
- Real PostgreSQL OHLCV via `qp.prices()` or direct SQL. No synthetic data.
- Simple indicator core: Moving Averages, Bollinger Bands, OBV, VWAP, volume
  profiles, and optional Reddit/news sentiment conditioning.
- Hyperparameter tuning must use time-series-aware splits.

## 3. Consensus

Spawn `consensus-arbiter` to determine whether one theory has a 3-of-5
majority. Required output:

- Winner, majority count, dissenting positions, or `NO_CONSENSUS`.
- Scores for novelty, theory, implementation risk, data adequacy, overfit risk,
  and expected net Sharpe.
- Explicit reasons losers were rejected.
- Final implementation brief.

If there is no 3-of-5 majority, run one concise debate retry with the same
context plus the dissent summary. If there is still no majority, log
`NO_CONSENSUS` to `RESEARCH_LOG.md`, then start a fresh context pass. Do not
implement without a majority.

The PM writes the winning theory and dissent summary to `RESEARCH_LOG.md`
before implementation. Do not write theories, debate notes, or consensus drafts
to MemPalace before an experiment is completed and decided.

## 4. Implement

Spawn `implementer` with the final implementation brief.

Implementation requirements:

- Module path: `src/quantipy/alpha/<strategy_name>/`.
- Notebook path: `notebooks/experiments/<strategy_name>.ipynb`.
- Unit tests for feature generation, split logic, and metric extraction.
- Notebook sections: data inventory, hypothesis, real data loading, feature
  engineering, tuning, walk-forward backtest, transaction costs, OOS evaluation,
  null tests, and conclusion.
- Backtest holding period must match prediction horizon.
- Commit only after tests and notebook execution pass.

## 5. Verify

The PM extracts metrics. Use task output if sufficient; otherwise run focused
read-only commands in the target repo.

Required metrics:

- IS walk-forward Sharpe net.
- OOS Sharpe net.
- Max drawdown.
- Win rate.
- Trade count and trades/day.
- OOS trading days.
- Feature importances.
- Null test results.

Bug signals:

- Sharpe > 10.
- Win rate = 1.0.
- Max drawdown = 0%.
- Profit factor = inf.
- Accuracy < 50% with Sharpe > 5.
- OOS Sharpe > 2x IS Sharpe; use IS walk-forward Sharpe instead.

If a bug signal appears, send a targeted fix to `fixer`, then rerun verification.

## 6. Single Reviewer

Spawn exactly one `reviewer` on its configured `openai/gpt-5.5` high-reasoning
binding.

Reviewer focus:

- Was the chosen theory implemented correctly?
- Was the full intended dataset and timerange used, including 2021-2026 and at
  least 95% of available trading days?
- Did the experiment avoid cherry-picking tickers, windows, parameters, and
  thresholds?
- Did the method avoid leakage, overfitting, overlapping-hold errors, and
  transaction-cost omissions?
- Are null tests and OOS evaluation sufficient to trust the recommended metric?

Required output: verdict (`PASS`, `CONDITIONAL PASS`, `FAIL`), recommended
decision metric, critical issues, noncritical issues, and exact fix requests.

## 7. Fix/Test

- Critical reviewer issue: send a narrow fix to `fixer`, rerun tests/notebook,
  then rerun the single reviewer.
- Test failure: fix up to two times, then revert and log CRASH.
- No methodology issue: proceed to decide/log.

## 8. Decide And Log

Use the reviewer's recommended metric.

- Tests fail after retries: CRASH.
- Critical review issue remains: DISCARD.
- Decision Sharpe <= -0.5: DISCARD.
- Decision Sharpe improves baseline: KEEP.
- Decision Sharpe > 0.5: SIGNIFICANT KEEP.
- Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP.
- Max drawdown >= 30%: DISCARD regardless of Sharpe.

Actions:

- KEEP: keep the commit, update baseline if appropriate, and log the metrics.
- DISCARD/CRASH: revert the experiment commit, log why, and move to the next
  proposal.
- Always append to the target repo's experiment log and `RESEARCH_LOG.md`.
- After the final decision, the PM writes MemPalace drawers and KG facts for
  experiment, feature, model, metric, decision, and failure mode.
- Write a MemPalace diary entry only as part of final experiment logging.

## Recovery And Status

- On resume, read `RESEARCH_LOG.md`, git status, active background tasks, and
  MemPalace. Re-enter the first incomplete stage.
- Post `[TASK:running] autoresearch iteration N` after each launch.
- Post progress every 10 iterations.
- Do not stop after one experiment. Repeat until the human says `stop` or a
  declared finite goal is reached.
