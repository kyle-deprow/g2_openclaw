---
title: No Test Interdependence
impact: HIGH
impactDescription: eliminates flaky tests, enables parallel runs
tags: tdd, testing, isolation, independence
---

## No Test Interdependence

Every test must be fully independent — it sets up its own state, runs, and
tears down without relying on other tests having run before it. Tests that
depend on execution order become flaky and mask real failures.

**Incorrect (tests share mutable state):**

```python
# BAD: test_update assumes test_create ran first and left data behind
patient_id = None

def test_create(service):
    global patient_id
    patient_id = service.create_patient(name="Jane").id

def test_update(service):
    service.update_patient(patient_id, name="Janet")  # fails if test_create didn't run
```

**Correct (each test owns its own state):**

```python
def test_create_patient(service: PatientService) -> None:
    patient = service.create_patient(name="Jane", dob=date(1990, 1, 1))
    assert patient.id is not None


def test_update_patient(service: PatientService) -> None:
    patient = service.create_patient(name="Jane", dob=date(1990, 1, 1))

    updated = service.update_patient(patient.id, name="Janet")

    assert updated.name == "Janet"
```

Use pytest fixtures with appropriate scope (`function` by default) for shared
setup that needs isolation.
