"""Strict platform-readiness manifest contract for autoresearch."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from gateway.autoresearch_panel_receipts import (
    PANEL_RECEIPT_CONTRACT_VERSION,
    PanelReceiptValidationError,
    decode_compact_price_coverage,
    validate_research_panel_receipt,
)

DEFAULT_PLATFORM_READINESS_PATH = (
    Path.home() / ".openclaw" / "autoresearch" / "platform-readiness.json"
)
PLATFORM_READINESS_SCHEMA_VERSION = 3
QUANTIPY_DATA_CONTRACT_EVIDENCE_SCHEMA_VERSION = 3
READINESS_SHA256_RE = re.compile(r"[0-9a-f]{64}")
READINESS_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
DATASET_AVAILABILITY_REASON_MAX_CHARS = 160
DATASET_AVAILABILITY_REASON_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9 .,;:()'/_-]{{0,{DATASET_AVAILABILITY_REASON_MAX_CHARS - 1}}}"
)
READINESS_PROMPT_CAPABILITIES_MAX_BYTES = 4096
XNYS_EVIDENCE_SCHEMA_VERSION = 1
QUANTIPY_ALEMBIC_HEAD_REVISION = "020_price_bar_constraint_align"
QUANTIPY_ALEMBIC_HEAD_FILENAME = "020_align_price_bar_portable_constraints.py"
QUANTIPY_ALEMBIC_HEAD_ENV_VAR = "QUANTIPY_REQUIRED_ALEMBIC_HEAD_REVISION"
QUANTIPY_ALEMBIC_HEAD_FILENAME_ENV_VAR = "QUANTIPY_REQUIRED_ALEMBIC_HEAD_FILENAME"
QUANTIPY_CAMPAIGN_XNYS_START = date(2022, 1, 3)
QUANTIPY_CAMPAIGN_XNYS_END = date(2025, 12, 31)
QUANTIPY_READINESS_PROBE_DATE_ENV_VAR = "QUANTIPY_READINESS_PROBE_DATE"
QUANTIPY_READINESS_LOCAL_API_URL = "http://127.0.0.1:8000"
QUANTIPY_RESEARCH_PANEL_PATH = "/price-data/research-panel"
EXTERNAL_VERIFICATION_RETRY_PROBE_SYMBOL = "AAPL"
EXTERNAL_VERIFICATION_RETRY_PROBE_SESSION = date(2022, 1, 3)
EXTERNAL_VERIFICATION_RETRY_PROBE_TIMEOUT_SECONDS = 20.0
EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_RESPONSE_BYTES = 1024 * 1024
EXTERNAL_VERIFICATION_RETRY_OPERATOR_ENV_VAR = "G2_OPENCLAW_OPERATOR_RETRY"
EXTERNAL_VERIFICATION_RETRY_OPERATOR_VALUE = "1"
EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_COMPRESSION_RATIO = 200.0
RESEARCH_PANEL_RECEIPT_KEYS = frozenset(
    {
        "contract_version",
        "request",
        "request_sha256",
        "coverage",
        "coverage_sha256",
        "panel_sha256",
        "hydrated_at",
        "exported_at",
    }
)


class ReadinessError(ValueError):
    """Base class for readiness manifest failures."""


class ReadinessManifestError(ReadinessError):
    """Raised when a readiness manifest is malformed or unverifiable."""


class ReadinessBlockedError(ReadinessError):
    """Raised when a valid readiness manifest explicitly blocks research."""


class ReadinessStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class EvidenceId(StrEnum):
    QUANTIPY_DATA_CONTRACT = "quantipy_data_contract"
    XNYS_TRADING_CALENDAR = "xnys_trading_calendar"


@dataclass(frozen=True, slots=True)
class ResearchPanelProbeReceipt:
    """Immutable evidence that the repaired local panel route served one bounded request."""

    endpoint: str
    observed_at: str
    response_bytes: int
    response_sha256: str
    session_date: str
    symbol: str

    def __post_init__(self) -> None:
        if self.endpoint != f"{QUANTIPY_READINESS_LOCAL_API_URL}{QUANTIPY_RESEARCH_PANEL_PATH}":
            raise ReadinessManifestError(
                "research-panel probe endpoint is not the local Quantipy route"
            )
        if self.symbol != EXTERNAL_VERIFICATION_RETRY_PROBE_SYMBOL:
            raise ReadinessManifestError("research-panel probe must use the bounded AAPL symbol")
        if self.session_date != EXTERNAL_VERIFICATION_RETRY_PROBE_SESSION.isoformat():
            raise ReadinessManifestError("research-panel probe must use the bounded XNYS session")
        if (
            not isinstance(self.response_bytes, int)
            or isinstance(self.response_bytes, bool)
            or not 0 < self.response_bytes <= EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_RESPONSE_BYTES
        ):
            raise ReadinessManifestError("research-panel probe response size is invalid")
        _require_sha256(self.response_sha256, label="research-panel probe response_sha256")
        try:
            observed_at = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReadinessManifestError("research-panel probe observed_at is invalid") from exc
        if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
            raise ReadinessManifestError("research-panel probe observed_at must be UTC-aware")

    @classmethod
    def from_dict(cls, raw: object) -> ResearchPanelProbeReceipt:
        data = _require_mapping(raw, label="research_panel_probe")
        _require_exact_keys(
            data,
            {
                "endpoint",
                "observed_at",
                "response_bytes",
                "response_sha256",
                "session_date",
                "symbol",
            },
            label="research_panel_probe",
        )
        endpoint = data["endpoint"]
        observed_at = data["observed_at"]
        response_bytes = data["response_bytes"]
        session_date = data["session_date"]
        symbol = data["symbol"]
        if not isinstance(endpoint, str):
            raise ReadinessManifestError("research-panel probe endpoint is invalid")
        if not isinstance(observed_at, str):
            raise ReadinessManifestError("research-panel probe observed_at is invalid")
        if not isinstance(session_date, str):
            raise ReadinessManifestError("research-panel probe session_date is invalid")
        if not isinstance(symbol, str):
            raise ReadinessManifestError("research-panel probe strings are invalid")
        if not isinstance(response_bytes, int) or isinstance(response_bytes, bool):
            raise ReadinessManifestError("research-panel probe response_bytes is invalid")
        return cls(
            endpoint=endpoint,
            observed_at=observed_at,
            response_bytes=response_bytes,
            response_sha256=_require_sha256(
                data["response_sha256"], label="research_panel_probe.response_sha256"
            ),
            session_date=session_date,
            symbol=symbol,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "observed_at": self.observed_at,
            "response_bytes": self.response_bytes,
            "response_sha256": self.response_sha256,
            "session_date": self.session_date,
            "symbol": self.symbol,
        }


def probe_research_panel_for_external_verification_retry() -> ResearchPanelProbeReceipt:
    """Probe exactly one symbol and one XNYS session through the repaired local API."""
    endpoint = f"{QUANTIPY_READINESS_LOCAL_API_URL}{QUANTIPY_RESEARCH_PANEL_PATH}"
    query = urllib.parse.urlencode(
        (
            ("tickers", EXTERNAL_VERIFICATION_RETRY_PROBE_SYMBOL),
            ("start", EXTERNAL_VERIFICATION_RETRY_PROBE_SESSION.isoformat()),
            ("end", EXTERNAL_VERIFICATION_RETRY_PROBE_SESSION.isoformat()),
            ("timeframe", "1d"),
            ("market_hours", "regular"),
        )
    )
    request = urllib.request.Request(f"{endpoint}?{query}", method="GET")
    try:
        with urllib.request.urlopen(
            request, timeout=EXTERNAL_VERIFICATION_RETRY_PROBE_TIMEOUT_SECONDS
        ) as response:
            if response.getcode() != 200:
                raise ReadinessManifestError("research-panel probe did not return HTTP 200")
            content_type = response.headers.get_content_type()
            body = response.read(EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ReadinessManifestError("research-panel live probe failed closed") from exc
    if not body or len(body) > EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_RESPONSE_BYTES:
        raise ReadinessManifestError("research-panel probe response is empty or exceeds its bound")
    if content_type != "application/zip" or not body.startswith(b"PK"):
        raise ReadinessManifestError("research-panel probe response is not a ZIP archive")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            if tuple(info.filename for info in infos) != ("panel.parquet", "receipt.json"):
                raise ReadinessManifestError(
                    "research-panel probe ZIP does not match the two-member contract"
                )
            for info in infos:
                _validate_probe_zip_member(info)
            panel_bytes = _read_bounded_probe_zip_member(archive, infos[0])
            receipt = json.loads(_read_bounded_probe_zip_member(archive, infos[1]))
    except ReadinessManifestError:
        raise
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ReadinessManifestError("research-panel probe ZIP is invalid") from exc
    if (
        not isinstance(receipt, dict)
        or frozenset(receipt) != RESEARCH_PANEL_RECEIPT_KEYS
        or receipt.get("contract_version") != PANEL_RECEIPT_CONTRACT_VERSION
    ):
        raise ReadinessManifestError(
            "research-panel probe receipt is not the strict panel contract"
        )
    _validate_external_retry_probe_receipt(receipt, panel_bytes)
    return ResearchPanelProbeReceipt(
        endpoint=endpoint,
        observed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        response_bytes=len(body),
        response_sha256=hashlib.sha256(body).hexdigest(),
        session_date=EXTERNAL_VERIFICATION_RETRY_PROBE_SESSION.isoformat(),
        symbol=EXTERNAL_VERIFICATION_RETRY_PROBE_SYMBOL,
    )


def _validate_probe_zip_member(info: zipfile.ZipInfo) -> None:
    if info.is_dir() or info.flag_bits & 0x1:
        raise ReadinessManifestError("research-panel probe ZIP members must be unencrypted files")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ReadinessManifestError("research-panel probe ZIP uses unsupported compression")
    if info.file_size > EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_RESPONSE_BYTES:
        raise ReadinessManifestError("research-panel probe ZIP member exceeds expanded size bound")
    compression_ratio = info.file_size / max(info.compress_size, 1)
    if compression_ratio > EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_COMPRESSION_RATIO:
        raise ReadinessManifestError("research-panel probe ZIP member exceeds compression ratio")


def _read_bounded_probe_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    chunks: list[bytes] = []
    bytes_read = 0
    with archive.open(info, mode="r") as source:
        while chunk := source.read(64 * 1024):
            bytes_read += len(chunk)
            if bytes_read > EXTERNAL_VERIFICATION_RETRY_PROBE_MAX_RESPONSE_BYTES:
                raise ReadinessManifestError("research-panel probe ZIP member exceeds read bound")
            chunks.append(chunk)
    if bytes_read != info.file_size:
        raise ReadinessManifestError("research-panel probe ZIP member size is inconsistent")
    return b"".join(chunks)


def _validate_external_retry_probe_receipt(receipt: object, panel_bytes: bytes) -> None:
    try:
        normalized = validate_research_panel_receipt(
            receipt,
            label="research-panel probe receipt",
            panel_bytes=panel_bytes,
        )
        coverage = decode_compact_price_coverage(
            normalized["coverage"], label="research-panel probe coverage"
        )
    except PanelReceiptValidationError as exc:
        raise ReadinessManifestError(str(exc)) from exc
    request = normalized["request"]
    compact_coverage = normalized["coverage"]
    assert isinstance(request, dict)
    assert isinstance(compact_coverage, dict)
    if request != {
        "contract_version": "research-price-panel-v1",
        "tickers": [EXTERNAL_VERIFICATION_RETRY_PROBE_SYMBOL],
        "start": "2022-01-03T05:00:00Z",
        "end": "2022-01-04T04:59:59.999999Z",
        "timeframe": "1d",
        "market_hours": "regular",
    }:
        raise ReadinessManifestError("research-panel probe request is not the bounded AAPL request")
    coverage_tickers = coverage.get("tickers")
    if not isinstance(coverage_tickers, list) or len(coverage_tickers) != 1:
        raise ReadinessManifestError("research-panel probe coverage tickers are invalid")
    if not isinstance(coverage_tickers[0], dict):
        raise ReadinessManifestError("research-panel probe coverage ticker is invalid")
    sessions = coverage_tickers[0].get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 1:
        raise ReadinessManifestError(
            "research-panel probe coverage must contain exactly one AAPL session"
        )
    if (
        not isinstance(sessions[0], dict)
        or sessions[0].get("session_date") != EXTERNAL_VERIFICATION_RETRY_PROBE_SESSION.isoformat()
    ):
        raise ReadinessManifestError(
            "research-panel probe coverage session is not the bounded XNYS session"
        )
    if (
        coverage.get("contract_version") != "price-coverage-v1"
        or coverage.get("requested_start_date") != "2022-01-03"
        or coverage.get("requested_end_date") != "2022-01-03"
        or coverage.get("timeframe") != "1min"
        or coverage.get("market_hours") != "regular"
        or coverage.get("provider_source") != "massive"
        or coverage_tickers[0].get("ticker") != EXTERNAL_VERIFICATION_RETRY_PROBE_SYMBOL
    ):
        raise ReadinessManifestError(
            "research-panel probe coverage is not the bounded AAPL receipt"
        )


@dataclass(frozen=True, slots=True)
class XNYSCalendarEvidence:
    """Parsed authoritative XNYS sessions from the pinned operator receipt."""

    range_start: date
    range_end: date
    closed_dates: frozenset[date]
    scheduled_half_days: frozenset[date]

    @classmethod
    def from_dict(cls, raw: object) -> XNYSCalendarEvidence:
        data = _require_mapping(raw, label="XNYS calendar evidence")
        _require_exact_keys(
            data,
            {
                "admission_status",
                "authority",
                "closed_dates",
                "declared_range",
                "evidence_type",
                "limitations",
                "retrieved_at",
                "scheduled_half_days",
                "schema_version",
                "session_definition",
                "source_files",
            },
            label="XNYS calendar evidence",
        )
        if data["schema_version"] != XNYS_EVIDENCE_SCHEMA_VERSION:
            raise ReadinessManifestError("unsupported XNYS calendar evidence schema_version")
        if data["evidence_type"] != EvidenceId.XNYS_TRADING_CALENDAR.value:
            raise ReadinessManifestError("XNYS evidence_type must be xnys_trading_calendar")
        if data["admission_status"] != ReadinessStatus.READY.value:
            raise ReadinessManifestError("XNYS calendar evidence admission_status must be READY")

        declared_range = _require_mapping(
            data["declared_range"], label="XNYS calendar evidence.declared_range"
        )
        _require_exact_keys(
            declared_range,
            {"start", "end", "timezone"},
            label="XNYS calendar evidence.declared_range",
        )
        if declared_range["timezone"] != "America/New_York":
            raise ReadinessManifestError("XNYS calendar timezone must be America/New_York")
        range_start = _parse_evidence_date(declared_range["start"], label="declared_range.start")
        range_end = _parse_evidence_date(declared_range["end"], label="declared_range.end")
        if range_start > range_end:
            raise ReadinessManifestError("XNYS declared range start must not follow end")

        session_definition = _require_mapping(
            data["session_definition"], label="XNYS calendar evidence.session_definition"
        )
        _require_exact_keys(
            session_definition,
            {"regular_close", "regular_open", "scheduled_early_close", "unit"},
            label="XNYS calendar evidence.session_definition",
        )
        if session_definition != {
            "regular_close": "16:00",
            "regular_open": "09:30",
            "scheduled_early_close": "13:00",
            "unit": "ET",
        }:
            raise ReadinessManifestError("XNYS session_definition is not the supported contract")

        closed_dates = _parse_evidence_date_list(data["closed_dates"], label="closed_dates")
        half_days = _parse_evidence_date_list(
            data["scheduled_half_days"], label="scheduled_half_days"
        )
        for label, values in (("closed_dates", closed_dates), ("scheduled_half_days", half_days)):
            if any(value < range_start or value > range_end for value in values):
                raise ReadinessManifestError(f"XNYS {label} must fit declared_range")
            if any(value.weekday() >= 5 for value in values):
                raise ReadinessManifestError(f"XNYS {label} cannot contain weekends")
        if closed_dates & half_days:
            raise ReadinessManifestError(
                "XNYS closed_dates and scheduled_half_days must be disjoint"
            )
        return cls(range_start, range_end, closed_dates, half_days)

    @property
    def sessions(self) -> tuple[date, ...]:
        current = self.range_start
        sessions: list[date] = []
        while current <= self.range_end:
            if current.weekday() < 5 and current not in self.closed_dates:
                sessions.append(current)
            current += timedelta(days=1)
        return tuple(sessions)


def _validate_campaign_xnys_interval(
    *,
    xnys: XNYSCalendarEvidence,
    campaign_start: date,
    campaign_end: date,
) -> None:
    if campaign_start != QUANTIPY_CAMPAIGN_XNYS_START or campaign_end != QUANTIPY_CAMPAIGN_XNYS_END:
        raise ReadinessManifestError(
            "Quantipy campaign XNYS interval must be pinned to "
            f"{QUANTIPY_CAMPAIGN_XNYS_START.isoformat()}.."
            f"{QUANTIPY_CAMPAIGN_XNYS_END.isoformat()}"
        )
    if campaign_start < xnys.range_start or campaign_end > xnys.range_end:
        raise ReadinessManifestError(
            "XNYS evidence declared_range does not cover required campaign interval "
            f"{campaign_start.isoformat()}..{campaign_end.isoformat()}"
        )
    sessions = xnys.sessions
    if campaign_start not in sessions:
        raise ReadinessManifestError(
            f"campaign XNYS start {campaign_start.isoformat()} is not an actual session "
            "in the pinned evidence"
        )
    if campaign_end not in sessions:
        raise ReadinessManifestError(
            f"campaign XNYS end {campaign_end.isoformat()} is not an actual session "
            "in the pinned evidence"
        )


def _parse_evidence_date(raw: object, *, label: str) -> date:
    if not isinstance(raw, str):
        raise ReadinessManifestError(f"XNYS {label} must be an ISO date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ReadinessManifestError(f"XNYS {label} must be an ISO date") from exc


def _parse_evidence_date_list(raw: object, *, label: str) -> frozenset[date]:
    if not isinstance(raw, list) or not raw:
        raise ReadinessManifestError(f"XNYS {label} must be a non-empty date list")
    parsed = tuple(_parse_evidence_date(item, label=label) for item in raw)
    if tuple(sorted(set(parsed))) != parsed:
        raise ReadinessManifestError(f"XNYS {label} must be sorted and unique")
    return frozenset(parsed)


def _require_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ReadinessManifestError(f"{label} must be an object")
    return raw


def _require_exact_keys(data: Mapping[str, object], expected: set[str], *, label: str) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ReadinessManifestError(f"{label} has invalid fields: {'; '.join(details)}")


def _require_identifier(data: Mapping[str, object], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or READINESS_IDENTIFIER_RE.fullmatch(value) is None:
        raise ReadinessManifestError(f"{field_name} must match {READINESS_IDENTIFIER_RE.pattern!r}")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or READINESS_SHA256_RE.fullmatch(value) is None:
        raise ReadinessManifestError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _require_sha256_commit(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ReadinessManifestError(f"{label} must be a full lowercase Git commit SHA")
    return value


def _require_literal_bool(value: object, expected: bool, *, label: str) -> bool:
    if not isinstance(value, bool) or value is not expected:
        raise ReadinessManifestError(f"{label} must be {str(expected).lower()}")
    return value


@dataclass(frozen=True, slots=True)
class DatasetAvailability:
    """Observed live dataset extent, or an explicit unavailable result."""

    available: bool
    start_date: str | None
    end_date: str | None
    record_count: int | None
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise ReadinessManifestError("dataset availability.available must be a boolean")
        if self.available:
            if not isinstance(self.start_date, str) or not isinstance(self.end_date, str):
                raise ReadinessManifestError("available dataset requires observed date bounds")
            try:
                start = date.fromisoformat(self.start_date)
                end = date.fromisoformat(self.end_date)
            except ValueError as exc:
                raise ReadinessManifestError("dataset date bounds must be ISO dates") from exc
            if start > end:
                raise ReadinessManifestError("dataset start_date must not follow end_date")
            if (
                isinstance(self.record_count, bool)
                or not isinstance(self.record_count, int)
                or self.record_count < 1
            ):
                raise ReadinessManifestError("available dataset requires a positive record_count")
            if self.reason is not None:
                raise ReadinessManifestError("available dataset must set reason=null")
            return
        if any(value is not None for value in (self.start_date, self.end_date, self.record_count)):
            raise ReadinessManifestError("unavailable dataset must not claim bounds or counts")
        if not isinstance(self.reason, str):
            raise ReadinessManifestError("unavailable dataset requires a reason")
        reason = self.reason.strip()
        if DATASET_AVAILABILITY_REASON_RE.fullmatch(reason) is None:
            raise ReadinessManifestError(
                "unavailable dataset reason must be 1-160 characters of single-line ASCII text"
            )
        object.__setattr__(self, "reason", reason)

    @classmethod
    def from_dict(cls, raw: object, *, label: str) -> DatasetAvailability:
        data = _require_mapping(raw, label=label)
        _require_exact_keys(
            data,
            {"available", "start_date", "end_date", "record_count", "reason"},
            label=label,
        )
        available = data["available"]
        start_date = data["start_date"]
        end_date = data["end_date"]
        record_count = data["record_count"]
        reason = data["reason"]
        if not isinstance(available, bool):
            raise ReadinessManifestError(f"{label}.available must be a boolean")
        if start_date is not None and not isinstance(start_date, str):
            raise ReadinessManifestError(f"{label}.start_date must be an ISO date or null")
        if end_date is not None and not isinstance(end_date, str):
            raise ReadinessManifestError(f"{label}.end_date must be an ISO date or null")
        if record_count is not None and (
            isinstance(record_count, bool) or not isinstance(record_count, int)
        ):
            raise ReadinessManifestError(f"{label}.record_count must be an integer or null")
        if reason is not None and not isinstance(reason, str):
            raise ReadinessManifestError(f"{label}.reason must be a string or null")
        return cls(available, start_date, end_date, record_count, reason)

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "end_date": self.end_date,
            "reason": self.reason,
            "record_count": self.record_count,
            "start_date": self.start_date,
        }


@dataclass(frozen=True, slots=True)
class SecurityMasterCapabilities:
    historical_snapshots_interface: bool
    historical_security_type_common_stock_filter_pit_certified: bool
    inactive_listings_interface: bool
    unadjusted_liquidity_screens_interface: bool
    universe_history_api_and_client_interface: bool
    next_session_execution_policy_interface: bool
    split_actions_interface: bool
    dividend_actions_interface: bool
    ticker_detail_market_cap_interface: bool
    ticker_detail_market_cap_pit_certified: bool

    def __post_init__(self) -> None:
        for field_name in (
            "historical_snapshots_interface",
            "historical_security_type_common_stock_filter_pit_certified",
            "inactive_listings_interface",
            "unadjusted_liquidity_screens_interface",
            "universe_history_api_and_client_interface",
            "next_session_execution_policy_interface",
            "split_actions_interface",
            "dividend_actions_interface",
            "ticker_detail_market_cap_interface",
        ):
            _require_literal_bool(getattr(self, field_name), True, label=field_name)
        _require_literal_bool(
            self.ticker_detail_market_cap_pit_certified,
            False,
            label="ticker_detail_market_cap_pit_certified",
        )

    @classmethod
    def from_dict(cls, raw: object) -> SecurityMasterCapabilities:
        label = "capabilities.security_master"
        data = _require_mapping(raw, label=label)
        expected = {
            "historical_snapshots_interface",
            "historical_security_type_common_stock_filter_pit_certified",
            "inactive_listings_interface",
            "unadjusted_liquidity_screens_interface",
            "universe_history_api_and_client_interface",
            "next_session_execution_policy_interface",
            "split_actions_interface",
            "dividend_actions_interface",
            "ticker_detail_market_cap_interface",
            "ticker_detail_market_cap_pit_certified",
        }
        _require_exact_keys(data, expected, label=label)
        values = {
            field_name: _require_literal_bool(
                data[field_name],
                field_name != "ticker_detail_market_cap_pit_certified",
                label=f"{label}.{field_name}",
            )
            for field_name in expected
        }
        return cls(**values)

    def to_dict(self) -> dict[str, bool]:
        return {
            "dividend_actions_interface": self.dividend_actions_interface,
            "historical_snapshots_interface": self.historical_snapshots_interface,
            "historical_security_type_common_stock_filter_pit_certified": (
                self.historical_security_type_common_stock_filter_pit_certified
            ),
            "inactive_listings_interface": self.inactive_listings_interface,
            "next_session_execution_policy_interface": self.next_session_execution_policy_interface,
            "split_actions_interface": self.split_actions_interface,
            "ticker_detail_market_cap_interface": self.ticker_detail_market_cap_interface,
            "ticker_detail_market_cap_pit_certified": (self.ticker_detail_market_cap_pit_certified),
            "unadjusted_liquidity_screens_interface": (self.unadjusted_liquidity_screens_interface),
            "universe_history_api_and_client_interface": (
                self.universe_history_api_and_client_interface
            ),
        }


@dataclass(frozen=True, slots=True)
class MarketDataCapabilities:
    ohlcv_cache_or_hydrate_interface: bool
    historical_trades_interface: bool
    historical_quotes_interface: bool
    historical_fundamentals_interface: bool

    def __post_init__(self) -> None:
        _require_literal_bool(
            self.ohlcv_cache_or_hydrate_interface,
            True,
            label="ohlcv_cache_or_hydrate_interface",
        )
        for field_name in (
            "historical_trades_interface",
            "historical_quotes_interface",
            "historical_fundamentals_interface",
        ):
            _require_literal_bool(getattr(self, field_name), False, label=field_name)

    @classmethod
    def from_dict(cls, raw: object) -> MarketDataCapabilities:
        label = "capabilities.market_data"
        data = _require_mapping(raw, label=label)
        expected = {
            "ohlcv_cache_or_hydrate_interface",
            "historical_trades_interface",
            "historical_quotes_interface",
            "historical_fundamentals_interface",
        }
        _require_exact_keys(data, expected, label=label)
        return cls(
            ohlcv_cache_or_hydrate_interface=_require_literal_bool(
                data["ohlcv_cache_or_hydrate_interface"],
                True,
                label=f"{label}.ohlcv_cache_or_hydrate_interface",
            ),
            historical_trades_interface=_require_literal_bool(
                data["historical_trades_interface"],
                False,
                label=f"{label}.historical_trades_interface",
            ),
            historical_quotes_interface=_require_literal_bool(
                data["historical_quotes_interface"],
                False,
                label=f"{label}.historical_quotes_interface",
            ),
            historical_fundamentals_interface=_require_literal_bool(
                data["historical_fundamentals_interface"],
                False,
                label=f"{label}.historical_fundamentals_interface",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "historical_fundamentals_interface": self.historical_fundamentals_interface,
            "historical_quotes_interface": self.historical_quotes_interface,
            "historical_trades_interface": self.historical_trades_interface,
            "ohlcv_cache_or_hydrate_interface": self.ohlcv_cache_or_hydrate_interface,
        }


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    security_master: SecurityMasterCapabilities
    market_data: MarketDataCapabilities
    reddit_dataset: DatasetAvailability
    news_dataset: DatasetAvailability

    def __post_init__(self) -> None:
        if not isinstance(self.security_master, SecurityMasterCapabilities):
            raise ReadinessManifestError("security_master must be security-master capabilities")
        if not isinstance(self.market_data, MarketDataCapabilities):
            raise ReadinessManifestError("market_data must be market-data capabilities")
        if not isinstance(self.reddit_dataset, DatasetAvailability) or not isinstance(
            self.news_dataset, DatasetAvailability
        ):
            raise ReadinessManifestError("dataset capabilities must be observed availability")

    @classmethod
    def from_dict(cls, raw: object) -> PlatformCapabilities:
        data = _require_mapping(raw, label="capabilities")
        _require_exact_keys(
            data,
            {"security_master", "market_data", "reddit_dataset", "news_dataset"},
            label="capabilities",
        )
        return cls(
            security_master=SecurityMasterCapabilities.from_dict(data["security_master"]),
            market_data=MarketDataCapabilities.from_dict(data["market_data"]),
            reddit_dataset=DatasetAvailability.from_dict(
                data["reddit_dataset"], label="capabilities.reddit_dataset"
            ),
            news_dataset=DatasetAvailability.from_dict(
                data["news_dataset"], label="capabilities.news_dataset"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "market_data": self.market_data.to_dict(),
            "news_dataset": self.news_dataset.to_dict(),
            "reddit_dataset": self.reddit_dataset.to_dict(),
            "security_master": self.security_master.to_dict(),
        }


def _file_sha256(path: Path) -> str:
    """Hash one regular file through an open descriptor without following links."""
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ReadinessManifestError(f"readiness evidence path is not a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except ReadinessManifestError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ReadinessManifestError(
                f"readiness evidence path is not a regular file: {path}"
            ) from exc
        raise ReadinessManifestError(f"cannot read readiness evidence file {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _stat_fingerprint(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class _ImmutableEvidenceDescriptor:
    path: Path
    descriptor: int
    fingerprint: tuple[int, int, int, int, int, int]
    sha256: str

    @classmethod
    def open(cls, path: Path) -> _ImmutableEvidenceDescriptor:
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ReadinessManifestError(f"XNYS evidence path is not a regular file: {path}")
            sha256 = _descriptor_sha256(descriptor)
            after = os.fstat(descriptor)
            fingerprint = _stat_fingerprint(before)
            if _stat_fingerprint(after) != fingerprint:
                raise ReadinessManifestError("XNYS evidence changed during readiness build")
            opened = cls(path, descriptor, fingerprint, sha256)
            opened.revalidate()
            return opened
        except ReadinessManifestError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ReadinessManifestError(f"cannot read XNYS evidence file {path}: {exc}") from exc

    def revalidate(self) -> None:
        try:
            descriptor_stat = os.fstat(self.descriptor)
            path_stat = os.stat(self.path, follow_symlinks=False)
            unchanged = (
                stat.S_ISREG(path_stat.st_mode)
                and _stat_fingerprint(descriptor_stat) == self.fingerprint
                and _stat_fingerprint(path_stat) == self.fingerprint
                and _descriptor_sha256(self.descriptor) == self.sha256
                and _stat_fingerprint(os.fstat(self.descriptor)) == self.fingerprint
            )
        except OSError as exc:
            raise ReadinessManifestError("XNYS evidence changed during readiness build") from exc
        if not unchanged:
            raise ReadinessManifestError("XNYS evidence changed during readiness build")

    def close(self) -> None:
        os.close(self.descriptor)

    def read_bytes(self) -> bytes:
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(self.descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        self.revalidate()
        return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    """One operator-provided evidence file or an explicit unavailable entry."""

    path: str | None
    sha256: str | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.path is not None and (
            not isinstance(self.path, str) or not Path(self.path).is_absolute()
        ):
            raise ReadinessManifestError("evidence path must be absolute or null")
        if self.sha256 is not None:
            _require_sha256(self.sha256, label="evidence.sha256")
        if self.path is None and self.sha256 is not None:
            raise ReadinessManifestError("evidence cannot provide sha256 without path")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ReadinessManifestError("evidence.reason must be a non-empty string or null")
        if self.path is None and self.reason is None:
            raise ReadinessManifestError("evidence must explain unavailable evidence")
        if isinstance(self.reason, str):
            object.__setattr__(self, "reason", self.reason.strip())

    @classmethod
    def from_dict(cls, raw: object, *, label: str) -> ReadinessEvidence:
        data = _require_mapping(raw, label=label)
        _require_exact_keys(data, {"path", "sha256", "reason"}, label=label)
        path = data["path"]
        sha256 = data["sha256"]
        reason = data["reason"]
        if path is not None and not isinstance(path, str):
            raise ReadinessManifestError(f"{label}.path must be an absolute path or null")
        if sha256 is not None:
            sha256 = _require_sha256(sha256, label=f"{label}.sha256")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ReadinessManifestError(f"{label}.reason must be a non-empty string or null")
        if path is None and sha256 is not None:
            raise ReadinessManifestError(f"{label} cannot provide sha256 without path")
        if path is not None and not Path(path).is_absolute():
            raise ReadinessManifestError(f"{label}.path must be absolute")
        if path is None and reason is None:
            raise ReadinessManifestError(f"{label} must explain unavailable evidence")
        return cls(
            path=path,
            sha256=sha256,
            reason=reason.strip() if isinstance(reason, str) else None,
        )

    def validate_for_status(
        self,
        status: ReadinessStatus,
        *,
        label: str,
        sha256_overrides: Mapping[Path, str] | None = None,
    ) -> None:
        if self.path is None:
            if status is ReadinessStatus.READY:
                raise ReadinessManifestError(f"READY manifest requires {label}.path")
            return
        path = Path(self.path)
        has_override = sha256_overrides is not None and path in sha256_overrides
        if not has_override and (path.is_symlink() or not path.is_file()):
            if status is ReadinessStatus.READY:
                raise ReadinessManifestError(
                    f"READY manifest evidence path is not a regular file: {path}"
                )
            return
        if self.sha256 is None:
            if status is ReadinessStatus.READY:
                raise ReadinessManifestError(f"READY manifest requires {label}.sha256")
            return
        actual = (
            sha256_overrides[path]
            if has_override and sha256_overrides is not None
            else _file_sha256(path)
        )
        if actual != self.sha256:
            raise ReadinessManifestError(
                f"{label} SHA-256 mismatch for {path}: expected {self.sha256}, got {actual}"
            )

    def to_dict(self) -> dict[str, str | None]:
        return {"path": self.path, "reason": self.reason, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReadinessIdentity:
    """Stable identity pinned into autoresearch state."""

    manifest_id: str
    snapshot_id: str
    receipt_sha256: str
    quantipy_commit: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> ReadinessIdentity:
        data = _require_mapping(raw, label="platform_readiness")
        expected = {"manifest_id", "snapshot_id", "receipt_sha256"}
        if "quantipy_commit" in data:
            expected.add("quantipy_commit")
        _require_exact_keys(data, expected, label="platform_readiness")
        quantipy_commit = data.get("quantipy_commit")
        if quantipy_commit is not None:
            quantipy_commit = _require_sha256_commit(quantipy_commit, label="quantipy_commit")
        return cls(
            manifest_id=_require_identifier(data, "manifest_id"),
            snapshot_id=_require_identifier(data, "snapshot_id"),
            receipt_sha256=_require_sha256(data["receipt_sha256"], label="receipt_sha256"),
            quantipy_commit=quantipy_commit,
        )

    def to_dict(self) -> dict[str, str | None]:
        identity: dict[str, str | None] = {
            "manifest_id": self.manifest_id,
            "receipt_sha256": self.receipt_sha256,
            "snapshot_id": self.snapshot_id,
        }
        if self.quantipy_commit is not None:
            identity["quantipy_commit"] = self.quantipy_commit
        return identity


@dataclass(frozen=True, slots=True)
class PlatformReadinessManifest:
    schema_version: int
    status: ReadinessStatus
    manifest_id: str
    snapshot_id: str
    evidence: Mapping[EvidenceId, ReadinessEvidence]
    reason: str | None
    capabilities: PlatformCapabilities | None = None
    _evidence_sha256_overrides: InitVar[Mapping[Path, str] | None] = None

    def __post_init__(self, _evidence_sha256_overrides: Mapping[Path, str] | None) -> None:
        """Validate direct construction and freeze the evidence mapping."""
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != PLATFORM_READINESS_SCHEMA_VERSION
        ):
            raise ReadinessManifestError(
                f"unsupported platform readiness schema_version: {self.schema_version!r}"
            )
        if not isinstance(self.status, ReadinessStatus):
            raise ReadinessManifestError("status must be READY or BLOCKED")
        for field_name, value in (
            ("manifest_id", self.manifest_id),
            ("snapshot_id", self.snapshot_id),
        ):
            if not isinstance(value, str) or READINESS_IDENTIFIER_RE.fullmatch(value) is None:
                raise ReadinessManifestError(
                    f"{field_name} must match {READINESS_IDENTIFIER_RE.pattern!r}"
                )
        if not isinstance(self.evidence, Mapping):
            raise ReadinessManifestError("evidence must be an object")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ReadinessManifestError("reason must be a non-empty string or null")
        if self.capabilities is not None and not isinstance(
            self.capabilities, PlatformCapabilities
        ):
            raise ReadinessManifestError(
                "capabilities must be a platform capability object or null"
            )
        expected_ids = set(EvidenceId)
        actual_ids = set(self.evidence)
        if actual_ids != expected_ids:
            raise ReadinessManifestError("evidence must contain exactly the required evidence IDs")
        normalized_evidence: dict[EvidenceId, ReadinessEvidence] = {}
        for evidence_id in EvidenceId:
            evidence = self.evidence[evidence_id]
            if not isinstance(evidence, ReadinessEvidence):
                raise ReadinessManifestError(
                    f"evidence.{evidence_id.value} must be ReadinessEvidence"
                )
            normalized_evidence[evidence_id] = evidence
        object.__setattr__(self, "evidence", MappingProxyType(normalized_evidence))
        self.validate(_evidence_sha256_overrides=_evidence_sha256_overrides)

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        _evidence_sha256_overrides: Mapping[Path, str] | None = None,
    ) -> PlatformReadinessManifest:
        data = _require_mapping(raw, label="platform_readiness_manifest")
        _require_exact_keys(
            data,
            {
                "schema_version",
                "status",
                "manifest_id",
                "snapshot_id",
                "evidence",
                "capabilities",
                "reason",
            },
            label="platform_readiness_manifest",
        )
        schema_version = data["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ReadinessManifestError("schema_version must be an integer")
        if schema_version != PLATFORM_READINESS_SCHEMA_VERSION:
            raise ReadinessManifestError(
                f"unsupported platform readiness schema_version: {schema_version!r}"
            )
        status_raw = data["status"]
        if not isinstance(status_raw, str):
            raise ReadinessManifestError("status must be READY or BLOCKED")
        try:
            status = ReadinessStatus(status_raw)
        except ValueError as exc:
            raise ReadinessManifestError("status must be READY or BLOCKED") from exc
        evidence_data = _require_mapping(data["evidence"], label="evidence")
        _require_exact_keys(
            evidence_data,
            {item.value for item in EvidenceId},
            label="evidence",
        )
        evidence = {
            evidence_id: ReadinessEvidence.from_dict(
                evidence_data[evidence_id.value], label=f"evidence.{evidence_id.value}"
            )
            for evidence_id in EvidenceId
        }
        reason = data["reason"]
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ReadinessManifestError("reason must be a non-empty string or null")
        manifest = cls(
            schema_version=schema_version,
            status=status,
            manifest_id=_require_identifier(data, "manifest_id"),
            snapshot_id=_require_identifier(data, "snapshot_id"),
            evidence=evidence,
            reason=reason.strip() if isinstance(reason, str) else None,
            capabilities=(
                PlatformCapabilities.from_dict(data["capabilities"])
                if data["capabilities"] is not None
                else None
            ),
            _evidence_sha256_overrides=_evidence_sha256_overrides,
        )
        return manifest

    def validate(self, *, _evidence_sha256_overrides: Mapping[Path, str] | None = None) -> None:
        if self.schema_version != PLATFORM_READINESS_SCHEMA_VERSION:
            raise ReadinessManifestError(
                f"unsupported platform readiness schema_version: {self.schema_version!r}"
            )
        if not isinstance(self.status, ReadinessStatus):
            raise ReadinessManifestError("status must be READY or BLOCKED")
        if self.status is ReadinessStatus.READY and self.reason is not None:
            raise ReadinessManifestError("READY manifest must set reason=null")
        if self.status is ReadinessStatus.READY and self.capabilities is None:
            raise ReadinessManifestError("READY manifest requires complete capabilities")
        if self.status is ReadinessStatus.BLOCKED and not self.reason:
            raise ReadinessManifestError(
                "BLOCKED manifest requires a concrete operator-facing reason"
            )
        if self.status is ReadinessStatus.BLOCKED and self.capabilities is not None:
            raise ReadinessManifestError("BLOCKED manifest must set capabilities=null")
        for evidence_id, evidence in self.evidence.items():
            evidence.validate_for_status(
                self.status,
                label=f"evidence.{evidence_id.value}",
                sha256_overrides=_evidence_sha256_overrides,
            )

    def identity(self) -> ReadinessIdentity:
        self.validate()
        if self.status is not ReadinessStatus.READY:
            raise ReadinessBlockedError(self.reason or "platform readiness is BLOCKED")
        _, quantipy_commit = load_quantipy_data_contract_evidence(self)
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return ReadinessIdentity(
            manifest_id=self.manifest_id,
            snapshot_id=self.snapshot_id,
            receipt_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            quantipy_commit=quantipy_commit,
        )

    def require_ready(self) -> ReadinessIdentity:
        return self.identity()

    def to_dict(self) -> dict[str, object]:
        return {
            "capabilities": self.capabilities.to_dict() if self.capabilities is not None else None,
            "evidence": {
                evidence_id.value: self.evidence[evidence_id].to_dict()
                for evidence_id in EvidenceId
            },
            "manifest_id": self.manifest_id,
            "reason": self.reason,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "status": self.status.value,
        }

    def prompt_capabilities(self) -> str:
        """Render the only readiness block stages may receive."""
        self.require_ready()
        payload = {
            "capabilities": self.capabilities.to_dict() if self.capabilities is not None else None,
            "evidence": {
                evidence_id.value: self.evidence[evidence_id].sha256 for evidence_id in EvidenceId
            },
            "contract_identity": {
                "manifest_id": self.manifest_id,
                "snapshot_id": self.snapshot_id,
            },
            "schema_version": self.schema_version,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(serialized.encode("utf-8")) > READINESS_PROMPT_CAPABILITIES_MAX_BYTES:
            raise ReadinessManifestError("readiness prompt capabilities exceed 4096 bytes")
        return serialized


def load_platform_readiness(
    path: Path = DEFAULT_PLATFORM_READINESS_PATH,
) -> PlatformReadinessManifest:
    """Load and fully validate the operator-owned readiness manifest."""
    path = path.expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReadinessManifestError(
            f"missing platform readiness manifest: {path}; initialize it explicitly before research"
        ) from exc
    except OSError as exc:
        raise ReadinessManifestError(
            f"failed to read platform readiness manifest {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReadinessManifestError(f"invalid platform readiness JSON: {path}") from exc
    return PlatformReadinessManifest.from_dict(raw)


def validate_state_readiness(
    state_identity: ReadinessIdentity | None,
    manifest: PlatformReadinessManifest,
) -> ReadinessIdentity:
    """Require a READY manifest whose identity matches the persisted state."""
    current = manifest.require_ready()
    if state_identity is None:
        raise ReadinessManifestError(
            "autoresearch state has no pinned platform readiness receipt; "
            "run autoresearch-pin-readiness explicitly before dispatch"
        )
    if state_identity != current:
        raise ReadinessManifestError(
            "autoresearch state platform readiness receipt is stale; "
            "run autoresearch-resume explicitly after reviewing the new manifest"
        )
    return current


def load_xnys_calendar_evidence(
    manifest: PlatformReadinessManifest,
) -> tuple[str, XNYSCalendarEvidence]:
    """Re-read, hash, and parse the XNYS file pinned by a READY manifest."""
    manifest.require_ready()
    receipt = manifest.evidence[EvidenceId.XNYS_TRADING_CALENDAR]
    if receipt.path is None or receipt.sha256 is None:
        raise ReadinessManifestError("READY manifest requires pinned XNYS evidence")
    descriptor = _ImmutableEvidenceDescriptor.open(Path(receipt.path))
    try:
        if descriptor.sha256 != receipt.sha256:
            raise ReadinessManifestError(
                "XNYS evidence SHA-256 does not match the readiness manifest"
            )
        content = descriptor.read_bytes()
    finally:
        descriptor.close()
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessManifestError("XNYS calendar evidence must be valid UTF-8 JSON") from exc
    return receipt.sha256, XNYSCalendarEvidence.from_dict(raw)


def load_quantipy_data_contract_evidence(
    manifest: PlatformReadinessManifest,
) -> tuple[str, str]:
    """Return the exact committed Quantipy SHA pinned by READY evidence."""
    manifest.validate()
    if manifest.status is not ReadinessStatus.READY:
        raise ReadinessBlockedError(manifest.reason or "platform readiness is BLOCKED")
    receipt = manifest.evidence[EvidenceId.QUANTIPY_DATA_CONTRACT]
    if receipt.path is None or receipt.sha256 is None:
        raise ReadinessManifestError("READY manifest requires pinned Quantipy contract evidence")
    descriptor = _ImmutableEvidenceDescriptor.open(Path(receipt.path))
    try:
        if descriptor.sha256 != receipt.sha256:
            raise ReadinessManifestError(
                "Quantipy contract evidence SHA-256 does not match the readiness manifest"
            )
        raw = json.loads(descriptor.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessManifestError("Quantipy contract evidence must be valid UTF-8 JSON") from exc
    finally:
        descriptor.close()
    data = _require_mapping(raw, label="quantipy contract evidence")
    quantipy_commit = _require_sha256_commit(
        data.get("quantipy_commit"), label="quantipy contract evidence.quantipy_commit"
    )
    return receipt.sha256, quantipy_commit


def canonical_platform_capabilities(
    *,
    reddit_dataset: DatasetAvailability | None = None,
    news_dataset: DatasetAvailability | None = None,
) -> PlatformCapabilities:
    unavailable = DatasetAvailability(
        available=False,
        start_date=None,
        end_date=None,
        record_count=None,
        reason="live database evidence unavailable",
    )
    return PlatformCapabilities.from_dict(
        {
            "security_master": {
                "historical_snapshots_interface": True,
                "historical_security_type_common_stock_filter_pit_certified": True,
                "inactive_listings_interface": True,
                "unadjusted_liquidity_screens_interface": True,
                "universe_history_api_and_client_interface": True,
                "next_session_execution_policy_interface": True,
                "split_actions_interface": True,
                "dividend_actions_interface": True,
                "ticker_detail_market_cap_interface": True,
                "ticker_detail_market_cap_pit_certified": False,
            },
            "market_data": {
                "ohlcv_cache_or_hydrate_interface": True,
                "historical_trades_interface": False,
                "historical_quotes_interface": False,
                "historical_fundamentals_interface": False,
            },
            "reddit_dataset": (reddit_dataset or unavailable).to_dict(),
            "news_dataset": (news_dataset or unavailable).to_dict(),
        }
    )


_CONTRACT_TESTS = (
    "tests/unit/test_security_master.py",
    "tests/unit/test_security_master_history.py",
    "tests/unit/test_massive_provider.py",
    "tests/unit/test_client.py",
    "tests/unit/test_price_data_service.py",
    "tests/unit/test_price_data_schemas.py",
    "tests/unit/test_dynamic_price_coverage.py",
)

_CONTRACT_PROBE = r"""
import asyncio
import inspect
import json
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

