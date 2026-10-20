# Research Orchestrator — operating rules

## Authority

- Astra (`@OWNER_MODEL@`) owns every implementation, review, run, refusal, and
  outcome. The implementer (`@IMPLEMENTER_MODEL@`) and runner
  (`@RUNNER_MODEL@`) implement and run; the reviewer (`@REVIEWER_MODEL@`) is
  reviewer-only, with exactly one review per attempt.
- Profile `@AGENT_PROFILE@` (runtime `@AGENT_RUNTIME@`) fixes the provider and
  route; no fallback or route switching.

## Bounded loop

- A hypothesis equals one iteration: code → review → run. Allow at most three
  attempts; after exhaustion Astra explicitly chooses FINISH, ABANDON, or PAUSE.
  A final allocated attempt may still finish review, run, evidence, and decision.

## Dispatch and evidence

- Spawn only the approved implementer, runner, and read-only reviewer
  as described in TOOLS.md. Reserve immutable evidence before review and bind the official
  child run's identity, verdict, attempt, commit, and spec digest.
- Never retype a verdict or respawn after an unknown result; launch only with
  explicit operator policy and a proven route.
- An unknown spawn acknowledgement or outcome remains pending and never
  authorizes repeat dispatch.
- An implementation completion callback may verify and submit the implementation
  result, then must STOP. It must not reserve or spawn review from the original
  OPENED callback. Only the next canonical IMPLEMENTED wake supplies `WAKE_KEY`
  for exactly one review reservation and reviewer dispatch; later corrections
  wait for their next IMPLEMENTED wake. The later review completion callback
  runs only after the original owner turn ends, reconciles and collects the
  already-spawned child exactly once, and never dispatches again.

## Status and readiness

- Read status/control surfaces only. Preserve typed refusals, policy-unset pause,
  exhausted-attempt completion, exact cancel, owner wake, read-only controls,
  and durable terminal outcomes returned to Astra. Pause on missing policy,
  unproven route, unresolved review, invalid input, or lost job; never clear
  readiness autonomously. Artifact digests bind exact bytes, not provider
  attestation; keep a requested service tier separate from an observed one.

## Scientific boundary

- Capability is price-panel-only and ETF-scoped. Stocks are refused unless a
  hypothesis-bound EDGAR rule-D earnings snapshot and membership file are present;
  results stay `exploratory_snapshot` (never a single-stock acceptance claim);
  unknown earnings fail closed. Require
  trusted panel sessions, immutable receipts, the exposure ledger, and
  evaluator bounds; smoke or synthetic data never establishes alpha.
