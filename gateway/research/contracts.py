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
# Single source of truth for run limits; store, worker, jobs, cli and hypothesis import these.
MAX_RUN_TIMEOUT_SECONDS = 28800
MAX_STAGE_RSS_MB = 16384
# Fixed driver overhead added to a plan's stage budget to derive the run timeout.
RUN_OVERHEAD_SECONDS = 300.0
# RunPlan accepts any positive analysis timeout; 1 s is the smallest sensible one.
MIN_ANALYSIS_SECONDS = 1.0
# The smallest valid run plan runs one validate, one targets and one evaluate stage.
MIN_PLAN_SCENARIO_STAGES = 3
_MAX_RUN_SECONDS = float(MAX_RUN_TIMEOUT_SECONDS)
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


COMPUTE_PROBE_CONTRACT = "research-compute-probe-v1"
_PROBE_ID = re.compile(r"^P[0-9a-f]{12}$")
_PROBE_STAGE = re.compile(r"^(?:validate-c\d{3}|evaluate-s\d{3})$")
_PROBE_PIN_KEYS = {
    "evaluator_sha256",
    "pyvenv_sha256",
    "shared_python_sha256",
    "snapshot_sha256",
    "universe_sha256",
}
_PROBE_INPUT_KEYS = {
    "dividends_sha256",
    "evaluation_spec_set_sha256",
    "panel_sha256",
    "receipt_sha256",
}
# Bound only when the hypothesis carries the matching optional input (stock campaigns).
_PROBE_OPTIONAL_INPUT_KEYS = {"earnings_sha256", "membership_sha256"}
# Headroom the probe requires over the measured values (see ``compute_requirements``).
PROBE_RSS_HEADROOM_NUMERATOR = 5
PROBE_RSS_HEADROOM_DENOMINATOR = 4
PROBE_WALL_HEADROOM = 1.5


