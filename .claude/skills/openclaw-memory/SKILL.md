---
name: openclaw-memory
description: OpenClaw memory system — Markdown-based storage, vector/hybrid search, embedding providers, and retrieval tuning. Use when configuring memory backends, tuning BM25/vector/MMR/temporal-decay retrieval, designing memory file structure, or debugging memory search quality — noting this repo's MemPalace-only, read-only memory policy.
---

# OpenClaw Memory System

Durable knowledge in plain Markdown with derived vector indexes — heavily overridden in this repo by the MemPalace read-only architecture.

**Canonical reference:** `.agents/skills/openclaw-memory/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Three file types:** `MEMORY.md` (critical long-term facts, always in bootstrap), `memory/YYYY-MM-DD.md` (daily episodic notes), `USER.md` (user-specific knowledge).
- **MEMORY.md is bootstrap-loaded every session** — keep it to durable, high-impact facts; ephemeral details belong in daily files.
- **Structure daily files** with consistent sections (Conversations, Decisions, Tasks, Learnings) — unstructured dumps are hard to search.
- **Markdown is the source of truth:** the vector index is derived from files, never the reverse. If the index corrupts, re-index from Markdown.
- **Memory survives everything:** session resets, compaction, Gateway restarts. Only explicit file deletion removes it; memory dirs are per-agent.
- **Config location (v2026.3.2+):** memory search config goes under `agents.defaults.memorySearch`, NOT a top-level `memory` key. Top-level `memory` only accepts `backend` ("builtin"|"qmd"), `citations`, and `qmd`.
- **Backend choice:** `sqlite`/builtin for most deployments (<1000 files); `qmd` for large stores. `sqliteVec: true` only pays off at 500+ files.
- **Chunking defaults:** `chunkSize`/`tokens` 512, overlap 50; use 1024/100 for long research documents.
- **Enable vector search at scale** (50+ files, frequent recall); skip it under ~20 files where direct file reads suffice.
- **Embedding providers:** `local` (default — fast, free, private), `openai` (e.g. `text-embedding-3-large`), `gemini`, `voyage`. Start local; go cloud only for domain jargon, multi-language, or semantic nuance.
- **Embedding cache:** `~/.openclaw/cache/embeddings/`, keyed by content hash, auto-invalidated on model change; no manual management.
- **Hybrid weight** (`hybridWeight`/`vectorWeight`, default 0.7): 0.0 = pure BM25 keyword, 1.0 = pure vector. Raise if concepts are missed, lower if exact keywords are missed.
- **`topK` 20 default** (candidates before re-ranking); increase to 50 for very large stores or broad queries.
- **MMR re-ranking** (`lambda` 0.7 default, `fetchMultiplier` 3) balances relevance vs diversity; lower lambda toward 0.5 if results are redundant.
- **Temporal decay** (`halfLifeDays` 30, `weight` 0.2): enable for assistants where recency matters; disable for reference knowledge bases.
- **Memory tools (generic OpenClaw):** `memory_search` (semantic recall) and `memory_get` (fetch a file by path) — both denied in this repo, see overrides.
- **Search-before-write hygiene** (generic): check for existing entries before writing to avoid index bloat — moot here because agents cannot write memory at all.

## This repo

- **MemPalace read-only MCP:** all agents get 19 `mempalace-readonly__*` tools served by `gateway/mempalace_readonly_server.py`; that is the only agent-facing memory access.
- **The only memory writer** is the non-model finalizer `gateway/mempalace_finalizer.py` / `gateway/mempalace_finalizer_script.py`, driven by the autoresearch supervisor (`gateway/autoresearch_supervisor.py`).
- **Policy lives in** `gateway/openclaw_config/openclaw.json`: `tools.deny` includes `memory_search` and `memory_get`; `agents.defaults.memorySearch.enabled: false`; `compaction.memoryFlush.enabled: false` — all deliberate.
- **Health/install:** `make mempalace-install` (idempotent MCP server install) and `make mempalace-health` (`scripts/check-mempalace-health.py` validates embedding/index invariants).
- **Config changes** go through `gateway/openclaw_config/` + `bash scripts/push-openclaw-config.sh`; never hand-edit `~/.openclaw/`.

## Repo policy overrides

- **Do NOT enable `memory_search`/`memory_get`:** globally denied in `tools.deny` by design. The canonical "two tools for memory access" workflow does not apply; agents read memory only via `mempalace-readonly__*` MCP tools.
- **Do NOT enable `agents.defaults.memorySearch`:** deliberately `enabled: false`. The canonical vector-search tuning knobs (hybrid weight, MMR, temporal decay, embeddings) are reference material, not levers to flip on here.
- **Do NOT enable `compaction.memoryFlush`:** deliberately `enabled: false`. The canonical rule "always enable the pre-compaction flush" is inverted here — models never write memory; the MemPalace finalizer is the sole writer.
- **"Search before write" and memory-hygiene prompts are inapplicable:** agents have no memory write path; write-path changes mean modifying the finalizer, not agent instructions.
