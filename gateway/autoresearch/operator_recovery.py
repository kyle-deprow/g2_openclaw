"""Operator-owned recovery and retry workflows for autoresearch."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch import attestation as attestation_module
from gateway.autoresearch import constants
from gateway.autoresearch import persistence as persistence_module
from gateway.autoresearch import transitions as transitions_module
from gateway.autoresearch.artifacts import (
    QuantipyExperimentEvidence as QuantipyExperimentEvidence,
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
    _canonical_quantipy_manifest_sha256 as _canonical_quantipy_manifest_sha256,
)
from gateway.autoresearch.evidence import (
    _legacy_quantipy_bash_command as _legacy_quantipy_bash_command,
)
from gateway.autoresearch.evidence import (
    _run_failure_from_mapping as _run_failure_from_mapping,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_execution_source_against_commit as _validate_quantipy_execution_source_against_commit,  # noqa: E501
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_experiment_evidence as _validate_quantipy_experiment_evidence,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_run_envelope as _validate_quantipy_run_envelope,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_v2_manifest as _validate_quantipy_v2_manifest,
)
from gateway.autoresearch.evidence import (
    build_quantipy_execution_contract as build_quantipy_execution_contract,
)
from gateway.autoresearch.fields import (
    _canonical_json_digest as _canonical_json_digest,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.gitops import (
    _require_clean_git_worktree as _require_clean_git_worktree,
)
from gateway.autoresearch.gitops import (
    _require_strict_canonical_workspace_path as _require_strict_canonical_workspace_path,
)
from gateway.autoresearch.gitops import (
    _resolve_git_commit as _resolve_git_commit,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy as AutoresearchPolicy,
)
from gateway.autoresearch.policy import (
    ReceiptCatalog as ReceiptCatalog,
)
from gateway.autoresearch.recovery_receipts import (
    ExternalVerificationRetryReceipt as ExternalVerificationRetryReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    InterruptedVerificationAttemptReceipt as InterruptedVerificationAttemptReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    PlatformRuntimeRecoveryReceipt as PlatformRuntimeRecoveryReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    _deterministic_quantipy_run_id as _deterministic_quantipy_run_id,
)
from gateway.autoresearch.recovery_receipts import (
    _is_manifest_bound_legacy_local_research_panel_http_413 as _is_manifest_bound_legacy_local_research_panel_http_413,  # noqa: E501
)
from gateway.autoresearch.recovery_receipts import (
    _validate_external_verification_retry_eligibility as _validate_external_verification_retry_eligibility,  # noqa: E501
)
from gateway.autoresearch.secure_io import (
    _secure_open_snapshot as _secure_open_snapshot,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)
from gateway.autoresearch_readiness import (
    ReadinessIdentity as ReadinessIdentity,
)
from gateway.autoresearch_readiness import (
    ResearchPanelProbeReceipt as ResearchPanelProbeReceipt,
)
from gateway.autoresearch_systemd import (
    SystemdUnitStateError as SystemdUnitStateError,
)
from gateway.autoresearch_systemd import (
    systemd_unit_is_active as systemd_unit_is_active,
)

if TYPE_CHECKING:
    from gateway.autoresearch_runs import RunRecord


def retry_external_verification(
    state: AutoresearchState,
    receipt: ExternalVerificationRetryReceipt,
) -> AutoresearchState:
    """Resume only the current external TEST_FAILURE without invoking the alpha fixer."""
    _validate_external_verification_retry_eligibility(state)
    expected = ExternalVerificationRetryReceipt.for_state(
        state,
        receipt.probe,
        receipt.operator_reason,
    )
    if receipt != expected:
        raise AutoresearchValidationError(
            "external verification retry receipt does not match the current failed state"
        )
    return replace(
        state,
        external_verification_retry_receipt=receipt,
        pending_fix_trigger=None,
        phase=Phase.VERIFICATION,
    )


def retry_external_verification_state_file(
    state_path: Path,
    probe: ResearchPanelProbeReceipt,
    *,
    operator_reason: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> AutoresearchState:
    """Atomically authorize one bounded operator retry of the current panel failure."""
    resolved_path = state_path.expanduser().resolve(strict=False)
    with persistence_module._exclusive_state_locks((resolved_path,)):
        raw = persistence_module._load_state_raw(resolved_path)
        schema_version = _require_int(raw, "schema_version")
        if schema_version != constants.AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "external verification retry accepts only the compatible schema-v"
                f"{constants.AUTORESEARCH_STATE_SCHEMA_VERSION} state"
            )
        state = AutoresearchState.from_dict(raw)
        transitions_module._validate_state(state, policy, validation_context)
        state = _materialize_attested_pending_retry_failure(
            state,
            policy=policy,
            validation_context=validation_context,
        )
        prior_verification = state.latest_verification
        if prior_verification is None:
            raise AutoresearchValidationError(
                "external verification retry requires a preserved verification artifact"
            )
        _validate_quantipy_experiment_evidence(
            state,
            prior_verification,
            validation_context=validation_context,
        )
        receipt = ExternalVerificationRetryReceipt.for_state(state, probe, operator_reason)
        retried = retry_external_verification(state, receipt)
        transitions_module._validate_state(retried, policy, validation_context)
        persistence_module._atomic_save_state_file(resolved_path, retried)
        return retried


def _default_systemd_is_active(unit: str) -> bool:
    def run_command(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutoresearchValidationError(
                f"cannot inspect interrupted detached systemd unit {unit}: {exc}"
            ) from exc

    try:
        return systemd_unit_is_active(unit, run_command=run_command)
    except SystemdUnitStateError as exc:
        raise AutoresearchValidationError(
            f"cannot prove interrupted detached systemd unit is inactive: {unit}"
        ) from exc


def _detached_pid_is_alive(pid: int | None, *, proc_root: Path) -> bool:
    if pid is None:
        return False
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AutoresearchValidationError(
            f"cannot inspect interrupted detached process {pid}: {exc}"
        ) from exc
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split() if closing >= 0 else []
    if not fields:
        raise AutoresearchValidationError(
            f"malformed process stat for interrupted detached process {pid}"
        )
    return fields[0] != "Z"


@dataclass(frozen=True, slots=True)
class _SealedInterruptedRunAttestation:
    """Immutable detached evidence required before interrupted-v3 publication."""

    manifest_sha256: str
    status_sha256: str
    stdout_sha256: str
    stderr_sha256: str


def _attest_sealed_interrupted_run(record: RunRecord) -> _SealedInterruptedRunAttestation:
    capture = record.status.output_capture
    if capture is None:
        raise AutoresearchValidationError(
            "interrupted verification recovery requires sealed worker output capture"
        )
    return _SealedInterruptedRunAttestation(
        manifest_sha256=record.status.manifest_sha256,
        status_sha256=_canonical_json_digest(record.status.to_dict()),
        stdout_sha256=capture.stdout.sha256,
        stderr_sha256=capture.stderr.sha256,
    )


def _reattest_sealed_interrupted_run(
    record: RunRecord,
    *,
    runs_root: Path,
    expected: _SealedInterruptedRunAttestation,
) -> None:
    """Re-read the sealed record and capture tails before state publication."""
    import gateway.autoresearch_runs as detached_runs

    try:
        current = detached_runs.read_run_record(
            run_dir=record.run_directory,
            runs_root=runs_root,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification recovery cannot re-attest the sealed detached v3 record"
        ) from exc
    if _attest_sealed_interrupted_run(current) != expected:
        raise AutoresearchValidationError(
            "interrupted verification recovery sealed detached v3 evidence changed"
        )


def _require_absent_interrupted_run_artifact(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise AutoresearchValidationError(
            "interrupted verification recovery requires the v3 run.json to be absent"
        )


def _find_exact_platform_v4_detached_run(
    *,
    runs_root: Path,
    iteration: int,
    directory_name: str,
    task_label: str,
    state_reference_sha256: str,
) -> RunRecord:
    """Select exactly one sealed v4 record; duplicates are a fail-closed ambiguity."""
    import gateway.autoresearch_runs as detached_runs

    expected_directory = runs_root / directory_name
    try:
        record = detached_runs.read_run_record(run_dir=expected_directory, runs_root=runs_root)
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery expected detached v4 record is unavailable or invalid"
        ) from exc
    manifest = record.manifest
    if (
        manifest.phase is not Phase.VERIFICATION
        or manifest.iteration != iteration
        or manifest.attempt != 4
        or manifest.task_label != task_label
        or manifest.state_reference_sha256 != state_reference_sha256
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery expected detached v4 manifest identity is invalid"
        )
    duplicate_count = 0
    for directory, _children, files in os.walk(runs_root, followlinks=False):
        if "manifest.json" not in files:
            continue
        candidate = Path(directory)
        try:
            candidate_manifest = detached_runs.read_run_manifest(
                run_dir=candidate, runs_root=runs_root
            )
        except (OSError, ValueError):
            continue
        if (
            candidate_manifest.phase is Phase.VERIFICATION
            and candidate_manifest.iteration == iteration
            and candidate_manifest.attempt == 4
            and candidate_manifest.task_label == task_label
            and candidate_manifest.state_reference_sha256 == state_reference_sha256
        ):
            duplicate_count += 1
    if duplicate_count != 1:
        raise AutoresearchValidationError(
            "platform runtime recovery found duplicate expected detached v4 identities"
        )
    return record


def _require_absent_platform_v5_identity(
    *,
    run_id: str,
    iteration: int,
    implementation_commit: str,
) -> None:
    """Refuse recovery if any v5 artifact or detached identity already exists."""
    artifact_directory = constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / run_id
    try:
        artifact_directory.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery cannot inspect the v5 artifact directory"
        ) from exc
    else:
        raise AutoresearchValidationError(
            "platform runtime recovery requires the v5 artifact directory to be absent"
        )
    import gateway.autoresearch_runs as detached_runs

    runs_root = detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
    directory = runs_root / (f"i{iteration}-verification-r1-a5-{implementation_commit[:12]}-v5")
    try:
        directory.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery cannot inspect the v5 detached identity"
        ) from exc
    else:
        raise AutoresearchValidationError(
            "platform runtime recovery requires the v5 detached identity to be absent"
        )
    expected_artifact = str(artifact_directory / "run.json")
    for raw_directory, _children, files in os.walk(runs_root, followlinks=False):
        if "manifest.json" not in files:
            continue
        candidate = Path(raw_directory)
        try:
            manifest = detached_runs.read_run_manifest(run_dir=candidate, runs_root=runs_root)
        except (OSError, ValueError):
            continue
        if (
            manifest.iteration == iteration
            and manifest.phase is Phase.VERIFICATION
            and manifest.attempt == 5
            and manifest.expected_artifact_path == expected_artifact
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery found a duplicate detached v5 identity"
            )


def _require_unchanged_platform_runtime_recovery_state(
    state_path: Path,
    expected: AutoresearchState,
) -> None:
    """Reject an out-of-lock state-file write before v5 authorization publishes."""
    if persistence_module.load_state_file(state_path) != expected:
        raise AutoresearchValidationError(
            "platform runtime recovery state changed before publication"
        )


def _platform_runtime_recovery_identity_contexts(
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None,
) -> tuple[AutoresearchValidationContext, ReadinessIdentity]:
    """Authorize only the historical three-field identity's exact v4→v5 upgrade."""
    if validation_context is None:
        raise AutoresearchValidationError(
            "platform runtime recovery requires the exact readiness-pinned Quantipy commit"
        )
    historical_identity = state.platform_readiness
    current_identity = validation_context.readiness_identity
    if (
        historical_identity is None
        or current_identity is None
        or current_identity.quantipy_commit is None
        or validation_context.quantipy_commit != current_identity.quantipy_commit
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery requires the exact readiness-pinned Quantipy commit"
        )
    if historical_identity == current_identity:
        return validation_context, current_identity
    if historical_identity.quantipy_commit is not None or historical_identity != replace(
        current_identity, quantipy_commit=None
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery readiness identity may differ only by the current "
            "nonnull Quantipy commit"
        )
    return (
        replace(validation_context, readiness_identity=historical_identity),
        current_identity,
    )


