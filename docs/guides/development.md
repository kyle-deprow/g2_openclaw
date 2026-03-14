# Development Workflow Guide

Quick reference for testing, linting, and conventions across all G2 OpenClaw components.

## 1. Project Structure

The repo uses a flat layout — each component at the repo root:

| Directory | Language | Description |
|-----------|----------|-------------|
| `gateway/` | Python | PC Gateway — WebSocket server, transcription, AI routing |
| `g2_app/` | TypeScript | G2 App — thin client for iPhone / G2 glasses (Vite) |
| `infra/` | Python + Bicep | Infra CLI + Azure Bicep IaC modules |
| `infra/` | Bicep | Azure infrastructure-as-code modules |
| `tests/` | Python | Gateway unit and integration tests (pytest) |

Python packages are managed with **uv** (never pip or poetry). TypeScript packages use **npm**.

## 2. Running Tests

### Gateway (Python)

```bash
# Unit tests
uv run pytest tests/gateway/ -v

# Integration tests
uv run pytest tests/integration/ -v
```

### G2 App (TypeScript)

```bash
cd g2_app && npm test
```

## 3. Linting & Formatting

### Python — Ruff

```bash
uv run ruff check .     # lint
uv run ruff format .    # format
```

Ruff config in `pyproject.toml`: targets Python 3.13, line-length 100, rule sets `E, F, I, UP, B, SIM, RUF`.

### G2 App — TypeScript compiler

```bash
cd g2_app && npx tsc --noEmit
```

No separate linter; TypeScript strict mode is the enforced check.

## 4. Type Checking

```bash
# Python (mypy strict mode)
uv run mypy gateway/ infra/

# G2 App
cd g2_app && npm run typecheck
```

mypy is configured in `pyproject.toml` with `strict = true` targeting Python 3.13.

## 5. Pre-commit Hooks

```bash
# Install hooks (one-time)
uv run pre-commit install

# Run all hooks manually
uv run pre-commit run --all-files
```

Configured hooks: **ruff** (lint + format) and **mypy** (type checking).

## 6. Key Conventions

### Python

- **Ruff** for linting and formatting; **mypy strict** for type safety.
- Prefer frozen dataclasses and async/await patterns.
- All timestamps in UTC.
- No raw SQL with f-strings or `.format()`.
- Use `uv` exclusively — never `pip install` or `poetry`.

### TypeScript

- **Strict mode** enabled in both TS projects.
- **Vitest** for testing in G2 App.
- **Vite** for building the G2 App.
- G2 App has no runtime linter beyond `tsc --noEmit`.

### Protocol

[docs/design/protocol.md](../design/protocol.md) is the **single source of truth** for WebSocket frame types. Both `gateway/protocol.py` and `g2_app/src/protocol.ts` must stay in sync with it.

### Infrastructure

Bicep templates live in `infra/modules/`. Deploy via the Infra CLI:

```bash
uv run azure-infra-cli deploy --env dev
```

## 7. Environment Setup

### Gateway `.env`

Create `gateway/.env`:

```env
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8765
GATEWAY_TOKEN=your-secret-token
```

### Installing Dependencies

```bash
# Python (from repo root) — includes dev tools (ruff, pytest, mypy)
uv sync --extra dev

# G2 App
cd g2_app && npm install
```

---

See the [root README](../../README.md) for full architecture details and the [design docs](../design/) for component specifications.
