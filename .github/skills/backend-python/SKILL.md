---
name: backend-python
description:
  Python backend development with TDD, typed dataclasses, and clean architecture principles. Use when building gateway modules, protocol definitions, session management, audio processing, or writing tests for the G2 OpenClaw Gateway. Triggers on tasks involving Python modules, pytest, TypedDict schemas, asyncio WebSockets, or gateway service layers.
---

# Backend Python

Principles and patterns for building the G2 OpenClaw Gateway. Test-driven
development, strong typing, asyncio WebSockets, and pragmatic design.

## When to Apply

Reference these guidelines when:

- Writing or modifying Python modules under `gateway/`
- Creating or updating tests under `tests/gateway/`
- Designing TypedDict or dataclass schemas for protocol frames
- Adding new gateway service modules (audio, transcription, session management)
- Working with asyncio WebSocket server or client code
- Reviewing code for type safety, test coverage, or design smell

## Rule Categories by Priority

| Priority | Category             | Impact   | Prefix      |
| -------- | -------------------- | -------- | ----------- |
| 1        | Test-Driven Dev      | CRITICAL | `tdd-`      |
| 2        | Type Safety          | CRITICAL | `typing-`   |
| 3        | Design Principles    | HIGH     | `design-`   |
| 4        | Gateway Patterns     | HIGH     | `gateway-`  |
| 5        | Project Conventions  | MEDIUM   | `project-`  |

## Quick Reference

### 1. Test-Driven Development (CRITICAL)

- `tdd-red-green-refactor` - Write a failing test first, make it pass, then refactor
- `tdd-test-behavior-not-implementation` - Test observable outcomes, not internal wiring
- `tdd-one-assertion-per-concept` - Each test verifies one logical concept
- `tdd-arrange-act-assert` - Structure every test as Arrange → Act → Assert
- `tdd-no-test-interdependence` - Tests must run independently in any order
- `tdd-fixtures-over-factories` - Use pytest fixtures for reusable test setup

### 2. Type Safety & Pydantic (CRITICAL)

- `typing-strict-models` - All API boundaries use Pydantic BaseModel with strict config
- `typing-no-primitive-obsession` - Use NewType or domain types instead of bare str/int for IDs
- `typing-no-any` - Never use Any; narrow with Union, Protocol, or generics
- `typing-datetime-utc` - All datetimes are timezone-aware UTC via `datetime.datetime` with `tzinfo`
- `typing-optional-explicit` - Use `X | None` instead of `Optional[X]`; never default mutable

### 3. Design Principles (HIGH)

- `design-yagni` - Don't build it until a test or requirement demands it
- `design-dry-extract-on-third` - Tolerate duplication once; extract on the third occurrence
- `design-single-responsibility` - Each module/class/function does one thing
- `design-dependency-inversion` - Depend on protocols, not concrete implementations
- `design-fail-fast` - Validate inputs at boundaries; raise immediately on invalid state

### 4. Gateway Patterns (HIGH)

- `gateway-protocol-frames` - All WebSocket frames use TypedDict with Literal type discriminators
- `gateway-session-lifecycle` - Session resolution, switching, and history replay are atomic operations
- `gateway-asyncio-tasks` - Use structured concurrency; cancel tasks on disconnect
- `gateway-timestamp-utc` - OpenClaw timestamps may be in milliseconds; auto-detect with `> 1e12` threshold

### 5. Project Conventions (MEDIUM)

- `project-src-layout` - All source under `gateway/`, tests under `tests/gateway/` with `test_` prefix
- `project-uv-only` - Use `uv` for all dependency management; never pip/poetry
- `project-no-bloat` - Remove dead code, legacy fallbacks, and unused files immediately

## How to Use

Read individual rule files for detailed explanations and code examples:

```
rules/tdd-red-green-refactor.md
rules/gateway-protocol-frames.md
```

Each rule file contains:

- Brief explanation of why it matters
- Incorrect code example with explanation
- Correct code example with explanation
- Additional context and references
