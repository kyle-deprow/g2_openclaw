---
name: openclaw-codex-config
description:
  OpenClaw configuration for OpenAI/Codex-backed agent turns. Use when installing OpenClaw locally, authenticating OpenAI/Codex, configuring the codex plugin, validating models status, pushing gateway/openclaw_config overlays, or debugging provider/runtime routing. Do not use for generic Azure/OpenRouter setup unless the user explicitly selected those providers.
---

# OpenClaw Codex Runtime Config

Use this skill for the current G2 OpenClaw agent path: OpenClaw receives G2
messages, then executes agent turns through the OpenAI provider and Codex
app-server runtime.

## Current Route

The supported default route is:

```text
G2 glasses -> iPhone WebView -> Python gateway -> OpenClaw gateway -> OpenAI provider -> Codex runtime
```

OpenClaw docs now treat `openai/*` model refs as the canonical route for
OpenAI/Codex subscription-backed agent turns. Runtime config can be omitted in
stock OpenClaw, but this repo pins `models.providers.openai.agentRuntime.id` to
`codex` so the route is explicit and auditable.

## Required Local Setup

```bash
node --version                 # must be >= 22
openclaw --version             # expected local install: 2026.6.11 or newer
openclaw plugins install @openclaw/codex
openclaw models auth login --provider openai
openclaw models list --provider openai
openclaw models status --plain
bash scripts/push-openclaw-config.sh
```

For headless machines, use:

```bash
openclaw models auth login --provider openai --device-code
```

## Repo Config Files

| File | Purpose |
|---|---|
| `gateway/openclaw_config/openclaw.json` | Repo-managed OpenClaw overlay with provider/runtime config |
| `gateway/openclaw_config/.env.example` | Local provider/model selection template |
| `gateway/agent_config/` | Repo-managed bootstrap files copied to every configured OpenClaw agent workspace, plus runtime skills copied into `~/.openclaw/skills/` |
| `scripts/push-openclaw-config.sh` | Idempotent merge/deploy script |

Do not edit `~/.openclaw/openclaw.json` directly. Edit repo files, then push:

```bash
bash scripts/push-openclaw-config.sh
systemctl --user restart openclaw-gateway.service
openclaw gateway health
openclaw models status --plain
```

## Provider Selection Rules

The default is Codex:

```bash
OPENCLAW_PROVIDER=codex
OPENAI_MODEL=gpt-5.4
```

Azure and OpenRouter are explicit alternatives only. The push script must fail
on unsupported selections instead of silently falling back to another provider
or model.

## Validation Checklist

1. `openclaw plugins list` shows the Codex plugin enabled.
2. `openclaw models list --provider openai` lists the selected model.
3. `openclaw models status --plain` reports a usable OpenAI/Codex route.
4. `openclaw gateway health` succeeds after restart.
5. The repo config contains no legacy external coding-agent provider or runtime refs.

## Gotchas

- OpenClaw manages its own OpenAI OAuth profile; it no longer imports auth from
  `~/.codex`.
- The repo uses Codex OAuth, not `OPENAI_API_KEY`. OpenClaw still labels these
  model refs as `openai/*` because that is the provider namespace for Codex
  app-server models.
- Agent-scoped Codex compaction can read the selected agent's local
  `openclaw-agent.sqlite` auth tables directly. After logging in on `main`,
  run `bash scripts/push-openclaw-config.sh`; the push script syncs the
  portable OpenClaw OAuth profile rows into every managed OpenAI/Codex agent
  store. Do not replace this with API-key fallback.
- Keep `agents.defaults.compaction.mode` at `default` for Codex. In OpenClaw
  2026.6.11, `safeguard` can send automatic CLI-budget compaction through the
  generic OpenAI API-key path after Codex declines non-manual native
  compaction. This repo uses Codex OAuth only, so `safeguard` causes false
  `No API key found for provider "openai"` failures.
- The gateway pre-start verifier at
  `scripts/ensure-openclaw-codex-runtime.mjs` is mandatory infrastructure. It
  patches only the known 2026.6.11 branch that turns Codex's intentional
  automatic-compaction deferral into a generic API-key fallback. It must fail
  closed when the package version or source shape changes; do not replace it
  with a provider fallback or an API key.
- Prefer canonical model refs like `openai/gpt-5.4` or `openai/gpt-5.5` in
  OpenClaw config. Legacy Codex-prefixed refs should be repaired with
  `openclaw doctor --fix`.
- Do not configure legacy external coding-agent runtimes. OpenClaw coding and
  research work goes through Codex subagents.
- If the Codex plugin command is unavailable after an OpenClaw upgrade, rerun
  `openclaw plugins install @openclaw/codex` and restart the gateway.
