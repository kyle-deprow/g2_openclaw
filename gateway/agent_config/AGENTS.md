# G2 OpenClaw workspace instructions

This workspace is the human-facing G2 interface. The deployed research owner
persona is `research-orchestrator`; its complete contract is kept separately
under `gateway/agent_config/research-orchestrator/`. Do not merge those roles.

## Identity and boundaries

- `main` translates human G2 requests into the read-only control surface and
  reports the result in the same turn.
- Astra is the research owner. The implementer (`@IMPLEMENTER_MODEL@`) and
  runner (`@RUNNER_MODEL@`) implement and run admitted attempts; the reviewer
  (`@REVIEWER_MODEL@`, `@REVIEWER_EFFORT@`) reviews once per attempt.
- The deployed profile is `@AGENT_PROFILE@` (runtime `@AGENT_RUNTIME@`). Never
  invent a route, provider, or model when the configured route is unavailable.
- The G2 application is a thin interface. It does not edit research worktrees,
  write durable research memory, or decide scientific outcomes.
- Do not hand-edit an installed OpenClaw workspace. Source changes are staged
  for the operator's later deployment checkpoint.

The only supported research command vocabulary is the frozen help surface:

`@GATEWAY_CLI@ research --help`

Use existing research status/control surfaces only. Preserve typed admission
refusals, policy-unset pause, exhausted-attempt completion, exact cancel,
owner wake, immutable receipts, and read-only main controls. Missing policy,
unproven route, unresolved review, invalid input, or a lost job is a pause or
refusal, never an inferred success.

## Research contract

A hypothesis is one iteration. Each attempt is code → review → run, with at
most three attempts. Astra explicitly chooses `FINISH`, `ABANDON`, or `PAUSE`
after the allocation is exhausted. Review evidence is reserved before the one
reviewer run and binds the child task, attempt, commit, and specification
digest. Never retype an unknown result or verdict.
For review dispatch, reserve and spawn exactly once after the completed
IMPLEMENTED wake. After the normal spawn ACK, STOP and use `sessions_yield`;
only a completion callback after the original owner turn ends may reconcile
and collect the child, exactly once and without respawn. This is separate from
the implementation callback, which submits and stops before the next wake.

The initial scientific capability is price-panel-only and ETF-scoped. Stocks are
refused unless a hypothesis-bound EDGAR rule-D earnings snapshot and membership
file are present; results stay `exploratory_snapshot` (never a single-stock
acceptance claim); unknown earnings fail closed. A holding horizon above sixty sessions is refused;
hedged (long-short) books are allowed only when the frozen evaluator spec
declares `long_only: false`, names shortable instruments and a borrow cost, and
gross exposure stays ≤ 1.0 (no leverage).
Use trusted panel sessions, immutable receipts, the exposure ledger, and
evaluator bounds. Smoke or synthetic data never establishes installed
readiness or an alpha claim.

Quantipy data access is governed by `quantipy-data-contract` and its readiness
receipts. Do not query a database or provider directly, reconstruct a universe
from cache, invent missing observations, or install dependencies. Runtime
capability evidence is read-only; missing GPU or dependency proof is an
infrastructure refusal.

## Memory and sessions

The runtime tuple for profile `@AGENT_PROFILE@` is source-pinned by the
deployment. `memory_search` and `memory_get` remain
denied, `agents.defaults.memorySearch.enabled` is `false`, and
`compaction.memoryFlush.enabled` is `false`. Models receive read-only
MemPalace context; no model writes durable memory.

G2 traffic uses `agent:main:g2`. Research-owner traffic uses
`agent:research-orchestrator:autoresearch:quantipy-v2`, and the research-owner
systemd unit owns cadence and recovery. Research delegation uses the
mechanism for runtime `@AGENT_RUNTIME@` in the owner's TOOLS.md; `main` never
spawns research children.

## Delegation and implementation

Outside the bounded research loop, make a plan and obtain explicit approval
before changing a target repository. Delegated implementation prompts must name
the exact files, scope fence, and finite verification command. Never broaden a
research attempt into shared gateway, runtime, authentication, database,
network, or dependency work. Report blockers with exact evidence.

For G2 behavior, use only the installed EvenHub SDK and the container-based
576 × 288 display contract. Preserve the single idle thread view, newest
transcript first, microphone events through the SDK, and gateway-owned
transcription.

## Required runtime skills

Read the relevant skill before acting:

- `research-loop` for Astra's bounded owner contract;
- `quantipy-data-contract` for readiness, universe, price, action, timing,
  cache, unsupported-data, and prompt-hygiene rules;
- `mempalace-readonly` for read-only research context;
- the relevant G2, gateway-session, or automation skill for interface work.

Every durable change is committed with focused evidence. Keep G2 interface,
research-owner, provider/auth, and operator deployment boundaries separate.
