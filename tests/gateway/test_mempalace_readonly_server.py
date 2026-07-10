from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypedDict, cast

import pytest
from gateway.mempalace_readonly_server import (
    EXPECTED_TOOL_NAMES,
    PROHIBITED_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    ReadOnlyRegistryError,
    build_readonly_tools,
    configure_readonly_server,
)


class _ToolEntry(TypedDict):
    description: str
    input_schema: dict[str, object]
    handler: Callable[[], dict[str, str]]


class _FakeMcpServer:
    TOOLS: dict[str, object]

    def __init__(self) -> None:
        self.TOOLS = {
            tool_name: {
                "description": tool_name,
                "input_schema": {"type": "object", "properties": {}},
                "handler": (lambda name=tool_name: {"tool": name}),
            }
            for tool_name in EXPECTED_TOOL_NAMES
        }

    def handle_request(self, request: dict[str, object]) -> dict[str, object]:
        raw_params = request.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if request.get("method") == "tools/list":
            return {
                "result": {
                    "tools": [{"name": name} for name in self.TOOLS],
                }
            }
        if request.get("method") == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or tool_name not in self.TOOLS:
                return {"error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}
            handler = cast(_ToolEntry, self.TOOLS[tool_name])["handler"]
            result = handler()
            return {
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                }
            }
        raise AssertionError(f"unexpected method: {request.get('method')}")

    def main(self) -> None:
        raise AssertionError("main should not be called in this test")


def test_build_readonly_tools_returns_exact_expected_read_tools() -> None:
    tools = {tool_name: object() for tool_name in EXPECTED_TOOL_NAMES}

    filtered = build_readonly_tools(tools)

    assert tuple(filtered) == READ_ONLY_TOOL_NAMES
    assert set(filtered) == set(READ_ONLY_TOOL_NAMES)
    assert set(filtered).isdisjoint(PROHIBITED_TOOL_NAMES)


def test_build_readonly_tools_fails_closed_on_registry_drift() -> None:
    tools = {tool_name: object() for tool_name in EXPECTED_TOOL_NAMES}
    tools["mempalace_new_tool"] = object()

    with pytest.raises(ReadOnlyRegistryError, match="registry drifted"):
        build_readonly_tools(tools)


def test_configure_readonly_server_blocks_removed_tool_dispatch() -> None:
    module = _FakeMcpServer()

    configure_readonly_server(module)

    listed_response = module.handle_request({"method": "tools/list"})
    listed_result = cast(dict[str, object], listed_response["result"])
    listed_tools = cast(list[dict[str, str]], listed_result["tools"])
    assert {tool["name"] for tool in listed_tools} == set(READ_ONLY_TOOL_NAMES)

    refusal = module.handle_request(
        {"method": "tools/call", "params": {"name": "mempalace_diary_write"}}
    )
    error = cast(dict[str, object], refusal["error"])
    assert error["code"] == -32601
    assert error["message"] == "Unknown tool: mempalace_diary_write"
