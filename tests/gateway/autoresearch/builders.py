from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import zlib
from collections.abc import Mapping
from dataclasses import (
    dataclass,
    replace,
)
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast

import gateway.autoresearch_runner as autoresearch_runner
import gateway.autoresearch_runs as autoresearch_runs
from gateway.autoresearch_platform_validation import (
    DynamicPriceCoverageReceipt,
    PlatformCoverageScope,
    PlatformCoverageStatus,
    PlatformCoverageViolationCode,
    canonical_dynamic_price_coverage_digest,
)
from gateway.autoresearch_readiness import (
    PLATFORM_READINESS_SCHEMA_VERSION,
    EvidenceId,
    PlatformReadinessManifest,
)
from gateway.autoresearch_runner import (
    DEFAULT_AUTORESEARCH_WORKTREE_ROOT,
    MEMBER_UNION_DIGEST_ALGORITHM,
    AggregateCoverageReceipt,
    AuthoritativeSnapshotReceipt,
    AutoresearchPolicy,
    AutoresearchState,
    AutoresearchValidationContext,
    ComputeFitArtifact,
    ComputeTarget,
    ConsensusResultArtifact,
    ConsensusStatus,
    ContextPacketArtifact,
    CoverageReceipt,
    DebateResultArtifact,
    DebateSubmission,
    DynamicUniverseCoverageReceipt,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    FixResultArtifact,
    FixTriggerPhase,
    GroupedSummaryReceipt,
    ImplementationResultArtifact,
    InfraGateOutcome,
    MemberUnionManifestReceipt,
    MetricDirection,
    PriceHydrationReceipt,
    PriceHydrationScopePreflight,
    QuantipyExperimentEvidence,
    QuantipyExperimentFailureEvidence,
    QuantipyExperimentPanelEvidence,
    ResearchMode,
    ReviewResultArtifact,
    ReviewVerdict,
    SetupContextArtifact,
    UniverseDateVerificationReceipt,
    UniverseHistoryBatchReceipt,
    UniversePlanArtifact,
    UniverseVerificationReceipt,
    VerificationResultArtifact,
    VerificationStatus,
    price_hydration_coverage_digest,
    price_hydration_request_digest,
    validate_artifact_workspace,
)
from gateway.autoresearch_runner import advance_state as _runner_advance_state
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_SOURCE_FILE,
    FinalMemoryWriteRequest,
    finalization_journal_path,
)

from tests.gateway.autoresearch_fixtures import write_xnys_calendar_evidence

QUANTIPY_V2_CONTRACT_FILE_SHA256 = {
    "src/quantipy/experiments/filesystem.py": "".join(
        (
            "ec0656ed0fbb9851",  # pragma: allowlist secret
            "a53168a8c7278653",  # pragma: allowlist secret
            "ac0ad9102d14a967",  # pragma: allowlist secret
            "2e75006bc5a6d413",  # pragma: allowlist secret
        )
    ),
    "src/quantipy/experiments/preflight.py": "".join(
        (
            "ee7601b415713499",  # pragma: allowlist secret
            "f7b0c867977cd3d4",  # pragma: allowlist secret
            "058478ded35341d4",  # pragma: allowlist secret
            "cc1a3894783d215a",  # pragma: allowlist secret
        )
    ),
    "src/quantipy/experiments/runtime.py": "".join(
        (
            "7a656526d9fb2a7c",  # pragma: allowlist secret
            "5035e093d354f77a",  # pragma: allowlist secret
            "28aabe5a51b5424d",  # pragma: allowlist secret
            "1f285d93f89bedbc",  # pragma: allowlist secret
        )
    ),
    "src/quantipy/experiments/schemas.py": "".join(
        (
            "7915fc05d724b20a",  # pragma: allowlist secret
            "504e8c45258651bc",  # pragma: allowlist secret
            "f88030e708149155",  # pragma: allowlist secret
            "4a06beff36c7be51",  # pragma: allowlist secret
        )
    ),
}

_MEMBER_UNION_PATH = Path("tests/fixtures/autoresearch-member-union.txt").resolve()

_MEMBER_UNION_SHA256 = sha256(_MEMBER_UNION_PATH.read_bytes()).hexdigest()

_MEMBER_UNION_DIGEST = (
    "549c815538959493fe913ff6e4514bb52cd37cec360a9908c9795cd303f743e6"  # pragma: allowlist secret
)

_SOURCE_PRICE_COVERAGE_RESPONSE_DIGEST = "d" * 64

StateArtifact = (
    SetupContextArtifact
    | ContextPacketArtifact
    | DebateResultArtifact
    | ConsensusResultArtifact
    | ImplementationResultArtifact
    | VerificationResultArtifact
    | ReviewResultArtifact
    | FixResultArtifact
    | FinalDecisionArtifact
)


def advance_state(
    state: AutoresearchState,
    artifact: StateArtifact,
    policy: AutoresearchPolicy,
) -> AutoresearchState:
    context: AutoresearchValidationContext | None = None
    if state.mode in (ResearchMode.ALPHA_RESEARCH, ResearchMode.DATA_INFRA_G0) and (
        isinstance(artifact, VerificationResultArtifact)
        or (
            state.mode is ResearchMode.DATA_INFRA_G0 and isinstance(artifact, FinalDecisionArtifact)
        )
    ):
        context = AutoresearchValidationContext(
            state.platform_readiness,
            "f" * 64,
            (date(2021, 1, 5),),
        )
    return _runner_advance_state(state, artifact, policy, validation_context=context)


def _ready_manifest(
    tmp_path: Path,
    *,
    quantipy_commit: str = "a" * 40,
) -> PlatformReadinessManifest:
    evidence: dict[EvidenceId, dict[str, str | None]] = {}
    tmp_path.mkdir(parents=True, exist_ok=True)
    for evidence_id in EvidenceId:
        path = tmp_path / f"{evidence_id.value}.json"
        if evidence_id is EvidenceId.XNYS_TRADING_CALENDAR:
            write_xnys_calendar_evidence(path)
        else:
            path.write_text(
                json.dumps({"quantipy_commit": quantipy_commit}),
                encoding="utf-8",
            )
        evidence[evidence_id] = {
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "reason": None,
        }
    return PlatformReadinessManifest.from_dict(
        {
            "schema_version": PLATFORM_READINESS_SCHEMA_VERSION,
            "status": "READY",
            "manifest_id": "manifest-test-1",
            "snapshot_id": "snapshot-test-1",
            "evidence": {evidence_id.value: value for evidence_id, value in evidence.items()},
            "capabilities": {
                "security_master": {
                    "historical_snapshots_interface": True,
                    "historical_security_type_common_stock_filter_pit_certified": True,
                    "inactive_listings_interface": True,
                    "unadjusted_liquidity_screens_interface": True,
                    "universe_history_api_and_client_interface": True,
                    "next_session_execution_policy_interface": True,
                    "split_actions_interface": True,
                    "dividend_actions_interface": True,
                    "ticker_detail_market_cap_interface": True,
                    "ticker_detail_market_cap_pit_certified": False,
                },
                "market_data": {
                    "ohlcv_cache_or_hydrate_interface": True,
                    "historical_trades_interface": False,
                    "historical_quotes_interface": False,
                    "historical_fundamentals_interface": False,
                },
                "reddit_dataset": {
                    "available": False,
                    "start_date": None,
                    "end_date": None,
                    "record_count": None,
                    "reason": "live database query unavailable",
                },
                "news_dataset": {
                    "available": False,
                    "start_date": None,
                    "end_date": None,
                    "record_count": None,
                    "reason": "live database query unavailable",
                },
            },
            "reason": None,
        }
    )


