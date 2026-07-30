"""Contract tests for the shared compact research-panel receipt decoder."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib

import pytest
from gateway.autoresearch_panel_receipts import (
    COMPACT_COVERAGE_MAX_COMPRESSED_BYTES,
    COMPACT_COVERAGE_MAX_EXPANDED_BYTES,
    COMPACT_COVERAGE_MAX_RATIO,
    PanelReceiptValidationError,
    decode_compact_price_coverage,
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compact_coverage(coverage: object) -> dict[str, object]:
    expanded = _canonical_bytes(coverage)
    compressed = zlib.compress(expanded, level=zlib.Z_BEST_COMPRESSION)
    return {
        "contract_version": "price-coverage-compact-v1",
        "encoding": "canonical-json-zlib-base64-v1",
        "compressed_size": len(compressed),
        "expanded_size": len(expanded),
        "compression_ratio": len(expanded) / len(compressed),
        "coverage_sha256": hashlib.sha256(expanded).hexdigest(),
        "payload": base64.b64encode(compressed).decode("ascii"),
    }


def test_compact_coverage_decoder_returns_the_exact_canonical_evidence() -> None:
    # Arrange
    coverage = {
        "contract_version": "price-coverage-v1",
        "requested_start_date": "2022-01-03",
        "requested_end_date": "2022-01-03",
        "timeframe": "1min",
        "market_hours": "regular",
        "provider_source": "massive",
        "tickers": [
            {
                "ticker": "AAPL",
                "sessions": [
                    {
                        "session_date": "2022-01-03",
                        "coverage_state": "observed",
                        "mode_bar_count": 1,
                        "provider_request_id": "request-1",
                        "provider_http_status": 200,
                        "provider_query_count": 1,
                        "provider_results_count": 1,
                        "provider_requested_start": "2022-01-03T14:30:00Z",
                        "provider_requested_end": "2022-01-03T21:00:00Z",
                        "hydrated_at": "2022-01-04T00:00:00Z",
                    }
                ],
            }
        ],
    }

    # Act
    decoded = decode_compact_price_coverage(_compact_coverage(coverage), label="coverage")

    # Assert
    assert decoded == coverage


def test_compact_coverage_decoder_requires_quantipy_ascii_canonical_json() -> None:
    # Arrange
    coverage = {
        "contract_version": "price-coverage-v1",
        "requested_start_date": "2022-01-03",
        "requested_end_date": "2022-01-03",
        "timeframe": "1min",
        "market_hours": "regular",
        "provider_source": "massive",
        "tickers": [
            {
                "ticker": "AAPL",
                "sessions": [
                    {
                        "session_date": "2022-01-03",
                        "coverage_state": "observed",
                        "mode_bar_count": 1,
                        "provider_request_id": "café",
                        "provider_http_status": 200,
                        "provider_query_count": 1,
                        "provider_results_count": 1,
                        "provider_requested_start": "2022-01-03T14:30:00Z",
                        "provider_requested_end": "2022-01-03T21:00:00Z",
                        "hydrated_at": "2022-01-04T00:00:00Z",
                    }
                ],
            }
        ],
    }
    compact = _compact_coverage(coverage)

    # Act
    decoded = decode_compact_price_coverage(compact, label="coverage")

    # Assert
    assert decoded == coverage


def test_compact_coverage_decoder_rejects_incomplete_price_coverage_sessions() -> None:
    # Arrange
    coverage = {
        "contract_version": "price-coverage-v1",
        "requested_start_date": "2022-01-03",
        "requested_end_date": "2022-01-03",
        "timeframe": "1min",
        "market_hours": "regular",
        "provider_source": "massive",
        "tickers": [{"ticker": "AAPL", "sessions": [{"session_date": "2022-01-03"}]}],
    }

    # Act / Assert
    with pytest.raises(PanelReceiptValidationError, match=r"sessions\[0\]"):
        decode_compact_price_coverage(_compact_coverage(coverage), label="coverage")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("invalid_base64", "base64"),
        ("trailing", "trailing"),
        ("noncanonical", "canonical JSON"),
        ("digest", "digest"),
    ],
)
def test_compact_coverage_decoder_rejects_invalid_transport_evidence(
    mutation: str,
    match: str,
) -> None:
    # Arrange
    coverage = {
        "contract_version": "price-coverage-v1",
        "padding": "x" * 1024,
        "tickers": [],
    }
    compact = _compact_coverage(coverage)
    if mutation == "invalid_base64":
        compact["payload"] = "not base64!"
    elif mutation == "trailing":
        compressed = base64.b64decode(str(compact["payload"])) + b"trailing"
        compact["payload"] = base64.b64encode(compressed).decode("ascii")
        compact["compressed_size"] = len(compressed)
        compact["compression_ratio"] = len(_canonical_bytes(coverage)) / len(compressed)
    elif mutation == "noncanonical":
        expanded = (
            b'{"tickers":[], "padding":"'
            + b"x" * 1024
            + b'", "contract_version":"price-coverage-v1"}'
        )
        compressed = zlib.compress(expanded)
        compact.update(
            compressed_size=len(compressed),
            expanded_size=len(expanded),
            compression_ratio=len(expanded) / len(compressed),
            coverage_sha256=hashlib.sha256(expanded).hexdigest(),
            payload=base64.b64encode(compressed).decode("ascii"),
        )
    else:
        compact["coverage_sha256"] = "0" * 64

    # Act / Assert
    with pytest.raises(PanelReceiptValidationError, match=match):
        decode_compact_price_coverage(compact, label="coverage")


@pytest.mark.parametrize(
    ("compact", "match"),
    [
        (
            {
                "contract_version": "price-coverage-compact-v1",
                "encoding": "canonical-json-zlib-base64-v1",
                "compressed_size": COMPACT_COVERAGE_MAX_COMPRESSED_BYTES + 1,
                "expanded_size": 1,
                "compression_ratio": 1.0,
                "coverage_sha256": "0" * 64,
                "payload": "eA==",
            },
            "compressed_size",
        ),
        (
            {
                "contract_version": "price-coverage-compact-v1",
                "encoding": "canonical-json-zlib-base64-v1",
                "compressed_size": 1,
                "expanded_size": COMPACT_COVERAGE_MAX_EXPANDED_BYTES + 1,
                "compression_ratio": 1.0,
                "coverage_sha256": "0" * 64,
                "payload": "eA==",
            },
            "expanded_size",
        ),
        (
            {
                "contract_version": "price-coverage-compact-v1",
                "encoding": "canonical-json-zlib-base64-v1",
                "compressed_size": 1,
                "expanded_size": 1,
                "compression_ratio": COMPACT_COVERAGE_MAX_RATIO + 1,
                "coverage_sha256": "0" * 64,
                "payload": "eA==",
            },
            "compression_ratio",
        ),
    ],
)
def test_compact_coverage_decoder_applies_declared_limits_before_decoding(
    compact: dict[str, object],
    match: str,
) -> None:
    # Act / Assert
    with pytest.raises(PanelReceiptValidationError, match=match):
        decode_compact_price_coverage(compact, label="coverage")


def test_compact_coverage_decoder_bounds_a_zlib_bomb_without_flushing() -> None:
    # Arrange
    expanded = b"x" * 4096
    compressed = zlib.compress(expanded)
    compact = {
        "contract_version": "price-coverage-compact-v1",
        "encoding": "canonical-json-zlib-base64-v1",
        "compressed_size": len(compressed),
        "expanded_size": len(compressed),
        "compression_ratio": 1.0,
        "coverage_sha256": hashlib.sha256(expanded).hexdigest(),
        "payload": base64.b64encode(compressed).decode("ascii"),
    }

    # Act / Assert
    with pytest.raises(PanelReceiptValidationError, match="expanded_size"):
        decode_compact_price_coverage(compact, label="coverage")
