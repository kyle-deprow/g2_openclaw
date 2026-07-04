---
title: Every Migration Has a Downgrade
impact: HIGH
impactDescription: safe rollbacks in production
tags: alembic, migrations, downgrade, rollback
---

## Every Migration Has a Downgrade

Every `upgrade()` must have a corresponding `downgrade()` that fully reverses
the schema change. A migration without a working downgrade is a one-way door
that blocks rollback in production incidents.

**Incorrect (empty or missing downgrade):**

```python
def upgrade() -> None:
    op.create_table("consent_records",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("patient_id", sa.Uuid, nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
    )

def downgrade() -> None:
    pass  # BAD: Can't roll back
```

**Correct (fully reversible):**

```python
def upgrade() -> None:
    op.create_table("consent_records",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("patient_id", sa.Uuid, nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
    )

def downgrade() -> None:
    op.drop_table("consent_records")
```

For destructive operations (dropping columns), the downgrade should recreate
the column. If data loss is unavoidable on downgrade, document it with a
comment explaining why.
