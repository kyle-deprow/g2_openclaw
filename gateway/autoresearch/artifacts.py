"""Autoresearch artifact value objects."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gateway.autoresearch.compute import (
    ComputeFitArtifact as ComputeFitArtifact,
)
from gateway.autoresearch.constants import (
    MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS as MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS,
)
from gateway.autoresearch.constants import (
    MAX_EXAMPLE_TICKERS as MAX_EXAMPLE_TICKERS,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_MEMBERS_PER_DATE as MAX_UNIVERSE_MEMBERS_PER_DATE,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_SELECTION_DATES as MAX_UNIVERSE_SELECTION_DATES,
)
from gateway.autoresearch.constants import (
    NEXT_SESSION_EXECUTION_POLICY as NEXT_SESSION_EXECUTION_POLICY,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXECUTION_NOT_STARTED_REASONS as QUANTIPY_EXECUTION_NOT_STARTED_REASONS,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES as QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_STAGE_ORDER as QUANTIPY_EXPERIMENT_STAGE_ORDER,
)
from gateway.autoresearch.enums import (
    ArtifactType as ArtifactType,
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
    MetricDirection as MetricDirection,
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
    _normalise_predicate as _normalise_predicate,
)
from gateway.autoresearch.fields import (
    _optional_string_list as _optional_string_list,
)
from gateway.autoresearch.fields import (
    _require_bool as _require_bool,
)
from gateway.autoresearch.fields import (
    _require_canonical_identifier as _require_canonical_identifier,
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
    _require_iso_date as _require_iso_date,
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
    _require_workspace_path as _require_workspace_path,
)
from gateway.autoresearch.fields import (
    _validate_iso_date_value as _validate_iso_date_value,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.fields import (
    _validate_workspace_path as _validate_workspace_path,
)
from gateway.autoresearch.manifest import (
    SourceReceipt as SourceReceipt,
)

if TYPE_CHECKING:
    from typing import Protocol

    from gateway.autoresearch.manifest import (
        AuthoritativeStateReference,
    )

    class InstructionSourceManifest(Protocol):
        @property
        def state_reference(self) -> AuthoritativeStateReference: ...

        def sha256(self) -> str: ...

        def to_dict(self) -> dict[str, object]: ...


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
        _require_exact_keys(
            data,
            label="setup_context",
            expected=(
                "goal",
                "metric_name",
                "metric_direction",
                "target_repo",
                "writable_scope",
                "baseline_summary",
                "hard_constraints",
                "data_sources",
            ),
        )
        artifact = cls(
            goal=_require_str(data, "goal"),
            metric_name=_require_str(data, "metric_name"),
            metric_direction=MetricDirection(_require_str(data, "metric_direction")),
            target_repo=_require_str(data, "target_repo"),
            writable_scope=_require_str(data, "writable_scope"),
            baseline_summary=_require_str(data, "baseline_summary"),
            hard_constraints=_optional_string_list(data, "hard_constraints"),
            data_sources=_optional_string_list(data, "data_sources"),
        )
        return artifact

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
class UniversePlanArtifact:
    profile_id: str
    profile_digest: str
    selection_dates: tuple[str, ...]
    max_members_per_date: int
    execution_policy: str

    @classmethod
    def from_dict(cls, raw: object) -> UniversePlanArtifact:
        data = _ensure_mapping(raw, label="universe_plan")
        _require_exact_keys(
            data,
            label="universe_plan",
            expected=(
                "profile_id",
                "profile_digest",
                "selection_dates",
                "max_members_per_date",
                "execution_policy",
            ),
        )
        artifact = cls(
            profile_id=_require_str(data, "profile_id"),
            profile_digest=_require_sha256(data, "profile_digest"),
            selection_dates=_require_string_list(data, "selection_dates"),
            max_members_per_date=_require_int(data, "max_members_per_date"),
            execution_policy=_require_str(data, "execution_policy"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        _validate_sha256(self.profile_digest, label="profile_digest")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.profile_id):
            raise AutoresearchValidationError("universe plan profile_id must be kebab-case")
        if not self.selection_dates:
            raise AutoresearchValidationError("universe plan requires selection dates")
        if len(self.selection_dates) > MAX_UNIVERSE_SELECTION_DATES:
            raise AutoresearchValidationError(
                f"universe plan allows at most {MAX_UNIVERSE_SELECTION_DATES} selection dates"
            )
        for selection_date in self.selection_dates:
            _validate_iso_date_value(selection_date, label="selection_date")
        if tuple(sorted(set(self.selection_dates))) != self.selection_dates:
            raise AutoresearchValidationError(
                "universe plan selection_dates must be sorted and unique"
            )
        if not 1 <= self.max_members_per_date <= MAX_UNIVERSE_MEMBERS_PER_DATE:
            raise AutoresearchValidationError(
                "universe plan max_members_per_date must be between 1 and 1000"
            )
        if self.execution_policy != NEXT_SESSION_EXECUTION_POLICY:
            raise AutoresearchValidationError(
                f"universe plan execution_policy must be {NEXT_SESSION_EXECUTION_POLICY}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "selection_dates": list(self.selection_dates),
            "max_members_per_date": self.max_members_per_date,
            "execution_policy": self.execution_policy,
        }


@dataclass(frozen=True, slots=True)
class DebateSubmission:
    agent_id: str
    theory_id: str
    theory_family: str
    vote_family: str
    hypothesis: str
    universe: str
    example_tickers: tuple[str, ...]
    feature_pipeline: str
    model_plan: str
    walk_forward_plan: str
    transaction_cost_model: str
    data_coverage_plan: str
    rejection_criteria: str
    objections: tuple[str, ...]
    compute_fit: ComputeFitArtifact | None = None
    materially_new_evidence: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> DebateSubmission:
        data = _ensure_mapping(raw, label="debate_submission")
        _require_exact_keys(
            data,
            label="debate_submission",
            expected=(
                "agent_id",
                "theory_id",
                "theory_family",
                "vote_family",
                "hypothesis",
                "universe",
                "example_tickers",
                "feature_pipeline",
                "model_plan",
                "walk_forward_plan",
                "transaction_cost_model",
                "data_coverage_plan",
                "rejection_criteria",
                "objections",
                "compute_fit",
                "materially_new_evidence",
            ),
        )
        exemption = data.get("materially_new_evidence")
        if exemption is not None and not isinstance(exemption, str):
            raise AutoresearchValidationError("materially_new_evidence must be a string or null")
        compute_fit_raw = data.get("compute_fit")
        artifact = cls(
            agent_id=_require_str(data, "agent_id"),
            theory_id=_require_str(data, "theory_id"),
            theory_family=_require_str(data, "theory_family"),
            vote_family=_require_str(data, "vote_family"),
            hypothesis=_require_str(data, "hypothesis"),
            universe=_require_str(data, "universe"),
            example_tickers=_require_string_list(data, "example_tickers"),
            feature_pipeline=_require_str(data, "feature_pipeline"),
            model_plan=_require_str(data, "model_plan"),
            walk_forward_plan=_require_str(data, "walk_forward_plan"),
            transaction_cost_model=_require_str(data, "transaction_cost_model"),
            data_coverage_plan=_require_str(data, "data_coverage_plan"),
            rejection_criteria=_require_str(data, "rejection_criteria"),
            objections=_require_string_list(data, "objections"),
            compute_fit=(
                ComputeFitArtifact.from_dict(compute_fit_raw)
                if compute_fit_raw is not None
                else None
            ),
            materially_new_evidence=exemption.strip() if isinstance(exemption, str) else None,
        )
        if len(artifact.example_tickers) > MAX_EXAMPLE_TICKERS:
            raise AutoresearchValidationError("example_tickers allows at most 8 symbols")
        if tuple(sorted(set(artifact.example_tickers))) != artifact.example_tickers:
            raise AutoresearchValidationError("example_tickers must be sorted and unique")
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "theory_id": self.theory_id,
            "theory_family": self.theory_family,
            "vote_family": self.vote_family,
            "hypothesis": self.hypothesis,
            "universe": self.universe,
            "example_tickers": list(self.example_tickers),
            "feature_pipeline": self.feature_pipeline,
            "model_plan": self.model_plan,
            "walk_forward_plan": self.walk_forward_plan,
            "transaction_cost_model": self.transaction_cost_model,
            "data_coverage_plan": self.data_coverage_plan,
            "rejection_criteria": self.rejection_criteria,
            "objections": list(self.objections),
            "compute_fit": self.compute_fit.to_dict() if self.compute_fit is not None else None,
            "materially_new_evidence": self.materially_new_evidence,
        }


@dataclass(frozen=True, slots=True)
class DebateResultArtifact:
    round_number: int
    submissions: tuple[DebateSubmission, ...]

    @classmethod
    def from_dict(cls, raw: object) -> DebateResultArtifact:
        data = _ensure_mapping(raw, label="debate_result")
        _require_exact_keys(data, label="debate_result", expected=("round_number", "submissions"))
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
    universe_plan: UniversePlanArtifact | None = None

    @classmethod
    def from_dict(cls, raw: object) -> ConsensusResultArtifact:
        data = _ensure_mapping(raw, label="consensus_result")
        _require_exact_keys(
            data,
            label="consensus_result",
            expected=(
                "round_number",
                "status",
                "winner_theory_id",
                "winner_theory_family",
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
                "implementation_brief",
                "dissent_summary",
                "universe_plan",
            ),
        )
        winner_theory_id = data.get("winner_theory_id")
        winner_theory_family = data.get("winner_theory_family")
        implementation_brief = data.get("implementation_brief")
        if "universe_plan" not in data:
            raise AutoresearchValidationError("universe_plan must be an object or null")
        universe_plan_raw = data["universe_plan"]
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
            universe_plan=(
                UniversePlanArtifact.from_dict(universe_plan_raw)
                if universe_plan_raw is not None
                else None
            ),
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
            if (
                self.winner_theory_id
                or self.winner_theory_family
                or self.implementation_brief
                or self.universe_plan is not None
            ):
                raise AutoresearchValidationError(
                    "NO_CONSENSUS must not include winner, implementation brief, or universe plan"
                )
        if self.universe_plan is not None:
            self.universe_plan.validate()

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
            "universe_plan": self.universe_plan.to_dict()
            if self.universe_plan is not None
            else None,
        }


@dataclass(frozen=True, slots=True)
class QuantipyExperimentFailureEvidence:
    category: str
    message: str

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExperimentFailureEvidence:
        data = _ensure_mapping(raw, label="quantipy_experiment_evidence.failure")
        _require_exact_keys(
            data,
            label="quantipy_experiment_evidence.failure",
            expected=("category", "message"),
        )
        failure = cls(
            category=_require_str(data, "category"),
            message=_require_str(data, "message"),
        )
        failure.validate()
        return failure

    def validate(self) -> None:
        if self.category not in QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES:
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.failure.category is not a Quantipy failure category"
            )

    def to_dict(self) -> dict[str, object]:
        return {"category": self.category, "message": self.message}


@dataclass(frozen=True, slots=True)
class QuantipyExperimentPanelEvidence:
    panel_path: str
    panel_sha256: str
    receipt_path: str
    receipt_sha256: str
    request_sha256: str
    coverage_sha256: str

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExperimentPanelEvidence:
        data = _ensure_mapping(raw, label="quantipy_experiment_evidence.panel")
        _require_exact_keys(
            data,
            label="quantipy_experiment_evidence.panel",
            expected=(
                "panel_path",
                "panel_sha256",
                "receipt_path",
                "receipt_sha256",
                "request_sha256",
                "coverage_sha256",
            ),
        )
        evidence = cls(
            panel_path=_require_str(data, "panel_path"),
            panel_sha256=_require_sha256(data, "panel_sha256"),
            receipt_path=_require_str(data, "receipt_path"),
            receipt_sha256=_require_sha256(data, "receipt_sha256"),
            request_sha256=_require_sha256(data, "request_sha256"),
            coverage_sha256=_require_sha256(data, "coverage_sha256"),
        )
        evidence.validate()
        return evidence

    def validate(self) -> None:
        if self.panel_path != "panel/panel.parquet":
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.panel.panel_path must be panel/panel.parquet"
            )
        if self.receipt_path != "panel/receipt.json":
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.panel.receipt_path must be panel/receipt.json"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "panel_path": self.panel_path,
            "panel_sha256": self.panel_sha256,
            "receipt_path": self.receipt_path,
            "receipt_sha256": self.receipt_sha256,
            "request_sha256": self.request_sha256,
            "coverage_sha256": self.coverage_sha256,
        }


@dataclass(frozen=True, slots=True)
class QuantipyExperimentEvidence:
    manifest_path: str
    manifest_sha256: str
    detached_run_directory: str
    detached_run_manifest_sha256: str
    run_id: str
    run_json_path: str
    run_json_sha256: str
    success: bool
    completed_stages: tuple[str, ...]
    terminal_stage: str | None
    terminal_status: str | None
    failure: QuantipyExperimentFailureEvidence | None
    panel: QuantipyExperimentPanelEvidence | None

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExperimentEvidence:
        data = _ensure_mapping(raw, label="quantipy_experiment_evidence")
        _require_exact_keys(
            data,
            label="quantipy_experiment_evidence",
            expected=(
                "manifest_path",
                "manifest_sha256",
                "detached_run_directory",
                "detached_run_manifest_sha256",
                "run_id",
                "run_json_path",
                "run_json_sha256",
                "success",
                "completed_stages",
                "terminal_stage",
                "terminal_status",
                "failure",
                "panel",
            ),
        )
        failure_raw = data.get("failure")
        panel_raw = data.get("panel")
        evidence = cls(
            manifest_path=_require_workspace_path(data, "manifest_path"),
            manifest_sha256=_require_sha256(data, "manifest_sha256"),
            detached_run_directory=_require_workspace_path(data, "detached_run_directory"),
            detached_run_manifest_sha256=_require_sha256(
                data,
                "detached_run_manifest_sha256",
            ),
            run_id=_require_str(data, "run_id"),
            run_json_path=_require_workspace_path(data, "run_json_path"),
            run_json_sha256=_require_sha256(data, "run_json_sha256"),
            success=_require_bool(data, "success"),
            completed_stages=_require_string_list(data, "completed_stages"),
            terminal_stage=(
                _require_str(data, "terminal_stage")
                if data.get("terminal_stage") is not None
                else None
            ),
            terminal_status=(
                _require_str(data, "terminal_status")
                if data.get("terminal_status") is not None
                else None
            ),
            failure=(
                QuantipyExperimentFailureEvidence.from_dict(failure_raw)
                if failure_raw is not None
                else None
            ),
            panel=(
                QuantipyExperimentPanelEvidence.from_dict(panel_raw)
                if panel_raw is not None
                else None
            ),
        )
        evidence.validate()
        return evidence

    def validate(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.run_id) is None:
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.run_id must be a safe Quantipy run ID"
            )
        if self.completed_stages != QUANTIPY_EXPERIMENT_STAGE_ORDER[: len(self.completed_stages)]:
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.completed_stages must be an ordered Quantipy prefix"
            )
        if self.success and self.completed_stages != QUANTIPY_EXPERIMENT_STAGE_ORDER:
            raise AutoresearchValidationError(
                "successful Quantipy experiment evidence requires all four completed stages"
            )
        if self.success and self.failure is not None:
            raise AutoresearchValidationError(
                "successful Quantipy experiment evidence cannot contain failure evidence"
            )
        if self.success and (self.terminal_stage is not None or self.terminal_status is not None):
            raise AutoresearchValidationError(
                "successful Quantipy experiment evidence cannot contain a terminal failure stage"
            )
        if (self.terminal_stage is None) != (self.terminal_status is None):
            raise AutoresearchValidationError(
                "Quantipy experiment terminal stage and status must both be present or null"
            )
        if (
            self.terminal_stage is not None
            and self.terminal_stage not in QUANTIPY_EXPERIMENT_STAGE_ORDER
        ):
            raise AutoresearchValidationError("Quantipy experiment terminal stage is invalid")
        if self.terminal_status is not None and self.terminal_status not in {"rejected", "failed"}:
            raise AutoresearchValidationError("Quantipy experiment terminal status is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "detached_run_directory": self.detached_run_directory,
            "detached_run_manifest_sha256": self.detached_run_manifest_sha256,
            "run_id": self.run_id,
            "run_json_path": self.run_json_path,
            "run_json_sha256": self.run_json_sha256,
            "success": self.success,
            "completed_stages": list(self.completed_stages),
            "terminal_stage": self.terminal_stage,
            "terminal_status": self.terminal_status,
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "panel": self.panel.to_dict() if self.panel is not None else None,
        }


@dataclass(frozen=True, slots=True)
class QuantipyExecutionNotStartedEvidence:
    manifest_path: str
    manifest_sha256: str
    expected_run_id: str
    expected_run_json_path: str
    reason: str
    command: str
    evidence: str

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExecutionNotStartedEvidence:
        data = _ensure_mapping(raw, label="quantipy_execution_not_started")
        _require_exact_keys(
            data,
            label="quantipy_execution_not_started",
            expected=(
                "manifest_path",
                "manifest_sha256",
                "expected_run_id",
                "expected_run_json_path",
                "reason",
                "command",
                "evidence",
            ),
        )
        receipt = cls(
            manifest_path=_require_workspace_path(data, "manifest_path"),
            manifest_sha256=_require_sha256(data, "manifest_sha256"),
            expected_run_id=_require_str(data, "expected_run_id"),
            expected_run_json_path=_require_workspace_path(data, "expected_run_json_path"),
            reason=_require_str(data, "reason"),
            command=_require_str(data, "command"),
            evidence=_require_str(data, "evidence"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if self.reason not in QUANTIPY_EXECUTION_NOT_STARTED_REASONS:
            raise AutoresearchValidationError(
                "quantipy_execution_not_started.reason must be focused_tests_failed"
            )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.expected_run_id) is None:
            raise AutoresearchValidationError(
                "quantipy_execution_not_started.expected_run_id is invalid"
            )
        if not self.command.strip() or not self.evidence.strip():
            raise AutoresearchValidationError(
                "quantipy_execution_not_started requires exact command and evidence"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "expected_run_id": self.expected_run_id,
            "expected_run_json_path": self.expected_run_json_path,
            "reason": self.reason,
            "command": self.command,
            "evidence": self.evidence,
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
        _require_exact_keys(
            data,
            label="review_result",
            expected=(
                "reviewer_agent_id",
                "verdict",
                "recommended_metric_name",
                "recommended_metric_value",
                "critical_issues",
                "noncritical_issues",
                "fix_requests",
                "summary",
            ),
        )
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
class NextAction:
    phase: Phase
    next_agent_ids: tuple[str, ...]
    expected_artifact_type: ArtifactType
    required_receipts: tuple[SourceReceipt, ...]
    instruction_source_manifest: InstructionSourceManifest
    source_manifest_sha256: str
    state_reference_sha256: str
    prompt_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "next_agent_ids": list(self.next_agent_ids),
            "expected_artifact_type": self.expected_artifact_type.value,
            "required_receipts": [receipt.to_dict() for receipt in self.required_receipts],
            "instruction_source_manifest": self.instruction_source_manifest.to_dict(),
            "source_manifest_sha256": self.source_manifest_sha256,
            "state_reference_sha256": self.state_reference_sha256,
            "prompt_text": self.prompt_text,
        }


@dataclass(frozen=True, slots=True)
class PhaseTarget:
    agent_ids: tuple[str, ...]
    artifact_type: ArtifactType


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
            "research_mode",
            "mode_rationale",
            "burned_theory_families",
        ],
        "field_types": {
            "baseline_metric": "string",
            "current_best_metric": "string",
            "recent_experiment_outcomes": "array[string]",
            "prior_findings": "array[string]",
            "open_proposals": "array[string]",
            "hard_constraints": "array[string]",
            "available_data_sources": "array[string]",
            "loaded_quantipy_sources": "array[string]",
            "research_mode": "enum[alpha_research,data_infra_g0]",
            "mode_rationale": "string",
            "burned_theory_families": "array[string]",
        },
        "shape_constraints": [
            "Use exactly the listed keys and no extra keys",
            "Do not use nested objects in the context_packet artifact",
            "Every array item must be a string",
        ],
    },
    ArtifactType.DEBATE_RESULT: {
        "required_fields": [
            "round_number",
            "submissions[5]",
            "submissions[*].example_tickers",
            "submissions[*].compute_fit",
        ],
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
            "universe_plan|null",
        ]
    },
    ArtifactType.IMPLEMENTATION_RESULT: {
        "required_fields": [
            "summary",
            "workspace_path",
            "commit_sha",
            "module_path",
            "notebook_path",
            "tests_added_or_updated",
            "commands_run",
            "experiment_manifest_path",
            "experiment_manifest_sha256",
            "compute_fit",
            "price_hydration_scope_preflight",
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
            "data_coverage",
            "platform_coverage_validation",
            "infra_gate_outcome",
            "infra_rationale",
            "universe_verification_receipt",
            "price_hydration_receipt",
            "quantipy_experiment_evidence",
            "quantipy_execution_not_started",
        ],
        "mode_requirements": {
            ResearchMode.ALPHA_RESEARCH.value: {
                "infra_gate_outcome": None,
                "infra_rationale": None,
            },
            ResearchMode.DATA_INFRA_G0.value: {
                "infra_gate_outcome": "required",
                "infra_rationale": "required",
            },
        },
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
            "workspace_path",
            "commit_sha",
            "fixes_applied",
            "tests_rerun",
            "remaining_issues",
            "price_hydration_scope_preflight",
        ]
    },
    ArtifactType.FINAL_DECISION: {
        "required_fields": [
            "experiment_id",
            "decision",
            "recommended_metric_name",
            "recommended_metric_value",
            "reviewer_verdict",
            "rationale",
            "log_summary",
            "continue_loop",
            "memory_write_required",
            "infra_rationale",
        ],
        "mode_requirements": {
            ResearchMode.ALPHA_RESEARCH.value: {"infra_rationale": None},
            ResearchMode.DATA_INFRA_G0.value: {"infra_rationale": "required"},
            "no_consensus": {
                "memory_write_required": False,
                "infra_rationale": None,
            },
        },
    },
    ArtifactType.MEMORY_WRITE: {
        "required_fields": ["memory_verification_receipt"],
        "receipt_type": "memory_verification_receipt",
    },
    ArtifactType.NEXT_ITERATION: {"required_fields": ["start_next_iteration"]},
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
    research_mode: ResearchMode
    mode_rationale: str
    burned_theory_families: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> ContextPacketArtifact:
        data = _ensure_mapping(raw, label="context_packet")
        _require_exact_keys(
            data,
            label="context_packet",
            expected=(
                "baseline_metric",
                "current_best_metric",
                "recent_experiment_outcomes",
                "prior_findings",
                "open_proposals",
                "hard_constraints",
                "available_data_sources",
                "loaded_quantipy_sources",
                "research_mode",
                "mode_rationale",
                "burned_theory_families",
            ),
        )
        return cls(
            baseline_metric=_require_str(data, "baseline_metric"),
            current_best_metric=_require_str(data, "current_best_metric"),
            recent_experiment_outcomes=_require_string_list(data, "recent_experiment_outcomes"),
            prior_findings=_require_string_list(data, "prior_findings"),
            open_proposals=_require_string_list(data, "open_proposals"),
            hard_constraints=_require_string_list(data, "hard_constraints"),
            available_data_sources=_require_string_list(data, "available_data_sources"),
            loaded_quantipy_sources=_require_string_list(data, "loaded_quantipy_sources"),
            research_mode=ResearchMode(_require_str(data, "research_mode")),
            mode_rationale=_require_str(data, "mode_rationale"),
            burned_theory_families=tuple(
                _normalise_identifier(family)
                for family in _require_string_list(data, "burned_theory_families")
            ),
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
            "research_mode": self.research_mode.value,
            "mode_rationale": self.mode_rationale,
            "burned_theory_families": list(self.burned_theory_families),
        }


@dataclass(frozen=True, slots=True)
class PriceHydrationScopePreflight:
    member_union_count: int
    experiment_start: str
    experiment_end: str
    timeframe: str
    market_hours: str
    session_count: int
    planned_symbol_sessions: int
    within_budget: bool

    @classmethod
    def from_dict(cls, raw: object) -> PriceHydrationScopePreflight:
        data = _ensure_mapping(raw, label="price_hydration_scope_preflight")
        _require_exact_keys(
            data,
            label="price_hydration_scope_preflight",
            expected=(
                "member_union_count",
                "experiment_start",
                "experiment_end",
                "timeframe",
                "market_hours",
                "session_count",
                "planned_symbol_sessions",
                "within_budget",
            ),
        )
        receipt = cls(
            member_union_count=_require_int(data, "member_union_count"),
            experiment_start=_require_iso_date(data, "experiment_start"),
            experiment_end=_require_iso_date(data, "experiment_end"),
            timeframe=_require_str(data, "timeframe"),
            market_hours=_require_str(data, "market_hours"),
            session_count=_require_int(data, "session_count"),
            planned_symbol_sessions=_require_int(data, "planned_symbol_sessions"),
            within_budget=_require_bool(data, "within_budget"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.experiment_start, label="experiment_start")
        _validate_iso_date_value(self.experiment_end, label="experiment_end")
        if self.experiment_start > self.experiment_end:
            raise AutoresearchValidationError("price preflight experiment range is invalid")
        if self.member_union_count <= 0:
            raise AutoresearchValidationError("price preflight member_union_count must be positive")
        if self.session_count <= 0:
            raise AutoresearchValidationError("price preflight session_count must be positive")
        expected = self.member_union_count * self.session_count
        if self.planned_symbol_sessions != expected:
            raise AutoresearchValidationError(
                "price preflight planned_symbol_sessions must equal "
                "member_union_count * session_count"
            )
        expected_within_budget = expected <= MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS
        if self.within_budget is not expected_within_budget:
            raise AutoresearchValidationError(
                "price preflight within_budget must match the alpha hydration budget"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
            "session_count": self.session_count,
            "planned_symbol_sessions": self.planned_symbol_sessions,
            "within_budget": self.within_budget,
        }


@dataclass(frozen=True, slots=True)
class FixResultArtifact:
    trigger_phase: FixTriggerPhase
    summary: str
    workspace_path: str
    commit_sha: str
    fixes_applied: tuple[str, ...]
    tests_rerun: tuple[str, ...]
    remaining_issues: tuple[str, ...]
    price_hydration_scope_preflight: PriceHydrationScopePreflight | None = None

    @classmethod
    def from_dict(cls, raw: object) -> FixResultArtifact:
        data = _ensure_mapping(raw, label="fix_result")
        _require_exact_keys(
            data,
            label="fix_result",
            expected=(
                "trigger_phase",
                "summary",
                "workspace_path",
                "commit_sha",
                "fixes_applied",
                "tests_rerun",
                "remaining_issues",
                "price_hydration_scope_preflight",
            ),
        )
        preflight_raw = data.get("price_hydration_scope_preflight")
        artifact = cls(
            trigger_phase=FixTriggerPhase(_require_str(data, "trigger_phase")),
            summary=_require_str(data, "summary"),
            workspace_path=_require_workspace_path(data, "workspace_path"),
            commit_sha=_require_str(data, "commit_sha"),
            fixes_applied=_require_string_list(data, "fixes_applied"),
            tests_rerun=_require_string_list(data, "tests_rerun"),
            remaining_issues=_require_string_list(data, "remaining_issues"),
            price_hydration_scope_preflight=(
                PriceHydrationScopePreflight.from_dict(preflight_raw)
                if preflight_raw is not None
                else None
            ),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        _validate_workspace_path(self.workspace_path, label="fix_result workspace_path")
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.commit_sha):
            raise AutoresearchValidationError("fix_result commit_sha must be a Git commit SHA")

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger_phase": self.trigger_phase.value,
            "summary": self.summary,
            "workspace_path": self.workspace_path,
            "commit_sha": self.commit_sha,
            "fixes_applied": list(self.fixes_applied),
            "tests_rerun": list(self.tests_rerun),
            "remaining_issues": list(self.remaining_issues),
            "price_hydration_scope_preflight": (
                self.price_hydration_scope_preflight.to_dict()
                if self.price_hydration_scope_preflight is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class FinalDecisionArtifact:
    experiment_id: str
    decision: FinalDecision
    recommended_metric_name: str
    recommended_metric_value: float | None
    reviewer_verdict: FinalReviewerVerdict
    rationale: str
    log_summary: str
    continue_loop: bool
    memory_write_required: bool
    infra_rationale: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> FinalDecisionArtifact:
        data = _ensure_mapping(raw, label="final_decision")
        _require_exact_keys(
            data,
            label="final_decision",
            expected=(
                "experiment_id",
                "decision",
                "recommended_metric_name",
                "recommended_metric_value",
                "reviewer_verdict",
                "rationale",
                "log_summary",
                "continue_loop",
                "memory_write_required",
                "infra_rationale",
            ),
        )
        metric_value = data.get("recommended_metric_value")
        if metric_value is not None and (
            isinstance(metric_value, bool) or not isinstance(metric_value, int | float)
        ):
            raise AutoresearchValidationError("recommended_metric_value must be numeric or null")
        infra_rationale = data.get("infra_rationale")
        if infra_rationale is not None and not isinstance(infra_rationale, str):
            raise AutoresearchValidationError("infra_rationale must be a string or null")
        return cls(
            experiment_id=_require_canonical_identifier(data, "experiment_id"),
            decision=FinalDecision(_require_str(data, "decision")),
            recommended_metric_name=_require_str(data, "recommended_metric_name"),
            recommended_metric_value=float(metric_value) if metric_value is not None else None,
            reviewer_verdict=FinalReviewerVerdict(_require_str(data, "reviewer_verdict")),
            rationale=_require_str(data, "rationale"),
            log_summary=_require_str(data, "log_summary"),
            continue_loop=_require_bool(data, "continue_loop"),
            memory_write_required=_require_bool(data, "memory_write_required"),
            infra_rationale=infra_rationale.strip() if isinstance(infra_rationale, str) else None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "decision": self.decision.value,
            "recommended_metric_name": self.recommended_metric_name,
            "recommended_metric_value": self.recommended_metric_value,
            "reviewer_verdict": self.reviewer_verdict.value,
            "rationale": self.rationale,
            "log_summary": self.log_summary,
            "continue_loop": self.continue_loop,
            "memory_write_required": self.memory_write_required,
            "infra_rationale": self.infra_rationale,
        }


@dataclass(frozen=True, slots=True)
class MemoryVerificationReceipt:
    experiment_id: str
    kg_path: str
    predicates: tuple[str, ...]
    verified_rows_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "kg_path": self.kg_path,
            "predicates": list(self.predicates),
            "verified_rows_digest": self.verified_rows_digest,
        }

    @classmethod
    def from_dict(cls, raw: object) -> MemoryVerificationReceipt:
        data = _ensure_mapping(raw, label="memory_verification_receipt")
        _require_exact_keys(
            data,
            label="memory_verification_receipt",
            expected=("experiment_id", "kg_path", "predicates", "verified_rows_digest"),
        )
        digest = _require_str(data, "verified_rows_digest")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AutoresearchValidationError("verified_rows_digest must be a SHA-256 hex digest")
        return cls(
            experiment_id=_require_canonical_identifier(data, "experiment_id"),
            kg_path=_require_str(data, "kg_path"),
            predicates=tuple(
                sorted(
                    _normalise_predicate(item) for item in _require_string_list(data, "predicates")
                )
            ),
            verified_rows_digest=digest,
        )
