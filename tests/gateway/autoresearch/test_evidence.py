from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast

import gateway.autoresearch.constants as autoresearch_constants
import gateway.autoresearch.evidence as autoresearch_evidence
import gateway.autoresearch.secure_io as autoresearch_secure_io
import gateway.autoresearch_runs as autoresearch_runs
import pytest
from gateway.autoresearch.artifacts import (
    QuantipyExecutionInterruptedEvidence,
    QuantipyExecutionNotStartedEvidence,
    QuantipyExperimentEvidence,
    QuantipyExperimentFailureEvidence,
)
from gateway.autoresearch.enums import (
    Phase,
    ResearchMode,
    VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy,
)
from gateway.autoresearch.state import (
    AutoresearchState,
    AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import advance_state as _runner_advance_state
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference,
    validate_artifact_workspace,
)
from gateway.autoresearch_readiness import PlatformReadinessManifest

from tests.gateway.autoresearch import builders as autoresearch_builders
from tests.gateway.autoresearch.builders import (
    QUANTIPY_V2_CONTRACT_FILE_SHA256,
    GitWorktree,
    _git,
    _implementation_artifact,
    _implementation_result,
    _majority_consensus,
    _rebind_quantipy_source_inventory,
    _rebind_test_detached_artifact_attestation,
    _rewrite_test_detached_status,
    _runtime_verification_context,
    _runtime_verification_state,
    _state_to_consensus,
    _verification_result,
    _workspace_setup,
    _write_quantipy_detached_run_record,
    advance_state,
)

_ORIGINAL_WRITE_QUANTIPY_V2_RUN: Callable[..., tuple[str, str, Path, str, str, str]] = (
    autoresearch_builders._write_quantipy_v2_run
)


def _write_quantipy_v2_run(
    *args: object,
    **kwargs: object,
) -> tuple[str, str, Path, str, str, str]:
    result = _ORIGINAL_WRITE_QUANTIPY_V2_RUN(*args, **kwargs)
    run_path = result[2]
    payload = cast(dict[str, object], json.loads(run_path.read_text(encoding="utf-8")))
    stage_receipts = cast(list[dict[str, object]], payload["stage_receipts"])
    for receipt in stage_receipts:
        if receipt["stage"] != "feasibility" or receipt["status"] != "completed":
            continue
        feasibility_result = cast(dict[str, object], receipt["result"])
        feasibility_result["summary"] = json.dumps(
            {
                "calibration_fit_seconds": 1.0,
                "projected_model_seconds": 2.0,
            },
            separators=(",", ":"),
        )
    run_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    return (*result[:3], sha256(run_bytes).hexdigest(), *result[4:])


autoresearch_builders._write_quantipy_v2_run = _write_quantipy_v2_run


def _parse_run_with_feasibility_summary(
    run_path: Path,
    summary: str,
) -> dict[str, object]:
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    stage_receipts = cast(list[dict[str, object]], payload["stage_receipts"])
    feasibility = next(receipt for receipt in stage_receipts if receipt["stage"] == "feasibility")
    result = cast(dict[str, object], feasibility["result"])
    result["summary"] = summary
    run_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )
    return autoresearch_evidence._validate_quantipy_run_envelope(snapshot)


def _rewrite_quantipy_run_payload(run_path: Path, payload: dict[str, object]) -> None:
    run_path.chmod(0o600)
    run_path.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _valid_derived_provenance(panel: dict[str, object]) -> dict[str, object]:
    receipt = cast(dict[str, object], panel["receipt"])
    request = cast(dict[str, object], receipt["request"])
    return {
        "member_union_count": 1,
        "member_union_digest": "a" * 64,
        "member_union_digest_algorithm": autoresearch_constants.MEMBER_UNION_DIGEST_ALGORITHM,
        "experiment_start": request["start"],
        "experiment_end": request["end"],
        "timeframe": "1min",
        "market_hours": "all",
        "request_sha256": panel["request_sha256"],
        "coverage_sha256": panel["coverage_sha256"],
        "panel_sha256": panel["panel_sha256"],
        "hydrated_at": receipt["hydrated_at"],
        "exported_at": receipt["exported_at"],
    }


def test_quantipy_run_envelope_preserves_validated_derived_provenance(
    git_worktree: GitWorktree,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    _, _, run_path, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        run_root=trusted_quantipy_runs_root,
        panel_requested=True,
    )
    payload = cast(dict[str, object], json.loads(run_path.read_text(encoding="utf-8")))
    panel = cast(dict[str, object], payload["panel"])
    expected_provenance = _valid_derived_provenance(panel)
    payload["derived_provenance"] = expected_provenance
    _rewrite_quantipy_run_payload(run_path, payload)
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )

    # Act
    normalized = autoresearch_evidence._validate_quantipy_run_envelope(
        snapshot,
        mode=ResearchMode.ALPHA_RESEARCH,
    )

    # Assert
    assert normalized["derived_provenance"] == expected_provenance


def test_quantipy_run_envelope_rejects_mismatched_derived_provenance_digest(
    git_worktree: GitWorktree,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    _, _, run_path, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        run_root=trusted_quantipy_runs_root,
        panel_requested=True,
    )
    payload = cast(dict[str, object], json.loads(run_path.read_text(encoding="utf-8")))
    panel = cast(dict[str, object], payload["panel"])
    provenance = _valid_derived_provenance(panel)
    provenance["request_sha256"] = "0" * 64
    payload["derived_provenance"] = provenance
    _rewrite_quantipy_run_payload(run_path, payload)
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="derived_provenance request_sha256 does not match panel evidence",
    ):
        autoresearch_evidence._validate_quantipy_run_envelope(
            snapshot,
            mode=ResearchMode.ALPHA_RESEARCH,
        )


def test_quantipy_run_envelope_preserves_current_null_derived_provenance(
    git_worktree: GitWorktree,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    _, _, run_path, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        run_root=trusted_quantipy_runs_root,
    )
    payload = cast(dict[str, object], json.loads(run_path.read_text(encoding="utf-8")))
    payload["derived_provenance"] = None
    _rewrite_quantipy_run_payload(run_path, payload)
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )

    # Act
    normalized = autoresearch_evidence._validate_quantipy_run_envelope(snapshot)

    # Assert
    assert "derived_provenance" in normalized
    assert normalized["derived_provenance"] is None


def test_quantipy_run_envelope_rejects_null_derived_provenance_for_alpha_panel(
    git_worktree: GitWorktree,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    _, _, run_path, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        run_root=trusted_quantipy_runs_root,
        panel_requested=True,
    )
    payload = cast(dict[str, object], json.loads(run_path.read_text(encoding="utf-8")))
    payload["derived_provenance"] = None
    _rewrite_quantipy_run_payload(run_path, payload)
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="ALPHA_RESEARCH runs with panel evidence must carry runtime-derived provenance",
    ):
        autoresearch_evidence._validate_quantipy_run_envelope(
            snapshot,
            mode=ResearchMode.ALPHA_RESEARCH,
        )


def test_legacy_quantipy_run_envelope_omits_derived_provenance(
    git_worktree: GitWorktree,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    _, _, run_path, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        run_root=trusted_quantipy_runs_root,
        panel_requested=True,
    )
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )

    # Act
    normalized = autoresearch_evidence._validate_quantipy_run_envelope(
        snapshot,
        mode=ResearchMode.ALPHA_RESEARCH,
    )

    # Assert
    assert "derived_provenance" not in normalized


def test_advance_state_requires_successful_quantipy_v2_run_receipt(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    manifest_path, manifest_sha256, run_path, run_sha256, commit_sha, run_id = (
        _write_quantipy_v2_run(git_worktree, run_root=trusted_quantipy_runs_root)
    )
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = replace(state, setup=_workspace_setup(git_worktree.target_checkout))
    implementation = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=commit_sha,
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha256,
    )
    validate_artifact_workspace(state, implementation)
    state = advance_state(state, implementation, policy)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    detached_run_directory, detached_run_manifest_sha256 = _write_quantipy_detached_run_record(
        workspace=git_worktree.workspace,
        runtime_root=git_worktree.target_checkout,
        manifest_path=manifest_path,
        run_id=run_id,
        run_path=run_path,
    )
    verification = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=QuantipyExperimentEvidence(
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            detached_run_directory=detached_run_directory,
            detached_run_manifest_sha256=detached_run_manifest_sha256,
            run_id=run_id,
            run_json_path=str(run_path),
            run_json_sha256=run_sha256,
            success=True,
            completed_stages=("prepare", "smoke", "feasibility", "model"),
            terminal_stage=None,
            terminal_status=None,
            failure=None,
            panel=None,
        ),
    )

    # Act
    advanced = _runner_advance_state(
        state,
        verification,
        policy,
        validation_context=AutoresearchValidationContext(
            state.platform_readiness,
            "f" * 64,
            (date(2021, 1, 5),),
        ),
        state_path=state_path,
    )

    # Assert
    assert advanced.phase is Phase.REVIEW


