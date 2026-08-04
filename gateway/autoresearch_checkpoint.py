"""Durable supervisor recovery and memory-wake checkpoints."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gateway.autoresearch_rpc import (
    _strict_json_object as _strict_json_object,
)
from gateway.autoresearch_shared import (
    RecoveryStatus as RecoveryStatus,
)
from gateway.autoresearch_shared import (
    SupervisorCheckpointError as SupervisorCheckpointError,
)
from gateway.autoresearch_shared import (
    SupervisorError as SupervisorError,
)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(slots=True)
class RecoveryRecord:
    status: RecoveryStatus = RecoveryStatus.READY
    attempt_count: int = 0
    claim_token: str | None = None
    claim_pid: int | None = None
    claim_process_identity: str | None = None
    claim_started_at: float | None = None
    woke_at: float | None = None
    failed_at: float | None = None
    last_error: str | None = None
    alerted: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RecoveryRecord:
        status_raw = raw.get("status", RecoveryStatus.READY.value)
        if not isinstance(status_raw, str):
            raise SupervisorError(f"invalid recovery status: {status_raw!r}")
        try:
            status = RecoveryStatus(status_raw)
        except ValueError as exc:
            raise SupervisorError(f"invalid recovery status: {status_raw!r}") from exc
        attempt_count = raw.get("attempt_count", 0)
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise SupervisorError("recovery attempt_count must be a non-negative integer")
        return cls(
            status=status,
            attempt_count=attempt_count,
            claim_token=_optional_str(raw.get("claim_token")),
            claim_pid=_optional_int(raw.get("claim_pid")),
            claim_process_identity=_optional_str(raw.get("claim_process_identity")),
            claim_started_at=_optional_float(raw.get("claim_started_at")),
            woke_at=_optional_float(raw.get("woke_at")),
            failed_at=_optional_float(raw.get("failed_at")),
            last_error=_optional_str(raw.get("last_error")),
            alerted=raw.get("alerted") is True,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MemoryWakeAcknowledgement:
    status: str
    acknowledged_at: float
    run_id: str | None = None
    cached_terminal: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> MemoryWakeAcknowledgement:
        status = _optional_str(raw.get("status"))
        if status not in {"accepted", "in_flight", "ok"}:
            raise SupervisorError(f"invalid memory wake acknowledgement status: {status!r}")
        acknowledged_at = _optional_float(raw.get("acknowledged_at"))
        if acknowledged_at is None or not math.isfinite(acknowledged_at) or acknowledged_at <= 0:
            raise SupervisorError("memory wake acknowledgement requires acknowledged_at")
        run_id = _optional_str(raw.get("run_id"))
        cached_terminal = raw.get("cached_terminal") is True
        if status == "ok" and not cached_terminal:
            raise SupervisorError("cached memory wake acknowledgement must be terminal")
        if status != "ok" and cached_terminal:
            raise SupervisorError("non-terminal memory wake acknowledgement cannot be cached")
        return cls(
            status=status,
            acknowledged_at=acknowledged_at,
            run_id=run_id,
            cached_terminal=cached_terminal,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "acknowledged_at": self.acknowledged_at,
            "run_id": self.run_id,
            "cached_terminal": self.cached_terminal,
        }


@dataclass(slots=True)
class SupervisorCheckpoint:
    recovery_records: dict[str, RecoveryRecord] = field(default_factory=dict)
    memory_wake_acknowledgements: dict[str, MemoryWakeAcknowledgement] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SupervisorCheckpoint:
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_strict_json_object
            )
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            raise SupervisorCheckpointError(
                f"failed to read supervisor checkpoint {path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SupervisorCheckpointError(f"invalid supervisor checkpoint JSON: {path}") from exc
        if not isinstance(raw, Mapping):
            raise SupervisorCheckpointError(f"invalid supervisor checkpoint payload: {path}")
        records_raw = raw.get("recovery_records", {})
        if not isinstance(records_raw, Mapping):
            raise SupervisorCheckpointError("recovery_records must be an object")
        records: dict[str, RecoveryRecord] = {}
        for key, value in records_raw.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                raise SupervisorCheckpointError("recovery_records entries must be keyed objects")
            try:
                records[key] = RecoveryRecord.from_dict(value)
            except SupervisorError as exc:
                raise SupervisorCheckpointError(
                    f"invalid supervisor checkpoint recovery record: {key}"
                ) from exc
        acks_raw = raw.get("memory_wake_acknowledgements", {})
        if not isinstance(acks_raw, Mapping):
            raise SupervisorCheckpointError("memory_wake_acknowledgements must be an object")
        acknowledgements: dict[str, MemoryWakeAcknowledgement] = {}
        for key, value in acks_raw.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                raise SupervisorCheckpointError(
                    "memory_wake_acknowledgements entries must be keyed objects"
                )
            try:
                acknowledgements[key] = MemoryWakeAcknowledgement.from_dict(value)
            except SupervisorError as exc:
                raise SupervisorCheckpointError(
                    f"invalid supervisor checkpoint memory wake acknowledgement: {key}"
                ) from exc
        return cls(
            recovery_records=records,
            memory_wake_acknowledgements=acknowledgements,
        )

    def save(self, path: Path) -> None:
        serialized = json.dumps(
            {
                "memory_wake_acknowledgements": {
                    key: acknowledgement.to_dict()
                    for key, acknowledgement in self.memory_wake_acknowledgements.items()
                },
                "recovery_records": {
                    key: record.to_dict() for key, record in self.recovery_records.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except OSError as exc:
            raise SupervisorError(
                f"failed to atomically save supervisor checkpoint {path}: {exc}"
            ) from exc
        finally:
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()


def reset_recovery_checkpoint_for_manual_wake(path: Path, *, iteration: int, phase: str) -> None:
    """Allow an explicit operator wake to retry an exhausted recovery key.

    Recovery attempt limits protect the autonomous supervisor from retry loops,
    but an operator-initiated wake is a deliberate new recovery window. Remove
    only failed or exhausted records for the current state phase; successful
    records remain bounded history and must not be reset implicitly.
    """
    checkpoint_path = path.expanduser()
    lock_path = checkpoint_path.with_name(f"{checkpoint_path.name}.lock")
    prefixes = (
        f"stale_state:{iteration}:{phase}:",
        f"missing_verification_artifact:{iteration}:{phase}:",
    )
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                checkpoint = SupervisorCheckpoint.load(checkpoint_path)
                removed = [
                    key
                    for key, record in checkpoint.recovery_records.items()
                    if key.startswith(prefixes)
                    and record.status in {RecoveryStatus.FAILED, RecoveryStatus.EXHAUSTED}
                ]
                if removed:
                    for key in removed:
                        del checkpoint.recovery_records[key]
                    checkpoint.save(checkpoint_path)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise SupervisorError(
            f"failed to reset supervisor checkpoint {checkpoint_path}: {exc}"
        ) from exc
