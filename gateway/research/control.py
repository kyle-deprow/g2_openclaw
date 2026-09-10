"""Narrow human start/stop control for the bounded research driver."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

from gateway.openclaw_client import OpenClawClient

from .contracts import Attempt
from .jobs import JobRecord
from .jobs import cancel as cancel_job
from .readiness import build_readiness_gate
from .review_evidence import cancel_review, request_cancel
from .status import ResearchStatus, read_status, resolve_research_root, unavailable_status
from .store import ResearchStore

OWNER_UNIT = "research-owner.service"
_SYSTEMD_TIMEOUT_SECONDS = 5.0
_OWNER_ENV_FILE = "G2_OWNER_ENV_FILE"
_OWNER_ENV_KEYS = frozenset(
    {
        "OPENCLAW_HOST",
        "OPENCLAW_PORT",
        "OPENCLAW_GATEWAY_TOKEN",
        "RESEARCH_CORE_DATABASE",
        "RESEARCH_V2_ROOT",
    }
)
_OWNER_ENV_MAX_BYTES = 64 * 1024
_UNRESOLVED_PLACEHOLDER = re.compile(r"\$(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*|\()")


class ControlError(RuntimeError):
    """Raised when an owner-only control operation cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class _OwnerEnvironment:
    """Allowlisted deployment contract without mutating process environment."""

    values: Mapping[str, str]
    refusal: str | None = None


def _owner_environment_refusal(reason: str) -> _OwnerEnvironment:
    return _OwnerEnvironment({}, f"owner_environment_{reason}")


def _read_protected_owner_env(path_text: str) -> tuple[str | None, str | None]:
    """Read one private dotenv file through a non-following descriptor."""
    if not path_text or "\x00" in path_text:
        return None, "invalid"
    path = Path(path_text)
    if not path.is_absolute():
        return None, "not_absolute"
    try:
        path_stat = path.lstat()
    except OSError:
        return None, "missing"
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return None, "not_regular"
    if path_stat.st_uid != os.geteuid():
        return None, "owner"
    if stat.S_IMODE(path_stat.st_mode) & 0o077:
        return None, "permissions"
    if path_stat.st_nlink != 1:
        return None, "hardlink"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened_stat = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or stat.S_ISLNK(opened_stat.st_mode)
                or opened_stat.st_uid != os.geteuid()
                or stat.S_IMODE(opened_stat.st_mode) & 0o077
                or opened_stat.st_nlink != 1
            ):
                return None, "identity"
            raw = source.read(_OWNER_ENV_MAX_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(raw) > _OWNER_ENV_MAX_BYTES:
        return None, "too_large"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "encoding"


def _parse_owner_environment(raw: str) -> _OwnerEnvironment:
    """Parse dotenv syntax while enforcing the exact production allowlist."""
    seen: set[str] = set()
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        if "=" not in candidate:
            return _owner_environment_refusal("malformed")
        key = candidate.split("=", 1)[0].strip()
        if key not in _OWNER_ENV_KEYS:
            return _owner_environment_refusal("unknown_key")
        if key in seen:
            return _owner_environment_refusal("duplicate_key")
        seen.add(key)
    try:
        parsed = dotenv_values(stream=StringIO(raw), interpolate=False)
    except (OSError, UnicodeError, ValueError):
        return _owner_environment_refusal("malformed")
    if set(parsed) != seen:
        return _owner_environment_refusal("malformed")
    values: dict[str, str] = {}
    for key in _OWNER_ENV_KEYS:
        value = parsed.get(key)
        if not isinstance(value, str) or not value:
            return _owner_environment_refusal("missing_or_empty")
        if _UNRESOLVED_PLACEHOLDER.search(value):
            return _owner_environment_refusal("unresolved_placeholder")
        values[key] = value
    host = values["OPENCLAW_HOST"]
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return _owner_environment_refusal("host")
    try:
        port = int(values["OPENCLAW_PORT"])
    except ValueError:
        return _owner_environment_refusal("port")
    if not 1 <= port <= 65535:
        return _owner_environment_refusal("port")
    core_database = Path(values["RESEARCH_CORE_DATABASE"])
    root = Path(values["RESEARCH_V2_ROOT"])
    if not core_database.is_absolute() or core_database.is_symlink():
        return _owner_environment_refusal("core_database_path")
    if not root.is_absolute() or root.is_symlink():
        return _owner_environment_refusal("research_root_path")
    for key, value in values.items():
        process_value = os.environ.get(key)
        if process_value is None:
            continue
        if key in {"RESEARCH_CORE_DATABASE", "RESEARCH_V2_ROOT"}:
            same = Path(process_value).expanduser().resolve(strict=False) == Path(value).resolve(
                strict=False
            )
        else:
            same = process_value == value
        if not same:
            return _owner_environment_refusal("contract_mismatch")
    return _OwnerEnvironment(values)


def _load_owner_environment() -> _OwnerEnvironment:
    """Load only the explicitly deployed protected owner environment file."""
    path_text = os.environ.get(_OWNER_ENV_FILE)
    if not path_text:
        return _owner_environment_refusal("file_unset")
    raw, refusal = _read_protected_owner_env(path_text)
    if refusal is not None:
        return _owner_environment_refusal(f"file_{refusal}")
    if raw is None:
        return _owner_environment_refusal("file_unreadable")
    return _parse_owner_environment(raw)


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
        status_unavailable_reason: str | None = None,
    ) -> None:
        self.root = resolve_research_root(root)
        self.owner_unit = owner_unit
        self._unit = unit or SystemdOwnerUnit()
        self._review_canceller = review_canceller
        self._job_cancel = job_cancel
        self._status_reader = status_reader
        self._readiness_gate = readiness_gate
        self._status_unavailable_reason = status_unavailable_reason

    def status(self) -> ResearchStatus:
        if self._status_unavailable_reason is not None:
            return unavailable_status(self._status_unavailable_reason)
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


