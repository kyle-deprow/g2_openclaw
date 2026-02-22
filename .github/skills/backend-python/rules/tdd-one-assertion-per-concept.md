---
title: One Assertion Per Concept
impact: HIGH
impactDescription: clear failure messages, pinpointed regressions
tags: tdd, testing, assertions, clarity
---

## One Assertion Per Concept

Each test should verify one logical concept. Multiple `assert` statements are
fine when they check facets of the same outcome, but don't test unrelated
behaviors in one test — a failure won't tell you what broke.

**Incorrect (testing multiple unrelated behaviors):**

```python
# BAD: If the first assert fails, you never learn about the others
def test_patient_profile(service):
    patient = service.create_patient(name="Jane", dob=date(1990, 1, 1))
    assert patient.name == "Jane"
    assert patient.id is not None
    updated = service.update_patient(patient.id, name="Janet")
    assert updated.name == "Janet"
    service.delete_patient(patient.id)
    assert service.get_patient(patient.id) is None
```

**Correct (one concept per test):**

```python
def test_create_patient_assigns_id(service: PatientService) -> None:
    patient = service.create_patient(name="Jane", dob=date(1990, 1, 1))
    assert patient.id is not None
    assert patient.name == "Jane"  # same concept: creation result


def test_update_patient_changes_name(service: PatientService) -> None:
    patient = service.create_patient(name="Jane", dob=date(1990, 1, 1))
    updated = service.update_patient(patient.id, name="Janet")
    assert updated.name == "Janet"


def test_delete_patient_removes_record(service: PatientService) -> None:
    patient = service.create_patient(name="Jane", dob=date(1990, 1, 1))
    service.delete_patient(patient.id)
    assert service.get_patient(patient.id) is None
```
