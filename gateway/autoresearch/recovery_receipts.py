"""Operator recovery receipt value objects."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES as CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES as CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION as PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.fields import (
    canonical_member_union_digest as canonical_member_union_digest,
)
from gateway.autoresearch.fields import (
    canonical_member_union_manifest as canonical_member_union_manifest,
)
from gateway.autoresearch.secure_io import (
    _path_is_within as _path_is_within,
)
from gateway.autoresearch.secure_io import (
    _require_canonical_absolute_path as _require_canonical_absolute_path,
)
from gateway.autoresearch_readiness import (
    ResearchPanelProbeReceipt as ResearchPanelProbeReceipt,
)

if TYPE_CHECKING:
    from gateway.autoresearch.receipts import (
        UniverseVerificationReceipt as UniverseVerificationReceipt,
    )


def _verify_member_union_manifest(receipt: UniverseVerificationReceipt) -> tuple[str, ...]:
    manifest = receipt.member_union_manifest
    descriptor: int | None = None
    try:
        descriptor = os.open(
            manifest.path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AutoresearchValidationError("member union manifest must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"cannot read member union manifest: {manifest.path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    content = b"".join(chunks)
    if hashlib.sha256(content).hexdigest() != manifest.sha256:
        raise AutoresearchValidationError("member union manifest SHA-256 mismatch")
    try:
        symbols = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise AutoresearchValidationError("member union manifest must be UTF-8") from exc
    canonical = canonical_member_union_manifest(symbols)
    if content != canonical:
        raise AutoresearchValidationError(
            "member union manifest must be uppercase sorted unique UTF-8 lines "
            "with one trailing newline"
        )
    count, digest = canonical_member_union_digest(symbols)
    if count != receipt.member_union_count or digest != receipt.member_union_digest:
        raise AutoresearchValidationError(
            "member union manifest must recompute the persisted count and digest"
        )
    return tuple(symbols)


def _deterministic_quantipy_run_id(iteration: int, commit_sha: str, *, attempt: int) -> str:
    if iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    if re.fullmatch(r"[0-9a-f]{7,64}", commit_sha) is None:
        raise AutoresearchValidationError("implementation_result commit_sha is invalid")
    if attempt < 1:
        raise AutoresearchValidationError("Quantipy verification attempt must be >= 1")
    base = f"autoresearch-i{iteration}-{commit_sha[:12]}"
    return base if attempt == 1 else f"{base}-v{attempt}"


@dataclass(frozen=True, slots=True)
class CanonicalQuantipyRuntimeAttestation:
    """Pinned proof that a canonical run resolves Quantipy from the canonical runtime."""

    root: str
    commit_sha: str
    readiness_quantipy_commit: str
    pyproject_sha256: str
    uv_lock_sha256: str
    venv_prefix: str
    executable_path: str
    executable_sha256: str
    executable_size_bytes: int
    executable_mode: int
    executable_owner_uid: int
    import_path: str
    base_interpreter_path: str
    base_interpreter_version: str
    base_interpreter_sha256: str
    base_interpreter_size_bytes: int
    base_interpreter_mode: int
    base_interpreter_owner_uid: int
    schema_version: int = 3

    def __post_init__(self) -> None:
        if self.schema_version != 3:
            raise AutoresearchValidationError(
                "unsupported canonical Quantipy runtime schema_version"
            )
        root = _require_canonical_absolute_path(self.root, label="canonical Quantipy runtime root")
        if str(root) != self.root:
            raise AutoresearchValidationError("canonical Quantipy runtime root is invalid")
        for label, value in (
            ("commit_sha", self.commit_sha),
            ("readiness_quantipy_commit", self.readiness_quantipy_commit),
        ):
            if re.fullmatch(r"[0-9a-f]{7,64}", value) is None:
                raise AutoresearchValidationError(f"canonical Quantipy runtime {label} is invalid")
        for label, value in (
            ("pyproject_sha256", self.pyproject_sha256),
            ("uv_lock_sha256", self.uv_lock_sha256),
            ("executable_sha256", self.executable_sha256),
            ("base_interpreter_sha256", self.base_interpreter_sha256),
        ):
            _validate_sha256(value, label=f"canonical_quantipy_runtime.{label}")
        for label, value in (
            ("venv_prefix", self.venv_prefix),
            ("executable_path", self.executable_path),
            ("import_path", self.import_path),
            ("base_interpreter_path", self.base_interpreter_path),
        ):
            path = _require_canonical_absolute_path(
                value, label=f"canonical Quantipy runtime {label}"
            )
            if str(path) != value:
                raise AutoresearchValidationError(f"canonical Quantipy runtime {label} is invalid")
        root_path = Path(self.root)
        venv = root_path / ".venv"
        if Path(self.venv_prefix) != venv:
            raise AutoresearchValidationError("canonical Quantipy runtime venv prefix is invalid")
        if Path(self.executable_path) != venv / "bin" / "quantipy":
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable is not the .venv quantipy entrypoint"
            )
        if (
            not isinstance(self.executable_size_bytes, int)
            or isinstance(self.executable_size_bytes, bool)
            or not 0 <= self.executable_size_bytes <= CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable size is invalid"
            )
        if (
            not isinstance(self.executable_mode, int)
            or isinstance(self.executable_mode, bool)
            or not 0 <= self.executable_mode <= 0o777
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable mode is invalid"
            )
        if (
            not isinstance(self.executable_owner_uid, int)
            or isinstance(self.executable_owner_uid, bool)
            or self.executable_owner_uid < 0
            or self.executable_owner_uid != os.getuid()
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable owner UID is invalid"
            )
        if Path(self.import_path) != root_path / "src" / "quantipy":
            raise AutoresearchValidationError(
                "canonical Quantipy runtime import is not the src/quantipy package"
            )
        if _path_is_within(Path(self.base_interpreter_path), root_path):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter must be uv-managed external"
            )
        if not self.base_interpreter_version:
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter version is invalid"
            )
        if (
            not isinstance(self.base_interpreter_size_bytes, int)
            or isinstance(self.base_interpreter_size_bytes, bool)
            or not 0
            <= self.base_interpreter_size_bytes
            <= CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter size is invalid"
            )
        if (
            not isinstance(self.base_interpreter_mode, int)
            or isinstance(self.base_interpreter_mode, bool)
            or not 0 <= self.base_interpreter_mode <= 0o777
            or self.base_interpreter_mode & 0o002
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter mode is invalid"
            )
        if (
            not isinstance(self.base_interpreter_owner_uid, int)
            or isinstance(self.base_interpreter_owner_uid, bool)
            or self.base_interpreter_owner_uid < 0
            or self.base_interpreter_owner_uid != os.getuid()
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter owner UID is invalid"
            )

    @classmethod
    def from_dict(cls, raw: object) -> CanonicalQuantipyRuntimeAttestation:
        data = _ensure_mapping(raw, label="canonical_quantipy_runtime")
        _require_exact_keys(
            data,
            label="canonical_quantipy_runtime",
            expected=(
                "root",
                "commit_sha",
                "readiness_quantipy_commit",
                "pyproject_sha256",
                "uv_lock_sha256",
                "venv_prefix",
                "executable_path",
                "executable_sha256",
                "executable_size_bytes",
                "executable_mode",
                "executable_owner_uid",
                "import_path",
                "base_interpreter_path",
                "base_interpreter_version",
                "base_interpreter_sha256",
                "base_interpreter_size_bytes",
                "base_interpreter_mode",
                "base_interpreter_owner_uid",
                "schema_version",
            ),
        )
        return cls(
            root=_require_str(data, "root"),
            commit_sha=_require_str(data, "commit_sha"),
            readiness_quantipy_commit=_require_str(data, "readiness_quantipy_commit"),
            pyproject_sha256=_require_sha256(data, "pyproject_sha256"),
            uv_lock_sha256=_require_sha256(data, "uv_lock_sha256"),
            venv_prefix=_require_str(data, "venv_prefix"),
            executable_path=_require_str(data, "executable_path"),
            executable_sha256=_require_sha256(data, "executable_sha256"),
            executable_size_bytes=_require_int(data, "executable_size_bytes"),
            executable_mode=_require_int(data, "executable_mode"),
            executable_owner_uid=_require_int(data, "executable_owner_uid"),
            import_path=_require_str(data, "import_path"),
            base_interpreter_path=_require_str(data, "base_interpreter_path"),
            base_interpreter_version=_require_str(data, "base_interpreter_version"),
            base_interpreter_sha256=_require_sha256(data, "base_interpreter_sha256"),
            base_interpreter_size_bytes=_require_int(data, "base_interpreter_size_bytes"),
            base_interpreter_mode=_require_int(data, "base_interpreter_mode"),
            base_interpreter_owner_uid=_require_int(data, "base_interpreter_owner_uid"),
            schema_version=_require_int(data, "schema_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "commit_sha": self.commit_sha,
            "readiness_quantipy_commit": self.readiness_quantipy_commit,
            "pyproject_sha256": self.pyproject_sha256,
            "uv_lock_sha256": self.uv_lock_sha256,
            "venv_prefix": self.venv_prefix,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "executable_size_bytes": self.executable_size_bytes,
            "executable_mode": self.executable_mode,
            "executable_owner_uid": self.executable_owner_uid,
            "import_path": self.import_path,
            "base_interpreter_path": self.base_interpreter_path,
            "base_interpreter_version": self.base_interpreter_version,
            "base_interpreter_sha256": self.base_interpreter_sha256,
            "base_interpreter_size_bytes": self.base_interpreter_size_bytes,
            "base_interpreter_mode": self.base_interpreter_mode,
            "base_interpreter_owner_uid": self.base_interpreter_owner_uid,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class PlatformRuntimeRecoveryReceipt:
    """Versioned, immutable authorization for the exact historical v4→v5 repair."""

    expected_run_id: str
    implementation_commit: str
    implementation_manifest_sha256: str
    verification_history_sha256: tuple[str, ...]
    interruption_sha256: str
    prior_retry_receipt_sha256: str
    v4_verification_sha256: str
    v4_detached_run_manifest_sha256: str
    v4_detached_run_status_sha256: str
    old_worktree_runtime_commit: str
    runtime: CanonicalQuantipyRuntimeAttestation
    execution_command_sha256: str
    probe: ResearchPanelProbeReceipt
    operator_reason: str
    schema_version: int = PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "unsupported platform runtime recovery receipt schema_version"
            )
        if (
            re.fullmatch(r"autoresearch-i[1-9][0-9]*-[0-9a-f]{7,12}-v5", self.expected_run_id)
            is None
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery receipt expected_run_id is invalid"
            )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.implementation_commit) is None:
            raise AutoresearchValidationError(
                "platform runtime recovery implementation_commit is invalid"
            )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.old_worktree_runtime_commit) is None:
            raise AutoresearchValidationError(
                "platform runtime recovery old runtime commit is invalid"
            )
        if len(self.verification_history_sha256) != 3:
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires exact v1/v2/v4 verification history"
            )
        for index, digest in enumerate(
            (
                *self.verification_history_sha256,
                self.implementation_manifest_sha256,
                self.interruption_sha256,
                self.prior_retry_receipt_sha256,
                self.v4_verification_sha256,
                self.v4_detached_run_manifest_sha256,
                self.v4_detached_run_status_sha256,
                self.execution_command_sha256,
            ),
            start=1,
        ):
            _validate_sha256(digest, label=f"platform_runtime_recovery_receipt.digest[{index}]")
        if not isinstance(self.runtime, CanonicalQuantipyRuntimeAttestation):
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires runtime attestation"
            )
        if not isinstance(self.probe, ResearchPanelProbeReceipt):
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires a research-panel probe"
            )
        if not self.operator_reason or self.operator_reason.strip() != self.operator_reason:
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires a trimmed operator reason"
            )

    @classmethod
    def from_dict(cls, raw: object) -> PlatformRuntimeRecoveryReceipt:
        data = _ensure_mapping(raw, label="platform_runtime_recovery_receipt")
        _require_exact_keys(
            data,
            label="platform_runtime_recovery_receipt",
            expected=(
                "expected_run_id",
                "implementation_commit",
                "implementation_manifest_sha256",
                "verification_history_sha256",
                "interruption_sha256",
                "prior_retry_receipt_sha256",
                "v4_verification_sha256",
                "v4_detached_run_manifest_sha256",
                "v4_detached_run_status_sha256",
                "old_worktree_runtime_commit",
                "runtime",
                "execution_command_sha256",
                "probe",
                "operator_reason",
                "schema_version",
            ),
        )
        history = data["verification_history_sha256"]
        if not isinstance(history, list):
            raise AutoresearchValidationError(
                "platform runtime recovery verification_history_sha256 must be a list"
            )
        return cls(
            expected_run_id=_require_str(data, "expected_run_id"),
            implementation_commit=_require_str(data, "implementation_commit"),
            implementation_manifest_sha256=_require_sha256(data, "implementation_manifest_sha256"),
            verification_history_sha256=tuple(
                _require_sha256({"value": digest}, "value") for digest in history
            ),
            interruption_sha256=_require_sha256(data, "interruption_sha256"),
            prior_retry_receipt_sha256=_require_sha256(data, "prior_retry_receipt_sha256"),
            v4_verification_sha256=_require_sha256(data, "v4_verification_sha256"),
            v4_detached_run_manifest_sha256=_require_sha256(
                data, "v4_detached_run_manifest_sha256"
            ),
            v4_detached_run_status_sha256=_require_sha256(data, "v4_detached_run_status_sha256"),
            old_worktree_runtime_commit=_require_str(data, "old_worktree_runtime_commit"),
            runtime=CanonicalQuantipyRuntimeAttestation.from_dict(data["runtime"]),
            execution_command_sha256=_require_sha256(data, "execution_command_sha256"),
            probe=ResearchPanelProbeReceipt.from_dict(data["probe"]),
            operator_reason=_require_str(data, "operator_reason"),
            schema_version=_require_int(data, "schema_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_run_id": self.expected_run_id,
            "implementation_commit": self.implementation_commit,
            "implementation_manifest_sha256": self.implementation_manifest_sha256,
            "verification_history_sha256": list(self.verification_history_sha256),
            "interruption_sha256": self.interruption_sha256,
            "prior_retry_receipt_sha256": self.prior_retry_receipt_sha256,
            "v4_verification_sha256": self.v4_verification_sha256,
            "v4_detached_run_manifest_sha256": self.v4_detached_run_manifest_sha256,
            "v4_detached_run_status_sha256": self.v4_detached_run_status_sha256,
            "old_worktree_runtime_commit": self.old_worktree_runtime_commit,
            "runtime": self.runtime.to_dict(),
            "execution_command_sha256": self.execution_command_sha256,
            "probe": self.probe.to_dict(),
            "operator_reason": self.operator_reason,
            "schema_version": self.schema_version,
        }
