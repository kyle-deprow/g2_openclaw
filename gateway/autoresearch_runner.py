"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

DEFAULT_OPENCLAW_CONFIG_PATH = Path("gateway/openclaw_config/openclaw.json")
DEFAULT_QUANTIPY_ROOT = Path("/home/dev/repos/quantipy")
_T = TypeVar("_T")


class AutoresearchError(ValueError):
    """Base error for deterministic autoresearch control-plane failures."""


class AutoresearchConfigError(AutoresearchError):
    """Raised when the OpenClaw stage-agent config deviates from policy."""


class AutoresearchReceiptError(AutoresearchError):
    """Raised when a required source receipt cannot be generated."""


class AutoresearchValidationError(AutoresearchError):
    """Raised when an artifact or state is invalid."""


class Phase(StrEnum):
    SETUP_CONTEXT = "setup_context"
    DEBATE = "debate"
    CONSENSUS = "consensus"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    REVIEW = "review"
    FIX_TEST = "fix_test"
    DECISION_LOG = "decision_log"
    REPEAT = "repeat"


class ArtifactType(StrEnum):
    SETUP = "setup_context"
    CONTEXT_PACKET = "context_packet"
    DEBATE_RESULT = "debate_result"
    CONSENSUS_RESULT = "consensus_result"
    IMPLEMENTATION_RESULT = "implementation_result"
    VERIFICATION_RESULT = "verification_result"
    REVIEW_RESULT = "review_result"
    FIX_RESULT = "fix_result"
    FINAL_DECISION = "final_decision"
    MEMORY_WRITE = "memory_write"
    NEXT_ITERATION = "next_iteration"


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class ConsensusStatus(StrEnum):
    MAJORITY = "MAJORITY"
    NO_CONSENSUS = "NO_CONSENSUS"


class VerificationStatus(StrEnum):
    PASS = "PASS"
    BUG_SIGNAL = "BUG_SIGNAL"
    TEST_FAILURE = "TEST_FAILURE"


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL PASS"
    FAIL = "FAIL"


class FinalDecision(StrEnum):
    KEEP = "KEEP"
    SIGNIFICANT_KEEP = "SIGNIFICANT KEEP"
    STRONG_KEEP = "STRONG KEEP"
    DISCARD = "DISCARD"
    CRASH = "CRASH"
    NO_CONSENSUS = "NO_CONSENSUS"


class FixTriggerPhase(StrEnum):
    VERIFICATION = "verification"
    REVIEW = "review"


KEEP_DECISIONS = frozenset(
    {FinalDecision.KEEP, FinalDecision.SIGNIFICANT_KEEP, FinalDecision.STRONG_KEEP}
)
MEMPALACE_MUTATION_TOOLS = (
    "mempalace_add_drawer",
    "mempalace_check_duplicate",
    "mempalace_checkpoint",
    "mempalace_create_tunnel",
    "mempalace_delete_by_source",
    "mempalace_delete_drawer",
    "mempalace_delete_hallway",
    "mempalace_delete_tunnel",
    "mempalace_diary_write",
    "mempalace_hook_settings",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_mine",
    "mempalace_reconnect",
    "mempalace_sync",
    "mempalace_update_drawer",
)


def _ensure_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise AutoresearchValidationError(f"{label} must be an object")
    return raw


