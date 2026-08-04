from __future__ import annotations

from dataclasses import replace

import pytest
from gateway.autoresearch.constants import MAX_HYPOTHESIS_REGISTRY_ENTRIES
from gateway.autoresearch.enums import ConsensusStatus, FinalDecision, ResearchMode
from gateway.autoresearch.errors import AutoresearchValidationError
from gateway.autoresearch.governance import (
    CampaignCounters,
    HypothesisRegistryEntry,
    contested_families,
    derive_campaign_counters,
    family_trial_counts,
    theory_family_fingerprint,
)
from gateway.autoresearch.policy import AutoresearchPolicy
from gateway.autoresearch.state import AutoresearchState
from gateway.autoresearch.transitions import validate_state

from tests.gateway.autoresearch.builders import (
    _debate_result,
    _majority_consensus,
    _no_consensus,
)


def test_theory_family_fingerprint_is_deterministic_and_normalizes_family(
    policy: AutoresearchPolicy,
) -> None:
    consensus = _majority_consensus(round_number=1, policy=policy)

    first = theory_family_fingerprint(consensus, ResearchMode.ALPHA_RESEARCH)
    second = theory_family_fingerprint(
        replace(consensus, winner_theory_family=" VWAP_OBV "), ResearchMode.ALPHA_RESEARCH
    )

    assert first == second
    assert first is not None
    assert len(first) == 64


def test_theory_family_fingerprint_is_none_for_no_consensus_and_operator_precondition(
    policy: AutoresearchPolicy,
) -> None:
    assert theory_family_fingerprint(_no_consensus(1), ResearchMode.ALPHA_RESEARCH) is None
    operator = replace(
        _majority_consensus(1, policy),
        winner_theory_id="no-code-operator-evidence-precondition",
        winner_theory_family="no-code-operator-evidence-precondition",
        implementation_brief="Do not enter ENGINEER and do not modify Quantipy.",
    )

    assert theory_family_fingerprint(operator, ResearchMode.ALPHA_RESEARCH) is None


def test_contested_families_extracts_two_vote_families(policy: AutoresearchPolicy) -> None:
    debate = _debate_result(policy, round_number=1)

    assert contested_families(debate) == ("bollinger-regime", "vwap-obv")


def test_campaign_counters_ignore_infrastructure_entries() -> None:
    entries = (
        HypothesisRegistryEntry(
            1,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.MAJORITY,
            FinalDecision.DISCARD,
            "family-a",
            (),
            "a" * 64,
            0.1,
            "discarded",
            None,
        ),
        HypothesisRegistryEntry(
            2,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.MAJORITY,
            FinalDecision.INFRA_BLOCKED,
            "family-a",
            (),
            None,
            None,
            "blocked",
            None,
        ),
        HypothesisRegistryEntry(
            3,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.NO_CONSENSUS,
            FinalDecision.NO_CONSENSUS,
            None,
            ("family-b",),
            None,
            None,
            "no consensus",
            None,
        ),
        HypothesisRegistryEntry(
            4,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.MAJORITY,
            FinalDecision.INFRA_REPAIRED,
            "family-a",
            (),
            None,
            None,
            "repaired",
            None,
        ),
    )

    assert derive_campaign_counters(entries, acknowledged_through_iteration=0) == CampaignCounters(
        consecutive_non_keep=2,
        consecutive_no_consensus=1,
        iterations_since_last_keep=4,
    )
    assert derive_campaign_counters(entries, acknowledged_through_iteration=3) == CampaignCounters(
        consecutive_non_keep=0,
        consecutive_no_consensus=0,
        iterations_since_last_keep=4,
    )


def test_registry_entry_round_trips_with_exact_keys() -> None:
    entry = HypothesisRegistryEntry(
        1,
        ResearchMode.ALPHA_RESEARCH,
        ConsensusStatus.MAJORITY,
        FinalDecision.DISCARD,
        "family-a",
        (),
        "a" * 64,
        0.1,
        "a deterministic reason",
        None,
    )

    assert HypothesisRegistryEntry.from_dict(entry.to_dict()) == entry
    with pytest.raises(AutoresearchValidationError, match="exact keys"):
        HypothesisRegistryEntry.from_dict({**entry.to_dict(), "extra": True})


