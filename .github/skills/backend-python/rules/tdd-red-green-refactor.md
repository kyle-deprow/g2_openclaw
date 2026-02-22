---
title: Red-Green-Refactor Cycle
impact: CRITICAL
impactDescription: catches regressions, documents intent, drives design
tags: tdd, testing, red-green-refactor, workflow
---

## Red-Green-Refactor Cycle

Always follow the TDD cycle: write a failing test (Red), write the minimum code
to pass it (Green), then improve the code without changing behavior (Refactor).
Never write production code without a failing test demanding it.

**Incorrect (code first, tests bolted on):**

```python
# BAD: Wrote the entire service, then added tests that mirror the implementation
class PatientService:
    def create_patient(self, data: dict) -> dict:
        validated = self._validate(data)
        patient = self._repo.save(validated)
        self._send_welcome_email(patient)
        return patient.to_dict()

# Tests added after — they test implementation details, not behavior
def test_create_patient_calls_validate(mocker):
    mocker.patch.object(service, "_validate")
    service.create_patient({})
    service._validate.assert_called_once()
```

**Correct (test drives the design):**

```python
# Step 1 — RED: Write the test for the behavior you want
def test_create_patient_returns_patient_with_id(patient_repo: FakePatientRepo) -> None:
    service = PatientService(repo=patient_repo)

    result = service.create_patient(name="Jane Doe", date_of_birth=date(1990, 5, 15))

    assert result.id is not None
    assert result.name == "Jane Doe"


# Step 2 — GREEN: Write the minimum code to pass
class PatientService:
    def __init__(self, repo: PatientRepository) -> None:
        self._repo = repo

    def create_patient(self, name: str, date_of_birth: date) -> Patient:
        patient = Patient(name=name, date_of_birth=date_of_birth)
        return self._repo.save(patient)


# Step 3 — REFACTOR: Improve structure without changing behavior
# (tests still pass after refactor)
```

Reference: [Kent Beck — Test-Driven Development by Example](https://www.oreilly.com/library/view/test-driven-development/0321146530/)
