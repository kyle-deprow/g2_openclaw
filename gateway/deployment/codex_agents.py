"""Managed OpenClaw Codex deployment helpers extracted from the push script."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

CODEX_WRITABLE_ROOTS: tuple[Path, ...] = (
    Path("/home/dev/.openclaw/autoresearch/model-workspaces"),
    Path("/home/dev/.openclaw/autoresearch/stage-inbox"),
)


class MCPServer(TypedDict, total=False):
    """Configuration fields emitted for a direct MCP server."""

    command: str
    args: list[str]
    env: dict[str, str]
    default_tools_approval_mode: str


def ensure_writable_roots(roots: Sequence[Path] | None = None) -> None:
    """Ensure every declared Codex writable root exists with private defaults."""

    if roots is None:
        roots = CODEX_WRITABLE_ROOTS
    for root in roots:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            root.chmod(0o700)


def validate_stage_agents() -> None:
    """Validate the managed native Codex stage-agent roster."""

    import sys
    import tomllib
    from pathlib import Path

    agents_dir = Path(sys.argv[1])
    expected = {
        "context_curator": "gpt-5.4",
        "debater_microstructure": "gpt-5.5",
        "debater_data": "gpt-5.6-terra",
        "debater_skeptic": "gpt-5.5",
        "debater_theory": "gpt-5.4",
        "debater_implementation": "gpt-5.4",
        "consensus_arbiter": "gpt-5.6-sol",
        "implementer": "gpt-5.4",
        "reviewer": "gpt-5.6-sol",
        "fixer": "gpt-5.4",
    }

    if not agents_dir.is_dir():
        raise SystemExit(f"missing native Codex agents directory: {agents_dir}")
    for name, model in expected.items():
        path = agents_dir / f"{name}.toml"
        if not path.is_file():
            raise SystemExit(f"missing native Codex stage agent: {path}")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"invalid native Codex stage agent TOML {path}: {exc}") from exc
        if data.get("name") != name:
            raise SystemExit(f"native Codex stage agent {path} must be named {name}")
        if data.get("model") != model:
            raise SystemExit(f"native Codex stage agent {name} must use {model}")
        if data.get("model_reasoning_effort") != "high":
            raise SystemExit(f"native Codex stage agent {name} must use high reasoning")
        if "mcp_servers" in data:
            raise SystemExit(
                f"native Codex stage agent {name} must not override inherited MCP servers"
            )


def write_runtime_config() -> None:
    """Write the managed Codex runtime configuration from deployment environment variables."""

    import json
    import os
    from pathlib import Path

    def quoted(value: str) -> str:
        return json.dumps(value)

    def array(values: list[str]) -> str:
        return "[" + ", ".join(quoted(value) for value in values) + "]"

    agent_id = os.environ["CODEX_RUNTIME_AGENT_ID"]
    config_path = Path(os.environ["CODEX_RUNTIME_CONFIG_PATH"])
    ensure_writable_roots()
    mempalace_server: MCPServer = {
        "command": os.environ["CODEX_RUNTIME_MEMPALACE_PYTHON"],
        "args": [
            os.environ["CODEX_RUNTIME_MEMPALACE_WRAPPER"],
            "--palace",
            os.environ["CODEX_RUNTIME_MEMPALACE_PALACE"],
        ],
        "env": {
            "FASTEMBED_CACHE_PATH": os.environ["CODEX_RUNTIME_FASTEMBED_CACHE_PATH"],
            "MEMPALACE_EMBEDDING_MODEL": os.environ["CODEX_RUNTIME_MEMPALACE_EMBEDDING_MODEL"],
            "HF_HUB_OFFLINE": os.environ["CODEX_RUNTIME_HF_HUB_OFFLINE"],
        },
        "default_tools_approval_mode": "approve",
    }
    servers: dict[str, MCPServer] = {"mempalace-readonly": mempalace_server}
    if agent_id == "main":
        servers["g2-control"] = {
            "command": os.environ["CODEX_RUNTIME_G2_PYTHON"],
            "args": ["-m", os.environ["CODEX_RUNTIME_G2_MODULE"]],
            "env": {"PYTHONPATH": os.environ["CODEX_RUNTIME_REPO_ROOT"]},
            "default_tools_approval_mode": "approve",
        }

    lines = [
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        "",
        "[shell_environment_policy.set]",
        'UV_CACHE_DIR = "/tmp/uv-cache-autoresearch"',
        "",
        "[sandbox_workspace_write]",
        "network_access = true",
        "writable_roots = " + array([str(root) for root in CODEX_WRITABLE_ROOTS]),
        "exclude_tmpdir_env_var = false",
        "exclude_slash_tmp = false",
        "",
    ]
    for server_name in sorted(servers):
        server = servers[server_name]
        lines.extend(
            [
                f"[mcp_servers.{quoted(server_name)}]",
                f"command = {quoted(server['command'])}",
                f"args = {array(server['args'])}",
            ]
        )
        default_tools_approval_mode = server.get("default_tools_approval_mode")
        if default_tools_approval_mode is not None:
            lines.append(f"default_tools_approval_mode = {quoted(default_tools_approval_mode)}")
        env = server.get("env", {})
        if env:
            lines.append(f"[mcp_servers.{quoted(server_name)}.env]")
            for key in sorted(env):
                lines.append(f"{key} = {quoted(env[key])}")
        lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")


def validate_mcp_wiring() -> None:
    """Validate the direct MCP wiring in a generated Codex runtime config."""

    import sys
    import tomllib
    from pathlib import Path

    config_path = Path(sys.argv[1])
    agent_id = sys.argv[2]
    mempalace_python = sys.argv[3]
    mempalace_wrapper = sys.argv[4]
    mempalace_palace = sys.argv[5]
    g2_python = sys.argv[6]
    g2_module = sys.argv[7]
    repo_root = sys.argv[8]
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if data.get("approval_policy") != "never":
        raise SystemExit("Codex runtime config must set approval_policy=never")
    if data.get("sandbox_mode") != "workspace-write":
        raise SystemExit("Codex runtime config must set sandbox_mode=workspace-write")
    workspace = data.get("sandbox_workspace_write")
    if not isinstance(workspace, dict):
        raise SystemExit("Codex runtime config missing [sandbox_workspace_write]")
    if workspace.get("network_access") is not True:
        raise SystemExit(
            "Codex runtime config must enable network_access for localhost Quantipy HTTP"
        )
    if workspace.get("writable_roots") != [str(root) for root in CODEX_WRITABLE_ROOTS]:
        raise SystemExit(
            "Codex runtime config must scope writable_roots to model workspace and stage inbox only"
        )
    if "permissions" in data or "default_permissions" in data or "network_proxy" in data:
        raise SystemExit(
            "Codex runtime config must not use unsupported permissions/network_proxy profiles"
        )
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        raise SystemExit("Codex runtime config must define direct mcp_servers")
    expected_names = (
        {"mempalace-readonly", "g2-control"} if agent_id == "main" else {"mempalace-readonly"}
    )
    if set(servers) != expected_names:
        raise SystemExit(
            f"Codex runtime {agent_id} has wrong direct MCP server set: {sorted(servers)}"
        )
    readonly = servers["mempalace-readonly"]
    if readonly.get("command") != mempalace_python or readonly.get("args") != [
        mempalace_wrapper,
        "--palace",
        mempalace_palace,
    ]:
        raise SystemExit("Codex runtime MemPalace MCP command is not exact")
    readonly_env = readonly.get("env")
    if not isinstance(readonly_env, dict) or set(readonly_env) != {
        "FASTEMBED_CACHE_PATH",
        "HF_HUB_OFFLINE",
        "MEMPALACE_EMBEDDING_MODEL",
    }:
        raise SystemExit("Codex runtime MemPalace MCP env is not exact")
    if agent_id == "main":
        g2 = servers["g2-control"]
        if g2.get("command") != g2_python or g2.get("args") != ["-m", g2_module]:
            raise SystemExit("Codex runtime g2-control MCP command is not exact")
        if g2.get("default_tools_approval_mode") != "approve":
            raise SystemExit("Codex runtime g2-control MCP approval mode is not exact")
        if g2.get("env") != {"PYTHONPATH": repo_root}:
            raise SystemExit("Codex runtime g2-control MCP env is not exact")


def _build_parser() -> argparse.ArgumentParser:
    """Build the deployment helper command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_parser = subparsers.add_parser("validate-stage-agents")
    stage_parser.add_argument("agents_dir")

    subparsers.add_parser("write-runtime-config")

    mcp_parser = subparsers.add_parser("validate-mcp-wiring")
    mcp_parser.add_argument("config_path")
    mcp_parser.add_argument("agent_id")
    mcp_parser.add_argument("mempalace_python")
    mcp_parser.add_argument("mempalace_wrapper")
    mcp_parser.add_argument("mempalace_palace")
    mcp_parser.add_argument("g2_python")
    mcp_parser.add_argument("g2_module")
    mcp_parser.add_argument("repo_root")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one managed OpenClaw Codex deployment helper."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate-stage-agents":
        sys.argv = [sys.argv[0], args.agents_dir]
        validate_stage_agents()
        return 0
    if args.command == "write-runtime-config":
        write_runtime_config()
        return 0
    if args.command == "validate-mcp-wiring":
        sys.argv = [
            sys.argv[0],
            args.config_path,
            args.agent_id,
            args.mempalace_python,
            args.mempalace_wrapper,
            args.mempalace_palace,
            args.g2_python,
            args.g2_module,
            args.repo_root,
        ]
        validate_mcp_wiring()
        return 0
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
