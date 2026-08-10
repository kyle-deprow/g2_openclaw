"""Strict, secret-free records for detached autoresearch commands."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import BinaryIO

from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_LONG_RUNS_ROOT,
)
from gateway.autoresearch.enums import Phase

DEFAULT_AUTORESEARCH_RUNS_ROOT = DEFAULT_AUTORESEARCH_LONG_RUNS_ROOT
RUN_RECORD_SCHEMA_VERSION = 1
RUN_STATUS_SCHEMA_VERSION = 5
_HISTORIC_RUN_STATUS_SCHEMA_VERSION = 1
_PREVIOUS_RUN_STATUS_SCHEMA_VERSION = 2
_CAPTURE_RUN_STATUS_SCHEMA_VERSION = 3
_ARTIFACT_ATTESTATION_RUN_STATUS_SCHEMA_VERSION = 4
OUTPUT_CAPTURE_MAX_BYTES = 64 * 1024
EXPECTED_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024
_OUTPUT_CAPTURE_FINAL_DRAIN_SECONDS = 0.25
_OUTPUT_CAPTURE_INCOMPLETE_EXIT_CODE = 3
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
    r"(?:\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9_]{16,}|\bxox[baprs]-[A-Za-z0-9-]{16,}|"
    r"\bAKIA[0-9A-Z]{16})"
)
_SECRET_REFERENCE_SUFFIXES = ("file", "path", "env", "var")
_COMMAND_HANDOFF_NAME = ".command-handoff.json"
_STATUS_LOCK_NAME = ".status.lock"
COMMAND_INPUT_STDIN_SCHEMA_VERSION = 1
_OUTPUT_CAPTURE_FILE_NAMES = {
    "stdout": "stdout.log",
    "stderr": "stderr.log",
}
_OUTPUT_CAPTURE_RECEIPT_SUFFIX = ".capture.json"
_OUTPUT_CAPTURE_COMPLETION_MARKER_NAME = ".capture-completion"
_SUPERVISED_COMMAND_RESULT_NAME = ".command-result.json"
_STARTUP_MARKER_NAME = ".startup-published.json"
_TIMEOUT_MARKER_NAME = ".timeout-fired"
_OPERATOR_STOP_MARKER_NAME = ".operator-stop-fired"
_SUPERVISOR_POLL_SECONDS = 0.05
_SUPERVISED_COMMAND_RESULT_SCHEMA_VERSION = 1


class AutoresearchRunRecordError(ValueError):
    """A detached run record is malformed, unsafe, or does not match its manifest."""


class ExpectedArtifactAttestationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    ATTESTED = "attested"
    FAILED = "failed"
    HISTORIC_UNKNOWN = "historic_unknown"


class ExpectedArtifactAttestationError(StrEnum):
    MISSING = "missing"
    SYMLINK = "symlink"
    UNSAFE_ANCESTOR = "unsafe_ancestor"
    NOT_REGULAR = "not_regular"
    WRONG_OWNER = "wrong_owner"
    WRONG_MODE = "wrong_mode"
    HARD_LINK = "hard_link"
    OVERSIZED = "oversized"
    CHANGED_DURING_READ = "changed_during_read"
    IO_ERROR = "io_error"


class _ExpectedArtifactAttestationFailure(AutoresearchRunRecordError):
    def __init__(self, reason: ExpectedArtifactAttestationError, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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
    OUTPUT_CAPTURE_ERROR = "output_capture_error"


class RunOutputStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


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


def _require_canonical_absolute_path(value: object, *, label: str) -> str:
    text = _require_non_empty_string(value, label=label)
    path = Path(text)
    if (
        not path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != text
    ):
        raise AutoresearchRunRecordError(f"{label} must be a canonical absolute path")
    return text


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
            expected_artifact = _require_canonical_absolute_path(
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
class RunOutputStreamCapture:
    relative_path: str
    bytes_observed: int
    bytes_stored: int
    sha256: str
    truncated: bool
    eof_observed: bool

    @classmethod
    def from_dict(cls, raw: object, *, label: str) -> RunOutputStreamCapture:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError(f"{label} must be an object")
        _require_exact_keys(
            raw,
            (
                "relative_path",
                "bytes_observed",
                "bytes_stored",
                "sha256",
                "truncated",
                "eof_observed",
            ),
            label=label,
        )
        relative_path = _require_non_empty_string(raw["relative_path"], label=f"{label} path")
        path = Path(relative_path)
        if path.is_absolute() or len(path.parts) != 1 or path.name != relative_path:
            raise AutoresearchRunRecordError(
                f"{label} path must be a file name relative to the run"
            )
        observed = raw["bytes_observed"]
        stored = raw["bytes_stored"]
        truncated = raw["truncated"]
        eof_observed = raw["eof_observed"]
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise AutoresearchRunRecordError(
                f"{label} bytes_observed must be a non-negative integer"
            )
        if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
            raise AutoresearchRunRecordError(f"{label} bytes_stored must be a non-negative integer")
        if stored > OUTPUT_CAPTURE_MAX_BYTES:
            raise AutoresearchRunRecordError(
                f"{label} bytes_stored exceeds the fixed capture limit"
            )
        if observed < stored:
            raise AutoresearchRunRecordError(
                f"{label} bytes_observed cannot be less than bytes_stored"
            )
        if not isinstance(truncated, bool) or truncated != (observed > stored):
            raise AutoresearchRunRecordError(
                f"{label} truncated must match the observed byte count"
            )
        if not isinstance(eof_observed, bool):
            raise AutoresearchRunRecordError(f"{label} eof_observed must be a boolean")
        return cls(
            relative_path=relative_path,
            bytes_observed=observed,
            bytes_stored=stored,
            sha256=_require_sha256(raw["sha256"], label=f"{label} sha256"),
            truncated=truncated,
            eof_observed=eof_observed,
        )

    @classmethod
    def from_schema_v2_dict(cls, raw: object, *, label: str) -> RunOutputStreamCapture:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError(f"{label} must be an object")
        _require_exact_keys(
            raw,
            ("relative_path", "bytes_observed", "bytes_stored", "sha256", "truncated"),
            label=f"schema-v2 {label}",
        )
        return cls.from_dict({**raw, "eof_observed": False}, label=label)

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "bytes_observed": self.bytes_observed,
            "bytes_stored": self.bytes_stored,
            "sha256": self.sha256,
            "truncated": self.truncated,
            "eof_observed": self.eof_observed,
        }


@dataclass(frozen=True, slots=True)
class RunOutputCapture:
    stdout: RunOutputStreamCapture
    stderr: RunOutputStreamCapture

    @classmethod
    def from_dict(cls, raw: object) -> RunOutputCapture:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("output_capture must be an object")
        _require_exact_keys(raw, ("stdout", "stderr"), label="output_capture")
        stdout = RunOutputStreamCapture.from_dict(raw["stdout"], label="stdout capture")
        stderr = RunOutputStreamCapture.from_dict(raw["stderr"], label="stderr capture")
        if stdout.relative_path != _OUTPUT_CAPTURE_FILE_NAMES[RunOutputStream.STDOUT]:
            raise AutoresearchRunRecordError("stdout capture path is invalid")
        if stderr.relative_path != _OUTPUT_CAPTURE_FILE_NAMES[RunOutputStream.STDERR]:
            raise AutoresearchRunRecordError("stderr capture path is invalid")
        return cls(stdout=stdout, stderr=stderr)

    @classmethod
    def from_schema_v2_dict(cls, raw: object) -> RunOutputCapture:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("schema-v2 output_capture must be an object")
        _require_exact_keys(raw, ("stdout", "stderr"), label="schema-v2 output_capture")
        return cls(
            stdout=RunOutputStreamCapture.from_schema_v2_dict(
                raw["stdout"],
                label="stdout capture",
            ),
            stderr=RunOutputStreamCapture.from_schema_v2_dict(
                raw["stderr"],
                label="stderr capture",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"stdout": self.stdout.to_dict(), "stderr": self.stderr.to_dict()}


@dataclass(frozen=True, slots=True)
class RunExpectedArtifactAttestation:
    path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_dict(cls, raw: object) -> RunExpectedArtifactAttestation:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("expected_artifact_attestation must be an object")
        _require_exact_keys(
            raw,
            (
                "path",
                "size_bytes",
                "sha256",
                "device",
                "inode",
                "mtime_ns",
                "ctime_ns",
            ),
            label="expected_artifact_attestation",
        )
        integer_fields: dict[str, int] = {}
        for field in ("size_bytes", "device", "inode", "mtime_ns", "ctime_ns"):
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AutoresearchRunRecordError(
                    f"expected_artifact_attestation {field} must be a non-negative integer"
                )
            integer_fields[field] = value
        if integer_fields["size_bytes"] > EXPECTED_ARTIFACT_MAX_BYTES:
            raise AutoresearchRunRecordError(
                "expected_artifact_attestation size_bytes exceeds the fixed limit"
            )
        return cls(
            path=_require_canonical_absolute_path(
                raw["path"],
                label="expected_artifact_attestation path",
            ),
            size_bytes=integer_fields["size_bytes"],
            sha256=_require_sha256(
                raw["sha256"],
                label="expected_artifact_attestation sha256",
            ),
            device=integer_fields["device"],
            inode=integer_fields["inode"],
            mtime_ns=integer_fields["mtime_ns"],
            ctime_ns=integer_fields["ctime_ns"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }


@dataclass(frozen=True, slots=True)
class _OutputCaptureReceipt:
    bytes_observed: int
    eof_observed: bool


@dataclass(frozen=True, slots=True)
class _SupervisedCommandResult:
    exit_code: int
    signal_number: int | None

    @classmethod
    def from_dict(cls, raw: object) -> _SupervisedCommandResult:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("supervised command result must be an object")
        _require_exact_keys(
            raw,
            ("schema_version", "exit_code", "signal_number"),
            label="supervised command result",
        )
        if raw["schema_version"] != _SUPERVISED_COMMAND_RESULT_SCHEMA_VERSION:
            raise AutoresearchRunRecordError(
                "supervised command result schema_version is unsupported"
            )
        exit_code = raw["exit_code"]
        signal_number = raw["signal_number"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
            raise AutoresearchRunRecordError(
                "supervised command result exit_code must be non-negative"
            )
        if signal_number is not None and (
            isinstance(signal_number, bool)
            or not isinstance(signal_number, int)
            or signal_number < 1
        ):
            raise AutoresearchRunRecordError(
                "supervised command result signal_number must be positive or null"
            )
        if signal_number is None and exit_code > 255:
            raise AutoresearchRunRecordError(
                "supervised command result exit_code must fit shell status"
            )
        if signal_number is not None and exit_code != 128 + signal_number:
            raise AutoresearchRunRecordError(
                "supervised command result signal evidence is inconsistent"
            )
        return cls(exit_code=exit_code, signal_number=signal_number)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SUPERVISED_COMMAND_RESULT_SCHEMA_VERSION,
            "exit_code": self.exit_code,
            "signal_number": self.signal_number,
        }


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
    output_capture: RunOutputCapture | None
    expected_artifact_attestation_status: ExpectedArtifactAttestationStatus
    expected_artifact_attestation_error: ExpectedArtifactAttestationError | None
    expected_artifact_attestation: RunExpectedArtifactAttestation | None

    @classmethod
    def from_dict(cls, raw: object) -> RunStatus:
        if not isinstance(raw, dict):
            raise AutoresearchRunRecordError("status must be an object")
        schema_version = raw.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise AutoresearchRunRecordError("status schema_version must be an integer")
        historic_schema = schema_version != RUN_STATUS_SCHEMA_VERSION
        if schema_version == _HISTORIC_RUN_STATUS_SCHEMA_VERSION:
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
                label="historical status",
            )
            raw = {
                **raw,
                "schema_version": RUN_STATUS_SCHEMA_VERSION,
                "output_capture": None,
                "expected_artifact_attestation_status": (
                    ExpectedArtifactAttestationStatus.HISTORIC_UNKNOWN.value
                ),
                "expected_artifact_attestation_error": None,
                "expected_artifact_attestation": None,
            }
        elif schema_version == _PREVIOUS_RUN_STATUS_SCHEMA_VERSION:
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
                    "output_capture",
                ),
                label="schema-v2 status",
            )
            previous_capture = raw["output_capture"]
            raw = {
                **raw,
                "schema_version": RUN_STATUS_SCHEMA_VERSION,
                "output_capture": (
                    RunOutputCapture.from_schema_v2_dict(previous_capture).to_dict()
                    if previous_capture is not None
                    else None
                ),
                "expected_artifact_attestation_status": (
                    ExpectedArtifactAttestationStatus.HISTORIC_UNKNOWN.value
                ),
                "expected_artifact_attestation_error": None,
                "expected_artifact_attestation": None,
            }
        elif schema_version == _CAPTURE_RUN_STATUS_SCHEMA_VERSION:
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
                    "output_capture",
                ),
                label="schema-v3 status",
            )
            raw = {
                **raw,
                "schema_version": RUN_STATUS_SCHEMA_VERSION,
                "expected_artifact_attestation_status": (
                    ExpectedArtifactAttestationStatus.HISTORIC_UNKNOWN.value
                ),
                "expected_artifact_attestation_error": None,
                "expected_artifact_attestation": None,
            }
        elif schema_version == _ARTIFACT_ATTESTATION_RUN_STATUS_SCHEMA_VERSION:
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
                    "output_capture",
                    "expected_artifact_attestation",
                ),
                label="schema-v4 status",
            )
            raw = {
                **raw,
                "schema_version": RUN_STATUS_SCHEMA_VERSION,
                "expected_artifact_attestation_status": (
                    ExpectedArtifactAttestationStatus.HISTORIC_UNKNOWN.value
                ),
                "expected_artifact_attestation_error": None,
            }
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
                "output_capture",
                "expected_artifact_attestation_status",
                "expected_artifact_attestation_error",
                "expected_artifact_attestation",
            ),
            label="status",
        )
        if raw["schema_version"] != RUN_STATUS_SCHEMA_VERSION:
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
        try:
            attestation_status = ExpectedArtifactAttestationStatus(
                raw["expected_artifact_attestation_status"]
            )
        except (TypeError, ValueError) as exc:
            raise AutoresearchRunRecordError(
                "expected_artifact_attestation_status is invalid"
            ) from exc
        attestation_error_raw = raw["expected_artifact_attestation_error"]
        try:
            attestation_error = (
                ExpectedArtifactAttestationError(attestation_error_raw)
                if attestation_error_raw is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise AutoresearchRunRecordError(
                "expected_artifact_attestation_error is invalid"
            ) from exc
        attestation = (
            RunExpectedArtifactAttestation.from_dict(raw["expected_artifact_attestation"])
            if raw["expected_artifact_attestation"] is not None
            else None
        )
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
            if attestation is not None:
                raise AutoresearchRunRecordError(
                    "running status cannot contain expected artifact attestation"
                )
        elif pid is None or finished_at is None or exit_code is None:
            raise AutoresearchRunRecordError("terminal status requires finished_at and exit_code")
        elif state is RunState.SUCCEEDED and (
            exit_code != 0 or signal_number is not None or failure is not None
        ):
            raise AutoresearchRunRecordError("succeeded status must have only zero exit evidence")
        elif state is RunState.FAILED and failure is None:
            raise AutoresearchRunRecordError("failed status requires a failure classification")
        if (
            state is RunState.SUCCEEDED
            and attestation_status is ExpectedArtifactAttestationStatus.FAILED
        ):
            raise AutoresearchRunRecordError(
                "succeeded process status cannot contain failed artifact attestation"
            )
        if (
            failure is RunFailureClassification.ARTIFACT_MISSING
            and attestation_status is not ExpectedArtifactAttestationStatus.FAILED
        ):
            raise AutoresearchRunRecordError(
                "artifact_missing requires failed artifact attestation evidence"
            )
        if attestation_status is ExpectedArtifactAttestationStatus.ATTESTED:
            if attestation is None or attestation_error is not None:
                raise AutoresearchRunRecordError(
                    "attested artifact status requires only an attestation"
                )
        elif attestation_status is ExpectedArtifactAttestationStatus.FAILED:
            if attestation is not None or attestation_error is None:
                raise AutoresearchRunRecordError(
                    "failed artifact status requires only an attestation error"
                )
        elif attestation_status in {
            ExpectedArtifactAttestationStatus.NOT_REQUESTED,
            ExpectedArtifactAttestationStatus.PENDING,
        }:
            if attestation is not None or attestation_error is not None:
                raise AutoresearchRunRecordError(
                    "unattempted artifact status cannot contain attestation evidence"
                )
            if (
                attestation_status is ExpectedArtifactAttestationStatus.PENDING
                and state is not RunState.RUNNING
            ):
                raise AutoresearchRunRecordError(
                    "terminal status cannot leave artifact attestation pending"
                )
        elif not historic_schema or attestation_error is not None:
            raise AutoresearchRunRecordError(
                "historic artifact status is valid only for migrated records"
            )
        return cls(
            schema_version=RUN_STATUS_SCHEMA_VERSION,
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
            output_capture=(
                RunOutputCapture.from_dict(raw["output_capture"])
                if raw["output_capture"] is not None
                else None
            ),
            expected_artifact_attestation_status=attestation_status,
            expected_artifact_attestation_error=attestation_error,
            expected_artifact_attestation=attestation,
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
            "output_capture": self.output_capture.to_dict()
            if self.output_capture is not None
            else None,
            "expected_artifact_attestation_status": (
                self.expected_artifact_attestation_status.value
            ),
            "expected_artifact_attestation_error": (
                self.expected_artifact_attestation_error.value
                if self.expected_artifact_attestation_error is not None
                else None
            ),
            "expected_artifact_attestation": (
                self.expected_artifact_attestation.to_dict()
                if self.expected_artifact_attestation is not None
                else None
            ),
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


def _read_private_json_snapshot(
    path: Path,
    *,
    label: str,
    allowed_modes: frozenset[int] = frozenset({0o600}),
) -> tuple[dict[str, object], int]:
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
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) not in allowed_modes:
            rendered_modes = "/".join(f"{mode:04o}" for mode in sorted(allowed_modes))
            raise AutoresearchRunRecordError(
                f"{label} must be owned by this user with mode {rendered_modes}"
            )
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
    return raw, stat.S_IMODE(metadata.st_mode)


def _read_private_json(
    path: Path,
    *,
    label: str,
    allowed_modes: frozenset[int] = frozenset({0o600}),
) -> dict[str, object]:
    raw, _mode = _read_private_json_snapshot(
        path,
        label=label,
        allowed_modes=allowed_modes,
    )
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


def _output_capture_path(run_dir: Path, stream: RunOutputStream) -> Path:
    return run_dir / _OUTPUT_CAPTURE_FILE_NAMES[stream]


def _output_capture_receipt_path(run_dir: Path, stream: RunOutputStream) -> Path:
    return run_dir / f".{stream.value}{_OUTPUT_CAPTURE_RECEIPT_SUFFIX}"


def _output_capture_completion_marker_path(run_dir: Path) -> Path:
    return run_dir / _OUTPUT_CAPTURE_COMPLETION_MARKER_NAME


def _create_private_empty_file(path: Path, *, label: str) -> None:
    _reject_symlink(path, label=label)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise AutoresearchRunRecordError(f"{label} already exists") from exc
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot create {label}: {exc}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _read_private_bytes(path: Path, *, label: str) -> bytes:
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
            return handle.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _attestation_failure(
    reason: ExpectedArtifactAttestationError,
    message: str,
) -> _ExpectedArtifactAttestationFailure:
    return _ExpectedArtifactAttestationFailure(reason, message)


def _validate_existing_expected_artifact_ancestors(path: Path) -> None:
    """Reject unsafe existing artifact ancestors before a detached task starts."""
    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if current == current.parent:
                break
            current = current.parent
            continue
        except OSError as exc:
            raise AutoresearchRunRecordError(
                f"cannot inspect expected artifact ancestor: {current}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AutoresearchRunRecordError(
                f"expected artifact ancestor must be a non-symlink directory: {current}"
            )
        if metadata.st_uid not in {0, os.getuid()}:
            raise AutoresearchRunRecordError(
                f"expected artifact ancestor has an untrusted owner: {current}"
            )
        writable_by_others = bool(stat.S_IMODE(metadata.st_mode) & 0o022)
        trusted_sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if writable_by_others and not trusted_sticky_root:
            raise AutoresearchRunRecordError(
                f"expected artifact ancestor is group/world writable: {current}"
            )
        if current == current.parent:
            break
        current = current.parent


_OVERFLOW_UID = 65534


def _open_expected_artifact_fd(path: Path, *, label: str) -> tuple[int, os.stat_result]:
    """Pin each ancestor with openat before opening the final artifact."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = -1
    try:
        directory_fd = os.open(path.anchor, directory_flags)
        root_metadata = os.fstat(directory_fd)
        # Inside a uid-mapped sandbox namespace every foreign owner reports
        # the overflow uid, so ownership is unverifiable there; content
        # integrity is still proven by attestation digest equality against
        # the seal made in the unsandboxed worker.
        overflow_mapped_namespace = root_metadata.st_uid == _OVERFLOW_UID
        trusted_root_owners = {0, _OVERFLOW_UID} if overflow_mapped_namespace else {0}
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid not in trusted_root_owners
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise _attestation_failure(
                ExpectedArtifactAttestationError.UNSAFE_ANCESTOR,
                f"{label} filesystem root is not trusted",
            )
        for component in path.parts[1:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError as exc:
                raise _attestation_failure(
                    ExpectedArtifactAttestationError.MISSING,
                    f"missing {label}: {path}",
                ) from exc
            except OSError as exc:
                reason = (
                    ExpectedArtifactAttestationError.SYMLINK
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                    else ExpectedArtifactAttestationError.IO_ERROR
                )
                raise _attestation_failure(
                    reason,
                    f"cannot securely open {label} ancestor: {path}: {exc}",
                ) from exc
            try:
                ancestor = os.fstat(next_fd)
                if not stat.S_ISDIR(ancestor.st_mode):
                    raise _attestation_failure(
                        ExpectedArtifactAttestationError.UNSAFE_ANCESTOR,
                        f"{label} ancestor must be a directory",
                    )
                trusted_ancestor_owners = {0, os.getuid()}
                if overflow_mapped_namespace:
                    trusted_ancestor_owners.add(_OVERFLOW_UID)
                if ancestor.st_uid not in trusted_ancestor_owners:
                    raise _attestation_failure(
                        ExpectedArtifactAttestationError.UNSAFE_ANCESTOR,
                        f"{label} ancestor has an untrusted owner",
                    )
                writable_by_others = bool(stat.S_IMODE(ancestor.st_mode) & 0o022)
                trusted_sticky_root = ancestor.st_uid == 0 and bool(ancestor.st_mode & stat.S_ISVTX)
                if writable_by_others and not trusted_sticky_root:
                    raise _attestation_failure(
                        ExpectedArtifactAttestationError.UNSAFE_ANCESTOR,
                        f"{label} ancestor is group/world writable",
                    )
            except BaseException:
                os.close(next_fd)
                raise
            os.close(directory_fd)
            directory_fd = next_fd
        parent_metadata = os.fstat(directory_fd)
        try:
            artifact_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.MISSING,
                f"missing {label}: {path}",
            ) from exc
        except OSError as exc:
            reason = (
                ExpectedArtifactAttestationError.SYMLINK
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                else ExpectedArtifactAttestationError.IO_ERROR
            )
            raise _attestation_failure(
                reason,
                f"cannot securely open {label}: {path}: {exc}",
            ) from exc
        return artifact_fd, parent_metadata
    finally:
        if directory_fd != -1:
            os.close(directory_fd)


