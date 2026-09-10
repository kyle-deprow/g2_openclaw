---
name: openclaw-improvement
description: Operational playbook for bounded OpenClaw research-owner diagnostics and source-only fixes.
---

# Improving OpenClaw

Read the canonical .agents/skills/openclaw-improvement/SKILL.md before
non-trivial work. Keep behavioral constraints in agent files and procedures in
skills. Never hand-edit an installed workspace.

## Current repository contract

- Astra owns the bounded research loop; native Luna implements and runs; Claude
  Code Opus via ACP reviews once per attempt.
- The current source surfaces are gateway/research/, gateway/research/cli.py,
  research-owner.service, and the research-orchestrator persona.
- G2 remains the read-only main interface. Research-owner status, typed
  refusals, policy-unset pause, exact cancel, owner wake, and immutable receipts
  are authoritative.
- Models receive read-only MemPalace context. Built-in memory search and
  pre-compaction flush are disabled; model turns never write durable memory.
- OpenAI/Codex OAuth is the configured route. Do not invent a provider, retry
  through another route, or alter auth while diagnosing.
- Research is price-panel-only and ETF-scoped. Stock work needs trusted
  point-in-time earnings coverage; unknown earnings and horizons above five
  sessions fail closed.
- Worktrees are owner-only under /home/dev/.openclaw/autoresearch/worktrees.
  Preserve dirty worktrees and use finite, focused checks.
- Source changes are deployed later through the guarded push script. A source
  test or synthetic run does not prove installed readiness.

## Failure handling

Read current status and immutable evidence first. Preserve exact refusal,
pause, correlation, and child-task identity. Do not infer state from silence,
retry an unknown acknowledgement, substitute data or evaluators, or clear a
readiness pause autonomously. Report the missing proof and stop.

For a behavior change, update the narrowest canonical skill or source guidance,
refresh its distilled mirror, run the scoped checks, and record the evidence.
Prune a workaround only after the replacement is proven. Do not add a
compatibility route or fallback section.
