---
title: No Raw SQL String Construction
impact: HIGH
impactDescription: prevents SQL injection, HIPAA security requirement
tags: project, sql, security, convention
---

## No Raw SQL String Construction

Never build SQL queries using f-strings, `.format()`, or string concatenation.
This is a security requirement for a HIPAA-regulated system — SQL injection
is a direct path to a data breach. Use parameterized queries or the ORM's
query builder.

**Incorrect (string interpolation — SQL injection risk):**

```python
# BAD: Direct injection vector
def get_patient(patient_id: str) -> Patient:
    query = f"SELECT * FROM patients WHERE id = '{patient_id}'"
    return db.execute(query).fetchone()

# BAD: .format() is equally dangerous
query = "SELECT * FROM patients WHERE email = '{}'".format(email)
```

**Correct (parameterized query):**

```python
from sqlalchemy import text

# Parameterized — safe from injection
def get_patient(session: Session, patient_id: PatientId) -> Patient | None:
    stmt = text("SELECT * FROM patients WHERE id = :patient_id")
    row = session.execute(stmt, {"patient_id": patient_id}).fetchone()
    return Patient.from_row(row) if row else None


# ORM query builder — even better
def get_patient(session: Session, patient_id: PatientId) -> Patient | None:
    return session.get(PatientModel, patient_id)
```

Reference: [OWASP — SQL Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)
