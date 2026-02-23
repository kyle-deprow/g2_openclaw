# OpenClaw Research

Comprehensive research on [OpenClaw](https://github.com/openclaw/openclaw) — the open-source personal AI assistant (🦞).

**Source:** [myclaw.ai](https://myclaw.ai/) (hosted) / [docs.openclaw.ai](https://docs.openclaw.ai) (docs) / [GitHub](https://github.com/openclaw/openclaw) (source)

**Research Date:** $(date +%Y-%m-%d)

---

## Files

| # | File | Topics |
|---|---|---|
| 01 | [Overview & Installation](01_overview_and_installation.md) | What is OpenClaw, features, installation methods (script/npm/source/Docker/Nix/hosted), system requirements, quick start, dev channels, env vars |
| 02 | [Architecture & Context](02_agent_architecture_and_context.md) | Gateway control plane, wire protocol, Pi agent runtime, agent loop (entry points → queueing → streaming → compaction), session model (keys, scopes, lifecycle, resets, DM modes, identity links), session pruning (cache-ttl, soft-trim, hard-clear), prompt assembly, steering |
| 03 | [Personas & Identity](03_agent_personas_and_identity.md) | Bootstrap files (AGENTS.md, SOUL.md, IDENTITY.md, USER.md, TOOLS.md, BOOTSTRAP.md), personality design, multi-agent personas, identity persistence |
| 04 | [Tools, Plugins & MCP](04_tools_plugins_and_mcp.md) | Built-in tools (30+), tool profiles & groups, allow/deny policies, browser (CDP), MCP server config (stdio/SSE/HTTP), plugin system (TypeScript via jiti), skills (SKILL.md, ClawHub, gating), subagent tool access |
| 05 | [Automation & Scheduling](05_automation_scheduling_and_webhooks.md) | Cron jobs (at/every/cron schedules, isolated/main execution, announce/webhook/none delivery), hooks (event-driven: command/agent/gateway/message events), webhooks (HTTP endpoints, mapped hooks, Gmail Pub/Sub), heartbeats |
| 06 | [Memory System](06_memory_system.md) | Markdown memory files, vector search (SQLite/QMD backends), embedding providers (local/OpenAI/Gemini/Voyage), hybrid search (BM25 + vector), MMR re-ranking, temporal decay, memory tools, pre-compaction flush, session memory indexing |
| 07 | [Multi-Agent & Subagents](07_multi_agent_and_subagents.md) | Agent config, session tools (list/history/send/spawn), subagent architecture (session keys, tool access, sandbox visibility), agent routing (channel bindings, commands, allowlists), patterns (delegation, supervisor, pipeline, collaborative) |
| 08 | [Configuration Reference](08_configuration_reference.md) | Full config.json structure, model/provider setup, session config, MCP config, memory config, cron config, webhook config, channel config (10 channels), gateway config, environment variables, directory structure |

---

## Quick Facts

- **License:** MIT
- **Language:** TypeScript (84.6%)
- **Runtime:** Node.js ≥ 22
- **Stars:** 217k+ GitHub
- **Contributors:** 723+
- **Created by:** Peter Steinberger (@steipete)
- **Gateway Port:** 18789 (WebSocket)
- **Supported Channels:** WhatsApp, Telegram, Slack, Discord, iMessage, Signal, Matrix, Google Chat, MS Teams, WebChat, CLI
- **Supported Providers:** Anthropic, OpenAI, Google, Groq, Ollama, custom OpenAI-compatible
