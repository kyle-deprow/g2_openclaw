"""Read-only MemPalace MCP entrypoint for non-PM autoresearch agents."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from types import ModuleType
from typing import Protocol, cast

PROHIBITED_TOOL_NAMES = (
    "mempalace_add_drawer",
    "mempalace_update_drawer",
    "mempalace_delete_drawer",
    "mempalace_delete_by_source",
    "mempalace_check_duplicate",
    "mempalace_checkpoint",
    "mempalace_mine",
    "mempalace_sync",
    "mempalace_create_tunnel",
    "mempalace_delete_tunnel",
    "mempalace_delete_hallway",
    "mempalace_hook_settings",
    "mempalace_reconnect",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_diary_write",
)
READ_ONLY_TOOL_NAMES = (
    "mempalace_status",
    "mempalace_search",
    "mempalace_get_drawer",
    "mempalace_list_drawers",
    "mempalace_list_wings",
    "mempalace_list_rooms",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    "mempalace_diary_read",
    "mempalace_kg_query",
    "mempalace_kg_timeline",
    "mempalace_kg_stats",
    "mempalace_traverse",
    "mempalace_find_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_graph_stats",
    "mempalace_list_tunnels",
    "mempalace_list_hallways",
    "mempalace_memories_filed_away",
)
EXPECTED_TOOL_NAMES = frozenset((*PROHIBITED_TOOL_NAMES, *READ_ONLY_TOOL_NAMES))
WRAPPER_BASENAME = "mempalace-readonly-server.py"


class ReadOnlyRegistryError(RuntimeError):
    """Raised when the upstream MemPalace MCP registry drifts."""


class _McpServerModule(Protocol):
    TOOLS: dict[str, object]

    def main(self) -> None: ...


def build_readonly_tools(tools: Mapping[str, object]) -> dict[str, object]:
    """Return the exact read-only MemPalace tool registry or fail closed."""

    actual_names = frozenset(tools)
    missing_names = sorted(EXPECTED_TOOL_NAMES - actual_names)
    extra_names = sorted(actual_names - EXPECTED_TOOL_NAMES)
    if missing_names or extra_names:
        details: list[str] = []
        if missing_names:
            details.append(f"missing={','.join(missing_names)}")
        if extra_names:
            details.append(f"extra={','.join(extra_names)}")
        raise ReadOnlyRegistryError(
            "MemPalace MCP TOOLS registry drifted; refusing to expose a stale read-only filter "
            f"({'; '.join(details)})"
        )

    filtered = {tool_name: tools[tool_name] for tool_name in READ_ONLY_TOOL_NAMES}
    if frozenset(filtered) != frozenset(READ_ONLY_TOOL_NAMES):
        raise ReadOnlyRegistryError("Read-only MemPalace registry must contain exactly 19 tools")
    if frozenset(PROHIBITED_TOOL_NAMES) & frozenset(filtered):
        raise ReadOnlyRegistryError("Read-only MemPalace registry still exposes prohibited tools")
    return filtered


def configure_readonly_server(mcp_server: _McpServerModule) -> None:
    """Replace the upstream TOOLS registry with the enforced read-only subset."""

    raw_tools = getattr(mcp_server, "TOOLS", None)
    if not isinstance(raw_tools, dict):
        raise ReadOnlyRegistryError("mempalace.mcp_server.TOOLS must be a dict")
    mcp_server.TOOLS = build_readonly_tools(raw_tools)


def main() -> None:
    """Run the upstream MemPalace MCP server with a filtered tool registry."""

    mcp_server = cast(_McpServerModule, importlib.import_module("mempalace.mcp_server"))
    configure_readonly_server(mcp_server)
    main_fn = getattr(cast(ModuleType, mcp_server), "main", None)
    if not callable(main_fn):
        raise ReadOnlyRegistryError("mempalace.mcp_server.main must be callable")
    main_fn()


if __name__ == "__main__":
    main()
