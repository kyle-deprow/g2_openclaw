"""Decode the trusted panel sessions out of a stored price-panel receipt.

Shared by the CLI admission wiring and the ``hypothesis-create`` bounds checks, so both read
the receipt's compact coverage evidence the same way and never derive calendar dates.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from datetime import date

from .admission import DAILY_RECEIPT_CONTRACT, ValidationReceipt

MEMBERSHIP_CONTRACT = "pit-universe-membership-v1"


def strict_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def decode_panel_sessions(
    receipt_wire: dict[str, object], receipt: ValidationReceipt
) -> tuple[str, ...]:
    """Decode the trusted compact receipt artifact; never derive calendar dates."""
    raw_coverage = strict_object(receipt_wire.get("coverage"), "receipt.coverage")
    expected_keys = {
        "compressed_size",
        "compression_ratio",
        "contract_version",
        "coverage_sha256",
        "encoding",
        "expanded_size",
        "payload",
    }
    if set(raw_coverage) != expected_keys:
        raise ValueError("receipt.coverage has unexpected keys")
    compressed_size = raw_coverage.get("compressed_size")
    expanded_size = raw_coverage.get("expanded_size")
    payload = raw_coverage.get("payload")
    if (
        type(compressed_size) is not int
        or type(expanded_size) is not int
        or compressed_size < 1
        or expanded_size < 1
        or not isinstance(payload, str)
        or raw_coverage.get("contract_version") != "price-coverage-compact-v1"
        or raw_coverage.get("encoding") != "canonical-json-zlib-base64-v1"
        or raw_coverage.get("coverage_sha256") != receipt.coverage_sha256
    ):
        raise ValueError("receipt coverage is not the pinned compact contract")
    try:
        compressed = base64.b64decode(payload.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("receipt coverage payload is not canonical base64") from exc
    if len(compressed) != compressed_size:
        raise ValueError("receipt coverage compressed size does not match payload")
    try:
        decompressor = zlib.decompressobj()
        expanded = decompressor.decompress(compressed, expanded_size + 1)
    except zlib.error as exc:
        raise ValueError("receipt coverage payload is not valid zlib") from exc
    if (
        len(expanded) != expanded_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("receipt coverage payload is incomplete or has trailing data")
    if hashlib.sha256(expanded).hexdigest() != receipt.coverage_sha256:
        raise ValueError("receipt coverage digest does not match expanded evidence")
    try:
        expanded_object = json.loads(expanded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt coverage payload is not JSON") from exc
    canonical = json.dumps(expanded_object, sort_keys=True, separators=(",", ":")).encode()
    if expanded != canonical:
        raise ValueError("receipt coverage payload is not canonical JSON")
    coverage = strict_object(expanded_object, "expanded receipt coverage")
    if coverage.get("contract_version") != "price-coverage-v1":
        raise ValueError("expanded receipt coverage contract is unsupported")
    tickers = coverage.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("expanded receipt coverage has no tickers")
    session_sets: list[tuple[str, ...]] = []
    for ticker_index, raw_ticker in enumerate(tickers):
        ticker = strict_object(raw_ticker, f"expanded receipt tickers[{ticker_index}]")
        sessions = ticker.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            raise ValueError("expanded receipt ticker has no sessions")
        dates: list[str] = []
        for session_index, raw_session in enumerate(sessions):
            session = strict_object(
                raw_session,
                f"expanded receipt tickers[{ticker_index}].sessions[{session_index}]",
            )
            date_text = session.get("session_date")
            if not isinstance(date_text, str):
                raise ValueError("expanded receipt session date is not text")
            try:
                parsed = date.fromisoformat(date_text)
            except ValueError as exc:
                raise ValueError("expanded receipt session date is not ISO") from exc
            if parsed.isoformat() != date_text:
                raise ValueError("expanded receipt session date is not canonical")
            if session.get("coverage_state") != "observed":
                raise ValueError("expanded receipt session is not observed")
            dates.append(date_text)
        if dates != sorted(set(dates)):
            raise ValueError("expanded receipt sessions are not unique and ordered")
        session_sets.append(tuple(dates))
    if any(item != session_sets[0] for item in session_sets[1:]):
        raise ValueError("expanded receipt tickers disagree on panel sessions")
    return session_sets[0]


def is_daily_receipt_wire(wire: object) -> bool:
    """Whether parsed receipt JSON declares the daily-panel contract."""
    return isinstance(wire, dict) and wire.get("contract") == DAILY_RECEIPT_CONTRACT


def _iso_date(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use canonical YYYY-MM-DD form")
    return value


def daily_request(wire: dict[str, object]) -> dict[str, object]:
    """Return the validated ``request`` object of a daily receipt."""
    request = strict_object(wire.get("request"), "receipt.request")
    start = _iso_date(request.get("start"), "receipt.request.start")
    end = _iso_date(request.get("end"), "receipt.request.end")
    if start > end:
        raise ValueError("receipt.request range is reversed")
    if request.get("adjusted") is not True:
        raise ValueError("daily receipt request must be adjusted")
    membership = request.get("membership_sha256")
    if membership is not None and (
        not isinstance(membership, str) or re.fullmatch(r"[0-9a-f]{64}", membership) is None
    ):
        raise ValueError("receipt.request.membership_sha256 must be a SHA-256 or null")
    return request


def daily_receipt_membership_sha256(wire: dict[str, object]) -> str | None:
    """The membership digest a daily receipt binds, or ``None`` when it binds none."""
    membership = daily_request(wire).get("membership_sha256")
    return None if membership is None else str(membership)


def _weekday(value: str, name: str) -> None:
    """Refuse a weekend date.

    The gateway has no exchange calendar (admission never derives calendar dates), so a
    holiday cannot be recognised here; the evaluator builds its own session list from the
    XNYS calendar and the in-sandbox ``validate-inputs`` is the authoritative gate for it.
    """
    if date.fromisoformat(value).weekday() >= 5:
        raise ValueError(f"{name} {value} is a weekend, not an XNYS session")


def decode_daily_sessions(
    wire: dict[str, object], *, start: str | None = None, end: str | None = None
) -> tuple[str, ...]:
    """The panel sessions a daily receipt lists: every session with a positive row count.

    With ``start`` and ``end`` (the evaluator spec range) the list is clipped to that range and
    must match what the evaluator will see there: a listed session with no rows, or a
    ``missing_sessions`` entry, inside the range is refused (the evaluator treats every XNYS
    session in range as a panel session). Weekend dates are refused anywhere.
    """
    request = daily_request(wire)
    counts = strict_object(wire.get("session_ticker_counts"), "receipt.session_ticker_counts")
    missing_raw = wire.get("missing_sessions", [])
    if not isinstance(missing_raw, list):
        raise ValueError("receipt.missing_sessions must be an array")
    missing = [_iso_date(item, "receipt.missing_sessions entry") for item in missing_raw]
    low = _iso_date(start, "evaluation range start") if start is not None else None
    high = _iso_date(end, "evaluation range end") if end is not None else None
    ranged = low is not None and high is not None

    def in_range(value: str) -> bool:
        return not ranged or (low is not None and high is not None and low <= value <= high)

    sessions: list[str] = []
    for key, count in counts.items():
        _iso_date(key, "receipt.session_ticker_counts session")
        _weekday(key, "receipt.session_ticker_counts session")
        if type(count) is not int or count < 0:
            raise ValueError("receipt.session_ticker_counts counts must be non-negative integers")
        if count == 0 and ranged and in_range(key):
            raise ValueError(
                f"daily receipt lists session {key} with no rows inside the evaluation range"
            )
        if count > 0 and in_range(key):
            sessions.append(key)
    for key in missing:
        _weekday(key, "receipt.missing_sessions entry")
        if ranged and in_range(key):
            raise ValueError(
                f"daily receipt reports session {key} missing inside the evaluation range"
            )
    sessions.sort()
    if not sessions:
        raise ValueError("daily receipt lists no sessions")
    all_keys = sorted(counts)
    if all_keys and (all_keys[0] < str(request["start"]) or all_keys[-1] > str(request["end"])):
        raise ValueError("daily receipt sessions fall outside its request range")
    return tuple(sessions)


def membership_universe(membership_bytes: bytes) -> tuple[str, ...]:
    """Sorted union of the membership file's member tickers and reference tickers."""
    try:
        parsed = json.loads(membership_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("universe membership is not JSON") from exc
    document = strict_object(parsed, "universe membership")
    if document.get("contract") != MEMBERSHIP_CONTRACT:
        raise ValueError(f"universe membership contract must be {MEMBERSHIP_CONTRACT}")
    members = strict_object(document.get("members"), "universe membership members")
    references = document.get("reference_tickers", [])
    if not isinstance(references, list):
        raise ValueError("universe membership reference_tickers must be an array")
    if any(not isinstance(ticker, str) for ticker in references):
        raise ValueError("universe membership reference_tickers must be strings")
    tickers = set(members) | {str(ticker) for ticker in references}
    if not tickers or any(not ticker for ticker in tickers):
        raise ValueError("universe membership lists no tickers")
    return tuple(sorted(tickers))


def daily_receipt_view(
    wire: dict[str, object], *, receipt_sha256: str, membership_bytes: bytes
) -> ValidationReceipt:
    """Typed admission view of a ``research-price-panel-daily-v1`` receipt.

    Its tickers are the membership union plus reference tickers, and the membership
    file must be the one the receipt binds.
    """
    request = daily_request(wire)
    bound = request.get("membership_sha256")
    if bound is None or hashlib.sha256(membership_bytes).hexdigest() != bound:
        raise ValueError("universe membership does not match the receipt membership_sha256")
    panel_sha256 = wire.get("panel_sha256")
    exported_at = wire.get("exported_at")
    if not isinstance(panel_sha256, str) or not isinstance(exported_at, str):
        raise ValueError("daily receipt panel_sha256 and exported_at are required")
    canonical_request = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    return ValidationReceipt(
        receipt_sha256=receipt_sha256,
        request_sha256=hashlib.sha256(canonical_request).hexdigest(),
        panel_sha256=panel_sha256,
        instruments=membership_universe(membership_bytes),
        acceptance_class="wire",
        contract_version=DAILY_RECEIPT_CONTRACT,
        request_start=str(request["start"]),
        request_end=str(request["end"]),
        timeframe="1d",
        exported_at=exported_at,
    )


def panel_sessions_from_receipt_bytes(
    receipt_bytes: bytes, *, start: str | None = None, end: str | None = None
) -> tuple[str, ...]:
    """Parse receipt bytes (wire JSON) and return its ordered panel sessions.

    ``start``/``end`` (the evaluator spec range) clip and strictly check a daily receipt;
    minute receipts are unchanged.
    """
    wire = strict_object(json.loads(receipt_bytes.decode("utf-8")), "receipt")
    if is_daily_receipt_wire(wire):
        return decode_daily_sessions(wire, start=start, end=end)
    receipt = ValidationReceipt.from_wire(
        wire, acceptance_class="wire", receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest()
    )
    return decode_panel_sessions(wire, receipt)
