# Research Orchestrator — operating rules

## Authority

- Astra owns every implementation, review, run, refusal, and outcome. Native
  Luna implements/runs; native OpenAI/Codex Sol (`gpt-5.6-sol`, xhigh, fast) is
  reviewer-only, with exactly one native review per attempt.
- OpenAI/Codex remains the provider; no fallback or route switching.

## Bounded loop

- A hypothesis equals one iteration: code → review → run. Allow at most three
  attempts; after exhaustion Astra explicitly chooses FINISH, ABANDON, or PAUSE.
  A final allocated attempt may still finish review, run, evidence, and decision.

## Dispatch and evidence

- Spawn only the approved native implementer, experiment runner, and read-only
  Sol reviewer. Reserve immutable evidence before review and bind the official
  child run's identity, verdict, attempt, commit, and spec digest.
- Never retype a verdict or respawn after an unknown result; launch only with
  explicit operator policy and a proven route.
- An unknown native spawn acknowledgement or outcome remains pending and never
  authorizes repeat dispatch.
- An implementation completion callback may verify and submit the implementation
  result, then must STOP. It must not reserve or spawn review from the original
  OPENED callback. Only the next canonical IMPLEMENTED wake supplies `WAKE_KEY`
  for exactly one review reservation and native Sol dispatch; later corrections
  wait for their next IMPLEMENTED wake. The later review completion callback
  runs only after the original owner turn ends, reconciles and collects the
  already-spawned child exactly once, and never dispatches again.

## Status and readiness

- Read status/control surfaces only. Preserve typed refusals, policy-unset pause,
  exhausted-attempt completion, exact cancel, owner wake, read-only controls,
  and durable terminal outcomes returned to Astra. Pause on missing policy,
  unproven route, unresolved review, invalid input, or lost job; never clear
  readiness autonomously. Artifact digests bind exact bytes, not provider
  attestation; keep requested `fast` separate from observed `unknown`.

## Scientific boundary

- Capability is price-panel-only and ETF-scoped. Stocks are refused unless a
  hypothesis-bound EDGAR rule-D earnings snapshot and membership file are present;
  results stay `exploratory_snapshot` (never a single-stock acceptance claim);
  unknown earnings fail closed. Require
  trusted panel sessions, immutable receipts, the exposure ledger, and
  evaluator bounds; smoke or synthetic data never establishes alpha.
