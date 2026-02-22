# SpineSense — Copilot Instructions

## Project Overview

SpineSense is a HIPAA-compliant healthcare platform for spinal health. It has two client-facing components—a mobile Patient App and a web-based Provider Platform—backed by a shared Python API and PostgreSQL database. This repository contains the backend service layer.

## Tech Stack

- **Language:** Python 3.12+
- **Package/environment manager:** uv (not pip, not poetry)

## Project Layout

This project uses a **src-layout**:

```
src/spine_sense/   → application source code (the importable package)
tests/             → all tests live here, mirroring src structure
docs/requirements/ → functional & non-functional requirements specs
docs/schemas/      → database schema design documentation (not executable SQL)
```

Always place new source modules under `src/spine_sense/`. Always place tests under `tests/` with filenames prefixed `test_`.


## Commands Reference

```bash
uv sync --extra dev    # install all deps
uv run pytest          # run tests with coverage
uv run ruff check .    # lint
uv run ruff format .   # format
uv run pre-commit run --all-files  # run all pre-commit hooks
```

## Things to Avoid

- Do not use `pip install` or `poetry`. This project uses **uv** exclusively.
- Do not create raw SQL strings with f-strings or `.format()`.
- Do not store or compare timestamps in local time. Always use UTC.
- Do not bypass RLS or consent checks in queries, even in "admin" contexts.
- Do not add dependencies without also adding them to `pyproject.toml` via `uv add`.
