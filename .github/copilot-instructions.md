# G2 OpenClaw — Copilot Instructions

## Project Overview

G2 OpenClaw bridges Even Realities G2 AR smart glasses to a local OpenClaw AI assistant via a PC gateway. The system follows a thin-client model: the iPhone app acts as a transparent pipe between glasses (BLE) and a Python WebSocket gateway that handles transcription and AI inference — fully local, no cloud dependency.

## Tech Stack

- **Python 3.13+** — PC Gateway, Infra CLI (managed with **uv**, not pip or poetry)
- **TypeScript / Node.js 22+** — G2 App, Copilot Bridge (managed with **npm**)
- **Azure Bicep** — Infrastructure-as-code

## Project Layout

This is a polyglot monorepo with a **flat layout** — each component at the repo root:

```
gateway/           → PC Gateway (Python WebSocket server, Whisper, OpenClaw relay)
g2_app/            → G2 App (TypeScript thin client for iPhone / G2 glasses)
copilot_bridge/    → Copilot Bridge (TypeScript, GitHub Copilot SDK wrapper)
infra/             → Infra CLI (Python) + Azure Bicep infrastructure-as-code modules
tests/             → Python tests (pytest), mirrors gateway structure
docs/              → Design docs, guides, implementation plans, reference
```

Place new Python gateway modules under `gateway/`. Place new gateway tests under `tests/gateway/` with filenames prefixed `test_`.

## Commands Reference

```bash
# Python
uv sync --extra dev                    # install all deps
uv run pytest tests/gateway/ -v        # gateway unit tests
uv run pytest tests/integration/ -v    # integration tests
uv run ruff check .                    # lint
uv run ruff format .                   # format
uv run mypy gateway/ infra/             # type check
uv run pre-commit run --all-files      # run all pre-commit hooks

# G2 App
cd g2_app && npm install && npm test

# Copilot Bridge
cd copilot_bridge && npm install && npm test
```

## Things to Avoid

- Do not use `pip install` or `poetry`. Python packages use **uv** exclusively.
- Do not create raw SQL strings with f-strings or `.format()`.
- Do not store or compare timestamps in local time. Always use UTC.
- Do not add dependencies without also adding them to `pyproject.toml` (Python) or `package.json` (TypeScript).
- Do not put `defaultQuery` or other unsupported keys in OpenClaw provider config — the Zod schema rejects them. See the `openclaw-azure-config` skill.
- Do not commit raw API keys. Use `env:VAR_NAME` placeholders in repo config; the push script resolves them at deploy time.