def recover_platform_runtime_state_file(
    state_path: Path,
    *,
    probe: ResearchPanelProbeReceipt,
    operator_reason: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
    systemd_is_active: Callable[[str], bool] | None = None,
    proc_root: Path = Path("/proc"),
) -> AutoresearchState:
    """Materialize only the sealed v4 panel-receipt failure and authorize canonical v5."""
    publication_path = state_path.expanduser().resolve(strict=False)
    authoritative_path = constants.DEFAULT_AUTORESEARCH_STATE_PATH.expanduser().resolve(
        strict=False
    )
    unit_is_active = systemd_is_active or _default_systemd_is_active
    with persistence_module._exclusive_state_locks((authoritative_path, publication_path)):
        try:
            authoritative_bytes = authoritative_path.read_bytes()
            publication_bytes = publication_path.read_bytes()
        except OSError as exc:
            raise AutoresearchValidationError(
                "platform runtime recovery cannot read the authoritative state copy"
            ) from exc
        if publication_bytes != authoritative_bytes:
            raise AutoresearchValidationError(
                "platform runtime recovery output must be a byte-exact copy of the "
                "authoritative state"
            )
        state = persistence_module.load_state_file(authoritative_path)
        historical_validation_context, current_readiness_identity = (
            _platform_runtime_recovery_identity_contexts(state, validation_context)
        )
        transitions_module._validate_state(state, policy, historical_validation_context)
        receipt = state.external_verification_retry_receipt
        implementation = state.implementation_result
        if (
            state.phase is not Phase.VERIFICATION
            or state.mode is not ResearchMode.ALPHA_RESEARCH
            or receipt is None
            or receipt.schema_version
            != constants.INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
            or receipt.retry_attempt != 4
            or implementation is None
            or len(state.interrupted_verification_history) != 1
            or len(state.verification_history) != 2
            or state.platform_runtime_recovery_receipt is not None
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery accepts only the exact pending v4 topology"
            )
        if validation_context is None or validation_context.quantipy_commit is None:
            raise AutoresearchValidationError(
                "platform runtime recovery requires the exact readiness-pinned Quantipy commit"
            )
        if not operator_reason or operator_reason.strip() != operator_reason:
            raise AutoresearchValidationError(
                "platform runtime recovery requires a trimmed operator reason"
            )
        expected_v4_run_id = _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=4
        )
        expected_v5_run_id = _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=5
        )
        _require_absent_platform_v5_identity(
            run_id=expected_v5_run_id,
            iteration=state.iteration,
            implementation_commit=implementation.commit_sha,
        )
        if receipt.expected_run_id != expected_v4_run_id:
            raise AutoresearchValidationError(
                "platform runtime recovery v4 retry receipt identity is stale"
            )
        state_reference_sha256 = transitions_module.build_authoritative_state_reference(
            state, state_path=authoritative_path
        ).sha256()
        task_label = (
            f"autoresearch-i{state.iteration}-verification-r1-a4-"
            f"{implementation.commit_sha[:12]}-v4"
        )
        directory_name = (
            f"i{state.iteration}-verification-r1-a4-{implementation.commit_sha[:12]}-v4"
        )
        import gateway.autoresearch_runs as detached_runs

        record = _find_exact_platform_v4_detached_run(
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            iteration=state.iteration,
            directory_name=directory_name,
            task_label=task_label,
            state_reference_sha256=state_reference_sha256,
        )
        expected_run_path = (
            constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_v4_run_id / "run.json"
        )
        expected_command = _legacy_quantipy_bash_command(implementation, run_id=expected_v4_run_id)
        manifest = record.manifest
        status = record.status
        if (
            manifest.working_directory != implementation.workspace_path
            or manifest.expected_artifact_path != str(expected_run_path)
            or manifest.command_sha256 != detached_runs.command_sha256(expected_command)
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery detached manifest is not the exact historical v4 command"
            )
        capture = status.output_capture
        if (
            status.state is not detached_runs.RunState.FAILED
            or status.exit_code != 1
            or status.signal_number is not None
            or status.failure_classification
            is not detached_runs.RunFailureClassification.PROCESS_ERROR
            or status.systemd_unit is None
            or constants.OPENCLAW_LONG_TASK_UNIT_RE.fullmatch(status.systemd_unit) is None
            or capture is None
            or any(
                stream.truncated
                or not stream.eof_observed
                or stream.bytes_observed != stream.bytes_stored
                for stream in (capture.stdout, capture.stderr)
            )
            or status.expected_artifact_attestation_status
            is not detached_runs.ExpectedArtifactAttestationStatus.ATTESTED
            or status.expected_artifact_attestation is None
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery requires sealed v4 process_error/exit-1 "
                "EOF artifact evidence"
            )
        run_snapshot = _secure_open_snapshot(
            expected_run_path,
            label="sealed v4 Quantipy run.json",
            trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=constants.QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
        )
        if (
            status.expected_artifact_attestation.path != str(expected_run_path)
            or status.expected_artifact_attestation.size_bytes != len(run_snapshot.content)
            or status.expected_artifact_attestation.sha256 != run_snapshot.sha256
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery v4 run.json does not match detached artifact attestation"
            )
        run = _validate_quantipy_run_envelope(run_snapshot)
        failure = _run_failure_from_mapping(run["failure"])
        if (
            run["run_id"] != expected_v4_run_id
            or run["success"] is not False
            or run["panel_requested"] is not True
            or run["panel"] is not None
            or run["stage_receipts"]
            or failure is None
            or failure.category != "panel"
            or failure.message != "ExperimentPanelError: Research panel receipt is invalid."
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery requires the exact v4 panel receipt failure"
            )
        workspace = _require_strict_canonical_workspace_path(
            implementation.workspace_path, label="implementation_result workspace_path"
        )
        _require_clean_git_worktree(workspace)
        commit = _resolve_git_commit(
            workspace, implementation.commit_sha, label="implementation_result commit_sha"
        )
        if (
            _resolve_git_commit(workspace, "HEAD", label="implementation_result workspace HEAD")
            != commit
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery implementation worktree HEAD must equal its commit"
            )
        manifest_snapshot = _secure_open_snapshot(
            implementation.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )
        experiment_manifest = _validate_quantipy_v2_manifest(
            manifest_snapshot,
            workspace=workspace,
            commit_sha=commit,
            expected_sha256=implementation.experiment_manifest_sha256,
        )
        if run["manifest_sha256"] != _canonical_quantipy_manifest_sha256(experiment_manifest):
            raise AutoresearchValidationError(
                "platform runtime recovery v4 run.json manifest does not match immutable source"
            )
        source = run["source"]
        if source is None:
            raise AutoresearchValidationError(
                "platform runtime recovery v4 run.json requires source inventory"
            )
        _validate_quantipy_execution_source_against_commit(
            _ensure_mapping(source, label="sealed v4 Quantipy run.json source"),
            manifest=experiment_manifest,
            source_root=manifest_snapshot.path.parent,
            workspace=workspace,
            commit_sha=commit,
        )
        prior = state.latest_verification
        assert prior is not None
        v4_evidence = QuantipyExperimentEvidence(
            manifest_path=implementation.experiment_manifest_path,
            manifest_sha256=implementation.experiment_manifest_sha256,
            detached_run_directory=str(record.run_directory),
            detached_run_manifest_sha256=status.manifest_sha256,
            run_id=expected_v4_run_id,
            run_json_path=str(expected_run_path),
            run_json_sha256=run_snapshot.sha256,
            success=False,
            completed_stages=(),
            terminal_stage=None,
            terminal_status=None,
            failure=failure,
            panel=None,
        )
        v4 = replace(
            prior,
            status=VerificationStatus.TEST_FAILURE,
            is_walk_forward_sharpe_net=None,
            oos_sharpe_net=None,
            max_drawdown_pct=None,
            win_rate=None,
            trade_count=None,
            trades_per_day=None,
            oos_trading_days=None,
            feature_importances_summary="runner-owned sealed v4 platform runtime evidence",
            null_test_summary="ExperimentPanelError: Research panel receipt is invalid.",
            bug_signals=(),
            tests_passed=False,
            commands_run=(),
            data_coverage=None,
            platform_coverage_validation=None,
            infra_gate_outcome=None,
            infra_rationale=None,
            universe_verification_receipt=None,
            price_hydration_receipt=None,
            quantipy_experiment_evidence=v4_evidence,
            quantipy_execution_not_started=None,
        )
        v4.validate(mode=state.mode)
        history = (*state.verification_history, v4)
        history_sha256 = tuple(_canonical_json_digest(item.to_dict()) for item in history)
        interruption = state.interrupted_verification_history[0]
        _require_absent_interrupted_run_artifact(
            constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT
            / interruption.expected_run_id
            / "run.json"
        )
        runtime = attestation_module._attest_canonical_quantipy_runtime(
            state,
            implementation,
            readiness_quantipy_commit=validation_context.quantipy_commit,
        )
        v5 = ExternalVerificationRetryReceipt(
            expected_run_id=_deterministic_quantipy_run_id(
                state.iteration, implementation.commit_sha, attempt=5
            ),
            prior_verification_sha256=history_sha256[-1],
            probe=probe,
            retry_attempt=5,
            implementation_commit=implementation.commit_sha,
            manifest_sha256=implementation.experiment_manifest_sha256,
            readiness_manifest_id=receipt.readiness_manifest_id,
            readiness_snapshot_id=receipt.readiness_snapshot_id,
            operator_reason=operator_reason,
            verification_history_sha256=history_sha256,
            interruption_history_sha256=(_canonical_json_digest(interruption.to_dict()),),
            schema_version=constants.INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        )
        v5_contract = build_quantipy_execution_contract(
            runtime_root=Path(runtime.root),
            manifest_path=Path(implementation.experiment_manifest_path),
            output_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            run_id=v5.expected_run_id,
        )
        recovery = PlatformRuntimeRecoveryReceipt(
            expected_run_id=v5.expected_run_id,
            implementation_commit=implementation.commit_sha,
            implementation_manifest_sha256=implementation.experiment_manifest_sha256,
            verification_history_sha256=history_sha256,
            interruption_sha256=_canonical_json_digest(interruption.to_dict()),
            prior_retry_receipt_sha256=_canonical_json_digest(
                interruption.prior_retry_receipt.to_dict()
            ),
            v4_verification_sha256=history_sha256[-1],
            v4_detached_run_manifest_sha256=status.manifest_sha256,
            v4_detached_run_status_sha256=_canonical_json_digest(status.to_dict()),
            old_worktree_runtime_commit=commit,
            runtime=runtime,
            execution_command_sha256=hashlib.sha256(
                "\0".join(v5_contract.command).encode("utf-8")
            ).hexdigest(),
            probe=probe,
            operator_reason=operator_reason,
        )
        recovered = replace(
            state,
            platform_readiness=current_readiness_identity,
            verification_history=history,
            external_verification_retry_receipt=v5,
            platform_runtime_recovery_receipt=recovery,
            canonical_quantipy_runtime_attestation=runtime,
            phase=Phase.VERIFICATION,
            pending_fix_trigger=None,
        )
        transitions_module._validate_state(recovered, policy, validation_context)
        if status.systemd_unit is not None and unit_is_active(status.systemd_unit):
            raise AutoresearchValidationError(
                "platform runtime recovery refuses an active detached systemd unit"
            )
        if _detached_pid_is_alive(status.pid, proc_root=proc_root):
            raise AutoresearchValidationError(
                "platform runtime recovery refuses a live detached process"
            )
        # Re-read every mutable external fact immediately before the atomic publication.
        current = detached_runs.read_run_record(
            run_dir=record.run_directory, runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
        )
        if (
            _canonical_json_digest(current.status.to_dict())
            != recovery.v4_detached_run_status_sha256
            or current.status.manifest_sha256 != recovery.v4_detached_run_manifest_sha256
            or _secure_open_snapshot(
                expected_run_path,
                label="sealed v4 Quantipy run.json",
                trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
                private=True,
                max_bytes=constants.QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
            ).sha256
            != v4_evidence.run_json_sha256
            or attestation_module._attest_canonical_quantipy_runtime(
                state,
                implementation,
                readiness_quantipy_commit=validation_context.quantipy_commit,
            )
            != runtime
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery evidence changed before state publication"
            )
        if (
            status.systemd_unit is None
            or unit_is_active(status.systemd_unit)
            or _detached_pid_is_alive(status.pid, proc_root=proc_root)
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery detached process became active before state publication"
            )
        _require_absent_interrupted_run_artifact(
            constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT
            / interruption.expected_run_id
            / "run.json"
        )
        _require_absent_platform_v5_identity(
            run_id=expected_v5_run_id,
            iteration=state.iteration,
            implementation_commit=implementation.commit_sha,
        )
        _require_clean_git_worktree(workspace)
        if (
            _resolve_git_commit(workspace, "HEAD", label="implementation_result workspace HEAD")
            != commit
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery implementation worktree changed before publication"
            )
        current_manifest_snapshot = _secure_open_snapshot(
            implementation.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )
        current_manifest = _validate_quantipy_v2_manifest(
            current_manifest_snapshot,
            workspace=workspace,
            commit_sha=commit,
            expected_sha256=implementation.experiment_manifest_sha256,
        )
        if current_manifest != experiment_manifest:
            raise AutoresearchValidationError(
                "platform runtime recovery implementation manifest changed before publication"
            )
        _validate_quantipy_execution_source_against_commit(
            _ensure_mapping(source, label="sealed v4 Quantipy run.json source"),
            manifest=current_manifest,
            source_root=current_manifest_snapshot.path.parent,
            workspace=workspace,
            commit_sha=commit,
        )
        _require_absent_platform_v5_identity(
            run_id=expected_v5_run_id,
            iteration=state.iteration,
            implementation_commit=implementation.commit_sha,
        )
        try:
            if authoritative_path.read_bytes() != authoritative_bytes:
                raise AutoresearchValidationError(
                    "platform runtime recovery authoritative state changed before publication"
                )
            if publication_path.read_bytes() != publication_bytes:
                raise AutoresearchValidationError(
                    "platform runtime recovery output state changed before publication"
                )
        except OSError as exc:
            raise AutoresearchValidationError(
                "platform runtime recovery cannot re-read the output state copy"
            ) from exc
        _require_unchanged_platform_runtime_recovery_state(authoritative_path, state)
        persistence_module._atomic_save_state_file(publication_path, recovered)
        return recovered


