---
name: mempalace-readonly
description: Read-only MemPalace context access for non-PM autoresearch stage agents. Provides search, diary, traversal, and knowledge graph query guidance without any write-capable workflows.
version: 1.0.0
---

# MemPalace Read-Only Context

## Activation

This skill is for non-PM autoresearch stage agents only. It provides read access
to MemPalace context so debates, reviews, implementation, and fixes can account
for prior experiments without mutating durable memory.

The write-capable `mempalace` skill is PM-only. If this agent is not the PM,
do not activate or rely on that skill.

Do not use OpenClaw built-in memory tools (`memory_search`, `memory_get`) or
Markdown memory files (`MEMORY.md`, `memory/YYYY-MM-DD.md`) for research
continuity. MemPalace is the only durable research memory layer.

## Allowed Read Tools

Use only read-only MemPalace tools:

- `mempalace_status`
- `mempalace_search`
- `mempalace_get_drawer`
- `mempalace_list_drawers`
- `mempalace_list_wings`
- `mempalace_list_rooms`
- `mempalace_get_taxonomy`
- `mempalace_get_aaak_spec`
- `mempalace_diary_read`
- `mempalace_kg_query`
- `mempalace_kg_timeline`
- `mempalace_kg_stats`
- `mempalace_traverse`
- `mempalace_find_tunnels`
- `mempalace_follow_tunnels`
- `mempalace_graph_stats`
- `mempalace_list_tunnels`
- `mempalace_list_hallways`
- `mempalace_memories_filed_away`

## Denied Write And Operation Tools

Non-PM agents should not receive these tools in config. If one appears
available, treat that as a configuration error and stop instead of calling it.

- `mempalace_add_drawer`
- `mempalace_update_drawer`
- `mempalace_delete_drawer`
- `mempalace_delete_by_source`
- `mempalace_check_duplicate`
- `mempalace_checkpoint`
- `mempalace_mine`
- `mempalace_sync`
- `mempalace_create_tunnel`
- `mempalace_delete_tunnel`
- `mempalace_delete_hallway`
- `mempalace_hook_settings`
- `mempalace_reconnect`
- `mempalace_kg_add`
- `mempalace_kg_invalidate`
- `mempalace_diary_write`

## Stage Usage

### Context Curator

Build a compact packet for debate:

- Current best metrics and baseline.
- Last 10 experiment outcomes from MemPalace and `RESEARCH_LOG.md`.
- Prior failures, reviewer objections, data coverage issues, and feature/model
  families already tried.
- Any prior KEEP decisions that should shape the next proposal.

### Debate Agents

Read prior context only when it helps evaluate novelty, leakage risk, data
coverage, overfit risk, or implementation cost. Do not write debate notes,
candidate theories, winning theories, dissent summaries, or `NO_CONSENSUS`
outcomes to MemPalace.

### Implementer And Fixer

Use MemPalace reads to avoid repeating failed feature families or methodology
mistakes. Do not write implementation plans, intermediate metrics, test
failures, or fix notes to MemPalace.

### Reviewer

Use MemPalace reads to compare against prior methodology failures, reviewer
objections, data-coverage requirements, and overfit patterns. Do not write
review findings to MemPalace.

## Failure Policy

If a required MemPalace read tool fails, report the blocker to the PM. Do not
fall back to built-in OpenClaw memory, Markdown memory files, or unstructured
local state.