@dataclass(frozen=True, slots=True)
class ProbeStage:
    """One measured, contained probe stage (validate-inputs or empty-target evaluate)."""

    stage: str
    spec_id: str
    exit: int
    wall_seconds: float
    peak_rss_mb: int

    def __post_init__(self) -> None:
        if _PROBE_STAGE.fullmatch(self.stage) is None:
            raise ValueError("probe stage must be validate-cNNN or evaluate-sNNN")
        _check_id(self.spec_id, _SPEC_ID, "spec_id")
        if self.stage.startswith("validate-") and self.stage != f"validate-{self.spec_id}":
            raise ValueError("validate stage name must match its spec_id")
        if isinstance(self.exit, bool) or not isinstance(self.exit, int) or self.exit != 0:
            raise ValueError("recorded probe stages must have exit 0")
        if (
            isinstance(self.wall_seconds, bool)
            or not isinstance(self.wall_seconds, (int, float))
            or not math.isfinite(float(self.wall_seconds))
            or float(self.wall_seconds) <= 0
        ):
            raise ValueError("probe wall_seconds must be a positive finite number")
        if (
            isinstance(self.peak_rss_mb, bool)
            or not isinstance(self.peak_rss_mb, int)
            or self.peak_rss_mb < 1
        ):
            raise ValueError("probe peak_rss_mb must be a positive integer")

    def to_json_value(self) -> dict[str, object]:
        return {
            "exit": self.exit,
            "peak_rss_mb": self.peak_rss_mb,
            "spec_id": self.spec_id,
            "stage": self.stage,
            "wall_seconds": self.wall_seconds,
        }

    @classmethod
    def from_json_value(cls, value: object) -> Self:
        raw = _json_dict(value, "probe stage")
        data = require_keys_exact(
            raw, {"stage", "spec_id", "exit", "wall_seconds", "peak_rss_mb"}, "probe stage"
        )
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ComputeProbe:
    """Immutable measured cost of validate-inputs and an empty-target evaluate."""

    contract: str
    probe_id: str
    hypothesis_id: str
    pins: dict[str, str]
    inputs: dict[str, str]
    stages: tuple[ProbeStage, ...]
    measured_at: str

    def __post_init__(self) -> None:
        if self.contract != COMPUTE_PROBE_CONTRACT:
            raise ValueError("unsupported compute probe contract")
        _check_id(self.probe_id, _PROBE_ID, "probe_id")
        _check_id(self.hypothesis_id, _H, "hypothesis_id")
        data = require_keys_exact(self.pins, _PROBE_PIN_KEYS, "pins")
        for key, digest in data.items():
            require_sha256(digest, f"pins.{key}")
        if not isinstance(self.inputs, dict):
            raise ValueError("inputs must be an object")
        optional = set(self.inputs) & _PROBE_OPTIONAL_INPUT_KEYS
        data = require_keys_exact(self.inputs, _PROBE_INPUT_KEYS | optional, "inputs")
        for key, digest in data.items():
            require_sha256(digest, f"inputs.{key}")
        if not isinstance(self.stages, tuple) or len(self.stages) < 2:
            raise ValueError("probe stages must contain validate stages and one evaluate stage")
        if any(not isinstance(item, ProbeStage) for item in self.stages):
            raise ValueError("probe stages must contain ProbeStage values")
        names = [item.stage for item in self.stages]
        if len(set(names)) != len(names):
            raise ValueError("probe stage names must be unique")
        evaluates = [item for item in self.stages if item.stage.startswith("evaluate-")]
        if len(evaluates) != 1 or not self.stages[-1].stage.startswith("evaluate-"):
            raise ValueError("probe must end with exactly one evaluate stage")
        require_utc_iso(self.measured_at, "measured_at")

    def to_json(self) -> str:
        return to_json(
            {
                "contract": self.contract,
                "hypothesis_id": self.hypothesis_id,
                "inputs": dict(self.inputs),
                "measured_at": self.measured_at,
                "pins": dict(self.pins),
                "probe_id": self.probe_id,
                "stages": [item.to_json_value() for item in self.stages],
            }
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "compute probe")
        data = require_keys_exact(
            raw,
            {
                "contract",
                "probe_id",
                "hypothesis_id",
                "pins",
                "inputs",
                "stages",
                "measured_at",
            },
            "compute probe",
        )
        stages = data["stages"]
        if not isinstance(stages, list):
            raise ValueError("stages must be an array")
        data["stages"] = tuple(ProbeStage.from_json_value(item) for item in stages)
        for key in ("pins", "inputs"):
            data[key] = dict(_json_dict(data[key], key))
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ComputeRequirements:
    """Minimum frozen bounds a measured probe allows, with the stages that set them."""

    min_rss_mb: int
    min_scenario_timeout_seconds: int
    max_peak_rss_mb: int
    max_peak_rss_stage: str
    max_wall_seconds: float
    max_wall_stage: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def rss_feasible(self) -> bool:
        """Whether some legal ``compute.max_rss_mb`` can satisfy ``min_rss_mb``."""
        return self.min_rss_mb <= MAX_STAGE_RSS_MB

    @property
    def timeout_feasible(self) -> bool:
        """Whether the smallest legal run plan can use ``min_scenario_timeout_seconds``.

        Its budget is ``3 * t`` for the validate, targets and evaluate stages plus the analysis
        stage, and the derived run timeout adds the fixed driver overhead; that must fit in
        ``MAX_RUN_TIMEOUT_SECONDS`` or no submitted plan could ever be run.
        """
        return (
            MIN_PLAN_SCENARIO_STAGES * self.min_scenario_timeout_seconds
            + MIN_ANALYSIS_SECONDS
            + RUN_OVERHEAD_SECONDS
            <= MAX_RUN_TIMEOUT_SECONDS
        )