def recover_interrupted_verification_state_file(
    state_path: Path,
    *,
    operator_reason: str,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    validation_context: AutoresearchValidationContext | None = None,
    systemd_is_active: Callable[[str], bool] | None = None,
    proc_root: Path = Path("/proc"),
) -> AutoresearchState:
    """Record the one operator-stopped v3 attempt and authorize deterministic v4.

    This deliberately does not materialize a VerificationResultArtifact: no
    Quantipy envelope was produced for the stopped process.
    """
    resolved_path = state_path.expanduser().resolve(strict=False)
    unit_is_active = systemd_is_active or _default_systemd_is_active
    with persistence_module._exclusive_state_locks((resolved_path,)):
        state = persistence_module.load_state_file(resolved_path)
        transitions_module._validate_state(state, policy, validation_context)
        receipt = state.external_verification_retry_receipt
        implementation = state.implementation_result
        if (
            state.phase is not Phase.VERIFICATION
            or state.mode is not ResearchMode.ALPHA_RESEARCH
            or receipt is None
            or receipt.schema_version
            != constants.EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
            or receipt.retry_attempt != 3
            or implementation is None
            or state.interrupted_verification_history
            or len(state.verification_history) != 2
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery accepts only the exact pending v3 topology"
            )
        if not operator_reason or operator_reason.strip() != operator_reason:
            raise AutoresearchValidationError(
                "interrupted verification recovery requires a trimmed operator reason"
            )
        expected_run_id = _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=3
        )
        if receipt.expected_run_id != expected_run_id:
            raise AutoresearchValidationError(
                "interrupted verification recovery retry receipt identity is stale"
            )
        expected_state_reference = transitions_module.build_authoritative_state_reference(
            state, state_path=resolved_path
        ).sha256()
        expected_run_path = (
            constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_run_id / "run.json"
        )
        expected_task_label = (
            f"autoresearch-i{state.iteration}-verification-r1-a3-"
            f"{implementation.commit_sha[:12]}-v3"
        )
        expected_directory_name = (
            f"i{state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
        )
        try:
            import gateway.autoresearch_runs as detached_runs

            record = _find_exact_interrupted_detached_run(
                runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
                iteration=state.iteration,
                directory_name=expected_directory_name,
                task_label=expected_task_label,
                state_reference_sha256=expected_state_reference,
            )
        except (OSError, ValueError) as exc:
            raise AutoresearchValidationError(
                "interrupted verification recovery cannot inspect detached run records"
            ) from exc
        expected_instruction_digest = record.manifest.instruction_manifest_sha256
        expected_command = _legacy_quantipy_bash_command(
            implementation,
            run_id=expected_run_id,
        )
        if (
            record.manifest.phase is not Phase.VERIFICATION
            or record.manifest.attempt != 3
            or record.manifest.task_label != expected_task_label
            or record.manifest.working_directory != implementation.workspace_path
            or record.manifest.expected_artifact_path != str(expected_run_path)
            or record.manifest.command_sha256 != detached_runs.command_sha256(expected_command)
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery detached manifest is not the exact v3 command"
            )
        status = record.status
        if (
            status.state is not detached_runs.RunState.FAILED
            or status.exit_code != 143
            or status.signal_number is not None
            or status.failure_classification
            is not detached_runs.RunFailureClassification.OPERATOR_STOPPED
            or status.systemd_unit is None
            or constants.OPENCLAW_LONG_TASK_UNIT_RE.fullmatch(status.systemd_unit) is None
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery detached status is not terminal "
                "operator_stopped/143"
            )
        capture = status.output_capture
        if capture is None or any(
            stream.truncated
            or not stream.eof_observed
            or stream.bytes_observed != stream.bytes_stored
            for stream in (capture.stdout, capture.stderr)
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery requires complete sealed worker output capture"
            )
        if (
            status.expected_artifact_attestation_status
            is not detached_runs.ExpectedArtifactAttestationStatus.FAILED
            or status.expected_artifact_attestation_error
            is not detached_runs.ExpectedArtifactAttestationError.MISSING
            or status.expected_artifact_attestation is not None
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery requires a missing v3 artifact attestation"
            )
        sealed_attestation = _attest_sealed_interrupted_run(record)
        history_sha256 = tuple(
            _canonical_json_digest(artifact.to_dict()) for artifact in state.verification_history
        )
        interruption = InterruptedVerificationAttemptReceipt(
            expected_run_id=expected_run_id,
            interrupted_attempt=3,
            implementation_commit=implementation.commit_sha,
            implementation_manifest_sha256=implementation.experiment_manifest_sha256,
            detached_run_directory=str(record.run_directory),
            detached_run_manifest_sha256=status.manifest_sha256,
            detached_run_status_sha256=_canonical_json_digest(status.to_dict()),
            state_sha256=_canonical_json_digest(state.to_dict()),
            state_reference_sha256=expected_state_reference,
            instruction_manifest_sha256=expected_instruction_digest,
            prior_retry_receipt_sha256=_canonical_json_digest(receipt.to_dict()),
            prior_retry_receipt=receipt,
            verification_history_sha256=history_sha256,
            operator_reason=operator_reason,
        )
        next_receipt = ExternalVerificationRetryReceipt(
            expected_run_id=_deterministic_quantipy_run_id(
                state.iteration, implementation.commit_sha, attempt=4
            ),
            prior_verification_sha256=history_sha256[-1],
            probe=receipt.probe,
            retry_attempt=4,
            implementation_commit=implementation.commit_sha,
            manifest_sha256=implementation.experiment_manifest_sha256,
            readiness_manifest_id=receipt.readiness_manifest_id,
            readiness_snapshot_id=receipt.readiness_snapshot_id,
            operator_reason=receipt.operator_reason,
            verification_history_sha256=history_sha256,
            interruption_history_sha256=(_canonical_json_digest(interruption.to_dict()),),
            schema_version=constants.INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        )
        recovered = replace(
            state,
            external_verification_retry_receipt=next_receipt,
            interrupted_verification_history=(interruption,),
        )
        transitions_module._validate_state(recovered, policy, validation_context)
        if unit_is_active(status.systemd_unit):
            raise AutoresearchValidationError(
                "interrupted verification recovery refuses an active detached systemd unit"
            )
        if _detached_pid_is_alive(status.pid, proc_root=proc_root):
            raise AutoresearchValidationError(
                "interrupted verification recovery refuses a live detached process"
            )
        _reattest_sealed_interrupted_run(
            record,
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            expected=sealed_attestation,
        )
        _require_absent_interrupted_run_artifact(expected_run_path)
        persistence_module._atomic_save_state_file(resolved_path, recovered)
        return recovered


