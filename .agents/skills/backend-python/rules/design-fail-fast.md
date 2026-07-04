---
title: Fail Fast — Validate at Boundaries
impact: HIGH
impactDescription: clear error messages, prevents invalid state propagation
tags: design, fail-fast, validation, errors
---

## Fail Fast — Validate at Boundaries

Validate inputs as early as possible — at API boundaries (Pydantic), service
entry points (precondition checks), and repository boundaries (constraint
violations). Never let invalid data travel deep into the system before
failing.

**Incorrect (late failure deep in the stack):**

```python
# BAD: Invalid severity silently passes through two layers,
# crashes in the database with a cryptic constraint error
def create_symptom(data: dict) -> SymptomEntry:
    entry = SymptomEntry(**data)
    return repo.save(entry)  # DB constraint: severity CHECK (0..10)
    # Error: "IntegrityError: check constraint violated" — unhelpful
```

**Correct (validate at each boundary):**

```python
# API boundary — Pydantic rejects immediately with a clear message
class SymptomEntryCreate(BaseModel):
    severity: int = Field(ge=0, le=10)  # "Input should be less than or equal to 10"


# Service boundary — business rule check
class SymptomService:
    def create(self, data: SymptomEntryCreate, patient_id: PatientId) -> SymptomEntry:
        if not self._patient_repo.exists(patient_id):
            raise PatientNotFoundError(patient_id)
        # Proceed only with validated, known-good data
        return self._repo.save(SymptomEntry(patient_id=patient_id, **data.model_dump()))
```

Custom exception types (`PatientNotFoundError`, `ConsentRequiredError`)
produce actionable error messages. Never raise bare `Exception` or
`ValueError` for domain errors.
