from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from gateway.agent_profiles import load_profile
from gateway.deployment import agent_guard
from gateway.deployment.agent_guard import (
    FILE_WRITE_TOOLS,
    OPERATOR_ONLY_SUBCOMMANDS,
    OWNER_SUBCOMMANDS,
    RUNNER_SUBCOMMANDS,
    SUBAGENT_TOOLS,
    guard_config,
    guard_plugin_dir,
    main,
)

ROOTS = {
    "owner_workspace_root": Path("/ws"),
    "hypothesis_worktrees_root": Path("/ws/hyp-worktrees"),
    "research_root": Path("/ws/research"),
    "gateway_cli": Path("/repo/.venv/bin/gateway-cli"),
}
CLI = "/repo/.venv/bin/gateway-cli"


def _agents(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    agents = config["agents"]
    assert isinstance(agents, dict)
    return agents


def test_plugin_dir_has_manifest_and_package() -> None:
    plugin = guard_plugin_dir()
    manifest = json.loads((plugin / "openclaw.plugin.json").read_text())
    package = json.loads((plugin / "package.json").read_text())
    assert manifest["id"] == "g2-agent-guard"
    assert manifest["activation"]["onStartup"] is True
    assert manifest["configSchema"]["additionalProperties"] is False
    assert package["type"] == "module"
    assert package["openclaw"]["extensions"] == ["./index.js"]
    assert (plugin / "index.js").is_file()


def test_claude_config_policies() -> None:
    agents = _agents(guard_config(load_profile("claude"), **ROOTS))
    assert set(agents) == {
        "main",
        "research-orchestrator",
        "research-implementer",
        "research-runner",
        "research-reviewer",
    }
    # "*" would block OpenClaw's own MCP tools (sessions_list...), so main lists natives.
    assert "*" not in agents["main"]["denyTools"]
    assert {"bash", "read", *FILE_WRITE_TOOLS} <= set(agents["main"]["denyTools"])
    sub = list(SUBAGENT_TOOLS)
    assert agents["research-orchestrator"]["denyTools"] == sub
    assert agents["research-orchestrator"]["writeRoots"] == ["/ws", "/ws/research"]
    assert agents["research-implementer"]["denyTools"] == sub
    assert agents["research-implementer"]["writeRoots"] == ["/ws/hyp-worktrees"]
    assert agents["research-runner"]["denyTools"] == [*FILE_WRITE_TOOLS, *sub]
    assert "writeRoots" not in agents["research-runner"]
    assert agents["research-reviewer"]["denyTools"] == [*FILE_WRITE_TOOLS, "bash", *sub]
    for policy in agents.values():
        assert set(SUBAGENT_TOOLS) <= set(policy["denyTools"])
        assert {"agent", "task", "workflow", "monitor", "enter_worktree", "cron_create"} <= set(
            policy["denyTools"]
        )
        assert {"cron_list", "cron_delete", "schedule_wakeup", "send_message"} <= set(
            policy["denyTools"]
        )
        assert "enter_plan_mode" in policy["denyTools"]


def test_cli_gate_policies() -> None:
    agents = _agents(guard_config(load_profile("claude"), **ROOTS))
    owner = agents["research-orchestrator"]["cliGate"]
    assert owner == {
        "cli": CLI,
        "mode": "gated",
        "allow": list(OWNER_SUBCOMMANDS),
        "research_root": "/ws/research",
        "required_flags": {"pause": ["--owner"]},
    }
    assert not set(owner["allow"]) & set(OPERATOR_ONLY_SUBCOMMANDS)
    assert agents["research-runner"]["cliGate"] == {
        "cli": CLI,
        "mode": "cli-calls-only",
        "allow": ["run"],
        "research_root": "/ws/research",
    }
    assert RUNNER_SUBCOMMANDS == ("run",)
    assert agents["research-implementer"]["cliGate"] == {"cli": CLI, "mode": "gated", "allow": []}
    for name in ("main", "research-reviewer"):
        assert agents[name]["cliGate"] == {"cli": CLI, "mode": "cli-calls-only", "allow": []}


def test_owner_allowlist_plus_operator_only_is_every_registered_subcommand() -> None:
    source = (Path(__file__).parents[3] / "gateway" / "research" / "cli.py").read_text()
    commands = re.findall(r'@app\.command\("([a-z0-9-]+)"\)', source)
    assert len(commands) == len(set(commands)) > 20
    assert len(OWNER_SUBCOMMANDS) == len(set(OWNER_SUBCOMMANDS))
    assert not set(OWNER_SUBCOMMANDS) & set(OPERATOR_ONLY_SUBCOMMANDS)
    assert set(OWNER_SUBCOMMANDS) | set(OPERATOR_ONLY_SUBCOMMANDS) == set(commands)


def test_roots_and_cli_must_be_absolute() -> None:
    profile = load_profile("claude")
    for key in ROOTS:
        bad = {**ROOTS, key: Path("relative/x")}
        with pytest.raises(ValueError, match=key):
            guard_config(profile, **bad)


def test_cli_rejects_relative_path(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["config", "--profile", "claude", "--owner-workspace-root", "ws"]
    argv += ["--hypothesis-worktrees-root", "/b", "--research-root", "/c", "--gateway-cli", "/d"]
    assert main(argv) == 2
    assert "owner_workspace_root" in capsys.readouterr().err


def test_codex_config_is_empty() -> None:
    assert guard_config(load_profile("codex"), **ROOTS) == {"agents": {}}


def test_config_only_uses_schema_keys() -> None:
    for policy in _agents(guard_config(load_profile("claude"), **ROOTS)).values():
        assert set(policy) <= {"denyTools", "writeRoots", "cliGate"}
        for key in ("denyTools", "writeRoots"):
            assert all(isinstance(item, str) and item for item in policy.get(key, []))
        assert set(policy["cliGate"]) <= {"cli", "mode", "allow", "research_root", "required_flags"}


def test_cli_config(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "config",
        "--profile",
        "claude",
        "--owner-workspace-root",
        "/ws",
        "--hypothesis-worktrees-root",
        "/ws/hyp-worktrees",
        "--research-root",
        "/ws/research",
        "--gateway-cli",
        CLI,
    ]
    assert main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == guard_config(load_profile("claude"), **ROOTS)


def test_cli_unknown_profile(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["config", "--profile", "nope", "--owner-workspace-root", "/a"]
    argv += ["--hypothesis-worktrees-root", "/b", "--research-root", "/c", "--gateway-cli", "/d"]
    assert main(argv) == 2
    assert "unknown profile" in capsys.readouterr().err


def test_cli_plugin_dir(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plugin-dir"]) == 0
    assert capsys.readouterr().out.strip() == str(agent_guard.guard_plugin_dir())
