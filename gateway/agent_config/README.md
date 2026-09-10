# Managed OpenClaw workspace guidance

This directory contains the source-managed bootstrap files copied to the
human-facing G2 workspace. The separate
`gateway/agent_config/research-orchestrator/` directory is the only deployed
research-owner persona. Keep those workspaces and their authorities separate.

## Runtime contract

OpenClaw is pinned to 2026.9.2, `@openclaw/codex` to 2026.9.2, and embedded
Codex to 0.153.4. The configured route is OpenAI/Codex OAuth. A missing or
unproven route is a refusal; do not invent an alias or switch providers.

G2 traffic uses `agent:main:g2`. The bounded owner loop uses
`agent:research-orchestrator:autoresearch:quantipy-v2` and is served by
`research-owner.service`. The owner unit is bound to the gateway lifecycle,
uses an owner-only environment, and fails closed on an invalid or missing
research root. The source template is
`gateway/openclaw_config/research-owner.service.template`.

The owner is Astra. Native Luna implements and runs admitted work. Claude Code
Opus via ACP is reviewer-only, once per attempt. A hypothesis equals one
iteration; each attempt is code → review → run, with at most three attempts.
Astra chooses FINISH, ABANDON, or PAUSE after exhaustion. Review evidence is
reserved before review and binds the child, attempt, commit, and spec digest.

## Scientific boundary

The initial capability is price-panel-only and ETF-scoped. Stock work requires
trusted point-in-time earnings coverage and fails closed when earnings status is
unknown. A holding horizon above five sessions is refused. Trusted panel
sessions, immutable receipts, the exposure ledger, and evaluator bounds are
required for a scientific statement. Smoke or synthetic data proves contracts,
not installed readiness or alpha.

Admission, status, pause, exact cancel, owner wake, and terminal outcomes are
typed and durable. The main G2 interface is read-only. Models receive only
read-only MemPalace context: `memory_search` and `memory_get` are denied,
`agents.defaults.memorySearch.enabled` is `false`, and
`compaction.memoryFlush.enabled` is `false`. No model writes durable
research memory.

## Source and deployment boundary

The canonical research command vocabulary is the frozen help output:

`uv run gateway-cli research --help`

Read source-managed config and persona files before changing them. Use focused
source-only checks first. Deployment is an operator checkpoint performed later
through the guarded push script; do not run it from documentation work.

The push script owns atomic config publication, stale installed-copy pruning,
and the research-owner unit lifecycle. Keep its prune arrays, route checks,
model checks, and transactional rollback behavior unchanged. Never hand-edit
`~/.openclaw/` or installed service files.

## Main workspace role

The files in this directory are for the human-facing `main` workspace. Main
translates the G2 control MCP operations for status/start/stop requests and
returns the result in the same turn. It does not inspect research worktrees,
spawn children, write MemPalace, or send autonomous announcements.

For G2 changes, preserve the single idle thread view, newest transcript first,
microphone events through the installed EvenHub SDK, and the 576 × 288
container-based display contract. For research changes, use the bounded
research-owner persona and the `quantipy-data-contract` readiness receipts.
