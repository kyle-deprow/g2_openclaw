"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
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


class ResearchMode(StrEnum):
    """The only two mutually exclusive purposes for an iteration."""

    ALPHA_RESEARCH = "alpha_research"
    DATA_INFRA_G0 = "data_infra_g0"


class ConsensusStatus(StrEnum):
    MAJORITY = "MAJORITY"
    NO_CONSENSUS = "NO_CONSENSUS"


class VerificationStatus(StrEnum):
    PASS = "PASS"
    BUG_SIGNAL = "BUG_SIGNAL"
    TEST_FAILURE = "TEST_FAILURE"


class InfraGateOutcome(StrEnum):
    GATE_PASSED = "GATE_PASSED"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL PASS"
    FAIL = "FAIL"


class FinalReviewerVerdict(StrEnum):
    """The reviewer outcome persisted with a final decision."""

    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class FinalDecision(StrEnum):
    KEEP = "KEEP"
    SIGNIFICANT_KEEP = "SIGNIFICANT KEEP"
    STRONG_KEEP = "STRONG KEEP"
    DISCARD = "DISCARD"
    CRASH = "CRASH"
    NO_CONSENSUS = "NO_CONSENSUS"
    INFRA_REPAIRED = "INFRA_REPAIRED"
    INFRA_BLOCKED = "INFRA_BLOCKED"


class FixTriggerPhase(StrEnum):
    VERIFICATION = "verification"
    REVIEW = "review"


KEEP_DECISIONS = frozenset(
    {FinalDecision.KEEP, FinalDecision.SIGNIFICANT_KEEP, FinalDecision.STRONG_KEEP}
)
MEMPALACE_CONFIG_PLACEHOLDER = "PLACEHOLDER_RESOLVED_BY_PUSH_SCRIPT"
MEMPALACE_FULL_SERVER_ID = "mempalace"
MEMPALACE_READONLY_SERVER_ID = "mempalace-readonly"
MEMPALACE_READONLY_WRAPPER_BASENAME = "mempalace-readonly-server.py"
MEMPALACE_KG_OBJECT_MAX_LENGTH = 128
MEMPALACE_KG_OBJECT_SHA256_LENGTH = 64
# OpenClaw policy checks compare internal MCP tool.name ids such as
# "mempalace__mempalace_search". Codex-facing docs and traces show dotted
# display ids such as "mempalace.mempalace_add_drawer" or
# "mempalace-readonly.mempalace_search" after adaptation.
MEMPALACE_POLICY_TOOL_PREFIX = "mempalace__"
MEMPALACE_FULL_SERVER_DISPLAY_NAMESPACE = MEMPALACE_FULL_SERVER_ID
MEMPALACE_READONLY_DISPLAY_NAMESPACE = MEMPALACE_READONLY_SERVER_ID
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
MEMPALACE_OBSOLETE_MUTATION_TOOL_ID_PREFIXES = (
    "",
    "mcp__mempalace__",
    f"{MEMPALACE_FULL_SERVER_DISPLAY_NAMESPACE}.",
)
MEMPALACE_READONLY_TOOL_NAMES = (
    "mempalace_status",
    "mempalace_search",
    "mempalace_get_drawer",
    "mempalace_list_drawers",
    "mempalace_list_wings",
    "mempalace_list_rooms",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    "mempalace_diary_read",
    "mempalace_kg_query",
    "mempalace_kg_timeline",
    "mempalace_kg_stats",
    "mempalace_traverse",
    "mempalace_find_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_graph_stats",
    "mempalace_list_tunnels",
    "mempalace_list_hallways",
    "mempalace_memories_filed_away",
)


def _compile_mempalace_policy_tool_ids(
    tool_names: Sequence[str],
) -> tuple[str, ...]:
    return tuple(f"{MEMPALACE_POLICY_TOOL_PREFIX}{tool_name}" for tool_name in tool_names)


