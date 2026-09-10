"""Frozen, strict wire records for the research driver."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Self

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
