"""Narrow MCP control surface for the main OpenClaw agent."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Protocol, TextIO

from gateway.autoresearch_control import AutoresearchControl, ControlStatus, StopResult
from gateway.autoresearch_supervisor import SupervisorError

TOOL_NAMES = (
    "g2_autoresearch_status",
    "g2_autoresearch_start",
    "g2_autoresearch_stop",
)


class _ControlProtocol(Protocol):
    def status(self) -> ControlStatus: ...

    def start(self) -> None: ...

    def stop(self) -> StopResult: ...


def _tool_schema(name: str) -> dict[str, object]:
    descriptions = {
        "g2_autoresearch_status": "Return deterministic Quantipy autoresearch control status.",
        "g2_autoresearch_start": "Start the deterministic Quantipy autoresearch supervisor.",
        "g2_autoresearch_stop": "Stop deterministic Quantipy autoresearch work.",
    }
    annotations = {
        "g2_autoresearch_status": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "g2_autoresearch_start": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "g2_autoresearch_stop": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }
    return {
        "name": name,
        "description": descriptions[name],
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": annotations[name],
    }


def _content(payload: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]}


class G2ControlMcpServer:
    """Minimal stdio JSON-RPC MCP server with three deterministic tools."""

    def __init__(
        self,
        control_factory: Callable[[], _ControlProtocol] = AutoresearchControl,
    ) -> None:
        self._control_factory = control_factory

    def handle_message(self, message: object) -> dict[str, object] | list[dict[str, object]] | None:
        if isinstance(message, list):
            if not message:
                return self._error(None, -32600, "batch must not be empty")
            responses: list[dict[str, object]] = []
            for item in message:
                if not isinstance(item, Mapping):
                    responses.append(self._error(None, -32600, "request must be an object"))
                    continue
                response = self.handle(item)
                if response is not None:
                    responses.append(response)
            return responses or None
        if not isinstance(message, Mapping):
            return self._error(None, -32600, "request must be an object")
        return self.handle(message)

    def handle(self, request: Mapping[str, object]) -> dict[str, object] | None:
        try:
            validation = self._validate_request(request)
        except ValueError as exc:
            error_id = None
            if "id" in request and self._valid_request_id(request.get("id")):
                error_id = request.get("id")
            return self._error(error_id, -32600, str(exc))
        request_id, method, is_notification = validation
        try:
            if method == "initialize":
                result: object = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "g2-control", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                result = {"tools": [_tool_schema(name) for name in TOOL_NAMES]}
            elif method == "tools/call":
                result = self._call_tool(request.get("params"))
            else:
                if is_notification:
                    return None
                return self._error(request_id, -32601, f"Unknown method: {method}")
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except SupervisorError as exc:
            if is_notification:
                return None
            return self._error(request_id, -32000, str(exc))
        except (TypeError, ValueError) as exc:
            if is_notification:
                return None
            return self._error(request_id, -32602, str(exc))
        except Exception as exc:
            if is_notification:
                return None
            return self._error(request_id, -32603, f"Internal error: {exc}")

    def _call_tool(self, raw_params: object) -> dict[str, object]:
        if not isinstance(raw_params, Mapping):
            raise ValueError("tools/call params must be an object")
        name = raw_params.get("name")
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown G2 control tool: {name}")
        arguments = raw_params.get("arguments", {})
        if not isinstance(arguments, Mapping) or arguments:
            raise ValueError(f"{name} accepts no arguments")
        control = self._control_factory()
        if name == "g2_autoresearch_status":
            return _content(asdict(control.status()))
        if name == "g2_autoresearch_start":
            control.start()
            return _content({"started": True})
        return _content(asdict(control.stop()))

    @classmethod
    def _validate_request(cls, request: Mapping[str, object]) -> tuple[object, str, bool]:
        request_id = request.get("id")
        is_notification = "id" not in request
        if not set(request).issubset({"jsonrpc", "id", "method", "params"}):
            raise ValueError("JSON-RPC request contains unsupported fields")
        if request.get("jsonrpc") != "2.0":
            raise ValueError("JSON-RPC version must be 2.0")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise ValueError("JSON-RPC method must be a non-empty string")
        if not cls._valid_request_id(request_id):
            raise ValueError("JSON-RPC id must be a string, integer, or null")
        return (request_id, method, is_notification)

    @staticmethod
    def _valid_request_id(request_id: object) -> bool:
        return request_id is None or (
            isinstance(request_id, str)
            or (isinstance(request_id, int) and not isinstance(request_id, bool))
        )

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def run_stdio(
    server: G2ControlMcpServer | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> None:
    active_server = server or G2ControlMcpServer()
    for line in stdin:
        if not line.strip():
            continue
        response: dict[str, object] | list[dict[str, object]] | None
        try:
            request = json.loads(line, parse_constant=_reject_non_finite_json)
        except json.JSONDecodeError as exc:
            response = G2ControlMcpServer._error(None, -32700, str(exc))
        except ValueError as exc:
            response = G2ControlMcpServer._error(None, -32700, str(exc))
        else:
            if _contains_non_finite_number(request):
                response = G2ControlMcpServer._error(
                    None,
                    -32700,
                    "JSON must not contain non-finite numbers",
                )
            else:
                response = active_server.handle_message(request)
        if response is None:
            continue
        stdout.write(json.dumps(response, sort_keys=True))
        stdout.write("\n")
        stdout.flush()


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"JSON must not contain non-finite number: {value}")


def _contains_non_finite_number(value: object) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_non_finite_number(item) for item in value)
    return False


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()
