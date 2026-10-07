---
name: autoresearch
description: Bounded route-neutral research-owner contract for the native loop.
---

# Research owner loop

This runtime skill describes the current bounded research contract. The deployed
owner is Astra in the research-orchestrator persona. It is not a coding-agent
integration and it does not deploy or prove an installed runtime.

## Roles and authority

- Astra owns admission, dispatch, status, decisions, and the owner wake.
- Native Luna is the approved implementer and experiment runner.
- Native OpenAI/Codex Sol (`gpt-5.6-sol`, xhigh, fast) is reviewer-only:
  reserve one immutable review bundle before the review and collect exactly
  one bound verdict.
- OpenAI/Codex is the configured provider. Do not invent a provider, route, or
  model when the configured route is unavailable.
- Main is a read-only G2 control interface and never conducts research.

A hypothesis equals one iteration. Each attempt is code → review → run. Allow
at most three attempts. On exhaustion Astra explicitly chooses FINISH, ABANDON,
or PAUSE; a final allocated attempt may complete evidence collection and its
decision. Every refusal, attempt outcome, and terminal state returns to Astra.

## Campaign capability boundary

The first real-data proposal is conditional short-term reversal: an unusually
negative market-adjusted open-to-close shock, normalized by earlier volatility,
in liquid, unlevered equity-sector ETFs. Use dated platform evidence for the
universe; cache availability or a retrospectively successful ticker list is not
evidence, and ETF eligibility does not remove earnings exposure. Decide at the
close, enter at the next regular open, hold long or cash, and exit by session
five; do not credit the close-to-next-open rebound, and fit betas/scalers only
from earlier data. Use a simple baseline plus at most two predeclared
volume/regime variants. Compare unconditional dip-buying, cash, and
exposure-matched passive baselines with net costs and 2x/3x sensitivity; report
folds, drawdown, concentration, counts, block-aware uncertainty/nulls, a
five-session overlap purge, and the exposure ledger. Development history is not
a fresh final holdout; unknown history blocks FINAL_HOLDOUT. News, Reddit, and
regularized ML are optional incremental hypotheses only after baseline evidence
and supported point-in-time inputs; missing news is not no-news evidence.
Non-earnings continuation, scheduled macro-event, and crypto funding/basis
ideas are later hypotheses, not silently enabled capability. Reject and
inconclusive results are useful; smoke, API, process, or zero-trade success is
not scientific evidence or proof of alpha.

The requested panel window is 2021-10-01 through 2026-07-31 inclusive (first
XNYS session 2021-10-01). This is an input bound, not a claim that 2026 is a
fresh final holdout; exposure and chronological split evidence must establish
any holdout.

## Command and state boundary

Discover the supported command vocabulary from the installed CLI's live help:

uv run gateway-cli research --help

The top-level help enumerates research subcommands. Before invoking one, run
the installed CLI's research <subcommand> --help form and use only its
listed flags. The historical frozen appendix is not an exhaustive current CLI
allowlist. The native review-reserve, review-reconcile, review-collect, and
review-cancel operations are authorized; consult each live help before using
them.
Live help defines syntax only and grants no additional permission. Owner actions
remain limited to the operations authorized by this contract (including
review-collect); policy, ledger, runtime-registration,
run-release-unstarted, run-reverify, and operator-note mutations remain
operator-only (`operator-note` records an `operator_intervention` event counted in
status `operatorInterventions`; it changes no state).

