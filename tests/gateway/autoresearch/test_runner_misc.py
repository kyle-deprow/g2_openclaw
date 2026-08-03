from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import gateway.autoresearch_runner as autoresearch_runner
import pytest
from gateway.autoresearch_runner import (
    MEMPALACE_READONLY_DISPLAY_TOOL_IDS,
    MEMPALACE_READONLY_SERVER_ID,
    MEMPALACE_READONLY_TOOL_NAMES,
    PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS,
    AutoresearchConfigError,
    AutoresearchValidationError,
    ComputeCapabilitySnapshot,
    ComputeFitArtifact,
    ComputeTarget,
    VerificationResultArtifact,
    VerificationStatus,
    load_autoresearch_policy,
)

from tests.gateway.autoresearch.builders import (
    _add_codex_native_tool_surface_key,
    _add_codex_network_proxy,
    _add_forbidden_full_mempalace_server,
    _add_pm_openclaw_subagent_allowlist,
    _add_stage_openclaw_subagent_allowlist,
    _break_readonly_server_args,
    _drop_codex_app_server_sandbox,
    _drop_codex_plugin_allow,
    _drop_mempalace_readonly_server,
    _drop_pm_mempalace_skill,
    _give_main_a_pm_skill,
    _give_stage_agent_write_skill,
    _load_config,
    _raise_agent_run_concurrency,
    _raise_subagent_concurrency,
    _remove_pm_native_codex_delegation_deny,
    _set_agent_runtime_id,
    _set_codex_danger_full_access,
    _set_codex_wrong_default_workspace,
    _set_main_full_profile,
    _set_openai_api,
    _set_rejecting_subagent_child_cap,
    _set_safeguard_compaction,
    _verification_result,
)


def test_gpu_compute_fit_fails_closed_when_dependency_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = ComputeCapabilitySnapshot(
        cpu_model="test-cpu",
        logical_cpus=16,
        memory_gib=32.0,
        target_python_available=True,
        gpu_available=True,
        gpu_name="Test GPU",
        gpu_vram_gib=10.0,
        cuda_runtime_available=True,
        installed_gpu_packages=(),
        probe_errors=(),
    )
    monkeypatch.setattr(
        autoresearch_runner,
        "collect_compute_capability_snapshot",
        lambda _target_repo: snapshot,
    )
    compute_fit = ComputeFitArtifact(
        target=ComputeTarget.GPU,
        rationale="The proposed model requires GPU acceleration.",
        required_dependencies=("torch",),
        benchmark_plan="Compare GPU and CPU wall time on the full training window.",
    )

    with pytest.raises(AutoresearchValidationError, match="unavailable dependencies"):
        autoresearch_runner._validate_compute_fit_environment(compute_fit, tmp_path)


def test_gpu_compute_fit_fails_closed_without_target_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = ComputeCapabilitySnapshot(
        cpu_model="test-cpu",
        logical_cpus=16,
        memory_gib=32.0,
        target_python_available=False,
        gpu_available=True,
        gpu_name="Test GPU",
        gpu_vram_gib=10.0,
        cuda_runtime_available=True,
        installed_gpu_packages=(),
        probe_errors=(),
    )
    monkeypatch.setattr(
        autoresearch_runner,
        "collect_compute_capability_snapshot",
        lambda _target_repo: snapshot,
    )
    compute_fit = ComputeFitArtifact(
        target=ComputeTarget.GPU,
        rationale="The proposed model requires GPU acceleration.",
        required_dependencies=("cuda_runtime",),
        benchmark_plan="Compare GPU and CPU wall time on the full training window.",
    )

    with pytest.raises(AutoresearchValidationError, match="virtualenv is unavailable"):
        autoresearch_runner._validate_compute_fit_environment(compute_fit, tmp_path)


