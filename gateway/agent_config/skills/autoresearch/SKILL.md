---
name: autoresearch
description: PM-owned autonomous research loop for Quantipy using MemPalace, five-agent debate, Codex implementation, and a single high-reasoning reviewer.
version: 8.0.0
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

## Platform Readiness Preflight

Before any stage dispatch, the runner validates the operator-owned manifest at
`~/.openclaw/autoresearch/platform-readiness.json`. Override it explicitly with
`--readiness-manifest <path>` for tests or a controlled operator migration. The
manifest must be schema version 3, identify a canonical `manifest_id` and
`snapshot_id`, and contain SHA-256 receipts for the Quantipy data contract and
authoritative XNYS calendar evidence. `READY` requires both files to be
absolute, regular files whose current hashes match and exposes the canonical
capability object injected into every stage. `BLOCKED` states the concrete
operator action required. The runner never downloads, infers, substitutes, or
repairs evidence.

Before `autoresearch-next`, stop the supervisor and prepare schema-v2
state with exactly one procedure. Losslessly migrate only a schema-less
pristine state:

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  tmp="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-migrate-state "$state" --output "$tmp"
  mv -- "$tmp" "$state"
  trap - EXIT
)
```

For a new campaign, or after archiving state that cannot migrate losslessly,
initialize a pristine state from the READY manifest:

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  tmp="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-init-state \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$tmp"
  mv -- "$tmp" "$state"
  trap - EXIT
)
```

Both procedures atomically leave schema-v2 state at the authoritative path
used by control and the supervisor. Use that path for the first dispatch:

```bash
cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next \
  /home/dev/.openclaw/autoresearch/quantipy-state.json
```

Never run both preparation procedures for one campaign. An existing
incompatible state must be archived, not rewritten or silently pinned.

