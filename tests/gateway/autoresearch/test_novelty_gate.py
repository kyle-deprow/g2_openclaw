from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from gateway.autoresearch.artifacts import UniversePlanArtifact
from gateway.autoresearch.configuration import load_autoresearch_policy
from gateway.autoresearch.constants import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    NEXT_ACTION_PROMPT_TARGET_BYTES,
)
from gateway.autoresearch.engine import _phase_instruction, next_action
from gateway.autoresearch.enums import (
    ArtifactType,
    ConsensusStatus,
    FinalDecision,
    Phase,
    ResearchMode,
    ReviewVerdict,
)
from gateway.autoresearch.errors import AutoresearchValidationError
from gateway.autoresearch.governance import (
    HypothesisRegistryEntry,
    derive_campaign_counters,
    theory_family_fingerprint,
)
from gateway.autoresearch.policy import AutoresearchPolicy, ReceiptCatalog
from gateway.autoresearch.prompts import _negative_results_ledger
from gateway.autoresearch.state import AutoresearchState
from gateway.autoresearch.transitions import (
    _build_hypothesis_registry_entry,
    _validate_consensus_novelty_gate,
    advance_state,
    validate_state,
)
from gateway.autoresearch_readiness import PlatformReadinessManifest

from tests.gateway.autoresearch.builders import (
    _final_decision,
    _majority_consensus,
    _no_consensus,
    _operator_precondition_consensus,
    _review_result,
    _setup_artifact,
    _state_to_consensus,
    _state_to_decision,
    _state_to_review,
)


def _policy() -> AutoresearchPolicy:
    return load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)


def _prior_entry(
    policy: AutoresearchPolicy,
    *,
    decision: FinalDecision,
    consensus_status: ConsensusStatus = ConsensusStatus.MAJORITY,
    contested_families: tuple[str, ...] = (),
    novelty_delta_sha256: str | None = None,
) -> HypothesisRegistryEntry:
    consensus = _majority_consensus(round_number=1, policy=policy)
    family = "vwap-obv" if consensus_status is not ConsensusStatus.NO_CONSENSUS else None
    fingerprint = (
        theory_family_fingerprint(consensus, ResearchMode.ALPHA_RESEARCH)
        if family is not None
        else None
    )
    return HypothesisRegistryEntry(
        iteration=1,
        research_mode=ResearchMode.ALPHA_RESEARCH,
        consensus_status=consensus_status,
        decision=decision,
        family=family,
        contested_families=contested_families,
        fingerprint=fingerprint,
        metric_value=0.1,
        reason="prior result",
        novelty_delta_sha256=novelty_delta_sha256,
    )


def _active_state(
    policy: AutoresearchPolicy,
    entry: HypothesisRegistryEntry | None = None,
) -> AutoresearchState:
    state = _state_to_consensus(policy)
    registry = (entry,) if entry is not None else ()
    return replace(
        state,
        iteration=2,
        hypothesis_registry=registry,
        campaign_counters=derive_campaign_counters(
            registry,
            acknowledged_through_iteration=0,
        ),
    )


@pytest.mark.parametrize("decision", [FinalDecision.DISCARD, FinalDecision.CRASH])
def test_tier_one_repeated_fingerprint_requires_delta(
    decision: FinalDecision,
) -> None:
    policy = _policy()
    state = _active_state(policy, _prior_entry(policy, decision=decision))
    consensus = _majority_consensus(round_number=1, policy=policy)

    with pytest.raises(
        AutoresearchValidationError,
        match=rf"winner_theory_family 'vwap-obv' repeats iteration 1 {decision.value}",
    ):
        _validate_consensus_novelty_gate(state, consensus)


def test_tier_one_no_consensus_decision_requires_delta() -> None:
    policy = _policy()
    state = _active_state(
        policy,
        _prior_entry(policy, decision=FinalDecision.NO_CONSENSUS),
    )

    with pytest.raises(AutoresearchValidationError, match="repeats iteration 1 NO_CONSENSUS"):
        _validate_consensus_novelty_gate(
            state,
            _majority_consensus(round_number=1, policy=policy),
        )


def test_tier_two_no_consensus_deadlock_requires_delta() -> None:
    policy = _policy()
    entry = _prior_entry(
        policy,
        decision=FinalDecision.NO_CONSENSUS,
        consensus_status=ConsensusStatus.NO_CONSENSUS,
        contested_families=("bollinger-regime", "vwap-obv"),
        novelty_delta_sha256=None,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="repeats the iteration 1 NO_CONSENSUS deadlock",
    ):
        _validate_consensus_novelty_gate(
            _active_state(policy, entry),
            _majority_consensus(round_number=1, policy=policy),
        )


def test_required_delta_is_accepted_and_returned_as_hash() -> None:
    policy = _policy()
    entry = _prior_entry(policy, decision=FinalDecision.DISCARD)
    consensus = replace(
        _majority_consensus(round_number=1, policy=policy),
        novelty_delta="The new plan adds an independent liquidity-conditioned feature family.",
    )

    digest = _validate_consensus_novelty_gate(_active_state(policy, entry), consensus)

    assert digest is not None
    assert len(digest) == 64


