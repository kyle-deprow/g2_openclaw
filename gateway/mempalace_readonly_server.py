"""Read-only MemPalace MCP entrypoint for every model thread."""

from __future__ import annotations

import importlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

FINALIZATION_JOURNAL_DIRECTORY = ".g2-openclaw-finalizations"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
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

    def _acquire_mcp_writer_lock(self) -> tuple[bool, str]: ...

    def main(self) -> None: ...


def _palace_path_from_argv(argv: list[str] | None = None) -> Path:
    args = list(sys.argv[1:] if argv is None else argv)
    for index, value in enumerate(args):
        if value == "--palace" and index + 1 < len(args):
            return Path(args[index + 1]).expanduser()
    return Path.home() / ".mempalace" / "palace"


def _finalization_journal_directory(palace_path: Path) -> Path:
    return palace_path.expanduser() / FINALIZATION_JOURNAL_DIRECTORY


def _require_sha256(value: object, *, journal_path: Path) -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ReadOnlyRegistryError(
            f"MemPalace finalization journal request_sha256 is invalid: {journal_path}"
        )


def _validate_finalization_journal_schema(raw: Mapping[str, object], journal_path: Path) -> None:
    status = raw.get("status")
    if status == "committed":
        if set(raw) != {"status", "request_sha256", "drawer_id"}:
            raise ReadOnlyRegistryError(
                f"committed MemPalace finalization journal has invalid schema: {journal_path}"
            )
        _require_sha256(raw.get("request_sha256"), journal_path=journal_path)
        drawer_id = raw.get("drawer_id")
        if not isinstance(drawer_id, str) or not drawer_id.strip():
            raise ReadOnlyRegistryError(
                f"committed MemPalace finalization journal is missing drawer_id: {journal_path}"
            )
        return
    if status == "pending":
        if set(raw) != {"status", "request_sha256"}:
            raise ReadOnlyRegistryError(
                f"pending MemPalace finalization journal has invalid schema: {journal_path}"
            )
        _require_sha256(raw.get("request_sha256"), journal_path=journal_path)
        raise ReadOnlyRegistryError(f"MemPalace finalization journal is pending: {journal_path}")
    raise ReadOnlyRegistryError(
        f"MemPalace finalization journal has invalid status: {journal_path}"
    )


def _assert_no_pending_or_invalid_finalization_journals(palace_path: Path) -> None:
    journal_dir = _finalization_journal_directory(palace_path)
    try:
        metadata = journal_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReadOnlyRegistryError(
            f"MemPalace finalization journal directory is unreadable: {journal_dir}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReadOnlyRegistryError(
            f"MemPalace finalization journal directory is not a plain directory: {journal_dir}"
        )
    if metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) == 0:
        raise ReadOnlyRegistryError(
            f"MemPalace finalization journal directory is unreadable: {journal_dir}"
        )
    if metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) == 0:
        raise ReadOnlyRegistryError(
            f"MemPalace finalization journal directory cannot be listed: {journal_dir}"
        )
    try:
        with os.scandir(journal_dir) as entries:
            journal_paths = sorted(
                Path(entry.path) for entry in entries if entry.name.endswith(".json")
            )
    except OSError as exc:
        raise ReadOnlyRegistryError(
            f"MemPalace finalization journal directory is unreadable: {journal_dir}"
        ) from exc
    for journal_path in journal_paths:
        try:
            journal_metadata = journal_path.lstat()
        except OSError as exc:
            raise ReadOnlyRegistryError(
                f"MemPalace finalization journal is unreadable: {journal_path}"
            ) from exc
        if stat.S_ISLNK(journal_metadata.st_mode) or not stat.S_ISREG(journal_metadata.st_mode):
            raise ReadOnlyRegistryError(
                f"MemPalace finalization journal is not a plain file: {journal_path}"
            )
        if journal_metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) == 0:
            raise ReadOnlyRegistryError(
                f"MemPalace finalization journal is unreadable: {journal_path}"
            )
        try:
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReadOnlyRegistryError(
                f"MemPalace finalization journal is unreadable: {journal_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ReadOnlyRegistryError(
                f"MemPalace finalization journal is invalid JSON: {journal_path}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ReadOnlyRegistryError(
                f"MemPalace finalization journal must be an object: {journal_path}"
            )
        _validate_finalization_journal_schema(raw, journal_path)


def _closed_result(error: ReadOnlyRegistryError) -> dict[str, object]:
    return {
        "error": (
            "MemPalace read-only view withheld until every finalization journal is "
            f"committed: {error}"
        )
    }


def _wrap_readonly_handler(handler: object, palace_path: Path) -> object:
    if not callable(handler):
        raise ReadOnlyRegistryError("MemPalace read-only handler must be callable")
    callable_handler = cast(Callable[..., object], handler)

    @wraps(callable_handler)
    def guarded_handler(*args: object, **kwargs: object) -> object:
        try:
            _assert_no_pending_or_invalid_finalization_journals(palace_path)
        except ReadOnlyRegistryError as exc:
            return _closed_result(exc)
        return callable_handler(*args, **kwargs)

    return guarded_handler


def build_readonly_tools(
    tools: Mapping[str, object],
    *,
    palace_path: Path | None = None,
) -> dict[str, object]:
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
    if palace_path is not None:
        filtered = _guard_readonly_tools(filtered, palace_path)
    return filtered


def _guard_readonly_tools(
    tools: Mapping[str, object],
    palace_path: Path,
) -> dict[str, object]:
    guarded: dict[str, object] = {}
    for tool_name, raw_tool in tools.items():
        if not isinstance(raw_tool, dict):
            raise ReadOnlyRegistryError(f"MemPalace tool entry must be a dict: {tool_name}")
        tool = dict(raw_tool)
        tool["handler"] = _wrap_readonly_handler(tool.get("handler"), palace_path)
        guarded[tool_name] = tool
    return guarded


def configure_readonly_server(
    mcp_server: _McpServerModule,
    *,
    palace_path: Path | None = None,
) -> None:
    """Replace the upstream TOOLS registry with the enforced read-only subset."""

    raw_tools = getattr(mcp_server, "TOOLS", None)
    if not isinstance(raw_tools, dict):
        raise ReadOnlyRegistryError("mempalace.mcp_server.TOOLS must be a dict")
    mcp_server.TOOLS = build_readonly_tools(
        raw_tools,
        palace_path=_palace_path_from_argv() if palace_path is None else palace_path,
    )
    setattr(mcp_server, "_acquire_mcp_writer_lock", _refuse_writer_lease)  # noqa: B010


def _refuse_writer_lease() -> tuple[bool, str]:
    """Keep read-only MCP processes from claiming MemPalace's writer lease."""
    return False, "read-only MemPalace server never acquires the writer lease"


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
