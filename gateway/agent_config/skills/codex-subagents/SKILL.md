---
name: codex-subagents
description: Native Codex delegation contract for bounded G2 and research work.
---

# Native Codex delegation

This runtime skill governs delegation from the configured OpenClaw Codex
runtime. It does not create a second orchestration route.

## Research roles

- Astra is the research owner and receives every child result.
- Native Luna is the approved implementer and experiment runner.
- Claude Code Opus via ACP is reviewer-only and receives one reserved review
  bundle per attempt.
- Main is read-only and never dispatches research work.

Use only configured native Codex roles. Do not discover roles from session
history, invent a role, override a model, or switch provider. The research
owner uses the exact route and session supplied by the current admission
receipt. OpenClaw session spawning is not the research delegation mechanism.

## Dispatch contract

A dispatch prompt names the hypothesis, attempt, exact worktree, allowed files,
scope fence, evidence contract, finite verification command, and completion
sentinel. Bind the child task to the attempt, commit, and specification digest.
The implementer changes only its experiment worktree. The runner executes only
the admitted committed worktree and performs no repair, retry, substitution,
evaluator substitution, or invented result.

Reserve immutable review evidence before the one ACP Opus review. Verify the
acknowledgement, child task identity, run, mode, attempt, commit, and spec
digest. Collect a strict bound verdict. Never retype a verdict or respawn after
an unknown acknowledgement. An exact cancel rereads the current task and is
sent once; unknown correlation remains pending and pauses the owner.

When a new owner turn needs a completion-required coding or runner stage, spawn
a fresh configured native child bound to the same hypothesis, attempt, worktree,
and checkpoint; this is the same attempt and needs no new admission. An
authorized fresh child for a later bounded correction may continue that same
attempt, but it does not authorize duplicating or re-running a completed stage.
Do not revive a child completed in a prior owner turn with `followup_task` and
assume its completion is owned by the new turn. Followups to a live child already
owned by the current turn remain allowed. If `sessions_yield` reports no owned
pending completion or an error, report its exact result/error and durable
checkpoint; do not claim an owned wait/autowake or fabricate a callback. That
error is not permission to duplicate or re-run the completed stage, restart it,
or switch route/provider.

Every completion-required child handoff receives a non-empty normal Astra
acknowledgement, including while another required child remains pending. Do not
use a tool-only wait, conceal an error, send an autonomous G2 announcement, or
claim completion without the durable artifact.

## Verification and boundaries

A hypothesis equals one iteration; each attempt is code → review → run, with at
most three attempts. Astra explicitly chooses FINISH, ABANDON, or PAUSE after
exhaustion. Typed admission refusals, policy-unset pause, exhausted-attempt
completion, owner wake, exact cancel, and read-only main controls are durable
contract behavior.

The scientific boundary is price-panel-only and ETF-scoped. Refuse stock work
without trusted point-in-time earnings coverage; unknown earnings and horizons
above five sessions fail closed. Require trusted panel sessions, immutable
receipts, the exposure ledger, and evaluator bounds. Smoke or synthetic data
proves contracts only.

Use quantipy-data-contract readiness receipts. Do not query a database or
provider directly, install dependencies, edit shared gateway/runtime/auth
files, write MemPalace, or deploy source changes. Models have read-only
MemPalace context; memory search and pre-compaction flush are disabled.

The frozen command vocabulary is available from:

uv run gateway-cli research --help

Report exact evidence and stop when route, policy, review, input, or job proof
is missing.