import quantipy
import quantipy as qp
from quantipy import client
from quantipy.api.main import create_app
from quantipy.price_data.dynamic_coverage import (
    DynamicPriceCoverageContractMismatchError,
    DynamicPriceCoverageScope,
    DynamicPriceCoverageStatus,
    dynamic_price_coverage_requested_sessions_digest,
)
from quantipy.price_data.integrity import PriceDataSource
from quantipy.price_data.schemas import (
    MarketHours,
    PriceCoverageResponse,
    PriceCoverageSessionReceipt,
    PriceCoverageTickerReceipt,
    SessionCoverageState,
    Timeframe,
)
from quantipy.price_data.service import PriceDataService
from quantipy.security_master.providers.massive import MassiveSecurityMasterProvider
from quantipy.security_master.schemas import (
    CorporateActionType,
    GroupedDailySummaryDTO,
    TickerDetailDTO,
    UniverseHistoryRequest,
    UniverseHistoryResponse,
    UniverseHistoryScreenProfile,
    UniverseScreenResponse,
)

root = Path(os.environ["QUANTIPY_PROBE_ROOT"]).resolve()
for module in (quantipy, client):
    if not Path(inspect.getfile(module)).resolve().is_relative_to(root):
        raise AssertionError("probe imported outside committed checkout")

