"""Narrow human start/stop control for the bounded research driver."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import Attempt
from .jobs import JobRecord
from .jobs import cancel as cancel_job
from .status import ResearchStatus, read_status, resolve_research_root
from .store import ResearchStore

OWNER_UNIT = "research-owner.service"
_SYSTEMD_TIMEOUT_SECONDS = 5.0


class ControlError(RuntimeError):
    """Raised when an owner-only control operation cannot be proven safe."""


class OwnerUnit(Protocol):
    """The only process-lifecycle surface this module may use."""

    def state(self, unit: str) -> str: ...

    def start(self, unit: str) -> None: ...

    def stop(self, unit: str) -> None: ...


class ReviewCanceller(Protocol):
    """P3b1b adapter for reconciling one exact pending review attempt."""

    def cancel(self, attempt: Attempt, root: Path, reason: str) -> ReviewCancellation: ...


@dataclass(frozen=True, slots=True)
class ReviewCancellation:
    """Structured result from the future native/ACP review adapter."""

    state: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StartResult:
    started: bool
    resumed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StopResult:
    paused: bool
    job_cancelled: bool
    review_cancellation: str
    owner_stopped: bool
    completed: bool
    errors: tuple[str, ...] = ()


class SystemdOwnerUnit:
    """Run only ``systemctl --user`` for the exact owner unit."""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._run = run

    def state(self, unit: str) -> str:
        try:
            result = self._run(
                ["systemctl", "--user", "is-active", unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=_SYSTEMD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError(f"owner unit state probe failed: {exc}") from exc
        if result.returncode == 0:
            return "active"
        if result.returncode == 3:
            return "inactive"
        if result.returncode == 4 or "not-found" in (result.stdout + result.stderr).lower():
            return "missing"
        raise ControlError(
            result.stderr.strip() or result.stdout.strip() or "owner unit probe failed"
        )

    def start(self, unit: str) -> None:
        self._required("start", unit)

    def stop(self, unit: str) -> None:
        self._required("stop", unit)

    def _required(self, action: str, unit: str) -> None:
        try:
            result = self._run(
                ["systemctl", "--user", action, unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=_SYSTEMD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError(f"owner unit {action} failed: {exc}") from exc
        if result.returncode != 0:
            raise ControlError(
                result.stderr.strip() or result.stdout.strip() or f"owner unit {action} failed"
            )


def _configured(root: Path) -> bool:
    """Check initialization without constructing a mutating ResearchStore."""
    database = root.expanduser() / "state.sqlite3"
    if not database.is_file():
        return False
    uri = f"{database.resolve().as_uri()}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("PRAGMA query_only=ON")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_config'"
        ).fetchone()
        if table is None:
            return False
        row = conn.execute("SELECT 1 FROM driver_config WHERE singleton=1").fetchone()
        campaign = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign'"
        ).fetchone()
        campaign_row = (
            conn.execute("SELECT 1 FROM campaign WHERE singleton=1").fetchone()
            if campaign is not None
            else None
        )
        return row is not None and campaign_row is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        if conn is not None:
            conn.close()


def _job_record(row: sqlite3.Row, attempt_id: str, expected_job_id: str | None) -> JobRecord:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ControlError("active job payload is malformed") from exc
    if not isinstance(payload, dict):
        raise ControlError("active job payload is not an object")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id or job_id != expected_job_id:
        raise ControlError("active job identity does not match the attempt")
    if payload.get("attempt_id") != attempt_id:
        raise ControlError("active job attempt identity does not match")
    try:
        return JobRecord(
            job_id,
            attempt_id,
            int(payload["worker_pid"]),
            int(payload["worker_starttime"]),
            str(payload["run_dir"]),
            str(payload.get("state", "LAUNCHED")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlError("active job payload is missing identity fields") from exc


class OwnerControl:
    """Start or stop exactly the dedicated research owner service."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        unit: OwnerUnit | None = None,
        owner_unit: str = OWNER_UNIT,
        review_canceller: ReviewCanceller | None = None,
        job_cancel: Callable[[JobRecord], str] = cancel_job,
        status_reader: Callable[..., ResearchStatus] = read_status,
        readiness_gate: Callable[[ResearchStatus], str | None] | None = None,
    ) -> None:
        self.root = resolve_research_root(root)
        self.owner_unit = owner_unit
        self._unit = unit or SystemdOwnerUnit()
        self._review_canceller = review_canceller
        self._job_cancel = job_cancel
        self._status_reader = status_reader
        self._readiness_gate = readiness_gate

    def status(self) -> ResearchStatus:
        return self._status_reader(
            self.root, unit_state=self._unit.state, owner_unit=self.owner_unit
        )

    def start(self, reason: str = "operator start") -> StartResult:
        if not _configured(self.root):
            raise ControlError("research store is missing or not configured")
        try:
            state = self._unit.state(self.owner_unit)
        except Exception as exc:
            raise ControlError(f"owner unit cannot be inspected: {exc}") from exc
        if state == "missing":
            raise ControlError(f"owner unit is not installed: {self.owner_unit}")
        if state == "unknown":
            raise ControlError(f"owner unit state is unknown: {self.owner_unit}")

        status = self.status()
        if not status.available:
            raise ControlError(
                f"research status is unavailable: {status.unavailable_reason or 'unknown'}"
            )
        if status.campaign_status not in {"ACTIVE", "PAUSED"}:
            raise ControlError(
                f"research campaign state is not startable: {status.campaign_status or 'unknown'}"
            )
        if self._readiness_gate is None:
            raise ControlError("research start readiness gate is not integrated")
        try:
            refusal = self._readiness_gate(status)
        except Exception as exc:
            raise ControlError(f"research start readiness probe failed: {exc}") from exc
        if refusal is not None:
            raise ControlError(f"research start refused: {refusal}")
        resumed = status.campaign_status == "PAUSED"
        store = ResearchStore(self.root)
        if resumed:
            try:
                store.resume(reason)
            except Exception as exc:
                raise ControlError(f"campaign resume failed: {exc}") from exc
        try:
            self._unit.start(self.owner_unit)
        except Exception as exc:
            # Do not leave an active campaign represented as runnable when the
            # only owner unit failed to start (including a failed resume).
            try:
                store.pause(f"owner unit start failed: {exc}")
            except Exception as rollback_exc:
                raise ControlError(
                    f"owner unit start failed ({exc}); pause rollback failed ({rollback_exc})"
                ) from exc
            raise ControlError(f"owner unit start failed: {exc}") from exc
        return StartResult(started=True, resumed=resumed)

    def stop(self, reason: str = "operator stop") -> StopResult:
        if not _configured(self.root):
            raise ControlError("research store is missing or not configured")
        status = self.status()
        if not status.available:
            raise ControlError(
                f"research status is unavailable: {status.unavailable_reason or 'unknown'}"
            )
        store = ResearchStore(self.root)
        # Pause is intentionally first: no new wake or attempt may launch while
        # cancellation inspects the durable owner records.
        try:
            store.pause(reason)
        except Exception as exc:
            raise ControlError(f"campaign pause failed: {exc}") from exc

        errors: list[str] = []
        job_cancelled = False
        review_result = "not_pending"
        try:
            attempt = self._active_run_attempt(store)
            if attempt is not None:
                if attempt.state == "RUN_QUEUED":
                    errors.append("queued job cancellation adapter is not integrated")
                else:
                    row = store.job_for(attempt.attempt_id)
                    if row is None:
                        errors.append("active job record is missing")
                    else:
                        job = _job_record(row, attempt.attempt_id, attempt.run_job_id)
                        result = self._job_cancel(job)
                        if result not in {"CANCELLED", "cancelled"}:
                            errors.append(f"active job cancellation was not acknowledged: {result}")
                        else:
                            job_cancelled = True
                            # Keep the DB evidence aligned with the identity-checked
                            # cancellation; no unrelated process is touched.
                            payload = json.loads(str(row["payload_json"]))
                            if isinstance(payload, dict):
                                payload["state"] = "CANCELLED"
                                store.update_job(payload, attempt.attempt_id)
        except Exception as exc:
            errors.append(f"active job cancellation incomplete: {exc}")

        attempt = self._pending_review_attempt(store)
        if attempt is not None:
            if self._review_canceller is None:
                review_result = "incomplete_adapter"
            else:
                try:
                    outcome = self._review_canceller.cancel(attempt, self.root, reason)
                    review_result = (
                        "cancelled"
                        if outcome.state == "cancelled"
                        else f"incomplete_{outcome.state}"
                    )
                except Exception as exc:
                    review_result = "incomplete_error"
                    errors.append(f"pending review cancellation failed: {exc}")

        owner_stopped = False
        try:
            self._unit.stop(self.owner_unit)
            owner_stopped = True
        except Exception as exc:
            errors.append(f"owner unit stop failed: {exc}")

        completed = owner_stopped and not errors and review_result in {"not_pending", "cancelled"}
        return StopResult(
            paused=True,
            job_cancelled=job_cancelled,
            review_cancellation=review_result,
            owner_stopped=owner_stopped,
            completed=completed,
            errors=tuple(errors),
        )

    @staticmethod
    def _active_run_attempt(store: ResearchStore) -> Attempt | None:
        hypotheses = store.hypotheses()
        attempts = [attempt for h in hypotheses for attempt in store.attempts_for(h.hypothesis_id)]
        return next(
            (item for item in reversed(attempts) if item.state in {"RUNNING", "RUN_QUEUED"}),
            None,
        )

    @staticmethod
    def _pending_review_attempt(store: ResearchStore) -> Attempt | None:
        hypotheses = store.hypotheses()
        attempts = [attempt for h in hypotheses for attempt in store.attempts_for(h.hypothesis_id)]
        return next((item for item in reversed(attempts) if item.state == "IMPLEMENTED"), None)
