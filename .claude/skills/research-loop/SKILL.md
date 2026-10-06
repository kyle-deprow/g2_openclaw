---
name: research-loop
description: Concise mirror of the bounded native research-owner contract.
---

# Research loop mirror

This mirror carries the same canonical rules as the OpenClaw runtime skill.
The reversal text is a proposal and operating contract, not evidence of alpha
or proof that a current-price ETF feed is installed.

## Canonical rules

- Astra is the research owner.
- Native Luna is the implementer and runner.
- Native OpenAI/Codex Sol (`gpt-5.6-sol`, xhigh, fast) is reviewer-only.
- OpenAI/Codex remains the provider; no fallback or route switching.
- Rule: hypothesis equals iteration; each attempt is code → review → run.
- Use maximum three attempts; Astra explicitly chooses FINISH, ABANDON, or PAUSE.
- Every attempt outcome, refusal, and terminal state returns to Astra.
- Reserve review evidence before the one native Sol review; the evidence and
  bundle are immutable.
- Reconcile the official child run and bind its identity, verdict, mode, attempt,
  commit, and spec digest. Never retype a verdict or respawn after an unknown
  result.
- Native review collection reserves with
  `review-reserve ATTEMPT_ID --root ROOT --bundle-dir BUNDLE_DIR --wake-key WAKE_KEY --owner-key OWNER_KEY
  --openclaw-database
  /home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`;
  the command binds the owner run from the completed IMPLEMENTED wake and official event, requiring it to be the active running owner writer, and defers the owner thread: reconcile binds it from the official event once flushed. Reserve and spawn exactly
  once in this owner turn. After the normal native spawn ACK, STOP and call
  `sessions_yield` for the owned completion. Do not reconcile or collect in
  this owner turn: exact owner run/thread correlation requires the original
  owner turn to end. The completion callback after that turn ends may reconcile
  and collect exactly once from the official child-thread ACK; it must never
  respawn. The reservation output's
  top-level `model`, `effort`, and `prompt_sha256` are authoritative. Its
  `spawn_arguments` contains only `task_name`, `message`, `agent_type`, and
  `fork_turns` (`none`), with no `cwd`; `message` contains the immutable
  absolute bundle path. Invoke exactly the returned native spawn arguments.
  That later reconcile records the official child-thread ACK; no separate
  acknowledgement is created or retyped. Reconcile/collect/cancel use these same
  managed-root database paths
  `/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`
  and
  `/home/dev/.openclaw/agents/research-orchestrator/agent/codex-home/state_5.sqlite`,
  and pass the latter as `--codex-state-database` to review-reconcile,
  review-collect, and review-cancel. The CLI derives these paths from the managed OpenClaw root that contains `--root` (the wake text renders them concretely) and rejects any other path, including arbitrary/latest
  records or caller-invented paths. The spawn message is encrypted on the host,
  so `prompt_sha256` is reservation-side evidence only and is never
  host-verified; binding rests on the exact task name plus host role, model,
  effort, and the strict verdict binding. Collect trusts the child's official terminal rollout; an `announce:codex-native` callback is optional corroboration that must agree if present. An unfinished child stays pending, never FAIL. A failed, errored, or aborted reviewer child makes the attempt a terminal REVIEW_FAILED with the host reason as the sole finding, with no supersession or re-review; it is distinct from a reviewer FAIL verdict but final for the attempt. Limitation: an operator cannot stop a
  running native reviewer through `chat.abort` after the owner yields (the reply
  reports `owner_run_inactive_no_supported_child_cancel`); it ends on its own
  terminal evidence. Do not use the retired `--core-database`, ACPX, or Claude
  paths, infer
  alternatives, search arbitrary records, or repair a verdict. Preserve
  refusal if correlation remains unavailable.
- Analysis uses `--scenarios /scenarios --inputs /inputs --out /stage/analysis`;
  cost specs are `/inputs/evaluation-specs/{spec_id}.json`, not
  `/evaluation-specs`. The canonical EvaluationSpecSet and RunPlan are
  host-attested control evidence, not automatically mounted analysis inputs;
  do not invent mounts/args or leak host paths. Frozen future specs, RunPlans,
  synthetic fixtures, and reviewed analysis must agree with this contract.
- Read existing research status/control surfaces only. Preserve typed admission
  refusals, policy-unset pause, exhausted-attempt completion, exact cancel,
  owner wake, and read-only main controls.