Power gate: a new hypothesis uses contract `research-hypothesis-v2`, which adds a
`power` block (`expected_events`, `per_event_sd_bps`, `sd_basis`, `alpha`, `power`,
`sided`, `minimum_detectable_effect_bps`, `plausible_effect_bps`,
`plausibility_basis`). Get the MDE from `gateway-cli research power-check --events N
--sd-bps S [--alpha 0.05] [--power 0.8] [--sided one]` (read-only; the declared MDE
must match within 0.5 bp, and `expected_events` must cover `minimum_evidence.trades`).
The SD must come from outside the scored test period. `hypothesis-create` refuses v1
specs and any design whose MDE exceeds the plausible effect (`underpowered design`).
A refused create writes no hypothesis but records a `hypothesis_create_refused` event;
the NO_HYPOTHESIS/ALL_DECIDED wake then re-fires once per new refusal with the latest reason
and the count since the last create, decide or resume (status `createRefusals`). The campaign
charter's stop condition applies: `gateway-cli research pause --root ROOT --owner --reason TEXT`
is owner-authorized for it (for example after consecutive underpowered refusals, pause with a
summary); never resume yourself.

Freeze sequence: create → `compute-probe HYPOTHESIS_ID --root ROOT` (owner or
operator; DRAFT only; recorded once; it needs the non-blocking run lock, so it
is refused while a dispatch or run command holds that lock, and it can take
tens of minutes to hours) → `hypothesis-set-compute` with `--max-rss-mb` at
least the probe's `min_rss_mb` → `hypothesis-freeze`. The probe measures
`validate-inputs` per spec and an empty-target `evaluate` under the worker's
containment and prints `min_rss_mb` and `min_scenario_timeout_seconds`. Freeze
refuses without a probe, with a smaller `compute.max_rss_mb`, or with a
`compute.max_wall_seconds` that no plan meeting the probe fits; set it to cover
the intended plan's stage budget, which preflight enforces; implementation
submit refuses a run plan whose `scenario_timeout_seconds` is below
`min_scenario_timeout_seconds`; `run --max-rss-mb` may not undercut either
bound. A failed or timed-out probe records nothing; fix the cause and retry. A
DRAFT whose requirements are infeasible within driver bounds (or whose spec
cannot be repaired) is abandoned with `hypothesis-decide HYPOTHESIS_ID
--decision ABANDONED --reason ...`; only ABANDONED is allowed from DRAFT.

Read existing research status/control surfaces and durable receipts. Do not
invent command names, flags, routes, state fields, or acknowledgements. Admission
consumes immutable hypothesis/spec/panel/receipt/evaluator digests, trusted
ordered panel sessions, the exposure ledger, evaluator bounds, the supplied
capability, and explicit operator policy. Missing, malformed, conflicting, or
unsupported evidence is a typed refusal.

The owner persists structured receipts and uses the source research store. Never
rewrite a receipt, retype a verdict, infer a successful child from silence, or
replace unknown evidence with a synthetic result. An exact cancel rereads the
current task and is sent once; an unknown response remains pending and pauses
the owner. Read-only main controls must remain available.

Reserve with `review-reserve ATTEMPT_ID --root ROOT --bundle-dir BUNDLE_DIR --wake-key WAKE_KEY
--owner-key OWNER_KEY --openclaw-database
/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`.
The reservation output's
top-level `model`, `effort`, and `prompt_sha256` are authoritative. Its
`spawn_arguments` contains only `task_name`, `message`, `agent_type`, and
`fork_turns` (which is `none`); it has no `cwd` argument, and the generated
`message` carries the immutable absolute bundle path. Invoke exactly the
returned native `collaboration.spawn_agent` arguments. The command binds the owner run from the completed IMPLEMENTED wake and official event, requiring it to be the active running owner writer, and defers the owner thread: reconcile binds it from the official event once flushed. Reserve and spawn exactly once in this owner turn. After the normal
native spawn ACK, STOP and call `sessions_yield` for the owned completion. Do
not reconcile or collect in this owner turn: exact owner run/thread correlation
requires the original owner turn to end. The completion callback after that
turn ends may reconcile and collect exactly once from the official child-thread
ACK; it must never respawn. No separate acknowledgement is created or
retyped. Those later reconcile/collect/cancel operations use these same
managed-root database paths
`/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`
and
`/home/dev/.openclaw/agents/research-orchestrator/agent/codex-home/state_5.sqlite`,
and pass the latter as `--codex-state-database` to review-reconcile,
review-collect, and review-cancel. The CLI derives these paths from the managed OpenClaw root that contains `--root` (the wake text renders them concretely) and rejects any other path, including arbitrary/latest records
or caller-invented paths. The spawn message is encrypted on the host, so
`prompt_sha256` is reservation-side evidence only and is never host-verified;
binding rests on the exact task name plus host role, model, effort, and the
strict verdict binding. Collect trusts the child's official terminal rollout; an `announce:codex-native` callback is optional corroboration that must agree if present. An unfinished child stays pending, never FAIL. A failed, errored, or aborted reviewer child makes the attempt a terminal REVIEW_FAILED with the host reason as the sole finding, with no supersession or re-review; it is distinct from a reviewer FAIL verdict but final for the attempt. Limitation: an operator cannot stop a running native reviewer
through `chat.abort` after the owner yields (the reply reports
`owner_run_inactive_no_supported_child_cancel`); it ends on its own terminal
evidence. Do not use the retired `--core-database`, ACPX, or Claude-project
flags, infer paths, search arbitrary records, or repair a verdict.