def _compile_mempalace_codex_display_tool_ids(
    tool_names: Sequence[str],
    *,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(f"{namespace}.{tool_name}" for tool_name in tool_names)


def _compile_mempalace_alias_tool_ids(
    tool_names: Sequence[str],
    *,
    prefixes: Sequence[str] = MEMPALACE_OBSOLETE_MUTATION_TOOL_ID_PREFIXES,
) -> tuple[str, ...]:
    expanded: list[str] = []
    for prefix in prefixes:
        expanded.extend(f"{prefix}{tool_name}" for tool_name in tool_names)
    return tuple(expanded)


MEMPALACE_MUTATION_DENY_TOOL_IDS = _compile_mempalace_policy_tool_ids(MEMPALACE_MUTATION_TOOLS)
MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_IDS = _compile_mempalace_alias_tool_ids(
    MEMPALACE_MUTATION_TOOLS
)
MEMPALACE_READONLY_DISPLAY_TOOL_IDS = _compile_mempalace_codex_display_tool_ids(
    MEMPALACE_READONLY_TOOL_NAMES,
    namespace=MEMPALACE_READONLY_DISPLAY_NAMESPACE,
)
MEMPALACE_MUTATION_DENY_TOOL_ID_SET = frozenset(MEMPALACE_MUTATION_DENY_TOOL_IDS)
MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_ID_SET = frozenset(
    MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_IDS
)
DEFAULT_ALLOWED_TARGET_STATUS_LINES = ("?? docs/quantipy_experiment_mempalace_preload.md",)


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


def _require_string_sequence(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise AutoresearchValidationError(f"{label} must be a list of strings")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AutoresearchValidationError(f"{label} must be a list of strings")
        items.append(item)
    return tuple(items)


def _optional_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    if field_name not in raw:
        return ()
    return _require_string_list(raw, field_name)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise_identifier(value: str) -> str:
    """Return the sole documented identifier form: lowercase kebab-case."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", normalized):
        raise AutoresearchValidationError(
            "identifier must normalize to lowercase kebab-case beginning with a letter"
        )
    return normalized


def _require_canonical_identifier(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value:
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
        raise AutoresearchValidationError(
            f"{field_name} must be canonical lowercase kebab-case beginning with a letter"
        )
    return value


def _normalise_predicate(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def standardize_mempalace_kg_object(value: str) -> str:
    """Normalize a KG object and compact it to MemPalace's 128-character limit."""
    normalized = _normalise_predicate(value)
    if len(normalized) <= MEMPALACE_KG_OBJECT_MAX_LENGTH:
        return normalized
    prefix_length = MEMPALACE_KG_OBJECT_MAX_LENGTH - MEMPALACE_KG_OBJECT_SHA256_LENGTH - 1
    return f"{normalized[:prefix_length]}_{_sha256_text(normalized)}"


def validate_target_worktree_clean(
    status_lines: Sequence[str],
    *,
    allowed_status_lines: Sequence[str] = DEFAULT_ALLOWED_TARGET_STATUS_LINES,
) -> None:
    """Fail if the target repo has unapproved dirty files.

    The autoresearch loop may choose any strategy, but each stage must start
    from an uncontaminated target repo. Known persistent local docs can be
    allowlisted explicitly; crash residue and late writer output cannot.
    """
    allowed = set(allowed_status_lines)
    unexpected = tuple(line for line in status_lines if line and line not in allowed)
    if unexpected:
        details = "\n".join(f"- {line}" for line in unexpected)
        raise AutoresearchValidationError(
            "target repo worktree is dirty with unapproved changes:\n"
            f"{details}\n"
            "Stop stale writers and clean or commit the target repo before "
            "launching the next autoresearch stage."
        )


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
    pm: StageAgentPolicy
    main_interface: StageAgentPolicy
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
            self.pm.to_summary(),
            self.main_interface.to_summary(),
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
    research_mode: ResearchMode
    mode_rationale: str
    burned_theory_families: tuple[str, ...]

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
    materially_new_evidence: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> DebateSubmission:
        data = _ensure_mapping(raw, label="debate_submission")
        exemption = data.get("materially_new_evidence")
        if exemption is not None and not isinstance(exemption, str):
            raise AutoresearchValidationError("materially_new_evidence must be a string or null")
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
            materially_new_evidence=exemption.strip() if isinstance(exemption, str) else None,
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
            "materially_new_evidence": self.materially_new_evidence,
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
    workspace_path: str
    commit_sha: str
    module_path: str
    notebook_path: str
    tests_added_or_updated: tuple[str, ...]
    commands_run: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> ImplementationResultArtifact:
        data = _ensure_mapping(raw, label="implementation_result")
        artifact = cls(
            summary=_require_str(data, "summary"),
            workspace_path=_require_str(data, "workspace_path"),
            commit_sha=_require_str(data, "commit_sha"),
            module_path=_require_str(data, "module_path"),
            notebook_path=_require_str(data, "notebook_path"),
            tests_added_or_updated=_require_string_list(data, "tests_added_or_updated"),
            commands_run=_require_string_list(data, "commands_run"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        path = Path(self.workspace_path).expanduser()
        if not path.is_absolute():
            raise AutoresearchValidationError(
                "implementation_result workspace_path must be absolute"
            )
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.commit_sha):
            raise AutoresearchValidationError(
                "implementation_result commit_sha must be a Git commit SHA"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "workspace_path": self.workspace_path,
            "commit_sha": self.commit_sha,
            "module_path": self.module_path,
            "notebook_path": self.notebook_path,
            "tests_added_or_updated": list(self.tests_added_or_updated),
            "commands_run": list(self.commands_run),
        }


def _require_iso_date(raw: Mapping[str, object], field_name: str) -> str:
    value = _require_str(raw, field_name)
    try:
        # ISO date lexical ordering is also chronological ordering.
        from datetime import date

        date.fromisoformat(value)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{field_name} must be an ISO-8601 date") from exc
    return value


@dataclass(frozen=True, slots=True)
class CoverageReceipt:
    symbol: str
    declared_intended_start: str
    declared_intended_end: str
    actual_common_start: str
    actual_common_end: str
    oos_start: str
    oos_end: str
    expected_trading_days: int
    actual_trading_days: int
    coverage_percent: float
    missing_reason: str | None
    default_fold_count: int
    fallback_fold_count: int
    cap_provenance_available: bool
    fixed_sleeve_local_data: bool

    @classmethod
    def from_dict(cls, raw: object) -> CoverageReceipt:
        data = _ensure_mapping(raw, label="coverage_receipt")
        missing_reason = data.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            raise AutoresearchValidationError("missing_reason must be a string or null")
        receipt = cls(
            symbol=_require_str(data, "symbol"),
            declared_intended_start=_require_iso_date(data, "declared_intended_start"),
            declared_intended_end=_require_iso_date(data, "declared_intended_end"),
            actual_common_start=_require_iso_date(data, "actual_common_start"),
            actual_common_end=_require_iso_date(data, "actual_common_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            expected_trading_days=_require_int(data, "expected_trading_days"),
            actual_trading_days=_require_int(data, "actual_trading_days"),
            coverage_percent=_require_float(data, "coverage_percent"),
            missing_reason=missing_reason.strip() if isinstance(missing_reason, str) else None,
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
            cap_provenance_available=_require_bool(data, "cap_provenance_available"),
            fixed_sleeve_local_data=_require_bool(data, "fixed_sleeve_local_data"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_coverage_values(self, label=f"coverage receipt for {self.symbol}")

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "declared_intended_start": self.declared_intended_start,
            "declared_intended_end": self.declared_intended_end,
            "actual_common_start": self.actual_common_start,
            "actual_common_end": self.actual_common_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "expected_trading_days": self.expected_trading_days,
            "actual_trading_days": self.actual_trading_days,
            "coverage_percent": self.coverage_percent,
            "missing_reason": self.missing_reason,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
            "cap_provenance_available": self.cap_provenance_available,
            "fixed_sleeve_local_data": self.fixed_sleeve_local_data,
        }


def _validate_coverage_values(receipt: CoverageReceipt, *, label: str) -> None:
    if not (
        receipt.declared_intended_start
        <= receipt.actual_common_start
        <= receipt.actual_common_end
        <= receipt.declared_intended_end
    ):
        raise AutoresearchValidationError(f"{label} actual common range must fit intended range")
    if not (
        receipt.actual_common_start
        <= receipt.oos_start
        <= receipt.oos_end
        <= receipt.actual_common_end
    ):
        raise AutoresearchValidationError(f"{label} OOS range must fit actual common range")
    if (
        receipt.expected_trading_days <= 0
        or not 0 <= receipt.actual_trading_days <= receipt.expected_trading_days
    ):
        raise AutoresearchValidationError(f"{label} trading day counts are invalid")
    if not 0.0 <= receipt.coverage_percent <= 100.0:
        raise AutoresearchValidationError(f"{label} coverage_percent must be between 0 and 100")
    expected_percent = receipt.actual_trading_days / receipt.expected_trading_days * 100.0
    if abs(receipt.coverage_percent - expected_percent) > 0.01:
        raise AutoresearchValidationError(f"{label} coverage_percent must match trading day counts")
    if receipt.actual_trading_days < receipt.expected_trading_days and not receipt.missing_reason:
        raise AutoresearchValidationError(
            f"{label} missing_reason is required for missing trading days"
        )
    if receipt.actual_trading_days == receipt.expected_trading_days and receipt.missing_reason:
        raise AutoresearchValidationError(
            f"{label} missing_reason is only valid for missing trading days"
        )
    if receipt.default_fold_count < 0 or receipt.fallback_fold_count < 0:
        raise AutoresearchValidationError(f"{label} fold counts must be non-negative")
    if receipt.fixed_sleeve_local_data and receipt.cap_provenance_available:
        raise AutoresearchValidationError(
            f"{label} fixed_sleeve_local_data cannot claim cap_provenance_available"
        )


@dataclass(frozen=True, slots=True)
class AggregateCoverageReceipt:
    declared_intended_start: str
    declared_intended_end: str
    actual_common_start: str
    actual_common_end: str
    oos_start: str
    oos_end: str
    expected_trading_days: int
    actual_trading_days: int
    coverage_percent: float
    missing_reason: str | None
    default_fold_count: int
    fallback_fold_count: int
    cap_provenance_available: bool
    fixed_sleeve_local_data: bool
    per_symbol: tuple[CoverageReceipt, ...]

    @classmethod
    def from_dict(cls, raw: object) -> AggregateCoverageReceipt:
        data = _ensure_mapping(raw, label="aggregate_coverage_receipt")
        symbols_raw = data.get("per_symbol")
        if not isinstance(symbols_raw, Sequence) or isinstance(symbols_raw, str | bytes):
            raise AutoresearchValidationError("per_symbol must be a list")
        missing_reason = data.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            raise AutoresearchValidationError("missing_reason must be a string or null")
        receipt = cls(
            declared_intended_start=_require_iso_date(data, "declared_intended_start"),
            declared_intended_end=_require_iso_date(data, "declared_intended_end"),
            actual_common_start=_require_iso_date(data, "actual_common_start"),
            actual_common_end=_require_iso_date(data, "actual_common_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            expected_trading_days=_require_int(data, "expected_trading_days"),
            actual_trading_days=_require_int(data, "actual_trading_days"),
            coverage_percent=_require_float(data, "coverage_percent"),
            missing_reason=missing_reason.strip() if isinstance(missing_reason, str) else None,
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
            cap_provenance_available=_require_bool(data, "cap_provenance_available"),
            fixed_sleeve_local_data=_require_bool(data, "fixed_sleeve_local_data"),
            per_symbol=tuple(CoverageReceipt.from_dict(item) for item in symbols_raw),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if not self.per_symbol:
            raise AutoresearchValidationError(
                "aggregate coverage requires at least one per-symbol receipt"
            )
        synthetic = CoverageReceipt(
            symbol="aggregate",
            declared_intended_start=self.declared_intended_start,
            declared_intended_end=self.declared_intended_end,
            actual_common_start=self.actual_common_start,
            actual_common_end=self.actual_common_end,
            oos_start=self.oos_start,
            oos_end=self.oos_end,
            expected_trading_days=self.expected_trading_days,
            actual_trading_days=self.actual_trading_days,
            coverage_percent=self.coverage_percent,
            missing_reason=self.missing_reason,
            default_fold_count=self.default_fold_count,
            fallback_fold_count=self.fallback_fold_count,
            cap_provenance_available=self.cap_provenance_available,
            fixed_sleeve_local_data=self.fixed_sleeve_local_data,
        )
        _validate_coverage_values(synthetic, label="aggregate coverage")
        if len({receipt.symbol for receipt in self.per_symbol}) != len(self.per_symbol):
            raise AutoresearchValidationError("aggregate coverage cannot contain duplicate symbols")
        for receipt in self.per_symbol:
            receipt.validate()
            if (
                receipt.declared_intended_start != self.declared_intended_start
                or receipt.declared_intended_end != self.declared_intended_end
            ):
                raise AutoresearchValidationError(
                    "aggregate declared intended range must match every per-symbol receipt"
                )
            if receipt.fixed_sleeve_local_data != self.fixed_sleeve_local_data:
                raise AutoresearchValidationError(
                    "per-symbol fixed_sleeve_local_data must match aggregate"
                )
            if receipt.cap_provenance_available != self.cap_provenance_available:
                raise AutoresearchValidationError("per-symbol cap provenance must match aggregate")
        expected_common_start = max(receipt.actual_common_start for receipt in self.per_symbol)
        expected_common_end = min(receipt.actual_common_end for receipt in self.per_symbol)
        if self.actual_common_start != expected_common_start:
            raise AutoresearchValidationError(
                "aggregate actual_common_start must equal the latest per-symbol actual start"
            )
        if self.actual_common_end != expected_common_end:
            raise AutoresearchValidationError(
                "aggregate actual_common_end must equal the earliest per-symbol actual end"
            )
        expected_oos_start = max(receipt.oos_start for receipt in self.per_symbol)
        expected_oos_end = min(receipt.oos_end for receipt in self.per_symbol)
        if self.oos_start != expected_oos_start or self.oos_end != expected_oos_end:
            raise AutoresearchValidationError(
                "aggregate OOS range must equal the common per-symbol OOS intersection"
            )
        if any(
            receipt.expected_trading_days != self.expected_trading_days
            for receipt in self.per_symbol
        ):
            raise AutoresearchValidationError(
                "aggregate expected_trading_days must match every per-symbol common calendar"
            )
        if any(
            receipt.actual_trading_days != self.actual_trading_days for receipt in self.per_symbol
        ):
            raise AutoresearchValidationError(
                "aggregate actual_trading_days must match every per-symbol common calendar"
            )
        if any(receipt.coverage_percent != self.coverage_percent for receipt in self.per_symbol):
            raise AutoresearchValidationError(
                "aggregate coverage_percent must match every per-symbol common calendar"
            )
        if any(receipt.missing_reason != self.missing_reason for receipt in self.per_symbol):
            raise AutoresearchValidationError(
                "aggregate missing_reason must match every per-symbol common calendar"
            )
        expected_default_folds = min(receipt.default_fold_count for receipt in self.per_symbol)
        expected_fallback_folds = min(receipt.fallback_fold_count for receipt in self.per_symbol)
        if self.default_fold_count != expected_default_folds:
            raise AutoresearchValidationError(
                "aggregate default_fold_count must equal the fewest per-symbol default folds"
            )
        if self.fallback_fold_count != expected_fallback_folds:
            raise AutoresearchValidationError(
                "aggregate fallback_fold_count must equal the fewest per-symbol fallback folds"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "declared_intended_start": self.declared_intended_start,
            "declared_intended_end": self.declared_intended_end,
            "actual_common_start": self.actual_common_start,
            "actual_common_end": self.actual_common_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "expected_trading_days": self.expected_trading_days,
            "actual_trading_days": self.actual_trading_days,
            "coverage_percent": self.coverage_percent,
            "missing_reason": self.missing_reason,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
            "cap_provenance_available": self.cap_provenance_available,
            "fixed_sleeve_local_data": self.fixed_sleeve_local_data,
            "per_symbol": [receipt.to_dict() for receipt in self.per_symbol],
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
    data_coverage: AggregateCoverageReceipt
    infra_gate_outcome: InfraGateOutcome | None = None
    infra_rationale: str | None = None

    @classmethod
    def from_dict(
        cls, raw: object, *, mode: ResearchMode | None = None
    ) -> VerificationResultArtifact:
        data = _ensure_mapping(raw, label="verification_result")
        infra_gate_raw = data.get("infra_gate_outcome")
        infra_rationale = data.get("infra_rationale")
        if infra_gate_raw is not None and not isinstance(infra_gate_raw, str):
            raise AutoresearchValidationError("infra_gate_outcome must be a string or null")
        if infra_rationale is not None and not isinstance(infra_rationale, str):
            raise AutoresearchValidationError("infra_rationale must be a string or null")
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
            data_coverage=AggregateCoverageReceipt.from_dict(data.get("data_coverage")),
            infra_gate_outcome=InfraGateOutcome(infra_gate_raw)
            if infra_gate_raw is not None
            else None,
            infra_rationale=infra_rationale.strip() if isinstance(infra_rationale, str) else None,
        )
        artifact.validate(mode=mode)
        return artifact

    def validate(
        self,
        *,
        mode: ResearchMode | None = None,
        infra_gate_outcome: InfraGateOutcome | None = None,
    ) -> None:
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
        self.data_coverage.validate()
        outcome = infra_gate_outcome if infra_gate_outcome is not None else self.infra_gate_outcome
        if mode is ResearchMode.DATA_INFRA_G0 and (outcome is None or not self.infra_rationale):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 verification requires infra_gate_outcome and infra_rationale"
            )
        if mode is ResearchMode.ALPHA_RESEARCH and (outcome is not None or self.infra_rationale):
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH verification cannot contain infrastructure gate outcomes"
            )
        if (
            mode is ResearchMode.ALPHA_RESEARCH
            and self.data_coverage.fixed_sleeve_local_data
            and self.data_coverage.cap_provenance_available
        ):
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH fixed local sleeve cannot claim cap-verified universe compliance"
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
            "data_coverage": self.data_coverage.to_dict(),
            "infra_gate_outcome": self.infra_gate_outcome.value
            if self.infra_gate_outcome is not None
            else None,
            "infra_rationale": self.infra_rationale,
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
    workspace_path: str
    commit_sha: str
    fixes_applied: tuple[str, ...]
    tests_rerun: tuple[str, ...]
    remaining_issues: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> FixResultArtifact:
        data = _ensure_mapping(raw, label="fix_result")
        artifact = cls(
            trigger_phase=FixTriggerPhase(_require_str(data, "trigger_phase")),
            summary=_require_str(data, "summary"),
            workspace_path=_require_str(data, "workspace_path"),
            commit_sha=_require_str(data, "commit_sha"),
            fixes_applied=_require_string_list(data, "fixes_applied"),
            tests_rerun=_require_string_list(data, "tests_rerun"),
            remaining_issues=_require_string_list(data, "remaining_issues"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        path = Path(self.workspace_path).expanduser()
        if not path.is_absolute():
            raise AutoresearchValidationError("fix_result workspace_path must be absolute")
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
    mode: ResearchMode | None = None
    memory_verification_receipt: MemoryVerificationReceipt | None = None

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
        receipt_raw = data.get("memory_verification_receipt")
        pending_fix_trigger_raw = data.get("pending_fix_trigger")
        if pending_fix_trigger_raw is not None and not isinstance(pending_fix_trigger_raw, str):
            raise AutoresearchValidationError("pending_fix_trigger must be a string or null")
        if "mode" not in data:
            raise AutoresearchValidationError(
                "mode must be explicit in persisted autoresearch state"
            )
        mode_raw = data.get("mode")
        if mode_raw is not None and not isinstance(mode_raw, str):
            raise AutoresearchValidationError("mode must be a string or null")

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
                "verification_history",
                lambda item: VerificationResultArtifact.from_dict(
                    item, mode=ResearchMode(mode_raw) if mode_raw is not None else None
                ),
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
            mode=ResearchMode(mode_raw) if mode_raw is not None else None,
            memory_verification_receipt=MemoryVerificationReceipt.from_dict(receipt_raw)
            if receipt_raw is not None
            else None,
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
            "mode": self.mode.value if self.mode is not None else None,
            "memory_verification_receipt": self.memory_verification_receipt.to_dict()
            if self.memory_verification_receipt is not None
            else None,
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
            "research_mode",
            "mode_rationale",
            "burned_theory_families",
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
            "workspace_path",
            "commit_sha",
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
            "data_coverage",
            "infra_gate_outcome",
            "infra_rationale",
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
    plugins = _ensure_mapping(config.get("plugins"), label="plugins")
    try:
        plugin_allow = _require_string_list(plugins, "allow")
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError("plugins.allow must explicitly include codex") from exc
    if "codex" not in plugin_allow:
        raise AutoresearchConfigError("plugins.allow must explicitly include codex")
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
    for required_model in ("gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra"):
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
        pm=_agent_policy_from_json(agent_map, "autoresearch-pm"),
        main_interface=_agent_policy_from_json(agent_map, "main"),
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
    _validate_policy(policy, agent_map, config)
    return policy


def _validate_policy(
    policy: AutoresearchPolicy,
    agent_map: Mapping[str, Mapping[str, object]],
    config: Mapping[str, object],
) -> None:
    if policy.main_interface.model != "openai/gpt-5.4" or policy.main_interface.reasoning != "high":
        raise AutoresearchConfigError("main must be openai/gpt-5.4 with high reasoning")
    if policy.main_interface.skills:
        raise AutoresearchConfigError("main must load no skills")
    main_raw = agent_map["main"]
    if main_raw.get("subagents") is not None:
        raise AutoresearchConfigError("main must not declare a subagent allowlist")
    if policy.pm.model != "openai/gpt-5.6-sol" or policy.pm.reasoning != "high":
        raise AutoresearchConfigError("PM must be openai/gpt-5.6-sol with high reasoning")
    if (
        policy.context_curator.model != "openai/gpt-5.4"
        or policy.context_curator.reasoning != "high"
    ):
        raise AutoresearchConfigError("context-curator must be openai/gpt-5.4 with high reasoning")

    expected_debate_models = {
        "debater-microstructure": "openai/gpt-5.5",
        "debater-data": "openai/gpt-5.6-terra",
        "debater-skeptic": "openai/gpt-5.6-sol",
        "debater-theory": "openai/gpt-5.4",
        "debater-implementation": "openai/gpt-5.4",
    }
    for agent in policy.debate_agents:
        if agent.reasoning != "high":
            raise AutoresearchConfigError(f"{agent.agent_id} must use high reasoning")
        if agent.model != expected_debate_models[agent.agent_id]:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be {expected_debate_models[agent.agent_id]} "
                "with high reasoning"
            )

    if policy.consensus.model != "openai/gpt-5.6-sol" or policy.consensus.reasoning != "high":
        raise AutoresearchConfigError(
            "consensus-arbiter must be openai/gpt-5.6-sol with high reasoning"
        )
    for agent in (policy.implementer, policy.fixer):
        if agent.model != "openai/gpt-5.4" or agent.reasoning != "high":
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be openai/gpt-5.4 with high reasoning"
            )
    if policy.reviewer.model != "openai/gpt-5.6-sol" or policy.reviewer.reasoning != "high":
        raise AutoresearchConfigError("reviewer must be exactly one openai/gpt-5.6-sol high agent")
    if policy.reviewer.agent_id != "reviewer":
        raise AutoresearchConfigError("reviewer stage must be configured as agent id 'reviewer'")

    if tuple(policy.pm.skills) != ("mempalace", "autoresearch"):
        raise AutoresearchConfigError("PM must load exactly mempalace and autoresearch")
    pm_raw = agent_map["autoresearch-pm"]
    subagents = _ensure_mapping(pm_raw.get("subagents"), label="autoresearch-pm.subagents")
    allow_agents = _require_string_list(subagents, "allowAgents")
    if tuple(allow_agents) != policy.all_stage_agent_ids:
        raise AutoresearchConfigError(
            "PM allowAgents must exactly match the autoresearch stage roster"
        )
    _validate_mempalace_server_split(config, policy)
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
        # These denies must use internal OpenClaw policy ids, not the dotted
        # display ids that Codex shows in traces and docs.
        denied_tool_list = _require_string_list(tools, "deny")
        denied_tools = set(denied_tool_list)
        missing_deny = sorted(MEMPALACE_MUTATION_DENY_TOOL_ID_SET - denied_tools)
        if missing_deny:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny exact MemPalace mutation policy IDs: "
                f"{', '.join(missing_deny)}"
            )
        obsolete_aliases = sorted(MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_ID_SET & denied_tools)
        if obsolete_aliases:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must not deny obsolete MemPalace mutation aliases: "
                f"{', '.join(obsolete_aliases)}"
            )
        unexpected_deny = sorted(denied_tools - MEMPALACE_MUTATION_DENY_TOOL_ID_SET)
        if unexpected_deny:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny only canonical MemPalace mutation policy IDs: "
                f"{', '.join(unexpected_deny)}"
            )
        if tuple(denied_tool_list) != MEMPALACE_MUTATION_DENY_TOOL_IDS:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny exactly the canonical MemPalace mutation policy IDs"
            )


def _validate_mempalace_server_split(
    config: Mapping[str, object],
    policy: AutoresearchPolicy,
) -> None:
    try:
        mcp = _ensure_mapping(config.get("mcp"), label="mcp")
        servers = _ensure_mapping(mcp.get("servers"), label="mcp.servers")
        full_server = _ensure_mapping(
            servers.get(MEMPALACE_FULL_SERVER_ID),
            label=f"mcp.servers.{MEMPALACE_FULL_SERVER_ID}",
        )
        readonly_server = _ensure_mapping(
            servers.get(MEMPALACE_READONLY_SERVER_ID),
            label=f"mcp.servers.{MEMPALACE_READONLY_SERVER_ID}",
        )
        _validate_mempalace_server(
            full_server,
            server_id=MEMPALACE_FULL_SERVER_ID,
            expected_agents=(policy.pm.agent_id,),
            expected_args_prefix=("-m", "mempalace.mcp_server", "--palace"),
        )
        _validate_mempalace_server(
            readonly_server,
            server_id=MEMPALACE_READONLY_SERVER_ID,
            expected_agents=policy.all_stage_agent_ids,
            expected_args_prefix=(MEMPALACE_READONLY_WRAPPER_BASENAME, "--palace"),
        )
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc


def _validate_mempalace_server(
    server: Mapping[str, object],
    *,
    server_id: str,
    expected_agents: tuple[str, ...],
    expected_args_prefix: tuple[str, ...],
) -> None:
    _require_str(server, "command")
    args = _require_string_sequence(server.get("args"), label=f"mcp.servers.{server_id}.args")
    codex = _ensure_mapping(server.get("codex"), label=f"mcp.servers.{server_id}.codex")
    agents = _require_string_list(codex, "agents")
    if tuple(agents) != expected_agents:
        raise AutoresearchConfigError(
            f"mcp.servers.{server_id}.codex.agents must exactly match {expected_agents}"
        )
    if server_id == MEMPALACE_FULL_SERVER_ID:
        if len(args) != 4 or args[:3] != expected_args_prefix:
            raise AutoresearchConfigError(
                "mcp.servers.mempalace.args must be "
                "['-m', 'mempalace.mcp_server', '--palace', '<path>']"
            )
        if not args[3].strip():
            raise AutoresearchConfigError("mcp.servers.mempalace.args[3] must be a palace path")
        return

    if len(args) != 3 or args[1] != "--palace":
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args must be ['<wrapper>', '--palace', '<path>']"
        )
    readonly_entrypoint = args[0].strip()
    if not readonly_entrypoint:
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[0] must be a wrapper path"
        )
    if readonly_entrypoint != MEMPALACE_CONFIG_PLACEHOLDER and (
        Path(readonly_entrypoint).name != MEMPALACE_READONLY_WRAPPER_BASENAME
    ):
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[0] must point to "
            f"{MEMPALACE_READONLY_WRAPPER_BASENAME}"
        )
    if not args[2].strip():
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[2] must be a palace path"
        )


