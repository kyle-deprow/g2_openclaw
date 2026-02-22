---
name: backend-python
description:
  Python backend development with TDD, Pydantic, Alembic, and clean architecture principles. Use when building API endpoints, domain models, database migrations, service layers, or writing tests for the SpineSense Python backend. Triggers on tasks involving Python modules, pytest, SQLAlchemy, Pydantic schemas, or Alembic migrations.
---

# Backend Python

Principles and patterns for building the SpineSense Python backend. Test-driven
development, strong typing, disciplined migrations, and pragmatic design.

## When to Apply

Reference these guidelines when:

- Writing or modifying Python modules under `src/spine_sense/`
- Creating or updating tests under `tests/`
- Designing Pydantic models for API request/response schemas
- Creating or reviewing Alembic migration scripts
- Adding new service-layer or repository functions
- Reviewing code for type safety, test coverage, or design smell

## Rule Categories by Priority

| Priority | Category             | Impact   | Prefix      |
| -------- | -------------------- | -------- | ----------- |
| 1        | Test-Driven Dev      | CRITICAL | `tdd-`      |
| 2        | Type Safety          | CRITICAL | `typing-`   |
| 3        | Design Principles    | HIGH     | `design-`   |
| 4        | Database Migrations  | HIGH     | `alembic-`  |
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

### 4. Database & Alembic (HIGH)

- `alembic-one-change-per-migration` - Each migration does one logical schema change
- `alembic-always-downgrade` - Every upgrade has a working downgrade
- `alembic-no-data-in-schema` - Separate schema migrations from data migrations
- `alembic-descriptive-names` - Migration messages describe the change, not a ticket number

### 5. Project Conventions (MEDIUM)

- `project-src-layout` - All source under `src/spine_sense/`, tests under `tests/`
- `project-uv-only` - Use `uv` for all dependency management; never pip/poetry
- `project-no-raw-sql` - No f-string or .format() SQL; use parameterized queries or ORM

## How to Use

Read individual rule files for detailed explanations and code examples:

```
rules/tdd-red-green-refactor.md
rules/typing-strict-models.md
```

Each rule file contains:

- Brief explanation of why it matters
- Incorrect code example with explanation
- Correct code example with explanation
- Additional context and references