def test_compute_capability_snapshot_is_serializable() -> None:
    snapshot = ComputeCapabilitySnapshot(
        cpu_model="test-cpu",
        logical_cpus=8,
        memory_gib=16.0,
        target_python_available=False,
        gpu_available=False,
        gpu_name=None,
        gpu_vram_gib=None,
        cuda_runtime_available=False,
        installed_gpu_packages=(),
        probe_errors=("nvidia-smi is not installed",),
    )

    assert snapshot.to_dict() == {
        "cpu_model": "test-cpu",
        "logical_cpus": 8,
        "memory_gib": 16.0,
        "target_python_available": False,
        "gpu_available": False,
        "gpu_name": None,
        "gpu_vram_gib": None,
        "cuda_runtime_available": False,
        "installed_gpu_packages": [],
        "probe_errors": ["nvidia-smi is not installed"],
    }


def test_verification_schema_rejects_missing_execution_not_started_field() -> None:
    raw = _verification_result(VerificationStatus.TEST_FAILURE).to_dict()
    del raw["quantipy_execution_not_started"]

    with pytest.raises(
        AutoresearchValidationError,
        match="verification_result must contain exact keys",
    ):
        VerificationResultArtifact.from_dict(raw)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (_drop_codex_plugin_allow, "plugins.allow must explicitly include codex"),
        (_set_openai_api, "providers.openai.api must be openai-responses"),
        (_set_agent_runtime_id, "providers.openai.agentRuntime.id must be codex"),
        (_set_codex_danger_full_access, "Codex app-server sandbox must be workspace-write"),
        (_drop_codex_app_server_sandbox, "Codex app-server sandbox must be workspace-write"),
        (
            _set_codex_wrong_default_workspace,
            "Codex app-server defaultWorkspaceDir must be "
            "/home/dev/.openclaw/autoresearch/model-workspaces",
        ),
        (
            _add_codex_network_proxy,
            "Codex app-server networkProxy must not be configured",
        ),
        (
            _add_codex_native_tool_surface_key,
            "nativeToolSurfaceEnabled is not supported by the current Codex plugin schema",
        ),
        (
            _set_safeguard_compaction,
            "agents.defaults.compaction.mode must be default for the Codex OAuth route",
        ),
        (
            _raise_agent_run_concurrency,
            "agents.defaults.maxConcurrent must be 2 to cap the main lane with PM headroom",
        ),
        (
            _raise_subagent_concurrency,
            "agents.defaults.subagents.maxConcurrent must be 1 to serialize heavy Codex stages",
        ),
        (
            _set_rejecting_subagent_child_cap,
            "agents.defaults.subagents.maxChildrenPerAgent must not be configured",
        ),
        (_drop_pm_mempalace_skill, "PM must load exactly mempalace-readonly and autoresearch"),
        (
            _remove_pm_native_codex_delegation_deny,
            "PM must deny OpenClaw/session discovery and delegation tools "
            "for native Codex delegation",
        ),
        (_add_pm_openclaw_subagent_allowlist, "PM must not declare OpenClaw subagents"),
        (
            _add_stage_openclaw_subagent_allowlist,
            "consensus_arbiter must not declare OpenClaw subagents",
        ),
        (_give_main_a_pm_skill, "main must load exactly mempalace-readonly"),
        (_set_main_full_profile, "main\\.tools\\.profile must be minimal"),
        (
            _give_stage_agent_write_skill,
            "must load exactly mempalace-readonly, quantipy-methodology, and "
            "quantipy-data-contract",
        ),
        (
            _drop_mempalace_readonly_server,
            "mcp.servers must expose exactly mempalace-readonly and g2-control",
        ),
        (
            _add_forbidden_full_mempalace_server,
            "mcp.servers must expose exactly mempalace-readonly and g2-control",
        ),
        (
            _break_readonly_server_args,
            "mcp\\.servers\\.mempalace-readonly\\.args must be "
            "\\['<wrapper>', '--palace', '<path>'\\]",
        ),
    ],
)
def test_load_autoresearch_policy_validates_route_skills_and_mempalace_denies(
    tmp_path: Path,
    mutator: Callable[[dict[str, object]], None],
    match: str,
) -> None:
    config = deepcopy(_load_config())
    mutator(config)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(AutoresearchConfigError, match=match):
        load_autoresearch_policy(config_path)


