---
title: Single Responsibility Principle
impact: HIGH
impactDescription: understandable units, easy to test and change
tags: design, srp, modularity, cohesion
---

## Single Responsibility Principle

Each module, class, and function should have one reason to change. If a
function validates input, executes business logic, *and* formats the response,
it has three reasons to change and is harder to test in isolation.

**Incorrect (one function does everything):**

```python
# BAD: Validation, business logic, persistence, and formatting in one function
def create_symptom_entry(request_data: dict) -> dict:
    if request_data.get("severity", -1) < 0:
        raise ValueError("bad severity")
    entry = SymptomEntry(**request_data)
    db.session.add(entry)
    db.session.commit()
    return {"id": str(entry.id), "severity": entry.severity}
```

**Correct (separated concerns):**

```python
# Schema handles validation
class SymptomEntryCreate(BaseModel):
    model_config = ConfigDict(strict=True)
    symptom_type: SymptomType
    severity: int = Field(ge=0, le=10)


# Service handles business logic
class SymptomService:
    def __init__(self, repo: SymptomRepository) -> None:
        self._repo = repo

    def create(self, data: SymptomEntryCreate, patient_id: PatientId) -> SymptomEntry:
        entry = SymptomEntry(patient_id=patient_id, **data.model_dump())
        return self._repo.save(entry)


# Route handles HTTP concerns
@app.post("/symptoms", response_model=SymptomEntryResponse)
async def create_symptom(body: SymptomEntryCreate) -> SymptomEntryResponse:
    entry = symptom_service.create(body, patient_id=current_patient_id())
    return SymptomEntryResponse.model_validate(entry, from_attributes=True)
```
