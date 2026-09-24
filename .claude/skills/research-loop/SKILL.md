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
- Claude Code Opus via ACP is reviewer-only.
- OpenAI/Codex remains the provider; no fallback or route switching.
- Rule: hypothesis equals iteration; each attempt is code → review → run.
- Use maximum three attempts; Astra explicitly chooses FINISH, ABANDON, or PAUSE.
- Every attempt outcome, refusal, and terminal state returns to Astra.
- Reserve review evidence before the one ACP Opus review; the evidence and
  bundle are immutable.
- The ACK binds the exact child task, run, mode, attempt, commit, and spec
  digest. Never retype a verdict or respawn after an unknown acknowledgement.
- Review collection uses the verified local paths
  `--core-database /home/dev/.openclaw/state/openclaw.sqlite`,
  `--acpx-sessions /home/dev/.openclaw/workspace/state/sessions`, and
  `--claude-projects /home/dev/.claude/projects`; do not infer alternatives,
  search arbitrary records, or repair a verdict. Preserve refusal if correlation
  remains unavailable.
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
`missing_data_rule`, `compute`, and `deliverables`.

Exposure counting uses a digest-matched immutable ledger and the classifications
NONE, KNOWN_EXPOSED, and UNKNOWN_HISTORY. Unknown history blocks FINAL_HOLDOUT;
development may record it. A holding horizon above five sessions is refused.

Reserve the immutable review bundle before the one ACP Opus review; collect a
strict ACK/verdict bound to the child task, run, mode, attempt, commit, and spec.
An exact cancel is at most once, with unknown correlation left pending and
paused. Never retype a verdict, respawn after unknown acknowledgement, or launch
without operator policy and proven route. The runner runs admitted committed work
only, with no-repair/no-retry, substitution, or invented result.
Synthetic-versus-installed proof boundary: smoke or synthetic data does not
prove installed readiness. Make no alpha claim from smoke or synthetic data.

## Containment provenance

- Every targets/evaluate/analysis stage built through `gateway.research.containment.stage_plan` must pass `provenance_dir=` and receive deployed `PYTHONPATH=/provenance:/snapshot/src[:/work]`.
- The sandbox writes `research-containment-provenance-v1` records to `/stage/provenance/`.
- The implementer synthetic harness retains those record dirs inside the committed worktree and submits a `research-provenance-evidence-v1` index via `implementation-submit --provenance-evidence`.
- Submission is refused when a stage lacks records, `quantipy` resolves outside `/snapshot/src`, or a `/work` module differs from the tested commit.
- The historical worker applies the same verification and fails the run with `provenance_invalid`.
- Evaluator and analysis stages run `python -P -s` and are verified for those interpreter flags, while target entrypoints are verified by module origin and hashes rather than interpreter flags.
