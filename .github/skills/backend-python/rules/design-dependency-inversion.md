---
title: Dependency Inversion via Protocols
impact: HIGH
impactDescription: testable, swappable implementations
tags: design, dependency-inversion, protocol, testing
---

## Dependency Inversion via Protocols

Depend on `Protocol` types, not concrete implementations. Services accept a
protocol (interface) for their dependencies, making it trivial to swap in
test doubles without monkeypatching or mock libraries.

**Incorrect (concrete dependency — hard to test):**

```python
# BAD: Service directly imports and uses the database class
from spine_sense.db import PostgresSymptomRepo

class SymptomService:
    def __init__(self) -> None:
        self._repo = PostgresSymptomRepo()  # can't replace in tests
```

**Correct (protocol dependency — injectable):**

```python
from typing import Protocol


class SymptomRepository(Protocol):
    def save(self, entry: SymptomEntry) -> SymptomEntry: ...
    def get_by_patient(self, patient_id: PatientId) -> list[SymptomEntry]: ...


class SymptomService:
    def __init__(self, repo: SymptomRepository) -> None:
        self._repo = repo

    def create(self, data: SymptomEntryCreate, patient_id: PatientId) -> SymptomEntry:
        entry = SymptomEntry(patient_id=patient_id, **data.model_dump())
        return self._repo.save(entry)


# Production: inject Postgres implementation
service = SymptomService(repo=PostgresSymptomRepo(session))

# Tests: inject in-memory fake
service = SymptomService(repo=InMemorySymptomRepo())
```

Python `Protocol` is structural — implementing classes don't need explicit
inheritance. Any class with matching methods satisfies the protocol.
