---
title: Separate Schema and Data Migrations
impact: HIGH
impactDescription: testable, auditable, rollback-safe
tags: alembic, migrations, data, schema
---

## Separate Schema and Data Migrations

Never mix DDL (schema changes) and DML (data changes) in the same migration.
Schema migrations alter structure (tables, columns, indexes). Data migrations
backfill, transform, or seed rows. Mixing them makes rollbacks unpredictable
and can cause long locks on production tables.

**Incorrect (schema + data in one migration):**

```python
# BAD: Column add + data backfill in one transaction
def upgrade() -> None:
    op.add_column("patients", sa.Column("full_name", sa.String))
    # Data migration mixed in — locks entire table during UPDATE
    op.execute("UPDATE patients SET full_name = first_name || ' ' || last_name")
    op.drop_column("patients", "first_name")
    op.drop_column("patients", "last_name")
```

**Correct (separate migrations):**

```python
# Migration 1 — Schema: add new column
def upgrade() -> None:
    op.add_column("patients", sa.Column("full_name", sa.String, nullable=True))

def downgrade() -> None:
    op.drop_column("patients", "full_name")


# Migration 2 — Data: backfill (separate script, can run in batches)
def upgrade() -> None:
    op.execute(
        "UPDATE patients SET full_name = first_name || ' ' || last_name "
        "WHERE full_name IS NULL"
    )

def downgrade() -> None:
    pass  # data backfill is idempotent, no rollback needed


# Migration 3 — Schema: drop old columns (after backfill is verified)
def upgrade() -> None:
    op.alter_column("patients", "full_name", nullable=False)
    op.drop_column("patients", "first_name")
    op.drop_column("patients", "last_name")
```

This three-step approach allows you to deploy, verify, and proceed safely.