def _prompt_json_value(prompt: str, prefix: str) -> str:
    return prompt.rsplit(prefix, maxsplit=1)[1].split("\n", maxsplit=1)[0]


def _round_trip_compact_json(payload: str) -> dict[str, object]:
    decoded = cast(dict[str, object], json.loads(payload))
    assert json.dumps(decoded, sort_keys=True, separators=(",", ":")) == payload
    return decoded


def _legacy_artifact_context(state: AutoresearchState) -> dict[str, object]:
    return {
        "iteration": state.iteration,
        "suspended": state.suspended,
        "suspension_reason": state.suspension_reason,
        "setup": state.setup.to_dict() if state.setup else None,
        "context_packet": state.context_packet.to_dict() if state.context_packet else None,
        "latest_debate": state.latest_debate.to_dict() if state.latest_debate else None,
        "latest_consensus": state.latest_consensus.to_dict() if state.latest_consensus else None,
        "implementation_result": state.implementation_result.to_dict()
        if state.implementation_result
        else None,
        "latest_verification": state.latest_verification.to_dict()
        if state.latest_verification
        else None,
        "latest_review": state.latest_review.to_dict() if state.latest_review else None,
        "latest_fix": state.latest_fix.to_dict() if state.latest_fix else None,
        "pending_fix_trigger": state.pending_fix_trigger.value
        if state.pending_fix_trigger is not None
        else None,
        "final_decision": state.final_decision.to_dict() if state.final_decision else None,
    }


def _write_active_mempalace_facts(
    kg_path: Path,
    *,
    subject: str,
    facts: Mapping[str, str],
    object_overrides: Mapping[str, str] | None = None,
) -> None:
    overrides = object_overrides if object_overrides is not None else {}
    connection = sqlite3.connect(kg_path)
    connection.executemany(
        "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
        [
            (
                str(index),
                subject,
                predicate,
                overrides.get(predicate, object_value),
                FINAL_MEMORY_SOURCE_FILE,
                "drawer-finalizer",
            )
            for index, (predicate, object_value) in enumerate(facts.items(), start=1)
        ],
    )
    connection.commit()
    connection.close()


def _write_committed_finalization_journal(
    kg_path: Path,
    request: FinalMemoryWriteRequest,
) -> None:
    digest = sha256(
        json.dumps(request.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    path = finalization_journal_path(kg_path.parent, request.experiment_id)
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "drawer_id": "drawer-finalizer",
                "request_sha256": digest,
                "status": "committed",
            }
        ),
        encoding="utf-8",
    )


def _setup_artifact() -> SetupContextArtifact:
    return SetupContextArtifact(
        goal="Find a profitable intraday alpha",
        metric_name="OOS Sharpe net",
        metric_direction=MetricDirection.MAXIMIZE,
        target_repo="/home/dev/repos/quantipy",
        writable_scope="src/quantipy/alpha, notebooks/experiments, tests",
        baseline_summary="Baseline OOS Sharpe net is 0.18.",
        hard_constraints=("No overnight holds", "Use real OHLCV only"),
        data_sources=("qp.prices()", "PostgreSQL OHLCV", "news sentiment"),
    )


def _context_artifact() -> ContextPacketArtifact:
    return ContextPacketArtifact(
        baseline_metric="0.18 OOS Sharpe net",
        current_best_metric="0.22 OOS Sharpe net",
        recent_experiment_outcomes=("T1 discard: leakage", "T2 keep: VWAP mean reversion"),
        prior_findings=("VWAP works better with volume filters",),
        open_proposals=("Test OBV + VWAP interaction",),
        hard_constraints=("2021-2026 coverage", "95% trading days minimum"),
        available_data_sources=("qp.prices()", "Reddit sentiment", "news sentiment"),
        loaded_quantipy_sources=("AGENTS.md", "experiment-data", "data-querying"),
        research_mode=ResearchMode.ALPHA_RESEARCH,
        mode_rationale="Coverage and provenance evidence permit an alpha experiment.",
        burned_theory_families=(),
    )


def _universe_plan() -> UniversePlanArtifact:
    return UniversePlanArtifact(
        profile_id="liquid-common-stocks-v1",
        profile_digest="a" * 64,
        selection_dates=("2021-01-04",),
        max_members_per_date=300,
        execution_policy="next-session-or-later",
    )


def _universe_verification_receipt() -> UniverseVerificationReceipt:
    return UniverseVerificationReceipt(
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
                        calendar_digest="f" * 64,
                        selected_member_count=1,
                        snapshot=AuthoritativeSnapshotReceipt(
                            as_of_date="2021-01-04",
                            source="massive",
                            result_count=17,
                            identity_digest="c" * 64,
                            content_digest="c" * 64,
                            completed_at="2026-07-15T12:00:00+00:00",
                        ),
                        summary=GroupedSummaryReceipt(
                            summary_date="2021-01-04",
                            source="massive",
                            result_count=17,
                            identity_digest="d" * 64,
                            content_digest="d" * 64,
                            completed_at="2026-07-15T12:00:00+00:00",
                            adjusted=False,
                        ),
                    ),
                ),
            ),
        ),
        member_union_digest_algorithm=MEMBER_UNION_DIGEST_ALGORITHM,
        member_union_count=1,
        member_union_digest=_MEMBER_UNION_DIGEST,
        member_union_manifest=MemberUnionManifestReceipt(
            path=str(_MEMBER_UNION_PATH), sha256=_MEMBER_UNION_SHA256
        ),
    )