_RUNTIME_RESEAL_RECEIPT_MAX_NAME_ATTEMPTS = 1000
_RUNTIME_RESEAL_RECEIPT_DIR_NAME = "runtime-reseal-receipts"


def _canonical_no_symlink_state_path(state_path: Path) -> Path:
    expanded = state_path.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    canonical = Path(os.path.normpath(os.fspath(absolute)))
    if os.fspath(canonical) != os.fspath(absolute):
        raise AutoresearchValidationError(
            f"autoresearch state path must be canonical without '.' or '..': {absolute}"
        )
    if canonical.name in ("", ".", ".."):
        raise AutoresearchValidationError(f"invalid autoresearch state path: {state_path}")
    state_dir = canonical.parent
    state_dir_fd = _open_directory_chain(state_dir)
    try:
        directory_stat = os.fstat(state_dir_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise AutoresearchValidationError(
                f"autoresearch state directory is not a directory: {state_dir}"
            )
        if _fd_path(state_dir_fd) != state_dir:
            raise AutoresearchValidationError(
                "autoresearch state directory must be a canonical no-symlink path: "
                f"{state_dir}"
            )
    finally:
        os.close(state_dir_fd)
    return canonical


def _open_directory_chain(path: Path) -> int:
    if not path.is_absolute():
        raise AutoresearchValidationError(f"directory path must be absolute: {path}")
    current_fd = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise AutoresearchValidationError(
                    f"directory path must be canonical without '.' or '..': {path}"
                )
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise AutoresearchValidationError(
                        f"directory path must not contain symlinks: {path}"
                    ) from exc
                raise AutoresearchValidationError(
                    f"unable to open directory without following symlinks: {path}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _fd_path(fd: int) -> Path:
    try:
        raw = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        raise AutoresearchValidationError("unable to verify canonical directory fd") from exc
    return Path(raw)


def _open_runtime_reseal_receipt_directory(
    state_directory_fd: int,
    receipt_directory: Path,
) -> int:
    try:
        os.mkdir(_RUNTIME_RESEAL_RECEIPT_DIR_NAME, mode=0o700, dir_fd=state_directory_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to create runtime reseal receipt directory: {receipt_directory}"
        ) from exc
    try:
        receipt_directory_fd = os.open(
            _RUNTIME_RESEAL_RECEIPT_DIR_NAME,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=state_directory_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise AutoresearchValidationError(
                f"refusing symlinked runtime reseal receipt directory: {receipt_directory}"
            ) from exc
        raise AutoresearchValidationError(
            f"unable to open runtime reseal receipt directory: {receipt_directory}"
        ) from exc
    try:
        receipt_directory_stat = os.fstat(receipt_directory_fd)
        if not stat.S_ISDIR(receipt_directory_stat.st_mode):
            raise AutoresearchValidationError(
                f"runtime reseal receipt path is not a directory: {receipt_directory}"
            )
        if receipt_directory_stat.st_uid != os.getuid():
            raise AutoresearchValidationError(
                f"runtime reseal receipt directory has wrong owner: {receipt_directory}"
            )
        if stat.S_IMODE(receipt_directory_stat.st_mode) != 0o700:
            raise AutoresearchValidationError(
                f"runtime reseal receipt directory permissions must be 0700: {receipt_directory}"
            )
        if _fd_path(receipt_directory_fd) != receipt_directory:
            raise AutoresearchValidationError(
                "runtime reseal receipt directory must be a canonical no-symlink path: "
                f"{receipt_directory}"
            )
    except Exception:
        with suppress(OSError):
            os.close(receipt_directory_fd)
        raise
    return receipt_directory_fd


def _write_runtime_reseal_receipt(
    receipt_directory: Path,
    receipt_name: str,
    content: bytes,
) -> Path:
    """Publish one unique receipt without following or replacing an existing name."""
    state_directory_fd = _open_directory_chain(receipt_directory.parent)
    try:
        receipt_directory_fd = _open_runtime_reseal_receipt_directory(
            state_directory_fd,
            receipt_directory,
        )
    finally:
        os.close(state_directory_fd)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        receipt_stem = receipt_name[:-5]
        for suffix in range(1, _RUNTIME_RESEAL_RECEIPT_MAX_NAME_ATTEMPTS + 1):
            candidate_name = (
                receipt_name if suffix == 1 else f"{receipt_stem}-{suffix}.json"
            )
            candidate_path = receipt_directory / candidate_name
            try:
                receipt_fd = os.open(candidate_name, flags, 0o600, dir_fd=receipt_directory_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise AutoresearchValidationError(
                        f"runtime reseal receipt path must not be a symlink: {candidate_path}"
                    ) from exc
                raise AutoresearchValidationError(
                    f"unable to create runtime reseal receipt: {candidate_path}"
                ) from exc
            try:
                os.fchmod(receipt_fd, 0o600)
                offset = 0
                while offset < len(content):
                    written = os.write(receipt_fd, content[offset:])
                    if written == 0:
                        raise OSError(
                            errno.EIO,
                            "zero-byte write while writing runtime reseal receipt",
                        )
                    offset += written
                os.fsync(receipt_fd)
                fd_to_close, receipt_fd = receipt_fd, -1
                os.close(fd_to_close)
                os.fsync(receipt_directory_fd)
            except Exception as exc:
                if receipt_fd >= 0:
                    fd_to_close, receipt_fd = receipt_fd, -1
                    with suppress(Exception):
                        os.close(fd_to_close)
                with suppress(Exception):
                    os.unlink(candidate_name, dir_fd=receipt_directory_fd)
                if isinstance(exc, OSError):
                    raise AutoresearchValidationError(
                        f"unable to write runtime reseal receipt: {candidate_path}: {exc}"
                    ) from exc
                raise
            return candidate_path
        raise AutoresearchValidationError(
            "unable to allocate a unique runtime reseal receipt name after "
            f"{_RUNTIME_RESEAL_RECEIPT_MAX_NAME_ATTEMPTS} attempts: "
            f"{receipt_directory / receipt_name}"
        )
    finally:
        with suppress(OSError):
            os.close(receipt_directory_fd)


def reseal_canonical_runtime_state_file(
    state_path: Path,
    *,
    operator_reason: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> Path:
    """Re-attest the current canonical runtime for a sealed verification dispatch."""
    resolved_path = _canonical_no_symlink_state_path(state_path)
    with persistence_module._exclusive_state_locks((resolved_path,)):
        raw = persistence_module._load_state_raw(resolved_path)
        schema_version = _require_int(raw, "schema_version")
        if schema_version != constants.AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "runtime reseal accepts only the compatible schema-v"
                f"{constants.AUTORESEARCH_STATE_SCHEMA_VERSION} state"
            )
        state = persistence_module.load_state_file(resolved_path)
        transitions_module._validate_state(state, policy, validation_context)
        if not operator_reason or operator_reason.strip() != operator_reason:
            raise AutoresearchValidationError(
                "runtime reseal requires a trimmed operator reason"
            )
        if state.phase is not Phase.VERIFICATION or state.implementation_result is None:
            raise AutoresearchValidationError(
                "runtime reseal requires phase verification with a sealed implementation result"
            )
        if state.platform_runtime_recovery_receipt is not None:
            raise AutoresearchValidationError(
                "platform runtime recovery receipt is live; reseal does not apply to the v5 "
                "topology"
            )
        if validation_context is None or validation_context.quantipy_commit is None:
            raise AutoresearchValidationError(
                "runtime reseal requires the readiness-pinned Quantipy commit"
            )

        old = state.canonical_quantipy_runtime_attestation
        implementation = state.implementation_result
        assert implementation is not None
        new = attestation_module._attest_canonical_quantipy_runtime(
            state,
            implementation,
            readiness_quantipy_commit=validation_context.quantipy_commit,
        )
        if new == old:
            raise AutoresearchValidationError(
                "canonical runtime attestation is unchanged; reseal is not required"
            )
        sealed = replace(state, canonical_quantipy_runtime_attestation=new)
        transitions_module._validate_state(sealed, policy, validation_context)
        attestation_module._require_canonical_verification_runtime_attestation(
            sealed,
            validation_context=validation_context,
        )
        persistence_module._atomic_save_state_file(resolved_path, sealed)

        timestamp = datetime.now(tz=UTC)
        receipt_directory = resolved_path.parent / _RUNTIME_RESEAL_RECEIPT_DIR_NAME
        receipt_name = (
            f"reseal-i{state.iteration:06d}-{timestamp.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        payload = {
            "schema_version": 1,
            "receipt_type": "g2-openclaw.autoresearch.runtime-reseal-receipt",
            "recorded_at": timestamp.isoformat().replace("+00:00", "Z"),
            "operator_reason": operator_reason,
            "iteration": state.iteration,
            "phase": "verification",
            "old_commit_sha": old.commit_sha if old is not None else None,
            "new_commit_sha": new.commit_sha,
            "new_runtime_attestation": new.to_dict(),
        }
        try:
            receipt_path = _write_runtime_reseal_receipt(
                receipt_directory,
                receipt_name,
                (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        except Exception as exc:
            raise AutoresearchValidationError(
                f"runtime reseal state was saved but receipt write failed: {exc}"
            ) from exc
        return receipt_path


def _find_exact_interrupted_detached_run(
    *,
    runs_root: Path,
    iteration: int,
    directory_name: str,
    task_label: str,
    state_reference_sha256: str,
) -> RunRecord:
    """Select the sole state-bound v3 manifest before reading any run status."""
    import gateway.autoresearch_runs as detached_runs

    try:
        metadata = runs_root.lstat()
    except FileNotFoundError as exc:
        raise AutoresearchValidationError("detached runs root is unavailable") from exc
    if runs_root.is_symlink() or not runs_root.is_dir() or not metadata:
        raise AutoresearchValidationError("detached runs root is not a safe directory")

    expected_directory = runs_root / directory_name
    try:
        expected_manifest = detached_runs.read_run_manifest(
            run_dir=expected_directory,
            runs_root=runs_root,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification recovery expected detached v3 manifest is unavailable "
            "or invalid"
        ) from exc
    if (
        expected_manifest.phase is not Phase.VERIFICATION
        or expected_manifest.iteration != iteration
        or expected_manifest.attempt != 3
        or expected_manifest.task_label != task_label
        or expected_manifest.state_reference_sha256 != state_reference_sha256
    ):
        raise AutoresearchValidationError(
            "interrupted verification recovery expected detached v3 manifest identity is invalid"
        )

    duplicate_directories: list[Path] = []
    for directory, child_directories, files in os.walk(runs_root, followlinks=False):
        child_directories.sort()
        files.sort()
        parent = Path(directory)
        if parent == expected_directory or "manifest.json" not in files:
            continue
        try:
            manifest = detached_runs.read_run_manifest(run_dir=parent, runs_root=runs_root)
        except (OSError, ValueError):
            continue
        if (
            manifest.phase is Phase.VERIFICATION
            and manifest.iteration == iteration
            and manifest.attempt == 3
            and manifest.task_label == task_label
            and manifest.state_reference_sha256 == state_reference_sha256
        ):
            duplicate_directories.append(parent)
    if duplicate_directories:
        raise AutoresearchValidationError(
            "interrupted verification recovery found duplicate expected detached v3 identities"
        )
    try:
        return detached_runs.read_run_record(run_dir=expected_directory, runs_root=runs_root)
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification recovery expected detached v3 status is unavailable "
            "or invalid"
        ) from exc


def _materialize_attested_pending_retry_failure(
    state: AutoresearchState,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Advance only the preserved v2 artifact that predates state-file handoff."""
    receipt = state.external_verification_retry_receipt
    if receipt is None or state.phase is not Phase.VERIFICATION:
        return state
    if receipt.retry_attempt != 2 or len(state.verification_history) != 1:
        raise AutoresearchValidationError(
            "external verification retry state-file recovery only accepts the preserved v2 attempt"
        )
    previous = state.latest_verification
    if previous is None or previous.status is not VerificationStatus.TEST_FAILURE:
        raise AutoresearchValidationError(
            "external verification retry requires the preserved initial verification failure"
        )
    implementation = state.implementation_result
    if implementation is None:
        raise AutoresearchValidationError("external verification retry requires implementation")
    expected_run_id = _deterministic_quantipy_run_id(
        state.iteration,
        implementation.commit_sha,
        attempt=receipt.retry_attempt,
    )
    if receipt.expected_run_id != expected_run_id:
        raise AutoresearchValidationError(
            "external verification retry receipt run ID does not bind the implementation"
        )
    legacy_run_identity = f"{implementation.commit_sha[:12]}-v{receipt.retry_attempt}"
    if expected_run_id != f"autoresearch-i{state.iteration}-{legacy_run_identity}":
        raise AutoresearchValidationError(
            "external verification retry receipt run ID has an invalid legacy identity"
        )
    try:
        import gateway.autoresearch_runs as detached_runs

        detached_run_directory = detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
            f"i{state.iteration}-verification-r1-a{receipt.retry_attempt}-{legacy_run_identity}"
        )
        detached_record = detached_runs.read_run_record(
            run_dir=detached_run_directory,
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "preserved v2 detached Quantipy run record is unavailable or invalid"
        ) from exc
    expected_run_path = (
        constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / receipt.expected_run_id / "run.json"
    )
    if detached_record.manifest.expected_artifact_path != str(expected_run_path):
        raise AutoresearchValidationError(
            "preserved v2 detached run does not attest the deterministic run artifact"
        )
    run_snapshot = _secure_open_snapshot(
        expected_run_path,
        label="preserved v2 Quantipy run.json",
        trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        private=True,
        max_bytes=constants.QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
    )
    run = _validate_quantipy_run_envelope(run_snapshot)
    failure = _run_failure_from_mapping(run["failure"])
    if (
        run["run_id"] != receipt.expected_run_id
        or run["success"] is not False
        or run["panel_requested"] is not True
        or run["panel"] is not None
        or run["stage_receipts"]
        or failure is None
        or failure.category != "panel"
        or not _is_manifest_bound_legacy_local_research_panel_http_413(state, failure.message)
    ):
        raise AutoresearchValidationError(
            "preserved v2 artifact is not the attested local research-panel HTTP 413 failure"
        )
    evidence = QuantipyExperimentEvidence(
        manifest_path=implementation.experiment_manifest_path,
        manifest_sha256=implementation.experiment_manifest_sha256,
        detached_run_directory=str(detached_record.run_directory),
        detached_run_manifest_sha256=detached_record.status.manifest_sha256,
        run_id=receipt.expected_run_id,
        run_json_path=str(expected_run_path),
        run_json_sha256=run_snapshot.sha256,
        success=False,
        completed_stages=(),
        terminal_stage=None,
        terminal_status=None,
        failure=failure,
        panel=None,
    )
    materialized = replace(
        previous,
        status=VerificationStatus.TEST_FAILURE,
        tests_passed=False,
        quantipy_experiment_evidence=evidence,
        quantipy_execution_not_started=None,
    )
    materialized.validate(mode=state.mode)
    transitions_module._validate_alpha_price_scope_verification(state, materialized)
    _validate_quantipy_experiment_evidence(
        state,
        materialized,
        validation_context=validation_context,
    )
    intermediate = replace(
        state,
        verification_history=(*state.verification_history, materialized),
        pending_fix_trigger=FixTriggerPhase.VERIFICATION,
        phase=Phase.FIX_TEST,
    )
    transitions_module._validate_state(intermediate, policy, validation_context)
    return intermediate
