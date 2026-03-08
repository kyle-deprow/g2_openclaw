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
  server.py        → WebSocket server, session management, session menu handlers
  protocol.py      → Frame definitions (inbound/outbound, session menu types)
  session_resolver.py → OpenClaw session metadata resolution from local store
  session_history.py → Conversation history from OpenClaw transcript files
  audio_buffer.py  → PCM audio buffering for Whisper transcription
  openclaw_client.py → WebSocket client to OpenClaw daemon
  transcriber.py   → faster-whisper STT integration
  tts.py           → Text-to-speech (Piper)
  cli.py           → CLI commands (launch, stop, init-env, push-config)
  config.py        → Gateway configuration
g2_app/            → G2 App (TypeScript thin client for iPhone / G2 glasses)
  src/main.ts      → App bootstrap, boot-to-menu flow, frame routing
  src/state.ts     → State machine (loading → menu → idle → recording → ...)
  src/display.ts   → Display manager (transcript mode + menu mode)
  src/conversation.ts → Conversation history, formatReverse(), removeLastUser()
  src/input.ts     → Input handler (tap-to-toggle, double-tap menu, menu taps)
  src/gateway.ts   → WebSocket client, session list/switch/create helpers
  src/protocol.ts  → Protocol types including session menu frames
copilot_bridge/    → Copilot Bridge (TypeScript, GitHub Copilot SDK wrapper)
infra/             → Infra CLI (Python) + Azure Bicep infrastructure-as-code modules
tests/             → Python tests (pytest), mirrors gateway structure
docs/              → Design docs, guides, implementation plans, reference
```

Place new Python gateway modules under `gateway/`. Place new gateway tests under `tests/gateway/` with filenames prefixed `test_`.

## State Machine

The G2 App uses a strict state machine with validated transitions:

```
LOADING → MENU (boot default) → IDLE → RECORDING → TRANSCRIBING → CONFIRMING → THINKING → STREAMING → IDLE
                                                                                            ↕
                                                                                    ERROR / DISCONNECTED
```

- **Boot lands on MENU** (session picker), not idle
- **Double-tap in idle** opens the session menu
- **Single tap in idle** starts recording (tap-to-toggle)
- **MENU state** transitions to idle (on session selection), error, or disconnected

## Key Patterns

- **Reverse chronological display:** Newest messages at top (`formatReverse()` in `conversation.ts`). G2 has no scroll API, so this ensures the latest content is always visible.
- **Session menu:** `ListContainerProperty` on G2, backed by `session_list_request` → `session_list` → `session_switch`/`session_create` → `session_switched` protocol frames.
- **Rejected transcription removal:** `removeLastUser()` uses `splice()` to fully remove bad transcriptions from the conversation (no marking, no prefix).
- **Dual display modes:** `DisplayManager` switches between `transcript` mode (text container) and `menu` mode (list container). Error/disconnect force `exitMenuMode()` first.

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

```bash
# Sim Stack (kill + restart all services)
make sim                               # stop all, then launch gateway + vite + simulator
make restart                           # alias for sim
uv run python -m gateway launch --restart  # same as make sim, from CLI

# Individual controls
make launch                            # start gateway + vite + simulator
make stop                              # kill all running services
```

## Things to Avoid

- Do not use `pip install` or `poetry`. Python packages use **uv** exclusively.
- Do not create raw SQL strings with f-strings or `.format()`.
- Do not store or compare timestamps in local time. Always use UTC.
- Do not add dependencies without also adding them to `pyproject.toml` (Python) or `package.json` (TypeScript).
- Do not put `defaultQuery` or other unsupported keys in OpenClaw provider config — the Zod schema rejects them. See the `openclaw-azure-config` skill.
- Do not commit raw API keys. Use `env:VAR_NAME` placeholders in repo config; the push script resolves them at deploy time.
- DO NOT MAINTAIN BLOAT IN THE REPO, IF CODE, INFO, FILE, OR DATA IS NOT NEEDED, REMOVE IT. NO LEGACY FALLBACKS, BACKWARD COMPATIBILITY
