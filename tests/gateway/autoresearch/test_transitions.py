from __future__ import annotations

import json
from dataclasses import replace
from datetime import (
    date,
    timedelta,
)
from hashlib import sha256
from pathlib import Path
from typing import cast

import gateway.autoresearch.manifest_runtime as autoresearch_manifest_runtime
import gateway.autoresearch.persistence as autoresearch_persistence
import gateway.autoresearch.transitions as autoresearch_transitions
import pytest
from gateway.autoresearch.artifacts import (
    DebateResultArtifact,
    FinalDecisionArtifact,
    PriceHydrationScopePreflight,
    VerificationResultArtifact,
)
from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_DIGEST_DOMAIN,
    MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_NEXT_ACTION_PROMPT_BYTES,
    NEXT_ACTION_PROMPT_TARGET_BYTES,
    OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,
    OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,
    OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
)
from gateway.autoresearch.engine import (
    next_action,
)
from gateway.autoresearch.enums import (
    ArtifactType,
    FinalDecision,
    FinalReviewerVerdict,
    FixTriggerPhase,
    InfraGateOutcome,
    Phase,
    ResearchMode,
    ReviewVerdict,
    VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.governance import (
    CampaignCounters,
    CampaignReviewRecord,
)
from gateway.autoresearch.lifecycle import (
    resume_suspended_iteration,
    start_next_iteration,
    suspend_for_infrastructure,
)
from gateway.autoresearch.manifest_runtime import (
    expected_instruction_manifest_sha256,
)
from gateway.autoresearch.memory import (
    can_write_memory,
)
from gateway.autoresearch.persistence import (
    advance_infrastructure_verification_failure,
    load_artifact_file,
    save_state_file,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy,
    ReceiptCatalog,
)
from gateway.autoresearch.receipts import (
    MemberUnionManifestReceipt,
)
from gateway.autoresearch.state import (
    AutoresearchState,
    AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import advance_state as _runner_advance_state
from gateway.autoresearch.transitions import (
    validate_state,
)
from gateway.autoresearch_platform_validation import (
    PlatformCoverageScope,
    PlatformCoverageStatus,
)
from gateway.autoresearch_readiness import PlatformReadinessManifest

from tests.gateway.autoresearch.builders import (
    _MEMBER_UNION_DIGEST,
    _MEMBER_UNION_PATH,
    _MEMBER_UNION_SHA256,
    GitWorktree,
    _context_artifact,
    _debate_result,
    _dynamic_coverage_receipt,
    _final_decision,
    _final_decision_with,
    _fix_result,
    _g0_platform_contract_mismatch_bug_signal,
    _g0_remediation_verification,
    _implementation_result,
    _legacy_artifact_context,
    _majority_consensus,
    _no_consensus,
    _operator_precondition_consensus,
    _persisted_g0_infra_repaired_repeat_state,
    _platform_coverage_receipt,
    _price_hydration_receipt,
    _prompt_json_value,
    _review_result,
    _round_trip_compact_json,
    _runtime_verification_state,
    _setup_artifact,
    _state_to_consensus,
    _state_to_decision,
    _state_to_g0_decision,
    _state_to_review,
    _universe_verification_receipt,
    _verification_result,
    advance_state,
)


def test_infrastructure_verification_failure_rejects_a_stale_state_reference_before_write(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    state = AutoresearchState(phase=Phase.VERIFICATION, iteration=11)
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    original = state_path.read_bytes()
    artifact = VerificationResultArtifact(
        status=VerificationStatus.TEST_FAILURE,
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        feature_importances_summary="detached run failed",
        null_test_summary="detached run failed",
        bug_signals=(),
        tests_passed=False,
        commands_run=(),
        data_coverage=None,
    )

    with pytest.raises(AutoresearchValidationError, match="state reference"):
        advance_infrastructure_verification_failure(
            state_path=state_path,
            state_reference_sha256="0" * 64,
            instruction_manifest_sha256="1" * 64,
            artifact=artifact,
            policy=policy,
            receipts=receipts,
            validation_context=None,
        )

    assert state_path.read_bytes() == original


def test_infrastructure_verification_failure_advances_to_fix_test_atomically(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
    git_worktree: GitWorktree,
    trusted_quantipy_runs_root: Path,
) -> None:
    state, state_path, evidence = _runtime_verification_state(
        git_worktree,
        policy,
        platform_readiness,
        tmp_path,
        trusted_quantipy_runs_root,
        success=False,
        terminal_stage="smoke",
        terminal_status="rejected",
    )
    save_state_file(state_path, state)
    state_reference_sha256 = autoresearch_transitions.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    instruction_manifest_sha256 = (
        autoresearch_manifest_runtime.expected_instruction_manifest_sha256(
            state,
            policy,
            receipts,
            state_path=state_path,
        )
    )
    artifact = VerificationResultArtifact(
        status=VerificationStatus.TEST_FAILURE,
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        feature_importances_summary="detached run failed",
        null_test_summary="detached run failed",
        bug_signals=(),
        tests_passed=False,
        commands_run=("env PYTHONDONTWRITEBYTECODE=1 uv run quantipy experiment run",),
        data_coverage=None,
        quantipy_experiment_evidence=evidence,
    )

    assert artifact.commands_run == (
        "env PYTHONDONTWRITEBYTECODE=1 uv run quantipy experiment run",
    )

    advanced = advance_infrastructure_verification_failure(
        state_path=state_path,
        state_reference_sha256=state_reference_sha256,
        instruction_manifest_sha256=instruction_manifest_sha256,
        artifact=artifact,
        policy=policy,
        receipts=receipts,
        validation_context=AutoresearchValidationContext(
            state.platform_readiness,
            "f" * 64,
            (date(2021, 1, 5),),
        ),
    )

    assert advanced.phase is Phase.FIX_TEST
    assert advanced.latest_verification == artifact


def test_infrastructure_verification_failure_rejects_instruction_digest_mismatch(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(_implementation_result(), commit_sha="a1a1a1a1a1a1"),
        policy,
    )
    state_path = tmp_path / "quantipy-state.json"
    save_state_file(state_path, state)
    state_reference_sha256 = autoresearch_transitions.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    artifact = VerificationResultArtifact(
        status=VerificationStatus.TEST_FAILURE,
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        feature_importances_summary="detached run failed",
        null_test_summary="detached run failed",
        bug_signals=(),
        tests_passed=False,
        commands_run=(),
        data_coverage=None,
    )

    with pytest.raises(AutoresearchValidationError, match="instruction manifest"):
        advance_infrastructure_verification_failure(
            state_path=state_path,
            state_reference_sha256=state_reference_sha256,
            instruction_manifest_sha256="1" * 64,
            artifact=artifact,
            policy=policy,
            receipts=receipts,
            validation_context=AutoresearchValidationContext.from_readiness(platform_readiness),
        )


def test_expanded_universe_receipt_fits_local_artifact_budget() -> None:
    base = _universe_verification_receipt()
    template = base.batches[0].dates[0]
    dates = tuple(
        replace(
            template,
            selection_date=(date(2021, 1, 4) + timedelta(days=index)).isoformat(),
            earliest_execution_date=(date(2021, 1, 5) + timedelta(days=index)).isoformat(),
            snapshot=replace(
                template.snapshot,
                as_of_date=(date(2021, 1, 4) + timedelta(days=index)).isoformat(),
            ),
            summary=replace(
                template.summary,
                summary_date=(date(2021, 1, 4) + timedelta(days=index)).isoformat(),
            ),
        )
        for index in range(48)
    )
    receipt = replace(
        base,
        batches=(
            replace(base.batches[0], dates=dates[:32]),
            replace(base.batches[0], dates=dates[32:]),
        ),
    )
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        universe_verification_receipt=receipt,
    )
    payload = json.dumps(
        {
            "instruction_manifest_sha256": "0" * 64,
            "state_reference_sha256": "1" * 64,
            "artifact": artifact.to_dict(),
        },
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(payload) > 24 * 1024
    assert len(payload) <= MAX_ARTIFACT_FILE_BYTES


def test_load_artifact_file_rejects_a_tampered_persisted_state(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=state_path,
    )
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": digest,
                "state_reference_sha256": (
                    autoresearch_transitions.build_authoritative_state_reference(
                        state,
                        state_path=state_path,
                    ).sha256()
                ),
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(replace(state, iteration=2).to_dict()),
        encoding="utf-8",
    )

    with pytest.raises(AutoresearchValidationError, match="persisted state does not match"):
        load_artifact_file(
            artifact_path,
            state,
            policy,
            instruction_manifest_sha256=digest,
            state_path=state_path,
        )


def test_load_artifact_file_rejects_a_missing_persisted_state(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "missing-state.json"
    digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=state_path,
    )
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": digest,
                "state_reference_sha256": (
                    autoresearch_transitions.build_authoritative_state_reference(
                        state,
                        state_path=state_path,
                    ).sha256()
                ),
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AutoresearchValidationError, match="missing state file"):
        load_artifact_file(
            artifact_path,
            state,
            policy,
            instruction_manifest_sha256=digest,
            state_path=state_path,
        )


def test_advance_state_rejects_a_tampered_persisted_state(
    tmp_path: Path,
    policy: AutoresearchPolicy,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(replace(state, iteration=2).to_dict()),
        encoding="utf-8",
    )

    with pytest.raises(AutoresearchValidationError, match="persisted state does not match"):
        _runner_advance_state(
            state,
            _setup_artifact(),
            policy,
            state_path=state_path,
        )


def test_load_artifact_file_rejects_an_envelope_bound_to_a_different_state_path(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = AutoresearchState()
    default_state_path = tmp_path / "default-state.json"
    custom_state_path = tmp_path / "custom-state.json"
    serialized_state = json.dumps(state.to_dict())
    default_state_path.write_text(serialized_state, encoding="utf-8")
    custom_state_path.write_text(serialized_state, encoding="utf-8")
    digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=default_state_path,
    )
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": digest,
                "state_reference_sha256": (
                    autoresearch_transitions.build_authoritative_state_reference(
                        state,
                        state_path=default_state_path,
                    ).sha256()
                ),
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AutoresearchValidationError, match="dispatched manifest"):
        load_artifact_file(
            artifact_path,
            state,
            policy,
            instruction_manifest_sha256=expected_instruction_manifest_sha256(
                state,
                policy,
                receipts,
                state_path=custom_state_path,
            ),
            state_path=custom_state_path,
        )