def compute_requirements(probe: ComputeProbe) -> ComputeRequirements:
    """Minimum frozen bounds a measured probe allows.

    ``min_rss_mb`` is ``ceil(1.25 * max peak_rss_mb)`` (integer arithmetic, no float
    rounding).  ``min_scenario_timeout_seconds`` is ``ceil(1.5 * max stage wall)``.
    The freeze gate enforces the first, the submit gate the second.
    """
    peak_stage = max(probe.stages, key=lambda item: item.peak_rss_mb)
    wall_stage = max(probe.stages, key=lambda item: item.wall_seconds)
    min_rss = -(
        -peak_stage.peak_rss_mb * PROBE_RSS_HEADROOM_NUMERATOR // PROBE_RSS_HEADROOM_DENOMINATOR
    )
    return ComputeRequirements(
        min_rss_mb=min_rss,
        min_scenario_timeout_seconds=math.ceil(PROBE_WALL_HEADROOM * wall_stage.wall_seconds),
        max_peak_rss_mb=peak_stage.peak_rss_mb,
        max_peak_rss_stage=peak_stage.stage,
        max_wall_seconds=wall_stage.wall_seconds,
        max_wall_stage=wall_stage.stage,
    )


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
                raise ValueError(f"{name} exceeds the 28800 second limit")
        if self.stage_budget_seconds > _MAX_RUN_SECONDS:
            raise ValueError("run plan stage timeouts exceed the 28800 second aggregate limit")

    @property
    def spec_ids(self) -> tuple[str, ...]:
        """Distinct evaluation spec ids in first-use order; one validation stage each."""
        return tuple(dict.fromkeys(item.spec_id for item in self.scenarios))

    @property
    def stage_budget_seconds(self) -> float:
        """Sum of every stage cap the worker can spend.

        One ``validate-<spec>`` stage per distinct spec, a ``targets-<scenario>`` and an
        ``evaluate-<scenario>`` stage per scenario (each capped at the scenario timeout), plus
        the analysis stage.
        """
        return (
            self.scenario_timeout_seconds * (2 * len(self.scenarios) + len(self.spec_ids))
            + self.analysis_timeout_seconds
        )

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


SUBMISSION_INPUT_CONTRACT = "research-submission-input-v1"
_RESERVED_TARGET_FLAGS = frozenset({"--panel", "--receipt", "--scenario-id", "--out"})


def is_host_owned_target_flag(token: str) -> bool:
    """Whether ``token`` would set a host-owned targets flag, however it is spelled.

    Covers the exact flag, the ``--flag=value`` form and any argparse-style abbreviation
    (a ``--`` prefix of a reserved flag, including a bare ``--``), so extra arguments cannot
    override the bound panel, receipt, scenario id or output path.
    """
    if not token.startswith("--"):
        return False
    name = token.split("=", 1)[0]
    return any(flag.startswith(name) for flag in _RESERVED_TARGET_FLAGS)


def _repo_relative(value: object, name: str) -> str:
    text = require_str(value, name)
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be a safe repo-relative path")
    return path.as_posix()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class SubmissionScenarioInput:
    """One scenario of the declarative submission input; the host derives its argv."""

    scenario_id: str
    spec_id: str
    extra_args: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_id(self.scenario_id, _SCENARIO_ID, "scenario_id")
        _check_id(self.spec_id, _SPEC_ID, "spec_id")
        if not isinstance(self.extra_args, tuple) or any(
            not isinstance(item, str) or not item or is_host_owned_target_flag(item)
            for item in self.extra_args
        ):
            raise ValueError(
                "extra_args must be non-empty strings that do not set the host-owned "
                "--panel/--receipt/--scenario-id/--out flags (also as --flag=value or an "
                "abbreviation)"
            )

    @classmethod
    def from_json_value(cls, value: object) -> Self:
        raw = _json_dict(value, "submission scenario")
        data = require_keys_exact(raw, {"scenario_id", "spec_id", "extra_args"}, "scenario")
        return cls(
            scenario_id=require_str(data["scenario_id"], "scenario_id"),
            spec_id=require_str(data["spec_id"], "spec_id"),
            extra_args=_string_tuple(data["extra_args"], "extra_args"),
        )


@dataclass(frozen=True, slots=True)
class ProvenanceStageInput:
    """One provenance stage and its committed, repo-relative records directory."""

    stage: str
    records_dir: str

    def __post_init__(self) -> None:
        require_str(self.stage, "provenance stage")
        _repo_relative(self.records_dir, "provenance records_dir")

    @classmethod
    def from_json_value(cls, value: object) -> Self:
        raw = _json_dict(value, "provenance stage")
        data = require_keys_exact(raw, {"stage", "records_dir"}, "provenance stage")
        return cls(
            stage=require_str(data["stage"], "provenance stage"),
            records_dir=_repo_relative(data["records_dir"], "provenance records_dir"),
        )


