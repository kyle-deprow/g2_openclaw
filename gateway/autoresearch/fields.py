"""Autoresearch scalar fields and digest helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from gateway.autoresearch.errors import AutoresearchValidationError


def _ensure_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise AutoresearchValidationError(f"{label} must be an object")
    return raw


def _require_exact_keys(raw: Mapping[str, object], *, label: str, expected: Sequence[str]) -> None:
    expected_keys = set(expected)
    actual_keys = set(raw)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise AutoresearchValidationError(
            f"{label} must contain exact keys; missing={missing}, unexpected={unexpected}"
        )


def _require_str(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_workspace_path(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    _validate_workspace_path(value, label=field_name)
    return value


def _validate_workspace_path(value: str, *, label: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AutoresearchValidationError(f"{label} must not contain ASCII control characters")
    if not Path(value).expanduser().is_absolute():
        raise AutoresearchValidationError(f"{label} must be absolute")


def _require_bool(raw: Mapping[str, object], field_name: str) -> bool:
    value = raw.get(field_name)
    if not isinstance(value, bool):
        raise AutoresearchValidationError(f"{field_name} must be a boolean")
    return value


def _require_int(raw: Mapping[str, object], field_name: str) -> int:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoresearchValidationError(f"{field_name} must be an integer")
    return value


def _require_float(raw: Mapping[str, object], field_name: str) -> float:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{field_name} must be numeric")
    return float(value)


def _optional_int(raw: Mapping[str, object], field_name: str) -> int | None:
    if field_name not in raw:
        raise AutoresearchValidationError(f"{field_name} must be an integer or null")
    value = raw[field_name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoresearchValidationError(f"{field_name} must be an integer or null")
    return value


def _optional_float(raw: Mapping[str, object], field_name: str) -> float | None:
    if field_name not in raw:
        raise AutoresearchValidationError(f"{field_name} must be numeric or null")
    value = raw[field_name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{field_name} must be numeric or null")
    return float(value)


def _require_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    value = raw.get(field_name)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise AutoresearchValidationError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AutoresearchValidationError(f"{field_name} must be a list of strings")
        items.append(item)
    return tuple(items)


def _require_string_sequence(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise AutoresearchValidationError(f"{label} must be a list of strings")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AutoresearchValidationError(f"{label} must be a list of strings")
        items.append(item)
    return tuple(items)


def _optional_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    if field_name not in raw:
        return ()
    return _require_string_list(raw, field_name)


def _validate_sha256(value: str, *, label: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AutoresearchValidationError(f"{label} must be a lowercase SHA-256 digest")


def _require_sha256(raw: Mapping[str, object], field_name: str) -> str:
    value = _require_str(raw, field_name)
    _validate_sha256(value, label=field_name)
    return value


def _validate_iso_date_value(value: str, *, label: str) -> None:
    try:
        from datetime import date

        date.fromisoformat(value)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO-8601 date") from exc


def _parse_timestamp(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutoresearchValidationError(f"{label} must include a UTC offset")
    return parsed


def _parse_utc_request_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise AutoresearchValidationError("panel request datetime is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutoresearchValidationError("panel request datetime is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AutoresearchValidationError("panel request datetime must be UTC-aware")
    return parsed.astimezone(UTC)


def _normalise_identifier(value: str) -> str:
    """Return the sole documented identifier form: lowercase kebab-case."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", normalized):
        raise AutoresearchValidationError(
            "identifier must normalize to lowercase kebab-case beginning with a letter"
        )
    return normalized


