"""Tests for OpenClaw WebSocket client."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping

import pytest
import websockets
from gateway.device_identity import DeviceIdentity, _generate_identity
from gateway.openclaw_client import (
    OpenClawClient,
    OpenClawError,
    OpenClawTransportError,
    _parse_agent_event,
)
from websockets import ServerConnection
from websockets.asyncio.server import Server

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_identity() -> DeviceIdentity:
    """Generate a throwaway device identity for testing."""
    return _generate_identity()


def _agent_event(
    *,
    run_id: str,
    seq: object,
    stream: str,
    ts: object,
    data: Mapping[str, object],
    optional_fields: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build an OpenClaw 2026.7.1 agent event frame."""
    payload: dict[str, object] = {
        "runId": run_id,
        "seq": seq,
        "stream": stream,
        "ts": ts,
        "data": dict(data),
    }
    if optional_fields is not None:
        payload.update(optional_fields)

    return {
        "type": "event",
        "event": "agent",
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Mock OpenClaw server helpers
# ---------------------------------------------------------------------------


async def _mock_openclaw_handler(
    ws: ServerConnection,
    *,
    auth_ok: bool = True,
    deltas: list[str] | None = None,
    error_on_agent: bool = False,
    disconnect_mid_stream: bool = False,
    send_challenge: bool = True,
) -> None:
    """Simple handler that mimics OpenClaw protocol.

    When *send_challenge* is ``True`` (default), the handler sends the
    ``connect.challenge`` event immediately after the WebSocket opens,
    matching real OpenClaw server behaviour.
    """
    deltas = deltas or ["Hello ", "from ", "OpenClaw."]

    # Phase 1 — send challenge nonce
    if send_challenge:
        nonce = secrets.token_urlsafe(16)
        await ws.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": nonce},
                }
            )
        )

    async for raw in ws:
        msg = json.loads(raw)
        if msg["method"] == "connect":
            if auth_ok:
                await ws.send(
                    json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                )
            else:
                await ws.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": msg["id"],
                            "ok": False,
                            "error": "bad token",
                        }
                    )
                )
                return
        elif msg["method"] == "agent":
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg["id"],
                        "ok": True,
                        "payload": {
                            "runId": "mock-run-1",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )

            if error_on_agent:
                await ws.send(
                    json.dumps(
                        _agent_event(
                            run_id="mock-run-1",
                            seq=1,
                            stream="lifecycle",
                            ts=1,
                            data={"phase": "error", "error": "model crashed"},
                        )
                    )
                )
                return

            if disconnect_mid_stream:
                await ws.send(
                    json.dumps(
                        _agent_event(
                            run_id="mock-run-1",
                            seq=1,
                            stream="assistant",
                            ts=1,
                            data={"delta": deltas[0]},
                        )
                    )
                )
                await ws.close()
                return

            for seq, delta in enumerate(deltas, start=1):
                await ws.send(
                    json.dumps(
                        _agent_event(
                            run_id="mock-run-1",
                            seq=seq,
                            stream="assistant",
                            ts=seq,
                            data={"delta": delta},
                        )
                    )
                )
                await asyncio.sleep(0.01)

            await ws.send(
                json.dumps(
                    _agent_event(
                        run_id="mock-run-1",
                        seq=len(deltas) + 1,
                        stream="lifecycle",
                        ts=len(deltas) + 1,
                        data={"phase": "end"},
                    )
                )
            )


