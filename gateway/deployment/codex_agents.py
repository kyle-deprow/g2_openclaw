"""Managed OpenClaw Codex deployment helpers extracted from the push script."""

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

# The G2-facing main runtime has no direct research filesystem authority. Its
# control MCP server is the narrow boundary; the research owner and native
# children receive their explicit task roots below instead.
CODEX_WRITABLE_ROOTS: tuple[Path, ...] = ()
NATIVE_RESEARCH_AGENT_IDS: tuple[str, ...] = ("implementer", "experiment_runner")
NATIVE_RESEARCH_AGENT_MODELS: dict[str, str] = {
    "implementer": "gpt-5.6-luna",
    "experiment_runner": "gpt-5.6-luna",
}


class MCPServer(TypedDict, total=False):
    """Configuration fields emitted for a direct MCP server."""

    command: str
    args: list[str]
    env: dict[str, str]
    default_tools_approval_mode: str


def _validate_private_env_file(raw_path: str) -> str:
    """Validate the path-only owner env contract without reading or executing it."""

    path = Path(raw_path)
    if not path.is_absolute():
        raise SystemExit("G2_OWNER_ENV_FILE must be an absolute path")
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise SystemExit(f"G2_OWNER_ENV_FILE is not readable: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise SystemExit("G2_OWNER_ENV_FILE must be a regular non-symlink file")
    if path_stat.st_uid != os.getuid():
        raise SystemExit("G2_OWNER_ENV_FILE must be owned by the deployment user")
    if stat.S_IMODE(path_stat.st_mode) & 0o077:
        raise SystemExit("G2_OWNER_ENV_FILE must not be group/world accessible")
    return str(path)


def ensure_writable_roots(roots: Sequence[Path] | None = None) -> None:
    """Ensure every declared Codex writable root exists with private defaults."""

    if roots is None:
        roots = CODEX_WRITABLE_ROOTS
    for root in roots:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            root.chmod(0o700)


def _validate_stage_agents(
    *, strict_roster: bool = True, allow_root_templates: bool = False
) -> None:
    """Validate the managed native Codex stage-agent roster."""

    import os
    import sys
    import tomllib

    agents_dir = Path(sys.argv[1]).resolve()
    expected = NATIVE_RESEARCH_AGENT_MODELS

    if not agents_dir.is_dir():
        raise SystemExit(f"missing native Codex agents directory: {agents_dir}")
    actual = {
        path.stem for path in agents_dir.iterdir() if path.is_file() and path.name.endswith(".toml")
    }
    unexpected = sorted(actual - set(expected))
    if strict_roster and unexpected:
        raise SystemExit("unexpected native Codex research agent TOML(s): " + ", ".join(unexpected))
    missing = sorted(set(expected) - actual)
    if missing:
        raise SystemExit("missing native Codex research agent(s): " + ", ".join(missing))
    for name, model in expected.items():
        path = agents_dir / f"{name}.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"invalid native Codex stage agent TOML {path}: {exc}") from exc
        if data.get("name") != name:
            raise SystemExit(f"native Codex stage agent {path} must be named {name}")
        if data.get("model") != model:
            raise SystemExit(f"native Codex stage agent {name} must use {model}")
        if data.get("model_reasoning_effort") != "xhigh":
            raise SystemExit(f"native Codex research agent {name} must use xhigh reasoning")
        if data.get("service_tier") != "fast":
            raise SystemExit(f"native Codex research agent {name} must use service_tier=fast")
        if "config_file" in data:
            raise SystemExit(
                f"native Codex research agent {name} must not set unsupported "
                "standalone config_file"
            )
        expected_layer = f".codex/agent-configs/{name}.toml"
        if "mcp_servers" in data:
            raise SystemExit(
                f"native Codex research agent {name} must not override inherited MCP servers"
            )
        layer = agents_dir.parent.parent / expected_layer
        if not layer.is_file():
            raise SystemExit(f"native Codex research agent layer not found: {layer}")
        try:
            layer_data = tomllib.loads(layer.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"invalid native Codex research layer TOML {layer}: {exc}") from exc
        layer_agents = layer_data.get("agents")
        if not isinstance(layer_agents, dict) or layer_agents.get("max_depth") != 1:
            raise SystemExit(f"native Codex research agent {name} must set agents.max_depth=1")
        if layer_agents.get("max_threads") != 1:
            raise SystemExit(f"native Codex research agent {name} must set agents.max_threads=1")
        if layer_data.get("model") != model or layer_data.get("model_reasoning_effort") != "xhigh":
            raise SystemExit(f"native Codex research layer {layer} has wrong model settings")
        if layer_data.get("service_tier") != "fast":
            raise SystemExit(f"native Codex research layer {layer} must use service_tier=fast")
        sandbox = layer_data.get("sandbox_workspace_write")
        if not isinstance(sandbox, dict) or sandbox.get("network_access") is not True:
            raise SystemExit(f"native Codex research layer {layer} must enable network_access")
        if allow_root_templates:
            expected_roots = (
                ["@RESEARCH_V2_ROOT@"]
                if name == "experiment_runner"
                else ["@HYPOTHESIS_WORKTREES_ROOT@"]
            )
        else:
            research_root = (
                os.environ.get("CODEX_RUNTIME_RESEARCH_V2_ROOT")
                or os.environ.get("RESEARCH_V2_ROOT")
                or "@RESEARCH_V2_ROOT@"
            )
            hypothesis_root = (
                os.environ.get("CODEX_RUNTIME_HYPOTHESIS_WORKTREES_ROOT")
                or os.environ.get("HYPOTHESIS_WORKTREES_ROOT")
                or "@HYPOTHESIS_WORKTREES_ROOT@"
            )
            expected_roots = [research_root] if name == "experiment_runner" else [hypothesis_root]
        if sandbox.get("writable_roots") != expected_roots:
            raise SystemExit(
                f"native Codex research layer {layer} has an unexpected writable_roots scope"
            )


def validate_stage_agents() -> None:
    """Validate an exact managed native Codex research-agent roster."""

    _validate_stage_agents(strict_roster=True)


def validate_stage_agent_sources() -> None:
    """Validate required research role files without pruning protected roles."""

    _validate_stage_agents(strict_roster=False, allow_root_templates=True)


def write_native_research_role_config() -> None:
    """Append scoped owner role registrations with absolute layer paths.

    Codex 0.153.4 accepts ``config_file`` only in the project
    ``[agents.<name>]`` registration.  It rejects that key in standalone role
    TOMLs, so the managed workspace keeps source metadata in ``.codex/agents``
    and the scoped owner ``CODEX_HOME/config.toml`` registers the two layer
    references.
    """

    import json
    import sys
    import tomllib

    agents_dir = Path(sys.argv[1]).resolve()
    layer_dir = Path(sys.argv[2]).resolve()
    config_path = Path(sys.argv[3]).resolve()
    lines: list[str] = []
    for name in NATIVE_RESEARCH_AGENT_IDS:
        role_path = agents_dir / f"{name}.toml"
        data = tomllib.loads(role_path.read_text(encoding="utf-8"))
        description = data.get("description")
        if not isinstance(description, str) or not description:
            raise SystemExit(f"native Codex research agent {name} must have a description")
        layer_path = (layer_dir / f"{name}.toml").resolve()
        lines.extend(
            [
                f"[agents.{name}]",
                f"description = {json.dumps(description, ensure_ascii=False)}",
                f"config_file = {json.dumps(str(layer_path), ensure_ascii=False)}",
                "",
            ]
        )
    existing = config_path.read_text(encoding="utf-8")
    separator = "" if not existing.strip() else "\n"
    config_path.write_text(existing.rstrip() + separator + "\n".join(lines), encoding="utf-8")


def validate_native_research_runtime_roles() -> None:
    """Validate the single scoped-CODEX_HOME native role registration."""

    import sys
    import tomllib

    config_path = Path(sys.argv[1]).resolve()
    source_dir = Path(sys.argv[2]).resolve()
    layer_dir = Path(sys.argv[3]).resolve()
    expected_roots = {
        "implementer": [sys.argv[5]],
        "experiment_runner": [sys.argv[4]],
    }
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"invalid native Codex runtime role config {config_path}: {exc}") from exc
    agents = config.get("agents")
    if not isinstance(agents, dict):
        raise SystemExit("native Codex runtime role config must define [agents]")
    if agents.get("max_depth") != 1 or agents.get("max_threads") != 1:
        raise SystemExit("native Codex runtime role config must cap native nesting at one")
    for name, model in NATIVE_RESEARCH_AGENT_MODELS.items():
        source_path = source_dir / f"{name}.toml"
        layer_path = (layer_dir / f"{name}.toml").resolve()
        source = tomllib.loads(source_path.read_text(encoding="utf-8"))
        role = agents.get(name)
        if not isinstance(role, dict):
            raise SystemExit(f"native Codex runtime role config missing [agents.{name}]")
        if role.get("description") != source.get("description"):
            raise SystemExit(f"native Codex runtime role config has wrong {name} description")
        if role.get("config_file") != str(layer_path):
            raise SystemExit(f"native Codex runtime role {name} must reference {layer_path}")
        layer = tomllib.loads(layer_path.read_text(encoding="utf-8"))
        if layer.get("model") != model or layer.get("model_reasoning_effort") != "xhigh":
            raise SystemExit(f"native Codex runtime layer {layer_path} has wrong model settings")
        if layer.get("service_tier") != "fast":
            raise SystemExit(f"native Codex runtime layer {layer_path} must use service_tier=fast")
        if not isinstance(layer.get("developer_instructions"), str):
            raise SystemExit(
                f"native Codex runtime layer {layer_path} needs developer_instructions"
            )
        layer_agents = layer.get("agents")
        if not isinstance(layer_agents, dict) or layer_agents.get("max_depth") != 1:
            raise SystemExit(f"native Codex runtime layer {layer_path} must set agents.max_depth=1")
        if layer_agents.get("max_threads") != 1:
            raise SystemExit(
                f"native Codex runtime layer {layer_path} must set agents.max_threads=1"
            )
        sandbox = layer.get("sandbox_workspace_write")
        if not isinstance(sandbox, dict) or sandbox.get("network_access") is not True:
            raise SystemExit(f"native Codex runtime layer {layer_path} needs network_access=true")
        if sandbox.get("writable_roots") != expected_roots[name]:
            raise SystemExit(
                f"native Codex runtime layer {layer_path} has an unexpected root scope"
            )