def _validate_state(state: AutoresearchState, policy: AutoresearchPolicy) -> None:
    if state.iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    if state.consensus_retry_count not in (0, 1):
        raise AutoresearchValidationError("consensus_retry_count must be 0 or 1")
    if state.context_packet is not None and state.setup is None:
        raise AutoresearchValidationError("context_packet requires setup first")
    if state.context_packet is not None and state.mode is None:
        raise AutoresearchValidationError("mode must be explicit after a context_packet exists")
    if state.context_packet is not None and state.mode is not state.context_packet.research_mode:
        raise AutoresearchValidationError("state mode must match context_packet research_mode")
    if state.debate_rounds and state.context_packet is None:
        raise AutoresearchValidationError("debate history requires a context_packet")
    if state.consensus_history and state.latest_debate is None:
        raise AutoresearchValidationError("consensus history requires a debate_result")
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
        if decision.decision is FinalDecision.NO_CONSENSUS:
            if decision.memory_write_required:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS requires final_decision.memory_write_required=false"
                )
            if state.memory_verification_receipt is not None:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS must not have a memory_verification_receipt"
                )
        elif not decision.memory_write_required:
            raise AutoresearchValidationError(
                "completed final decisions require memory_write_required=true"
            )
    if state.implementation_result and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation_result requires a majority consensus")
    if state.implementation_result:
        _validate_implementation_workspace(state, state.implementation_result)
    if state.verification_history and state.implementation_result is None:
        raise AutoresearchValidationError("verification history requires an implementation_result")
    if state.review_history and not state.verification_history:
        raise AutoresearchValidationError("review history requires a verification_result")
    if state.pending_fix_trigger is not None and state.phase is not Phase.FIX_TEST:
        raise AutoresearchValidationError("pending_fix_trigger is only valid during fix_test")
    if state.final_decision is not None and state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError("final_decision requires repeat phase")
    for debate in state.debate_rounds:
        _validate_debate_result(debate, policy, mode=state.mode, context=state.context_packet)
    for verification in state.verification_history:
        verification.validate(mode=state.mode)
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