def _attest_expected_artifact(
    path: Path,
    *,
    seal: bool,
) -> RunExpectedArtifactAttestation:
    label = "expected artifact"
    try:
        canonical_text = _require_canonical_absolute_path(str(path), label=label)
    except AutoresearchRunRecordError as exc:
        raise _attestation_failure(
            ExpectedArtifactAttestationError.UNSAFE_ANCESTOR,
            str(exc),
        ) from exc
    descriptor = -1
    try:
        descriptor, parent_metadata = _open_expected_artifact_fd(
            Path(canonical_text),
            label=label,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _attestation_failure(
                ExpectedArtifactAttestationError.NOT_REGULAR,
                f"{label} must be a regular file",
            )
        if before.st_uid != os.getuid():
            raise _attestation_failure(
                ExpectedArtifactAttestationError.WRONG_OWNER,
                f"{label} must be owned by this user",
            )
        required_mode = 0o600 if seal else 0o400
        if stat.S_IMODE(before.st_mode) != required_mode:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.WRONG_MODE,
                f"{label} must have mode {required_mode:04o}",
            )
        if before.st_nlink != 1:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.HARD_LINK,
                f"{label} must have exactly one hard link",
            )
        if before.st_dev != parent_metadata.st_dev:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.UNSAFE_ANCESTOR,
                f"{label} must be on the same device as its pinned parent",
            )
        if before.st_size > EXPECTED_ARTIFACT_MAX_BYTES:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.OVERSIZED,
                f"{label} exceeds the fixed byte limit",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(EXPECTED_ARTIFACT_MAX_BYTES + 1)
        after_read = os.fstat(descriptor)
        if len(content) > EXPECTED_ARTIFACT_MAX_BYTES:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.OVERSIZED,
                f"{label} exceeds the fixed byte limit",
            )
        stable_identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_identity_after = (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_uid,
            after_read.st_nlink,
            after_read.st_size,
            after_read.st_mtime_ns,
            after_read.st_ctime_ns,
        )
        if stable_identity_before != stable_identity_after or len(content) != before.st_size:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.CHANGED_DURING_READ,
                f"{label} changed while it was being attested",
            )
        if seal:
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        sealed = os.fstat(descriptor)
        if stat.S_IMODE(sealed.st_mode) != 0o400:
            raise _attestation_failure(
                ExpectedArtifactAttestationError.WRONG_MODE,
                f"{label} could not be sealed mode 0400",
            )
        return RunExpectedArtifactAttestation(
            path=canonical_text,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            device=sealed.st_dev,
            inode=sealed.st_ino,
            mtime_ns=sealed.st_mtime_ns,
            ctime_ns=sealed.st_ctime_ns,
        )
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _read_capture_receipt(
    run_dir: Path, stream: RunOutputStream, *, required: bool
) -> _OutputCaptureReceipt | None:
    receipt_path = _output_capture_receipt_path(run_dir, stream)
    _reject_symlink(receipt_path, label="capture receipt")
    if not receipt_path.exists():
        if required:
            raise AutoresearchRunRecordError(f"missing {stream.value} capture receipt")
        return None
    if receipt_path.stat().st_size == 0:
        # The capture drainer creates the receipt exclusively and then renames
        # the content in atomically; an empty file is a not-yet-written receipt.
        if required:
            raise AutoresearchRunRecordError(f"missing {stream.value} capture receipt")
        return None
    raw = _read_private_json(receipt_path, label="capture receipt")
    _require_exact_keys(
        raw,
        ("stream", "bytes_observed", "eof_observed"),
        label="capture receipt",
    )
    try:
        receipt_stream = RunOutputStream(_require_non_empty_string(raw["stream"], label="stream"))
    except ValueError as exc:
        raise AutoresearchRunRecordError("capture receipt stream is invalid") from exc
    if receipt_stream is not stream:
        raise AutoresearchRunRecordError("capture receipt stream does not match its file")
    observed = raw["bytes_observed"]
    eof_observed = raw["eof_observed"]
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise AutoresearchRunRecordError(
            "capture receipt bytes_observed must be a non-negative integer"
        )
    if not isinstance(eof_observed, bool):
        raise AutoresearchRunRecordError("capture receipt eof_observed must be a boolean")
    return _OutputCaptureReceipt(
        bytes_observed=observed,
        eof_observed=eof_observed,
    )


