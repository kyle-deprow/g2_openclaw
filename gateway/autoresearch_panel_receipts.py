"""Shared fail-closed decoder for Quantipy compact panel coverage evidence."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import zlib
from collections.abc import Mapping
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

PANEL_RECEIPT_CONTRACT_VERSION = "research-price-panel-receipt-v2"
PANEL_REQUEST_CONTRACT_VERSION = "research-price-panel-v1"
COMPACT_COVERAGE_CONTRACT_VERSION = "price-coverage-compact-v1"
COMPACT_COVERAGE_ENCODING = "canonical-json-zlib-base64-v1"
PRICE_COVERAGE_CONTRACT_VERSION = "price-coverage-v1"
PANEL_RECEIPT_MAX_BYTES = 4 * 1024 * 1024
RUN_ENVELOPE_MAX_BYTES = 8 * 1024 * 1024
# Mirrors quantipy price_data.schemas caps (raised in quantipy 9f55006): a
# 103-ticker four-year 1-minute panel produces ~38.4MB of coverage evidence.
COMPACT_COVERAGE_MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
COMPACT_COVERAGE_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
COMPACT_COVERAGE_MAX_RATIO = 200.0
_MAX_BASE64_PAYLOAD_CHARS = 4 * ((COMPACT_COVERAGE_MAX_COMPRESSED_BYTES + 2) // 3)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PanelReceiptValidationError(ValueError):
    """Raised when compact panel evidence fails a wire-contract boundary check."""


def validate_research_panel_receipt(
    value: object,
    *,
    label: str,
    panel_bytes: bytes | None = None,
) -> dict[str, object]:
    """Validate one complete Quantipy panel receipt and optional panel payload."""
    data = _exact_mapping(
        value,
        label=label,
        expected=(
            "contract_version",
            "request",
            "request_sha256",
            "coverage",
            "coverage_sha256",
            "panel_sha256",
            "hydrated_at",
            "exported_at",
        ),
    )
    if data["contract_version"] != PANEL_RECEIPT_CONTRACT_VERSION:
        raise PanelReceiptValidationError(f"{label}.contract_version is invalid")
    request = validate_research_panel_request(data["request"], label=f"{label}.request")
    coverage = decode_compact_price_coverage(data["coverage"], label=f"{label}.coverage")
    request_sha256 = _sha256(data["request_sha256"], label=f"{label}.request_sha256")
    coverage_sha256 = _sha256(data["coverage_sha256"], label=f"{label}.coverage_sha256")
    panel_sha256 = _sha256(data["panel_sha256"], label=f"{label}.panel_sha256")
    hydrated_at = _canonical_utc_value(data["hydrated_at"], label=f"{label}.hydrated_at")
    exported_at = _canonical_utc_value(data["exported_at"], label=f"{label}.exported_at")
    if exported_at < hydrated_at:
        raise PanelReceiptValidationError(f"{label} export precedes hydration")
    if request_sha256 != _canonical_json_sha256(request):
        raise PanelReceiptValidationError(f"{label} request digest does not match request")
    compact_coverage = _exact_mapping(
        data["coverage"],
        label=f"{label}.coverage",
        expected=(
            "contract_version",
            "encoding",
            "compressed_size",
            "expanded_size",
            "compression_ratio",
            "coverage_sha256",
            "payload",
        ),
    )
    if coverage_sha256 != _sha256(
        compact_coverage["coverage_sha256"], label=f"{label}.coverage.coverage_sha256"
    ):
        raise PanelReceiptValidationError(f"{label} coverage digest does not bind compact coverage")
    if coverage_sha256 != _canonical_json_sha256(coverage):
        raise PanelReceiptValidationError(f"{label} coverage digest does not match coverage")
    request_tickers = request["tickers"]
    coverage_tickers = coverage["tickers"]
    assert isinstance(request_tickers, list)
    assert isinstance(coverage_tickers, list)
    if [ticker["ticker"] for ticker in coverage_tickers] != request_tickers:
        raise PanelReceiptValidationError(f"{label} coverage tickers do not match request")
    request_start = _utc_datetime(request["start"], label=f"{label}.request.start")
    request_end = _utc_datetime(request["end"], label=f"{label}.request.end")
    new_york = ZoneInfo("America/New_York")
    if (
        coverage["requested_start_date"] != request_start.astimezone(new_york).date().isoformat()
        or coverage["requested_end_date"] != request_end.astimezone(new_york).date().isoformat()
        or coverage["market_hours"] != request["market_hours"]
        or coverage["timeframe"] != "1min"
    ):
        raise PanelReceiptValidationError(f"{label} coverage does not match panel request")
    if panel_bytes is not None and hashlib.sha256(panel_bytes).hexdigest() != panel_sha256:
        raise PanelReceiptValidationError(f"{label} panel digest does not match panel bytes")
    return {
        "contract_version": PANEL_RECEIPT_CONTRACT_VERSION,
        "request": request,
        "request_sha256": request_sha256,
        "coverage": dict(compact_coverage),
        "coverage_sha256": coverage_sha256,
        "panel_sha256": panel_sha256,
        "hydrated_at": _canonical_utc_text(hydrated_at),
        "exported_at": _canonical_utc_text(exported_at),
    }


def validate_research_panel_request(value: object, *, label: str) -> dict[str, object]:
    """Validate and normalize the public research-price-panel-v1 request."""
    data = _exact_mapping(
        value,
        label=label,
        expected=("contract_version", "tickers", "start", "end", "timeframe", "market_hours"),
    )
    if data["contract_version"] != PANEL_REQUEST_CONTRACT_VERSION:
        raise PanelReceiptValidationError(f"{label}.contract_version is invalid")
    tickers_raw = data["tickers"]
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise PanelReceiptValidationError(f"{label}.tickers must be a non-empty JSON array")
    tickers = [_string(ticker, label=f"{label}.tickers") for ticker in tickers_raw]
    if any(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker) is None for ticker in tickers):
        raise PanelReceiptValidationError(f"{label}.tickers contains a noncanonical ticker")
    if tickers != sorted(tickers) or len(tickers) != len(set(tickers)):
        raise PanelReceiptValidationError(f"{label}.tickers must be unique and sorted")
    start = _canonical_utc_value(data["start"], label=f"{label}.start")
    end = _canonical_utc_value(data["end"], label=f"{label}.end")
    if start > end:
        raise PanelReceiptValidationError(f"{label} start must not be after end")
    timeframe = _enum(
        data["timeframe"],
        label=f"{label}.timeframe",
        allowed=frozenset(("1min", "5min", "15min", "30min", "1h", "4h", "1d")),
    )
    market_hours = _enum(
        data["market_hours"],
        label=f"{label}.market_hours",
        allowed=frozenset(("all", "regular", "extended")),
    )
    return {
        "contract_version": PANEL_REQUEST_CONTRACT_VERSION,
        "tickers": tickers,
        "start": _canonical_utc_text(start),
        "end": _canonical_utc_text(end),
        "timeframe": timeframe,
        "market_hours": market_hours,
    }


def decode_compact_price_coverage(value: object, *, label: str) -> dict[str, object]:
    """Decode exactly one bounded canonical compact `price-coverage-v1` object.

    This intentionally returns the expanded object only to its immediate validator;
    callers persist the original compact object, never this return value.
    """
    compact = _exact_mapping(
        value,
        label=label,
        expected=(
            "contract_version",
            "encoding",
            "compressed_size",
            "expanded_size",
            "compression_ratio",
            "coverage_sha256",
            "payload",
        ),
    )
    if compact["contract_version"] != COMPACT_COVERAGE_CONTRACT_VERSION:
        raise PanelReceiptValidationError(f"{label}.contract_version is invalid")
    if compact["encoding"] != COMPACT_COVERAGE_ENCODING:
        raise PanelReceiptValidationError(f"{label}.encoding is invalid")
    compressed_size = _bounded_int(
        compact["compressed_size"],
        label=f"{label}.compressed_size",
        maximum=COMPACT_COVERAGE_MAX_COMPRESSED_BYTES,
    )
    expanded_size = _bounded_int(
        compact["expanded_size"],
        label=f"{label}.expanded_size",
        maximum=COMPACT_COVERAGE_MAX_EXPANDED_BYTES,
    )
    ratio = _bounded_ratio(compact["compression_ratio"], label=f"{label}.compression_ratio")
    coverage_sha256 = _sha256(compact["coverage_sha256"], label=f"{label}.coverage_sha256")
    payload = compact["payload"]
    if not isinstance(payload, str) or not payload or len(payload) > _MAX_BASE64_PAYLOAD_CHARS:
        raise PanelReceiptValidationError(f"{label}.payload exceeds its raw size limit")

    try:
        payload_bytes = payload.encode("ascii")
        compressed = base64.b64decode(payload_bytes, validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise PanelReceiptValidationError(f"{label}.payload is not strict base64") from exc
    if base64.b64encode(compressed) != payload_bytes:
        raise PanelReceiptValidationError(f"{label}.payload is not canonical base64")
    if len(compressed) != compressed_size:
        raise PanelReceiptValidationError(f"{label}.compressed_size does not match payload")
    if expanded_size / compressed_size != ratio:
        raise PanelReceiptValidationError(f"{label}.compression_ratio does not match sizes")

    try:
        decompressor = zlib.decompressobj()
        expanded = decompressor.decompress(compressed, expanded_size + 1)
    except zlib.error as exc:
        raise PanelReceiptValidationError(f"{label}.payload is not valid zlib data") from exc
    if len(expanded) != expanded_size:
        raise PanelReceiptValidationError(f"{label}.expanded_size does not match payload")
    if not decompressor.eof:
        raise PanelReceiptValidationError(f"{label}.payload is incomplete or exceeds its bound")
    if decompressor.unused_data or decompressor.unconsumed_tail:
        raise PanelReceiptValidationError(f"{label}.payload has trailing data")

    expanded_coverage = _parse_canonical_json(expanded, label=label)
    if expanded_coverage.get("contract_version") != PRICE_COVERAGE_CONTRACT_VERSION:
        raise PanelReceiptValidationError(f"{label}.payload coverage contract_version is invalid")
    if hashlib.sha256(expanded).hexdigest() != coverage_sha256:
        raise PanelReceiptValidationError(f"{label}.coverage digest does not match payload")
    normalized_coverage = validate_price_coverage(expanded_coverage, label=label)
    canonical_coverage = json.dumps(
        normalized_coverage, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if expanded != canonical_coverage:
        raise PanelReceiptValidationError(f"{label}.payload is not Quantipy canonical JSON")
    return normalized_coverage


def validate_price_coverage(value: object, *, label: str) -> dict[str, object]:
    """Validate every strict PriceCoverageResponse field accepted by Quantipy."""
    data = _exact_mapping(
        value,
        label=label,
        expected=(
            "contract_version",
            "requested_start_date",
            "requested_end_date",
            "timeframe",
            "market_hours",
            "provider_source",
            "tickers",
        ),
    )
    if data["contract_version"] != PRICE_COVERAGE_CONTRACT_VERSION:
        raise PanelReceiptValidationError(f"{label}.contract_version is invalid")
    start = _date(data["requested_start_date"], label=f"{label}.requested_start_date")
    end = _date(data["requested_end_date"], label=f"{label}.requested_end_date")
    if start > end:
        raise PanelReceiptValidationError(f"{label}.requested date range is invalid")
    timeframe = _enum(
        data["timeframe"],
        label=f"{label}.timeframe",
        allowed=frozenset(("1min", "5min", "15min", "30min", "1h", "4h", "1d")),
    )
    market_hours = _enum(
        data["market_hours"],
        label=f"{label}.market_hours",
        allowed=frozenset(("all", "regular", "extended")),
    )
    provider_source = _enum(
        data["provider_source"],
        label=f"{label}.provider_source",
        allowed=frozenset(("massive", "databento")),
    )
    tickers_raw = data["tickers"]
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise PanelReceiptValidationError(f"{label}.tickers must be a non-empty JSON array")
    normalized_tickers: list[dict[str, object]] = []
    ticker_names: list[str] = []
    for ticker_index, ticker_raw in enumerate(tickers_raw):
        ticker_label = f"{label}.tickers[{ticker_index}]"
        ticker = _exact_mapping(ticker_raw, label=ticker_label, expected=("ticker", "sessions"))
        ticker_name = _string(ticker["ticker"], label=f"{ticker_label}.ticker")
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker_name) is None:
            raise PanelReceiptValidationError(f"{ticker_label}.ticker is noncanonical")
        sessions_raw = ticker["sessions"]
        if not isinstance(sessions_raw, list):
            raise PanelReceiptValidationError(f"{ticker_label}.sessions must be a JSON array")
        normalized_sessions: list[dict[str, object]] = []
        session_dates: list[date] = []
        for session_index, session_raw in enumerate(sessions_raw):
            session_label = f"{ticker_label}.sessions[{session_index}]"
            session = _exact_mapping(
                session_raw,
                label=session_label,
                expected=(
                    "session_date",
                    "coverage_state",
                    "mode_bar_count",
                    "provider_request_id",
                    "provider_http_status",
                    "provider_query_count",
                    "provider_results_count",
                    "provider_requested_start",
                    "provider_requested_end",
                    "hydrated_at",
                ),
            )
            session_date = _date(session["session_date"], label=f"{session_label}.session_date")
            coverage_state = _enum(
                session["coverage_state"],
                label=f"{session_label}.coverage_state",
                allowed=frozenset(("observed", "provider_confirmed_empty")),
            )
            mode_bar_count = _nonnegative_int(
                session["mode_bar_count"], label=f"{session_label}.mode_bar_count"
            )
            request_id = _string(
                session["provider_request_id"], label=f"{session_label}.provider_request_id"
            )
            if not request_id.strip():
                raise PanelReceiptValidationError(
                    f"{session_label}.provider_request_id must not be blank"
                )
            if session["provider_http_status"] != 200 or isinstance(
                session["provider_http_status"], bool
            ):
                raise PanelReceiptValidationError(
                    f"{session_label}.provider_http_status must be 200"
                )
            query_count = _nonnegative_int(
                session["provider_query_count"], label=f"{session_label}.provider_query_count"
            )
            results_count = _nonnegative_int(
                session["provider_results_count"], label=f"{session_label}.provider_results_count"
            )
            requested_start = _utc_datetime(
                session["provider_requested_start"],
                label=f"{session_label}.provider_requested_start",
            )
            requested_end = _utc_datetime(
                session["provider_requested_end"], label=f"{session_label}.provider_requested_end"
            )
            hydrated_at = _utc_datetime(
                session["hydrated_at"], label=f"{session_label}.hydrated_at"
            )
            if query_count != results_count or results_count < mode_bar_count:
                raise PanelReceiptValidationError(
                    f"{session_label} provider counts are inconsistent"
                )
            if requested_start > requested_end:
                raise PanelReceiptValidationError(
                    f"{session_label} provider request range is invalid"
                )
            session_dates.append(session_date)
            normalized_sessions.append(
                {
                    "session_date": session_date.isoformat(),
                    "coverage_state": coverage_state,
                    "mode_bar_count": mode_bar_count,
                    "provider_request_id": request_id,
                    "provider_http_status": 200,
                    "provider_query_count": query_count,
                    "provider_results_count": results_count,
                    "provider_requested_start": _canonical_utc_text(requested_start),
                    "provider_requested_end": _canonical_utc_text(requested_end),
                    "hydrated_at": _canonical_utc_text(hydrated_at),
                }
            )
        if session_dates != sorted(session_dates) or len(session_dates) != len(set(session_dates)):
            raise PanelReceiptValidationError(
                f"{ticker_label}.sessions must have unique ordered dates"
            )
        ticker_names.append(ticker_name)
        normalized_tickers.append({"ticker": ticker_name, "sessions": normalized_sessions})
    if ticker_names != sorted(ticker_names) or len(ticker_names) != len(set(ticker_names)):
        raise PanelReceiptValidationError(f"{label}.tickers must be unique and ordered")
    return {
        "contract_version": PRICE_COVERAGE_CONTRACT_VERSION,
        "requested_start_date": start.isoformat(),
        "requested_end_date": end.isoformat(),
        "timeframe": timeframe,
        "market_hours": market_hours,
        "provider_source": provider_source,
        "tickers": normalized_tickers,
    }


def _exact_mapping(value: object, *, label: str, expected: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise PanelReceiptValidationError(f"{label} has an invalid object shape")
    return value


def _bounded_int(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise PanelReceiptValidationError(f"{label} is outside its permitted bounds")
    return value


def _bounded_ratio(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PanelReceiptValidationError(f"{label} must be a finite number")
    ratio = float(value)
    if not math.isfinite(ratio) or not 1.0 <= ratio <= COMPACT_COVERAGE_MAX_RATIO:
        raise PanelReceiptValidationError(f"{label} is outside its permitted bounds")
    return ratio


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PanelReceiptValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_canonical_json(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PanelReceiptValidationError(f"{label}.payload is not strict UTF-8 JSON") from exc
    _reject_nonfinite_values(value, label=label)
    if not isinstance(value, dict):
        raise PanelReceiptValidationError(f"{label}.payload must contain a JSON object")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if raw != canonical:
        raise PanelReceiptValidationError(f"{label}.payload is not canonical JSON")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value}")


def _reject_nonfinite_values(value: object, *, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PanelReceiptValidationError(f"{label}.payload contains a non-finite JSON number")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_nonfinite_values(item, label=label)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite_values(item, label=label)


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PanelReceiptValidationError(f"{label} must be a string")
    return value


def _enum(value: object, *, label: str, allowed: frozenset[str]) -> str:
    result = _string(value, label=label)
    if result not in allowed:
        raise PanelReceiptValidationError(f"{label} is not an allowed value")
    return result


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PanelReceiptValidationError(f"{label} must be a non-negative integer")
    return value


def _date(value: object, *, label: str) -> date:
    raw = _string(value, label=label)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise PanelReceiptValidationError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise PanelReceiptValidationError(f"{label} must use canonical ISO date spelling")
    return parsed


def _utc_datetime(value: object, *, label: str) -> datetime:
    raw = _string(value, label=label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise PanelReceiptValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PanelReceiptValidationError(f"{label} must be UTC-aware")
    return parsed


def _canonical_utc_value(value: object, *, label: str) -> datetime:
    raw = _string(value, label=label)
    parsed = _utc_datetime(raw, label=label)
    if raw != _canonical_utc_text(parsed):
        raise PanelReceiptValidationError(f"{label} must use canonical UTC spelling")
    return parsed


def _canonical_utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