def _validate_debate_result(
    debate: DebateResultArtifact,
    policy: AutoresearchPolicy,
    *,
    mode: ResearchMode | None = None,
    context: ContextPacketArtifact | None = None,
) -> None:
    expected_ids = set(policy.debate_agent_ids)
    actual_ids = {submission.agent_id for submission in debate.submissions}
    if actual_ids != expected_ids:
        raise AutoresearchValidationError(
            "debate_result must contain exactly the configured five debate agents"
        )
    if mode is ResearchMode.ALPHA_RESEARCH and context is not None:
        burned = set(context.burned_theory_families)
        for submission in debate.submissions:
            family = _normalise_identifier(submission.theory_family)
            if family in burned and not submission.materially_new_evidence:
                raise AutoresearchValidationError(
                    "alpha debate theory_family is burned and requires materially_new_evidence"
                )


def _validate_review_result(review: ReviewResultArtifact, policy: AutoresearchPolicy) -> None:
    if review.reviewer_agent_id != policy.reviewer.agent_id:
        raise AutoresearchValidationError(
            "review_result must come from the single configured reviewer"
        )


def _validate_implementation_workspace(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact,
) -> None:
    artifact.validate()
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
    if state.implementation_result is None:
        raise AutoresearchValidationError("fix_result requires implementation_result")
    if artifact.workspace_path != state.implementation_result.workspace_path:
        raise AutoresearchValidationError(
            "fix_result workspace_path must match implementation_result workspace_path"
        )
    _validate_implementation_workspace(
        state,
        replace(
            state.implementation_result,
            commit_sha=artifact.commit_sha,
        ),
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
            return PhaseTarget((policy.pm.agent_id,), ArtifactType.SETUP)
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
        return PhaseTarget((policy.pm.agent_id,), ArtifactType.VERIFICATION_RESULT)
    if state.phase is Phase.REVIEW:
        return PhaseTarget((policy.reviewer.agent_id,), ArtifactType.REVIEW_RESULT)
    if state.phase is Phase.FIX_TEST:
        return PhaseTarget((policy.fixer.agent_id,), ArtifactType.FIX_RESULT)
    if state.phase is Phase.DECISION_LOG:
        return PhaseTarget((policy.pm.agent_id,), ArtifactType.FINAL_DECISION)
    if state.final_decision is not None and state.final_decision.memory_write_required:
        if state.memory_written:
            return PhaseTarget((), ArtifactType.NEXT_ITERATION)
        return PhaseTarget((), ArtifactType.MEMORY_WRITE)
    if _is_explicit_no_memory_transition(state):
        return PhaseTarget((), ArtifactType.NEXT_ITERATION)
    raise AutoresearchValidationError("repeat phase has no valid memory transition")


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

    if state.mode is ResearchMode.DATA_INFRA_G0:
        if artifact.decision not in (FinalDecision.INFRA_REPAIRED, FinalDecision.INFRA_BLOCKED):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision must be INFRA_REPAIRED or INFRA_BLOCKED"
            )
        if not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires infra_rationale"
            )
        if latest_verification is None or latest_verification.infra_gate_outcome is None:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires an infrastructure verification gate"
            )
        expected = (
            FinalDecision.INFRA_REPAIRED
            if latest_verification.infra_gate_outcome is InfraGateOutcome.GATE_PASSED
            else FinalDecision.INFRA_BLOCKED
        )
        if artifact.decision is not expected:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision must match infra_gate_outcome"
            )
        return

    if artifact.infra_rationale:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH final_decision cannot contain infra_rationale"
        )

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
            "Use implementation_result.workspace_path and "
            "implementation_result.commit_sha as the source under test. "
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
    workspace_contract = _workspace_isolation_contract(phase)
    mode_contract = _mode_contract(state)
    mempalace_fact_instruction = _mempalace_kg_fact_instruction(state, expected_artifact_type)
    return (
        f"Autoresearch phase: {phase.value}\n"
        f"Target agents: {agent_text}\n"
        f"Expected artifact type: {expected_artifact_type.value}\n"
        f"Phase instruction:\n{instructions[phase]}\n\n"
        f"{mode_contract}"
        f"{workspace_contract}"
        f"{mempalace_fact_instruction}"
        f"Artifact contract:\n{contract}\n\n"
        f"Current state snapshot:\n{state_json}"
    )