- Receipt artifact digests bind exact bytes rather than provider attestation;
  native readiness uses model, effort, and role from the official rollout, with
  requested `fast` kept separate from observed `unknown` service tier.
- Pause on a missing policy ceiling, unproven route, unresolved review, invalid
  inputs, or lost job. Do not clear a readiness pause autonomously. Invalid
  inputs remain refusals.
- The capability is price-panel-only capability within ETF scope.
- Stock refusal without trusted point-in-time earnings coverage is mandatory;
  unknown or unavailable earnings fail closed.
- Scientific boundary: price-panel-only capability, ETF scope, trusted panel
  sessions, and immutable receipts/ledger/evaluator bounds.
- Preserve typed admission refusals and trusted panel sessions as caller
  evidence.
- Use immutable receipts, ledger, and evaluator bounds for scientific evidence.
- Use trusted panel sessions, immutable receipts, the exposure ledger, and
  evaluator bounds; unknown earnings fail closed.
- Make no alpha claim from smoke or synthetic data.

## Reversal campaign bounds

- The requested panel window is 2021-10-01 through 2026-07-31 inclusive (first
  XNYS session 2021-10-01). It is an input bound, not a claim that 2026 is a
  fresh final holdout; exposure and chronological split evidence establish any
  holdout.
- Test a market-adjusted, earlier-volatility-normalized negative intraday shock
  in dated-evidence-supported liquid, unlevered equity-sector ETFs. ETF
  eligibility does not remove earnings exposure; cache availability and a
  retrospectively successful ticker list are not universe evidence.
- Decide at the close, enter at the next regular open, hold long or cash, and
  exit by session five; do not credit the close-to-next-open rebound. Fit all
  betas and scalers on earlier data only. A longer horizon is refused.
- Use one simple baseline and no more than two predeclared volume/regime
  variants. Indicators are features, not independent confirmations.
- Compare unconditional dip-buying, cash, and exposure-matched passive
  baselines; report net costs, 2x/3x costs, folds, drawdown, concentration,
  counts, block-aware uncertainty/nulls, a five-session overlap purge, and the
  exposure ledger. Development exposure is not a fresh final holdout; unknown
  history blocks FINAL_HOLDOUT.
- News, Reddit, and regularized ML are later incremental hypotheses requiring
  supported point-in-time inputs. Historical final engagement or edited-post
  fields are leakage. Missing news is not no-news evidence; any future
  information filter requires validated point-in-time news. Later non-earnings
  continuation, macro-event, and crypto funding/basis ideas are not enabled
  runtime capability. Reject or inconclusive evidence is useful; smoke, API,
  process, or zero-trade success is not scientific evidence.

## Campaign record and new-hypothesis gates (2026-10-04)

- **Family closed.** The single-slot sector-ETF residual reversal family
  (H0001–H0008) is closed: H0008 FINISHED as REJECT_FOR_THIS_DESIGN (paired
  −26.2 bp, p=0.871). Do not author further variants of it on this panel.
- **Holdout.** Sessions after 2026-07-31 are a reserved holdout. Never score,
  tune or inspect them in development; evaluating them needs an explicit
  operator decision.
- **Controls.** A new hypothesis must say why it should beat the same-date
  matched-market and unconditional-dip controls, which beat H0008's signal.
- **Power.** New hypotheses use contract `research-hypothesis-v2` with a
  `power` block. The SD basis must come from outside the scored test period;
  `research power-check` computes the MDE. Create refuses non-canonical specs
  and designs whose MDE exceeds the declared plausible effect.
- **Compute.** Run `compute-probe` after create (it can take tens of minutes to
  hours), then `hypothesis-set-compute` from its requirements, then freeze. A
  DRAFT that cannot be frozen is abandoned with `hypothesis-decide ... ABANDONED`.
- **Submission.** `submission-build` derives the record, run plan and
  provenance index; `submission-preflight` must PASS before
  `implementation-submit`. Never hand-compute digests or targets argv.
- **Operator-only.** `run-reverify` (once, for a verified
  `run_evidence_mismatch`) and `operator-note` (counted in
  `operatorInterventions`) are operator mutations.

