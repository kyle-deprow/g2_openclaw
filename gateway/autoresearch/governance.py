"""Deterministic campaign-governance value objects and derivations."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact as ConsensusResultArtifact,
)
from gateway.autoresearch.artifacts import (
    DebateResultArtifact as DebateResultArtifact,
)
from gateway.autoresearch.constants import (
    _OPERATOR_PRECONDITION_BRIEF_MARKERS as _OPERATOR_PRECONDITION_BRIEF_MARKERS,
)
from gateway.autoresearch.constants import (
    _OPERATOR_PRECONDITION_MARKERS as _OPERATOR_PRECONDITION_MARKERS,
)
from gateway.autoresearch.constants import (
    HYPOTHESIS_FINGERPRINT_DIGEST_DOMAIN as HYPOTHESIS_FINGERPRINT_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    HYPOTHESIS_FINGERPRINT_VERSION as HYPOTHESIS_FINGERPRINT_VERSION,
)
from gateway.autoresearch.constants import (
    HYPOTHESIS_REGISTRY_REASON_MAX_CHARS as HYPOTHESIS_REGISTRY_REASON_MAX_CHARS,
)
from gateway.autoresearch.enums import (
    ConsensusStatus as ConsensusStatus,
)
from gateway.autoresearch.enums import (
    FinalDecision as FinalDecision,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _normalise_identifier as _normalise_identifier,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_float as _require_float,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _require_string_list as _require_string_list,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)


def _optional_string(data: Mapping[str, object], field_name: str) -> str | None:
    value = data[field_name]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string or null")
    return value.strip()


def _optional_sha256(data: Mapping[str, object], field_name: str) -> str | None:
    value = data[field_name]
    if value is None:
        return None
    return _require_sha256(data, field_name)


def _optional_float(data: Mapping[str, object], field_name: str) -> float | None:
    value = data[field_name]
    if value is None:
        return None
    return _require_float(data, field_name)


def _optional_int(data: Mapping[str, object], field_name: str) -> int | None:
    value = data[field_name]
    if value is None:
        return None
    return _require_int(data, field_name)


def _is_operator_precondition_consensus(consensus: ConsensusResultArtifact) -> bool:
    id_text = " ".join(
        value.lower()
        for value in (consensus.winner_theory_id, consensus.winner_theory_family)
        if value
    )
    if any(marker in id_text for marker in _OPERATOR_PRECONDITION_MARKERS):
        return True
    brief = (consensus.implementation_brief or "").lower()
    return all(marker in brief for marker in _OPERATOR_PRECONDITION_BRIEF_MARKERS)


def theory_family_fingerprint(
    consensus: ConsensusResultArtifact,
    mode: ResearchMode,
) -> str | None:
    """Return the stable family/universe fingerprint for an accepted consensus."""
    if consensus.status is ConsensusStatus.NO_CONSENSUS or _is_operator_precondition_consensus(
        consensus
    ):
        return None
    if consensus.winner_theory_family is None:
        raise AutoresearchValidationError("majority consensus requires a theory family fingerprint")
    if consensus.universe_plan is None:
        raise AutoresearchValidationError("majority consensus requires a universe plan fingerprint")
    consensus.universe_plan.validate()
    dates = consensus.universe_plan.selection_dates
    payload = {
        "version": HYPOTHESIS_FINGERPRINT_VERSION,
        "research_mode": mode.value,
        "family": _normalise_identifier(consensus.winner_theory_family),
        "profile_id": consensus.universe_plan.profile_id,
        "profile_digest": consensus.universe_plan.profile_digest,
        "selection_span": [dates[0], dates[-1], len(dates)],
        "max_members_per_date": consensus.universe_plan.max_members_per_date,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        "\n".join((HYPOTHESIS_FINGERPRINT_DIGEST_DOMAIN, canonical)).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HypothesisRegistryEntry:
    iteration: int
    research_mode: ResearchMode
    consensus_status: ConsensusStatus
    decision: FinalDecision
    family: str | None
    contested_families: tuple[str, ...]
    fingerprint: str | None
    metric_value: float | None
    reason: str
    novelty_delta_sha256: str | None

    @classmethod
    def from_dict(cls, raw: object) -> HypothesisRegistryEntry:
        data = _ensure_mapping(raw, label="hypothesis_registry_entry")
        _require_exact_keys(
            data,
            label="hypothesis_registry_entry",
            expected=(
                "iteration",
                "research_mode",
                "consensus_status",
                "decision",
                "family",
                "contested_families",
                "fingerprint",
                "metric_value",
                "reason",
                "novelty_delta_sha256",
            ),
        )
        family_raw = data["family"]
        if family_raw is not None and not isinstance(family_raw, str):
            raise AutoresearchValidationError("family must be a string or null")
        entry = cls(
            iteration=_require_int(data, "iteration"),
            research_mode=ResearchMode(_require_str(data, "research_mode")),
            consensus_status=ConsensusStatus(_require_str(data, "consensus_status")),
            decision=FinalDecision(_require_str(data, "decision")),
            family=family_raw.strip() if isinstance(family_raw, str) else None,
            contested_families=_require_string_list(data, "contested_families"),
            fingerprint=_optional_sha256(data, "fingerprint"),
            metric_value=_optional_float(data, "metric_value"),
            reason=_require_str(data, "reason"),
            novelty_delta_sha256=_optional_sha256(data, "novelty_delta_sha256"),
        )
        entry.validate()
        return entry

    def validate(self) -> None:
        if self.iteration < 1:
            raise AutoresearchValidationError("hypothesis registry iteration must be >= 1")
        no_family_status = self.consensus_status in {
            ConsensusStatus.NO_CONSENSUS,
            ConsensusStatus.NONE,
        }
        if (self.family is None) != no_family_status:
            raise AutoresearchValidationError(
                "hypothesis registry family must be null exactly for NO_CONSENSUS or NONE"
            )
        if self.family is not None and self.family != _normalise_identifier(self.family):
            raise AutoresearchValidationError(
                "hypothesis registry family must be normalized lowercase kebab-case"
            )
        if len(self.contested_families) > 2:
            raise AutoresearchValidationError(
                "hypothesis registry contested_families allows at most 2 families"
            )
        if tuple(sorted(set(self.contested_families))) != self.contested_families:
            raise AutoresearchValidationError(
                "hypothesis registry contested_families must be sorted and unique"
            )
        for family in self.contested_families:
            if family != _normalise_identifier(family):
                raise AutoresearchValidationError(
                    "hypothesis registry contested_families must be normalized"
                )
        contested_allowed = (
            self.consensus_status is ConsensusStatus.NO_CONSENSUS
            and self.decision is FinalDecision.NO_CONSENSUS
        )
        if self.contested_families and not contested_allowed:
            raise AutoresearchValidationError(
                "hypothesis registry contested_families requires NO_CONSENSUS status and decision"
            )
        if (
            self.consensus_status
            in {
                ConsensusStatus.NO_CONSENSUS,
                ConsensusStatus.NONE,
            }
            and self.fingerprint is not None
        ):
            raise AutoresearchValidationError(
                "hypothesis registry NO_CONSENSUS or NONE entries cannot have a fingerprint"
            )
        if self.fingerprint is not None:
            _validate_sha256(self.fingerprint, label="hypothesis registry fingerprint")
        if self.metric_value is not None and not math.isfinite(self.metric_value):
            raise AutoresearchValidationError("hypothesis registry metric_value must be finite")
        if len(self.reason) > HYPOTHESIS_REGISTRY_REASON_MAX_CHARS:
            raise AutoresearchValidationError(
                "hypothesis registry reason exceeds the 160 character limit"
            )
        if self.reason != self.reason.strip():
            raise AutoresearchValidationError("hypothesis registry reason must be trimmed")
        if self.novelty_delta_sha256 is not None:
            _validate_sha256(
                self.novelty_delta_sha256,
                label="hypothesis registry novelty_delta_sha256",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "research_mode": self.research_mode.value,
            "consensus_status": self.consensus_status.value,
            "decision": self.decision.value,
            "family": self.family,
            "contested_families": list(self.contested_families),
            "fingerprint": self.fingerprint,
            "metric_value": self.metric_value,
            "reason": self.reason,
            "novelty_delta_sha256": self.novelty_delta_sha256,
        }


@dataclass(frozen=True, slots=True)
class CampaignCounters:
    consecutive_non_keep: int = 0
    consecutive_no_consensus: int = 0
    iterations_since_last_keep: int = 0

    @classmethod
    def zero(cls) -> CampaignCounters:
        return cls()

    @classmethod
    def from_dict(cls, raw: object) -> CampaignCounters:
        data = _ensure_mapping(raw, label="campaign_counters")
        _require_exact_keys(
            data,
            label="campaign_counters",
            expected=(
                "consecutive_non_keep",
                "consecutive_no_consensus",
                "iterations_since_last_keep",
            ),
        )
        counters = cls(
            consecutive_non_keep=_require_int(data, "consecutive_non_keep"),
            consecutive_no_consensus=_require_int(data, "consecutive_no_consensus"),
            iterations_since_last_keep=_require_int(data, "iterations_since_last_keep"),
        )
        counters.validate()
        return counters

    def validate(self) -> None:
        if any(
            value < 0
            for value in (
                self.consecutive_non_keep,
                self.consecutive_no_consensus,
                self.iterations_since_last_keep,
            )
        ):
            raise AutoresearchValidationError("campaign counters must be non-negative")

    def to_dict(self) -> dict[str, int]:
        return {
            "consecutive_non_keep": self.consecutive_non_keep,
            "consecutive_no_consensus": self.consecutive_no_consensus,
            "iterations_since_last_keep": self.iterations_since_last_keep,
        }


@dataclass(frozen=True, slots=True)
class CampaignReviewRecord:
    triggered_iteration: int
    reason: str
    counters: CampaignCounters
    acknowledgement: str | None
    acknowledged_iteration: int | None

    @classmethod
    def from_dict(cls, raw: object) -> CampaignReviewRecord:
        data = _ensure_mapping(raw, label="campaign_review_record")
        _require_exact_keys(
            data,
            label="campaign_review_record",
            expected=(
                "triggered_iteration",
                "reason",
                "counters",
                "acknowledgement",
                "acknowledged_iteration",
            ),
        )
        record = cls(
            triggered_iteration=_require_int(data, "triggered_iteration"),
            reason=_require_str(data, "reason"),
            counters=CampaignCounters.from_dict(data["counters"]),
            acknowledgement=_optional_string(data, "acknowledgement"),
            acknowledged_iteration=_optional_int(data, "acknowledged_iteration"),
        )
        record.validate()
        return record

    def validate(self) -> None:
        if self.triggered_iteration < 1:
            raise AutoresearchValidationError("campaign review triggered_iteration must be >= 1")
        self.counters.validate()
        if (self.acknowledgement is None) != (self.acknowledged_iteration is None):
            raise AutoresearchValidationError(
                "campaign review acknowledgement and acknowledged_iteration must be paired"
            )
        if self.acknowledged_iteration is not None and self.acknowledged_iteration < 1:
            raise AutoresearchValidationError("campaign review acknowledged_iteration must be >= 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "triggered_iteration": self.triggered_iteration,
            "reason": self.reason,
            "counters": self.counters.to_dict(),
            "acknowledgement": self.acknowledgement,
            "acknowledged_iteration": self.acknowledged_iteration,
        }


def derive_campaign_counters(
    registry: Sequence[HypothesisRegistryEntry],
    *,
    acknowledged_through_iteration: int,
) -> CampaignCounters:
    """Derive counters from append-only entries after the acknowledgement baseline."""
    if acknowledged_through_iteration < 0:
        raise AutoresearchValidationError("acknowledged_through_iteration must be >= 0")
    consecutive_non_keep = 0
    consecutive_no_consensus = 0
    last_keep_index = -1
    eligible_entries: list[HypothesisRegistryEntry] = []
    for entry in registry:
        entry.validate()
        if entry.decision in (FinalDecision.INFRA_BLOCKED, FinalDecision.INFRA_REPAIRED):
            continue
        eligible_entries.append(entry)
        index = len(eligible_entries) - 1
        if entry.iteration <= acknowledged_through_iteration:
            if entry.decision in {
                FinalDecision.KEEP,
                FinalDecision.SIGNIFICANT_KEEP,
                FinalDecision.STRONG_KEEP,
            }:
                last_keep_index = index
            continue
        if entry.decision in {
            FinalDecision.KEEP,
            FinalDecision.SIGNIFICANT_KEEP,
            FinalDecision.STRONG_KEEP,
        }:
            consecutive_non_keep = 0
            consecutive_no_consensus = 0
            last_keep_index = index
            continue
        consecutive_non_keep += 1
        if entry.decision is FinalDecision.NO_CONSENSUS:
            consecutive_no_consensus += 1
        else:
            consecutive_no_consensus = 0
    iterations_since_last_keep = len(eligible_entries) - last_keep_index - 1
    return CampaignCounters(
        consecutive_non_keep=consecutive_non_keep,
        consecutive_no_consensus=consecutive_no_consensus,
        iterations_since_last_keep=iterations_since_last_keep,
    )


def contested_families(debate: DebateResultArtifact) -> tuple[str, ...]:
    """Extract normalized vote families with at least two votes from a debate round."""
    counts = Counter(
        _normalise_identifier(submission.vote_family) for submission in debate.submissions
    )
    families = tuple(sorted(family for family, count in counts.items() if count >= 2))
    if len(families) > 2:
        raise AutoresearchValidationError("contested_families allows at most 2 families")
    return families


def family_trial_counts(registry: Sequence[HypothesisRegistryEntry]) -> dict[str, int]:
    """Count registry trials by selected normalized theory family."""
    counts: Counter[str] = Counter()
    for entry in registry:
        entry.validate()
        if entry.family is not None:
            counts[entry.family] += 1
    return dict(sorted(counts.items()))