def _mode_contract(state: AutoresearchState) -> str:
    if state.mode is None:
        return (
            "Mode contract:\n"
            "- The context packet must choose ALPHA_RESEARCH or DATA_INFRA_G0 and give "
            "a nonempty rationale plus burned theory families.\n\n"
        )
    if state.mode is ResearchMode.DATA_INFRA_G0:
        return (
            "Mode contract: DATA_INFRA_G0\n"
            "- Repair data/provenance/folds only. Verification must emit an explicit "
            "infrastructure gate outcome; do not claim alpha performance validation.\n\n"
        )
    return (
        "Mode contract: ALPHA_RESEARCH\n"
        "- This is a strategy experiment. Burned theory families require materially "
        "new evidence. A fixed local sleeve must be disclosed and cannot claim "
        "cap-verified compliance.\n\n"
    )


def _workspace_isolation_contract(phase: Phase) -> str:
    if phase not in (Phase.IMPLEMENTATION, Phase.FIX_TEST):
        return ""
    return (
        "Workspace isolation contract:\n"
        "- Create and use a disposable git worktree for this iteration before editing "
        "Quantipy; do not implement directly in the main target repo checkout.\n"
        "- Do not leave background experiment, notebook, pytest, or data-generation "
        "processes running after the stage exits.\n"
        "- Commit all accepted implementation changes before emitting the artifact; if "
        "the worktree cannot be made clean, fail closed and report that blocker.\n"
        "- Include the disposable worktree path in workspace_path and the accepted "
        "commit SHA in commit_sha.\n"
        "- Preserve unrelated user files such as "
        "docs/quantipy_experiment_mempalace_preload.md.\n\n"
    )


