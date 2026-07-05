from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from gateway.autoresearch_runner import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    QUANTIPY_RECEIPT_PATHS,
    AutoresearchConfigError,
    AutoresearchPolicy,
    AutoresearchReceiptError,
    AutoresearchState,
    AutoresearchValidationError,
    ConsensusResultArtifact,
    ConsensusStatus,
    ContextPacketArtifact,
    DebateResultArtifact,
    DebateSubmission,
    FinalDecision,
    FinalDecisionArtifact,
    FixResultArtifact,
    FixTriggerPhase,
    ImplementationResultArtifact,
    MetricDirection,
    Phase,
    ReceiptCatalog,
    ReviewResultArtifact,
    ReviewVerdict,
    SetupContextArtifact,
    VerificationResultArtifact,
    VerificationStatus,
    advance_state,
    build_receipt_catalog,
    can_write_memory,
    load_autoresearch_policy,
    mark_memory_written,
    next_action,
    start_next_iteration,
    validate_target_worktree_clean,
)


@pytest.fixture()
def policy() -> AutoresearchPolicy:
    return load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)


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
                traded_tickers=("AMD", "SMCI"),
                feature_pipeline="OHLCV -> VWAP/OBV/Bollinger -> classifier",
                model_plan="HistGradientBoosting with time-series tuning",
                walk_forward_plan="Expanding walk-forward with monthly test blocks",
                transaction_cost_model="0.7 bps spread + slippage",
                data_coverage_plan="Use 2021-2026 and >=95% of trading days",
                rejection_criteria="Discard if OOS Sharpe net <= baseline",
                objections=("Signal may be regime-specific",),
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
    )


def _no_consensus(round_number: int) -> ConsensusResultArtifact:
    return ConsensusResultArtifact(
        round_number=round_number,
        status=ConsensusStatus.NO_CONSENSUS,
        winner_theory_id=None,
        winner_theory_family=None,
        majority_count=2,
        majority_agent_ids=("debater-microstructure", "debater-data"),
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
        workspace_path="/tmp/quantipy-autoresearch-worktrees/iteration-1",
        commit_sha="abc1234",
        module_path="src/quantipy/alpha/vwap_obv_intraday/",
        notebook_path="notebooks/experiments/vwap_obv_intraday.ipynb",
        tests_added_or_updated=("tests/test_vwap_obv.py",),
        commands_run=("uv run pytest tests/test_vwap_obv.py",),
    )


def _verification_result(status: VerificationStatus) -> VerificationResultArtifact:
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


def _fix_result(trigger_phase: FixTriggerPhase) -> FixResultArtifact:
    return FixResultArtifact(
        trigger_phase=trigger_phase,
        summary="Applied the requested narrow fix.",
        workspace_path="/tmp/quantipy-autoresearch-worktrees/iteration-1",
        commit_sha="def5678",
        fixes_applied=("Expanded ticker coverage to 5 names",),
        tests_rerun=("uv run pytest",),
        remaining_issues=(),
    )


def _final_decision() -> FinalDecisionArtifact:
    return FinalDecisionArtifact(
        decision=FinalDecision.KEEP,
        recommended_metric_name="OOS Sharpe net",
        recommended_metric_value=0.38,
        reviewer_verdict=ReviewVerdict.PASS.value,
        rationale="Improves baseline without review blockers.",
        log_summary="KEEP vwap_obv_intraday with updated baseline review.",
        continue_loop=True,
        memory_write_required=True,
    )


def _final_decision_with(
    *,
    decision: FinalDecision,
    metric_value: float | None,
    reviewer_verdict: str | None,
) -> FinalDecisionArtifact:
    artifact = _final_decision()
    return replace(
        artifact,
        decision=decision,
        recommended_metric_value=metric_value,
        reviewer_verdict=reviewer_verdict,
    )


def _state_to_consensus(policy: AutoresearchPolicy) -> AutoresearchState:
    state = AutoresearchState()
    state = advance_state(state, _setup_artifact(), policy)
    state = advance_state(state, _context_artifact(), policy)
    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    return state


def _state_to_review(policy: AutoresearchPolicy) -> AutoresearchState:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.PASS), policy)
    return state