def _price_hydration_receipt() -> PriceHydrationReceipt:
    request_digest = price_hydration_request_digest(
        member_union_count=1,
        member_union_digest=_MEMBER_UNION_DIGEST,
        experiment_start="2021-01-05",
        experiment_end="2021-01-05",
        timeframe="1min",
        market_hours="regular",
    )
    completed_at = "2026-07-15T12:00:00+00:00"
    return PriceHydrationReceipt(
        member_union_count=1,
        member_union_digest=_MEMBER_UNION_DIGEST,
        experiment_start="2021-01-05",
        experiment_end="2021-01-05",
        timeframe="1min",
        market_hours="regular",
        operation_count=1,
        request_digest=request_digest,
        coverage_receipt_digest=price_hydration_coverage_digest(
            request_digest=request_digest, operation_count=1, completed_at=completed_at
        ),
        source_price_coverage_response_digest=_SOURCE_PRICE_COVERAGE_RESPONSE_DIGEST,
        completed_at=completed_at,
        folds_started_at="2026-07-15T12:01:00+00:00",
    )


def _dynamic_coverage_receipt() -> DynamicUniverseCoverageReceipt:
    return DynamicUniverseCoverageReceipt(
        member_union_count=1,
        member_union_digest=_MEMBER_UNION_DIGEST,
        experiment_start="2021-01-05",
        experiment_end="2021-01-05",
        oos_start="2021-01-05",
        oos_end="2021-01-05",
        timeframe="1min",
        market_hours="regular",
        expected_symbol_sessions=1,
        covered_symbol_sessions=1,
        missing_symbol_count=0,
        missing_symbol_sessions=0,
        default_fold_count=24,
        fallback_fold_count=0,
    )


def _coverage_receipt() -> AggregateCoverageReceipt:
    per_symbol = CoverageReceipt(
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
        cap_provenance_available=True,
        fixed_sleeve_local_data=False,
    )
    return AggregateCoverageReceipt(
        declared_intended_start=per_symbol.declared_intended_start,
        declared_intended_end=per_symbol.declared_intended_end,
        actual_common_start=per_symbol.actual_common_start,
        actual_common_end=per_symbol.actual_common_end,
        oos_start=per_symbol.oos_start,
        oos_end=per_symbol.oos_end,
        expected_trading_days=per_symbol.expected_trading_days,
        actual_trading_days=per_symbol.actual_trading_days,
        coverage_percent=per_symbol.coverage_percent,
        missing_reason=None,
        default_fold_count=0,
        fallback_fold_count=0,
        cap_provenance_available=True,
        fixed_sleeve_local_data=False,
        per_symbol=(per_symbol,),
    )


def _debate_result(policy: AutoresearchPolicy, round_number: int) -> DebateResultArtifact:
    submissions: list[DebateSubmission] = []
    for index, agent_id in enumerate(policy.debate_agent_ids, start=1):
        submissions.append(
            DebateSubmission(
                agent_id=agent_id,
                theory_id=f"theory-{round_number}-{index}",
                theory_family="vwap-obv",
                vote_family="vwap-obv" if index <= 3 else "bollinger-regime",
                hypothesis="VWAP + OBV captures intraday accumulation regimes.",
                universe="4-6 small-cap semis",
                example_tickers=("AMD", "SMCI"),
                feature_pipeline="OHLCV -> VWAP/OBV/Bollinger -> classifier",
                model_plan="HistGradientBoosting with time-series tuning",
                walk_forward_plan="Expanding walk-forward with monthly test blocks",
                transaction_cost_model="0.7 bps spread + slippage",
                data_coverage_plan="Use 2021-2026 and >=95% of trading days",
                rejection_criteria="Discard if OOS Sharpe net <= baseline",
                objections=("Signal may be regime-specific",),
                compute_fit=ComputeFitArtifact(
                    target=ComputeTarget.CPU,
                    rationale=(
                        "The tabular feature set is small and the existing CPU stack is "
                        "reproducible."
                    ),
                    required_dependencies=(),
                    benchmark_plan=(
                        "Record wall time and peak memory for the full walk-forward run."
                    ),
                ),
            )
        )
    return DebateResultArtifact(round_number=round_number, submissions=tuple(submissions))


def _majority_consensus(
    round_number: int,
    policy: AutoresearchPolicy,
) -> ConsensusResultArtifact:
    return ConsensusResultArtifact(
        round_number=round_number,
        status=ConsensusStatus.MAJORITY,
        winner_theory_id=f"theory-{round_number}-1",
        winner_theory_family="vwap-obv",
        majority_count=3,
        majority_agent_ids=policy.debate_agent_ids[:3],
        dissenting_positions=("Prefer Bollinger regime filter", "Risk of microstructure decay"),
        novelty_score=0.62,
        theory_score=0.71,
        implementation_risk_score=0.33,
        data_adequacy_score=0.9,
        overfit_risk_score=0.27,
        expected_net_sharpe=0.54,
        rejection_reasons=("Losers lacked coverage plan",),
        implementation_brief="Implement VWAP + OBV with walk-forward tuning.",
        dissent_summary="Two dissenters preferred a simpler regime filter.",
        universe_plan=_universe_plan(),
    )


def _operator_precondition_consensus(
    round_number: int,
    policy: AutoresearchPolicy,
) -> ConsensusResultArtifact:
    return ConsensusResultArtifact(
        round_number=round_number,
        status=ConsensusStatus.MAJORITY,
        winner_theory_id="i26-operator-evidence-precondition",
        winner_theory_family="no-code-operator-evidence-precondition",
        majority_count=5,
        majority_agent_ids=policy.debate_agent_ids,
        dissenting_positions=(),
        novelty_score=1.0,
        theory_score=9.0,
        implementation_risk_score=1.0,
        data_adequacy_score=1.0,
        overfit_risk_score=1.0,
        expected_net_sharpe=0.0,
        rejection_reasons=("Missing immutable operator evidence bundle.",),
        implementation_brief=(
            "Do not enter ENGINEER and do not modify Quantipy. The operator "
            "must first supply the immutable Quantipy/XNYS bundle manifest."
        ),
        dissent_summary="All agents agree this is an operator precondition.",
    )


def _no_consensus(round_number: int) -> ConsensusResultArtifact:
    return ConsensusResultArtifact(
        round_number=round_number,
        status=ConsensusStatus.NO_CONSENSUS,
        winner_theory_id=None,
        winner_theory_family=None,
        majority_count=2,
        majority_agent_ids=("debater_microstructure", "debater_data"),
        dissenting_positions=("Theory split across three families",),
        novelty_score=0.45,
        theory_score=0.49,
        implementation_risk_score=0.52,
        data_adequacy_score=0.83,
        overfit_risk_score=0.44,
        expected_net_sharpe=0.21,
        rejection_reasons=("No 3-of-5 majority",),
        implementation_brief=None,
        dissent_summary="Panel split between VWAP, Bollinger, and sentiment families.",
    )


