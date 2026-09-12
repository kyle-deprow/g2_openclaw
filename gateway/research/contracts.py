"""Frozen, strict wire records for the research driver."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self, cast

from .codec import (
    require_enum,
    require_keys_exact,
    require_sha256,
    require_str,
    require_utc_iso,
    to_json,
)


class HypothesisState(StrEnum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    DECIDED = "DECIDED"


class AttemptState(StrEnum):
    OPENED = "OPENED"
    IMPLEMENTED = "IMPLEMENTED"
    REVIEW_PASSED = "REVIEW_PASSED"
    REVIEW_FAILED = "REVIEW_FAILED"
    RUN_QUEUED = "RUN_QUEUED"
    RUNNING = "RUNNING"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    RUN_FAILED = "RUN_FAILED"
    CLOSED = "CLOSED"


class AstraDecision(StrEnum):
    RETRY_SAME_HYPOTHESIS = "RETRY_SAME_HYPOTHESIS"
    FINISH_HYPOTHESIS = "FINISH_HYPOTHESIS"
    NEXT_HYPOTHESIS = "NEXT_HYPOTHESIS"
    PAUSE = "PAUSE"


class AttemptDecision(StrEnum):
    RETRY = "RETRY"
    FINISH = "FINISH"
    PAUSE = "PAUSE"


class HypothesisDecision(StrEnum):
    FINISHED = "FINISHED"
    ABANDONED = "ABANDONED"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    LAUNCH_RESERVED = "LAUNCH_RESERVED"
    LAUNCHED = "LAUNCHED"
    ATTACHED = "ATTACHED"
    EXITED = "EXITED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


_H = re.compile(r"^H\d{4}$")
_A = re.compile(r"^H\d{4}-A\d{3}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_VERDICTS = {"PASS", "FAIL"}
_SPEC_ID = re.compile(r"^c\d{3}$")
_SCENARIO_ID = re.compile(r"^s\d{3}$")
_DOTTED_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SECRET_HINT = re.compile(r"(?i)(?:api[_-]?key|password|secret|token)")
_MAX_SCENARIOS = 16
_MAX_RUN_SECONDS = 7200.0
_MAX_ANALYSIS_ARGS = 32
_MAX_ANALYSIS_ARTIFACTS = 16
_MAX_ANALYSIS_BYTES = 16 * 1024 * 1024


def _check_id(value: str, pattern: re.Pattern[str], name: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {name}: {value!r}")
    return value


def _check_commit(value: str, name: str) -> str:
    if _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{name} must be a git commit SHA")
    return value


def _optional_str(value: object, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{name} must be a non-empty string or null")
    return value if isinstance(value, str) else None


def _json_dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _absolute_regular_path(value: object, name: str) -> str:
    path = require_str(value, name)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError(f"{name} must be absolute")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{name} must be a regular non-symlink file")
    return path


@dataclass(frozen=True, slots=True)
class EvaluationSpecEntry:
    """One immutable evaluator-spec file bound into a hypothesis spec set."""

    spec_id: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if _SPEC_ID.fullmatch(self.spec_id) is None:
            raise ValueError("spec_id must match cNNN")
        object.__setattr__(self, "path", _absolute_regular_path(self.path, "spec path"))
        require_sha256(self.sha256, "spec sha256")

    def to_json_value(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "spec_id": self.spec_id}

    @classmethod
    def from_json_value(cls, value: object) -> Self:
        raw = _json_dict(value, "evaluation spec entry")
        data = require_keys_exact(raw, {"spec_id", "path", "sha256"}, "evaluation spec entry")
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class EvaluationSpecSet:
    """Immutable, contiguous set of true evaluator specifications."""

    contract: str
    hypothesis_id: str
    primary_spec_id: str
    specs: tuple[EvaluationSpecEntry, ...]
    created_at: str

    def __post_init__(self) -> None:
        if self.contract != "research-evaluation-spec-set-v1":
            raise ValueError("unsupported evaluation spec set contract")
        _check_id(self.hypothesis_id, _H, "hypothesis_id")
        if not isinstance(self.specs, tuple) or not self.specs:
            raise ValueError("specs must be a non-empty tuple")
        if len(self.specs) > _MAX_SCENARIOS:
            raise ValueError("evaluation spec set may contain at most 16 specs")
        if any(not isinstance(item, EvaluationSpecEntry) for item in self.specs):
            raise ValueError("specs must contain EvaluationSpecEntry values")
        expected = tuple(f"c{index:03d}" for index in range(len(self.specs)))
        actual = tuple(item.spec_id for item in self.specs)
        if actual != expected:
            raise ValueError("spec ids must be contiguous and ordered from c000")
        if self.primary_spec_id not in actual:
            raise ValueError("primary_spec_id must name a spec entry")
        if len({item.sha256 for item in self.specs}) != len(self.specs):
            raise ValueError("evaluation spec digests must be unique")
        if len({item.path for item in self.specs}) != len(self.specs):
            raise ValueError("evaluation spec paths must be unique")
        require_utc_iso(self.created_at, "created_at")

    def to_json(self) -> str:
        return to_json(
            {
                "contract": self.contract,
                "created_at": self.created_at,
                "hypothesis_id": self.hypothesis_id,
                "primary_spec_id": self.primary_spec_id,
                "specs": [item.to_json_value() for item in self.specs],
            }
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "evaluation spec set")
        data = require_keys_exact(
            raw,
            {"contract", "hypothesis_id", "primary_spec_id", "specs", "created_at"},
            "evaluation spec set",
        )
        entries = data["specs"]
        if not isinstance(entries, list):
            raise ValueError("specs must be an array")
        data["specs"] = tuple(EvaluationSpecEntry.from_json_value(item) for item in entries)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RunScenario:
    """One sequential target/evaluator scenario in a reviewed run plan."""

    scenario_id: str
    targets_argv: tuple[str, ...]
    spec_id: str
    evaluation_spec_sha256: str
    targets_sha256_expected: None = None

    def __post_init__(self) -> None:
        if _SCENARIO_ID.fullmatch(self.scenario_id) is None:
            raise ValueError("scenario_id must match sNNN")
        if (
            not isinstance(self.targets_argv, tuple)
            or not self.targets_argv
            or any(not isinstance(item, str) or not item for item in self.targets_argv)
        ):
            raise ValueError("targets_argv must contain non-empty strings")
        if _SPEC_ID.fullmatch(self.spec_id) is None:
            raise ValueError("spec_id must match cNNN")
        require_sha256(self.evaluation_spec_sha256, "evaluation_spec_sha256")
        if self.targets_sha256_expected is not None:
            raise ValueError("targets_sha256_expected must be null")

    def to_json_value(self) -> dict[str, object]:
        return {
            "evaluation_spec_sha256": self.evaluation_spec_sha256,
            "scenario_id": self.scenario_id,
            "spec_id": self.spec_id,
            "targets_argv": list(self.targets_argv),
            "targets_sha256_expected": self.targets_sha256_expected,
        }

    @classmethod
    def from_json_value(cls, value: object) -> Self:
        raw = _json_dict(value, "run scenario")
        data = require_keys_exact(
            raw,
            {
                "scenario_id",
                "targets_argv",
                "spec_id",
                "evaluation_spec_sha256",
                "targets_sha256_expected",
            },
            "run scenario",
        )
        argv = data["targets_argv"]
        if not isinstance(argv, list):
            raise ValueError("scenario targets_argv must be an array")
        data["targets_argv"] = tuple(argv)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AnalysisPlan:
    """Fixed read-only analysis invocation and declared output manifest."""

    module: str
    args: tuple[str, ...]
    artifacts: tuple[str, ...]
    max_artifact_bytes: int

    def __post_init__(self) -> None:
        if _DOTTED_MODULE.fullmatch(self.module) is None:
            raise ValueError("analysis module must be a dotted import name")
        if not isinstance(self.args, tuple) or len(self.args) > _MAX_ANALYSIS_ARGS:
            raise ValueError("analysis args must contain at most 32 tokens")
        for token in self.args:
            if not isinstance(token, str) or not token or token.startswith("-"):
                raise ValueError("analysis args must be plain non-flag tokens")
            if Path(token).is_absolute() or _SECRET_HINT.search(token):
                raise ValueError("analysis args may not contain absolute paths or secret hints")
        if not isinstance(self.artifacts, tuple) or not (
            1 <= len(self.artifacts) <= _MAX_ANALYSIS_ARTIFACTS
        ):
            raise ValueError("analysis artifacts must contain 1..16 paths")
        if tuple(sorted(self.artifacts)) != self.artifacts or len(set(self.artifacts)) != len(
            self.artifacts
        ):
            raise ValueError("analysis artifacts must be sorted and unique")
        for artifact in self.artifacts:
            path = Path(artifact)
            if (
                path.is_absolute()
                or not artifact.startswith("analysis/")
                or path.name == "analysis"
                or len(path.parts) != 2
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("analysis artifacts must be direct files under analysis/")
        if type(self.max_artifact_bytes) is not int or not (
            1 <= self.max_artifact_bytes <= _MAX_ANALYSIS_BYTES
        ):
            raise ValueError("max_artifact_bytes exceeds the 16 MiB limit")

    def to_json_value(self) -> dict[str, object]:
        return {
            "args": list(self.args),
            "artifacts": list(self.artifacts),
            "max_artifact_bytes": self.max_artifact_bytes,
            "module": self.module,
        }

    @classmethod
    def from_json_value(cls, value: object) -> Self:
        raw = _json_dict(value, "analysis")
        data = require_keys_exact(
            raw, {"module", "args", "artifacts", "max_artifact_bytes"}, "analysis"
        )
        for key in ("args", "artifacts"):
            values = data[key]
            if not isinstance(values, list):
                raise ValueError(f"analysis {key} must be an array")
            data[key] = tuple(cast(list[object], values))
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RunPlan:
    """Strict reviewed sequential execution plan for one attempt."""

    contract: str
    attempt_id: str
    commit: str
    implementation_sha256: str
    evaluation_spec_set_sha256: str
    primary_scenario_id: str
    scenarios: tuple[RunScenario, ...]
    analysis: AnalysisPlan
    scenario_timeout_seconds: float
    analysis_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.contract != "research-run-plan-v1":
            raise ValueError("unsupported run plan contract")
        _check_id(self.attempt_id, _A, "attempt_id")
        _check_commit(self.commit, "commit")
        require_sha256(self.implementation_sha256, "implementation_sha256")
        require_sha256(self.evaluation_spec_set_sha256, "evaluation_spec_set_sha256")
        if not isinstance(self.scenarios, tuple) or not (
            1 <= len(self.scenarios) <= _MAX_SCENARIOS
        ):
            raise ValueError("run plan scenarios must contain 1..16 entries")
        if any(not isinstance(item, RunScenario) for item in self.scenarios):
            raise ValueError("scenarios must contain RunScenario values")
        expected = tuple(f"s{index:03d}" for index in range(len(self.scenarios)))
        if tuple(item.scenario_id for item in self.scenarios) != expected:
            raise ValueError("scenario ids must be contiguous and ordered from s000")
        if self.primary_scenario_id not in expected:
            raise ValueError("primary_scenario_id must name a scenario")
        for value, name in (
            (self.scenario_timeout_seconds, "scenario_timeout_seconds"),
            (self.analysis_timeout_seconds, "analysis_timeout_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
            if value > _MAX_RUN_SECONDS:
                raise ValueError(f"{name} exceeds the 7200 second limit")
        if (
            self.scenario_timeout_seconds * len(self.scenarios) + self.analysis_timeout_seconds
            > _MAX_RUN_SECONDS
        ):
            raise ValueError("run plan stage timeouts exceed the 7200 second aggregate limit")

    def to_json(self) -> str:
        return to_json(
            {
                "analysis": self.analysis.to_json_value(),
                "attempt_id": self.attempt_id,
                "commit": self.commit,
                "contract": self.contract,
                "evaluation_spec_set_sha256": self.evaluation_spec_set_sha256,
                "implementation_sha256": self.implementation_sha256,
                "primary_scenario_id": self.primary_scenario_id,
                "scenario_timeout_seconds": self.scenario_timeout_seconds,
                "scenarios": [item.to_json_value() for item in self.scenarios],
                "analysis_timeout_seconds": self.analysis_timeout_seconds,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "run plan")
        data = require_keys_exact(
            raw,
            {
                "contract",
                "attempt_id",
                "commit",
                "implementation_sha256",
                "evaluation_spec_set_sha256",
                "primary_scenario_id",
                "scenarios",
                "analysis",
                "scenario_timeout_seconds",
                "analysis_timeout_seconds",
            },
            "run plan",
        )
        scenarios = data["scenarios"]
        if not isinstance(scenarios, list):
            raise ValueError("scenarios must be an array")
        data["scenarios"] = tuple(RunScenario.from_json_value(item) for item in scenarios)
        data["analysis"] = AnalysisPlan.from_json_value(data["analysis"])
        return cls(**data)  # type: ignore[arg-type]


# Short names used by the admission and worker layers.  Keep the descriptive
# wire-record names above as the canonical public API.
SpecEntry = EvaluationSpecEntry
Scenario = RunScenario


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    hypothesis_id: str
    title: str
    spec_json: str
    spec_sha256: str
    panel_path: str
    receipt_path: str
    evaluation_spec_path: str
    evaluation_spec_sha256: str
    panel_sha256: str
    receipt_sha256: str
    max_attempts: int
    base_commit: str
    created_at: str
    dividends_path: str
    dividends_sha256: str
    state: HypothesisState = HypothesisState.DRAFT

    def __post_init__(self) -> None:
        _check_id(self.hypothesis_id, _H, "hypothesis_id")
        if not isinstance(self.state, HypothesisState):
            raise ValueError("state must be a HypothesisState")
        require_str(self.title, "title")
        require_str(self.spec_json, "spec_json")
        for value, name in (
            (self.spec_sha256, "spec_sha256"),
            (self.evaluation_spec_sha256, "evaluation_spec_sha256"),
            (self.panel_sha256, "panel_sha256"),
            (self.receipt_sha256, "receipt_sha256"),
        ):
            require_sha256(value, name)
        for value, name in (
            (self.panel_path, "panel_path"),
            (self.receipt_path, "receipt_path"),
            (self.evaluation_spec_path, "evaluation_spec_path"),
        ):
            require_str(value, name)
        require_str(self.dividends_path, "dividends_path")
        require_sha256(self.dividends_sha256, "dividends_sha256")
        _check_commit(self.base_commit, "base_commit")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        require_utc_iso(self.created_at, "created_at")

    def to_json(self) -> str:
        return to_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "hypothesis")
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        data = require_keys_exact(raw, keys, "hypothesis")
        data["state"] = require_enum(data["state"], HypothesisState, "state")
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    hypothesis_id: str
    number: int
    state: AttemptState
    worktree_path: str
    commit: str | None
    implementation_sha256: str | None
    review_verdict: str | None
    review_commit: str | None
    review_spec_sha256: str | None
    reported_reviewer_model: str | None
    reported_reviewer_actual_model: str | None
    reported_coder_model: str | None
    coder_effort: str | None
    coder_service_tier: str | None
    run_job_id: str | None
    run_outcome: str | None
    decision: str | None
    decision_reason: str | None
    opened_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _check_id(self.attempt_id, _A, "attempt_id")
        _check_id(self.hypothesis_id, _H, "hypothesis_id")
        if not isinstance(self.state, AttemptState):
            raise ValueError("state must be an AttemptState")
        if self.number < 1:
            raise ValueError("number must be positive")
        require_str(self.worktree_path, "worktree_path")
        for value, name in ((self.commit, "commit"), (self.review_commit, "review_commit")):
            if value is not None:
                _check_commit(value, name)
        for value, name in (
            (self.implementation_sha256, "implementation_sha256"),
            (self.review_spec_sha256, "review_spec_sha256"),
        ):
            if value is not None:
                require_sha256(value, name)
        if self.review_verdict is not None and self.review_verdict not in _VERDICTS:
            raise ValueError("review_verdict must be PASS, FAIL, or null")
        for value, name in (
            (self.reported_reviewer_model, "reported_reviewer_model"),
            (self.reported_reviewer_actual_model, "reported_reviewer_actual_model"),
            (self.reported_coder_model, "reported_coder_model"),
            (self.coder_effort, "coder_effort"),
            (self.coder_service_tier, "coder_service_tier"),
            (self.run_job_id, "run_job_id"),
            (self.run_outcome, "run_outcome"),
            (self.decision, "decision"),
            (self.decision_reason, "decision_reason"),
        ):
            _optional_str(value, name)
        require_utc_iso(self.opened_at, "opened_at")
        require_utc_iso(self.updated_at, "updated_at")

    def to_json(self) -> str:
        return to_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "attempt")
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        data = require_keys_exact(raw, keys, "attempt")
        data["state"] = require_enum(data["state"], AttemptState, "state")
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ImplementationRecord:
    attempt_id: str
    commit: str
    targets_argv: tuple[str, ...]
    test_evidence_path: str
    reported_coder_model: str
    coder_effort: str
    coder_service_tier: str
    submitted_at: str

    def __post_init__(self) -> None:
        _check_id(self.attempt_id, _A, "attempt_id")
        _check_commit(self.commit, "commit")
        if not isinstance(self.targets_argv, tuple):
            raise ValueError("targets_argv must be a tuple")
        if not self.targets_argv or any(
            not isinstance(item, str) or not item for item in self.targets_argv
        ):
            raise ValueError("targets_argv must contain non-empty strings")
        for value, name in (
            (self.test_evidence_path, "test_evidence_path"),
            (self.reported_coder_model, "reported_coder_model"),
            (self.coder_effort, "coder_effort"),
            (self.coder_service_tier, "coder_service_tier"),
        ):
            require_str(value, name)
        require_utc_iso(self.submitted_at, "submitted_at")

    def to_json(self) -> str:
        data = asdict(self)
        data["targets_argv"] = list(self.targets_argv)
        return to_json(data)

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "implementation")
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        data = require_keys_exact(raw, keys, "implementation")
        argv = data["targets_argv"]
        if not isinstance(argv, list):
            raise ValueError("targets_argv must be an array")
        data["targets_argv"] = tuple(argv)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    attempt_id: str
    commit: str
    spec_sha256: str
    verdict: str
    findings: tuple[str, ...]
    reported_reviewer_model: str
    reported_reviewer_actual_model: str
    acp_session_id: str
    submitted_at: str

    def __post_init__(self) -> None:
        _check_id(self.attempt_id, _A, "attempt_id")
        _check_commit(self.commit, "commit")
        require_sha256(self.spec_sha256, "spec_sha256")
        if not isinstance(self.findings, tuple):
            raise ValueError("findings must be a tuple")
        if self.verdict not in _VERDICTS:
            raise ValueError("verdict must be PASS or FAIL")
        if any(not isinstance(item, str) or not item for item in self.findings):
            raise ValueError("findings must contain strings")
        for value, name in (
            (self.reported_reviewer_model, "reported_reviewer_model"),
            (self.reported_reviewer_actual_model, "reported_reviewer_actual_model"),
            (self.acp_session_id, "acp_session_id"),
        ):
            require_str(value, name)
        require_utc_iso(self.submitted_at, "submitted_at")

    def to_json(self) -> str:
        data = asdict(self)
        data["findings"] = list(self.findings)
        return to_json(data)

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "review")
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        data = require_keys_exact(raw, keys, "review")
        findings = data["findings"]
        if not isinstance(findings, list):
            raise ValueError("findings must be an array")
        data["findings"] = tuple(findings)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    """A review verdict after host evidence has been verified.

    This is deliberately distinct from :class:`ReviewRecord`: a caller cannot
    create review state from a self-reported JSON file.  The review collector
    constructs this record only after binding the transcript, task, bundle,
    model, effort, and final verdict to the frozen attempt.
    """

    attempt_id: str
    commit: str
    spec_sha256: str
    verdict: str
    findings: tuple[str, ...]
    reported_reviewer_model: str
    reported_reviewer_actual_model: str
    acp_session_id: str
    submitted_at: str

    def __post_init__(self) -> None:
        _check_id(self.attempt_id, _A, "attempt_id")
        _check_commit(self.commit, "commit")
        require_sha256(self.spec_sha256, "spec_sha256")
        if self.verdict not in _VERDICTS:
            raise ValueError("verdict must be PASS or FAIL")
        if not isinstance(self.findings, tuple):
            raise ValueError("findings must be a tuple")
        if any(not isinstance(item, str) or not item for item in self.findings):
            raise ValueError("findings must contain strings")
        for value, name in (
            (self.reported_reviewer_model, "reported_reviewer_model"),
            (self.reported_reviewer_actual_model, "reported_reviewer_actual_model"),
            (self.acp_session_id, "acp_session_id"),
        ):
            require_str(value, name)
        require_utc_iso(self.submitted_at, "submitted_at")

    def to_review_record(self) -> ReviewRecord:
        return ReviewRecord(
            self.attempt_id,
            self.commit,
            self.spec_sha256,
            self.verdict,
            self.findings,
            self.reported_reviewer_model,
            self.reported_reviewer_actual_model,
            self.acp_session_id,
            self.submitted_at,
        )

    def to_json(self) -> str:
        return self.to_review_record().to_json()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    attempt_id: str
    job_id: str
    exit_code: int
    compliant: bool
    zero_trade: bool
    metrics_available: bool
    acceptance_class: str
    earnings_provenance: str
    result_path: str
    started_at: str
    finished_at: str
    status: str = "succeeded"

    def __post_init__(self) -> None:
        _check_id(self.attempt_id, _A, "attempt_id")
        require_str(self.job_id, "job_id")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ValueError("exit_code must be an integer")
        for boolean_value, name in (
            (self.compliant, "compliant"),
            (self.zero_trade, "zero_trade"),
            (self.metrics_available, "metrics_available"),
        ):
            if not isinstance(boolean_value, bool):
                raise ValueError(f"{name} must be boolean")
        for text_value, name in (
            (self.acceptance_class, "acceptance_class"),
            (self.earnings_provenance, "earnings_provenance"),
        ):
            require_str(text_value, name)
        if not isinstance(self.result_path, str):
            raise ValueError("result_path must be a string")
        require_str(self.status, "status")
        require_utc_iso(self.started_at, "started_at")
        require_utc_iso(self.finished_at, "finished_at")

    def to_json(self) -> str:
        return to_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = json.loads(value)
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**require_keys_exact(raw, keys, "run outcome"))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    at: str
    hypothesis_id: str
    attempt_id: str | None
    kind: str
    detail: str
    actor: str

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError("seq must be positive")
        _check_id(self.hypothesis_id, _H, "hypothesis_id")
        if self.attempt_id is not None:
            _check_id(self.attempt_id, _A, "attempt_id")
        require_str(self.kind, "kind")
        require_str(self.detail, "detail")
        try:
            json.loads(self.detail)
        except json.JSONDecodeError as exc:
            raise ValueError("detail must be JSON text") from exc
        if self.actor not in {"astra", "driver", "operator"}:
            raise ValueError("actor must be astra, driver, or operator")
        require_utc_iso(self.at, "at")

    def to_json(self) -> str:
        return to_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = json.loads(value)
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**require_keys_exact(raw, keys, "event"))  # type: ignore[arg-type]