def _state_to_decision(policy: AutoresearchPolicy) -> AutoresearchState:
    state = _state_to_review(policy)
    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)
    return state


def test_phase_progression(policy: AutoresearchPolicy, receipts: ReceiptCatalog) -> None:
    state = AutoresearchState()

    action = next_action(state, policy, receipts)
    assert action.phase is Phase.SETUP_CONTEXT
    assert action.next_agent_ids == ("main",)

    state = advance_state(state, _setup_artifact(), policy)
    action = next_action(state, policy, receipts)
    assert action.next_agent_ids == ("context-curator",)

    state = advance_state(state, _context_artifact(), policy)
    assert state.phase is Phase.DEBATE
    assert next_action(state, policy, receipts).next_agent_ids == policy.debate_agent_ids

    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    assert state.phase is Phase.CONSENSUS

    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    assert state.phase is Phase.IMPLEMENTATION

    state = advance_state(state, _implementation_result(), policy)
    assert state.phase is Phase.VERIFICATION

    state = advance_state(state, _verification_result(VerificationStatus.PASS), policy)
    assert state.phase is Phase.REVIEW
    assert next_action(state, policy, receipts).next_agent_ids == (policy.reviewer.agent_id,)

    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)
    assert state.phase is Phase.DECISION_LOG

    state = advance_state(state, _final_decision(), policy)
    assert state.phase is Phase.REPEAT


def test_missing_receipt_file_fails_fast(tmp_path: Path) -> None:
    for receipt_id, relative_path in QUANTIPY_RECEIPT_PATHS.items():
        if receipt_id == "quantipy.skill.data_querying":
            continue
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")

    with pytest.raises(AutoresearchReceiptError, match=r"data-querying/SKILL.md"):
        build_receipt_catalog(tmp_path)


def test_no_majority_allows_one_retry_then_routes_to_decision(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)

    state = advance_state(state, _no_consensus(round_number=1), policy)
    assert state.phase is Phase.DEBATE
    assert state.consensus_retry_count == 1

    state = advance_state(state, _debate_result(policy, round_number=2), policy)
    state = advance_state(state, _no_consensus(round_number=2), policy)
    assert state.phase is Phase.DECISION_LOG


def test_no_implementation_without_majority(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _no_consensus(round_number=1), policy)
    invalid = replace(state, phase=Phase.IMPLEMENTATION)

    with pytest.raises(AutoresearchValidationError, match="majority consensus"):
        next_action(invalid, policy, receipts)

    with pytest.raises(AutoresearchValidationError, match="majority"):
        advance_state(invalid, _implementation_result(), policy)


def test_implementation_result_requires_workspace_identity() -> None:
    with pytest.raises(AutoresearchValidationError, match="workspace_path"):
        ImplementationResultArtifact.from_dict(
            {
                "summary": "Added strategy module and notebook.",
                "module_path": "src/quantipy/alpha/vwap_obv_intraday/",
                "notebook_path": "notebooks/experiments/vwap_obv_intraday.ipynb",
                "tests_added_or_updated": ["tests/test_vwap_obv.py"],
                "commands_run": ["uv run pytest tests/test_vwap_obv.py"],
            }
        )