def test_decision_log_stores_the_accepted_delta_hash() -> None:
    policy = _policy()
    delta = "The new plan adds an independent liquidity-conditioned feature family."
    state = _state_to_decision(policy)
    assert state.latest_consensus is not None
    state = replace(
        state,
        consensus_history=(replace(state.latest_consensus, novelty_delta=delta),),
    )

    advanced = advance_state(state, _final_decision(), policy)

    assert (
        advanced.hypothesis_registry[-1].novelty_delta_sha256 == sha256(delta.encode()).hexdigest()
    )


def test_duplicate_delta_hash_is_rejected() -> None:
    policy = _policy()
    delta = "The new plan adds an independent liquidity-conditioned feature family."
    entry = _prior_entry(
        policy,
        decision=FinalDecision.DISCARD,
        novelty_delta_sha256=sha256(delta.encode()).hexdigest(),
    )
    consensus = replace(
        _majority_consensus(round_number=1, policy=policy),
        novelty_delta=delta,
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="novelty_delta duplicates the iteration 1 delta verbatim",
    ):
        _validate_consensus_novelty_gate(_active_state(policy, entry), consensus)


def test_gratuitous_delta_is_rejected() -> None:
    policy = _policy()
    consensus = replace(
        _majority_consensus(round_number=1, policy=policy),
        universe_plan=UniversePlanArtifact(
            profile_id="liquid-common-stocks-v1",
            profile_digest="a" * 64,
            selection_dates=("2021-01-04", "2021-01-05"),
            max_members_per_date=300,
            execution_policy="next-session-or-later",
        ),
        novelty_delta="This delta is gratuitous because the fingerprint is new.",
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="novelty_delta is only valid when the theory family repeats",
    ):
        _validate_consensus_novelty_gate(
            _active_state(policy, _prior_entry(policy, decision=FinalDecision.DISCARD)),
            consensus,
        )


def test_same_family_different_universe_after_discard_is_the_documented_non_rule() -> None:
    policy = _policy()
    consensus = replace(
        _majority_consensus(round_number=1, policy=policy),
        universe_plan=UniversePlanArtifact(
            profile_id="liquid-common-stocks-v1",
            profile_digest="a" * 64,
            selection_dates=("2021-01-04", "2021-01-05"),
            max_members_per_date=300,
            execution_policy="next-session-or-later",
        ),
    )

    assert (
        _validate_consensus_novelty_gate(
            _active_state(policy, _prior_entry(policy, decision=FinalDecision.DISCARD)),
            consensus,
        )
        is None
    )


def test_no_consensus_and_operator_precondition_are_gate_exempt() -> None:
    policy = _policy()
    state = _active_state(policy, _prior_entry(policy, decision=FinalDecision.DISCARD))

    assert _validate_consensus_novelty_gate(state, _no_consensus(1)) is None
    operator = replace(
        _operator_precondition_consensus(1, policy),
        novelty_delta="This operator precondition is not a research hypothesis delta.",
    )
    with pytest.raises(
        AutoresearchValidationError,
        match="operator-precondition consensus must not include novelty_delta",
    ):
        operator.validate()
    with pytest.raises(
        AutoresearchValidationError,
        match="operator-precondition consensus must not include novelty_delta",
    ):
        _validate_consensus_novelty_gate(state, operator)

    operator_state = replace(
        _state_to_consensus(policy),
        consensus_history=(operator,),
        phase=Phase.DECISION_LOG,
    )
    entry = _build_hypothesis_registry_entry(operator_state, _final_decision())
    assert entry.novelty_delta_sha256 is None


def test_gate_is_not_a_load_invariant_and_does_not_match_its_own_entry() -> None:
    policy = _policy()
    entry = _prior_entry(policy, decision=FinalDecision.DISCARD)
    reviewed = _state_to_review(policy)
    state = replace(
        reviewed,
        phase=Phase.REPEAT,
        iteration=1,
        final_decision=_final_decision(),
        review_history=(_review_result(ReviewVerdict.PASS, policy),),
        hypothesis_registry=(entry,),
        campaign_counters=derive_campaign_counters((entry,), acknowledged_through_iteration=0),
    )
    # The direct gate reads only entries with iteration < state.iteration.
    assert (
        _validate_consensus_novelty_gate(
            state,
            _majority_consensus(round_number=1, policy=policy),
        )
        is None
    )

    # A persisted repeat state can contain its just-completed entry; loading it
    # does not run the acceptance-time novelty gate.
    validate_state(state, policy)


def test_empty_registry_advancement_never_fires_gate() -> None:
    policy = _policy()
    state = _state_to_consensus(policy)
    advanced = advance_state(
        state,
        _majority_consensus(round_number=1, policy=policy),
        policy,
    )

    assert advanced.phase is Phase.IMPLEMENTATION
    assert _negative_results_ledger(state) == ""


