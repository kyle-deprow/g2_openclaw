"""Tests for managed Codex deployment helpers."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import gateway.deployment.codex_agents as codex_agents
import pytest


def test_ensure_writable_roots_creates_private_nested_directories(tmp_path: Path) -> None:
    roots = (tmp_path / "nested" / "model-workspaces", tmp_path / "stage-inbox")

    codex_agents.ensure_writable_roots(roots)

    for root in roots:
        assert root.is_dir()
        assert root.stat().st_mode & 0o777 == 0o700


def test_ensure_writable_roots_tightens_existing_directory_mode(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)

    codex_agents.ensure_writable_roots((existing,))

    assert existing.stat().st_mode & 0o777 == 0o700


def test_managed_native_research_roster_is_exact_and_uses_fast_luna_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents_dir = tmp_path / "workspace/.codex/agents"
    layer_dir = tmp_path / "workspace/.codex/agent-configs"
    layer_dir.mkdir(parents=True)
    agents_dir.mkdir()
    for agent_id in ("implementer", "experiment_runner"):
        (agents_dir / f"{agent_id}.toml").write_text(
            "\n".join(
                [
                    f'name = "{agent_id}"',
                    f'description = "{agent_id} description"',
                    f'model = "{codex_agents.NATIVE_RESEARCH_AGENT_MODELS[agent_id]}"',
                    'model_reasoning_effort = "xhigh"',
                    'service_tier = "fast"',
                ]
            ),
            encoding="utf-8",
        )
        (layer_dir / f"{agent_id}.toml").write_text(
            "\n".join(
                [
                    f'model = "{codex_agents.NATIVE_RESEARCH_AGENT_MODELS[agent_id]}"',
                    'model_reasoning_effort = "xhigh"',
                    'service_tier = "fast"',
                    "[agents]",
                    "max_depth = 1",
                    "max_threads = 1",
                    "",
                    "[sandbox_workspace_write]",
                    "network_access = true",
                    (
                        'writable_roots = ["@RESEARCH_V2_ROOT@"]'
                        if agent_id == "experiment_runner"
                        else 'writable_roots = ["@HYPOTHESIS_WORKTREES_ROOT@"]'
                    ),
                ]
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(sys, "argv", ["codex_agents", str(agents_dir)])
    codex_agents.validate_stage_agents()

    assert tomllib.loads((layer_dir / "implementer.toml").read_text())["agents"]["max_depth"] == 1


def test_managed_native_research_roster_rejects_extra_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents_dir = tmp_path / "workspace/.codex/agents"
    layer_dir = tmp_path / "workspace/.codex/agent-configs"
    agents_dir.mkdir(parents=True)
    layer_dir.mkdir()
    for agent_id in codex_agents.NATIVE_RESEARCH_AGENT_IDS:
        (agents_dir / f"{agent_id}.toml").write_text(
            "\n".join(
                [
                    f'name = "{agent_id}"',
                    'model = "gpt-5.6-luna"',
                    'model_reasoning_effort = "xhigh"',
                    'service_tier = "fast"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (layer_dir / f"{agent_id}.toml").write_text(
            "\n".join(
                [
                    'model = "gpt-5.6-luna"',
                    'model_reasoning_effort = "xhigh"',
                    'service_tier = "fast"',
                    "[agents]",
                    "max_depth = 1",
                    "max_threads = 1",
                    "",
                    "[sandbox_workspace_write]",
                    "network_access = true",
                    (
                        'writable_roots = ["@RESEARCH_V2_ROOT@"]'
                        if agent_id == "experiment_runner"
                        else 'writable_roots = ["@HYPOTHESIS_WORKTREES_ROOT@"]'
                    ),
                ]
            ),
            encoding="utf-8",
        )
    (agents_dir / "stale.toml").write_text('name = "stale"\n', encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["codex_agents", str(agents_dir)])
    with pytest.raises(SystemExit, match="stale"):
        codex_agents.validate_stage_agents()


def test_native_research_role_config_renders_absolute_layer_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents_dir = tmp_path / "owner workspace/.codex/agents"
    layer_dir = agents_dir.parent / "agent-configs"
    agents_dir.mkdir(parents=True)
    layer_dir.mkdir()
    research_root = tmp_path / "research"
    hypothesis_root = tmp_path / "hypothesis"
    for agent_id in codex_agents.NATIVE_RESEARCH_AGENT_IDS:
        (agents_dir / f"{agent_id}.toml").write_text(
            "\n".join(
                [
                    f'name = "{agent_id}"',
                    f'description = "{agent_id} description with spaces"',
                    'developer_instructions = "bounded"',
                ]
            ),
            encoding="utf-8",
        )
        (layer_dir / f"{agent_id}.toml").write_text(
            "\n".join(
                [
                    'model = "gpt-5.6-luna"',
                    'model_reasoning_effort = "xhigh"',
                    'service_tier = "fast"',
                    'developer_instructions = "bounded"',
                    "[agents]",
                    "max_depth = 1",
                    "max_threads = 1",
                    "",
                    "[sandbox_workspace_write]",
                    "network_access = true",
                    f'writable_roots = ["{hypothesis_root}"]'
                    if agent_id == "implementer"
                    else f'writable_roots = ["{research_root}"]',
                ]
            ),
            encoding="utf-8",
        )
    config_path = agents_dir.parent / "config.toml"
    config_path.write_text("[agents]\nmax_depth = 1\nmax_threads = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex_agents", str(agents_dir), str(layer_dir), str(config_path)],
    )
    codex_agents.write_native_research_role_config()

    rendered = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for agent_id in codex_agents.NATIVE_RESEARCH_AGENT_IDS:
        role = rendered["agents"][agent_id]
        assert role["config_file"] == str((layer_dir / f"{agent_id}.toml").resolve())

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_agents",
            str(config_path),
            str(agents_dir),
            str(layer_dir),
            str(research_root),
            str(hypothesis_root),
        ],
    )
    codex_agents.validate_native_research_runtime_roles()

    wrong_registration = config_path.read_text(encoding="utf-8").replace(
        str((layer_dir / "implementer.toml").resolve()),
        str((layer_dir / "wrong-implementer.toml").resolve()),
    )
    config_path.write_text(wrong_registration, encoding="utf-8")
    with pytest.raises(SystemExit, match="must reference"):
        codex_agents.validate_native_research_runtime_roles()

    config_path.write_text(
        wrong_registration.replace(
            str((layer_dir / "wrong-implementer.toml").resolve()),
            str((layer_dir / "implementer.toml").resolve()),
        ),
        encoding="utf-8",
    )
    implementer_layer = layer_dir / "implementer.toml"
    original_layer = implementer_layer.read_text(encoding="utf-8")
    implementer_layer.write_text(
        original_layer.replace(
            f'writable_roots = ["{hypothesis_root}"]',
            f'writable_roots = ["{research_root}", "{hypothesis_root}"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="unexpected root scope"):
        codex_agents.validate_native_research_runtime_roles()
    implementer_layer.write_text(original_layer, encoding="utf-8")

    runner_layer = layer_dir / "experiment_runner.toml"
    original_runner_layer = runner_layer.read_text(encoding="utf-8")
    runner_layer.write_text(
        original_runner_layer.replace(
            f'writable_roots = ["{research_root}"]',
            f'writable_roots = ["{research_root}", "{hypothesis_root}"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="unexpected root scope"):
        codex_agents.validate_native_research_runtime_roles()


def test_native_research_stage_rejects_unsupported_standalone_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents_dir = tmp_path / "workspace/.codex/agents"
    layer_dir = tmp_path / "workspace/.codex/agent-configs"
    agents_dir.mkdir(parents=True)
    layer_dir.mkdir()
    for agent_id in codex_agents.NATIVE_RESEARCH_AGENT_IDS:
        standalone = [
            f'name = "{agent_id}"',
            f'description = "{agent_id} description"',
            f'model = "{codex_agents.NATIVE_RESEARCH_AGENT_MODELS[agent_id]}"',
            'model_reasoning_effort = "xhigh"',
            'service_tier = "fast"',
        ]
        if agent_id == "implementer":
            standalone.append('config_file = ".codex/agent-configs/implementer.toml"')
        (agents_dir / f"{agent_id}.toml").write_text("\n".join(standalone), encoding="utf-8")
        layer_roots = (
            '"@HYPOTHESIS_WORKTREES_ROOT@"' if agent_id == "implementer" else '"@RESEARCH_V2_ROOT@"'
        )
        (layer_dir / f"{agent_id}.toml").write_text(
            f"""model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"
