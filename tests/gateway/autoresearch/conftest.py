from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import gateway.autoresearch_runner as autoresearch_runner
import gateway.autoresearch_runs as autoresearch_runs
import pytest
from gateway.autoresearch_readiness import PlatformReadinessManifest
from gateway.autoresearch_runner import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    QUANTIPY_RECEIPT_PATHS,
    AutoresearchPolicy,
    AutoresearchState,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    FixTriggerPhase,
    InfraGateOutcome,
    MemoryVerificationReceipt,
    Phase,
    QuantipyExperimentEvidence,
    ReceiptCatalog,
    ResearchMode,
    VerificationStatus,
    build_receipt_catalog,
    load_autoresearch_policy,
    mark_memory_written,
)

from tests.gateway.autoresearch.builders import (
    GitWorktree,
    _context_artifact,
    _debate_result,
    _final_decision,
    _fix_result,
    _g0_remediation_verification,
    _git,
    _implementation_result,
    _majority_consensus,
    _no_consensus,
    _ready_manifest,
    _setup_artifact,
    _state_to_decision,
    _state_to_g0_decision,
    _verification_result,
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
    monkeypatch.setattr(autoresearch_runner, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", root)
    detached_root = tmp_path / "trusted-detached-runs"
    monkeypatch.setattr(
        autoresearch_runs,
        "DEFAULT_AUTORESEARCH_RUNS_ROOT",
        detached_root,
    )
    return root


@pytest.fixture()
def successful_quantipy_evidence() -> QuantipyExperimentEvidence:
    evidence = _verification_result(VerificationStatus.PASS).quantipy_experiment_evidence
    assert evidence is not None
    return evidence
