---
name: autoresearch
description: PM-owned autonomous research loop for Quantipy using MemPalace, five-agent debate, Codex implementation, and a single high-reasoning reviewer.
version: 7.2.0
---

# Autoresearch

Autoresearch is a PM-owned loop, but the loop state and next-stage selection
must come from the deterministic runner in `gateway.autoresearch_runner` (or
`gateway-cli autoresearch-next`), not from prompt memory. Because the PM
workspace is outside this repo, invoke it exactly as `cd
/home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next
/home/dev/.openclaw/autoresearch/quantipy-state.json`. The PM uses that
control-plane output to choose the next stage, verify metrics, log to MemPalace,
and repeat until the human says `stop`.

Durable research memory is MemPalace only. Do not use `memory_search`,
`memory_get`, flat daily memory files, OpenClaw memory flush, or prompt-only
loop memory for research continuity. Only the PM loads the write-capable
`mempalace` skill; all stage agents load `mempalace-readonly`.

## Explicit Research Modes

The context packet must select exactly one mode and give a nonempty
`mode_rationale`; the deterministic state persists that choice.

- `ALPHA_RESEARCH` is for a strategy experiment. It may produce performance
  metrics and a KEEP/DISCARD-family decision only after coverage verification.
- `DATA_INFRA_G0` is for repairing data range, provenance, universe, or fold
  construction. It is not an alpha experiment and must never be presented as
  Sharpe/performance validation. Its verification returns an explicit
  infrastructure gate outcome and its final decision is `INFRA_REPAIRED` or
  `INFRA_BLOCKED`.

The context packet also records normalized `burned_theory_families`. In alpha
mode, a debate submission in a burned family is rejected unless it contains a
nonempty `materially_new_evidence` explanation. Do not evade this gate by
renaming the family.

## Loop

```text
context-curator
  -> 5-agent debate
  -> consensus
  -> implement
  -> verify
  -> single reviewer
  -> fix/test
  -> decide/log
  -> repeat
```

## Model Policy

- Every stage must run with high reasoning.
- The PM agent `autoresearch-pm` must run on `openai/gpt-5.6-sol` with high
  reasoning.
- The five debate agents use the configured mixed-intelligence panel: strongest
  reasoning for data/skeptic pressure, lower-cost models for bounded theory and
  implementation feasibility.
- The consensus arbiter must run on `openai/gpt-5.6-sol` with high reasoning.
- The reviewer is exactly one stage: `reviewer` on `openai/gpt-5.6-sol` with high
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
| Debate 2 | `debater-data` | `openai/gpt-5.6-terra`, high |
| Debate 3 | `debater-skeptic` | `openai/gpt-5.6-sol`, high |
| Debate 4 | `debater-theory` | `openai/gpt-5.4`, high |
| Debate 5 | `debater-implementation` | `openai/gpt-5.4`, high |
| Consensus | `consensus-arbiter` | `openai/gpt-5.6-sol`, high |
| Implement | `implementer` | `openai/gpt-5.4`, high |
| Review | `reviewer` | `openai/gpt-5.6-sol`, high |
| Fix | `fixer` | `openai/gpt-5.4`, high |

Every stage agent except `autoresearch-pm` loads `mempalace-readonly` and
`quantipy-methodology`. The methodology skill requires stage agents to read the
current Quantipy source-of-truth files from `/home/dev/repos/quantipy`
(`AGENTS.md`, relevant `.agents/skills`, and relevant `.codex/agents`) before
context, debate, consensus, implementation, review, or fix work. Do not copy
those target-repo files into G2 OpenClaw.

## Execution Hygiene

The loop has full authority to choose and test research strategies. Infra
guardrails exist only to keep execution clean:

- Ownership boundary: shared Quantipy platform infrastructure, shared test
  harnesses/fixtures, dependency/runtime/tooling, G2/OpenClaw orchestration,
  supervisor/recovery, and monitoring belong to the human operator or Codex.
  Every alpha module, experiment notebook, experiment-specific unit test, and
  research metric/methodology behavior belongs to autoresearch even when a
  dependency upgrade exposed the bug.
- Numerical runtime caps on OpenClaw-launched Quantipy processes are
  operator-managed shared infrastructure. PM and stage agents must not override,
  remove, or work around them. A systemd `daemon-reload` after installing the
  drop-in does not change an already running OpenClaw Gateway process; only an
  external operator restart makes the caps effective for subsequently launched
  children.
- Classification test: if a change alters strategy/features/folds/models/null
  tests/metrics, do not operator-edit it; record the exact failure and let the
  loop implement/review/fix it. If it only repairs shared execution substrate,
  record the blocker with exact evidence and await human/Codex operator action
  in the authoritative checkout.
