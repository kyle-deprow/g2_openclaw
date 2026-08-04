"""Behavioral coverage for the mode-aware autoresearch control plane."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from gateway.autoresearch.artifacts import (
    ARTIFACT_CONTRACTS,
    ConsensusResultArtifact,
    ContextPacketArtifact,
    FinalDecisionArtifact,
    ImplementationResultArtifact,
    MemoryVerificationReceipt,
    PriceHydrationScopePreflight,
    VerificationResultArtifact,
)
from gateway.autoresearch.compute import (
    ComputeFitArtifact,
)
from gateway.autoresearch.configuration import load_autoresearch_policy
from gateway.autoresearch.constants import (
    MEMBER_UNION_DIGEST_ALGORITHM,
)
from gateway.autoresearch.enums import (
    ArtifactType,
    ComputeTarget,
    FinalDecision,
    FinalReviewerVerdict,
    InfraGateOutcome,
    Phase,
    ResearchMode,
    VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    price_hydration_coverage_digest,
    price_hydration_request_digest,
)
from gateway.autoresearch.memory import (
    build_final_memory_write_request,
    verify_mempalace_final_decision,
)
from gateway.autoresearch.receipts import (
    AggregateCoverageReceipt,
    AuthoritativeSnapshotReceipt,
    CoverageReceipt,
    GroupedSummaryReceipt,
    MemberUnionManifestReceipt,
    PriceHydrationReceipt,
    UniverseDateVerificationReceipt,
    UniverseHistoryBatchReceipt,
    UniverseVerificationReceipt,
)
from gateway.autoresearch.state import (
    AutoresearchState,
)
from gateway.autoresearch_platform_validation import (
    canonical_dynamic_price_coverage_digest,
    canonical_requested_sessions_digest,
)
from gateway.mempalace_finalizer import FINAL_MEMORY_SOURCE_FILE, finalization_journal_path

from tests.gateway.autoresearch.builders import _majority_consensus, _no_consensus


def _coverage(*, fixed_sleeve_local_data: bool = False) -> AggregateCoverageReceipt:
    symbol = CoverageReceipt(
        symbol="AMD",
        declared_intended_start="2021-01-04",
        declared_intended_end="2021-12-31",
        actual_common_start="2021-01-04",
        actual_common_end="2021-12-31",
        oos_start="2021-10-01",
        oos_end="2021-12-31",
        expected_trading_days=252,
        actual_trading_days=252,
        coverage_percent=100.0,
        missing_reason=None,
        default_fold_count=0,
        fallback_fold_count=0,
        cap_provenance_available=not fixed_sleeve_local_data,
        fixed_sleeve_local_data=fixed_sleeve_local_data,
    )
    return AggregateCoverageReceipt(
        declared_intended_start=symbol.declared_intended_start,
        declared_intended_end=symbol.declared_intended_end,
        actual_common_start=symbol.actual_common_start,
        actual_common_end=symbol.actual_common_end,
        oos_start=symbol.oos_start,
        oos_end=symbol.oos_end,
        expected_trading_days=symbol.expected_trading_days,
        actual_trading_days=symbol.actual_trading_days,
        coverage_percent=symbol.coverage_percent,
        missing_reason=None,
        default_fold_count=0,
        fallback_fold_count=0,
        cap_provenance_available=not fixed_sleeve_local_data,
        fixed_sleeve_local_data=fixed_sleeve_local_data,
        per_symbol=(symbol,),
    )


def _alpha_verification() -> VerificationResultArtifact:
    return VerificationResultArtifact(
        status=VerificationStatus.PASS,
        is_walk_forward_sharpe_net=0.41,
        oos_sharpe_net=0.38,
        max_drawdown_pct=12.4,
        win_rate=0.54,
        trade_count=211,
        trades_per_day=1.9,
        oos_trading_days=128,
        feature_importances_summary="VWAP distance and OBV slope dominate.",
        null_test_summary="Null shuffle drops Sharpe near zero.",
        bug_signals=(),
        tests_passed=True,
        commands_run=("uv run pytest",),
        data_coverage=_coverage(),
    )


def _retention_eligible_repeat_state() -> AutoresearchState:
    return AutoresearchState(
        phase=Phase.REPEAT,
        mode=ResearchMode.ALPHA_RESEARCH,
        verification_history=(_alpha_verification(),),
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-1",
            decision=FinalDecision.KEEP,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.38,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Improves baseline.",
            log_summary="KEEP.",
            continue_loop=True,
            memory_write_required=True,
        ),
    )


def _write_committed_finalization_journal(
    state: AutoresearchState,
    palace_path: Path,
    *,
    drawer_id: str,
) -> None:
    request = build_final_memory_write_request(state)
    request_payload = json.dumps(request.to_dict(), separators=(",", ":"), sort_keys=True)
    journal_path = finalization_journal_path(palace_path, request.experiment_id)
    journal_path.parent.mkdir()
    journal_path.write_text(
        json.dumps(
            {
                "drawer_id": drawer_id,
                "request_sha256": hashlib.sha256(request_payload.encode("utf-8")).hexdigest(),
                "status": "committed",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def g0_pass_verification_raw() -> dict[str, object]:
    member_union_digest = "f" * 64
    source_coverage_digest = "c" * 64
    completed_at = "2026-07-15T12:00:00+00:00"
    universe = UniverseVerificationReceipt(
        profile_id="liquid-common-stocks-v1",
        profile_digest="a" * 64,
        execution_policy="next-session-or-later",
        max_members_per_date=300,
        batches=(
            UniverseHistoryBatchReceipt(
                contract_digest="b" * 64,
                operation_count=1,
                dates=(
                    UniverseDateVerificationReceipt(
                        selection_date="2021-01-04",
                        earliest_execution_date="2021-01-05",
                        calendar_identity="XNYS",
                        calendar_digest="c" * 64,
                        selected_member_count=1,
                        snapshot=AuthoritativeSnapshotReceipt(
                            as_of_date="2021-01-04",
                            source="massive",
                            result_count=17,
                            identity_digest="d" * 64,
                            content_digest="d" * 64,
                            completed_at=completed_at,
                        ),
                        summary=GroupedSummaryReceipt(
                            summary_date="2021-01-04",
                            source="massive",
                            result_count=17,
                            identity_digest="e" * 64,
                            content_digest="e" * 64,
                            completed_at=completed_at,
                            adjusted=False,
                        ),
                    ),
                ),
            ),
        ),
        member_union_digest_algorithm=MEMBER_UNION_DIGEST_ALGORITHM,
        member_union_count=1,
        member_union_digest=member_union_digest,
        member_union_manifest=MemberUnionManifestReceipt(
            path="/tmp/member-union.txt", sha256="b" * 64
        ),
    )
    request_digest = price_hydration_request_digest(
        member_union_count=1,
        member_union_digest=member_union_digest,
        experiment_start="2021-01-04",
        experiment_end="2021-01-04",
        timeframe="1min",
        market_hours="regular",
    )
    hydration = PriceHydrationReceipt(
        member_union_count=1,
        member_union_digest=member_union_digest,
        experiment_start="2021-01-04",
        experiment_end="2021-01-04",
        timeframe="1min",
        market_hours="regular",
        operation_count=1,
        request_digest=request_digest,
        coverage_receipt_digest=price_hydration_coverage_digest(
            request_digest=request_digest,
            operation_count=1,
            completed_at=completed_at,
        ),
        source_price_coverage_response_digest=source_coverage_digest,
        completed_at=completed_at,
        folds_started_at="2026-07-15T12:01:00+00:00",
    )
    platform_coverage_validation: dict[str, object] = {
        "contract_version": "dynamic-price-coverage-v1",
        "source_contract_version": "price-coverage-v1",
        "scope": "full_union_hydration",
        "status": "COMPLETE",
        "requested_start_date": "2021-01-04",
        "requested_end_date": "2021-01-04",
        "timeframe": "1min",
        "market_hours": "regular",
        "source_requested_start_date": "2021-01-04",
        "source_requested_end_date": "2021-01-04",
        "source_timeframe": "1min",
        "source_market_hours": "regular",
        "source_provider": "massive",
        "member_union_digest": member_union_digest,
        "requested_sessions_digest": canonical_requested_sessions_digest((date(2021, 1, 4),)),
        "pit_active_roster_digest": "d" * 64,
        "source_price_coverage_response_digest": source_coverage_digest,
        "member_union_count": 1,
        "requested_session_count": 1,
        "hydrated_symbol_sessions": 1,
        "observed_hydrated_symbol_sessions": 1,
        "provider_empty_hydrated_symbol_sessions": 0,
        "missing_hydrated_symbol_sessions": 0,
        "active_symbol_sessions": 1,
        "observed_active_symbol_sessions": 1,
        "provider_empty_active_symbol_sessions": 0,
        "missing_active_symbol_sessions": 0,
        "inactive_union_symbol_sessions": 0,
        "unexpected_ticker_count": 0,
        "unexpected_session_count": 0,
        "violation_codes": [],
    }
    platform_coverage_validation["receipt_digest"] = canonical_dynamic_price_coverage_digest(
        platform_coverage_validation
    )
    raw = _alpha_verification().to_dict()
    raw.update(
        {
            "platform_coverage_validation": platform_coverage_validation,
            "universe_verification_receipt": universe.to_dict(),
            "price_hydration_receipt": hydration.to_dict(),
        }
    )
    return raw


def test_context_packet_roundtrip_requires_explicit_mode_and_rationale() -> None:
    artifact = ContextPacketArtifact(
        baseline_metric="0.18 OOS Sharpe net",
        current_best_metric="0.22 OOS Sharpe net",
        recent_experiment_outcomes=("T1 discard",),
        prior_findings=("Prior coverage was incomplete",),
        open_proposals=("Repair source provenance",),
        hard_constraints=("2021-2026",),
        available_data_sources=("qp.prices()",),
        loaded_quantipy_sources=("AGENTS.md",),
        research_mode=ResearchMode.DATA_INFRA_G0,
        mode_rationale="Data provenance is not yet sufficient for alpha claims.",
        burned_theory_families=("vwap-obv",),
    )

    loaded = ContextPacketArtifact.from_dict(artifact.to_dict())

    assert loaded == artifact


def test_state_json_rejects_missing_mode_after_context_exists() -> None:
    raw = AutoresearchState(
        context_packet=ContextPacketArtifact(
            baseline_metric="0.18",
            current_best_metric="0.22",
            recent_experiment_outcomes=(),
            prior_findings=(),
            open_proposals=(),
            hard_constraints=(),
            available_data_sources=("qp.prices()",),
            loaded_quantipy_sources=("AGENTS.md",),
            research_mode=ResearchMode.ALPHA_RESEARCH,
            mode_rationale="Coverage evidence is complete.",
            burned_theory_families=(),
        ),
        mode=ResearchMode.ALPHA_RESEARCH,
    ).to_dict()
    del raw["mode"]

    with pytest.raises(AutoresearchValidationError, match="mode must be explicit"):
        AutoresearchState.from_dict(raw)


def test_coverage_rejects_unexplained_missing_trading_days() -> None:
    receipt = _coverage().to_dict()
    receipt["actual_trading_days"] = 250
    receipt["coverage_percent"] = 99.21

    with pytest.raises(AutoresearchValidationError, match="missing_reason"):
        AggregateCoverageReceipt.from_dict(receipt)


def test_alpha_fixed_sleeve_cannot_claim_cap_provenance() -> None:
    receipt = _coverage(fixed_sleeve_local_data=True).to_dict()
    receipt["cap_provenance_available"] = True

    with pytest.raises(AutoresearchValidationError, match="fixed_sleeve_local_data"):
        AggregateCoverageReceipt.from_dict(receipt)


def test_aggregate_coverage_derives_common_windows_from_each_symbol() -> None:
    first = _coverage().per_symbol[0]
    second = CoverageReceipt(
        symbol="NVDA",
        declared_intended_start="2021-01-04",
        declared_intended_end="2021-12-31",
        actual_common_start="2021-02-01",
        actual_common_end="2021-12-31",
        oos_start="2021-11-01",
        oos_end="2021-12-31",
        expected_trading_days=252,
        actual_trading_days=252,
        coverage_percent=100.0,
        missing_reason=None,
        default_fold_count=0,
        fallback_fold_count=0,
        cap_provenance_available=True,
        fixed_sleeve_local_data=False,
    )
    aggregate = AggregateCoverageReceipt(
        declared_intended_start=first.declared_intended_start,
        declared_intended_end=first.declared_intended_end,
        actual_common_start=first.actual_common_start,
        actual_common_end=first.actual_common_end,
        oos_start=first.oos_start,
        oos_end=first.oos_end,
        expected_trading_days=252,
        actual_trading_days=252,
        coverage_percent=100.0,
        missing_reason=None,
        default_fold_count=0,
        fallback_fold_count=0,
        cap_provenance_available=True,
        fixed_sleeve_local_data=False,
        per_symbol=(first, second),
    )

    with pytest.raises(AutoresearchValidationError, match="actual_common_start"):
        aggregate.validate()


def test_aggregate_coverage_rejects_non_common_day_counts_and_fold_counts() -> None:
    first = _coverage().per_symbol[0]
    second = CoverageReceipt(
        symbol="NVDA",
        declared_intended_start=first.declared_intended_start,
        declared_intended_end=first.declared_intended_end,
        actual_common_start=first.actual_common_start,
        actual_common_end=first.actual_common_end,
        oos_start=first.oos_start,
        oos_end=first.oos_end,
        expected_trading_days=252,
        actual_trading_days=251,
        coverage_percent=99.6,
        missing_reason="One common-calendar trading day is missing.",
        default_fold_count=1,
        fallback_fold_count=2,
        cap_provenance_available=True,
        fixed_sleeve_local_data=False,
    )
    aggregate = AggregateCoverageReceipt(
        declared_intended_start=first.declared_intended_start,
        declared_intended_end=first.declared_intended_end,
        actual_common_start=first.actual_common_start,
        actual_common_end=first.actual_common_end,
        oos_start=first.oos_start,
        oos_end=first.oos_end,
        expected_trading_days=252,
        actual_trading_days=252,
        coverage_percent=100.0,
        missing_reason=None,
        default_fold_count=0,
        fallback_fold_count=0,
        cap_provenance_available=True,
        fixed_sleeve_local_data=False,
        per_symbol=(first, second),
    )

    with pytest.raises(AutoresearchValidationError, match="actual_trading_days"):
        aggregate.validate()


def test_g0_verification_requires_an_explicit_infrastructure_outcome(
    g0_pass_verification_raw: dict[str, object],
) -> None:
    raw = g0_pass_verification_raw
    raw["infra_gate_outcome"] = None
    raw["infra_rationale"] = None

    with pytest.raises(AutoresearchValidationError, match="infra_gate_outcome"):
        VerificationResultArtifact.from_dict(raw, mode=ResearchMode.DATA_INFRA_G0)


def test_alpha_verification_rejects_infrastructure_gate_outcome() -> None:
    verification = _alpha_verification()

    with pytest.raises(AutoresearchValidationError, match="ALPHA_RESEARCH"):
        verification.validate(
            mode=ResearchMode.ALPHA_RESEARCH,
            infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
        )


def test_affected_artifact_contracts_match_their_serialized_fields() -> None:
    implementation = ImplementationResultArtifact(
        summary="Added the narrow implementation.",
        workspace_path="/tmp/quantipy-worktree",
        commit_sha="abc1234",
        module_path="src/quantipy/alpha/example/",
        notebook_path="notebooks/experiments/example.ipynb",
        tests_added_or_updated=("tests/test_example.py",),
        commands_run=("uv run pytest tests/test_example.py",),
        compute_fit=ComputeFitArtifact(
            target=ComputeTarget.CPU,
            rationale=(
                "The example is a small tabular experiment and CPU execution is reproducible."
            ),
            required_dependencies=(),
            benchmark_plan="Record wall time and peak memory during verification.",
        ),
        price_hydration_scope_preflight=PriceHydrationScopePreflight(
            member_union_count=1,
            experiment_start="2021-01-04",
            experiment_end="2021-12-31",
            timeframe="1min",
            market_hours="regular",
            session_count=252,
            planned_symbol_sessions=252,
            within_budget=True,
        ),
    )
    final_decision = FinalDecisionArtifact(
        experiment_id="iteration-1",
        decision=FinalDecision.KEEP,
        recommended_metric_name="OOS Sharpe net",
        recommended_metric_value=0.38,
        reviewer_verdict=FinalReviewerVerdict.PASS,
        rationale="Improves baseline.",
        log_summary="KEEP.",
        continue_loop=True,
        memory_write_required=True,
    )

    implementation_fields = cast(
        list[str], ARTIFACT_CONTRACTS[ArtifactType.IMPLEMENTATION_RESULT]["required_fields"]
    )
    verification_fields = cast(
        list[str], ARTIFACT_CONTRACTS[ArtifactType.VERIFICATION_RESULT]["required_fields"]
    )
    decision_fields = cast(
        list[str], ARTIFACT_CONTRACTS[ArtifactType.FINAL_DECISION]["required_fields"]
    )

    assert set(implementation_fields) == set(implementation.to_dict())
    assert set(verification_fields) == set(_alpha_verification().to_dict())
    assert set(decision_fields) == set(final_decision.to_dict())

    consensus = _majority_consensus(
        round_number=1,
        policy=load_autoresearch_policy(Path("gateway/openclaw_config/openclaw.json")),
    )
    consensus_fields = cast(
        list[str], ARTIFACT_CONTRACTS[ArtifactType.CONSENSUS_RESULT]["required_fields"]
    )
    assert "novelty_delta|null" in consensus_fields
    assert "novelty_delta" in consensus.to_dict()
    assert ConsensusResultArtifact.from_dict(consensus.to_dict()) == consensus
    with pytest.raises(AutoresearchValidationError, match="exact keys"):
        ConsensusResultArtifact.from_dict(
            {key: value for key, value in consensus.to_dict().items() if key != "novelty_delta"}
        )

    for delta in ("x" * 31, "x" * 1025, " " * 32):
        with pytest.raises(AutoresearchValidationError, match="novelty_delta"):
            replace(consensus, novelty_delta=delta).validate()
    with pytest.raises(AutoresearchValidationError, match="NO_CONSENSUS"):
        replace(_no_consensus(1), novelty_delta="x" * 32).validate()


@pytest.mark.parametrize("experiment_id", ("Iteration-1", " iteration-1 "))
def test_final_decision_rejects_noncanonical_experiment_id(experiment_id: str) -> None:
    raw = FinalDecisionArtifact(
        experiment_id="iteration-1",
        decision=FinalDecision.KEEP,
        recommended_metric_name="OOS Sharpe net",
        recommended_metric_value=0.38,
        reviewer_verdict=FinalReviewerVerdict.PASS,
        rationale="Improves baseline.",
        log_summary="KEEP.",
        continue_loop=True,
        memory_write_required=True,
    ).to_dict()
    raw["experiment_id"] = experiment_id

    with pytest.raises(AutoresearchValidationError, match="canonical lowercase kebab-case"):
        FinalDecisionArtifact.from_dict(raw)


@pytest.mark.parametrize("experiment_id", ("Iteration-1", " iteration-1 "))
def test_memory_receipt_rejects_noncanonical_experiment_id(experiment_id: str) -> None:
    raw = MemoryVerificationReceipt(
        experiment_id="iteration-1",
        kg_path="/tmp/knowledge_graph.sqlite3",
        predicates=("decision",),
        verified_rows_digest="0" * 64,
    ).to_dict()
    raw["experiment_id"] = experiment_id

    with pytest.raises(AutoresearchValidationError, match="canonical lowercase kebab-case"):
        MemoryVerificationReceipt.from_dict(raw)


def test_mempalace_receipt_verifies_required_standardized_facts(tmp_path: Path) -> None:
    state = _retention_eligible_repeat_state()
    drawer_id = "drawer-finalizer"
    database = tmp_path / "knowledge_graph.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE triples (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            object TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
            source_file TEXT, source_drawer_id TEXT
        );
        """
    )
    facts = (
        ("decision", "keep"),
        ("research_mode", "alpha_research"),
        ("alpha_decision_metric", "oos_sharpe_net_0_38"),
        ("data_window", "2021_01_04_to_2021_12_31_oos_2021_10_01_to_2021_12_31"),
        ("reviewer_verdict", "pass"),
        ("keeper_rationale", "improves_baseline"),
    )
    connection.executemany(
        "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
        [
            (
                f"row-{index}",
                "iteration-1",
                predicate,
                obj,
                FINAL_MEMORY_SOURCE_FILE,
                drawer_id,
            )
            for index, (predicate, obj) in enumerate(facts)
        ],
    )
    connection.commit()
    connection.close()
    _write_committed_finalization_journal(state, tmp_path, drawer_id=drawer_id)

    receipt = verify_mempalace_final_decision(state, database)

    assert isinstance(receipt, MemoryVerificationReceipt)