def test_family_trial_counts_omits_no_consensus_entries() -> None:
    entries = (
        HypothesisRegistryEntry(
            1,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.MAJORITY,
            FinalDecision.DISCARD,
            "family-a",
            (),
            "a" * 64,
            None,
            "discarded",
            None,
        ),
        HypothesisRegistryEntry(
            2,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.NO_CONSENSUS,
            FinalDecision.NO_CONSENSUS,
            None,
            ("family-a",),
            None,
            None,
            "split",
            None,
        ),
    )

    assert family_trial_counts(entries) == {"family-a": 1}


def test_infrastructure_neutral_entry_has_none_consensus_shape() -> None:
    entry = HypothesisRegistryEntry(
        1,
        ResearchMode.ALPHA_RESEARCH,
        ConsensusStatus.NONE,
        FinalDecision.INFRA_BLOCKED,
        None,
        (),
        None,
        None,
        "blocked before consensus",
        None,
    )

    entry.validate()
    assert HypothesisRegistryEntry.from_dict(entry.to_dict()) == entry


@pytest.mark.parametrize(
    ("entry", "message"),
    (
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.NO_CONSENSUS,
                FinalDecision.NO_CONSENSUS,
                None,
                ("a", "b", "c"),
                None,
                None,
                "too many",
                None,
            ),
            "at most 2",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.NO_CONSENSUS,
                FinalDecision.NO_CONSENSUS,
                None,
                ("vwap-obv", "bollinger-regime"),
                None,
                None,
                "unsorted",
                None,
            ),
            "sorted and unique",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.MAJORITY,
                FinalDecision.DISCARD,
                "family-a",
                ("family-b",),
                "a" * 64,
                None,
                "majority",
                None,
            ),
            "requires NO_CONSENSUS",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.NO_CONSENSUS,
                FinalDecision.INFRA_BLOCKED,
                None,
                ("family-a",),
                None,
                None,
                "infra",
                None,
            ),
            "requires NO_CONSENSUS",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.MAJORITY,
                FinalDecision.DISCARD,
                "family-a",
                (),
                "a" * 64,
                None,
                "x" * 161,
                None,
            ),
            "160 character",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.MAJORITY,
                FinalDecision.DISCARD,
                "Family A",
                (),
                "a" * 64,
                None,
                "not normalized",
                None,
            ),
            "normalized lowercase",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.MAJORITY,
                FinalDecision.DISCARD,
                None,
                (),
                None,
                None,
                "missing family",
                None,
            ),
            "family must be null",
        ),
        (
            HypothesisRegistryEntry(
                1,
                ResearchMode.ALPHA_RESEARCH,
                ConsensusStatus.NONE,
                FinalDecision.INFRA_BLOCKED,
                "family-a",
                (),
                None,
                None,
                "invalid none family",
                None,
            ),
            "family must be null",
        ),
    ),
)
def test_registry_entry_validate_rejects_forbidden_shapes(
    entry: HypothesisRegistryEntry,
    message: str,
) -> None:
    with pytest.raises(AutoresearchValidationError, match=message):
        entry.validate()


def test_state_validation_rejects_registry_over_the_512_entry_cap(
    policy: AutoresearchPolicy,
) -> None:
    entries = tuple(
        HypothesisRegistryEntry(
            iteration,
            ResearchMode.ALPHA_RESEARCH,
            ConsensusStatus.MAJORITY,
            FinalDecision.DISCARD,
            "family-a",
            (),
            "a" * 64,
            None,
            "discarded",
            None,
        )
        for iteration in range(1, MAX_HYPOTHESIS_REGISTRY_ENTRIES + 2)
    )
    state = replace(
        AutoresearchState(iteration=len(entries)),
        hypothesis_registry=entries,
        campaign_counters=derive_campaign_counters(
            entries,
            acknowledged_through_iteration=0,
        ),
    )

    with pytest.raises(AutoresearchValidationError, match="512-entry limit"):
        validate_state(state, policy)
