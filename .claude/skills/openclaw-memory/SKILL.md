---
name: openclaw-memory
description: OpenClaw 2026.8.1 memory configuration and the repository's read-only MemPalace policy.
---

# OpenClaw Memory

Read the canonical `.agents/skills/openclaw-memory/SKILL.md` before
non-trivial memory work. This mirror keeps the same current policy and avoids
generic configuration examples that would enable a built-in writer.

## Current repository policy

No autoresearch model writes MemPalace: every model is read-only, the built-in
memory tools memory_search and memory_get are denied, memory flush is disabled,
and the durable research records are the research store and artifact receipts.

MemPalace is optional read-only retrieval, never a control ledger or completion
gate. There is no automated MemPalace writer. Do not use Markdown memory files
for research continuity, and do not add a fallback when retrieval is
unavailable; report the blocker instead.

## OpenClaw 8.1 fields

- Set `memory.search.enabled` explicitly to `false`; the runtime default is
  enabled.
- Set `agents.defaults.compaction.memoryFlush.enabled` explicitly to `false`;
  the runtime default is enabled.
- Use `agents.entries.<id>.memory.search` only for supported per-agent search
  overrides. Shared search settings belong under top-level `memory.search`.
- Keep `tools.deny` entries for `memory_search` and `memory_get` and expose only
  the 19 read-only `mempalace-readonly__*` tools.

Generic OpenClaw material about embeddings, indexing, Markdown memory, session
search, or automatic flush is reference-only here. Do not copy an enabling
example into this repository, invent a writer, or introduce a provider fallback.
Research artifacts and receipts remain the durable authority.
