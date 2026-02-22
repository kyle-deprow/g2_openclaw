---
title: Fixtures Over Factories
impact: MEDIUM
impactDescription: DRY test setup, consistent test data
tags: tdd, testing, pytest, fixtures
---

## Fixtures Over Factories

Use pytest fixtures for reusable test setup — database connections, service
instances, sample data. Fixtures are composable, scoped, and automatically
cleaned up. Reserve factory functions for when you need parameterized
variations of domain objects.

**Incorrect (manual setup in every test):**

```python
# BAD: Repeated boilerplate in every test
def test_create_symptom():
    repo = InMemorySymptomRepo()
    service = SymptomService(repo=repo)
    patient_repo = InMemoryPatientRepo()
    patient = patient_repo.save(Patient(name="Jane"))
    entry = service.create(patient_id=patient.id, type="pain", severity=5)
    assert entry.severity == 5

def test_list_symptoms():
    repo = InMemorySymptomRepo()    # duplicated
    service = SymptomService(repo=repo)  # duplicated
    # ...
```

**Correct (fixtures compose cleanly):**

```python
# conftest.py
@pytest.fixture()
def symptom_repo() -> InMemorySymptomRepo:
    return InMemorySymptomRepo()


@pytest.fixture()
def symptom_service(symptom_repo: InMemorySymptomRepo) -> SymptomService:
    return SymptomService(repo=symptom_repo)


@pytest.fixture()
def sample_patient(patient_repo: InMemoryPatientRepo) -> Patient:
    return patient_repo.save(Patient(name="Jane", dob=date(1990, 1, 1)))


# Tests are clean
def test_create_symptom(symptom_service: SymptomService, sample_patient: Patient) -> None:
    entry = symptom_service.create(
        patient_id=sample_patient.id, type="pain", severity=5,
    )
    assert entry.severity == 5
```
