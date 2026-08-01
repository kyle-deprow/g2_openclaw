---
name: openclaw-automation
description: OpenClaw automation via cron jobs, hooks, webhooks, and heartbeats. Use when scheduling recurring agent tasks, building event-driven hook handlers, configuring webhook endpoints or mapped hooks, setting up heartbeats, or debugging missed cron runs and webhook failures.
---

# OpenClaw Automation & Event System

Schedule tasks, react to lifecycle events, and integrate external systems through cron, hooks, webhooks, and heartbeats.

**Canonical reference:** `.agents/skills/openclaw-automation/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Three schedule formats:** `"at": "HH:MM"` (fixed daily time), `"every": "<duration>"` (interval monitoring), `"cron": "<expr>"` (complex schedules like weekdays-only).
- **Execution modes:** `isolated` (default; fresh `cron:<jobId>` session per run — standalone tasks/reports) vs `main` (shares `agent:<agentId>:main` context — use sparingly, it clutters conversation).
- **Delivery modes:** `announce` (post to the agent's default channel), `webhook` (POST result to URL), `none` (silent, transcript only). Frequent checks use `none` and instruct the agent to alert only on failures.
- **Model overrides on cron jobs** must use explicit repo-managed model refs — never ad hoc aliases.
- **Bind jobs to agents** (`"agent": "<id>"`) in multi-agent setups so the right specialist runs each schedule.
- **Cron management:** tools `cron_create`, `cron_list`, `cron_delete`; CLI `openclaw cron list|create|delete|run <id>` (manual trigger).
- **Hook structure:** a directory with `HOOK.md` (Description, Events, Priority, Enabled) plus `handler.ts`; handlers receive the event and a context API (e.g. `context.addBootstrapNote`).
- **Hook event categories:** command (`command:slash|new|reset|stop`), agent (`agent:start|end|error|bootstrap|compaction`), gateway (`gateway:start|stop|client_connect|client_disconnect`), message (`message:received|sending|sent`).
- **Bundled hooks:** `session-memory`, `bootstrap-extra-files`, `command-logger`, `boot-md` — check these before building custom.
- **Hook discovery order** (highest first): workspace `~/.openclaw/hooks/` → managed `~/.openclaw/managed/hooks/` → bundled; hooks can ship as npm packages.
- **Wrap hook handlers in try/catch** — a crashing hook kills the event pipeline.
- **Two webhook endpoints:** `POST /hooks/wake` (lightweight message queue) vs `POST /hooks/agent` (full agent run with tools, `wait: true` for the result).
- **Webhook session-key policies:** `unique` (`hook:<uuid>` per call, default), `provided` (sessionKey from request body), `mapped` (pre-configured `webhooks.mappedHooks` — stable session keys let related events share context, e.g. all deploys in one session).
- **Webhook auth is mandatory in production:** set `OPENCLAW_GATEWAY_TOKEN`; callers send `Authorization: Bearer <token>`.
- **Gmail Pub/Sub** is built in via `webhooks.gmail` (`topicName`, `labels`, `agent`, `prompt`).
- **Heartbeats** are periodic nudges running in the main session with full conversation context (`heartbeat.enabled`, `intervalMinutes`, `prompt`); best for proactive follow-ups. Keep intervals at 15-30 min minimum.
- **Heartbeat vs cron:** heartbeat = fixed interval, main session only; cron = any schedule, main or isolated. Use cron for independent scheduled tasks.
- **Monitor-and-alert pattern:** frequent cron with `delivery: none` + prompt-level escalation rules; never `announce` every 5 minutes.

## This repo

- **The load-bearing automation is a systemd user unit, not OpenClaw cron:** `quantipy-autoresearch-supervisor.service` (template in `gateway/openclaw_config/quantipy-autoresearch-supervisor.service.template`), 60s poll, `BindsTo=openclaw-gateway.service`; logic in `gateway/autoresearch_supervisor.py` and `gateway/autoresearch_systemd.py`.
- **The supervisor drives** autoresearch runs in `agent:autoresearch-pm:autoresearch:quantipy` (`gateway/autoresearch_runner.py`, `gateway/autoresearch_runs.py`) and the MemPalace finalizer (`gateway/mempalace_finalizer.py`).
- **Webhook/daemon endpoints:** OpenClaw Gateway daemon on port `18789`; the repo's G2 gateway WebSocket runs on port `8765`.
- **Config changes** (cron jobs, webhooks, heartbeat settings) are edited in `gateway/openclaw_config/openclaw.json` and deployed with `bash scripts/push-openclaw-config.sh` — never hand-edit `~/.openclaw/`.
- **Long-running task scripts:** `scripts/run-long-task.sh`, `scripts/run-long-task-worker.sh`.

## Repo policy overrides

- **No ad hoc model refs in scheduled jobs:** the canonical example (`"model": "openai/gpt-5-mini"`) is illustrative only. This deployment pins models per agent in `openclaw.json` (main gpt-5.4; autoresearch-pm/consensus_arbiter/reviewer gpt-5.6-sol; debater_data gpt-5.6-terra; debater_microstructure/debater_skeptic gpt-5.5; others gpt-5.4) on a single OpenAI/Codex OAuth provider — no alias-based model guidance applies.
- **Supervisor-driven automation supersedes in-agent scheduling:** autoresearch orchestration and the memory finalizer are driven by the external systemd supervisor, not agent-created cron jobs; do not replicate them as `cron_create` jobs.
- **Hooks that write memory are off-policy:** the `session-memory` bundled hook (transcript indexing into vector memory) conflicts with this repo's read-only MemPalace architecture (`memorySearch.enabled: false`, memory tools denied); leave it out of any hook plans.
