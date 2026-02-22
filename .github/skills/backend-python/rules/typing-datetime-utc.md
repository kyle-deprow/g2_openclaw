---
title: UTC Datetimes with Timezone Info
impact: CRITICAL
impactDescription: prevents time-zone bugs in HIPAA-regulated data
tags: typing, datetime, utc, timezone
---

## UTC Datetimes with Timezone Info

All datetimes in the system must be timezone-aware UTC. Never store or compare
naive datetimes. Use `datetime.UTC` (Python 3.11+) and Pydantic's
`AwareDatetime` annotation to enforce this at both application and API
boundaries.

**Incorrect (naive datetimes — ambiguous timezone):**

```python
from datetime import datetime

# BAD: No timezone — is this UTC? Local? Server time?
created_at = datetime.now()
```

**Correct (timezone-aware UTC):**

```python
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict


# Application code — always explicit UTC
created_at = datetime.now(UTC)


# Pydantic models — enforce awareness at API boundary
class SymptomEntryCreate(BaseModel):
    model_config = ConfigDict(strict=True)

    onset_time: AwareDatetime  # rejects naive datetimes


# Database columns — store as timestamptz
# SQLAlchemy: Column(DateTime(timezone=True))
```

Use `AwareDatetime` in Pydantic models to reject naive datetimes at
deserialization. In the database, always use `TIMESTAMPTZ` columns.

Reference: [Python docs — datetime.UTC](https://docs.python.org/3/library/datetime.html#datetime.UTC)