def _implementation_result() -> ImplementationResultArtifact:
    return ImplementationResultArtifact(
        summary="Added strategy module and experiment notebook.",
        workspace_path=str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT / "iteration-1"),
        commit_sha="abc1234",
        module_path="src/quantipy/alpha/vwap_obv_intraday/",
        notebook_path="notebooks/experiments/vwap_obv_intraday.ipynb",
        tests_added_or_updated=("tests/test_vwap_obv.py",),
        commands_run=("uv run pytest tests/test_vwap_obv.py",),
        experiment_manifest_path=str(
            DEFAULT_AUTORESEARCH_WORKTREE_ROOT / "iteration-1" / "experiment-manifest.json"
        ),
        experiment_manifest_sha256="a" * 64,
        compute_fit=ComputeFitArtifact(
            target=ComputeTarget.CPU,
            rationale=(
                "The tabular feature set is small and the existing CPU stack is reproducible."
            ),
            required_dependencies=(),
            benchmark_plan="Record wall time and peak memory for the full walk-forward run.",
        ),
        price_hydration_scope_preflight=PriceHydrationScopePreflight(
            member_union_count=1,
            experiment_start="2021-01-05",
            experiment_end="2021-01-05",
            timeframe="1min",
            market_hours="regular",
            session_count=1,
            planned_symbol_sessions=1,
            within_budget=True,
        ),
    )


def _platform_coverage_receipt(
    *,
    status: PlatformCoverageStatus = PlatformCoverageStatus.COMPLETE,
    scope: PlatformCoverageScope = PlatformCoverageScope.FULL_UNION_HYDRATION,
    source_contract_version: str = "price-coverage-v1",
    member_union_digest: str | None = None,
    requested_sessions_digest: str | None = None,
    pit_active_roster_digest: str | None = None,
    source_price_coverage_response_digest: str | None = None,
) -> DynamicPriceCoverageReceipt:
    preflight = _implementation_result().price_hydration_scope_preflight
    assert preflight is not None
    missing = 1 if status is PlatformCoverageStatus.REMEDIATION_REQUIRED else 0
    raw: dict[str, object] = {
        "contract_version": "dynamic-price-coverage-v1",
        "source_contract_version": source_contract_version,
        "scope": scope.value,
        "status": status.value,
        "requested_start_date": preflight.experiment_start,
        "requested_end_date": preflight.experiment_end,
        "timeframe": preflight.timeframe,
        "market_hours": preflight.market_hours,
        "source_requested_start_date": preflight.experiment_start,
        "source_requested_end_date": preflight.experiment_end,
        "source_timeframe": preflight.timeframe,
        "source_market_hours": preflight.market_hours,
        "source_provider": "massive",
        "member_union_digest": member_union_digest
        or autoresearch_runner.quantipy_member_union_digest(("AMD",))[1],
        "requested_sessions_digest": requested_sessions_digest
        or autoresearch_runner.platform_requested_sessions_digest((date(2021, 1, 5),)),
        "pit_active_roster_digest": pit_active_roster_digest or "c" * 64,
        "source_price_coverage_response_digest": (
            source_price_coverage_response_digest
            or _price_hydration_receipt().source_price_coverage_response_digest
        ),
        "member_union_count": preflight.member_union_count,
        "requested_session_count": preflight.session_count,
        "hydrated_symbol_sessions": preflight.planned_symbol_sessions,
        "observed_hydrated_symbol_sessions": preflight.planned_symbol_sessions - missing,
        "provider_empty_hydrated_symbol_sessions": 0,
        "missing_hydrated_symbol_sessions": missing,
        "active_symbol_sessions": preflight.planned_symbol_sessions,
        "observed_active_symbol_sessions": preflight.planned_symbol_sessions - missing,
        "provider_empty_active_symbol_sessions": 0,
        "missing_active_symbol_sessions": missing,
        "inactive_union_symbol_sessions": 0,
        "unexpected_ticker_count": 0,
        "unexpected_session_count": 0,
        "violation_codes": (
            [
                PlatformCoverageViolationCode.MISSING_ACTIVE_SYMBOL_SESSION.value,
                PlatformCoverageViolationCode.MISSING_HYDRATED_SYMBOL_SESSION.value,
            ]
            if missing
            else []
        ),
    }
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)
    return DynamicPriceCoverageReceipt.from_dict(raw)


def _verification_result(
    status: VerificationStatus,
    *,
    external_panel_failure: bool = False,
) -> VerificationResultArtifact:
    bug_signals = ("Sharpe > 10",) if status is VerificationStatus.BUG_SIGNAL else ()
    return VerificationResultArtifact(
        status=status,
        is_walk_forward_sharpe_net=0.41,
        oos_sharpe_net=0.38,
        max_drawdown_pct=12.4,
        win_rate=0.54,
        trade_count=211,
        trades_per_day=1.9,
        oos_trading_days=128,
        feature_importances_summary="VWAP distance and OBV slope dominate.",
        null_test_summary="Null shuffle drops Sharpe near zero.",
        bug_signals=bug_signals,
        tests_passed=status is not VerificationStatus.TEST_FAILURE,
        commands_run=("uv run pytest", "uv run jupyter execute notebook.ipynb"),
        data_coverage=_dynamic_coverage_receipt(),
        platform_coverage_validation=_platform_coverage_receipt(),
        universe_verification_receipt=_universe_verification_receipt(),
        price_hydration_receipt=_price_hydration_receipt(),
        quantipy_experiment_evidence=QuantipyExperimentEvidence(
            manifest_path=str(
                DEFAULT_AUTORESEARCH_WORKTREE_ROOT / "iteration-1" / "experiment-manifest.json"
            ),
            manifest_sha256="a" * 64,
            detached_run_directory="/tmp/autoresearch-runs/iteration-1/verification/attempt-1",
            detached_run_manifest_sha256="c" * 64,
            run_id="autoresearch-i1-abc1234",
            run_json_path="/tmp/quantipy-runtime/autoresearch-i1-abc1234/run.json",
            run_json_sha256="b" * 64,
            success=status is VerificationStatus.PASS,
            completed_stages=("prepare", "smoke", "feasibility", "model")
            if status is VerificationStatus.PASS
            else (() if external_panel_failure else ("prepare",)),
            terminal_stage=(
                None if status is VerificationStatus.PASS or external_panel_failure else "smoke"
            ),
            terminal_status=(
                None if status is VerificationStatus.PASS or external_panel_failure else "rejected"
            ),
            failure=(
                QuantipyExperimentFailureEvidence(
                    category="panel",
                    message=(
                        "ExperimentPanelError: Client error '413 Request Entity Too Large' for url "
                        "'http://127.0.0.1:8000/price-data/research-panel?tickers=AAPL'\n"
                        "For more information check: "
                        "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/413"
                    ),
                )
                if external_panel_failure
                else None
            ),
            panel=None,
        ),
    )


