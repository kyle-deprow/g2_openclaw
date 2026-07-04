---
title: No Primitive Obsession
impact: CRITICAL
impactDescription: prevents ID mix-ups, self-documenting signatures
tags: typing, newtype, domain-types, ids
---

## No Primitive Obsession

Don't pass bare `str` or `int` for domain identifiers like patient IDs,
provider IDs, or invite codes. Use `NewType` or a thin wrapper to create
distinct types. This prevents mixing up two `str` arguments and makes
function signatures self-documenting.

**Incorrect (bare strings — easy to swap arguments):**

```python
# BAD: Which str is the patient, which is the provider?
def link_patient_to_provider(patient_id: str, provider_id: str) -> None: ...

# Silent bug — arguments swapped, no type error
link_patient_to_provider(provider_id, patient_id)
```

**Correct (NewType — compiler catches mix-ups):**

```python
from typing import NewType

PatientId = NewType("PatientId", str)
ProviderId = NewType("ProviderId", str)
InviteCode = NewType("InviteCode", str)


def link_patient_to_provider(patient_id: PatientId, provider_id: ProviderId) -> None:
    ...


# mypy error: Argument 1 has incompatible type "ProviderId"; expected "PatientId"
link_patient_to_provider(provider_id, patient_id)
```

`NewType` has zero runtime overhead — it's erased at runtime but checked by
mypy. For richer domain types that need validation, use a Pydantic
`RootModel` or `Annotated` type instead.
