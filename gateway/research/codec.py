"""Strict JSON and scalar validation shared by research records."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def require_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def require_enum(value: object, enum: type[StrEnum], name: str) -> StrEnum:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string enum value")
    try:
        return enum(value)
    except ValueError as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def require_utc_iso(value: object, name: str) -> str:
    text = require_str(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{name} must be ISO-8601 UTC")
    return text


def require_sha256(value: object, name: str) -> str:
    text = require_str(value, name)
    if _SHA.fullmatch(text) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def require_keys_exact(value: object, keys: set[str], name: str = "object") -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        raise ValueError(f"{name} keys must be exactly {sorted(keys)!r}")
    return dict(value)


def to_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def from_json(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON") from exc
