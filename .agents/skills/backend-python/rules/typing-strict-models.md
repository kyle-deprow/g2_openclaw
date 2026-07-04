---
title: Strict Pydantic Models at API Boundaries
impact: CRITICAL
impactDescription: runtime validation, self-documenting contracts
tags: typing, pydantic, api, validation
---

## Strict Pydantic Models at API Boundaries

Every API request and response must use a Pydantic `BaseModel` with
`model_config = ConfigDict(strict=True)`. Strict mode rejects type coercion
(e.g., `"7"` for an `int` field), catching malformed input at the gate
instead of deep in business logic.

**Incorrect (raw dicts, no validation):**

```python
# BAD: No validation, no documentation, anything can sneak through
@app.post("/symptoms")
async def create_symptom(request: Request):
    data = await request.json()
    severity = data["severity"]  # could be "seven", None, or missing
    return {"id": save_symptom(data)}
```

**Correct (strict Pydantic model):**

```python
from pydantic import BaseModel, ConfigDict, Field


class SymptomEntryCreate(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    symptom_type: SymptomType
    severity: int = Field(ge=0, le=10)
    location: BodyRegion
    onset_time: datetime
    notes: str = ""


class SymptomEntryResponse(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    id: SymptomEntryId
    symptom_type: SymptomType
    severity: int
    location: BodyRegion
    created_at: datetime


@app.post("/symptoms", response_model=SymptomEntryResponse)
async def create_symptom(body: SymptomEntryCreate) -> SymptomEntryResponse:
    entry = symptom_service.create(body)
    return SymptomEntryResponse.model_validate(entry, from_attributes=True)
```

Use `frozen=True` on response models to prevent accidental mutation after
construction.

Reference: [Pydantic — Strict Mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)