- On confirmed shared-infrastructure failure, the PM must report/block with
  exact evidence, including the failing command or test, relevant path, process
  or route status, timestamps, and any decisive log lines. Then wait for the
  human operator or Codex. The PM never stops or relaunches the loop, repairs
  shared infrastructure, promotes shared-infrastructure patches, or touches G2.
- Every spawned stage must use a deterministic unique label derived from
  persisted state, never a generic reused stage name. Format:
  `autoresearch-i{iteration}-{stage}-r{round}-a{attempt}`. The same
  iteration/stage/round/attempt tuple must map to exactly one label, and any
  retry or recovery that changes round or attempt must change the label.
- On recovery, reconcile every expected label against the task ledger and its
  child-session transcript before waiting. Consume terminal outputs even if the
  completion announcement was missed. Relaunch only a task with no recoverable
  terminal output, using the next attempt label; never wait indefinitely for an
  announcement from a terminal task.
- `gateway-cli autoresearch-next` fails closed if the Quantipy worktree contains
  unapproved dirty files before a stage launch. The persistent
  `docs/quantipy_experiment_mempalace_preload.md` audit note is the only
  default allowlisted local file.
- All implementation and Fix/Test workspaces must be under the exact canonical
  operator-controlled root `/home/dev/.openclaw/autoresearch/worktrees`; never
  use `/tmp` or another fallback. Create that parent with `mkdir -p` before
  `git worktree add`. `/tmp` is a 31G tmpfs and each Quantipy worktree virtualenv
  is about 1.5G, so stale iteration worktrees can exhaust it. Fix/Test reuses
  the exact persisted implementation worktree and accepted experiment commit;
  it never creates another worktree.
- `gateway-cli autoresearch-advance` mechanically verifies implementation and
  fix artifacts: `workspace_path` is the strict canonical resolved worktree
  path (no symlink, `..`, or other alias), the workspace exists, is a registered
  Git worktree distinct from the authoritative target checkout, is clean, and
  has the artifact commit at `HEAD`. Implementation evidence, the persisted
  implementation workspace, and the Fix/Test workspace must all be below
  `/home/dev/.openclaw/autoresearch/worktrees`; every path outside that root
  fails closed. For Fix/Test it additionally verifies the persisted
  implementation and fix artifact use that same exact canonical path, and that
  the implementation commit and authoritative target `HEAD` are ancestors of
  the final fix commit.
- Behavioral guardrails (not mechanically provable): before editing, preserve
  unrelated work; only incorporate already-authoritative human/Codex shared
  infrastructure; never independently edit shared infrastructure; and fail
  closed if reconciliation is ambiguous or risks unrelated loss. No background
  jobs may remain when the stage exits.
- Never promote experiment changes from `INFRA_BLOCKED`, `DISCARD`, or `CRASH`
  disposable worktrees. Only the human operator or Codex may promote
  independently reviewed shared-infrastructure patches in the authoritative
  checkout. The PM never promotes.
- Do not run concurrent `pytest` processes in the same checkout because
  coverage state can corrupt. Serialize verification or use isolated
  worktrees.
- Crash residue from a prior iteration is not evidence and must not be reused
  as scaffolding unless a later committed experiment explicitly owns it.

## Setup

Do once before the first iteration:

1. Define the mechanical goal, metric, metric direction, target repo, and
   writable scope.
2. Establish the baseline and record iteration 0.
3. As PM, read `RESEARCH_LOG.md`, recent git history, current in-scope files,
   and MemPalace prior experiments with `mempalace_status`,
   `mempalace_diary_read`, `mempalace_search`, and `mempalace_kg_query`.
4. Confirm MemPalace tools are available. If unavailable, fail closed:
   report/block with exact evidence and await human/Codex operator action. The
   PM does not stop or relaunch the loop.

## 1. Context Curator

Spawn `context-curator` with read-only MemPalace access to produce a compact
packet for the debate:

- Current best metric, baseline, and last 10 experiment outcomes.
- Prior MemPalace findings: failures, keeps, feature families, model families,
  data coverage issues, and reviewer objections.
- Open proposals from `RESEARCH_LOG.md`, marked as prior context only.
- Hard constraints and available data sources.
- Selected `research_mode`, a concrete mode rationale, and burned theory
  families from completed failures.

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

For alpha mode, a burned theory family requires materially new evidence, not a
restatement of the old hypothesis. G0 work should debate the smallest data or
provenance repair needed to restore a valid alpha gate, not a new strategy.

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
implement without a majority. The final decision must explicitly set
`reviewer_verdict=NOT_RUN` and `memory_write_required=false`; this is the only
auditable no-memory transition. Do not write MemPalace facts or fabricate a
memory receipt for `NO_CONSENSUS`.

The PM writes the winning theory and dissent summary to `RESEARCH_LOG.md`
before implementation. Do not write theories, debate notes, or consensus drafts
to MemPalace before an experiment is completed and decided.

## 4. Implement

Spawn `implementer` with the final implementation brief.

Implementation requirements:

- Create the disposable Git worktree under the exact canonical root
  `/home/dev/.openclaw/autoresearch/worktrees` (for example,
  `/home/dev/.openclaw/autoresearch/worktrees/quantipy-i{iteration}-implementation`).
  First run `mkdir -p /home/dev/.openclaw/autoresearch/worktrees`; the root
  itself must exist and be canonical before the artifact can be accepted.
  Never use `/tmp`: it is a 31G tmpfs, while each Quantipy worktree virtualenv
  is about 1.5G and stale iterations exhaust the filesystem.
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

Every verification artifact also contains a structured coverage receipt for
each symbol plus one aggregate receipt: declared intended start/end, actual
common start/end, OOS start/end, expected/actual trading days, coverage percent,
missing reason, default/fallback fold counts, cap-provenance availability, and
an explicit `fixed_sleeve_local_data` flag. Date ranges and counts must agree.
A fixed local sleeve is permitted only when explicitly declared and may not
claim cap-verified universe compliance.

Coverage is a common-calendar analysis. Every per-symbol receipt must use the
aggregate declared range; aggregate actual start is the latest per-symbol
actual start and aggregate actual end is the earliest per-symbol actual end.
Aggregate OOS is the intersection of per-symbol OOS windows. Expected/actual
trading-day counts, coverage percent, and missing reason must be identical
across all receipts after common-calendar filtering; never sum symbol day
counts. Aggregate default/fallback folds equal the fewest corresponding
per-symbol folds.

In `DATA_INFRA_G0`, record `infra_gate_outcome` (`GATE_PASSED` or
`REMEDIATION_REQUIRED`) plus `infra_rationale`; do not use Sharpe to decide
whether the infrastructure repair succeeded.

Bug signals:

- Sharpe > 10.
- Win rate = 1.0.
- Max drawdown = 0%.
- Profit factor = inf.
- Accuracy < 50% with Sharpe > 5.
- OOS Sharpe > 2x IS Sharpe; use IS walk-forward Sharpe instead.

If a bug signal appears, send a targeted fix to `fixer`, then rerun verification.

## 6. Single Reviewer

Spawn exactly one `reviewer` on its configured `openai/gpt-5.6-sol` high-reasoning
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
- Reuse the exact persisted implementation `workspace_path` and accepted commit;
  it must already be under `/home/dev/.openclaw/autoresearch/worktrees`, and
  the Fix/Test artifact must report that same canonical path. There is no
  legacy or `/tmp` fallback. Never create another worktree. Before editing,
  preserve unrelated work and use only already-authoritative human/Codex shared
  infrastructure history; never edit shared infrastructure, and fail closed if
  reconciliation is ambiguous or risks unrelated loss. These are agent
  behavioral guardrails, not mechanical proofs. The artifact-advance boundary
  mechanically requires a clean committed result at that same worktree and
  verifies both prior implementation and current authoritative-target ancestry.

## 8. Decide And Log

Use the reviewer's recommended metric only for `ALPHA_RESEARCH`.

- Tests fail after retries: CRASH.
- Critical review issue remains: DISCARD.
- Decision Sharpe <= -0.5: DISCARD.
- Decision Sharpe improves baseline: KEEP.
- Decision Sharpe > 0.5: SIGNIFICANT KEEP.
- Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP.
- Max drawdown >= 30%: DISCARD regardless of Sharpe.

For `DATA_INFRA_G0`, decide `INFRA_REPAIRED` only when its explicit
infrastructure gate passed; otherwise decide `INFRA_BLOCKED`. A methodology
`PASS` means the data/process gate was assessed correctly, not that an alpha
strategy has passed.

Actions:

- KEEP: keep the commit, update baseline if appropriate, and log the metrics.
- DISCARD/CRASH: revert the experiment commit, log why, and move to the next
  proposal.
- NO_CONSENSUS: log the split to `RESEARCH_LOG.md`; set `NOT_RUN` and the
  explicit no-memory flag, then begin the next context pass.
- Always append to the target repo's experiment log and `RESEARCH_LOG.md`.
- After every memory-required final decision, the PM writes MemPalace drawers
  and KG facts for experiment, feature, model, metric, decision, and failure
  mode. `NO_CONSENSUS` never enters MemPalace.
- Write a MemPalace diary entry only as part of final experiment logging.

## Recovery And Status

- On resume, read `RESEARCH_LOG.md`, git status, active background tasks, and
  MemPalace. Re-enter the first incomplete stage.
- Post `[TASK:running] autoresearch iteration N` after each launch.
- Post progress every 10 iterations.
- Monitoring is read-only. On confirmed shared-infrastructure failure, report
  the exact blocker evidence and await human/Codex operator action. The PM
  never touches G2 directly.
- Do not stop after one experiment. If a declared finite goal is satisfied, the
  PM may report that status but must continue the loop until an explicit
  human/Codex control-command `stop` halts it.