def _g0_remediation_verification(
    status: VerificationStatus,
) -> VerificationResultArtifact:
    return replace(
        _verification_result(status),
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
        infra_rationale="Verification did not complete successfully.",
        platform_coverage_validation=_platform_coverage_receipt(
            status=PlatformCoverageStatus.REMEDIATION_REQUIRED
        ),
    )


def _g0_platform_contract_mismatch_bug_signal() -> VerificationResultArtifact:
    return replace(
        _verification_result(VerificationStatus.BUG_SIGNAL),
        bug_signals=("platform_coverage_contract_mismatch",),
        infra_gate_outcome=None,
        infra_rationale=None,
        data_coverage=None,
        platform_coverage_validation=None,
        universe_verification_receipt=None,
        price_hydration_receipt=None,
    )


def _review_result(verdict: ReviewVerdict, policy: AutoresearchPolicy) -> ReviewResultArtifact:
    critical_issues = ("Need wider ticker coverage",) if verdict is ReviewVerdict.FAIL else ()
    fix_requests = ("Expand coverage and rerun notebook",) if critical_issues else ()
    return ReviewResultArtifact(
        reviewer_agent_id=policy.reviewer.agent_id,
        verdict=verdict,
        recommended_metric_name="OOS Sharpe net",
        recommended_metric_value=0.38,
        critical_issues=critical_issues,
        noncritical_issues=("Document feature importance caveat",),
        fix_requests=fix_requests,
        summary="Methodology review complete.",
    )


def _fix_result(
    trigger_phase: FixTriggerPhase,
    *,
    price_hydration_scope_preflight: PriceHydrationScopePreflight | None = None,
) -> FixResultArtifact:
    return FixResultArtifact(
        trigger_phase=trigger_phase,
        summary="Applied the requested narrow fix.",
        workspace_path=str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT / "iteration-1"),
        commit_sha="def5678",
        fixes_applied=("Expanded ticker coverage to 5 names",),
        tests_rerun=("uv run pytest",),
        remaining_issues=(),
        price_hydration_scope_preflight=price_hydration_scope_preflight,
    )


def _final_decision() -> FinalDecisionArtifact:
    return FinalDecisionArtifact(
        experiment_id="iteration-1",
        decision=FinalDecision.KEEP,
        recommended_metric_name="OOS Sharpe net",
        recommended_metric_value=0.38,
        reviewer_verdict=FinalReviewerVerdict.PASS,
        rationale="Improves baseline without review blockers.",
        log_summary="KEEP vwap_obv_intraday with updated baseline review.",
        continue_loop=True,
        memory_write_required=True,
    )


def _final_decision_with(
    *,
    decision: FinalDecision,
    metric_value: float | None,
    reviewer_verdict: FinalReviewerVerdict,
) -> FinalDecisionArtifact:
    artifact = _final_decision()
    return replace(
        artifact,
        decision=decision,
        recommended_metric_value=metric_value,
        reviewer_verdict=reviewer_verdict,
    )


def _state_to_consensus(
    policy: AutoresearchPolicy,
    readiness: PlatformReadinessManifest | None = None,
) -> AutoresearchState:
    state = AutoresearchState(
        platform_readiness=readiness.identity() if readiness is not None else None
    )
    state = advance_state(state, _setup_artifact(), policy)
    state = advance_state(state, _context_artifact(), policy)
    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    return state


def _state_to_review(
    policy: AutoresearchPolicy,
    readiness: PlatformReadinessManifest | None = None,
) -> AutoresearchState:
    state = _state_to_consensus(policy, readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.PASS), policy)
    return state


def _state_to_decision(
    policy: AutoresearchPolicy,
    readiness: PlatformReadinessManifest | None = None,
) -> AutoresearchState:
    state = _state_to_review(policy, readiness)
    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)
    return state


def _state_to_g0_decision(
    policy: AutoresearchPolicy,
    *,
    readiness: PlatformReadinessManifest | None = None,
    infra_gate_outcome: InfraGateOutcome,
) -> AutoresearchState:
    state = AutoresearchState(
        platform_readiness=readiness.identity() if readiness is not None else None
    )
    state = advance_state(state, _setup_artifact(), policy)
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
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            infra_gate_outcome=infra_gate_outcome,
            infra_rationale="Cap/source provenance still needs operator remediation.",
            platform_coverage_validation=_platform_coverage_receipt(
                status=(
                    PlatformCoverageStatus.COMPLETE
                    if infra_gate_outcome is InfraGateOutcome.GATE_PASSED
                    else PlatformCoverageStatus.REMEDIATION_REQUIRED
                )
            ),
        ),
        policy,
    )
    return advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)


def _bind_universe_receipt_to_validation_context(
    receipt: UniverseVerificationReceipt,
    context: AutoresearchValidationContext,
) -> UniverseVerificationReceipt:
    return replace(
        receipt,
        batches=tuple(
            replace(
                batch,
                dates=tuple(
                    replace(date_receipt, calendar_digest=context.xnys_evidence_digest)
                    for date_receipt in batch.dates
                ),
            )
            for batch in receipt.batches
        ),
    )


def _persisted_g0_infra_repaired_repeat_state(
    policy: AutoresearchPolicy,
    readiness: PlatformReadinessManifest,
) -> tuple[AutoresearchState, AutoresearchValidationContext]:
    state = _state_to_g0_decision(
        policy,
        readiness=readiness,
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
    )
    state = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="g0-iteration-1",
            decision=FinalDecision.INFRA_REPAIRED,
            recommended_metric_name="coverage gate",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Data repair completed.",
            log_summary="G0 gate passed.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Cap/source provenance is now present for the declared sleeve.",
        ),
        policy,
    )
    context = AutoresearchValidationContext.from_readiness(readiness)
    verification = state.latest_verification
    assert verification is not None
    universe = verification.universe_verification_receipt
    assert universe is not None
    bound_verification = replace(
        verification,
        universe_verification_receipt=_bind_universe_receipt_to_validation_context(
            universe,
            context,
        ),
    )
    persisted = replace(
        state,
        verification_history=(*state.verification_history[:-1], bound_verification),
    )
    return persisted, context


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        cwd=cwd,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class GitWorktree:
    target_checkout: Path
    workspace: Path
    implementation_commit: str
    final_commit: str


