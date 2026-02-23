"""Tests for gateway.protocol."""

import json
from typing import Any

import pytest
from gateway.protocol import (
    ProtocolError,
    parse_text_frame,
    serialize,
    validate_outbound,
)


class TestRoundTripFrames:
    """Round-trip parse → serialize for every frame type."""

    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "start_audio", "sampleRate": 16000, "channels": 1, "sampleWidth": 2},
            {"type": "stop_audio"},
            {"type": "text", "message": "hello world"},
            {"type": "pong"},
        ],
        ids=lambda f: f["type"],
    )
    def test_inbound_round_trip(self, frame: dict[str, Any]) -> None:
        serialized = serialize(frame)
        parsed = parse_text_frame(serialized)
        assert parsed == frame

    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "status", "status": "idle"},
            {"type": "transcription", "text": "what user said"},
            {"type": "assistant", "delta": "streamed chunk"},
            {"type": "end"},
            {"type": "error", "detail": "something broke", "code": "INTERNAL_ERROR"},
            {"type": "connected", "version": "1.0"},
            {"type": "ping"},
        ],
        ids=lambda f: f["type"],
    )
    def test_outbound_round_trip(self, frame: dict[str, Any]) -> None:
        validate_outbound(frame)
        serialized = serialize(frame)
        assert json.loads(serialized) == frame


class TestParseErrors:
    """parse_text_frame rejects malformed input."""

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProtocolError, match="Invalid JSON"):
            parse_text_frame("{bad json")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ProtocolError, match="must be a JSON object"):
            parse_text_frame('"just a string"')

    def test_missing_type_field_raises(self) -> None:
        with pytest.raises(ProtocolError, match="missing 'type'"):
            parse_text_frame('{"message": "no type"}')

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ProtocolError, match="Unknown frame type"):
            parse_text_frame('{"type": "bogus"}')

    @pytest.mark.parametrize(
        ("raw", "missing_field"),
        [
            ('{"type":"start_audio"}', "sampleRate"),
            ('{"type":"text"}', "message"),
        ],
        ids=["start_audio", "text"],
    )
    def test_missing_required_inbound_field_raises(self, raw: str, missing_field: str) -> None:
        with pytest.raises(ProtocolError, match=f"missing required field '{missing_field}'"):
            parse_text_frame(raw)


class TestValidateOutbound:
    """validate_outbound rejects malformed outbound frames."""

    @pytest.mark.parametrize(
        ("frame", "missing_field"),
        [
            ({"type": "status"}, "status"),
            ({"type": "error", "detail": "x"}, "code"),
            ({"type": "connected"}, "version"),
            ({"type": "assistant"}, "delta"),
            ({"type": "transcription"}, "text"),
        ],
        ids=["status", "error", "connected", "assistant", "transcription"],
    )
    def test_missing_required_outbound_field_raises(
        self, frame: dict[str, Any], missing_field: str
    ) -> None:
        with pytest.raises(ProtocolError, match=f"missing required field '{missing_field}'"):
            validate_outbound(frame)

    def test_unknown_outbound_type_raises(self) -> None:
        with pytest.raises(ProtocolError, match="Unknown outbound frame type"):
            validate_outbound({"type": "text", "message": "nope"})

    def test_non_dict_raises(self) -> None:
        with pytest.raises(ProtocolError, match="must be a dict"):
            validate_outbound("not a dict")  # type: ignore[arg-type]


class TestInboundOnly:
    """parse_text_frame only accepts inbound frame types."""

    @pytest.mark.parametrize(
        "frame_type",
        ["status", "transcription", "assistant", "end", "error", "connected", "ping"],
    )
    def test_outbound_type_rejected_by_parse(self, frame_type: str) -> None:
        with pytest.raises(ProtocolError, match="Unknown frame type"):
            parse_text_frame(json.dumps({"type": frame_type}))


class TestFieldTypeValidation:
    """Field type checking in parse_text_frame and validate_outbound."""

    def test_inbound_int_field_rejects_string(self) -> None:
        raw = json.dumps(
            {
                "type": "start_audio",
                "sampleRate": "not_an_int",
                "channels": 1,
                "sampleWidth": 2,
            }
        )
        with pytest.raises(ProtocolError, match="must be int"):
            parse_text_frame(raw)

    def test_inbound_str_field_rejects_int(self) -> None:
        raw = json.dumps({"type": "text", "message": 42})
        with pytest.raises(ProtocolError, match="must be str"):
            parse_text_frame(raw)

    def test_outbound_str_field_rejects_int(self) -> None:
        with pytest.raises(ProtocolError, match="must be str"):
            validate_outbound({"type": "assistant", "delta": 123})

    def test_outbound_str_field_rejects_list(self) -> None:
        with pytest.raises(ProtocolError, match="must be str"):
            validate_outbound({"type": "status", "status": ["idle"]})
