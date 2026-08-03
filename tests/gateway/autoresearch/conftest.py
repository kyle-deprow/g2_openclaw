from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import gateway.autoresearch_runner as autoresearch_runner
import gateway.autoresearch_runs as autoresearch_runs
import pytest
from gateway.autoresearch import constants
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest,
    ReadinessIdentity,
    ResearchPanelProbeReceipt,
)
from gateway.autoresearch_runner import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    QUANTIPY_RECEIPT_PATHS,
    AutoresearchPolicy,
    AutoresearchState,
    AutoresearchValidationContext,
    ExternalVerificationRetryReceipt,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    FixTriggerPhase,
    InfraGateOutcome,
    MemoryVerificationReceipt,
    Phase,
    QuantipyExperimentEvidence,
    QuantipyExperimentFailureEvidence,
    ReceiptCatalog,
    ResearchMode,
    ReviewVerdict,
    VerificationStatus,
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
    load_autoresearch_policy,
    mark_memory_written,
    retry_external_verification,
    retry_external_verification_state_file,
    save_state_file,
)
from gateway.autoresearch_runner import advance_state as _runner_advance_state

from tests.gateway.autoresearch.builders import (
    GitWorktree,
    PublicPlatformRecoveryFixture,
    _context_artifact,
    _debate_result,
    _final_decision,
    _fix_result,
    _g0_remediation_verification,
    _git,
    _implementation_result,
    _majority_consensus,
    _no_consensus,
    _prepare_real_canonical_runtime,
    _ready_manifest,
    _review_result,
    _runtime_verification_context,
    _runtime_verification_state,
    _setup_artifact,
    _state_to_consensus,
    _state_to_decision,
    _state_to_g0_decision,
    _verification_result,
    _write_quantipy_detached_run_record,
    advance_state,
)


@pytest.fixture()
def policy() -> AutoresearchPolicy:
    return load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)


@pytest.fixture(autouse=True)
def isolated_autoresearch_lock_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autoresearch_runner,
        "AUTORESEARCH_LOCK_NAMESPACE",
        tmp_path / "autoresearch-locks",
        raising=False,
    )


@pytest.fixture()
def quantipy_root(tmp_path: Path) -> Path:
    for relative_path in QUANTIPY_RECEIPT_PATHS.values():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixture for {relative_path}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def receipts(quantipy_root: Path) -> ReceiptCatalog:
    return build_receipt_catalog(quantipy_root)


@pytest.fixture()
def platform_readiness(tmp_path: Path) -> PlatformReadinessManifest:
    return _ready_manifest(tmp_path / "platform-readiness")


