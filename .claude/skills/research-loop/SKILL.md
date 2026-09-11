---
name: research-loop
description: Concise mirror of the bounded native research-owner contract.
---

# Research loop mirror

This mirror carries the same canonical rules as the OpenClaw runtime skill.

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
- Scientific boundary: price-panel-only capability, ETF scope, trusted panel
  sessions, and immutable receipts/ledger/evaluator bounds.
- Stock refusal without trusted point-in-time earnings coverage is mandatory.
- Preserve typed admission refusals and trusted panel sessions as caller
  evidence.
- Use immutable receipts, ledger, and evaluator bounds for scientific evidence.
- Use trusted panel sessions, immutable receipts, the exposure ledger, and
  evaluator bounds; unknown earnings fail closed.
- Make no alpha claim from smoke or synthetic data.

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
without operator policy and proven route. The runner runs admitted committed
work only, with no-repair/no-retry, substitution, or invented result.
Synthetic-versus-installed proof boundary: smoke or synthetic data does not
prove installed readiness. Make no alpha claim from smoke or synthetic data.