def test_authoritative_state_reference_rejects_a_tampered_state_file(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    action = next_action(
        state,
        policy,
        receipts,
        platform_readiness,
        state_path=state_path,
    )
    tampered = replace(state, iteration=2)
    state_path.write_text(json.dumps(tampered.to_dict()), encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="does not match the current state"):
        autoresearch_persistence.validate_authoritative_state_reference(
            action.instruction_source_manifest.state_reference
        )


def test_artifact_envelope_rejects_a_stale_state_reference(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    action = next_action(
        state,
        policy,
        receipts,
        platform_readiness,
        state_path=state_path,
    )
    tampered = replace(state, iteration=2)
    tampered_digest = expected_instruction_manifest_sha256(
        tampered,
        policy,
        receipts,
        state_path=state_path,
    )
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": tampered_digest,
                "state_reference_sha256": action.state_reference_sha256,
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps(tampered.to_dict()), encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="state_reference_sha256"):
        load_artifact_file(
            artifact_path,
            tampered,
            policy,
            instruction_manifest_sha256=tampered_digest,
            state_path=state_path,
        )


def test_phase_progression(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())

    action = next_action(state, policy, receipts, platform_readiness)
    assert action.phase is Phase.SETUP_CONTEXT
    assert action.next_agent_ids == ("autoresearch-pm",)

    state = advance_state(state, _setup_artifact(), policy)
    action = next_action(state, policy, receipts, platform_readiness)
    assert action.next_agent_ids == ("context_curator",)

    state = advance_state(state, _context_artifact(), policy)
    assert state.phase is Phase.DEBATE
    assert (
        next_action(state, policy, receipts, platform_readiness).next_agent_ids
        == policy.debate_agent_ids
    )

    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    assert state.phase is Phase.CONSENSUS

    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    assert state.phase is Phase.IMPLEMENTATION

    state = advance_state(state, _implementation_result(), policy)
    assert state.phase is Phase.VERIFICATION

    state = advance_state(state, _verification_result(VerificationStatus.PASS), policy)
    assert state.phase is Phase.REVIEW
    assert next_action(state, policy, receipts, platform_readiness).next_agent_ids == (
        policy.reviewer.agent_id,
    )

    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)
    assert state.phase is Phase.DECISION_LOG

    state = advance_state(state, _final_decision(), policy)
    assert state.phase is Phase.REPEAT


def test_prompt_hard_byte_budget_for_reachable_phase_modes(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    identity = platform_readiness.identity()
    initial = AutoresearchState(platform_readiness=identity)
    setup_done = advance_state(initial, _setup_artifact(), policy)
    context_done = advance_state(setup_done, _context_artifact(), policy)
    debate_done = advance_state(context_done, _debate_result(policy, round_number=1), policy)
    consensus_done = advance_state(
        debate_done, _majority_consensus(round_number=1, policy=policy), policy
    )
    implementation_done = advance_state(consensus_done, _implementation_result(), policy)
    verification_failed = advance_state(
        implementation_done, _verification_result(VerificationStatus.TEST_FAILURE), policy
    )
    fix_done = advance_state(verification_failed, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    verification_done = advance_state(
        implementation_done, _verification_result(VerificationStatus.PASS), policy
    )
    review_done = advance_state(
        verification_done, _review_result(ReviewVerdict.PASS, policy), policy
    )
    repeat_memory = advance_state(review_done, _final_decision(), policy)
    no_consensus_once = advance_state(debate_done, _no_consensus(round_number=1), policy)
    no_consensus_retry = advance_state(
        no_consensus_once, _debate_result(policy, round_number=2), policy
    )
    no_consensus_decision = advance_state(no_consensus_retry, _no_consensus(round_number=2), policy)
    g0_setup = advance_state(initial, _setup_artifact(), policy)
    g0_context = advance_state(
        g0_setup,
        replace(
            _context_artifact(),
            research_mode=ResearchMode.DATA_INFRA_G0,
            mode_rationale="Repair cap and source provenance before an alpha rerun.",
        ),
        policy,
    )
    g0_debate = advance_state(g0_context, _debate_result(policy, round_number=1), policy)
    g0_consensus = advance_state(
        g0_debate, _majority_consensus(round_number=1, policy=policy), policy
    )
    g0_implementation = advance_state(g0_consensus, _implementation_result(), policy)
    g0_verification = advance_state(
        g0_implementation,
        replace(
            _verification_result(VerificationStatus.PASS),
            infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
            infra_rationale="Data infrastructure gate passed.",
        ),
        policy,
    )
    g0_decision = advance_state(g0_verification, _review_result(ReviewVerdict.PASS, policy), policy)
    states = (
        initial,
        setup_done,
        context_done,
        debate_done,
        consensus_done,
        implementation_done,
        verification_failed,
        fix_done,
        verification_done,
        review_done,
        repeat_memory,
        no_consensus_retry,
        no_consensus_decision,
        g0_context,
        g0_consensus,
        g0_implementation,
        g0_verification,
        g0_decision,
    )

    for state in states:
        prompt = next_action(state, policy, receipts, platform_readiness).prompt_text
        assert len(prompt.encode("utf-8")) <= NEXT_ACTION_PROMPT_TARGET_BYTES - 1024, (
            state.phase.value
        )


def test_next_action_keeps_verbose_state_out_of_the_prompt(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())
    state = advance_state(
        state,
        replace(
            _setup_artifact(),
            baseline_summary="reviewer baseline overflow " + ("x" * 40_000),
        ),
        policy,
    )

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "reviewer baseline overflow" not in prompt
    assert len(prompt.encode("utf-8")) <= NEXT_ACTION_PROMPT_TARGET_BYTES - 1024


def test_next_action_uses_manifest_bound_state_reference_for_verbose_no_consensus_retry(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    verbose_detail = "accepted debate evidence and provenance remain available. " * 28
    base_debate = _debate_result(policy, round_number=1)
    verbose_debate = DebateResultArtifact(
        round_number=base_debate.round_number,
        submissions=tuple(
            replace(
                submission,
                hypothesis=f"{submission.hypothesis} {verbose_detail}",
                feature_pipeline=f"{submission.feature_pipeline} {verbose_detail}",
                model_plan=f"{submission.model_plan} {verbose_detail}",
                objections=(f"{submission.objections[0]} {verbose_detail}",),
            )
            for submission in base_debate.submissions
        ),
    )
    verbose_context = replace(
        _context_artifact(),
        recent_experiment_outcomes=tuple(
            f"real-shaped prior experiment outcome {index}: {verbose_detail}" for index in range(12)
        ),
        prior_findings=tuple(
            f"real-shaped provenance finding {index}: {verbose_detail}" for index in range(8)
        ),
    )
    state = AutoresearchState(platform_readiness=platform_readiness.identity())
    state = advance_state(state, _setup_artifact(), policy)
    state = advance_state(state, verbose_context, policy)
    state = advance_state(state, verbose_debate, policy)
    state = advance_state(state, _no_consensus(round_number=1), policy)
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

    action = next_action(
        state,
        policy,
        receipts,
        platform_readiness,
        state_path=state_path,
    )
    prompt = action.prompt_text
    state_reference = _round_trip_compact_json(_prompt_json_value(prompt, "STATE_REF="))
    compact_state_reference = json.dumps(state_reference, sort_keys=True, separators=(",", ":"))
    compact_legacy_state = json.dumps(
        _legacy_artifact_context(state),
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_embedded_state_prompt = prompt.replace(
        f"STATE_REF={compact_state_reference}\n",
        f"STATE={compact_legacy_state}\n",
    )

    assert len(legacy_embedded_state_prompt.encode("utf-8")) > MAX_NEXT_ACTION_PROMPT_BYTES
    assert len(prompt.encode("utf-8")) <= NEXT_ACTION_PROMPT_TARGET_BYTES - 1024
    assert "STATE=" not in prompt
    assert state_reference == action.instruction_source_manifest.state_reference.to_dict()
    assert state_reference["path"] == str(state_path.resolve())
    assert state_reference["phase"] == Phase.DEBATE.value
    assert (
        state_reference["state_sha256"]
        == action.instruction_source_manifest.state_reference.state_sha256
    )


def test_later_phase_prompt_keeps_verbose_history_in_the_verified_state_file(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_decision(policy, platform_readiness)
    assert state.context_packet is not None
    assert state.latest_debate is not None
    verbose_detail = "later phase historical evidence remains lossless in state. " * 32
    verbose_context = replace(
        state.context_packet,
        prior_findings=tuple(f"finding {index}: {verbose_detail}" for index in range(12)),
    )
    verbose_debate = replace(
        state.latest_debate,
        submissions=tuple(
            replace(submission, hypothesis=f"{submission.hypothesis} {verbose_detail}")
            for submission in state.latest_debate.submissions
        ),
    )
    verbose_state = replace(
        state,
        context_packet=verbose_context,
        debate_rounds=(verbose_debate,),
    )
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(verbose_state.to_dict()), encoding="utf-8")

    action = next_action(
        verbose_state,
        policy,
        receipts,
        platform_readiness,
        state_path=state_path,
    )

    assert action.phase is Phase.DECISION_LOG
    assert len(action.prompt_text.encode("utf-8")) <= NEXT_ACTION_PROMPT_TARGET_BYTES - 1024
    assert verbose_detail not in action.prompt_text
    validated = autoresearch_persistence.validate_authoritative_state_reference(
        action.instruction_source_manifest.state_reference
    )

    assert validated.to_dict() == AutoresearchState.from_dict(verbose_state.to_dict()).to_dict()


def test_next_action_fails_closed_when_accepted_union_manifest_is_deleted(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "member-union.txt"
    manifest.write_bytes(_MEMBER_UNION_PATH.read_bytes())
    state = _state_to_review(policy, platform_readiness)
    verification = state.latest_verification
    assert verification is not None
    universe = verification.universe_verification_receipt
    assert universe is not None
    state = replace(
        state,
        verification_history=(
            replace(
                verification,
                universe_verification_receipt=replace(
                    universe,
                    member_union_manifest=MemberUnionManifestReceipt(
                        path=str(manifest), sha256=_MEMBER_UNION_SHA256
                    ),
                ),
            ),
        ),
    )
    manifest.unlink()

    with pytest.raises(AutoresearchValidationError, match="cannot read member union manifest"):
        next_action(state, policy, receipts, platform_readiness)


def test_next_action_fails_closed_when_accepted_union_manifest_is_mutated_later(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "member-union.txt"
    manifest.write_bytes(_MEMBER_UNION_PATH.read_bytes())
    state = _state_to_decision(policy, platform_readiness)
    verification = state.latest_verification
    assert verification is not None
    universe = verification.universe_verification_receipt
    assert universe is not None
    state = replace(
        state,
        verification_history=(
            replace(
                verification,
                universe_verification_receipt=replace(
                    universe,
                    member_union_manifest=MemberUnionManifestReceipt(
                        path=str(manifest), sha256=_MEMBER_UNION_SHA256
                    ),
                ),
            ),
        ),
    )
    manifest.write_bytes(b"MUTATED\n")

    with pytest.raises(AutoresearchValidationError, match="SHA-256 mismatch"):
        next_action(state, policy, receipts, platform_readiness)


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


def test_data_infra_majority_without_universe_plan_fails_at_consensus(
    policy: AutoresearchPolicy,
) -> None:
    state = AutoresearchState()
    state = advance_state(state, _setup_artifact(), policy)
    state = advance_state(
        state,
        replace(
            _context_artifact(),
            research_mode=ResearchMode.DATA_INFRA_G0,
            mode_rationale="Repair source provenance before an alpha rerun.",
        ),
        policy,
    )
    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    consensus = replace(_majority_consensus(round_number=1, policy=policy), universe_plan=None)

    with pytest.raises(
        AutoresearchValidationError,
        match="majority consensus requires a frozen universe_plan",
    ):
        advance_state(state, consensus, policy)


def test_persisted_data_infra_current_majority_requires_a_universe_plan(
    policy: AutoresearchPolicy,
) -> None:
    state = AutoresearchState()
    state = advance_state(state, _setup_artifact(), policy)
    state = advance_state(
        state,
        replace(
            _context_artifact(),
            research_mode=ResearchMode.DATA_INFRA_G0,
            mode_rationale="Repair source provenance before an alpha rerun.",
        ),
        policy,
    )
    state = advance_state(state, _debate_result(policy, round_number=1), policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    assert state.latest_consensus is not None
    forged = replace(
        state,
        consensus_history=(replace(state.latest_consensus, universe_plan=None),),
    )

    persisted = AutoresearchState.from_dict(json.loads(json.dumps(forged.to_dict())))

    with pytest.raises(
        AutoresearchValidationError,
        match="non-operator majority consensus at history index 1 requires a frozen universe_plan",
    ):
        validate_state(persisted, policy)


def test_persisted_history_cannot_hide_an_earlier_planless_majority(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    assert state.latest_consensus is not None
    forged = replace(
        state,
        consensus_history=(
            replace(state.latest_consensus, universe_plan=None),
            replace(state.latest_consensus, round_number=2),
        ),
    )

    persisted = AutoresearchState.from_dict(json.loads(json.dumps(forged.to_dict())))

    with pytest.raises(
        AutoresearchValidationError,
        match="non-operator majority consensus at history index 1 requires a frozen universe_plan",
    ):
        validate_state(persisted, policy)


def test_data_infra_operator_precondition_without_plan_routes_to_decision_log(
    policy: AutoresearchPolicy,
) -> None:
    state = AutoresearchState()
    state = advance_state(state, _setup_artifact(), policy)
    state = advance_state(
        state,
        replace(
            _context_artifact(),
            research_mode=ResearchMode.DATA_INFRA_G0,
            mode_rationale="Repair source provenance before an alpha rerun.",
        ),
        policy,
    )
    state = advance_state(state, _debate_result(policy, round_number=1), policy)

    advanced = advance_state(state, _operator_precondition_consensus(1, policy), policy)

    assert advanced.phase is Phase.DECISION_LOG


def test_consensus_prompt_requires_universe_plan_for_both_modes(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "both ALPHA_RESEARCH and DATA_INFRA_G0" in prompt


def test_operator_precondition_majority_routes_to_decision_log(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)

    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)

    assert state.phase is Phase.DECISION_LOG
    action = next_action(state, policy, receipts, platform_readiness)
    assert action.next_agent_ids == (policy.pm.agent_id,)
    assert action.expected_artifact_type is ArtifactType.FINAL_DECISION
    assert "memory_write_required=false" in action.prompt_text
    assert "no-code operator precondition" in action.prompt_text


def test_operator_precondition_final_decision_allows_infra_blocked_without_verification(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)

    decided = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="i26-operator-evidence-precondition",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
            log_summary="Blocked before implementation on missing operator evidence.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Missing operator-supplied first-party evidence bundle.",
        ),
        policy,
    )

    assert decided.phase is Phase.REPEAT
    assert decided.final_decision is not None
    assert decided.final_decision.decision is FinalDecision.INFRA_BLOCKED
    assert can_write_memory(decided) is False
    assert decided.iteration == 1
    assert decided.suspended is True
    with pytest.raises(AutoresearchValidationError, match="autoresearch-resume"):
        start_next_iteration(decided)


def test_persisted_operator_precondition_no_memory_state_validates(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)
    state = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="i26-operator-evidence-precondition",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
            log_summary="Blocked before implementation on missing operator evidence.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Missing operator-supplied first-party evidence bundle.",
        ),
        policy,
    )
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(state.to_dict())))

    validate_state(persisted, policy)

    assert persisted.suspended is True
    assert can_write_memory(persisted) is False


def test_persisted_unsuspended_operator_precondition_blocker_is_invalid(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)
    suspended = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="i26-operator-evidence-precondition",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
            log_summary="Blocked before implementation on missing operator evidence.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Missing operator-supplied first-party evidence bundle.",
        ),
        policy,
    )
    unsuspended = replace(suspended, suspended=False, suspension_reason=None)
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(unsuspended.to_dict())))

    with pytest.raises(AutoresearchValidationError, match=r"operator-precondition.*suspended"):
        validate_state(persisted, policy)


