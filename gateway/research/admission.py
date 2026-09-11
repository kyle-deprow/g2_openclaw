"""Pure, typed scientific admission for a frozen research hypothesis.

This module deliberately does not read files, call Quantipy, inspect a runtime
route, or mutate driver state.  Its inputs are the already parsed hypothesis,
the immutable outer research record, and typed trusted evidence supplied by a
caller.  In particular, a source label in a hypothesis is a declaration; this
function only proves membership in the caller-supplied execution capability.

The validation-receipt wire view mirrors the host rerun receipt.  Legacy
caller-constructed receipts may still carry separate raw and semantic
evaluation-spec digests; the host wire does not invent those fields and is
parsed with its exact key sets.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise
from typing import Self, cast

from .codec import require_keys_exact, require_sha256, require_str, to_json
from .contracts import HypothesisSpec
from .hypothesis import HypothesisDocument


class AdmissionPurpose(StrEnum):
    DEVELOPMENT_VALIDATION = "DEVELOPMENT_VALIDATION"
    FINAL_HOLDOUT = "FINAL_HOLDOUT"


class OverlapClassification(StrEnum):
    NONE = "NONE"
    KNOWN_EXPOSED = "KNOWN_EXPOSED"
    UNKNOWN_HISTORY = "UNKNOWN_HISTORY"


class AdmissionReason(StrEnum):
    HYPOTHESIS_SPEC_MISMATCH = "HYPOTHESIS_SPEC_MISMATCH"
    SPEC_DIGEST_MISMATCH = "SPEC_DIGEST_MISMATCH"
    PANEL_DIGEST_MISMATCH = "PANEL_DIGEST_MISMATCH"
    RECEIPT_DIGEST_MISMATCH = "RECEIPT_DIGEST_MISMATCH"
    EVALUATION_SPEC_DIGEST_MISMATCH = "EVALUATION_SPEC_DIGEST_MISMATCH"
    RECEIPT_REJECTED = "RECEIPT_REJECTED"
    RECEIPT_INTERNAL_DIGEST_MISMATCH = "RECEIPT_INTERNAL_DIGEST_MISMATCH"
    INSTRUMENT_SET_MISMATCH = "INSTRUMENT_SET_MISMATCH"
    PANEL_BOUNDS_MISMATCH = "PANEL_BOUNDS_MISMATCH"
    EXPOSURE_LEDGER_INVALID = "EXPOSURE_LEDGER_INVALID"
    HOLDOUT_KNOWN_EXPOSED = "HOLDOUT_KNOWN_EXPOSED"
    HOLDOUT_HISTORY_UNKNOWN = "HOLDOUT_HISTORY_UNKNOWN"
    INPUT_CAPABILITY_UNSUPPORTED = "INPUT_CAPABILITY_UNSUPPORTED"
    CAMPAIGN_POLICY_UNSET = "CAMPAIGN_POLICY_UNSET"
    CAMPAIGN_ATTEMPT_CAP_EXCEEDED = "CAMPAIGN_ATTEMPT_CAP_EXCEEDED"
    ANALYSIS_OUTSIDE_PANEL = "ANALYSIS_OUTSIDE_PANEL"
    FEATURE_LOOKBACK_OUTSIDE_PANEL = "FEATURE_LOOKBACK_OUTSIDE_PANEL"
    HOLDING_HORIZON_EXCEEDS_LIMIT = "HOLDING_HORIZON_EXCEEDS_LIMIT"
    SEARCH_BUDGET_INSUFFICIENT = "SEARCH_BUDGET_INSUFFICIENT"
    STOCK_EARNINGS_UNAVAILABLE = "STOCK_EARNINGS_UNAVAILABLE"


class InstrumentClass(StrEnum):
    ETF = "etf"
    STOCK = "stock"


class EarningsCoverageStatus(StrEnum):
    TRUSTED = "TRUSTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


def _date(value: object, name: str) -> str:
    text = require_str(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{name} must use canonical YYYY-MM-DD form")
    return text


def _sha(value: object, name: str) -> str:
    return require_sha256(value, name)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class Instrument:
    """An instrument in the trusted evaluator specification."""

    ticker: str
    instrument_class: InstrumentClass

    def __post_init__(self) -> None:
        require_str(self.ticker, "instrument.ticker")
        if not isinstance(self.instrument_class, InstrumentClass):
            raise ValueError("instrument.instrument_class must be an InstrumentClass")


@dataclass(frozen=True, slots=True)
class EarningsCoverage:
    """Trusted earnings-calendar coverage status supplied by the caller."""

    status: EarningsCoverageStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, EarningsCoverageStatus):
            raise ValueError("earnings coverage status must be an EarningsCoverageStatus")

    @property
    def trusted(self) -> bool:
        return self.status is EarningsCoverageStatus.TRUSTED


@dataclass(frozen=True, slots=True)
class EvaluatorBounds:
    """Typed evaluator bounds and trusted semantic evaluation-spec identity."""

    panel_start: str
    panel_end: str
    instruments: tuple[Instrument, ...]
    semantic_spec_sha256: str
    max_holding_sessions: int
    earnings_coverage: EarningsCoverage
    panel_sessions: tuple[str, ...]

    def __post_init__(self) -> None:
        panel_start = _date(self.panel_start, "evaluator.panel_start")
        panel_end = _date(self.panel_end, "evaluator.panel_end")
        if panel_start > panel_end:
            raise ValueError("evaluator panel bounds are reversed")
        object.__setattr__(self, "panel_start", panel_start)
        object.__setattr__(self, "panel_end", panel_end)
        _sha(self.semantic_spec_sha256, "evaluator.semantic_spec_sha256")
        if type(self.max_holding_sessions) is not int or self.max_holding_sessions < 0:
            raise ValueError("evaluator.max_holding_sessions must be non-negative")
        if not isinstance(self.instruments, tuple) or not self.instruments:
            raise ValueError("evaluator.instruments must be a non-empty tuple")
        if any(not isinstance(item, Instrument) for item in self.instruments):
            raise ValueError("evaluator.instruments must contain Instrument values")
        tickers = [item.ticker for item in self.instruments]
        if len(set(tickers)) != len(tickers):
            raise ValueError("evaluator.instruments must have unique tickers")
        if not isinstance(self.earnings_coverage, EarningsCoverage):
            raise ValueError("evaluator.earnings_coverage must be EarningsCoverage")
        if not isinstance(self.panel_sessions, tuple) or not self.panel_sessions:
            raise ValueError("evaluator.panel_sessions must be a non-empty tuple")
        sessions = tuple(_date(item, "evaluator.panel_sessions") for item in self.panel_sessions)
        if any(left >= right for left, right in pairwise(sessions)):
            raise ValueError("evaluator.panel_sessions must be strictly increasing")
        if sessions[0] != panel_start or sessions[-1] != panel_end:
            raise ValueError("evaluator.panel_sessions must span the panel bounds")
        object.__setattr__(self, "panel_sessions", sessions)


@dataclass(frozen=True, slots=True)
class DividendCoverage:
    """The exact dividend coverage object present in the receipt."""

    action_type: str
    start_date: str
    end_date: str
    completed_at: str

    def __post_init__(self) -> None:
        require_str(self.action_type, "receipt.dividends_coverage.action_type")
        start = _date(self.start_date, "receipt.dividends_coverage.start_date")
        end = _date(self.end_date, "receipt.dividends_coverage.end_date")
        if start > end:
            raise ValueError("receipt dividend coverage is reversed")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        require_str(self.completed_at, "receipt.dividends_coverage.completed_at")


@dataclass(frozen=True, slots=True)
class ValidationReceipt:
    """Typed view of the ``research-price-panel-receipt-v2`` wire."""

    receipt_sha256: str
    request_sha256: str
    panel_sha256: str
    instruments: tuple[str, ...]
    acceptance_class: str
    contract_version: str | None = None
    request_contract_version: str | None = None
    request_start: str | None = None
    request_end: str | None = None
    timeframe: str | None = None
    market_hours: str | None = None
    coverage_contract_version: str | None = None
    coverage_encoding: str | None = None
    coverage_compressed_size: int | None = None
    coverage_expanded_size: int | None = None
    coverage_compression_ratio: float | None = None
    coverage_sha256: str | None = None
    coverage_payload: str | None = None
    hydrated_at: str | None = None
    exported_at: str | None = None
    snapshot_sha256: str | None = None
    spec_sha256_raw: str | None = None
    spec_sha256_semantic: str | None = None
    universe_sha256: str | None = None
    dividends_sha256: str | None = None
    universe_file_sha256: str | None = None
    validation_dividends_sha256: str | None = None
    panel_start: str | None = None
    panel_end: str | None = None
    verdict: str | None = None
    reasons: tuple[str, ...] = ()
    dividends_coverage: DividendCoverage | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.snapshot_sha256, "receipt.snapshot_sha256"),
            (self.spec_sha256_raw, "receipt.spec_sha256_raw"),
            (self.spec_sha256_semantic, "receipt.spec_sha256_semantic"),
            (self.universe_sha256, "receipt.universe_sha256"),
            (self.dividends_sha256, "receipt.dividends_sha256"),
            (self.receipt_sha256, "receipt.receipt_sha256"),
            (self.request_sha256, "receipt.request_sha256"),
            (self.panel_sha256, "receipt.panel_sha256"),
            (self.universe_file_sha256, "receipt.universe_file_sha256"),
            (self.validation_dividends_sha256, "receipt.validation_dividends_sha256"),
        ):
            if value is not None:
                _sha(value, name)
        if (self.panel_start is None) != (self.panel_end is None):
            raise ValueError("receipt panel bounds must be both present or absent")
        if self.panel_start is not None and self.panel_end is not None:
            start = _date(self.panel_start, "receipt.start_session")
            end = _date(self.panel_end, "receipt.end_session")
            if start > end:
                raise ValueError("receipt panel bounds are reversed")
            object.__setattr__(self, "panel_start", start)
            object.__setattr__(self, "panel_end", end)
        if not isinstance(self.instruments, tuple) or not self.instruments:
            raise ValueError("receipt.instruments must be a non-empty tuple")
        if any(not isinstance(item, str) or not item for item in self.instruments):
            raise ValueError("receipt.instruments must contain non-empty strings")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("receipt.instruments must be unique")
        if self.verdict is not None and self.verdict not in {"PASS", "FAIL"}:
            raise ValueError("receipt.verdict must be PASS or FAIL")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(item, str) or not item for item in self.reasons
        ):
            raise ValueError("receipt.reasons must contain strings")
        require_str(self.acceptance_class, "receipt.acceptance_class")
        if self.dividends_coverage is not None and not isinstance(
            self.dividends_coverage, DividendCoverage
        ):
            raise ValueError("receipt.dividends_coverage must be DividendCoverage")
        for value, name in (
            (self.contract_version, "receipt.contract_version"),
            (self.request_contract_version, "receipt.request.contract_version"),
            (self.request_start, "receipt.request.start"),
            (self.request_end, "receipt.request.end"),
            (self.timeframe, "receipt.request.timeframe"),
            (self.market_hours, "receipt.request.market_hours"),
            (self.coverage_contract_version, "receipt.coverage.contract_version"),
            (self.coverage_encoding, "receipt.coverage.encoding"),
            (self.coverage_payload, "receipt.coverage.payload"),
            (self.hydrated_at, "receipt.hydrated_at"),
            (self.exported_at, "receipt.exported_at"),
        ):
            if value is not None:
                require_str(value, name)
        if self.coverage_compressed_size is not None:
            _nonnegative_int(self.coverage_compressed_size, "receipt.coverage.compressed_size")
        if self.coverage_expanded_size is not None:
            _nonnegative_int(self.coverage_expanded_size, "receipt.coverage.expanded_size")
        if self.coverage_compression_ratio is not None and (
            isinstance(self.coverage_compression_ratio, bool)
            or not isinstance(self.coverage_compression_ratio, (int, float))
            or not math.isfinite(float(self.coverage_compression_ratio))
            or self.coverage_compression_ratio <= 0
        ):
            raise ValueError("receipt.coverage.compression_ratio must be positive and finite")
        if self.coverage_sha256 is not None:
            _sha(self.coverage_sha256, "receipt.coverage_sha256")

    @classmethod
    def from_wire(
        cls,
        value: object,
        *,
        acceptance_class: str,
        receipt_sha256: str,
    ) -> Self:
        """Parse the exact host receipt, with its external artifact digest."""

        outer = require_keys_exact(
            value,
            {
                "contract_version",
                "coverage",
                "coverage_sha256",
                "exported_at",
                "hydrated_at",
                "panel_sha256",
                "request",
                "request_sha256",
            },
            "price panel receipt",
        )
        request = require_keys_exact(
            outer["request"],
            {
                "contract_version",
                "end",
                "market_hours",
                "tickers",
                "start",
                "timeframe",
            },
        )
        coverage = require_keys_exact(
            outer["coverage"],
            {
                "compressed_size",
                "compression_ratio",
                "contract_version",
                "coverage_sha256",
                "encoding",
                "expanded_size",
                "payload",
            },
            "price panel receipt.coverage",
        )
        tickers = request["tickers"]
        if not isinstance(tickers, list):
            raise ValueError("price panel receipt.request.tickers must be an array")
        digest = _sha(receipt_sha256, "receipt_sha256")
        receipt = cls(
            receipt_sha256=digest,
            request_sha256=cast(str, outer["request_sha256"]),
            panel_sha256=cast(str, outer["panel_sha256"]),
            instruments=tuple(cast(str, item) for item in tickers),
            acceptance_class=acceptance_class,
            contract_version=cast(str, outer["contract_version"]),
            request_contract_version=cast(str, request["contract_version"]),
            request_start=cast(str, request["start"]),
            request_end=cast(str, request["end"]),
            timeframe=cast(str, request["timeframe"]),
            market_hours=cast(str, request["market_hours"]),
            coverage_contract_version=cast(str, coverage["contract_version"]),
            coverage_encoding=cast(str, coverage["encoding"]),
            coverage_compressed_size=cast(int, coverage["compressed_size"]),
            coverage_expanded_size=cast(int, coverage["expanded_size"]),
            coverage_compression_ratio=cast(float, coverage["compression_ratio"]),
            coverage_sha256=cast(str, outer["coverage_sha256"]),
            coverage_payload=cast(str, coverage["payload"]),
            hydrated_at=cast(str, outer["hydrated_at"]),
            exported_at=cast(str, outer["exported_at"]),
        )
        if receipt.coverage_sha256 != cast(str, coverage["coverage_sha256"]):
            raise ValueError("receipt coverage digests disagree")
        return receipt


@dataclass(frozen=True, slots=True)
class ExposureRange:
    start: str
    end: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _date(self.start, "exposure.start"))
        object.__setattr__(self, "end", _date(self.end, "exposure.end"))


@dataclass(frozen=True, slots=True)
class ExposureLedger:
    """Immutable exposure evidence; semantic validity is checked at admission."""

    known_exposed_ranges: tuple[ExposureRange, ...]
    history_unknown: bool
    trial_count: int | None
    ledger_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.known_exposed_ranges, tuple) or any(
            not isinstance(item, ExposureRange) for item in self.known_exposed_ranges
        ):
            raise ValueError("exposure known_exposed_ranges must be a tuple")
        if not isinstance(self.history_unknown, bool):
            raise ValueError("exposure history_unknown must be boolean")
        if self.trial_count is not None:
            _nonnegative_int(self.trial_count, "exposure.trial_count")
        if not isinstance(self.ledger_sha256, str):
            raise ValueError("exposure.ledger_sha256 must be a string")

    def payload(self) -> dict[str, object]:
        return {
            "history_unknown": self.history_unknown,
            "known_exposed_ranges": [
                {"end": item.end, "start": item.start} for item in self.known_exposed_ranges
            ],
            "trial_count": self.trial_count,
        }

    @property
    def computed_sha256(self) -> str:
        return hashlib.sha256(to_json(self.payload()).encode("utf-8")).hexdigest()

    @property
    def digest_matches(self) -> bool:
        return self.ledger_sha256 == self.computed_sha256

    @property
    def structurally_valid(self) -> bool:
        ordered = sorted(self.known_exposed_ranges, key=lambda item: (item.start, item.end))
        return all(item.start <= item.end for item in ordered) and all(
            left.end < right.start for left, right in pairwise(ordered)
        )

    @property
    def valid(self) -> bool:
        return self.digest_matches and self.structurally_valid


@dataclass(frozen=True, slots=True)
class ExecutionCapability:
    """Caller-provided input capability, preserved in the decision."""

    sources: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sources, frozenset)
            or not self.sources
            or any(not isinstance(item, str) or not item for item in self.sources)
        ):
            raise ValueError("execution capability sources must be a non-empty frozenset")


@dataclass(frozen=True, slots=True)
class CampaignPolicy:
    """Explicit operator policy; an unset policy leaves the campaign paused."""

    is_set: bool
    attempt_cap: int | None = None
    wall_clock_cap_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.is_set, bool):
            raise ValueError("campaign policy is_set must be boolean")
        if not self.is_set:
            if self.attempt_cap is not None or self.wall_clock_cap_seconds is not None:
                raise ValueError("unset campaign policy cannot carry caps")
            return
        if self.attempt_cap is None:
            raise ValueError("set campaign policy requires attempt_cap")
        _positive_int(self.attempt_cap, "campaign.attempt_cap")
        if self.wall_clock_cap_seconds is not None and (
            isinstance(self.wall_clock_cap_seconds, bool)
            or not isinstance(self.wall_clock_cap_seconds, (int, float))
            or not math.isfinite(float(self.wall_clock_cap_seconds))
            or self.wall_clock_cap_seconds <= 0
        ):
            raise ValueError("campaign.wall_clock_cap_seconds must be a positive finite number")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Canonical, immutable result of pure admission."""

    admitted: bool
    purpose: AdmissionPurpose
    reason: AdmissionReason | None
    detail: str | None
    hypothesis_document: HypothesisDocument
    hypothesis_spec: HypothesisSpec
    hypothesis_sha256: str
    spec_sha256: str
    panel_sha256: str
    receipt_sha256: str
    evaluation_spec_sha256: str
    max_attempts: int
    receipt: ValidationReceipt
    exposure_ledger_sha256: str | None
    overlap_classification: OverlapClassification
    execution_capability: ExecutionCapability
    campaign_policy: CampaignPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise ValueError("decision.admitted must be boolean")
        if not isinstance(self.purpose, AdmissionPurpose):
            raise ValueError("decision.purpose must be AdmissionPurpose")
        if self.admitted and self.reason is not None:
            raise ValueError("admitted decision cannot have a reason")
        if not self.admitted and self.reason is None:
            raise ValueError("refused decision requires a reason")
        if self.detail is not None:
            require_str(self.detail, "decision.detail")
        if not isinstance(self.hypothesis_document, HypothesisDocument):
            raise ValueError("decision.hypothesis_document must be HypothesisDocument")
        if not isinstance(self.hypothesis_spec, HypothesisSpec):
            raise ValueError("decision.hypothesis_spec must be HypothesisSpec")
        for value, name in (
            (self.hypothesis_sha256, "decision.hypothesis_sha256"),
            (self.spec_sha256, "decision.spec_sha256"),
            (self.panel_sha256, "decision.panel_sha256"),
            (self.receipt_sha256, "decision.receipt_sha256"),
            (self.evaluation_spec_sha256, "decision.evaluation_spec_sha256"),
        ):
            _sha(value, name)
        _positive_int(self.max_attempts, "decision.max_attempts")
        if self.exposure_ledger_sha256 is not None:
            _sha(self.exposure_ledger_sha256, "decision.exposure_ledger_sha256")
        if not isinstance(self.overlap_classification, OverlapClassification):
            raise ValueError("decision.overlap_classification must be OverlapClassification")
        if not isinstance(self.execution_capability, ExecutionCapability):
            raise ValueError("decision.execution_capability must be ExecutionCapability")
        if not isinstance(self.campaign_policy, CampaignPolicy):
            raise ValueError("decision.campaign_policy must be CampaignPolicy")

    def mapping(self) -> dict[str, object]:
        dividends_coverage = None
        if self.receipt.dividends_coverage is not None:
            dividends_coverage = {
                "action_type": self.receipt.dividends_coverage.action_type,
                "completed_at": self.receipt.dividends_coverage.completed_at,
                "end_date": self.receipt.dividends_coverage.end_date,
                "start_date": self.receipt.dividends_coverage.start_date,
            }
        receipt = {
            "acceptance_class": self.receipt.acceptance_class,
            "contract_version": self.receipt.contract_version,
            "coverage_compressed_size": self.receipt.coverage_compressed_size,
            "coverage_compression_ratio": self.receipt.coverage_compression_ratio,
            "coverage_contract_version": self.receipt.coverage_contract_version,
            "coverage_encoding": self.receipt.coverage_encoding,
            "coverage_expanded_size": self.receipt.coverage_expanded_size,
            "coverage_payload": self.receipt.coverage_payload,
            "coverage_sha256": self.receipt.coverage_sha256,
            "dividends_coverage": dividends_coverage,
            "dividends_sha256": self.receipt.dividends_sha256,
            "exported_at": self.receipt.exported_at,
            "hydrated_at": self.receipt.hydrated_at,
            "instruments": list(self.receipt.instruments),
            "panel_end": self.receipt.panel_end,
            "panel_sha256": self.receipt.panel_sha256,
            "panel_start": self.receipt.panel_start,
            "receipt_sha256": self.receipt.receipt_sha256,
            "reasons": list(self.receipt.reasons),
            "request_contract_version": self.receipt.request_contract_version,
            "request_end": self.receipt.request_end,
            "request_sha256": self.receipt.request_sha256,
            "request_start": self.receipt.request_start,
            "snapshot_sha256": self.receipt.snapshot_sha256,
            "spec_sha256_raw": self.receipt.spec_sha256_raw,
            "spec_sha256_semantic": self.receipt.spec_sha256_semantic,
            "timeframe": self.receipt.timeframe,
            "universe_file_sha256": self.receipt.universe_file_sha256,
            "universe_sha256": self.receipt.universe_sha256,
            "validation_dividends_sha256": self.receipt.validation_dividends_sha256,
            "verdict": self.receipt.verdict,
            "market_hours": self.receipt.market_hours,
        }
        return {
            "admitted": self.admitted,
            "campaign_policy": {
                "attempt_cap": self.campaign_policy.attempt_cap,
                "is_set": self.campaign_policy.is_set,
                "wall_clock_cap_seconds": self.campaign_policy.wall_clock_cap_seconds,
            },
            "detail": self.detail,
            "evaluation_spec_sha256": self.evaluation_spec_sha256,
            "execution_capability": sorted(self.execution_capability.sources),
            "exposure_ledger_sha256": self.exposure_ledger_sha256,
            "hypothesis_document_json": self.hypothesis_document.to_json(),
            "hypothesis_sha256": self.hypothesis_sha256,
            "hypothesis_spec_json": self.hypothesis_spec.to_json(),
            "max_attempts": self.max_attempts,
            "overlap_classification": self.overlap_classification.value,
            "panel_sha256": self.panel_sha256,
            "purpose": self.purpose.value,
            "reason": None if self.reason is None else self.reason.value,
            "receipt": receipt,
            "receipt_sha256": self.receipt_sha256,
            "spec_sha256": self.spec_sha256,
        }

    def to_json(self) -> str:
        return to_json(self.mapping())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _decision(
    *,
    admitted: bool,
    purpose: AdmissionPurpose,
    reason: AdmissionReason | None,
    detail: str | None,
    hypothesis: HypothesisDocument,
    spec: HypothesisSpec,
    receipt: ValidationReceipt,
    ledger: ExposureLedger | None,
    overlap: OverlapClassification,
    capability: ExecutionCapability,
    policy: CampaignPolicy,
) -> AdmissionDecision:
    ledger_digest: str | None = None
    if isinstance(ledger, ExposureLedger):
        try:
            _sha(ledger.ledger_sha256, "exposure.ledger_sha256")
        except ValueError:
            pass
        else:
            ledger_digest = ledger.ledger_sha256
    return AdmissionDecision(
        admitted=admitted,
        purpose=purpose,
        reason=reason,
        detail=detail,
        hypothesis_document=hypothesis,
        hypothesis_spec=spec,
        hypothesis_sha256=hypothesis.sha256,
        spec_sha256=spec.spec_sha256,
        panel_sha256=spec.panel_sha256,
        receipt_sha256=spec.receipt_sha256,
        evaluation_spec_sha256=spec.evaluation_spec_sha256,
        max_attempts=spec.max_attempts,
        receipt=receipt,
        exposure_ledger_sha256=ledger_digest,
        overlap_classification=overlap,
        execution_capability=capability,
        campaign_policy=policy,
    )


