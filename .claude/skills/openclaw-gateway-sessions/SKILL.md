---
name: openclaw-gateway-sessions
description: OpenClaw Gateway architecture, session lifecycle, context management, and pruning/compaction. Use when configuring the Gateway daemon, designing session keys or DM scoping, tuning context pruning or compaction, or debugging session resets, stale context, or WebSocket wire-protocol issues.
---

# OpenClaw Gateway & Sessions

Keep the Gateway control plane predictable, session keys deliberate, and context within token budget.

**Canonical reference:** `.agents/skills/openclaw-gateway-sessions/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Single daemon:** the Gateway is one long-lived process owning all channel connections. Never run two Gateways against the same `~/.openclaw` directory — session state corrupts.
- **Always authenticate:** set `OPENCLAW_GATEWAY_TOKEN` in production; without it the WebSocket API and webhook endpoints are open.
- **Client roles:** `client` (interactive UI), `node` (device commands), `agent` (internal Pi runtime), `webhook` (inbound HTTP). Never expose `agent` or `node` roles to untrusted networks.
- **Restrict CORS** to known origins in production (default is localhost-only); never `"*"`.
- **Session key anatomy:** `agent:<agentId>:main` (main DM), `agent:<agentId>:dm:<peerId>` (per-peer), `agent:<agentId>:<channel>:group:<id>` (group), `cron:<jobId>`, `hook:<uuid>`, `agent:<agentId>:subagent:<uuid>`. Never construct keys manually in application code — use SDK helpers.
- **DM scope options:** `main` (single user, default), `per-peer`, `per-channel-peer`, `per-account-channel-peer`. Wrong scope for a single user fragments context.
- **Identity links** (`session.identityLinks`) unify one person across channels so they share a DM session under per-peer scoping.
- **Session resets:** default daily reset at 4 AM local (`dailyResetHour: 4`); tune per type (`resetByType`) and per channel (`resetByChannel`) — idle resets suit groups/threads.
- **Session storage:** `~/.openclaw/agents/<agentId>/sessions/` — `sessions.json` maps key → session ID; `<sessionId>.jsonl` append-only transcripts are the source of truth. Back them up.
- **Prompt assembly order:** base prompt → skills list → bootstrap files (AGENTS.md → SOUL.md → IDENTITY.md → USER.md → TOOLS.md → BOOTSTRAP.md) → per-run overrides. Keep combined bootstrap under 2,000 tokens.
- **Context budget:** effective context = model limit − `compaction.reserveTokensFloor` (default 20,000) − system prompt tokens.
- **Queue modes:** `steer` injects inbound messages into the current run (checked after each tool call; remaining tool calls of the turn are skipped) — for interactive use. `collect` for batch/cron work.
- **Pruning modes:** `off` (default) or `cache-ttl` (e.g. `ttl: "5m"`, `keepLastAssistants: 3`). Only `toolResult` messages are pruned — user/assistant messages never are; the last N assistant turns are protected; image-bearing tool results are skipped.
- **Soft vs hard pruning:** soft-trim at `softTrimRatio` 0.3 keeps head + tail with a `...` separator; hard-clear at `hardClearRatio` 0.5 replaces the whole tool result with a placeholder.
- **Manual compaction:** `/compact` (optional instructions) when a session feels sluggish or repetitive.
- **Wire protocol:** first WebSocket frame must be `connect` with role + auth token; RPC is `req`/`res` with unique client-generated `id`s; side-effecting methods (`send`, `agent`) require idempotency keys; events carry `seq` for missed-event detection.

## This repo

- **Gateway daemon port `18789`; G2 gateway WebSocket port `8765`** (repo gateway server in `gateway/server.py`, protocol in `gateway/protocol.py`).
- **Session keys in use:** G2 traffic → `agent:main:g2`; autoresearch runs only in `agent:autoresearch-pm:autoresearch:quantipy`. Resolution helpers: `gateway/session_resolver.py`, history in `gateway/session_history.py`.
- **Config deployment:** edit `gateway/openclaw_config/openclaw.json` or `gateway/agent_config/`, then run `bash scripts/push-openclaw-config.sh` (guarded, transactional, fail-closed). Never hand-edit `~/.openclaw/` files.
- **Bootstrap files** live in `gateway/agent_config/` (AGENTS.md, SOUL.md, TOOLS.md, BOOTSTRAP.md).
- **Lifecycle via Makefile:** `make launch` (foreground gateway), `make sim` (full stack + OTel), `make sim-lite`, `make stop`.
- **Supervisor coupling:** `quantipy-autoresearch-supervisor.service` (systemd user unit, template in `gateway/openclaw_config/`) has `BindsTo=openclaw-gateway.service` — gateway restarts take the supervisor down with it.

## Repo policy overrides

- **Do NOT enable `compaction.memoryFlush`:** the canonical skill says to always enable the pre-compaction memory flush; this deployment deliberately sets `compaction.memoryFlush.enabled: false`. Memory persistence is handled exclusively by the non-model MemPalace finalizer (`gateway/mempalace_finalizer.py`), not by an agent flush turn.
- **`cache-ttl` pruning guidance is Anthropic-oriented:** this deployment's provider is OpenAI/Codex app-server via OAuth only, so the "prune when Anthropic cache is stale" rationale does not transfer directly — treat pruning changes as reviewed config edits against `gateway/openclaw_config/openclaw.json`.
- **No manual `~/.openclaw` edits:** the canonical skill discusses workspace files directly; here every change flows through `scripts/push-openclaw-config.sh`.
