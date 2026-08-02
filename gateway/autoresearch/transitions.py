"""State-consuming validation and transition-boundary helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch.constants import (
    _OPERATOR_PRECONDITION_BRIEF_MARKERS as _OPERATOR_PRECONDITION_BRIEF_MARKERS,
)
from gateway.autoresearch.constants import (
    _OPERATOR_PRECONDITION_MARKERS as _OPERATOR_PRECONDITION_MARKERS,
)
from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_DIGEST_DOMAIN as AUTHORITATIVE_STATE_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_REFERENCE_VERSION as AUTHORITATIVE_STATE_REFERENCE_VERSION,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_STATE_PATH as DEFAULT_AUTORESEARCH_STATE_PATH,
)
from gateway.autoresearch.constants import (
    HYDRATE_CAPABLE_COMMAND_RE as HYDRATE_CAPABLE_COMMAND_RE,
)
from gateway.autoresearch.constants import (
    MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS as MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS,
)
from gateway.autoresearch.constants import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY as OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,  # noqa: E501
)
from gateway.autoresearch.constants import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME as OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,  # noqa: E501
)
from gateway.autoresearch.constants import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE as OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
)
from gateway.autoresearch.enums import (
    ConsensusStatus as ConsensusStatus,
)
from gateway.autoresearch.enums import (
    FinalDecision as FinalDecision,
)
from gateway.autoresearch.enums import (
    FinalReviewerVerdict as FinalReviewerVerdict,
)
from gateway.autoresearch.enums import (
    InfraGateOutcome as InfraGateOutcome,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.enums import (
    ReviewVerdict as ReviewVerdict,
)
from gateway.autoresearch.enums import (
    VerificationStatus as VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _sha256_text as _sha256_text,
)
from gateway.autoresearch.fields import (
    quantipy_member_union_digest as quantipy_member_union_digest,
)
from gateway.autoresearch.manifest import (
    AuthoritativeStateReference as AuthoritativeStateReference,
)
from gateway.autoresearch.receipts import (
    DynamicUniverseCoverageReceipt as DynamicUniverseCoverageReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    _verify_member_union_manifest as _verify_member_union_manifest,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch_platform_validation import (
    PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL as PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,
)
from gateway.autoresearch_platform_validation import (
    PlatformCoverageStatus as PlatformCoverageStatus,
)

if TYPE_CHECKING:
    from gateway.autoresearch.artifacts import (
        ConsensusResultArtifact as ConsensusResultArtifact,
    )
    from gateway.autoresearch.artifacts import (
        FinalDecisionArtifact as FinalDecisionArtifact,
    )
    from gateway.autoresearch.artifacts import (
        FixResultArtifact as FixResultArtifact,
    )
    from gateway.autoresearch.artifacts import (
        ImplementationResultArtifact as ImplementationResultArtifact,
    )
    from gateway.autoresearch.artifacts import (
        PriceHydrationScopePreflight as PriceHydrationScopePreflight,
    )
    from gateway.autoresearch.artifacts import (
        VerificationResultArtifact as VerificationResultArtifact,
    )
    from gateway.autoresearch.receipts import (
        PriceHydrationReceipt as PriceHydrationReceipt,
    )
    from gateway.autoresearch.receipts import (
        UniverseVerificationReceipt as UniverseVerificationReceipt,
    )
    from gateway.autoresearch.state import (
        AutoresearchValidationContext as AutoresearchValidationContext,
    )
    from gateway.autoresearch_platform_validation import (
        DynamicPriceCoverageReceipt as DynamicPriceCoverageReceipt,
    )


KEEP_DECISIONS = frozenset(
    {FinalDecision.KEEP, FinalDecision.SIGNIFICANT_KEEP, FinalDecision.STRONG_KEEP}
)


def _canonical_iteration_experiment_id(iteration: int) -> str:
    if iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    return f"iteration-{iteration}"


def _is_operator_precondition_consensus(
    consensus: ConsensusResultArtifact | None,
) -> bool:
    """Return true for a majority that deliberately requires operator action."""
    if consensus is None or consensus.status is not ConsensusStatus.MAJORITY:
        return False
    id_text = " ".join(
        value.lower()
        for value in (
            consensus.winner_theory_id,
            consensus.winner_theory_family,
        )
        if value
    )
    if any(marker in id_text for marker in _OPERATOR_PRECONDITION_MARKERS):
        return True
    brief = (consensus.implementation_brief or "").lower()
    return all(marker in brief for marker in _OPERATOR_PRECONDITION_BRIEF_MARKERS)


def _validate_alpha_verification_price_preflight(state: AutoresearchState) -> None:
    if state.phase is not Phase.VERIFICATION or state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result"
        )
    if state.implementation_result.price_hydration_scope_preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result."
            "price_hydration_scope_preflight before dispatch"
        )


def _validate_alpha_implementation_price_preflight(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    preflight = artifact.price_hydration_scope_preflight
    if preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH implementation_result requires price_hydration_scope_preflight"
        )
    if preflight.within_budget:
        return
    hydrate_commands = tuple(
        command for command in artifact.commands_run if HYDRATE_CAPABLE_COMMAND_RE.search(command)
    )
    if hydrate_commands:
        raise AutoresearchValidationError(
            "over-budget ALPHA implementation_result must not include hydrate-capable "
            f"commands: {', '.join(hydrate_commands)}"
        )


def _latest_verification_is_price_scope_bug_signal(state: AutoresearchState) -> bool:
    latest = state.latest_verification
    return (
        latest is not None
        and latest.status is VerificationStatus.BUG_SIGNAL
        and any("price_hydration_scope_exceeds_budget" in signal for signal in latest.bug_signals)
    )


def _validate_price_scope_fix_result_commands(
    state: AutoresearchState,
    artifact: FixResultArtifact,
) -> None:
    if not _latest_verification_is_price_scope_bug_signal(state):
        return
    hydrate_commands = tuple(
        command for command in artifact.tests_rerun if HYDRATE_CAPABLE_COMMAND_RE.search(command)
    )
    if hydrate_commands:
        raise AutoresearchValidationError(
            "price-scope BUG_SIGNAL fix_result must not include hydrate-capable "
            f"commands: {', '.join(hydrate_commands)}"
        )


def _validate_alpha_price_preflight_matches_receipts(
    preflight: PriceHydrationScopePreflight,
    artifact: VerificationResultArtifact,
) -> None:
    if isinstance(artifact.data_coverage, DynamicUniverseCoverageReceipt):
        for field_name in (
            "member_union_count",
            "experiment_start",
            "experiment_end",
            "timeframe",
            "market_hours",
        ):
            if getattr(artifact.data_coverage, field_name) != getattr(preflight, field_name):
                raise AutoresearchValidationError(
                    f"dynamic coverage {field_name} must match price preflight"
                )
        if artifact.data_coverage.expected_symbol_sessions != preflight.planned_symbol_sessions:
            raise AutoresearchValidationError(
                "dynamic coverage expected_symbol_sessions must match price preflight"
            )
    if artifact.price_hydration_receipt is not None:
        for field_name in (
            "member_union_count",
            "experiment_start",
            "experiment_end",
            "timeframe",
            "market_hours",
        ):
            if getattr(artifact.price_hydration_receipt, field_name) != getattr(
                preflight, field_name
            ):
                raise AutoresearchValidationError(
                    f"price hydration {field_name} must match price preflight"
                )


def _validate_alpha_price_scope_verification(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result"
        )
    preflight = state.implementation_result.price_hydration_scope_preflight
    if preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result."
            "price_hydration_scope_preflight before artifact acceptance"
        )
    if preflight.within_budget:
        _validate_alpha_price_preflight_matches_receipts(preflight, artifact)
        if (
            artifact.status is VerificationStatus.PASS
            and isinstance(artifact.data_coverage, DynamicUniverseCoverageReceipt)
            and preflight.planned_symbol_sessions > MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS
        ):
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH PASS dynamic coverage exceeds the alpha price "
                f"hydration budget of {MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS}"
            )
        return
    if artifact.status is not VerificationStatus.BUG_SIGNAL:
        raise AutoresearchValidationError(
            "over-budget ALPHA price hydration preflight requires BUG_SIGNAL verification"
        )
    if not any("price_hydration_scope_exceeds_budget" in signal for signal in artifact.bug_signals):
        raise AutoresearchValidationError(
            "over-budget ALPHA price hydration preflight requires "
            "price_hydration_scope_exceeds_budget bug signal"
        )
    if (
        artifact.data_coverage is not None
        or artifact.universe_verification_receipt is not None
        or artifact.price_hydration_receipt is not None
        or artifact.is_walk_forward_sharpe_net is not None
        or artifact.oos_sharpe_net is not None
        or artifact.max_drawdown_pct is not None
        or artifact.win_rate is not None
        or artifact.trade_count is not None
        or artifact.trades_per_day is not None
        or artifact.oos_trading_days is not None
    ):
        raise AutoresearchValidationError(
            "over-budget ALPHA price hydration BUG_SIGNAL must not include "
            "hydrate-dependent metrics, coverage, or receipts"
        )


def _platform_receipt_has_expected_runner_provenance(
    receipt: DynamicPriceCoverageReceipt,
    *,
    preflight: PriceHydrationScopePreflight,
    universe: UniverseVerificationReceipt,
    hydration: PriceHydrationReceipt,
    requested_sessions: Sequence[date] | None = None,
) -> bool:
    """Return whether a canonical receipt is independently bound to runner evidence."""
    try:
        receipt.validate()
        preflight.validate()
        hydration.validate_against_universe(universe)
        member_union_symbols = _verify_member_union_manifest(universe)
    except ValueError:
        return False
    try:
        quantipy_member_union_count, quantipy_member_union_sha256 = quantipy_member_union_digest(
            member_union_symbols
        )
    except ValueError:
        return False
    sessions_match = True
    if requested_sessions is not None:
        sessions = tuple(requested_sessions)
        try:
            receipt.validate_requested_sessions(sessions)
        except ValueError:
            sessions_match = False
    return (
        receipt.matches_shared_contract
        and receipt.timeframe == "1min"
        and receipt.source_timeframe == "1min"
        and receipt.requested_start_date == preflight.experiment_start
        and receipt.requested_end_date == preflight.experiment_end
        and receipt.source_requested_start_date == preflight.experiment_start
        and receipt.source_requested_end_date == preflight.experiment_end
        and receipt.timeframe == preflight.timeframe == hydration.timeframe
        and receipt.market_hours.value == preflight.market_hours == hydration.market_hours
        and receipt.source_timeframe == preflight.timeframe
        and receipt.source_market_hours.value == preflight.market_hours
        and receipt.member_union_count
        == preflight.member_union_count
        == universe.member_union_count
        == hydration.member_union_count
        == quantipy_member_union_count
        and receipt.requested_session_count == preflight.session_count
        and receipt.hydrated_symbol_sessions == preflight.planned_symbol_sessions
        and universe.member_union_digest == hydration.member_union_digest
        and receipt.member_union_digest == quantipy_member_union_sha256
        and receipt.source_price_coverage_response_digest
        == hydration.source_price_coverage_response_digest
        and sessions_match
    )


def _requested_sessions_for_preflight(
    preflight: PriceHydrationScopePreflight,
    validation_context: AutoresearchValidationContext | None,
) -> tuple[date, ...]:
    if validation_context is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage requires a strict readiness validation context"
        )
    start = date.fromisoformat(preflight.experiment_start)
    end = date.fromisoformat(preflight.experiment_end)
    if not validation_context.xnys_sessions:
        raise AutoresearchValidationError("XNYS calendar evidence contains no sessions")
    evidence_start = validation_context.xnys_range_start or validation_context.xnys_sessions[0]
    evidence_end = validation_context.xnys_range_end or validation_context.xnys_sessions[-1]
    if start < evidence_start or end > evidence_end:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform preflight range extends outside pinned XNYS evidence"
        )
    session_labels = set(validation_context.xnys_sessions)
    if start not in session_labels or end not in session_labels:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform preflight start/end must be actual XNYS session labels "
            "in pinned evidence"
        )
    sessions = tuple(
        session for session in validation_context.xnys_sessions if start <= session <= end
    )
    if len(sessions) != preflight.session_count:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform preflight session_count must match pinned XNYS sessions"
        )
    return sessions


def _require_g0_platform_provenance(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    if state.mode is not ResearchMode.DATA_INFRA_G0:
        return
    is_contract_mismatch = (
        artifact.status is VerificationStatus.BUG_SIGNAL
        and artifact.bug_signals == (PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,)
    )
    if is_contract_mismatch or artifact.status is not VerificationStatus.PASS:
        return
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage requires implementation_result"
        )
    preflight = state.implementation_result.price_hydration_scope_preflight
    receipt = artifact.platform_coverage_validation
    universe = artifact.universe_verification_receipt
    hydration = artifact.price_hydration_receipt
    if preflight is None or receipt is None or universe is None or hydration is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage requires runner-checkable preflight, "
            "universe, price hydration, and platform coverage provenance; use "
            "platform_coverage_contract_mismatch BUG_SIGNAL when unavailable or mismatched"
        )
    if validation_context is not None:
        validation_context.validate_universe_receipt(universe)
    requested_sessions = _requested_sessions_for_preflight(preflight, validation_context)
    if not _platform_receipt_has_expected_runner_provenance(
        receipt,
        preflight=preflight,
        universe=universe,
        hydration=hydration,
        requested_sessions=requested_sessions,
    ):
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage receipt is not bound to the exact runner "
            "preflight, universe, and price hydration evidence; use "
            "platform_coverage_contract_mismatch BUG_SIGNAL"
        )


def _is_fail_closed_g0_platform_contract_bug_signal(
    verification: VerificationResultArtifact | None,
) -> bool:
    return (
        verification is not None
        and verification.status is VerificationStatus.BUG_SIGNAL
        and verification.bug_signals == (PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,)
        and verification.infra_gate_outcome is None
        and verification.infra_rationale is None
        and verification.platform_coverage_validation is None
    )


def _compact_json_block(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_authoritative_state_reference(
    state: AutoresearchState,
    *,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
) -> AuthoritativeStateReference:
    """Bind a stage dispatch to one canonical, complete persisted state."""
    canonical_state_model = AutoresearchState.from_dict(state.to_dict())
    canonical_state = _compact_json_block(canonical_state_model.to_dict())
    state_sha256 = _sha256_text("\n".join((AUTHORITATIVE_STATE_DIGEST_DOMAIN, canonical_state)))
    return AuthoritativeStateReference(
        version=AUTHORITATIVE_STATE_REFERENCE_VERSION,
        digest_domain=AUTHORITATIVE_STATE_DIGEST_DOMAIN,
        path=str(state_path.expanduser().resolve(strict=False)),
        state_sha256=state_sha256,
        phase=canonical_state_model.phase.value,
        iteration=canonical_state_model.iteration,
    )


def _validate_alpha_universe_chain(
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    consensus = state.latest_consensus
    if consensus is None or consensus.status is not ConsensusStatus.MAJORITY:
        return
    if _is_operator_precondition_consensus(consensus):
        return
    if consensus.universe_plan is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH majority consensus requires a frozen universe_plan"
        )
    consensus.universe_plan.validate()
    for verification in state.verification_history:
        if verification.universe_verification_receipt is None:
            if verification.status is VerificationStatus.PASS:
                raise AutoresearchValidationError(
                    "ALPHA_RESEARCH PASS cannot omit universe verification receipts"
                )
            continue
        if verification.price_hydration_receipt is None:
            raise AutoresearchValidationError(
                "verification cannot persist a partial universe receipt chain"
            )
        verification.universe_verification_receipt.validate_against_plan(consensus.universe_plan)
        if validation_context is not None:
            validation_context.validate_universe_receipt(verification.universe_verification_receipt)
        verification.price_hydration_receipt.validate_against_universe(
            verification.universe_verification_receipt
        )


def _validate_consensus_history_universe_plans(state: AutoresearchState) -> None:
    """Require a frozen plan for every persisted non-operator majority."""
    for index, consensus in enumerate(state.consensus_history, start=1):
        if consensus.status is ConsensusStatus.MAJORITY and not _is_operator_precondition_consensus(
            consensus
        ):
            if consensus.universe_plan is None:
                if index == len(
                    state.consensus_history
                ) and _is_data_infra_g0_blocked_no_memory_state(state):
                    continue
                raise AutoresearchValidationError(
                    "non-operator majority consensus at history index "
                    f"{index} requires a frozen universe_plan"
                )
            consensus.universe_plan.validate()


def _revalidate_accepted_member_union_manifests(state: AutoresearchState) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    for verification in state.verification_history:
        receipt = verification.universe_verification_receipt
        if verification.status is VerificationStatus.PASS and receipt is not None:
            receipt.member_union_manifest.validate()
            _verify_member_union_manifest(receipt)


def _validate_no_consensus_completion(state: AutoresearchState) -> None:
    decision = state.final_decision
    if decision is None or decision.decision is not FinalDecision.NO_CONSENSUS:
        return
    expected_rounds = (1, 2)
    debate_rounds = tuple(debate.round_number for debate in state.debate_rounds)
    consensus_rounds = tuple(consensus.round_number for consensus in state.consensus_history)
    consensus_statuses = tuple(consensus.status for consensus in state.consensus_history)
    if (
        state.consensus_retry_count != 1
        or debate_rounds != expected_rounds
        or consensus_rounds != expected_rounds
        or consensus_statuses != (ConsensusStatus.NO_CONSENSUS, ConsensusStatus.NO_CONSENSUS)
    ):
        raise AutoresearchValidationError(
            "NO_CONSENSUS final state requires the mandatory second round after one retry"
        )


def _final_decision_requires_memory_write(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> bool:
    """Return the sole final-decision class eligible for MemPalace retention."""
    latest_verification = state.latest_verification
    return (
        state.mode is ResearchMode.ALPHA_RESEARCH
        and decision.decision in (*KEEP_DECISIONS, FinalDecision.DISCARD)
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.PASS
        and latest_verification.tests_passed
    )


def _validate_final_decision_memory_requirement(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> None:
    """Fail closed when a PM-selected memory flag disagrees with retention policy."""
    memory_write_required = _final_decision_requires_memory_write(state, decision)
    if decision.memory_write_required is memory_write_required:
        return
    if memory_write_required:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH completed PASS final decisions require memory_write_required=true"
        )
    raise AutoresearchValidationError(
        f"{decision.decision.value} final decision is not eligible for MemPalace retention; "
        "memory_write_required=false"
    )


def _is_operator_infrastructure_suspension_state(state: AutoresearchState) -> bool:
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and state.mode is ResearchMode.ALPHA_RESEARCH
        and state.suspended
        and state.setup is not None
        and state.context_packet is not None
        and state.context_packet.research_mode is ResearchMode.ALPHA_RESEARCH
        and state.platform_readiness is not None
        and decision is not None
        and decision.experiment_id == _canonical_iteration_experiment_id(state.iteration)
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and decision.recommended_metric_name == OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME
        and decision.recommended_metric_value is None
        and decision.rationale == OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE
        and decision.log_summary == OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY
        and decision.continue_loop
        and not decision.memory_write_required
        and decision.infra_rationale == state.suspension_reason
        and state.suspension_reason is not None
        and state.suspension_reason == state.suspension_reason.strip()
        and not state.memory_written
        and state.memory_verification_receipt is None
    )


def _is_authorized_no_memory_final_decision(state: AutoresearchState) -> bool:
    """Return whether a non-retained final decision completed a valid terminal path."""
    decision = state.final_decision
    if (
        decision is None
        or decision.memory_write_required
        or _final_decision_requires_memory_write(state, decision)
    ):
        return False

    latest_verification = state.latest_verification
    if decision.decision is FinalDecision.NO_CONSENSUS:
        return (
            state.implementation_result is None
            and state.consensus_retry_count == 1
            and tuple(debate.round_number for debate in state.debate_rounds) == (1, 2)
            and tuple(consensus.round_number for consensus in state.consensus_history) == (1, 2)
            and tuple(consensus.status for consensus in state.consensus_history)
            == (ConsensusStatus.NO_CONSENSUS, ConsensusStatus.NO_CONSENSUS)
        )

    if decision.decision is FinalDecision.INFRA_BLOCKED:
        return state.suspended and (
            _is_operator_infrastructure_suspension_state(state)
            or (
                _is_operator_precondition_consensus(state.latest_consensus)
                and state.implementation_result is None
                and latest_verification is None
                and decision.reviewer_verdict is FinalReviewerVerdict.NOT_RUN
                and decision.recommended_metric_value is None
                and bool(decision.infra_rationale)
            )
        )

    if (
        decision.decision is FinalDecision.CRASH
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.TEST_FAILURE
        and state.verification_fix_attempts >= 2
    ):
        return True

    if (
        decision.decision is FinalDecision.DISCARD
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.BUG_SIGNAL
        and state.verification_fix_attempts >= 2
    ):
        return True

    if state.mode is not ResearchMode.DATA_INFRA_G0 or latest_verification is None:
        return False
    if (
        latest_verification.status is not VerificationStatus.PASS
        or not latest_verification.tests_passed
    ):
        return False
    if decision.decision is FinalDecision.INFRA_REPAIRED:
        return latest_verification.infra_gate_outcome is InfraGateOutcome.GATE_PASSED
    return (
        decision.decision is FinalDecision.DISCARD
        and latest_verification.infra_gate_outcome is InfraGateOutcome.REMEDIATION_REQUIRED
    )


def _validate_operator_precondition_infra_blocked_suspension(state: AutoresearchState) -> None:
    decision = state.final_decision
    if (
        decision is not None
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and _is_operator_precondition_consensus(state.latest_consensus)
        and not state.suspended
    ):
        raise AutoresearchValidationError(
            "operator-precondition INFRA_BLOCKED state must be suspended"
        )


def _is_data_infra_g0_blocked_no_memory_state(state: AutoresearchState) -> bool:
    decision = state.final_decision
    latest_verification = state.latest_verification
    return (
        state.phase is Phase.REPEAT
        and state.mode is ResearchMode.DATA_INFRA_G0
        and state.suspended
        and decision is not None
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and bool(decision.infra_rationale)
        and not decision.memory_write_required
        and not state.memory_written
        and state.memory_verification_receipt is None
        and state.implementation_result is not None
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.PASS
        and latest_verification.tests_passed
        and latest_verification.infra_gate_outcome is InfraGateOutcome.REMEDIATION_REQUIRED
    )


def _extract_first_float(text: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    return float(match.group(0))


def _baseline_metric(state: AutoresearchState) -> float | None:
    if state.context_packet is not None:
        baseline = _extract_first_float(state.context_packet.baseline_metric)
        if baseline is not None:
            return baseline
    if state.setup is not None:
        return _extract_first_float(state.setup.baseline_summary)
    return None


def _validate_final_decision_artifact(
    artifact: FinalDecisionArtifact,
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    _validate_final_decision_memory_requirement(state, artifact)
    if artifact.recommended_metric_name == OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires the dedicated operator transition"
        )

    latest_review = state.latest_review
    latest_verification = state.latest_verification
    latest_consensus = state.latest_consensus
    expected_reviewer_verdict = (
        FinalReviewerVerdict(latest_review.verdict.value)
        if latest_review is not None
        else FinalReviewerVerdict.NOT_RUN
    )
    if artifact.reviewer_verdict != expected_reviewer_verdict:
        raise AutoresearchValidationError(
            "final_decision reviewer_verdict must match latest review"
        )

    if (
        latest_consensus is not None
        and latest_consensus.status is ConsensusStatus.NO_CONSENSUS
        and state.implementation_result is None
    ):
        if artifact.decision is not FinalDecision.NO_CONSENSUS:
            raise AutoresearchValidationError(
                "final_decision must be NO_CONSENSUS when consensus never reached a majority"
            )
        if artifact.memory_write_required:
            raise AutoresearchValidationError(
                "NO_CONSENSUS requires final_decision.memory_write_required=false"
            )
        if artifact.infra_rationale is not None:
            raise AutoresearchValidationError(
                "NO_CONSENSUS final_decision cannot contain infra_rationale"
            )
        return

    if (
        _is_operator_precondition_consensus(latest_consensus)
        and state.implementation_result is None
        and latest_verification is None
    ):
        if artifact.decision is not FinalDecision.INFRA_BLOCKED:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires final_decision=INFRA_BLOCKED"
            )
        if artifact.memory_write_required:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires memory_write_required=false"
            )
        if artifact.reviewer_verdict is not FinalReviewerVerdict.NOT_RUN:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires reviewer_verdict=NOT_RUN"
            )
        if artifact.recommended_metric_value is not None:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires recommended_metric_value=null"
            )
        if not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires infra_rationale"
            )
        return

    if (
        latest_verification is not None
        and latest_verification.status is VerificationStatus.TEST_FAILURE
        and state.verification_fix_attempts >= 2
    ):
        if artifact.decision is not FinalDecision.CRASH:
            raise AutoresearchValidationError(
                "test failures after retries require final_decision=CRASH"
            )
        return

    if (
        latest_verification is not None
        and latest_verification.status is VerificationStatus.BUG_SIGNAL
        and state.verification_fix_attempts >= 2
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "bug signals after retries require final_decision=DISCARD"
            )
        if (
            state.mode is ResearchMode.DATA_INFRA_G0
            and _is_fail_closed_g0_platform_contract_bug_signal(latest_verification)
            and artifact.memory_write_required
        ):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 platform_coverage_contract_mismatch BUG_SIGNAL discard "
                "requires memory_write_required=false"
            )
        return

    if state.mode is ResearchMode.DATA_INFRA_G0:
        if not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires infra_rationale"
            )
        if latest_verification is None or latest_verification.infra_gate_outcome is None:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires an infrastructure verification gate"
            )
        if (
            latest_verification.status is not VerificationStatus.PASS
            or not latest_verification.tests_passed
        ):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 infrastructure final decisions require a successful completed "
                "verification assessment with status=PASS and tests_passed=true"
            )
        expected = (
            FinalDecision.INFRA_REPAIRED
            if latest_verification.infra_gate_outcome is InfraGateOutcome.GATE_PASSED
            else FinalDecision.DISCARD
        )
        if artifact.decision is not expected:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision must be INFRA_REPAIRED for GATE_PASSED "
                "or non-suspending DISCARD for REMEDIATION_REQUIRED"
            )
        receipt = latest_verification.platform_coverage_validation
        preflight = (
            state.implementation_result.price_hydration_scope_preflight
            if state.implementation_result is not None
            else None
        )
        receipt_is_trusted = (
            receipt is not None
            and receipt.matches_shared_contract
            and receipt.status is PlatformCoverageStatus.COMPLETE
            and preflight is not None
            and latest_verification.universe_verification_receipt is not None
            and latest_verification.price_hydration_receipt is not None
            and _platform_receipt_has_expected_runner_provenance(
                receipt,
                preflight=preflight,
                universe=latest_verification.universe_verification_receipt,
                hydration=latest_verification.price_hydration_receipt,
                requested_sessions=_requested_sessions_for_preflight(
                    preflight,
                    validation_context,
                ),
            )
        )
        if expected is FinalDecision.INFRA_REPAIRED and not receipt_is_trusted:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 INFRA_REPAIRED requires a COMPLETE receipt cross-checked "
                "against runner-owned preflight identity and counts"
            )
        if expected is FinalDecision.DISCARD and artifact.memory_write_required:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 remediation DISCARD requires memory_write_required=false"
            )
        return

    if artifact.decision is FinalDecision.INFRA_BLOCKED:
        raise AutoresearchValidationError(
            "INFRA_BLOCKED requires the explicit operator-owned readiness suspension "
            "transition and cannot be emitted by a stage artifact"
        )

    if artifact.infra_rationale:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH final_decision cannot contain infra_rationale"
        )

    if latest_review is not None and (
        latest_review.verdict is ReviewVerdict.FAIL or latest_review.critical_issues
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "critical review issues require final_decision=DISCARD"
            )
        return

    if (
        latest_verification is not None
        and latest_verification.max_drawdown_pct is not None
        and latest_verification.max_drawdown_pct >= 30.0
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "max_drawdown_pct >= 30 requires final_decision=DISCARD"
            )
        return

    metric_value = artifact.recommended_metric_value
    if metric_value is None:
        raise AutoresearchValidationError(
            "final_decision requires recommended_metric_value for completed experiments"
        )

    if metric_value <= -0.5:
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "decision Sharpe <= -0.5 requires final_decision=DISCARD"
            )
        return

    if (
        metric_value > 1.0
        and latest_review is not None
        and latest_review.verdict is ReviewVerdict.PASS
    ):
        if artifact.decision is not FinalDecision.STRONG_KEEP:
            raise AutoresearchValidationError(
                "decision Sharpe > 1.0 with reviewer PASS requires final_decision=STRONG KEEP"
            )
        return

    if metric_value > 0.5:
        if artifact.decision not in (
            FinalDecision.SIGNIFICANT_KEEP,
            FinalDecision.STRONG_KEEP,
        ):
            raise AutoresearchValidationError(
                "decision Sharpe > 0.5 requires SIGNIFICANT KEEP or STRONG KEEP"
            )
        return

    baseline_metric = _baseline_metric(state)
    if baseline_metric is None:
        if artifact.decision is FinalDecision.KEEP:
            raise AutoresearchValidationError(
                "plain KEEP requires a numeric baseline to prove improvement"
            )
        if artifact.decision in KEEP_DECISIONS:
            raise AutoresearchValidationError(
                "KEEP-family decisions without a numeric baseline require Sharpe > 0.5"
            )
        return

    if metric_value > baseline_metric:
        if artifact.decision not in KEEP_DECISIONS:
            raise AutoresearchValidationError(
                "decision Sharpe above baseline requires a KEEP-family final_decision"
            )
        return

    if artifact.decision is not FinalDecision.DISCARD:
        raise AutoresearchValidationError(
            "non-improving Sharpe must end with final_decision=DISCARD"
        )
