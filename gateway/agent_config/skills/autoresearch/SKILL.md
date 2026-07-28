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

The owner-only supervisor polls the authoritative state and OpenClaw task
records every 60 seconds. Its default stale-task threshold is 15 minutes so
long implementation, verification, review, and fix turns are not interrupted
while running tests or backtests. A task is still stale when it exceeds that
threshold without an OpenClaw event, and recovery must use the owner control
commands; do not interact with G2 or kill a live stage merely because it is
quiet for a few minutes.

Long hydrate-capable, backtest, notebook, and similar commands must not sit in
an unbounded foreground tool call. Use the detached launcher with an immutable
run manifest and a one-time private command input file:

```bash
cd /home/dev/repos/g2_openclaw
command_file=/home/dev/.openclaw/autoresearch/command-inputs/<unique-command>.json
uv run gateway-cli autoresearch-create-command-file --output "$command_file"
# stdin protocol for the helper:
# {"schema_version":1,"command":["bash","-lc","<non-secret command>"]}
/home/dev/repos/g2_openclaw/scripts/run-long-task.sh \
  --run-dir <absolute-run-dir> \
  --manifest <absolute-manifest.json> \
  --command-file "$command_file"
```

The helper reads only the schema-v1 stdin protocol and creates the command
input file atomically with `O_EXCL`, `O_NOFOLLOW`, and mode 0600 under an
operator-owned private directory. The manifest must already exist and its
`command_sha256` must match the command file. The launcher consumes the command
file exactly once and never accepts positional command payloads. Never pass API
keys, tokens, passwords, client secrets, or private keys as command arguments.
Use credential files, environment references, or inherited authentication
instead. The launcher runs the worker in a dedicated transient user-systemd
service with explicit memory bounds, outside the OpenClaw gateway cgroup.
It submits that unit with `systemd-run --no-block`, validates the unit start,
and waits only for coherent startup metadata; it does not retain a
`systemd-run --wait` client in the caller lifecycle. Once the launcher returns,
the caller may exit while the transient unit continues. To control an active
run, resolve its exact unit with
`systemctl --user whoami "$(cat <absolute-run-dir>/pid)"` and use
`systemctl --user stop <unit>` when required;
the worker records a signal stop as terminal failure and preserves the child's
actual exit status: an ordinary uncaught `SIGTERM` commonly yields `143`, while
a child that handles or delays `SIGTERM` may yield its own code (for example,
`7`).
Direct foreground execution is invalid. If `systemd-run` or the launcher cannot
be used, fail closed and report the infrastructure blocker without emitting a
stage artifact. Do not reduce scope simply to avoid this requirement; launch
the real command safely, surface concise status, and clean up stale
processes and run directories when the stage ends.
The launcher status ledger is intentionally narrow: `status.json` emits only
`running`, `succeeded`, or `failed`. Any blocker is a PM-owned classification
derived from bounded polling plus logs and receipts; it is not a literal
launcher status.

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

Before `autoresearch-next`, stop the supervisor and prepare schema-v3 state by
initializing a pristine state from the READY manifest. A live schema-v2 state,
or state missing `schema_version`, is unsupported and must be archived, not
migrated, repaired, or overwritten. Complete this reinitialization before
restarting the supervisor:

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
  if [ -e "$state" ]; then
    archive="${state}.schema-v2.$(date -u +%Y%m%dT%H%M%SZ).archive"
    mv -- "$state" "$archive"
  fi
  mv -- "$tmp" "$state"
  trap - EXIT
)
```

This procedure atomically leaves schema-v3 state at the authoritative path used
by control and the supervisor. Use that path for the first dispatch:

```bash
cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next \
  /home/dev/.openclaw/autoresearch/quantipy-state.json
