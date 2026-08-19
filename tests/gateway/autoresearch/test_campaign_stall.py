from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import cast

import gateway.cli as gateway_cli
import pytest
from gateway.autoresearch.configuration import load_autoresearch_policy
from gateway.autoresearch.constants import DEFAULT_OPENCLAW_CONFIG_PATH
from gateway.autoresearch.engine import next_action
from gateway.autoresearch.enums import ConsensusStatus, FinalDecision, Phase, ResearchMode
from gateway.autoresearch.errors import AutoresearchConfigError, AutoresearchValidationError
from gateway.autoresearch.governance import (
    CampaignCounters,
    CampaignReviewRecord,
    HypothesisRegistryEntry,
    derive_campaign_counters,
)
from gateway.autoresearch.lifecycle import acknowledge_campaign_review, start_next_iteration
from gateway.autoresearch.policy import AutoresearchPolicy, CampaignGovernancePolicy, ReceiptCatalog
from gateway.autoresearch.state import AutoresearchState
from gateway.autoresearch.transitions import (
    _campaign_stall_reason,
    advance_state,
    build_authoritative_state_reference,
)
from gateway.autoresearch_readiness import PlatformReadinessManifest
from gateway.autoresearch_supervisor import (
    AutoresearchSupervisor,
    SupervisorConfig,
    SupervisorOutcome,
    SupervisorResult,
)
from typer.testing import CliRunner

from tests.gateway.autoresearch.builders import (
    _final_decision,
    _final_decision_with,
    _state_to_decision,
)


def _registry_entry(iteration: int, decision: FinalDecision) -> HypothesisRegistryEntry:
    return HypothesisRegistryEntry(
        iteration=iteration,
        research_mode=ResearchMode.ALPHA_RESEARCH,
        consensus_status=ConsensusStatus.MAJORITY,
        decision=decision,
        family="vwap-obv",
        contested_families=(),
        fingerprint="a" * 64,
        metric_value=0.0,
        reason="discard",
        novelty_delta_sha256=None,
    )


def _decision_state(
    policy: AutoresearchPolicy,
    entries: tuple[HypothesisRegistryEntry, ...],
) -> AutoresearchState:
    base = _state_to_decision(policy)
    return replace(
        base,
        phase=Phase.DECISION_LOG,
        iteration=len(entries) + 1,
        final_decision=None,
        memory_written=False,
        memory_verification_receipt=None,
        hypothesis_registry=entries,
        campaign_counters=derive_campaign_counters(
            entries,
            acknowledged_through_iteration=0,
        ),
        campaign_review_required=False,
        campaign_review_reason=None,
        campaign_review_history=(),
    )


def test_default_policy_and_model_summary_remain_stable(policy: AutoresearchPolicy) -> None:
    assert policy.campaign_governance == CampaignGovernancePolicy()
    custom = replace(
        policy,
        campaign_governance=CampaignGovernancePolicy(
            stall_consecutive_non_keep=9,
            stall_consecutive_no_consensus=7,
        ),
    )
    assert custom.model_policy_summary() == policy.model_policy_summary()


def test_optional_campaign_governance_config_is_strict_and_non_default(tmp_path: Path) -> None:
    raw = cast(dict[str, object], json.loads(DEFAULT_OPENCLAW_CONFIG_PATH.read_text()))
    agents = cast(dict[str, object], raw["agents"])
    defaults = cast(dict[str, object], agents["defaults"])
    defaults["autoresearchCampaignGovernance"] = {
        "stallConsecutiveNonKeep": 9,
        "stallConsecutiveNoConsensus": 4,
    }
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    policy = load_autoresearch_policy(config_path)

    assert policy.campaign_governance == CampaignGovernancePolicy(9, 4)


@pytest.mark.parametrize(
    "value",
    [
        {"stallConsecutiveNonKeep": 8},
        {"stallConsecutiveNonKeep": 8, "stallConsecutiveNoConsensus": True},
        {"stallConsecutiveNonKeep": 0, "stallConsecutiveNoConsensus": 3},
        {"stallConsecutiveNonKeep": 8, "stallConsecutiveNoConsensus": 101},
    ],
)
def test_campaign_governance_config_rejects_non_exact_or_invalid_values(
    tmp_path: Path,
    value: dict[str, object],
) -> None:
    raw = cast(dict[str, object], json.loads(DEFAULT_OPENCLAW_CONFIG_PATH.read_text()))
    defaults = cast(dict[str, object], cast(dict[str, object], raw["agents"])["defaults"])
    defaults["autoresearchCampaignGovernance"] = value
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AutoresearchConfigError):
        load_autoresearch_policy(config_path)


