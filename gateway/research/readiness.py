"""Evidence-backed readiness checks for the bounded research driver.

The checks in this module only consume operator-pinned records and read-only
host evidence.  They never synthesize a capability receipt or select a
provider/model.  A successful native check therefore means that a previously
registered receipt is structurally valid and still matches its bytes; it does
not manufacture route evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .containment import (
    ContainmentError,
    bwrap_argv,
    host_bus_environment,
    runtime_pins_from_record,
    verify_configured_runtime_pins,
    verify_runtime_pins,
    which_tools,
)
from .host_records import MAX_METADATA_BYTES, HostRecordError, _readonly_database
from .store import StoreConflict

if TYPE_CHECKING:
    from .store import ResearchStore

NATIVE_CAPABILITY_REGISTRATION = "native-capability-registration.json"
NATIVE_CAPABILITY_SCHEMA = "native-capability-v1"
NATIVE_CAPABILITY_MODEL = "gpt-5.6-luna"
NATIVE_CAPABILITY_EFFORT = "xhigh"
NATIVE_CAPABILITY_SERVICE_TIER = "fast"
NATIVE_CAPABILITY_IDENTITIES = frozenset({"implementer", "experiment_runner"})


def _record_refusal(store: ResearchStore, attempt_id: str | None, guard: str, reason: str) -> str:
    code = f"{guard}_{reason}"
    if attempt_id is not None:
        store.record_review_event(
            attempt_id,
            "execution_guard_refused",
            {"guard": guard, "reason": code},
        )
    return code


def _bounded_file(path: Path, label: str) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError(f"{label} must be an absolute non-symlink file")
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is missing") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_METADATA_BYTES:
        raise ValueError(f"{label} is not a bounded regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} is unreadable") from exc


def _sha256(value: str, label: str) -> str:
    if len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc
    return value


def _native_receipt(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("native capability receipt must be an object")
    expected = {"schema", "identity", "model", "effort", "service_tier"}
    if set(payload) != expected:
        raise ValueError("native capability receipt has unexpected fields")
    if payload.get("schema") != NATIVE_CAPABILITY_SCHEMA:
        raise ValueError("native capability receipt schema is unsupported")
    values: dict[str, str] = {}
    for field in ("identity", "model", "effort", "service_tier"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"native capability receipt {field} is required")
        values[field] = value
    if values["identity"] not in NATIVE_CAPABILITY_IDENTITIES:
        raise ValueError("native capability receipt identity is not an approved native role")
    if values["model"] != NATIVE_CAPABILITY_MODEL:
        raise ValueError("native capability receipt model is not the pinned Luna route")
    if values["effort"] != NATIVE_CAPABILITY_EFFORT:
        raise ValueError("native capability receipt effort is not xhigh")
    if values["service_tier"] != NATIVE_CAPABILITY_SERVICE_TIER:
        raise ValueError("native capability receipt service tier is not fast")
    return values


def _read_registration(root: Path) -> tuple[Path, dict[str, str]]:
    registration_path = root / NATIVE_CAPABILITY_REGISTRATION
    raw = _bounded_file(registration_path, "native capability registration")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("native capability registration is not canonical JSON") from exc
    canonical = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if raw != canonical:
        raise ValueError("native capability registration is not canonical JSON")
    if not isinstance(payload, dict) or set(payload) != {"path", "sha256"}:
        raise ValueError("native capability registration has unexpected fields")
    receipt_path = payload.get("path")
    digest = payload.get("sha256")
    if not isinstance(receipt_path, str) or not receipt_path:
        raise ValueError("native capability registration path is required")
    if not isinstance(digest, str):
        raise ValueError("native capability registration digest is required")
    return Path(receipt_path), {"sha256": _sha256(digest, "native capability registration SHA-256")}


def validate_native_capability_registration(root: Path) -> None:
    receipt_path, registration = _read_registration(root)
    receipt_bytes = _bounded_file(receipt_path, "native capability receipt")
    actual = hashlib.sha256(receipt_bytes).hexdigest()
    if actual != registration["sha256"]:
        raise ValueError("native capability receipt SHA-256 does not match registration")
    try:
        payload = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("native capability receipt is not canonical JSON") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if receipt_bytes != canonical:
        raise ValueError("native capability receipt is not canonical JSON")
    _native_receipt(payload)


def register_native_capability_receipt(root: Path, path: Path, sha256: str) -> tuple[str, str]:
    """Register an existing capability receipt without generating evidence."""
    root = root.expanduser()
    if not root.is_absolute():
        raise ValueError("--root must be an absolute path")
    _sha256(sha256, "native capability receipt SHA-256")
    receipt_path = path.expanduser()
    if not receipt_path.is_absolute():
        raise ValueError("--path must be an absolute path")
    receipt_bytes = _bounded_file(receipt_path, "native capability receipt")
    actual = hashlib.sha256(receipt_bytes).hexdigest()
    if actual != sha256:
        raise ValueError("native capability receipt SHA-256 does not match file bytes")
    try:
        payload = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("native capability receipt is not canonical JSON") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if receipt_bytes != canonical:
        raise ValueError("native capability receipt is not canonical JSON")
    _native_receipt(payload)
    root.mkdir(parents=True, exist_ok=True)
    registration = root / NATIVE_CAPABILITY_REGISTRATION
    if registration.exists() and registration.is_symlink():
        raise ValueError("native capability registration must not be a symlink")
    registration.write_text(
        json.dumps(
            {"path": str(receipt_path), "sha256": sha256},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return str(receipt_path), sha256


def _host_failure(store: ResearchStore, attempt_id: str | None, reason: str) -> str:
    return _record_refusal(store, attempt_id, "host", reason)


def host_execution_ready(store: ResearchStore, attempt_id: str | None) -> str | None:
    """Validate the immutable host containment boundary."""
    try:
        pins = runtime_pins_from_record(dict(store.config()))
        verify_configured_runtime_pins(pins)
        verify_runtime_pins(pins)
        tools = which_tools()
        if len(tools) != 2 or any(
            not Path(tool).is_file() or not os.access(tool, os.X_OK) for tool in tools
        ):
            return _host_failure(store, attempt_id, "tools_unavailable")
        environment = host_bus_environment()
        if set(environment) != {"PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"} or any(
            not isinstance(value, str) or not value for value in environment.values()
        ):
            return _host_failure(store, attempt_id, "bus_environment_unavailable")
        if attempt_id is not None:
            attempt = store.get_attempt(attempt_id)
            hypothesis = store.get_hypothesis(attempt.hypothesis_id)
            spec = store.root / "hypotheses" / hypothesis.hypothesis_id / "spec.json"
            bwrap_argv(
                pins,
                "validate",
                (str(pins.shared_python), "-c", "pass"),
                panel=Path(hypothesis.panel_path),
                receipt=Path(hypothesis.receipt_path),
                spec=spec,
                dividends=Path(hypothesis.dividends_path),
            )
        else:
            bwrap_argv(
                pins,
                "validate",
                (str(pins.shared_python), "-c", "pass"),
                panel=pins.evaluator,
                receipt=pins.universe,
                spec=pins.evaluator,
                dividends=pins.universe,
            )
    except (ContainmentError, KeyError, OSError, TypeError, ValueError) as exc:
        return _host_failure(store, attempt_id, type(exc).__name__.lower())
    return None


def _native_failure(store: ResearchStore, attempt_id: str | None, reason: str) -> str:
    return _record_refusal(store, attempt_id, "native", reason)


def native_execution_ready(
    store: ResearchStore,
    attempt_id: str | None,
    *,
    core_database: Path | None = None,
) -> str | None:
    """Validate the trusted core task surface and registered native receipt."""
    database_text = os.environ.get("RESEARCH_CORE_DATABASE") if core_database is None else None
    if core_database is not None:
        database = core_database
    elif not database_text:
        return _native_failure(store, attempt_id, "core_database_unset")
    else:
        database = Path(database_text)
    if not database.is_absolute() or database.is_symlink():
        return _native_failure(store, attempt_id, "core_database_invalid")
    try:
        mode = database.stat().st_mode
        if mode & 0o022:
            return _native_failure(store, attempt_id, "core_database_permissions")
        with _readonly_database(database) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        if not {"task_runs", "acp_sessions"}.issubset(tables):
            return _native_failure(store, attempt_id, "core_database_schema")
        validate_native_capability_registration(store.root)
    except (HostRecordError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        return _native_failure(store, attempt_id, type(exc).__name__.lower())
    return None


def _budget_failure(store: ResearchStore, attempt_id: str | None, reason: str) -> str:
    return _record_refusal(store, attempt_id, "budget", reason)


def budget_execution_ready(store: ResearchStore, attempt_id: str | None) -> str | None:
    """Require explicit operator policy and remaining bounded allocation."""
    try:
        policy = store.campaign_policy()
        if not policy.is_set or policy.attempt_cap is None:
            return _budget_failure(store, attempt_id, "campaign_policy_unset")
        attempts = sum(len(store.attempts_for(spec.hypothesis_id)) for spec in store.hypotheses())
        if attempts >= policy.attempt_cap:
            return _budget_failure(store, attempt_id, "attempt_cap_exceeded")
        if policy.wall_clock_cap_seconds is not None:
            row = store._campaign_row()
            set_at = row["policy_set_at"]
            if not isinstance(set_at, str) or not set_at:
                return _budget_failure(store, attempt_id, "policy_timestamp_missing")
            started = datetime.fromisoformat(set_at.replace("Z", "+00:00"))
            if started.tzinfo is None or started.utcoffset() != UTC.utcoffset(started):
                return _budget_failure(store, attempt_id, "policy_timestamp_invalid")
            elapsed = (datetime.now(UTC) - started).total_seconds()
            if elapsed >= policy.wall_clock_cap_seconds:
                return _budget_failure(store, attempt_id, "wall_clock_cap_exceeded")
    except (OSError, StoreConflict, TypeError, ValueError) as exc:
        return _budget_failure(store, attempt_id, type(exc).__name__.lower())
    return None


def build_readiness_gate(
    root: Path,
    *,
    core_database: Path | None = None,
    configuration_refusal: str | None = None,
) -> Callable[[object], str | None]:
    """Build the owner start gate over the shared research root."""

    def gate(_status: object) -> str | None:
        if configuration_refusal is not None:
            return configuration_refusal
        from .store import ResearchStore

        store = ResearchStore(root)
        for check in (
            host_execution_ready,
            lambda store, attempt_id: native_execution_ready(
                store, attempt_id, core_database=core_database
            ),
            budget_execution_ready,
        ):
            reason = check(store, None)
            if reason is not None:
                return reason
        return None

    return gate
