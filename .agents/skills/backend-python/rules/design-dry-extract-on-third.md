---
title: DRY — Extract on the Third Occurrence
impact: HIGH
impactDescription: balanced abstraction, avoids premature generalization
tags: design, dry, duplication, refactor
---

## DRY — Extract on the Third Occurrence

Tolerate duplication the first time you see it. When the same logic appears a
*third* time, extract it into a shared function, class, or module. Premature
extraction (after just two occurrences) often creates wrong abstractions
because you don't yet know the full shape of variation.

**Incorrect (premature extraction after two occurrences):**

```python
# BAD: Created a "generic" validator after seeing it twice — wrong abstraction
class GenericRangeValidator:
    def __init__(self, min_val, max_val, field_name, model_type): ...
    # Over-engineered, hard to use, doesn't fit the third case
```

**Correct (extract when the pattern is clear):**

```python
# First occurrence: inline
severity = body.severity
if not 0 <= severity <= 10:
    raise ValueError("Severity must be 0-10")

# Second occurrence: tolerate the duplication
functional_score = body.functional_score
if not 0 <= functional_score <= 10:
    raise ValueError("Functional score must be 0-10")

# Third occurrence: now extract — the pattern is clear
def validate_nrs_score(value: int, field_name: str) -> int:
    """Validate a 0-10 Numeric Rating Scale score."""
    if not 0 <= value <= 10:
        raise ValueError(f"{field_name} must be between 0 and 10")
    return value
```

In Pydantic models, prefer `Field(ge=0, le=10)` constraints over manual
validation for simple range checks.
