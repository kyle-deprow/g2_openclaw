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
from statistics import NormalDist, StatisticsError
from typing import Self, cast

from .codec import require_keys_exact, require_str, to_json
from .contracts import MAX_RUN_TIMEOUT_SECONDS, MAX_STAGE_RSS_MB

_CONTRACT_V1 = "research-hypothesis-v1"
_CONTRACT_V2 = "research-hypothesis-v2"
_CONTRACTS = {_CONTRACT_V1, _CONTRACT_V2}
MDE_TOLERANCE_BPS = 0.5
_SIDES = {"one", "two"}
_SOURCES = {"panel", "derived", "reddit"}
_PURPOSES = {"DEVELOPMENT_VALIDATION", "FINAL_HOLDOUT"}
_MAX_TEXT_LENGTH = 16_384
_MAX_COLLECTION_LENGTH = 1_024
_MAX_INTEGER = 1_000_000_000
# The one definition of the evaluator holding / forward-label limit (contract v3).
MAX_HOLDING_SESSIONS = 60

_FEATURE_KEYS = {"name", "source", "as_of_rule", "lookback_sessions"}
_DATE_RANGE_KEYS = {"start", "end"}
_EVIDENCE_KEYS = {"sessions", "trades"}
_COMPUTE_KEYS = {"max_wall_seconds", "max_rss_mb"}
_POWER_KEYS = {
    "expected_events",
    "per_event_sd_bps",
    "sd_basis",
    "alpha",
    "power",
    "sided",
    "minimum_detectable_effect_bps",
    "plausible_effect_bps",
    "plausibility_basis",
}
_V1_DOCUMENT_KEYS = {
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
_V2_DOCUMENT_KEYS = _V1_DOCUMENT_KEYS | {"power"}


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
        _integer(self.max_rss_mb, "compute.max_rss_mb", minimum=1, maximum=MAX_STAGE_RSS_MB)

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = require_keys_exact(value, _COMPUTE_KEYS, "compute")
        return cls(
            max_wall_seconds=data["max_wall_seconds"],  # type: ignore[arg-type]
            max_rss_mb=data["max_rss_mb"],  # type: ignore[arg-type]
        )


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def minimum_detectable_effect_bps(
    events: int, sd_bps: float, alpha: float, power: float, sided: str
) -> float:
    """Return the minimum detectable mean effect in bps for a paired-event z-test.

    MDE = (z_{1-alpha/k} + z_{power}) * sd / sqrt(events), with k = 1 (one-sided) or 2.
    """
    _integer(events, "power.expected_events", minimum=1)
    sd = _finite_positive(sd_bps, "power.per_event_sd_bps")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 < alpha <= 0.5:
        raise ValueError("power.alpha must be finite and in (0, 0.5]")
    if isinstance(power, bool) or not isinstance(power, (int, float)) or not 0.5 <= power <= 0.99:
        raise ValueError("power.power must be finite and in [0.5, 0.99]")
    if sided not in _SIDES:
        raise ValueError(f"invalid power.sided: {sided!r}")
    normal = NormalDist()
    tail = float(alpha) / (2 if sided == "two" else 1)
    try:
        z_alpha = -normal.inv_cdf(tail)
        z_power = normal.inv_cdf(float(power))
    except StatisticsError as exc:
        raise ValueError("power.alpha or power.power is outside the supported bounds") from exc
    result = (z_alpha + z_power) * sd / math.sqrt(events)
    if not math.isfinite(result):
        raise ValueError("power parameters yield a non-finite minimum detectable effect")
    if result <= 0:
        raise ValueError("power parameters yield a zero minimum detectable effect")
    return result


@dataclass(frozen=True, slots=True)
class PowerDesign:
    """Pre-registered statistical power declaration (hypothesis contract v2)."""

    expected_events: int
    per_event_sd_bps: float
    sd_basis: str
    alpha: float
    power: float
    sided: str
    minimum_detectable_effect_bps: float
    plausible_effect_bps: float
    plausibility_basis: str

    def __post_init__(self) -> None:
        # Validate without coercing: the stored spec must round-trip byte-identically, so the
        # author's number types (``100`` vs ``100.0``) are kept as written.
        _integer(self.expected_events, "power.expected_events", minimum=1)
        _finite_positive(self.per_event_sd_bps, "power.per_event_sd_bps")
        _text(self.sd_basis, "power.sd_basis")
        _finite_positive(self.alpha, "power.alpha")
        _finite_positive(self.power, "power.power")
        _choice(self.sided, _SIDES, "power.sided")
        _finite_positive(self.minimum_detectable_effect_bps, "power.minimum_detectable_effect_bps")
        _finite_positive(self.plausible_effect_bps, "power.plausible_effect_bps")
        _text(self.plausibility_basis, "power.plausibility_basis")
        recomputed = self.recomputed_mde_bps()
        if abs(self.minimum_detectable_effect_bps - recomputed) > MDE_TOLERANCE_BPS:
            raise ValueError(
                f"power.minimum_detectable_effect_bps {self.minimum_detectable_effect_bps:g} "
                f"does not match the recomputed {recomputed:.3f} bps "
                f"(tolerance {MDE_TOLERANCE_BPS:g} bp)"
            )

    def recomputed_mde_bps(self) -> float:
        return minimum_detectable_effect_bps(
            self.expected_events, self.per_event_sd_bps, self.alpha, self.power, self.sided
        )

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = require_keys_exact(value, _POWER_KEYS, "power")
        return cls(
            expected_events=data["expected_events"],  # type: ignore[arg-type]
            per_event_sd_bps=data["per_event_sd_bps"],  # type: ignore[arg-type]
            sd_basis=data["sd_basis"],  # type: ignore[arg-type]
            alpha=data["alpha"],  # type: ignore[arg-type]
            power=data["power"],  # type: ignore[arg-type]
            sided=data["sided"],  # type: ignore[arg-type]
            minimum_detectable_effect_bps=data["minimum_detectable_effect_bps"],  # type: ignore[arg-type]
            plausible_effect_bps=data["plausible_effect_bps"],  # type: ignore[arg-type]
            plausibility_basis=data["plausibility_basis"],  # type: ignore[arg-type]
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
    is_v2 = isinstance(raw, dict) and raw.get("contract") == _CONTRACT_V2
    keys = _V2_DOCUMENT_KEYS if is_v2 else _V1_DOCUMENT_KEYS
    return require_keys_exact(raw, keys, "hypothesis document")


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
    if not math.isfinite(result) or result <= 0 or result > MAX_RUN_TIMEOUT_SECONDS:
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
    if document.contract not in _CONTRACTS:
        raise ValueError(f"invalid contract: {document.contract!r}")
    if document.contract == _CONTRACT_V2:
        if not isinstance(document.power, PowerDesign):
            raise ValueError("research-hypothesis-v2 requires a power block")
        if document.power.expected_events < document.minimum_evidence.trades:
            raise ValueError("power.expected_events must be at least minimum_evidence.trades")
    elif document.power is not None:
        raise ValueError("research-hypothesis-v1 must not carry a power block")
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
    _integer(
        document.forward_label_sessions,
        "forward_label_sessions",
        minimum=0,
        maximum=MAX_HOLDING_SESSIONS,
    )
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
    power: PowerDesign | None = None

    def __post_init__(self) -> None:
        _validate_document(self)

    def to_json(self) -> str:
        data = asdict(self)
        if self.power is None:
            # v1 documents never emitted a power key; keep their bytes and digest unchanged.
            del data["power"]
        return to_json(data)

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
            power=PowerDesign.from_mapping(data["power"]) if "power" in data else None,
        )
