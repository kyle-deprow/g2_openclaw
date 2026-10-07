"""Fold recorded ``hypothesis-create`` refusals into the owner-visible count.

Neutral helper shared by the store, the wake composer, and the read-only status projection.
It reads plain event rows and never imports the store.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

REFUSED_KIND = "hypothesis_create_refused"
# Progress or an operator restart: a refusal streak ends at any of these.
RESET_KINDS = ("hypothesis_created", "hypothesis_decided", "campaign_resumed")

_QUERY = (
    "SELECT kind,detail FROM events WHERE kind IN ("
    + ",".join(f"'{kind}'" for kind in (*RESET_KINDS, REFUSED_KIND))
    + ") ORDER BY seq"
)


def create_refusal_summary(rows: Iterable[tuple[str, str]]) -> tuple[int, str | None]:
    """Fold ``(kind, detail)`` event rows into (refusals since last reset, latest reason)."""
    count = 0
    latest: str | None = None
    for kind, detail in rows:
        if kind in RESET_KINDS:
            count, latest = 0, None
        elif kind == REFUSED_KIND:
            count += 1
            latest = _reason(detail)
    return count, latest


def read_create_refusals(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Count refusals since the last create, decide, or resume, and return the latest reason."""
    rows = conn.execute(_QUERY).fetchall()
    return create_refusal_summary((str(row[0]), str(row[1])) for row in rows)


def _reason(detail: str) -> str | None:
    try:
        parsed = json.loads(detail)
    except (TypeError, json.JSONDecodeError):
        return None
    reason = parsed.get("reason") if isinstance(parsed, dict) else None
    return reason if isinstance(reason, str) else None