def _output_stream_capture_metadata(
    run_dir: Path, stream: RunOutputStream, *, require_receipt: bool
) -> RunOutputStreamCapture:
    stored = _read_private_bytes(_output_capture_path(run_dir, stream), label="capture output")
    if len(stored) > OUTPUT_CAPTURE_MAX_BYTES:
        raise AutoresearchRunRecordError("capture output exceeds the fixed storage limit")
    receipt = _read_capture_receipt(run_dir, stream, required=require_receipt)
    observed = receipt.bytes_observed if receipt is not None else len(stored)
    if observed < len(stored):
        raise AutoresearchRunRecordError(
            "capture receipt observed bytes are less than stored bytes"
        )
    return RunOutputStreamCapture(
        relative_path=_OUTPUT_CAPTURE_FILE_NAMES[stream],
        bytes_observed=observed,
        bytes_stored=len(stored),
        sha256=hashlib.sha256(stored).hexdigest(),
        truncated=observed > len(stored),
        eof_observed=receipt.eof_observed if receipt is not None else False,
    )


def _output_capture_metadata(run_dir: Path, *, require_receipts: bool) -> RunOutputCapture | None:
    stdout_path = _output_capture_path(run_dir, RunOutputStream.STDOUT)
    stderr_path = _output_capture_path(run_dir, RunOutputStream.STDERR)
    paths = (stdout_path, stderr_path)
    for path in paths:
        _reject_symlink(path, label="capture output")
    exists = tuple(path.exists() for path in paths)
    if not any(exists):
        return None
    if not all(exists):
        raise AutoresearchRunRecordError("both stdout and stderr capture files are required")
    return RunOutputCapture(
        stdout=_output_stream_capture_metadata(
            run_dir, RunOutputStream.STDOUT, require_receipt=require_receipts
        ),
        stderr=_output_stream_capture_metadata(
            run_dir, RunOutputStream.STDERR, require_receipt=require_receipts
        ),
    )


