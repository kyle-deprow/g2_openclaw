# G2 OpenClaw — Claude Code Instructions

Shared project rules (stack, layout, G2/OpenClaw rules, commands, guardrails)
live in AGENTS.md and apply to Claude Code verbatim.

## Claude ↔ Codex parity

The repository keeps parallel coding-agent assets:

| Purpose | Codex (canonical) | Claude Code (mirror) |
|---------|-------------------|----------------------|
| Repo skills | .agents/skills/<name>/SKILL.md | .claude/skills/<name>/SKILL.md |
| Subagent personas | .codex/agents/<name>.toml | .claude/agents/<name>.md |
| Project instructions | AGENTS.md | CLAUDE.md |

The .claude/skills/ files are distilled mirrors. Read the canonical file
before non-trivial work and update the mirror when a rule changes. Runtime
skills under gateway/agent_config/skills/ are a separate deployed category;
never blend them into repo skills.

## Current research roles

Astra (research-orchestrator) owns the bounded loop. Native Luna is the
implementer and runner. Claude Code Opus via ACP is reviewer-only, once per
attempt. The route remains OpenAI/Codex with no invented provider or route.

A hypothesis equals one iteration and each attempt is code → review → run.
There are at most three attempts; Astra explicitly chooses FINISH, ABANDON, or
PAUSE after exhaustion. The capability is price-panel-only and ETF-scoped;
stock work requires trusted point-in-time earnings coverage, unknown earnings
fail closed, and horizons above five sessions are refused.

G2 is agent:main:g2. Research-owner traffic is
agent:research-orchestrator:autoresearch:quantipy-v2, served by
research-owner.service. Models have read-only MemPalace context;
memory_search, memory_get, and pre-compaction memory flush are disabled.

## Delegation

Claude is the orchestrator for bounded implementation tasks. Dispatch Codex CLI
workers with the configured gpt-5.6-luna xhigh contract when the task calls
for delegated implementation, and provide exact files, scope, verification,
and a completion sentinel. Do not use a deleted role persona or an ad-hoc
model/provider. Keep implementation, review, and operator deployment ownership
separate.

## Verification

Run finite checks against the changed scope. Read the applicable canonical
skill and repository instructions first. Do not deploy source changes while
reviewing them; the operator owns the later guarded push checkpoint.
