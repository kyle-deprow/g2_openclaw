"""Strict parsing for Quantipy dynamic price coverage receipts.

The receipt is produced by Quantipy's shared coverage validator.  This module
only accepts canonical, self-consistent receipt JSON.  Gate authority is applied
by the runner after it mechanically binds the receipt to independent preflight,
universe, and price-hydration evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

DYNAMIC_PRICE_COVERAGE_CONTRACT_VERSION = "dynamic-price-coverage-v1"
PRICE_COVERAGE_SOURCE_CONTRACT_VERSION = "price-coverage-v1"
PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL = "platform_coverage_contract_mismatch"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


class PlatformCoverageValidationError(ValueError):
    """Raised when a dynamic price coverage receipt is malformed."""


class PlatformCoverageScope(StrEnum):
    FULL_UNION_HYDRATION = "full_union_hydration"
    PIT_ACTIVE_ROSTER = "pit_active_roster"


class PlatformCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"


class PlatformCoverageMarketHours(StrEnum):
    ALL = "all"
    REGULAR = "regular"
    EXTENDED = "extended"


class PlatformCoverageSourceProvider(StrEnum):
    MASSIVE = "massive"
    DATABENTO = "databento"


class PlatformCoverageViolationCode(StrEnum):
    UNEXPECTED_TICKER = "unexpected_ticker"
    UNEXPECTED_SYMBOL_SESSION = "unexpected_symbol_session"
    MISSING_HYDRATED_SYMBOL_SESSION = "missing_hydrated_symbol_session"
    PROVIDER_EMPTY_ACTIVE_SYMBOL_SESSION = "provider_empty_active_symbol_session"
    MISSING_ACTIVE_SYMBOL_SESSION = "missing_active_symbol_session"


_RECEIPT_FIELDS = (
    "contract_version",
    "source_contract_version",
    "scope",
    "status",
    "requested_start_date",
    "requested_end_date",
    "timeframe",
    "market_hours",
    "source_requested_start_date",
    "source_requested_end_date",
    "source_timeframe",
    "source_market_hours",
    "source_provider",
    "member_union_digest",
    "requested_sessions_digest",
    "pit_active_roster_digest",
    "source_price_coverage_response_digest",
    "member_union_count",
    "requested_session_count",
    "hydrated_symbol_sessions",
    "observed_hydrated_symbol_sessions",
    "provider_empty_hydrated_symbol_sessions",
    "missing_hydrated_symbol_sessions",
    "active_symbol_sessions",
    "observed_active_symbol_sessions",
    "provider_empty_active_symbol_sessions",
    "missing_active_symbol_sessions",
    "inactive_union_symbol_sessions",
    "unexpected_ticker_count",
    "unexpected_session_count",
    "violation_codes",
    "receipt_digest",
)


def canonical_dynamic_price_coverage_digest(receipt: Mapping[str, object]) -> str:
    """Return the SHA-256 digest over the canonical receipt body, excluding itself."""
    body = {field: receipt[field] for field in _RECEIPT_FIELDS if field != "receipt_digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_requested_sessions_digest(requested_sessions: Sequence[date]) -> str:
    """Mirror Quantipy's digest of the ordered XNYS session-label sequence."""
    if not requested_sessions or any(type(session) is not date for session in requested_sessions):
        raise PlatformCoverageValidationError(
            "requested sessions must be a non-empty sequence of plain dates"
        )
    canonical_sessions = tuple(requested_sessions)
    if canonical_sessions != tuple(sorted(set(canonical_sessions))):
        raise PlatformCoverageValidationError(
            "requested sessions must be unique and canonically ordered"
        )
    canonical = json.dumps(
        [session.isoformat() for session in canonical_sessions],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_mapping(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise PlatformCoverageValidationError("dynamic price coverage receipt must be an object")
    return raw


def _require_exact_keys(data: Mapping[str, object]) -> None:
    expected = set(_RECEIPT_FIELDS)
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise PlatformCoverageValidationError(
            f"dynamic price coverage receipt has invalid fields: {'; '.join(details)}"
        )


def _require_string(data: Mapping[str, object], field_name: str) -> str:
    value = data[field_name]
    if not isinstance(value, str):
        raise PlatformCoverageValidationError(f"{field_name} must be a string")
    return value


def _require_count(data: Mapping[str, object], field_name: str) -> int:
    value = data[field_name]
    if type(value) is not int or value < 0:
        raise PlatformCoverageValidationError(f"{field_name} must be a non-negative integer")
    return value


def _require_iso_date(data: Mapping[str, object], field_name: str) -> str:
    value = _require_string(data, field_name)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PlatformCoverageValidationError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise PlatformCoverageValidationError(f"{field_name} must be a canonical ISO date")
    return value


def _require_text(data: Mapping[str, object], field_name: str) -> str:
    value = _require_string(data, field_name)
    if _TEXT_RE.fullmatch(value) is None:
        raise PlatformCoverageValidationError(f"{field_name} is not a supported identifier")
    return value


def _parse_violations(data: Mapping[str, object]) -> tuple[PlatformCoverageViolationCode, ...]:
    raw = data["violation_codes"]
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise PlatformCoverageValidationError("violation_codes must be a list")
    parsed: list[PlatformCoverageViolationCode] = []
    for item in raw:
        if not isinstance(item, str):
            raise PlatformCoverageValidationError("violation_codes must contain strings")
        try:
            parsed.append(PlatformCoverageViolationCode(item))
        except ValueError as exc:
            raise PlatformCoverageValidationError(
                "violation_codes contains an unsupported code"
            ) from exc
    if tuple(sorted(set(parsed), key=lambda value: value.value)) != tuple(parsed):
        raise PlatformCoverageValidationError("violation_codes must be sorted and unique")
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class DynamicPriceCoverageReceipt:
    """Canonical Quantipy evidence for full-union dynamic price coverage."""

    contract_version: str
    source_contract_version: str
    scope: PlatformCoverageScope
    status: PlatformCoverageStatus
    requested_start_date: str
    requested_end_date: str
    timeframe: str
    market_hours: PlatformCoverageMarketHours
    source_requested_start_date: str
    source_requested_end_date: str
    source_timeframe: str
    source_market_hours: PlatformCoverageMarketHours
    source_provider: PlatformCoverageSourceProvider
    member_union_digest: str
    requested_sessions_digest: str
    pit_active_roster_digest: str
    source_price_coverage_response_digest: str
    member_union_count: int
    requested_session_count: int
    hydrated_symbol_sessions: int
    observed_hydrated_symbol_sessions: int
    provider_empty_hydrated_symbol_sessions: int
    missing_hydrated_symbol_sessions: int
    active_symbol_sessions: int
    observed_active_symbol_sessions: int
    provider_empty_active_symbol_sessions: int
    missing_active_symbol_sessions: int
    inactive_union_symbol_sessions: int
    unexpected_ticker_count: int
    unexpected_session_count: int
    violation_codes: tuple[PlatformCoverageViolationCode, ...]
    receipt_digest: str

    @classmethod
    def from_dict(cls, raw: object) -> DynamicPriceCoverageReceipt:
        data = _require_mapping(raw)
        _require_exact_keys(data)
        contract_version = _require_string(data, "contract_version")
        if contract_version != DYNAMIC_PRICE_COVERAGE_CONTRACT_VERSION:
            raise PlatformCoverageValidationError("contract_version is unsupported")
        source_contract_version = _require_string(data, "source_contract_version")
        if source_contract_version != PRICE_COVERAGE_SOURCE_CONTRACT_VERSION:
            raise PlatformCoverageValidationError("source_contract_version is unsupported")
        try:
            scope = PlatformCoverageScope(_require_string(data, "scope"))
            status = PlatformCoverageStatus(_require_string(data, "status"))
            market_hours = PlatformCoverageMarketHours(_require_string(data, "market_hours"))
            source_market_hours = PlatformCoverageMarketHours(
                _require_string(data, "source_market_hours")
            )
            source_provider = PlatformCoverageSourceProvider(
                _require_string(data, "source_provider")
            )
        except ValueError as exc:
            raise PlatformCoverageValidationError(
                "scope, status, market_hours, source_market_hours, or source_provider "
                "is unsupported"
            ) from exc
        receipt = cls(
            contract_version=contract_version,
            source_contract_version=source_contract_version,
            scope=scope,
            status=status,
            requested_start_date=_require_iso_date(data, "requested_start_date"),
            requested_end_date=_require_iso_date(data, "requested_end_date"),
            timeframe=_require_text(data, "timeframe"),
            market_hours=market_hours,
            source_requested_start_date=_require_iso_date(data, "source_requested_start_date"),
            source_requested_end_date=_require_iso_date(data, "source_requested_end_date"),
            source_timeframe=_require_text(data, "source_timeframe"),
            source_market_hours=source_market_hours,
            source_provider=source_provider,
            member_union_digest=_require_string(data, "member_union_digest"),
            requested_sessions_digest=_require_string(data, "requested_sessions_digest"),
            pit_active_roster_digest=_require_string(data, "pit_active_roster_digest"),
            source_price_coverage_response_digest=_require_string(
                data, "source_price_coverage_response_digest"
            ),
            member_union_count=_require_count(data, "member_union_count"),
            requested_session_count=_require_count(data, "requested_session_count"),
            hydrated_symbol_sessions=_require_count(data, "hydrated_symbol_sessions"),
            observed_hydrated_symbol_sessions=_require_count(
                data, "observed_hydrated_symbol_sessions"
            ),
            provider_empty_hydrated_symbol_sessions=_require_count(
                data, "provider_empty_hydrated_symbol_sessions"
            ),
            missing_hydrated_symbol_sessions=_require_count(
                data, "missing_hydrated_symbol_sessions"
            ),
            active_symbol_sessions=_require_count(data, "active_symbol_sessions"),
            observed_active_symbol_sessions=_require_count(data, "observed_active_symbol_sessions"),
            provider_empty_active_symbol_sessions=_require_count(
                data, "provider_empty_active_symbol_sessions"
            ),
            missing_active_symbol_sessions=_require_count(data, "missing_active_symbol_sessions"),
            inactive_union_symbol_sessions=_require_count(data, "inactive_union_symbol_sessions"),
            unexpected_ticker_count=_require_count(data, "unexpected_ticker_count"),
            unexpected_session_count=_require_count(data, "unexpected_session_count"),
            violation_codes=_parse_violations(data),
            receipt_digest=_require_string(data, "receipt_digest"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if self.requested_start_date > self.requested_end_date:
            raise PlatformCoverageValidationError(
                "requested_start_date must not follow requested_end_date"
            )
        if self.timeframe != "1min":
            raise PlatformCoverageValidationError("timeframe must be the native 1min receipt")
        if self.source_timeframe != "1min":
            raise PlatformCoverageValidationError(
                "source_timeframe must be the native 1min receipt"
            )
        if (
            self.market_hours is not PlatformCoverageMarketHours.REGULAR
            or self.source_market_hours is not PlatformCoverageMarketHours.REGULAR
        ):
            raise PlatformCoverageValidationError(
                "dynamic price coverage receipt is regular-hours only"
            )
        if (
            self.source_requested_start_date,
            self.source_requested_end_date,
            self.source_timeframe,
            self.source_market_hours,
        ) != (
            self.requested_start_date,
            self.requested_end_date,
            self.timeframe,
            self.market_hours,
        ):
            raise PlatformCoverageValidationError(
                "source request identity must equal expected request identity"
            )
        if self.member_union_count < 1:
            raise PlatformCoverageValidationError("member_union_count must be positive")
        if self.requested_session_count < 1:
            raise PlatformCoverageValidationError("requested_session_count must be positive")
        if self.hydrated_symbol_sessions != (
            self.observed_hydrated_symbol_sessions
            + self.provider_empty_hydrated_symbol_sessions
            + self.missing_hydrated_symbol_sessions
        ):
            raise PlatformCoverageValidationError(
                "hydrated_symbol_sessions decomposition is invalid"
            )
        if self.active_symbol_sessions != (
            self.observed_active_symbol_sessions
            + self.provider_empty_active_symbol_sessions
            + self.missing_active_symbol_sessions
        ):
            raise PlatformCoverageValidationError("active_symbol_sessions decomposition is invalid")
        if self.active_symbol_sessions > self.hydrated_symbol_sessions:
            raise PlatformCoverageValidationError(
                "active_symbol_sessions cannot exceed hydrated_symbol_sessions"
            )
        if self.observed_active_symbol_sessions > self.observed_hydrated_symbol_sessions:
            raise PlatformCoverageValidationError(
                "observed active symbol-session count cannot exceed observed hydrated "
                "symbol-session count"
            )
        if (
            self.provider_empty_active_symbol_sessions
            > self.provider_empty_hydrated_symbol_sessions
        ):
            raise PlatformCoverageValidationError(
                "provider-empty active symbol-session count cannot exceed provider-empty "
                "hydrated symbol-session count"
            )
        if self.missing_active_symbol_sessions > self.missing_hydrated_symbol_sessions:
            raise PlatformCoverageValidationError(
                "missing active symbol-session count cannot exceed missing hydrated "
                "symbol-session count"
            )
        expected_hydrated_symbol_sessions = self.member_union_count * self.requested_session_count
        if self.hydrated_symbol_sessions != expected_hydrated_symbol_sessions:
            raise PlatformCoverageValidationError(
                "hydrated_symbol_sessions must match member_union_count * requested_session_count"
            )
        if self.inactive_union_symbol_sessions != (
            self.hydrated_symbol_sessions - self.active_symbol_sessions
        ):
            raise PlatformCoverageValidationError(
                "inactive_union_symbol_sessions must equal hydrated_symbol_sessions minus "
                "active_symbol_sessions"
            )
        expected_codes = self._observed_violation_codes()
        if self.status is PlatformCoverageStatus.COMPLETE:
            if expected_codes or self.violation_codes:
                raise PlatformCoverageValidationError(
                    "COMPLETE receipt cannot contain violation counts or codes"
                )
        else:
            if self.violation_codes != expected_codes:
                raise PlatformCoverageValidationError(
                    "violation_codes do not match the reported coverage counts"
                )
            if not self.violation_codes:
                raise PlatformCoverageValidationError(
                    "REMEDIATION_REQUIRED receipt must contain observed violation codes"
                )
        for field_name in (
            "member_union_digest",
            "requested_sessions_digest",
            "pit_active_roster_digest",
            "source_price_coverage_response_digest",
        ):
            if _SHA256_RE.fullmatch(getattr(self, field_name)) is None:
                raise PlatformCoverageValidationError(f"{field_name} must be a lowercase SHA-256")
        if _SHA256_RE.fullmatch(self.receipt_digest) is None:
            raise PlatformCoverageValidationError("receipt_digest must be a lowercase SHA-256")
        if self.receipt_digest != canonical_dynamic_price_coverage_digest(self.to_dict()):
            raise PlatformCoverageValidationError("receipt_digest is not canonical")

    def _observed_violation_codes(self) -> tuple[PlatformCoverageViolationCode, ...]:
        observed: list[PlatformCoverageViolationCode] = []
        for count, code in (
            (
                self.missing_hydrated_symbol_sessions,
                PlatformCoverageViolationCode.MISSING_HYDRATED_SYMBOL_SESSION,
            ),
            (
                self.provider_empty_active_symbol_sessions,
                PlatformCoverageViolationCode.PROVIDER_EMPTY_ACTIVE_SYMBOL_SESSION,
            ),
            (
                self.missing_active_symbol_sessions,
                PlatformCoverageViolationCode.MISSING_ACTIVE_SYMBOL_SESSION,
            ),
            (self.unexpected_ticker_count, PlatformCoverageViolationCode.UNEXPECTED_TICKER),
            (
                self.unexpected_session_count,
                PlatformCoverageViolationCode.UNEXPECTED_SYMBOL_SESSION,
            ),
        ):
            if count > 0:
                observed.append(code)
        return tuple(sorted(observed, key=lambda value: value.value))

    def validate_requested_sessions(self, requested_sessions: Sequence[date]) -> None:
        """Bind count and digest to the authoritative XNYS sequence for the range."""
        sessions = tuple(requested_sessions)
        if len(sessions) != self.requested_session_count:
            raise PlatformCoverageValidationError(
                "requested_session_count must equal the real XNYS session count"
            )
        if not sessions:
            raise PlatformCoverageValidationError(
                "requested sessions must contain at least one XNYS session"
            )
        if sessions[0] < date.fromisoformat(self.requested_start_date) or sessions[
            -1
        ] > date.fromisoformat(self.requested_end_date):
            raise PlatformCoverageValidationError(
                "requested sessions must fit the requested date range"
            )
        if self.requested_sessions_digest != canonical_requested_sessions_digest(sessions):
            raise PlatformCoverageValidationError(
                "requested_sessions_digest must match the real XNYS session sequence"
            )

    @property
    def matches_shared_contract(self) -> bool:
        return (
            self.contract_version == DYNAMIC_PRICE_COVERAGE_CONTRACT_VERSION
            and self.source_contract_version == PRICE_COVERAGE_SOURCE_CONTRACT_VERSION
            and self.scope is PlatformCoverageScope.FULL_UNION_HYDRATION
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "source_contract_version": self.source_contract_version,
            "scope": self.scope.value,
            "status": self.status.value,
            "requested_start_date": self.requested_start_date,
            "requested_end_date": self.requested_end_date,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours.value,
            "source_requested_start_date": self.source_requested_start_date,
            "source_requested_end_date": self.source_requested_end_date,
            "source_timeframe": self.source_timeframe,
            "source_market_hours": self.source_market_hours.value,
            "source_provider": self.source_provider.value,
            "member_union_digest": self.member_union_digest,
            "requested_sessions_digest": self.requested_sessions_digest,
            "pit_active_roster_digest": self.pit_active_roster_digest,
            "source_price_coverage_response_digest": (self.source_price_coverage_response_digest),
            "member_union_count": self.member_union_count,
            "requested_session_count": self.requested_session_count,
            "hydrated_symbol_sessions": self.hydrated_symbol_sessions,
            "observed_hydrated_symbol_sessions": self.observed_hydrated_symbol_sessions,
            "provider_empty_hydrated_symbol_sessions": self.provider_empty_hydrated_symbol_sessions,
            "missing_hydrated_symbol_sessions": self.missing_hydrated_symbol_sessions,
            "active_symbol_sessions": self.active_symbol_sessions,
            "observed_active_symbol_sessions": self.observed_active_symbol_sessions,
            "provider_empty_active_symbol_sessions": self.provider_empty_active_symbol_sessions,
            "missing_active_symbol_sessions": self.missing_active_symbol_sessions,
            "inactive_union_symbol_sessions": self.inactive_union_symbol_sessions,
            "unexpected_ticker_count": self.unexpected_ticker_count,
            "unexpected_session_count": self.unexpected_session_count,
            "violation_codes": [code.value for code in self.violation_codes],
            "receipt_digest": self.receipt_digest,
        }
