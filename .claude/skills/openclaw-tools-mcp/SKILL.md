---
name: openclaw-tools-mcp
description: OpenClaw tool system, MCP server integration, plugin development, and skills authoring. Use when configuring tool profiles or allow/deny policies, connecting MCP servers, writing SKILL.md manifests or TypeScript plugins, or debugging tool execution failures.
---

# OpenClaw Tools, MCP & Plugins

Configure and extend the OpenClaw tool ecosystem: built-in tools, MCP servers, TypeScript plugins, and skills.

**Canonical reference:** `.agents/skills/openclaw-tools-mcp/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Tool profiles** set the base tool set: `minimal` (basic chat), `coding` (file system, exec, process, apply_patch), `messaging` (message + sessions tools), `full` (everything, default). Start restrictive, expand as needed.
- **Allow/deny evaluation order:** profile determines the base set, then `allow` narrows to only listed tools (if specified), then `deny` removes tools from what remains.
- **Least privilege:** extra tools cost system-prompt tokens, widen the attack surface, and add decision complexity. Grant only what the agent needs.
- **Tool groups** (`tools.groups`) toggle categories: `filesystem`, `exec`, `browser`, `canvas`, `messaging`, `cron`, `web`, `memory`, `sessions`. Use groups for broad toggles, allow/deny for individual tools.
- **Provider policy** (`tools.providerPolicy`) can restrict tools per LLM provider that handles them poorly.
- **MCP transports:** stdio via `command` + `args` (npm packages, local scripts); SSE via `url` ending in `/sse`; Streamable HTTP via `url` + `transport: "streamable-http"`.
- **Never hardcode secrets** in MCP config — use `env:VAR_NAME` references, resolved at runtime from the Gateway's environment.
- **MCP tool naming:** tools are exposed as `<serverName>_<tool>` (double-underscore separator in this deployment's allowlists, e.g. `mempalace-readonly__*`). Keep server names short to save tokens.
- **MCP tools obey the same allow/deny policies** as built-in tools; deny dangerous individual MCP tools explicitly.
- **Disable, don't delete:** set `"disabled": true` on an MCP server entry to temporarily turn it off without losing its config.
- **Skills can bundle MCP servers** — the servers start when the skill activates and stop when it deactivates.
- **Plugins** are TypeScript modules loaded by jiti; extension points: `tools`, `services`, `channels`, `hooks`, `providers`, `rpcMethods`, `cliCommands`. Define tool params with full JSON Schema.
- **Plugin/skill discovery precedence** (highest first): workspace `~/.openclaw/plugins|skills/` → managed `~/.openclaw/managed/plugins|skills/` → bundled. Same-name workspace entries override managed/bundled.
- **Plugin errors must not crash the Gateway** — wrap plugin code in try/catch and log instead of re-throwing.
- **SKILL.md manifests** declare Description, Instructions, Tools, MCP Servers, Environment, and Gating; gating (required tools, platforms, feature flags) makes unmet skills silently not load.
- **Tool execution pipeline:** queue → `before_tool_call` hook → execute with timeout → `after_tool_call` hook → `tool_result_persist` hook → streamed `toolResult`. Hooks are where you add auditing, validation, and result filtering.
- **Filter large tool outputs** via `tool_result_persist` so they don't bloat transcripts.
- **Browser tool** launches local Chromium via CDP by default (`headless`, `viewport`, `timeout: 30000`); `BROWSER_CDP_URL` connects to an existing Chrome. Restrict with `allowedDomains`/`blockedDomains`; keep it disabled by default and enable per-agent.

## This repo

- **Config source of truth:** `gateway/openclaw_config/openclaw.json` (tool profiles, allow/deny, MCP servers) and `gateway/agent_config/` (AGENTS.md, SOUL.md, TOOLS.md, BOOTSTRAP.md, skills). Deploy with `bash scripts/push-openclaw-config.sh` (or `make push-config`) — never hand-edit `~/.openclaw/` files.
- **Repo MCP servers:** `gateway/g2_control_mcp_server.py` (G2 control tools) and `gateway/mempalace_readonly_server.py` (read-only MemPalace).
- **`main` agent policy:** tools.profile `minimal` with an exact allowlist — 3 `g2-control__*` tools + 19 `mempalace-readonly__*` tools — and denies `exec` and all `sessions_*` tools.
- **Global denies:** `memory_search` and `memory_get` are in `tools.deny` in `gateway/openclaw_config/openclaw.json` (deliberate; see openclaw-memory skill).
- **MemPalace install/health:** `make mempalace-install`, `make mempalace-health` (`scripts/check-mempalace-health.py`).
- **Pinned runtime:** OpenClaw `2026.7.1-2`, `@openclaw/codex` plugin `2026.7.1-1`, embedded `@openai/codex` `0.144.3` (see `scripts/ensure-openclaw-codex-runtime.mjs`).

## Repo policy overrides

- **Do not "expand the profile as needed" for `main`:** it deliberately runs `minimal` with an exact allowlist (3 `g2-control__*` + 19 `mempalace-readonly__*`) and denies `exec` and `sessions_*`. Broadening it is a reviewed config change, not a debugging step.
- **`providerPolicy` per-provider examples do not apply:** this deployment uses the OpenAI/Codex app-server via OAuth only — no Anthropic/Google providers, no provider fallback.
- **The `memory` tool group / memory tools stay off:** `memory_search`/`memory_get` are globally denied by design; never re-enable them to "fix" tool access.
- **Workspace-tier editing (`~/.openclaw/`) is prohibited:** all changes go through `gateway/openclaw_config/` or `gateway/agent_config/` plus `scripts/push-openclaw-config.sh` (guarded, transactional, fail-closed).
