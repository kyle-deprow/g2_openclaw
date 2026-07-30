from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
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


def _write_journal(palace_path: Path, experiment_id: str, payload: dict[str, object]) -> None:
    journal_dir = palace_path / ".g2-openclaw-finalizations"
    journal_dir.mkdir(parents=True)
    (journal_dir / f"{experiment_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
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

    def _acquire_mcp_writer_lock(self) -> tuple[bool, str]:
        return True, "writer lease was acquired"


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


def test_configure_readonly_server_never_acquires_the_mempalace_writer_lease() -> None:
    # Arrange
    module = _FakeMcpServer()

    # Act
    configure_readonly_server(module)
    acquired, reason = module._acquire_mcp_writer_lock()

    # Assert
    assert acquired is False
    assert "read-only" in reason


def test_readonly_handlers_fail_closed_while_a_finalization_journal_is_pending(
    tmp_path: Path,
) -> None:
    module = _FakeMcpServer()
    _write_journal(tmp_path, "iteration-7", {"status": "pending", "request_sha256": "a" * 64})

    configure_readonly_server(module, palace_path=tmp_path)

    for tool_name in READ_ONLY_TOOL_NAMES:
        response = module.handle_request({"method": "tools/call", "params": {"name": tool_name}})
        result = cast(dict[str, object], response["result"])
        content = cast(list[dict[str, str]], result["content"])
        payload = json.loads(content[0]["text"])
        assert "error" in payload, tool_name
        assert "withheld" in payload["error"]


def test_readonly_handlers_fail_closed_on_invalid_finalization_journal(tmp_path: Path) -> None:
    module = _FakeMcpServer()
    journal_dir = tmp_path / ".g2-openclaw-finalizations"
    journal_dir.mkdir()
    (journal_dir / "iteration-7.json").write_text("{", encoding="utf-8")

    configure_readonly_server(module, palace_path=tmp_path)

    response = module.handle_request(
        {"method": "tools/call", "params": {"name": "mempalace_search"}}
    )
    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, str]], result["content"])
    payload = json.loads(content[0]["text"])
    assert "error" in payload
    assert "invalid JSON" in payload["error"]


def test_readonly_handlers_allow_committed_finalization_journals(tmp_path: Path) -> None:
    module = _FakeMcpServer()
    _write_journal(
        tmp_path,
        "iteration-7",
        {"status": "committed", "request_sha256": "a" * 64, "drawer_id": "drawer-7"},
    )

    configure_readonly_server(module, palace_path=tmp_path)

    response = module.handle_request(
        {"method": "tools/call", "params": {"name": "mempalace_search"}}
    )
    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, str]], result["content"])
    assert json.loads(content[0]["text"]) == {"tool": "mempalace_search"}


def test_readonly_handlers_fail_closed_on_mode_000_journal_directory(tmp_path: Path) -> None:
    module = _FakeMcpServer()
    journal_dir = tmp_path / ".g2-openclaw-finalizations"
    journal_dir.mkdir()
    journal_dir.chmod(0)
    try:
        configure_readonly_server(module, palace_path=tmp_path)

        response = module.handle_request(
            {"method": "tools/call", "params": {"name": "mempalace_search"}}
        )
    finally:
        journal_dir.chmod(stat.S_IRWXU)

    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, str]], result["content"])
    payload = json.loads(content[0]["text"])
    assert "error" in payload
    assert "journal directory is unreadable" in payload["error"]


def test_readonly_handlers_fail_closed_on_unreadable_journal_file(tmp_path: Path) -> None:
    module = _FakeMcpServer()
    _write_journal(
        tmp_path,
        "iteration-7",
        {"status": "committed", "request_sha256": "a" * 64, "drawer_id": "drawer-7"},
    )
    journal_path = tmp_path / ".g2-openclaw-finalizations" / "iteration-7.json"
    journal_path.chmod(0)
    try:
        configure_readonly_server(module, palace_path=tmp_path)

        response = module.handle_request(
            {"method": "tools/call", "params": {"name": "mempalace_search"}}
        )
    finally:
        journal_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, str]], result["content"])
    payload = json.loads(content[0]["text"])
    assert "error" in payload
    assert "journal is unreadable" in payload["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "committed", "request_sha256": "a" * 64},
        {
            "status": "committed",
            "request_sha256": "a" * 64,
            "drawer_id": "drawer-7",
            "extra": True,
        },
        {"status": "committed", "request_sha256": "ABC", "drawer_id": "drawer-7"},
        {"status": "pending", "request_sha256": "a" * 64, "drawer_id": "drawer-7"},
    ],
)
def test_readonly_handlers_fail_closed_on_bad_finalization_journal_schema(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    module = _FakeMcpServer()
    _write_journal(tmp_path, "iteration-7", payload)

    configure_readonly_server(module, palace_path=tmp_path)

    response = module.handle_request(
        {"method": "tools/call", "params": {"name": "mempalace_search"}}
    )
    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, str]], result["content"])
    blocked = json.loads(content[0]["text"])
    assert "error" in blocked
    assert "finalization journal" in blocked["error"]


def test_copied_readonly_wrapper_is_self_contained_from_non_repo_cwd(tmp_path: Path) -> None:
    mempalace_python = Path.home() / ".local/share/mempalace/venv/bin/python"
    if not mempalace_python.exists():
        pytest.skip("MemPalace venv is not installed")
    probe = subprocess.run(
        [str(mempalace_python), "-c", "import mempalace.mcp_server"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip("MemPalace MCP server module is unavailable")
    wrapper = tmp_path / "mempalace-readonly-server.py"
    shutil.copyfile(Path(__file__).parents[2] / "gateway/mempalace_readonly_server.py", wrapper)
    palace = tmp_path / "palace"
    palace.mkdir()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }

    result = subprocess.run(
        [str(mempalace_python), str(wrapper), "--palace", str(palace)],
        input=json.dumps(request) + "\n",
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed"),
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1"),
        },
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout.splitlines()[0])
    tools = response["result"]["tools"]
    assert len(tools) == 19
    assert {tool["name"] for tool in tools} == set(READ_ONLY_TOOL_NAMES)
    assert "No module named 'gateway'" not in result.stderr