def test_start_next_rejects_unsuspended_operator_precondition_no_memory_state(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)
    suspended = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="i26-operator-evidence-precondition",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
            log_summary="Blocked before implementation on missing operator evidence.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Missing operator-supplied first-party evidence bundle.",
        ),
        policy,
    )
    unsuspended = replace(suspended, suspended=False, suspension_reason=None)

    with pytest.raises(AutoresearchValidationError, match=r"operator-precondition.*suspended"):
        start_next_iteration(unsuspended, readiness=platform_readiness)


def test_start_next_rejects_forged_memory_on_unsuspended_operator_precondition_blocker(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)
    suspended = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="i26-operator-evidence-precondition",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
            log_summary="Blocked before implementation on missing operator evidence.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Missing operator-supplied first-party evidence bundle.",
        ),
        policy,
    )
    forged = replace(
        suspended,
        suspended=False,
        suspension_reason=None,
        memory_written=True,
    )

    with pytest.raises(AutoresearchValidationError, match=r"operator-precondition.*suspended"):
        start_next_iteration(forged, readiness=platform_readiness)


def test_operator_precondition_final_decision_rejects_unverified_metric(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)

    with pytest.raises(AutoresearchValidationError, match="recommended_metric_value=null"):
        advance_state(
            state,
            FinalDecisionArtifact(
                experiment_id="i26-operator-evidence-precondition",
                decision=FinalDecision.INFRA_BLOCKED,
                recommended_metric_name="operator_precondition",
                recommended_metric_value=1.0,
                reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
                rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
                log_summary="Blocked before implementation on missing operator evidence.",
                continue_loop=True,
                memory_write_required=False,
                infra_rationale="Missing operator-supplied first-party evidence bundle.",
            ),
            policy,
        )


def test_persisted_operator_precondition_no_memory_state_requires_full_contract(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _operator_precondition_consensus(1, policy), policy)
    malformed = replace(
        state,
        phase=Phase.REPEAT,
        final_decision=FinalDecisionArtifact(
            experiment_id="i26-operator-evidence-precondition",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=1.0,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="The required immutable Quantipy/XNYS evidence bundle is absent.",
            log_summary="Blocked before implementation on missing operator evidence.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Missing operator-supplied first-party evidence bundle.",
        ),
        suspended=True,
        suspension_reason="Missing operator-supplied first-party evidence bundle.",
    )

    with pytest.raises(AutoresearchValidationError, match="recommended_metric_value=null"):
        next_action(malformed, policy, receipts, platform_readiness)


def test_next_action_rejects_operator_precondition_implementation_state(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = replace(
        _state_to_consensus(policy, platform_readiness),
        consensus_history=(_operator_precondition_consensus(1, policy),),
        phase=Phase.IMPLEMENTATION,
    )

    with pytest.raises(AutoresearchValidationError, match="operator-precondition"):
        next_action(state, policy, receipts, platform_readiness)


def test_second_no_consensus_is_an_unsuspended_no_memory_research_outcome(
    no_consensus_state: AutoresearchState,
) -> None:
    assert can_write_memory(no_consensus_state) is False
    assert no_consensus_state.suspended is False
    assert no_consensus_state.suspension_reason is None
    assert no_consensus_state.final_decision is not None
    assert no_consensus_state.final_decision.decision is FinalDecision.NO_CONSENSUS
    assert no_consensus_state.final_decision.infra_rationale is None


def test_persisted_no_consensus_state_retains_its_unsuspended_transition(
    no_consensus_state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> None:
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(no_consensus_state.to_dict())))

    validate_state(persisted, policy)

    assert persisted == no_consensus_state


def test_persisted_no_consensus_state_requires_the_mandatory_second_round(
    no_consensus_state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> None:
    forged = replace(
        no_consensus_state,
        debate_rounds=no_consensus_state.debate_rounds[:1],
        consensus_history=no_consensus_state.consensus_history[:1],
    )
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(forged.to_dict())))

    with pytest.raises(AutoresearchValidationError, match="mandatory second round"):
        validate_state(persisted, policy)