required = {
    "prices",
    "security_universe",
    "security_universe_history",
    "security_universe_screen",
    "ticker_detail",
    "corporate_actions",
}
if not required.issubset(set(quantipy.__all__)):
    raise AssertionError("public exports missing")
if not all(callable(getattr(client, name, None)) for name in required):
    raise AssertionError("client interface missing")
if "validate_dynamic_price_coverage" not in quantipy.__all__:
    raise AssertionError("dynamic price coverage validator is not publicly exported")
if not callable(getattr(qp, "validate_dynamic_price_coverage", None)):
    raise AssertionError("dynamic price coverage validator is missing")
routes = {route.path for route in create_app().routes}
if not {
    "/security-master/universe/history",
    "/security-master/tickers/{ticker}",
    "/security-master/actions",
}.issubset(routes):
    raise AssertionError("API interface missing")

expected_alembic_head = os.environ[__QUANTIPY_ALEMBIC_HEAD_ENV_VAR__]
expected_alembic_filename = os.environ[__QUANTIPY_ALEMBIC_HEAD_FILENAME_ENV_VAR__]
config = Config(str(root / "alembic.ini"))
config.set_main_option("script_location", str(root / "src/quantipy/migrations"))
script_directory = ScriptDirectory.from_config(config)
if script_directory.get_heads() != [expected_alembic_head]:
    raise AssertionError(f"Alembic head is not {expected_alembic_head}")
