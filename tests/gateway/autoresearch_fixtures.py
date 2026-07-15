from __future__ import annotations

import json
from pathlib import Path


def xnys_calendar_payload() -> dict[str, object]:
    return {
        "admission_status": "READY",
        "authority": {
            "hours_calendar_url": "https://www.nyse.com/trade/hours-calendars",
            "name": "NYSE Group / Intercontinental Exchange",
        },
        "closed_dates": ["2021-07-05", "2021-12-24"],
        "declared_range": {
            "end": "2026-12-31",
            "start": "2021-01-01",
            "timezone": "America/New_York",
        },
        "evidence_type": "xnys_trading_calendar",
        "limitations": ["Test fixture bound to these canonical bytes."],
        "retrieved_at": "2026-07-15T12:00:00+00:00",
        "scheduled_half_days": ["2021-11-26"],
        "schema_version": 1,
        "session_definition": {
            "regular_close": "16:00",
            "regular_open": "09:30",
            "scheduled_early_close": "13:00",
            "unit": "ET",
        },
        "source_files": [
            {
                "canonical_url": "https://www.nyse.com/trade/hours-calendars",
                "path": "/operator/xnys-calendar.pdf",
                "sha256": "a" * 64,
                "year": "2021-2026",
            }
        ],
    }


def write_xnys_calendar_evidence(path: Path) -> None:
    path.write_text(
        json.dumps(xnys_calendar_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