def test_successful_quantipy_bug_signal_routes_to_fix_without_fabricating_alpha_evidence(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    verification = replace(
        _verification_result(VerificationStatus.BUG_SIGNAL),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        bug_signals=("quantipy_runtime_missing_alpha_metrics",),
        data_coverage=None,
        platform_coverage_validation=None,
        universe_verification_receipt=None,
        price_hydration_receipt=None,
        quantipy_experiment_evidence=evidence,
    )

    # Act
    advanced = _runner_advance_state(
        state,
        verification,
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    # Assert
    assert advanced.phase is Phase.FIX_TEST
    assert advanced.latest_verification is not None
    assert advanced.latest_verification.quantipy_experiment_evidence == evidence
    assert advanced.latest_verification.is_walk_forward_sharpe_net is None
    assert advanced.latest_verification.data_coverage is None
    assert advanced.latest_verification.universe_verification_receipt is None


def test_successful_quantipy_bug_signal_requires_nonempty_signals(
    successful_quantipy_evidence: QuantipyExperimentEvidence,
) -> None:
    # Arrange
    artifact = replace(
        _verification_result(VerificationStatus.BUG_SIGNAL),
        bug_signals=(),
        quantipy_experiment_evidence=replace(
            successful_quantipy_evidence,
            panel=None,
        ),
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="requires at least one bug signal"):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_successful_quantipy_bug_signal_requires_passing_tests(
    successful_quantipy_evidence: QuantipyExperimentEvidence,
) -> None:
    # Arrange
    artifact = replace(
        _verification_result(VerificationStatus.BUG_SIGNAL),
        tests_passed=False,
        quantipy_experiment_evidence=successful_quantipy_evidence,
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="successful Quantipy experiment requires tests_passed=true",
    ):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_successful_quantipy_evidence_rejects_incomplete_stages(
    successful_quantipy_evidence: QuantipyExperimentEvidence,
) -> None:
    # Arrange
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=replace(
            successful_quantipy_evidence,
            completed_stages=("prepare", "smoke", "feasibility"),
        ),
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="requires all four completed stages"):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_successful_quantipy_evidence_rejects_failure_evidence(
    successful_quantipy_evidence: QuantipyExperimentEvidence,
) -> None:
    # Arrange
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=replace(
            successful_quantipy_evidence,
            failure=QuantipyExperimentFailureEvidence(category="panel", message="bad panel"),
        ),
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="cannot contain failure evidence"):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_successful_quantipy_evidence_rejects_terminal_failure_stage(
    successful_quantipy_evidence: QuantipyExperimentEvidence,
) -> None:
    # Arrange
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=replace(
            successful_quantipy_evidence,
            terminal_stage="model",
            terminal_status="failed",
        ),
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="cannot contain a terminal failure stage",
    ):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_test_failure_rejects_successful_quantipy_evidence(
    successful_quantipy_evidence: QuantipyExperimentEvidence,
) -> None:
    # Arrange
    artifact = replace(
        _verification_result(VerificationStatus.TEST_FAILURE),
        quantipy_experiment_evidence=successful_quantipy_evidence,
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="TEST_FAILURE verification cannot claim a successful Quantipy experiment run",
    ):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_quantipy_pass_rejects_run_json_substituted_after_worker_attestation(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    replacement_path = run_path.with_suffix(".replacement")
    replacement_bytes = run_path.read_bytes() + b"\n"
    replacement_path.write_bytes(replacement_bytes)
    replacement_path.chmod(0o400)
    replacement_path.replace(run_path)

    with pytest.raises(
        AutoresearchValidationError,
        match="detached run record is unavailable or invalid",
    ):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence,
                    run_json_sha256=sha256(replacement_bytes).hexdigest(),
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_pass_rejects_non_successful_detached_terminal_status(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    status_path = Path(evidence.detached_run_directory) / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.update(
        state="failed",
        exit_code=1,
        signal_number=None,
        failure_classification="process_error",
    )
    Path(evidence.detached_run_directory).chmod(0o700)
    status_path.chmod(0o600)
    status_path.write_text(
        json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    status_path.chmod(0o400)
    Path(evidence.detached_run_directory).chmod(0o500)

    with pytest.raises(
        AutoresearchValidationError,
        match="successful Quantipy envelope requires detached success",
    ):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_pass_rejects_historical_detached_status_without_attestation(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    status_path = Path(evidence.detached_run_directory) / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["schema_version"] = 3
    del status["expected_artifact_attestation_status"]
    del status["expected_artifact_attestation_error"]
    del status["expected_artifact_attestation"]
    Path(evidence.detached_run_directory).chmod(0o700)
    status_path.chmod(0o600)
    status_path.write_text(
        json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    status_path.chmod(0o400)
    Path(evidence.detached_run_directory).chmod(0o500)

    with pytest.raises(
        AutoresearchValidationError,
        match="detached run record is unavailable or invalid",
    ):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_pass_rejects_detached_manifest_digest_from_artifact_claim(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="detached run manifest digest does not match evidence",
    ):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence,
                    detached_run_manifest_sha256="0" * 64,
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_nested_manifest_resolves_package_notebook_and_stages_from_manifest_parent(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    manifest_path, manifest_sha, run_path, run_sha, commit_sha, run_id = _write_quantipy_v2_run(
        git_worktree,
        run_root=trusted_quantipy_runs_root,
        manifest_parent=Path("research") / "candidate",
        notebook_requested=True,
    )
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = replace(state, setup=_workspace_setup(git_worktree.target_checkout))
    implementation = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=commit_sha,
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha,
    )
    validate_artifact_workspace(state, implementation)
    state = advance_state(state, implementation, policy)
    state_path = tmp_path / "nested-state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    detached_run_directory, detached_run_manifest_sha256 = _write_quantipy_detached_run_record(
        workspace=git_worktree.workspace,
        runtime_root=git_worktree.target_checkout,
        manifest_path=manifest_path,
        run_id=run_id,
        run_path=run_path,
    )
    evidence = QuantipyExperimentEvidence(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        detached_run_directory=detached_run_directory,
        detached_run_manifest_sha256=detached_run_manifest_sha256,
        run_id=run_id,
        run_json_path=str(run_path),
        run_json_sha256=run_sha,
        success=True,
        completed_stages=("prepare", "smoke", "feasibility", "model"),
        terminal_stage=None,
        terminal_status=None,
        failure=None,
        panel=None,
    )

    advanced = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            quantipy_experiment_evidence=evidence,
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.REVIEW


def test_quantipy_failed_envelope_accepts_exact_detached_contract_exit(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        success=False,
        terminal_stage="model",
        terminal_status="failed",
    )

    advanced = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.TEST_FAILURE),
            tests_passed=False,
            quantipy_experiment_evidence=evidence,
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.FIX_TEST


@pytest.mark.parametrize(
    ("detached_state", "exit_code", "signal_number", "failure_classification"),
    (
        ("succeeded", 0, None, None),
        ("failed", 2, None, "process_error"),
        ("failed", 137, 9, "process_error"),
        ("failed", 1, None, "timeout"),
        ("failed", 1, None, "operator_stopped"),
        ("failed", 1, None, "resource_exhausted"),
        ("failed", 1, None, "artifact_missing"),
        ("failed", 1, None, "output_capture_error"),
    ),
)
def test_quantipy_failed_envelope_rejects_non_contract_process_outcomes(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    detached_state: str,
    exit_code: int,
    signal_number: int | None,
    failure_classification: str | None,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        success=False,
        terminal_stage="model",
        terminal_status="failed",
    )
    _rewrite_test_detached_status(
        evidence,
        state=detached_state,
        exit_code=exit_code,
        signal_number=signal_number,
        failure_classification=failure_classification,
    )

    with pytest.raises(AutoresearchValidationError, match="detached"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.BUG_SIGNAL),
                bug_signals=("quantipy_runtime_failure",),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_pass_rejects_missing_quantipy_runtime_evidence(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    state, state_path, _ = _runtime_verification_state(
        git_worktree, policy, platform_readiness, tmp_path, trusted_quantipy_runs_root
    )
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=None,
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="Quantipy experiment evidence"):
        _runner_advance_state(
            state,
            artifact,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_advance_state_rejects_tampered_quantipy_run_json(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree, policy, platform_readiness, tmp_path, trusted_quantipy_runs_root
    )
    run_path = Path(evidence.run_json_path)
    run_path.chmod(0o600)
    run_path.write_text("{}", encoding="utf-8")
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=evidence,
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="run_json_sha256"):
        _runner_advance_state(
            state,
            artifact,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


@pytest.mark.parametrize(
    ("status", "terminal_stage", "terminal_status", "bug_signals"),
    (
        (VerificationStatus.TEST_FAILURE, "smoke", "rejected", ()),
        (VerificationStatus.BUG_SIGNAL, "model", "failed", ("model anomaly",)),
    ),
)
def test_nonpass_requires_truthful_typed_quantipy_terminal_evidence(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    status: VerificationStatus,
    terminal_stage: str,
    terminal_status: str,
    bug_signals: tuple[str, ...],
    trusted_quantipy_runs_root: Path,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        success=False,
        terminal_stage=terminal_stage,
        terminal_status=terminal_status,
    )
    artifact = replace(
        _verification_result(status),
        bug_signals=bug_signals,
        tests_passed=status is VerificationStatus.BUG_SIGNAL,
        quantipy_experiment_evidence=evidence,
    )

    # Act
    advanced = _runner_advance_state(
        state,
        artifact,
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    # Assert
    assert advanced.phase is Phase.FIX_TEST


def test_completed_feasibility_rejects_null_calibration_fit_seconds(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    _, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="feasibility stage must MEASURE one real fit at the true encoded width: "
        "calibration_fit_seconds",
    ):
        _parse_run_with_feasibility_summary(
            Path(evidence.run_json_path),
            json.dumps(
                {
                    "calibration_fit_seconds": None,
                    "projected_model_seconds": 1.0,
                }
            ),
        )


def test_completed_feasibility_rejects_zero_projected_model_seconds(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    _, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="feasibility stage must MEASURE one real fit at the true encoded width: "
        "projected_model_seconds",
    ):
        _parse_run_with_feasibility_summary(
            Path(evidence.run_json_path),
            json.dumps(
                {
                    "calibration_fit_seconds": 1.0,
                    "projected_model_seconds": 0,
                }
            ),
        )


def test_completed_feasibility_accepts_positive_fit_projection(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    _, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )

    run = _parse_run_with_feasibility_summary(
        Path(evidence.run_json_path),
        json.dumps(
            {
                "calibration_fit_seconds": 1.0,
                "projected_model_seconds": 2.0,
            }
        ),
    )

    assert run["success"] is True


def test_completed_feasibility_rejects_non_json_summary(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    _, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="feasibility summary must be a JSON object",
    ):
        _parse_run_with_feasibility_summary(Path(evidence.run_json_path), "accepted")


@pytest.mark.parametrize(
    "mutation",
    (
        "invalid_timestamp",
        "negative_stage_duration",
        "missing_summary",
        "inconsistent_result_stage",
        "completed_with_failure",
        "failed_without_failure",
        "success_flag_mismatch",
        "non_prefix_stages",
        "invalid_telemetry_scope",
        "reversed_telemetry",
        "negative_telemetry_duration",
        "extra_nested_field",
        "missing_success_source",
        "invalid_source_algorithm",
        "invalid_source_domain",
        "unordered_source_files",
        "duplicate_source_file",
        "mismatched_source_digest",
        "mismatched_source_total",
        "oversized_source_file",
        "non_python_source_path",
        "escaping_source_path",
        "oversized_stage_summary",
        "oversized_failure_message",
        "oversized_identity_path",
        "extra_source_field",
    ),
)
def test_quantipy_run_parser_rejects_complete_schema_and_invariant_violations(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    mutation: str,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    if mutation == "invalid_timestamp":
        payload["stage_receipts"][0]["started_at"] = "not-a-timestamp"
    elif mutation == "negative_stage_duration":
        payload["stage_receipts"][0]["wall_seconds"] = -0.1
    elif mutation == "missing_summary":
        del payload["stage_receipts"][0]["result"]["summary"]
    elif mutation == "inconsistent_result_stage":
        payload["stage_receipts"][0]["result"]["stage"] = "model"
    elif mutation == "completed_with_failure":
        payload["stage_receipts"][0]["failure"] = {
            "category": "stage",
            "message": "contradiction",
        }
    elif mutation == "failed_without_failure":
        payload["stage_receipts"][-1]["status"] = "failed"
        payload["stage_receipts"][-1]["result"] = None
    elif mutation == "success_flag_mismatch":
        payload["success"] = False
    elif mutation == "non_prefix_stages":
        payload["stage_receipts"][1]["stage"] = "feasibility"
    elif mutation == "invalid_telemetry_scope":
        payload["telemetry"]["scope"] = "stage_only"
    elif mutation == "reversed_telemetry":
        payload["telemetry"]["completed_at"] = "2026-07-28T11:59:59Z"
    elif mutation == "negative_telemetry_duration":
        payload["telemetry"]["wall_seconds"] = -1
    elif mutation == "extra_nested_field":
        payload["stage_receipts"][0]["result"]["agent_claim"] = "passed"
    elif mutation == "missing_success_source":
        payload["source"] = None
    elif mutation == "invalid_source_algorithm":
        payload["source"]["algorithm"] = "sha512"
    elif mutation == "invalid_source_domain":
        payload["source"]["domain"] = "unbound"
    elif mutation == "unordered_source_files":
        payload["source"]["files"].reverse()
    elif mutation == "duplicate_source_file":
        payload["source"]["files"].append(payload["source"]["files"][0])
    elif mutation == "mismatched_source_digest":
        payload["source"]["sha256"] = "0" * 64
    elif mutation == "mismatched_source_total":
        payload["source"]["total_bytes"] += 1
    elif mutation == "oversized_source_file":
        payload["source"]["files"][0]["size_bytes"] = 1024 * 1024 + 1
    elif mutation == "non_python_source_path":
        payload["source"]["files"][0]["path"] = "experiment/source.txt"
    elif mutation == "escaping_source_path":
        payload["source"]["files"][0]["path"] = "../experiment/source.py"
    elif mutation == "oversized_stage_summary":
        payload["stage_receipts"][0]["result"]["summary"] = "x" * 4097
    elif mutation == "oversized_failure_message":
        payload["failure"] = {"category": "stage", "message": "x" * 2049}
    elif mutation == "oversized_identity_path":
        payload["identity"]["package_path"] = "/" + ("x" * 4096)
    else:
        payload["source"]["files"][0]["unexpected"] = True
    run_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=replace(
            evidence, run_json_sha256=sha256(run_bytes).hexdigest()
        ),
    )

    with pytest.raises(AutoresearchValidationError, match="Quantipy"):
        _runner_advance_state(
            state,
            artifact,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_verification_rejects_run_outside_trusted_canonical_layout(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    arbitrary_path = tmp_path / "arbitrary" / evidence.run_id / "run.json"
    arbitrary_path.parent.mkdir(parents=True)
    arbitrary_path.write_bytes(Path(evidence.run_json_path).read_bytes())
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        quantipy_experiment_evidence=replace(evidence, run_json_path=str(arbitrary_path)),
    )

    with pytest.raises(AutoresearchValidationError, match="trusted canonical run layout"):
        _runner_advance_state(
            state,
            artifact,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_verification_rejects_nonprivate_trusted_runs_root(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    trusted_quantipy_runs_root.chmod(0o755)

    with pytest.raises(AutoresearchValidationError, match="mode-0700 non-symlink directory"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_verification_rejects_dirty_experiment_source_tree(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    (git_worktree.workspace / "experiment" / "mutable.py").write_text(
        "MUTABLE = True\n", encoding="utf-8"
    )

    with pytest.raises(AutoresearchValidationError, match="untracked or ignored package file"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_verification_ignores_generated_bytecode_and_runtime_artifacts(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    package = git_worktree.workspace / "experiment"
    bytecode_directory = package / "__pycache__"
    bytecode_directory.mkdir(mode=0o755)
    (bytecode_directory / "prepare.cpython-313.pyc").write_bytes(b"generated bytecode")
    (package / "runtime.log").write_text("runtime output\n", encoding="utf-8")
    (package / "metrics.json").write_text('{"wall_seconds":1}\n', encoding="utf-8")

    next_state = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            quantipy_experiment_evidence=evidence,
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert next_state.phase is Phase.REVIEW


def test_implementation_rejects_ignored_transitive_package_source(
    git_worktree: GitWorktree,
) -> None:
    manifest_path, manifest_sha, _, _, commit_sha, _ = _write_quantipy_v2_run(git_worktree)
    package = git_worktree.workspace / "experiment"
    (git_worktree.workspace / ".gitignore").write_text(
        "experiment/ignored_helper.py\n", encoding="utf-8"
    )
    (git_worktree.workspace / ".gitignore").chmod(0o644)
    ignored_helper = package / "ignored_helper.py"
    ignored_helper.write_text("VALUE = 7\n", encoding="utf-8")
    ignored_helper.chmod(0o644)
    prepare = package / "prepare.py"
    prepare.write_text(
        "from .ignored_helper import VALUE\n\n"
        "def run(context):\n"
        "    return context.accept(str(VALUE))\n",
        encoding="utf-8",
    )
    prepare.chmod(0o644)
    _git(git_worktree.workspace, "add", ".gitignore", "experiment/prepare.py")
    _git(git_worktree.workspace, "commit", "-m", "import ignored helper")
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=_git(git_worktree.workspace, "rev-parse", "HEAD"),
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha,
    )
    assert commit_sha != artifact.commit_sha

    with pytest.raises(AutoresearchValidationError, match="untracked or ignored package file"):
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )


@pytest.mark.parametrize(
    ("size_bytes", "accepted"),
    (
        (70 * 1024, True),
        (autoresearch_constants.QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES + 1, False),
    ),
)
def test_committed_package_source_uses_quantipy_one_mib_snapshot_limit(
    git_worktree: GitWorktree,
    size_bytes: int,
    accepted: bool,
) -> None:
    manifest_path, manifest_sha, _, _, _, _ = _write_quantipy_v2_run(git_worktree)
    helper_path = git_worktree.workspace / "experiment" / "large_helper.py"
    prefix = b"VALUE = 1\n"
    helper_path.write_bytes(prefix + (b" " * (size_bytes - len(prefix))))
    helper_path.chmod(0o644)
    _git(git_worktree.workspace, "add", "experiment/large_helper.py")
    _git(git_worktree.workspace, "commit", "-m", "add bounded large source")
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=_git(git_worktree.workspace, "rev-parse", "HEAD"),
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha,
    )

    if accepted:
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )
    else:
        with pytest.raises(AutoresearchValidationError, match="exceeds the byte limit"):
            validate_artifact_workspace(
                AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
                artifact,
            )


@pytest.mark.parametrize(
    ("padding_bytes", "accepted"),
    (
        (70 * 1024, True),
        (autoresearch_constants.QUANTIPY_EXPERIMENT_NOTEBOOK_MAX_BYTES, False),
    ),
)
def test_committed_notebook_uses_quantipy_eight_mib_snapshot_limit(
    git_worktree: GitWorktree,
    padding_bytes: int,
    accepted: bool,
) -> None:
    manifest_path, manifest_sha, _, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        notebook_requested=True,
    )
    notebook_path = git_worktree.workspace / "report.ipynb"
    notebook_path.write_text(
        json.dumps(
            {
                "cells": [],
                "metadata": {"padding": "x" * padding_bytes},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    notebook_path.chmod(0o644)
    _git(git_worktree.workspace, "add", "report.ipynb")
    _git(git_worktree.workspace, "commit", "-m", "update bounded notebook")
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=_git(git_worktree.workspace, "rev-parse", "HEAD"),
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha,
    )

    if accepted:
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )
    else:
        with pytest.raises(AutoresearchValidationError, match="exceeds the byte limit"):
            validate_artifact_workspace(
                AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
                artifact,
            )


def test_quantipy_verification_rejects_tracked_source_byte_mismatch(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    prepare = git_worktree.workspace / "experiment" / "prepare.py"
    prepare.write_text(
        "def run(context):\n    return context.reject('dirty execution source')\n",
        encoding="utf-8",
    )

    with pytest.raises(AutoresearchValidationError, match="match commit_sha exactly"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_execution_source_rejects_dirty_run_after_source_restore(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    run_path.unlink()
    run_path.parent.rmdir()
    model_path = git_worktree.workspace / "experiment" / "model.py"
    committed_bytes = model_path.read_bytes()
    dirty_bytes = (
        b"def run(context):\n    return context.accept('executed from dirty restored source')\n"
    )
    model_path.write_bytes(dirty_bytes)
    quantipy_cli = Path("/home/dev/repos/quantipy/.venv/bin/quantipy")
    if not quantipy_cli.is_file():
        pytest.skip("current Quantipy v2 CLI is unavailable for source provenance cross-check")
    try:
        result = subprocess.run(
            (
                str(quantipy_cli),
                "experiment",
                "run",
                evidence.manifest_path,
                "--output-root",
                str(trusted_quantipy_runs_root),
                "--run-id",
                evidence.run_id,
            ),
            cwd=git_worktree.workspace,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        model_path.write_bytes(committed_bytes)
    assert result.returncode == 0, result.stderr
    assert not _git(git_worktree.workspace, "status", "--porcelain")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    model_source = next(
        item for item in run["source"]["files"] if item["path"] == "experiment/model.py"
    )
    assert model_source["sha256"] == sha256(dirty_bytes).hexdigest()
    feasibility = next(
        receipt for receipt in run["stage_receipts"] if receipt["stage"] == "feasibility"
    )
    cast(dict[str, object], feasibility["result"])["summary"] = json.dumps(
        {
            "calibration_fit_seconds": 1.0,
            "projected_model_seconds": 2.0,
        },
        separators=(",", ":"),
    )
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)

    with pytest.raises(
        AutoresearchValidationError,
        match="execution-time source evidence does not match implementation commit",
    ):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence,
                    run_json_sha256=sha256(run_bytes).hexdigest(),
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        (
            "omit_committed_initializer",
            "source inventory does not exactly match implementation commit",
        ),
        (
            "invent_uncommitted_helper",
            "source inventory does not exactly match implementation commit",
        ),
        (
            "replace_committed_digest",
            "execution-time source evidence does not match implementation commit",
        ),
        (
            "replace_committed_size",
            "execution-time source evidence does not match implementation commit",
        ),
    ),
)
def test_quantipy_execution_source_requires_exact_committed_python_inventory(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    mutation: str,
    error: str,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    source = cast(dict[str, object], run["source"])
    files = cast(list[dict[str, object]], source["files"])
    if mutation == "omit_committed_initializer":
        source["files"] = [item for item in files if item["path"] != "experiment/__init__.py"]
    elif mutation == "invent_uncommitted_helper":
        ghost_bytes = b"VALUE = 1\n"
        files.append(
            {
                "path": "experiment/ghost.py",
                "sha256": sha256(ghost_bytes).hexdigest(),
                "size_bytes": len(ghost_bytes),
            }
        )
    elif mutation == "replace_committed_digest":
        files[-1]["sha256"] = sha256(b"different execution bytes").hexdigest()
    else:
        files[-1]["size_bytes"] = cast(int, files[-1]["size_bytes"]) + 1
    _rebind_quantipy_source_inventory(source)
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)

    with pytest.raises(
        AutoresearchValidationError,
        match=error,
    ):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence,
                    run_json_sha256=sha256(run_bytes).hexdigest(),
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_verification_requires_head_equal_implementation_commit(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    (git_worktree.workspace / "post-implementation.txt").write_text("later\n", encoding="utf-8")
    _git(git_worktree.workspace, "add", "post-implementation.txt")
    _git(git_worktree.workspace, "commit", "-m", "advance head")

    with pytest.raises(AutoresearchValidationError, match="workspace HEAD"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_secure_reader_rejects_symlinked_run_json(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    target = tmp_path / "run-target.json"
    target.write_bytes(run_path.read_bytes())
    run_path.unlink()
    run_path.symlink_to(target)

    with pytest.raises(AutoresearchValidationError, match="non-symlink regular file"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=evidence,
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_secure_reader_rejects_committed_symlink_manifest(
    git_worktree: GitWorktree,
    tmp_path: Path,
) -> None:
    manifest_path, _, _, _, _, _ = _write_quantipy_v2_run(git_worktree)
    manifest = Path(manifest_path)
    target = tmp_path / "manifest-target.json"
    target.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(target)
    _git(git_worktree.workspace, "add", "experiment-manifest.json")
    _git(git_worktree.workspace, "commit", "-m", "replace manifest with link")
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=_git(git_worktree.workspace, "rev-parse", "HEAD"),
        experiment_manifest_path=str(manifest),
        experiment_manifest_sha256=sha256(target.read_bytes()).hexdigest(),
    )

    with pytest.raises(AutoresearchValidationError, match="non-symlink regular file"):
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )


def test_quantipy_secure_reader_rejects_symlinked_panel_receipt(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    receipt_path = Path(evidence.run_json_path).parent / "panel" / "receipt.json"
    receipt_path.parent.chmod(0o700)
    target = tmp_path / "receipt-target.json"
    target.write_bytes(receipt_path.read_bytes())
    receipt_path.unlink()
    receipt_path.symlink_to(target)
    receipt_path.parent.chmod(0o500)

    try:
        with pytest.raises(AutoresearchValidationError, match="non-symlink regular file"):
            _runner_advance_state(
                state,
                replace(
                    _verification_result(VerificationStatus.PASS),
                    quantipy_experiment_evidence=evidence,
                ),
                policy,
                validation_context=_runtime_verification_context(state),
                state_path=state_path,
            )
    finally:
        receipt_path.parent.chmod(0o700)
        receipt_path.unlink()
        target.unlink()


def test_quantipy_panel_rejects_arbitrary_receipt_file(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    receipt_path = Path(evidence.run_json_path).parent / "panel" / "receipt.json"
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(b'{"arbitrary":true}')
    receipt_path.chmod(0o400)
    assert evidence.panel is not None
    arbitrary_sha = sha256(receipt_path.read_bytes()).hexdigest()
    run_path = Path(evidence.run_json_path)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    cast(dict[str, object], run["panel"])["receipt_sha256"] = arbitrary_sha
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    _rebind_test_detached_artifact_attestation(evidence)

    with pytest.raises(AutoresearchValidationError, match="panel receipt"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence,
                    run_json_sha256=sha256(run_bytes).hexdigest(),
                    panel=replace(evidence.panel, receipt_sha256=arbitrary_sha),
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_panel_receipt_is_parsed_and_bound_to_manifest_and_files(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    advanced = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            quantipy_experiment_evidence=evidence,
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.REVIEW


def test_quantipy_panel_receipt_rejects_legacy_v1_before_coverage_fallback() -> None:
    # Arrange
    receipt = {
        "contract_version": "research-price-panel-v1",
        "request": None,
        "request_sha256": "0" * 64,
        "coverage": None,
        "coverage_sha256": "0" * 64,
        "panel_sha256": "0" * 64,
        "hydrated_at": "2026-07-28T13:00:00Z",
        "exported_at": "2026-07-28T13:00:01Z",
    }

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="contract_version"):
        autoresearch_evidence._validate_panel_receipt(receipt, label="panel receipt")


def test_quantipy_compact_panel_constants_match_the_shared_gateway_contract() -> None:
    # Arrange
    quantipy_schemas = Path("/home/dev/repos/quantipy/src/quantipy/price_data/schemas.py")

    # Act
    source = quantipy_schemas.read_text(encoding="utf-8")

    # Assert
    assert (
        "RESEARCH_PRICE_PANEL_RECEIPT_CONTRACT_VERSION: "
        'Literal["research-price-panel-receipt-v2"]' in source
    )
    assert 'COMPACT_PRICE_COVERAGE_CONTRACT_VERSION: Literal["price-coverage-compact-v1"]' in source
    assert 'COMPACT_PRICE_COVERAGE_ENCODING: Literal["canonical-json-zlib-base64-v1"]' in source
    assert "MAX_COMPACT_PRICE_COVERAGE_BYTES = 32 * 1024 * 1024" in source
    assert "MAX_COMPACT_PRICE_COVERAGE_COMPRESSED_BYTES = 4 * 1024 * 1024" in source
    assert "MAX_COMPACT_PRICE_COVERAGE_RATIO = 200.0" in source


def test_quantipy_run_receipt_larger_than_64k_is_accepted(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        extra_source_file_count=245,
    )
    run_path = Path(evidence.run_json_path)
    run_bytes = run_path.read_bytes()
    assert len(run_bytes) > 64 * 1024

    advanced = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            quantipy_experiment_evidence=replace(
                evidence, run_json_sha256=sha256(run_bytes).hexdigest()
            ),
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.REVIEW


def test_quantipy_run_receipt_at_8_mib_is_rejected(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["stage_receipts"][-1]["result"]["summary"] = ""
    baseline_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run["stage_receipts"][-1]["result"]["summary"] = "x" * (
        autoresearch_constants.QUANTIPY_RUN_ENVELOPE_MAX_BYTES - len(baseline_bytes)
    )
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    assert len(run_bytes) == autoresearch_constants.QUANTIPY_RUN_ENVELOPE_MAX_BYTES
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)

    with pytest.raises(AutoresearchValidationError, match="exceeds the byte limit"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence, run_json_sha256=sha256(run_bytes).hexdigest()
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_quantipy_panel_receipt_member_remains_capped_at_4_mib(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    assert evidence.panel is not None
    run_path = Path(evidence.run_json_path)
    receipt_path = run_path.parent / evidence.panel.receipt_path
    receipt_path.chmod(0o600)
    oversized_receipt = receipt_path.read_bytes() + (
        b" " * autoresearch_constants.QUANTIPY_PANEL_RECEIPT_MAX_BYTES
    )
    receipt_path.write_bytes(oversized_receipt)
    receipt_path.chmod(0o400)
    receipt_sha = sha256(oversized_receipt).hexdigest()
    run = json.loads(run_path.read_text(encoding="utf-8"))
    cast(dict[str, object], run["panel"])["receipt_sha256"] = receipt_sha
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    _rebind_test_detached_artifact_attestation(evidence)

    with pytest.raises(AutoresearchValidationError, match="exceeds the byte limit"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence,
                    run_json_sha256=sha256(run_bytes).hexdigest(),
                    panel=replace(evidence.panel, receipt_sha256=receipt_sha),
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_requested_panel_preflight_failure_without_panel_evidence_is_valid(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    run_path = Path(evidence.run_json_path)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    failure = {"category": "preflight", "message": "preflight rejected source"}
    run.update(success=False, source=None, panel=None, stage_receipts=[], failure=failure)
    panel_directory = run_path.parent / "panel"
    panel_directory.chmod(0o700)
    (panel_directory / "panel.parquet").unlink()
    (panel_directory / "receipt.json").unlink()
    panel_directory.rmdir()
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    failed_evidence = replace(
        evidence,
        run_json_sha256=sha256(run_bytes).hexdigest(),
        success=False,
        completed_stages=(),
        failure=QuantipyExperimentFailureEvidence(
            category="preflight", message="preflight rejected source"
        ),
        panel=None,
    )
    _rebind_test_detached_artifact_attestation(failed_evidence)

    advanced = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.TEST_FAILURE),
            tests_passed=False,
            quantipy_experiment_evidence=failed_evidence,
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.FIX_TEST


@pytest.mark.parametrize(
    ("stage_category", "run_category", "run_message"),
    (
        ("preflight", None, None),
        ("stage", "import", "stage failed"),
        ("stage", "stage", "different message"),
    ),
)
def test_quantipy_failure_invariants_reject_invalid_or_mismatched_terminal_failure(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    stage_category: str,
    run_category: str | None,
    run_message: str | None,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        success=False,
        terminal_stage="model",
        terminal_status="failed",
    )
    run_path = Path(evidence.run_json_path)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["stage_receipts"][-1]["failure"] = {
        "category": stage_category,
        "message": "stage failed",
    }
    run["failure"] = (
        {"category": run_category, "message": run_message}
        if run_category is not None and run_message is not None
        else None
    )
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    _rebind_test_detached_artifact_attestation(evidence)

    with pytest.raises(AutoresearchValidationError, match="failure"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.TEST_FAILURE),
                tests_passed=False,
                quantipy_experiment_evidence=replace(
                    evidence, run_json_sha256=sha256(run_bytes).hexdigest()
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


@pytest.mark.parametrize(
    ("case", "expected_valid"),
    (
        ("matching_entered_failure", True),
        ("entered_preflight_failure", False),
        ("mismatched_run_category", False),
        ("requested_panel_preflight_failure", True),
        ("requested_panel_stage_failure_without_receipt", False),
        ("mismatched_source_digest", False),
        ("entered_source_missing", False),
        ("oversized_summary", False),
    ),
)
def test_local_failure_fixture_acceptance_matches_current_quantipy_v2(
    git_worktree: GitWorktree,
    tmp_path: Path,
    case: str,
    expected_valid: bool,
) -> None:
    _, _, run_path, _, _, _ = _write_quantipy_v2_run(
        git_worktree,
        success=False,
        terminal_stage="model",
        terminal_status="failed",
        panel_requested=case.startswith("requested_panel"),
    )
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if case == "matching_entered_failure":
        run["failure"] = {"category": "stage", "message": "run-level detail"}
    elif case == "entered_preflight_failure":
        run["stage_receipts"][-1]["failure"]["category"] = "preflight"
    elif case == "mismatched_run_category":
        run["failure"] = {"category": "import", "message": "different category"}
    elif case == "mismatched_source_digest":
        run["source"]["sha256"] = "0" * 64
    elif case == "entered_source_missing":
        run["source"] = None
    elif case == "oversized_summary":
        run["stage_receipts"][0]["result"]["summary"] = "x" * 4097
    else:
        category = "preflight" if case == "requested_panel_preflight_failure" else "stage"
        run.update(
            source=None if category == "preflight" else run["source"],
            panel=None,
            stage_receipts=[],
            failure={"category": category, "message": "pre-stage failure"},
        )
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    fixture_path = tmp_path / f"{case}.json"
    fixture_path.write_bytes(run_bytes)
    fixture_path.chmod(0o600)

    local_valid = True
    try:
        snapshot = autoresearch_secure_io._secure_open_snapshot(
            fixture_path, label="cross-contract run fixture"
        )
        autoresearch_evidence._validate_quantipy_run_envelope(snapshot)
    except AutoresearchValidationError:
        local_valid = False

    quantipy_python = Path("/home/dev/repos/quantipy/.venv/bin/python")
    if not quantipy_python.is_file():
        pytest.skip("current Quantipy v2 environment is unavailable for contract cross-check")
    quantipy_root = Path("/home/dev/repos/quantipy")
    assert {
        relative_path: sha256((quantipy_root / relative_path).read_bytes()).hexdigest()
        for relative_path in QUANTIPY_V2_CONTRACT_FILE_SHA256
    } == QUANTIPY_V2_CONTRACT_FILE_SHA256
    quantipy_result = subprocess.run(
        (
            str(quantipy_python),
            "-c",
            (
                "import sys\n"
                "from pydantic import ValidationError\n"
                "from quantipy.experiments.schemas import ExperimentRunEnvelope\n"
                "try:\n"
                "    ExperimentRunEnvelope.model_validate_json(sys.stdin.buffer.read())\n"
                "except ValidationError:\n"
                "    raise SystemExit(1)\n"
            ),
        ),
        cwd="/home/dev/repos/quantipy",
        input=run_bytes,
        check=False,
    )
    quantipy_valid = quantipy_result.returncode == 0

    assert quantipy_valid is expected_valid
    assert local_valid is quantipy_valid


def test_g2_source_and_envelope_limits_match_pinned_quantipy_v2() -> None:
    quantipy_python = Path("/home/dev/repos/quantipy/.venv/bin/python")
    if not quantipy_python.is_file():
        pytest.skip("current Quantipy v2 environment is unavailable for limit cross-check")
    result = subprocess.run(
        (
            str(quantipy_python),
            "-c",
            (
                "import json\n"
                "from quantipy.experiments.schemas import (\n"
                "    EXPERIMENT_RUN_ENVELOPE_MAX_BYTES,\n"
                "    EXPERIMENT_SOURCE_FILE_MAX_BYTES,\n"
                "    EXPERIMENT_SOURCE_FILE_MAX_COUNT,\n"
                "    EXPERIMENT_SOURCE_PATH_MAX_LENGTH,\n"
                "    EXPERIMENT_SOURCE_TOTAL_MAX_BYTES,\n"
                ")\n"
                "print(json.dumps({\n"
                "    'run': EXPERIMENT_RUN_ENVELOPE_MAX_BYTES,\n"
                "    'source_file': EXPERIMENT_SOURCE_FILE_MAX_BYTES,\n"
                "    'source_count': EXPERIMENT_SOURCE_FILE_MAX_COUNT,\n"
                "    'source_path': EXPERIMENT_SOURCE_PATH_MAX_LENGTH,\n"
                "    'source_total': EXPERIMENT_SOURCE_TOTAL_MAX_BYTES,\n"
                "}, sort_keys=True))\n"
            ),
        ),
        cwd="/home/dev/repos/quantipy",
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "run": autoresearch_constants.QUANTIPY_RUN_ENVELOPE_MAX_BYTES,
        "source_file": autoresearch_constants.QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES,
        "source_count": autoresearch_constants.QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_COUNT,
        "source_path": autoresearch_constants.QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH,
        "source_total": autoresearch_constants.QUANTIPY_EXPERIMENT_SOURCE_TOTAL_MAX_BYTES,
    }


@pytest.mark.parametrize(("panel_requested", "remove_panel"), ((False, False), (True, True)))
def test_quantipy_panel_requested_flag_has_exact_evidence_semantics(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    panel_requested: bool,
    remove_panel: bool,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    run_path = Path(evidence.run_json_path)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["panel_requested"] = panel_requested
    if remove_panel:
        payload["panel"] = None
    run_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)

    with pytest.raises(AutoresearchValidationError, match="panel"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                quantipy_experiment_evidence=replace(
                    evidence, run_json_sha256=sha256(run_bytes).hexdigest()
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_nonpass_without_run_requires_bound_execution_not_started_receipt(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    run_path.unlink()
    run_path.parent.rmdir()
    command = "uv run pytest tests/alpha/test_candidate.py"
    not_started = QuantipyExecutionNotStartedEvidence(
        manifest_path=evidence.manifest_path,
        manifest_sha256=evidence.manifest_sha256,
        expected_run_id=evidence.run_id,
        expected_run_json_path=evidence.run_json_path,
        reason="focused_tests_failed",
        command=command,
        evidence="1 focused test failed before Quantipy preflight",
    )
    advanced = _runner_advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.TEST_FAILURE),
            tests_passed=False,
            commands_run=(command,),
            quantipy_experiment_evidence=None,
            quantipy_execution_not_started=not_started,
        ),
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.FIX_TEST
    tombstone = Path(evidence.run_json_path).parent
    assert tombstone.stat().st_mode & 0o777 == 0o700
    tombstone_path = tombstone / ".g2-execution-not-started.json"
    assert tombstone_path.stat().st_mode & 0o777 == 0o600
    tombstone_payload = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert tombstone_payload["expected_run_id"] == evidence.run_id
    assert tombstone_payload["manifest_sha256"] == evidence.manifest_sha256


def test_execution_not_started_rejects_existing_stage_receipt_without_run_json(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    run_path.unlink()
    stages = run_path.parent / "stages"
    stages.mkdir(mode=0o700)
    stage_receipt = stages / "prepare.json"
    stage_receipt.write_text('{"stage":"prepare"}', encoding="utf-8")
    stage_receipt.chmod(0o600)
    command = "uv run pytest tests/alpha/test_candidate.py"

    with pytest.raises(AutoresearchValidationError, match="run directory already exists"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.TEST_FAILURE),
                tests_passed=False,
                commands_run=(command,),
                quantipy_experiment_evidence=None,
                quantipy_execution_not_started=QuantipyExecutionNotStartedEvidence(
                    manifest_path=evidence.manifest_path,
                    manifest_sha256=evidence.manifest_sha256,
                    expected_run_id=evidence.run_id,
                    expected_run_json_path=evidence.run_json_path,
                    reason="focused_tests_failed",
                    command=command,
                    evidence="focused test failed",
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_execution_not_started_atomic_reservation_loses_concurrent_creation_race(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    run_path.unlink()
    run_path.parent.rmdir()
    original_mkdir = os.mkdir

    def concurrent_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == evidence.run_id and dir_fd is not None:
            original_mkdir(path, mode=0o700, dir_fd=dir_fd)
        original_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", concurrent_mkdir)
    command = "uv run pytest tests/alpha/test_candidate.py"

    with pytest.raises(AutoresearchValidationError, match="run directory already exists"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.TEST_FAILURE),
                tests_passed=False,
                commands_run=(command,),
                quantipy_experiment_evidence=None,
                quantipy_execution_not_started=QuantipyExecutionNotStartedEvidence(
                    manifest_path=evidence.manifest_path,
                    manifest_sha256=evidence.manifest_sha256,
                    expected_run_id=evidence.run_id,
                    expected_run_json_path=evidence.run_json_path,
                    reason="focused_tests_failed",
                    command=command,
                    evidence="focused test failed",
                ),
            ),
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_execution_not_started_receipt_is_rejected_when_expected_run_exists(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    command = "uv run pytest tests/alpha/test_candidate.py"
    artifact = replace(
        _verification_result(VerificationStatus.TEST_FAILURE, external_panel_failure=True),
        tests_passed=False,
        commands_run=(command,),
        quantipy_experiment_evidence=None,
        quantipy_execution_not_started=QuantipyExecutionNotStartedEvidence(
            manifest_path=evidence.manifest_path,
            manifest_sha256=evidence.manifest_sha256,
            expected_run_id=evidence.run_id,
            expected_run_json_path=evidence.run_json_path,
            reason="focused_tests_failed",
            command=command,
            evidence="claimed pre-runtime failure",
        ),
    )

    with pytest.raises(AutoresearchValidationError, match="run directory already exists"):
        _runner_advance_state(
            state,
            artifact,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def _timeout_interrupted_quantipy_execution(
    state: AutoresearchState,
    evidence: QuantipyExperimentEvidence,
    *,
    git_worktree: GitWorktree,
    tmp_path: Path,
    detached_root: Path,
    state_path: Path,
    truncated_capture: bool = False,
) -> QuantipyExecutionInterruptedEvidence:
    implementation = state.implementation_result
    assert implementation is not None
    run_path = Path(evidence.run_json_path)
    run_path.unlink()
    contract = autoresearch_evidence.build_quantipy_execution_contract(
        runtime_root=git_worktree.target_checkout,
        manifest_path=Path(implementation.experiment_manifest_path),
        output_root=autoresearch_constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        run_id=evidence.run_id,
    )
    detached_root.mkdir(mode=0o700, exist_ok=True)
    detached_root.chmod(0o700)
    detached_run_directory = detached_root / "interrupted-timeout"
    manifest_input = tmp_path / "interrupted-timeout-manifest.json"
    manifest_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": state.iteration,
                "phase": "verification",
                "attempt": 1,
                "task_label": "quantipy-verification",
                "state_reference_sha256": build_authoritative_state_reference(
                    state,
                    state_path=state_path,
                ).sha256(),
                "instruction_manifest_sha256": "b" * 64,
                "run_directory": str(detached_run_directory),
                "working_directory": str(contract.working_directory),
                "command_sha256": autoresearch_runs.command_sha256(contract.command),
                "expected_artifact_path": str(run_path),
                "timeout_seconds": 30.0,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=manifest_input,
        run_dir=detached_run_directory,
        runs_root=detached_root,
        command=contract.command,
    )
    autoresearch_runs.prepare_output_capture(
        run_dir=detached_run_directory,
        runs_root=detached_root,
    )
    autoresearch_runs.start_run(
        run_dir=detached_run_directory,
        pid=123,
        systemd_unit="openclaw-long-task-1-1.service",
        runs_root=detached_root,
    )
    output = b"x" * (autoresearch_runs.OUTPUT_CAPTURE_MAX_BYTES + 1)
    for stream in autoresearch_runs.RunOutputStream:
        autoresearch_runs.capture_output_stream(
            run_dir=detached_run_directory,
            runs_root=detached_root,
            stream=stream,
            source=BytesIO(output if truncated_capture else b""),
        )
    autoresearch_runs.complete_run(
        run_dir=detached_run_directory,
        exit_code=124,
        signal_number=None,
        peak_rss_bytes=None,
        timed_out=True,
        runs_root=detached_root,
    )
    record = autoresearch_runs.read_run_record(
        run_dir=detached_run_directory,
        runs_root=detached_root,
    )
    capture = record.status.output_capture
    assert capture is not None
    assert record.status.finished_at is not None
    assert record.status.exit_code is not None
    assert record.status.failure_classification is not None
    return QuantipyExecutionInterruptedEvidence(
        expected_run_id=evidence.run_id,
        expected_run_json_path=evidence.run_json_path,
        manifest_path=evidence.manifest_path,
        manifest_sha256=evidence.manifest_sha256,
        detached_run_directory=str(record.run_directory),
        detached_manifest_sha256=record.status.manifest_sha256,
        detached_status_sha256=sha256(
            json.dumps(record.status.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        exit_code=record.status.exit_code,
        signal_number=record.status.signal_number,
        failure_classification=record.status.failure_classification.value,
        timeout_seconds=record.manifest.timeout_seconds,
        wall_seconds_observed=(
            datetime.fromisoformat(record.status.finished_at.replace("Z", "+00:00"))
            - datetime.fromisoformat(record.status.started_at.replace("Z", "+00:00"))
        ).total_seconds(),
        stdout_sha256=capture.stdout.sha256,
        stdout_bytes_observed=capture.stdout.bytes_observed,
        stdout_truncated=capture.stdout.truncated,
        stderr_sha256=capture.stderr.sha256,
        stderr_bytes_observed=capture.stderr.bytes_observed,
        stderr_truncated=capture.stderr.truncated,
    )


def test_execution_interrupted_receipt_accepts_sealed_timeout_run(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    detached_root = tmp_path / "interrupted-detached-runs"
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)
    interrupted = _timeout_interrupted_quantipy_execution(
        state,
        evidence,
        git_worktree=git_worktree,
        tmp_path=tmp_path,
        detached_root=detached_root,
        state_path=state_path,
    )
    verification = replace(
        _verification_result(VerificationStatus.TEST_FAILURE),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        tests_passed=False,
        data_coverage=None,
        quantipy_experiment_evidence=None,
        quantipy_execution_interrupted=interrupted,
    )

    # Act
    advanced = _runner_advance_state(
        state,
        verification,
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    # Assert
    assert advanced.phase is Phase.FIX_TEST


def test_execution_interrupted_walk_skips_prepared_never_launched_runs(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A historical run directory holding only a manifest (prepared, never
    # launched) elsewhere in the detached root must be skipped, not treated
    # as a corrupt record.
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    detached_root = tmp_path / "interrupted-detached-runs"
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)
    interrupted = _timeout_interrupted_quantipy_execution(
        state,
        evidence,
        git_worktree=git_worktree,
        tmp_path=tmp_path,
        detached_root=detached_root,
        state_path=state_path,
    )
    residue = detached_root / "historical-prepared-only"
    residue.mkdir(mode=0o700)
    (residue / "manifest.json").write_text("{}", encoding="utf-8")
    verification = replace(
        _verification_result(VerificationStatus.TEST_FAILURE),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        data_coverage=None,
        tests_passed=False,
        quantipy_experiment_evidence=None,
        quantipy_execution_interrupted=interrupted,
    )

    advanced = _runner_advance_state(
        state,
        verification,
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    assert advanced.phase is Phase.FIX_TEST


def test_execution_interrupted_receipt_rejects_capture_digest_mismatch(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    detached_root = tmp_path / "interrupted-detached-runs"
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)
    interrupted = _timeout_interrupted_quantipy_execution(
        state,
        evidence,
        git_worktree=git_worktree,
        tmp_path=tmp_path,
        detached_root=detached_root,
        state_path=state_path,
    )
    verification = replace(
        _verification_result(VerificationStatus.TEST_FAILURE),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        tests_passed=False,
        data_coverage=None,
        quantipy_experiment_evidence=None,
        quantipy_execution_interrupted=replace(interrupted, stdout_sha256="0" * 64),
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="stdout capture digest"):
        _runner_advance_state(
            state,
            verification,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_execution_interrupted_receipt_accepts_truncated_capture(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    detached_root = tmp_path / "interrupted-detached-runs"
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)
    interrupted = _timeout_interrupted_quantipy_execution(
        state,
        evidence,
        git_worktree=git_worktree,
        tmp_path=tmp_path,
        detached_root=detached_root,
        state_path=state_path,
        truncated_capture=True,
    )
    verification = replace(
        _verification_result(VerificationStatus.TEST_FAILURE),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        tests_passed=False,
        data_coverage=None,
        quantipy_experiment_evidence=None,
        quantipy_execution_interrupted=interrupted,
    )

    # Act
    advanced = _runner_advance_state(
        state,
        verification,
        policy,
        validation_context=_runtime_verification_context(state),
        state_path=state_path,
    )

    # Assert
    assert advanced.phase is Phase.FIX_TEST


def test_execution_interrupted_receipt_rejects_capture_without_eof(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    detached_root = tmp_path / "interrupted-detached-runs"
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)
    interrupted = _timeout_interrupted_quantipy_execution(
        state,
        evidence,
        git_worktree=git_worktree,
        tmp_path=tmp_path,
        detached_root=detached_root,
        state_path=state_path,
    )
    status_path = Path(interrupted.detached_run_directory) / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["output_capture"]["stderr"]["eof_observed"] = False
    run_directory = status_path.parent
    capture_receipt_path = run_directory / ".stderr.capture.json"
    capture_receipt = json.loads(capture_receipt_path.read_text(encoding="utf-8"))
    capture_receipt["eof_observed"] = False
    run_directory.chmod(0o700)
    status_path.chmod(0o600)
    status_path.write_text(
        json.dumps(status, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    status_path.chmod(0o400)
    capture_receipt_path.chmod(0o600)
    capture_receipt_path.write_text(
        json.dumps(capture_receipt, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    capture_receipt_path.chmod(0o600)
    run_directory.chmod(0o500)
    record = autoresearch_runs.read_run_record(
        run_dir=run_directory,
        runs_root=detached_root,
    )
    status_digest = sha256(
        json.dumps(record.status.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    interrupted = replace(
        interrupted,
        detached_status_sha256=status_digest,
    )
    verification = replace(
        _verification_result(VerificationStatus.TEST_FAILURE),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        tests_passed=False,
        data_coverage=None,
        quantipy_experiment_evidence=None,
        quantipy_execution_interrupted=interrupted,
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="output capture is incomplete"):
        _runner_advance_state(
            state,
            verification,
            policy,
            validation_context=_runtime_verification_context(state),
            state_path=state_path,
        )


def test_implementation_rejects_raw_notebook_as_manifest(
    git_worktree: GitWorktree,
) -> None:
    # Arrange
    notebook = git_worktree.workspace / "report.ipynb"
    notebook.write_text('{"cells": [], "nbformat": 4}', encoding="utf-8")
    notebook.chmod(0o644)
    _git(git_worktree.workspace, "add", "report.ipynb")
    _git(git_worktree.workspace, "commit", "-m", "add report notebook")
    artifact = replace(
        _implementation_artifact(git_worktree),
        commit_sha=_git(git_worktree.workspace, "rev-parse", "HEAD"),
        experiment_manifest_path=str(notebook),
        experiment_manifest_sha256=sha256(notebook.read_bytes()).hexdigest(),
    )
    state = AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout))

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="exact v2 shape"):
        validate_artifact_workspace(state, artifact)


def test_implementation_rejects_manifest_outside_workspace(
    git_worktree: GitWorktree,
    tmp_path: Path,
) -> None:
    # Arrange
    external_manifest = tmp_path / "external-manifest.json"
    external_manifest.write_text("{}", encoding="utf-8")
    external_manifest.chmod(0o644)
    artifact = replace(
        _implementation_artifact(git_worktree),
        commit_sha=git_worktree.final_commit,
        experiment_manifest_path=str(external_manifest),
        experiment_manifest_sha256=sha256(external_manifest.read_bytes()).hexdigest(),
    )
    state = AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout))

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must be under its workspace"):
        validate_artifact_workspace(state, artifact)


def test_implementation_rejects_uncommitted_manifest(
    git_worktree: GitWorktree,
) -> None:
    # Arrange
    manifest_path, _, _, _, commit_sha, _ = _write_quantipy_v2_run(git_worktree)
    manifest = Path(manifest_path)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    artifact = replace(
        _implementation_artifact(git_worktree),
        commit_sha=commit_sha,
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
    )
    state = AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout))

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must be clean"):
        validate_artifact_workspace(state, artifact)


def test_implementation_rejects_group_writable_manifest(
    git_worktree: GitWorktree,
) -> None:
    manifest_path, manifest_sha, _, _, commit_sha, _ = _write_quantipy_v2_run(git_worktree)
    Path(manifest_path).chmod(0o664)
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=commit_sha,
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha,
    )

    with pytest.raises(AutoresearchValidationError, match="group- or world-writable"):
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )


def test_implementation_rejects_group_writable_experiment_package(
    git_worktree: GitWorktree,
) -> None:
    manifest_path, manifest_sha, _, _, commit_sha, _ = _write_quantipy_v2_run(git_worktree)
    package_path = Path(manifest_path).parent / "experiment"
    package_path.chmod(0o775)
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=commit_sha,
        experiment_manifest_path=manifest_path,
        experiment_manifest_sha256=manifest_sha,
    )

    with pytest.raises(AutoresearchValidationError, match="package directories"):
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (("duplicate_stage", "unique"), ("extra_panel", "exact keys")),
)
def test_implementation_parses_complete_quantipy_v2_manifest_schema(
    git_worktree: GitWorktree,
    mutation: str,
    message: str,
) -> None:
    manifest_path, _, _, _, _, _ = _write_quantipy_v2_run(git_worktree, panel_requested=True)
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if mutation == "duplicate_stage":
        manifest["stage_files"][1]["file_path"] = manifest["stage_files"][0]["file_path"]
    else:
        manifest["panel"]["request"]["unsupported"] = True
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_file.write_bytes(manifest_bytes)
    _git(git_worktree.workspace, "add", "experiment-manifest.json")
    _git(git_worktree.workspace, "commit", "-m", "malformed v2 manifest")
    artifact = replace(
        _implementation_result(),
        workspace_path=str(git_worktree.workspace),
        commit_sha=_git(git_worktree.workspace, "rev-parse", "HEAD"),
        experiment_manifest_path=str(manifest_file),
        experiment_manifest_sha256=sha256(manifest_bytes).hexdigest(),
    )

    with pytest.raises(AutoresearchValidationError, match=message):
        validate_artifact_workspace(
            AutoresearchState(setup=_workspace_setup(git_worktree.target_checkout)),
            artifact,
        )


def test_failed_run_feasibility_guard_does_not_block_failure_evidence(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> None:
    """A failed run's receipts are failure evidence and must stay submittable.

    The measured-projection guard exists to keep timeout derivations honest on
    successful runs; binding it on failed runs blocks reporting the very
    defect, which is the anti-pattern this validator family eliminates.
    """
    _, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
    )
    run_path = Path(evidence.run_json_path)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["success"] = False
    stage_receipts = cast(list[dict[str, object]], payload["stage_receipts"])
    feasibility = next(receipt for receipt in stage_receipts if receipt["stage"] == "feasibility")
    result = cast(dict[str, object], feasibility["result"])
    result["summary"] = json.dumps({"projected_model_seconds": 1.0})
    model = next(receipt for receipt in stage_receipts if receipt["stage"] == "model")
    model["status"] = "failed"
    model["result"] = None
    model["failure"] = {"category": "stage", "message": "ValueError: no qualifying candidate"}
    run_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    run_path.chmod(0o600)
    run_path.write_bytes(run_bytes)
    snapshot = autoresearch_secure_io._secure_open_snapshot(
        run_path,
        label="test Quantipy run.json",
    )

    run = autoresearch_evidence._validate_quantipy_run_envelope(snapshot)

    assert run["success"] is False
