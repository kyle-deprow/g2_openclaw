---
name: openclaw-persona-identity
description: OpenClaw agent persona design via the bootstrap Markdown files SOUL.md, IDENTITY.md, AGENTS.md, USER.md, TOOLS.md, and BOOTSTRAP.md. Use when creating or refining agent personalities, writing behavioral rules, configuring per-agent personas, or debugging personality drift across sessions.
---

# OpenClaw Persona & Identity Design

Craft agent personalities through six bootstrap Markdown files that define who the agent is, how it behaves, what it knows about its human, and how character persists across sessions.

**Canonical reference:** `.agents/skills/openclaw-persona-identity/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Fixed load order** into the system prompt: 1. `AGENTS.md` (operational rules), 2. `SOUL.md` (deep personality), 3. `IDENTITY.md` (name/creature/vibe), 4. `USER.md` (learned user knowledge), 5. `TOOLS.md` (tool preferences), 6. `BOOTSTRAP.md` (custom extras). Later files may reference earlier ones, never the reverse.
- **Bootstrap files live on disk, not in session state** — session resets clear conversation history but never touch bootstrap files. This is the foundation of personality persistence.
- **Token budget** (canonical guide): AGENTS.md 400–600, SOUL.md 300–500, IDENTITY.md 50–100, USER.md 200–400, TOOLS.md 100–200, BOOTSTRAP.md 200–400 — total ~1,500 tokens; over 2,000 combined costs conversational depth. Separately, OpenClaw enforces a 20,000-character hard truncation limit per file (see `openclaw-improvement`).
- **Per-agent overrides**: `~/.openclaw/agents/<name>/` bootstrap files override workspace defaults; only override the files that differ.
- **SOUL.md opening line sets the frame** — the default "You're not a chatbot. You're becoming someone." tells the model to develop character, not just answer.
- **Four SOUL pillars**: Core Truths (values), Boundaries (what it won't do — prevents persona collapse under adversarial prompting), Vibe (specific texture, not "friendly"), Continuity (relation to its own history).
- **Show, don't tell**: write behavioral instructions ("if a task is ambiguous, ask one clarifying question"), never trait declarations ("you are creative") — models ignore the latter.
- **Explicit anti-sycophancy rules**: no "Great question!"/"Absolutely!" openers, disagree with reasoning, praise only when earned, "I don't know" is valid.
- **Keep SOUL.md evocative, 300–500 tokens** — one well-chosen metaphor beats ten bullets.
- **IDENTITY.md is a five-field card**: Name, Creature, Vibe, Emoji, Avatar. Names short (1–2 syllables) and distinctive; same emoji everywhere (chat headers, group mentions, cron announcements).
- **AGENTS.md holds structure**: first-run ritual (introduce, explain skills, ask about the user, seed USER.md), session read order, and non-negotiable safety rules (safety belongs in AGENTS.md, not SOUL.md).
- **Group chat needs explicit rules** or the agent dominates: be concise, don't respond to every message, use threading, address people by name.
- **USER.md is agent-maintained**, seeded by the human with blank structure only; it holds durable facts ("prefers uv over pip"), never ephemera ("seemed tired today"). It survives resets, compaction, and migrations.
- **Start minimal and grow organically**: Day 1 IDENTITY + SOUL; add AGENTS.md after real interactions; TOOLS.md/BOOTSTRAP.md later. Over-specifying day one bloats the prompt.
- **Anti-patterns**: 1000+ token SOUL.md, trait declarations, human-edited USER.md, ephemeral facts in USER.md, one shared SOUL for all agents, missing first-run ritual or group-chat rules.

## This repo

- Deployed bootstrap sources live in `gateway/agent_config/` (`AGENTS.md`, `SOUL.md`, `TOOLS.md`, `BOOTSTRAP.md`, `skills/`); edit there, then deploy with `bash scripts/push-openclaw-config.sh` — never hand-edit `~/.openclaw/`.
- Deep knowledge goes in `gateway/agent_config/skills/<name>/SKILL.md` (loaded on demand), keeping bootstrap files thin — see the `openclaw-improvement` skill for size discipline and the 20,000-char truncation limit.
- Bootstrap doc invariants are tested in `tests/gateway/test_agent_config_docs.py`.

## Repo policy overrides

- **No `memory_search`/`memory_get`**: the canonical skill says the agent can "always `memory_search` for details" and instructs agents to write `memory/YYYY-MM-DD.md`, USER.md, and MEMORY.md. In this deployment those tools are globally denied, `agents.defaults.memorySearch.enabled: false` and `compaction.memoryFlush.enabled: false` are set deliberately, and models never write memory. The only memory writer is the non-model finalizer `gateway/mempalace_finalizer.py` (driven by the autoresearch supervisor); agents get read-only MemPalace MCP retrieval (`mempalace_status`, `mempalace_diary_read`, `mempalace_search`, `mempalace_kg_query`). Do not add memory-writing instructions to AGENTS.md/SOUL.md here.