def _require_canonical_identifier(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value:
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
        raise AutoresearchValidationError(
            f"{field_name} must be canonical lowercase kebab-case beginning with a letter"
        )
    return value


def _normalise_predicate(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _require_iso_date(raw: Mapping[str, object], field_name: str) -> str:
    value = _require_str(raw, field_name)
    _validate_iso_date_value(value, label=field_name)
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json_digest(value: object) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def canonical_member_union_digest(tickers: Sequence[str]) -> tuple[int, str]:
    """SHA-256 uppercase sorted-unique symbols joined by LF, including final LF."""
    canonical = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not canonical:
        raise AutoresearchValidationError("member union must contain at least one ticker")
    payload = canonical_member_union_manifest(canonical)
    return len(canonical), hashlib.sha256(payload).hexdigest()


def canonical_member_union_manifest(tickers: Sequence[str]) -> bytes:
    """Return canonical UTF-8 union bytes with one symbol per line and a trailing LF."""
    canonical = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not canonical:
        raise AutoresearchValidationError("member union must contain at least one ticker")
    return "".join(f"{ticker}\n" for ticker in canonical).encode("utf-8")


def quantipy_member_union_digest(tickers: Sequence[str]) -> tuple[int, str]:
    """SHA-256 over Quantipy's canonical compact JSON array member-union body."""
    canonical = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not canonical:
        raise AutoresearchValidationError("member union must contain at least one ticker")
    return len(canonical), _canonical_json_digest(canonical)


def price_hydration_request_digest(
    *,
    member_union_count: int,
    member_union_digest: str,
    experiment_start: str,
    experiment_end: str,
    timeframe: str,
    market_hours: str,
) -> str:
    return _canonical_json_digest(
        {
            "member_union_count": member_union_count,
            "member_union_digest": member_union_digest,
            "experiment_start": experiment_start,
            "experiment_end": experiment_end,
            "timeframe": timeframe,
            "market_hours": market_hours,
        }
    )


def price_hydration_coverage_digest(
    *, request_digest: str, operation_count: int, completed_at: str
) -> str:
    return _canonical_json_digest(
        {
            "request_digest": request_digest,
            "operation_count": operation_count,
            "completed_at": completed_at,
        }
    )


def platform_requested_sessions_digest(requested_sessions: Sequence[date]) -> str:
    """Mirror Quantipy's digest of the ordered XNYS session-label sequence."""
    if not requested_sessions or any(type(session) is not date for session in requested_sessions):
        raise AutoresearchValidationError(
            "requested sessions must be a non-empty sequence of plain dates"
        )
    canonical = tuple(requested_sessions)
    if canonical != tuple(sorted(set(canonical))):
        raise AutoresearchValidationError(
            "requested sessions must be unique and canonically ordered"
        )
    return _canonical_json_digest([session.isoformat() for session in canonical])


def _strict_json_keys(
    value: object,
    *,
    label: str,
    expected: Sequence[str],
) -> Mapping[str, object]:
    data = _ensure_mapping(value, label=label)
    _require_exact_keys(data, label=label, expected=expected)
    return data


def _strict_json_string(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) < minimum
        or (maximum is not None and len(value) > maximum)
    ):
        bound = f" between {minimum} and {maximum}" if maximum is not None else f" >= {minimum}"
        raise AutoresearchValidationError(f"{label} must be a string of length{bound}")
    return value


def _strict_json_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise AutoresearchValidationError(f"{label} must be a boolean")
    return value


def _strict_json_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AutoresearchValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _strict_json_float(value: object, *, label: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise AutoresearchValidationError(f"{label} must be a finite number >= {minimum}")
    return result


def _strict_json_enum(value: object, *, label: str, allowed: frozenset[str]) -> str:
    result = _strict_json_string(value, label=label)
    if result not in allowed:
        raise AutoresearchValidationError(f"{label} is not an allowed value")
    return result


def _strict_json_sha256(value: object, *, label: str) -> str:
    result = _strict_json_string(value, label=label)
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise AutoresearchValidationError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _strict_json_datetime(value: object, *, label: str, utc_only: bool = False) -> datetime:
    raw = _strict_json_string(value, label=label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutoresearchValidationError(f"{label} must be timezone-aware")
    if utc_only and parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AutoresearchValidationError(f"{label} must be UTC-aware")
    return parsed


def _strict_json_date(value: object, *, label: str) -> date:
    raw = _strict_json_string(value, label=label)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise AutoresearchValidationError(f"{label} must use canonical ISO date spelling")
    return parsed