def prepare_output_capture(
    *, run_dir: Path, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> RunOutputCapture:
    """Create the two private run-local log files before launching a child."""
    canonical_run_dir, _manifest, _digest = _load_manifest(run_dir, runs_root)
    created_paths: list[Path] = []
    with _status_lock(canonical_run_dir):
        if (canonical_run_dir / "status.json").exists():
            raise AutoresearchRunRecordError("cannot prepare output capture after startup")
        try:
            for stream in RunOutputStream:
                path = _output_capture_path(canonical_run_dir, stream)
                _create_private_empty_file(path, label="capture output")
                created_paths.append(path)
        except AutoresearchRunRecordError:
            for path in created_paths:
                with suppress(OSError):
                    path.unlink()
            raise
    metadata = _output_capture_metadata(canonical_run_dir, require_receipts=False)
    if metadata is None:
        raise AutoresearchRunRecordError(
            "output capture initialization did not create both streams"
        )
    return metadata


def signal_output_capture_completion(
    *, run_dir: Path, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> None:
    """Tell stream drainers that the managed command process group is closed."""
    canonical_run_dir, _manifest, _digest = _load_manifest(run_dir, runs_root)
    _create_private_empty_file(
        _output_capture_completion_marker_path(canonical_run_dir),
        label="capture completion marker",
    )


def _completion_marker_exists(path: Path) -> bool:
    _reject_symlink(path, label="capture completion marker")
    return path.exists()


def _drain_bounded_tail(
    *,
    source: BinaryIO,
    completion_marker: Path | None,
) -> tuple[int, bytearray, bool]:
    observed = 0
    tail = bytearray()
    completion_deadline: float | None = None
    source_descriptor = source.fileno() if completion_marker is not None else None
    while True:
        if completion_marker is None:
            chunk = source.read(8192)
        else:
            now = time.monotonic()
            if completion_deadline is None and _completion_marker_exists(completion_marker):
                completion_deadline = now + _OUTPUT_CAPTURE_FINAL_DRAIN_SECONDS
            if completion_deadline is not None and now >= completion_deadline:
                return observed, tail, False
            wait_seconds = 0.05
            if completion_deadline is not None:
                wait_seconds = min(wait_seconds, max(0.0, completion_deadline - now))
            readable, _writable, _exceptional = select.select(
                (source_descriptor,),
                (),
                (),
                wait_seconds,
            )
            if not readable:
                continue
            if source_descriptor is None:
                raise AutoresearchRunRecordError("capture source descriptor is unavailable")
            chunk = os.read(source_descriptor, 8192)
        if not chunk:
            return observed, tail, True
        observed += len(chunk)
        tail.extend(chunk)
        if len(tail) > OUTPUT_CAPTURE_MAX_BYTES:
            del tail[: len(tail) - OUTPUT_CAPTURE_MAX_BYTES]


def capture_output_stream(
    *,
    run_dir: Path,
    stream: RunOutputStream,
    source: BinaryIO,
    completion_marker: Path | None = None,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> bool:
    """Drain one child stream fully while retaining only its bounded tail."""
    canonical_run_dir, _manifest, _digest = _load_manifest(run_dir, runs_root)
    output_path = _output_capture_path(canonical_run_dir, stream)
    if completion_marker is not None:
        expected_marker = _output_capture_completion_marker_path(canonical_run_dir)
        if completion_marker != expected_marker:
            raise AutoresearchRunRecordError(
                "capture completion marker must use the run-local fixed path"
            )
    _reject_symlink(output_path, label="capture output")
    descriptor = -1
    try:
        descriptor = os.open(output_path, os.O_WRONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AutoresearchRunRecordError("capture output must be a regular file")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AutoresearchRunRecordError(
                "capture output must be owned by this user with mode 0600"
            )
        if metadata.st_size != 0:
            raise AutoresearchRunRecordError(
                "capture output must be empty before draining a stream"
            )
        observed, tail, eof_observed = _drain_bounded_tail(
            source=source,
            completion_marker=completion_marker,
        )
        remaining = memoryview(tail)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise AutoresearchRunRecordError("capture output write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as exc:
        raise AutoresearchRunRecordError(f"cannot capture {stream.value}: {exc}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    receipt_path = _output_capture_receipt_path(canonical_run_dir, stream)
    _create_private_empty_file(receipt_path, label="capture receipt")
    try:
        _atomic_write(
            receipt_path,
            _canonical_json(
                {
                    "stream": stream.value,
                    "bytes_observed": observed,
                    "eof_observed": eof_observed,
                }
            ),
            mode=0o600,
        )
    except AutoresearchRunRecordError:
        with suppress(OSError):
            receipt_path.unlink()
        raise
    return eof_observed


def _write_private_json_exclusive(
    path: Path,
    payload: dict[str, object],
    *,
    label: str,
) -> None:
    _create_private_empty_file(path, label=label)
    try:
        _atomic_write(path, _canonical_json(payload), mode=0o600)
    except AutoresearchRunRecordError:
        with suppress(OSError):
            path.unlink()
        raise


def _secure_marker_exists(path: Path, *, label: str) -> bool:
    _reject_symlink(path, label=label)
    return path.exists()


def _peek_child_exit(pid: int) -> os.waitid_result | None:
    return os.waitid(
        os.P_PID,
        pid,
        os.WEXITED | os.WNOHANG | os.WNOWAIT,
    )


def _active_process_group_members(group_id: int, *, leader_pid: int) -> tuple[int, ...]:
    members: list[int] = []
    try:
        process_entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise AutoresearchRunRecordError("cannot inspect command process group") from exc
    for entry in process_entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == leader_pid:
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            fields = stat_text[stat_text.rfind(")") + 2 :].split()
            state = fields[0]
            process_group = int(fields[2])
        except (FileNotFoundError, IndexError, OSError, ValueError):
            continue
        if process_group == group_id and state != "Z":
            members.append(pid)
    return tuple(members)


def _signal_anchored_process_group(leader_pid: int, signal_number: int) -> None:
    """Signal a group only while its leader PID is held live or waitable by this process."""
    try:
        os.killpg(leader_pid, signal_number)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise AutoresearchRunRecordError("cannot signal supervised command group") from exc


def _terminate_group_members_while_leader_waitable(
    leader_pid: int,
    *,
    grace_seconds: float,
) -> None:
    if not _active_process_group_members(leader_pid, leader_pid=leader_pid):
        return
    _signal_anchored_process_group(leader_pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while _active_process_group_members(leader_pid, leader_pid=leader_pid):
        if time.monotonic() >= deadline:
            _signal_anchored_process_group(leader_pid, signal.SIGKILL)
            return
        time.sleep(_SUPERVISOR_POLL_SECONDS)


def _termination_requested(run_dir: Path) -> bool:
    return _secure_marker_exists(
        run_dir / _TIMEOUT_MARKER_NAME,
        label="timeout marker",
    ) or _secure_marker_exists(
        run_dir / _OPERATOR_STOP_MARKER_NAME,
        label="operator stop marker",
    )


def _wait_for_leader_exit_without_reaping(
    leader_pid: int,
    *,
    run_dir: Path,
    grace_seconds: float,
) -> os.waitid_result:
    termination_deadline: float | None = None
    kill_sent = False
    while True:
        observation = _peek_child_exit(leader_pid)
        if observation is not None:
            return observation
        now = time.monotonic()
        if termination_deadline is None and _termination_requested(run_dir):
            _signal_anchored_process_group(leader_pid, signal.SIGTERM)
            termination_deadline = now + grace_seconds
        elif termination_deadline is not None and not kill_sent and now >= termination_deadline:
            _signal_anchored_process_group(leader_pid, signal.SIGKILL)
            kill_sent = True
        time.sleep(_SUPERVISOR_POLL_SECONDS)


def _supervised_result(observation: os.waitid_result) -> _SupervisedCommandResult:
    if observation.si_code == os.CLD_EXITED:
        return _SupervisedCommandResult(
            exit_code=observation.si_status,
            signal_number=None,
        )
    if observation.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        return _SupervisedCommandResult(
            exit_code=128 + observation.si_status,
            signal_number=observation.si_status,
        )
    raise AutoresearchRunRecordError("command wait observation is not terminal")


def supervise_command(
    *,
    run_dir: Path,
    runs_root: Path,
    systemd_unit: str,
    grace_seconds: float,
    stdout_descriptor: int,
    stderr_descriptor: int,
) -> _SupervisedCommandResult:
    """Own one command leader until its process group is closed and capture is signaled."""
    if grace_seconds <= 0:
        raise AutoresearchRunRecordError("command termination grace must be positive")
    canonical_run_dir, manifest, _digest = _load_manifest(run_dir, runs_root)
    # Ancestor-trust validation must run here, in the unsandboxed detached
    # unit: the sandboxed prepare step sees uid-mapped (nobody-owned)
    # ancestors and would fail closed on trusted paths.
    if manifest.expected_artifact_path is not None:
        _validate_existing_expected_artifact_ancestors(Path(manifest.expected_artifact_path))
    command = consume_command_handoff(run_dir=run_dir, runs_root=runs_root)
    try:
        process = subprocess.Popen(
            command,
            cwd=manifest.working_directory,
            stdin=subprocess.DEVNULL,
            stdout=stdout_descriptor,
            stderr=stderr_descriptor,
            start_new_session=True,
        )
    finally:
        os.close(stdout_descriptor)
        os.close(stderr_descriptor)
    try:
        status = start_run(
            run_dir=canonical_run_dir,
            runs_root=runs_root,
            pid=process.pid,
            systemd_unit=systemd_unit,
        )
        _write_private_json_exclusive(
            canonical_run_dir / _STARTUP_MARKER_NAME,
            status.to_dict(),
            label="startup marker",
        )
        observation = _wait_for_leader_exit_without_reaping(
            process.pid,
            run_dir=canonical_run_dir,
            grace_seconds=grace_seconds,
        )
        _terminate_group_members_while_leader_waitable(
            process.pid,
            grace_seconds=grace_seconds,
        )
        signal_output_capture_completion(
            run_dir=canonical_run_dir,
            runs_root=runs_root,
        )
        result = _supervised_result(observation)
        _write_private_json_exclusive(
            canonical_run_dir / _SUPERVISED_COMMAND_RESULT_NAME,
            result.to_dict(),
            label="supervised command result",
        )
    except BaseException:
        _signal_anchored_process_group(process.pid, signal.SIGKILL)
        process.wait()
        raise
    process.wait()
    return result


def consume_supervised_command_result(
    *,
    run_dir: Path,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> _SupervisedCommandResult:
    canonical_run_dir, _manifest, _digest = _load_manifest(run_dir, runs_root)
    result_path = canonical_run_dir / _SUPERVISED_COMMAND_RESULT_NAME
    result = _SupervisedCommandResult.from_dict(
        _read_private_json(result_path, label="supervised command result")
    )
    try:
        result_path.unlink()
    except OSError as exc:
        raise AutoresearchRunRecordError("cannot remove supervised command result") from exc
    return result


def _preserve_supervisor_on_control_signal(
    _signal_number: int,
    _frame: FrameType | None,
) -> None:
    return


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


def read_run_manifest(
    *, run_dir: Path, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> RunManifest:
    """Load and validate a detached manifest without reading mutable status data."""
    _directory, manifest, _digest = _load_manifest(run_dir, runs_root)
    return manifest


def start_run(
    *,
    run_dir: Path,
    pid: int | None,
    systemd_unit: str | None = None,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> RunStatus:
    canonical_run_dir, manifest, digest = _load_manifest(run_dir, runs_root)
    if pid is None or isinstance(pid, bool) or pid < 1:
        raise AutoresearchRunRecordError("startup requires a positive pid")
    with _status_lock(canonical_run_dir):
        status_path = canonical_run_dir / "status.json"
        if status_path.exists() or status_path.is_symlink():
            raise AutoresearchRunRecordError("startup status already exists")
        now = _utc_now()
        status = RunStatus(
            schema_version=RUN_STATUS_SCHEMA_VERSION,
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
            output_capture=_output_capture_metadata(canonical_run_dir, require_receipts=False),
            expected_artifact_attestation_status=(
                ExpectedArtifactAttestationStatus.PENDING
                if manifest.expected_artifact_path is not None
                else ExpectedArtifactAttestationStatus.NOT_REQUESTED
            ),
            expected_artifact_attestation_error=None,
            expected_artifact_attestation=None,
        )
        _atomic_write(status_path, _canonical_json(status.to_dict()), mode=0o600)
        return status


def _current_status(run_dir: Path, runs_root: Path) -> tuple[Path, RunManifest, str, RunStatus]:
    canonical_run_dir, manifest, digest = _load_manifest(run_dir, runs_root)
    status_raw, status_mode = _read_private_json_snapshot(
        canonical_run_dir / "status.json",
        label="status",
        allowed_modes=frozenset({0o400, 0o600}),
    )
    status = RunStatus.from_dict(status_raw)
    expected_status_mode = 0o600 if status.state is RunState.RUNNING else 0o400
    if status_mode != expected_status_mode:
        raise AutoresearchRunRecordError(
            f"{status.state.value} status must be sealed mode {expected_status_mode:04o}"
        )
    expected_directory_mode = 0o700 if status.state is RunState.RUNNING else 0o500
    directory_fd = os.open(
        canonical_run_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        directory_metadata = os.fstat(directory_fd)
    finally:
        os.close(directory_fd)
    if stat.S_IMODE(directory_metadata.st_mode) != expected_directory_mode:
        raise AutoresearchRunRecordError(
            f"{status.state.value} run directory must have mode {expected_directory_mode:04o}"
        )
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
        canonical_run_dir, manifest, digest, previous = _current_status(run_dir, runs_root)
        if previous.state is not RunState.RUNNING:
            raise AutoresearchRunRecordError("cannot complete a terminal run")
        now = _utc_now()
        expected_artifact_attestation: RunExpectedArtifactAttestation | None = None
        artifact_attestation_error: ExpectedArtifactAttestationError | None = None
        if manifest.expected_artifact_path is not None:
            try:
                expected_artifact_attestation = _attest_expected_artifact(
                    Path(manifest.expected_artifact_path),
                    seal=True,
                )
            except _ExpectedArtifactAttestationFailure as exc:
                artifact_attestation_error = exc.reason
            except OSError:
                artifact_attestation_error = ExpectedArtifactAttestationError.IO_ERROR
        process_otherwise_succeeded = (
            exit_code == 0
            and signal_number is None
            and not timed_out
            and failure_classification is None
        )
        if process_otherwise_succeeded and artifact_attestation_error is not None:
            failure_classification = RunFailureClassification.ARTIFACT_MISSING
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
            schema_version=RUN_STATUS_SCHEMA_VERSION,
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
            output_capture=(
                _output_capture_metadata(canonical_run_dir, require_receipts=True)
                if previous.output_capture is not None
                else None
            ),
            expected_artifact_attestation_status=(
                ExpectedArtifactAttestationStatus.NOT_REQUESTED
                if manifest.expected_artifact_path is None
                else ExpectedArtifactAttestationStatus.ATTESTED
                if expected_artifact_attestation is not None
                else ExpectedArtifactAttestationStatus.FAILED
            ),
            expected_artifact_attestation_error=artifact_attestation_error,
            expected_artifact_attestation=expected_artifact_attestation,
        )
        _atomic_write(
            canonical_run_dir / "status.json", _canonical_json(status.to_dict()), mode=0o400
        )
        os.chmod(canonical_run_dir, 0o500)
        return status


def read_run_record(
    *, run_dir: Path, runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT
) -> RunRecord:
    canonical_run_dir = _validate_run_directory(run_dir, runs_root)
    with _status_lock(canonical_run_dir):
        canonical_run_dir, manifest, _digest, status = _current_status(run_dir, runs_root)
        expected_artifact_path = manifest.expected_artifact_path
        attestation = status.expected_artifact_attestation
        attestation_status = status.expected_artifact_attestation_status
        if expected_artifact_path is None and (
            attestation is not None
            or attestation_status
            not in {
                ExpectedArtifactAttestationStatus.NOT_REQUESTED,
                ExpectedArtifactAttestationStatus.HISTORIC_UNKNOWN,
            }
        ):
            raise AutoresearchRunRecordError(
                "run without an expected artifact cannot contain artifact attestation"
            )
        if expected_artifact_path is not None:
            if attestation_status is ExpectedArtifactAttestationStatus.HISTORIC_UNKNOWN:
                raise AutoresearchRunRecordError(
                    "historical records cannot prove artifact attestation status"
                )
            if attestation is not None and attestation.path != expected_artifact_path:
                raise AutoresearchRunRecordError(
                    "expected artifact attestation path does not match manifest"
                )
            if status.state is RunState.SUCCEEDED and (
                attestation_status is not ExpectedArtifactAttestationStatus.ATTESTED
                or attestation is None
            ):
                raise AutoresearchRunRecordError(
                    "terminal success lacks mandatory expected artifact attestation; "
                    "historical records cannot prove artifact bytes"
                )
            if attestation is not None:
                current_attestation = _attest_expected_artifact(
                    Path(expected_artifact_path),
                    seal=False,
                )
                if current_attestation != attestation:
                    raise AutoresearchRunRecordError(
                        "current expected artifact does not match terminal worker attestation"
                    )
        if status.state is not RunState.RUNNING and status.output_capture is not None:
            current_capture = _output_capture_metadata(canonical_run_dir, require_receipts=True)
            if current_capture != status.output_capture:
                raise AutoresearchRunRecordError(
                    "terminal output capture metadata does not match the private capture files"
                )
        return RunRecord(manifest=manifest, status=status, run_directory=canonical_run_dir)


def validate_startup_marker(
    *,
    run_dir: Path,
    marker_path: Path,
    runs_root: Path = DEFAULT_AUTORESEARCH_RUNS_ROOT,
) -> None:
    """Require a startup receipt to bind the live record to one manifest and pid."""
    record = read_run_record(run_dir=run_dir, runs_root=runs_root)
    marker = RunStatus.from_dict(_read_private_json(marker_path, label="startup marker"))
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
    prepare_capture = subparsers.add_parser("prepare-output-capture")
    prepare_capture.add_argument("--run-dir", type=Path, required=True)
    prepare_capture.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    capture_stream = subparsers.add_parser("capture-output-stream")
    capture_stream.add_argument("--run-dir", type=Path, required=True)
    capture_stream.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    capture_stream.add_argument(
        "--stream",
        choices=tuple(stream.value for stream in RunOutputStream),
        required=True,
    )
    supervise = subparsers.add_parser("supervise-command")
    supervise.add_argument("--run-dir", type=Path, required=True)
    supervise.add_argument("--runs-root", type=Path, default=DEFAULT_AUTORESEARCH_RUNS_ROOT)
    supervise.add_argument("--systemd-unit", required=True)
    supervise.add_argument("--termination-grace-seconds", type=float, required=True)
    consume_result = subparsers.add_parser("consume-supervised-command-result")
    consume_result.add_argument("--run-dir", type=Path, required=True)
    consume_result.add_argument(
        "--runs-root",
        type=Path,
        default=DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
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
    failure_group = complete.add_mutually_exclusive_group()
    failure_group.add_argument("--resource-exhausted", action="store_true")
    failure_group.add_argument("--operator-stopped", action="store_true")
    failure_group.add_argument("--output-capture-failed", action="store_true")
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
    elif args.operation == "prepare-output-capture":
        prepare_output_capture(run_dir=args.run_dir, runs_root=args.runs_root)
    elif args.operation == "capture-output-stream":
        for termination_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(termination_signal, signal.SIG_IGN)
        canonical_run_dir = _validate_run_directory(args.run_dir, args.runs_root)
        capture_complete = capture_output_stream(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            stream=RunOutputStream(args.stream),
            source=sys.stdin.buffer,
            completion_marker=_output_capture_completion_marker_path(canonical_run_dir),
        )
        if not capture_complete:
            return _OUTPUT_CAPTURE_INCOMPLETE_EXIT_CODE
    elif args.operation == "supervise-command":
        for termination_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(termination_signal, _preserve_supervisor_on_control_signal)
        supervise_command(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            systemd_unit=args.systemd_unit,
            grace_seconds=args.termination_grace_seconds,
            stdout_descriptor=3,
            stderr_descriptor=4,
        )
    elif args.operation == "consume-supervised-command-result":
        result = consume_supervised_command_result(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
        )
        sys.stdout.write(f"{result.exit_code}\n")
        sys.stdout.write(f"{result.signal_number if result.signal_number is not None else ''}\n")
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
                RunFailureClassification.RESOURCE_EXHAUSTED
                if args.resource_exhausted
                else RunFailureClassification.OPERATOR_STOPPED
                if args.operator_stopped
                else RunFailureClassification.OUTPUT_CAPTURE_ERROR
                if args.output_capture_failed
                else None
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
