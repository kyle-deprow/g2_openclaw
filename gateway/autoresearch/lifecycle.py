"""Iteration lifecycle transitions for the autoresearch control plane."""

from __future__ import annotations

from dataclasses import replace

from gateway.autoresearch.artifacts import (
    FinalDecisionArtifact as FinalDecisionArtifact,
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
    FinalDecision as FinalDecision,
)
from gateway.autoresearch.enums import (
    FinalReviewerVerdict as FinalReviewerVerdict,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.governance import (
    derive_campaign_counters as derive_campaign_counters,
)
from gateway.autoresearch.memory import (
    _is_explicit_no_memory_transition as _is_explicit_no_memory_transition,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.transitions import (
    _acknowledged_through_iteration as _acknowledged_through_iteration,
)
from gateway.autoresearch.transitions import (
    _build_hypothesis_registry_entry as _build_hypothesis_registry_entry,
)
from gateway.autoresearch.transitions import (
    _canonical_iteration_experiment_id as _canonical_iteration_experiment_id,
)
from gateway.autoresearch.transitions import (
    _is_operator_infrastructure_suspension_state as _is_operator_infrastructure_suspension_state,
)
from gateway.autoresearch.transitions import (
    _validate_operator_precondition_infra_blocked_suspension as _validate_operator_precondition_infra_blocked_suspension,  # noqa: E501
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest as PlatformReadinessManifest,
)

OPERATOR_INFRASTRUCTURE_SUSPENSION_ACTIVE_PHASES = frozenset(
    (
        Phase.DEBATE,
        Phase.CONSENSUS,
        Phase.IMPLEMENTATION,
        Phase.VERIFICATION,
        Phase.REVIEW,
        Phase.FIX_TEST,
        Phase.DECISION_LOG,
    )
)


def suspend_for_infrastructure(state: AutoresearchState, reason: str) -> AutoresearchState:
    """Durably suspend an active alpha iteration for operator-owned infra repair."""
    if not reason or not reason.strip():
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires a non-empty reason"
        )
    if reason != reason.strip():
        raise AutoresearchValidationError(
            "operator infrastructure suspension reason must not have leading or trailing whitespace"
        )
    if state.suspended:
        raise AutoresearchValidationError("autoresearch state is already suspended")
    if state.phase is Phase.REPEAT or state.final_decision is not None:
        raise AutoresearchValidationError(
            "autoresearch state is already finalized or in repeat phase"
        )
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires an active ALPHA_RESEARCH iteration"
        )
    if state.phase not in OPERATOR_INFRASTRUCTURE_SUSPENSION_ACTIVE_PHASES:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires a coherent active ALPHA_RESEARCH phase"
        )
    if state.setup is None or state.context_packet is None or state.platform_readiness is None:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires setup, context packet, and "
            "pinned platform readiness"
        )
    if state.context_packet.research_mode is not ResearchMode.ALPHA_RESEARCH:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires an ALPHA_RESEARCH context packet"
        )

    decision = FinalDecisionArtifact(
        experiment_id=_canonical_iteration_experiment_id(state.iteration),
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name=OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,
        recommended_metric_value=None,
        reviewer_verdict=(
            FinalReviewerVerdict(state.latest_review.verdict.value)
            if state.latest_review is not None
            else FinalReviewerVerdict.NOT_RUN
        ),
        rationale=OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
        log_summary=OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,
        continue_loop=True,
        memory_write_required=False,
        infra_rationale=reason,
    )
    registry_entry = _build_hypothesis_registry_entry(state, decision)
    next_registry = (*state.hypothesis_registry, registry_entry)
    return replace(
        state,
        phase=Phase.REPEAT,
        pending_fix_trigger=None,
        final_decision=decision,
        memory_written=False,
        memory_verification_receipt=None,
        suspended=True,
        suspension_reason=reason,
        hypothesis_registry=next_registry,
        campaign_counters=derive_campaign_counters(
            next_registry,
            acknowledged_through_iteration=_acknowledged_through_iteration(state),
        ),
    )


