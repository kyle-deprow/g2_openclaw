---
name: research-loop
description: Bounded, route-neutral research-owner contract for the native research loop.
---

# Research loop

This is an OpenClaw runtime skill. It is not a Codex/G2 implementation skill.
It describes the route-neutral contract only; it does not deploy or prove an
installed runtime.

## Canonical roles and loop

- Astra is the research owner.
- Native Luna is the implementer and runner.
- Claude Code Opus via ACP is reviewer-only.
- OpenAI/Codex remains the provider; no fallback or route switching.
- Rule: hypothesis equals iteration; each attempt is code → review → run.
- Use maximum three attempts; Astra explicitly chooses FINISH, ABANDON, or PAUSE.
- Every attempt outcome, refusal, and terminal state returns to Astra.

## Frozen specification

The immutable outer `HypothesisSpec` fields are `hypothesis_id`, `title`,
`spec_json`, `spec_sha256`, `panel_path`, `receipt_path`,
`evaluation_spec_path`, `evaluation_spec_sha256`, `panel_sha256`,
`receipt_sha256`, `max_attempts`, `base_commit`, `created_at`, `dividends_path`,
`dividends_sha256`, and `state`.

The frozen hypothesis document fields are `contract`, `mechanism`, `prediction`,
`target`, `baseline`, `universe_rule`, `features`, `entry_rule`, `exit_rule`,
`position_sizing_rule`, `variants`, `search_budget_evaluations`, `analysis`,
`evaluation`, `training`, `purpose`, `forward_label_sessions`, `purge_rule`,
`primary_metric`, `minimum_evidence`, `null_tests`, `reject_criteria`,
`missing_data_rule`, `compute`, and `deliverables`. Each feature declares
`name`, `source`, `as_of_rule`, and `lookback_sessions`; bounds include dates,
minimum evidence, compute limits, and relative deliverable paths.

## Admission, status, and readiness

- Admission consumes typed caller evidence: immutable spec/panel/receipt/
  evaluator digests, a trusted receipt, trusted ordered panel sessions, the
  exposure ledger, evaluator bounds, the supplied capability, and explicit
  operator policy. Missing, malformed, conflicting, or unsupported input is a
  typed refusal, never an inferred value.
- The capability is price-panel-only capability within ETF scope.
- Scientific boundary: price-panel-only capability, ETF scope, trusted panel
  sessions, and immutable receipts/ledger/evaluator bounds.
- Stock refusal without trusted point-in-time earnings coverage is mandatory;
  unknown or unavailable earnings fail closed. A holding horizon above five
  sessions is refused.
- Preserve typed admission refusals and trusted panel sessions as caller
  evidence.
- Use immutable receipts, ledger, and evaluator bounds for scientific evidence.
- Exposure counting uses an immutable, digest-matched ledger with non-overlap,
  known ranges, `history_unknown`, and `trial_count`. Classify exposure as
  NONE, KNOWN_EXPOSED, or UNKNOWN_HISTORY. Unknown history blocks FINAL_HOLDOUT;
  valid development work may record it. Never treat absent history as clean.
- Read existing research status/control surfaces only. Preserve typed admission
  refusals, policy-unset pause, exhausted-attempt completion, exact cancel,
  owner wake, and read-only main controls. Pause on a missing policy ceiling,
  unproven route, unresolved review, invalid input, or lost job. Do not clear a
  readiness pause autonomously. Invalid inputs remain refusals.
- Receipt artifact digests bind the exact bytes but do not attest a provider;
  native readiness reads model, effort, and role from the official rollout,
  while requested `fast` and observed `unknown` service tiers stay distinct.

## Review and runner contract

- Spawn only the approved native implementer and experiment runner.
- Reserve review evidence before the one ACP Opus review; the evidence and
  bundle are immutable.
- The ACK must bind the exact child task, run, mode, attempt, commit, and spec
  digest. Collect only a strict verdict bound to that bundle.
- Never retype a verdict or respawn after an unknown acknowledgement.
- Never retype a verdict, respawn after unknown acknowledgement, or launch
  without operator policy and proven route.
- An exact cancel rereads the current task and is sent at most once. Preserve a
  pending/unknown response and pause until correlation is resolved.
- Run only an admitted, committed worktree after review PASS. The runner does
  no-repair/no-retry, input substitution, evaluator substitution, or
  invented result. A failed or lost job remains terminal evidence for Astra.
- Do not launch without explicit operator policy and a proven route. Completion
  of the final allocated attempt remains allowed even when new admission is
  exhausted.

## Scientific proof boundary

Synthetic-versus-installed proof boundary: trusted receipts, ledgers, panel
sessions, and evaluator bounds are immutable inputs. Synthetic tests prove only
contract behavior. Smoke or synthetic data does not prove installed readiness.
Make no alpha claim from smoke or synthetic data. Earnings
freshness, live routing, child behavior, and campaign launch readiness remain
unproven here.