resolved_alembic_head = script_directory.get_revision(expected_alembic_head)
if Path(resolved_alembic_head.path).name != expected_alembic_filename:
    raise AssertionError(
        f"Alembic head {expected_alembic_head} is not defined by {expected_alembic_filename}"
    )

for model in (GroupedDailySummaryDTO, TickerDetailDTO, UniverseHistoryRequest):
    if model.model_config.get("strict") is not True:
        raise AssertionError("required schema is not strict")
if UniverseHistoryRequest.model_config.get("extra") != "forbid":
    raise AssertionError("universe-history request permits extra fields")
try:
    GroupedDailySummaryDTO(
        ticker="TEST", summary_date=date(2024, 1, 2), close=Decimal("1"),
        volume=Decimal("1"), adjusted=True,
    )
except ValidationError:
    pass
else:
    raise AssertionError("grouped daily schema accepts adjusted data")
if TickerDetailDTO.model_fields["pit_certified"].default is not False:
    raise AssertionError("ticker market cap is over-attested as PIT")
profile = UniverseHistoryScreenProfile.model_validate({"security_types": ["cs", "CS"]}, strict=True)
if profile.security_types != ("CS",):
    raise AssertionError("historical security-type/common-stock filter is not canonical")
try:
    UniverseHistoryScreenProfile.model_validate({"market_cap": Decimal(1_000_000)}, strict=True)
