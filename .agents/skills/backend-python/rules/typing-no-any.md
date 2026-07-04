---
title: Never Use Any
impact: HIGH
impactDescription: preserves type safety chain
tags: typing, any, mypy, generics
---

## Never Use Any

`Any` silently disables type checking for everything it touches. Use `Union`,
`Protocol`, generics (`T`), or `object` instead. The `--strict` mypy config
in this project flags implicit `Any` — keep it that way.

**Incorrect (Any breaks the type chain):**

```python
from typing import Any

# BAD: Caller has no idea what they get back
def process_record(record: Any) -> Any:
    return record["name"]  # no type error, but will crash on non-dicts
```

**Correct (narrow types):**

```python
from typing import Protocol


class HasName(Protocol):
    @property
    def name(self) -> str: ...


def process_record(record: HasName) -> str:
    return record.name


# Or use a Union for a known set of types
def parse_input(value: str | int) -> str:
    return str(value)
```

If you genuinely need a container for heterogeneous data, use `object` and
narrow with `isinstance` checks.
