"""Fold recorded lost owner runs into a per-resume count.

An owner run is *lost* when the gateway that accepted it restarted (or no longer knows the run),
so the wake row can never reach a terminal status. Neutral helper shared by the store, the wake
composer, and the read-only status projection; it reads plain event rows and never imports the
store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable

OWNER_FAILED_KIND = "owner_turn_failed"
RESUMED_KIND = "campaign_resumed"
GATEWAY_RESTART_STATUS = "run_lost_gateway_restart"
UNKNOWN_RUN_STATUS = "run_lost_unknown_run"
# The third lost run within one resume pauses the campaign for the operator.
MAX_LOST_OWNER_RUNS = 3

_QUERY = (
    "SELECT kind,attempt_id,detail FROM events "
    f"WHERE kind IN ('{OWNER_FAILED_KIND}','{RESUMED_KIND}') ORDER BY seq"
)


def lost_wake_key(base_key: str, lost: int) -> str:
    """Salt a wake key with the lost-run count; zero lost runs keeps the historical key."""
    if lost <= 0:
        return base_key
    return hashlib.sha256(f"{base_key}|lost:{lost}".encode()).hexdigest()


def _lost_detail(detail: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(detail)
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and parsed.get("lost") is True:
        return parsed
    return None


def lost_owner_run_summary(
    rows: Iterable[tuple[str, str | None, str]],
) -> tuple[int, tuple[str | None, str | None] | None]:
    """Fold ``(kind, attempt_id, detail)`` rows into (count since last resume, latest lost wake).

    The latest lost wake is ``(attempt_id, state)`` of the wake whose run was lost.
    """
    count = 0
    latest: tuple[str | None, str | None] | None = None
    for kind, attempt_id, detail in rows:
        if kind == RESUMED_KIND:
            count, latest = 0, None
        elif kind == OWNER_FAILED_KIND and (parsed := _lost_detail(detail)) is not None:
            count += 1
            state = parsed.get("wake_state")
            latest = (attempt_id, state if isinstance(state, str) else None)
    return count, latest


def read_lost_owner_runs(
    conn: sqlite3.Connection,
) -> tuple[int, tuple[str | None, str | None] | None]:
    """Count lost owner runs since the last operator resume."""
    rows = conn.execute(_QUERY).fetchall()
    return lost_owner_run_summary(
        (str(row[0]), None if row[1] is None else str(row[1]), str(row[2])) for row in rows
    )