@dataclass(frozen=True, slots=True)
class SubmissionInput:
    """Small declarative input from which ``submission-build`` derives every digest and argv."""

    contract: str
    commit: str
    test_evidence_path: str
    reported_coder_model: str
    coder_effort: str
    coder_service_tier: str
    interpreter: str
    target_script: str
    scenarios: tuple[SubmissionScenarioInput, ...]
    primary_scenario_id: str
    analysis: AnalysisPlan
    scenario_timeout_seconds: float
    analysis_timeout_seconds: float
    provenance_stages: tuple[ProvenanceStageInput, ...]

    def __post_init__(self) -> None:
        if self.contract != SUBMISSION_INPUT_CONTRACT:
            raise ValueError("unsupported submission input contract")
        _check_commit(self.commit, "commit")
        _repo_relative(self.test_evidence_path, "test_evidence_path")
        _repo_relative(self.target_script, "target_script")
        for value, name in (
            (self.reported_coder_model, "reported_coder_model"),
            (self.coder_effort, "coder_effort"),
            (self.coder_service_tier, "coder_service_tier"),
        ):
            require_str(value, name)
        if not Path(require_str(self.interpreter, "interpreter")).is_absolute():
            raise ValueError("interpreter must be an absolute path")
        if not isinstance(self.scenarios, tuple) or not (
            1 <= len(self.scenarios) <= _MAX_SCENARIOS
        ):
            raise ValueError("scenarios must contain 1..16 entries")
        expected = tuple(f"s{index:03d}" for index in range(len(self.scenarios)))
        if tuple(item.scenario_id for item in self.scenarios) != expected:
            raise ValueError("scenario ids must be contiguous and ordered from s000")
        if self.primary_scenario_id not in expected:
            raise ValueError("primary_scenario_id must name a scenario")
        if not self.provenance_stages:
            raise ValueError("provenance_stages must not be empty")
        for seconds, seconds_name in (
            (self.scenario_timeout_seconds, "scenario_timeout_seconds"),
            (self.analysis_timeout_seconds, "analysis_timeout_seconds"),
        ):
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError(f"{seconds_name} must be a number")

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "submission input")
        data = require_keys_exact(
            raw,
            {
                "contract",
                "commit",
                "test_evidence_path",
                "reported_coder_model",
                "coder_effort",
                "coder_service_tier",
                "interpreter",
                "target_script",
                "scenarios",
                "primary_scenario_id",
                "analysis",
                "scenario_timeout_seconds",
                "analysis_timeout_seconds",
                "provenance_stages",
            },
            "submission input",
        )
        scenarios = data["scenarios"]
        stages = data["provenance_stages"]
        if not isinstance(scenarios, list) or not isinstance(stages, list):
            raise ValueError("scenarios and provenance_stages must be arrays")
        data["scenarios"] = tuple(SubmissionScenarioInput.from_json_value(i) for i in scenarios)
        data["provenance_stages"] = tuple(ProvenanceStageInput.from_json_value(i) for i in stages)
        data["analysis"] = AnalysisPlan.from_json_value(data["analysis"])
        for key in ("scenario_timeout_seconds", "analysis_timeout_seconds"):
            number = data[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValueError(f"{key} must be a number")
            data[key] = float(number)
        return cls(**data)  # type: ignore[arg-type]


# Short names used by the admission and worker layers.  Keep the descriptive
# wire-record names above as the canonical public API.
SpecEntry = EvaluationSpecEntry
Scenario = RunScenario


#: Optional bound hypothesis inputs, persisted as insert-only ``hypothesis_evidence`` rows so
#: the key-exact ``HypothesisSpec`` row never widens.  Artifact name -> evidence kind.
INPUT_BINDING_KINDS: dict[str, str] = {
    "earnings": "earnings_binding",
    "membership": "membership_binding",
}
OPTIONAL_INPUT_NAMES: tuple[str, ...] = tuple(INPUT_BINDING_KINDS)


@dataclass(frozen=True, slots=True)
class InputBinding:
    """Path and SHA-256 of one optional frozen input (``earnings`` or ``membership``)."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        require_str(self.path, "path")
        require_sha256(self.sha256, "sha256")

    def to_json(self) -> str:
        return to_json({"path": self.path, "sha256": self.sha256})

    @classmethod
    def from_json(cls, value: str) -> Self:
        raw = _json_dict(json.loads(value), "input binding")
        data = require_keys_exact(raw, {"path", "sha256"}, "input binding")
        return cls(**data)  # type: ignore[arg-type]


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