def test_mempalace_policy_and_codex_display_names_are_intentionally_distinct() -> None:
    assert "mempalace-readonly.mempalace_search" in MEMPALACE_READONLY_DISPLAY_TOOL_IDS
    assert "mempalace__mempalace_search" not in MEMPALACE_READONLY_DISPLAY_TOOL_IDS


def test_mempalace_readonly_tool_registry_contains_no_mutators() -> None:
    assert len(MEMPALACE_READONLY_TOOL_NAMES) == 19
    assert "mempalace_status" in MEMPALACE_READONLY_TOOL_NAMES
    assert "mempalace_diary_write" not in MEMPALACE_READONLY_TOOL_NAMES


def test_default_openclaw_config_projects_readonly_memory_and_main_control_only() -> None:
    config = _load_config()
    mcp = cast(dict[str, object], config["mcp"])
    servers = cast(dict[str, object], mcp["servers"])
    readonly_server = cast(dict[str, object], servers[MEMPALACE_READONLY_SERVER_ID])
    control_server = cast(dict[str, object], servers[autoresearch_runner.G2_CONTROL_SERVER_ID])

    assert list(servers) == [MEMPALACE_READONLY_SERVER_ID, autoresearch_runner.G2_CONTROL_SERVER_ID]
    assert cast(dict[str, object], readonly_server["codex"])["agents"] == [
        "main",
        "autoresearch-pm",
        "context_curator",
        "debater_microstructure",
        "debater_data",
        "debater_skeptic",
        "debater_theory",
        "debater_implementation",
        "consensus_arbiter",
        "implementer",
        "reviewer",
        "fixer",
    ]
    assert cast(list[str], readonly_server["args"])[1:] == [
        "--palace",
        "PLACEHOLDER_RESOLVED_BY_PUSH_SCRIPT",
    ]
    control_codex = cast(dict[str, object], control_server["codex"])
    assert control_codex["agents"] == ["main"]
    assert control_codex["defaultToolsApprovalMode"] == "approve"
    assert cast(list[str], control_server["args"]) == [
        "-m",
        autoresearch_runner.G2_CONTROL_MODULE,
    ]


def test_default_openclaw_config_has_no_model_visible_mempalace_write_tools() -> None:
    config = _load_config()
    agents_root = cast(dict[str, object], config["agents"])
    agents = cast(list[dict[str, object]], agents_root["list"])
    for agent in agents:
        agent_id = cast(str, agent["id"])
        if agent_id == "main":
            tools = cast(dict[str, object], agent["tools"])
            assert tools["profile"] == "minimal"
            assert cast(list[str], tools["allow"]) == list(
                autoresearch_runner.MAIN_OPENCLAW_TOOL_ALLOW_POLICY
            )
            denied_tools = cast(list[str], tools["deny"])
            assert "exec" in denied_tools
            assert "sessions_spawn" in denied_tools
            continue
        if agent_id == "autoresearch-pm":
            tools = cast(dict[str, object], agent["tools"])
            denied_tools = cast(list[str], tools["deny"])
            assert denied_tools == list(PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS)
            assert "sessions_yield" in denied_tools
            continue
        assert "tools" not in agent, agent_id


def test_native_codex_autoresearch_stage_agents_have_no_mcp_overrides() -> None:
    config = _load_config()
    mcp = cast(dict[str, object], config["mcp"])
    servers = cast(dict[str, object], mcp["servers"])
    readonly_server = cast(dict[str, object], servers[MEMPALACE_READONLY_SERVER_ID])
    readonly_agents = cast(list[str], cast(dict[str, object], readonly_server["codex"])["agents"])

    assert readonly_agents[:2] == ["main", "autoresearch-pm"]
    for agent_id in readonly_agents[2:]:
        path = autoresearch_runner.G2_OPENCLAW_REPO_ROOT / ".codex" / "agents" / f"{agent_id}.toml"
        assert "[mcp_servers" not in path.read_text(encoding="utf-8"), agent_id
