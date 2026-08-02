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
    from gateway.autoresearch_runner import (
        AutoresearchState as AutoresearchState,
    )
    from gateway.autoresearch_runner import (
        ExternalVerificationRetryReceipt as ExternalVerificationRetryReceipt,
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