def write_runtime_config() -> None:
    """Write the managed Codex runtime configuration from deployment environment variables."""

    import json
    from pathlib import Path

    def quoted(value: str) -> str:
        return json.dumps(value)

    def array(values: list[str]) -> str:
        return "[" + ", ".join(quoted(value) for value in values) + "]"

    agent_id = os.environ["CODEX_RUNTIME_AGENT_ID"]
    config_path = Path(os.environ["CODEX_RUNTIME_CONFIG_PATH"])
    if agent_id == "research-orchestrator":
        owner_roots = (
            Path(os.environ["CODEX_RUNTIME_RESEARCH_V2_ROOT"]),
            Path(os.environ["CODEX_RUNTIME_HYPOTHESIS_WORKTREES_ROOT"]),
        )
        ensure_writable_roots(owner_roots)
    else:
        ensure_writable_roots()
    servers: dict[str, MCPServer] = {}
    if agent_id != "research-orchestrator":
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
        servers["mempalace-readonly"] = mempalace_server
    if agent_id == "main":
        owner_env_file = _validate_private_env_file(os.environ["CODEX_RUNTIME_OWNER_ENV_FILE"])
        research_root = os.environ["CODEX_RUNTIME_RESEARCH_V2_ROOT"]
        servers["g2-control"] = {
            "command": os.environ["CODEX_RUNTIME_G2_PYTHON"],
            "args": ["-m", os.environ["CODEX_RUNTIME_G2_MODULE"]],
            "env": {
                "PYTHONPATH": os.environ["CODEX_RUNTIME_REPO_ROOT"],
                "RESEARCH_V2_ROOT": research_root,
                "RESEARCH_CORE_DATABASE": os.environ["CODEX_RUNTIME_RESEARCH_CORE_DATABASE"],
                "OPENCLAW_HOST": os.environ["CODEX_RUNTIME_OPENCLAW_HOST"],
                "OPENCLAW_PORT": os.environ["CODEX_RUNTIME_OPENCLAW_PORT"],
                "G2_OWNER_ENV_FILE": owner_env_file,
            },
            "default_tools_approval_mode": "approve",
        }

    lines = [
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        "model_auto_compact_token_limit = 100000",
        "",
        *(
            ["[agents]", "max_depth = 1", "max_threads = 1", ""]
            if agent_id == "research-orchestrator"
            else []
        ),
        "[shell_environment_policy.set]",
        'UV_CACHE_DIR = "/tmp/uv-cache-autoresearch"',
        "",
        "[sandbox_workspace_write]",
        "network_access = true",
        "writable_roots = "
        + array(
            [
                str(root)
                for root in (
                    (
                        Path(os.environ["CODEX_RUNTIME_RESEARCH_V2_ROOT"]),
                        Path(os.environ["CODEX_RUNTIME_HYPOTHESIS_WORKTREES_ROOT"]),
                    )
                    if agent_id == "research-orchestrator"
                    else CODEX_WRITABLE_ROOTS
                )
            ]
        ),
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
    owner_env_file = sys.argv[9]
    research_core_database = sys.argv[10]
    openclaw_host = sys.argv[11]
    openclaw_port = sys.argv[12]
    research_root = Path(sys.argv[13]) if len(sys.argv) > 13 and sys.argv[13] else None
    hypothesis_root = Path(sys.argv[14]) if len(sys.argv) > 14 and sys.argv[14] else None
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
    expected_roots = (
        [str(research_root), str(hypothesis_root)]
        if agent_id == "research-orchestrator"
        else [str(root) for root in CODEX_WRITABLE_ROOTS]
    )
    if workspace.get("writable_roots") != expected_roots:
        raise SystemExit("Codex runtime config has an unexpected writable_roots scope")
    if agent_id == "research-orchestrator":
        if research_root is None or hypothesis_root is None:
            raise SystemExit(
                "research-orchestrator Codex runtime validation requires both bounded roots"
            )
        if "mcp_servers" in data:
            raise SystemExit("research-orchestrator Codex runtime config must omit mcp_servers")
        agents = data.get("agents")
        if not isinstance(agents, dict) or agents.get("max_depth") != 1:
            raise SystemExit(
                "research-orchestrator Codex runtime config must set agents.max_depth=1"
            )
        if agents.get("max_threads") != 1:
            raise SystemExit(
                "research-orchestrator Codex runtime config must set agents.max_threads=1"
            )
    if "permissions" in data or "default_permissions" in data or "network_proxy" in data:
        raise SystemExit(
            "Codex runtime config must not use unsupported permissions/network_proxy profiles"
        )
    servers = data.get("mcp_servers")
    if agent_id == "research-orchestrator":
        return
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
        if research_root is None:
            raise SystemExit("main Codex runtime validation requires RESEARCH_V2_ROOT")
        expected_owner_env_file = _validate_private_env_file(owner_env_file)
        g2 = servers["g2-control"]
        if g2.get("command") != g2_python or g2.get("args") != ["-m", g2_module]:
            raise SystemExit("Codex runtime g2-control MCP command is not exact")
        if g2.get("default_tools_approval_mode") != "approve":
            raise SystemExit("Codex runtime g2-control MCP approval mode is not exact")
        if g2.get("env") != {
            "PYTHONPATH": repo_root,
            "RESEARCH_V2_ROOT": str(research_root),
            "RESEARCH_CORE_DATABASE": research_core_database,
            "OPENCLAW_HOST": openclaw_host,
            "OPENCLAW_PORT": openclaw_port,
            "G2_OWNER_ENV_FILE": expected_owner_env_file,
        }:
            raise SystemExit("Codex runtime g2-control MCP env is not exact")


def _build_parser() -> argparse.ArgumentParser:
    """Build the deployment helper command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_parser = subparsers.add_parser("validate-stage-agents")
    stage_parser.add_argument("agents_dir")

    source_parser = subparsers.add_parser("validate-stage-agent-sources")
    source_parser.add_argument("agents_dir")

    role_config_parser = subparsers.add_parser("write-native-research-role-config")
    role_config_parser.add_argument("agents_dir")
    role_config_parser.add_argument("layer_dir")
    role_config_parser.add_argument("config_path")

    role_validate_parser = subparsers.add_parser("validate-native-research-runtime-roles")
    role_validate_parser.add_argument("config_path")
    role_validate_parser.add_argument("source_dir")
    role_validate_parser.add_argument("layer_dir")
    role_validate_parser.add_argument("research_root")
    role_validate_parser.add_argument("hypothesis_root")

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
    mcp_parser.add_argument("owner_env_file")
    mcp_parser.add_argument("research_core_database")
    mcp_parser.add_argument("openclaw_host")
    mcp_parser.add_argument("openclaw_port")
    mcp_parser.add_argument("research_root", nargs="?")
    mcp_parser.add_argument("hypothesis_root", nargs="?")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one managed OpenClaw Codex deployment helper."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate-stage-agents":
        sys.argv = [sys.argv[0], args.agents_dir]
        validate_stage_agents()
        return 0
    if args.command == "validate-stage-agent-sources":
        sys.argv = [sys.argv[0], args.agents_dir]
        validate_stage_agent_sources()
        return 0
    if args.command == "write-native-research-role-config":
        sys.argv = [sys.argv[0], args.agents_dir, args.layer_dir, args.config_path]
        write_native_research_role_config()
        return 0
    if args.command == "validate-native-research-runtime-roles":
        sys.argv = [
            sys.argv[0],
            args.config_path,
            args.source_dir,
            args.layer_dir,
            args.research_root,
            args.hypothesis_root,
        ]
        validate_native_research_runtime_roles()
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
            args.owner_env_file,
            args.research_core_database,
            args.openclaw_host,
            args.openclaw_port,
        ]
        if args.research_root is not None:
            sys.argv.extend([args.research_root, args.hypothesis_root or ""])
        validate_mcp_wiring()
        return 0
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