def _mempalace_kg_fact_instruction(
    state: AutoresearchState,
    expected_artifact_type: ArtifactType,
) -> str:
    if expected_artifact_type is not ArtifactType.MEMORY_WRITE:
        return ""
    if state.final_decision is None:
        raise AutoresearchValidationError("MemPalace memory write requires final_decision")
    facts = standardized_mempalace_kg_facts(state)
    return (
        "Required standardized MemPalace KG facts:\n"
        "- Use the exact predicate/object pairs below with mempalace_kg_add. "
        "Do not re-normalize, shorten, or regenerate their objects.\n"
        f"- Use subject: {state.final_decision.experiment_id}\n"
        f"{_json_block(facts)}\n\n"
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
        _validate_debate_result(
            artifact,
            policy,
            mode=state.mode,
            context=state.context_packet,
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
        _validate_implementation_workspace(state, artifact)
        return replace(state, implementation_result=artifact, phase=Phase.VERIFICATION)

    if state.phase is Phase.VERIFICATION:
        if not isinstance(artifact, VerificationResultArtifact):
            raise AutoresearchValidationError("verification phase accepts verification_result only")
        if state.implementation_result is None:
            raise AutoresearchValidationError("verification requires implementation_result")
        artifact.validate(mode=state.mode)
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
        _validate_fix_workspace(state, artifact)
        assert state.implementation_result is not None
        next_implementation = replace(
            state.implementation_result,
            commit_sha=artifact.commit_sha,
        )
        return replace(
            state,
            implementation_result=next_implementation,
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
    return (
        state.phase is Phase.REPEAT
        and state.final_decision is not None
        and state.final_decision.memory_write_required
    )


def _is_explicit_no_memory_transition(state: AutoresearchState) -> bool:
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and decision is not None
        and decision.decision is FinalDecision.NO_CONSENSUS
        and not decision.memory_write_required
        and not state.memory_written
        and state.memory_verification_receipt is None
    )


def _default_mempalace_kg_path() -> Path:
    palace_root = Path(
        os.environ.get("MEMPALACE_PALACE", str(Path.home() / ".mempalace/palace"))
    ).expanduser()
    return palace_root / "knowledge_graph.sqlite3"


def _standard_metric_object(decision: FinalDecisionArtifact) -> str:
    if decision.recommended_metric_value is None:
        return standardize_mempalace_kg_object(decision.recommended_metric_name)
    metric = f"{decision.recommended_metric_name}_{decision.recommended_metric_value:g}"
    return standardize_mempalace_kg_object(metric)


def _standard_data_window_object(coverage: AggregateCoverageReceipt) -> str:
    """Return the normalized common data/OOS window token required in MemPalace."""
    return standardize_mempalace_kg_object(
        f"{coverage.actual_common_start}_to_{coverage.actual_common_end}_oos_"
        f"{coverage.oos_start}_to_{coverage.oos_end}"
    )


def standardized_mempalace_kg_facts(state: AutoresearchState) -> dict[str, str]:
    """Return the exact standardized KG facts required for a final decision."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError(
            "standardized MemPalace facts require final_decision and mode"
        )
    verification = state.latest_verification
    if verification is None:
        raise AutoresearchValidationError(
            "standardized MemPalace facts require the final decision's verification_result"
        )
    decision = state.final_decision
    facts = {
        "decision": standardize_mempalace_kg_object(decision.decision.value),
        "research_mode": standardize_mempalace_kg_object(state.mode.value),
        "data_window": _standard_data_window_object(verification.data_coverage),
        "reviewer_verdict": standardize_mempalace_kg_object(decision.reviewer_verdict.value),
    }
    if state.mode is ResearchMode.ALPHA_RESEARCH:
        facts["alpha_decision_metric"] = _standard_metric_object(decision)
        facts["keeper_rationale" if decision.decision in KEEP_DECISIONS else "failed_due_to"] = (
            standardize_mempalace_kg_object(decision.rationale)
        )
        return facts
    if verification.infra_gate_outcome is None or decision.infra_rationale is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 standardized MemPalace facts require gate outcome and infra_rationale"
        )
    facts["infra_gate_outcome"] = standardize_mempalace_kg_object(
        verification.infra_gate_outcome.value
    )
    facts["infra_rationale"] = standardize_mempalace_kg_object(decision.infra_rationale)
    return facts


def verify_mempalace_final_decision(
    state: AutoresearchState,
    kg_path: Path | None = None,
) -> MemoryVerificationReceipt:
    """Read and attest KG facts; this function never mutates MemPalace."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError("MemPalace verification requires final_decision and mode")
    if not state.final_decision.memory_write_required:
        raise AutoresearchValidationError(
            "MemPalace verification is not required for this final decision"
        )
    path = (kg_path if kg_path is not None else _default_mempalace_kg_path()).expanduser()
    if not path.is_file():
        raise AutoresearchValidationError(f"MemPalace KG does not exist: {path}")
    decision = state.final_decision
    expected_objects = standardized_mempalace_kg_facts(state)
    required = set(expected_objects)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(triples)").fetchall()}
        required_columns = {
            "id",
            "subject",
            "predicate",
            "object",
            "valid_from",
            "valid_to",
            "source_file",
            "source_drawer_id",
        }
        if not required_columns <= columns:
            raise AutoresearchValidationError("MemPalace KG triples schema is incomplete")
        rows = connection.execute(
            """
            SELECT id, subject, predicate, object, valid_from, valid_to,
                   source_file, source_drawer_id
            FROM triples WHERE subject = ? AND valid_to IS NULL
            """,
            (decision.experiment_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AutoresearchValidationError(f"cannot read MemPalace KG: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    facts: dict[str, list[tuple[str, str, str, str, str, str, str, str]]] = {}
    for row in rows:
        normalized_predicate = _normalise_predicate(str(row[2]))
        if normalized_predicate not in required:
            continue
        source_file = str(row[6] or "").strip()
        source_drawer_id = str(row[7] or "").strip()
        if not source_file and not source_drawer_id:
            raise AutoresearchValidationError(
                "MemPalace standardized facts require source_file or source_drawer_id"
            )
        if not str(row[3]).strip():
            raise AutoresearchValidationError(
                "MemPalace standardized facts require non-empty objects"
            )
        normalized_row = (
            "" if row[0] is None else str(row[0]),
            "" if row[1] is None else str(row[1]),
            "" if row[2] is None else str(row[2]),
            "" if row[3] is None else str(row[3]),
            "" if row[4] is None else str(row[4]),
            "" if row[5] is None else str(row[5]),
            "" if row[6] is None else str(row[6]),
            "" if row[7] is None else str(row[7]),
        )
        facts.setdefault(normalized_predicate, []).append(normalized_row)
    missing = sorted(required - facts.keys())
    if missing:
        raise AutoresearchValidationError(
            "MemPalace required standardized facts are missing: " + ", ".join(missing)
        )

    for predicate, expected_object in expected_objects.items():
        if any(row[3] != expected_object for row in facts[predicate]):
            raise AutoresearchValidationError(
                f"MemPalace {predicate} fact does not match final decision artifact"
            )
    stable_rows = sorted(row for predicate_rows in facts.values() for row in predicate_rows)
    digest = _sha256_text(json.dumps(stable_rows, separators=(",", ":"), ensure_ascii=True))
    return MemoryVerificationReceipt(
        experiment_id=decision.experiment_id,
        kg_path=str(path),
        predicates=tuple(sorted(facts)),
        verified_rows_digest=digest,
    )


def mark_memory_written(
    state: AutoresearchState,
    receipt: MemoryVerificationReceipt,
) -> AutoresearchState:
    if not can_write_memory(state):
        raise AutoresearchValidationError(
            "memory write is allowed only after final decision that requires memory"
        )
    if state.final_decision is None or receipt.experiment_id != state.final_decision.experiment_id:
        raise AutoresearchValidationError("memory receipt must match final decision experiment_id")
    return replace(state, memory_written=True, memory_verification_receipt=receipt)


def start_next_iteration(state: AutoresearchState) -> AutoresearchState:
    if state.setup is None or state.final_decision is None:
        raise AutoresearchValidationError("next iteration requires completed current iteration")
    if not state.memory_written and not _is_explicit_no_memory_transition(state):
        raise AutoresearchValidationError(
            "cannot start next iteration before memory is written or an explicit "
            "NO_CONSENSUS no-memory transition"
        )
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
        return VerificationResultArtifact.from_dict(raw, mode=state.mode)
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
