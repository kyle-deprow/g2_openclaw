"""State-consuming validation and transition-boundary helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch import compute as compute_module
from gateway.autoresearch import constants
from gateway.autoresearch import transitions as transitions_module
from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact as ConsensusResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ContextPacketArtifact as ContextPacketArtifact,
)
from gateway.autoresearch.artifacts import (
    DebateResultArtifact as DebateResultArtifact,
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
    ReviewResultArtifact as ReviewResultArtifact,
)
from gateway.autoresearch.artifacts import (
    SetupContextArtifact as SetupContextArtifact,
)
from gateway.autoresearch.artifacts import (
    VerificationResultArtifact as VerificationResultArtifact,
)
from gateway.autoresearch.compute import (
    ComputeFitArtifact as ComputeFitArtifact,
)
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
    HYPOTHESIS_REGISTRY_REASON_MAX_CHARS as HYPOTHESIS_REGISTRY_REASON_MAX_CHARS,
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
    ComputeTarget as ComputeTarget,
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
    FixTriggerPhase as FixTriggerPhase,
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
    ReviewFindingDisposition as ReviewFindingDisposition,
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
from gateway.autoresearch.evidence import (
    _validate_quantipy_experiment_evidence as _validate_quantipy_experiment_evidence,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_v2_manifest as _validate_quantipy_v2_manifest,
)
from gateway.autoresearch.fields import (
    _normalise_identifier as _normalise_identifier,
)
from gateway.autoresearch.fields import (
    _sha256_text as _sha256_text,
)
from gateway.autoresearch.fields import (
    _validate_workspace_path as _validate_workspace_path,
)
from gateway.autoresearch.fields import (
    quantipy_member_union_digest as quantipy_member_union_digest,
)
from gateway.autoresearch.gitops import (
    _require_artifact_origin_matches_target as _require_artifact_origin_matches_target,
)
from gateway.autoresearch.gitops import (
    _require_clean_git_worktree as _require_clean_git_worktree,
)
from gateway.autoresearch.gitops import (
    _require_git_worktree_root as _require_git_worktree_root,
)
from gateway.autoresearch.gitops import (
    _require_isolated_git_clone_root as _require_isolated_git_clone_root,
)
from gateway.autoresearch.gitops import (
    _require_strict_canonical_workspace_path as _require_strict_canonical_workspace_path,
)
from gateway.autoresearch.gitops import _require_workspace_under_autoresearch_worktree_root
from gateway.autoresearch.gitops import (
    _resolve_git_commit as _resolve_git_commit,
)
from gateway.autoresearch.governance import (
    CampaignCounters as CampaignCounters,
)
from gateway.autoresearch.governance import (
    CampaignReviewRecord as CampaignReviewRecord,
)
from gateway.autoresearch.governance import (
    HypothesisRegistryEntry as HypothesisRegistryEntry,
)
from gateway.autoresearch.governance import (
    contested_families as contested_families,
)
from gateway.autoresearch.governance import (
    derive_campaign_counters as derive_campaign_counters,
)
from gateway.autoresearch.governance import (
    theory_family_fingerprint as theory_family_fingerprint,
)
from gateway.autoresearch.manifest import (
    AuthoritativeStateReference as AuthoritativeStateReference,
)
from gateway.autoresearch.receipts import (
    DynamicUniverseCoverageReceipt as DynamicUniverseCoverageReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    _validate_external_verification_retry_receipt as _validate_external_verification_retry_receipt,
)
from gateway.autoresearch.recovery_receipts import (
    _verify_member_union_manifest as _verify_member_union_manifest,
)
from gateway.autoresearch.secure_io import (
    _require_private_directory as _require_private_directory,
)
from gateway.autoresearch.secure_io import (
    _secure_open_snapshot as _secure_open_snapshot,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.workspace import (
    _require_ancestor as _require_ancestor,
)
from gateway.autoresearch_platform_validation import (
    PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL as PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,
)
from gateway.autoresearch_platform_validation import (
    PlatformCoverageStatus as PlatformCoverageStatus,
)
from gateway.autoresearch_readiness import (
    XNYSCalendarEvidence as XNYSCalendarEvidence,
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
    from gateway.autoresearch.policy import (
        AutoresearchPolicy as AutoresearchPolicy,
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

# Mirrors the operator campaign activity floor in
# gateway/agent_config/skills/autoresearch/SKILL.md section 8. If the operator
# changes the floor, both must change together.
ALPHA_MIN_TRADES_PER_DAY = 1.0

# Bounded review-driven fix rounds per iteration; on the cap the iteration
# routes to decision_log where critical-remains -> DISCARD applies.
MAX_REVIEW_FIX_ROUNDS = 3
DEFAULT_XNYS_CALENDAR_EVIDENCE_PATH = Path(
    "/home/dev/.openclaw/autoresearch/evidence/xnys-trading-calendar.json"
)

# Extending this set requires the runtime transport to actually ship, with the
# consensus data contract re-pinned in the same change.
SUPPORTED_EXPERIMENT_TRANSPORTS = ("price_panel",)


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


def _validate_consensus_data_requirements(
    artifact: ConsensusResultArtifact,
    *,
    require_submission_field: bool,
) -> None:
    # Trust boundary: the arbiter's data_requirements declaration is the trust
    # point; this validator cannot mechanically infer semantic data dependencies.
    artifact.validate()
    if require_submission_field:
        artifact.require_submitted_data_requirements()
    if artifact.data_requirements is None:
        return
    if artifact.status is not ConsensusStatus.MAJORITY:
        return
    if _is_operator_precondition_consensus(artifact):
        return
    unsupported = tuple(
        requirement
        for requirement in artifact.data_requirements
        if requirement not in SUPPORTED_EXPERIMENT_TRANSPORTS
    )
    if unsupported:
        raise AutoresearchValidationError(
            "unsupported consensus data requirement "
            f"'{unsupported[0]}'; resubmit as an operator-precondition consensus "
            "(the existing no-code path) or reshape to supported data"
        )


def _acknowledged_through_iteration(state: AutoresearchState) -> int:
    return max(
        (record.acknowledged_iteration or 0 for record in state.campaign_review_history),
        default=0,
    )


def _build_hypothesis_registry_entry(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> HypothesisRegistryEntry:
    if state.mode is None:
        raise AutoresearchValidationError(
            "hypothesis registry entries require an explicit research mode"
        )
    consensus = state.latest_consensus
    contested: tuple[str, ...]
    if consensus is None:
        consensus_status = ConsensusStatus.NONE
        family = None
        contested = ()
        fingerprint = None
    else:
        consensus_status = consensus.status
        family = (
            _normalise_identifier(consensus.winner_theory_family)
            if consensus.winner_theory_family is not None
            else None
        )
        contested = (
            contested_families(state.latest_debate)
            if (
                consensus_status is ConsensusStatus.NO_CONSENSUS
                and decision.decision is FinalDecision.NO_CONSENSUS
                and state.latest_debate is not None
            )
            else ()
        )
        fingerprint = theory_family_fingerprint(consensus, state.mode)
    return HypothesisRegistryEntry(
        iteration=state.iteration,
        research_mode=state.mode,
        consensus_status=consensus_status,
        decision=decision.decision,
        family=family,
        contested_families=contested,
        fingerprint=fingerprint,
        metric_value=decision.recommended_metric_value,
        reason=_hypothesis_registry_reason(state, decision),
        novelty_delta_sha256=(
            _sha256_text(consensus.novelty_delta)
            if (
                consensus is not None
                and not _is_operator_precondition_consensus(consensus)
                and consensus.novelty_delta is not None
            )
            else None
        ),
    )


def _campaign_stall_reason(
    counters: CampaignCounters,
    policy: AutoresearchPolicy,
) -> str | None:
    governance = policy.campaign_governance
    if counters.consecutive_non_keep >= governance.stall_consecutive_non_keep:
        return (
            "campaign stalled: "
            f"{counters.consecutive_non_keep} consecutive non-KEEP iterations "
            f"(threshold {governance.stall_consecutive_non_keep})"
        )
    if counters.consecutive_no_consensus >= governance.stall_consecutive_no_consensus:
        return (
            "campaign stalled: "
            f"{counters.consecutive_no_consensus} consecutive NO_CONSENSUS iterations "
            f"(threshold {governance.stall_consecutive_no_consensus})"
        )
    return None


def _validate_consensus_novelty_gate(
    state: AutoresearchState,
    artifact: ConsensusResultArtifact,
) -> str | None:
    """Reject repeated failed hypotheses unless the arbiter explains the change."""
    if artifact.status is ConsensusStatus.NO_CONSENSUS:
        return None
    if _is_operator_precondition_consensus(artifact):
        if artifact.novelty_delta is not None:
            raise AutoresearchValidationError(
                "operator-precondition consensus must not include novelty_delta"
            )
        return None

    current_fingerprint = (
        theory_family_fingerprint(artifact, state.mode)
        if state.mode is not None and artifact.universe_plan is not None
        else None
    )
    current_family = (
        _normalise_identifier(artifact.winner_theory_family)
        if artifact.winner_theory_family is not None
        else None
    )
    prior_entries = sorted(
        (entry for entry in state.hypothesis_registry if entry.iteration < state.iteration),
        key=lambda entry: entry.iteration,
        reverse=True,
    )
    tier_one = next(
        (
            entry
            for entry in prior_entries
            if entry.decision
            in {FinalDecision.DISCARD, FinalDecision.CRASH, FinalDecision.NO_CONSENSUS}
            and current_fingerprint is not None
            and entry.fingerprint == current_fingerprint
        ),
        None,
    )
    tier_two = next(
        (
            entry
            for entry in prior_entries
            if entry.consensus_status is ConsensusStatus.NO_CONSENSUS
            and entry.decision is FinalDecision.NO_CONSENSUS
            and current_family is not None
            and current_family in entry.contested_families
        ),
        None,
    )
    required_entry = tier_one or tier_two
    if required_entry is not None and artifact.novelty_delta is None:
        if tier_one is not None:
            raise AutoresearchValidationError(
                "consensus winner_theory_family "
                f"'{current_family}' repeats iteration {tier_one.iteration} "
                f"{tier_one.decision.value}; set novelty_delta explaining what changed"
            )
        assert tier_two is not None
        raise AutoresearchValidationError(
            "consensus winner_theory_family "
            f"'{current_family}' repeats the iteration {tier_two.iteration} "
            "NO_CONSENSUS deadlock; set novelty_delta explaining what changed"
        )

    if artifact.novelty_delta is not None:
        delta_hash = _sha256_text(artifact.novelty_delta)
        duplicate = next(
            (entry for entry in prior_entries if entry.novelty_delta_sha256 == delta_hash),
            None,
        )
        if duplicate is not None:
            raise AutoresearchValidationError(
                "consensus novelty_delta duplicates the "
                f"iteration {duplicate.iteration} delta verbatim"
            )
        if required_entry is None:
            raise AutoresearchValidationError(
                "consensus novelty_delta is only valid when the theory family repeats a "
                "prior non-KEEP outcome"
            )
        return delta_hash
    return None


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
    validation_context: AutoresearchValidationContext | None = None,
    *,
    calendar_path: Path | None = None,
    calendar_content: Mapping[str, object] | None = None,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    preflight = artifact.price_hydration_scope_preflight
    if preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH implementation_result requires price_hydration_scope_preflight"
        )
    if validation_context is not None or calendar_path is not None or calendar_content is not None:
        derived_session_count = _derive_xnys_session_count(
            preflight,
            validation_context=validation_context,
            calendar_path=calendar_path,
            calendar_content=calendar_content,
        )
        window = f"{preflight.experiment_start}..{preflight.experiment_end}"
        if preflight.session_count != derived_session_count:
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH price preflight session_count mismatch for window "
                f"{window}: declared={preflight.session_count}, derived={derived_session_count}"
            )
        derived_planned_symbol_sessions = preflight.member_union_count * derived_session_count
        if preflight.planned_symbol_sessions != derived_planned_symbol_sessions:
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH price preflight planned_symbol_sessions mismatch for window "
                f"{window}: declared={preflight.planned_symbol_sessions}, "
                f"derived={derived_planned_symbol_sessions} "
                f"(member_union_count={preflight.member_union_count}, "
                f"derived_session_count={derived_session_count})"
            )
        preflight.validate()
    else:
        preflight.validate()
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


def _derive_xnys_session_count(
    preflight: PriceHydrationScopePreflight,
    *,
    validation_context: AutoresearchValidationContext | None,
    calendar_path: Path | None,
    calendar_content: Mapping[str, object] | None,
) -> int:
    if calendar_path is not None and calendar_content is not None:
        raise AutoresearchValidationError(
            "XNYS calendar evidence must be supplied by path or content, not both"
        )
    start = date.fromisoformat(preflight.experiment_start)
    end = date.fromisoformat(preflight.experiment_end)
    if start > end:
        raise AutoresearchValidationError(
            "XNYS calendar evidence cannot derive a session count for a reversed window "
            f"{preflight.experiment_start}..{preflight.experiment_end}"
        )

    if calendar_content is not None:
        try:
            evidence = XNYSCalendarEvidence.from_dict(calendar_content)
        except (TypeError, ValueError) as exc:
            raise AutoresearchValidationError(
                f"XNYS calendar evidence content is unreadable: {exc}"
            ) from exc
        sessions = evidence.sessions
        evidence_label = "injected XNYS calendar evidence"
    elif calendar_path is not None:
        try:
            raw = json.loads(calendar_path.read_text(encoding="utf-8"))
            evidence = XNYSCalendarEvidence.from_dict(raw)
        except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AutoresearchValidationError(
                f"XNYS calendar evidence unreadable at {calendar_path}: {exc}"
            ) from exc
        sessions = evidence.sessions
        evidence_label = str(calendar_path)
    elif validation_context is not None:
        sessions = validation_context.xnys_sessions
        evidence_label = "pinned validation-context XNYS calendar evidence"
    else:
        raise AutoresearchValidationError(
            "XNYS calendar evidence unreadable at "
            f"{DEFAULT_XNYS_CALENDAR_EVIDENCE_PATH}: no validation context or injected evidence"
        )

    if not sessions:
        raise AutoresearchValidationError(f"{evidence_label} contains no sessions")
    if start < sessions[0] or end > sessions[-1]:
        raise AutoresearchValidationError(
            f"{evidence_label} does not cover declaration window "
            f"{preflight.experiment_start}..{preflight.experiment_end}"
        )
    return sum(start <= session <= end for session in sessions)


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


_INFRA_BLOCKED_RATIONALE_MIN_CHARS = 64
_INFRA_BLOCKED_CONTRACT_TERM_RE = re.compile(
    r"\b(?:ExperimentManifest|ExperimentRunContext|[Rr]untime|[Tt]ransport|"
    r"[A-Z][A-Za-z0-9_]*Receipt(?:Contract)?(?:\s+contract)?)\b"
)
_INFRA_BLOCKED_GENERIC_TERMS = frozenset(("runtime", "transport"))


# Contracts that EXIST cannot be claimed missing. Two of the first three uses
# of the mid-implementation route were false positives: one keyword-matched a
# prohibition clause, one claimed the panel execution path itself was absent.
# A brief whose consensus declared only supported transports is implementable
# by construction; genuine breakage of a supported transport is a verification
# BUG_SIGNAL, never INFRA_BLOCKED.
_EXISTING_CONTRACT_CLAIMS_RE = re.compile(
    r"(?:ExperimentRunContext[^.]{0,60}?(?:panel|load_frame|price)"
    r"|(?:panel|load_frame|price_panel)[^.]{0,60}?ExperimentRunContext"
    r"|price_panel[^.]{0,60}?(?:missing|absent|lack(?:s|ed|ing)?|does not exist)"
    r"|(?:missing|absent|lack(?:s|ed|ing)?)[^.]{0,60}?price_panel)",
    re.IGNORECASE,
)


def _has_runtime_or_transport_contract_term(text: str | None) -> bool:
    """Require a bounded rationale naming a runtime/transport contract that
    is not one of the contracts the platform already provides."""
    if text is None:
        return False
    normalized = text.strip()
    if _EXISTING_CONTRACT_CLAIMS_RE.search(normalized) is not None:
        return False
    return (
        len(normalized) >= _INFRA_BLOCKED_RATIONALE_MIN_CHARS
        and _INFRA_BLOCKED_CONTRACT_TERM_RE.search(normalized) is not None
    )


def _hypothesis_registry_reason(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> str:
    source = (
        decision.infra_rationale
        if _is_implementation_infra_blocked_contract(state, decision)
        else decision.log_summary
    )
    if source is None:
        raise AutoresearchValidationError(
            "implementation INFRA_BLOCKED registry reason requires infra_rationale"
        )
    normalized = " ".join(source.split())
    if len(normalized) <= HYPOTHESIS_REGISTRY_REASON_MAX_CHARS:
        return normalized

    if _is_implementation_infra_blocked_contract(state, decision):
        named_term = next(
            (
                match.group(0)
                for match in _INFRA_BLOCKED_CONTRACT_TERM_RE.finditer(source)
                if match.group(0).casefold() not in _INFRA_BLOCKED_GENERIC_TERMS
            ),
            None,
        )
        if named_term is None:
            named_term_match = _INFRA_BLOCKED_CONTRACT_TERM_RE.search(source)
            named_term = named_term_match.group(0) if named_term_match is not None else None
        if named_term is not None:
            prefix_length = HYPOTHESIS_REGISTRY_REASON_MAX_CHARS - len(named_term) - 1
            if prefix_length > 0:
                return f"{normalized[:prefix_length].rstrip()} {named_term}"
            return named_term[:HYPOTHESIS_REGISTRY_REASON_MAX_CHARS]

    return normalized[:HYPOTHESIS_REGISTRY_REASON_MAX_CHARS].strip()


def _is_implementation_infra_blocked_state(state: AutoresearchState) -> bool:
    """Return whether the approved brief gates the blocker-only implementation dispatch."""
    consensus = state.latest_consensus
    return (
        state.phase in (Phase.IMPLEMENTATION, Phase.REPEAT)
        and state.mode is ResearchMode.ALPHA_RESEARCH
        and state.implementation_result is None
        and state.latest_verification is None
        and state.latest_review is None
        and consensus is not None
        and consensus.status is ConsensusStatus.MAJORITY
        and not _is_operator_precondition_consensus(consensus)
        and _has_runtime_or_transport_contract_term(consensus.implementation_brief)
    )


def _requires_implementation_infra_blocked_decision(state: AutoresearchState) -> bool:
    """Return whether IMPLEMENTATION must dispatch its infrastructure-blocker decision."""
    return state.phase is Phase.IMPLEMENTATION and _is_implementation_infra_blocked_state(state)


def _is_implementation_infra_blocked_contract(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> bool:
    """Return true for the no-memory blocker accepted before implementation evidence."""
    rationale = decision.infra_rationale
    # OPERATOR-AUTHORIZED ONLY. The consensus data-requirements gate guarantees
    # every new MAJORITY brief is implementable with supported transports, so a
    # stage-authored mid-implementation INFRA_BLOCKED is no longer a legitimate
    # discovery: three of this route's first four uses were adaptively-worded
    # false positives abandoning implementable briefs. The operator authorizes
    # a genuine case by placing the exact token below in the wake directive,
    # which the PM must quote verbatim in infra_rationale.
    operator_authorized = rationale is not None and ("OPERATOR-AUTHORIZED-INFRA-BLOCK" in rationale)
    return (
        operator_authorized
        and _is_implementation_infra_blocked_state(state)
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and decision.reviewer_verdict is FinalReviewerVerdict.NOT_RUN
        and decision.recommended_metric_value is None
        and decision.continue_loop
        and not decision.memory_write_required
        and not state.suspended
        and state.suspension_reason is None
        and _has_runtime_or_transport_contract_term(rationale)
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
        return (
            _is_implementation_infra_blocked_contract(state, decision)
            or (state.suspended and _is_operator_infrastructure_suspension_state(state))
            or (
                state.suspended
                and _is_operator_precondition_consensus(state.latest_consensus)
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

    if _is_implementation_infra_blocked_contract(state, artifact):
        return

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

    metric_name = artifact.recommended_metric_name
    if metric_name and (
        metric_name.lower().startswith("is_")
        or "in-sample" in metric_name.lower()
        or "in_sample" in metric_name.lower()
    ):
        raise AutoresearchValidationError(
            f"ALPHA_RESEARCH decision metric {metric_name!r} must be out-of-sample and cost-net"
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

    if (
        latest_verification is not None
        and latest_verification.trades_per_day is not None
        and latest_verification.trades_per_day < ALPHA_MIN_TRADES_PER_DAY
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "average activity below 1.0 trades/day requires final_decision=DISCARD"
            )
        return

    # The runner must not accept a model-supplied number as the decision Sharpe;
    # oos_sharpe_net is guaranteed non-null for any non-G0 PASS verification by
    # artifacts.py:1994-2010.
    metric_value = (
        latest_verification.oos_sharpe_net
        if latest_verification is not None and latest_verification.oos_sharpe_net is not None
        else artifact.recommended_metric_value
    )
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


def _validate_persisted_autoresearch_workspace_path(value: str, *, label: str) -> None:
    """Apply a filesystem-free lexical policy to persisted workspace evidence."""
    _validate_workspace_path(value, label=label)
    workspace_path = Path(value)
    if (
        not workspace_path.is_absolute()
        or value != str(workspace_path)
        or any(part in {".", ".."} for part in workspace_path.parts)
    ):
        raise AutoresearchValidationError(f"{label} must be an absolute lexically canonical path")
    try:
        workspace_path.relative_to(constants.DEFAULT_AUTORESEARCH_WORKTREE_ROOT)
    except ValueError as exc:
        raise AutoresearchValidationError(
            f"{label} must be under the canonical autoresearch worktree root"
        ) from exc


def _validate_compute_fit_environment(
    compute_fit: ComputeFitArtifact,
    target_repo: Path,
) -> None:
    compute_fit.validate()
    if compute_fit.target not in {ComputeTarget.GPU, ComputeTarget.MIXED}:
        return
    snapshot = compute_module.collect_compute_capability_snapshot(target_repo)
    if snapshot.probe_errors:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution, but the capability probe failed: "
            + "; ".join(snapshot.probe_errors)
        )
    if not snapshot.target_python_available:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution, but the target Quantipy virtualenv is unavailable"
        )
    if not snapshot.gpu_available or not snapshot.cuda_runtime_available:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution, but the capability probe found no "
            "usable GPU/CUDA runtime"
        )
    available = set(snapshot.installed_gpu_packages)
    if snapshot.cuda_runtime_available:
        available.add("cuda_runtime")
    missing = sorted(
        dependency
        for dependency in compute_fit.required_dependencies
        if dependency not in available
    )
    if missing:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution with unavailable dependencies: "
            + ", ".join(missing)
        )


def _validate_persisted_state_matches(
    state: AutoresearchState,
    *,
    state_path: Path,
) -> AutoresearchState:
    """Reject an artifact handoff if its input state changed after dispatch."""
    supplied_reference = build_authoritative_state_reference(state, state_path=state_path)
    from gateway.autoresearch import persistence as persistence_module

    persisted_state = persistence_module.load_state_file(state_path)
    persisted_reference = build_authoritative_state_reference(
        persisted_state,
        state_path=state_path,
    )
    if persisted_reference != supplied_reference:
        raise AutoresearchValidationError(
            "persisted state does not match the supplied authoritative state"
        )
    return persisted_state


def _validate_state(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    if validation_context is not None:
        validation_context.validate_for_state(state)
    if state.iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    if len(state.hypothesis_registry) > constants.MAX_HYPOTHESIS_REGISTRY_ENTRIES:
        raise AutoresearchValidationError(
            "hypothesis registry exceeds the 512-entry limit; archive the campaign and "
            "initialize a fresh state"
        )
    previous_registry_iteration = 0
    for entry in state.hypothesis_registry:
        entry.validate()
        if entry.iteration <= previous_registry_iteration:
            raise AutoresearchValidationError(
                "hypothesis registry iterations must be strictly increasing"
            )
        if entry.iteration > state.iteration or (
            entry.iteration == state.iteration and state.phase is not Phase.REPEAT
        ):
            raise AutoresearchValidationError(
                "hypothesis registry entry iteration must be before the active iteration"
            )
        previous_registry_iteration = entry.iteration
    if len(state.campaign_review_history) > constants.MAX_CAMPAIGN_REVIEW_RECORDS:
        raise AutoresearchValidationError("campaign review history exceeds the 32-record limit")
    for record in state.campaign_review_history:
        record.validate()
    state.campaign_counters.validate()
    expected_campaign_counters = derive_campaign_counters(
        state.hypothesis_registry,
        acknowledged_through_iteration=_acknowledged_through_iteration(state),
    )
    if state.campaign_counters != expected_campaign_counters:
        raise AutoresearchValidationError("campaign_counters do not match the hypothesis registry")
    if state.campaign_review_required is not (state.campaign_review_reason is not None):
        raise AutoresearchValidationError(
            "campaign_review_reason must be non-null exactly when campaign_review_required"
        )
    if state.campaign_review_reason is not None and not state.campaign_review_reason.strip():
        raise AutoresearchValidationError("campaign_review_reason must be non-empty")
    if state.campaign_review_required and (
        state.phase is not Phase.REPEAT or state.final_decision is None
    ):
        raise AutoresearchValidationError(
            "campaign_review_required state must be in repeat phase with a final decision"
        )
    if state.suspended:
        decision = state.final_decision
        if state.phase is not Phase.REPEAT or decision is None:
            raise AutoresearchValidationError(
                "suspended autoresearch state must be in repeat phase with a final decision"
            )
        if decision.decision is not FinalDecision.INFRA_BLOCKED:
            raise AutoresearchValidationError(
                "suspended autoresearch state requires final_decision=INFRA_BLOCKED"
            )
        if not state.suspension_reason or not state.suspension_reason.strip():
            raise AutoresearchValidationError(
                "suspended autoresearch state requires suspension_reason"
            )
        if (
            decision.memory_write_required
            or state.memory_written
            or state.memory_verification_receipt is not None
        ):
            raise AutoresearchValidationError(
                "suspended autoresearch state cannot require or record a memory write"
            )
    elif state.suspension_reason is not None:
        raise AutoresearchValidationError("suspension_reason requires suspended=true")
    if state.consensus_retry_count not in (0, 1):
        raise AutoresearchValidationError("consensus_retry_count must be 0 or 1")
    if state.context_packet is not None and state.setup is None:
        raise AutoresearchValidationError("context_packet requires setup first")
    if state.context_packet is not None and state.mode is None:
        raise AutoresearchValidationError("mode must be explicit after a context_packet exists")
    if state.context_packet is not None and state.mode is not state.context_packet.research_mode:
        raise AutoresearchValidationError("state mode must match context_packet research_mode")
    _validate_external_verification_retry_receipt(state, validation_context)
    if state.debate_rounds and state.context_packet is None:
        raise AutoresearchValidationError("debate history requires a context_packet")
    if state.consensus_history and state.latest_debate is None:
        raise AutoresearchValidationError("consensus history requires a debate_result")
    for consensus in state.consensus_history:
        _validate_consensus_data_requirements(consensus, require_submission_field=False)
    _validate_consensus_history_universe_plans(state)
    if (
        state.suspended
        and state.mode is ResearchMode.DATA_INFRA_G0
        and state.latest_verification is not None
    ):
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 remediation must end in non-suspending DISCARD"
        )
    _validate_alpha_universe_chain(state)
    if state.memory_written and state.final_decision is None:
        raise AutoresearchValidationError("memory_written cannot be true before final_decision")
    if (
        state.memory_written
        and state.final_decision is not None
        and not state.final_decision.memory_write_required
    ):
        raise AutoresearchValidationError(
            "memory_written is invalid when final_decision.memory_write_required=false"
        )
    if state.memory_written and state.memory_verification_receipt is None:
        raise AutoresearchValidationError("memory_written requires a memory_verification_receipt")
    if not state.memory_written and state.memory_verification_receipt is not None:
        raise AutoresearchValidationError(
            "memory_verification_receipt requires memory_written=true"
        )
    if (
        state.memory_verification_receipt is not None
        and state.final_decision is not None
        and state.memory_verification_receipt.experiment_id != state.final_decision.experiment_id
    ):
        raise AutoresearchValidationError("memory receipt experiment_id must match final_decision")
    if state.final_decision is not None:
        decision = state.final_decision
        _validate_final_decision_memory_requirement(state, decision)
        _validate_no_consensus_completion(state)
        _validate_operator_precondition_infra_blocked_suspension(state)
        is_operator_infrastructure_suspension = _is_operator_infrastructure_suspension_state(state)
        if decision.decision is FinalDecision.NO_CONSENSUS:
            if decision.memory_write_required:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS requires final_decision.memory_write_required=false"
                )
            if state.memory_verification_receipt is not None:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS must not have a memory_verification_receipt"
                )
        if not is_operator_infrastructure_suspension:
            _validate_final_decision_artifact(decision, state, validation_context)
        if not decision.memory_write_required and not _is_authorized_no_memory_final_decision(
            state
        ):
            raise AutoresearchValidationError(
                "final_decision.memory_write_required=false requires an authorized "
                "no-memory terminal path"
            )
    if state.implementation_result and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation_result requires a majority consensus")
    if state.implementation_result:
        _validate_persisted_autoresearch_workspace_path(
            state.implementation_result.workspace_path,
            label="implementation_result workspace_path",
        )
        transitions_module._validate_implementation_workspace(state, state.implementation_result)
    if state.fix_history and state.implementation_result is None:
        raise AutoresearchValidationError("fix history requires an implementation_result")
    for fix in state.fix_history:
        fix.validate()
        _validate_persisted_autoresearch_workspace_path(
            fix.workspace_path,
            label="fix_history workspace_path",
        )
        if state.implementation_result is not None and (
            fix.workspace_path != state.implementation_result.workspace_path
        ):
            raise AutoresearchValidationError(
                "fix_history workspace_path must exactly match implementation_result workspace_path"
            )
    if state.verification_history and state.implementation_result is None:
        raise AutoresearchValidationError("verification history requires an implementation_result")
    if state.review_history and not state.verification_history:
        raise AutoresearchValidationError("review history requires a verification_result")
    if state.pending_fix_trigger is not None and state.phase is not Phase.FIX_TEST:
        raise AutoresearchValidationError("pending_fix_trigger is only valid during fix_test")
    if state.final_decision is not None and state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError("final_decision requires repeat phase")
    for debate in state.debate_rounds:
        transitions_module._validate_debate_result(
            debate, policy, mode=state.mode, context=state.context_packet
        )
    for verification in state.verification_history:
        verification.validate(
            mode=state.mode,
        )
    for review in state.review_history:
        transitions_module._validate_review_result(review, policy)
    if state.phase is Phase.DEBATE and state.context_packet is None:
        raise AutoresearchValidationError("debate phase requires a context_packet")
    if state.phase is Phase.CONSENSUS and state.latest_debate is None:
        raise AutoresearchValidationError("consensus phase requires a debate_result")
    if state.phase is Phase.IMPLEMENTATION and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation phase requires a majority consensus")
    if state.phase is Phase.IMPLEMENTATION and _is_operator_precondition_consensus(
        state.latest_consensus
    ):
        raise AutoresearchValidationError(
            "operator-precondition consensus must route to decision_log, not implementation"
        )
    if state.phase is Phase.VERIFICATION and state.implementation_result is None:
        raise AutoresearchValidationError("verification phase requires an implementation_result")
    if state.phase is Phase.REVIEW:
        if not state.verification_history:
            raise AutoresearchValidationError("review phase requires a verification_result")
        if (
            state.latest_verification is None
            or state.latest_verification.status is not VerificationStatus.PASS
        ):
            raise AutoresearchValidationError("review phase requires a passing verification_result")
    if state.phase is Phase.FIX_TEST:
        if state.pending_fix_trigger is None:
            raise AutoresearchValidationError("fix_test phase requires pending_fix_trigger")
        if state.pending_fix_trigger is FixTriggerPhase.VERIFICATION and (
            state.latest_verification is None
            or state.latest_verification.status is VerificationStatus.PASS
        ):
            raise AutoresearchValidationError(
                "verification-triggered fix_test requires a failing verification_result"
            )
        if state.pending_fix_trigger is FixTriggerPhase.REVIEW:
            latest_review = state.latest_review
            if (
                latest_review is None
                or latest_review.finding_disposition is not ReviewFindingDisposition.FIX_REQUIRED
            ):
                raise AutoresearchValidationError(
                    "review-triggered fix_test requires a FIX_REQUIRED review_result"
                )
    if state.phase is Phase.DECISION_LOG and (
        state.latest_consensus is None
        and state.latest_review is None
        and state.latest_verification is None
    ):
        raise AutoresearchValidationError("decision_log phase requires prior artifacts")
    if state.phase is Phase.REPEAT and state.final_decision is None:
        raise AutoresearchValidationError("repeat phase requires final_decision")


def validate_state(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    transitions_module._validate_state(state, policy, validation_context)


def _validate_debate_result(
    debate: DebateResultArtifact,
    policy: AutoresearchPolicy,
    *,
    mode: ResearchMode | None = None,
    context: ContextPacketArtifact | None = None,
    target_repo: Path | None = None,
    require_compute_fit: bool = False,
) -> None:
    expected_ids = set(policy.debate_agent_ids)
    actual_ids = {submission.agent_id for submission in debate.submissions}
    if actual_ids != expected_ids:
        raise AutoresearchValidationError(
            "debate_result must contain exactly the configured five debate agents"
        )
    if mode is ResearchMode.ALPHA_RESEARCH and context is not None:
        burned = set(context.burned_theory_families)
        contested = set(context.contested_methodology_families)
        gated_families = burned | contested
        for submission in debate.submissions:
            family = _normalise_identifier(submission.theory_family)
            if family in gated_families and not submission.materially_new_evidence:
                category = "burned" if family in burned else "contested methodology"
                raise AutoresearchValidationError(
                    f"alpha debate theory_family is {category} and requires materially_new_evidence"
                )
    for submission in debate.submissions:
        if require_compute_fit and submission.compute_fit is None:
            raise AutoresearchValidationError(
                "new debate submissions must include a compute_fit artifact"
            )
        if submission.compute_fit is not None:
            submission.compute_fit.validate()
            if target_repo is not None:
                transitions_module._validate_compute_fit_environment(
                    submission.compute_fit, target_repo
                )


def _validate_review_result(review: ReviewResultArtifact, policy: AutoresearchPolicy) -> None:
    if review.reviewer_agent_id != policy.reviewer.agent_id:
        raise AutoresearchValidationError(
            "review_result must come from the single configured reviewer"
        )
    review.validate()


def _validate_implementation_workspace(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact,
    *,
    require_compute_fit: bool = False,
) -> None:
    artifact.validate()
    if require_compute_fit and artifact.compute_fit is None:
        raise AutoresearchValidationError(
            "new implementation_result artifacts must include a compute_fit artifact"
        )
    if artifact.compute_fit is not None:
        artifact.compute_fit.validate()
        if state.setup is not None:
            transitions_module._validate_compute_fit_environment(
                artifact.compute_fit,
                Path(state.setup.target_repo),
            )
    _validate_persisted_autoresearch_workspace_path(
        artifact.workspace_path,
        label="implementation_result workspace_path",
    )
    if state.setup is None:
        return
    workspace_path = Path(artifact.workspace_path).expanduser().resolve()
    target_repo = Path(state.setup.target_repo).expanduser().resolve()
    if workspace_path == target_repo:
        raise AutoresearchValidationError(
            "implementation_result workspace_path must be an isolated worktree, "
            "not the main target_repo"
        )


def _validate_fix_workspace(state: AutoresearchState, artifact: FixResultArtifact) -> None:
    artifact.validate()
    _validate_price_scope_fix_result_commands(state, artifact)
    _validate_persisted_autoresearch_workspace_path(
        artifact.workspace_path,
        label="fix_result workspace_path",
    )
    if state.implementation_result is None:
        raise AutoresearchValidationError("fix_result requires implementation_result")
    _validate_persisted_autoresearch_workspace_path(
        state.implementation_result.workspace_path,
        label="implementation_result workspace_path",
    )
    if artifact.workspace_path != state.implementation_result.workspace_path:
        raise AutoresearchValidationError(
            "fix_result workspace_path must match implementation_result workspace_path"
        )
    candidate_implementation = replace(
        state.implementation_result,
        commit_sha=artifact.commit_sha,
        price_hydration_scope_preflight=(
            artifact.price_hydration_scope_preflight
            if artifact.price_hydration_scope_preflight is not None
            else state.implementation_result.price_hydration_scope_preflight
        ),
    )
    transitions_module._validate_implementation_workspace(
        state,
        candidate_implementation,
    )


def _require_autoresearch_worktree_root() -> Path:
    root = _require_strict_canonical_workspace_path(
        str(constants.DEFAULT_AUTORESEARCH_WORKTREE_ROOT),
        label="autoresearch worktree root",
    )
    _require_private_directory(root, label="autoresearch worktree root")
    return root


def validate_artifact_workspace(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact | FixResultArtifact,
) -> None:
    """Mechanically validate a committed artifact at the CLI advancement boundary.

    This intentionally performs filesystem and Git checks only at artifact
    advancement; deserializing persisted state remains pure and portable.
    """
    artifact.validate()
    if state.setup is None:
        raise AutoresearchValidationError("artifact workspace validation requires setup")
    workspace = _require_strict_canonical_workspace_path(
        artifact.workspace_path,
        label="artifact workspace_path",
    )
    worktree_root = _require_autoresearch_worktree_root()
    if isinstance(artifact, FixResultArtifact):
        if state.implementation_result is None:
            raise AutoresearchValidationError("fix_result requires implementation_result")
        state.implementation_result.validate()
        implementation_workspace = _require_strict_canonical_workspace_path(
            state.implementation_result.workspace_path,
            label="persisted implementation_result workspace_path",
        )
        _require_workspace_under_autoresearch_worktree_root(
            implementation_workspace,
            label="persisted implementation_result workspace_path",
            worktree_root=worktree_root,
        )
        _require_workspace_under_autoresearch_worktree_root(
            workspace,
            label="fix_result workspace_path",
            worktree_root=worktree_root,
        )
        if artifact.workspace_path != state.implementation_result.workspace_path:
            raise AutoresearchValidationError(
                "fix_result workspace_path must exactly match implementation_result workspace_path"
            )
        if workspace != implementation_workspace:
            raise AutoresearchValidationError(
                "fix_result workspace_path must identify the persisted implementation worktree"
            )
        transitions_module._validate_fix_workspace(state, artifact)
    else:
        _require_workspace_under_autoresearch_worktree_root(
            workspace,
            label="implementation_result workspace_path",
            worktree_root=worktree_root,
        )
        transitions_module._validate_implementation_workspace(state, artifact)

    workspace = _require_isolated_git_clone_root(workspace, label="artifact workspace_path")
    target_checkout = _require_git_worktree_root(
        Path(state.setup.target_repo).expanduser(),
        label="authoritative target_repo",
    )
    if workspace == target_checkout:
        raise AutoresearchValidationError(
            "artifact workspace_path must be distinct from authoritative target_repo"
        )
    _require_artifact_origin_matches_target(
        workspace,
        target_checkout,
        label="artifact workspace_path",
    )
    artifact_commit = _resolve_git_commit(
        workspace,
        artifact.commit_sha,
        label="artifact commit_sha",
    )
    worktree_head = _resolve_git_commit(workspace, "HEAD", label="worktree HEAD")
    if artifact_commit != worktree_head:
        raise AutoresearchValidationError("artifact commit_sha must equal worktree HEAD")
    _require_clean_git_worktree(workspace)

    if isinstance(artifact, FixResultArtifact):
        assert state.implementation_result is not None
        _require_ancestor(
            workspace,
            state.implementation_result.commit_sha,
            artifact_commit,
            error_message="prior implementation commit_sha is not an ancestor of final fix commit",
            missing_is_not_ancestor=True,
        )
        authoritative_head = _resolve_git_commit(
            target_checkout,
            "HEAD",
            label="authoritative target_repo HEAD",
        )
        _require_ancestor(
            workspace,
            authoritative_head,
            artifact_commit,
            error_message=("authoritative target_repo HEAD is not an ancestor of final fix commit"),
            missing_is_not_ancestor=True,
        )
    else:
        authoritative_head = _resolve_git_commit(
            target_checkout,
            "HEAD",
            label="authoritative target_repo HEAD",
        )
        _require_ancestor(
            workspace,
            authoritative_head,
            artifact_commit,
            error_message=(
                "authoritative target_repo HEAD is not an ancestor of implementation commit"
            ),
            missing_is_not_ancestor=True,
        )
        manifest_snapshot = _secure_open_snapshot(
            artifact.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )
        _validate_quantipy_v2_manifest(
            manifest_snapshot,
            workspace=workspace,
            commit_sha=artifact_commit,
            expected_sha256=artifact.experiment_manifest_sha256,
        )
    _require_private_directory(workspace, label="artifact workspace_path")


def _clear_consumed_platform_runtime_receipts(state: AutoresearchState) -> AutoresearchState:
    """Remove active v5 authorization material once its result is in history."""
    receipt = state.external_verification_retry_receipt
    if receipt is None:
        return replace(state, canonical_quantipy_runtime_attestation=None)
    if receipt.retry_attempt != 5:
        return replace(state, canonical_quantipy_runtime_attestation=None)
    if state.platform_runtime_recovery_receipt is None:
        raise AutoresearchValidationError("v5 verification requires its runtime recovery receipt")
    if state.latest_verification is None:
        raise AutoresearchValidationError("v5 receipt cannot be consumed without a result")
    return replace(
        state,
        external_verification_retry_receipt=None,
        interrupted_verification_history=(),
        platform_runtime_recovery_receipt=None,
        canonical_quantipy_runtime_attestation=None,
    )


def _advance_final_decision(
    state: AutoresearchState,
    artifact: FinalDecisionArtifact,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    registry_entry = _build_hypothesis_registry_entry(state, artifact)
    next_registry = (*state.hypothesis_registry, registry_entry)
    next_counters = derive_campaign_counters(
        next_registry,
        acknowledged_through_iteration=_acknowledged_through_iteration(state),
    )
    next_review_required = state.campaign_review_required
    next_review_reason = state.campaign_review_reason
    next_campaign_review_history: tuple[CampaignReviewRecord, ...] = state.campaign_review_history
    if not next_review_required:
        next_review_reason = _campaign_stall_reason(next_counters, policy)
        if next_review_reason is not None:
            next_review_required = True
            next_campaign_review_history = (
                *next_campaign_review_history,
                CampaignReviewRecord(
                    triggered_iteration=state.iteration,
                    reason=next_review_reason,
                    counters=next_counters,
                    acknowledgement=None,
                    acknowledged_iteration=None,
                ),
            )[-constants.MAX_CAMPAIGN_REVIEW_RECORDS :]
    next_state = replace(
        state,
        final_decision=artifact,
        phase=Phase.REPEAT,
        suspended=(
            artifact.decision is FinalDecision.INFRA_BLOCKED
            and not _is_implementation_infra_blocked_contract(state, artifact)
        ),
        suspension_reason=(
            artifact.infra_rationale
            if (
                artifact.decision is FinalDecision.INFRA_BLOCKED
                and not _is_implementation_infra_blocked_contract(state, artifact)
            )
            else None
        ),
        hypothesis_registry=next_registry,
        campaign_counters=next_counters,
        campaign_review_required=next_review_required,
        campaign_review_reason=next_review_reason,
        campaign_review_history=next_campaign_review_history,
    )
    transitions_module._validate_state(next_state, policy, validation_context)
    return next_state


def advance_state(
    state: AutoresearchState,
    artifact: SetupContextArtifact
    | ContextPacketArtifact
    | DebateResultArtifact
    | ConsensusResultArtifact
    | ImplementationResultArtifact
    | VerificationResultArtifact
    | ReviewResultArtifact
    | FixResultArtifact
    | FinalDecisionArtifact,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
    *,
    state_path: Path | None = None,
    expected_instruction_manifest_sha256: str | None = None,
    runs_root: Path | None = None,
    xnys_calendar_path: Path | None = None,
    xnys_calendar_content: Mapping[str, object] | None = None,
) -> AutoresearchState:
    if state_path is not None:
        state = transitions_module._validate_persisted_state_matches(state, state_path=state_path)
    transitions_module._validate_state(state, policy, validation_context)
    if state.mode in (ResearchMode.ALPHA_RESEARCH, ResearchMode.DATA_INFRA_G0) and (
        state.phase is Phase.VERIFICATION
        or (state.mode is ResearchMode.DATA_INFRA_G0 and state.phase is Phase.DECISION_LOG)
    ):
        if validation_context is None:
            raise AutoresearchValidationError(
                f"{state.mode.name} artifact advancement requires a strict readiness "
                "validation context"
            )
        validation_context.validate_for_state(state)

    if state.phase is Phase.SETUP_CONTEXT:
        if isinstance(artifact, SetupContextArtifact):
            if state.setup is not None:
                raise AutoresearchValidationError("setup artifact already exists")
            return replace(state, setup=artifact)
        if isinstance(artifact, ContextPacketArtifact):
            if state.setup is None:
                raise AutoresearchValidationError("context packet requires setup first")
            return replace(
                state,
                context_packet=artifact,
                mode=artifact.research_mode,
                phase=Phase.DEBATE,
            )
        raise AutoresearchValidationError(
            "setup_context phase accepts setup or context_packet artifacts only"
        )

    if state.phase is Phase.DEBATE:
        if not isinstance(artifact, DebateResultArtifact):
            raise AutoresearchValidationError("debate phase accepts debate_result only")
        transitions_module._validate_debate_result(
            artifact,
            policy,
            mode=state.mode,
            context=state.context_packet,
            target_repo=Path(state.setup.target_repo) if state.setup is not None else None,
            require_compute_fit=True,
        )
        expected_round = len(state.debate_rounds) + 1
        if artifact.round_number != expected_round:
            raise AutoresearchValidationError(
                f"debate round must be {expected_round}, got {artifact.round_number}"
            )
        return replace(
            state,
            debate_rounds=(*state.debate_rounds, artifact),
            phase=Phase.CONSENSUS,
        )

    if state.phase is Phase.CONSENSUS:
        if not isinstance(artifact, ConsensusResultArtifact):
            raise AutoresearchValidationError("consensus phase accepts consensus_result only")
        latest_debate = state.latest_debate
        if latest_debate is None:
            raise AutoresearchValidationError("consensus requires a debate_result first")
        if artifact.round_number != latest_debate.round_number:
            raise AutoresearchValidationError(
                "consensus round_number must match the latest debate round"
            )
        if artifact.status is ConsensusStatus.NO_CONSENSUS and state.consensus_retry_count >= 1:
            raise AutoresearchValidationError(
                "NO_CONSENSUS retry is exhausted; the arbiter must resolve the split "
                "with the pre-registered deterministic tie-break"
            )
        _validate_consensus_data_requirements(artifact, require_submission_field=True)
        _validate_consensus_novelty_gate(state, artifact)
        next_consensus_history = (*state.consensus_history, artifact)
        if artifact.status is ConsensusStatus.MAJORITY:
            if _is_operator_precondition_consensus(artifact):
                return replace(
                    state,
                    consensus_history=next_consensus_history,
                    phase=Phase.DECISION_LOG,
                )
            if artifact.universe_plan is None:
                raise AutoresearchValidationError(
                    "non-operator majority consensus requires a frozen universe_plan"
                )
            artifact.universe_plan.validate()
            return replace(
                state,
                consensus_history=next_consensus_history,
                phase=Phase.IMPLEMENTATION,
            )
        if state.consensus_retry_count == 0:
            return replace(
                state,
                consensus_history=next_consensus_history,
                consensus_retry_count=1,
                phase=Phase.DEBATE,
            )
        return replace(
            state,
            consensus_history=next_consensus_history,
            phase=Phase.DECISION_LOG,
        )

    if state.phase is Phase.IMPLEMENTATION:
        if isinstance(artifact, FinalDecisionArtifact):
            if not _is_implementation_infra_blocked_contract(state, artifact):
                raise AutoresearchValidationError(
                    "implementation phase final_decision requires an ALPHA_RESEARCH "
                    "INFRA_BLOCKED artifact with the no-memory runtime-contract fields"
                )
            _validate_final_decision_artifact(artifact, state, validation_context)
            return _advance_final_decision(state, artifact, policy, validation_context)
        if not isinstance(artifact, ImplementationResultArtifact):
            raise AutoresearchValidationError(
                "implementation phase accepts implementation_result only"
            )
        if (
            state.latest_consensus is None
            or state.latest_consensus.status is not ConsensusStatus.MAJORITY
        ):
            raise AutoresearchValidationError(
                "cannot advance implementation without consensus majority"
            )
        if state_path is not None:
            transitions_module.validate_artifact_workspace(state, artifact)
        transitions_module._validate_implementation_workspace(
            state, artifact, require_compute_fit=True
        )
        _validate_alpha_implementation_price_preflight(
            state,
            artifact,
            validation_context,
            calendar_path=xnys_calendar_path,
            calendar_content=xnys_calendar_content,
        )
        next_state = replace(state, implementation_result=artifact, phase=Phase.VERIFICATION)
        _validate_alpha_universe_chain(next_state)
        return next_state

    if state.phase is Phase.VERIFICATION:
        if not isinstance(artifact, VerificationResultArtifact):
            raise AutoresearchValidationError("verification phase accepts verification_result only")
        if state.implementation_result is None:
            raise AutoresearchValidationError("verification requires implementation_result")
        artifact.validate(mode=state.mode)
        _validate_alpha_price_scope_verification(state, artifact)
        _require_g0_platform_provenance(state, artifact, validation_context)
        if state_path is not None:
            _validate_quantipy_experiment_evidence(
                state,
                artifact,
                validation_context=validation_context,
                state_path=state_path,
                expected_instruction_manifest_sha256=expected_instruction_manifest_sha256,
                runs_root=runs_root,
            )
        next_verification_history = (*state.verification_history, artifact)
        consumed_runtime_recovery = transitions_module._clear_consumed_platform_runtime_receipts(
            replace(state, verification_history=next_verification_history)
        )
        if artifact.status is VerificationStatus.PASS:
            next_state = replace(
                consumed_runtime_recovery,
                pending_fix_trigger=None,
                phase=Phase.REVIEW,
            )
            _validate_alpha_universe_chain(next_state, validation_context)
            return next_state
        if (
            artifact.status in (VerificationStatus.TEST_FAILURE, VerificationStatus.BUG_SIGNAL)
            and state.verification_fix_attempts >= 2
        ):
            next_state = replace(
                consumed_runtime_recovery,
                pending_fix_trigger=None,
                phase=Phase.DECISION_LOG,
            )
            _validate_alpha_universe_chain(next_state, validation_context)
            return next_state
        next_state = replace(
            consumed_runtime_recovery,
            pending_fix_trigger=FixTriggerPhase.VERIFICATION,
            phase=Phase.FIX_TEST,
        )
        _validate_alpha_universe_chain(next_state, validation_context)
        return next_state

    if state.phase is Phase.REVIEW:
        if not isinstance(artifact, ReviewResultArtifact):
            raise AutoresearchValidationError("review phase accepts review_result only")
        transitions_module._validate_review_result(artifact, policy)
        next_review_history = (*state.review_history, artifact)
        if artifact.finding_disposition is ReviewFindingDisposition.FIX_REQUIRED:
            # Review-fix rounds are BOUNDED. Findings that are properties of
            # the evidence (a bootstrap interval, fold concentration) cannot
            # be changed by any code fix; unbounded routing produced a
            # five-lap livelock in iteration 24 with no exit, because
            # fix_test only accepts fix results and the DISCARD ladder is
            # unreachable outside decision_log. After the cap, the iteration
            # proceeds to decision_log where the deterministic rule
            # "critical review issue remains: DISCARD" applies.
            if len(next_review_history) >= MAX_REVIEW_FIX_ROUNDS:
                return replace(
                    state,
                    review_history=next_review_history,
                    pending_fix_trigger=None,
                    phase=Phase.DECISION_LOG,
                )
            return replace(
                state,
                review_history=next_review_history,
                pending_fix_trigger=FixTriggerPhase.REVIEW,
                phase=Phase.FIX_TEST,
            )
        return replace(
            state,
            review_history=next_review_history,
            pending_fix_trigger=None,
            phase=Phase.DECISION_LOG,
        )

    if state.phase is Phase.FIX_TEST:
        if not isinstance(artifact, FixResultArtifact):
            raise AutoresearchValidationError("fix_test phase accepts fix_result only")
        if state.pending_fix_trigger is None:
            raise AutoresearchValidationError("fix_test phase requires pending_fix_trigger")
        if artifact.trigger_phase is not state.pending_fix_trigger:
            raise AutoresearchValidationError(
                "fix_result trigger_phase must match the pending fix source"
            )
        if artifact.trigger_phase is FixTriggerPhase.VERIFICATION:
            next_attempts = state.verification_fix_attempts + 1
        else:
            next_attempts = state.verification_fix_attempts
        if state_path is not None:
            transitions_module.validate_artifact_workspace(state, artifact)
        transitions_module._validate_fix_workspace(state, artifact)
        assert state.implementation_result is not None
        next_implementation = replace(
            state.implementation_result,
            commit_sha=artifact.commit_sha,
            price_hydration_scope_preflight=(
                artifact.price_hydration_scope_preflight
                if artifact.price_hydration_scope_preflight is not None
                else state.implementation_result.price_hydration_scope_preflight
            ),
        )
        return replace(
            state,
            implementation_result=next_implementation,
            fix_history=(*state.fix_history, artifact),
            external_verification_retry_receipt=None,
            interrupted_verification_history=(),
            platform_runtime_recovery_receipt=None,
            verification_fix_attempts=next_attempts,
            pending_fix_trigger=None,
            phase=Phase.VERIFICATION,
        )

    if state.phase is Phase.DECISION_LOG:
        if not isinstance(artifact, FinalDecisionArtifact):
            raise AutoresearchValidationError("decision_log phase accepts final_decision only")
        if (
            state.latest_consensus is None
            and state.latest_review is None
            and state.latest_verification is None
        ):
            raise AutoresearchValidationError("final_decision requires prior artifacts")
        _validate_final_decision_artifact(artifact, state, validation_context)
        return _advance_final_decision(state, artifact, policy, validation_context)

    raise AutoresearchValidationError(
        "repeat phase does not accept artifacts; mark memory or start next iteration"
    )
