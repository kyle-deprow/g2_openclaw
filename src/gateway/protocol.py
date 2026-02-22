"""WebSocket protocol frame definitions and parsing for the G2 OpenClaw Gateway."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal, TypedDict

# ---------------------------------------------------------------------------
# Status states & error codes
# ---------------------------------------------------------------------------

StatusState = Literal[
    "loading", "recording", "transcribing", "thinking", "streaming", "idle", "error"
]


class ErrorCode(StrEnum):
    AUTH_FAILED = "AUTH_FAILED"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    BUFFER_OVERFLOW = "BUFFER_OVERFLOW"
    OPENCLAW_ERROR = "OPENCLAW_ERROR"
    INVALID_FRAME = "INVALID_FRAME"
    INVALID_STATE = "INVALID_STATE"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Phone → Gateway frames
# ---------------------------------------------------------------------------


class StartAudioFrame(TypedDict):
    type: Literal["start_audio"]
    sampleRate: int
    channels: int
    sampleWidth: int


class StopAudioFrame(TypedDict):
    type: Literal["stop_audio"]


class TextFrame(TypedDict):
    type: Literal["text"]
    message: str


class PongFrame(TypedDict):
    type: Literal["pong"]


InboundFrame = StartAudioFrame | StopAudioFrame | TextFrame | PongFrame


# ---------------------------------------------------------------------------
# Gateway → Phone frames
# ---------------------------------------------------------------------------


class StatusFrame(TypedDict):
    type: Literal["status"]
    status: StatusState


class TranscriptionFrame(TypedDict):
    type: Literal["transcription"]
    text: str


class AssistantFrame(TypedDict):
    type: Literal["assistant"]
    delta: str


class EndFrame(TypedDict):
    type: Literal["end"]


class ErrorFrame(TypedDict):
    type: Literal["error"]
    detail: str
    code: str


class ConnectedFrame(TypedDict):
    type: Literal["connected"]
    version: str


class PingFrame(TypedDict):
    type: Literal["ping"]


# ---------------------------------------------------------------------------
# Required fields per frame type (excluding the 'type' key itself)
# ---------------------------------------------------------------------------

_INBOUND_FIELDS: dict[str, list[str]] = {
    "start_audio": ["sampleRate", "channels", "sampleWidth"],
    "stop_audio": [],
    "text": ["message"],
    "pong": [],
}

_OUTBOUND_FIELDS: dict[str, list[str]] = {
    "status": ["status"],
    "transcription": ["text"],
    "assistant": ["delta"],
    "end": [],
    "error": ["detail", "code"],
    "connected": ["version"],
    "ping": [],
}

_ALL_FIELDS: dict[str, list[str]] = {**_INBOUND_FIELDS, **_OUTBOUND_FIELDS}

_FIELD_TYPES: dict[str, type] = {
    "sampleRate": int,
    "channels": int,
    "sampleWidth": int,
    "message": str,
    "delta": str,
    "text": str,
    "detail": str,
    "code": str,
    "version": str,
    "status": str,
}


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """Raised when an incoming frame is malformed or invalid."""


def _check_fields(data: dict, required: list[str], frame_type: str) -> None:
    """Check required fields are present and have correct types."""
    for field in required:
        if field not in data:
            raise ProtocolError(
                f"Frame type '{frame_type}' missing required field '{field}'"
            )
    for field, value in data.items():
        if field == "type":
            continue
        expected_type = _FIELD_TYPES.get(field)
        if expected_type is not None and not isinstance(value, expected_type):
            raise ProtocolError(
                f"Field '{field}' must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )


def parse_text_frame(raw: str) -> dict:
    """Parse an inbound (phone → gateway) JSON text frame.

    Returns the parsed dict on success.
    Raises ``ProtocolError`` on invalid JSON, unknown/outbound type, or
    missing/mistyped fields.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ProtocolError("Frame must be a JSON object")

    frame_type = data.get("type")
    if frame_type is None:
        raise ProtocolError("Frame missing 'type' field")

    required = _INBOUND_FIELDS.get(frame_type)
    if required is None:
        raise ProtocolError(f"Unknown frame type: {frame_type}")

    _check_fields(data, required, frame_type)
    return data


def validate_outbound(frame: dict) -> None:
    """Validate a server-outbound (gateway → phone) frame dict.

    Raises ``ProtocolError`` on unknown type, missing fields, or wrong types.
    """
    if not isinstance(frame, dict):
        raise ProtocolError("Frame must be a dict")

    frame_type = frame.get("type")
    if frame_type is None:
        raise ProtocolError("Frame missing 'type' field")

    required = _OUTBOUND_FIELDS.get(frame_type)
    if required is None:
        raise ProtocolError(f"Unknown outbound frame type: {frame_type}")

    _check_fields(frame, required, frame_type)


def serialize(frame: dict) -> str:
    """Serialize a frame dict to a JSON string."""
    return json.dumps(frame, separators=(",", ":"))