```

An existing incompatible state or state missing `schema_version` must be
archived, not rewritten or silently pinned.

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
must not produce a READY receipt. Then resume the same schema-v3 state
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

In both `ALPHA_RESEARCH` and `DATA_INFRA_G0`, second-round `NO_CONSENSUS`
remains `NO_CONSENSUS`; it does not suspend, does not write MemPalace, and
`autoresearch-start-next` begins the next iteration with fresh context.
`INFRA_BLOCKED` and suspension are reserved only for explicit operator-owned
readiness suspension. Completed
`DATA_INFRA_G0` `REMEDIATION_REQUIRED` proceeds to review and non-suspending
`DISCARD`.

## Instruction Source Manifest

`autoresearch-next` no longer injects full instruction file contents. It emits
only `required_receipts`, a canonical v3 `instruction_source_manifest`, and
`source_manifest_sha256`. Each listed source has exactly `receipt_id`, absolute
canonical `path`, and `sha256`. The manifest is versioned,
domain-separated, sorted by `receipt_id`, duplicate-rejecting, and bound to the
current phase, expected artifact type, ordered target agent IDs, and canonical
target repo root.

Before dispatching or doing stage work, the PM and every stage agent treat
`source_manifest_sha256` and `state_reference_sha256` from `autoresearch-next`
as immutable dispatch identities. Read listed live sources from disk when their
current methodology rules are needed, but do not fail solely because a mutable
live file changed after dispatch. Missing or unreadable files whose rules are
required for the stage are operator-owned blockers. OpenClaw-configured skills
remain authoritative; target-repo methodology and agent files are read live from
`/home/dev/repos/quantipy` using the listed canonical paths.

Every production artifact file passed to `gateway-cli autoresearch-advance`
must be exactly:

```json
{
  "instruction_manifest_sha256": "<source_manifest_sha256 from autoresearch-next>",
  "state_reference_sha256": "<state_reference_sha256 from autoresearch-next>",
  "artifact": {}
}
```

The `artifact` value is the phase-specific structured artifact. Do not add
extra envelope keys, omit the digest, or pass legacy unwrapped artifacts.
Write production artifacts to an absolute path in the PM workspace, then use
that exact variable for validation and advancement so repository cwd changes
cannot make `jq`, `wc`, or `autoresearch-advance` inspect different files:

```bash
state=/home/dev/.openclaw/autoresearch/quantipy-state.json
artifact=/home/dev/.openclaw/workspace-autoresearch-pm/<artifact-name>.json
jq -e . "$artifact" >/dev/null
wc -c "$artifact"
cd /home/dev/repos/g2_openclaw
uv run gateway-cli autoresearch-advance "$state" "$artifact" \
  --instruction-manifest-sha256 "<source_manifest_sha256 from autoresearch-next>" \
  --state-reference-sha256 "<state_reference_sha256 from autoresearch-next>" \
  --output "$state"
