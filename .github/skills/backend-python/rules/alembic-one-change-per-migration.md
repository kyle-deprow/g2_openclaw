---
title: One Logical Change Per Migration
impact: HIGH
impactDescription: safe rollbacks, reviewable diffs
tags: alembic, migrations, schema, database
---

## One Logical Change Per Migration

Each Alembic migration script should perform exactly one logical schema change
(add a table, add a column, create an index). Bundling multiple unrelated
changes into one migration makes rollbacks risky — you can't undo one change
without undoing all of them.

**Incorrect (multiple unrelated changes bundled):**

```python
# BAD: Adding a table AND modifying an unrelated table in one migration
def upgrade() -> None:
    op.create_table("symptom_entries", ...)
    op.add_column("patients", sa.Column("insurance_carrier", sa.String))
    op.create_index("ix_patients_email", "patients", ["email"])
```

**Correct (one change per migration):**

```python
# Migration 1: create symptom_entries table
def upgrade() -> None:
    op.create_table("symptom_entries", ...)

def downgrade() -> None:
    op.drop_table("symptom_entries")


# Migration 2: add insurance_carrier to patients
def upgrade() -> None:
    op.add_column("patients", sa.Column("insurance_carrier", sa.String))

def downgrade() -> None:
    op.drop_column("patients", "insurance_carrier")


# Migration 3: index patients.email
def upgrade() -> None:
    op.create_index("ix_patients_email", "patients", ["email"])

def downgrade() -> None:
    op.drop_index("ix_patients_email", "patients")
```

Reference: [Alembic — Working with Migration Scripts](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