def test_no_consensus_next_action_allows_starting_the_next_iteration(
    no_consensus_state: AutoresearchState,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    action = next_action(no_consensus_state, policy, receipts, platform_readiness)

    assert action.phase is Phase.REPEAT
    assert action.expected_artifact_type is ArtifactType.NEXT_ITERATION
    assert action.next_agent_ids == ()


def test_persisted_no_consensus_state_rejects_an_infrastructure_rationale(
    no_consensus_state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> None:
    assert no_consensus_state.final_decision is not None
    malformed = replace(
        no_consensus_state,
        final_decision=replace(
            no_consensus_state.final_decision,
            infra_rationale="No majority is a research outcome, not an infrastructure blocker.",
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="NO_CONSENSUS final_decision cannot contain infra_rationale",
    ):
        validate_state(malformed, policy)


def test_no_consensus_starts_the_next_iteration_and_dispatches_setup(
    no_consensus_state: AutoresearchState,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    next_iteration = start_next_iteration(
        no_consensus_state,
        readiness=platform_readiness,
    )
    action = next_action(next_iteration, policy, receipts, platform_readiness)

    assert next_iteration.iteration == 2
    assert action.phase is Phase.SETUP_CONTEXT
    assert action.expected_artifact_type is ArtifactType.CONTEXT_PACKET
    assert action.next_agent_ids == (policy.context_curator.agent_id,)


def test_lifecycle_carries_all_dispatch_a_governance_fields_forward(
    no_consensus_state: AutoresearchState,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    next_iteration = start_next_iteration(no_consensus_state, readiness=platform_readiness)

    for field_name in (
        "hypothesis_registry",
        "campaign_counters",
        "campaign_review_required",
        "campaign_review_reason",
        "campaign_review_history",
    ):
        assert getattr(next_iteration, field_name) == getattr(no_consensus_state, field_name)

    suspended = suspend_for_infrastructure(
        _state_to_decision(policy, platform_readiness),
        "Operator is repairing infrastructure.",
    )
    changed_readiness = replace(platform_readiness, manifest_id="manifest-test-2")
    resumed = resume_suspended_iteration(suspended, changed_readiness)

    for field_name in (
        "hypothesis_registry",
        "campaign_counters",
        "campaign_review_required",
        "campaign_review_reason",
        "campaign_review_history",
    ):
        assert getattr(resumed, field_name) == getattr(suspended, field_name)


def test_no_consensus_rejects_a_memory_write_requirement(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _no_consensus(round_number=1), policy)
    state = advance_state(state, _debate_result(policy, round_number=2), policy)
    state = advance_state(state, _no_consensus(round_number=2), policy)
    decision = FinalDecisionArtifact(
        experiment_id="no-consensus-1",
        decision=FinalDecision.NO_CONSENSUS,
        recommended_metric_name="consensus outcome",
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
        rationale="The retry produced no majority and no implementation was created.",
        log_summary="No consensus after the allowed retry.",
        continue_loop=True,
        memory_write_required=True,
    )

    with pytest.raises(AutoresearchValidationError, match="memory_write_required=false"):
        advance_state(state, decision, policy)


def test_alpha_final_decision_rejects_infra_blocked(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_decision(policy)
    decision = replace(
        _final_decision_with(
            decision=FinalDecision.INFRA_BLOCKED,
            metric_value=0.18,
            reviewer_verdict=FinalReviewerVerdict.PASS,
        ),
        memory_write_required=False,
    )

    with pytest.raises(AutoresearchValidationError, match="operator-owned"):
        advance_state(state, decision, policy)


def test_alpha_debate_rejects_a_burned_theory_family_without_new_evidence(
    policy: AutoresearchPolicy,
) -> None:
    state = advance_state(AutoresearchState(), _setup_artifact(), policy)
    context = replace(_context_artifact(), burned_theory_families=("vwap-obv",))
    state = advance_state(state, context, policy)

    with pytest.raises(AutoresearchValidationError, match="materially_new_evidence"):
        advance_state(state, _debate_result(policy, round_number=1), policy)


def test_g0_final_decision_uses_infrastructure_outcome_not_sharpe(
    policy: AutoresearchPolicy,
) -> None:
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
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
            infra_rationale="Every source and cap record has auditable provenance.",
        ),
        policy,
    )
    state = advance_state(state, _review_result(ReviewVerdict.PASS, policy), policy)

    result = advance_state(
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

    assert result.final_decision is not None
    assert result.final_decision.decision is FinalDecision.INFRA_REPAIRED


def test_g0_verification_requires_strict_readiness_validation_context(
    policy: AutoresearchPolicy,
) -> None:
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
    state = advance_state(state, _implementation_result(), policy)

    with pytest.raises(AutoresearchValidationError, match=r"DATA_INFRA_G0.*validation context"):
        _runner_advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
                infra_rationale="Every source and cap record has auditable provenance.",
            ),
            policy,
        )


def test_g0_final_decision_requires_validation_context_for_accepted_provenance(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_g0_decision(
        policy,
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
    )

    with pytest.raises(AutoresearchValidationError, match=r"DATA_INFRA_G0.*validation context"):
        _runner_advance_state(
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


def test_g0_platform_receipt_rejects_old_hydration_metadata_digest_binding(
    policy: AutoresearchPolicy,
) -> None:
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
    state = advance_state(state, _implementation_result(), policy)
    stale_metadata_digest = _price_hydration_receipt().coverage_receipt_digest

    with pytest.raises(
        AutoresearchValidationError,
        match="platform_coverage_contract_mismatch BUG_SIGNAL",
    ):
        advance_state(
            state,
            replace(
                _verification_result(VerificationStatus.PASS),
                infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
                infra_rationale="Every source and cap record has auditable provenance.",
                platform_coverage_validation=_platform_coverage_receipt(
                    source_price_coverage_response_digest=stale_metadata_digest
                ),
            ),
            policy,
        )


def test_g0_platform_receipt_rejects_universe_newline_member_union_digest(
    policy: AutoresearchPolicy,
    g0_verification_state: AutoresearchState,
) -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
        infra_rationale="Every source and cap record has auditable provenance.",
        platform_coverage_validation=_platform_coverage_receipt(
            member_union_digest=_MEMBER_UNION_DIGEST
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="platform_coverage_contract_mismatch BUG_SIGNAL",
    ):
        advance_state(g0_verification_state, artifact, policy)


def test_platform_preflight_rejects_weekend_endpoint() -> None:
    preflight = PriceHydrationScopePreflight(
        member_union_count=1,
        experiment_start="2021-07-03",
        experiment_end="2021-07-06",
        timeframe="1min",
        market_hours="regular",
        session_count=1,
        planned_symbol_sessions=1,
        within_budget=True,
    )
    context = AutoresearchValidationContext(
        None,
        "d" * 64,
        (date(2021, 7, 2), date(2021, 7, 6)),
        date(2021, 7, 2),
        date(2021, 7, 6),
    )

    with pytest.raises(AutoresearchValidationError, match="actual XNYS session labels"):
        autoresearch_transitions._requested_sessions_for_preflight(preflight, context)


def test_platform_preflight_rejects_range_outside_pinned_xnys_evidence() -> None:
    preflight = PriceHydrationScopePreflight(
        member_union_count=1,
        experiment_start="2021-01-04",
        experiment_end="2021-01-05",
        timeframe="1min",
        market_hours="regular",
        session_count=1,
        planned_symbol_sessions=1,
        within_budget=True,
    )
    context = AutoresearchValidationContext(
        None,
        "d" * 64,
        (date(2021, 1, 5),),
        date(2021, 1, 5),
        date(2021, 1, 5),
    )

    with pytest.raises(AutoresearchValidationError, match="outside pinned XNYS evidence"):
        autoresearch_transitions._requested_sessions_for_preflight(preflight, context)


def test_platform_preflight_rejects_truncated_xnys_session_evidence() -> None:
    preflight = PriceHydrationScopePreflight(
        member_union_count=1,
        experiment_start="2021-01-05",
        experiment_end="2021-01-06",
        timeframe="1min",
        market_hours="regular",
        session_count=2,
        planned_symbol_sessions=2,
        within_budget=True,
    )
    context = AutoresearchValidationContext(
        None,
        "d" * 64,
        (date(2021, 1, 5),),
        date(2021, 1, 5),
        date(2021, 1, 5),
    )

    with pytest.raises(AutoresearchValidationError, match="outside pinned XNYS evidence"):
        autoresearch_transitions._requested_sessions_for_preflight(preflight, context)


def test_g0_remediation_rejects_stage_authored_infra_blocked(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_g0_decision(
        policy,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
    )

    with pytest.raises(AutoresearchValidationError, match="non-suspending DISCARD"):
        advance_state(
            state,
            FinalDecisionArtifact(
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
            ),
            policy,
        )


def test_g0_remediation_discards_without_suspending(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_g0_decision(
        policy,
        readiness=platform_readiness,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
    )

    state = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="g0-iteration-1",
            decision=FinalDecision.DISCARD,
            recommended_metric_name="coverage gate",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Data infrastructure remains blocked.",
            log_summary="G0 gate still requires remediation.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Cap/source provenance still needs operator remediation.",
        ),
        policy,
    )

    assert state.suspended is False
    assert can_write_memory(state) is False
    assert state.phase is Phase.REPEAT


def test_persisted_g0_remediation_discard_no_memory_state_validates(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_g0_decision(
        policy,
        readiness=platform_readiness,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
    )
    state = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="g0-iteration-1",
            decision=FinalDecision.DISCARD,
            recommended_metric_name="coverage gate",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Data infrastructure remains blocked.",
            log_summary="G0 gate still requires remediation.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Cap/source provenance still needs operator remediation.",
        ),
        policy,
    )
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(state.to_dict())))

    validate_state(persisted, policy)

    assert persisted.suspended is False
    assert can_write_memory(persisted) is False


def test_persisted_nonlegacy_g0_remediation_suspension_is_rejected(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    suspended_g0_remediation_state: AutoresearchState,
) -> None:
    persisted = AutoresearchState.from_dict(
        json.loads(json.dumps(suspended_g0_remediation_state.to_dict()))
    )

    with pytest.raises(AutoresearchValidationError, match="non-suspending DISCARD"):
        validate_state(persisted, policy)


def test_g0_suspended_receipt_omission_is_rejected(
    policy: AutoresearchPolicy,
    suspended_g0_remediation_state: AutoresearchState,
) -> None:
    raw = suspended_g0_remediation_state.to_dict()
    history = raw["verification_history"]
    assert isinstance(history, list)
    for verification in history:
        assert isinstance(verification, dict)
        del verification["platform_coverage_validation"]

    with pytest.raises(AutoresearchValidationError, match="platform_coverage_validation"):
        AutoresearchState.from_dict(raw)


def test_nonlegacy_g0_state_serialization_includes_platform_coverage_validation(
    suspended_g0_remediation_state: AutoresearchState,
) -> None:
    history = suspended_g0_remediation_state.to_dict()["verification_history"]

    assert isinstance(history, list)
    assert isinstance(history[0], dict)
    assert "platform_coverage_validation" in history[0]


def test_persisted_planless_g0_remediation_state_rejects_an_earlier_majority(
    policy: AutoresearchPolicy,
    suspended_g0_remediation_state: AutoresearchState,
) -> None:
    assert suspended_g0_remediation_state.latest_consensus is not None
    latest_consensus = replace(
        suspended_g0_remediation_state.latest_consensus,
        round_number=2,
        universe_plan=None,
    )
    forged = replace(
        suspended_g0_remediation_state,
        consensus_history=(
            replace(latest_consensus, round_number=1),
            latest_consensus,
        ),
    )

    persisted = AutoresearchState.from_dict(json.loads(json.dumps(forged.to_dict())))

    with pytest.raises(
        AutoresearchValidationError,
        match="non-operator majority consensus at history index 1 requires a frozen universe_plan",
    ):
        validate_state(persisted, policy)


def test_persisted_planless_g0_remediation_state_rejects_an_unsuspended_state(
    policy: AutoresearchPolicy,
    suspended_g0_remediation_state: AutoresearchState,
) -> None:
    assert suspended_g0_remediation_state.latest_consensus is not None
    forged = replace(
        suspended_g0_remediation_state,
        consensus_history=(
            replace(suspended_g0_remediation_state.latest_consensus, universe_plan=None),
        ),
        suspended=False,
        suspension_reason=None,
    )

    persisted = AutoresearchState.from_dict(json.loads(json.dumps(forged.to_dict())))

    with pytest.raises(
        AutoresearchValidationError,
        match="non-operator majority consensus at history index 1 requires a frozen universe_plan",
    ):
        validate_state(persisted, policy)


def test_persisted_planless_g0_remediation_state_rejects_a_near_miss(
    policy: AutoresearchPolicy,
    suspended_g0_remediation_state: AutoresearchState,
) -> None:
    assert suspended_g0_remediation_state.latest_consensus is not None
    assert suspended_g0_remediation_state.latest_verification is not None
    forged = replace(
        suspended_g0_remediation_state,
        consensus_history=(
            replace(suspended_g0_remediation_state.latest_consensus, universe_plan=None),
        ),
        verification_history=(
            replace(
                suspended_g0_remediation_state.latest_verification,
                infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
            ),
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="platform coverage receipt status",
    ):
        AutoresearchState.from_dict(json.loads(json.dumps(forged.to_dict())))


@pytest.mark.parametrize(
    "verification_status",
    (VerificationStatus.TEST_FAILURE, VerificationStatus.BUG_SIGNAL),
)
def test_g0_remediation_required_verifier_failure_routes_to_fix_without_suspending(
    policy: AutoresearchPolicy,
    g0_verification_state: AutoresearchState,
    verification_status: VerificationStatus,
) -> None:
    result = advance_state(
        g0_verification_state,
        _g0_remediation_verification(verification_status),
        policy,
    )

    assert result.phase is Phase.FIX_TEST
    assert result.pending_fix_trigger is FixTriggerPhase.VERIFICATION
    assert result.suspended is False
    assert result.final_decision is None


def test_exhausted_g0_verifier_failure_rejects_infra_blocked(
    exhausted_g0_verification: tuple[AutoresearchState, FinalDecision],
    policy: AutoresearchPolicy,
) -> None:
    state, _ = exhausted_g0_verification
    decision = FinalDecisionArtifact(
        experiment_id="g0-verification-failure-1",
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name="coverage gate",
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
        rationale="Verification failed before the infrastructure gate could complete.",
        log_summary="G0 verification retries exhausted.",
        continue_loop=True,
        memory_write_required=False,
        infra_rationale="Verification did not complete successfully.",
    )

    with pytest.raises(AutoresearchValidationError, match="after retries require"):
        advance_state(state, decision, policy)


def test_exhausted_g0_verifier_failure_finalizes_without_suspending(
    exhausted_g0_verification: tuple[AutoresearchState, FinalDecision],
    policy: AutoresearchPolicy,
) -> None:
    state, expected_decision = exhausted_g0_verification
    decision = FinalDecisionArtifact(
        experiment_id="g0-verification-failure-1",
        decision=expected_decision,
        recommended_metric_name="coverage gate",
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
        rationale="Verification failed before the infrastructure gate could complete.",
        log_summary="G0 verification retries exhausted.",
        continue_loop=True,
        memory_write_required=False,
        infra_rationale="Verification did not complete successfully.",
    )

    result = advance_state(
        state,
        decision,
        policy,
    )

    assert result.phase is Phase.REPEAT
    assert result.suspended is False
    assert result.final_decision is not None
    assert result.final_decision.decision is expected_decision
    assert result.final_decision.memory_write_required is False


def test_exhausted_g0_platform_contract_mismatch_discard_rejects_memory_write(
    policy: AutoresearchPolicy,
) -> None:
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
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
            infra_rationale="The initial coverage proof passed before review found defects.",
        ),
        policy,
    )
    state = advance_state(state, _review_result(ReviewVerdict.FAIL, policy), policy)
    state = advance_state(state, _fix_result(FixTriggerPhase.REVIEW), policy)
    for _ in range(2):
        state = advance_state(state, _g0_platform_contract_mismatch_bug_signal(), policy)
        state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    state = advance_state(state, _g0_platform_contract_mismatch_bug_signal(), policy)

    with pytest.raises(
        AutoresearchValidationError,
        match="DISCARD final decision is not eligible for MemPalace retention",
    ):
        advance_state(
            state,
            FinalDecisionArtifact(
                experiment_id="g0-iteration-45",
                decision=FinalDecision.DISCARD,
                recommended_metric_name="coverage gate",
                recommended_metric_value=0.0,
                reviewer_verdict=FinalReviewerVerdict.FAIL,
                rationale="Coverage contract proof stayed unverifiable after the bounded fix path.",
                log_summary="Discarded after repeated platform coverage contract mismatch.",
                continue_loop=True,
                memory_write_required=True,
            ),
            policy,
        )


def test_exhausted_g0_platform_contract_mismatch_discard_without_memory_starts_next_iteration(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = advance_state(
        AutoresearchState(platform_readiness=platform_readiness.identity()),
        _setup_artifact(),
        policy,
    )
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
            infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
            infra_rationale="The initial coverage proof passed before review found defects.",
        ),
        policy,
    )
    state = advance_state(state, _review_result(ReviewVerdict.FAIL, policy), policy)
    state = advance_state(state, _fix_result(FixTriggerPhase.REVIEW), policy)
    for _ in range(2):
        state = advance_state(state, _g0_platform_contract_mismatch_bug_signal(), policy)
        state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    state = advance_state(state, _g0_platform_contract_mismatch_bug_signal(), policy)

    result = advance_state(
        state,
        FinalDecisionArtifact(
            experiment_id="g0-iteration-45",
            decision=FinalDecision.DISCARD,
            recommended_metric_name="coverage gate",
            recommended_metric_value=0.0,
            reviewer_verdict=FinalReviewerVerdict.FAIL,
            rationale="Coverage contract proof stayed unverifiable after the bounded fix path.",
            log_summary="Discarded after repeated platform coverage contract mismatch.",
            continue_loop=True,
            memory_write_required=False,
        ),
        policy,
    )
    action = next_action(result, policy, receipts, platform_readiness)

    assert result.phase is Phase.REPEAT
    assert result.final_decision is not None
    assert result.final_decision.decision is FinalDecision.DISCARD
    assert result.final_decision.memory_write_required is False
    assert can_write_memory(result) is False
    assert action.phase is Phase.REPEAT
    assert action.expected_artifact_type is ArtifactType.NEXT_ITERATION
    assert action.next_agent_ids == ()