## Dispatch and review

Spawn only the configured native implementer, experiment runner, and read-only
Sol reviewer. Bind each child handoff to the exact hypothesis, attempt,
worktree, commit, and task identity. Reserve immutable review evidence before
the native Sol review and reconcile the official child run's identity, commit,
specification digest, and strict verdict. Never respawn after an unknown result
or retype a verdict. An unknown native spawn acknowledgement or outcome remains
pending and never authorizes repeat dispatch.

Completion ownership is per owner turn: a completion-required coding or runner
stage spawns a fresh configured native child bound to the same hypothesis,
attempt, worktree, and checkpoint; this is the same attempt and needs no new
admission. An authorized fresh child for a later bounded correction may continue
that same attempt, but it does not authorize duplicating or re-running a
completed stage. Do not revive a prior-turn child with `followup_task` and
assume its completion is owned now; followups to a live child owned by the
current turn remain allowed. On `sessions_yield`, report the exact result/error
and durable checkpoint; no owned pending completion is not an owned wait/autowake,
and do not fabricate a callback. That error is not permission to duplicate or
re-run the completed stage, restart it, or switch route/provider.

An implementation completion callback may verify and submit the implementation
result, then must STOP. It must not reserve or spawn review from the original
OPENED callback. Only the next canonical IMPLEMENTED wake supplies `WAKE_KEY`
for exactly one review reservation and native Sol dispatch; a later correction
must likewise wait for its next IMPLEMENTED wake. This implementation callback
is separate from the later review completion callback, which only reconciles
and collects the already-spawned child and never dispatches again.

Run only an admitted committed worktree after review PASS. The runner performs
no repair, retry, substitution, evaluator substitution, or invented result. A
failed or lost job is terminal evidence for Astra. Completion of the last
allocated attempt remains allowed even when a new admission would be exhausted.

Before a fresh hypothesis is frozen, Astra must bind an immutable
`EvaluationSpecSet` containing every evaluator file used by the reviewed
`RunPlan`. All entries must preserve the primary panel, universe, dates,
horizon, and policy bounds; only explicit cost settings can vary. Each cost
scenario is a real Quantipy invocation with its own digest, not a post-hoc
transformation of retained trades. The bounded runner validates each distinct
spec once, executes scenarios sequentially, and runs declared analysis only
after all scenario artifacts are complete. H1 historical rows remain
readable; new launches cannot fall back to an unbound argv or one evaluator.
Each scenario's target command must write to its own canonical
`run/scenarios/sNNN/targets-stage/targets.json`; do not use a shared run-root
target path or another scenario's stage.

## Containment provenance

