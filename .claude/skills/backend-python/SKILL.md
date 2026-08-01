---
name: backend-python
description: Python backend development with TDD, strict typing, and clean architecture for the G2 OpenClaw Gateway. Use when writing gateway modules, protocol frames, session management, asyncio WebSocket code, or pytest suites under tests/gateway/.
---

# Backend Python

Principles and patterns for building the G2 OpenClaw Gateway: test-driven development, strong typing, asyncio WebSockets, and pragmatic design.

**Canonical reference:** `.agents/skills/backend-python/SKILL.md` and `rules/*.md`. This file is the distilled operating summary — read the canonical file (and the specific rules/*.md files) before non-trivial work in this area.

## Core rules

- **TDD red-green-refactor**: write a failing test first, make it pass, then refactor. Test observable behavior, not internal wiring.
- **One logical concept per test**, structured Arrange → Act → Assert; tests must run independently in any order (no interdependence).
- **Use pytest fixtures over factories** for reusable setup.
- **Strict Pydantic at API boundaries**: every API request/response is a `BaseModel` with `model_config = ConfigDict(strict=True)`; add `frozen=True` on response models to prevent mutation.
- **No primitive obsession**: use `NewType` or domain types instead of bare `str`/`int` for IDs.
- **Never `Any`** — narrow with `Union`, `Protocol`, or generics. Use `X | None`, not `Optional[X]`; never default to mutable values.
- **All datetimes are timezone-aware UTC** (`datetime` with `tzinfo` set).
- **YAGNI**: don't build it until a test or requirement demands it. Tolerate duplication once; extract on the third occurrence.
- **Single responsibility** per module/class/function; depend on protocols, not concrete implementations (dependency inversion).
- **Fail fast**: validate inputs at boundaries; raise immediately on invalid state.
- **WebSocket protocol frames use `TypedDict` with `Literal` type discriminators.**
- **Session resolution, switching, and history replay are atomic operations.**
- **Structured concurrency for asyncio**: cancel tasks on disconnect.
- **OpenClaw timestamps may be milliseconds** — auto-detect with the `> 1e12` threshold.
- **Source layout**: code under `gateway/`, tests under `tests/gateway/` with `test_` prefix.
- **uv exclusively** — never pip/poetry/conda. `uv add`, `uv sync --extra dev`, `uv run pytest`, `uv run ruff check .`, `uv run mypy`. Never commit changes that require `pip install`.
- **No bloat**: remove dead code, legacy fallbacks, and unused files immediately.
- **Rule files** live at `.agents/skills/backend-python/rules/<prefix>-<name>.md` with incorrect/correct code examples; prefixes: `tdd-`, `typing-`, `design-`, `project-`, plus `alembic-` migration rules.

## This repo

- Python 3.13+, `mypy --strict`, ruff; run via `make test-gateway`, `make lint-python`, `make typecheck-python`, `make format-python` (or `uv run` equivalents).
- Gateway modules: `gateway/server.py`, `gateway/protocol.py`, `gateway/session_resolver.py`, `gateway/session_history.py`, `gateway/audio_buffer.py`, `gateway/transcriber.py`, `gateway/openclaw_client.py`, `gateway/config.py`, `gateway/cli.py`.
- Tests: `tests/gateway/` (fixtures in `tests/gateway/conftest.py`, `tests/gateway/autoresearch_fixtures.py`); integration tests in `tests/integration/`; shared fixtures/mocks in `tests/fixtures/` and `tests/mocks/`.
- Dependencies declared in `pyproject.toml`, locked in `uv.lock`.
