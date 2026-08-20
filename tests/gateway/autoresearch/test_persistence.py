from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import (
    Barrier,
    Event,
    Thread,
)
from typing import cast

import gateway.autoresearch.constants as autoresearch_constants
import gateway.autoresearch.operator_recovery as autoresearch_operator_recovery
import gateway.autoresearch.persistence as autoresearch_persistence
import gateway.autoresearch.transitions as autoresearch_transitions
import pytest
from gateway.autoresearch import constants
from gateway.autoresearch import manifest_runtime as manifest_runtime_module
from gateway.autoresearch import persistence as persistence_module
from gateway.autoresearch import transitions as transitions_module
from gateway.autoresearch.artifacts import (
    FinalDecisionArtifact,
    QuantipyExperimentEvidence,
    SetupContextArtifact,
)
from gateway.autoresearch.constants import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
)
from gateway.autoresearch.enums import (
    FinalDecision,
    FinalReviewerVerdict,
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
from gateway.autoresearch.persistence import (
    persist_derived_state,
    save_state_file,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy,
    ReceiptCatalog,
)
from gateway.autoresearch.state import (
    AutoresearchState,
    AutoresearchValidationContext,
)
from gateway.autoresearch_readiness import PlatformReadinessManifest

from tests.gateway.autoresearch.builders import (
    PublicPlatformRecoveryFixture,
    StateArtifact,
    _majority_consensus,
    _rewrite_test_detached_status,
    _setup_artifact,
    _state_to_consensus,
    _write_public_v5_verification_artifact,
)


@pytest.mark.parametrize(
    ("status", "expected_phase"),
    (
        (VerificationStatus.PASS, Phase.REVIEW),
        (VerificationStatus.TEST_FAILURE, Phase.FIX_TEST),
        (VerificationStatus.BUG_SIGNAL, Phase.FIX_TEST),
    ),
)
def test_public_v5_artifact_advancement_routes_and_consumes_runtime_receipts(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
    status: VerificationStatus,
    expected_phase: Phase,
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=status,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    artifact_payload = cast(
        dict[str, object],
        json.loads(artifact_path.read_text(encoding="utf-8")),
    )
    artifact_body = cast(dict[str, object], artifact_payload["artifact"])
    evidence_body = cast(
        dict[str, object],
        artifact_body["quantipy_experiment_evidence"],
    )
    immutable_v5_paths = tuple(
        path
        for root in (
            Path(cast(str, evidence_body["run_json_path"])).parent,
            Path(cast(str, evidence_body["detached_run_directory"])),
        )
        for path in sorted(path for path in root.rglob("*") if path.is_file())
    )
    immutable_v5_hashes = tuple(
        (path, sha256(path.read_bytes()).hexdigest()) for path in immutable_v5_paths
    )

    # Act
    advanced = autoresearch_persistence.advance_artifact_state_file(
        state_path=fixture.copied_state_path,
        output_path=fixture.copied_state_path,
        artifact_path=artifact_path,
        instruction_manifest_sha256=action.source_manifest_sha256,
        state_reference_sha256=action.state_reference_sha256,
        policy=policy,
        validation_context=fixture.validation_context,
    )

    # Assert
    assert advanced.phase is expected_phase
    assert advanced.external_verification_retry_receipt is None
    assert advanced.interrupted_verification_history == ()
    assert advanced.platform_runtime_recovery_receipt is None
    assert advanced.canonical_quantipy_runtime_attestation is None
    assert autoresearch_persistence.load_state_file(fixture.copied_state_path) == advanced
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes
    assert (
        tuple((path, sha256(path.read_bytes()).hexdigest()) for path, _ in immutable_v5_hashes)
        == immutable_v5_hashes
    )


def test_public_v5_successful_bug_signal_advances_and_consumes_runtime_receipts(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=VerificationStatus.BUG_SIGNAL,
        successful_bug_signal=True,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    artifact_payload = cast(
        dict[str, object],
        json.loads(artifact_path.read_text(encoding="utf-8")),
    )
    artifact_body = cast(dict[str, object], artifact_payload["artifact"])
    evidence_body = cast(
        dict[str, object],
        artifact_body["quantipy_experiment_evidence"],
    )
    panel_directory = Path(cast(str, evidence_body["run_json_path"])).parent / "panel"
    assert stat.S_IMODE(panel_directory.stat().st_mode) == 0o500

    # Act
    advanced = autoresearch_persistence.advance_artifact_state_file(
        state_path=fixture.copied_state_path,
        output_path=fixture.copied_state_path,
        artifact_path=artifact_path,
        instruction_manifest_sha256=action.source_manifest_sha256,
        state_reference_sha256=action.state_reference_sha256,
        policy=policy,
        validation_context=fixture.validation_context,
    )

    # Assert
    assert advanced.phase is Phase.FIX_TEST
    assert advanced.pending_fix_trigger is FixTriggerPhase.VERIFICATION
    assert advanced.latest_verification is not None
    assert advanced.latest_verification.status is VerificationStatus.BUG_SIGNAL
    assert advanced.latest_verification.quantipy_experiment_evidence is not None
    assert advanced.latest_verification.quantipy_experiment_evidence.success is True
    assert advanced.latest_verification.is_walk_forward_sharpe_net is None
    assert advanced.latest_verification.data_coverage is None
    assert advanced.external_verification_retry_receipt is None
    assert len(advanced.verification_history) == 4
    assert advanced.platform_runtime_recovery_receipt is None
    assert advanced.canonical_quantipy_runtime_attestation is None
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes


@pytest.mark.parametrize("mode", (0o700, 0o550, 0o777))
def test_public_v5_artifact_advancement_rejects_unsealed_panel_directory_modes(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
    mode: int,
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=VerificationStatus.PASS,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    payload = cast(dict[str, object], json.loads(artifact_path.read_text(encoding="utf-8")))
    artifact = cast(dict[str, object], payload["artifact"])
    evidence = cast(dict[str, object], artifact["quantipy_experiment_evidence"])
    Path(cast(str, evidence["run_json_path"])).parent.joinpath("panel").chmod(mode)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="Quantipy panel directory"):
        autoresearch_persistence.advance_artifact_state_file(
            state_path=fixture.copied_state_path,
            output_path=fixture.copied_state_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256=action.source_manifest_sha256,
            state_reference_sha256=action.state_reference_sha256,
            policy=policy,
            validation_context=fixture.validation_context,
        )


def test_public_v5_artifact_advancement_rejects_symlinked_panel_directory(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=VerificationStatus.PASS,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    payload = cast(dict[str, object], json.loads(artifact_path.read_text(encoding="utf-8")))
    artifact = cast(dict[str, object], payload["artifact"])
    evidence = cast(dict[str, object], artifact["quantipy_experiment_evidence"])
    panel_directory = Path(cast(str, evidence["run_json_path"])).parent / "panel"
    panel_directory.chmod(0o700)
    sealed_directory = panel_directory.with_name("sealed-panel")
    panel_directory.rename(sealed_directory)
    panel_directory.symlink_to(sealed_directory, target_is_directory=True)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="Quantipy panel"):
        autoresearch_persistence.advance_artifact_state_file(
            state_path=fixture.copied_state_path,
            output_path=fixture.copied_state_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256=action.source_manifest_sha256,
            state_reference_sha256=action.state_reference_sha256,
            policy=policy,
            validation_context=fixture.validation_context,
        )


@pytest.mark.parametrize("relative_path", ("panel/panel.parquet", "panel/receipt.json"))
def test_public_v5_artifact_advancement_rejects_unsealed_panel_file_modes(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
    relative_path: str,
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=VerificationStatus.PASS,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    payload = cast(dict[str, object], json.loads(artifact_path.read_text(encoding="utf-8")))
    artifact = cast(dict[str, object], payload["artifact"])
    evidence = cast(dict[str, object], artifact["quantipy_experiment_evidence"])
    run_directory = Path(cast(str, evidence["run_json_path"])).parent
    (run_directory / relative_path).chmod(0o600)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="mode-0400 sealed file"):
        autoresearch_persistence.advance_artifact_state_file(
            state_path=fixture.copied_state_path,
            output_path=fixture.copied_state_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256=action.source_manifest_sha256,
            state_reference_sha256=action.state_reference_sha256,
            policy=policy,
            validation_context=fixture.validation_context,
        )


@pytest.mark.parametrize("race", ("runtime", "source", "status", "run", "state"))
def test_public_v5_artifact_advancement_rejects_publication_races(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=VerificationStatus.PASS,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    payload = cast(dict[str, object], json.loads(artifact_path.read_text(encoding="utf-8")))
    artifact = cast(dict[str, object], payload["artifact"])
    evidence = cast(dict[str, object], artifact["quantipy_experiment_evidence"])
    if race == "runtime":
        runtime = recovered.canonical_quantipy_runtime_attestation
        assert runtime is not None
        entrypoint = Path(runtime.executable_path)
        entrypoint.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        entrypoint.chmod(0o775)
    elif race == "source":
        implementation = recovered.implementation_result
        assert implementation is not None
        Path(implementation.experiment_manifest_path).write_text("{}\n", encoding="utf-8")
    elif race == "status":
        _rewrite_test_detached_status(
            QuantipyExperimentEvidence.from_dict(evidence),
            exit_code=1,
        )
    elif race == "run":
        run_path = Path(cast(str, evidence["run_json_path"]))
        run_path.chmod(0o600)
        run_path.write_bytes(run_path.read_bytes() + b"\n")
    else:
        save_state_file(
            fixture.copied_state_path,
            replace(recovered, verification_fix_attempts=1),
        )

    # Act / Assert
    with pytest.raises(AutoresearchValidationError):
        autoresearch_persistence.advance_artifact_state_file(
            state_path=fixture.copied_state_path,
            output_path=fixture.copied_state_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256=action.source_manifest_sha256,
            state_reference_sha256=action.state_reference_sha256,
            policy=policy,
            validation_context=fixture.validation_context,
        )
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes


@pytest.mark.parametrize(
    "race",
    ("runtime", "source", "status", "run", "state", "artifact"),
)
def test_public_v5_artifact_advancement_rejects_races_at_atomic_publication(
    public_platform_v4_recovery_fixture: PublicPlatformRecoveryFixture,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
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
    artifact_path, action = _write_public_v5_verification_artifact(
        fixture=fixture,
        recovered=recovered,
        status=VerificationStatus.PASS,
        policy=policy,
        receipts=receipts,
        tmp_path=tmp_path,
    )
    output_path = tmp_path / "advanced-state.json"
    payload = cast(dict[str, object], json.loads(artifact_path.read_text(encoding="utf-8")))
    artifact = cast(dict[str, object], payload["artifact"])
    evidence = cast(dict[str, object], artifact["quantipy_experiment_evidence"])
    original_atomic_save = persistence_module._atomic_save_state_file
    mutation_applied = False

    def mutate_at_atomic_publication(
        path: Path,
        state: AutoresearchState,
        *,
        publication_guard: autoresearch_persistence._ArtifactAdvancePublicationGuard | None = None,
    ) -> None:
        nonlocal mutation_applied
        assert path == output_path
        assert not mutation_applied
        assert publication_guard is not None
        mutation_applied = True
        if race == "runtime":
            runtime = recovered.canonical_quantipy_runtime_attestation
            assert runtime is not None
            entrypoint = Path(runtime.executable_path)
            entrypoint.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            entrypoint.chmod(0o775)
        elif race == "source":
            implementation = recovered.implementation_result
            assert implementation is not None
            Path(implementation.experiment_manifest_path).write_text("{}\n", encoding="utf-8")
        elif race == "status":
            _rewrite_test_detached_status(
                QuantipyExperimentEvidence.from_dict(evidence),
                exit_code=1,
            )
        elif race == "run":
            run_path = Path(cast(str, evidence["run_json_path"]))
            run_path.chmod(0o600)
            run_path.write_bytes(run_path.read_bytes() + b"\n")
        elif race == "state":
            fixture.copied_state_path.write_text(
                json.dumps(replace(recovered, verification_fix_attempts=1).to_dict()),
                encoding="utf-8",
            )
        else:
            payload["instruction_manifest_sha256"] = "0" * 64
            artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        original_atomic_save(
            path,
            state,
            publication_guard=publication_guard,
        )

    monkeypatch.setattr(
        persistence_module,
        "_atomic_save_state_file",
        mutate_at_atomic_publication,
    )

    with pytest.raises(AutoresearchValidationError):
        autoresearch_persistence.advance_artifact_state_file(
            state_path=fixture.copied_state_path,
            output_path=output_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256=action.source_manifest_sha256,
            state_reference_sha256=action.state_reference_sha256,
            policy=policy,
            validation_context=fixture.validation_context,
        )

    assert mutation_applied
    assert not output_path.exists()
    assert fixture.live_state_path.read_bytes() == fixture.live_state_bytes


def test_persist_derived_state_rejects_source_mutated_immediately_before_publication(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    source_path = tmp_path / "source-state.json"
    output_path = tmp_path / "derived-state.json"
    source_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    changed_source_state = replace(source_state, iteration=2)
    derived_state = replace(source_state, iteration=3)
    source_path.write_text(json.dumps(source_state.to_dict()), encoding="utf-8")
    failures: list[AutoresearchValidationError] = []

    def persist() -> None:
        try:
            persist_derived_state(source_path, output_path, source_state, derived_state)
        except AutoresearchValidationError as exc:
            failures.append(exc)

    with autoresearch_persistence._exclusive_state_lock(source_path):
        worker = Thread(target=persist)
        worker.start()
        source_path.write_text(json.dumps(changed_source_state.to_dict()), encoding="utf-8")

    worker.join()

    assert failures[0].args[0] == "persisted state does not match the supplied authoritative state"
    assert not output_path.exists()


def test_artifact_advance_reloads_the_artifact_under_the_publication_lock(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    output_path = tmp_path / "output.json"
    artifact_path = tmp_path / "artifact.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    instruction_digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=state_path,
    )
    state_reference = autoresearch_transitions.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()

    def write_artifact(artifact: SetupContextArtifact) -> None:
        artifact_path.write_text(
            json.dumps(
                {
                    "instruction_manifest_sha256": instruction_digest,
                    "state_reference_sha256": state_reference,
                    "artifact": artifact.to_dict(),
                }
            ),
            encoding="utf-8",
        )

    write_artifact(_setup_artifact())
    failures: list[AutoresearchValidationError] = []
    initial_derivation_complete = Event()
    allow_lock_acquisition = Event()
    original_advance = autoresearch_transitions.advance_state
    calls = 0

    def pause_after_initial_derivation(
        state: AutoresearchState,
        artifact: StateArtifact,
        advance_policy: AutoresearchPolicy,
        validation_context: AutoresearchValidationContext | None = None,
        *,
        state_path: Path | None = None,
    ) -> AutoresearchState:
        nonlocal calls
        result = original_advance(
            state,
            artifact,
            advance_policy,
            validation_context=validation_context,
            state_path=state_path,
        )
        calls += 1
        if calls == 1:
            initial_derivation_complete.set()
            assert allow_lock_acquisition.wait(timeout=2)
        return result

    monkeypatch.setattr(transitions_module, "advance_state", pause_after_initial_derivation)

    def advance() -> None:
        try:
            autoresearch_persistence.advance_artifact_state_file(
                state_path=state_path,
                output_path=output_path,
                artifact_path=artifact_path,
                instruction_manifest_sha256=instruction_digest,
                policy=policy,
                validation_context=None,
            )
        except AutoresearchValidationError as exc:
            failures.append(exc)

    # Act
    with autoresearch_persistence._exclusive_state_lock(state_path):
        worker = Thread(target=advance)
        worker.start()
        assert initial_derivation_complete.wait(timeout=2)
        write_artifact(replace(_setup_artifact(), goal="Find a different intraday alpha"))
        allow_lock_acquisition.set()
    worker.join()

    # Assert
    assert failures[0].args[0] == "artifact changed before state publication"
    assert not output_path.exists()


def test_stage_submission_inbox_validates_then_supervisor_advances(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    artifact_path = tmp_path / "artifact.json"
    inbox_path = tmp_path / "stage-inbox"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    instruction_digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=state_path,
    )
    state_reference = autoresearch_transitions.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": instruction_digest,
                "state_reference_sha256": state_reference,
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )

    submission_path = autoresearch_persistence.submit_stage_artifact_file(
        state_path=state_path,
        artifact_path=artifact_path,
        inbox_path=inbox_path,
        instruction_manifest_sha256=instruction_digest,
        policy=policy,
        validation_context=None,
    )

    assert submission_path.parent == inbox_path
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()
    monkeypatch.setattr(manifest_runtime_module, "build_receipt_catalog", lambda _: receipts)
    advanced = autoresearch_persistence.consume_stage_submission_inbox(
        state_path=state_path,
        output_path=state_path,
        inbox_path=inbox_path,
        openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
        quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
        validation_context=None,
    )
    assert advanced is not None
    assert advanced.setup == _setup_artifact()
    assert autoresearch_persistence.load_state_file(state_path).setup == _setup_artifact()
    assert not submission_path.exists()
    assert (inbox_path / "accepted" / submission_path.name).is_file()


def test_stage_submission_loads_implementation_infra_blocked_final_decision_envelope(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = autoresearch_transitions.advance_state(
        state,
        _majority_consensus(round_number=1, policy=policy),
        policy,
    )
    assert state.latest_consensus is not None
    state = replace(
        state,
        consensus_history=(
            replace(
                state.latest_consensus,
                implementation_brief=(
                    "The approved brief requires an ExperimentManifest transport contract "
                    "the platform does not provide."
                ),
            ),
        ),
    )
    state_path = tmp_path / "state.json"
    artifact_path = tmp_path / "artifact.json"
    inbox_path = tmp_path / "stage-inbox"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    instruction_digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=state_path,
    )
    state_reference = autoresearch_transitions.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    decision = FinalDecisionArtifact(
        experiment_id="iteration-1",
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name="runtime contract",
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
        rationale="The approved brief requires a runtime contract the platform does not provide.",
        log_summary="Blocked during implementation on a missing runtime contract.",
        continue_loop=True,
        memory_write_required=False,
        infra_rationale=(
            "OPERATOR-AUTHORIZED-INFRA-BLOCK: "
            "The approved brief requires an ExperimentManifest transport contract that the "
            "platform lacks."
        ),
    )
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": instruction_digest,
                "state_reference_sha256": state_reference,
                "artifact": decision.to_dict(),
            }
        ),
        encoding="utf-8",
    )

    submission_path = autoresearch_persistence.submit_stage_artifact_file(
        state_path=state_path,
        artifact_path=artifact_path,
        inbox_path=inbox_path,
        instruction_manifest_sha256=instruction_digest,
        policy=policy,
        validation_context=None,
    )
    monkeypatch.setattr(manifest_runtime_module, "build_receipt_catalog", lambda _: receipts)
    advanced = autoresearch_persistence.consume_stage_submission_inbox(
        state_path=state_path,
        output_path=state_path,
        inbox_path=inbox_path,
        openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
        quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
        validation_context=None,
    )

    assert advanced is not None
    assert advanced.final_decision == decision
    assert advanced.suspended is False
    assert advanced.suspension_reason is None
    assert "ExperimentManifest" in advanced.hypothesis_registry[-1].reason
    assert advanced.campaign_counters.iterations_since_last_keep == (
        state.campaign_counters.iterations_since_last_keep
    )
    assert autoresearch_persistence.load_state_file(state_path) == advanced
    assert not submission_path.exists()
    assert (inbox_path / "accepted" / submission_path.name).is_file()


def test_stage_submission_inbox_rejects_invalid_envelope_without_state_write(
    tmp_path: Path,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    inbox_path = tmp_path / "stage-inbox"
    inbox_path.mkdir()
    inbox_path.chmod(0o700)
    bad_submission = inbox_path / "bad.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    bad_submission.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": "0" * 64,
                "state_reference_sha256": "1" * 64,
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )

    advanced = autoresearch_persistence.consume_stage_submission_inbox(
        state_path=state_path,
        output_path=state_path,
        inbox_path=inbox_path,
        openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
        quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
        validation_context=None,
    )

    assert advanced is None
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()
    assert not bad_submission.exists()
    assert (inbox_path / "rejected" / bad_submission.name).is_file()


def test_stage_submission_inbox_rejects_symlinked_root_without_outside_write(
    tmp_path: Path,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    outside = tmp_path / "outside"
    inbox_path = tmp_path / "stage-inbox"
    outside.mkdir()
    inbox_path.symlink_to(outside, target_is_directory=True)
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    protected = outside / "bad.json"
    protected.write_text("do-not-touch\n", encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="cannot open stage submission inbox"):
        autoresearch_persistence.consume_stage_submission_inbox(
            state_path=state_path,
            output_path=state_path,
            inbox_path=inbox_path,
            openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
            quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
            validation_context=None,
        )

    assert protected.read_text(encoding="utf-8") == "do-not-touch\n"
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()


def test_stage_submission_inbox_rejects_symlinked_rejected_directory_without_overwrite(
    tmp_path: Path,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    inbox_path = tmp_path / "stage-inbox"
    outside = tmp_path / "outside"
    inbox_path.mkdir()
    inbox_path.chmod(0o700)
    outside.mkdir()
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    bad_submission = inbox_path / "bad.json"
    bad_submission.write_text("{}", encoding="utf-8")
    (inbox_path / "rejected").symlink_to(outside, target_is_directory=True)
    protected = outside / "bad.json"
    protected.write_text("do-not-overwrite\n", encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="rejected path must be a plain"):
        autoresearch_persistence.consume_stage_submission_inbox(
            state_path=state_path,
            output_path=state_path,
            inbox_path=inbox_path,
            openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
            quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
            validation_context=None,
        )

    assert protected.read_text(encoding="utf-8") == "do-not-overwrite\n"
    assert bad_submission.is_file()
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()


def test_stage_submission_inbox_rejects_symlinked_accepted_directory_without_overwrite(
    tmp_path: Path,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    inbox_path = tmp_path / "stage-inbox"
    outside = tmp_path / "outside"
    inbox_path.mkdir()
    inbox_path.chmod(0o700)
    outside.mkdir()
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    bad_submission = inbox_path / "bad.json"
    bad_submission.write_text("{}", encoding="utf-8")
    (inbox_path / "accepted").symlink_to(outside, target_is_directory=True)
    protected = outside / "bad.json"
    protected.write_text("do-not-overwrite\n", encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="accepted path must be a plain"):
        autoresearch_persistence.consume_stage_submission_inbox(
            state_path=state_path,
            output_path=state_path,
            inbox_path=inbox_path,
            openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
            quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
            validation_context=None,
        )

    assert protected.read_text(encoding="utf-8") == "do-not-overwrite\n"
    assert bad_submission.is_file()
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()


def test_stage_submission_inbox_rejects_hardlinked_submission_without_state_write(
    tmp_path: Path,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    inbox_path = tmp_path / "stage-inbox"
    inbox_path.mkdir()
    inbox_path.chmod(0o700)
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    hardlink = inbox_path / "hardlink.json"
    os.link(source, hardlink)

    advanced = autoresearch_persistence.consume_stage_submission_inbox(
        state_path=state_path,
        output_path=state_path,
        inbox_path=inbox_path,
        openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
        quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
        validation_context=None,
    )

    assert advanced is None
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()
    assert not hardlink.exists()
    assert (inbox_path / "rejected" / "hardlink.json").is_file()


def test_stage_submission_inbox_duplicate_rejection_destination_gets_unique_name(
    tmp_path: Path,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    inbox_path = tmp_path / "stage-inbox"
    rejected_path = inbox_path / "rejected"
    inbox_path.mkdir()
    inbox_path.chmod(0o700)
    rejected_path.mkdir()
    rejected_path.chmod(0o700)
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    bad_submission = inbox_path / "bad.json"
    bad_submission.write_text("{}", encoding="utf-8")
    stale_rejected = rejected_path / "bad.json"
    stale_rejected.write_text("do-not-overwrite\n", encoding="utf-8")

    advanced = autoresearch_persistence.consume_stage_submission_inbox(
        state_path=state_path,
        output_path=state_path,
        inbox_path=inbox_path,
        openclaw_config=DEFAULT_OPENCLAW_CONFIG_PATH,
        quantipy_root=autoresearch_constants.DEFAULT_QUANTIPY_ROOT,
        validation_context=None,
    )

    assert advanced is None
    assert stale_rejected.read_text(encoding="utf-8") == "do-not-overwrite\n"
    assert not bad_submission.exists()
    rejected_names = sorted(path.name for path in rejected_path.iterdir())
    assert rejected_names[0] == "bad.json"
    assert any(name.startswith("bad.json.") for name in rejected_names)
    assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()


def test_persist_derived_state_rejects_an_invalid_candidate_before_write(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    # Arrange
    source_path = tmp_path / "source-state.json"
    output_path = tmp_path / "derived-state.json"
    source_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    source_path.write_text(json.dumps(source_state.to_dict()), encoding="utf-8")

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="debate phase requires a context_packet"):
        persist_derived_state(
            source_path,
            output_path,
            source_state,
            replace(source_state, phase=Phase.DEBATE),
        )

    assert not output_path.exists()


def test_persist_derived_state_preserves_a_distinct_authorizing_source(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    source_path = tmp_path / "source-state.json"
    output_path = tmp_path / "derived-state.json"
    source_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    derived_state = replace(source_state, iteration=2)
    source_path.write_text(json.dumps(source_state.to_dict()), encoding="utf-8")

    persist_derived_state(source_path, output_path, source_state, derived_state)

    assert (
        AutoresearchState.from_dict(json.loads(source_path.read_text(encoding="utf-8"))),
        AutoresearchState.from_dict(json.loads(output_path.read_text(encoding="utf-8"))),
    ) == (source_state, derived_state)


def test_persist_derived_state_replaces_the_matching_authorizing_source(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state_path = tmp_path / "state.json"
    source_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    derived_state = replace(source_state, iteration=2)
    state_path.write_text(json.dumps(source_state.to_dict()), encoding="utf-8")

    persist_derived_state(state_path, state_path, source_state, derived_state)

    persisted_state = AutoresearchState.from_dict(
        json.loads(state_path.read_text(encoding="utf-8"))
    )

    assert persisted_state == derived_state


def test_canonical_state_lock_paths_are_sorted_and_deduplicated(tmp_path: Path) -> None:
    first_path = tmp_path / "a-state.json"
    second_path = tmp_path / "b-state.json"
    first_alias = tmp_path / "." / first_path.name

    canonical_paths = autoresearch_persistence._canonical_state_paths(
        (second_path, first_alias, first_path)
    )

    assert canonical_paths == (first_path.resolve(), second_path.resolve())


def test_state_lock_paths_hash_unique_canonical_paths_and_collapse_symlink_aliases(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first" / "state.json"
    second_path = tmp_path / "second" / "state.json"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.touch()
    second_path.touch()
    first_alias = tmp_path / "first-state-alias.json"
    first_alias.symlink_to(first_path)

    first_lock_path = autoresearch_persistence._state_lock_path(first_path)
    second_lock_path = autoresearch_persistence._state_lock_path(second_path)
    alias_lock_path = autoresearch_persistence._state_lock_path(first_alias)

    assert first_lock_path != second_lock_path
    assert alias_lock_path == first_lock_path


def test_lock_namespace_and_path_are_process_invariant_across_temp_environments(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    first_temp_root = tmp_path / "first-temp"
    second_temp_root = tmp_path / "second-temp"
    first_temp_root.mkdir()
    second_temp_root.mkdir()
    script = (
        "import json\n"
        "from pathlib import Path\n"
        "import gateway.autoresearch.constants as constants\n"
        "import gateway.autoresearch.persistence as persistence\n"
        f"state_path = Path({str(state_path)!r})\n"
        "print(json.dumps({"
        "'namespace': str(constants.AUTORESEARCH_LOCK_NAMESPACE), "
        "'lock_path': str(persistence._state_lock_path(state_path))"
        "}, sort_keys=True))\n"
    )

    outputs: list[str] = []
    for temp_root in (first_temp_root, second_temp_root):
        environment = {
            **os.environ,
            "TMPDIR": str(temp_root),
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
        }
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[3],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout.strip())

    expected_namespace = f"/tmp/g2-openclaw-autoresearch-locks-{os.getuid()}"
    payload = cast(dict[str, str], json.loads(outputs[0]))

    assert outputs[0] == outputs[1]
    assert payload["namespace"] == expected_namespace
    assert Path(payload["lock_path"]).parent == Path(expected_namespace)


def test_lock_namespace_and_lock_files_use_private_permissions(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    with autoresearch_persistence._exclusive_state_locks((state_path,)):
        lock_path = autoresearch_persistence._state_lock_path(state_path)
        namespace_path = lock_path.parent

        assert stat.S_IMODE(namespace_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("via_symlink", [False, True], ids=("direct", "symlink-alias"))
def test_state_output_inside_lock_namespace_fails_closed(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
    *,
    via_symlink: bool,
) -> None:
    source_path = tmp_path / "source.json"
    source_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    source_path.write_text(json.dumps(source_state.to_dict()), encoding="utf-8")
    namespace_path = autoresearch_persistence._prepare_lock_namespace()
    output_parent = namespace_path
    if via_symlink:
        output_parent = tmp_path / "lock-namespace-alias"
        output_parent.symlink_to(namespace_path, target_is_directory=True)
    output_path = output_parent / "forbidden-state-output.json"

    with pytest.raises(AutoresearchValidationError, match="lock namespace"):
        persist_derived_state(
            source_path,
            output_path,
            source_state,
            replace(source_state, iteration=2),
        )


def test_insecure_existing_lock_namespace_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_path = constants.AUTORESEARCH_LOCK_NAMESPACE
    monkeypatch.setattr(constants, "AUTORESEARCH_LOCK_NAMESPACE", namespace_path)
    namespace_path.mkdir(mode=0o755)
    namespace_path.chmod(0o755)

    with (
        pytest.raises(AutoresearchValidationError, match="permissions must be 0700"),
        autoresearch_persistence._exclusive_state_locks((tmp_path / "state.json",)),
    ):
        pass


def test_symlink_lock_file_fails_with_validation_error(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    autoresearch_persistence._prepare_lock_namespace()
    lock_path = autoresearch_persistence._state_lock_path(state_path)
    symlink_target = tmp_path / "lock-target"
    symlink_target.touch(mode=0o600)
    lock_path.symlink_to(symlink_target)

    with (
        pytest.raises(
            AutoresearchValidationError,
            match="unable to open autoresearch state lock",
        ),
        autoresearch_persistence._exclusive_state_locks((state_path,)),
    ):
        pass


def test_old_adjacent_sidecar_output_cannot_break_concurrent_source_coordination(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "state.json"
    old_sidecar_output = tmp_path / ".state.json.lock"
    source_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    source_path.write_text(json.dumps(source_state.to_dict()), encoding="utf-8")
    first_published = Event()
    release_first = Event()
    second_completed = Event()
    original_atomic_save = persistence_module._atomic_save_state_file

    def pause_after_old_sidecar_publication(
        path: Path,
        state: AutoresearchState,
    ) -> None:
        original_atomic_save(path, state)
        if path == old_sidecar_output.resolve():
            first_published.set()
            release_first.wait(timeout=2)

    monkeypatch.setattr(
        persistence_module,
        "_atomic_save_state_file",
        pause_after_old_sidecar_publication,
    )

    first_worker = Thread(
        target=persist_derived_state,
        args=(
            source_path,
            old_sidecar_output,
            source_state,
            replace(source_state, iteration=2),
        ),
    )

    def persist_same_source() -> None:
        persist_derived_state(
            source_path,
            source_path,
            source_state,
            replace(source_state, iteration=3),
        )
        second_completed.set()

    first_worker.start()
    assert first_published.wait(timeout=2)
    second_worker = Thread(target=persist_same_source)
    second_worker.start()

    assert not second_completed.wait(timeout=0.1)
    release_first.set()
    first_worker.join(timeout=2)
    second_worker.join(timeout=2)

    assert second_completed.is_set()


def test_crossed_source_destination_writers_complete_without_abba_deadlock(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    first_path = tmp_path / "a-state.json"
    second_path = tmp_path / "b-state.json"
    first_state = AutoresearchState(platform_readiness=platform_readiness.identity())
    second_state = replace(first_state, iteration=2)
    first_path.write_text(json.dumps(first_state.to_dict()), encoding="utf-8")
    second_path.write_text(json.dumps(second_state.to_dict()), encoding="utf-8")
    start = Barrier(3)
    successes: list[str] = []
    failures: list[AutoresearchValidationError] = []

    def persist_crossed(
        label: str,
        source_path: Path,
        output_path: Path,
        source_state: AutoresearchState,
        derived_state: AutoresearchState,
    ) -> None:
        start.wait()
        try:
            persist_derived_state(source_path, output_path, source_state, derived_state)
            successes.append(label)
        except AutoresearchValidationError as exc:
            failures.append(exc)

    first_worker = Thread(
        target=persist_crossed,
        args=("first", first_path, second_path, first_state, replace(first_state, iteration=3)),
    )
    second_worker = Thread(
        target=persist_crossed,
        args=(
            "second",
            second_path,
            first_path,
            second_state,
            replace(second_state, iteration=4),
        ),
    )
    first_worker.start()
    second_worker.start()

    start.wait()
    first_worker.join(timeout=2)
    second_worker.join(timeout=2)

    assert not first_worker.is_alive() and not second_worker.is_alive()
    assert len(successes) == 1
    assert len(failures) == 1


def test_save_state_file_waits_for_an_active_destination_writer(
    tmp_path: Path,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    output_path = tmp_path / "initialized-state.json"
    state = AutoresearchState(platform_readiness=platform_readiness.identity())
    completed = Event()

    def save() -> None:
        save_state_file(output_path, state)
        completed.set()

    with autoresearch_persistence._exclusive_state_locks((output_path,)):
        worker = Thread(target=save)
        worker.start()

        assert not completed.wait(timeout=0.1)

    worker.join(timeout=2)

    assert completed.is_set()
