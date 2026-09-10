---
name: openclaw-memory
description:
  OpenClaw 2026.9.2 memory configuration and read-only MemPalace boundaries for
  this repository. Use when reviewing memory search, compaction flush, or
  memory-tool policy; do not enable a built-in writer or fallback.
---

# OpenClaw Memory Policy

This repository uses the OpenClaw 2026.9.2 memory schema, but its autoresearch
models use optional read-only MemPalace context rather than built-in OpenClaw
memory. The managed configuration and the `mempalace-readonly` runtime skill
are authoritative for this boundary.

## Current repository policy

No autoresearch model writes MemPalace: every model is read-only, the built-in
memory tools memory_search and memory_get are denied, memory flush is disabled,
and the durable research records are the research store and artifact receipts.

MemPalace is optional read-only retrieval. It is never a control ledger or a
completion gate, and this loop has no automated MemPalace writer. Do not use
Markdown memory files as research continuity or authority. If a required
read-only retrieval fails, report the blocker; do not fall back to built-in
memory, Markdown files, or unstructured local state.

## Supported 9.2 configuration shape

The installed 9.2 schema supports these relevant fields:

- `memory.search.enabled` controls built-in memory search. Its runtime default
  is enabled, so this repository explicitly sets it to `false`.
- `memory.citations` controls citation rendering and remains independent of the
  search switch.
- `agents.entries.<id>.memory.search` is the supported per-agent search
  override; shared defaults belong under top-level `memory.search`.
- `agents.defaults.compaction.memoryFlush.enabled` controls the pre-compaction
  agentic flush. The runtime default is enabled, so this repository explicitly
  sets it to `false`.

The repository also denies `memory_search` and `memory_get` globally. Keep the
19 read-only `mempalace-readonly__*` tools from the runtime skill as the only
agent-facing memory surface. Do not add write-capable tools, a built-in memory
backend, or an implicit provider/fallback route.

## Generic reference boundary

OpenClaw's general documentation discusses Markdown files, embeddings, vector
search, indexing, and compaction housekeeping. Those concepts can explain the
platform, but generic examples that enable built-in search, pre-compaction
flush, session indexing, or a memory writer do not apply here. Do not copy
those examples into the managed config or runtime skills.

For research continuity, preserve the existing source-of-truth boundary:
research artifacts and receipts are durable records; MemPalace is optional
context only. Changes to that ownership require a separately reviewed runtime
design, not an agent instruction or compatibility fallback.

## Verification checklist

When reviewing a source change, confirm all of the following:

1. `memory.search.enabled` is explicitly `false`.
2. `agents.defaults.compaction.memoryFlush.enabled` is explicitly `false`.
3. `tools.deny` contains `memory_search` and `memory_get`.
4. The read-only MemPalace allowlist remains unchanged and contains no writer.
5. Research-store and artifact-receipt authority remains explicit.

Config changes belong in `gateway/openclaw_config/` and are later published only
through the guarded push workflow. Never hand-edit an installed OpenClaw
workspace or enable a live route while reviewing source.
