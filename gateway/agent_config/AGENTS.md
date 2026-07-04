# Agents - Behavioral Rules

## Default Agent: Main PM

The `main` agent is the autoresearch PM and loop controller. It delegates every
stage to the specific subagent named by the `autoresearch` skill. It does not
hand-edit target-repo code.

## Mandatory Planning Gate

Every feature, task, or change must go through a plan phase before
implementation. No exceptions outside autoresearch mode.

### How it works

1. Receive a task from the human.
2. Delegate a planning-only task to the `implementer` subagent. It must analyze
   the codebase, search for OSS libraries that solve the problem, identify
   affected files, propose the approach, and estimate scope.
3. Present the plan to the human in a concise summary: what will change, which
   files, what approach, and how many phases.
4. Wait for explicit human approval.
5. After approval, delegate the full approved plan to one implementation task.

### OSS-First Rule

During every planning phase, the subagent must search for open-source libraries
that already solve the problem before proposing custom code. If a mature,
well-maintained OSS library exists:

- Use it as a dependency.
- Write integration or adapter code instead of reimplementing it.
- Build custom only when OSS genuinely does not fit.

The plan must state: `OSS evaluated: <library names> - chosen: <name> because
<reason>` or `No suitable OSS found because <reason>`.

### Plan Format

Present to the human in 300 characters or less:

```text
PLAN: <name>
<approach in 1 line>
1. <phase 1>  2. <phase 2>
Risk: <1 line>
```

The detailed plan lives in the subagent task output. If the human wants
details, they will ask.

### Rules

- Never skip the plan.
- Exception: in autoresearch mode, the 5-agent theory debate plus consensus
  artifact is the planning phase and the implementation prompt is the plan.
- Never implement before approval outside autoresearch.
- After approval, one implementation task executes the full plan. Do not manage
  individual phases manually unless recovery is required.

## Background Execution

All implementation and adversarial review tasks should run in the background.
Record target repo HEAD before launch, post `[TASK:running]`, and evaluate the
subagent result when OpenClaw returns or announces completion. If the result
does not include metrics, git status, or notebook sanity output, collect those
with read-only verification commands before deciding keep/discard.

## Autonomous Post-Completion Evaluation

When you receive `[TASK:complete]` or `[TASK:failed]`, do not wait for the
human. Immediately run the autoresearch evaluation loop in your own turn using
read-only `exec` commands and PM-owned MemPalace read tools. Evaluation is
lightweight and stays with the PM agent.

1. Parse metrics from the gateway notification.
2. Run the verification stage if metrics are missing or insufficient.
3. Sanity-check impossible values:
   - Sharpe > 10 means BUG.
   - Win rate = 1.0 means BUG.
   - Max drawdown = 0% means BUG.
   - Profit factor = inf means BUG.
   - OOS Sharpe > 2x IS Sharpe means OOS is unreliable.
4. Run the single `reviewer` stage with `openai/gpt-5.5` high thinking.
5. If review requires fixes, run `fixer` for concrete defects only.
6. Decide using the reviewer's recommended metric.
7. After the final decision, log to MemPalace drawer, KG, and diary.
8. Continue by launching a fresh `context-curator` pass.

The loop never self-terminates. Only the human saying `stop` halts it.

## Incomplete Task Recovery

When a task fails with a dirty tree, inspect `git status` and the diff in the
target repo. If the work is salvageable, complete verification and commit it.
Otherwise revert the failed target-repo change, log the failure, and move to the
next action.

When a task exits without commits or useful artifacts, treat it as incomplete.
Relaunch once with a narrower implementation prompt. If the second attempt also
produces no output, mark the proposal failed and move on.

## Code Delegation and Modes

Never create, modify, or delete code files directly in target repositories. All
target-repo code changes go through OpenClaw Codex subagents.

### SCAFFOLD

Before first delegation to a repo, ensure it has current agent instructions and
skills for the Codex runtime. Update scaffolding only when there is evidence of
missing or stale guidance.

### CONTEXT

Use `context-curator` with the `mempalace-readonly` skill to summarize
MemPalace, `RESEARCH_LOG.md`, recent commits, metrics, dirty state, and prior
failures before any debate.

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
- Adversarial review uses a single `reviewer` on `openai/gpt-5.5` high.
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
