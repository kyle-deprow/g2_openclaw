"""Narrow human start/stop control for the bounded research driver."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gateway.openclaw_client import OpenClawClient

from .contracts import Attempt
from .jobs import JobRecord
from .jobs import cancel as cancel_job
from .readiness import build_readiness_gate
from .review_evidence import cancel_review, request_cancel
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


class OpenClawReviewCanceller:
    """Bind the exact review cancellation protocol to one trusted core DB."""

    def __init__(
        self,
        core_database: Path,
        request_once: Callable[..., Awaitable[Mapping[str, object]]],
    ) -> None:
        self._core_database = core_database
        self._request_once = request_once

    def cancel(self, attempt: Attempt, root: Path, reason: str) -> ReviewCancellation:
        async def request(task_id: str, cancel_reason: str) -> Mapping[str, object]:
            return await request_cancel(self._request_once, task_id, cancel_reason)

        outcome = cancel_review(
            ResearchStore(root),
            attempt.attempt_id,
            reason,
            self._core_database,
            request,
        )
        if outcome.pending:
            return ReviewCancellation("pending", outcome.status)
        if outcome.status in {"cancelled", "interrupted"}:
            return ReviewCancellation("cancelled", outcome.status)
        return ReviewCancellation("terminal", outcome.status)


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
                    try:
                        self._cancel_queued(store, attempt, reason)
                        job_cancelled = True
                    except Exception as exc:
                        errors.append(f"queued job cancellation incomplete: {exc}")
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
    def _cancel_queued(store: ResearchStore, attempt: Attempt, reason: str) -> None:
        """Release one exact queued job and mark its canonical row cancelled."""
        job_id = attempt.run_job_id
        if not job_id:
            raise ControlError("queued attempt has no job identity")
        store.acquire_run_lock()
        try:
            current = store.get_attempt(attempt.attempt_id)
            row = store.job_for(attempt.attempt_id)
            if row is None or str(row["job_id"]) != job_id or str(row["state"]) != "QUEUED":
                raise ControlError("queued job identity is no longer canonical")
            payload = json.loads(str(row["payload_json"]))
            if (
                not isinstance(payload, dict)
                or payload.get("job_id") != job_id
                or payload.get("attempt_id") != attempt.attempt_id
            ):
                raise ControlError("queued job payload identity does not match")
            if current.state.value != "RUN_QUEUED" or current.run_job_id != job_id:
                raise ControlError("queued attempt identity is no longer canonical")
            store.release_queued_run(attempt.attempt_id, job_id, reason)
            payload["state"] = "CANCELLED"
            store.update_job(payload, attempt.attempt_id, event="job_cancelled")
        finally:
            store.release_run_lock()

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


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ControlError(f"{name} is required for production research control")
    return value


def production_owner_control(root: Path | None = None) -> OwnerControl:
    """Construct the production control surface from deployment-provided env."""
    core_database = Path(_required_environment("RESEARCH_CORE_DATABASE"))
    if not core_database.is_absolute() or core_database.is_symlink() or not core_database.is_file():
        raise ControlError("RESEARCH_CORE_DATABASE must be an absolute regular file")
    host = _required_environment("OPENCLAW_HOST")
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ControlError("OPENCLAW_HOST must be a loopback host")
    raw_port = _required_environment("OPENCLAW_PORT")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ControlError("OPENCLAW_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ControlError("OPENCLAW_PORT must be in the valid TCP range")
    token = _required_environment("OPENCLAW_GATEWAY_TOKEN")
    client = OpenClawClient(host, port, token)
    resolved_root = resolve_research_root(root)
    return OwnerControl(
        resolved_root,
        review_canceller=OpenClawReviewCanceller(core_database, client.request_once),
        readiness_gate=build_readiness_gate(resolved_root),
    )