def _unsupported_feature(
    hypothesis: HypothesisDocument, capability: ExecutionCapability
) -> tuple[str, str] | None:
    for feature in hypothesis.features:
        if feature.source == "panel":
            required = {"panel"}
        elif feature.source == "reddit":
            required = {"reddit"}
        elif feature.source == "derived":
            # The accepted parser currently has no source-membership field for
            # derived features.  Do not pretend that the label proves its
            # dependencies; a future accepted representation may expose a
            # ``sources`` attribute and can then use the same membership test.
            sources = getattr(feature, "sources", None)
            if not isinstance(sources, (tuple, frozenset, list)) or not sources:
                return (
                    feature.name,
                    "derived feature sources are not exposed by the accepted parser",
                )
            required = {str(source) for source in sources}
        else:
            required = {feature.source}
        if not required.issubset(capability.sources):
            return feature.name, f"requires sources {sorted(required)!r}"
    return None


def _ledger_classification(
    ledger: ExposureLedger,
    hypothesis: HypothesisDocument,
) -> OverlapClassification:
    if ledger.history_unknown or ledger.trial_count is None:
        return OverlapClassification.UNKNOWN_HISTORY
    analysis = (hypothesis.analysis.start, hypothesis.analysis.end)
    for exposed in ledger.known_exposed_ranges:
        if exposed.start <= analysis[1] and analysis[0] <= exposed.end:
            return OverlapClassification.KNOWN_EXPOSED
    return OverlapClassification.NONE


