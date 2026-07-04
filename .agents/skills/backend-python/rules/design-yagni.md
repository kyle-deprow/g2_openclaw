---
title: YAGNI — You Aren't Gonna Need It
impact: HIGH
impactDescription: avoids unused code, keeps codebase lean
tags: design, yagni, simplicity, pragmatism
---

## YAGNI — You Aren't Gonna Need It

Don't build functionality until a test or requirement explicitly demands it.
Speculative abstractions add complexity, maintenance burden, and are often
wrong when the actual need arises. Let tests drive what gets built.

**Incorrect (building for imagined future needs):**

```python
# BAD: No requirement for CSV/XML export, plugin system, or multi-tenancy yet
class PatientExporter:
    def export_json(self, patient: Patient) -> str: ...
    def export_csv(self, patient: Patient) -> str: ...      # no requirement
    def export_xml(self, patient: Patient) -> str: ...      # no requirement

class PluginRegistry:  # no requirement for plugins at all
    def register(self, plugin: Plugin) -> None: ...
```

**Correct (build only what's needed now):**

```python
# GOOD: Export as JSON is the only current requirement
def export_patient_json(patient: Patient) -> str:
    return PatientExportSchema.model_validate(
        patient, from_attributes=True,
    ).model_dump_json()
```

When a second export format is actually required, you'll know the real shape
of the need and can design the abstraction correctly. Until then, a simple
function is enough.
