# G2 OpenClaw Agent Instructions

## Project Overview

G2 OpenClaw bridges Even Realities G2 glasses to a local OpenClaw assistant through a PC gateway. The G2 app is a thin WebView client: the iPhone relays BLE display, input, and microphone data between the glasses and the Python gateway. The gateway owns transcription, session selection, OpenClaw communication, telemetry, and local process lifecycle.

The current agent path is Codex based. OpenClaw agent turns use the OpenAI provider with the Codex app-server runtime. Do not add alternate coding-agent integrations, compatibility shims, or silent provider fallbacks.

## Stack

- Python 3.13+ for `gateway/`, `infra/`, and tests, managed with `uv`.
- TypeScript and Node.js 22+ for `g2_app/`, managed with `npm`.
- The `g2_app` lockfile resolves EvenHub CLI `@evenrealities/evenhub-cli` 0.1.13 and SDK `@evenrealities/even_hub_sdk` 0.0.11. The simulator is installed globally; inspect its version before using version-specific features. Current audited upstream versions are SDK 0.0.12 and simulator 0.8.0.
- OpenClaw local gateway with OpenAI/Codex auth. Authenticate with `openclaw models auth login --provider openai`.
- Azure Bicep under `infra/` for infrastructure modules.

## Layout

- `gateway/` contains the Python WebSocket gateway, OpenClaw client, transcription, TTS, config, metrics, and CLI.
- `gateway/agent_config/` contains the OpenClaw PM persona, tools, bootstrap docs, and runtime skills deployed to `~/.openclaw/`.
- `gateway/openclaw_config/` contains the repo-managed OpenClaw config overlay and provider selection template.
- `g2_app/` contains the TypeScript G2 WebView app, protocol client, display manager, input handler, and state machine.
- `docs/` contains reference docs (`docs/reference/`) and the Quantipy autonomous research plan.
- `.agents/skills/` contains canonical repo skills. Codex discovers repo skills from this path.
- `.codex/agents/` contains Codex custom subagent definitions in TOML.
- `.claude/skills/` contains distilled Claude Code mirrors of the canonical repo skills; `.claude/agents/` mirrors `.codex/agents/`. `CLAUDE.md` imports this file. Keep mirrors in sync when either side changes.

## G2 Rules

- Boot directly into the single autoresearch thread view (idle); there is no session menu or multi-thread UX.
- Keep newest transcript messages at the top because G2 has no scroll API.
- Use container-based G2 UI only: text, list, and image containers on the 576 x 288 canvas. The locked SDK allows 1-12 total containers, at most 8 text/list and 4 image containers.
- Use `waitForEvenAppBridge()` and import G2 SDK types from `@evenrealities/even_hub_sdk`.
- Use the glasses microphone through SDK event handling; the gateway handles buffering and STT.
- Before changing G2 behavior, read the relevant `.agents/skills/g2-*/SKILL.md`, inspect the installed SDK declarations, and treat `docs/reference/g2-platform/` as versioned secondary evidence.
- The locked SDK 0.0.11 uses `borderRadius`, `shutDownPageContainer`, event values 0-8, and distinguishable temple/ring sources. Do not copy stale `borderRdaius`, `shutDownContaniner`, `ContainerData`, key-down/up, or `onMicData` guidance.
- Do not use SDK 0.0.12-only `zOrderIndex` until the dependency and `min_sdk_version` are deliberately migrated.
- Use `.agents/skills/g2-sim-automation/` for the local OpenClaw `/_dev` API and `.agents/skills/g2-simulator-automation/` for the official simulator 0.8.0 native control plane.

## OpenClaw Rules

- The default route is `openai/gpt-5.4` through OpenClaw's Codex runtime. Any Azure or OpenRouter use must be explicitly selected with `OPENCLAW_PROVIDER`; never silently retry through another provider.
- After changing `gateway/agent_config/` or `gateway/openclaw_config/`, run `bash scripts/push-openclaw-config.sh` and restart the OpenClaw gateway service.
- OpenClaw runtime skills live in `gateway/agent_config/skills/`; Codex repo skills live in `.agents/skills/`. Keep those roles separate.
- Target-repo implementation and review work should be delegated to Codex subagents from OpenClaw, then mechanically verified.

## Commands

```bash
uv sync
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy gateway tests

cd g2_app && npm install && npm test

make sim
make stop
make push-config
```

## Guardrails

- Do not use `pip`, Poetry, or Conda for this repo.
- Do not add dependencies without updating the appropriate lockfile and docs.
- Do not commit secrets or local OpenClaw auth material.
- Do not retain obsolete files, fallbacks, or legacy integration paths once a replacement is in place.
- Treat `/home/dev/repos/quantipy` as a separate worktree; preserve unrelated dirty changes there.