- **Active campaign (2026-10-05).** calendar-flows-20261005 runs in root
  `/home/dev/.openclaw/research-v3`, on 15 ETFs from 2021-11 to 2026-07 with
  the quantipy 7a2ce40 evaluator. Its charter is Astra's workspace file
  `campaigns/calendar-flows-20261005/CHARTER.md` (a read-only original sits in
  the campaign input root).

## Frozen fields and proof boundary

The outer frozen fields are `hypothesis_id`, `title`, `spec_json`,
`spec_sha256`, `panel_path`, `receipt_path`, `evaluation_spec_path`,
`evaluation_spec_sha256`, `panel_sha256`, `receipt_sha256`, `max_attempts`,
`base_commit`, `created_at`, `dividends_path`, `dividends_sha256`, and `state`.
The document also freezes its contract, mechanism, prediction, target,
baseline, universe, features, rules, variants, search budget, date bounds,
purpose, forward label, purge, metric, evidence, null tests, rejection,
missing-data, compute, and deliverables fields.
The exact document fields are `contract`, `mechanism`, `prediction`, `target`,
`baseline`, `universe_rule`, `features`, `entry_rule`, `exit_rule`,
`position_sizing_rule`, `variants`, `search_budget_evaluations`, `analysis`,
`evaluation`, `training`, `purpose`, `forward_label_sessions`, `purge_rule`,
`primary_metric`, `minimum_evidence`, `null_tests`, `reject_criteria`,
`missing_data_rule`, `compute`, and `deliverables`; contract v2 adds `power`.

Exposure counting uses a digest-matched immutable ledger and the classifications
NONE, KNOWN_EXPOSED, and UNKNOWN_HISTORY. Unknown history blocks FINAL_HOLDOUT;
development may record it. A holding horizon above five sessions is refused.

Reserve the immutable review bundle before the one native Sol review; reconcile
the official child run and collect a strict verdict bound to the child task,
run, mode, attempt, commit, and spec.
An exact cancel is at most once, with unknown correlation left pending and
paused. Never retype a verdict, respawn after an unknown result, or launch
without operator policy and proven route. An unknown native spawn
acknowledgement or outcome remains pending and never authorizes repeat dispatch.
The runner runs admitted committed work
only, with no-repair/no-retry, substitution, or invented result.

- Completion ownership is per owner turn: a completion-required coding or runner
  stage spawns a fresh configured native child bound to the same hypothesis,
  attempt, worktree, and checkpoint; this is the same attempt and needs no new
  admission. An authorized fresh child for a later bounded correction may
  continue that same attempt, but it does not authorize duplicating or re-running
  a completed stage. Do not revive a prior-turn child with `followup_task` and
  assume its completion is owned now. Live-child followups owned by the current
  turn remain allowed. On `sessions_yield`, report the exact result/error and
  durable checkpoint; no owned pending completion is not an owned wait/autowake,
  and do not fabricate a callback. That error is not permission to duplicate or
  re-run the completed stage, restart it, or switch route/provider.
- An implementation completion callback may verify and submit the implementation
  result, then must STOP. It must not reserve or spawn review from the original
  OPENED callback. Only the next canonical IMPLEMENTED wake supplies `WAKE_KEY`
  for exactly one review reservation and native Sol dispatch; a later correction
  must likewise wait for its next IMPLEMENTED wake. This implementation callback
  is separate from the later review completion callback, which only reconciles
  and collects the already-spawned child and never dispatches again.

Synthetic-versus-installed proof boundary: smoke or synthetic data does not
prove installed readiness. Make no alpha claim from smoke or synthetic data.

## Containment provenance

- Every targets/evaluate/analysis stage built through `gateway.research.containment.stage_plan` must pass `provenance_dir=` and receive deployed `PYTHONPATH=/provenance:/snapshot/src[:/work]`.
- The sandbox writes `research-containment-provenance-v1` records to `/stage/provenance/`.
- The implementer synthetic harness retains those record dirs inside the committed worktree and submits a `research-provenance-evidence-v1` index via `implementation-submit --provenance-evidence`.
- Submission is refused when a stage lacks records, `quantipy` resolves outside `/snapshot/src`, or a `/work` module differs from the tested commit.
- The historical worker applies the same verification and fails the run with `provenance_invalid`.
- Evaluator and analysis stages run `python -P -s` and are verified for those interpreter flags, while target entrypoints are verified by module origin and hashes rather than interpreter flags.