def _require_str(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_bool(raw: Mapping[str, object], field_name: str) -> bool:
    value = raw.get(field_name)
    if not isinstance(value, bool):
        raise AutoresearchValidationError(f"{field_name} must be a boolean")
    return value


def _require_int(raw: Mapping[str, object], field_name: str) -> int:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoresearchValidationError(f"{field_name} must be an integer")
    return value


def _require_float(raw: Mapping[str, object], field_name: str) -> float:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{field_name} must be numeric")
    return float(value)


def _require_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    value = raw.get(field_name)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise AutoresearchValidationError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AutoresearchValidationError(f"{field_name} must be a list of strings")
        items.append(item)
    return tuple(items)


def _optional_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    if field_name not in raw:
        return ()
    return _require_string_list(raw, field_name)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    receipt_id: str
    path: Path
    sha256: str
    content: str

    @property
    def label(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "path": str(self.path),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class StageAgentPolicy:
    agent_id: str
    model: str
    reasoning: str
    skills: tuple[str, ...]

    def to_summary(self) -> str:
        skill_text = ", ".join(self.skills)
        return f"{self.agent_id}: {self.model} / {self.reasoning} / skills=[{skill_text}]"


@dataclass(frozen=True, slots=True)
class AutoresearchPolicy:
    main: StageAgentPolicy
    context_curator: StageAgentPolicy
    debate_agents: tuple[StageAgentPolicy, ...]
    consensus: StageAgentPolicy
    implementer: StageAgentPolicy
    reviewer: StageAgentPolicy
    fixer: StageAgentPolicy

    @property
    def debate_agent_ids(self) -> tuple[str, ...]:
        return tuple(agent.agent_id for agent in self.debate_agents)

    @property
    def all_stage_agent_ids(self) -> tuple[str, ...]:
        return (
            self.context_curator.agent_id,
            *self.debate_agent_ids,
            self.consensus.agent_id,
            self.implementer.agent_id,
            self.reviewer.agent_id,
            self.fixer.agent_id,
        )

    def model_policy_summary(self) -> str:
        lines = [
            self.main.to_summary(),
            self.context_curator.to_summary(),
            *(agent.to_summary() for agent in self.debate_agents),
            self.consensus.to_summary(),
            self.implementer.to_summary(),
            self.reviewer.to_summary(),
            self.fixer.to_summary(),
        ]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ReceiptCatalog:
    receipts: dict[str, SourceReceipt]

    def require(self, receipt_ids: Sequence[str]) -> tuple[SourceReceipt, ...]:
        ordered: list[SourceReceipt] = []
        for receipt_id in receipt_ids:
            try:
                ordered.append(self.receipts[receipt_id])
            except KeyError as exc:
                raise AutoresearchReceiptError(f"missing receipt id: {receipt_id}") from exc
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class SetupContextArtifact:
    goal: str
    metric_name: str
    metric_direction: MetricDirection
    target_repo: str
    writable_scope: str
    baseline_summary: str
    hard_constraints: tuple[str, ...]
    data_sources: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> SetupContextArtifact:
        data = _ensure_mapping(raw, label="setup_context")
        return cls(
            goal=_require_str(data, "goal"),
            metric_name=_require_str(data, "metric_name"),
            metric_direction=MetricDirection(_require_str(data, "metric_direction")),
            target_repo=_require_str(data, "target_repo"),
            writable_scope=_require_str(data, "writable_scope"),
            baseline_summary=_require_str(data, "baseline_summary"),
            hard_constraints=_optional_string_list(data, "hard_constraints"),
            data_sources=_optional_string_list(data, "data_sources"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "metric_name": self.metric_name,
            "metric_direction": self.metric_direction.value,
            "target_repo": self.target_repo,
            "writable_scope": self.writable_scope,
            "baseline_summary": self.baseline_summary,
            "hard_constraints": list(self.hard_constraints),
            "data_sources": list(self.data_sources),
        }


@dataclass(frozen=True, slots=True)
class ContextPacketArtifact:
    baseline_metric: str
    current_best_metric: str
    recent_experiment_outcomes: tuple[str, ...]
    prior_findings: tuple[str, ...]
    open_proposals: tuple[str, ...]
    hard_constraints: tuple[str, ...]
    available_data_sources: tuple[str, ...]
    loaded_quantipy_sources: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> ContextPacketArtifact:
        data = _ensure_mapping(raw, label="context_packet")
        return cls(
            baseline_metric=_require_str(data, "baseline_metric"),
            current_best_metric=_require_str(data, "current_best_metric"),
            recent_experiment_outcomes=_require_string_list(data, "recent_experiment_outcomes"),
            prior_findings=_require_string_list(data, "prior_findings"),
            open_proposals=_require_string_list(data, "open_proposals"),
            hard_constraints=_require_string_list(data, "hard_constraints"),
            available_data_sources=_require_string_list(data, "available_data_sources"),
            loaded_quantipy_sources=_require_string_list(data, "loaded_quantipy_sources"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_metric": self.baseline_metric,
            "current_best_metric": self.current_best_metric,
            "recent_experiment_outcomes": list(self.recent_experiment_outcomes),
            "prior_findings": list(self.prior_findings),
            "open_proposals": list(self.open_proposals),
            "hard_constraints": list(self.hard_constraints),
            "available_data_sources": list(self.available_data_sources),
            "loaded_quantipy_sources": list(self.loaded_quantipy_sources),
        }


@dataclass(frozen=True, slots=True)
class DebateSubmission:
    agent_id: str
    theory_id: str
    theory_family: str
    vote_family: str
    hypothesis: str
    universe: str
    traded_tickers: tuple[str, ...]
    feature_pipeline: str
    model_plan: str
    walk_forward_plan: str
    transaction_cost_model: str
    data_coverage_plan: str
    rejection_criteria: str
    objections: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> DebateSubmission:
        data = _ensure_mapping(raw, label="debate_submission")
        return cls(
            agent_id=_require_str(data, "agent_id"),
            theory_id=_require_str(data, "theory_id"),
            theory_family=_require_str(data, "theory_family"),
            vote_family=_require_str(data, "vote_family"),
            hypothesis=_require_str(data, "hypothesis"),
            universe=_require_str(data, "universe"),
            traded_tickers=_require_string_list(data, "traded_tickers"),
            feature_pipeline=_require_str(data, "feature_pipeline"),
            model_plan=_require_str(data, "model_plan"),
            walk_forward_plan=_require_str(data, "walk_forward_plan"),
            transaction_cost_model=_require_str(data, "transaction_cost_model"),
            data_coverage_plan=_require_str(data, "data_coverage_plan"),
            rejection_criteria=_require_str(data, "rejection_criteria"),
            objections=_require_string_list(data, "objections"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "theory_id": self.theory_id,
            "theory_family": self.theory_family,
            "vote_family": self.vote_family,
            "hypothesis": self.hypothesis,
            "universe": self.universe,
            "traded_tickers": list(self.traded_tickers),
            "feature_pipeline": self.feature_pipeline,
            "model_plan": self.model_plan,
            "walk_forward_plan": self.walk_forward_plan,
            "transaction_cost_model": self.transaction_cost_model,
            "data_coverage_plan": self.data_coverage_plan,
            "rejection_criteria": self.rejection_criteria,
            "objections": list(self.objections),
        }


@dataclass(frozen=True, slots=True)
class DebateResultArtifact:
    round_number: int
    submissions: tuple[DebateSubmission, ...]

    @classmethod
    def from_dict(cls, raw: object) -> DebateResultArtifact:
        data = _ensure_mapping(raw, label="debate_result")
        submissions_raw = data.get("submissions")
        if not isinstance(submissions_raw, Sequence) or isinstance(submissions_raw, str | bytes):
            raise AutoresearchValidationError("submissions must be a list")
        submissions = tuple(DebateSubmission.from_dict(item) for item in submissions_raw)
        if len(submissions) != 5:
            raise AutoresearchValidationError("debate_result must contain exactly 5 submissions")
        return cls(round_number=_require_int(data, "round_number"), submissions=submissions)

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "submissions": [submission.to_dict() for submission in self.submissions],
        }


@dataclass(frozen=True, slots=True)
class ConsensusResultArtifact:
    round_number: int
    status: ConsensusStatus
    winner_theory_id: str | None
    winner_theory_family: str | None
    majority_count: int
    majority_agent_ids: tuple[str, ...]
    dissenting_positions: tuple[str, ...]
    novelty_score: float
    theory_score: float
    implementation_risk_score: float
    data_adequacy_score: float
    overfit_risk_score: float
    expected_net_sharpe: float
    rejection_reasons: tuple[str, ...]
    implementation_brief: str | None
    dissent_summary: str

    @classmethod
    def from_dict(cls, raw: object) -> ConsensusResultArtifact:
        data = _ensure_mapping(raw, label="consensus_result")
        winner_theory_id = data.get("winner_theory_id")
        winner_theory_family = data.get("winner_theory_family")
        implementation_brief = data.get("implementation_brief")
        if winner_theory_id is not None and not isinstance(winner_theory_id, str):
            raise AutoresearchValidationError("winner_theory_id must be a string or null")
        if winner_theory_family is not None and not isinstance(winner_theory_family, str):
            raise AutoresearchValidationError("winner_theory_family must be a string or null")
        if implementation_brief is not None and not isinstance(implementation_brief, str):
            raise AutoresearchValidationError("implementation_brief must be a string or null")
        artifact = cls(
            round_number=_require_int(data, "round_number"),
            status=ConsensusStatus(_require_str(data, "status")),
            winner_theory_id=winner_theory_id,
            winner_theory_family=winner_theory_family,
            majority_count=_require_int(data, "majority_count"),
            majority_agent_ids=_require_string_list(data, "majority_agent_ids"),
            dissenting_positions=_require_string_list(data, "dissenting_positions"),
            novelty_score=_require_float(data, "novelty_score"),
            theory_score=_require_float(data, "theory_score"),
            implementation_risk_score=_require_float(data, "implementation_risk_score"),
            data_adequacy_score=_require_float(data, "data_adequacy_score"),
            overfit_risk_score=_require_float(data, "overfit_risk_score"),
            expected_net_sharpe=_require_float(data, "expected_net_sharpe"),
            rejection_reasons=_require_string_list(data, "rejection_reasons"),
            implementation_brief=implementation_brief.strip()
            if isinstance(implementation_brief, str)
            else None,
            dissent_summary=_require_str(data, "dissent_summary"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if self.status is ConsensusStatus.MAJORITY:
            if self.majority_count < 3:
                raise AutoresearchValidationError("majority consensus requires majority_count >= 3")
            if not self.winner_theory_id or not self.winner_theory_family:
                raise AutoresearchValidationError("majority consensus requires a winner")
            if not self.implementation_brief:
                raise AutoresearchValidationError(
                    "majority consensus requires an implementation_brief"
                )
        else:
            if self.majority_count >= 3:
                raise AutoresearchValidationError("NO_CONSENSUS cannot report a 3-of-5 majority")
            if self.winner_theory_id or self.winner_theory_family or self.implementation_brief:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS must not include winner or implementation brief"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "status": self.status.value,
            "winner_theory_id": self.winner_theory_id,
            "winner_theory_family": self.winner_theory_family,
            "majority_count": self.majority_count,
            "majority_agent_ids": list(self.majority_agent_ids),
            "dissenting_positions": list(self.dissenting_positions),
            "novelty_score": self.novelty_score,
            "theory_score": self.theory_score,
            "implementation_risk_score": self.implementation_risk_score,
            "data_adequacy_score": self.data_adequacy_score,
            "overfit_risk_score": self.overfit_risk_score,
            "expected_net_sharpe": self.expected_net_sharpe,
            "rejection_reasons": list(self.rejection_reasons),
            "implementation_brief": self.implementation_brief,
            "dissent_summary": self.dissent_summary,
        }


@dataclass(frozen=True, slots=True)
class ImplementationResultArtifact:
    summary: str
    module_path: str
    notebook_path: str
    tests_added_or_updated: tuple[str, ...]
    commands_run: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> ImplementationResultArtifact:
        data = _ensure_mapping(raw, label="implementation_result")
        return cls(
            summary=_require_str(data, "summary"),
            module_path=_require_str(data, "module_path"),
            notebook_path=_require_str(data, "notebook_path"),
            tests_added_or_updated=_require_string_list(data, "tests_added_or_updated"),
            commands_run=_require_string_list(data, "commands_run"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "module_path": self.module_path,
            "notebook_path": self.notebook_path,
            "tests_added_or_updated": list(self.tests_added_or_updated),
            "commands_run": list(self.commands_run),
        }


@dataclass(frozen=True, slots=True)
class VerificationResultArtifact:
    status: VerificationStatus
    is_walk_forward_sharpe_net: float
    oos_sharpe_net: float
    max_drawdown_pct: float
    win_rate: float
    trade_count: int
    trades_per_day: float
    oos_trading_days: int
    feature_importances_summary: str
    null_test_summary: str
    bug_signals: tuple[str, ...]
    tests_passed: bool
    commands_run: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> VerificationResultArtifact:
        data = _ensure_mapping(raw, label="verification_result")
        artifact = cls(
            status=VerificationStatus(_require_str(data, "status")),
            is_walk_forward_sharpe_net=_require_float(data, "is_walk_forward_sharpe_net"),
            oos_sharpe_net=_require_float(data, "oos_sharpe_net"),
            max_drawdown_pct=_require_float(data, "max_drawdown_pct"),
            win_rate=_require_float(data, "win_rate"),
            trade_count=_require_int(data, "trade_count"),
            trades_per_day=_require_float(data, "trades_per_day"),
            oos_trading_days=_require_int(data, "oos_trading_days"),
            feature_importances_summary=_require_str(data, "feature_importances_summary"),
            null_test_summary=_require_str(data, "null_test_summary"),
            bug_signals=_require_string_list(data, "bug_signals"),
            tests_passed=_require_bool(data, "tests_passed"),
            commands_run=_require_string_list(data, "commands_run"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if self.status is VerificationStatus.PASS and (self.bug_signals or not self.tests_passed):
            raise AutoresearchValidationError(
                "PASS verification cannot include bug signals or failing tests"
            )
        if self.status is VerificationStatus.BUG_SIGNAL and not self.bug_signals:
            raise AutoresearchValidationError(
                "BUG_SIGNAL verification requires at least one bug signal"
            )
        if self.status is VerificationStatus.TEST_FAILURE and self.tests_passed:
            raise AutoresearchValidationError(
                "TEST_FAILURE verification cannot mark tests_passed=true"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "is_walk_forward_sharpe_net": self.is_walk_forward_sharpe_net,
            "oos_sharpe_net": self.oos_sharpe_net,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "trades_per_day": self.trades_per_day,
            "oos_trading_days": self.oos_trading_days,
            "feature_importances_summary": self.feature_importances_summary,
            "null_test_summary": self.null_test_summary,
            "bug_signals": list(self.bug_signals),
            "tests_passed": self.tests_passed,
            "commands_run": list(self.commands_run),
        }


@dataclass(frozen=True, slots=True)
class ReviewResultArtifact:
    reviewer_agent_id: str
    verdict: ReviewVerdict
    recommended_metric_name: str
    recommended_metric_value: float
    critical_issues: tuple[str, ...]
    noncritical_issues: tuple[str, ...]
    fix_requests: tuple[str, ...]
    summary: str

    @classmethod
    def from_dict(cls, raw: object) -> ReviewResultArtifact:
        data = _ensure_mapping(raw, label="review_result")
        artifact = cls(
            reviewer_agent_id=_require_str(data, "reviewer_agent_id"),
            verdict=ReviewVerdict(_require_str(data, "verdict")),
            recommended_metric_name=_require_str(data, "recommended_metric_name"),
            recommended_metric_value=_require_float(data, "recommended_metric_value"),
            critical_issues=_require_string_list(data, "critical_issues"),
            noncritical_issues=_require_string_list(data, "noncritical_issues"),
            fix_requests=_require_string_list(data, "fix_requests"),
            summary=_require_str(data, "summary"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        needs_fix = self.verdict is ReviewVerdict.FAIL or bool(self.critical_issues)
        if needs_fix and not self.fix_requests:
            raise AutoresearchValidationError(
                "review_result with critical issues or FAIL verdict requires fix_requests"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewer_agent_id": self.reviewer_agent_id,
            "verdict": self.verdict.value,
            "recommended_metric_name": self.recommended_metric_name,
            "recommended_metric_value": self.recommended_metric_value,
            "critical_issues": list(self.critical_issues),
            "noncritical_issues": list(self.noncritical_issues),
            "fix_requests": list(self.fix_requests),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class FixResultArtifact:
    trigger_phase: FixTriggerPhase
    summary: str
    fixes_applied: tuple[str, ...]
    tests_rerun: tuple[str, ...]
    remaining_issues: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> FixResultArtifact:
        data = _ensure_mapping(raw, label="fix_result")
        return cls(
            trigger_phase=FixTriggerPhase(_require_str(data, "trigger_phase")),
            summary=_require_str(data, "summary"),
            fixes_applied=_require_string_list(data, "fixes_applied"),
            tests_rerun=_require_string_list(data, "tests_rerun"),
            remaining_issues=_require_string_list(data, "remaining_issues"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger_phase": self.trigger_phase.value,
            "summary": self.summary,
            "fixes_applied": list(self.fixes_applied),
            "tests_rerun": list(self.tests_rerun),
            "remaining_issues": list(self.remaining_issues),
        }


@dataclass(frozen=True, slots=True)
class FinalDecisionArtifact:
    decision: FinalDecision
    recommended_metric_name: str
    recommended_metric_value: float | None
    reviewer_verdict: str | None
    rationale: str
    log_summary: str
    continue_loop: bool
    memory_write_required: bool

    @classmethod
    def from_dict(cls, raw: object) -> FinalDecisionArtifact:
        data = _ensure_mapping(raw, label="final_decision")
        metric_value = data.get("recommended_metric_value")
        if metric_value is not None and (
            isinstance(metric_value, bool) or not isinstance(metric_value, int | float)
        ):
            raise AutoresearchValidationError("recommended_metric_value must be numeric or null")
        reviewer_verdict = data.get("reviewer_verdict")
        if reviewer_verdict is not None and not isinstance(reviewer_verdict, str):
            raise AutoresearchValidationError("reviewer_verdict must be a string or null")
        return cls(
            decision=FinalDecision(_require_str(data, "decision")),
            recommended_metric_name=_require_str(data, "recommended_metric_name"),
            recommended_metric_value=float(metric_value) if metric_value is not None else None,
            reviewer_verdict=reviewer_verdict,
            rationale=_require_str(data, "rationale"),
            log_summary=_require_str(data, "log_summary"),
            continue_loop=_require_bool(data, "continue_loop"),
            memory_write_required=_require_bool(data, "memory_write_required"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "recommended_metric_name": self.recommended_metric_name,
            "recommended_metric_value": self.recommended_metric_value,
            "reviewer_verdict": self.reviewer_verdict,
            "rationale": self.rationale,
            "log_summary": self.log_summary,
            "continue_loop": self.continue_loop,
            "memory_write_required": self.memory_write_required,
        }


@dataclass(frozen=True, slots=True)
class NextAction:
    phase: Phase
    next_agent_ids: tuple[str, ...]
    expected_artifact_type: ArtifactType
    required_receipts: tuple[SourceReceipt, ...]
    prompt_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "next_agent_ids": list(self.next_agent_ids),
            "expected_artifact_type": self.expected_artifact_type.value,
            "required_receipts": [receipt.to_dict() for receipt in self.required_receipts],
            "prompt_text": self.prompt_text,
        }


@dataclass(frozen=True, slots=True)
class PhaseTarget:
    agent_ids: tuple[str, ...]
    artifact_type: ArtifactType


@dataclass(frozen=True, slots=True)
class AutoresearchState:
    phase: Phase = Phase.SETUP_CONTEXT
    iteration: int = 1
    consensus_retry_count: int = 0
    verification_fix_attempts: int = 0
    setup: SetupContextArtifact | None = None
    context_packet: ContextPacketArtifact | None = None
    debate_rounds: tuple[DebateResultArtifact, ...] = field(default_factory=tuple)
    consensus_history: tuple[ConsensusResultArtifact, ...] = field(default_factory=tuple)
    implementation_result: ImplementationResultArtifact | None = None
    verification_history: tuple[VerificationResultArtifact, ...] = field(default_factory=tuple)
    review_history: tuple[ReviewResultArtifact, ...] = field(default_factory=tuple)
    fix_history: tuple[FixResultArtifact, ...] = field(default_factory=tuple)
    pending_fix_trigger: FixTriggerPhase | None = None
    final_decision: FinalDecisionArtifact | None = None
    memory_written: bool = False

    @property
    def latest_debate(self) -> DebateResultArtifact | None:
        return self.debate_rounds[-1] if self.debate_rounds else None

    @property
    def latest_consensus(self) -> ConsensusResultArtifact | None:
        return self.consensus_history[-1] if self.consensus_history else None

    @property
    def latest_verification(self) -> VerificationResultArtifact | None:
        return self.verification_history[-1] if self.verification_history else None

    @property
    def latest_review(self) -> ReviewResultArtifact | None:
        return self.review_history[-1] if self.review_history else None

    @property
    def latest_fix(self) -> FixResultArtifact | None:
        return self.fix_history[-1] if self.fix_history else None

    @classmethod
    def from_dict(cls, raw: object) -> AutoresearchState:
        data = _ensure_mapping(raw, label="autoresearch_state")

        def _parse_tuple(
            field_name: str,
            parser: Callable[[object], _T],
        ) -> tuple[_T, ...]:
            value = data.get(field_name, [])
            if not isinstance(value, Sequence) or isinstance(value, str | bytes):
                raise AutoresearchValidationError(f"{field_name} must be a list")
            return tuple(parser(item) for item in value)

        setup_raw = data.get("setup")
        context_raw = data.get("context_packet")
        implementation_raw = data.get("implementation_result")
        decision_raw = data.get("final_decision")
        pending_fix_trigger_raw = data.get("pending_fix_trigger")
        if pending_fix_trigger_raw is not None and not isinstance(pending_fix_trigger_raw, str):
            raise AutoresearchValidationError("pending_fix_trigger must be a string or null")

        state = cls(
            phase=Phase(_require_str(data, "phase")) if "phase" in data else Phase.SETUP_CONTEXT,
            iteration=_require_int(data, "iteration") if "iteration" in data else 1,
            consensus_retry_count=_require_int(data, "consensus_retry_count")
            if "consensus_retry_count" in data
            else 0,
            verification_fix_attempts=_require_int(data, "verification_fix_attempts")
            if "verification_fix_attempts" in data
            else 0,
            setup=SetupContextArtifact.from_dict(setup_raw) if setup_raw is not None else None,
            context_packet=ContextPacketArtifact.from_dict(context_raw)
            if context_raw is not None
            else None,
            debate_rounds=_parse_tuple("debate_rounds", DebateResultArtifact.from_dict),
            consensus_history=_parse_tuple("consensus_history", ConsensusResultArtifact.from_dict),
            implementation_result=ImplementationResultArtifact.from_dict(implementation_raw)
            if implementation_raw is not None
            else None,
            verification_history=_parse_tuple(
                "verification_history", VerificationResultArtifact.from_dict
            ),
            review_history=_parse_tuple("review_history", ReviewResultArtifact.from_dict),
            fix_history=_parse_tuple("fix_history", FixResultArtifact.from_dict),
            pending_fix_trigger=FixTriggerPhase(pending_fix_trigger_raw)
            if pending_fix_trigger_raw is not None
            else None,
            final_decision=FinalDecisionArtifact.from_dict(decision_raw)
            if decision_raw is not None
            else None,
            memory_written=_require_bool(data, "memory_written")
            if "memory_written" in data
            else False,
        )
        return state

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "iteration": self.iteration,
            "consensus_retry_count": self.consensus_retry_count,
            "verification_fix_attempts": self.verification_fix_attempts,
            "setup": self.setup.to_dict() if self.setup else None,
            "context_packet": self.context_packet.to_dict() if self.context_packet else None,
            "debate_rounds": [artifact.to_dict() for artifact in self.debate_rounds],
            "consensus_history": [artifact.to_dict() for artifact in self.consensus_history],
            "implementation_result": self.implementation_result.to_dict()
            if self.implementation_result
            else None,
            "verification_history": [artifact.to_dict() for artifact in self.verification_history],
            "review_history": [artifact.to_dict() for artifact in self.review_history],
            "fix_history": [artifact.to_dict() for artifact in self.fix_history],
            "pending_fix_trigger": self.pending_fix_trigger.value
            if self.pending_fix_trigger is not None
            else None,
            "final_decision": self.final_decision.to_dict() if self.final_decision else None,
            "memory_written": self.memory_written,
        }


LOCAL_RECEIPT_PATHS: dict[str, Path] = {
    "g2.autoresearch_skill": Path("gateway/agent_config/skills/autoresearch/SKILL.md"),
    "g2.quantipy_methodology": Path("gateway/agent_config/skills/quantipy-methodology/SKILL.md"),
}

QUANTIPY_RECEIPT_PATHS: dict[str, Path] = {
    "quantipy.agents": Path("AGENTS.md"),
    "quantipy.skill.backend_python": Path(".agents/skills/backend-python/SKILL.md"),
    "quantipy.skill.backtesting": Path(".agents/skills/backtesting/SKILL.md"),
    "quantipy.skill.data_collection": Path(".agents/skills/data-collection/SKILL.md"),
    "quantipy.skill.data_querying": Path(".agents/skills/data-querying/SKILL.md"),
    "quantipy.skill.experiment_data": Path(".agents/skills/experiment-data/SKILL.md"),
    "quantipy.agent.backend_python": Path(".codex/agents/backend-python.toml"),
    "quantipy.agent.contrarian": Path(".codex/agents/contrarian.toml"),
    "quantipy.agent.explorer": Path(".codex/agents/explorer.toml"),
    "quantipy.agent.orchestrator": Path(".codex/agents/orchestrator.toml"),
    "quantipy.agent.researcher": Path(".codex/agents/researcher.toml"),
    "quantipy.agent.reviewer": Path(".codex/agents/reviewer.toml"),
    "quantipy.agent.theorist": Path(".codex/agents/theorist.toml"),
}

PHASE_RECEIPTS: dict[Phase, tuple[str, ...]] = {
    Phase.SETUP_CONTEXT: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.experiment_data",
        "quantipy.skill.data_querying",
        "quantipy.agent.explorer",
        "quantipy.agent.researcher",
    ),
    Phase.DEBATE: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_collection",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.contrarian",
        "quantipy.agent.orchestrator",
        "quantipy.agent.researcher",
        "quantipy.agent.reviewer",
        "quantipy.agent.theorist",
    ),
    Phase.CONSENSUS: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_collection",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.contrarian",
        "quantipy.agent.researcher",
        "quantipy.agent.reviewer",
        "quantipy.agent.theorist",
    ),
    Phase.IMPLEMENTATION: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.orchestrator",
    ),
    Phase.VERIFICATION: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.reviewer",
    ),
    Phase.REVIEW: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.contrarian",
        "quantipy.agent.reviewer",
    ),
    Phase.FIX_TEST: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.orchestrator",
        "quantipy.agent.reviewer",
    ),
    Phase.DECISION_LOG: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "quantipy.agents",
    ),
    Phase.REPEAT: (
        "g2.autoresearch_skill",
        "quantipy.agents",
    ),
}

ARTIFACT_CONTRACTS: dict[ArtifactType, dict[str, object]] = {
    ArtifactType.SETUP: {
        "required_fields": [
            "goal",
            "metric_name",
            "metric_direction",
            "target_repo",
            "writable_scope",
            "baseline_summary",
            "hard_constraints",
            "data_sources",
        ]
    },
    ArtifactType.CONTEXT_PACKET: {
        "required_fields": [
            "baseline_metric",
            "current_best_metric",
            "recent_experiment_outcomes",
            "prior_findings",
            "open_proposals",
            "hard_constraints",
            "available_data_sources",
            "loaded_quantipy_sources",
        ]
    },
    ArtifactType.DEBATE_RESULT: {
        "required_fields": ["round_number", "submissions[5]"],
    },
    ArtifactType.CONSENSUS_RESULT: {
        "required_fields": [
            "round_number",
            "status",
            "majority_count",
            "majority_agent_ids",
            "dissenting_positions",
            "novelty_score",
            "theory_score",
            "implementation_risk_score",
            "data_adequacy_score",
            "overfit_risk_score",
            "expected_net_sharpe",
            "rejection_reasons",
            "dissent_summary",
            "winner_theory_id|null",
            "implementation_brief|null",
        ]
    },
    ArtifactType.IMPLEMENTATION_RESULT: {
        "required_fields": [
            "summary",
            "module_path",
            "notebook_path",
            "tests_added_or_updated",
            "commands_run",
        ]
    },
    ArtifactType.VERIFICATION_RESULT: {
        "required_fields": [
            "status",
            "is_walk_forward_sharpe_net",
            "oos_sharpe_net",
            "max_drawdown_pct",
            "win_rate",
            "trade_count",
            "trades_per_day",
            "oos_trading_days",
            "feature_importances_summary",
            "null_test_summary",
            "bug_signals",
            "tests_passed",
            "commands_run",
        ]
    },
    ArtifactType.REVIEW_RESULT: {
        "required_fields": [
            "reviewer_agent_id",
            "verdict",
            "recommended_metric_name",
            "recommended_metric_value",
            "critical_issues",
            "noncritical_issues",
            "fix_requests",
            "summary",
        ]
    },
    ArtifactType.FIX_RESULT: {
        "required_fields": [
            "trigger_phase",
            "summary",
            "fixes_applied",
            "tests_rerun",
            "remaining_issues",
        ]
    },
    ArtifactType.FINAL_DECISION: {
        "required_fields": [
            "decision",
            "recommended_metric_name",
            "recommended_metric_value|null",
            "reviewer_verdict|null",
            "rationale",
            "log_summary",
            "continue_loop",
            "memory_write_required",
        ]
    },
    ArtifactType.MEMORY_WRITE: {"required_fields": ["memory_written=true"]},
    ArtifactType.NEXT_ITERATION: {"required_fields": ["start_next_iteration"]},
}


def _load_receipt(receipt_id: str, path: Path) -> SourceReceipt:
    if not path.is_file():
        raise AutoresearchReceiptError(f"missing required receipt source: {path}")
    content = path.read_text(encoding="utf-8")
    return SourceReceipt(
        receipt_id=receipt_id, path=path, sha256=_sha256_text(content), content=content
    )


def build_receipt_catalog(quantipy_root: Path = DEFAULT_QUANTIPY_ROOT) -> ReceiptCatalog:
    receipts: dict[str, SourceReceipt] = {}
    for receipt_id, path in LOCAL_RECEIPT_PATHS.items():
        receipts[receipt_id] = _load_receipt(receipt_id, path)
    for receipt_id, relative_path in QUANTIPY_RECEIPT_PATHS.items():
        receipts[receipt_id] = _load_receipt(receipt_id, quantipy_root / relative_path)
    return ReceiptCatalog(receipts=receipts)


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchConfigError(f"missing config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchConfigError(f"invalid JSON in config file: {path}") from exc
    return _ensure_mapping(raw, label=str(path))


def _agent_policy_from_json(
    agent_map: Mapping[str, Mapping[str, object]], agent_id: str
) -> StageAgentPolicy:
    try:
        raw = agent_map[agent_id]
    except KeyError as exc:
        raise AutoresearchConfigError(f"missing configured agent: {agent_id}") from exc
    model = _ensure_mapping(raw.get("model"), label=f"{agent_id}.model")
    skills = _require_string_list(raw, "skills")
    return StageAgentPolicy(
        agent_id=agent_id,
        model=_require_str(model, "primary"),
        reasoning=_require_str(raw, "thinkingDefault"),
        skills=skills,
    )


def load_autoresearch_policy(
    config_path: Path = DEFAULT_OPENCLAW_CONFIG_PATH,
) -> AutoresearchPolicy:
    config = _load_json(config_path)
    models = _ensure_mapping(config.get("models"), label="models")
    providers = _ensure_mapping(models.get("providers"), label="providers")
    openai_provider = _ensure_mapping(providers.get("openai"), label="providers.openai")
    if _require_str(openai_provider, "api") != "openai-responses":
        raise AutoresearchConfigError("providers.openai.api must be openai-responses")
    agent_runtime = _ensure_mapping(
        openai_provider.get("agentRuntime"), label="providers.openai.agentRuntime"
    )
    if _require_str(agent_runtime, "id") != "codex":
        raise AutoresearchConfigError("providers.openai.agentRuntime.id must be codex")
    openai_models_raw = openai_provider.get("models")
    if not isinstance(openai_models_raw, Sequence) or isinstance(openai_models_raw, str | bytes):
        raise AutoresearchConfigError("providers.openai.models must be a list")
    model_caps: dict[str, bool] = {}
    for item in openai_models_raw:
        data = _ensure_mapping(item, label="provider_model")
        model_caps[_require_str(data, "id")] = _require_bool(data, "reasoning")
    for required_model in ("gpt-5.4", "gpt-5.5"):
        if model_caps.get(required_model) is not True:
            raise AutoresearchConfigError(f"openai/{required_model} must exist with reasoning=true")

    agents = _ensure_mapping(config.get("agents"), label="agents")
    agent_list_raw = agents.get("list")
    if not isinstance(agent_list_raw, Sequence) or isinstance(agent_list_raw, str | bytes):
        raise AutoresearchConfigError("agents.list must be a list")
    agent_map: dict[str, Mapping[str, object]] = {}
    for item in agent_list_raw:
        data = _ensure_mapping(item, label="agent")
        agent_map[_require_str(data, "id")] = data

    policy = AutoresearchPolicy(
        main=_agent_policy_from_json(agent_map, "main"),
        context_curator=_agent_policy_from_json(agent_map, "context-curator"),
        debate_agents=tuple(
            _agent_policy_from_json(agent_map, agent_id)
            for agent_id in (
                "debater-microstructure",
                "debater-data",
                "debater-skeptic",
                "debater-theory",
                "debater-implementation",
            )
        ),
        consensus=_agent_policy_from_json(agent_map, "consensus-arbiter"),
        implementer=_agent_policy_from_json(agent_map, "implementer"),
        reviewer=_agent_policy_from_json(agent_map, "reviewer"),
        fixer=_agent_policy_from_json(agent_map, "fixer"),
    )
    _validate_policy(policy, agent_map)
    return policy


def _validate_policy(
    policy: AutoresearchPolicy, agent_map: Mapping[str, Mapping[str, object]]
) -> None:
    if policy.main.model != "openai/gpt-5.5" or policy.main.reasoning != "high":
        raise AutoresearchConfigError("main must be openai/gpt-5.5 with high reasoning")
    if (
        policy.context_curator.model != "openai/gpt-5.4"
        or policy.context_curator.reasoning != "high"
    ):
        raise AutoresearchConfigError("context-curator must be openai/gpt-5.4 with high reasoning")

    high_55 = 0
    high_54 = 0
    for agent in policy.debate_agents:
        if agent.reasoning != "high":
            raise AutoresearchConfigError(f"{agent.agent_id} must use high reasoning")
        if agent.model == "openai/gpt-5.5":
            high_55 += 1
        elif agent.model == "openai/gpt-5.4":
            high_54 += 1
        else:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be openai/gpt-5.5 or openai/gpt-5.4"
            )
    if high_55 != 3 or high_54 != 2:
        raise AutoresearchConfigError(
            "debate panel must be exactly three gpt-5.5 high agents and two gpt-5.4 high agents"
        )

    for agent in (policy.consensus, policy.implementer, policy.fixer):
        if agent.model != "openai/gpt-5.4" or agent.reasoning != "high":
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be openai/gpt-5.4 with high reasoning"
            )
    if policy.reviewer.model != "openai/gpt-5.5" or policy.reviewer.reasoning != "high":
        raise AutoresearchConfigError("reviewer must be exactly one openai/gpt-5.5 high agent")
    if policy.reviewer.agent_id != "reviewer":
        raise AutoresearchConfigError("reviewer stage must be configured as agent id 'reviewer'")

    main_raw = agent_map["main"]
    if tuple(policy.main.skills) != ("mempalace", "autoresearch"):
        raise AutoresearchConfigError("main must load exactly mempalace and autoresearch")
    subagents = _ensure_mapping(main_raw.get("subagents"), label="main.subagents")
    allow_agents = _require_string_list(subagents, "allowAgents")
    if tuple(allow_agents) != policy.all_stage_agent_ids:
        raise AutoresearchConfigError(
            "main allowAgents must exactly match the autoresearch stage roster"
        )
    for agent in (
        policy.context_curator,
        *policy.debate_agents,
        policy.consensus,
        policy.implementer,
        policy.reviewer,
        policy.fixer,
    ):
        if tuple(agent.skills) != ("mempalace-readonly", "quantipy-methodology"):
            raise AutoresearchConfigError(
                f"{agent.agent_id} must load exactly mempalace-readonly and quantipy-methodology"
            )
        if "mempalace" in agent.skills:
            raise AutoresearchConfigError(f"{agent.agent_id} must not load mempalace")
        tools = _ensure_mapping(
            agent_map[agent.agent_id].get("tools"),
            label=f"{agent.agent_id}.tools",
        )
        denied_tools = set(_require_string_list(tools, "deny"))
        missing_deny = sorted(set(MEMPALACE_MUTATION_TOOLS) - denied_tools)
        if missing_deny:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny MemPalace mutation tools: {', '.join(missing_deny)}"
            )


def _validate_state(state: AutoresearchState, policy: AutoresearchPolicy) -> None:
    if state.iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    if state.consensus_retry_count not in (0, 1):
        raise AutoresearchValidationError("consensus_retry_count must be 0 or 1")
    if state.context_packet is not None and state.setup is None:
        raise AutoresearchValidationError("context_packet requires setup first")
    if state.debate_rounds and state.context_packet is None:
        raise AutoresearchValidationError("debate history requires a context_packet")
    if state.consensus_history and state.latest_debate is None:
        raise AutoresearchValidationError("consensus history requires a debate_result")
    if state.memory_written and state.final_decision is None:
        raise AutoresearchValidationError("memory_written cannot be true before final_decision")
    if state.implementation_result and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation_result requires a majority consensus")
    if state.verification_history and state.implementation_result is None:
        raise AutoresearchValidationError("verification history requires an implementation_result")
    if state.review_history and not state.verification_history:
        raise AutoresearchValidationError("review history requires a verification_result")
    if state.pending_fix_trigger is not None and state.phase is not Phase.FIX_TEST:
        raise AutoresearchValidationError("pending_fix_trigger is only valid during fix_test")
    if state.final_decision is not None and state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError("final_decision requires repeat phase")
    for debate in state.debate_rounds:
        _validate_debate_result(debate, policy)
    for review in state.review_history:
        _validate_review_result(review, policy)
    if state.phase is Phase.DEBATE and state.context_packet is None:
        raise AutoresearchValidationError("debate phase requires a context_packet")
    if state.phase is Phase.CONSENSUS and state.latest_debate is None:
        raise AutoresearchValidationError("consensus phase requires a debate_result")
    if state.phase is Phase.IMPLEMENTATION and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation phase requires a majority consensus")
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
            if latest_review is None or (
                latest_review.verdict is not ReviewVerdict.FAIL
                and not latest_review.critical_issues
            ):
                raise AutoresearchValidationError(
                    "review-triggered fix_test requires a failing review_result"
                )
    if state.phase is Phase.DECISION_LOG and (
        state.latest_consensus is None
        and state.latest_review is None
        and state.latest_verification is None
    ):
        raise AutoresearchValidationError("decision_log phase requires prior artifacts")
    if state.phase is Phase.REPEAT and state.final_decision is None:
        raise AutoresearchValidationError("repeat phase requires final_decision")


def validate_state(state: AutoresearchState, policy: AutoresearchPolicy) -> None:
    _validate_state(state, policy)


def _validate_debate_result(debate: DebateResultArtifact, policy: AutoresearchPolicy) -> None:
    expected_ids = set(policy.debate_agent_ids)
    actual_ids = {submission.agent_id for submission in debate.submissions}
    if actual_ids != expected_ids:
        raise AutoresearchValidationError(
            "debate_result must contain exactly the configured five debate agents"
        )


def _validate_review_result(review: ReviewResultArtifact, policy: AutoresearchPolicy) -> None:
    if review.reviewer_agent_id != policy.reviewer.agent_id:
        raise AutoresearchValidationError(
            "review_result must come from the single configured reviewer"
        )


def _json_block(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _render_receipt_block(receipts: Sequence[SourceReceipt]) -> str:
    parts: list[str] = []
    for receipt in receipts:
        header = f"=== {receipt.receipt_id} | {receipt.path} | sha256={receipt.sha256} ==="
        parts.append(f"{header}\n{receipt.content}")
    return "\n\n".join(parts)


def _artifact_context(state: AutoresearchState) -> dict[str, object]:
    return {
        "iteration": state.iteration,
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


def _select_phase_target(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> PhaseTarget:
    if state.phase is Phase.SETUP_CONTEXT:
        if state.setup is None:
            return PhaseTarget((policy.main.agent_id,), ArtifactType.SETUP)
        return PhaseTarget((policy.context_curator.agent_id,), ArtifactType.CONTEXT_PACKET)
    if state.phase is Phase.DEBATE:
        return PhaseTarget(policy.debate_agent_ids, ArtifactType.DEBATE_RESULT)
    if state.phase is Phase.CONSENSUS:
        return PhaseTarget((policy.consensus.agent_id,), ArtifactType.CONSENSUS_RESULT)
    if state.phase is Phase.IMPLEMENTATION:
        if (
            state.latest_consensus is None
            or state.latest_consensus.status is not ConsensusStatus.MAJORITY
        ):
            raise AutoresearchValidationError(
                "implementation next action requires a majority consensus"
            )
        return PhaseTarget((policy.implementer.agent_id,), ArtifactType.IMPLEMENTATION_RESULT)
    if state.phase is Phase.VERIFICATION:
        return PhaseTarget((policy.main.agent_id,), ArtifactType.VERIFICATION_RESULT)
    if state.phase is Phase.REVIEW:
        return PhaseTarget((policy.reviewer.agent_id,), ArtifactType.REVIEW_RESULT)
    if state.phase is Phase.FIX_TEST:
        return PhaseTarget((policy.fixer.agent_id,), ArtifactType.FIX_RESULT)
    if state.phase is Phase.DECISION_LOG:
        return PhaseTarget((policy.main.agent_id,), ArtifactType.FINAL_DECISION)
    if not state.memory_written:
        return PhaseTarget((), ArtifactType.MEMORY_WRITE)
    return PhaseTarget((), ArtifactType.NEXT_ITERATION)


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
) -> None:
    latest_review = state.latest_review
    latest_verification = state.latest_verification
    latest_consensus = state.latest_consensus
    expected_reviewer_verdict = latest_review.verdict.value if latest_review is not None else None
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

    if latest_review is not None and (
        latest_review.verdict is ReviewVerdict.FAIL or latest_review.critical_issues
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "critical review issues require final_decision=DISCARD"
            )
        return

    if latest_verification is not None and latest_verification.max_drawdown_pct >= 30.0:
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


def _phase_instruction(
    state: AutoresearchState,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    agent_ids: Sequence[str],
) -> str:
    instructions: dict[Phase, str] = {
        Phase.SETUP_CONTEXT: (
            "Own the setup/context phase deterministically. "
            "If setup is missing, create the setup artifact. "
            "If setup exists, produce the context packet only after "
            "loading the required instructions."
        ),
        Phase.DEBATE: (
            "Run the fixed five-agent debate. "
            "Each configured debate agent returns one theory, one vote, "
            "and explicit objections. Do not substitute agents or models."
        ),
        Phase.CONSENSUS: (
            "Decide whether the latest debate round has a 3-of-5 majority. "
            "Return MAJORITY or NO_CONSENSUS only. "
            "There is exactly one retry if the first consensus is NO_CONSENSUS."
        ),
        Phase.IMPLEMENTATION: (
            "Implementation is allowed only after a majority consensus. "
            "Use the final implementation brief exactly "
            "as approved. No implementation without consensus majority."
        ),
        Phase.VERIFICATION: (
            "Verify the produced experiment deterministically. "
            "Reject impossible metrics, failing tests, "
            "or incomplete required metrics."
        ),
        Phase.REVIEW: (
            "Run exactly one configured reviewer. "
            "The reviewer must return PASS, CONDITIONAL PASS, or FAIL "
            "with concrete fix requests."
        ),
        Phase.FIX_TEST: (
            "Apply a narrow fix against the latest verification or review failure. "
            "After a fix, the next step "
            "is always verification."
        ),
        Phase.DECISION_LOG: (
            "Decide and log the completed iteration. "
            "Memory writes are forbidden before this final decision "
            "artifact exists."
        ),
        Phase.REPEAT: (
            "The iteration is complete. Do not start the next loop from prompt memory. "
            "Write memory only if allowed, then create a new state explicitly."
        ),
    }
    contract = _json_block(ARTIFACT_CONTRACTS[expected_artifact_type])
    state_json = _json_block(_artifact_context(state))
    agent_text = ", ".join(agent_ids) if agent_ids else "(controller/no agent spawn)"
    return (
        f"Autoresearch phase: {phase.value}\n"
        f"Target agents: {agent_text}\n"
        f"Expected artifact type: {expected_artifact_type.value}\n"
        f"Phase instruction:\n{instructions[phase]}\n\n"
        f"Artifact contract:\n{contract}\n\n"
        f"Current state snapshot:\n{state_json}"
    )


def _build_prompt_text(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    agent_ids: Sequence[str],
    receipts: Sequence[SourceReceipt],
) -> str:
    return (
        "Deterministic autoresearch runner prompt.\n"
        "This phase, agent roster, and artifact contract are owned by "
        "executable control-plane logic. "
        "Do not carry loop state in prompt memory.\n\n"
        f"Validated model policy:\n{policy.model_policy_summary()}\n\n"
        f"{_phase_instruction(state, phase, expected_artifact_type, agent_ids)}\n\n"
        "Loaded instructions and receipts:\n"
        f"{_render_receipt_block(receipts)}"
    )


def next_action(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> NextAction:
    _validate_state(state, policy)
    target = _select_phase_target(state, policy)
    required_receipts = receipts.require(PHASE_RECEIPTS[state.phase])
    return NextAction(
        phase=state.phase,
        next_agent_ids=target.agent_ids,
        expected_artifact_type=target.artifact_type,
        required_receipts=required_receipts,
        prompt_text=_build_prompt_text(
            state=state,
            policy=policy,
            phase=state.phase,
            expected_artifact_type=target.artifact_type,
            agent_ids=target.agent_ids,
            receipts=required_receipts,
        ),
    )


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
) -> AutoresearchState:
    _validate_state(state, policy)

    if state.phase is Phase.SETUP_CONTEXT:
        if isinstance(artifact, SetupContextArtifact):
            if state.setup is not None:
                raise AutoresearchValidationError("setup artifact already exists")
            return replace(state, setup=artifact)
        if isinstance(artifact, ContextPacketArtifact):
            if state.setup is None:
                raise AutoresearchValidationError("context packet requires setup first")
            return replace(state, context_packet=artifact, phase=Phase.DEBATE)
        raise AutoresearchValidationError(
            "setup_context phase accepts setup or context_packet artifacts only"
        )

    if state.phase is Phase.DEBATE:
        if not isinstance(artifact, DebateResultArtifact):
            raise AutoresearchValidationError("debate phase accepts debate_result only")
        _validate_debate_result(artifact, policy)
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
        next_consensus_history = (*state.consensus_history, artifact)
        if artifact.status is ConsensusStatus.MAJORITY:
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
        return replace(state, implementation_result=artifact, phase=Phase.VERIFICATION)

    if state.phase is Phase.VERIFICATION:
        if not isinstance(artifact, VerificationResultArtifact):
            raise AutoresearchValidationError("verification phase accepts verification_result only")
        if state.implementation_result is None:
            raise AutoresearchValidationError("verification requires implementation_result")
        next_verification_history = (*state.verification_history, artifact)
        if artifact.status is VerificationStatus.PASS:
            return replace(
                state,
                verification_history=next_verification_history,
                pending_fix_trigger=None,
                phase=Phase.REVIEW,
            )
        if (
            artifact.status is VerificationStatus.TEST_FAILURE
            and state.verification_fix_attempts >= 2
        ):
            return replace(
                state,
                verification_history=next_verification_history,
                pending_fix_trigger=None,
                phase=Phase.DECISION_LOG,
            )
        return replace(
            state,
            verification_history=next_verification_history,
            pending_fix_trigger=FixTriggerPhase.VERIFICATION,
            phase=Phase.FIX_TEST,
        )

    if state.phase is Phase.REVIEW:
        if not isinstance(artifact, ReviewResultArtifact):
            raise AutoresearchValidationError("review phase accepts review_result only")
        _validate_review_result(artifact, policy)
        next_review_history = (*state.review_history, artifact)
        if artifact.verdict is ReviewVerdict.FAIL or artifact.critical_issues:
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
        return replace(
            state,
            fix_history=(*state.fix_history, artifact),
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
        _validate_final_decision_artifact(artifact, state)
        return replace(state, final_decision=artifact, phase=Phase.REPEAT)

    raise AutoresearchValidationError(
        "repeat phase does not accept artifacts; mark memory or start next iteration"
    )


def can_write_memory(state: AutoresearchState) -> bool:
    return state.phase is Phase.REPEAT and state.final_decision is not None


def mark_memory_written(state: AutoresearchState) -> AutoresearchState:
    if not can_write_memory(state):
        raise AutoresearchValidationError("memory write is allowed only after final decision")
    return replace(state, memory_written=True)


def start_next_iteration(state: AutoresearchState) -> AutoresearchState:
    if not state.memory_written:
        raise AutoresearchValidationError("cannot start next iteration before memory is written")
    if state.setup is None or state.final_decision is None:
        raise AutoresearchValidationError("next iteration requires completed current iteration")
    return AutoresearchState(
        phase=Phase.SETUP_CONTEXT,
        iteration=state.iteration + 1,
        setup=state.setup,
    )


def load_artifact_file(
    path: Path,
    state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> (
    SetupContextArtifact
    | ContextPacketArtifact
    | DebateResultArtifact
    | ConsensusResultArtifact
    | ImplementationResultArtifact
    | VerificationResultArtifact
    | ReviewResultArtifact
    | FixResultArtifact
    | FinalDecisionArtifact
):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing artifact file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid artifact JSON: {path}") from exc

    _validate_state(state, policy)
    target = _select_phase_target(state, policy)
    if target.artifact_type is ArtifactType.SETUP:
        return SetupContextArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.CONTEXT_PACKET:
        return ContextPacketArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.DEBATE_RESULT:
        return DebateResultArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.CONSENSUS_RESULT:
        return ConsensusResultArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.IMPLEMENTATION_RESULT:
        return ImplementationResultArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.VERIFICATION_RESULT:
        return VerificationResultArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.REVIEW_RESULT:
        return ReviewResultArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.FIX_RESULT:
        return FixResultArtifact.from_dict(raw)
    if target.artifact_type is ArtifactType.FINAL_DECISION:
        return FinalDecisionArtifact.from_dict(raw)
    raise AutoresearchValidationError(
        f"{state.phase.value} does not accept artifact files; use state mutation commands instead"
    )


def load_state_file(path: Path) -> AutoresearchState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing state file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid state JSON: {path}") from exc
    return AutoresearchState.from_dict(raw)


def save_state_file(path: Path, state: AutoresearchState) -> None:
    path.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
