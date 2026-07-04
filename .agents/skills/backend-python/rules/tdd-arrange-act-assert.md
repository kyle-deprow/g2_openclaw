---
title: Arrange-Act-Assert Structure
impact: HIGH
impactDescription: readable tests, consistent structure
tags: tdd, testing, aaa, structure
---

## Arrange-Act-Assert Structure

Structure every test as three distinct phases separated by blank lines:
**Arrange** (set up preconditions), **Act** (execute the behavior under test),
**Assert** (verify the outcome). This makes tests scannable and intention clear.

**Incorrect (phases mixed together):**

```python
def test_symptom(repo):
    entry = repo.save(SymptomEntry(type="pain", severity=7, patient_id="p1"))
    assert entry.id is not None
    entries = repo.list_by_patient("p1")
    assert len(entries) == 1
    assert entries[0].severity == 7
```

**Correct (clear AAA phases):**

```python
def test_saved_symptom_entry_is_retrievable(repo: FakeSymptomRepo) -> None:
    # Arrange
    entry = SymptomEntry(type="pain", severity=7, patient_id=PatientId("p1"))

    # Act
    saved = repo.save(entry)
    retrieved = repo.list_by_patient(PatientId("p1"))

    # Assert
    assert saved.id is not None
    assert len(retrieved) == 1
    assert retrieved[0].severity == 7
```

For very short tests the blank-line separation is sufficient — comments are
optional when the phases are obvious.
