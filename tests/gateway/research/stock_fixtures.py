"""Builders for the stock-campaign inputs: daily receipt, membership and earnings snapshot.

The shapes follow the quantipy ``feat/research-evaluator-daily`` contracts
(``research-price-panel-daily-v1``, ``pit-universe-membership-v1``, ``earnings-snapshot-v1``);
the gateway only binds and routes these files, so the contents here are minimal but
structurally faithful.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, timedelta

from tests.gateway.research.test_admission import _payload, _set_events

EVAL_START = "2024-01-02"
EVAL_END = "2024-03-28"
PANEL_START = "2024-01-02"
PANEL_END = "2024-04-30"


# NYSE full-day holidays inside the fixture panel (2024-01-02..2024-04-30).
NYSE_HOLIDAYS = frozenset({"2024-01-15", "2024-02-19", "2024-03-29"})


def _sessions(start: date, end: date) -> tuple[str, ...]:
    """XNYS sessions: weekdays minus the exchange holidays above."""
    days = (start + timedelta(days=offset) for offset in range((end - start).days + 1))
    return tuple(
        day.isoformat()
        for day in days
        if day.weekday() < 5 and day.isoformat() not in NYSE_HOLIDAYS
    )


# The daily panel runs past the evaluation end so held lots keep marks (tail sessions).
SESSIONS = _sessions(date.fromisoformat(PANEL_START), date.fromisoformat(PANEL_END))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def membership_bytes(
    tickers: tuple[str, ...] = ("AAA", "BBB"), references: tuple[str, ...] = ("SPY",)
) -> bytes:
    document = {
        "contract": "pit-universe-membership-v1",
        "rule": {"top_n": 1500},
        "reference_tickers": list(references),
        "members": {ticker: [[PANEL_START, PANEL_END]] for ticker in tickers},
    }
    return json.dumps(document, sort_keys=True, indent=1).encode()


def earnings_bytes() -> bytes:
    document = {
        "snapshot_version": "earnings-snapshot-v1",
        "source_label": "edgar-8k-2.02-rule-d",
        "exported_at": "2026-10-01T00:00:00Z",
        "coverage": [],
        "events": [],
    }
    return json.dumps(document, sort_keys=True).encode()


def daily_receipt_text(
    panel_sha256: str,
    membership_sha256: str | None,
    *,
    sessions: tuple[str, ...] = SESSIONS,
    start: str = PANEL_START,
    end: str = PANEL_END,
    counts: dict[str, int] | None = None,
    missing_sessions: tuple[str, ...] = (),
) -> str:
    document = {
        "contract": "research-price-panel-daily-v1",
        "request": {
            "start": start,
            "end": end,
            "adjusted": True,
            "source": "massive_grouped_daily",
            "membership_sha256": membership_sha256,
        },
        "adjustment_as_of": "2026-10-01",
        "adjustment_as_of_utc": "2026-10-01T00:00:00Z",
        "splits_manifest_sha256": "d" * 64,
        "tail_sessions": 66,
        "session_ticker_counts": counts or {session: 2 for session in sessions},
        "missing_sessions": list(missing_sessions),
        "identity_breaks": [],
        "panel_sha256": panel_sha256,
        "exported_at": "2026-10-02T00:00:00Z",
    }
    return json.dumps(document, sort_keys=True, indent=1)


def stock_payload() -> dict[str, object]:
    """A hypothesis document whose ranges are the spec's, with no training split."""
    payload = copy.deepcopy(_payload())
    payload["analysis"] = {"start": EVAL_START, "end": EVAL_END}
    payload["evaluation"] = {"start": EVAL_START, "end": EVAL_END}
    payload["training"] = None
    payload["features"][0]["lookback_sessions"] = 0  # type: ignore[index]
    _set_events(payload, 5)
    return payload


def stock_eval_spec_text(half_spread_bps: int = 1) -> str:
    """A real-parseable evaluator spec with one common stock and one reference ETF."""
    document: dict[str, object] = {
        "instruments": [
            {"ticker": "AAA", "instrument_class": "common_stock"},
            {"ticker": "SPY", "instrument_class": "etf"},
        ],
        "start_session": EVAL_START,
        "end_session": EVAL_END,
        "holding": {"max_sessions": 5},
        "costs": {"half_spread_bps": half_spread_bps},
    }
    return json.dumps(document, separators=(",", ":"))