except ValidationError:
    pass
else:
    raise AssertionError("historical universe profile over-attests market cap")
for model in (UniverseScreenResponse, UniverseHistoryResponse):
    annotation = str(model.model_fields["execution_policy"].annotation)
    if "next-session-or-later" not in annotation:
        raise AssertionError("next-session execution policy missing")

class Response:
    status_code = 200
    def __init__(self, payload):
        self.content = json.dumps(payload).encode()

class Client:
    def __init__(self):
        self.calls = []
    async def get(self, url, *, params):
        clean = {key: value for key, value in params.items() if key != "apiKey"}
        self.calls.append((url, clean))
        if url.endswith("/v3/reference/tickers"):
            active = clean["active"] == "true"
            return Response({"results": [{
                "ticker": "ACTIVE" if active else "INACTIVE",
                "active": active, "market": "stocks", "locale": "us",
            }]})
        if "/v2/aggs/grouped/" in url:
            return Response({"results": [{"T": "TEST", "c": 2, "v": 3}]})
        if url.endswith("/v3/reference/tickers/TEST"):
            return Response({"results": {"ticker": "TEST", "market_cap": 123}})
        if url.endswith("/stocks/v1/splits"):
            return Response({"results": [{
                "ticker": "TEST", "execution_date": "2024-01-02",
                "split_from": 1, "split_to": 2,
            }]})
        if url.endswith("/stocks/v1/dividends"):
            return Response({"results": [{
                "ticker": "TEST", "ex_dividend_date": "2024-01-03", "cash_amount": 1,
            }]})
        raise AssertionError("unexpected provider request")

