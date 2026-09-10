---
name: openclaw-codex-config
description: OpenClaw configuration for OpenAI/Codex-backed agent turns. Use when installing OpenClaw locally, authenticating OpenAI/Codex OAuth, configuring the codex plugin, validating models status, pushing gateway/openclaw_config overlays, or debugging provider/runtime routing failures.
---

# OpenClaw Codex Runtime Config

Configure and validate the G2 OpenClaw agent path: G2 messages execute agent turns through the OpenAI provider and the Codex app-server runtime.

**Canonical reference:** `.agents/skills/openclaw-codex-config/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **The route**: G2 glasses → iPhone WebView → Python gateway → OpenClaw gateway → OpenAI provider → Codex runtime. This repo pins `models.providers.openai.agentRuntime.id` to `codex` so the route is explicit and auditable.
- **Exact runtime tuple, no minimum-version checks**: OpenClaw `2026.8.1`, `@openclaw/codex` plugin `2026.8.1`, embedded `@openai/codex` `0.151.0`. Verify all three after consuming the reviewed local archive.
- **Local setup**: Node >= 22; `openclaw plugins install npm-pack:/work/incoming/openclaw-codex-2026.8.1.tgz --force --accept-capabilities`; `openclaw daemon install --force --port 18789 --json`; `openclaw models auth login --provider openai` (add `--device-code` for headless); then `bash scripts/push-openclaw-config.sh`.
- **OAuth only, no API key**: the repo uses Codex OAuth, not `OPENAI_API_KEY`. Model refs are still namespaced `openai/*` (that is the provider namespace for Codex app-server models). Never add API-key fallback.
- **Never edit `~/.openclaw/openclaw.json` directly** — edit repo files, then `bash scripts/push-openclaw-config.sh` → `systemctl --user restart openclaw-gateway.service` → `openclaw gateway health` → `openclaw models status --plain`.
- **Provider selection**: default `OPENCLAW_PROVIDER=codex`, `OPENAI_MODEL=gpt-5.4`. Azure/OpenRouter are explicit alternatives only; the push script must fail on unsupported selections, never silently fall back.
- **Validation checklist**: `openclaw plugins inspect codex --json` shows plugin `2026.8.1` enabled/loaded with embedded `0.151.0`; `openclaw models list --provider openai` lists the selected model; `openclaw models status --plain` shows a usable route; `openclaw gateway health` succeeds; no legacy external coding-agent provider/runtime refs in repo config.
- **OAuth profile sync**: OpenClaw manages its own OpenAI OAuth profile (no longer imports from `~/.codex`). After logging in on `main`, run the push script — it syncs the portable OAuth profile rows into every managed OpenAI/Codex agent store (`openclaw-agent.sqlite`).
- **Keep `agents.defaults.compaction.mode` at `default`**: OpenClaw 2026.8.1 and native Codex own automatic compaction. Do not add a compatibility shim, API-key fallback, or alternate runtime.
- **After every OpenClaw install/upgrade**: rerun `openclaw daemon install --force --port 18789 --json` — upgrades can leave `openclaw-gateway.service` pointing into the old global package. The command rewrites the unit only; it does not restart the service.
- **Model refs**: prefer canonical `openai/gpt-5.4` / `openai/gpt-5.5`; repair legacy Codex-prefixed refs with `openclaw doctor --fix`.
- **If Codex inspection fails after an upgrade**: rerun `bash scripts/bootstrap.sh`; do not fall back to another plugin, provider, or app-server binary.
- **Optional Azure preload**: `azure-api-version-preload.cjs` patches Azure OpenAI requests with `api-version` only when Azure is explicitly selected — keep it out of the default Codex path.

## This repo

- Config sources: `gateway/openclaw_config/openclaw.json` (provider/runtime overlay), `gateway/openclaw_config/.env.example` behavior via `make init-env`, `gateway/agent_config/` (bootstrap files + runtime skills copied to `~/.openclaw/`).
- Deploy: `scripts/push-openclaw-config.sh` (guarded, transactional, fail-closed) or `make push-config`; bootstrap via `scripts/bootstrap.sh`.
- Push-script guarding is tested by `tests/gateway/test_openclaw_guarding.py` and `tests/gateway/test_openclaw_script_guarding.py`.
- Systemd drop-ins: `openclaw-gateway-native-crash-hardening.conf`, `openclaw-gateway-runtime-caps.conf`.
- Model pins in this deployment: `main`=openai/gpt-5.4; `autoresearch-pm`/`consensus_arbiter`/`reviewer`=gpt-5.6-sol; `debater_data`=gpt-5.6-terra; `debater_microstructure`/`debater_skeptic`=gpt-5.5; all others gpt-5.4; all `thinkingDefault: "high"`.