def test_negative_results_ledger_has_twelve_entry_and_2048_byte_caps() -> None:
    policy = _policy()
    entries = tuple(
        replace(
            _prior_entry(policy, decision=FinalDecision.DISCARD),
            iteration=iteration,
            reason=(f"negative result {iteration} " + ("detail " * 18)).strip(),
        )
        for iteration in range(1, 513)
    )
    state = replace(AutoresearchState(iteration=513), hypothesis_registry=entries)

    ledger = _negative_results_ledger(state)
    lines = ledger.splitlines()
    rows = [line for line in lines if line.startswith("iteration=")]

    assert len(ledger.encode("utf-8")) <= 2048
    assert len(rows) <= 12
    assert rows[0].startswith("iteration=512|")
    assert lines[-1] == f"... {512 - len(rows)} older negative results omitted"
    assert all("|contested=" in row for row in rows)


def test_negative_results_ledger_honors_the_twelve_entry_cap_before_bytes() -> None:
    policy = _policy()
    entries = tuple(
        replace(
            _prior_entry(policy, decision=FinalDecision.DISCARD),
            iteration=iteration,
            fingerprint=None,
            metric_value=None,
            reason="x",
        )
        for iteration in range(1, 21)
    )
    ledger = _negative_results_ledger(
        replace(AutoresearchState(iteration=21), hypothesis_registry=entries)
    )
    rows = [line for line in ledger.splitlines() if line.startswith("iteration=")]

    assert len(rows) == 12
    assert rows[0].startswith("iteration=20|")
    assert ledger.splitlines()[-1] == "... 8 older negative results omitted"
    assert len(ledger.encode("utf-8")) <= 2048


@pytest.mark.parametrize(
    "artifact_type",
    [ArtifactType.CONTEXT_PACKET, ArtifactType.DEBATE_RESULT, ArtifactType.CONSENSUS_RESULT],
)
def test_negative_results_ledger_is_injected_for_all_three_model_stages(
    artifact_type: ArtifactType,
) -> None:
    policy = _policy()
    entry = _prior_entry(policy, decision=FinalDecision.DISCARD)
    state = replace(
        _state_to_consensus(policy),
        hypothesis_registry=(entry,),
        campaign_counters=derive_campaign_counters((entry,), acknowledged_through_iteration=0),
    )
    phase = {
        ArtifactType.CONTEXT_PACKET: Phase.SETUP_CONTEXT,
        ArtifactType.DEBATE_RESULT: Phase.DEBATE,
        ArtifactType.CONSENSUS_RESULT: Phase.CONSENSUS,
    }[artifact_type]

    prompt = _phase_instruction(
        state,
        phase,
        artifact_type,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )

    assert "Negative-results ledger (newest first):" in prompt
    assert "iteration=1|decision=DISCARD|" in prompt


def test_512_entry_registry_keeps_context_prompt_under_target_margin(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    entries = tuple(
        replace(
            _prior_entry(policy, decision=FinalDecision.DISCARD),
            iteration=iteration,
            reason=(f"negative result {iteration} " + ("detail " * 18)).strip(),
        )
        for iteration in range(1, 513)
    )
    state = AutoresearchState(
        iteration=513,
        setup=_setup_artifact(),
        platform_readiness=platform_readiness.identity(),
        hypothesis_registry=entries,
        campaign_counters=derive_campaign_counters(
            entries,
            acknowledged_through_iteration=0,
        ),
    )

    action = next_action(state, policy, receipts, platform_readiness)

    assert len(_negative_results_ledger(state).encode("utf-8")) <= 2048
    assert len(action.prompt_text.encode("utf-8")) <= NEXT_ACTION_PROMPT_TARGET_BYTES - 1024


def test_ledger_is_empty_and_non_stage_prompts_are_unchanged_by_registry() -> None:
    policy = _policy()
    empty = _state_to_consensus(policy)
    entry = _prior_entry(policy, decision=FinalDecision.DISCARD)
    with_registry = replace(
        empty,
        hypothesis_registry=(entry,),
        campaign_counters=derive_campaign_counters((entry,), acknowledged_through_iteration=0),
    )

    assert _negative_results_ledger(empty) == ""
    baseline = _phase_instruction(
        empty,
        Phase.IMPLEMENTATION,
        ArtifactType.IMPLEMENTATION_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    changed = _phase_instruction(
        with_registry,
        Phase.IMPLEMENTATION,
        ArtifactType.IMPLEMENTATION_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    assert baseline == changed

    verification = _state_to_review(policy)
    verification_with_registry = replace(
        verification,
        hypothesis_registry=(entry,),
        campaign_counters=derive_campaign_counters((entry,), acknowledged_through_iteration=0),
    )
    baseline = _phase_instruction(
        verification,
        Phase.VERIFICATION,
        ArtifactType.VERIFICATION_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    changed = _phase_instruction(
        verification_with_registry,
        Phase.VERIFICATION,
        ArtifactType.VERIFICATION_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    assert baseline == changed