async def exercise_provider():
    http = Client()
    provider = MassiveSecurityMasterProvider("probe-key", client=http)
    listings = await provider.list_tickers(as_of_date=date(2024, 1, 2))
    if {item.active for item in listings.listings} != {True, False}:
        raise AssertionError("inactive listing behavior missing")
    grouped = await provider.fetch_grouped_daily(summary_date=date(2024, 1, 2))
    if grouped[0].adjusted is not False:
        raise AssertionError("provider emitted adjusted grouped data")
    grouped_calls = [params for url, params in http.calls if "/v2/aggs/grouped/" in url]
    if grouped_calls != [{"adjusted": "false"}]:
        raise AssertionError("provider did not request strict unadjusted data")
    detail = await provider.fetch_ticker_details(ticker="TEST", as_of_date=date(2024, 1, 2))
    if detail.market_cap != Decimal("123") or detail.pit_certified is not False:
        raise AssertionError("ticker market-cap semantics are invalid")
    split = await provider.fetch_corporate_actions(
        action_type=CorporateActionType.SPLIT,
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
    )
    dividend = await provider.fetch_corporate_actions(
        action_type=CorporateActionType.DIVIDEND,
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
    )
    if split.actions[0].action_type is not CorporateActionType.SPLIT:
        raise AssertionError("split interface invalid")
    if dividend.actions[0].action_type is not CorporateActionType.DIVIDEND:
        raise AssertionError("dividend interface invalid")

asyncio.run(exercise_provider())

coverage_sessions = (date(2024, 1, 2), date(2024, 1, 3))
member_union = ("AAPL", "MSFT")
active_roster = {session: ("AAPL",) for session in coverage_sessions}
price_service = PriceDataService(repository=object())
expected_session_bounds = {
    session.session_date: (
        session.all_open,
        session.all_close - timedelta(microseconds=1),
    )
    for session in price_service._expected_sessions(coverage_sessions[0], coverage_sessions[-1])
}
if tuple(expected_session_bounds) != coverage_sessions:
    raise AssertionError("synthetic dynamic coverage sessions are not canonical XNYS sessions")
coverage_response = PriceCoverageResponse(
    requested_start_date=coverage_sessions[0],
    requested_end_date=coverage_sessions[-1],
    timeframe=Timeframe.ONE_MIN,
    market_hours=MarketHours.REGULAR,
    provider_source=PriceDataSource.MASSIVE,
    tickers=tuple(
        PriceCoverageTickerReceipt(
            ticker=ticker,
            sessions=tuple(
                PriceCoverageSessionReceipt(
                    session_date=session,
                    coverage_state=SessionCoverageState.OBSERVED,
                    mode_bar_count=1,
                    provider_request_id=f"probe-{ticker}-{session.isoformat()}",
                    provider_http_status=200,
                    provider_query_count=1,
                    provider_results_count=1,
                    provider_requested_start=expected_session_bounds[session][0],
                    provider_requested_end=expected_session_bounds[session][1],
                    hydrated_at=expected_session_bounds[session][1],
                )
                for session in coverage_sessions
            ),
        )
        for ticker in member_union
    ),
)

def validate_coverage(scope, expected_symbol_session_count):
    return qp.validate_dynamic_price_coverage(
        coverage_response,
        canonical_member_union=member_union,
        requested_sessions=coverage_sessions,
        requested_start_date=coverage_sessions[0],
        requested_end_date=coverage_sessions[-1],
        timeframe=Timeframe.ONE_MIN,
        market_hours=MarketHours.REGULAR,
        active_roster_by_session=active_roster,
        scope=scope,
        expected_symbol_session_count=expected_symbol_session_count,
    )

full_union_receipt = validate_coverage(DynamicPriceCoverageScope.FULL_UNION, 4)
pit_receipt = validate_coverage(DynamicPriceCoverageScope.POINT_IN_TIME, 2)
for receipt in (full_union_receipt, pit_receipt):
    if receipt.status is not DynamicPriceCoverageStatus.COMPLETE:
        raise AssertionError("synthetic dynamic coverage must be complete")
    if (
        receipt.hydrated_symbol_sessions,
        receipt.active_symbol_sessions,
        receipt.inactive_union_symbol_sessions,
    ) != (4, 2, 2):
        raise AssertionError("synthetic dynamic coverage geometry is invalid")
    if (
        receipt.source_requested_start_date,
        receipt.source_requested_end_date,
        receipt.source_timeframe,
        receipt.source_market_hours,
        receipt.source_provider,
    ) != (
        coverage_sessions[0],
        coverage_sessions[-1],
        Timeframe.ONE_MIN,
        MarketHours.REGULAR,
        PriceDataSource.MASSIVE,
    ):
        raise AssertionError("dynamic coverage source identity/provider is invalid")
    if receipt.requested_sessions_digest != dynamic_price_coverage_requested_sessions_digest(
        coverage_sessions
    ):
        raise AssertionError("dynamic coverage XNYS session digest is invalid")
    for field_name in (
        "member_union_digest",
        "requested_sessions_digest",
        "pit_active_roster_digest",
        "source_price_coverage_response_digest",
        "receipt_digest",
    ):
        value = getattr(receipt, field_name)
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise AssertionError(f"{field_name} is not a lowercase SHA-256")
try:
    validate_coverage(DynamicPriceCoverageScope.FULL_UNION, 2)
except DynamicPriceCoverageContractMismatchError:
    pass
else:
    raise AssertionError("wrong full-union asserted count was accepted")
wrong_identity_response = coverage_response.model_copy(
    update={"requested_end_date": date(2024, 1, 4)}
)
try:
    qp.validate_dynamic_price_coverage(
        wrong_identity_response,
        canonical_member_union=member_union,
        requested_sessions=coverage_sessions,
        requested_start_date=coverage_sessions[0],
        requested_end_date=coverage_sessions[-1],
        timeframe=Timeframe.ONE_MIN,
        market_hours=MarketHours.REGULAR,
        active_roster_by_session=active_roster,
        scope=DynamicPriceCoverageScope.FULL_UNION,
        expected_symbol_session_count=4,
    )
