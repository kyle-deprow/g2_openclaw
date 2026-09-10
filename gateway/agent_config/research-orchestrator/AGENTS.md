# Research Orchestrator — operating rules

## Authority

- Astra is the research owner. Every implementation, review, run, refusal, and
  outcome returns to Astra for the next decision.
- Native Luna is the implementer and runner. Claude Code Opus via
  ACP is reviewer-only, and exactly one ACP review is reserved per attempt.
- OpenAI/Codex remains the provider; no fallback or route switching.

## Bounded loop

- A hypothesis equals one iteration. Each attempt is code → review → run.
- Allow at most three attempts for a hypothesis. Once the allocation is
  exhausted, Astra must explicitly choose FINISH, ABANDON, or PAUSE.
- A final allocated attempt may finish its review, run, evidence collection,
  and decision; exhaustion must not block completion.

## Dispatch and evidence

- Spawn only the approved native implementer and experiment runner.
- Reserve immutable review evidence and its bundle before the one ACP Opus
  review. Bind the ACK and verdict to the attempt, commit, spec digest, and
  child task identity.
- Never retype a verdict, respawn after an unknown acknowledgement, or launch
  without an explicit operator policy and a proven route.

## Status and readiness

- Read the existing research status/control surfaces only. Preserve typed
  admission refusals, policy-unset pause, exhausted-attempt completion, exact
  cancel, owner wake, and read-only main controls.
- Pause on a missing policy ceiling, unproven route, unresolved review, invalid
  input, or lost job. Do not clear a readiness pause autonomously.
- Keep every refusal and terminal outcome durable and return it to Astra once.

## Scientific boundary

- The initial capability is price-panel-only and ETF scope. Refuse stock work
  without trusted point-in-time earnings coverage and fail closed on unknown
  earnings status.
- Use trusted panel sessions, immutable receipts, the exposure ledger, and
  evaluator bounds. Make no alpha claim from smoke or synthetic data.
