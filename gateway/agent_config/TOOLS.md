# Tools

## Main interface

The `main` workspace may use only the configured G2 control MCP operations for
human start, status, and stop requests. It must not inspect or mutate research
worktrees, invoke a research child, write MemPalace, or send autonomous
completion announcements. Return the control result to the requesting human.

## Research-owner boundary

Astra owns the bounded research loop in the separate
`research-orchestrator` workspace. Native Luna is the approved implementer and
runner; Claude Code Opus via ACP is the one reviewer per attempt. Delegation is
through native Codex `spawn_agent` with the configured roles, never an ad-hoc
provider or route. Read the frozen `research --help` surface before using a
research operation.

Each completion-required child handoff receives a non-empty normal owner
acknowledgement, even while another required child is pending. Do not silently
wait with a tool-only turn, retype a verdict, respawn after an unknown
acknowledgement, or send a progress message to G2 in place of the owner wake.

## Memory and security

`memory_search` and `memory_get` are denied. `agents.defaults.memorySearch.enabled`
and `compaction.memoryFlush.enabled` are both `false`. Models use the
read-only MemPalace surface; no model writes durable memory. Do not expose
OAuth material, database credentials, raw research data, or installed workspace
contents.

## Verification

Use finite, focused checks against the exact changed scope. A contract test or
synthetic run proves behavior only; it does not prove installed readiness,
provider routing, earnings freshness, child behavior, or campaign launch.
Report exact evidence and pause on an unproven prerequisite.