def test_stall_reason_checks_non_keep_before_no_consensus(policy: AutoresearchPolicy) -> None:
    assert _campaign_stall_reason(CampaignCounters(8, 3, 8), policy) == (
        "campaign stalled: 8 consecutive non-KEEP iterations (threshold 8)"
    )
    assert _campaign_stall_reason(CampaignCounters(3, 3, 3), policy) == (
        "campaign stalled: 3 consecutive NO_CONSENSUS iterations (threshold 3)"
    )


def test_eight_discard_decisions_set_one_review_record(policy: AutoresearchPolicy) -> None:
    entries = tuple(_registry_entry(iteration, FinalDecision.DISCARD) for iteration in range(1, 8))
    state = _decision_state(policy, entries)
    # The decision Sharpe is derived from the verification artifact, so a DISCARD
    # fixture needs discard-worthy out-of-sample evidence to stay coherent.
    state = replace(
        state,
        verification_history=(
            *state.verification_history[:-1],
            replace(state.verification_history[-1], oos_sharpe_net=-0.2),
        ),
    )
    decision = replace(
        _final_decision_with(
            decision=FinalDecision.DISCARD,
            metric_value=0.0,
            reviewer_verdict=_final_decision().reviewer_verdict,
        ),
        experiment_id="iteration-8",
    )

    stalled = advance_state(state, decision, policy)

    assert stalled.campaign_review_required is True
    assert stalled.campaign_review_reason == (
        "campaign stalled: 8 consecutive non-KEEP iterations (threshold 8)"
    )
    assert stalled.campaign_counters == CampaignCounters(8, 0, 8)
    assert len(stalled.campaign_review_history) == 1
    assert stalled.campaign_review_history[-1].counters == stalled.campaign_counters


def test_infrastructure_entries_are_neutral_and_acknowledgement_preserves_last_keep_distance(
    policy: AutoresearchPolicy,
) -> None:
    entries = (
        _registry_entry(1, FinalDecision.KEEP),
        _registry_entry(2, FinalDecision.DISCARD),
        _registry_entry(3, FinalDecision.INFRA_BLOCKED),
        _registry_entry(4, FinalDecision.DISCARD),
        _registry_entry(5, FinalDecision.INFRA_REPAIRED),
        _registry_entry(6, FinalDecision.DISCARD),
    )
    counters = derive_campaign_counters(entries, acknowledged_through_iteration=0)
    pending = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=6,
        final_decision=_final_decision(),
        hypothesis_registry=entries,
        campaign_counters=counters,
        campaign_review_required=True,
        campaign_review_reason="campaign stalled: 3 consecutive non-KEEP iterations (threshold 3)",
        campaign_review_history=(
            CampaignReviewRecord(
                triggered_iteration=6,
                reason="campaign stalled: 3 consecutive non-KEEP iterations (threshold 3)",
                counters=counters,
                acknowledgement=None,
                acknowledged_iteration=None,
            ),
        ),
    )

    resumed = acknowledge_campaign_review(
        pending,
        "Reviewed the three failed outcomes and approved a fresh hypothesis pass.",
    )

    assert resumed.campaign_counters == CampaignCounters(0, 0, 3)
    assert resumed.campaign_review_required is False
    assert resumed.campaign_review_reason is None
    assert resumed.campaign_review_history[-1].acknowledged_iteration == 6


def test_campaign_review_acknowledged_state_round_trips_and_digest_binds_history() -> None:
    entries = (_registry_entry(1, FinalDecision.DISCARD),)
    counters = derive_campaign_counters(entries, acknowledged_through_iteration=0)
    pending = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=1,
        final_decision=_final_decision(),
        hypothesis_registry=entries,
        campaign_counters=counters,
        campaign_review_required=True,
        campaign_review_reason="campaign stalled: 1 consecutive non-KEEP iterations (threshold 1)",
        campaign_review_history=(
            CampaignReviewRecord(
                triggered_iteration=1,
                reason="campaign stalled: 1 consecutive non-KEEP iterations (threshold 1)",
                counters=counters,
                acknowledgement=None,
                acknowledged_iteration=None,
            ),
        ),
    )
    acknowledged = acknowledge_campaign_review(
        pending,
        "Reviewed the failed outcome and approved a fresh hypothesis pass.",
    )

    assert AutoresearchState.from_dict(acknowledged.to_dict()) == acknowledged
    assert build_authoritative_state_reference(pending).state_sha256 != (
        build_authoritative_state_reference(acknowledged).state_sha256
    )