service_tier = "fast"
[agents]
max_depth = 1
max_threads = 1
[sandbox_workspace_write]
network_access = true
writable_roots = [{layer_roots}]
""",
            encoding="utf-8",
        )

    monkeypatch.setattr(sys, "argv", ["codex_agents", str(agents_dir)])
    with pytest.raises(SystemExit, match="unsupported standalone config_file"):
        codex_agents.validate_stage_agent_sources()


def test_write_runtime_config_round_trips_through_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = (tmp_path / "research-v2", tmp_path / "research-v2/hypothesis")
    config_path = tmp_path / "runtime.toml"
    monkeypatch.setattr(codex_agents, "CODEX_WRITABLE_ROOTS", roots)
    runtime_environment = {
        "CODEX_RUNTIME_AGENT_ID": "research-orchestrator",
        "CODEX_RUNTIME_CONFIG_PATH": str(config_path),
        "CODEX_RUNTIME_MEMPALACE_PYTHON": "/opt/mempalace-python",
        "CODEX_RUNTIME_MEMPALACE_WRAPPER": "/opt/mempalace-wrapper.py",
        "CODEX_RUNTIME_MEMPALACE_PALACE": "/opt/palace",
        "CODEX_RUNTIME_FASTEMBED_CACHE_PATH": "/opt/cache",
        "CODEX_RUNTIME_MEMPALACE_EMBEDDING_MODEL": "bge-base",
        "CODEX_RUNTIME_HF_HUB_OFFLINE": "1",
        "CODEX_RUNTIME_RESEARCH_V2_ROOT": str(tmp_path / "research-v2"),
        "CODEX_RUNTIME_HYPOTHESIS_WORKTREES_ROOT": str(tmp_path / "research-v2/hypothesis"),
    }
    for key, value in runtime_environment.items():
        monkeypatch.setenv(key, value)

    codex_agents.write_runtime_config()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_agents",
            str(config_path),
            "research-orchestrator",
            "/opt/mempalace-python",
            "/opt/mempalace-wrapper.py",
            "/opt/palace",
            "/opt/g2-python",
            "gateway.g2_control_mcp_server",
            str(tmp_path),
            str(tmp_path / "research-v2"),
            str(tmp_path / "research-v2/hypothesis"),
        ],
    )

    codex_agents.validate_mcp_wiring()

    assert all(root.is_dir() and root.stat().st_mode & 0o777 == 0o700 for root in roots)
    import tomllib

    rendered = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert rendered["model_auto_compact_token_limit"] == 100000
    assert "mcp_servers" not in rendered
    assert rendered["agents"] == {"max_depth": 1, "max_threads": 1}
    assert rendered["shell_environment_policy"]["set"]["UV_CACHE_DIR"] == (
        "/tmp/uv-cache-autoresearch"
    )


def test_main_runtime_config_has_no_retired_research_writable_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime.toml"
    for key, value in {
        "CODEX_RUNTIME_AGENT_ID": "main",
        "CODEX_RUNTIME_CONFIG_PATH": str(config_path),
        "CODEX_RUNTIME_MEMPALACE_PYTHON": "/opt/mempalace-python",
        "CODEX_RUNTIME_MEMPALACE_WRAPPER": "/opt/mempalace-wrapper.py",
        "CODEX_RUNTIME_MEMPALACE_PALACE": "/opt/palace",
        "CODEX_RUNTIME_FASTEMBED_CACHE_PATH": "/opt/cache",
        "CODEX_RUNTIME_MEMPALACE_EMBEDDING_MODEL": "bge-base",
        "CODEX_RUNTIME_HF_HUB_OFFLINE": "1",
        "CODEX_RUNTIME_G2_PYTHON": "/opt/g2-python",
        "CODEX_RUNTIME_G2_MODULE": "gateway.g2_control_mcp_server",
        "CODEX_RUNTIME_REPO_ROOT": str(tmp_path),
        "CODEX_RUNTIME_RESEARCH_V2_ROOT": str(tmp_path / "research-v2"),
        "CODEX_RUNTIME_HYPOTHESIS_WORKTREES_ROOT": str(tmp_path / "research-v2/hypothesis"),
    }.items():
        monkeypatch.setenv(key, value)

    codex_agents.write_runtime_config()

    rendered = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert rendered["sandbox_workspace_write"]["writable_roots"] == []
    assert rendered["mcp_servers"]["g2-control"]["env"] == {
        "PYTHONPATH": str(tmp_path),
        "RESEARCH_V2_ROOT": str(tmp_path / "research-v2"),
    }
