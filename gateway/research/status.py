"""Read-only status projection for the bounded research driver.

This module deliberately does not import :class:`ResearchStore`.  Constructing
that class is a mutating operation (it creates the root and initialises the
schema), which is not safe for a status endpoint.  The projection reads one
existing SQLite database in a short, read-only transaction and degrades to an
explicit unavailable result when the database cannot be trusted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

DEFAULT_RESEARCH_ROOT = Path.home() / ".openclaw" / "research-v2"
RESEARCH_V2_ROOT_ENV = "RESEARCH_V2_ROOT"
DEFAULT_STATUS_INTERVAL_SECONDS = 5.0
_BUSY_TIMEOUT_MS = 2_000
_SYSTEMD_TIMEOUT_SECONDS = 5.0

ResearchStage = Literal["idle", "coding", "review", "run_queued", "running", "awaiting_decision"]
OwnerState = Literal["active", "inactive", "unknown"]

# The gateway's existing session-history reader supplies these immutable
# snapshots to the status poller. Keeping the callback's shape here avoids a
# second transcript service while keeping this module independent of the
# OpenClaw JSONL reader.
OwnerHistoryEntry = tuple[str, str, int]


def resolve_research_root(explicit: Path | None = None) -> Path:
    """Resolve the one shared research root from an override or environment."""
    if explicit is not None:
        return explicit.expanduser()
    return Path(os.environ.get(RESEARCH_V2_ROOT_ENV, str(DEFAULT_RESEARCH_ROOT))).expanduser()


@dataclass(frozen=True, slots=True)
class ResearchStatus:
    """Typed public status projection.

    Nullable values are intentional: an empty initialized campaign has no
    hypothesis or attempt yet and remains an available idle campaign.
    """

    hypothesis_id: str | None
    hypothesis_state: str | None
    attempt_id: str | None
    attempt_state: str | None
    stage: ResearchStage
    last_astra_decision: str | None
    campaign_status: str | None
    boundary_failure: str | None
    last_event_at: str | None
    owner_state: OwnerState
    updated_at: str | None
    available: bool = True
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the snake-case representation used by Python callers."""
        return asdict(self)


def unavailable_status(reason: str, *, owner_state: OwnerState = "unknown") -> ResearchStatus:
    """Build a structured unavailable response without touching the filesystem."""
    return ResearchStatus(
        hypothesis_id=None,
        hypothesis_state=None,
        attempt_id=None,
        attempt_state=None,
        stage="idle",
        last_astra_decision=None,
        campaign_status=None,
        boundary_failure=None,
        last_event_at=None,
        owner_state=owner_state,
        updated_at=None,
        available=False,
        unavailable_reason=reason,
    )


_REQUIRED_TABLES: dict[str, frozenset[str]] = {
    "hypotheses": frozenset({"hypothesis_id", "state", "created_at"}),
    "attempts": frozenset({"attempt_id", "hypothesis_id", "state", "updated_at"}),
    "events": frozenset({"seq", "at", "hypothesis_id", "attempt_id", "kind", "detail", "actor"}),
    "campaign": frozenset({"singleton", "status"}),
}


def _connect_read_only(database: Path) -> sqlite3.Connection:
    # SQLite's URI mode=ro refuses to create a missing file.  Keep the path
    # absolute so a caller cannot accidentally open a relative database.
    uri = f"{database.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _schema_is_compatible(conn: sqlite3.Connection) -> bool:
    for table, required in _REQUIRED_TABLES.items():
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            return False
        columns = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not required.issubset(columns):
            return False
    return True


