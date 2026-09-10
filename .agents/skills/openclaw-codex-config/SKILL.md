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
openclaw --version             # expected local install: exactly 2026.8.1
openclaw plugins install npm-pack:/work/incoming/openclaw-codex-2026.8.1.tgz --force --accept-capabilities
openclaw plugins inspect codex --runtime --json  # loaded runtime proof: plugin 2026.8.1, @openai/codex 0.151.0
openclaw models auth login --provider openai
openclaw models list --provider openai
openclaw models status --plain
bash scripts/push-openclaw-config.sh
openclaw daemon install --force --port 18789 --json  # final start gate: writes, enables, and restarts the service
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
openclaw daemon install --force --port 18789 --json
openclaw gateway health
openclaw models status --plain
```

`openclaw daemon install --force` is the final explicit start gate on 2026.8.1:
it writes the unit, enables it, and restarts the service. Do not use
it as a preparation step or describe it as a no-start unit rewrite.

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

1. `openclaw plugins inspect codex --runtime --json` reports the loaded plugin
   `2026.8.1`, enabled
   and loaded, with embedded `@openai/codex` `0.151.0`.
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
- Astra (`openai/gpt-6-astra`) and Luna (`openai/gpt-5.6-luna`) are native
  OpenAI/Codex routes. Reviewer-only Opus is an explicit Claude Code ACP
  exception, not an OpenAI OAuth model; do not add it to the OpenAI provider
  catalog or model policy.
- Keep `agents.defaults.compaction.mode` at `default` for Codex. OpenClaw
  2026.8.1 and its native Codex runtime own automatic compaction; do not add a
  compatibility shim or generic OpenAI API-key fallback. This repo uses Codex
  OAuth only.
- Treat core, plugin, and embedded app-server versions as one exact runtime
  tuple: OpenClaw `2026.8.1`, `@openclaw/codex` `2026.8.1`, and
  `@openai/codex` `0.151.0`. Do not use minimum-version checks, registry
  installs, or unpinned fallbacks. The migration command consumes the reviewed
  local archive with `--accept-capabilities` and then verifies all three
  versions. The plugin package has no `bin`; the embedded `@openai/codex`
  package owns `bin/codex.js`.
- After every OpenClaw install or upgrade, use
  `openclaw daemon install --force --port 18789 --json` only as the final
  explicit start gate. It writes and enables the unit, then restarts
  the service so it points at the current package.
- Prefer canonical model refs like `openai/gpt-5.4` or `openai/gpt-5.5` in
  OpenClaw config. OpenClaw coding and research worker routes use native Codex
  subagents; the reviewer-only Opus route is the explicit Claude Code ACP
  exception described above.
- If Codex inspection fails after an upgrade, rerun `bash scripts/bootstrap.sh`;
  do not fall back to another plugin, provider, or app-server binary.