except DynamicPriceCoverageContractMismatchError:
    pass
else:
    raise AssertionError("source request identity mismatch was accepted")

print("QUANTIPY_READINESS_PROBE=" + json.dumps({
    "contract_verified": True,
    "dynamic_price_coverage_validator_verified": True,
}, sort_keys=True))
""".replace("__QUANTIPY_ALEMBIC_HEAD_ENV_VAR__", repr(QUANTIPY_ALEMBIC_HEAD_ENV_VAR)).replace(
    "__QUANTIPY_ALEMBIC_HEAD_FILENAME_ENV_VAR__",
    repr(QUANTIPY_ALEMBIC_HEAD_FILENAME_ENV_VAR),
)

_DATASET_PROBE = r"""
import asyncio
import json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from quantipy.common.config import Settings

async def extent(query):
    engine = create_async_engine(Settings().database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                row = (await connection.execute(text(query))).one()
            finally:
                await transaction.rollback()
        count = int(row[2])
        if count < 1 or row[0] is None or row[1] is None:
            return {"available": False, "start_date": None, "end_date": None,
                    "record_count": None, "reason": "no live rows observed"}
        return {"available": True, "start_date": row[0].isoformat(),
                "end_date": row[1].isoformat(), "record_count": count, "reason": None}
    except Exception:
        return {"available": False, "start_date": None, "end_date": None,
                "record_count": None, "reason": "live database query unavailable"}
    finally:
        await engine.dispose()

async def main():
    reddit = await extent(
        "SELECT MIN(post_created_utc)::date, MAX(post_created_utc)::date, COUNT(*) "
        "FROM analyzed_posts"
    )
    news = await extent(
        "SELECT MIN(published_at)::date, MAX(published_at)::date, COUNT(*) FROM news_articles"
    )
    print("QUANTIPY_DATASET_PROBE=" + json.dumps({"reddit": reddit, "news": news}, sort_keys=True))

asyncio.run(main())
"""

_CAMPAIGN_START_DATA_ACCESS_PROBE = r"""
from datetime import datetime
import json
import os
from zoneinfo import ZoneInfo

import quantipy as qp

probe_date = os.environ["QUANTIPY_READINESS_PROBE_DATE"]
api_url = __QUANTIPY_READINESS_LOCAL_API_URL__
screen = qp.security_universe_screen(
    probe_date,
    security_types=("CS",),
    active_only=True,
    limit=1,
    api_url=api_url,
)
if screen.as_of_date.isoformat() != probe_date or not screen.entries:
    raise AssertionError("universe screen returned no campaign-start entries")
if any(not entry.active or entry.type != "CS" or not entry.ticker for entry in screen.entries):
    raise AssertionError("universe screen returned an invalid common-stock entry")
prices = qp.prices(
    "AAPL",
    probe_date,
    probe_date,
    timeframe="1d",
    market_hours="regular",
    api_url=api_url,
)
required_price_columns = {"ticker", "timestamp", "open", "high", "low", "close", "volume"}
price_columns = sorted(str(column) for column in prices.columns)
if len(prices) < 1 or not required_price_columns.issubset(price_columns):
    raise AssertionError("daily regular-hours prices returned no structurally valid rows")
price_tickers = sorted({str(ticker) for ticker in prices["ticker"]})
price_xnys_dates = []
for ticker, timestamp in zip(prices["ticker"], prices["timestamp"], strict=True):
    if ticker != "AAPL":
        raise AssertionError("daily regular-hours prices returned an unexpected ticker")
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise AssertionError("daily regular-hours prices returned a timezone-naive timestamp")
    price_xnys_date = timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    if price_xnys_date != probe_date:
        raise AssertionError(
            "daily regular-hours prices returned a timestamp outside the requested XNYS date"
        )
    price_xnys_dates.append(price_xnys_date)
price_xnys_dates = sorted(set(price_xnys_dates))
print(
    "QUANTIPY_CAMPAIGN_DATA_ACCESS_PROBE="
    + json.dumps(
        {
            "price_columns": price_columns,
            "price_tickers": price_tickers,
            "price_xnys_dates": price_xnys_dates,
            "price_row_count": len(prices),
            "probe_date": probe_date,
            "universe_entry_count": len(screen.entries),
        },
        sort_keys=True,
    )
)
""".replace("__QUANTIPY_READINESS_LOCAL_API_URL__", repr(QUANTIPY_READINESS_LOCAL_API_URL))


def _run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadinessManifestError(f"cannot inspect Quantipy Git checkout: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ReadinessManifestError(f"cannot inspect Quantipy Git checkout: {detail}")
    return result.stdout.strip()


def _run_probe(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    marker: str,
    timeout: int,
) -> Mapping[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadinessManifestError("Quantipy runtime probe could not complete") from exc
    if result.returncode != 0:
        raise ReadinessManifestError("Quantipy runtime contract probe failed closed")
    marked = [
        line.removeprefix(marker) for line in result.stdout.splitlines() if line.startswith(marker)
    ]
    if len(marked) != 1:
        raise ReadinessManifestError("Quantipy runtime probe returned no canonical result")
    try:
        return _require_mapping(json.loads(marked[0]), label="Quantipy runtime probe")
    except json.JSONDecodeError as exc:
        raise ReadinessManifestError("Quantipy runtime probe returned invalid JSON") from exc


def _pytest_count_summary(stdout: str, stderr: str) -> str:
    counts: list[str] = []
    for count, label in re.findall(
        r"\b(\d+)\s+(failed|errors?|passed|skipped|xfailed|xpassed)\b",
        f"{stdout}\n{stderr}",
    ):
        item = f"{count} {label}"
        if item not in counts:
            counts.append(item)
    return ", ".join(counts[:4]) or "no pytest counts reported"


def _run_committed_contract_tests(
    python: Path,
    worktree: Path,
    environment: Mapping[str, str],
    *,
    test_files: Sequence[str] = _CONTRACT_TESTS,
) -> None:
    """Run each contract test file in a fresh interpreter to isolate global metadata."""
    for test_file in test_files:
        try:
            result = subprocess.run(
                [str(python), "-m", "pytest", "-q", test_file],
                cwd=worktree,
                env=dict(environment),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReadinessManifestError(
                f"Quantipy committed contract test timed out: {test_file}"
            ) from exc
        except OSError as exc:
            raise ReadinessManifestError(
                f"Quantipy committed contract test could not run: {test_file}"
            ) from exc
        if result.returncode != 0:
            summary = _pytest_count_summary(result.stdout, result.stderr)
            raise ReadinessManifestError(
                "Quantipy committed contract test failed: "
                f"{test_file} (exit_code={result.returncode}; {summary})"
            )


def _probe_quantipy_contract(root: Path, expected_commit: str) -> tuple[str, Mapping[str, object]]:
    root = root.expanduser().resolve()
    if re.fullmatch(r"[0-9a-f]{40,64}", expected_commit) is None:
        raise ReadinessManifestError("expected Quantipy commit must be a full lowercase Git hash")
    actual_commit = _run_git(root, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise ReadinessManifestError(
            f"Quantipy commit mismatch: expected {expected_commit}, got {actual_commit}"
        )
    if _run_git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ReadinessManifestError("Quantipy tracked worktree must be clean at current HEAD")
    try:
        _run_git(
            root,
            "cat-file",
            "-e",
            (f"{actual_commit}:src/quantipy/migrations/versions/{QUANTIPY_ALEMBIC_HEAD_FILENAME}"),
        )
    except ReadinessManifestError as exc:
        raise ReadinessManifestError(
            f"Quantipy Alembic head {QUANTIPY_ALEMBIC_HEAD_REVISION} is missing"
        ) from exc
    python = root / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ReadinessManifestError("Quantipy own virtualenv Python is unavailable")

    with tempfile.TemporaryDirectory(prefix="quantipy-readiness-") as temporary:
        worktree = Path(temporary) / "checkout"
        try:
            _run_git(root, "worktree", "add", "--detach", "--quiet", str(worktree), actual_commit)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(worktree / "src")
            environment["QUANTIPY_PROBE_ROOT"] = str(worktree)
            environment[QUANTIPY_ALEMBIC_HEAD_ENV_VAR] = QUANTIPY_ALEMBIC_HEAD_REVISION
            environment[QUANTIPY_ALEMBIC_HEAD_FILENAME_ENV_VAR] = QUANTIPY_ALEMBIC_HEAD_FILENAME
            probe = _run_probe(
                [str(python), "-c", _CONTRACT_PROBE],
                cwd=worktree,
                environment=environment,
                marker="QUANTIPY_READINESS_PROBE=",
                timeout=60,
            )
            _run_committed_contract_tests(python, worktree, environment)
        finally:
            if worktree.exists():
                with contextlib.suppress(ReadinessManifestError):
                    _run_git(root, "worktree", "remove", "--force", str(worktree))
    return actual_commit, probe


def _probe_dataset_availability(root: Path) -> tuple[DatasetAvailability, DatasetAvailability]:
    python = root / ".venv/bin/python"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    try:
        raw = _run_probe(
            [str(python), "-c", _DATASET_PROBE],
            cwd=root,
            environment=environment,
            marker="QUANTIPY_DATASET_PROBE=",
            timeout=30,
        )
    except ReadinessManifestError:
        unavailable = DatasetAvailability(
            False, None, None, None, "live database query unavailable"
        )
        return unavailable, unavailable
    return (
        DatasetAvailability.from_dict(raw.get("reddit"), label="reddit dataset probe"),
        DatasetAvailability.from_dict(raw.get("news"), label="news dataset probe"),
    )


def _probe_campaign_start_data_access(root: Path, campaign_start: date) -> Mapping[str, object]:
    """Require the local Quantipy API to serve the campaign-start data entitlement."""
    python = root / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ReadinessManifestError("Quantipy own virtualenv Python is unavailable")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUANTIPY_")
        and key.lower() not in {"all_proxy", "http_proxy", "https_proxy", "no_proxy"}
    }
    environment["PYTHONPATH"] = str(root / "src")
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment[QUANTIPY_READINESS_PROBE_DATE_ENV_VAR] = campaign_start.isoformat()
    try:
        raw = _run_probe(
            [str(python), "-c", _CAMPAIGN_START_DATA_ACCESS_PROBE],
            cwd=root,
            environment=environment,
            marker="QUANTIPY_CAMPAIGN_DATA_ACCESS_PROBE=",
            timeout=120,
        )
        _require_exact_keys(
            raw,
            {
                "price_columns",
                "price_tickers",
                "price_xnys_dates",
                "price_row_count",
                "probe_date",
                "universe_entry_count",
            },
            label="campaign-start data-access probe",
        )
        probe_date = raw["probe_date"]
        universe_entry_count = raw["universe_entry_count"]
        price_row_count = raw["price_row_count"]
        price_columns = raw["price_columns"]
        price_tickers = raw["price_tickers"]
        price_xnys_dates = raw["price_xnys_dates"]
        if probe_date != campaign_start.isoformat():
            raise ReadinessManifestError("campaign-start probe date is not canonical")
        if (
            isinstance(universe_entry_count, bool)
            or not isinstance(universe_entry_count, int)
            or universe_entry_count < 1
        ):
            raise ReadinessManifestError("campaign-start universe result is empty or malformed")
        if (
            isinstance(price_row_count, bool)
            or not isinstance(price_row_count, int)
            or price_row_count < 1
        ):
            raise ReadinessManifestError("campaign-start price result is empty or malformed")
        if not isinstance(price_columns, list) or not all(
            isinstance(column, str) for column in price_columns
        ):
            raise ReadinessManifestError("campaign-start price result is malformed")
        canonical_columns = sorted(set(price_columns))
        if price_columns != canonical_columns or not {
            "ticker",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        }.issubset(canonical_columns):
            raise ReadinessManifestError("campaign-start price result is malformed")
        if price_tickers != ["AAPL"]:
            raise ReadinessManifestError("campaign-start price ticker result is malformed")
        if price_xnys_dates != [campaign_start.isoformat()]:
            raise ReadinessManifestError("campaign-start price date result is malformed")
        return {
            "price_columns": canonical_columns,
            "price_tickers": ["AAPL"],
            "price_xnys_dates": [campaign_start.isoformat()],
            "price_row_count": price_row_count,
            "probe_date": probe_date,
            "universe_entry_count": universe_entry_count,
        }
    except ReadinessManifestError as exc:
        raise ReadinessManifestError("campaign-start live data-access probe failed closed") from exc


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _validate_candidate_outputs(
    *,
    manifest_bytes: bytes,
    manifest_payload: Mapping[str, object],
    evidence_bytes: bytes,
    evidence_payload: Mapping[str, object],
    evidence_path: Path,
    xnys: _ImmutableEvidenceDescriptor,
) -> PlatformReadinessManifest:
    if evidence_bytes != _canonical_json_bytes(evidence_payload):
        raise ReadinessManifestError("Quantipy evidence candidate bytes are not canonical")
    if manifest_bytes != _canonical_json_bytes(manifest_payload):
        raise ReadinessManifestError("readiness manifest candidate bytes are not canonical")
    try:
        decoded_evidence = json.loads(evidence_bytes)
        decoded_manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessManifestError("readiness candidate bytes are invalid JSON") from exc
    if decoded_evidence != evidence_payload or decoded_manifest != manifest_payload:
        raise ReadinessManifestError("readiness candidate bytes changed during validation")
    return PlatformReadinessManifest.from_dict(
        decoded_manifest,
        _evidence_sha256_overrides={
            evidence_path: hashlib.sha256(evidence_bytes).hexdigest(),
            xnys.path: xnys.sha256,
        },
    )


def _validate_output_paths(
    *, manifest_path: Path, evidence_path: Path, xnys_path: Path, quantipy_root: Path
) -> tuple[Path, Path, Path]:
    resolved = (
        manifest_path.expanduser().resolve(),
        evidence_path.expanduser().resolve(),
        xnys_path.expanduser().resolve(),
    )
    if len(set(resolved)) != len(resolved):
        raise ReadinessManifestError("manifest, Quantipy evidence, and XNYS paths must be distinct")
    root = quantipy_root.expanduser().resolve()
    for output in resolved[:2]:
        if output.is_relative_to(root):
            raise ReadinessManifestError("readiness outputs must be outside the Quantipy tree")
    return resolved


def _atomic_write_outputs(
    payloads: Mapping[Path, bytes],
    *,
    before_commit: Callable[[Mapping[Path, Path]], None] | None = None,
) -> None:
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: set[Path] = set()
    try:
        for target, content in payloads.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
            temporary = Path(temporary_name)
            staged[target] = temporary
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        if before_commit is not None:
            before_commit(MappingProxyType(staged))
        for target in payloads:
            if target.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    dir=target.parent, prefix=f".{target.name}.", suffix=".bak"
                )
                os.close(descriptor)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(target, backup)
                backups[target] = backup
            os.replace(staged[target], target)
            replaced.add(target)
    except OSError as exc:
        for target in reversed(tuple(payloads)):
            if target in replaced:
                with contextlib.suppress(OSError):
                    target.unlink()
            rollback_backup = backups.get(target)
            if rollback_backup is not None:
                with contextlib.suppress(OSError):
                    os.replace(rollback_backup, target)
        raise ReadinessManifestError("cannot atomically write readiness outputs") from exc
    finally:
        for path in (*staged.values(), *backups.values()):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)


def build_quantipy_readiness(
    *,
    manifest_path: Path,
    quantipy_evidence_path: Path,
    quantipy_root: Path,
    expected_quantipy_commit: str,
    xnys_calendar_path: Path,
    campaign_xnys_start: date,
    campaign_xnys_end: date,
) -> PlatformReadinessManifest:
    """Generate a schema-v3 readiness manifest with schema-v3 Quantipy contract evidence."""
    manifest_path, evidence_path, xnys_path = _validate_output_paths(
        manifest_path=manifest_path,
        evidence_path=quantipy_evidence_path,
        xnys_path=xnys_calendar_path,
        quantipy_root=quantipy_root,
    )
    xnys = _ImmutableEvidenceDescriptor.open(xnys_path)
    try:
        try:
            xnys_evidence = XNYSCalendarEvidence.from_dict(json.loads(xnys.read_bytes()))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReadinessManifestError("XNYS calendar evidence must be valid UTF-8 JSON") from exc
        _validate_campaign_xnys_interval(
            xnys=xnys_evidence,
            campaign_start=campaign_xnys_start,
            campaign_end=campaign_xnys_end,
        )
        actual_commit, contract_probe = _probe_quantipy_contract(
            quantipy_root.expanduser().resolve(), expected_quantipy_commit
        )
        reddit_dataset, news_dataset = _probe_dataset_availability(
            quantipy_root.expanduser().resolve()
        )
        campaign_data_access_probe = _probe_campaign_start_data_access(
            quantipy_root.expanduser().resolve(), campaign_xnys_start
        )
        capabilities = canonical_platform_capabilities(
            reddit_dataset=reddit_dataset,
            news_dataset=news_dataset,
        )
        evidence_payload: dict[str, object] = {
            "capabilities": capabilities.to_dict(),
            "quantipy_commit": actual_commit,
            "schema_version": QUANTIPY_DATA_CONTRACT_EVIDENCE_SCHEMA_VERSION,
            "verification": {
                "alembic_head": QUANTIPY_ALEMBIC_HEAD_REVISION,
                "campaign_start_data_access_probe": dict(campaign_data_access_probe),
                "committed_contract_tests": list(_CONTRACT_TESTS),
                "runtime_probe": dict(contract_probe),
                "tracked_worktree_clean": True,
            },
        }
        evidence_bytes = _canonical_json_bytes(evidence_payload)
        evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
        snapshot_material = f"{actual_commit}:{evidence_sha256}:{xnys.sha256}"
        snapshot_sha256 = hashlib.sha256(snapshot_material.encode("ascii")).hexdigest()
        manifest_payload: dict[str, object] = {
            "capabilities": capabilities.to_dict(),
            "evidence": {
                EvidenceId.QUANTIPY_DATA_CONTRACT.value: {
                    "path": str(evidence_path),
                    "reason": None,
                    "sha256": evidence_sha256,
                },
                EvidenceId.XNYS_TRADING_CALENDAR.value: {
                    "path": str(xnys_path),
                    "reason": None,
                    "sha256": xnys.sha256,
                },
            },
            "manifest_id": f"quantipy-{actual_commit[:16]}",
            "reason": None,
            "schema_version": PLATFORM_READINESS_SCHEMA_VERSION,
            "snapshot_id": f"snapshot-{snapshot_sha256[:16]}",
            "status": ReadinessStatus.READY.value,
        }
        manifest_bytes = _canonical_json_bytes(manifest_payload)
        candidate = _validate_candidate_outputs(
            manifest_bytes=manifest_bytes,
            manifest_payload=manifest_payload,
            evidence_bytes=evidence_bytes,
            evidence_payload=evidence_payload,
            evidence_path=evidence_path,
            xnys=xnys,
        )

        def validate_staged(staged: Mapping[Path, Path]) -> None:
            _validate_candidate_outputs(
                manifest_bytes=staged[manifest_path].read_bytes(),
                manifest_payload=manifest_payload,
                evidence_bytes=staged[evidence_path].read_bytes(),
                evidence_payload=evidence_payload,
                evidence_path=evidence_path,
                xnys=xnys,
            )
            xnys.revalidate()

        _atomic_write_outputs(
            {
                evidence_path: evidence_bytes,
                manifest_path: manifest_bytes,
            },
            before_commit=validate_staged,
        )
        return candidate
    finally:
        xnys.close()