def _json_detail(value: object) -> Mapping[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _stage(hypothesis_state: str | None, attempt_state: str | None) -> ResearchStage:
    if hypothesis_state == "DECIDED" and attempt_state == "CLOSED":
        return "idle"
    return {
        None: "idle",
        "OPENED": "coding",
        "IMPLEMENTED": "review",
        "REVIEW_FAILED": "awaiting_decision",
        "REVIEW_PASSED": "run_queued",
        "RUN_QUEUED": "run_queued",
        "RUNNING": "running",
        "RUN_SUCCEEDED": "awaiting_decision",
        "RUN_FAILED": "awaiting_decision",
        "CLOSED": "awaiting_decision",
    }.get(attempt_state, "idle")  # type: ignore[return-value]


def _decision(events: list[sqlite3.Row]) -> str | None:
    for event in reversed(events):
        if str(event["actor"]) != "astra":
            continue
        detail = _json_detail(event["detail"])
        value = detail.get("decision")
        if isinstance(value, str) and value:
            return value
    return None


def _boundary_failure(events: list[sqlite3.Row], attempt: sqlite3.Row | None) -> str | None:
    # Event evidence is authoritative.  Do not infer a healthy boundary from
    # a later success: the latest failure remains visible until a new event
    # replaces it.
    if attempt is not None:
        attempt_id = str(attempt["attempt_id"])
        events = [
            event
            for event in events
            if str(event["attempt_id"] or "") == attempt_id
            or (str(event["kind"]) == "owner_turn_failed" and event["attempt_id"] is None)
        ]
    for event in reversed(events):
        kind = str(event["kind"])
        detail = _json_detail(event["detail"])
        if kind == "owner_turn_failed":
            return str(detail.get("status") or detail.get("reason") or "owner_turn_failed")
        if kind == "review_submitted" and detail.get("verdict") == "FAIL":
            return "review_failed"
        if kind == "run_finished":
            status = detail.get("status")
            state = detail.get("state")
            if state == "RUN_FAILED" or status not in {None, "succeeded"}:
                return str(status or state or "run_failed")
    if attempt is not None and str(attempt["state"]) == "RUN_FAILED":
        return "run_failed"
    return None


def _owner_state(
    root: Path,
    *,
    unit_state: Callable[[str], str] | None,
    owner_unit: str,
) -> OwnerState:
    """Determine owner state from the exact unit and existing lock heldness.

    A missing lock is not proof that an inactive unit is dead; this function
    only reports active when both sources support it and reports unknown on
    probe errors.
    """
    unit: str | None = None
    if unit_state is not None:
        try:
            unit = unit_state(owner_unit)
        except Exception:
            logger.debug("owner unit probe failed", exc_info=True)
            return "unknown"
    lock_path = root / "owner.lock"
    held = False
    if lock_path.exists():
        import fcntl

        try:
            with lock_path.open("r", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return "unknown"
    if unit in {"active", "running"} and held:
        return "active"
    if unit is None:
        # A caller that deliberately omitted the unit probe has no evidence of
        # inactivity.  A held flock is positive owner evidence; otherwise the
        # honest projection is unknown rather than an inferred inactive state.
        return "active" if held else "unknown"
    if unit in {"inactive", "dead", "failed", "missing", "unknown"} and not held:
        return "inactive" if unit != "unknown" else "unknown"
    return "unknown"


def _systemd_state(unit: str) -> str:
    """Probe the exact user unit without changing its lifecycle."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SYSTEMD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode == 0:
        return "active"
    if result.returncode == 3:
        return "inactive"
    if result.returncode == 4 or "not-found" in (result.stdout + result.stderr).lower():
        return "missing"
    return "unknown"


def _read_status_from_connection(
    conn: sqlite3.Connection,
    root: Path,
    *,
    unit_state: Callable[[str], str] | None,
    owner_unit: str,
) -> ResearchStatus:
    if not _schema_is_compatible(conn):
        return unavailable_status("incompatible research store schema")
    conn.execute("BEGIN")
    try:
        campaign = conn.execute("SELECT status FROM campaign WHERE singleton=1").fetchone()
        if campaign is None:
            return unavailable_status("research campaign row is missing")
        hypothesis = conn.execute(
            "SELECT hypothesis_id,state FROM hypotheses "
            "ORDER BY created_at DESC,hypothesis_id DESC LIMIT 1"
        ).fetchone()
        attempt = None
        if hypothesis is not None:
            attempt = conn.execute(
                "SELECT attempt_id,state,updated_at FROM attempts WHERE hypothesis_id=? "
                "ORDER BY updated_at DESC,attempt_id DESC LIMIT 1",
                (hypothesis["hypothesis_id"],),
            ).fetchone()
        events = conn.execute(
            "SELECT seq,at,hypothesis_id,attempt_id,kind,detail,actor FROM events ORDER BY seq"
        ).fetchall()
        latest = events[-1] if events else None
        status = ResearchStatus(
            hypothesis_id=str(hypothesis["hypothesis_id"]) if hypothesis is not None else None,
            hypothesis_state=str(hypothesis["state"]) if hypothesis is not None else None,
            attempt_id=str(attempt["attempt_id"]) if attempt is not None else None,
            attempt_state=str(attempt["state"]) if attempt is not None else None,
            stage=_stage(
                str(hypothesis["state"]) if hypothesis is not None else None,
                str(attempt["state"]) if attempt is not None else None,
            ),
            last_astra_decision=_decision(events),
            campaign_status=str(campaign["status"]) if campaign is not None else None,
            boundary_failure=_boundary_failure(events, attempt),
            last_event_at=str(latest["at"]) if latest is not None else None,
            owner_state=_owner_state(root, unit_state=unit_state, owner_unit=owner_unit),
            updated_at=(
                str(attempt["updated_at"])
                if attempt is not None
                else (str(latest["at"]) if latest is not None else None)
            ),
        )
    finally:
        conn.rollback()
    return status


def read_status(
    root: Path | None = None,
    *,
    unit_state: Callable[[str], str] | None = _systemd_state,
    owner_unit: str = "research-owner.service",
) -> ResearchStatus:
    """Read existing ``state.sqlite3`` without creating or repairing files.

    SQLite's normal ``mode=ro`` URI does not write research rows, but opening
    an existing WAL database may create SQLite coordination sidecars (for
    example ``-wal``/``-shm``). Those sidecars are standard SQLite read
    bookkeeping, not research-store initialization; missing roots and missing
    databases remain strictly non-creating and return an unavailable result.
    """
    root = resolve_research_root(root)
    database = root.expanduser() / "state.sqlite3"
    if not database.is_file():
        return unavailable_status("research state database is missing")
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_read_only(database)
        return _read_status_from_connection(
            conn, root.expanduser(), unit_state=unit_state, owner_unit=owner_unit
        )
    except sqlite3.DatabaseError as exc:
        return unavailable_status(f"research state database is unreadable: {exc}")
    except OSError as exc:
        return unavailable_status(f"research state database cannot be opened: {exc}")
    finally:
        if conn is not None:
            conn.close()


def build_status_frame(status: ResearchStatus) -> dict[str, object]:
    """Convert the typed projection into the camel-case wire frame."""
    return {
        "type": "autoresearch_status",
        "hypothesisId": status.hypothesis_id,
        "hypothesisState": status.hypothesis_state,
        "attemptId": status.attempt_id,
        "attemptState": status.attempt_state,
        "stage": status.stage,
        "lastAstraDecision": status.last_astra_decision,
        "campaignStatus": status.campaign_status,
        "boundaryFailure": status.boundary_failure,
        "lastEventAt": status.last_event_at,
        "ownerState": status.owner_state,
        "updatedAt": status.updated_at,
        "available": status.available,
        "unavailableReason": status.unavailable_reason,
    }


class ResearchStatusPublisher:
    """Push changed read-only projections to one connected G2 client."""

    def __init__(
        self,
        send: Callable[[dict[str, object]], Awaitable[None]],
        *,
        root: Path | None = None,
        poll_interval: float = DEFAULT_STATUS_INTERVAL_SECONDS,
        read: Callable[[], ResearchStatus] | None = None,
        read_owner_history: Callable[[], Sequence[OwnerHistoryEntry]] | None = None,
        owner_history_baseline: Sequence[OwnerHistoryEntry] | None = None,
    ) -> None:
        self._send = send
        self._root = resolve_research_root(root)
        self._poll_interval = poll_interval
        self._read = read or (lambda: read_status(self._root))
        self._read_owner_history = read_owner_history
        self._owner_history_baseline = (
            self._owner_tail(owner_history_baseline) if owner_history_baseline is not None else None
        )
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _owner_tail(
        entries: Sequence[OwnerHistoryEntry] | None,
    ) -> tuple[OwnerHistoryEntry, ...]:
        """Return the exact bounded assistant projection used by the G2 UI."""
        if entries is None:
            return ()
        seen: set[OwnerHistoryEntry] = set()
        assistants: list[OwnerHistoryEntry] = []
        for entry in entries:
            role, _text, _ts = entry
            if role != "assistant" or entry in seen:
                continue
            seen.add(entry)
            assistants.append(entry)
        return tuple(assistants[-10:])

    @staticmethod
    def _history_frame(entries: Sequence[OwnerHistoryEntry]) -> dict[str, object]:
        """Build the additive history frame for changed owner messages."""
        return {
            "type": "history",
            "historyKind": "research_owner_delta",
            "entries": [
                {"role": role, "text": text[:2000], "ts": timestamp}
                for role, text, timestamp in entries
            ],
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _run(self) -> None:
        previous: ResearchStatus | None = None
        while True:
            try:
                current = await asyncio.to_thread(self._read)
                if previous != current:
                    await self._send(build_status_frame(current))
                    previous = current
            except ConnectionClosed:
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("research status projection failed", exc_info=True)

            # Owner transcript presentation intentionally shares this existing
            # status poll. It is read-only and independent of the main spoken
            # session; status projection failures must not suppress it.
            if self._read_owner_history is not None:
                try:
                    current_owner = self._owner_tail(
                        await asyncio.to_thread(self._read_owner_history)
                    )
                    if self._owner_history_baseline is None:
                        # A directly constructed publisher has no initial
                        # history frame to compare with. Seed a baseline on
                        # its first read rather than flooding the client.
                        self._owner_history_baseline = current_owner
                    else:
                        previous_keys = set(self._owner_history_baseline)
                        changed = [entry for entry in current_owner if entry not in previous_keys]
                        if changed:
                            await self._send(self._history_frame(changed))
                        self._owner_history_baseline = current_owner
                except ConnectionClosed:
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("research owner history projection failed", exc_info=True)
            await asyncio.sleep(max(self._poll_interval, 0.05))