def test_implementation_result_rejects_main_target_checkout(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    implementation = replace(
        _implementation_result(),
        workspace_path="/home/dev/repos/quantipy",
    )

    with pytest.raises(AutoresearchValidationError, match="isolated worktree"):
        advance_state(state, implementation, policy)


def test_review_fix_cycle_routes_back_through_verification(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = _state_to_review(policy)

    state = advance_state(state, _review_result(ReviewVerdict.FAIL, policy), policy)
    assert state.phase is Phase.FIX_TEST
    assert next_action(state, policy, receipts).next_agent_ids == (policy.fixer.agent_id,)

    state = advance_state(state, _fix_result(FixTriggerPhase.REVIEW), policy)
    assert state.phase is Phase.VERIFICATION

    state = advance_state(state, _verification_result(VerificationStatus.PASS), policy)
    assert state.phase is Phase.REVIEW
    assert next_action(state, policy, receipts).next_agent_ids == (policy.reviewer.agent_id,)


def test_memory_write_gated_until_final_decision(policy: AutoresearchPolicy) -> None:
    state = _state_to_decision(policy)

    assert can_write_memory(state) is False
    with pytest.raises(AutoresearchValidationError, match="only after final decision"):
        mark_memory_written(state)

    state = advance_state(state, _final_decision(), policy)
    assert can_write_memory(state) is True

    state = mark_memory_written(state)
    assert state.memory_written is True

    next_iteration = start_next_iteration(state)
    assert next_iteration.phase is Phase.SETUP_CONTEXT
    assert next_iteration.iteration == 2


def test_repeat_phase_requires_final_decision(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = replace(AutoresearchState(), phase=Phase.REPEAT)

    with pytest.raises(AutoresearchValidationError, match="repeat phase requires final_decision"):
        next_action(state, policy, receipts)


def test_debate_phase_requires_context_packet(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = replace(AutoresearchState(setup=_setup_artifact()), phase=Phase.DEBATE)

    with pytest.raises(AutoresearchValidationError, match="debate phase requires a context_packet"):
        next_action(state, policy, receipts)


def test_fix_result_trigger_must_match_pending_verification_failure(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)

    with pytest.raises(
        AutoresearchValidationError,
        match="fix_result trigger_phase must match the pending fix source",
    ):
        advance_state(state, _fix_result(FixTriggerPhase.REVIEW), policy)


def test_fix_result_requires_workspace_identity() -> None:
    with pytest.raises(AutoresearchValidationError, match="workspace_path"):
        FixResultArtifact.from_dict(
            {
                "trigger_phase": "verification",
                "summary": "Applied the requested fix.",
                "fixes_applied": ["Expanded coverage"],
                "tests_rerun": ["uv run pytest"],
                "remaining_issues": [],
            }
        )


@pytest.mark.parametrize(
    ("workspace_path", "commit_sha", "match"),
    [
        ("relative/worktree", "def5678", "absolute"),
        ("/tmp/worktree", "not-a-sha", "commit_sha"),
    ],
)
def test_fix_result_rejects_invalid_workspace_identity(
    workspace_path: str,
    commit_sha: str,
    match: str,
) -> None:
    with pytest.raises(AutoresearchValidationError, match=match):
        FixResultArtifact.from_dict(
            {
                "trigger_phase": "verification",
                "summary": "Applied the requested fix.",
                "workspace_path": workspace_path,
                "commit_sha": commit_sha,
                "fixes_applied": ["Expanded coverage"],
                "tests_rerun": ["uv run pytest"],
                "remaining_issues": [],
            }
        )


def test_fix_result_updates_implementation_commit_for_reverification(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    implementation = _implementation_result()
    state = advance_state(state, implementation, policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)

    fixed = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)

    assert fixed.phase is Phase.VERIFICATION
    assert fixed.implementation_result is not None
    assert fixed.implementation_result.workspace_path == implementation.workspace_path
    assert fixed.implementation_result.commit_sha == "def5678"
    assert fixed.fix_history[-1].commit_sha == fixed.implementation_result.commit_sha


def test_fix_result_rejects_different_workspace(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)
    fix_result = replace(_fix_result(FixTriggerPhase.VERIFICATION), workspace_path="/tmp/other")

    with pytest.raises(AutoresearchValidationError, match="workspace_path must match"):
        advance_state(state, fix_result, policy)


def test_verification_failure_routes_fix_test_with_pending_trigger(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.BUG_SIGNAL), policy)

    assert state.phase is Phase.FIX_TEST
    assert state.pending_fix_trigger is FixTriggerPhase.VERIFICATION
    assert next_action(state, policy, receipts).next_agent_ids == (policy.fixer.agent_id,)


@pytest.mark.parametrize(
    ("decision", "metric_value", "reviewer_verdict", "match"),
    [
        (
            FinalDecision.DISCARD,
            0.38,
            ReviewVerdict.PASS.value,
            "decision Sharpe above baseline requires a KEEP-family final_decision",
        ),
        (
            FinalDecision.KEEP,
            0.7,
            ReviewVerdict.PASS.value,
            "decision Sharpe > 0.5 requires SIGNIFICANT KEEP or STRONG KEEP",
        ),
        (
            FinalDecision.SIGNIFICANT_KEEP,
            1.2,
            ReviewVerdict.PASS.value,
            "decision Sharpe > 1.0 with reviewer PASS requires final_decision=STRONG KEEP",
        ),
        (
            FinalDecision.KEEP,
            -0.6,
            ReviewVerdict.PASS.value,
            "decision Sharpe <= -0.5 requires final_decision=DISCARD",
        ),
    ],
)
def test_final_decision_rules_reject_incorrect_decisions(
    policy: AutoresearchPolicy,
    decision: FinalDecision,
    metric_value: float,
    reviewer_verdict: str,
    match: str,
) -> None:
    state = _state_to_decision(policy)

    with pytest.raises(AutoresearchValidationError, match=match):
        advance_state(
            state,
            _final_decision_with(
                decision=decision,
                metric_value=metric_value,
                reviewer_verdict=reviewer_verdict,
            ),
            policy,
        )


def test_final_decision_rules_enforce_drawdown_discard(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    verification = replace(
        _verification_result(VerificationStatus.PASS),
        max_drawdown_pct=34.0,
        oos_sharpe_net=0.92,
        is_walk_forward_sharpe_net=0.84,
    )
    state = advance_state(state, verification, policy)
    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)

    with pytest.raises(
        AutoresearchValidationError,
        match="max_drawdown_pct >= 30 requires final_decision=DISCARD",
    ):
        advance_state(
            state,
            _final_decision_with(
                decision=FinalDecision.SIGNIFICANT_KEEP,
                metric_value=0.92,
                reviewer_verdict=ReviewVerdict.PASS.value,
            ),
            policy,
        )


def test_final_decision_rules_enforce_crash_after_test_failures(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    for _ in range(2):
        state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)
        state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)
    assert state.phase is Phase.DECISION_LOG
    assert state.verification_fix_attempts == 2

    with pytest.raises(
        AutoresearchValidationError,
        match="test failures after retries require final_decision=CRASH",
    ):
        advance_state(
            state,
            _final_decision_with(
                decision=FinalDecision.DISCARD,
                metric_value=None,
                reviewer_verdict=None,
            ),
            policy,
        )


def test_final_decision_rules_enforce_discard_for_remaining_review_issue(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_review(policy)
    state = advance_state(state, _review_result(ReviewVerdict.FAIL, policy), policy)
    injected = replace(state, phase=Phase.DECISION_LOG, pending_fix_trigger=None)

    with pytest.raises(
        AutoresearchValidationError,
        match="critical review issues require final_decision=DISCARD",
    ):
        advance_state(
            injected,
            _final_decision_with(
                decision=FinalDecision.KEEP,
                metric_value=0.38,
                reviewer_verdict=ReviewVerdict.FAIL.value,
            ),
            policy,
        )


def test_final_decision_plain_keep_requires_numeric_baseline(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_decision(policy)
    nonnumeric_setup = (
        replace(state.setup, baseline_summary="Baseline unavailable")
        if state.setup is not None
        else None
    )
    nonnumeric_context = (
        replace(state.context_packet, baseline_metric="Unknown")
        if state.context_packet is not None
        else None
    )
    state = replace(state, setup=nonnumeric_setup, context_packet=nonnumeric_context)

    with pytest.raises(
        AutoresearchValidationError,
        match="plain KEEP requires a numeric baseline",
    ):
        advance_state(
            state,
            _final_decision_with(
                decision=FinalDecision.KEEP,
                metric_value=0.38,
                reviewer_verdict=ReviewVerdict.PASS.value,
            ),
            policy,
        )


def test_final_decision_no_consensus_requires_no_consensus_artifact(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _no_consensus(round_number=1), policy)
    state = advance_state(state, _debate_result(policy, round_number=2), policy)
    state = advance_state(state, _no_consensus(round_number=2), policy)
    assert state.phase is Phase.DECISION_LOG

    with pytest.raises(
        AutoresearchValidationError,
        match="final_decision must be NO_CONSENSUS",
    ):
        advance_state(
            state,
            _final_decision_with(
                decision=FinalDecision.DISCARD,
                metric_value=None,
                reviewer_verdict=None,
            ),
            policy,
        )


def test_target_worktree_guard_allows_only_known_persistent_audit_doc() -> None:
    validate_target_worktree_clean(
        ("?? docs/quantipy_experiment_mempalace_preload.md",),
        allowed_status_lines=("?? docs/quantipy_experiment_mempalace_preload.md",),
    )


def test_target_worktree_guard_rejects_crash_residue() -> None:
    with pytest.raises(AutoresearchValidationError, match="target repo worktree is dirty"):
        validate_target_worktree_clean(
            (
                "?? docs/quantipy_experiment_mempalace_preload.md",
                "?? src/quantipy/alpha/t105_osaf_r2/",
            ),
            allowed_status_lines=("?? docs/quantipy_experiment_mempalace_preload.md",),
        )


def test_implementation_prompt_contains_workspace_isolation_contract(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    prompt = next_action(state, policy, receipts).prompt_text

    assert "Workspace isolation contract" in prompt
    assert "disposable git worktree" in prompt
    assert "Commit all accepted implementation changes" in prompt
    assert "workspace_path" in prompt
    assert "commit_sha" in prompt


def test_verification_prompt_uses_recorded_workspace(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)

    prompt = next_action(state, policy, receipts).prompt_text

    assert "implementation_result.workspace_path" in prompt
    assert "implementation_result.commit_sha" in prompt


def _load_config() -> dict[str, object]:
    raw: object = json.loads(DEFAULT_OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
    return cast(dict[str, object], raw)


def _set_openai_api(config: dict[str, object]) -> None:
    models = cast(dict[str, object], config["models"])
    providers = cast(dict[str, object], models["providers"])
    openai = cast(dict[str, object], providers["openai"])
    openai["api"] = "openai-completions"


def _drop_codex_plugin_allow(config: dict[str, object]) -> None:
    plugins = cast(dict[str, object], config["plugins"])
    plugins["allow"] = []


def _set_agent_runtime_id(config: dict[str, object]) -> None:
    models = cast(dict[str, object], config["models"])
    providers = cast(dict[str, object], models["providers"])
    openai = cast(dict[str, object], providers["openai"])
    runtime = cast(dict[str, object], openai["agentRuntime"])
    runtime["id"] = "other"


def _drop_main_mempalace_skill(config: dict[str, object]) -> None:
    agents_root = cast(dict[str, object], config["agents"])
    agents = cast(list[dict[str, object]], agents_root["list"])
    agents[0]["skills"] = ["autoresearch"]


def _give_stage_agent_write_skill(config: dict[str, object]) -> None:
    agents_root = cast(dict[str, object], config["agents"])
    agents = cast(list[dict[str, object]], agents_root["list"])
    agents[1]["skills"] = ["mempalace", "quantipy-methodology"]


def _remove_stage_agent_mempalace_denies(config: dict[str, object]) -> None:
    agents_root = cast(dict[str, object], config["agents"])
    agents = cast(list[dict[str, object]], agents_root["list"])
    tools = cast(dict[str, object], agents[1]["tools"])
    tools["deny"] = ["mempalace_add_drawer"]


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (_drop_codex_plugin_allow, "plugins.allow must explicitly include codex"),
        (_set_openai_api, "providers.openai.api must be openai-responses"),
        (_set_agent_runtime_id, "providers.openai.agentRuntime.id must be codex"),
        (_drop_main_mempalace_skill, "main must load exactly mempalace and autoresearch"),
        (
            _give_stage_agent_write_skill,
            "must load exactly mempalace-readonly and quantipy-methodology",
        ),
        (_remove_stage_agent_mempalace_denies, "must deny MemPalace mutation tools"),
    ],
)
def test_load_autoresearch_policy_validates_route_skills_and_mempalace_denies(
    tmp_path: Path,
    mutator: Callable[[dict[str, object]], None],
    match: str,
) -> None:
    config = deepcopy(_load_config())
    mutator(config)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(AutoresearchConfigError, match=match):
        load_autoresearch_policy(config_path)
