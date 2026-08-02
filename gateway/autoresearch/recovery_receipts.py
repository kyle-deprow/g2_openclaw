"""Operator recovery receipt value objects."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES as CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES as CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION as EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION as INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION as LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION as PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.enums import (
    FixTriggerPhase as FixTriggerPhase,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.enums import (
    VerificationStatus as VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.evidence import (
    _expected_local_research_panel_http_error_message as _expected_local_research_panel_http_error_message,  # noqa: E501
)
from gateway.autoresearch.fields import (
    _canonical_json_digest as _canonical_json_digest,
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
    from gateway.autoresearch.artifacts import (
        ImplementationResultArtifact as ImplementationResultArtifact,
    )
    from gateway.autoresearch.artifacts import (
        VerificationResultArtifact as VerificationResultArtifact,
    )
    from gateway.autoresearch.receipts import (
        UniverseVerificationReceipt as UniverseVerificationReceipt,
    )
    from gateway.autoresearch.state import (
        AutoresearchState as AutoresearchState,
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


def _expected_quantipy_verification_run_id(state: AutoresearchState, commit_sha: str) -> str:
    receipt = state.external_verification_retry_receipt
    if receipt is not None:
        expected = _deterministic_quantipy_run_id(
            state.iteration,
            commit_sha,
            attempt=receipt.retry_attempt,
        )
        if receipt.expected_run_id != expected:
            raise AutoresearchValidationError(
                "external verification retry receipt run ID is stale for the implementation commit"
            )
        return receipt.expected_run_id
    return _deterministic_quantipy_run_id(state.iteration, commit_sha, attempt=1)


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


def _is_manifest_bound_legacy_local_research_panel_http_413(
    state: AutoresearchState,
    message: str,
) -> bool:
    """Accept solely the manifest-bound httpx 413 text from the preserved v2 artifact."""
    expected = _expected_local_research_panel_http_error_message(
        state,
        status=413,
        reason="Request Entity Too Large",
    )
    return expected is not None and message == expected


def _is_historically_authorized_local_research_panel_http_404(
    state: AutoresearchState,
    message: str,
) -> bool:
    """Validate the narrow 404 contract used only to issue the original v2 receipt."""
    expected = _expected_local_research_panel_http_error_message(
        state,
        status=404,
        reason="Not Found",
    )
    return expected is not None and message == expected


def _validate_external_verification_retry_history_artifact(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
    *,
    attempt: int,
    implementation: ImplementationResultArtifact,
) -> None:
    evidence = artifact.quantipy_experiment_evidence
    failure = evidence.failure if evidence is not None else None
    if (
        artifact.status is not VerificationStatus.TEST_FAILURE
        or evidence is None
        or evidence.run_id
        != _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=attempt
        )
        or evidence.success
        or evidence.panel is not None
        or evidence.completed_stages
        or evidence.terminal_stage is not None
        or evidence.terminal_status is not None
        or failure is None
        or failure.category != "panel"
    ):
        raise AutoresearchValidationError(
            "external verification retry verification history topology is invalid"
        )
    if attempt == 4 and state.platform_runtime_recovery_receipt is not None:
        exact_failure = (
            failure.message == "ExperimentPanelError: Research panel receipt is invalid."
        )
        status = "exact platform runtime v4 panel receipt"
    else:
        exact_failure = (
            _is_historically_authorized_local_research_panel_http_404
            if attempt == 1
            else _is_manifest_bound_legacy_local_research_panel_http_413
        )(state, failure.message)
        status = (
            "historical local research-panel HTTP 404"
            if attempt == 1
            else "exact local research-panel HTTP 413"
        )
    if not exact_failure:
        raise AutoresearchValidationError(
            f"external verification retry verification history requires the {status} failure"
        )


def _validate_external_verification_retry_history(
    state: AutoresearchState,
    receipt: ExternalVerificationRetryReceipt,
) -> None:
    """Fail closed unless every sealed retry artifact forms one exact chain."""
    interruptions = state.interrupted_verification_history
    if (
        interruptions
        and receipt.schema_version != INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
    ):
        raise AutoresearchValidationError(
            "interrupted verification history requires the interruption-aware retry receipt"
        )
    if (
        not interruptions
        and receipt.schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
    ):
        raise AutoresearchValidationError(
            "interruption-aware retry receipt requires an interruption history"
        )
    interruption_count = len(interruptions)
    pending_history_length = receipt.retry_attempt - 1 - interruption_count
    sealed_history_length = receipt.retry_attempt - interruption_count
    if pending_history_length < 1:
        raise AutoresearchValidationError(
            "external verification retry interruption topology is invalid"
        )
    history_length = len(state.verification_history)
    if history_length not in (pending_history_length, sealed_history_length):
        raise AutoresearchValidationError(
            "external verification retry verification history topology is invalid"
        )
    if history_length == pending_history_length and state.phase is not Phase.VERIFICATION:
        raise AutoresearchValidationError(
            "external verification retry pending verification history topology is invalid"
        )
    if history_length == sealed_history_length and state.phase is Phase.VERIFICATION:
        raise AutoresearchValidationError(
            "external verification retry sealed verification history topology is invalid"
        )
    if state.fix_history or state.verification_fix_attempts:
        raise AutoresearchValidationError(
            "external verification retry verification history topology permits no fixer attempts"
        )
    implementation = state.implementation_result
    assert implementation is not None
    interruption_attempts = tuple(
        interruption.interrupted_attempt for interruption in interruptions
    )
    if interruption_attempts != tuple(sorted(interruption_attempts)) or len(
        set(interruption_attempts)
    ) != len(interruption_attempts):
        raise AutoresearchValidationError(
            "interrupted verification history attempts must be ordered and unique"
        )
    for index, artifact in enumerate(state.verification_history, start=1):
        attempt = index + sum(
            1 for interruption_attempt in interruption_attempts if interruption_attempt <= index
        )
        _validate_external_verification_retry_history_artifact(
            state,
            artifact,
            attempt=attempt,
            implementation=implementation,
        )
    prior = state.verification_history[pending_history_length - 1]
    if receipt.prior_verification_sha256 != _canonical_json_digest(prior.to_dict()):
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind the immediately prior artifact"
        )
    if receipt.schema_version == LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
        return
    expected_history_sha256 = tuple(
        _canonical_json_digest(artifact.to_dict())
        for artifact in state.verification_history[:pending_history_length]
    )
    if receipt.verification_history_sha256 != expected_history_sha256:
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind the complete ordered "
            "verification history"
        )
    expected_interruption_history_sha256 = tuple(
        _canonical_json_digest(interruption.to_dict()) for interruption in interruptions
    )
    if receipt.schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION and (
        receipt.interruption_history_sha256 != expected_interruption_history_sha256
    ):
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind the complete ordered "
            "interruption history"
        )


def _validate_external_verification_retry_eligibility(state: AutoresearchState) -> int:
    if state.phase is not Phase.FIX_TEST:
        raise AutoresearchValidationError("external verification retry requires fix_test phase")
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        raise AutoresearchValidationError(
            "external verification retry requires an ALPHA_RESEARCH iteration"
        )
    if state.pending_fix_trigger is not FixTriggerPhase.VERIFICATION:
        raise AutoresearchValidationError(
            "external verification retry requires a verification-triggered fix_test"
        )
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "external verification retry requires the preserved implementation_result"
        )
    if (
        state.latest_verification is None
        or state.latest_verification.status is not VerificationStatus.TEST_FAILURE
    ):
        raise AutoresearchValidationError(
            "external verification retry requires a typed TEST_FAILURE verification"
        )
    current_receipt = state.external_verification_retry_receipt
    if current_receipt is not None:
        _validate_external_verification_retry_history(state, current_receipt)
    evidence = state.latest_verification.quantipy_experiment_evidence
    if evidence is None:
        raise AutoresearchValidationError(
            "external verification retry requires a completed failed Quantipy run artifact"
        )
    failure = evidence.failure
    if current_receipt is None:
        if (
            len(state.verification_history) != 1
            or state.fix_history
            or state.verification_fix_attempts
        ):
            raise AutoresearchValidationError(
                "external verification retry without a prior retry requires the initial "
                "panel failure"
            )
        if (
            failure is None
            or failure.category != "panel"
            or not _is_historically_authorized_local_research_panel_http_404(state, failure.message)
        ):
            raise AutoresearchValidationError(
                "external verification retry requires the historical local research-panel HTTP "
                "404 failure"
            )
        return 2
    if (
        failure is None
        or failure.category != "panel"
        or not _is_manifest_bound_legacy_local_research_panel_http_413(state, failure.message)
    ):
        raise AutoresearchValidationError(
            "external verification retry requires the exact local research-panel HTTP 413 failure"
        )
    if current_receipt.retry_attempt == 3:
        raise AutoresearchValidationError(
            "interrupted verification recovery accepts only the exact pending v3 topology"
        )
    if current_receipt.retry_attempt == 4:
        raise AutoresearchValidationError(
            "v4 platform receipt failure requires operator platform runtime recovery"
        )
    if evidence.success or evidence.panel is not None or evidence.completed_stages:
        raise AutoresearchValidationError(
            "external verification retry requires a failed pre-stage panel run without evidence"
        )
    if evidence.run_id != current_receipt.expected_run_id:
        raise AutoresearchValidationError(
            "external verification retry requires the prior expected Quantipy run artifact"
        )
    return current_receipt.retry_attempt + 1


@dataclass(frozen=True, slots=True)
class InterruptedVerificationAttemptReceipt:
    """Immutable proof that one exact detached verification attempt was stopped."""

    expected_run_id: str
    interrupted_attempt: int
    implementation_commit: str
    implementation_manifest_sha256: str
    detached_run_directory: str
    detached_run_manifest_sha256: str
    detached_run_status_sha256: str
    state_sha256: str
    state_reference_sha256: str
    instruction_manifest_sha256: str
    prior_retry_receipt_sha256: str
    prior_retry_receipt: ExternalVerificationRetryReceipt
    verification_history_sha256: tuple[str, ...]
    operator_reason: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AutoresearchValidationError(
                "unsupported interrupted verification attempt receipt schema_version"
            )
        if self.interrupted_attempt != 3:
            raise AutoresearchValidationError(
                "interrupted verification recovery accepts only the current v3 attempt"
            )
        if (
            re.fullmatch(
                rf"autoresearch-i[1-9][0-9]*-{self.implementation_commit[:12]}-v{self.interrupted_attempt}",
                self.expected_run_id,
            )
            is None
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt expected_run_id is invalid"
            )
        if (
            not isinstance(self.detached_run_directory, str)
            or not Path(self.detached_run_directory).is_absolute()
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt detached_run_directory must be absolute"
            )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.implementation_commit) is None:
            raise AutoresearchValidationError(
                "interrupted verification receipt implementation_commit is invalid"
            )
        for label, digest in (
            ("implementation_manifest_sha256", self.implementation_manifest_sha256),
            ("detached_run_manifest_sha256", self.detached_run_manifest_sha256),
            ("detached_run_status_sha256", self.detached_run_status_sha256),
            ("state_sha256", self.state_sha256),
            ("state_reference_sha256", self.state_reference_sha256),
            ("instruction_manifest_sha256", self.instruction_manifest_sha256),
            ("prior_retry_receipt_sha256", self.prior_retry_receipt_sha256),
        ):
            _validate_sha256(digest, label=f"interrupted_verification_attempt_receipt.{label}")
        if not isinstance(self.prior_retry_receipt, ExternalVerificationRetryReceipt):
            raise AutoresearchValidationError(
                "interrupted verification receipt requires the immutable prior retry receipt"
            )
        if (
            self.prior_retry_receipt.schema_version
            != EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt requires a schema-v2 prior retry receipt"
            )
        if (
            self.prior_retry_receipt.retry_attempt != self.interrupted_attempt
            or self.prior_retry_receipt.expected_run_id != self.expected_run_id
            or self.prior_retry_receipt.implementation_commit != self.implementation_commit
            or self.prior_retry_receipt.manifest_sha256 != self.implementation_manifest_sha256
            or self.prior_retry_receipt_sha256
            != _canonical_json_digest(self.prior_retry_receipt.to_dict())
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt prior retry receipt binding is invalid"
            )
        if (
            not isinstance(self.verification_history_sha256, tuple)
            or len(self.verification_history_sha256) != 2
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt requires the ordered v1/v2 history"
            )
        for index, digest in enumerate(self.verification_history_sha256, start=1):
            _validate_sha256(
                digest,
                label=(
                    f"interrupted_verification_attempt_receipt.verification_history_sha256[{index}]"
                ),
            )
        if not self.operator_reason or self.operator_reason.strip() != self.operator_reason:
            raise AutoresearchValidationError(
                "interrupted verification receipt requires a trimmed operator reason"
            )

    @classmethod
    def from_dict(cls, raw: object) -> InterruptedVerificationAttemptReceipt:
        data = _ensure_mapping(raw, label="interrupted_verification_attempt_receipt")
        _require_exact_keys(
            data,
            label="interrupted_verification_attempt_receipt",
            expected=(
                "expected_run_id",
                "interrupted_attempt",
                "implementation_commit",
                "implementation_manifest_sha256",
                "detached_run_directory",
                "detached_run_manifest_sha256",
                "detached_run_status_sha256",
                "state_sha256",
                "state_reference_sha256",
                "instruction_manifest_sha256",
                "prior_retry_receipt_sha256",
                "prior_retry_receipt",
                "verification_history_sha256",
                "operator_reason",
                "schema_version",
            ),
        )
        history = data["verification_history_sha256"]
        if not isinstance(history, list):
            raise AutoresearchValidationError(
                "interrupted verification receipt verification_history_sha256 must be a list"
            )
        return cls(
            expected_run_id=_require_str(data, "expected_run_id"),
            interrupted_attempt=_require_int(data, "interrupted_attempt"),
            implementation_commit=_require_str(data, "implementation_commit"),
            implementation_manifest_sha256=_require_sha256(data, "implementation_manifest_sha256"),
            detached_run_directory=_require_str(data, "detached_run_directory"),
            detached_run_manifest_sha256=_require_sha256(data, "detached_run_manifest_sha256"),
            detached_run_status_sha256=_require_sha256(data, "detached_run_status_sha256"),
            state_sha256=_require_sha256(data, "state_sha256"),
            state_reference_sha256=_require_sha256(data, "state_reference_sha256"),
            instruction_manifest_sha256=_require_sha256(data, "instruction_manifest_sha256"),
            prior_retry_receipt_sha256=_require_sha256(data, "prior_retry_receipt_sha256"),
            prior_retry_receipt=ExternalVerificationRetryReceipt.from_dict(
                data["prior_retry_receipt"]
            ),
            verification_history_sha256=tuple(
                _require_sha256({"value": digest}, "value") for digest in history
            ),
            operator_reason=_require_str(data, "operator_reason"),
            schema_version=_require_int(data, "schema_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_run_id": self.expected_run_id,
            "interrupted_attempt": self.interrupted_attempt,
            "implementation_commit": self.implementation_commit,
            "implementation_manifest_sha256": self.implementation_manifest_sha256,
            "detached_run_directory": self.detached_run_directory,
            "detached_run_manifest_sha256": self.detached_run_manifest_sha256,
            "detached_run_status_sha256": self.detached_run_status_sha256,
            "state_sha256": self.state_sha256,
            "state_reference_sha256": self.state_reference_sha256,
            "instruction_manifest_sha256": self.instruction_manifest_sha256,
            "prior_retry_receipt_sha256": self.prior_retry_receipt_sha256,
            "prior_retry_receipt": self.prior_retry_receipt.to_dict(),
            "verification_history_sha256": list(self.verification_history_sha256),
            "operator_reason": self.operator_reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExternalVerificationRetryReceipt:
    """One operator-authorized retry of an externally failed verification run."""

    expected_run_id: str
    prior_verification_sha256: str
    probe: ResearchPanelProbeReceipt
    retry_attempt: int
    implementation_commit: str
    manifest_sha256: str
    readiness_manifest_id: str
    readiness_snapshot_id: str
    operator_reason: str
    verification_history_sha256: tuple[str, ...] = field(default_factory=tuple)
    interruption_history_sha256: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in {
            LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            raise AutoresearchValidationError(
                "unsupported external verification retry receipt schema_version"
            )
        if self.retry_attempt not in {2, 3, 4, 5}:
            raise AutoresearchValidationError(
                "external verification retry receipt attempt is not supported"
            )
        _validate_sha256(
            self.prior_verification_sha256,
            label="external_verification_retry_receipt.prior_verification_sha256",
        )
        if not isinstance(self.verification_history_sha256, tuple):
            raise AutoresearchValidationError(
                "external verification retry receipt verification_history_sha256 must be a tuple"
            )
        if self.schema_version == LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            if (
                self.retry_attempt != 2
                or self.verification_history_sha256
                or self.interruption_history_sha256
            ):
                raise AutoresearchValidationError(
                    "legacy external verification retry receipt only accepts the live v2 bootstrap"
                )
        elif self.schema_version == EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            if self.retry_attempt not in {2, 3}:
                raise AutoresearchValidationError(
                    "schema-v2 external verification retry receipt only accepts v2 or v3"
                )
            if len(self.verification_history_sha256) != self.retry_attempt - 1:
                raise AutoresearchValidationError(
                    "external verification retry receipt must bind every prior verification "
                    "artifact"
                )
            if self.interruption_history_sha256:
                raise AutoresearchValidationError(
                    "schema-v2 external verification retry receipt cannot bind interruptions"
                )
        else:
            if self.retry_attempt not in {4, 5}:
                raise AutoresearchValidationError(
                    "interrupted external verification retry receipt only accepts v4 or v5"
                )
            if not isinstance(self.interruption_history_sha256, tuple):
                raise AutoresearchValidationError(
                    "interrupted external verification retry receipt interruption "
                    "history must be a tuple"
                )
            if (
                len(self.verification_history_sha256) + len(self.interruption_history_sha256)
                != self.retry_attempt - 1
            ):
                raise AutoresearchValidationError(
                    "interrupted external verification retry receipt must bind every prior attempt"
                )
            for history_name, history in (
                ("verification_history_sha256", self.verification_history_sha256),
                ("interruption_history_sha256", self.interruption_history_sha256),
            ):
                for index, digest in enumerate(history, start=1):
                    _validate_sha256(
                        digest,
                        label=f"external_verification_retry_receipt.{history_name}[{index}]",
                    )
            for index, digest in enumerate(self.verification_history_sha256, start=1):
                _validate_sha256(
                    digest,
                    label=(
                        f"external_verification_retry_receipt.verification_history_sha256[{index}]"
                    ),
                )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.implementation_commit) is None:
            raise AutoresearchValidationError("implementation_commit is invalid")
        _validate_sha256(self.manifest_sha256, label="manifest_sha256")
        if not self.readiness_manifest_id or not self.readiness_snapshot_id:
            raise AutoresearchValidationError(
                "external verification retry receipt requires readiness identities"
            )
        if not self.operator_reason or self.operator_reason.strip() != self.operator_reason:
            raise AutoresearchValidationError(
                "external verification retry receipt requires a trimmed operator reason"
            )
        if (
            re.fullmatch(
                rf"autoresearch-i[1-9][0-9]*-[0-9a-f]{{7,12}}-v{self.retry_attempt}",
                self.expected_run_id,
            )
            is None
        ):
            raise AutoresearchValidationError(
                "external verification retry receipt expected_run_id is invalid"
            )
        if not isinstance(self.probe, ResearchPanelProbeReceipt):
            raise AutoresearchValidationError(
                "external verification retry receipt requires a research-panel probe"
            )

    @classmethod
    def for_state(
        cls,
        state: AutoresearchState,
        probe: ResearchPanelProbeReceipt,
        operator_reason: str,
    ) -> ExternalVerificationRetryReceipt:
        attempt = _validate_external_verification_retry_eligibility(state)
        assert state.implementation_result is not None
        assert state.latest_verification is not None
        assert state.platform_readiness is not None
        commit_sha = state.implementation_result.commit_sha
        return cls(
            expected_run_id=_deterministic_quantipy_run_id(
                state.iteration,
                commit_sha,
                attempt=attempt,
            ),
            prior_verification_sha256=_canonical_json_digest(state.latest_verification.to_dict()),
            probe=probe,
            retry_attempt=attempt,
            implementation_commit=commit_sha,
            manifest_sha256=state.implementation_result.experiment_manifest_sha256,
            readiness_manifest_id=state.platform_readiness.manifest_id,
            readiness_snapshot_id=state.platform_readiness.snapshot_id,
            operator_reason=operator_reason,
            verification_history_sha256=tuple(
                _canonical_json_digest(artifact.to_dict())
                for artifact in state.verification_history
            ),
            interruption_history_sha256=tuple(
                _canonical_json_digest(interruption.to_dict())
                for interruption in state.interrupted_verification_history
            ),
            schema_version=(
                INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
                if state.interrupted_verification_history
                else EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
            ),
        )

    @classmethod
    def from_dict(cls, raw: object) -> ExternalVerificationRetryReceipt:
        data = _ensure_mapping(raw, label="external_verification_retry_receipt")
        schema_version = _require_int(data, "schema_version")
        expected: tuple[str, ...] = (
            "expected_run_id",
            "prior_verification_sha256",
            "probe",
            "retry_attempt",
            "implementation_commit",
            "manifest_sha256",
            "readiness_manifest_id",
            "readiness_snapshot_id",
            "operator_reason",
            "schema_version",
        )
        if schema_version in {
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            expected = (*expected, "verification_history_sha256")
        if schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            expected = (*expected, "interruption_history_sha256")
        if schema_version not in {
            LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            raise AutoresearchValidationError(
                "unsupported external verification retry receipt schema_version"
            )
        _require_exact_keys(
            data,
            label="external_verification_retry_receipt",
            expected=expected,
        )
        history_raw = data.get("verification_history_sha256", [])
        if not isinstance(history_raw, list):
            raise AutoresearchValidationError(
                "external verification retry receipt verification_history_sha256 must be a list"
            )
        history: list[str] = []
        for index, digest in enumerate(history_raw):
            if not isinstance(digest, str):
                raise AutoresearchValidationError(
                    "external verification retry receipt verification_history_sha256 "
                    f"entry {index} must be a SHA-256"
                )
            _validate_sha256(
                digest,
                label=(f"external_verification_retry_receipt.verification_history_sha256[{index}]"),
            )
            history.append(digest)
        interruptions_raw = data.get("interruption_history_sha256", [])
        if not isinstance(interruptions_raw, list):
            raise AutoresearchValidationError(
                "external verification retry receipt interruption_history_sha256 must be a list"
            )
        interruptions = tuple(
            _require_sha256({"value": digest}, "value") for digest in interruptions_raw
        )
        return cls(
            expected_run_id=_require_str(data, "expected_run_id"),
            prior_verification_sha256=_require_sha256(data, "prior_verification_sha256"),
            probe=ResearchPanelProbeReceipt.from_dict(data["probe"]),
            retry_attempt=_require_int(data, "retry_attempt"),
            implementation_commit=_require_str(data, "implementation_commit"),
            manifest_sha256=_require_sha256(data, "manifest_sha256"),
            readiness_manifest_id=_require_str(data, "readiness_manifest_id"),
            readiness_snapshot_id=_require_str(data, "readiness_snapshot_id"),
            operator_reason=_require_str(data, "operator_reason"),
            verification_history_sha256=tuple(history),
            interruption_history_sha256=interruptions,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, object]:
        receipt = {
            "expected_run_id": self.expected_run_id,
            "prior_verification_sha256": self.prior_verification_sha256,
            "probe": self.probe.to_dict(),
            "retry_attempt": self.retry_attempt,
            "implementation_commit": self.implementation_commit,
            "manifest_sha256": self.manifest_sha256,
            "readiness_manifest_id": self.readiness_manifest_id,
            "readiness_snapshot_id": self.readiness_snapshot_id,
            "operator_reason": self.operator_reason,
            "schema_version": self.schema_version,
        }
        if self.schema_version in {
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            receipt["verification_history_sha256"] = list(self.verification_history_sha256)
        if self.schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            receipt["interruption_history_sha256"] = list(self.interruption_history_sha256)
        return receipt