def test_mempalace_receipt_fails_closed_without_provenance(tmp_path: Path) -> None:
    state = _retention_eligible_repeat_state()
    drawer_id = "drawer-finalizer"
    database = tmp_path / "knowledge_graph.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE triples (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            object TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
            source_file TEXT, source_drawer_id TEXT
        );
        INSERT INTO triples VALUES
            ('1', 'iteration-1', 'decision', 'keep', NULL, NULL, 'result.json', NULL);
        """
    )
    connection.commit()
    connection.close()
    _write_committed_finalization_journal(state, tmp_path, drawer_id=drawer_id)

    with pytest.raises(AutoresearchValidationError, match="exact canonical finalizer provenance"):
        verify_mempalace_final_decision(state, database)


@pytest.mark.parametrize(
    ("predicate", "object_value"),
    (
        ("data_window", "2021_01_05_to_2021_12_31_oos_2021_10_01_to_2021_12_31"),
        ("keeper_rationale", "a_different_rationale"),
    ),
)
def test_mempalace_receipt_rejects_mismatched_required_fact_objects(
    tmp_path: Path,
    predicate: str,
    object_value: str,
) -> None:
    state = _retention_eligible_repeat_state()
    drawer_id = "drawer-finalizer"
    database = tmp_path / "knowledge_graph.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE triples (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            object TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
            source_file TEXT, source_drawer_id TEXT
        );
        """
    )
    facts = {
        "decision": "keep",
        "research_mode": "alpha_research",
        "alpha_decision_metric": "oos_sharpe_net_0_38",
        "data_window": "2021_01_04_to_2021_12_31_oos_2021_10_01_to_2021_12_31",
        "reviewer_verdict": "pass",
        "keeper_rationale": "improves_baseline",
    }
    facts[predicate] = object_value
    connection.executemany(
        "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
        [
            (
                f"row-{index}",
                "iteration-1",
                fact_predicate,
                fact_object,
                FINAL_MEMORY_SOURCE_FILE,
                drawer_id,
            )
            for index, (fact_predicate, fact_object) in enumerate(facts.items())
        ],
    )
    connection.commit()
    connection.close()
    _write_committed_finalization_journal(state, tmp_path, drawer_id=drawer_id)

    with pytest.raises(AutoresearchValidationError, match=predicate):
        verify_mempalace_final_decision(state, database)


def test_mempalace_verification_rejects_no_consensus_without_a_memory_requirement() -> None:
    state = AutoresearchState(
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="no-consensus-1",
            decision=FinalDecision.NO_CONSENSUS,
            recommended_metric_name="consensus outcome",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The retry produced no majority and no implementation was created.",
            log_summary="No consensus after the allowed retry.",
            continue_loop=True,
            memory_write_required=False,
        ),
    )

    with pytest.raises(AutoresearchValidationError, match="prohibited for a non-retention"):
        verify_mempalace_final_decision(state)
