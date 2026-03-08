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
        [
            "status",
            "transcription",
            "assistant",
            "end",
            "error",
            "connected",
            "ping",
            "history",
            "session_reset",
        ],
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


class TestConnectedSessionFields:
    """Connected frame with optional session fields."""

    def test_outbound_connected_with_session_fields_validates(self) -> None:
        validate_outbound(
            {
                "type": "connected",
                "version": "1.0",
                "sessionId": "ses_abc123",
                "sessionKey": "agent:claw:g2",
                "sessionStartedAt": "2026-03-07T10:00:00Z",
            }
        )

    def test_outbound_connected_without_optional_fields_still_validates(self) -> None:
        validate_outbound({"type": "connected", "version": "1.0"})

    def test_outbound_connected_rejects_wrong_type_session_id(self) -> None:
        with pytest.raises(ProtocolError, match="must be str"):
            validate_outbound(
                {
                    "type": "connected",
                    "version": "1.0",
                    "sessionId": 123,
                }
            )


class TestHistoryFrame:
    """History frame validation."""

    def test_validate_history_frame(self) -> None:
        validate_outbound(
            {
                "type": "history",
                "entries": [
                    {"role": "user", "text": "hi", "ts": 1700000000000},
                    {"role": "assistant", "text": "hello", "ts": 1700000001000},
                ],
            }
        )

    def test_validate_history_frame_empty_entries(self) -> None:
        validate_outbound(
            {
                "type": "history",
                "entries": [],
            }
        )

    def test_history_frame_missing_entries(self) -> None:
        with pytest.raises(ProtocolError, match="missing required field 'entries'"):
            validate_outbound({"type": "history"})


class TestStatusRequestFrame:
    """status_request inbound frame validation."""

    def test_status_request_parses(self) -> None:
        result = parse_text_frame('{"type":"status_request"}')
        assert result == {"type": "status_request"}


class TestEnhancedStatusFrame:
    """Status frame with optional metadata fields."""

    def test_status_with_metadata_validates(self) -> None:
        validate_outbound(
            {
                "type": "status",
                "status": "thinking",
                "question": "hello",
                "elapsedMs": 1234,
                "phase": "Waiting for OpenClaw",
            }
        )

    def test_status_without_optional_fields_validates(self) -> None:
        validate_outbound({"type": "status", "status": "idle"})

    def test_status_rejects_wrong_type_question(self) -> None:
        with pytest.raises(ProtocolError, match="must be str"):
            validate_outbound(
                {
                    "type": "status",
                    "status": "thinking",
                    "question": 42,
                }
            )

    def test_status_rejects_wrong_type_elapsed(self) -> None:
        with pytest.raises(ProtocolError, match="must be int"):
            validate_outbound(
                {
                    "type": "status",
                    "status": "thinking",
                    "elapsedMs": "slow",
                }
            )

    def test_status_rejects_wrong_type_phase(self) -> None:
        with pytest.raises(ProtocolError, match="must be str"):
            validate_outbound(
                {
                    "type": "status",
                    "status": "thinking",
                    "phase": 123,
                }
            )


class TestResetSessionFrame:
    """reset_session inbound frame validation."""

    def test_inbound_reset_session_valid(self) -> None:
        result = parse_text_frame('{"type":"reset_session"}')
        assert result == {"type": "reset_session"}


class TestSessionResetFrame:
    """session_reset outbound frame validation."""

    def test_outbound_session_reset_valid(self) -> None:
        validate_outbound({"type": "session_reset", "reason": "daily_reset"})

    def test_outbound_session_reset_user_request(self) -> None:
        validate_outbound({"type": "session_reset", "reason": "user_request"})

    def test_outbound_session_reset_missing_reason(self) -> None:
        with pytest.raises(ProtocolError, match="missing required field 'reason'"):
            validate_outbound({"type": "session_reset"})

    def test_outbound_session_reset_wrong_type_reason(self) -> None:
        with pytest.raises(ProtocolError, match="must be str"):
            validate_outbound({"type": "session_reset", "reason": 123})


class TestHistoryFrameWrongType:
    """History frame rejects wrong types for entries."""

    def test_history_entries_wrong_type(self) -> None:
        with pytest.raises(ProtocolError, match="must be list"):
            validate_outbound({"type": "history", "entries": "not_a_list"})
