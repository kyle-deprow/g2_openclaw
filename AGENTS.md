# G2 OpenClaw Codex Instructions

## Project Overview

G2 OpenClaw bridges Even Realities G2 glasses to a local OpenClaw assistant through a PC gateway. The G2 app is a thin WebView client: the iPhone relays BLE display, input, and microphone data between the glasses and the Python gateway. The gateway owns transcription, session selection, OpenClaw communication, telemetry, and local process lifecycle.

The current agent path is Codex based. OpenClaw agent turns use the OpenAI provider with the Codex app-server runtime. Do not add alternate coding-agent integrations, compatibility shims, or silent provider fallbacks.

## Stack

- Python 3.13+ for `gateway/`, `infra/`, and tests, managed with `uv`.
- TypeScript and Node.js 22+ for `g2_app/`, managed with `npm`.
- EvenHub CLI `@evenrealities/evenhub-cli` 0.1.13, SDK `@evenrealities/even_hub_sdk` 0.0.11, and simulator 0.7.3.
- OpenClaw local gateway with OpenAI/Codex auth. Authenticate with `openclaw models auth login --provider openai`.
- Azure Bicep under `infra/` for infrastructure modules.

## Layout

- `gateway/` contains the Python WebSocket gateway, OpenClaw client, transcription, TTS, config, metrics, and CLI.
- `gateway/agent_config/` contains the OpenClaw PM persona, tools, bootstrap docs, and runtime skills deployed to `~/.openclaw/`.
- `gateway/openclaw_config/` contains the repo-managed OpenClaw config overlay and provider selection template.
- `g2_app/` contains the TypeScript G2 WebView app, protocol client, display manager, input handler, and state machine.
- `docs/` contains design notes, reference docs, and the Quantipy autonomous research plan.
- `.agents/skills/` contains Codex repo skills. Codex discovers repo skills from this path.
- `.codex/agents/` contains Codex custom subagent definitions in TOML.

## G2 Rules

- Boot into the session menu, not idle.
- Keep newest transcript messages at the top because G2 has no scroll API.
- Use container-based G2 UI only: text, list, and image containers on the 576 x 288 canvas.
- Use `waitForEvenAppBridge()` and import G2 SDK types from `@evenrealities/even_hub_sdk`.
- Use the glasses microphone through SDK event handling; the gateway handles buffering and STT.

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