def admit_hypothesis(
    hypothesis: HypothesisDocument,
    hypothesis_spec: HypothesisSpec,
    receipt: ValidationReceipt,
    exposure_ledger: ExposureLedger | None,
    evaluator_bounds: EvaluatorBounds,
    campaign_policy: CampaignPolicy,
    execution_capability: ExecutionCapability,
) -> AdmissionDecision:
    """Return a fail-closed, canonical admission decision.

    All arguments are typed caller evidence.  No argument is discovered from a
    path or inferred from a provider.  Admission of a new attempt is separate
    from completion, so an exhausted allocation cannot deadlock a final run.
    """

    try:
        purpose = AdmissionPurpose(hypothesis.purpose)
    except ValueError:
        # The parser rejects this before admission; retain a total function for
        # callers that construct a typed document through another boundary.
        purpose = AdmissionPurpose.DEVELOPMENT_VALIDATION

    overlap = OverlapClassification.UNKNOWN_HISTORY

    if hypothesis_spec.spec_json != hypothesis.to_json():
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.HYPOTHESIS_SPEC_MISMATCH,
            detail="parsed hypothesis JSON differs from the immutable outer spec",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if hypothesis.sha256 != hypothesis_spec.spec_sha256:
        reason = AdmissionReason.SPEC_DIGEST_MISMATCH
        detail = "outer spec_sha256 does not match parsed hypothesis SHA"
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=reason,
            detail=detail,
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if receipt.panel_sha256 != hypothesis_spec.panel_sha256:
        reason = AdmissionReason.PANEL_DIGEST_MISMATCH
        detail = "trusted receipt panel_sha256 differs from outer panel_sha256"
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=reason,
            detail=detail,
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if receipt.receipt_sha256 != hypothesis_spec.receipt_sha256:
        reason = AdmissionReason.RECEIPT_DIGEST_MISMATCH
        detail = "trusted receipt digest differs from outer receipt_sha256"
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=reason,
            detail=detail,
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if (
        receipt.spec_sha256_raw is not None
        and receipt.spec_sha256_raw != hypothesis_spec.evaluation_spec_sha256
    ):
        reason = AdmissionReason.EVALUATION_SPEC_DIGEST_MISMATCH
        detail = "trusted receipt raw evaluation-spec digest differs from outer digest"
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=reason,
            detail=detail,
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if (
        receipt.spec_sha256_semantic is not None
        and receipt.spec_sha256_semantic != evaluator_bounds.semantic_spec_sha256
    ):
        reason = AdmissionReason.EVALUATION_SPEC_DIGEST_MISMATCH
        detail = "trusted receipt semantic evaluation-spec digest differs from evaluator bounds"
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=reason,
            detail=detail,
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if (
        receipt.dividends_sha256 is not None
        and receipt.validation_dividends_sha256 is not None
        and receipt.dividends_sha256 != receipt.validation_dividends_sha256
    ) or (
        receipt.universe_sha256 is not None
        and receipt.universe_file_sha256 is not None
        and receipt.universe_sha256 != receipt.universe_file_sha256
    ):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.RECEIPT_INTERNAL_DIGEST_MISMATCH,
            detail="trusted receipt contains disagreeing duplicated digests",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if receipt.panel_start is not None and (
        receipt.panel_start != evaluator_bounds.panel_start
        or receipt.panel_end != evaluator_bounds.panel_end
    ):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.PANEL_BOUNDS_MISMATCH,
            detail="trusted receipt panel bounds differ from evaluator bounds",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if (receipt.verdict is not None and receipt.verdict != "PASS") or receipt.reasons:
        reason = AdmissionReason.RECEIPT_REJECTED
        detail = "trusted validation receipt is not an acceptance PASS"
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=reason,
            detail=detail,
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )

    if not isinstance(exposure_ledger, ExposureLedger) or not exposure_ledger.valid:
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.EXPOSURE_LEDGER_INVALID,
            detail=(
                "exposure ledger is missing, malformed, reversed, overlapping, or digest-mismatched"
            ),
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    overlap = _ledger_classification(exposure_ledger, hypothesis)

    if not campaign_policy.is_set:
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.CAMPAIGN_POLICY_UNSET,
            detail="campaign remains paused until an explicit operator policy is set",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if campaign_policy.attempt_cap is None or (
        campaign_policy.attempt_cap < hypothesis_spec.max_attempts
    ):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.CAMPAIGN_ATTEMPT_CAP_EXCEEDED,
            detail="operator attempt cap is below the immutable hypothesis max_attempts",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )

    unsupported = _unsupported_feature(hypothesis, execution_capability)
    if unsupported is not None:
        name, why = unsupported
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.INPUT_CAPABILITY_UNSUPPORTED,
            detail=f"feature {name!r} is unsupported: {why}",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )

    expected_tickers = tuple(item.ticker for item in evaluator_bounds.instruments)
    if set(receipt.instruments) != set(expected_tickers):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.INSTRUMENT_SET_MISMATCH,
            detail="trusted receipt instruments differ from evaluator bounds",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if (
        hypothesis.analysis.start < evaluator_bounds.panel_start
        or hypothesis.analysis.end > evaluator_bounds.panel_end
        or hypothesis.evaluation.start < evaluator_bounds.panel_start
        or hypothesis.evaluation.end > evaluator_bounds.panel_end
    ):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.ANALYSIS_OUTSIDE_PANEL,
            detail="analysis/evaluation range is outside the trusted panel bounds",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    try:
        analysis_index = evaluator_bounds.panel_sessions.index(hypothesis.analysis.start)
    except ValueError:
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL,
            detail="analysis start is not an exact trusted panel session",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if any(analysis_index < feature.lookback_sessions for feature in hypothesis.features):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL,
            detail="feature lookback requires sessions before the trusted panel start",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if (
        hypothesis.forward_label_sessions > 5
        or evaluator_bounds.max_holding_sessions > 5
        or hypothesis.forward_label_sessions > evaluator_bounds.max_holding_sessions
    ):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.HOLDING_HORIZON_EXCEEDS_LIMIT,
            detail="holding horizon exceeds the fixed five-session evaluator limit",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if hypothesis.search_budget_evaluations < len(hypothesis.variants):
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.SEARCH_BUDGET_INSUFFICIENT,
            detail="declared variants exceed the declared search budget",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    has_stock = any(
        item.instrument_class is InstrumentClass.STOCK for item in evaluator_bounds.instruments
    )
    if has_stock and not evaluator_bounds.earnings_coverage.trusted:
        return _decision(
            admitted=False,
            purpose=purpose,
            reason=AdmissionReason.STOCK_EARNINGS_UNAVAILABLE,
            detail="single-stock instruments require trusted earnings coverage",
            hypothesis=hypothesis,
            spec=hypothesis_spec,
            receipt=receipt,
            ledger=exposure_ledger,
            overlap=overlap,
            capability=execution_capability,
            policy=campaign_policy,
        )
    if purpose is AdmissionPurpose.FINAL_HOLDOUT:
        holdout_reason: AdmissionReason | None
        holdout_detail: str | None
        if overlap is OverlapClassification.KNOWN_EXPOSED:
            holdout_reason = AdmissionReason.HOLDOUT_KNOWN_EXPOSED
            holdout_detail = "final holdout overlaps a known exposed range"
        elif overlap is OverlapClassification.UNKNOWN_HISTORY:
            holdout_reason = AdmissionReason.HOLDOUT_HISTORY_UNKNOWN
            holdout_detail = "final holdout history or trial count is unknown"
        else:
            holdout_reason = None
            holdout_detail = None
        if holdout_reason is not None:
            return _decision(
                admitted=False,
                purpose=purpose,
                reason=holdout_reason,
                detail=holdout_detail,
                hypothesis=hypothesis,
                spec=hypothesis_spec,
                receipt=receipt,
                ledger=exposure_ledger,
                overlap=overlap,
                capability=execution_capability,
                policy=campaign_policy,
            )
    return _decision(
        admitted=True,
        purpose=purpose,
        reason=None,
        detail=None,
        hypothesis=hypothesis,
        spec=hypothesis_spec,
        receipt=receipt,
        ledger=exposure_ledger,
        overlap=overlap,
        capability=execution_capability,
        policy=campaign_policy,
    )


admit = admit_hypothesis