@pytest.fixture()
def completed_memory_written_state(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    state = advance_state(_state_to_decision(policy, platform_readiness), _final_decision(), policy)
    return mark_memory_written(
        state,
        MemoryVerificationReceipt(
            experiment_id="iteration-1",
            kg_path="/tmp/knowledge_graph.sqlite3",
            predicates=("decision",),
            verified_rows_digest="0" * 64,
        ),
    )


@pytest.fixture()
def g0_verification_state(policy: AutoresearchPolicy) -> AutoresearchState:
    state = advance_state(AutoresearchState(), _setup_artifact(), policy)
    state = advance_state(
        state,
        replace(
            _context_artifact(),
            research_mode=ResearchMode.DATA_INFRA_G0,
            mode_rationale="Repair cap and source provenance before an alpha rerun.",
        ),
        policy,
    )
    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    return advance_state(state, _implementation_result(), policy)


@pytest.fixture()
def suspended_g0_remediation_state(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    state = _state_to_g0_decision(
        policy,
        readiness=platform_readiness,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
    )
    decision = FinalDecisionArtifact(
        experiment_id="g0-iteration-1",
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name="coverage gate",
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.PASS,
        rationale="Data infrastructure remains blocked.",
        log_summary="G0 gate still requires remediation.",
        continue_loop=True,
        memory_write_required=False,
        infra_rationale="Cap/source provenance still needs operator remediation.",
    )
    return replace(
        state,
        final_decision=decision,
        phase=Phase.REPEAT,
        suspended=True,
        suspension_reason=decision.infra_rationale,
    )


@pytest.fixture(
    params=(
        (VerificationStatus.TEST_FAILURE, FinalDecision.CRASH),
        (VerificationStatus.BUG_SIGNAL, FinalDecision.DISCARD),
    ),
    ids=("test-failure", "bug-signal"),
)
def exhausted_g0_verification(
    request: pytest.FixtureRequest,
    g0_verification_state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> tuple[AutoresearchState, FinalDecision]:
    verification_status, expected_decision = cast(
        tuple[VerificationStatus, FinalDecision], request.param
    )
    verification = _g0_remediation_verification(verification_status)
    state = g0_verification_state
    for _ in range(2):
        state = advance_state(state, verification, policy)
        state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    state = advance_state(state, verification, policy)
    return state, expected_decision


@pytest.fixture(
    params=(ResearchMode.ALPHA_RESEARCH, ResearchMode.DATA_INFRA_G0),
    ids=("alpha-research", "data-infra-g0"),
)
def no_consensus_state(
    request: pytest.FixtureRequest,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    mode = cast(ResearchMode, request.param)
    state = advance_state(
        AutoresearchState(platform_readiness=platform_readiness.identity()),
        _setup_artifact(),
        policy,
    )
    state = advance_state(
        state,
        replace(
            _context_artifact(),
            research_mode=mode,
            mode_rationale=f"Exercise the full {mode.value} no-consensus transition.",
        ),
        policy,
    )
    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    state = advance_state(state, _no_consensus(round_number=1), policy)
    state = advance_state(state, _debate_result(policy, round_number=2), policy)
    state = advance_state(state, _no_consensus(round_number=2), policy)

    return advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id=f"{mode.value.replace('_', '-')}-no-consensus-1",
            decision=FinalDecision.NO_CONSENSUS,
            recommended_metric_name="consensus outcome",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The retry produced no majority and no implementation was created.",
            log_summary="No consensus after the allowed retry.",
            continue_loop=True,
            memory_write_required=False,
        ),
        policy,
    )


@pytest.fixture()
def mempalace_kg_path(tmp_path: Path) -> Path:
    kg_path = tmp_path / "knowledge_graph.sqlite3"
    connection = sqlite3.connect(kg_path)
    connection.execute(
        """
        CREATE TABLE triples (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            object TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
            source_file TEXT, source_drawer_id TEXT
        )
        """
    )
    connection.close()
    return kg_path


@pytest.fixture()
def autoresearch_worktree_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    worktree_root = tmp_path / "operator-controlled" / "model-workspaces"
    monkeypatch.setattr(constants, "DEFAULT_AUTORESEARCH_WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(
        autoresearch_runner,
        "DEFAULT_AUTORESEARCH_WORKTREE_ROOT",
        worktree_root,
    )
    return worktree_root


@pytest.fixture()
def git_worktree(tmp_path: Path, autoresearch_worktree_root: Path) -> GitWorktree:
    target_checkout = tmp_path / "target"
    workspace = autoresearch_worktree_root / "workspace"
    autoresearch_worktree_root.mkdir(mode=0o700, parents=True)
    autoresearch_worktree_root.chmod(0o700)
    _git(tmp_path, "init", "--initial-branch=main", str(target_checkout))
    _git(target_checkout, "config", "user.email", "autoresearch@example.test")
    _git(target_checkout, "config", "user.name", "Autoresearch Test")
    (target_checkout / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(target_checkout, "add", "README.md")
    _git(target_checkout, "commit", "-m", "baseline")
    _git(tmp_path, "clone", str(target_checkout), str(workspace))
    _git(workspace, "config", "user.email", "autoresearch@example.test")
    _git(workspace, "config", "user.name", "Autoresearch Test")
    workspace.chmod(0o700)
    (workspace / ".git").chmod(0o700)
    (workspace / "experiment.txt").write_text("implementation\n", encoding="utf-8")
    _git(workspace, "add", "experiment.txt")
    _git(workspace, "commit", "-m", "implementation")
    implementation_commit = _git(workspace, "rev-parse", "HEAD")
    (workspace / "experiment.txt").write_text("fixed\n", encoding="utf-8")
    _git(workspace, "add", "experiment.txt")
    _git(workspace, "commit", "-m", "fix")
    final_commit = _git(workspace, "rev-parse", "HEAD")
    _git(
        target_checkout,
        "fetch",
        str(workspace),
        f"{implementation_commit}:refs/autoresearch-fixtures/{implementation_commit}",
    )
    _git(
        target_checkout,
        "fetch",
        str(workspace),
        f"{final_commit}:refs/autoresearch-fixtures/{final_commit}",
    )
    return GitWorktree(target_checkout, workspace, implementation_commit, final_commit)


@pytest.fixture()
def trusted_quantipy_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "trusted-quantipy-runs"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", root)
    monkeypatch.setattr(autoresearch_runner, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", root)
    detached_root = tmp_path / "trusted-detached-runs"
    monkeypatch.setattr(
        autoresearch_runs,
        "DEFAULT_AUTORESEARCH_RUNS_ROOT",
        detached_root,
    )
    return root


@pytest.fixture()
def public_platform_v4_recovery_fixture(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> PublicPlatformRecoveryFixture:
    readiness_commit = _prepare_real_canonical_runtime(git_worktree)
    readiness = _ready_manifest(
        tmp_path / "strict-platform-readiness",
        quantipy_commit=readiness_commit,
    )
    state, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    validation_context = AutoresearchValidationContext.from_readiness(readiness)
    current_identity = validation_context.readiness_identity
    assert current_identity is not None
    assert current_identity.quantipy_commit == readiness_commit
    historical_identity = ReadinessIdentity(
        manifest_id=current_identity.manifest_id,
        snapshot_id=current_identity.snapshot_id,
        receipt_sha256=current_identity.receipt_sha256,
    )
    historical_validation_context = replace(
        validation_context,
        readiness_identity=historical_identity,
    )
    state = replace(state, platform_readiness=historical_identity)
    manifest = json.loads(Path(evidence.manifest_path).read_text(encoding="utf-8"))
    panel = cast(dict[str, object], manifest["panel"])
    request = cast(dict[str, object], panel["request"])
    expected_url = "http://127.0.0.1:8000/price-data/research-panel?" + urlencode(
        (
            ("tickers", ",".join(cast(list[str], request["tickers"]))),
            ("start", "2026-07-28T12:00:00+00:00"),
            ("end", "2026-07-28T13:00:00+00:00"),
            ("timeframe", cast(str, request["timeframe"])),
            ("market_hours", cast(str, request["market_hours"])),
        )
    )
    initial = replace(
        _verification_result(VerificationStatus.TEST_FAILURE, external_panel_failure=True),
        data_coverage=None,
        platform_coverage_validation=None,
        universe_verification_receipt=None,
        price_hydration_receipt=None,
        quantipy_experiment_evidence=replace(
            evidence,
            success=False,
            completed_stages=(),
            terminal_stage=None,
            terminal_status=None,
            failure=QuantipyExperimentFailureEvidence(
                category="panel",
                message=(
                    "ExperimentPanelError: Client error '404 Not Found' for url "
                    f"'{expected_url}'\nFor more information check: "
                    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
                ),
            ),
            panel=None,
        ),
    )
    failed_initial = _runner_advance_state(
        state,
        initial,
        policy,
        validation_context=historical_validation_context,
    )
    probe = ResearchPanelProbeReceipt(
        endpoint="http://127.0.0.1:8000/price-data/research-panel",
        observed_at="2026-07-29T12:00:00Z",
        response_bytes=18,
        response_sha256="a" * 64,
        session_date="2022-01-03",
        symbol="AAPL",
    )
    v2_receipt = ExternalVerificationRetryReceipt.for_state(
        failed_initial,
        probe,
        "Raised the gateway receipt limit.",
    )
    v2_state = retry_external_verification(failed_initial, v2_receipt)
    v2_run_path = trusted_quantipy_runs_root / v2_receipt.expected_run_id / "run.json"
    run = json.loads(Path(evidence.run_json_path).read_text(encoding="utf-8"))
    run.update(
        run_id=v2_receipt.expected_run_id,
        success=False,
        panel_requested=True,
        panel=None,
        stage_receipts=[],
        failure={
            "category": "panel",
            "message": (
                "ExperimentPanelError: Client error '413 Request Entity Too Large' for url "
                f"'{expected_url}'\nFor more information check: "
                "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/413"
            ),
        },
    )
    v2_run_path.parent.mkdir(mode=0o700)
    v2_run_path.write_bytes(json.dumps(run, sort_keys=True, separators=(",", ":")).encode())
    v2_run_path.chmod(0o600)
    v2_detached_dir = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
        f"i{v2_state.iteration}-verification-r1-a2-{v2_receipt.implementation_commit[:12]}-v2"
    )
    _write_quantipy_detached_run_record(
        workspace=git_worktree.workspace,
        runtime_root=git_worktree.target_checkout,
        manifest_path=evidence.manifest_path,
        run_id=v2_receipt.expected_run_id,
        run_path=v2_run_path,
        detached_run_dir=v2_detached_dir,
    )
    live_state_path = tmp_path / "authoritative-live-v4.json"
    save_state_file(live_state_path, v2_state)
    v3_state = retry_external_verification_state_file(
        live_state_path,
        probe,
        operator_reason="Raised the gateway receipt limit again.",
        policy=policy,
        validation_context=historical_validation_context,
    )
    v3_receipt = v3_state.external_verification_retry_receipt
    implementation = v3_state.implementation_result
    assert v3_receipt is not None
    assert implementation is not None
    v3_run_path = trusted_quantipy_runs_root / v3_receipt.expected_run_id / "run.json"
    v3_run_dir = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
        f"i{v3_state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
    )
    v3_command = autoresearch_runner._legacy_quantipy_bash_command(
        implementation,
        run_id=v3_receipt.expected_run_id,
    )
    v3_manifest_path = tmp_path / "v3-detached-manifest.json"
    v3_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": v3_state.iteration,
                "phase": "verification",
                "attempt": 3,
                "task_label": (
                    f"autoresearch-i{v3_state.iteration}-verification-r1-a3-"
                    f"{implementation.commit_sha[:12]}-v3"
                ),
                "state_reference_sha256": (
                    autoresearch_runner.build_authoritative_state_reference(
                        v3_state,
                        state_path=live_state_path,
                    ).sha256()
                ),
                "instruction_manifest_sha256": expected_instruction_manifest_sha256(
                    v3_state,
                    policy,
                    receipts,
                    state_path=live_state_path,
                ),
                "run_directory": str(v3_run_dir),
                "working_directory": implementation.workspace_path,
                "command_sha256": autoresearch_runs.command_sha256(v3_command),
                "expected_artifact_path": str(v3_run_path),
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=v3_manifest_path,
        run_dir=v3_run_dir,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        command=v3_command,
    )
    autoresearch_runs.prepare_output_capture(
        run_dir=v3_run_dir,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    for stream in autoresearch_runs.RunOutputStream:
        autoresearch_runs.capture_output_stream(
            run_dir=v3_run_dir,
            runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            stream=stream,
            source=BytesIO(b""),
        )
    autoresearch_runs.start_run(
        run_dir=v3_run_dir,
        pid=999_999,
        systemd_unit="openclaw-long-task-1-1.service",
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    autoresearch_runs.complete_run(
        run_dir=v3_run_dir,
        exit_code=143,
        signal_number=None,
        peak_rss_bytes=None,
        failure_classification=autoresearch_runs.RunFailureClassification.OPERATOR_STOPPED,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    v4_state = autoresearch_runner.recover_interrupted_verification_state_file(
        live_state_path,
        operator_reason="Stopped the detached v3 process before it produced run.json.",
        policy=policy,
        receipts=receipts,
        validation_context=historical_validation_context,
        systemd_is_active=lambda _unit: False,
        proc_root=tmp_path / "proc",
    )
    v4_receipt = v4_state.external_verification_retry_receipt
    assert v4_receipt is not None
    v4_run_path = trusted_quantipy_runs_root / v4_receipt.expected_run_id / "run.json"
    run.update(
        run_id=v4_receipt.expected_run_id,
        failure={
            "category": "panel",
            "message": "ExperimentPanelError: Research panel receipt is invalid.",
        },
    )
    v4_run_path.parent.mkdir(mode=0o700)
    v4_run_path.write_bytes(json.dumps(run, sort_keys=True, separators=(",", ":")).encode())
    v4_run_path.chmod(0o600)
    v4_run_dir = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
        f"i{v4_state.iteration}-verification-r1-a4-{implementation.commit_sha[:12]}-v4"
    )
    v4_command = autoresearch_runner._legacy_quantipy_bash_command(
        implementation,
        run_id=v4_receipt.expected_run_id,
    )
    v4_manifest_path = tmp_path / "v4-detached-manifest.json"
    v4_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": v4_state.iteration,
                "phase": "verification",
                "attempt": 4,
                "task_label": (
                    f"autoresearch-i{v4_state.iteration}-verification-r1-a4-"
                    f"{implementation.commit_sha[:12]}-v4"
                ),
                "state_reference_sha256": (
                    autoresearch_runner.build_authoritative_state_reference(
                        v4_state,
                        state_path=live_state_path,
                    ).sha256()
                ),
                "instruction_manifest_sha256": expected_instruction_manifest_sha256(
                    v4_state,
                    policy,
                    receipts,
                    state_path=live_state_path,
                ),
                "run_directory": str(v4_run_dir),
                "working_directory": implementation.workspace_path,
                "command_sha256": autoresearch_runs.command_sha256(v4_command),
                "expected_artifact_path": str(v4_run_path),
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=v4_manifest_path,
        run_dir=v4_run_dir,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        command=v4_command,
    )
    autoresearch_runs.prepare_output_capture(
        run_dir=v4_run_dir,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    for stream in autoresearch_runs.RunOutputStream:
        autoresearch_runs.capture_output_stream(
            run_dir=v4_run_dir,
            runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            stream=stream,
            source=BytesIO(b""),
        )
    autoresearch_runs.start_run(
        run_dir=v4_run_dir,
        pid=999_999,
        systemd_unit="openclaw-long-task-2-2.service",
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    autoresearch_runs.complete_run(
        run_dir=v4_run_dir,
        exit_code=1,
        signal_number=None,
        peak_rss_bytes=None,
        failure_classification=autoresearch_runs.RunFailureClassification.PROCESS_ERROR,
        runs_root=autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
    )
    copied_state_path = tmp_path / "copied-live-v4.json"
    copied_state_path.write_bytes(live_state_path.read_bytes())
    persisted_live = json.loads(live_state_path.read_text(encoding="utf-8"))
    assert set(cast(dict[str, object], persisted_live["platform_readiness"])) == {
        "manifest_id",
        "snapshot_id",
        "receipt_sha256",
    }
    assert persisted_live.get("canonical_quantipy_runtime_attestation") is None
    assert persisted_live.get("platform_runtime_recovery_receipt") is None
    monkeypatch.setattr(
        autoresearch_runner,
        "DEFAULT_AUTORESEARCH_STATE_PATH",
        live_state_path,
    )
    artifact_hashes = tuple(
        (path, sha256(path.read_bytes()).hexdigest())
        for root in (
            v2_run_path.parent,
            v2_detached_dir,
            v3_run_dir,
            v4_run_path.parent,
            v4_run_dir,
        )
        for path in sorted(path for path in root.rglob("*") if path.is_file())
    )
    return PublicPlatformRecoveryFixture(
        live_state_path=live_state_path,
        copied_state_path=copied_state_path,
        probe=replace(probe, response_bytes=19, response_sha256="b" * 64),
        readiness=readiness,
        validation_context=validation_context,
        live_state_bytes=live_state_path.read_bytes(),
        artifact_hashes=artifact_hashes,
        successful_run_template_path=Path(evidence.run_json_path),
        failed_run_template_path=v4_run_path,
    )


@pytest.fixture()
def successful_quantipy_evidence() -> QuantipyExperimentEvidence:
    evidence = _verification_result(VerificationStatus.PASS).quantipy_experiment_evidence
    assert evidence is not None
    return evidence


@pytest.fixture()
def alpha_memory_state(policy: AutoresearchPolicy) -> AutoresearchState:
    long_rationale = "The completed experiment has a durable methodology limitation. " * 5
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            max_drawdown_pct=34.0,
            oos_sharpe_net=0.92,
            is_walk_forward_sharpe_net=0.84,
        ),
        policy,
    )
    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)
    return advance_state(
        state,
        replace(
            _final_decision(),
            experiment_id="alpha-discard-1",
            decision=FinalDecision.DISCARD,
            recommended_metric_value=0.92,
            rationale=long_rationale,
            log_summary="Discarded after a durable methodology limitation.",
        ),
        policy,
    )


@pytest.fixture()
def live_v2_http_413_state_file(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
) -> tuple[
    Path,
    ResearchPanelProbeReceipt,
    AutoresearchValidationContext,
    tuple[tuple[Path, str], ...],
]:
    """A private copy of the live stale-state plus attested v2 artifact shape."""
    state, _, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        panel_requested=True,
    )
    manifest = json.loads(Path(evidence.manifest_path).read_text(encoding="utf-8"))
    panel = cast(dict[str, object], manifest["panel"])
    request = cast(dict[str, object], panel["request"])
    expected_url = "http://127.0.0.1:8000/price-data/research-panel?" + urlencode(
        (
            ("tickers", ",".join(cast(list[str], request["tickers"]))),
            ("start", "2026-07-28T12:00:00+00:00"),
            ("end", "2026-07-28T13:00:00+00:00"),
            ("timeframe", cast(str, request["timeframe"])),
            ("market_hours", cast(str, request["market_hours"])),
        )
    )
    initial = replace(
        _verification_result(VerificationStatus.TEST_FAILURE, external_panel_failure=True),
        quantipy_experiment_evidence=replace(
            evidence,
            success=False,
            completed_stages=(),
            terminal_stage=None,
            terminal_status=None,
            failure=QuantipyExperimentFailureEvidence(
                category="panel",
                message=(
                    "ExperimentPanelError: Client error '404 Not Found' for url "
                    f"'{expected_url}'\n"
                    "For more information check: "
                    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
                ),
            ),
            panel=None,
        ),
    )
    failed_initial = advance_state(state, initial, policy)
    probe = ResearchPanelProbeReceipt(
        endpoint="http://127.0.0.1:8000/price-data/research-panel",
        observed_at="2026-07-29T12:00:00Z",
        response_bytes=18,
        response_sha256="a" * 64,
        session_date="2022-01-03",
        symbol="AAPL",
    )
    v2_receipt = ExternalVerificationRetryReceipt.for_state(
        failed_initial, probe, "Raised the gateway receipt limit."
    )
    stale_state = retry_external_verification(failed_initial, v2_receipt)
    v2_run_path = trusted_quantipy_runs_root / v2_receipt.expected_run_id / "run.json"
    run = json.loads(Path(evidence.run_json_path).read_text(encoding="utf-8"))
    run.update(
        run_id=v2_receipt.expected_run_id,
        success=False,
        panel_requested=True,
        panel=None,
        stage_receipts=[],
        failure={
            "category": "panel",
            "message": (
                "ExperimentPanelError: Client error '413 Request Entity Too Large' for url "
                f"'{expected_url}'\nFor more information check: "
                "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/413"
            ),
        },
    )
    v2_run_path.parent.mkdir(mode=0o700)
    v2_run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    v2_run_path.write_bytes(v2_run_bytes)
    v2_run_path.chmod(0o600)
    detached_run_dir = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
        f"i{stale_state.iteration}-verification-r1-a{v2_receipt.retry_attempt}-"
        f"{v2_receipt.implementation_commit[:12]}-v{v2_receipt.retry_attempt}"
    )
    _write_quantipy_detached_run_record(
        workspace=git_worktree.workspace,
        runtime_root=git_worktree.target_checkout,
        manifest_path=evidence.manifest_path,
        run_id=v2_receipt.expected_run_id,
        run_path=v2_run_path,
        detached_run_dir=detached_run_dir,
    )
    state_path = tmp_path / "live-v2-http-413-state.json"
    state_path.write_text(json.dumps(stale_state.to_dict()), encoding="utf-8")
    immutable_hashes = tuple(
        (path, sha256(path.read_bytes()).hexdigest())
        for root in (v2_run_path.parent, detached_run_dir)
        for path in sorted(path for path in root.rglob("*") if path.is_file())
    )
    return state_path, probe, _runtime_verification_context(stale_state), immutable_hashes
