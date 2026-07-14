"""Strict platform-readiness manifest contract for autoresearch."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

DEFAULT_PLATFORM_READINESS_PATH = (
    Path.home() / ".openclaw" / "autoresearch" / "platform-readiness.json"
)
READINESS_SCHEMA_VERSION = 1
READINESS_SHA256_RE = re.compile(r"[0-9a-f]{64}")
READINESS_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class ReadinessError(ValueError):
    """Base class for readiness manifest failures."""


class ReadinessManifestError(ReadinessError):
    """Raised when a readiness manifest is malformed or unverifiable."""


class ReadinessBlockedError(ReadinessError):
    """Raised when a valid readiness manifest explicitly blocks research."""


class ReadinessStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class EvidenceId(StrEnum):
    SEC_COMMON_STOCK_PROVENANCE = "sec_common_stock_provenance"
    XNYS_TRADING_CALENDAR = "xnys_trading_calendar"


def _require_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ReadinessManifestError(f"{label} must be an object")
    return raw


def _require_exact_keys(data: Mapping[str, object], expected: set[str], *, label: str) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ReadinessManifestError(f"{label} has invalid fields: {'; '.join(details)}")


def _require_identifier(data: Mapping[str, object], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or READINESS_IDENTIFIER_RE.fullmatch(value) is None:
        raise ReadinessManifestError(f"{field_name} must match {READINESS_IDENTIFIER_RE.pattern!r}")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or READINESS_SHA256_RE.fullmatch(value) is None:
        raise ReadinessManifestError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _file_sha256(path: Path) -> str:
    """Hash one regular file through an open descriptor without following links."""
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ReadinessManifestError(f"readiness evidence path is not a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except ReadinessManifestError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ReadinessManifestError(
                f"readiness evidence path is not a regular file: {path}"
            ) from exc
        raise ReadinessManifestError(f"cannot read readiness evidence file {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    """One operator-provided evidence file or an explicit unavailable entry."""

    path: str | None
    sha256: str | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.path is not None and (
            not isinstance(self.path, str) or not Path(self.path).is_absolute()
        ):
            raise ReadinessManifestError("evidence path must be absolute or null")
        if self.sha256 is not None:
            _require_sha256(self.sha256, label="evidence.sha256")
        if self.path is None and self.sha256 is not None:
            raise ReadinessManifestError("evidence cannot provide sha256 without path")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ReadinessManifestError("evidence.reason must be a non-empty string or null")
        if self.path is None and self.reason is None:
            raise ReadinessManifestError("evidence must explain unavailable evidence")
        if isinstance(self.reason, str):
            object.__setattr__(self, "reason", self.reason.strip())

    @classmethod
    def from_dict(cls, raw: object, *, label: str) -> ReadinessEvidence:
        data = _require_mapping(raw, label=label)
        _require_exact_keys(data, {"path", "sha256", "reason"}, label=label)
        path = data["path"]
        sha256 = data["sha256"]
        reason = data["reason"]
        if path is not None and not isinstance(path, str):
            raise ReadinessManifestError(f"{label}.path must be an absolute path or null")
        if sha256 is not None:
            sha256 = _require_sha256(sha256, label=f"{label}.sha256")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ReadinessManifestError(f"{label}.reason must be a non-empty string or null")
        if path is None and sha256 is not None:
            raise ReadinessManifestError(f"{label} cannot provide sha256 without path")
        if path is not None and not Path(path).is_absolute():
            raise ReadinessManifestError(f"{label}.path must be absolute")
        if path is None and reason is None:
            raise ReadinessManifestError(f"{label} must explain unavailable evidence")
        return cls(
            path=path,
            sha256=sha256,
            reason=reason.strip() if isinstance(reason, str) else None,
        )

    def validate_for_status(self, status: ReadinessStatus, *, label: str) -> None:
        if self.path is None:
            if status is ReadinessStatus.READY:
                raise ReadinessManifestError(f"READY manifest requires {label}.path")
            return
        path = Path(self.path)
        if path.is_symlink() or not path.is_file():
            if status is ReadinessStatus.READY:
                raise ReadinessManifestError(
                    f"READY manifest evidence path is not a regular file: {path}"
                )
            return
        if self.sha256 is None:
            if status is ReadinessStatus.READY:
                raise ReadinessManifestError(f"READY manifest requires {label}.sha256")
            return
        actual = _file_sha256(path)
        if actual != self.sha256:
            raise ReadinessManifestError(
                f"{label} SHA-256 mismatch for {path}: expected {self.sha256}, got {actual}"
            )

    def to_dict(self) -> dict[str, str | None]:
        return {"path": self.path, "reason": self.reason, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReadinessIdentity:
    """Stable identity pinned into autoresearch state."""

    manifest_id: str
    snapshot_id: str
    receipt_sha256: str

    @classmethod
    def from_dict(cls, raw: object) -> ReadinessIdentity:
        data = _require_mapping(raw, label="platform_readiness")
        _require_exact_keys(
            data, {"manifest_id", "snapshot_id", "receipt_sha256"}, label="platform_readiness"
        )
        return cls(
            manifest_id=_require_identifier(data, "manifest_id"),
            snapshot_id=_require_identifier(data, "snapshot_id"),
            receipt_sha256=_require_sha256(data["receipt_sha256"], label="receipt_sha256"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "manifest_id": self.manifest_id,
            "receipt_sha256": self.receipt_sha256,
            "snapshot_id": self.snapshot_id,
        }


@dataclass(frozen=True, slots=True)
class PlatformReadinessManifest:
    schema_version: int
    status: ReadinessStatus
    manifest_id: str
    snapshot_id: str
    evidence: Mapping[EvidenceId, ReadinessEvidence]
    reason: str | None

    def __post_init__(self) -> None:
        """Validate direct construction and freeze the evidence mapping."""
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != READINESS_SCHEMA_VERSION
        ):
            raise ReadinessManifestError(
                f"unsupported platform readiness schema_version: {self.schema_version!r}"
            )
        if not isinstance(self.status, ReadinessStatus):
            raise ReadinessManifestError("status must be READY or BLOCKED")
        for field_name, value in (
            ("manifest_id", self.manifest_id),
            ("snapshot_id", self.snapshot_id),
        ):
            if not isinstance(value, str) or READINESS_IDENTIFIER_RE.fullmatch(value) is None:
                raise ReadinessManifestError(
                    f"{field_name} must match {READINESS_IDENTIFIER_RE.pattern!r}"
                )
        if not isinstance(self.evidence, Mapping):
            raise ReadinessManifestError("evidence must be an object")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ReadinessManifestError("reason must be a non-empty string or null")
        expected_ids = set(EvidenceId)
        actual_ids = set(self.evidence)
        if actual_ids != expected_ids:
            raise ReadinessManifestError("evidence must contain exactly the required evidence IDs")
        normalized_evidence: dict[EvidenceId, ReadinessEvidence] = {}
        for evidence_id in EvidenceId:
            evidence = self.evidence[evidence_id]
            if not isinstance(evidence, ReadinessEvidence):
                raise ReadinessManifestError(
                    f"evidence.{evidence_id.value} must be ReadinessEvidence"
                )
            normalized_evidence[evidence_id] = evidence
        object.__setattr__(self, "evidence", MappingProxyType(normalized_evidence))
        self.validate()

    @classmethod
    def from_dict(cls, raw: object) -> PlatformReadinessManifest:
        data = _require_mapping(raw, label="platform_readiness_manifest")
        _require_exact_keys(
            data,
            {"schema_version", "status", "manifest_id", "snapshot_id", "evidence", "reason"},
            label="platform_readiness_manifest",
        )
        schema_version = data["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ReadinessManifestError("schema_version must be an integer")
        if schema_version != READINESS_SCHEMA_VERSION:
            raise ReadinessManifestError(
                f"unsupported platform readiness schema_version: {schema_version!r}"
            )
        status_raw = data["status"]
        if not isinstance(status_raw, str):
            raise ReadinessManifestError("status must be READY or BLOCKED")
        try:
            status = ReadinessStatus(status_raw)
        except ValueError as exc:
            raise ReadinessManifestError("status must be READY or BLOCKED") from exc
        evidence_data = _require_mapping(data["evidence"], label="evidence")
        _require_exact_keys(
            evidence_data,
            {item.value for item in EvidenceId},
            label="evidence",
        )
        evidence = {
            evidence_id: ReadinessEvidence.from_dict(
                evidence_data[evidence_id.value], label=f"evidence.{evidence_id.value}"
            )
            for evidence_id in EvidenceId
        }
        reason = data["reason"]
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ReadinessManifestError("reason must be a non-empty string or null")
        manifest = cls(
            schema_version=schema_version,
            status=status,
            manifest_id=_require_identifier(data, "manifest_id"),
            snapshot_id=_require_identifier(data, "snapshot_id"),
            evidence=evidence,
            reason=reason.strip() if isinstance(reason, str) else None,
        )
        return manifest

    def validate(self) -> None:
        if self.schema_version != READINESS_SCHEMA_VERSION:
            raise ReadinessManifestError(
                f"unsupported platform readiness schema_version: {self.schema_version!r}"
            )
        if not isinstance(self.status, ReadinessStatus):
            raise ReadinessManifestError("status must be READY or BLOCKED")
        if self.status is ReadinessStatus.READY and self.reason is not None:
            raise ReadinessManifestError("READY manifest must set reason=null")
        if self.status is ReadinessStatus.BLOCKED and not self.reason:
            raise ReadinessManifestError(
                "BLOCKED manifest requires a concrete operator-facing reason"
            )
        for evidence_id, evidence in self.evidence.items():
            evidence.validate_for_status(self.status, label=f"evidence.{evidence_id.value}")

    def identity(self) -> ReadinessIdentity:
        self.validate()
        if self.status is not ReadinessStatus.READY:
            raise ReadinessBlockedError(self.reason or "platform readiness is BLOCKED")
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return ReadinessIdentity(
            manifest_id=self.manifest_id,
            snapshot_id=self.snapshot_id,
            receipt_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def require_ready(self) -> ReadinessIdentity:
        return self.identity()

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence": {
                evidence_id.value: self.evidence[evidence_id].to_dict()
                for evidence_id in EvidenceId
            },
            "manifest_id": self.manifest_id,
            "reason": self.reason,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "status": self.status.value,
        }


def load_platform_readiness(
    path: Path = DEFAULT_PLATFORM_READINESS_PATH,
) -> PlatformReadinessManifest:
    """Load and fully validate the operator-owned readiness manifest."""
    path = path.expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReadinessManifestError(
            f"missing platform readiness manifest: {path}; initialize it explicitly before research"
        ) from exc
    except OSError as exc:
        raise ReadinessManifestError(
            f"failed to read platform readiness manifest {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReadinessManifestError(f"invalid platform readiness JSON: {path}") from exc
    return PlatformReadinessManifest.from_dict(raw)


def validate_state_readiness(
    state_identity: ReadinessIdentity | None,
    manifest: PlatformReadinessManifest,
) -> ReadinessIdentity:
    """Require a READY manifest whose identity matches the persisted state."""
    current = manifest.require_ready()
    if state_identity is None:
        raise ReadinessManifestError(
            "autoresearch state has no pinned platform readiness receipt; "
            "run autoresearch-pin-readiness explicitly before dispatch"
        )
    if state_identity != current:
        raise ReadinessManifestError(
            "autoresearch state platform readiness receipt is stale; "
            "run autoresearch-resume explicitly after reviewing the new manifest"
        )
    return current
