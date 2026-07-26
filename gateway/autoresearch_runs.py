"""Strict, secret-free records for detached autoresearch commands."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from gateway.autoresearch_runner import Phase

DEFAULT_AUTORESEARCH_RUNS_ROOT = Path("/home/dev/.openclaw/autoresearch/runs")
RUN_RECORD_SCHEMA_VERSION = 1
_CANONICAL_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_SECRET_NAME_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|auth|client[_-]?secret|credential|credentials|"
    r"pass(?:word|wd)?|private[_-]?key|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"AKIA[0-9A-Z]{16})"
)
_SECRET_REFERENCE_SUFFIXES = ("file", "path", "env", "var")
_COMMAND_HANDOFF_NAME = ".command-handoff.json"
_STATUS_LOCK_NAME = ".status.lock"
COMMAND_INPUT_STDIN_SCHEMA_VERSION = 1


class AutoresearchRunRecordError(ValueError):
    """A detached run record is malformed, unsafe, or does not match its manifest."""


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunFailureClassification(StrEnum):
    PROCESS_ERROR = "process_error"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    OPERATOR_STOPPED = "operator_stopped"
    TIMEOUT = "timeout"
    ARTIFACT_MISSING = "artifact_missing"


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AutoresearchRunRecordError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_keys(raw: dict[str, object], expected: tuple[str, ...], *, label: str) -> None:
    if set(raw) != set(expected):
        raise AutoresearchRunRecordError(f"{label} must contain exact keys")


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _CANONICAL_SHA256_RE.fullmatch(value) is None:
        raise AutoresearchRunRecordError(f"{label} must be a canonical lowercase SHA-256 digest")
    return value


def _require_non_empty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchRunRecordError(f"{label} must be a non-empty string")
    return value.strip()


def _require_absolute_path(value: object, *, label: str) -> str:
    text = _require_non_empty_string(value, label=label)
    if not Path(text).is_absolute():
        raise AutoresearchRunRecordError(f"{label} must be absolute")
    return str(Path(text).resolve(strict=False))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, *, label: str) -> str:
    text = _require_non_empty_string(value, label=label)
    if _CANONICAL_TIMESTAMP_RE.fullmatch(text) is None:
        raise AutoresearchRunRecordError(f"{label} must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutoresearchRunRecordError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    canonical = parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if text != canonical:
        raise AutoresearchRunRecordError(f"{label} must be a canonical UTC timestamp")
    return text


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def command_sha256(command: Sequence[str]) -> str:
    """Hash command argv without persisting its arguments."""
    if not command or any(not isinstance(argument, str) or not argument for argument in command):
        raise AutoresearchRunRecordError("command must contain non-empty string arguments")
    _reject_secret_bearing_command(command)
    return hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()


def _argument_name(argument: str) -> str:
    return argument.lstrip("-").split("=", 1)[0].replace("-", "_").lower()


def _secret_reference_name(name: str) -> bool:
    return name.endswith(_SECRET_REFERENCE_SUFFIXES) or any(
        name.endswith(f"_{suffix}") for suffix in _SECRET_REFERENCE_SUFFIXES
    )


def _is_secret_name(name: str) -> bool:
    return _SECRET_NAME_RE.search(name) is not None and not _secret_reference_name(name)


def _reject_secret_bearing_command(command: Sequence[str]) -> None:
    previous_requires_value = False
    for argument in command:
        if previous_requires_value:
            raise AutoresearchRunRecordError(
                "secret-bearing command arguments are forbidden; use credential files or "
                "inherited authentication"
            )
        previous_requires_value = False
        if _SECRET_VALUE_RE.search(argument) is not None:
            raise AutoresearchRunRecordError(
                "secret-looking command argument values are forbidden; use credential files or "
                "inherited authentication"
            )
        if "=" in argument:
            key, _value = argument.split("=", 1)
            if _is_secret_name(_argument_name(key)):
                raise AutoresearchRunRecordError(
                    "secret-bearing command arguments are forbidden; use credential files or "
                    "inherited authentication"
                )
            continue
        if argument.startswith("-") and _is_secret_name(_argument_name(argument)):
            previous_requires_value = True
    if previous_requires_value:
        raise AutoresearchRunRecordError(
            "secret-bearing command arguments are forbidden; use credential files or "
            "inherited authentication"
        )


@dataclass(frozen=True, slots=True)
class RunManifest:
    schema_version: int
    iteration: int
    phase: Phase
    attempt: int
    task_label: str
    state_reference_sha256: str
    instruction_manifest_sha256: str
    run_directory: str
    working_directory: str
    command_sha256: str
    expected_artifact_path: str | None
    timeout_seconds: float | None

    @classmethod
    def from_dict(cls, raw: object) -> RunManifest:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("manifest must be an object")
        _require_exact_keys(
            raw,
            (
                "schema_version",
                "iteration",
                "phase",
                "attempt",
                "task_label",
                "state_reference_sha256",
                "instruction_manifest_sha256",
                "run_directory",
                "working_directory",
                "command_sha256",
                "expected_artifact_path",
                "timeout_seconds",
            ),
            label="manifest",
        )
        schema_version = raw["schema_version"]
        iteration = raw["iteration"]
        attempt = raw["attempt"]
        if schema_version != RUN_RECORD_SCHEMA_VERSION:
            raise AutoresearchRunRecordError("manifest schema_version is unsupported")
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 1:
            raise AutoresearchRunRecordError("manifest iteration must be a positive integer")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise AutoresearchRunRecordError("manifest attempt must be a positive integer")
        expected_artifact = raw["expected_artifact_path"]
        if expected_artifact is not None:
            expected_artifact = _require_absolute_path(
                expected_artifact, label="expected_artifact_path"
            )
        timeout = raw["timeout_seconds"]
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
                raise AutoresearchRunRecordError(
                    "timeout_seconds must be a positive number or null"
                )
            timeout = float(timeout)
        try:
            phase = Phase(_require_non_empty_string(raw["phase"], label="phase"))
        except ValueError as exc:
            raise AutoresearchRunRecordError("manifest phase is unsupported") from exc
        return cls(
            schema_version=RUN_RECORD_SCHEMA_VERSION,
            iteration=iteration,
            phase=phase,
            attempt=attempt,
            task_label=_require_non_empty_string(raw["task_label"], label="task_label"),
            state_reference_sha256=_require_sha256(
                raw["state_reference_sha256"], label="state_reference_sha256"
            ),
            instruction_manifest_sha256=_require_sha256(
                raw["instruction_manifest_sha256"], label="instruction_manifest_sha256"
            ),
            run_directory=_require_absolute_path(raw["run_directory"], label="run_directory"),
            working_directory=_require_absolute_path(
                raw["working_directory"], label="working_directory"
            ),
            command_sha256=_require_sha256(raw["command_sha256"], label="command_sha256"),
            expected_artifact_path=expected_artifact,
            timeout_seconds=timeout,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "iteration": self.iteration,
            "phase": self.phase.value,
            "attempt": self.attempt,
            "task_label": self.task_label,
            "state_reference_sha256": self.state_reference_sha256,
            "instruction_manifest_sha256": self.instruction_manifest_sha256,
            "run_directory": self.run_directory,
            "working_directory": self.working_directory,
            "command_sha256": self.command_sha256,
            "expected_artifact_path": self.expected_artifact_path,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class RunResourceUsage:
    elapsed_seconds: float
    peak_rss_bytes: int | None

    @classmethod
    def from_dict(cls, raw: object) -> RunResourceUsage:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("resource_usage must be an object")
        _require_exact_keys(raw, ("elapsed_seconds", "peak_rss_bytes"), label="resource_usage")
        elapsed = raw["elapsed_seconds"]
        peak = raw["peak_rss_bytes"]
        if isinstance(elapsed, bool) or not isinstance(elapsed, int | float) or elapsed < 0:
            raise AutoresearchRunRecordError("elapsed_seconds must be non-negative")
        if peak is not None and (isinstance(peak, bool) or not isinstance(peak, int) or peak < 0):
            raise AutoresearchRunRecordError("peak_rss_bytes must be non-negative or null")
        return cls(elapsed_seconds=float(elapsed), peak_rss_bytes=peak)

    def to_dict(self) -> dict[str, object]:
        return {"elapsed_seconds": self.elapsed_seconds, "peak_rss_bytes": self.peak_rss_bytes}


@dataclass(frozen=True, slots=True)
class RunStatus:
    schema_version: int
    manifest_sha256: str
    state: RunState
    pid: int | None
    systemd_unit: str | None
    updated_at: str
    started_at: str
    finished_at: str | None
    exit_code: int | None
    signal_number: int | None
    failure_classification: RunFailureClassification | None
    resource_usage: RunResourceUsage

    @classmethod
    def from_dict(cls, raw: object) -> RunStatus:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("status must be an object")
        _require_exact_keys(
            raw,
            (
                "schema_version",
                "manifest_sha256",
                "state",
                "pid",
                "systemd_unit",
                "updated_at",
                "started_at",
                "finished_at",
                "exit_code",
                "signal_number",
                "failure_classification",
                "resource_usage",
            ),
            label="status",
        )
        if raw["schema_version"] != RUN_RECORD_SCHEMA_VERSION:
            raise AutoresearchRunRecordError("status schema_version is unsupported")
        pid = raw["pid"]
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid < 1):
            raise AutoresearchRunRecordError("pid must be a positive integer or null")
        systemd_unit_raw = raw["systemd_unit"]
        systemd_unit = (
            _require_non_empty_string(systemd_unit_raw, label="systemd_unit")
            if systemd_unit_raw is not None
            else None
        )
        exit_code = raw["exit_code"]
        signal_number = raw["signal_number"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0
        ):
            raise AutoresearchRunRecordError("exit_code must be a non-negative integer or null")
        if signal_number is not None and (
            isinstance(signal_number, bool)
            or not isinstance(signal_number, int)
            or signal_number < 1
        ):
            raise AutoresearchRunRecordError("signal_number must be a positive integer or null")
        try:
            state = RunState(_require_non_empty_string(raw["state"], label="state"))
        except ValueError as exc:
            raise AutoresearchRunRecordError("status state is invalid") from exc
        failure_raw = raw["failure_classification"]
        try:
            failure = RunFailureClassification(failure_raw) if failure_raw is not None else None
        except ValueError as exc:
            raise AutoresearchRunRecordError("failure_classification is invalid") from exc
        started_at = _parse_timestamp(raw["started_at"], label="started_at")
        updated_at = _parse_timestamp(raw["updated_at"], label="updated_at")
        if _timestamp_value(updated_at) < _timestamp_value(started_at):
            raise AutoresearchRunRecordError("updated_at cannot precede started_at")
        finished_at = raw["finished_at"]
        if finished_at is not None:
            finished_at = _parse_timestamp(finished_at, label="finished_at")
            if _timestamp_value(finished_at) < _timestamp_value(started_at):
                raise AutoresearchRunRecordError("finished_at cannot precede started_at")
            if _timestamp_value(updated_at) < _timestamp_value(finished_at):
                raise AutoresearchRunRecordError("updated_at cannot precede finished_at")
        if state is RunState.RUNNING:
            if pid is None:
                raise AutoresearchRunRecordError("running status requires a pid")
            if any(value is not None for value in (finished_at, exit_code, signal_number, failure)):
                raise AutoresearchRunRecordError("running status cannot contain terminal evidence")
        elif pid is None or finished_at is None or exit_code is None:
            raise AutoresearchRunRecordError("terminal status requires finished_at and exit_code")
        elif state is RunState.SUCCEEDED and (
            exit_code != 0 or signal_number is not None or failure is not None
        ):
            raise AutoresearchRunRecordError("succeeded status must have only zero exit evidence")
        elif state is RunState.FAILED and failure is None:
            raise AutoresearchRunRecordError("failed status requires a failure classification")
        return cls(
            schema_version=RUN_RECORD_SCHEMA_VERSION,
            manifest_sha256=_require_sha256(raw["manifest_sha256"], label="manifest_sha256"),
            state=state,
            pid=pid,
            systemd_unit=systemd_unit,
            updated_at=updated_at,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            signal_number=signal_number,
            failure_classification=failure,
            resource_usage=RunResourceUsage.from_dict(raw["resource_usage"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_sha256": self.manifest_sha256,
            "state": self.state.value,
            "pid": self.pid,
            "systemd_unit": self.systemd_unit,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "signal_number": self.signal_number,
            "failure_classification": self.failure_classification.value
            if self.failure_classification is not None
            else None,
            "resource_usage": self.resource_usage.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PreparedRun:
    manifest: RunManifest
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    manifest: RunManifest
    status: RunStatus
    run_directory: Path


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _manifest_digest(manifest: RunManifest) -> str:
    return hashlib.sha256(_canonical_json(manifest.to_dict())).hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    _reject_symlink(path, label=label)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except FileNotFoundError as exc:
        raise AutoresearchRunRecordError(f"missing {label}: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoresearchRunRecordError(f"invalid {label}: {path}") from exc
    if not isinstance(raw, dict):
        raise AutoresearchRunRecordError(f"{label} must be an object")
    return raw


def _read_private_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise AutoresearchRunRecordError(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AutoresearchRunRecordError(f"{label} must be a regular file")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AutoresearchRunRecordError(f"{label} must be owned by this user with mode 0600")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            try:
                raw = json.loads(
                    handle.read().decode("utf-8"),
                    object_pairs_hook=_strict_object,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AutoresearchRunRecordError(f"invalid {label}: {path}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise AutoresearchRunRecordError(f"{label} must be an object")
    return raw


def _reject_symlink(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise AutoresearchRunRecordError(f"{label} must not be a symlink: {path}")


def _validate_run_directory(run_dir: Path, runs_root: Path) -> Path:
    canonical_root = runs_root.resolve(strict=False)
    canonical_run_dir = run_dir.resolve(strict=False)
    try:
        canonical_run_dir.relative_to(canonical_root)
    except ValueError as exc:
        raise AutoresearchRunRecordError(
            "run directory must be under the canonical runs root"
        ) from exc
    current = canonical_run_dir
    while current != canonical_root:
        _reject_symlink(current, label="run directory")
        current = current.parent
    _reject_symlink(canonical_root, label="runs root")
    return canonical_run_dir


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AutoresearchRunRecordError(f"failed to atomically write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()


@contextmanager
def _status_lock(run_dir: Path) -> Iterator[None]:
    lock_path = run_dir / _STATUS_LOCK_NAME
    _reject_symlink(lock_path, label="status lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot open status lock: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _command_handoff_path(run_dir: Path) -> Path:
    return run_dir / _COMMAND_HANDOFF_NAME


def _require_private_regular_file(path: Path, *, label: str) -> None:
    _reject_symlink(path, label=label)
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise AutoresearchRunRecordError(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AutoresearchRunRecordError(f"{label} must be a regular file")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise AutoresearchRunRecordError(f"{label} must be owned by this user with mode 0600")


def _parse_command_input(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        raise AutoresearchRunRecordError(f"{label} must be an object")
    _require_exact_keys(raw, ("command",), label=label)
    command_raw = raw["command"]
    if not isinstance(command_raw, list):
        raise AutoresearchRunRecordError(f"{label} command must be a list")
    command = tuple(command_raw)
    command_sha256(command)
    return command


def _parse_command_stdin(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        raise AutoresearchRunRecordError("command stdin protocol must be an object")
    _require_exact_keys(raw, ("schema_version", "command"), label="command stdin protocol")
    if raw["schema_version"] != COMMAND_INPUT_STDIN_SCHEMA_VERSION:
        raise AutoresearchRunRecordError("command stdin protocol schema_version is unsupported")
    return _parse_command_input({"command": raw["command"]}, label="command stdin protocol")


def create_command_input_file_from_stdin(*, output_path: Path, payload: bytes) -> None:
    """Create a one-time private command file from the exact stdin protocol."""
    if not output_path.is_absolute():
        raise AutoresearchRunRecordError("command output path must be absolute")
    _reject_symlink(output_path, label="command output")
    parent = output_path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot prepare command output directory: {exc}") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        raise AutoresearchRunRecordError("command output directory must be a non-symlink directory")
    if parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise AutoresearchRunRecordError(
            "command output directory must be owned by this user and not group/world accessible"
        )
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutoresearchRunRecordError("invalid command stdin protocol JSON") from exc
    command = _parse_command_stdin(raw)
    encoded = _canonical_json({"command": list(command)})
    descriptor = -1
    try:
        descriptor = os.open(
            output_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise AutoresearchRunRecordError("command output file already exists") from exc
    except OSError as exc:
        with suppress(FileNotFoundError):
            output_path.unlink()
        raise AutoresearchRunRecordError(f"cannot create command output file: {exc}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    _require_private_regular_file(output_path, label="command output")


def consume_command_input_file(path: Path) -> tuple[str, ...]:
    raw = _read_private_json(path, label="command input")
    command = _parse_command_input(raw, label="command input")
    try:
        path.unlink()
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot remove one-time command input: {exc}") from exc
    return command


def prepare_run(
    *,
    manifest_path: Path,
    run_dir: Path,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
    command: Sequence[str],
) -> PreparedRun:
    canonical_run_dir = _validate_run_directory(run_dir, runs_root)
    _reject_symlink(manifest_path, label="source manifest")
    manifest = RunManifest.from_dict(_read_json(manifest_path, label="source manifest"))
    if manifest.run_directory != str(canonical_run_dir):
        raise AutoresearchRunRecordError("manifest run_directory does not match --run-dir")
    if manifest.command_sha256 != command_sha256(command):
        raise AutoresearchRunRecordError("manifest command_sha256 does not match command")
    working_directory = Path(manifest.working_directory)
    _reject_symlink(working_directory, label="manifest working_directory")
    if not working_directory.is_dir():
        raise AutoresearchRunRecordError("manifest working_directory must be a directory")
    try:
        canonical_run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise AutoresearchRunRecordError("run directory already exists") from exc
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot create run directory: {exc}") from exc
    manifest_file = canonical_run_dir / "manifest.json"
    _atomic_write(manifest_file, _canonical_json(manifest.to_dict()), mode=0o400)
    os.chmod(manifest_file, 0o400)
    return PreparedRun(manifest=manifest, manifest_sha256=_manifest_digest(manifest))


def write_command_handoff(
    *,
    run_dir: Path,
    command: Sequence[str],
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> None:
    canonical_run_dir, manifest, _digest = _load_manifest(run_dir, runs_root)
    if manifest.command_sha256 != command_sha256(command):
        raise AutoresearchRunRecordError("manifest command_sha256 does not match command")
    handoff_path = _command_handoff_path(canonical_run_dir)
    with _status_lock(canonical_run_dir):
        if (canonical_run_dir / "status.json").exists():
            raise AutoresearchRunRecordError("cannot create command handoff after startup")
        if handoff_path.exists() or handoff_path.is_symlink():
            raise AutoresearchRunRecordError("command handoff already exists")
        _atomic_write(handoff_path, _canonical_json({"command": list(command)}), mode=0o600)
        _require_private_regular_file(handoff_path, label="command handoff")


def consume_command_handoff(
    *, run_dir: Path, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> tuple[str, ...]:
    canonical_run_dir, manifest, _digest = _load_manifest(run_dir, runs_root)
    handoff_path = _command_handoff_path(canonical_run_dir)
    with _status_lock(canonical_run_dir):
        if (canonical_run_dir / "status.json").exists():
            raise AutoresearchRunRecordError("cannot consume command handoff after startup")
        _require_private_regular_file(handoff_path, label="command handoff")
        raw = _read_private_json(handoff_path, label="command handoff")
        command = _parse_command_input(raw, label="command handoff")
        if manifest.command_sha256 != command_sha256(command):
            raise AutoresearchRunRecordError("command handoff does not match manifest digest")
        try:
            handoff_path.unlink()
        except OSError as exc:
            raise AutoresearchRunRecordError(f"cannot remove command handoff: {exc}") from exc
    return command


def prepare_run_with_command_file(
    *,
    manifest_path: Path,
    run_dir: Path,
    command_file: Path,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> PreparedRun:
    command = consume_command_input_file(command_file)
    prepared = prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=command,
    )
    write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)
    return prepared


def _load_manifest(run_dir: Path, runs_root: Path) -> tuple[Path, RunManifest, str]:
    canonical_run_dir = _validate_run_directory(run_dir, runs_root)
    manifest = RunManifest.from_dict(
        _read_json(canonical_run_dir / "manifest.json", label="manifest")
    )
    if manifest.run_directory != str(canonical_run_dir):
        raise AutoresearchRunRecordError("manifest run_directory does not match record directory")
    return canonical_run_dir, manifest, _manifest_digest(manifest)


def start_run(
    *,
    run_dir: Path,
    pid: int | None,
    systemd_unit: str | None = None,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> RunStatus:
    canonical_run_dir, _manifest, digest = _load_manifest(run_dir, runs_root)
    if pid is None or isinstance(pid, bool) or pid < 1:
        raise AutoresearchRunRecordError("startup requires a positive pid")
    with _status_lock(canonical_run_dir):
        status_path = canonical_run_dir / "status.json"
        if status_path.exists() or status_path.is_symlink():
            raise AutoresearchRunRecordError("startup status already exists")
        now = _utc_now()
        status = RunStatus(
            schema_version=RUN_RECORD_SCHEMA_VERSION,
            manifest_sha256=digest,
            state=RunState.RUNNING,
            pid=pid,
            systemd_unit=systemd_unit,
            updated_at=now,
            started_at=now,
            finished_at=None,
            exit_code=None,
            signal_number=None,
            failure_classification=None,
            resource_usage=RunResourceUsage(elapsed_seconds=0.0, peak_rss_bytes=None),
        )
        _atomic_write(status_path, _canonical_json(status.to_dict()), mode=0o600)
        return status


def _current_status(run_dir: Path, runs_root: Path) -> tuple[Path, RunManifest, str, RunStatus]:
    canonical_run_dir, manifest, digest = _load_manifest(run_dir, runs_root)
    status = RunStatus.from_dict(_read_json(canonical_run_dir / "status.json", label="status"))
    if status.manifest_sha256 != digest:
        raise AutoresearchRunRecordError("status manifest_sha256 does not match manifest")
    return canonical_run_dir, manifest, digest, status


def heartbeat_run(
    *, run_dir: Path, peak_rss_bytes: int | None, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> RunStatus:
    canonical_run_dir = _validate_run_directory(run_dir, runs_root)
    with _status_lock(canonical_run_dir):
        canonical_run_dir, _manifest, _digest, status = _current_status(run_dir, runs_root)
        if status.state is not RunState.RUNNING:
            raise AutoresearchRunRecordError("cannot heartbeat a terminal run")
        current_peak = status.resource_usage.peak_rss_bytes
        next_peak = (
            max(value for value in (current_peak, peak_rss_bytes) if value is not None)
            if (current_peak is not None or peak_rss_bytes is not None)
            else None
        )
        updated = replace(
            status,
            updated_at=_utc_now(),
            resource_usage=RunResourceUsage(
                elapsed_seconds=max(
                    0.0, time.time() - _timestamp_value(status.started_at).timestamp()
                ),
                peak_rss_bytes=next_peak,
            ),
        )
        _atomic_write(
            canonical_run_dir / "status.json", _canonical_json(updated.to_dict()), mode=0o600
        )
        return updated


def _infer_failure(
    exit_code: int, signal_number: int | None, *, timed_out: bool
) -> RunFailureClassification:
    if timed_out:
        return RunFailureClassification.TIMEOUT
    return RunFailureClassification.PROCESS_ERROR


def complete_run(
    *,
    run_dir: Path,
    exit_code: int,
    signal_number: int | None,
    peak_rss_bytes: int | None,
    timed_out: bool = False,
    failure_classification: RunFailureClassification | None = None,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> RunStatus:
    canonical_run_dir = _validate_run_directory(run_dir, runs_root)
    with _status_lock(canonical_run_dir):
        canonical_run_dir, _manifest, digest, previous = _current_status(run_dir, runs_root)
        if previous.state is not RunState.RUNNING:
            raise AutoresearchRunRecordError("cannot complete a terminal run")
        now = _utc_now()
        succeeded = (
            exit_code == 0
            and signal_number is None
            and not timed_out
            and failure_classification is None
        )
        peak = (
            max(
                value
                for value in (previous.resource_usage.peak_rss_bytes, peak_rss_bytes)
                if value is not None
            )
            if (previous.resource_usage.peak_rss_bytes is not None or peak_rss_bytes is not None)
            else None
        )
        status = RunStatus(
            schema_version=RUN_RECORD_SCHEMA_VERSION,
            manifest_sha256=digest,
            state=RunState.SUCCEEDED if succeeded else RunState.FAILED,
            pid=previous.pid,
            systemd_unit=previous.systemd_unit,
            updated_at=now,
            started_at=previous.started_at,
            finished_at=now,
            exit_code=exit_code,
            signal_number=signal_number,
            failure_classification=(
                None
                if succeeded
                else failure_classification
                or _infer_failure(exit_code, signal_number, timed_out=timed_out)
            ),
            resource_usage=RunResourceUsage(
                elapsed_seconds=max(
                    0.0, time.time() - _timestamp_value(previous.started_at).timestamp()
                ),
                peak_rss_bytes=peak,
            ),
        )
        _atomic_write(
            canonical_run_dir / "status.json", _canonical_json(status.to_dict()), mode=0o600
        )
        return status


def read_run_record(
    *, run_dir: Path, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> RunRecord:
    canonical_run_dir = _validate_run_directory(run_dir, runs_root)
    with _status_lock(canonical_run_dir):
        canonical_run_dir, manifest, _digest, status = _current_status(run_dir, runs_root)
        return RunRecord(manifest=manifest, status=status, run_directory=canonical_run_dir)


def validate_startup_marker(
    *,
    run_dir: Path,
    marker_path: Path,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> None:
    """Require a startup receipt to bind the live record to one manifest and pid."""
    record = read_run_record(run_dir=run_dir, runs_root=runs_root)
    marker = RunStatus.from_dict(_read_json(marker_path, label="startup marker"))
    if marker.state is not RunState.RUNNING:
        raise AutoresearchRunRecordError("startup marker must record a running state")
    if (
        marker.manifest_sha256 != _manifest_digest(record.manifest)
        or marker.pid != record.status.pid
        or marker.started_at != record.status.started_at
    ):
        raise AutoresearchRunRecordError("startup marker does not bind the live run identity")


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    create_file = subparsers.add_parser("create-command-file")
    create_file.add_argument("--output", type=Path, required=True)
    prepare_file = subparsers.add_parser("prepare-with-command-file")
    prepare_file.add_argument("--manifest", type=Path, required=True)
    prepare_file.add_argument("--run-dir", type=Path, required=True)
    prepare_file.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    prepare_file.add_argument("--command-file", type=Path, required=True)
    consume = subparsers.add_parser("consume-command-handoff")
    consume.add_argument("--run-dir", type=Path, required=True)
    consume.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    startup = subparsers.add_parser("validate-startup")
    startup.add_argument("--run-dir", type=Path, required=True)
    startup.add_argument("--marker", type=Path, required=True)
    startup.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    start = subparsers.add_parser("start")
    start.add_argument("--run-dir", type=Path, required=True)
    start.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    start.add_argument("--pid", type=int, default=None)
    start.add_argument("--systemd-unit", default=None)
    heartbeat = subparsers.add_parser("heartbeat")
    heartbeat.add_argument("--run-dir", type=Path, required=True)
    heartbeat.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    heartbeat.add_argument("--peak-rss-bytes", type=int, default=None)
    complete = subparsers.add_parser("complete")
    complete.add_argument("--run-dir", type=Path, required=True)
    complete.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    complete.add_argument("--exit-code", type=int, required=True)
    complete.add_argument("--signal-number", type=int, default=None)
    complete.add_argument("--peak-rss-bytes", type=int, default=None)
    complete.add_argument("--timed-out", action="store_true")
    complete.add_argument("--artifact-missing", action="store_true")
    failure_group = complete.add_mutually_exclusive_group()
    failure_group.add_argument("--resource-exhausted", action="store_true")
    failure_group.add_argument("--operator-stopped", action="store_true")
    args = parser.parse_args()
    if args.operation == "create-command-file":
        create_command_input_file_from_stdin(
            output_path=args.output,
            payload=sys.stdin.buffer.read(),
        )
    elif args.operation == "prepare-with-command-file":
        prepare_run_with_command_file(
            manifest_path=args.manifest,
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            command_file=args.command_file,
        )
    elif args.operation == "consume-command-handoff":
        command = consume_command_handoff(run_dir=args.run_dir, runs_root=args.runs_root)
        sys.stdout.buffer.write(b"\0".join(argument.encode("utf-8") for argument in command))
    elif args.operation == "validate-startup":
        validate_startup_marker(
            run_dir=args.run_dir, marker_path=args.marker, runs_root=args.runs_root
        )
    elif args.operation == "start":
        start_run(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            pid=args.pid,
            systemd_unit=args.systemd_unit,
        )
    elif args.operation == "heartbeat":
        heartbeat_run(
            run_dir=args.run_dir, runs_root=args.runs_root, peak_rss_bytes=args.peak_rss_bytes
        )
    else:
        complete_run(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            exit_code=args.exit_code,
            signal_number=args.signal_number,
            peak_rss_bytes=args.peak_rss_bytes,
            timed_out=args.timed_out,
            failure_classification=(
                RunFailureClassification.ARTIFACT_MISSING
                if args.artifact_missing
                else RunFailureClassification.RESOURCE_EXHAUSTED
                if args.resource_exhausted
                else RunFailureClassification.OPERATOR_STOPPED
                if args.operator_stopped
                else None
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
