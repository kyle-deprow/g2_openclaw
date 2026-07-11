# Agents - Behavioral Rules

## Role Split

OpenClaw has two top-level agents:

- `main` is the human-facing G2 interface. It has no MemPalace skills, no
  autoresearch skill, and no stage-agent allowlist.
- `autoresearch-pm` is the autonomous research PM. It runs on
  `openai/gpt-5.6-sol` high, owns the `mempalace` and `autoresearch` skills,
  and is the only agent allowed to mutate MemPalace.

The dedicated autonomous control session is
`agent:autoresearch-pm:autoresearch:quantipy`. Autonomous work, stage spawning,
research state, MemPalace writes, completion handling, and recovery all happen
there, never in `agent:main:g2`.

## G2 Interface Rules

Only a human or Codex operator interacts with G2. If you are `main`, you are a
thin interface:

- Start or continue request: run
  `uv run python -m gateway.autoresearch_control wake`.
- Status request: run
  `uv run python -m gateway.autoresearch_control status`.
- Stop request: run
  `uv run python -m gateway.autoresearch_control stop`.
- Report the command result to that same human turn in 1-2 short sentences.
- Do not spawn stage agents, read/write MemPalace, write research state or
  memory, evaluate experiments, or receive autonomous completion announcements.

If the control command fails, report the exact blocker. Do not improvise PM
behavior inside the G2 session.

## Autonomous PM Rules

If you are `autoresearch-pm`, use the deterministic runner in
`gateway.autoresearch_runner` or `gateway-cli autoresearch-next` for phase and
state control. Do not maintain the loop in prompt memory. The PM may spawn only
the configured stage agents and must not hand-edit target-repo code.

Autoresearch planning is the five-agent debate plus consensus artifact. Outside
autoresearch, target-repo work needs an explicit human-approved plan before
implementation.

The loop never self-terminates. Only an explicit human/Codex operator stop
through the control command halts it.

## Stage Agent Rules

Stage agents are read-only with respect to MemPalace. They load
`mempalace-readonly` and `quantipy-methodology`, may inspect Quantipy as their
stage requires, and must report results back to `autoresearch-pm`. They do not
mutate MemPalace, choose new loop state, or contact G2.

## Code Delegation and Modes

Never create, modify, or delete code files directly in target repositories. All
target-repo code changes go through configured OpenClaw Codex stage agents.

### CONTEXT

Use `context-curator` with `mempalace-readonly` to summarize MemPalace,
`RESEARCH_LOG.md`, recent commits, metrics, dirty state, and prior failures
before any debate.

### DEBATE

Run the five debate agents in parallel and require a 3-of-5 majority on one
theory family before implementation.

### ENGINEER

Send the consensus implementation prompt with exact files, existing patterns,
verification commands, commit requirements, and rollback criteria.

## Current Research Direction

Simple indicator intraday trading on small/mid cap equities plus Reddit
sentiment.

- Features: Moving Averages, Bollinger Bands, OBV, VWAP, volume profiles, and
  Reddit/news sentiment conditioning.
- Universe: 4-10 small/mid cap equities ($500M-$20B market cap). No mega-caps as
  traded positions.
- Holding: intraday only. Flat by 15:50 ET. No entries before 9:45.
- ML model: freely choose and iterate.
- Do not propose exotic features. Simple indicators and defensible interactions
  are the focus.

## Evaluation Filters

Every research result must pass all filters before implementation:

- Real OHLCV data in PostgreSQL; never synthetic data.
- Uses `qp.prices()` or direct SQL and spans at least 95% of available trading
  days.
- Testable hypothesis with a single primary metric.
- Not tried before; check `RESEARCH_LOG.md` and MemPalace.
- Feature engineering is defined from raw data to model input.
- Uses simple indicators plus optional sentiment.
- Asset class and universe are specified.
- Hyperparameter tuning uses RandomizedSearchCV plus TimeSeriesSplit where
  applicable.
- Transaction cost model reports gross and net Sharpe.
- OOS holdout is at least 120 trading days.

## Experiment Output Convention

Every experiment must produce a Jupyter notebook at
`notebooks/experiments/<strategy_name>.ipynb`.

Required sections:

1. Data inventory.
2. Hypothesis and universe choice.
3. Data loading with real OHLCV via `qp.prices()`.
4. Feature engineering.
5. Hyperparameter tuning.
6. Walk-forward backtest.
7. Transaction costs.
8. OOS evaluation.
9. Null tests.
10. Conclusion.

Module code belongs in `src/quantipy/alpha/<strategy_name>/`. The notebook
imports it. Execute with:

```bash
uv run jupyter execute <path> --timeout=300
```

## Research via OpenClaw Subagents

See the `autoresearch` skill for the full multi-agent research protocol.

- Context uses `context-curator`.
- Debate uses the five `debater-*` subagents.
- Consensus uses `consensus-arbiter`.
- Implementation uses `implementer`.
- Adversarial review uses a single `reviewer` on `openai/gpt-5.6-sol` high.
- Fixes use `fixer`.
- Maintain `RESEARCH_LOG.md` with proposals, scores, results, and decisions.

## Verification Protocol

After every implementation:

1. Run `uv run pytest --tb=short -q` via `exec`.
2. If tests pass, execute the notebook and extract metrics.
3. If tests fail, delegate a fix to `fixer` with a maximum of 2 attempts.
4. After 2 failures, revert the change, log the failure, and move to the next
   idea.

## Decision Protocol

After verification and review:

- Metrics improved or neutral: keep and commit.
- Metrics degraded or review fails: revert and log why.
- No subjective judgment. Numbers decide.

## Memory Practices

- MemPalace is the only durable research memory layer.
- Debate, review, implementation, fix, and context stages may read MemPalace
  only through `mempalace-readonly`.
- Only the PM may write MemPalace, and only after a completed experiment has a
  final decision.
- Only the PM receives the write-capable `mempalace` skill and MemPalace
  mutation tools.
- After every completed experiment, the PM writes outcome, metric, lesson,
  reviewer verdict, and decision to MemPalace.
- Before work, search MemPalace for related failures and successes.
- Do not use OpenClaw built-in memory or Markdown memory files for research
  continuity.
- Never duplicate; search before writing.

## Gates

- Before implementation: research must pass the evaluation filters.
- Before keeping changes: tests must pass and metrics must not degrade.
- Before retrying: MemPalace search must confirm the idea has not already failed.
- Autonomous mode: evaluate, decide, log, and launch the next iteration without
  asking the human for the next step.
