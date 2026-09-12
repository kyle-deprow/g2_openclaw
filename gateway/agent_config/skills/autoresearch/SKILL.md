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
- Claude Code Opus via ACP is reviewer-only: reserve one immutable review bundle
  before the review and collect exactly one bound verdict.
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

The only supported command vocabulary is the frozen help output:

uv run gateway-cli research --help

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

## Dispatch and review

Spawn only the configured native implementer and experiment runner. Bind each
child handoff to the exact hypothesis, attempt, worktree, commit, and task
identity. Reserve immutable review evidence before the ACP Opus review and
verify its acknowledgement, child identity, commit, specification digest, and
strict verdict. Never respawn after an unknown acknowledgement or retype a
verdict.

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

## Scientific boundary

The initial capability is price-panel-only and ETF-scoped. Require trusted
panel sessions, immutable receipts, the exposure ledger, and evaluator bounds.
Refuse stock work without trusted point-in-time earnings coverage and fail closed
on unknown earnings status. A holding horizon above five sessions is refused.
Do not claim alpha from smoke or synthetic data.

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
