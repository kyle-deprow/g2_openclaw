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

- `mempalace-readonly.mempalace_status`
- `mempalace-readonly.mempalace_search`
- `mempalace-readonly.mempalace_get_drawer`
- `mempalace-readonly.mempalace_list_drawers`
- `mempalace-readonly.mempalace_list_wings`
- `mempalace-readonly.mempalace_list_rooms`
- `mempalace-readonly.mempalace_get_taxonomy`
- `mempalace-readonly.mempalace_get_aaak_spec`
- `mempalace-readonly.mempalace_diary_read`
- `mempalace-readonly.mempalace_kg_query`
- `mempalace-readonly.mempalace_kg_timeline`
- `mempalace-readonly.mempalace_kg_stats`
- `mempalace-readonly.mempalace_traverse`
- `mempalace-readonly.mempalace_find_tunnels`
- `mempalace-readonly.mempalace_follow_tunnels`
- `mempalace-readonly.mempalace_graph_stats`
- `mempalace-readonly.mempalace_list_tunnels`
- `mempalace-readonly.mempalace_list_hallways`
- `mempalace-readonly.mempalace_memories_filed_away`

Non-PM agents should not receive write-capable MemPalace tools in config. If
one appears available, treat that as a configuration error and stop instead of
calling it.

## Stage Usage

### Context Curator

Build a compact packet for debate:

- Current best metrics and baseline.
- Last 10 experiment outcomes from MemPalace and canonical decision receipts.
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
