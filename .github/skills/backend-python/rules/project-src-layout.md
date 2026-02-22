---
title: src-layout and Test Placement
impact: MEDIUM
impactDescription: correct imports, clear separation
tags: project, layout, convention, structure
---

## src-layout and Test Placement

All source code lives under `src/spine_sense/`. All tests live under `tests/`
with filenames prefixed `test_`, mirroring the source structure. This prevents
accidental imports of the development copy and ensures `pytest` discovers tests
correctly.

**Incorrect (flat layout, mixed concerns):**

```
# BAD: test files next to source, no src layout
spine_sense/
    models.py
    test_models.py  # mixed in with source
    services.py
```

**Correct (src-layout with mirrored test structure):**

```
src/
  spine_sense/
    __init__.py
    models/
      patient.py
      symptom.py
    services/
      patient_service.py
tests/
  __init__.py
  models/
    test_patient.py
    test_symptom.py
  services/
    test_patient_service.py
```

Test files import from the installed package: `from spine_sense.models.patient import Patient`.
