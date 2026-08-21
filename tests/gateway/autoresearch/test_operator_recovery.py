from __future__ import annotations

import json
import os
import subprocess
from dataclasses import (
    replace,
)
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import patch

import gateway.autoresearch.artifacts as autoresearch_artifacts
import gateway.autoresearch.attestation as autoresearch_attestation
import gateway.autoresearch.attestation as runtime_attestation
import gateway.autoresearch.constants as autoresearch_constants
import gateway.autoresearch.engine as autoresearch_engine
import gateway.autoresearch.evidence as autoresearch_evidence
import gateway.autoresearch.fields as autoresearch_fields
import gateway.autoresearch.operator_recovery as autoresearch_operator_recovery
import gateway.autoresearch.persistence as autoresearch_persistence
import gateway.autoresearch.recovery_receipts as autoresearch_recovery_receipts
import gateway.autoresearch.secure_io as autoresearch_secure_io
import gateway.autoresearch.transitions as autoresearch_transitions
import gateway.autoresearch_runs as autoresearch_runs
import pytest
from gateway.autoresearch import constants
from gateway.autoresearch.artifacts import (
    VerificationResultArtifact,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_STATE_PATH,
    DEFAULT_OPENCLAW_CONFIG_PATH,
)
from gateway.autoresearch.engine import (
    next_action,
)
from gateway.autoresearch.enums import (
    FixTriggerPhase,
    Phase,
    VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.manifest_runtime import (
    expected_instruction_manifest_sha256,
)
from gateway.autoresearch.operator_recovery import (
    retry_external_verification,
    retry_external_verification_state_file,
)
from gateway.autoresearch.persistence import (
    save_state_file,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy,
    ReceiptCatalog,
)
from gateway.autoresearch.recovery_receipts import (
    ExternalVerificationRetryReceipt,
)
from gateway.autoresearch.state import (
    AutoresearchState,
    AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    validate_state,
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest,
    ReadinessIdentity,
    ResearchPanelProbeReceipt,
)
from gateway.cli import app
from typer.testing import CliRunner

from tests.gateway.autoresearch.builders import (
    PublicPlatformRecoveryFixture,
    _git,
    _implementation_result,
    _majority_consensus,
    _state_to_consensus,
    _verification_result,
    _workspace_setup,
    advance_state,
)


def test_external_verification_retry_preserves_failure_and_reuses_implementation_with_v3_run(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    materialized = autoresearch_operator_recovery._materialize_attested_pending_retry_failure(
        stale_state,
        policy=policy,
        validation_context=validation_context,
    )
    receipt = ExternalVerificationRetryReceipt.for_state(
        materialized,
        probe,
        "Restarted the stale Quantipy API service.",
    )

    # Act
    retried = retry_external_verification(materialized, receipt)

    # Assert
    assert retried.phase is Phase.VERIFICATION
    assert retried.pending_fix_trigger is None
    assert retried.implementation_result == materialized.implementation_result
    assert retried.verification_history == materialized.verification_history
    assert retried.external_verification_retry_receipt == receipt
    assert receipt.expected_run_id.endswith("-v3")


def test_external_verification_retry_rejects_a_near_match_failure_message(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(_implementation_result(), commit_sha="a1a1a1a1a1a1"),
        policy,
    )
    failed_state = advance_state(
        state,
        _verification_result(VerificationStatus.TEST_FAILURE, external_panel_failure=True),
        policy,
    )
    latest = failed_state.latest_verification
    assert latest is not None
    evidence = latest.quantipy_experiment_evidence
    assert evidence is not None
    failure = evidence.failure
    assert failure is not None
    tampered = replace(
        failed_state,
        verification_history=(
            replace(
                latest,
                quantipy_experiment_evidence=replace(
                    evidence,
                    failure=replace(
                        failure,
                        message="ResearchPanelArchiveError: HTTP 413 without local panel context",
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="historical local research-panel HTTP 404",
    ):
        ExternalVerificationRetryReceipt.for_state(
            tampered,
            ResearchPanelProbeReceipt(
                endpoint="http://127.0.0.1:8000/price-data/research-panel",
                observed_at="2026-07-29T12:00:00Z",
                response_bytes=18,
                response_sha256="a" * 64,
                session_date="2022-01-03",
                symbol="AAPL",
            ),
            "Restarted the stale Quantipy API service.",
        )


def test_external_verification_retry_replaces_v2_receipt_with_deterministic_v3(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    failed_v2_state = autoresearch_operator_recovery._materialize_attested_pending_retry_failure(
        stale_state,
        policy=policy,
        validation_context=validation_context,
    )

    # Act
    v3_receipt = ExternalVerificationRetryReceipt.for_state(
        failed_v2_state, probe, "Raised the gateway receipt limit again."
    )
    retried = retry_external_verification(failed_v2_state, v3_receipt)

    # Assert
    assert retried.verification_history == failed_v2_state.verification_history
    assert retried.external_verification_retry_receipt == v3_receipt
    assert v3_receipt.expected_run_id.endswith("-v3")
    assert v3_receipt.schema_version == 2
    assert v3_receipt.verification_history_sha256 == tuple(
        autoresearch_fields._canonical_json_digest(artifact.to_dict())
        for artifact in failed_v2_state.verification_history
    )
    validate_state(retried, policy)


@pytest.mark.parametrize(
    "tampering", ("v1_null_summary", "v2_null_summary", "missing", "reordered")
)
def test_v3_retry_receipt_rejects_tampered_complete_prior_history(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
    tampering: str,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    failed_v2_state = autoresearch_operator_recovery._materialize_attested_pending_retry_failure(
        stale_state,
        policy=policy,
        validation_context=validation_context,
    )
    v3_receipt = ExternalVerificationRetryReceipt.for_state(
        failed_v2_state, probe, "Raised the gateway receipt limit again."
    )
    retried = retry_external_verification(failed_v2_state, v3_receipt)
    first, second = retried.verification_history
    history: tuple[VerificationResultArtifact, ...]
    if tampering == "v1_null_summary":
        history = (replace(first, null_test_summary="tampered first summary"), second)
    elif tampering == "v2_null_summary":
        history = (first, replace(second, null_test_summary="tampered second summary"))
    elif tampering == "missing":
        history = (first,)
    else:
        history = (second, first)
    tampered = replace(retried, verification_history=history)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="external verification retry"):
        validate_state(tampered, policy, validation_context)


def test_external_verification_retry_does_not_authorize_v4_from_a_sealed_generic_history(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    failed_v2_state = autoresearch_operator_recovery._materialize_attested_pending_retry_failure(
        stale_state,
        policy=policy,
        validation_context=validation_context,
    )
    v3_receipt = ExternalVerificationRetryReceipt.for_state(
        failed_v2_state, probe, "Raised the gateway receipt limit again."
    )
    retried_v3_state = retry_external_verification(failed_v2_state, v3_receipt)
    v2_failure = failed_v2_state.latest_verification
    assert v2_failure is not None
    v2_evidence = v2_failure.quantipy_experiment_evidence
    assert v2_evidence is not None
    sealed_v3_failure = replace(
        v2_failure,
        quantipy_experiment_evidence=replace(v2_evidence, run_id=v3_receipt.expected_run_id),
    )
    failed_v3_state = replace(
        retried_v3_state,
        phase=Phase.FIX_TEST,
        pending_fix_trigger=FixTriggerPhase.VERIFICATION,
        verification_history=(*retried_v3_state.verification_history, sealed_v3_failure),
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="interrupted verification recovery accepts only the exact pending v3 topology",
    ):
        ExternalVerificationRetryReceipt.for_state(
            failed_v3_state, probe, "Raised the gateway receipt limit a final time."
        )


def test_retry_state_file_materializes_the_attested_live_v2_http_413_to_v3(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, probe, validation_context, immutable_hashes = live_v2_http_413_state_file

    # Act
    retried = retry_external_verification_state_file(
        state_path,
        probe,
        operator_reason="Raised the gateway receipt limit again.",
        policy=policy,
        validation_context=validation_context,
    )

    # Assert
    assert retried.external_verification_retry_receipt is not None
    assert retried.external_verification_retry_receipt.expected_run_id.endswith("-v3")
    assert retried.latest_verification is not None
    assert retried.latest_verification.quantipy_experiment_evidence is not None
    assert retried.latest_verification.quantipy_experiment_evidence.run_id.endswith("-v2")
    assert len(retried.verification_history) == 2
    assert retried.external_verification_retry_receipt.prior_verification_sha256 == (
        autoresearch_fields._canonical_json_digest(retried.verification_history[-1].to_dict())
    )
    assert tuple((path, sha256(path.read_bytes()).hexdigest()) for path, _ in immutable_hashes) == (
        immutable_hashes
    )


def test_public_platform_runtime_recovery_publishes_only_a_copied_live_v4_state(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    fixture = public_platform_v4_recovery_fixture

    # Act
    recovered = autoresearch_operator_recovery.recover_platform_runtime_state_file(
        fixture.copied_state_path,
        probe=fixture.probe,
        operator_reason="Moved verification to the sealed canonical runtime.",
        policy=policy,
        validation_context=fixture.validation_context,
        systemd_is_active=lambda _unit: False,
        proc_root=fixture.copied_state_path.parent / "proc",
    )
    reattested = autoresearch_attestation.require_canonical_verification_dispatch_attestation(
        fixture.copied_state_path,
        policy=policy,
        validation_context=fixture.validation_context,
    )

    # Assert
    assert autoresearch_persistence.load_state_file(fixture.copied_state_path) == recovered
    assert reattested == recovered
    assert recovered.external_verification_retry_receipt is not None
    assert recovered.external_verification_retry_receipt.expected_run_id.endswith("-v5")
    assert recovered.platform_runtime_recovery_receipt is not None
    assert recovered.canonical_quantipy_runtime_attestation is not None
    assert recovered.platform_readiness == fixture.validation_context.readiness_identity
    assert recovered.platform_readiness is not None
    assert (
        recovered.platform_readiness.quantipy_commit
        == fixture.readiness.require_ready().quantipy_commit
    )
    persisted_copy = json.loads(fixture.copied_state_path.read_text(encoding="utf-8"))
    assert set(cast(dict[str, object], persisted_copy["platform_readiness"])) == {
        "manifest_id",
        "snapshot_id",
        "receipt_sha256",
        "quantipy_commit",
    }
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes
    assert (
        sha256(fixture.live_state_path.read_bytes()).digest()
        == sha256(fixture.live_state_bytes).digest()
    )
    assert (
        tuple((path, sha256(path.read_bytes()).hexdigest()) for path, _ in fixture.artifact_hashes)
        == fixture.artifact_hashes
    )


def test_public_platform_runtime_recovery_rejects_unrelated_current_readiness_identity(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
) -> None:
    fixture = public_platform_v4_recovery_fixture
    current_identity = fixture.validation_context.readiness_identity
    assert current_identity is not None
    unrelated_context = replace(
        fixture.validation_context,
        readiness_identity=replace(current_identity, snapshot_id="snapshot-unrelated"),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="may differ only by the current nonnull Quantipy commit",
    ):
        autoresearch_operator_recovery.recover_platform_runtime_state_file(
            fixture.copied_state_path,
            probe=fixture.probe,
            operator_reason="Moved verification to the sealed canonical runtime.",
            policy=policy,
            validation_context=unrelated_context,
            systemd_is_active=lambda _unit: False,
            proc_root=fixture.copied_state_path.parent / "proc",
        )

    assert fixture.copied_state_path.read_bytes() == fixture.live_state_bytes
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes


@pytest.mark.parametrize("race", ("runtime", "source", "status", "run", "state"))
def test_public_platform_runtime_recovery_rejects_publication_races(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    race: str,
) -> None:
    # Arrange
    fixture = public_platform_v4_recovery_fixture
    state = autoresearch_persistence.load_state_file(fixture.live_state_path)
    retry = state.external_verification_retry_receipt
    implementation = state.implementation_result
    setup = state.setup
    assert retry is not None
    assert implementation is not None
    assert setup is not None
    mutation_applied = False

    def mutate_during_publication(_unit: str) -> bool:
        nonlocal mutation_applied
        if mutation_applied:
            return False
        mutation_applied = True
        if race == "runtime":
            entrypoint = Path(setup.target_repo) / ".venv" / "bin" / "quantipy"
            entrypoint.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            entrypoint.chmod(0o775)
        elif race == "source":
            Path(implementation.experiment_manifest_path).write_text("{}\n", encoding="utf-8")
        elif race == "status":
            status_path = (
                autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
                / (f"i{state.iteration}-verification-r1-a4-{implementation.commit_sha[:12]}-v4")
                / "status.json"
            )
            status_path.parent.chmod(0o700)
            status_path.chmod(0o600)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["updated_at"] = status["started_at"]
            status_path.write_text(
                json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            status_path.chmod(0o400)
            status_path.parent.chmod(0o500)
        elif race == "run":
            run_path = (
                autoresearch_constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT
                / retry.expected_run_id
                / "run.json"
            )
            run_path.chmod(0o600)
            run_path.write_bytes(run_path.read_bytes() + b"\n")
            run_path.chmod(0o400)
        else:
            fixture.copied_state_path.write_bytes(fixture.copied_state_path.read_bytes() + b"\n")
        return False

    # Act / Assert
    with pytest.raises(ValueError):
        autoresearch_operator_recovery.recover_platform_runtime_state_file(
            fixture.copied_state_path,
            probe=fixture.probe,
            operator_reason="Moved verification to the sealed canonical runtime.",
            policy=policy,
            validation_context=fixture.validation_context,
            systemd_is_active=mutate_during_publication,
            proc_root=fixture.copied_state_path.parent / "proc",
        )
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes


@pytest.mark.parametrize("race", ("runtime", "source", "state"))
def test_public_autoresearch_next_rejects_post_action_dispatch_race(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    quantipy_root: Path,
    tmp_path: Path,
    race: str,
) -> None:
    # Arrange
    fixture = public_platform_v4_recovery_fixture
    recovered = autoresearch_operator_recovery.recover_platform_runtime_state_file(
        fixture.copied_state_path,
        probe=fixture.probe,
        operator_reason="Moved verification to the sealed canonical runtime.",
        policy=policy,
        validation_context=fixture.validation_context,
        systemd_is_active=lambda _unit: False,
        proc_root=tmp_path / "proc",
    )
    readiness_path = tmp_path / "strict-readiness.json"
    readiness_path.write_text(
        json.dumps(fixture.readiness.to_dict(), sort_keys=True),
        encoding="utf-8",
    )
    original_next_action = autoresearch_engine.next_action

    def mutate_after_action(
        state: AutoresearchState,
        action_policy: AutoresearchPolicy,
        action_receipts: ReceiptCatalog,
        readiness: PlatformReadinessManifest,
        *,
        state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
    ) -> autoresearch_artifacts.NextAction:
        action = original_next_action(
            state,
            action_policy,
            action_receipts,
            readiness,
            state_path=state_path,
        )
        if race == "runtime":
            runtime = state.canonical_quantipy_runtime_attestation
            assert runtime is not None
            Path(runtime.executable_path).write_text(
                "#!/bin/sh\nexit 1\n",
                encoding="utf-8",
            )
            Path(runtime.executable_path).chmod(0o775)
        elif race == "source":
            implementation = state.implementation_result
            assert implementation is not None
            Path(implementation.experiment_manifest_path).write_text(
                "{}\n",
                encoding="utf-8",
            )
        else:
            save_state_file(
                state_path,
                replace(state, verification_fix_attempts=state.verification_fix_attempts + 1),
            )
        return action

    # Act
    with patch(
        "gateway.autoresearch.engine.next_action",
        new=mutate_after_action,
    ):
        result = CliRunner().invoke(
            app,
            (
                "autoresearch-next",
                str(fixture.copied_state_path),
                "--quantipy-root",
                str(quantipy_root),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--readiness-manifest",
                str(readiness_path),
            ),
        )

    # Assert
    assert result.exit_code == 1
    assert "autoresearch-next failed" in result.output
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes
    assert (
        autoresearch_persistence.load_state_file(
            fixture.copied_state_path
        ).canonical_quantipy_runtime_attestation
        == recovered.canonical_quantipy_runtime_attestation
    )


def test_public_autoresearch_next_returns_only_the_final_guarded_state_reference(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    quantipy_root: Path,
    tmp_path: Path,
) -> None:
    # Arrange
    fixture = public_platform_v4_recovery_fixture
    autoresearch_operator_recovery.recover_platform_runtime_state_file(
        fixture.copied_state_path,
        probe=fixture.probe,
        operator_reason="Moved verification to the sealed canonical runtime.",
        policy=policy,
        validation_context=fixture.validation_context,
        systemd_is_active=lambda _unit: False,
        proc_root=tmp_path / "proc",
    )
    readiness_path = tmp_path / "strict-readiness.json"
    readiness_path.write_text(
        json.dumps(fixture.readiness.to_dict(), sort_keys=True),
        encoding="utf-8",
    )

    # Act
    result = CliRunner().invoke(
        app,
        (
            "autoresearch-next",
            str(fixture.copied_state_path),
            "--quantipy-root",
            str(quantipy_root),
            "--openclaw-config",
            str(DEFAULT_OPENCLAW_CONFIG_PATH),
            "--readiness-manifest",
            str(readiness_path),
        ),
    )

    # Assert
    assert result.exit_code == 0, result.output
    sealed = autoresearch_persistence.load_state_file(fixture.copied_state_path)
    action = next_action(
        sealed,
        policy,
        receipts,
        fixture.readiness,
        state_path=fixture.copied_state_path,
    )
    assert action.state_reference_sha256 in result.output
    assert (
        autoresearch_transitions.build_authoritative_state_reference(
            sealed,
            state_path=fixture.copied_state_path,
        ).sha256()
        == action.state_reference_sha256
    )
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes


def test_sealed_quantipy_panel_directory_rejects_a_foreign_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    panel_directory = tmp_path / "panel"
    panel_directory.mkdir(mode=0o500)
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: foreign_uid)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="Quantipy panel directory"):
        autoresearch_secure_io._require_sealed_quantipy_panel_directory(panel_directory)


def test_interrupted_verification_receipt_binds_the_pre_recovery_topology() -> None:
    # Arrange
    receipt_type = autoresearch_recovery_receipts.InterruptedVerificationAttemptReceipt
    prior_retry_receipt = ExternalVerificationRetryReceipt(
        expected_run_id="autoresearch-i1-aaaaaaaaaaaa-v3",
        prior_verification_sha256="3" * 64,
        probe=ResearchPanelProbeReceipt(
            endpoint="http://127.0.0.1:8000/price-data/research-panel",
            observed_at="2026-07-29T12:00:00Z",
            response_bytes=18,
            response_sha256="a" * 64,
            session_date="2022-01-03",
            symbol="AAPL",
        ),
        retry_attempt=3,
        implementation_commit="a" * 40,
        manifest_sha256="b" * 64,
        readiness_manifest_id="manifest-1",
        readiness_snapshot_id="snapshot-1",
        operator_reason="Raised the gateway receipt limit again.",
        verification_history_sha256=("2" * 64, "3" * 64),
    )

    # Act
    receipt = receipt_type(
        expected_run_id="autoresearch-i1-aaaaaaaaaaaa-v3",
        interrupted_attempt=3,
        implementation_commit="a" * 40,
        implementation_manifest_sha256="b" * 64,
        detached_run_directory="/tmp/autoresearch-runs/i1-verification-r1-a3",
        detached_run_manifest_sha256="c" * 64,
        detached_run_status_sha256="d" * 64,
        state_sha256="e" * 64,
        state_reference_sha256="0" * 64,
        instruction_manifest_sha256="f" * 64,
        prior_retry_receipt_sha256=autoresearch_fields._canonical_json_digest(
            prior_retry_receipt.to_dict()
        ),
        prior_retry_receipt=prior_retry_receipt,
        verification_history_sha256=("2" * 64, "3" * 64),
        operator_reason="Stopped the detached v3 process.",
    )

    # Assert
    assert receipt.to_dict()["interrupted_attempt"] == 3
    assert receipt.to_dict()["prior_retry_receipt"] == prior_retry_receipt.to_dict()


@pytest.mark.parametrize("artifact_appears_during_inactivity_check", (False, True))
def test_interrupted_v3_recovery_records_an_interruption_without_creating_a_verification_artifact(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
    artifact_appears_during_inactivity_check: bool,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    v3_state = retry_external_verification_state_file(
        state_path,
        probe,
        operator_reason="Raised the gateway receipt limit again.",
        policy=policy,
        validation_context=validation_context,
    )
    v3_receipt = v3_state.external_verification_retry_receipt
    assert v3_receipt is not None
    implementation = v3_state.implementation_result
    assert implementation is not None
    state_reference = autoresearch_transitions.build_authoritative_state_reference(
        v3_state, state_path=state_path
    ).sha256()
    instruction_digest = expected_instruction_manifest_sha256(
        v3_state, policy, receipts, state_path=state_path
    )
    task_label = (
        f"autoresearch-i{v3_state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
    )
    run_dir = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
        f"i{v3_state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
    )
    run_path = (
        autoresearch_constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT
        / v3_receipt.expected_run_id
        / "run.json"
    )
    command = (
        "bash",
        "-lc",
        " ".join(
            (
                "env",
                "PYTHONDONTWRITEBYTECODE=1",
                "uv",
                "run",
                "quantipy",
                "experiment",
                "run",
                implementation.experiment_manifest_path,
                "--output-root",
                str(autoresearch_constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT),
                "--run-id",
                v3_receipt.expected_run_id,
            )
        ),
    )
    manifest_path = tmp_path / "copied-live-v3-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": v3_state.iteration,
                "phase": "verification",
                "attempt": 3,
                "task_label": task_label,
                "state_reference_sha256": state_reference,
                "instruction_manifest_sha256": instruction_digest,
                "run_directory": str(run_dir),
                "working_directory": implementation.workspace_path,
                "command_sha256": autoresearch_runs.command_sha256(command),
                "expected_artifact_path": str(run_path),
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        command=command,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    autoresearch_runs.prepare_output_capture(
        run_dir=run_dir, runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
    )
    for stream in autoresearch_runs.RunOutputStream:
        autoresearch_runs.capture_output_stream(
            run_dir=run_dir,
            runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            stream=stream,
            source=BytesIO(b""),
        )
    autoresearch_runs.start_run(
        run_dir=run_dir,
        pid=999_999,
        systemd_unit="openclaw-long-task-1-1.service",
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    autoresearch_runs.complete_run(
        run_dir=run_dir,
        exit_code=143,
        signal_number=None,
        peak_rss_bytes=None,
        failure_classification=autoresearch_runs.RunFailureClassification.OPERATOR_STOPPED,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    preserved_v3_files = tuple(
        (path, sha256(path.read_bytes()).hexdigest())
        for path in sorted(path for path in run_dir.rglob("*") if path.is_file())
    )
    unrelated_record = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / "historical-malformed"
    unrelated_record.mkdir(mode=0o700)
    malformed_manifest = unrelated_record / "manifest.json"
    malformed_manifest.write_text("not JSON", encoding="utf-8")
    unrelated_manifest_digest = sha256(malformed_manifest.read_bytes()).hexdigest()
    state_before_recovery = state_path.read_bytes()

    def inactive_unit(_unit: str) -> bool:
        if artifact_appears_during_inactivity_check:
            run_path.parent.mkdir(mode=0o700)
            run_path.write_text("{}", encoding="utf-8")
        return False

    # Act / Assert
    if artifact_appears_during_inactivity_check:
        with pytest.raises(AutoresearchValidationError, match=r"run\.json to be absent"):
            autoresearch_operator_recovery.recover_interrupted_verification_state_file(
                state_path,
                operator_reason="Stopped the detached v3 process before it produced run.json.",
                policy=policy,
                receipts=receipts,
                validation_context=validation_context,
                systemd_is_active=inactive_unit,
                proc_root=tmp_path / "proc",
            )
        assert state_path.read_bytes() == state_before_recovery
        return

    recovered = autoresearch_operator_recovery.recover_interrupted_verification_state_file(
        state_path,
        operator_reason="Stopped the detached v3 process before it produced run.json.",
        policy=policy,
        receipts=receipts,
        validation_context=validation_context,
        systemd_is_active=inactive_unit,
        proc_root=tmp_path / "proc",
    )

    # Assert
    assert recovered.external_verification_retry_receipt is not None
    assert recovered.external_verification_retry_receipt.expected_run_id.endswith("-v4")
    assert len(recovered.verification_history) == 2
    assert len(recovered.interrupted_verification_history) == 1
    assert (
        tuple((path, sha256(path.read_bytes()).hexdigest()) for path, _ in preserved_v3_files)
        == preserved_v3_files
    )
    assert sha256(malformed_manifest.read_bytes()).hexdigest() == unrelated_manifest_digest


def test_interrupted_v3_recovery_rejects_duplicate_expected_manifest_identities(
    tmp_path: Path,
) -> None:
    # Arrange
    runs_root = tmp_path / "runs"
    runs_root.mkdir(mode=0o700)
    directory_name = "i1-verification-r1-a3-aaaaaaaaaaaa-v3"
    task_label = "autoresearch-i1-verification-r1-a3-aaaaaaaaaaaa-v3"
    for name, instruction_digest in (
        (directory_name, "c" * 64),
        ("duplicate", "e" * 64),
    ):
        run_dir = runs_root / name
        run_dir.mkdir(mode=0o700)
        manifest = autoresearch_runs.RunManifest(
            schema_version=1,
            iteration=1,
            phase=Phase.VERIFICATION,
            attempt=3,
            task_label=task_label,
            state_reference_sha256="b" * 64,
            instruction_manifest_sha256=instruction_digest,
            run_directory=str(run_dir),
            working_directory=str(tmp_path),
            command_sha256="d" * 64,
            expected_artifact_path=None,
            timeout_seconds=None,
        )
        (run_dir / "manifest.json").write_bytes(
            json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        (run_dir / "manifest.json").chmod(0o400)
        run_dir.chmod(0o500)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="duplicate expected detached v3"):
        autoresearch_operator_recovery._find_exact_interrupted_detached_run(
            runs_root=runs_root,
            iteration=1,
            directory_name=directory_name,
            task_label=task_label,
            state_reference_sha256="b" * 64,
        )


def test_generic_v4_http_413_retry_is_rejected_in_favor_of_platform_runtime_recovery(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    v3_state = retry_external_verification_state_file(
        state_path,
        probe,
        operator_reason="Raised the gateway receipt limit again.",
        policy=policy,
        validation_context=validation_context,
    )
    v3_receipt = v3_state.external_verification_retry_receipt
    implementation = v3_state.implementation_result
    assert v3_receipt is not None
    assert implementation is not None
    state_reference = autoresearch_transitions.build_authoritative_state_reference(
        v3_state, state_path=state_path
    ).sha256()
    instruction_digest = expected_instruction_manifest_sha256(
        v3_state, policy, receipts, state_path=state_path
    )
    task_label = (
        f"autoresearch-i{v3_state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
    )
    run_dir = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
        f"i{v3_state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
    )
    run_path = (
        autoresearch_constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT
        / v3_receipt.expected_run_id
        / "run.json"
    )
    command = (
        "bash",
        "-lc",
        " ".join(
            (
                "env",
                "PYTHONDONTWRITEBYTECODE=1",
                "uv",
                "run",
                "quantipy",
                "experiment",
                "run",
                implementation.experiment_manifest_path,
                "--output-root",
                str(autoresearch_constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT),
                "--run-id",
                v3_receipt.expected_run_id,
            )
        ),
    )
    manifest_path = tmp_path / "v5-recovery-v3-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": v3_state.iteration,
                "phase": "verification",
                "attempt": 3,
                "task_label": task_label,
                "state_reference_sha256": state_reference,
                "instruction_manifest_sha256": instruction_digest,
                "run_directory": str(run_dir),
                "working_directory": implementation.workspace_path,
                "command_sha256": autoresearch_runs.command_sha256(command),
                "expected_artifact_path": str(run_path),
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        command=command,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    autoresearch_runs.prepare_output_capture(
        run_dir=run_dir, runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
    )
    for stream in autoresearch_runs.RunOutputStream:
        autoresearch_runs.capture_output_stream(
            run_dir=run_dir,
            runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            stream=stream,
            source=BytesIO(b""),
        )
    autoresearch_runs.start_run(
        run_dir=run_dir,
        pid=999_999,
        systemd_unit="openclaw-long-task-1-1.service",
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    autoresearch_runs.complete_run(
        run_dir=run_dir,
        exit_code=143,
        signal_number=None,
        peak_rss_bytes=None,
        failure_classification=autoresearch_runs.RunFailureClassification.OPERATOR_STOPPED,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    recovered = autoresearch_operator_recovery.recover_interrupted_verification_state_file(
        state_path,
        operator_reason="Stopped the detached v3 process before it produced run.json.",
        policy=policy,
        receipts=receipts,
        validation_context=validation_context,
        systemd_is_active=lambda _unit: False,
        proc_root=tmp_path / "proc",
    )
    v4_receipt = recovered.external_verification_retry_receipt
    v2_failure = recovered.latest_verification
    assert v4_receipt is not None
    assert v2_failure is not None
    v2_evidence = v2_failure.quantipy_experiment_evidence
    assert v2_evidence is not None
    failed_v4 = replace(
        recovered,
        phase=Phase.FIX_TEST,
        pending_fix_trigger=FixTriggerPhase.VERIFICATION,
        verification_history=(
            *recovered.verification_history,
            replace(
                v2_failure,
                quantipy_experiment_evidence=replace(
                    v2_evidence,
                    run_id=v4_receipt.expected_run_id,
                ),
            ),
        ),
    )
    next_probe = replace(probe, response_bytes=19, response_sha256="b" * 64)

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="platform receipt failure requires operator platform runtime recovery",
    ):
        ExternalVerificationRetryReceipt.for_state(
            failed_v4,
            next_probe,
            "Increased the response limit after the v4 failure.",
        )


def test_external_verification_retry_receipt_rejects_the_removed_v6_through_v9_path() -> None:
    # Arrange
    probe = ResearchPanelProbeReceipt(
        endpoint="http://127.0.0.1:8000/price-data/research-panel",
        observed_at="2026-07-29T12:00:00Z",
        response_bytes=18,
        response_sha256="a" * 64,
        session_date="2022-01-03",
        symbol="AAPL",
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="attempt is not supported"):
        ExternalVerificationRetryReceipt(
            expected_run_id="autoresearch-i1-aaaaaaaaaaaa-v6",
            prior_verification_sha256="b" * 64,
            probe=probe,
            retry_attempt=6,
            implementation_commit="a" * 40,
            manifest_sha256="c" * 64,
            readiness_manifest_id="manifest-1",
            readiness_snapshot_id="snapshot-1",
            operator_reason="Attempted removed generic retry.",
            verification_history_sha256=("1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64),
            interruption_history_sha256=(),
            schema_version=autoresearch_constants.INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        )


def test_v2_retry_receipt_rejects_an_unrelated_historical_http_404(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, _, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    prior = stale_state.latest_verification
    assert prior is not None
    evidence = prior.quantipy_experiment_evidence
    assert evidence is not None
    failure = evidence.failure
    assert failure is not None
    unrelated = replace(
        stale_state,
        verification_history=(
            replace(
                prior,
                quantipy_experiment_evidence=replace(
                    evidence,
                    failure=replace(
                        failure,
                        message=(
                            "ExperimentPanelError: Client error '404 Not Found' for url "
                            "'http://127.0.0.1:8000/price-data/research-panel?"
                            "tickers=UNRELATED'\nFor more information check: "
                            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
                        ),
                    ),
                ),
            ),
        ),
    )
    receipt = unrelated.external_verification_retry_receipt
    assert receipt is not None
    unrelated = replace(
        unrelated,
        external_verification_retry_receipt=replace(
            receipt,
            prior_verification_sha256=autoresearch_fields._canonical_json_digest(
                unrelated.verification_history[0].to_dict()
            ),
        ),
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="historical local research-panel HTTP 404",
    ):
        validate_state(unrelated, policy, validation_context)


@pytest.mark.parametrize(
    "tampering",
    ("reordered_query", "missing_mdn"),
)
def test_v2_retry_receipt_rejects_a_tampered_historical_http_404_message(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
    tampering: str,
) -> None:
    # Arrange
    state_path, _, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    initial = stale_state.verification_history[0]
    evidence = initial.quantipy_experiment_evidence
    assert evidence is not None
    failure = evidence.failure
    assert failure is not None
    if tampering == "reordered_query":
        message_prefix, message_suffix = failure.message.split(" for url '", maxsplit=1)
        url, mdn = message_suffix.split("'\n", maxsplit=1)
        endpoint, query = url.split("?", maxsplit=1)
        query_parts = query.split("&")
        replacement = (
            f"{message_prefix} for url '{endpoint}?{query_parts[1]}&{query_parts[0]}&"
            f"{'&'.join(query_parts[2:])}'\n{mdn}"
        )
    else:
        replacement = failure.message.split("\n", maxsplit=1)[0]
    tampered_initial = replace(
        initial,
        quantipy_experiment_evidence=replace(
            evidence, failure=replace(failure, message=replacement)
        ),
    )
    receipt = stale_state.external_verification_retry_receipt
    assert receipt is not None
    tampered = replace(
        stale_state,
        verification_history=(tampered_initial,),
        external_verification_retry_receipt=replace(
            receipt,
            prior_verification_sha256=autoresearch_fields._canonical_json_digest(
                tampered_initial.to_dict()
            ),
        ),
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError, match="historical local research-panel HTTP 404"
    ):
        validate_state(tampered, policy, validation_context)


def test_v2_retry_receipt_rejects_an_appended_verification_history_entry(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, _, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    initial = stale_state.verification_history[0]
    receipt = stale_state.external_verification_retry_receipt
    assert receipt is not None
    tampered = replace(
        stale_state,
        verification_history=(initial, initial),
        external_verification_retry_receipt=replace(
            receipt,
            prior_verification_sha256=autoresearch_fields._canonical_json_digest(initial.to_dict()),
        ),
    )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="verification history topology"):
        validate_state(tampered, policy, validation_context)


def test_external_verification_retry_rejects_an_unrelated_legacy_http_413_query(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, probe, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    materialized = autoresearch_operator_recovery._materialize_attested_pending_retry_failure(
        stale_state,
        policy=policy,
        validation_context=validation_context,
    )
    latest = materialized.latest_verification
    assert latest is not None
    evidence = latest.quantipy_experiment_evidence
    assert evidence is not None
    failure = evidence.failure
    assert failure is not None
    unrelated = replace(
        materialized,
        verification_history=(
            *materialized.verification_history[:-1],
            replace(
                latest,
                quantipy_experiment_evidence=replace(
                    evidence,
                    failure=replace(
                        failure,
                        message=(
                            "ExperimentPanelError: Client error '413 Request Entity Too Large' "
                            "for url 'http://127.0.0.1:8000/price-data/research-panel?"
                            "tickers=UNRELATED'\nFor more information check: "
                            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/413"
                        ),
                    ),
                ),
            ),
        ),
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="local research-panel HTTP 413",
    ):
        ExternalVerificationRetryReceipt.for_state(
            unrelated,
            probe,
            "Raised the gateway receipt limit again.",
        )


def test_external_verification_retry_receipt_rejects_an_unrelated_legacy_http_413_query(
    live_v2_http_413_state_file: tuple[
        Path,
        ResearchPanelProbeReceipt,
        AutoresearchValidationContext,
        tuple[tuple[Path, str], ...],
    ],
    policy: AutoresearchPolicy,
) -> None:
    # Arrange
    state_path, _, validation_context, _ = live_v2_http_413_state_file
    stale_state = autoresearch_persistence.load_state_file(state_path)
    prior = stale_state.verification_history[0]
    evidence = prior.quantipy_experiment_evidence
    assert evidence is not None
    failure = evidence.failure
    assert failure is not None
    unrelated = replace(
        stale_state,
        verification_history=(
            replace(
                prior,
                quantipy_experiment_evidence=replace(
                    evidence,
                    failure=replace(
                        failure,
                        message=(
                            "ExperimentPanelError: Client error '413 Request Entity Too Large' "
                            "for url 'http://127.0.0.1:8000/price-data/research-panel?"
                            "tickers=UNRELATED'\nFor more information check: "
                            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/413"
                        ),
                    ),
                ),
            ),
        ),
    )
    receipt = unrelated.external_verification_retry_receipt
    assert receipt is not None
    unrelated = replace(
        unrelated,
        external_verification_retry_receipt=replace(
            receipt,
            prior_verification_sha256=autoresearch_fields._canonical_json_digest(
                unrelated.verification_history[0].to_dict()
            ),
        ),
    )

    # Act / Assert
    with pytest.raises(
        AutoresearchValidationError,
        match="historical local research-panel HTTP 404",
    ):
        validate_state(unrelated, policy, validation_context)


def test_external_verification_retry_command_path_rejects_schema_v3_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(_implementation_result(), commit_sha="a1a1a1a1a1a1"),
        policy,
    )
    failed_state = advance_state(
        state,
        _verification_result(VerificationStatus.TEST_FAILURE, external_panel_failure=True),
        policy,
    )
    v3_payload = failed_state.to_dict()
    v3_payload["schema_version"] = 3
    del v3_payload["external_verification_retry_receipt"]
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(v3_payload), encoding="utf-8")
    probe = ResearchPanelProbeReceipt(
        endpoint="http://127.0.0.1:8000/price-data/research-panel",
        observed_at="2026-07-29T12:00:00Z",
        response_bytes=18,
        response_sha256="a" * 64,
        session_date="2022-01-03",
        symbol="AAPL",
    )

    with pytest.raises(AutoresearchValidationError, match="compatible schema-v6"):
        retry_external_verification_state_file(
            state_path,
            probe,
            operator_reason="Restarted the stale Quantipy API service.",
            policy=policy,
            validation_context=AutoresearchValidationContext.from_readiness(platform_readiness),
        )


def test_quantipy_execution_contract_uses_canonical_runtime_and_immutable_source(
    tmp_path: Path,
) -> None:
    # Arrange
    runtime_root = tmp_path / "quantipy-runtime"
    manifest_path = tmp_path / "worktrees" / "alpha" / "experiment-manifest.json"
    output_root = tmp_path / "runs"

    # Act
    contract = autoresearch_evidence.build_quantipy_execution_contract(
        runtime_root=runtime_root,
        manifest_path=manifest_path,
        output_root=output_root,
        run_id="autoresearch-i1-aaaaaaaaaaaa-v5",
    )

    # Assert
    assert contract.command == (
        "env",
        "PYTHONDONTWRITEBYTECODE=1",
        "uv",
        "--directory",
        str(runtime_root),
        "run",
        "--frozen",
        "--no-sync",
        "quantipy",
        "experiment",
        "run",
        str(manifest_path),
        "--output-root",
        str(output_root),
        "--run-id",
        "autoresearch-i1-aaaaaaaaaaaa-v5",
    )
    assert contract.working_directory == runtime_root


def test_quantipy_execution_contract_allows_unsuffixed_ordinary_canonical_run(
    tmp_path: Path,
) -> None:
    contract = autoresearch_evidence.build_quantipy_execution_contract(
        runtime_root=tmp_path / "quantipy-runtime",
        manifest_path=tmp_path / "worktrees" / "alpha" / "experiment-manifest.json",
        output_root=tmp_path / "runs",
        run_id="autoresearch-i1-aaaaaaaaaaaa",
    )

    assert contract.run_id == "autoresearch-i1-aaaaaaaaaaaa"


@pytest.mark.parametrize("suffix", ("v6", "v9"))
def test_quantipy_execution_contract_rejects_arbitrary_version_suffixes(
    tmp_path: Path,
    suffix: str,
) -> None:
    with pytest.raises(
        AutoresearchValidationError,
        match="Quantipy execution contract run_id is invalid",
    ):
        autoresearch_evidence.build_quantipy_execution_contract(
            runtime_root=tmp_path / "quantipy-runtime",
            manifest_path=tmp_path / "worktrees" / "alpha" / "experiment-manifest.json",
            output_root=tmp_path / "runs",
            run_id=f"autoresearch-i1-aaaaaaaaaaaa-{suffix}",
        )


def test_canonical_runtime_attestation_allows_sibling_implementation_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    runtime_root = tmp_path / "runtime"
    workspace = tmp_path / "worktrees" / "alpha"
    workspace.parent.mkdir(mode=0o700)
    _git(tmp_path, "init", "--initial-branch=main", str(runtime_root))
    _git(runtime_root, "config", "user.email", "autoresearch@example.test")
    _git(runtime_root, "config", "user.name", "Autoresearch Test")
    (runtime_root / "pyproject.toml").write_text("[project]\nname='quantipy'\n", encoding="utf-8")
    (runtime_root / "uv.lock").write_bytes(b"# lock\n" + (b"x" * 385_043))
    (runtime_root / "pyproject.toml").chmod(0o664)
    (runtime_root / "uv.lock").chmod(0o664)
    _git(runtime_root, "add", "pyproject.toml", "uv.lock")
    _git(runtime_root, "commit", "-m", "readiness runtime")
    readiness_base = _git(runtime_root, "rev-parse", "HEAD")
    _git(runtime_root, "worktree", "add", "-b", "alpha", str(workspace))
    (workspace / "experiment.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "add", "experiment.py")
    _git(workspace, "commit", "-m", "immutable experiment")
    implementation_commit = _git(workspace, "rev-parse", "HEAD")
    entrypoint = runtime_root / ".venv" / "bin" / "quantipy"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.parent.parent.chmod(0o775)
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    entrypoint.chmod(0o775)
    (runtime_root / "src" / "quantipy").mkdir(parents=True)
    import_path = runtime_root / "src" / "quantipy" / "__init__.py"
    import_path.write_text("", encoding="utf-8")
    base_interpreter = tmp_path / "uv-managed-python"
    base_interpreter.write_bytes(b"external uv interpreter")
    base_interpreter.chmod(0o775)
    monkeypatch.setattr(
        runtime_attestation,
        "_probe_quantipy_runtime_resolution",
        lambda _root: (base_interpreter, import_path, "3.13.0"),
    )
    state = AutoresearchState(
        setup=_workspace_setup(runtime_root),
        platform_readiness=ReadinessIdentity(
            manifest_id="manifest-test",
            snapshot_id="snapshot-test",
            receipt_sha256="a" * 64,
            quantipy_commit=readiness_base,
        ),
    )
    implementation = replace(
        _implementation_result(),
        workspace_path=str(workspace),
        commit_sha=implementation_commit,
    )

    # Act
    attestation = autoresearch_attestation._attest_canonical_quantipy_runtime(state, implementation)

    # Assert
    assert attestation.root == str(runtime_root)
    assert attestation.readiness_quantipy_commit == readiness_base
    assert attestation.venv_prefix == str(runtime_root / ".venv")
    assert attestation.executable_sha256 == sha256(entrypoint.read_bytes()).hexdigest()
    assert attestation.executable_size_bytes == entrypoint.stat().st_size
    assert attestation.executable_mode == 0o775
    assert attestation.executable_owner_uid == os.getuid()
    assert attestation.base_interpreter_size_bytes == base_interpreter.stat().st_size
    assert attestation.base_interpreter_mode == 0o775
    assert attestation.base_interpreter_owner_uid == os.getuid()
    assert (
        autoresearch_recovery_receipts.CanonicalQuantipyRuntimeAttestation.from_dict(
            attestation.to_dict()
        )
        == attestation
    )
    wrong_owner = attestation.to_dict()
    wrong_owner["executable_owner_uid"] = os.getuid() + 1
    with pytest.raises(AutoresearchValidationError, match="owner UID"):
        autoresearch_recovery_receipts.CanonicalQuantipyRuntimeAttestation.from_dict(wrong_owner)

    entrypoint.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    reattested = autoresearch_attestation._attest_canonical_quantipy_runtime(state, implementation)

    assert reattested != attestation


def test_canonical_runtime_cli_rejects_a_world_writable_entrypoint(tmp_path: Path) -> None:
    # Arrange
    entrypoint = tmp_path / "quantipy"
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    entrypoint.chmod(0o777)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must not be world-writable"):
        autoresearch_secure_io._secure_open_snapshot(
            entrypoint,
            label="canonical Quantipy runtime .venv quantipy entrypoint",
            allow_group_write=True,
        )


def test_external_uv_base_attestation_accepts_installed_owner_mode_0775() -> None:
    # Arrange
    runtime_root = Path("/home/dev/repos/quantipy")
    base_interpreter, _, version = autoresearch_attestation._probe_quantipy_runtime_resolution(
        runtime_root
    )

    # Act
    snapshot = autoresearch_secure_io._secure_open_external_uv_base_interpreter(base_interpreter)

    # Assert
    assert snapshot.path == base_interpreter
    assert snapshot.mode == 0o775
    assert snapshot.owner_uid == os.getuid()
    assert len(snapshot.content) == base_interpreter.stat().st_size
    assert snapshot.sha256 == sha256(base_interpreter.read_bytes()).hexdigest()
    assert (
        version
        == subprocess.check_output(
            (
                str(base_interpreter),
                "-c",
                "import sys; print('.'.join(map(str, sys.version_info[:3])))",
            ),
            text=True,
        ).strip()
    )


def test_external_uv_base_interpreter_attestation_rejects_a_foreign_owner() -> None:
    # Arrange
    foreign_binary = Path("/usr/bin/env")

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must be owned by the autoresearch user"):
        autoresearch_secure_io._secure_open_external_uv_base_interpreter(foreign_binary)


def test_external_uv_base_interpreter_attestation_rejects_a_world_writable_file(
    tmp_path: Path,
) -> None:
    # Arrange
    base_interpreter = tmp_path / "uv-python"
    base_interpreter.write_bytes(b"external uv interpreter")
    base_interpreter.chmod(0o777)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must not be world-writable"):
        autoresearch_secure_io._secure_open_external_uv_base_interpreter(base_interpreter)


def test_platform_runtime_recovery_state_recheck_rejects_a_write_race(tmp_path: Path) -> None:
    # Arrange
    state_path = tmp_path / "quantipy-state.json"
    expected = AutoresearchState()
    state_path.write_text(json.dumps(expected.to_dict()), encoding="utf-8")
    changed = replace(expected, verification_fix_attempts=1)
    state_path.write_text(json.dumps(changed.to_dict()), encoding="utf-8")

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="state changed before publication"):
        autoresearch_operator_recovery._require_unchanged_platform_runtime_recovery_state(
            state_path,
            expected,
        )


def test_canonical_verification_dispatch_requires_a_sealed_runtime_attestation(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    # Arrange
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state_path = tmp_path / "verification-state.json"
    save_state_file(state_path, state)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="runtime attestation"):
        autoresearch_attestation.require_canonical_verification_dispatch_attestation(
            state_path,
            policy=policy,
            validation_context=AutoresearchValidationContext.from_readiness(platform_readiness),
        )


def test_verification_result_publication_rejects_an_unsealed_runtime(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    # Arrange
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state_path = tmp_path / "verification-state.json"
    artifact_path = tmp_path / "verification-result.json"
    save_state_file(state_path, state)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="runtime attestation"):
        autoresearch_persistence.advance_artifact_state_file(
            state_path=state_path,
            output_path=state_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256="a" * 64,
            state_reference_sha256="b" * 64,
            policy=policy,
            validation_context=AutoresearchValidationContext.from_readiness(platform_readiness),
        )


def test_platform_v5_recovery_rejects_a_partial_expected_artifact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    artifact_root = tmp_path / "quantipy-runs"
    detached_root = tmp_path / "detached-runs"
    artifact_root.mkdir(mode=0o700)
    detached_root.mkdir(mode=0o700)
    run_id = "autoresearch-i1-aaaaaaaaaaaa-v5"
    (artifact_root / run_id).mkdir(mode=0o700)
    monkeypatch.setattr(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", artifact_root)
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="artifact directory to be absent"):
        autoresearch_operator_recovery._require_absent_platform_v5_identity(
            run_id=run_id,
            iteration=1,
            implementation_commit="a" * 40,
        )


def test_platform_v5_recovery_rejects_alternate_detached_manifest_for_same_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    artifact_root = tmp_path / "quantipy-runs"
    detached_root = tmp_path / "detached-runs"
    artifact_root.mkdir(mode=0o700)
    detached_root.mkdir(mode=0o700)
    run_id = "autoresearch-i1-aaaaaaaaaaaa-v5"
    alternate_dir = detached_root / "alternate-name"
    alternate_dir.mkdir(mode=0o700)
    manifest = autoresearch_runs.RunManifest(
        schema_version=1,
        iteration=1,
        phase=Phase.VERIFICATION,
        attempt=5,
        task_label="alternate-task-label",
        state_reference_sha256="b" * 64,
        instruction_manifest_sha256="c" * 64,
        run_directory=str(alternate_dir),
        working_directory=str(tmp_path),
        command_sha256="d" * 64,
        expected_artifact_path=str(artifact_root / run_id / "run.json"),
        timeout_seconds=None,
    )
    manifest_path = alternate_dir / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    manifest_path.chmod(0o400)
    alternate_dir.chmod(0o500)
    monkeypatch.setattr(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", artifact_root)
    monkeypatch.setattr(autoresearch_runs, "DEFAULT_AUTORESEARCH_RUNS_ROOT", detached_root)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="duplicate detached v5 identity"):
        autoresearch_operator_recovery._require_absent_platform_v5_identity(
            run_id=run_id,
            iteration=1,
            implementation_commit="a" * 40,
        )
