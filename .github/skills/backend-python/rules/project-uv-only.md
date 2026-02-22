---
title: Use uv Exclusively
impact: MEDIUM
impactDescription: consistent dependency resolution, reproducible environments
tags: project, uv, dependencies, convention
---

## Use uv Exclusively

This project uses **uv** for all dependency and environment management. Never
use `pip install`, `poetry`, or `conda`. All dependencies are declared in
`pyproject.toml` and locked by uv.

**Incorrect:**

```bash
# BAD: Any of these
pip install pydantic
poetry add pydantic
conda install pydantic
```

**Correct:**

```bash
# Add a runtime dependency
uv add pydantic

# Add a dev-only dependency
uv add --extra dev pytest-asyncio

# Install all dependencies (including dev)
uv sync --extra dev

# Run a command in the managed environment
uv run pytest
uv run ruff check .
uv run mypy src/
```

Never commit changes that require `pip install` to set up the project.
