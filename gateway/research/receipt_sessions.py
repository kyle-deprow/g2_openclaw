"""Decode the trusted panel sessions out of a stored price-panel receipt.

Shared by the CLI admission wiring and the ``hypothesis-create`` bounds checks, so both read
the receipt's compact coverage evidence the same way and never derive calendar dates.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib
from datetime import date

from .admission import ValidationReceipt


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


def panel_sessions_from_receipt_bytes(receipt_bytes: bytes) -> tuple[str, ...]:
    """Parse receipt bytes (wire JSON) and return its ordered panel sessions."""
    wire = strict_object(json.loads(receipt_bytes.decode("utf-8")), "receipt")
    receipt = ValidationReceipt.from_wire(
        wire, acceptance_class="wire", receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest()
    )
    return decode_panel_sessions(wire, receipt)
