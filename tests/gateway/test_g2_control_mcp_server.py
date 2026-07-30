from __future__ import annotations

import json
from io import StringIO
from typing import cast

import pytest
from gateway.autoresearch_control import ControlStatus, StopResult
from gateway.g2_control_mcp_server import TOOL_NAMES, G2ControlMcpServer, run_stdio


class _FakeControl:
    def status(self) -> ControlStatus:
        return ControlStatus(
            owner_agent_id="autoresearch-pm",
            owner_session_key="agent:autoresearch-pm:autoresearch:quantipy",
            phase="repeat",
            iteration=4,
            owner_lifecycle_status=None,
            supervisor_active=True,
            tasks=(),
        )

    def start(self) -> None:
        return None

    def stop(self) -> StopResult:
        return StopResult(cancelled_task_ids=("task-1",), deleted_session=True)


class _CountingControl(_FakeControl):
    calls = 0

    def start(self) -> None:
        type(self).calls += 1
        return super().start()


class _ExplodingControl(_FakeControl):
    def status(self) -> ControlStatus:
        raise RuntimeError("boom")


def _payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    text = content[0]["text"]
    assert isinstance(text, str)
    return cast(dict[str, object], json.loads(text))


def test_g2_control_mcp_lists_only_deterministic_control_tools() -> None:
    server = G2ControlMcpServer(_FakeControl)

    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    tools = result["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == list(TOOL_NAMES)
    annotations = {tool["name"]: tool["annotations"] for tool in tools}
    assert annotations["g2_autoresearch_status"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert annotations["g2_autoresearch_start"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert annotations["g2_autoresearch_stop"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }


def test_g2_control_mcp_dispatches_status_start_and_stop() -> None:
    server = G2ControlMcpServer(_FakeControl)

    status = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "g2_autoresearch_status", "arguments": {}},
        }
    )
    start = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "g2_autoresearch_start", "arguments": {}},
        }
    )
    stop = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "g2_autoresearch_stop", "arguments": {}},
        }
    )

    assert status is not None
    assert start is not None
    assert stop is not None
    assert _payload(status)["phase"] == "repeat"
    assert _payload(start) == {"started": True}
    assert _payload(stop)["cancelled_task_ids"] == ["task-1"]


def test_g2_control_mcp_rejects_arbitrary_tool_names() -> None:
    server = G2ControlMcpServer(_FakeControl)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "exec", "arguments": {}},
        }
    )

    assert response is not None
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32602


def test_g2_control_raw_stdio_notifications_emit_no_response() -> None:
    stdout = StringIO()
    run_stdio(
        G2ControlMcpServer(_FakeControl),
        stdin=StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
            + "\n"
        ),
        stdout=stdout,
    )

    assert stdout.getvalue() == ""


def test_g2_control_valid_tool_notification_executes_silently() -> None:
    _CountingControl.calls = 0
    server = G2ControlMcpServer(_CountingControl)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "g2_autoresearch_start", "arguments": {}},
        }
    )

    assert response is None
    assert _CountingControl.calls == 1


def test_g2_control_batch_mixes_requests_notifications_and_errors() -> None:
    _CountingControl.calls = 0
    server = G2ControlMcpServer(_CountingControl)

    response = server.handle_message(
        [
            {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "g2_autoresearch_start", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": "bad", "method": "missing"},
            "not-an-object",
        ]
    )

    assert isinstance(response, list)
    assert [item["id"] for item in response] == ["list", "bad", None]
    assert "result" in response[0]
    assert cast(dict[str, object], response[1]["error"])["code"] == -32601
    assert cast(dict[str, object], response[2]["error"])["code"] == -32600
    assert _CountingControl.calls == 1


def test_g2_control_empty_batch_is_invalid_request() -> None:
    response = G2ControlMcpServer(_FakeControl).handle_message([])

    assert isinstance(response, dict)
    assert response["id"] is None
    assert cast(dict[str, object], response["error"])["code"] == -32600


def test_g2_control_unexpected_control_exception_maps_to_internal_error() -> None:
    response = G2ControlMcpServer(_ExplodingControl).handle(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "tools/call",
            "params": {"name": "g2_autoresearch_status", "arguments": {}},
        }
    )

    assert response is not None
    assert response["id"] == "status"
    assert cast(dict[str, object], response["error"])["code"] == -32603


def test_g2_control_raw_stdio_rejects_non_finite_json() -> None:
    stdout = StringIO()
    run_stdio(
        G2ControlMcpServer(_FakeControl),
        stdin=StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":NaN}}\n'),
        stdout=stdout,
    )

    response = json.loads(stdout.getvalue())
    assert response["id"] is None
    assert response["error"]["code"] == -32700


@pytest.mark.parametrize(
    "rpc_request",
    [
        {"id": 1, "method": "tools/list"},
        {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": {}, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "tools/list", "extra": True},
    ],
)
def test_g2_control_rejects_invalid_json_rpc_requests(
    rpc_request: dict[str, object],
) -> None:
    response = G2ControlMcpServer(_FakeControl).handle(rpc_request)

    assert response is not None
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32600


def test_g2_control_raw_stdio_rejects_malformed_request_shapes() -> None:
    stdout = StringIO()
    run_stdio(
        G2ControlMcpServer(_FakeControl),
        stdin=StringIO(
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": True, "method": "tools/list"}),
                    json.dumps({"jsonrpc": "1.0", "id": 2, "method": "tools/list"}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/list",
                            "extra": True,
                        }
                    ),
                ]
            )
            + "\n"
        ),
        stdout=stdout,
    )

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["error"]["code"] for response in responses] == [-32600, -32600, -32600]
    assert responses[0]["id"] is None
    assert responses[1]["id"] == 2
    assert responses[2]["id"] == 3


def test_g2_control_raw_stdio_writes_batch_response_array() -> None:
    stdout = StringIO()
    run_stdio(
        G2ControlMcpServer(_FakeControl),
        stdin=StringIO(
            json.dumps(
                [
                    {"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                ]
            )
            + "\n"
        ),
        stdout=stdout,
    )

    responses = json.loads(stdout.getvalue())
    assert isinstance(responses, list)
    assert len(responses) == 1
    assert responses[0]["id"] == "tools"


def test_g2_control_raw_stdio_outputs_only_protocol_lines() -> None:
    stdout = StringIO()
    run_stdio(
        G2ControlMcpServer(_FakeControl),
        stdin=StringIO(
            json.dumps({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}) + "\n"
        ),
        stdout=stdout,
    )

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["id"] == "tools"
    assert [tool["name"] for tool in response["result"]["tools"]] == list(TOOL_NAMES)
