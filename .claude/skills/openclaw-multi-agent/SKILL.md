---
name: openclaw-multi-agent
description: OpenClaw multi-agent orchestration — agent design, subagent spawning, inter-agent communication, routing, and delegation patterns. Use when designing multi-agent architectures, configuring agent allowlists or sandbox visibility, implementing delegation/supervisor/pipeline patterns, or debugging inter-agent communication and routing.
---

# OpenClaw Multi-Agent Orchestration

Design and operate multi-agent systems: orthogonal specialists, controlled communication, and safe delegation.

**Canonical reference:** `.agents/skills/openclaw-multi-agent/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Orthogonal specialists:** each agent gets a distinct, non-overlapping role with a clear `description` — overlap confuses routing and duplicates work.
- **Model per role is repo-managed:** change model refs only through reviewed config updates; never substitute generic aliases in prompts or per-spawn overrides.
- **Per-agent identity:** each agent gets its own IDENTITY.md and SOUL.md overrides so agents feel distinct in conversation.
- **Defaults then override:** configure `agents.defaults` (model, thinking, `timeoutSeconds: 600`, tools) and override per agent.
- **Four session tools** (where enabled): `sessions_list`, `sessions_history` (non-blocking reads), `sessions_send` (ping-pong message, optional `waitForReply`, `REPLY_SKIP` flag for fire-and-forget), `sessions_spawn` (delegated run with `waitForComplete` / `announceResult`).
- **Send vs spawn:** `sessions_send` for quick questions or notifications to an existing agent session; `sessions_spawn` for independent delegated execution, parallel tasks, or when the result must return to the parent.
- **Send policy** (`session.sendPolicy` allow/deny of `a->b` pairs): hub-and-spoke — coordinator reaches specialists, specialists report only to the coordinator. Prevents circular loops.
- **Subagents get fewer tools than the parent, never more.** Configure `tools.subagents.tools` globally and override per spawn with task-specific allow/deny.
- **Sandbox visibility:** `own` (default — subagent sees only its own session), `all` (collaborative workflows only), `none` (no session tools). Default to `own`.
- **Subagent session keys nest:** `agent:<id>:subagent:<uuid>`. Avoid nesting deeper than 2 levels — use sequential spawns from the coordinator instead of chains.
- **`announceResult: true`** posts the subagent's result to the originating channel; use `false` for background checks or when the parent synthesizes multiple results first.
- **Routing:** bind channels to agents via per-agent `channels` config; users switch explicitly with `/agent <id>` and list with `/agents`.
- **Allowlists:** `allowSpawn` / `allowSend` per agent restrict who can spawn or message whom; specialists usually get `allowSpawn: []`.
- **`agents_list` tool** lets a coordinator discover peers for routing decisions (denied to some agents here — see overrides).
- **Patterns:** delegation (coordinator → specialist spawns), parallel fan-out (multiple simultaneous spawns, synthesize), supervisor (cron-driven `sessions_list` health checks), pipeline (chained sends/spawns), escalation (configured lower-cost agent escalates via send policy).
- **Anti-patterns:** overlapping roles, full-tool subagents, 3+ level spawn nesting, no send policy, ad hoc model aliases, `visibility: all` by default, direct specialist-to-specialist comms.

## This repo

- **Agents and models are pinned** in `gateway/openclaw_config/openclaw.json`: `main`=openai/gpt-5.4 and `research-orchestrator`=openai/gpt-6-astra. Native Luna is a bounded OpenAI/Codex child route; reviewer-only Opus is an explicit Claude Code ACP exception, not an OpenAI OAuth route.
- **Session topology:** G2 traffic → `agent:main:g2`; research-owner traffic uses `agent:research-orchestrator:autoresearch:quantipy-v2`.
- **Orchestration driver:** `gateway/research/` and `research-owner.service`; status and receipts remain in the bounded research store. The owner unit is bound to `openclaw-gateway.service`.
- **Config changes** go through `gateway/openclaw_config/` + `bash scripts/push-openclaw-config.sh`, never `~/.openclaw/` edits.

## Repo policy overrides

- **OpenClaw session tools are not the general research delegation mechanism here.** `main` denies all `sessions_*` tools; `research-orchestrator` has a bounded `sessions_spawn` exception for managed ACP review, while native Codex `spawn_agent` handles bounded Luna children. The canonical session-spawning playbook does not otherwise apply to the owner.
- **No per-spawn model or tool broadening:** models are pinned per agent in `openclaw.json` (single provider: OpenAI/Codex app-server via OAuth, no fallback, no aliases); the canonical example models/emojis are illustrative only.
- **Owner cadence is external, not cron-in-agent:** research health is driven by the research-owner unit, not by an agent running `sessions_list` on a cron job.
