---
title: Explicit Optional with Union Syntax
impact: MEDIUM
impactDescription: readable signatures, safe defaults
tags: typing, optional, union, defaults
---

## Explicit Optional with Union Syntax

Use `X | None` (PEP 604) instead of `Optional[X]`. The `|` syntax is clearer
about intent and consistent with Python 3.12+ style. Never use a mutable
default value for optional parameters.

**Incorrect (old-style Optional, mutable default):**

```python
from typing import Optional, List

# BAD: Optional[X] is less readable; mutable default is a classic bug
def search_patients(
    name: Optional[str] = None,
    tags: List[str] = [],  # shared mutable default!
) -> List[Patient]:
    ...
```

**Correct (union syntax, immutable defaults):**

```python
def search_patients(
    name: str | None = None,
    tags: list[str] | None = None,
) -> list[Patient]:
    resolved_tags = tags if tags is not None else []
    ...
```

In Pydantic models, use `Field(default=None)` or `Field(default_factory=list)`
for optional fields with non-trivial defaults.