async def _start_mock_server(
    *,
    auth_ok: bool = True,
    deltas: list[str] | None = None,
    error_on_agent: bool = False,
    disconnect_mid_stream: bool = False,
    send_challenge: bool = True,
) -> tuple[Server, int]:
    """Start a mock OpenClaw server on an ephemeral port. Returns (server, port)."""

    async def handler(ws: ServerConnection) -> None:
        await _mock_openclaw_handler(
            ws,
            auth_ok=auth_ok,
            deltas=deltas,
            error_on_agent=error_on_agent,
            disconnect_mid_stream=disconnect_mid_stream,
            send_challenge=send_challenge,
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _make_client(port: int, token: str = "test-token") -> OpenClawClient:
    """Create a client with a test device identity (no disk I/O)."""
    return OpenClawClient("127.0.0.1", port, token, device_identity=_make_test_identity())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_one_shot_request_authenticates_and_returns_the_rpc_payload(self) -> None:
        captured: list[dict[str, object]] = []

        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                captured.append(message)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"server": {"version": "2026.7.1-2"}},
                            }
                        )
                    )
                elif message["method"] == "tasks.list":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"tasks": []},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            result = await client.request_once(
                "tasks.list",
                {"status": "running", "limit": 500},
                timeout_seconds=1.0,
                required_server_version="2026.7.1-2",
            )

            assert result == {"tasks": []}
            assert captured[-1]["method"] == "tasks.list"
            assert captured[-1]["params"] == {"status": "running", "limit": 500}
        finally:
            server.close()
            await server.wait_closed()

    async def test_one_shot_request_returns_the_source_shaped_cached_terminal_payload(
        self,
    ) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"server": {"version": "2026.7.1-2"}},
                            }
                        )
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {
                                    "runId": "cached-run",
                                    "status": "ok",
                                    "summary": "completed",
                                    "result": {
                                        "payloads": [{"text": "owner wake completed"}],
                                        "meta": {"durationMs": 42},
                                    },
                                },
                                "cached": True,
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await _make_client(port).request_once(
                "agent",
                {
                    "message": "continue",
                    "sessionKey": "agent:autoresearch-pm:autoresearch:quantipy",
                    "idempotencyKey": "idem",
                },
                timeout_seconds=1.0,
                required_server_version="2026.7.1-2",
            )
        finally:
            server.close()
            await server.wait_closed()

        assert result == {
            "runId": "cached-run",
            "status": "ok",
            "summary": "completed",
            "result": {
                "payloads": [{"text": "owner wake completed"}],
                "meta": {"durationMs": 42},
            },
        }

    async def test_one_shot_request_rejects_a_non_object_payload(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                else:
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": []})
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            with pytest.raises(OpenClawError, match="non-object payload"):
                await client.request_once("tasks.list", {"status": "running"}, timeout_seconds=1.0)
        finally:
            server.close()
            await server.wait_closed()

    async def test_one_shot_request_rejects_a_mismatched_gateway_server_version(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"server": {"version": "2026.6.10"}},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            with pytest.raises(OpenClawError, match="server version mismatch"):
                await client.request_once(
                    "tasks.list",
                    {"status": "running"},
                    timeout_seconds=1.0,
                    required_server_version="2026.7.1-2",
                )
        finally:
            server.close()
            await server.wait_closed()

    async def test_connect_auth_and_stream_deltas(self) -> None:
        server, port = await _start_mock_server(deltas=["Hello ", "world!"])
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            collected = [d async for d in stream]
            assert collected == ["Hello ", "world!"]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_multiple_sequential_messages(self) -> None:
        """Send two agent requests on the same connection; both get deltas."""
        server, port = await _start_mock_server(deltas=["A", "B"])
        try:
            client = _make_client(port)

            stream1 = await client.send_message("first")
            collected1 = [d async for d in stream1]
            assert collected1 == ["A", "B"]

            stream2 = await client.send_message("second")
            collected2 = [d async for d in stream2]
            assert collected2 == ["A", "B"]

            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_request_ids_increment(self) -> None:
        """Request IDs reset per connection (each send_message reconnects)."""
        server, port = await _start_mock_server(deltas=["x"])
        try:
            client = _make_client(port)
            # After ensure_connected, auth used id=1
            await client.ensure_connected()
            assert client._next_id == 2  # 1 consumed by auth

            stream = await client.send_message("msg1")
            _ = [d async for d in stream]
            assert client._next_id == 3  # 2 consumed by agent (ws closed)

            # Second message triggers reconnect → IDs reset
            stream2 = await client.send_message("msg2")
            _ = [d async for d in stream2]
            assert client._next_id == 3  # auth=1, agent=2 on fresh conn

            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_connect_sends_device_block_and_scopes(self) -> None:
        """The connect request includes device identity, role, and scopes."""
        captured: dict[str, object] = {}

        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    captured.update(msg["params"])
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            await client.ensure_connected()

            # Verify device block present
            assert "device" in captured
            device = captured["device"]
            assert isinstance(device, Mapping)
            assert "id" in device
            assert "publicKey" in device
            assert "signature" in device
            assert "signedAt" in device
            assert "nonce" in device

            # Verify scopes and role
            assert captured["scopes"] == ["operator.admin"]
            assert captured["role"] == "operator"
            assert captured["minProtocol"] == 4
            assert captured["maxProtocol"] == 4

            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAuthErrors:
    async def test_auth_rejected(self) -> None:
        server, port = await _start_mock_server(auth_ok=False)
        try:
            client = _make_client(port, "bad-token")
            with pytest.raises(OpenClawError, match="auth rejected") as raised:
                await client.send_message("Hi")
            assert not isinstance(raised.value, OpenClawTransportError)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestBufferedEvents:
    """Events arriving before the res frame must not be lost."""

    async def test_deltas_before_res_are_buffered(self) -> None:
        """If the server sends agent delta events BEFORE the res ack,
        the client should still yield them."""

        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    # Send deltas BEFORE the res frame
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="mock-run-1",
                                seq=1,
                                stream="assistant",
                                ts=1,
                                data={"delta": "early1 "},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="mock-run-1",
                                seq=2,
                                stream="assistant",
                                ts=2,
                                data={"delta": "early2 "},
                            )
                        )
                    )
                    # Now send the res ack
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"],
                                "ok": True,
                                "payload": {"runId": "mock-run-1"},
                            }
                        )
                    )
                    # Then more deltas after res
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="mock-run-1",
                                seq=3,
                                stream="assistant",
                                ts=3,
                                data={"delta": "late "},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="mock-run-1",
                                seq=4,
                                stream="lifecycle",
                                ts=4,
                                data={"phase": "end"},
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            collected = [d async for d in stream]
            assert collected == ["early1 ", "early2 ", "late "]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_lifecycle_end_in_buffer(self) -> None:
        """If lifecycle end arrives before the res frame, stream ends cleanly."""

        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    # Send delta + lifecycle end BEFORE res
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="mock-run-2",
                                seq=1,
                                stream="assistant",
                                ts=1,
                                data={"delta": "fast!"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="mock-run-2",
                                seq=2,
                                stream="lifecycle",
                                ts=2,
                                data={"phase": "end"},
                            )
                        )
                    )
                    # Res comes after (client already got everything)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"],
                                "ok": True,
                                "payload": {"runId": "mock-run-2"},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            collected = [d async for d in stream]
            assert collected == ["fast!"]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAgentErrors:
    async def test_agent_error_event(self) -> None:
        server, port = await _start_mock_server(error_on_agent=True)
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            with pytest.raises(OpenClawError, match="agent error"):
                async for _ in stream:
                    pass
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAgentRunCorrelation:
    """Agent events are consumed only by the accepted agent run."""

    @pytest.mark.parametrize(
        "optional_fields",
        [
            {"spawnedBy": "agent:main:parent"},
            {"isHeartbeat": True},
            {"isHeartbeat": False},
            {"sessionKey": "agent:main:g2"},
            {"sessionId": "session-123"},
            {"agentId": "main"},
        ],
    )
    async def test_accepts_documented_or_emitted_optional_envelope_field(
        self, optional_fields: Mapping[str, object]
    ) -> None:
        event = _agent_event(
            run_id="current-run",
            seq=0,
            stream="assistant",
            ts=0,
            data={"delta": "valid"},
            optional_fields=optional_fields,
        )

        parsed = _parse_agent_event(event)

        assert parsed is not None

    @pytest.mark.parametrize(
        "optional_fields",
        [
            {"spawnedBy": ""},
            {"spawnedBy": 1},
            {"isHeartbeat": "true"},
            {"isHeartbeat": 1},
            {"sessionKey": ""},
            {"sessionKey": 1},
            {"sessionId": ""},
            {"sessionId": 1},
            {"agentId": ""},
            {"agentId": 1},
        ],
    )
    async def test_rejects_invalid_optional_envelope_field_type_or_constraint(
        self, optional_fields: Mapping[str, object]
    ) -> None:
        event = _agent_event(
            run_id="current-run",
            seq=0,
            stream="assistant",
            ts=0,
            data={"delta": "valid"},
            optional_fields=optional_fields,
        )

        parsed = _parse_agent_event(event)

        assert parsed is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("runId", ""),
            ("runId", 1),
            ("stream", ""),
            ("stream", 1),
            ("data", []),
            ("data", "delta"),
        ],
    )
    async def test_rejects_invalid_required_envelope_field(self, field: str, value: object) -> None:
        payload: dict[str, object] = {
            "runId": "current-run",
            "seq": 0,
            "stream": "assistant",
            "ts": 0,
            "data": {"delta": "valid"},
        }
        payload[field] = value
        event = {"type": "event", "event": "agent", "payload": payload}

        parsed = _parse_agent_event(event)

        assert parsed is None

    @pytest.mark.parametrize("required_field", ["runId", "seq", "stream", "ts", "data"])
    async def test_rejects_missing_required_envelope_field(self, required_field: str) -> None:
        payload: dict[str, object] = {
            "runId": "current-run",
            "seq": 0,
            "stream": "assistant",
            "ts": 0,
            "data": {"delta": "valid"},
        }
        del payload[required_field]
        event = {"type": "event", "event": "agent", "payload": payload}

        parsed = _parse_agent_event(event)

        assert parsed is None

    @pytest.mark.parametrize(
        "unknown_field",
        ["unrecognized", "delta", "phase", "error"],
    )
    async def test_rejects_unknown_or_flattened_envelope_field(self, unknown_field: str) -> None:
        event = _agent_event(
            run_id="current-run",
            seq=0,
            stream="assistant",
            ts=0,
            data={"delta": "valid"},
            optional_fields={unknown_field: "value"},
        )

        parsed = _parse_agent_event(event)

        assert parsed is None

    @pytest.mark.parametrize(
        ("ts", "is_accepted"),
        [
            (0, True),
            (1, True),
            (1.0, False),
            (-1, False),
            (True, False),
            ("1", False),
            (float("inf"), False),
            (float("-inf"), False),
            (float("nan"), False),
        ],
    )
    async def test_timestamp_must_be_a_nonnegative_integer(
        self, ts: object, is_accepted: bool
    ) -> None:
        event = _agent_event(
            run_id="current-run", seq=0, stream="assistant", ts=ts, data={"delta": "valid"}
        )

        parsed = _parse_agent_event(event)

        assert (parsed is not None) is is_accepted

    @pytest.mark.parametrize(
        ("seq", "is_accepted"),
        [
            (0, True),
            (1, True),
            (1.0, False),
            (-1, False),
            (True, False),
            ("1", False),
            (float("inf"), False),
            (float("-inf"), False),
            (float("nan"), False),
        ],
    )
    async def test_sequence_must_be_a_nonnegative_integer(
        self, seq: object, is_accepted: bool
    ) -> None:
        event = _agent_event(
            run_id="current-run", seq=seq, stream="assistant", ts=0, data={"delta": "valid"}
        )

        parsed = _parse_agent_event(event)

        assert (parsed is not None) is is_accepted

    async def test_ignores_malformed_or_flattened_matching_events(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    await ws.send(json.dumps([]))
                    malformed_payloads = [
                        {
                            "runId": "current-run",
                            "stream": "assistant",
                            "ts": 1,
                            "data": {"delta": "missing-seq"},
                        },
                        {
                            "runId": "current-run",
                            "seq": -1,
                            "stream": "assistant",
                            "ts": 2,
                            "data": {"delta": "negative-seq"},
                        },
                        {
                            "runId": "current-run",
                            "seq": 2,
                            "ts": 3,
                            "data": {"delta": "missing-stream"},
                        },
                        {
                            "runId": "current-run",
                            "seq": 3,
                            "stream": "assistant",
                            "data": {"delta": "missing-ts"},
                        },
                        {
                            "runId": "current-run",
                            "seq": 4,
                            "stream": "assistant",
                            "ts": 5,
                        },
                        {
                            "runId": "current-run",
                            "seq": 5,
                            "stream": "lifecycle",
                            "ts": 6,
                            "phase": "end",
                        },
                    ]
                    for payload in malformed_payloads:
                        await ws.send(
                            json.dumps({"type": "event", "event": "agent", "payload": payload})
                        )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=6,
                                stream="assistant",
                                ts=7,
                                data={"delta": "valid"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=7,
                                stream="lifecycle",
                                ts=8,
                                data={"phase": "end"},
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            stream = await client.send_message("Hi")
            collected = [delta async for delta in stream]

            assert collected == ["valid"]
        finally:
            server.close()
            await server.wait_closed()

    async def test_foreign_terminal_events_timeout_and_close_the_socket(self) -> None:
        socket_closed = asyncio.Event()

        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="foreign-run",
                                seq=1,
                                stream="lifecycle",
                                ts=1,
                                data={"phase": "end"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="foreign-run",
                                seq=2,
                                stream="lifecycle",
                                ts=2,
                                data={"phase": "error", "error": "foreign failure"},
                            )
                        )
                    )
                    await ws.wait_closed()
                    socket_closed.set()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")

            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(stream), timeout=0.05)
            await asyncio.wait_for(socket_closed.wait(), timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

    async def test_ignores_foreign_lifecycle_before_acceptance(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="other-run",
                                seq=1,
                                stream="lifecycle",
                                ts=1,
                                data={
                                    "phase": "error",
                                    "error": "agent run aborted for restart",
                                    "aborted": True,
                                    "stopReason": "restart",
                                },
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=2,
                                stream="assistant",
                                ts=2,
                                data={"delta": "current"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=3,
                                stream="lifecycle",
                                ts=3,
                                data={"phase": "end"},
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            stream = await client.send_message("Hi")
            collected = [delta async for delta in stream]

            assert collected == ["current"]
        finally:
            server.close()
            await server.wait_closed()

    async def test_ignores_foreign_lifecycle_after_acceptance(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="other-run",
                                seq=1,
                                stream="lifecycle",
                                ts=1,
                                data={"phase": "end"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="other-run",
                                seq=2,
                                stream="lifecycle",
                                ts=2,
                                data={
                                    "phase": "error",
                                    "error": "agent run aborted for restart",
                                    "aborted": True,
                                    "stopReason": "restart",
                                },
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=3,
                                stream="assistant",
                                ts=3,
                                data={"delta": "current"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=4,
                                stream="lifecycle",
                                ts=4,
                                data={"phase": "end"},
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            stream = await client.send_message("Hi")
            collected = [delta async for delta in stream]

            assert collected == ["current"]
        finally:
            server.close()
            await server.wait_closed()

    async def test_ignores_foreign_or_missing_events_for_the_same_session(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    for seq, stream, data in (
                        (1, "assistant", {"delta": "foreign"}),
                        (2, "lifecycle", {"phase": "end"}),
                        (3, "lifecycle", {"phase": "error", "error": "foreign failure"}),
                    ):
                        await ws.send(
                            json.dumps(
                                _agent_event(
                                    run_id="other-run",
                                    seq=seq,
                                    stream=stream,
                                    ts=seq,
                                    data=data,
                                )
                            )
                        )
                    for seq, stream, data in (
                        (4, "assistant", {"delta": "missing"}),
                        (5, "lifecycle", {"phase": "end"}),
                        (6, "lifecycle", {"phase": "error", "error": "missing failure"}),
                    ):
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "event",
                                    "event": "agent",
                                    "payload": {
                                        "seq": seq,
                                        "stream": stream,
                                        "ts": seq,
                                        "data": data,
                                    },
                                }
                            )
                        )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=7,
                                stream="assistant",
                                ts=7,
                                data={"delta": "current"},
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=8,
                                stream="lifecycle",
                                ts=8,
                                data={"phase": "end"},
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            stream = await client.send_message("Hi")
            collected = [delta async for delta in stream]

            assert collected == ["current"]
        finally:
            server.close()
            await server.wait_closed()

    async def test_matching_restart_abort_ends_cleanly(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=1,
                                stream="lifecycle",
                                ts=1,
                                data={
                                    "phase": "error",
                                    "error": "agent run aborted for restart",
                                    "aborted": True,
                                    "stopReason": "restart",
                                },
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            stream = await client.send_message("Hi")
            collected = [delta async for delta in stream]

            assert collected == []
        finally:
            server.close()
            await server.wait_closed()

    async def test_matching_error_with_restart_text_but_no_abort_fields_raises(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": {"runId": "current-run"},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            _agent_event(
                                run_id="current-run",
                                seq=1,
                                stream="lifecycle",
                                ts=1,
                                data={
                                    "phase": "error",
                                    "error": "agent run aborted for restart",
                                },
                            )
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            stream = await client.send_message("Hi")
            with pytest.raises(OpenClawError, match="agent run aborted for restart"):
                async for _ in stream:
                    pass
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.parametrize("payload", [{}, {"runId": ""}, {"runId": " \t "}, {"runId": 1}])
    async def test_rejects_an_accepted_response_without_a_valid_run_id(
        self, payload: dict[str, object]
    ) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce"}}
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": message["id"], "ok": True, "payload": {}})
                    )
                elif message["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": message["id"],
                                "ok": True,
                                "payload": payload,
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)

            with pytest.raises(OpenClawError, match="runId"):
                await client.send_message("Hi")
        finally:
            server.close()
            await server.wait_closed()


class TestConnectionErrors:
    async def test_connection_refused(self) -> None:
        # Use ephemeral port to avoid conflicts
        import socket as _socket

        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
            _s.bind(("127.0.0.1", 0))
            ephemeral_port = _s.getsockname()[1]
        client = _make_client(ephemeral_port)
        with pytest.raises(OpenClawTransportError, match="connection refused"):
            await client.send_message("Hi")

    async def test_disconnect_mid_stream(self) -> None:
        server, port = await _start_mock_server(disconnect_mid_stream=True)
        try:
            client = _make_client(port)
            stream = await client.send_message("Hi")
            with pytest.raises(OpenClawError, match="disconnected"):
                collected = []
                async for d in stream:
                    collected.append(d)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_missing_challenge_nonce(self) -> None:
        """Connection fails if server sends no challenge event."""
        server, port = await _start_mock_server(send_challenge=False)
        try:
            client = _make_client(port)
            with pytest.raises(OpenClawError, match="challenge"):
                await client.send_message("Hi")
        finally:
            server.close()
            await server.wait_closed()


class TestClose:
    async def test_graceful_close(self) -> None:
        server, port = await _start_mock_server()
        try:
            client = _make_client(port)
            await client.ensure_connected()
            assert client._connected
            await client.close()
            assert not client._connected
            assert client._ws is None
        finally:
            server.close()
            await server.wait_closed()


class TestMalformedAgentAcceptance:
    """M-9: Malformed (non-JSON) agent acceptance response."""

    async def test_non_json_agent_response(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    await ws.send("<<<not json>>>")

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            with pytest.raises(OpenClawError, match="no response to agent request"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


class TestAgentAcceptanceIdMismatch:
    """M-10: Agent acceptance response has wrong request ID."""

    async def test_wrong_id_in_agent_response(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            nonce = secrets.token_urlsafe(16)
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": nonce},
                    }
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "connect":
                    await ws.send(
                        json.dumps({"type": "res", "id": msg["id"], "ok": True, "payload": {}})
                    )
                elif msg["method"] == "agent":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "res",
                                "id": msg["id"] + "_wrong",
                                "ok": True,
                                "payload": {},
                            }
                        )
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = _make_client(port)
            with pytest.raises(OpenClawError, match="unexpected agent response"):
                await client.send_message("Hi")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