def start_next_iteration(
    state: AutoresearchState,
    *,
    readiness: PlatformReadinessManifest | None = None,
) -> AutoresearchState:
    """Begin a completed iteration's successor with a newly validated READY receipt."""
    if state.setup is None or state.final_decision is None:
        raise AutoresearchValidationError("next iteration requires completed current iteration")
    _validate_operator_precondition_infra_blocked_suspension(state)
    if state.suspended:
        raise AutoresearchValidationError(
            "suspended INFRA_BLOCKED state requires explicit autoresearch-resume"
        )
    if state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError("start_next_iteration requires a completed repeat phase")
    if readiness is None:
        raise AutoresearchValidationError(
            "start_next_iteration requires an explicit platform readiness manifest"
        )
    if state.platform_readiness is None:
        raise AutoresearchValidationError(
            "autoresearch state has no pinned platform readiness receipt; "
            "run autoresearch-pin-readiness explicitly before dispatch"
        )
    if not state.memory_written and not _is_explicit_no_memory_transition(state):
        raise AutoresearchValidationError(
            "cannot start next iteration before a verified MemPalace write or a "
            "policy-approved no-memory final decision"
        )
    try:
        readiness_identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    return AutoresearchState(
        phase=Phase.SETUP_CONTEXT,
        iteration=state.iteration + 1,
        setup=state.setup,
        platform_readiness=readiness_identity,
        hypothesis_registry=state.hypothesis_registry,
        campaign_counters=state.campaign_counters,
        campaign_review_required=state.campaign_review_required,
        campaign_review_reason=state.campaign_review_reason,
        campaign_review_history=state.campaign_review_history,
    )


def acknowledge_campaign_review(
    state: AutoresearchState,
    acknowledgement: str,
) -> AutoresearchState:
    """Record an operator acknowledgement and clear the campaign-review pause."""
    if not state.campaign_review_required:
        raise AutoresearchValidationError("no pending campaign review to acknowledge")
    if state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError(
            "campaign review acknowledgement requires the campaign to be in repeat phase"
        )
    if not isinstance(acknowledgement, str):
        raise AutoresearchValidationError("campaign review acknowledgement must be a string")
    cleaned = acknowledgement.strip()
    if len(cleaned) < 32 or len(cleaned) > 1024:
        raise AutoresearchValidationError(
            "campaign review acknowledgement must be between 32 and 1024 characters"
        )
    if not state.campaign_review_history:
        raise AutoresearchValidationError(
            "campaign review is pending but its review-history record is missing"
        )
    trailing = state.campaign_review_history[-1]
    if trailing.acknowledgement is not None or trailing.acknowledged_iteration is not None:
        raise AutoresearchValidationError(
            "campaign review is pending but its trailing review-history record is already "
            "acknowledged"
        )
    acknowledged_record = replace(
        trailing,
        acknowledgement=cleaned,
        acknowledged_iteration=state.iteration,
    )
    history = (*state.campaign_review_history[:-1], acknowledged_record)
    counters = derive_campaign_counters(
        state.hypothesis_registry,
        acknowledged_through_iteration=state.iteration,
    )
    return replace(
        state,
        campaign_review_required=False,
        campaign_review_reason=None,
        campaign_review_history=history,
        campaign_counters=counters,
    )


def resume_suspended_iteration(
    state: AutoresearchState,
    readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    """Explicitly recheck readiness and resume after a durable infrastructure pause."""
    if not state.suspended or state.final_decision is None:
        raise AutoresearchValidationError("autoresearch state is not suspended")
    if state.final_decision.decision is not FinalDecision.INFRA_BLOCKED:
        raise AutoresearchValidationError("only an INFRA_BLOCKED state can be resumed explicitly")
    if state.setup is None:
        raise AutoresearchValidationError("suspended state is missing setup context")
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    if (
        state.final_decision.recommended_metric_name
        == OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME
    ):
        if not _is_operator_infrastructure_suspension_state(state):
            raise AutoresearchValidationError(
                "operator infrastructure suspension state has an invalid contract"
            )
        if state.platform_readiness is None:
            raise AutoresearchValidationError(
                "operator infrastructure suspension has no pinned readiness identity to replace"
            )
        if state.platform_readiness == identity:
            raise AutoresearchValidationError(
                "autoresearch-resume requires a changed READY platform readiness manifest"
            )
    return AutoresearchState(
        phase=Phase.SETUP_CONTEXT,
        iteration=state.iteration + 1,
        setup=state.setup,
        platform_readiness=identity,
        hypothesis_registry=state.hypothesis_registry,
        campaign_counters=state.campaign_counters,
        campaign_review_required=state.campaign_review_required,
        campaign_review_reason=state.campaign_review_reason,
        campaign_review_history=state.campaign_review_history,
    )


def pin_platform_readiness(
    state: AutoresearchState,
    readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    """Initialize readiness or repin an active state to the same contract IDs."""
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    if state.suspended:
        raise AutoresearchValidationError(
            "suspended readiness must continue through autoresearch-resume"
        )
    pinned = state.platform_readiness
    if pinned is not None and (
        pinned.manifest_id != identity.manifest_id or pinned.snapshot_id != identity.snapshot_id
    ):
        raise AutoresearchValidationError(
            "state readiness manifest_id or snapshot_id changed; same-ID repin required"
        )
    return replace(state, platform_readiness=identity)
