---
title: Descriptive Migration Messages
impact: MEDIUM
impactDescription: readable migration history, fast troubleshooting
tags: alembic, migrations, naming, conventions
---

## Descriptive Migration Messages

Migration revision messages should describe *what* the migration does, not
reference a ticket number or use a vague label. A descriptive message makes
`alembic history` readable without cross-referencing a project tracker.

**Incorrect (vague or ticket-only names):**

```bash
# BAD: What do these actually do?
alembic revision --autogenerate -m "SPINE-142"
alembic revision --autogenerate -m "fix stuff"
alembic revision --autogenerate -m "update schema"
```

**Correct (descriptive verb-noun messages):**

```bash
alembic revision --autogenerate -m "create symptom_entries table"
alembic revision --autogenerate -m "add insurance_carrier to patients"
alembic revision --autogenerate -m "create ix_symptom_entries_patient_id index"
alembic revision --autogenerate -m "add severity check constraint 0-10"
```

Pattern: `<verb> <object>` — e.g., "create", "add", "drop", "rename",
"alter", "backfill".