@pytest.mark.parametrize(
    "state, acknowledgement, match",
    [
        (
            AutoresearchState(),
            "Reviewed the failed outcome and approved a fresh hypothesis pass.",
            "no pending",
        ),
        (
            AutoresearchState(
                campaign_review_required=True,
                campaign_review_reason="pending",
            ),
            "Reviewed the failed outcome and approved a fresh hypothesis pass.",
            "repeat phase",
        ),
        (
            AutoresearchState(
                phase=Phase.REPEAT,
                final_decision=_final_decision(),
                campaign_review_required=True,
                campaign_review_reason="pending",
            ),
            "   ",
            "between 32 and 1024",
        ),
    ],
)
def test_campaign_review_acknowledgement_refusals(
    state: AutoresearchState,
    acknowledgement: str,
    match: str,
) -> None:
    with pytest.raises(AutoresearchValidationError, match=match):
        acknowledge_campaign_review(state, acknowledgement)


def test_campaign_review_is_advisory_for_next_action_and_lifecycle(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
    completed_memory_written_state: AutoresearchState,
) -> None:
    counters = completed_memory_written_state.campaign_counters
    pending = replace(
        completed_memory_written_state,
        campaign_review_required=True,
        campaign_review_reason="campaign stalled: operator review required",
        campaign_review_history=(
            CampaignReviewRecord(
                triggered_iteration=1,
                reason="campaign stalled: operator review required",
                counters=counters,
                acknowledgement=None,
                acknowledged_iteration=None,
            ),
        ),
    )

    action = next_action(pending, policy, receipts, platform_readiness)
    assert action.phase is Phase.REPEAT
    next_iteration = start_next_iteration(pending, readiness=platform_readiness)
    assert next_iteration.campaign_review_required is True


def test_supervisor_campaign_review_is_advisory_and_warning_is_latched(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    completed_memory_written_state: AutoresearchState,
) -> None:
    pending = replace(
        completed_memory_written_state,
        campaign_review_required=True,
        campaign_review_reason="campaign stalled: operator review required",
    )
    supervisor = AutoresearchSupervisor(SupervisorConfig())
    monkeypatch.setattr(supervisor, "_load_state", lambda: pending)
    monkeypatch.setattr(supervisor, "_validate_dispatchable_state", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_active_target_repo_writer_processes", lambda *_args: ())
    monkeypatch.setattr(
        supervisor,
        "_prepare_controller_lifecycle",
        lambda *_args, **_kwargs: SupervisorResult(
            SupervisorOutcome.NO_ACTION, "controller_checked"
        ),
    )
    wake_calls: list[object] = []
    monkeypatch.setattr(supervisor._rpc, "wake", lambda *args, **kwargs: wake_calls.append(args))

    with caplog.at_level(logging.WARNING, logger="gateway.autoresearch_supervisor"):
        result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "controller_checked"
    assert wake_calls == []
    assert "autoresearch-acknowledge-campaign-review" in caplog.text

    supervisor.run_once()
    assert caplog.text.count('"event": "supervisor.campaign_review_advisory"') == 1


def test_campaign_review_cli_persists_acknowledgement_and_refuses_repeat(
    tmp_path: Path,
    completed_memory_written_state: AutoresearchState,
) -> None:
    counters = completed_memory_written_state.campaign_counters
    pending = replace(
        completed_memory_written_state,
        campaign_review_required=True,
        campaign_review_reason="campaign stalled: operator review required",
        campaign_review_history=(
            CampaignReviewRecord(
                triggered_iteration=1,
                reason="campaign stalled: operator review required",
                counters=counters,
                acknowledgement=None,
                acknowledged_iteration=None,
            ),
        ),
    )
    state_path = tmp_path / "state.json"
    output_path = tmp_path / "acknowledged.json"
    state_path.write_text(json.dumps(pending.to_dict()), encoding="utf-8")
    acknowledgement = "Reviewed the stalled campaign and approved a fresh hypothesis pass."
    runner = CliRunner()

    result = runner.invoke(
        gateway_cli.app,
        [
            "autoresearch-acknowledge-campaign-review",
            str(state_path),
            "--acknowledgement",
            acknowledgement,
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    acknowledged = AutoresearchState.from_dict(json.loads(output_path.read_text()))
    assert acknowledged.campaign_review_required is False
    assert acknowledged.campaign_review_history[-1].acknowledgement == acknowledgement

    refusal = runner.invoke(
        gateway_cli.app,
        [
            "autoresearch-acknowledge-campaign-review",
            str(output_path),
            "--acknowledgement",
            acknowledgement,
            "--output",
            str(tmp_path / "refusal.json"),
        ],
    )

    assert refusal.exit_code == 1
    assert "no pending campaign review" in refusal.output