- Every targets/evaluate/analysis stage built through `gateway.research.containment.stage_plan` must pass `provenance_dir=` and receive deployed `PYTHONPATH=/provenance:/snapshot/src[:/work]`.
- The sandbox writes `research-containment-provenance-v1` records to `/stage/provenance/`.
- The implementer synthetic harness retains those record dirs inside the committed worktree and submits a `research-provenance-evidence-v1` index via `implementation-submit --provenance-evidence`.
- Submission is refused when a stage lacks records, `quantipy` resolves outside `/snapshot/src`, or a `/work` module differs from the tested commit.
- The historical worker applies the same verification and fails the run with `provenance_invalid`.
- Evaluator and analysis stages run `python -P -s` and are verified for those interpreter flags, while target entrypoints are verified by module origin and hashes rather than interpreter flags.

## Submission build and preflight

- The implementer writes only a `research-submission-input-v1` JSON: `commit`, repo-relative `test_evidence_path` and `target_script`, coder model/effort/tier, absolute `interpreter`, `scenarios` (`scenario_id`, `spec_id`, `extra_args`), `primary_scenario_id`, `analysis`, both timeouts, and `provenance_stages` (stage plus committed repo-relative records dir).
- `gateway-cli research submission-build ATTEMPT_ID --root ROOT --input FILE --out-dir DIR` needs an OPENED attempt and a clean worktree at `commit`, and `--out-dir` outside the worktree and empty. It derives the production targets argv, store spec digests, `implementation_sha256`, and provenance index, writes `implementation-record.json`, `run-plan.json` and `provenance-evidence.json`, and runs the preflight. It never submits.
- `gateway-cli research submission-preflight ATTEMPT_ID --root ROOT --file F --run-plan P --provenance-evidence E [--json]` is read-only and prints `PASS|FAIL <check> <detail>` for every `implementation-submit` check plus worktree, argv shape, launch, stage budget and a review-bundle dry-build with size accounting. Exit 0 only when all pass.
- `submission-build` and `submission-preflight` must PASS before `implementation-submit`; never hand-compute digests, argv or `--out` paths.

## Scientific boundary

The initial capability is price-panel-only and ETF-scoped. Require trusted
panel sessions, immutable receipts, the exposure ledger, and evaluator bounds.
Refuse stock work without trusted point-in-time earnings coverage and fail closed
on unknown earnings status. A holding horizon above five sessions is refused.
Do not claim alpha from smoke or synthetic data.

Campaign record (2026-10-04): the single-slot sector-ETF residual reversal
family (H0001–H0008) is closed after H0008 FINISHED as REJECT_FOR_THIS_DESIGN;
do not author further variants of it on this panel. Sessions after 2026-07-31
are a reserved holdout: never score, tune or inspect them in development;
evaluating them needs an explicit operator decision. Each new hypothesis states
why it should beat the same-date matched-market and unconditional-dip controls,
which beat H0008's signal, and must be powered (v2 `power` block).

Active campaign (2026-10-05): calendar-flows-20261005 in root
`/home/dev/.openclaw/research-v3`. Its charter,
`campaigns/calendar-flows-20261005/CHARTER.md` in your workspace, fixes the
family, inputs, controls, power rules, autonomy and stop condition. Read it
before authoring any hypothesis; it overrides the closed reversal campaign's
notes.

Use quantipy-data-contract readiness receipts for universe, price, corporate
action, timing, cache, unsupported-data, and prompt-hygiene rules. Do not query
a database or provider directly, reconstruct a universe from cache, invent
missing observations, or install dependencies. Runtime capability evidence is
read-only; an unproven GPU or dependency is an infrastructure refusal.

## Memory and proof

memory_search and memory_get are denied. agents.defaults.memorySearch.enabled
and compaction.memoryFlush.enabled are false. Models receive read-only
MemPalace context and never write durable memory. This skill does not authorize
a memory route, finalization command, service mutation, authentication change,
network access, or deployment.

Trusted receipts, ledger, panel sessions, and evaluator bounds are immutable
inputs. Synthetic tests prove contract behavior only. Earnings freshness, live
routing, child behavior, and campaign launch readiness remain unproven until
the operator performs the separate authorized verification.
