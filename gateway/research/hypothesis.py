"""Pure, strict scientific hypothesis documents.

``HypothesisDocument`` is a typed description of a research claim and its
declared analysis plan.  It is not an admission decision: parsing it does not
prove evaluator availability, input availability, price-only or sentiment
execution capability, leak-free folds, or FINAL_HOLDOUT eligibility.  Source
labels such as ``reddit`` are declarations, not permission to execute a
sentiment input.  Admission must later compare trusted input bounds and the
exposure ledger; known or unknown historical exposure cannot be hidden by
choosing ``FINAL_HOLDOUT``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import PurePosixPath, PureWindowsPath
from typing import Self, cast

from .codec import require_keys_exact, require_str, to_json

_CONTRACT = "research-hypothesis-v1"
_SOURCES = {"panel", "derived", "reddit"}
_PURPOSES = {"DEVELOPMENT_VALIDATION", "FINAL_HOLDOUT"}
_MAX_TEXT_LENGTH = 8_192
_MAX_COLLECTION_LENGTH = 1_024
_MAX_INTEGER = 1_000_000_000

_FEATURE_KEYS = {"name", "source", "as_of_rule", "lookback_sessions"}
_DATE_RANGE_KEYS = {"start", "end"}
_EVIDENCE_KEYS = {"sessions", "trades"}
_COMPUTE_KEYS = {"max_wall_seconds", "max_rss_mb"}
_DOCUMENT_KEYS = {
    "contract",
    "mechanism",
    "prediction",
    "target",
    "baseline",
    "universe_rule",
    "features",
    "entry_rule",
    "exit_rule",
    "position_sizing_rule",
    "variants",
    "search_budget_evaluations",
    "analysis",
    "evaluation",
    "training",
    "purpose",
    "forward_label_sessions",
    "purge_rule",
    "primary_metric",
    "minimum_evidence",
    "null_tests",
    "reject_criteria",
    "missing_data_rule",
    "compute",
    "deliverables",
}


@dataclass(frozen=True, slots=True)
class FeatureDeclaration:
    name: str
    source: str
    as_of_rule: str
    lookback_sessions: int

    def __post_init__(self) -> None:
        _text(self.name, "feature.name", max_length=256)
        _choice(self.source, _SOURCES, "feature.source")
        _text(self.as_of_rule, "feature.as_of_rule")
        _integer(self.lookback_sessions, "feature.lookback_sessions", minimum=0)

    @classmethod
    def from_mapping(cls, value: object, name: str) -> Self:
        data = require_keys_exact(value, _FEATURE_KEYS, name)
        return cls(
            name=data["name"],  # type: ignore[arg-type]
            source=data["source"],  # type: ignore[arg-type]
            as_of_rule=data["as_of_rule"],  # type: ignore[arg-type]
            lookback_sessions=data["lookback_sessions"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class DateRange:
    start: str
    end: str

    def __post_init__(self) -> None:
        start = _date_text(self.start, "date_range.start")
        end = _date_text(self.end, "date_range.end")
        if start > end:
            raise ValueError("date_range is reversed")

    @classmethod
    def from_mapping(cls, value: object, name: str) -> Self:
        data = require_keys_exact(value, _DATE_RANGE_KEYS, name)
        return cls(
            start=data["start"],  # type: ignore[arg-type]
            end=data["end"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MinimumEvidence:
    sessions: int
    trades: int

    def __post_init__(self) -> None:
        _integer(self.sessions, "minimum_evidence.sessions", minimum=1)
        _integer(self.trades, "minimum_evidence.trades", minimum=0)

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = require_keys_exact(value, _EVIDENCE_KEYS, "minimum_evidence")
        return cls(
            sessions=data["sessions"],  # type: ignore[arg-type]
            trades=data["trades"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ComputeLimits:
    max_wall_seconds: float
    max_rss_mb: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_wall_seconds",
            _validate_wall_seconds(self.max_wall_seconds),
        )
        _integer(self.max_rss_mb, "compute.max_rss_mb", minimum=1, maximum=8_192)

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = require_keys_exact(value, _COMPUTE_KEYS, "compute")
        return cls(
            max_wall_seconds=data["max_wall_seconds"],  # type: ignore[arg-type]
            max_rss_mb=data["max_rss_mb"],  # type: ignore[arg-type]
        )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_object(value: str) -> dict[str, object]:
    try:
        raw = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid hypothesis document JSON") from exc
    return require_keys_exact(raw, _DOCUMENT_KEYS, "hypothesis document")


def _text(value: object, name: str, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    text = require_str(value, name)
    if not text.strip():
        raise ValueError(f"{name} must not be blank")
    if "\x00" in text:
        raise ValueError(f"{name} must not contain NUL")
    if len(text) > max_length:
        raise ValueError(f"{name} exceeds the maximum length")
    return text


def _integer(value: object, name: str, *, minimum: int, maximum: int = _MAX_INTEGER) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside the supported bounds")
    return value


def _choice(value: object, choices: set[str], name: str) -> str:
    text = _text(value, name, max_length=128)
    if text not in choices:
        raise ValueError(f"invalid {name}: {text!r}")
    return text


def _date_text(value: object, name: str) -> str:
    text = _text(value, name, max_length=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{name} must use canonical YYYY-MM-DD form")
    return text


def _feature_declarations(value: object) -> tuple[FeatureDeclaration, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("features must be a non-empty array")
    if len(value) > _MAX_COLLECTION_LENGTH:
        raise ValueError("features exceeds the maximum collection length")
    features = tuple(
        FeatureDeclaration.from_mapping(item, f"features[{index}]")
        for index, item in enumerate(value)
    )
    return features


def _object_array(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty array")
    if len(value) > _MAX_COLLECTION_LENGTH:
        raise ValueError(f"{name} exceeds the maximum collection length")
    return tuple(value)


def _array_values(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _text_array(value: object, name: str, *, max_length: int = _MAX_TEXT_LENGTH) -> tuple[str, ...]:
    values = _object_array(value, name)
    return tuple(
        _text(item, f"{name}[{index}]", max_length=max_length) for index, item in enumerate(values)
    )


def _variants(value: object) -> tuple[str, ...]:
    variants = _text_array(value, "variants", max_length=256)
    if len(set(variants)) != len(variants):
        raise ValueError("variants must have unique labels")
    return variants


def _validate_wall_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("compute.max_wall_seconds must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("compute.max_wall_seconds must be finite") from exc
    if not math.isfinite(result) or result <= 0 or result > 7_200:
        raise ValueError("compute.max_wall_seconds is outside the supported bounds")
    return result


def _deliverable_paths(value: object) -> tuple[str, ...]:
    paths = _text_array(value, "deliverables")
    if len(set(paths)) != len(paths):
        raise ValueError("deliverables must have unique paths")
    safe: list[str] = []
    for index, path in enumerate(paths):
        name = f"deliverables[{index}]"
        if (
            path.startswith(("/", "\\"))
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).is_absolute()
            or PureWindowsPath(path).drive
        ):
            raise ValueError(f"{name} must be a relative POSIX path")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"{name} contains an unsafe path segment")
        safe.append(path)
    return tuple(safe)


def _validate_document(document: HypothesisDocument) -> None:
    if document.contract != _CONTRACT:
        raise ValueError(f"invalid contract: {document.contract!r}")
    for name, value in (
        ("mechanism", document.mechanism),
        ("prediction", document.prediction),
        ("target", document.target),
        ("baseline", document.baseline),
        ("universe_rule", document.universe_rule),
        ("entry_rule", document.entry_rule),
        ("exit_rule", document.exit_rule),
        ("position_sizing_rule", document.position_sizing_rule),
        ("purge_rule", document.purge_rule),
        ("primary_metric", document.primary_metric),
        ("reject_criteria", document.reject_criteria),
        ("missing_data_rule", document.missing_data_rule),
    ):
        _text(value, name)
    if (
        not isinstance(document.features, tuple)
        or not document.features
        or len(document.features) > _MAX_COLLECTION_LENGTH
    ):
        raise ValueError("features must be a non-empty tuple")
    feature_names: set[str] = set()
    for index, feature in enumerate(document.features):
        if not isinstance(feature, FeatureDeclaration):
            raise ValueError(f"features[{index}] has an invalid type")
        if feature.name in feature_names:
            raise ValueError(f"duplicate feature name: {feature.name!r}")
        feature_names.add(feature.name)
    if not isinstance(document.variants, tuple):
        raise ValueError("variants must be a tuple of strings")
    _variants(list(document.variants))
    _integer(document.search_budget_evaluations, "search_budget_evaluations", minimum=1)
    if document.search_budget_evaluations < len(document.variants):
        raise ValueError("search_budget_evaluations must cover all variants")
    _integer(document.forward_label_sessions, "forward_label_sessions", minimum=0)
    if not isinstance(document.analysis, DateRange) or not isinstance(
        document.evaluation, DateRange
    ):
        raise ValueError("analysis and evaluation must be date ranges")
    if (
        document.evaluation.start < document.analysis.start
        or document.evaluation.end > document.analysis.end
    ):
        raise ValueError("evaluation must be inside analysis")
    if document.training is not None and not isinstance(document.training, DateRange):
        raise ValueError("training must be a date range or null")
    if document.training is not None:
        if (
            document.training.start < document.analysis.start
            or document.training.end > document.analysis.end
        ):
            raise ValueError("training must be inside analysis")
        if document.training.end >= document.evaluation.start:
            raise ValueError("training must end before evaluation starts")
    _choice(document.purpose, _PURPOSES, "purpose")
    if not isinstance(document.minimum_evidence, MinimumEvidence):
        raise ValueError("minimum_evidence has an invalid type")
    if not isinstance(document.null_tests, tuple):
        raise ValueError("null_tests must be a tuple of strings")
    _text_array(list(document.null_tests), "null_tests")
    if not isinstance(document.compute, ComputeLimits):
        raise ValueError("compute has an invalid type")
    if not isinstance(document.deliverables, tuple):
        raise ValueError("deliverables must be a tuple of paths")
    _deliverable_paths(list(document.deliverables))


@dataclass(frozen=True, slots=True)
class HypothesisDocument:
    """A canonical, mechanically validated scientific hypothesis document."""

    contract: str
    mechanism: str
    prediction: str
    target: str
    baseline: str
    universe_rule: str
    features: tuple[FeatureDeclaration, ...]
    entry_rule: str
    exit_rule: str
    position_sizing_rule: str
    variants: tuple[str, ...]
    search_budget_evaluations: int
    analysis: DateRange
    evaluation: DateRange
    training: DateRange | None
    purpose: str
    forward_label_sessions: int
    purge_rule: str
    primary_metric: str
    minimum_evidence: MinimumEvidence
    null_tests: tuple[str, ...]
    reject_criteria: str
    missing_data_rule: str
    compute: ComputeLimits
    deliverables: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_document(self)

    def to_json(self) -> str:
        return to_json(asdict(self))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, value: str) -> Self:
        data = _load_object(value)
        features = _feature_declarations(data["features"])
        variants = cast(tuple[str, ...], tuple(_array_values(data["variants"], "variants")))
        training_value = data["training"]
        training = (
            None if training_value is None else DateRange.from_mapping(training_value, "training")
        )
        return cls(
            contract=data["contract"],  # type: ignore[arg-type]
            mechanism=data["mechanism"],  # type: ignore[arg-type]
            prediction=data["prediction"],  # type: ignore[arg-type]
            target=data["target"],  # type: ignore[arg-type]
            baseline=data["baseline"],  # type: ignore[arg-type]
            universe_rule=data["universe_rule"],  # type: ignore[arg-type]
            features=features,
            entry_rule=data["entry_rule"],  # type: ignore[arg-type]
            exit_rule=data["exit_rule"],  # type: ignore[arg-type]
            position_sizing_rule=data["position_sizing_rule"],  # type: ignore[arg-type]
            variants=variants,
            search_budget_evaluations=data["search_budget_evaluations"],  # type: ignore[arg-type]
            analysis=DateRange.from_mapping(data["analysis"], "analysis"),
            evaluation=DateRange.from_mapping(data["evaluation"], "evaluation"),
            training=training,
            purpose=data["purpose"],  # type: ignore[arg-type]
            forward_label_sessions=data["forward_label_sessions"],  # type: ignore[arg-type]
            purge_rule=data["purge_rule"],  # type: ignore[arg-type]
            primary_metric=data["primary_metric"],  # type: ignore[arg-type]
            minimum_evidence=MinimumEvidence.from_mapping(data["minimum_evidence"]),
            null_tests=cast(
                tuple[str, ...], tuple(_array_values(data["null_tests"], "null_tests"))
            ),
            reject_criteria=data["reject_criteria"],  # type: ignore[arg-type]
            missing_data_rule=data["missing_data_rule"],  # type: ignore[arg-type]
            compute=ComputeLimits.from_mapping(data["compute"]),
            deliverables=cast(
                tuple[str, ...], tuple(_array_values(data["deliverables"], "deliverables"))
            ),
        )