def test_persisted_alpha_discard_without_verification_is_not_an_authorized_no_memory_terminal(
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
    unverified_discard = replace(
        state,
        phase=Phase.REPEAT,
        final_decision=FinalDecisionArtifact(
            experiment_id="alpha-unverified-discard-1",
            decision=FinalDecision.DISCARD,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=-0.6,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="No completed verification exists for this proposed discard.",
            log_summary="Forged unverified alpha discard.",
            continue_loop=True,
            memory_write_required=False,
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="authorized no-memory terminal",
    ):
        validate_state(unverified_discard, policy)


def test_start_next_rejects_unverified_alpha_discard_no_memory_terminal(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    unverified_discard = replace(
        state,
        phase=Phase.REPEAT,
        final_decision=FinalDecisionArtifact(
            experiment_id="alpha-unverified-discard-1",
            decision=FinalDecision.DISCARD,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=-0.6,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="No completed verification exists for this proposed discard.",
            log_summary="Forged unverified alpha discard.",
            continue_loop=True,
            memory_write_required=False,
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="policy-approved no-memory final decision",
    ):
        start_next_iteration(unverified_discard, readiness=platform_readiness)


def test_persisted_suspended_alpha_infra_blocked_no_memory_state_is_rejected(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_decision(policy)
    impossible = replace(
        state,
        phase=Phase.REPEAT,
        final_decision=FinalDecisionArtifact(
            experiment_id="alpha-infra-blocked-1",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.38,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Incorrectly blocked alpha research on infrastructure.",
            log_summary="Impossible alpha infrastructure blocker.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Alpha research cannot own infrastructure gate remediation.",
        ),
        suspended=True,
        suspension_reason="Alpha research cannot own infrastructure gate remediation.",
    )
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(impossible.to_dict())))

    with pytest.raises(AutoresearchValidationError, match="explicit operator-owned"):
        validate_state(persisted, policy)


def test_operator_infrastructure_suspension_finalizes_active_alpha_verification(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)

    suspended = suspend_for_infrastructure(
        state,
        "Operator is repairing the historical market-data service.",
    )

    assert suspended.phase is Phase.REPEAT
    assert suspended.suspended is True
    assert suspended.suspension_reason == (
        "Operator is repairing the historical market-data service."
    )
    assert suspended.memory_written is False
    assert suspended.memory_verification_receipt is None
    assert suspended.setup == state.setup
    assert suspended.context_packet == state.context_packet
    assert suspended.consensus_history == state.consensus_history
    assert suspended.implementation_result == state.implementation_result
    assert suspended.final_decision == FinalDecisionArtifact(
        experiment_id="iteration-1",
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name=OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
        rationale=OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
        log_summary=OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,
        continue_loop=True,
        memory_write_required=False,
        infra_rationale="Operator is repairing the historical market-data service.",
    )


def test_operator_infrastructure_suspension_round_trip_validates(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_decision(policy, platform_readiness)
    suspended = suspend_for_infrastructure(state, "Operator is rotating data credentials.")

    persisted = AutoresearchState.from_dict(json.loads(json.dumps(suspended.to_dict())))

    validate_state(persisted, policy)


def test_operator_infrastructure_suspension_uses_latest_reviewer_verdict(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_decision(policy, platform_readiness)

    suspended = suspend_for_infrastructure(state, "Operator is rotating data credentials.")

    assert suspended.final_decision is not None
    assert suspended.final_decision.reviewer_verdict is FinalReviewerVerdict.PASS


@pytest.mark.parametrize(
    "reason",
    ["", "   "],
)
def test_operator_infrastructure_suspension_rejects_empty_reason(
    policy: AutoresearchPolicy,
    reason: str,
) -> None:
    state = _state_to_decision(policy)

    with pytest.raises(AutoresearchValidationError, match="non-empty reason"):
        suspend_for_infrastructure(state, reason)


def test_operator_infrastructure_suspension_rejects_already_suspended_state(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_decision(policy, platform_readiness)
    suspended = suspend_for_infrastructure(state, "Operator is repairing infrastructure.")

    with pytest.raises(AutoresearchValidationError, match="already suspended"):
        suspend_for_infrastructure(suspended, "Operator is repairing infrastructure.")


def test_operator_infrastructure_suspension_rejects_finalized_repeat_state(
    policy: AutoresearchPolicy,
) -> None:
    state = advance_state(_state_to_decision(policy), _final_decision(), policy)

    with pytest.raises(AutoresearchValidationError, match="already finalized or in repeat"):
        suspend_for_infrastructure(state, "Operator is repairing infrastructure.")


@pytest.mark.parametrize(
    "missing_prerequisite",
    ["setup", "context_packet", "platform_readiness"],
)
def test_operator_infrastructure_suspension_requires_active_alpha_prerequisites(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
    missing_prerequisite: str,
) -> None:
    state = _state_to_decision(policy, platform_readiness)
    if missing_prerequisite == "setup":
        state = replace(state, setup=None)
    elif missing_prerequisite == "context_packet":
        state = replace(state, context_packet=None)
    else:
        state = replace(state, platform_readiness=None)

    with pytest.raises(AutoresearchValidationError, match="requires setup, context packet"):
        suspend_for_infrastructure(state, "Operator is repairing infrastructure.")


def test_agent_final_decision_cannot_create_an_operator_infrastructure_suspension(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_decision(policy, platform_readiness)
    artifact = FinalDecisionArtifact(
        experiment_id="iteration-1",
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name=OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.PASS,
        rationale=OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
        log_summary=OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,
        continue_loop=True,
        memory_write_required=False,
        infra_rationale="Operator is repairing infrastructure.",
    )

    with pytest.raises(AutoresearchValidationError, match="dedicated operator transition"):
        advance_state(state, artifact, policy)


def test_g0_stage_receipt_cannot_create_a_suspended_infra_blocked_state(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_g0_decision(
        policy,
        readiness=platform_readiness,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
    )
    artifact = FinalDecisionArtifact(
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

    with pytest.raises(AutoresearchValidationError, match="non-suspending DISCARD"):
        advance_state(state, artifact, policy)


def test_persisted_g0_infra_repaired_rejects_memory_write_contract(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_g0_decision(
        policy,
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
    )
    invalid = replace(
        state,
        phase=Phase.REPEAT,
        final_decision=FinalDecisionArtifact(
            experiment_id="g0-iteration-1",
            decision=FinalDecision.INFRA_REPAIRED,
            recommended_metric_name="coverage gate",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Data repair completed.",
            log_summary="G0 gate passed.",
            continue_loop=True,
            memory_write_required=True,
            infra_rationale="Cap/source provenance is now present for the declared sleeve.",
        ),
    )
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(invalid.to_dict())))

    with pytest.raises(
        AutoresearchValidationError,
        match="INFRA_REPAIRED final decision is not eligible for MemPalace retention",
    ):
        validate_state(persisted, policy)


def test_persisted_g0_infra_repaired_state_fails_closed_without_readiness_context(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state, _ = _persisted_g0_infra_repaired_repeat_state(policy, platform_readiness)

    with pytest.raises(
        AutoresearchValidationError,
        match="DATA_INFRA_G0 platform coverage requires a strict readiness validation context",
    ):
        validate_state(state, policy)


def test_persisted_g0_infra_repaired_state_validates_and_routes_with_readiness_context(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state, context = _persisted_g0_infra_repaired_repeat_state(policy, platform_readiness)

    validate_state(state, policy, context)

    action = next_action(state, policy, receipts, platform_readiness)

    assert action.phase is Phase.REPEAT
    assert action.expected_artifact_type.value == "next_iteration"


def test_persisted_alpha_state_validation_ignores_readiness_calendar_binding(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_review(policy, platform_readiness)
    persisted = AutoresearchState.from_dict(json.loads(json.dumps(state.to_dict())))
    context = AutoresearchValidationContext.from_readiness(platform_readiness)

    validate_state(persisted, policy, context)


def test_no_implementation_without_majority(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _no_consensus(round_number=1), policy)
    invalid = replace(state, phase=Phase.IMPLEMENTATION)

    with pytest.raises(AutoresearchValidationError, match="majority consensus"):
        next_action(invalid, policy, receipts, platform_readiness)

    with pytest.raises(AutoresearchValidationError, match="majority"):
        advance_state(invalid, _implementation_result(), policy)


def test_review_fix_cycle_routes_back_through_verification(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_review(policy, platform_readiness)

    state = advance_state(state, _review_result(ReviewVerdict.FAIL, policy), policy)
    assert state.phase is Phase.FIX_TEST
    assert next_action(state, policy, receipts, platform_readiness).next_agent_ids == (
        policy.fixer.agent_id,
    )

    state = advance_state(state, _fix_result(FixTriggerPhase.REVIEW), policy)
    assert state.phase is Phase.VERIFICATION

    state = advance_state(state, _verification_result(VerificationStatus.PASS), policy)
    assert state.phase is Phase.REVIEW
    assert next_action(state, policy, receipts, platform_readiness).next_agent_ids == (
        policy.reviewer.agent_id,
    )


def test_repeat_phase_requires_final_decision(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = replace(
        AutoresearchState(platform_readiness=platform_readiness.identity()),
        phase=Phase.REPEAT,
    )

    with pytest.raises(AutoresearchValidationError, match="repeat phase requires final_decision"):
        next_action(state, policy, receipts, platform_readiness)


def test_debate_phase_requires_context_packet(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = replace(
        AutoresearchState(
            setup=_setup_artifact(),
            platform_readiness=platform_readiness.identity(),
        ),
        phase=Phase.DEBATE,
    )

    with pytest.raises(AutoresearchValidationError, match="debate phase requires a context_packet"):
        next_action(state, policy, receipts, platform_readiness)


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


def test_test_failure_persists_without_fabricating_unavailable_metrics(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    artifact = replace(
        _verification_result(VerificationStatus.TEST_FAILURE, external_panel_failure=True),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        data_coverage=None,
    )

    parsed = VerificationResultArtifact.from_dict(artifact.to_dict(), mode=state.mode)
    next_state = advance_state(state, parsed, policy)

    assert next_state.phase is Phase.FIX_TEST
    assert next_state.latest_verification is not None
    assert next_state.latest_verification.data_coverage is None
    assert next_state.latest_verification.oos_sharpe_net is None


def test_alpha_pass_rejects_unavailable_metrics_or_coverage(
    policy: AutoresearchPolicy,
) -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        is_walk_forward_sharpe_net=None,
        data_coverage=None,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="PASS verification requires complete metrics and data_coverage",
    ):
        artifact.validate(mode=ResearchMode.ALPHA_RESEARCH)


def test_mode_none_pass_rejects_unavailable_metrics_or_coverage() -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        is_walk_forward_sharpe_net=None,
        data_coverage=None,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="PASS verification requires complete metrics and data_coverage",
    ):
        artifact.validate()


def test_g0_pass_with_null_alpha_metrics_and_coverage_parses() -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        data_coverage=None,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
        infra_rationale="Shared provider entitlement requires operator remediation.",
        platform_coverage_validation=_platform_coverage_receipt(
            status=PlatformCoverageStatus.REMEDIATION_REQUIRED
        ),
    )

    parsed = VerificationResultArtifact.from_dict(
        artifact.to_dict(), mode=ResearchMode.DATA_INFRA_G0
    )

    assert parsed.status is VerificationStatus.PASS
    assert parsed.data_coverage is None
    assert parsed.infra_gate_outcome is InfraGateOutcome.REMEDIATION_REQUIRED


def test_g0_pass_rejects_partial_universe_and_hydration_receipts() -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        data_coverage=None,
        infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
        infra_rationale="Shared provider entitlement requires operator remediation.",
        price_hydration_receipt=None,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="universe and price hydration receipts must both be present or both be null",
    ):
        artifact.validate(mode=ResearchMode.DATA_INFRA_G0)


def test_g0_pass_requires_paired_platform_universe_and_hydration_receipts() -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
        infra_rationale="Every source and cap record has auditable provenance.",
        platform_coverage_validation=None,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="platform_coverage_contract_mismatch BUG_SIGNAL",
    ):
        artifact.validate(mode=ResearchMode.DATA_INFRA_G0)


@pytest.mark.parametrize(
    ("infra_gate_outcome", "infra_rationale"),
    (
        (None, "Shared provider entitlement requires operator remediation."),
        (InfraGateOutcome.REMEDIATION_REQUIRED, None),
    ),
)
def test_g0_pass_requires_gate_outcome_and_rationale(
    infra_gate_outcome: InfraGateOutcome | None,
    infra_rationale: str | None,
) -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        infra_gate_outcome=infra_gate_outcome,
        infra_rationale=infra_rationale,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="DATA_INFRA_G0 verification requires infra_gate_outcome and infra_rationale",
    ):
        artifact.validate(mode=ResearchMode.DATA_INFRA_G0)


def test_verification_requires_explicit_data_coverage_key() -> None:
    raw = _verification_result(VerificationStatus.TEST_FAILURE).to_dict()
    raw.pop("data_coverage")

    with pytest.raises(
        AutoresearchValidationError,
        match=r"exact keys.*data_coverage",
    ):
        VerificationResultArtifact.from_dict(raw)


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
    assert (
        fixed.implementation_result.price_hydration_scope_preflight
        == implementation.price_hydration_scope_preflight
    )
    assert fixed.fix_history[-1].commit_sha == fixed.implementation_result.commit_sha


def test_fix_result_updates_price_hydration_preflight_for_reverification(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    implementation = _implementation_result()
    state = advance_state(state, implementation, policy)
    state = advance_state(state, _verification_result(VerificationStatus.BUG_SIGNAL), policy)
    updated_preflight = PriceHydrationScopePreflight(
        member_union_count=2,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
        session_count=2400,
        planned_symbol_sessions=4800,
        within_budget=True,
    )

    fixed = advance_state(
        state,
        _fix_result(
            FixTriggerPhase.VERIFICATION,
            price_hydration_scope_preflight=updated_preflight,
        ),
        policy,
    )

    assert fixed.phase is Phase.VERIFICATION
    assert fixed.implementation_result is not None
    assert fixed.implementation_result.commit_sha == "def5678"
    assert fixed.implementation_result.price_hydration_scope_preflight == updated_preflight
    assert fixed.fix_history[-1].price_hydration_scope_preflight == updated_preflight


def test_price_scope_bug_fix_rejects_hydrate_capable_commands(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.BUG_SIGNAL),
            bug_signals=("price_hydration_scope_exceeds_budget: 1521531 > 600000",),
        ),
        policy,
    )
    fix = replace(
        _fix_result(FixTriggerPhase.VERIFICATION),
        tests_rerun=("uv run python notebooks/experiments/generate_t107_oarc_results.py",),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="price-scope BUG_SIGNAL fix_result must not include hydrate-capable commands",
    ):
        advance_state(state, fix, policy)


def test_price_scope_bug_fix_prompt_forbids_hydrate_capable_commands(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.BUG_SIGNAL),
            bug_signals=("price_hydration_scope_exceeds_budget: 1521531 > 600000",),
        ),
        policy,
    )

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "price_hydration_scope_exceeds_budget BUG_SIGNAL" in prompt
    assert "do not run any hydrate-capable command" in prompt
    assert "generate_*results" in prompt
    assert "fix_result.tests_rerun" in prompt


def test_verification_failure_routes_fix_test_with_pending_trigger(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.BUG_SIGNAL), policy)

    assert state.phase is Phase.FIX_TEST
    assert state.pending_fix_trigger is FixTriggerPhase.VERIFICATION
    assert next_action(state, policy, receipts, platform_readiness).next_agent_ids == (
        policy.fixer.agent_id,
    )


@pytest.mark.parametrize(
    ("decision", "metric_value", "reviewer_verdict", "match"),
    [
        (
            FinalDecision.DISCARD,
            0.38,
            FinalReviewerVerdict.PASS,
            "decision Sharpe above baseline requires a KEEP-family final_decision",
        ),
        (
            FinalDecision.KEEP,
            0.7,
            FinalReviewerVerdict.PASS,
            "decision Sharpe > 0.5 requires SIGNIFICANT KEEP or STRONG KEEP",
        ),
        (
            FinalDecision.SIGNIFICANT_KEEP,
            1.2,
            FinalReviewerVerdict.PASS,
            "decision Sharpe > 1.0 with reviewer PASS requires final_decision=STRONG KEEP",
        ),
        (
            FinalDecision.KEEP,
            -0.6,
            FinalReviewerVerdict.PASS,
            "decision Sharpe <= -0.5 requires final_decision=DISCARD",
        ),
    ],
)
def test_final_decision_rules_reject_incorrect_decisions(
    policy: AutoresearchPolicy,
    decision: FinalDecision,
    metric_value: float,
    reviewer_verdict: FinalReviewerVerdict,
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
                reviewer_verdict=FinalReviewerVerdict.PASS,
            ),
            policy,
        )


def test_final_decision_requires_memory_for_completed_alpha_verification(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_decision(policy)
    decision = replace(_final_decision(), memory_write_required=False)

    with pytest.raises(
        AutoresearchValidationError,
        match="ALPHA_RESEARCH completed PASS final decisions require memory_write_required=true",
    ):
        advance_state(state, decision, policy)


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
            replace(
                _final_decision_with(
                    decision=FinalDecision.DISCARD,
                    metric_value=None,
                    reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
                ),
                memory_write_required=False,
            ),
            policy,
        )


def test_repeated_bug_signal_routes_to_discard_decision(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    for _ in range(2):
        state = advance_state(state, _verification_result(VerificationStatus.BUG_SIGNAL), policy)
        state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)

    state = advance_state(state, _verification_result(VerificationStatus.BUG_SIGNAL), policy)

    assert state.phase is Phase.DECISION_LOG
    assert state.verification_fix_attempts == 2
    with pytest.raises(
        AutoresearchValidationError,
        match="bug signals after retries require final_decision=DISCARD",
    ):
        advance_state(
            state,
            replace(
                _final_decision_with(
                    decision=FinalDecision.CRASH,
                    metric_value=None,
                    reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
                ),
                memory_write_required=False,
            ),
            policy,
        )

    result = advance_state(
        state,
        replace(
            _final_decision_with(
                decision=FinalDecision.DISCARD,
                metric_value=None,
                reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            ),
            memory_write_required=False,
        ),
        policy,
    )

    assert result.phase is Phase.REPEAT
    assert result.final_decision is not None
    assert result.final_decision.decision is FinalDecision.DISCARD
    assert result.final_decision.memory_write_required is False


def test_crash_without_review_accepts_the_canonical_not_run_verdict(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    for _ in range(2):
        state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)
        state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)
    decision = _final_decision_with(
        decision=FinalDecision.CRASH,
        metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
    )
    decision = replace(decision, memory_write_required=False)

    result = advance_state(state, decision, policy)

    assert result.final_decision is not None
    assert result.final_decision.reviewer_verdict is FinalReviewerVerdict.NOT_RUN


def test_reviewed_final_decision_rejects_not_run_verdict(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_decision(policy)

    with pytest.raises(AutoresearchValidationError, match="reviewer_verdict must match"):
        advance_state(
            state,
            _final_decision_with(
                decision=FinalDecision.KEEP,
                metric_value=0.38,
                reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
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
                reviewer_verdict=FinalReviewerVerdict.FAIL,
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
                reviewer_verdict=FinalReviewerVerdict.PASS,
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
            replace(
                _final_decision_with(
                    decision=FinalDecision.DISCARD,
                    metric_value=None,
                    reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
                ),
                memory_write_required=False,
            ),
            policy,
        )


def test_alpha_implementation_prompt_batches_history_and_hydrates_union_once(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "one qp.security_universe_history() operation per batch" in prompt
    assert "qp.prices() exactly once for that union" in prompt
    assert "derive and prewarm the platform data plan before creating or running" in prompt
    assert "Quantipy runtime owns authoritative panel creation" in prompt
    assert "receipts remain runtime-owned" in prompt
    assert "stages must not import quantipy or use network, provider, SQL, filesystem" in prompt
    assert "v2 runtime intentionally gives stages only the immutable verified panel" in prompt
    assert "qp.security_universe_history() exactly once for all dates" not in prompt


def test_alpha_implementation_prompt_stops_over_budget_hydration(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "compute price_hydration_scope_preflight" in prompt
    assert "If within_budget is false" in prompt
    assert "do not run any qp.prices(), hydrate, full backtest" in prompt
    assert "structured feasibility BUG_SIGNAL" in prompt
    assert "qp.security_universe_history() exactly once over all dates" not in prompt


def test_alpha_implementation_rejects_missing_price_scope_preflight(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    with pytest.raises(
        AutoresearchValidationError,
        match="implementation_result requires price_hydration_scope_preflight",
    ):
        advance_state(
            state,
            replace(_implementation_result(), price_hydration_scope_preflight=None),
            policy,
        )


def test_over_budget_implementation_rejects_hydrate_commands(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    with pytest.raises(
        AutoresearchValidationError,
        match="must not include hydrate-capable commands",
    ):
        advance_state(
            state,
            replace(
                _implementation_result(),
                commands_run=("uv run python notebooks/experiments/generate_t107_oarc_results.py",),
                price_hydration_scope_preflight=PriceHydrationScopePreflight(
                    member_union_count=1_551,
                    experiment_start="2022-01-03",
                    experiment_end="2025-11-28",
                    timeframe="1min",
                    market_hours="regular",
                    session_count=981,
                    planned_symbol_sessions=1_521_531,
                    within_budget=False,
                ),
            ),
            policy,
        )


def test_alpha_verification_rejects_missing_price_scope_preflight(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = replace(
        state,
        phase=Phase.VERIFICATION,
        implementation_result=replace(
            _implementation_result(),
            price_hydration_scope_preflight=None,
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match=r"price_hydration_scope_preflight before dispatch",
    ):
        next_action(state, policy, receipts, platform_readiness)


def test_schema_v2_state_requires_archive_and_reinitialization(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    raw = state.to_dict()
    raw["schema_version"] = 2

    with pytest.raises(
        AutoresearchValidationError,
        match=r"archive the live schema-v2 state.*before restart",
    ):
        AutoresearchState.from_dict(raw)


def test_schema_v4_state_requires_archive_and_v5_reinitialization(
    policy: AutoresearchPolicy,
) -> None:
    raw = AutoresearchState().to_dict()
    raw["schema_version"] = 4

    with pytest.raises(
        AutoresearchValidationError,
        match=r"archive the live schema-v4 state.*fresh schema-v5 state.*autoresearch-init-state",
    ):
        AutoresearchState.from_dict(raw)


def test_schema_v5_requires_all_dispatch_a_state_keys(
    policy: AutoresearchPolicy,
) -> None:
    raw = AutoresearchState().to_dict()
    raw.pop("hypothesis_registry")

    with pytest.raises(AutoresearchValidationError, match="exact keys"):
        AutoresearchState.from_dict(raw)


def test_authoritative_state_digest_binds_campaign_review_history() -> None:
    state = AutoresearchState()
    reviewed = replace(
        state,
        campaign_review_history=(
            CampaignReviewRecord(
                triggered_iteration=1,
                reason="operator review",
                counters=CampaignCounters.zero(),
                acknowledgement=None,
                acknowledged_iteration=None,
            ),
        ),
    )

    empty_digest = autoresearch_transitions.build_authoritative_state_reference(state).state_sha256
    reviewed_digest = autoresearch_transitions.build_authoritative_state_reference(
        reviewed
    ).state_sha256

    assert empty_digest != reviewed_digest


def test_v4_shaped_payload_hash_differs_from_the_v5_state_hash() -> None:
    state = AutoresearchState()
    v4_payload = state.to_dict()
    v4_payload["schema_version"] = 4
    for field_name in (
        "hypothesis_registry",
        "campaign_counters",
        "campaign_review_required",
        "campaign_review_reason",
        "campaign_review_history",
    ):
        v4_payload.pop(field_name)
    v4_canonical = json.dumps(v4_payload, sort_keys=True, separators=(",", ":"))
    v4_digest = sha256(
        "\n".join((AUTHORITATIVE_STATE_DIGEST_DOMAIN, v4_canonical)).encode("utf-8")
    ).hexdigest()

    v5_digest = autoresearch_transitions.build_authoritative_state_reference(state).state_sha256

    assert v5_digest != v4_digest


def test_g2_autoresearch_skill_receipt_hashes_actual_file_bytes(
    receipts: ReceiptCatalog,
) -> None:
    receipt = receipts.receipts["g2.autoresearch_skill"]

    assert receipt.sha256 == sha256(receipt.path.read_bytes()).hexdigest()


def test_decision_log_reason_truncation_strips_boundary_whitespace(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_decision(policy)
    decision = replace(
        _final_decision(),
        log_summary=("x" * 159) + " \t trailing text",
    )

    advanced = advance_state(state, decision, policy)
    restored = AutoresearchState.from_dict(json.loads(json.dumps(advanced.to_dict())))

    assert advanced.hypothesis_registry[-1].reason == "x" * 159
    assert restored.hypothesis_registry[-1].reason == "x" * 159


def test_infrastructure_suspension_without_consensus_records_none_shape(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    suspended = suspend_for_infrastructure(state, "Operator is repairing infrastructure.")
    entry = suspended.hypothesis_registry[-1]

    assert entry.consensus_status.value == "NONE"
    assert entry.contested_families == ()
    assert entry.family is None


def _prompt_without_dispatch_digests(prompt: str) -> tuple[str, dict[str, str]]:
    digest_values: dict[str, str] = {}
    template_lines: list[str] = []
    for line in prompt.splitlines():
        if line.startswith("STATE_REF="):
            state_reference = _round_trip_compact_json(line.removeprefix("STATE_REF="))
            digest_values["state_sha256"] = str(state_reference["state_sha256"])
            state_reference["state_sha256"] = "<state_sha256>"
            template_lines.append(
                "STATE_REF=" + json.dumps(state_reference, sort_keys=True, separators=(",", ":"))
            )
        elif line.startswith("INSTRUCTION_MANIFEST="):
            manifest = _round_trip_compact_json(line.removeprefix("INSTRUCTION_MANIFEST="))
            state_reference = cast(dict[str, object], manifest["state_reference"])
            state_reference["state_sha256"] = "<state_sha256>"
            template_lines.append(
                "INSTRUCTION_MANIFEST="
                + json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            )
        elif "=" in line and line.split("=", 1)[0] in {
            "state_reference_sha256",
            "source_manifest_sha256",
        }:
            key, value = line.split("=", 1)
            digest_values[key] = value
            template_lines.append(f"{key}=<{key}>")
        else:
            template_lines.append(line)
    return "\n".join(template_lines), digest_values


def test_empty_registry_prompt_is_template_invariant_but_digest_lines_change(
    receipts: ReceiptCatalog,
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    initial = AutoresearchState(platform_readiness=platform_readiness.identity())
    setup_done = advance_state(initial, _setup_artifact(), policy)
    context_done = advance_state(setup_done, _context_artifact(), policy)
    debate_done = advance_state(context_done, _debate_result(policy, round_number=1), policy)
    consensus_done = advance_state(
        debate_done,
        _majority_consensus(round_number=1, policy=policy),
        policy,
    )
    implementation_done = advance_state(consensus_done, _implementation_result(), policy)
    verification_done = advance_state(
        implementation_done,
        _verification_result(VerificationStatus.PASS),
        policy,
    )
    review_done = advance_state(
        verification_done,
        _review_result(ReviewVerdict.PASS, policy),
        policy,
    )

    for empty_registry in (
        initial,
        setup_done,
        context_done,
        debate_done,
        consensus_done,
        implementation_done,
        verification_done,
        review_done,
    ):
        changed_history = replace(
            empty_registry,
            campaign_review_history=(
                CampaignReviewRecord(
                    triggered_iteration=empty_registry.iteration,
                    reason="operator review",
                    counters=CampaignCounters.zero(),
                    acknowledgement=None,
                    acknowledged_iteration=None,
                ),
            ),
        )
        empty_prompt = next_action(empty_registry, policy, receipts, platform_readiness).prompt_text
        changed_prompt = next_action(
            changed_history, policy, receipts, platform_readiness
        ).prompt_text
        empty_template, empty_digests = _prompt_without_dispatch_digests(empty_prompt)
        changed_template, changed_digests = _prompt_without_dispatch_digests(changed_prompt)

        assert empty_template == changed_template
        assert set(empty_digests) == {
            "state_sha256",
            "state_reference_sha256",
            "source_manifest_sha256",
        }
        assert set(changed_digests) == set(empty_digests)
        assert all(empty_digests[key] != changed_digests[key] for key in empty_digests)


def test_populated_registry_golden_round_trip_preserves_state_and_reference_digest(
    policy: AutoresearchPolicy,
) -> None:
    state = advance_state(_state_to_decision(policy), _final_decision(), policy)
    restored = AutoresearchState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.to_dict() == state.to_dict()
    assert autoresearch_transitions.build_authoritative_state_reference(
        state
    ) == autoresearch_transitions.build_authoritative_state_reference(restored)


def test_schema_v3_state_rejects_missing_required_fix_field(
    policy: AutoresearchPolicy,
) -> None:
    state = _state_to_consensus(policy)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)
    state = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)
    raw = state.to_dict()
    fix_history = cast(list[dict[str, object]], raw["fix_history"])
    fix_history[0].pop("price_hydration_scope_preflight")

    with pytest.raises(AutoresearchValidationError, match="price_hydration_scope_preflight"):
        AutoresearchState.from_dict(raw)


def test_over_budget_price_scope_verification_prompt_forbids_hydrate(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(
            _implementation_result(),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1_551,
                experiment_start="2022-01-03",
                experiment_end="2025-11-28",
                timeframe="1min",
                market_hours="regular",
                session_count=981,
                planned_symbol_sessions=1_521_531,
                within_budget=False,
            ),
        ),
        policy,
    )

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "Runner-bound price hydration scope preflight" in prompt
    assert '"planned_symbol_sessions":1521531' in prompt
    assert "This exceeds budget" in prompt
    assert "Do not run any command that can call qp.prices()" in prompt
    assert "price_hydration_scope_exceeds_budget" in prompt


def test_over_budget_price_scope_rejects_non_budget_bug_verification(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(
            _implementation_result(),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1_551,
                experiment_start="2022-01-03",
                experiment_end="2025-11-28",
                timeframe="1min",
                market_hours="regular",
                session_count=981,
                planned_symbol_sessions=1_521_531,
                within_budget=False,
            ),
        ),
        policy,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="over-budget ALPHA price hydration preflight requires BUG_SIGNAL",
    ):
        advance_state(state, _verification_result(VerificationStatus.PASS), policy)


def test_over_budget_price_scope_accepts_budget_bug_signal(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(
            _implementation_result(),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1_551,
                experiment_start="2022-01-03",
                experiment_end="2025-11-28",
                timeframe="1min",
                market_hours="regular",
                session_count=981,
                planned_symbol_sessions=1_521_531,
                within_budget=False,
            ),
        ),
        policy,
    )
    artifact = replace(
        _verification_result(VerificationStatus.BUG_SIGNAL),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        bug_signals=("price_hydration_scope_exceeds_budget: 1521531 > 600000",),
        data_coverage=None,
        universe_verification_receipt=None,
        price_hydration_receipt=None,
    )

    next_state = advance_state(state, artifact, policy)

    assert next_state.phase is Phase.FIX_TEST
    assert next_state.pending_fix_trigger is FixTriggerPhase.VERIFICATION


def test_price_scope_pass_rejects_underreported_dynamic_coverage(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(
            _implementation_result(),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1_551,
                experiment_start="2022-01-03",
                experiment_end="2025-11-28",
                timeframe="1min",
                market_hours="regular",
                session_count=981,
                planned_symbol_sessions=1_521_531,
                within_budget=False,
            ),
        ),
        policy,
    )
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        data_coverage=replace(
            _dynamic_coverage_receipt(),
            member_union_count=1,
            expected_symbol_sessions=2400,
            covered_symbol_sessions=2400,
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="over-budget ALPHA price hydration preflight requires BUG_SIGNAL",
    ):
        advance_state(state, artifact, policy)


def test_price_scope_pass_requires_coverage_identity_match(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(
            _implementation_result(),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1,
                experiment_start="2021-01-04",
                experiment_end="2021-12-31",
                timeframe="1min",
                market_hours="regular",
                session_count=2400,
                planned_symbol_sessions=2400,
                within_budget=True,
            ),
        ),
        policy,
    )
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        data_coverage=replace(
            _dynamic_coverage_receipt(),
            experiment_start="2021-01-04",
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="dynamic coverage experiment_start must match price hydration",
    ):
        advance_state(state, artifact, policy)


def test_verification_prompt_requires_terminal_structured_artifact_persistence(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(
        state,
        replace(
            _implementation_result(),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1,
                experiment_start="2021-01-04",
                experiment_end="2021-12-31",
                timeframe="1min",
                market_hours="regular",
                session_count=MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS + 1,
                planned_symbol_sessions=MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS + 1,
                within_budget=False,
            ),
        ),
        policy,
    )
    state_path = tmp_path / "verification-state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

    prompt = next_action(
        state,
        policy,
        receipts,
        platform_readiness,
        state_path=state_path,
    ).prompt_text

    assert "Verification handoff contract" in prompt
    assert "structured JSON verification_result artifact" in prompt
    assert (
        "uv run gateway-cli autoresearch-advance "
        f"{json.dumps(str(state_path.resolve()))} "
        "/home/dev/.openclaw/workspace-autoresearch-pm/<artifact.json> "
        "--instruction-manifest-sha256 <source_manifest_sha256> "
        "--state-reference-sha256 <state_reference_sha256>"
    ) in prompt
    assert "before any prose completion or status report" in prompt
    assert "prose-only verification completion is invalid" in prompt
    assert "Persist and advance the JSON artifact" in prompt
    assert "commands_run" in prompt


def test_verification_prompt_requires_failure_classification_and_coverage_fields(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "status TEST_FAILURE with tests_passed=false" in prompt
    assert "status BUG_SIGNAL with nonempty bug_signals" in prompt
    assert "PASS only when tests passed" in prompt
    assert (
        "For ALPHA_RESEARCH PASS, require complete alpha metrics, compact dynamic "
        "data_coverage, and paired universe and price hydration receipts"
    ) in prompt
    assert "price hydration scope preflight" in prompt
    assert str(MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS) in prompt
    assert "price_hydration_scope_exceeds_budget" in prompt
    assert "do not run the hydrate/backtest command" in prompt
    assert "uv --directory /home/dev/repos/quantipy run --frozen --no-sync" in prompt
    assert "quantipy experiment run" in prompt
    assert "PYTHONDONTWRITEBYTECODE=1 quantipy experiment" not in prompt
    assert "/home/dev/repos/g2_openclaw/scripts/run-long-task.sh" in prompt
    assert "expected_artifact_path" in prompt
    assert "Direct foreground execution" in prompt
    assert "non-malicious same-host agent trust model" in prompt
    assert "verifier claim cannot replace it" in prompt
    assert "complete EOF drain" in prompt
    assert "bounded-tail truncation metadata" in prompt
    assert "exits 0 exactly for success=true and 1 exactly for success=false" in prompt
    assert "detached FAILED/exit 1 with no signal" in prompt
    assert "ordinary process_error classification" in prompt
    assert "detached run directory/manifest digest" in prompt
    assert "worker attestation" in prompt
    assert "artifact-supplied hash alone is never proof" in prompt
    for field_name in (
        "member_union_count",
        "member_union_digest",
        "experiment_start",
        "experiment_end",
        "oos_start",
        "oos_end",
        "expected_symbol_sessions",
        "covered_symbol_sessions",
        "missing_symbol_count",
        "missing_symbol_sessions",
        "default_fold_count",
        "fallback_fold_count",
    ):
        assert field_name in prompt


def test_alpha_pass_rejects_dynamic_coverage_over_price_scope_budget(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        data_coverage=replace(
            _dynamic_coverage_receipt(),
            expected_symbol_sessions=MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS + 1,
            covered_symbol_sessions=MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS + 1,
        ),
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="dynamic coverage expected_symbol_sessions must match price preflight",
    ):
        advance_state(state, artifact, policy)


def test_data_infra_dynamic_coverage_can_exceed_alpha_price_scope_budget() -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        is_walk_forward_sharpe_net=None,
        oos_sharpe_net=None,
        max_drawdown_pct=None,
        win_rate=None,
        trade_count=None,
        trades_per_day=None,
        oos_trading_days=None,
        data_coverage=replace(
            _dynamic_coverage_receipt(),
            expected_symbol_sessions=MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS + 1,
            covered_symbol_sessions=MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS + 1,
        ),
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
        infra_rationale="The infrastructure gate uses its own deterministic audit.",
    )

    artifact.validate(mode=ResearchMode.DATA_INFRA_G0)


def test_g0_verification_prompt_requires_infra_gate_rationale(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = advance_state(
        AutoresearchState(platform_readiness=platform_readiness.identity()),
        _setup_artifact(),
        policy,
    )
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

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "Mode contract: DATA_INFRA_G0" in prompt
    assert "infra_gate_outcome" in prompt
    assert "infra_rationale" in prompt
    assert "GATE_PASSED" in prompt
    assert "REMEDIATION_REQUIRED" in prompt
    assert "Do not use Sharpe as the gate rationale" in prompt
    assert (
        "REMEDIATION_REQUIRED is a valid completed verification outcome: emit PASS with "
        "tests_passed=true when commands, tests, and typed Quantipy runtime execution succeeded"
    ) in prompt
    assert (
        "A DATA_INFRA_G0 PASS may set alpha metrics and data_coverage to null when "
        "unavailable, but the platform gate requires runner-checkable implementation "
        "preflight plus paired universe, price hydration, and platform coverage receipts"
    ) in prompt
    assert "PriceCoverageResponse; it is not the hydration coverage_receipt_digest" in prompt


def test_g0_remediation_with_null_alpha_metrics_and_coverage_advances_to_review(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())
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

    next_state = advance_state(
        state,
        replace(
            _verification_result(VerificationStatus.PASS),
            is_walk_forward_sharpe_net=None,
            oos_sharpe_net=None,
            max_drawdown_pct=None,
            win_rate=None,
            trade_count=None,
            trades_per_day=None,
            oos_trading_days=None,
            data_coverage=None,
            infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
            infra_rationale="Shared provider entitlement requires operator remediation.",
            platform_coverage_validation=_platform_coverage_receipt(
                status=PlatformCoverageStatus.REMEDIATION_REQUIRED
            ),
        ),
        policy,
    )

    assert next_state.phase is Phase.REVIEW

    decision_state = advance_state(next_state, _review_result(ReviewVerdict.PASS, policy), policy)
    discarded = advance_state(
        decision_state,
        FinalDecisionArtifact(
            experiment_id="g0-null-evidence-1",
            decision=FinalDecision.DISCARD,
            recommended_metric_name="coverage gate",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Data infrastructure remains blocked.",
            log_summary="G0 gate still requires remediation.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Shared provider entitlement requires operator remediation.",
        ),
        policy,
    )

    assert discarded.final_decision is not None
    assert discarded.final_decision.decision is FinalDecision.DISCARD
    assert discarded.suspended is False
    assert can_write_memory(discarded) is False
    assert next_action(discarded, policy, receipts, platform_readiness).phase is Phase.REPEAT


def test_g0_platform_contract_mismatch_routes_to_fixer_as_canonical_bug_signal(
    policy: AutoresearchPolicy,
    g0_verification_state: AutoresearchState,
) -> None:
    artifact = replace(
        _verification_result(VerificationStatus.BUG_SIGNAL),
        bug_signals=("platform_coverage_contract_mismatch",),
        infra_gate_outcome=None,
        infra_rationale=None,
        platform_coverage_validation=None,
    )

    result = advance_state(g0_verification_state, artifact, policy)

    assert result.phase is Phase.FIX_TEST
    assert result.pending_fix_trigger is FixTriggerPhase.VERIFICATION


def test_g0_wrong_scope_receipt_is_rejected_without_state_mutation(
    policy: AutoresearchPolicy,
    g0_verification_state: AutoresearchState,
) -> None:
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        platform_coverage_validation=_platform_coverage_receipt(
            scope=PlatformCoverageScope.PIT_ACTIVE_ROSTER
        ),
    )
    original = g0_verification_state

    with pytest.raises(
        AutoresearchValidationError,
        match="canonical BUG_SIGNAL artifact",
    ):
        advance_state(g0_verification_state, artifact, policy)

    assert g0_verification_state == original


def test_digest_valid_remediation_receipt_cannot_authorize_suspension(
    policy: AutoresearchPolicy,
    g0_verification_state: AutoresearchState,
) -> None:
    verified = advance_state(
        g0_verification_state,
        replace(
            _verification_result(VerificationStatus.PASS),
            infra_gate_outcome=InfraGateOutcome.REMEDIATION_REQUIRED,
            infra_rationale="Provider entitlement needs remediation.",
            platform_coverage_validation=_platform_coverage_receipt(
                status=PlatformCoverageStatus.REMEDIATION_REQUIRED
            ),
        ),
        policy,
    )
    decision_state = advance_state(verified, _review_result(ReviewVerdict.PASS, policy), policy)
    decision = FinalDecisionArtifact(
        experiment_id="g0-forged-remediation-1",
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name="coverage gate",
        recommended_metric_value=None,
        reviewer_verdict=FinalReviewerVerdict.PASS,
        rationale="Data infrastructure remains blocked.",
        log_summary="G0 gate still requires remediation.",
        continue_loop=True,
        memory_write_required=False,
        infra_rationale="Provider entitlement needs remediation.",
    )

    with pytest.raises(AutoresearchValidationError, match="non-suspending DISCARD"):
        advance_state(decision_state, decision, policy)


def test_g0_complete_receipt_with_preflight_identity_mismatch_fails_closed(
    policy: AutoresearchPolicy,
    g0_verification_state: AutoresearchState,
) -> None:
    implementation = g0_verification_state.implementation_result
    assert implementation is not None
    preflight = implementation.price_hydration_scope_preflight
    assert preflight is not None
    mismatched = replace(
        g0_verification_state,
        implementation_result=replace(
            implementation,
            price_hydration_scope_preflight=replace(
                preflight,
                experiment_start="2021-01-04",
            ),
        ),
    )
    artifact = replace(
        _verification_result(VerificationStatus.PASS),
        infra_gate_outcome=InfraGateOutcome.GATE_PASSED,
        infra_rationale="Coverage is complete.",
    )

    with pytest.raises(AutoresearchValidationError, match="outside pinned XNYS evidence"):
        advance_state(mismatched, artifact, policy)