`autoresearch-next` checks the current schema-v3 platform-readiness manifest
before selecting or dispatching
any stage and rejects a blocked, invalid, or stale-pinned state. An
`INFRA_BLOCKED` operator-precondition decision is durable: it sets the state to
suspended, does not increment the iteration, and does not write MemPalace. The
supervisor recognizes the suspended state and does not wake the PM. After the
operator changes and validates readiness, rebuild the manifest with the
explicit frozen-campaign XNYS interval `2022-01-03` through `2025-12-31`.
Reddit begins `2021-12-31`; the configured rolling aggregate entitlement
rejects January/July 2021 but supports 2022 onward. The readiness command
strictly probes the campaign start through Quantipy's public
`security_universe_screen` and daily regular-hours `prices` APIs for `AAPL`.
This intentional operator prewarm may hydrate/cache data, and any probe failure
must not produce a READY receipt. Then resume the same schema-v2 state
atomically:

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  resumed="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$resumed"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-build-readiness \
    /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --quantipy-root /home/dev/repos/quantipy \
    --expected-quantipy-commit <full-quantipy-git-hash> \
    --xnys-calendar /home/dev/.openclaw/autoresearch/evidence/xnys-trading-calendar.json \
    --campaign-xnys-start 2022-01-03 \
    --campaign-xnys-end 2025-12-31
  uv run gateway-cli autoresearch-resume "$state" \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$resumed"
  mv -- "$resumed" "$state"
  trap - EXIT
)
```

Resume accepts the new READY receipt, clears the suspension, and starts the next
iteration. Normal completed transitions, including memory-required decisions
such as `INFRA_REPAIRED`, and explicit no-memory `NO_CONSENSUS` transitions
still use `autoresearch-start-next`. Only at that completed boundary, after the
required memory write or explicit no-memory transition, `autoresearch-start-next`
may atomically replace the pinned identity with a different validated READY
receipt. It never changes an active or suspended iteration's receipt.

## Instruction Source Manifest

`autoresearch-next` no longer injects full instruction file contents. It emits
only `required_receipts`, a canonical v2 `instruction_source_manifest`, and
`source_manifest_sha256`. Each listed source has exactly `receipt_id`, absolute
canonical `path`, and `sha256`. The manifest is versioned,
domain-separated, sorted by `receipt_id`, duplicate-rejecting, and bound to the
current phase, expected artifact type, ordered target agent IDs, and canonical
target repo root.

Before dispatching or doing stage work, the PM and every stage agent must read
every listed live source from disk, recompute SHA-256 over the current bytes,
and fail closed if any file is missing, unreadable, or hash-mismatched.
OpenClaw-configured skills remain authoritative; target-repo methodology and
agent files are read live from `/home/dev/repos/quantipy` using the listed
canonical paths.

Every production artifact file passed to `gateway-cli autoresearch-advance`
must be exactly:

```json
{
  "instruction_manifest_sha256": "<source_manifest_sha256 from autoresearch-next>",
  "artifact": {}
}
```

The `artifact` value is the phase-specific structured artifact. Do not add
extra envelope keys, omit the digest, or pass legacy unwrapped artifacts.
`autoresearch-advance` rejects mismatched, missing, extra-key, and unwrapped
files before state advance. The complete envelope file must be at most 24 KiB;
compact the artifact rather than truncating it. `autoresearch-next` also has a
hard 32 KiB prompt budget and fails closed with an actionable error if accepted
state artifacts would exceed it.

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

## Compute Fit and GPU Choice

The deterministic runner injects a read-only machine-readable capability
snapshot into every stage prompt. It reports CPU, memory, GPU/VRAM, CUDA
visibility, and GPU-capable packages found in the Quantipy virtualenv. Treat
that snapshot as authoritative and do not infer capabilities from prose.

The loop has no CPU/GPU preference. Every new debate submission and
implementation result must include `compute_fit`:

- `target`: exactly `none`, `cpu`, `gpu`, or `mixed`.
- `rationale`: why that execution target fits the hypothesis and data scale.
- `required_dependencies`: a JSON list of compute dependencies required by the
  declared path; it is empty for `target=none` and includes `cuda_runtime` when
  GPU/CUDA execution requires it.
- `benchmark_plan`: how wall time, memory, or acceleration will be measured.

Choose the target from the hypothesis, data scale, reproducibility, and planned
or measured cost. GPU and mixed declarations are mechanically accepted only
when the snapshot proves usable GPU/CUDA access and all declared dependencies
are available. Otherwise report the exact infrastructure blocker. The
mechanical dependency gate applies to dependencies declared for GPU or mixed
execution; CPU choices must still be reproducible in the existing environment.
Never install dependencies, fabricate capability evidence, or switch execution
devices. Verification compares the actual run with the declared implementation
compute fit and reports any mismatch.

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

Every stage agent except `autoresearch-pm` loads `mempalace-readonly`,
`quantipy-methodology`, and `quantipy-data-contract`. The methodology skill
routes stage agents to current Quantipy source-of-truth files from
`/home/dev/repos/quantipy`
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
  use `/tmp` or any other root. Create that parent with `mkdir -p` before
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

### Quantipy Ownership Decision Tree

Use this tree before classifying a failure, editing files, relaunching work, or
asking a stage agent to fix anything:

1. Did the failure involve G2/OpenClaw orchestration, task ledger state,
   supervisor/recovery, shared Quantipy platform/runtime/tooling, shared data
   loaders, shared test harnesses/fixtures, dependency installation/import
   failures, runtime launch failures, the G2 simulator, headless launch, Codex
   routing, or OpenClaw process control?
   - Yes: this is operator/Codex-owned shared infrastructure. Report the exact
     blocker evidence and wait. Only the human operator or Codex fixes,
     promotes, restarts, relaunches, or changes these surfaces.
   - No: continue.
2. Did the failure involve an alpha module, experiment notebook,
   experiment-specific unit test, strategy feature, fold construction, model,
   null test, metric calculation, alpha validation, or methodology behavior?
   - Yes: this is autoresearch-owned experiment work, even when a dependency
     upgrade exposed the issue. Let implementer/reviewer/fixer handle it in
     the disposable experiment worktree.
   - No: continue.
3. Is the classification ambiguous or would a fix risk shared state or
   unrelated dirty changes?
   - Yes: fail closed as a shared-infrastructure blocker with evidence and
     wait for human/Codex operator action.
   - No: classify by the smallest owned surface actually changed.

Examples:

- T36 read-only shuffle failure in an experiment-specific null test is
  autoresearch-owned because it is methodology/null-test behavior in the alpha
  artifact.
- Task ledger corruption, missed terminal task reconciliation, supervisor
  recovery loops, G2 simulator failures, headless launch failures, Codex route
  failures, dependency/runtime failures, shared loader regressions, and shared
  fixture or harness failures are operator/Codex-owned shared infrastructure.
- A dependency upgrade that exposes a bug in a strategy fold, feature pipeline,
  model fit, null test, notebook metric, or alpha module remains
  autoresearch-owned. A dependency upgrade that prevents the platform, loader,
  harness, process launcher, or runtime from starting is operator/Codex-owned.

PM and stage-agent conduct:

- PM and stage agents report shared-infrastructure blockers with evidence and
  wait. Evidence must include the command or test, paths, labels/session ids,
  timestamps when available, and decisive stderr/log lines.
- Only the human operator or Codex fixes, promotes patches, restarts services,
  relaunches stages for shared-infrastructure recovery, or touches G2/OpenClaw
  orchestration. The PM never touches G2, promotes patches, edits shared
  infrastructure, or performs recovery relaunches for operator-owned issues.
- Non-PM agents do not write MemPalace. Stage agents use readonly retrieval
  only; the PM writes MemPalace only at the allowed final decision points.

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
- The pinned readiness receipt, capability summary, and compact references to
  relevant universe/coverage receipts. Do not include full ticker arrays.
- Hard constraints and supported data sources from those receipts.
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
- Historical universe screen profile, explicit full-range selection schedule,
  plan/profile identity, deterministic contiguous batch plan, and execution
  timing. Do not claim materialization identities or digests before
  verification.
- Feature pipeline from raw OHLCV/sentiment to model input.
- Model type and hyperparameter search plan.
- Walk-forward split, purge/embargo if applicable, and OOS holdout.
- Transaction cost model.
- Broad common-calendar coverage plan bounded by readiness and cache/hydration
  receipts, with an untouched OOS holdout.
- Compute fit with target, rationale, required dependencies, and benchmark plan.
- Rejection criteria.

For alpha mode, a burned theory family requires materially new evidence, not a
restatement of the old hypothesis. G0 work should debate the smallest data or
provenance repair needed to restore a valid alpha gate, not a new strategy.

Quantipy constraints:

- Load `quantipy-data-contract`; use only its universe, price, action,
  execution-timing, unsupported-data, cache-reuse, and prompt-hygiene rules.
- No overnight holds; flat by the target repo's close-out rule.
- Use real platform OHLCV through `qp.prices()` and no synthetic research data.
- Use a simple indicator core with optional Reddit/news sentiment conditioning.
- Hyperparameter tuning uses time-series-aware splits.

## 3. Consensus

Spawn `consensus-arbiter` to determine whether one theory has a 3-of-5
majority. Required output:

- Winner, majority count, dissenting positions, or `NO_CONSENSUS`.
- Scores for novelty, theory, implementation risk, data adequacy, overfit risk,
  and expected net Sharpe.
- Explicit reasons losers were rejected.
- Final implementation brief.
- Frozen canonical universe plan inputs: profile identity and digest, sorted
  full-range selection schedule, maximum members per date, and execution
  policy. Consensus stores no redundant batch boundaries. The runner derives
  deterministic contiguous batches that each stay within 32 dates, 1,000
  members per date, and 10,000 date-member slots; implementation performs one
  `qp.security_universe_history()` operation per batch. Consensus must
  not contain snapshot, summary, or member-union materialization digests.

If there is no 3-of-5 majority, run one concise debate retry with the same
context plus the dissent summary. If there is still no majority, log
`NO_CONSENSUS` to `RESEARCH_LOG.md`, then start a fresh context pass. Do not
implement without a majority. The final decision must explicitly set
`reviewer_verdict=NOT_RUN` and `memory_write_required=false`; do not write
MemPalace facts or fabricate a memory receipt for `NO_CONSENSUS`.

An operator-precondition consensus may also terminate before implementation as
`INFRA_BLOCKED`, with `reviewer_verdict=NOT_RUN`, a concrete `infra_rationale`,
and `memory_write_required=false`. This is a separate auditable no-memory path
for shared infrastructure prerequisites, not an experiment result.

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

Every verification attempt must end with a structured JSON
`verification_result` artifact, including attempts where tests fail or the run
uncovers a bug signal. The PM must write the JSON artifact and run
`cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-advance
/home/dev/.openclaw/autoresearch/quantipy-state.json <artifact.json>` before
any prose completion, status report, or handoff. A prose-only verification
completion is invalid and must be treated as no artifact. The artifact file
must use the strict `instruction_manifest_sha256` envelope from the active
`autoresearch-next` output. Never pass a raw unwrapped `verification_result`.

Verification is the first stage that records materialization evidence. Capture
each history batch's contract digest, the per-date snapshot and grouped-daily
identities and content digests, and the final member-union count and digest.
Verify them against the consensus plan/profile identity and batch order. Follow
the compact `quantipy-data-contract` rules for exact source receipt fields,
pinned-XNYS next-session validation, canonical union bytes, and the external
union-manifest path/SHA receipt; never place the member array in state.

Failure classification is mandatory:

- If any test or verification command exits nonzero or reports a failed test,
  set `status` to `TEST_FAILURE`, set `tests_passed` to `false`, and include
  the exact command in `commands_run` plus the decisive failure evidence in the
  artifact summaries.
- If commands ran but metrics are impossible, leaky, internally inconsistent,
  or match a bug signal, set `status` to `BUG_SIGNAL`, keep `bug_signals`
  nonempty, and include the exact command and evidence that exposed it.
- Use `PASS` only when tests passed, `bug_signals` is empty, all required
  metrics are present, and the coverage receipt is complete.
- Every required JSON key must still be present for failures. For `TEST_FAILURE`
  or `BUG_SIGNAL`, unavailable metrics or coverage must be `null`; do not use
  fabricated or zero-valued placeholders. `PASS` requires complete metrics and
  coverage.

In `DATA_INFRA_G0`, `status` and `tests_passed` describe verification command,
test, and notebook execution plus experiment correctness; they do not describe
whether the data-infrastructure gate passed. If required commands, tests, and
the notebook complete successfully and the deterministic audit returns a valid
`REMEDIATION_REQUIRED` receipt, emit `status=PASS` and `tests_passed=true` with
that gate outcome. This is a completed verification that proceeds to review
and final `INFRA_BLOCKED`, not a fixer task. Use `TEST_FAILURE` only for an
actual nonzero command or failed test, a malformed or missing required receipt,
an experiment defect, or inability to execute verification.

Required metrics:

- IS walk-forward Sharpe net.
- OOS Sharpe net.
- Max drawdown.
- Win rate.
- Trade count and trades/day.
- OOS trading days.
- Feature importances.
- Null test results.

For `ALPHA_RESEARCH`, the only coverage artifact is the compact
`DynamicUniverseCoverageReceipt`, bound to the verified member union, hydrated
range, timeframe, market-hours policy, OOS range, symbol-session counts, and
fold counts. ALPHA does not emit per-symbol or aggregate coverage receipts.
For `TEST_FAILURE` or `BUG_SIGNAL` when coverage is unavailable, set
`data_coverage` to `null` instead of inventing a receipt.

Legacy per-symbol `CoverageReceipt` plus `AggregateCoverageReceipt` is
explicitly `DATA_INFRA_G0`-only. In G0, every per-symbol receipt uses the
aggregate declared range; aggregate actual and OOS ranges are the common
intersections, common-calendar day counts and percentages agree, and aggregate
fold counts equal the minimum corresponding per-symbol folds. Locally inferred
or manually fixed symbol sleeves are not valid ALPHA universe evidence.

In `DATA_INFRA_G0`, record `infra_gate_outcome` (`GATE_PASSED` or
`REMEDIATION_REQUIRED`) plus `infra_rationale`; do not use Sharpe to decide
whether the infrastructure repair succeeded. Persist the gate rationale in the
`verification_result` before reporting the stage outcome. A valid
`REMEDIATION_REQUIRED` receipt is compatible with `status=PASS` and
`tests_passed=true`; it is an operator-owned infrastructure blocker, not a test
failure.

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
- Did the experiment use the full receipt-bounded intended range and broad
  common-calendar coverage?
- Did the experiment avoid cherry-picking universe dates, windows, parameters,
  and thresholds?
- Did the method avoid leakage, overfitting, overlapping-hold errors, and
  transaction-cost omissions?
- Are null tests and OOS evaluation sufficient to trust the recommended metric?

Required output: verdict (`PASS`, `CONDITIONAL PASS`, `FAIL`), recommended
decision metric, critical issues, noncritical issues, and exact fix requests.

## 7. Fix/Test

- Critical reviewer issue: send a narrow fix to `fixer`, rerun tests/notebook,
  then rerun the single reviewer.
- Test failure: fix up to two times, then classify and log CRASH. The disposable
  experiment worktree is not promoted.
- No methodology issue: proceed to decide/log.
- Reuse the exact persisted implementation `workspace_path` and accepted commit;
  it must already be under `/home/dev/.openclaw/autoresearch/worktrees`, and
  the Fix/Test artifact must report that same canonical path. Paths outside
  that root are invalid. Never create another worktree. Before editing,
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
- Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP.
- Decision Sharpe > 0.5: SIGNIFICANT KEEP or STRONG KEEP.
- At or below 0.5, require a numeric baseline: an improvement is KEEP-family
  and a non-improvement is DISCARD. Plain KEEP is invalid without a numeric
  baseline.
- Max drawdown >= 30%: DISCARD regardless of Sharpe.

For `DATA_INFRA_G0`, decide `INFRA_REPAIRED` only when its explicit
infrastructure gate passed; otherwise decide `INFRA_BLOCKED`. A methodology
`PASS` means the data/process gate was assessed correctly, not that an alpha
strategy has passed.

Actions:

- KEEP-family: retain the accepted experiment commit, update the numeric
  baseline when the decision artifact requires it, and log the metrics.
- DISCARD/CRASH: do not promote the disposable experiment commit; log why and
  move to the next proposal.
- NO_CONSENSUS: log the split to `RESEARCH_LOG.md`; set `NOT_RUN` and the
  explicit no-memory flag, then begin the next context pass.
- Always append to the target repo's experiment log and `RESEARCH_LOG.md`.
- After every memory-required final decision, the PM writes MemPalace drawers
  and KG facts for experiment, feature, model, metric, decision, and failure
  mode. `NO_CONSENSUS` and `INFRA_BLOCKED` never enter MemPalace.
- Write a MemPalace diary entry only as part of memory-required final experiment
  logging.

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
