---
name: mempalace-readonly
description: Read-only MemPalace context access for every autoresearch model thread. Provides search, diary, traversal, and knowledge graph query guidance without any write-capable workflows.
version: 1.1.0
---

# MemPalace Read-Only Context

## Activation

This skill is for every autoresearch model thread. It provides read access
to MemPalace context so debates, reviews, implementation, and fixes can account
for prior experiments without mutating durable memory.

No autoresearch model writes MemPalace: every model is read-only, the built-in memory tools memory_search and memory_get are denied, memory flush is disabled, and the durable research records are the research store and artifact receipts.

MemPalace is optional read-only retrieval for prior context. It is never a
control ledger; it is never a completion gate, and this loop has no automated
MemPalace writer. The research store and artifact receipts remain the durable
records.

Do not use OpenClaw built-in memory tools (`memory_search`, `memory_get`) or
Markdown memory files (`MEMORY.md`, `memory/YYYY-MM-DD.md`) for research
continuity. This skill does not make Markdown files a research authority.

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

No autoresearch model should receive write-capable MemPalace tools in config.
If a `mcp__mempalace__*` write tool appears available, treat that as a
configuration error and stop instead of calling it.

## Usage

Use read-only retrieval when prior context helps evaluate a current admitted
hypothesis, review, implementation, or run. Search only as needed; MemPalace
is optional and never a control ledger or completion gate. Do not write notes,
theories, outcomes, plans, metrics, failures, or verdicts. The research store
and artifact receipts remain the durable records.

## Failure Policy

If a required MemPalace read tool fails, report the blocker to the PM. Do not
fall back to built-in OpenClaw memory, Markdown memory files, or unstructured
local state.