def _valid_core_database(value: str) -> tuple[Path | None, str | None]:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        return None, "core_database_path"
    try:
        path_stat = path.lstat()
    except OSError:
        return None, "core_database_missing"
    if not stat.S_ISREG(path_stat.st_mode):
        return None, "core_database_not_regular"
    if stat.S_IMODE(path_stat.st_mode) & 0o022:
        return None, "core_database_permissions"
    return path, None


def production_owner_control(root: Path | None = None) -> OwnerControl:
    """Construct production control from one protected deployment contract."""
    environment = _load_owner_environment()
    values = environment.values
    configured_root: Path | None = root
    if configured_root is None and "RESEARCH_V2_ROOT" in values:
        configured_root = Path(values["RESEARCH_V2_ROOT"])
    status_reason = None
    if configured_root is None:
        status_reason = environment.refusal or "owner_environment_unset"
        configured_root = Path("/nonexistent/g2-openclaw-owner")
    elif "RESEARCH_V2_ROOT" in values:
        expected_root = Path(values["RESEARCH_V2_ROOT"]).resolve(strict=False)
        if configured_root.resolve(strict=False) != expected_root:
            environment = _owner_environment_refusal("root_mismatch")

    core_database: Path | None = None
    if "RESEARCH_CORE_DATABASE" in values:
        core_database, database_refusal = _valid_core_database(values["RESEARCH_CORE_DATABASE"])
        if database_refusal is not None and environment.refusal is None:
            environment = _owner_environment_refusal(database_refusal)

    review_canceller: ReviewCanceller | None = None
    if environment.refusal is None and core_database is not None:
        client = OpenClawClient(
            values["OPENCLAW_HOST"],
            int(values["OPENCLAW_PORT"]),
            values["OPENCLAW_GATEWAY_TOKEN"],
        )
        review_canceller = OpenClawReviewCanceller(core_database, client.request_once)

    return OwnerControl(
        configured_root,
        review_canceller=review_canceller,
        readiness_gate=build_readiness_gate(
            configured_root,
            core_database=core_database,
            configuration_refusal=environment.refusal,
        ),
        status_unavailable_reason=status_reason,
    )