def _workspace_setup(target_checkout: Path) -> SetupContextArtifact:
    return replace(_setup_artifact(), target_repo=str(target_checkout))


def _implementation_artifact(worktree: GitWorktree) -> ImplementationResultArtifact:
    return replace(
        _implementation_result(),
        workspace_path=str(worktree.workspace),
        commit_sha=worktree.implementation_commit,
    )


def _prepare_real_canonical_runtime(worktree: GitWorktree) -> str:
    runtime_root = worktree.target_checkout
    readiness_commit = _git(
        runtime_root,
        "merge-base",
        "HEAD",
        worktree.implementation_commit,
    )
    (runtime_root / "src" / "quantipy").mkdir(parents=True)
    (runtime_root / "src" / "quantipy" / "__init__.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    (runtime_root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                'name = "quantipy"',
                'version = "0.0.0"',
                'requires-python = ">=3.11"',
                "",
            )
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ("uv", "--directory", str(runtime_root), "lock"),
        check=True,
        capture_output=True,
        text=True,
    )
    external_python, _, _ = autoresearch_runner._probe_quantipy_runtime_resolution(
        Path("/home/dev/repos/quantipy")
    )
    subprocess.run(
        (
            "uv",
            "venv",
            "--python",
            str(external_python),
            str(runtime_root / ".venv"),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    python = runtime_root / ".venv" / "bin" / "python"
    site_packages = Path(
        subprocess.check_output(
            (
                str(python),
                "-c",
                "import site; print(site.getsitepackages()[0])",
            ),
            text=True,
        ).strip()
    )
    (site_packages / "quantipy-test-runtime.pth").write_text(
        str(runtime_root / "src") + "\n",
        encoding="utf-8",
    )
    entrypoint = runtime_root / ".venv" / "bin" / "quantipy"
    entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entrypoint.chmod(0o775)
    _git(runtime_root, "add", "pyproject.toml", "uv.lock", "src/quantipy/__init__.py")
    _git(runtime_root, "commit", "-m", "canonical runtime")
    return readiness_commit


def _fix_artifact(worktree: GitWorktree) -> FixResultArtifact:
    return replace(
        _fix_result(FixTriggerPhase.VERIFICATION),
        workspace_path=str(worktree.workspace),
        commit_sha=worktree.final_commit,
    )


def _write_quantipy_v2_run(
    worktree: GitWorktree,
    *,
    success: bool = True,
    terminal_stage: str = "model",
    terminal_status: str = "completed",
    run_root: Path | None = None,
    panel_requested: bool = False,
    manifest_parent: Path = Path(),
    notebook_requested: bool = False,
    extra_source_file_count: int = 0,
) -> tuple[str, str, Path, str, str, str]:
    canonical_head = _git(worktree.target_checkout, "rev-parse", "HEAD")
    _git(
        worktree.workspace,
        "fetch",
        str(worktree.target_checkout),
        f"{canonical_head}:refs/autoresearch-fixtures/canonical-head",
    )
    _git(worktree.workspace, "checkout", "--detach", canonical_head)
    source_root = worktree.workspace / manifest_parent
    source_root.mkdir(parents=True, exist_ok=True)
    current_directory = source_root
    while current_directory != worktree.workspace:
        current_directory.chmod(0o755)
        current_directory = current_directory.parent
    package = source_root / "experiment"
    package.mkdir()
    package.chmod(0o755)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").chmod(0o644)
    for stage in ("prepare", "smoke", "feasibility", "model"):
        stage_path = package / f"{stage}.py"
        stage_path.write_text(
            "def run(context):\n    return context.accept('accepted')\n",
            encoding="utf-8",
        )
        stage_path.chmod(0o644)
    if extra_source_file_count:
        helper_modules: list[str] = []
        for index in range(extra_source_file_count):
            module_name = f"helper_{index:03d}_" + ("x" * 150)
            helper_modules.append(module_name)
            helper_path = package / f"{module_name}.py"
            helper_path.write_text(f"VALUE = {index}\n", encoding="utf-8")
            helper_path.chmod(0o644)
        prepare_path = package / "prepare.py"
        imports = "".join(f"from . import {module}\n" for module in helper_modules)
        prepare_path.write_text(
            f"{imports}\ndef run(context):\n    return context.accept('accepted')\n",
            encoding="utf-8",
        )
    manifest: dict[str, object] = {
        "schema_version": "quantipy-experiment-v2",
        "experiment_id": "gateway-runtime-audit",
        "package_path": "experiment",
        "stage_files": [
            {
                "name": stage,
                "file_path": f"{stage}.py",
                "entrypoint": f"experiment.{stage}:run",
            }
            for stage in ("prepare", "smoke", "feasibility", "model")
        ],
    }
    panel_request: dict[str, object] | None = None
    if notebook_requested:
        notebook = source_root / "report.ipynb"
        notebook.write_text(
            '{"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5}',
            encoding="utf-8",
        )
        notebook.chmod(0o644)
        manifest["notebook_path"] = "report.ipynb"
    if panel_requested:
        request: dict[str, object] = {
            "contract_version": "research-price-panel-v1",
            "tickers": ["AMD"],
            "start": "2026-07-28T12:00:00Z",
            "end": "2026-07-28T13:00:00Z",
            "timeframe": "1min",
            "market_hours": "all",
        }
        panel_request = {"api_url": "http://127.0.0.1:8000", "request": request}
        manifest["panel"] = panel_request
    manifest_path = source_root / "experiment-manifest.json"
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o644)
    _git(worktree.workspace, "add", manifest_parent.as_posix() or ".")
    _git(worktree.workspace, "commit", "-m", "add v2 experiment runtime")
    commit_sha = _git(worktree.workspace, "rev-parse", "HEAD")
    _git(
        worktree.target_checkout,
        "fetch",
        str(worktree.workspace),
        f"{commit_sha}:refs/autoresearch-fixtures/{commit_sha}",
    )

    ordered_stages = ("prepare", "smoke", "feasibility", "model")
    entered_stages = ordered_stages[: ordered_stages.index(terminal_stage) + 1]
    source_files = []
    for source_path in sorted(package.rglob("*.py")):
        source_bytes = source_path.read_bytes()
        source_files.append(
            {
                "path": source_path.relative_to(source_root).as_posix(),
                "sha256": sha256(source_bytes).hexdigest(),
                "size_bytes": len(source_bytes),
            }
        )
    source_digest = sha256(
        json.dumps(
            {
                "domain": "quantipy-experiment-source-v1",
                "files": source_files,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    source_evidence: dict[str, object] = {
        "algorithm": "sha256",
        "domain": "quantipy-experiment-source-v1",
        "files": source_files,
        "total_bytes": sum(cast(int, item["size_bytes"]) for item in source_files),
        "sha256": source_digest,
    }
    stage_receipts = []
    for stage_index, stage in enumerate(entered_stages):
        status = terminal_status if stage == terminal_stage else "completed"
        receipt: dict[str, object] = {
            "stage": stage,
            "status": status,
            "started_at": f"2026-07-28T12:00:0{stage_index}Z",
            "completed_at": f"2026-07-28T12:00:0{stage_index + 1}Z",
            "wall_seconds": 1.0,
            "result": None,
            "failure": None,
        }
        if status == "completed":
            receipt["result"] = {
                "stage": stage,
                "decision": "accepted",
                "summary": "accepted",
            }
        elif status == "rejected":
            receipt["result"] = {
                "stage": stage,
                "decision": "rejected",
                "summary": "rejected",
            }
        else:
            receipt["failure"] = {"category": "stage", "message": "model failed"}
        stage_receipts.append(receipt)
    run_id = "autoresearch-i1-" + commit_sha[:12]
    run_panel: dict[str, object] | None = None
    panel_bytes = b"strict-panel-bytes"
    receipt_bytes: bytes | None = None
    if panel_requested:
        assert panel_request is not None
        request = cast(dict[str, object], panel_request["request"])
        coverage: dict[str, object] = {
            "contract_version": "price-coverage-v1",
            "requested_start_date": "2026-07-28",
            "requested_end_date": "2026-07-28",
            "timeframe": "1min",
            "market_hours": "all",
            "provider_source": "massive",
            "tickers": [
                {
                    "ticker": "AMD",
                    "sessions": [
                        {
                            "session_date": "2026-07-28",
                            "coverage_state": "observed",
                            "mode_bar_count": 1,
                            "provider_request_id": "panel-request",
                            "provider_http_status": 200,
                            "provider_query_count": 1,
                            "provider_results_count": 1,
                            "provider_requested_start": "2026-07-28T13:30:00Z",
                            "provider_requested_end": "2026-07-28T20:00:00Z",
                            "hydrated_at": "2026-07-28T21:00:00Z",
                        }
                    ],
                }
            ],
        }
        request_sha = sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        coverage_sha = sha256(
            json.dumps(coverage, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        expanded_coverage = json.dumps(
            coverage, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        compressed_coverage = zlib.compress(expanded_coverage, level=zlib.Z_BEST_COMPRESSION)
        compact_coverage = {
            "contract_version": "price-coverage-compact-v1",
            "encoding": "canonical-json-zlib-base64-v1",
            "compressed_size": len(compressed_coverage),
            "expanded_size": len(expanded_coverage),
            "compression_ratio": len(expanded_coverage) / len(compressed_coverage),
            "coverage_sha256": coverage_sha,
            "payload": base64.b64encode(compressed_coverage).decode("ascii"),
        }
        receipt = {
            "contract_version": "research-price-panel-receipt-v2",
            "request": request,
            "request_sha256": request_sha,
            "coverage": compact_coverage,
            "coverage_sha256": coverage_sha,
            "panel_sha256": sha256(panel_bytes).hexdigest(),
            "hydrated_at": "2026-07-28T13:00:00Z",
            "exported_at": "2026-07-28T13:00:01Z",
        }
        receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        run_panel = {
            "panel_path": "panel/panel.parquet",
            "panel_sha256": sha256(panel_bytes).hexdigest(),
            "receipt_path": "panel/receipt.json",
            "receipt_sha256": sha256(receipt_bytes).hexdigest(),
            "request_sha256": request_sha,
            "coverage_sha256": coverage_sha,
            "receipt": receipt,
        }
    run: dict[str, object] = {
        "run_id": run_id,
        "identity": {
            "experiment_id": manifest["experiment_id"],
            "package_path": str(package),
            "notebook_path": (str(source_root / "report.ipynb") if notebook_requested else None),
        },
        "manifest_sha256": sha256(
            json.dumps(
                {
                    **manifest,
                    "notebook_path": manifest.get("notebook_path"),
                    "panel": manifest.get("panel"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "source": source_evidence,
        "success": success,
        "panel_requested": panel_requested,
        "panel": run_panel,
        "stage_receipts": stage_receipts,
        "telemetry": {
            "scope": "process_wide",
            "started_at": "2026-07-28T12:00:00Z",
            "completed_at": f"2026-07-28T12:00:0{len(entered_stages)}Z",
            "wall_seconds": float(len(entered_stages)),
        },
        "failure": None,
    }
    run_root = run_root or worktree.workspace.parent / "runtime-receipts"
    run_root.mkdir(parents=True, exist_ok=True)
    run_root.chmod(0o700)
    run_path = run_root / run_id / "run.json"
    run_path.parent.mkdir(parents=True)
    run_path.parent.chmod(0o700)
    if panel_requested:
        panel_directory = run_path.parent / "panel"
        panel_directory.mkdir(mode=0o700)
        panel_path = panel_directory / "panel.parquet"
        panel_path.write_bytes(panel_bytes)
        panel_path.chmod(0o400)
        assert receipt_bytes is not None
        receipt_path = panel_directory / "receipt.json"
        receipt_path.write_bytes(receipt_bytes)
        receipt_path.chmod(0o400)
        panel_directory.chmod(0o500)
    run_bytes = json.dumps(run, sort_keys=True, separators=(",", ":")).encode()
    run_path.write_bytes(run_bytes)
    run_path.chmod(0o600)
    return (
        str(manifest_path),
        sha256(manifest_bytes).hexdigest(),
        run_path,
        sha256(run_bytes).hexdigest(),
        commit_sha,
        run_id,
    )


def _rebind_quantipy_source_inventory(source: dict[str, object]) -> None:
    files = cast(list[dict[str, object]], source["files"])
    files.sort(key=lambda item: cast(str, item["path"]))
    source["total_bytes"] = sum(cast(int, item["size_bytes"]) for item in files)
    source["sha256"] = sha256(
        json.dumps(
            {
                "domain": "quantipy-experiment-source-v1",
                "files": files,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _write_quantipy_detached_run_record(
    *,
    workspace: Path,
    runtime_root: Path | None = None,
    manifest_path: str,
    run_id: str,
    run_path: Path,
    iteration: int = 1,
    detached_run_dir: Path | None = None,
) -> tuple[str, str]:
    detached_root = autoresearch_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
    detached_run_dir = detached_run_dir or (
        detached_root / f"iteration-{iteration}" / "verification" / "attempt-1"
    )
    command = (
        (
            autoresearch_runner._build_historical_v2_quantipy_execution_contract(
                runtime_root=runtime_root,
                manifest_path=Path(manifest_path),
                output_root=autoresearch_runner.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
                run_id=run_id,
            )
            if run_id.endswith("-v2")
            else autoresearch_runner.build_quantipy_execution_contract(
                runtime_root=runtime_root,
                manifest_path=Path(manifest_path),
                output_root=autoresearch_runner.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
                run_id=run_id,
            )
        ).command
        if runtime_root is not None
        else (
            "env",
            "PYTHONDONTWRITEBYTECODE=1",
            "uv",
            "run",
            "quantipy",
            "experiment",
            "run",
            manifest_path,
            "--output-root",
            str(autoresearch_runner.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT),
            "--run-id",
            run_id,
        )
    )
    manifest = {
        "schema_version": 1,
        "iteration": iteration,
        "phase": "verification",
        "attempt": 1,
        "task_label": "quantipy-verification",
        "state_reference_sha256": "a" * 64,
        "instruction_manifest_sha256": "b" * 64,
        "run_directory": str(detached_run_dir),
        "working_directory": str(runtime_root if runtime_root is not None else workspace),
        "command_sha256": autoresearch_runs.command_sha256(command),
        "expected_artifact_path": str(run_path),
        "timeout_seconds": None,
    }
    manifest_path_input = workspace.parent / f"{run_id}-detached-manifest.json"
    manifest_path_input.write_text(json.dumps(manifest), encoding="utf-8")
    prepared = autoresearch_runs.prepare_run(
        manifest_path=manifest_path_input,
        run_dir=detached_run_dir,
        runs_root=detached_root,
        command=command,
    )
    autoresearch_runs.prepare_output_capture(
        run_dir=detached_run_dir,
        runs_root=detached_root,
    )
    autoresearch_runs.start_run(
        run_dir=detached_run_dir,
        pid=123,
        runs_root=detached_root,
    )
    for stream in autoresearch_runs.RunOutputStream:
        autoresearch_runs.capture_output_stream(
            run_dir=detached_run_dir,
            runs_root=detached_root,
            stream=stream,
            source=BytesIO(b""),
        )
    run_success = cast(bool, json.loads(run_path.read_text(encoding="utf-8"))["success"])
    autoresearch_runs.complete_run(
        run_dir=detached_run_dir,
        exit_code=0 if run_success else 1,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=detached_root,
    )
    return str(detached_run_dir), prepared.manifest_sha256


def _rebind_test_detached_artifact_attestation(
    evidence: QuantipyExperimentEvidence,
) -> None:
    """Model a worker attesting fixture bytes so parser tests reach their target invariant."""
    run_path = Path(evidence.run_json_path)
    run_bytes = run_path.read_bytes()
    run_path.chmod(0o400)
    file_stat = run_path.stat()
    detached_run_directory = Path(evidence.detached_run_directory)
    detached_run_directory.chmod(0o700)
    status_path = detached_run_directory / "status.json"
    status_path.chmod(0o600)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    run_success = cast(bool, json.loads(run_bytes)["success"])
    if not run_success:
        status.update(
            state="failed",
            exit_code=1,
            signal_number=None,
            failure_classification="process_error",
        )
    status["expected_artifact_attestation"] = {
        "path": str(run_path),
        "size_bytes": len(run_bytes),
        "sha256": sha256(run_bytes).hexdigest(),
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mtime_ns": file_stat.st_mtime_ns,
        "ctime_ns": file_stat.st_ctime_ns,
    }
    status_path.write_text(
        json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    status_path.chmod(0o400)
    detached_run_directory.chmod(0o500)


def _rewrite_test_detached_status(
    evidence: QuantipyExperimentEvidence,
    **updates: object,
) -> None:
    detached_run_directory = Path(evidence.detached_run_directory)
    status_path = detached_run_directory / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.update(updates)
    detached_run_directory.chmod(0o700)
    status_path.chmod(0o600)
    status_path.write_text(
        json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    status_path.chmod(0o400)
    detached_run_directory.chmod(0o500)


def _runtime_verification_context(state: AutoresearchState) -> AutoresearchValidationContext:
    return AutoresearchValidationContext(
        state.platform_readiness,
        "f" * 64,
        (date(2021, 1, 5),),
    )


def _runtime_verification_state(
    git_worktree: GitWorktree,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
    trusted_quantipy_runs_root: Path,
    *,
    success: bool = True,
    terminal_stage: str = "model",
    terminal_status: str = "completed",
    panel_requested: bool = False,
    extra_source_file_count: int = 0,
) -> tuple[AutoresearchState, Path, QuantipyExperimentEvidence]:
    manifest_path, manifest_sha256, run_path, run_sha256, commit_sha, run_id = (
        _write_quantipy_v2_run(
            git_worktree,
            success=success,
            terminal_stage=terminal_stage,
            terminal_status=terminal_status,
            run_root=trusted_quantipy_runs_root,
            panel_requested=panel_requested,
            extra_source_file_count=extra_source_file_count,
        )
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
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    run_panel = run_payload["panel"]
    panel_evidence = (
        QuantipyExperimentPanelEvidence(
            panel_path=run_panel["panel_path"],
            panel_sha256=run_panel["panel_sha256"],
            receipt_path=run_panel["receipt_path"],
            receipt_sha256=run_panel["receipt_sha256"],
            request_sha256=run_panel["request_sha256"],
            coverage_sha256=run_panel["coverage_sha256"],
        )
        if run_panel is not None
        else None
    )
    detached_run_directory, detached_run_manifest_sha256 = _write_quantipy_detached_run_record(
        workspace=git_worktree.workspace,
        runtime_root=git_worktree.target_checkout,
        manifest_path=manifest_path,
        run_id=run_id,
        run_path=run_path,
    )
    evidence = QuantipyExperimentEvidence(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        detached_run_directory=detached_run_directory,
        detached_run_manifest_sha256=detached_run_manifest_sha256,
        run_id=run_id,
        run_json_path=str(run_path),
        run_json_sha256=run_sha256,
        success=success,
        completed_stages=("prepare", "smoke", "feasibility", "model")
        if success
        else ("prepare",)
        if terminal_stage == "smoke"
        else ("prepare", "smoke", "feasibility"),
        terminal_stage=None if success else terminal_stage,
        terminal_status=None if success else terminal_status,
        failure=(
            QuantipyExperimentFailureEvidence(category="stage", message="model failed")
            if terminal_status == "failed"
            else None
        ),
        panel=panel_evidence,
    )
    return state, state_path, evidence