```

`autoresearch-advance` rejects mismatched, missing, extra-key, stale-state, and
unwrapped files before state advance. The complete envelope file must be at most
64 KiB; compact the artifact rather than truncating it. `autoresearch-next` also
has a hard 32 KiB prompt budget and fails closed with an actionable error if
accepted state artifacts would exceed it. The in-place output path uses the
runner's locked atomic persistence; never replace authoritative state with a
shell-created temporary file after a failed command.

## Explicit Research Modes

The context packet must select exactly one mode and give a nonempty
`mode_rationale`; the deterministic state persists that choice.

- `ALPHA_RESEARCH` is for a strategy experiment. It may produce performance
  metrics and a KEEP/DISCARD-family decision only after coverage verification.
- `DATA_INFRA_G0` is for repairing data range, provenance, universe, or fold
  construction. It is not an alpha experiment and must never be presented as
  Sharpe/performance validation. After implementation and verification, its
  explicit infrastructure gate outcome maps `GATE_PASSED` to non-suspending
  `INFRA_REPAIRED` only after runner preflight identity/count checks.
  `REMEDIATION_REQUIRED` is stage evidence only and maps to non-suspending
  `DISCARD`; it cannot authorize suspension.

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
  data-specialist routing for data pressure, `openai/gpt-5.5` for
  microstructure and skeptic pressure, and lower-cost models for bounded theory
  and implementation feasibility.
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
| Debate 3 | `debater-skeptic` | `openai/gpt-5.5`, high |
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
  OpenClaw session labels remain globally occupied after a task reaches a
  terminal state; uniqueness is therefore checked against the complete task
  ledger, not only currently running tasks.
- An owner-session stop, restart, supervisor recovery wake, gateway restart,
  or interrupted dispatch is a retry even when the authoritative state file
  still names the same phase. Before spawning, parse every prior matching
  label from the task ledger and choose the next unused attempt number. Never
  reuse `r1-a1` (or any prior attempt) after such an event. A label collision
  (`label already in use`) is a failed dispatch precondition: do not wait for
  the announcement, do not silently rename a task after spawning, and retry
  with the next unused attempt label while preserving the terminal artifact.
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

### Completion Delivery Protocol

OpenClaw `2026.7.1-2` records a delivery failure if a completion-required child
handoff lands without a visible PM assistant reply, even when the child's
structured output exists. Treat that failure as a hard infrastructure blocker
for recovery; a child completion is not accepted merely because its trajectory
has output.

For every completion-required child handoff, `autoresearch-pm` must emit a
non-empty normal assistant acknowledgement in its own transcript, including
while waiting for remaining required children. Waiting acknowledgements must be
concise and truthful, for example: `<stage> completion recorded; waiting for
<N> required completion(s).`

While any required child completion is still outstanding, the PM must not use
`sessions_yield`, `NO_REPLY`, `ANNOUNCE_SKIP`, or a tool-only turn for that
handoff. Do not substitute the message tool or send an autonomous update to G2;
these acknowledgements are internal PM transcript replies only.
Repo-managed PM config denies `sessions_yield` exactly so this fails closed in
tool policy. Do not broaden that PM deny-list to `sessions_spawn` or MemPalace
write tools; the PM must still spawn stages and write final experiment records.

Once all required children arrive, the PM must persist the authoritative
artifact and emit a non-empty completion summary. Do not silently wait between
child completion receipt and final summary emission.

## Setup

Do once before the first iteration:

1. Define the mechanical goal, metric, metric direction, target repo, and
   writable scope.
2. Establish the baseline and record iteration 0.
3. As PM, read recent git history, current in-scope files, canonical decision
   receipts, and MemPalace prior experiments with `mempalace_status`,
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
- Prior proposals from MemPalace and canonical decision receipts.
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
context plus the dissent summary. If there is still no majority, emit the
structured `NO_CONSENSUS` final decision and start a fresh context pass. This
rule is identical in `ALPHA_RESEARCH` and `DATA_INFRA_G0`; a G0 consensus
failure must not be relabeled `INFRA_BLOCKED`. Do not implement without a
majority. The final decision must explicitly set
`reviewer_verdict=NOT_RUN` and `memory_write_required=false`; do not write
MemPalace facts or fabricate a memory receipt for `NO_CONSENSUS`.

An operator-precondition consensus may also terminate before implementation as
`INFRA_BLOCKED`, with `reviewer_verdict=NOT_RUN`, a concrete `infra_rationale`,
and `memory_write_required=false`. This is a separate auditable no-memory path
for shared infrastructure prerequisites, not an experiment result.

Do not write theories, debate notes, or consensus drafts to MemPalace before an
experiment is completed and decided.

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
- Before any hydrate-capable command, compute
  `price_hydration_scope_preflight` from the planned member-union count,
  experiment date range, timeframe, market-hours policy, and pinned XNYS
  session count. Include this object in `implementation_result` with
  `planned_symbol_sessions = member_union_count * session_count` and
  `within_budget` evaluated against the 600,000 symbol-session ALPHA budget.
  ALPHA verification will not dispatch without it. If `within_budget=false`,
  do not run `qp.prices()`, hydrate, the full backtest, or notebook commands
  that load the price panel. Commit the scaffold, focused tests, notebook
  shell, and over-budget preflight so verification can emit the structured
  feasibility `BUG_SIGNAL` without spending the hydrate cost.
- Commit after focused tests pass; notebook rendering is optional smoke/report evidence and
  never replaces the required typed runtime verification.
- Any notebook execution, hydrate-capable run, or backtest expected to outlive
  the watchdog must be launched detached through
  `/home/dev/repos/g2_openclaw/scripts/run-long-task.sh` with `--manifest` and
  `--command-file`, then bounded polling. Direct foreground execution is
  invalid. Secret-bearing command arguments are invalid; use credential files
  or inherited auth. If the launcher cannot be used, fail closed and report the
  infrastructure blocker without emitting a fix artifact. Record the run
  directory in stage notes and use its status files for progress and recovery.
  The launcher gives the detached worker a `MemoryHigh=20G` soft limit and a
  `MemoryMax=24G` hard limit; these are separate from the OpenClaw gateway's
  native-crash containment limits.

## 5. Verify

The PM extracts metrics. Use task output if sufficient; otherwise run focused
read-only commands in the target repo.

Every verification attempt must end with a structured JSON
`verification_result` artifact, including attempts where tests fail or the run
uncovers a bug signal. The PM must write the JSON artifact and run
the absolute-path handoff template above before any prose completion, status
report, or handoff. A prose-only verification completion is invalid and must be
treated as no artifact. The artifact file must use the strict
`instruction_manifest_sha256` and `state_reference_sha256` envelope from the
active `autoresearch-next` output. Never pass a raw unwrapped
`verification_result`.

## Mandatory Quantipy Typed Runtime Gate

The implementation artifact names the absolute canonical committed
`quantipy-experiment-v2` manifest under `implementation_result.workspace_path`
and its SHA-256. It must declare exactly `prepare`, `smoke`, `feasibility`, and
`model` stage files in that order. A raw notebook is not an implementation
artifact and cannot stand in for this manifest. Quantipy resolves package and
notebook paths from the manifest's parent, then stage files below that package.
Every local Python source file present below the full package root must be
tracked at the implementation commit and exact-byte identical; ignored,
untracked, symlinked, or mutable source entries fail provenance. Generated
`__pycache__` directories, `*.pyc`, and non-source runtime artifacts are not
source provenance. Quantipy preflight reads the full package Python tree once,
then all stage imports execute only from that approved immutable in-memory
capsule. `run.json.source` must bind the complete uniquely ordered `.py` file
list, each size and SHA-256, total bytes, and the
`quantipy-experiment-source-v1` aggregate digest. No `run/source` directory is
retained or accepted as evidence. G2 independently rebuilds the same inventory
from Git blobs at the implementation commit; dirty execution followed by
workspace restoration is rejected.

Verification order is fixed: focused tests,
`env PYTHONDONTWRITEBYTECODE=1 uv run quantipy experiment preflight MANIFEST`, then
launch the exact `env PYTHONDONTWRITEBYTECODE=1 uv run quantipy experiment run
MANIFEST --output-root ROOT --run-id
autoresearch-i<iteration>-<commit12>` command through
`/home/dev/repos/g2_openclaw/scripts/run-long-task.sh`. The immutable detached
manifest must set `expected_artifact_path` to the known `ROOT/RUN_ID/run.json`.
Direct foreground execution cannot satisfy this contract. Under the
non-malicious same-host agent model, PASS requires the worker-produced sealed
attestation; a verifier claim cannot replace it. Before publishing terminal
success, the detached worker securely opens that artifact once and records its
path, size, SHA-256, and file identity in strict schema-v5 `status.json`, then
seals the artifact/status mode 0400 and detached run directory mode 0500.
Verification must bind the detached run directory and manifest digest and
require successful terminal status, complete EOF drain, truthful truncation
metadata for each bounded 64 KiB retained log tail, and current `run.json`
bytes matching the worker attestation; an
artifact-supplied hash alone is never proof. The deterministic run ID
means the final path is known before execution: `ROOT/RUN_ID/run.json`. `ROOT`
is the runner-declared fixed private autoresearch runs root; arbitrary output
roots and mutable workspace output are rejected. State initialization and
verification dispatch require that root to be owner-controlled mode 0700 and
reject symlinked path components. G2 mirrors Quantipy's strict
8 MiB canonical run-envelope cap. Committed source snapshots permit 1 MiB per
source file, and a committed notebook snapshot permits 8 MiB. Within the run
envelope, source evidence is limited to 256 ordered Python files, 1 MiB per
file and 8 MiB total; stage summaries are
limited to 4096 characters, failure messages to 2048, identity paths to 4096,
and the nested or standalone panel receipt to 4 MiB.

The CLI process contract is exact: exit 0 iff `run.success=true`, and exit 1
iff `run.success=false`. PASS requires detached `succeeded`/exit 0. A valid
typed rejected/failed envelope used by TEST_FAILURE or BUG_SIGNAL requires
detached `failed`/exit 1, no signal, ordinary `process_error`, and complete
sealed artifact attestation. Timeout, operator stop, resource exhaustion,
artifact/capture failure, signals, exit 2+, and all other outcomes fail closed.

Sealed local modes prevent ordinary verifier mutation, not a malicious
same-UID process or root/sudo-capable operator deliberately rebuilding the
control-plane record; that compromise is outside this threat model. Cleanup is
an explicit operator action: `chmod 0700 <exact-detached-run-dir>`, then remove
only that exact directory. Never recursively chmod or delete the runs root.

Quantipy itself is the cheap mechanical admission gate: smoke and feasibility
must complete before model is imported or executed. Never self-report that
gate; copy its typed run receipt into `quantipy_experiment_evidence`.

For `PASS`, evidence must match the implementation manifest path/digest and
commit, `run.json` path/digest and ID, successful result, all four ordered
completed stages, and panel identity/digests when requested. For
`TEST_FAILURE` or `BUG_SIGNAL`, preserve the actual failed or rejected typed
run evidence when a run exists. If execution never started,
`quantipy_experiment_evidence` is `null` and
`quantipy_execution_not_started` is mandatory. That strict receipt binds the
manifest, deterministic expected run ID/path, exact failed command and
evidence, and reason `focused_tests_failed` or `preflight_failed`; the expected
run directory must be absent. G2 atomically creates a private identity-bound
tombstone at that directory while validating the receipt, so the same run ID
can never start later. Retry only after a new implementation/fix commit yields
a new deterministic commit-bound run ID. A requested panel may omit evidence
only for a typed pre-stage preflight, panel, or filesystem failure; otherwise
its nested receipt and bound files are mandatory. `nbconvert`, `papermill`, and Jupyter execution can
only smoke-test or render a report; none substitutes for the v2 runtime run or
authorizes PASS.

Verification is the first stage that records materialization evidence. Capture
each history batch's contract digest, the per-date snapshot and grouped-daily
identities and content digests, and the final member-union count and digest.
Verify them against the consensus plan/profile identity and batch order. Follow
the compact `quantipy-data-contract` rules for exact source receipt fields,
pinned-XNYS next-session validation, canonical union bytes, and the external
union-manifest path/SHA receipt; never place the member array in state.

Before running any command that can call `qp.prices()` for the implemented
experiment, perform a price-hydration scope preflight from the planned
member-union count and XNYS experiment session count. The hard ALPHA budget is
600,000 symbol-sessions. If `member_union_count * session_count` exceeds that
budget, do not run the hydrate/backtest command. Emit a structured
`verification_result` with `status=BUG_SIGNAL`, a nonempty
`bug_signals` entry named `price_hydration_scope_exceeds_budget`, the computed
scope and limit, and `null` for metrics, coverage, and receipts that require
the skipped hydrate. This is an experiment feasibility signal for `fixer`, not
an operator infrastructure repair and not a reason to use direct SQL, provider
access, cache-derived universes, or a shorter unapproved range.

Failure classification is mandatory:

- If any test or verification command exits nonzero or reports a failed test,
  set `status` to `TEST_FAILURE`, set `tests_passed` to `false`, and include
  the exact command in `commands_run` plus the decisive failure evidence in the
  artifact summaries.
- Any hydrate, backtest, typed runtime run, notebook report render, or similarly long verification command must
  run detached with bounded polling and durable run artifacts. Foreground tool
  calls beyond the watchdog are unsafe and invalid for these commands.
- If commands ran but metrics are impossible, leaky, internally inconsistent,
  or match a bug signal, set `status` to `BUG_SIGNAL`, keep `bug_signals`
  nonempty, and include the exact command and evidence that exposed it.
- Use `PASS` only when tests passed and `bug_signals` is empty. `ALPHA_RESEARCH`
  `PASS` requires complete alpha metrics, compact dynamic coverage, and paired
  universe and price-hydration receipts.
- Every required JSON key must still be present for failures. For `TEST_FAILURE`
  or `BUG_SIGNAL`, unavailable metrics or coverage must be `null`; do not use
  fabricated or zero-valued placeholders.

In `DATA_INFRA_G0`, `status` and `tests_passed` describe verification command,
test, and typed Quantipy runtime execution plus experiment correctness; they do not describe
whether the data-infrastructure gate passed. If required commands, tests, and
the typed runtime run completes successfully and the deterministic audit returns a valid
`REMEDIATION_REQUIRED` receipt, emit `status=PASS` and `tests_passed=true` with
that gate outcome. This is a completed verification that proceeds to review and
non-suspending `DISCARD`, not a fixer task and never `INFRA_BLOCKED`. Use
`TEST_FAILURE` only for an
actual nonzero command or failed test, a malformed or missing required receipt,
an experiment defect, or inability to execute verification.

A `DATA_INFRA_G0` `PASS`, including one with
`infra_gate_outcome=REMEDIATION_REQUIRED`, may leave alpha metrics and
`data_coverage` as `null` when unavailable, but paired universe,
price-hydration, and platform coverage receipts are mandatory. If any are
unavailable or mismatched, emit the exact contract-mismatch `BUG_SIGNAL`
artifact. Never fabricate those artifacts.

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
`tests_passed=true`; it proceeds to review and non-suspending `DISCARD`, not a
test failure or operator-owned suspension.

Every new G0 envelope must include `platform_coverage_validation`, the canonical
output of Quantipy's shared `qp.validate_dynamic_price_coverage` validator. Its
canonical digest proves only receipt self-consistency. The runner trusts the
gate only when the receipt is mechanically bound to the exact implementation
preflight, universe verification receipt, price hydration receipt, and digest
source request identity/provider and fields `member_union_digest`,
`requested_sessions_digest`, `pit_active_roster_digest`, and
`source_price_coverage_response_digest`. G2 recomputes Quantipy's compact
JSON-array `member_union_digest` from the verified member-union manifest, while
separately requiring the universe and hydration newline-manifest digests to
match. It keeps `pit_active_roster_digest` as an intrinsic Quantipy receipt
field but does not claim independent exact PIT roster identity. The price
hydration receipt must carry the required `source_price_coverage_response_digest`
from the actual Quantipy `PriceCoverageResponse`; it is not the hydration
`coverage_receipt_digest` metadata digest. The accepted
contract is `contract_version=dynamic-price-coverage-v1`,
`source_contract_version=price-coverage-v1`, `timeframe=1min`, and
`market_hours=regular`. Use
`scope=full_union_hydration`: `hydrated_symbol_sessions` equals
`member_union_count * requested_session_count`, and
`inactive_union_symbol_sessions` equals hydrated sessions minus PIT-active
sessions for both scopes; scope selects the asserted upstream count semantics,
but every receipt reports both geometries. `pit_active_roster` is a useful
diagnostic scope but cannot prove full-union coverage. Provider-empty inactive
union sessions are valid and are not violation codes. `unexpected_session_count`
counts distinct unexpected dates. `GATE_PASSED` requires a `COMPLETE` receipt
cross-checked against runner-owned preflight identity and counts.
`REMEDIATION_REQUIRED` requires corresponding nonempty, real violation codes
but remains non-authorizing stage evidence. Unavailable or mismatched
provenance is always
`status=BUG_SIGNAL`, with the sole signal
`platform_coverage_contract_mismatch` and null infrastructure outcome,
rationale, and receipt; it goes to `fixer`. A stage agent must never self-author
a receipt as proof of infrastructure.

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
- Any long fix/test notebook, hydrate, or backtest rerun must use the detached
  launcher at `/home/dev/repos/g2_openclaw/scripts/run-long-task.sh` with
  `--manifest` and a one-time `--command-file`, and preserve run-directory status until the rerun is
  accepted or explicitly cleaned up.
- Test failure: fix up to two times, then classify and log CRASH. The disposable
  experiment worktree is not promoted.
- Verification `BUG_SIGNAL`: fix up to two times. If the signal persists after
  retries, classify and log DISCARD; do not loop indefinitely.
- For a `price_hydration_scope_exceeds_budget` BUG_SIGNAL, Fix/Test must not
  run hydrate-capable commands, including `qp.prices()`, `generate_*results`,
  `nbconvert`, `papermill`, or `jupyter execute`. Fix only the experiment
  scope/guard/tests and let the next verification stage perform any permitted
  hydrate/backtest. The control plane rejects hydrate-capable `tests_rerun`
  entries for these fixes.
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
- If a verification fix changes the planned ALPHA price-hydration scope, include
  the updated `price_hydration_scope_preflight` in `fix_result` using the same
  strict object shape as `implementation_result`. If the fix does not change
  scope, set `price_hydration_scope_preflight` to `null`; do not omit the key.

## 8. Decide And Log

Use the reviewer's recommended metric only for `ALPHA_RESEARCH`.

- Tests fail after retries: CRASH.
- Verification BUG_SIGNAL persists after retries: DISCARD.
- Critical review issue remains: DISCARD.
- Decision Sharpe <= -0.5: DISCARD.
- Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP.
- Decision Sharpe > 0.5: SIGNIFICANT KEEP or STRONG KEEP.
- At or below 0.5, require a numeric baseline: an improvement is KEEP-family
  and a non-improvement is DISCARD. Plain KEEP is invalid without a numeric
  baseline.
- Max drawdown >= 30%: DISCARD regardless of Sharpe.

After a `DATA_INFRA_G0` implementation and completed verification, decide
`INFRA_REPAIRED` only for explicit `infra_gate_outcome=GATE_PASSED` backed by
a `COMPLETE` platform coverage receipt cross-checked against runner-owned
preflight identity and counts. For `REMEDIATION_REQUIRED`, decide
non-suspending `DISCARD`. A remediation receipt never authorizes
`INFRA_BLOCKED`; suspension is explicit operator-owned readiness suspension
only. This gate mapping is not consensus handling. A methodology `PASS`
means the data/process gate was assessed correctly, not that an alpha strategy
has passed.

Actions:

- KEEP-family: retain the accepted experiment commit, update the numeric
  baseline when the decision artifact requires it, and log the metrics.
- DISCARD/CRASH: do not promote the disposable experiment commit; log why and
  move to the next proposal.
- NO_CONSENSUS: set `NOT_RUN` and the explicit no-memory flag, remain
  unsuspended, then begin the next iteration's fresh context pass.
- After every memory-required final decision, the PM writes MemPalace drawers
  and KG facts for experiment, feature, model, metric, decision, and failure
  mode. `NO_CONSENSUS` and `INFRA_BLOCKED` never enter MemPalace.
- Write a MemPalace diary entry only as part of memory-required final experiment
  logging.
- `autoresearch-start-next` persists the immutable per-iteration canonical
  decision receipt under the autoresearch state directory before replacing a
  completed state. That receipt, bound to the state reference, final decision,
  verification artifact, memory receipt, and instruction manifest digest, is
  the platform decision authority with MemPalace.

## Recovery And Status

- On resume, read git status, active background tasks, canonical decision
  receipts, and MemPalace. Re-enter the first incomplete stage and inspect
  detached run directories before relaunching anything.
- Report detached-run status in ordinary concise PM transcript prose derived
  from bounded polling, logs, receipts, and the launcher status ledger.
- Post progress every 10 iterations.
- Monitoring is read-only. On confirmed shared-infrastructure failure, report
  the exact blocker evidence and await human/Codex operator action. The PM
  never touches G2 directly.
- Do not stop after one experiment. If a declared finite goal is satisfied, the
  PM may report that status but must continue the loop until an explicit
  human/Codex control-command `stop` halts it.
- Do not reduce experiment or verification scope to dodge the launcher,
  watchdog, or cleanup requirements.
