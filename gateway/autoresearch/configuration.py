"""OpenClaw/Codex autoresearch configuration loading and validation."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from gateway.autoresearch import constants
from gateway.autoresearch.compute import StageAgentPolicy as StageAgentPolicy
from gateway.autoresearch.errors import (
    AutoresearchConfigError as AutoresearchConfigError,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _require_bool as _require_bool,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _require_string_list as _require_string_list,
)
from gateway.autoresearch.fields import (
    _require_string_sequence as _require_string_sequence,
)
from gateway.autoresearch.memory import (
    G2_OPENCLAW_REPO_ROOT as G2_OPENCLAW_REPO_ROOT,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy as AutoresearchPolicy,
)
from gateway.autoresearch.policy import (
    CampaignGovernancePolicy as CampaignGovernancePolicy,
)


def _compile_mempalace_codex_display_tool_ids(
    tool_names: Sequence[str],
    *,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(f"{namespace}.{tool_name}" for tool_name in tool_names)


def _compile_codex_mcp_runtime_tool_ids(
    tool_names: Sequence[str],
    *,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(f"{namespace}__{tool_name}" for tool_name in tool_names)


MEMPALACE_READONLY_DISPLAY_TOOL_IDS = _compile_mempalace_codex_display_tool_ids(
    constants.MEMPALACE_READONLY_TOOL_NAMES,
    namespace=constants.MEMPALACE_READONLY_DISPLAY_NAMESPACE,
)
G2_CONTROL_DISPLAY_TOOL_IDS = _compile_mempalace_codex_display_tool_ids(
    constants.G2_CONTROL_TOOL_NAMES,
    namespace=constants.G2_CONTROL_SERVER_ID,
)
MEMPALACE_READONLY_RUNTIME_TOOL_IDS = _compile_codex_mcp_runtime_tool_ids(
    constants.MEMPALACE_READONLY_TOOL_NAMES,
    namespace=constants.MEMPALACE_READONLY_RUNTIME_NAMESPACE,
)
G2_CONTROL_RUNTIME_TOOL_IDS = _compile_codex_mcp_runtime_tool_ids(
    constants.G2_CONTROL_TOOL_NAMES,
    namespace=constants.G2_CONTROL_RUNTIME_NAMESPACE,
)
MAIN_ALLOWED_TOOL_IDS = (*G2_CONTROL_RUNTIME_TOOL_IDS, *MEMPALACE_READONLY_RUNTIME_TOOL_IDS)
MAIN_OPENCLAW_TOOL_ALLOW_POLICY = MAIN_ALLOWED_TOOL_IDS


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchConfigError(f"missing config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchConfigError(f"invalid JSON in config file: {path}") from exc
    return _ensure_mapping(raw, label=str(path))


def _agent_policy_from_json(
    agent_map: Mapping[str, Mapping[str, object]], agent_id: str
) -> StageAgentPolicy:
    try:
        raw = agent_map[agent_id]
    except KeyError as exc:
        raise AutoresearchConfigError(f"missing configured agent: {agent_id}") from exc
    model = _ensure_mapping(raw.get("model"), label=f"{agent_id}.model")
    skills = _require_string_list(raw, "skills")
    return StageAgentPolicy(
        agent_id=agent_id,
        model=_require_str(model, "primary"),
        reasoning=_require_str(raw, "thinkingDefault"),
        skills=skills,
    )


def _codex_agent_model(model: str) -> str:
    prefix = "openai/"
    if not model.startswith(prefix):
        raise AutoresearchConfigError(f"stage model must use OpenAI provider ref: {model}")
    return model.removeprefix(prefix)


def _load_codex_agent_toml(path: Path) -> Mapping[str, object]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchConfigError(f"missing native Codex stage agent file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AutoresearchConfigError(f"invalid native Codex stage agent TOML: {path}") from exc
    return _ensure_mapping(data, label=str(path))


def _campaign_governance_from_defaults(
    defaults: Mapping[str, object],
) -> CampaignGovernancePolicy:
    if "autoresearchCampaignGovernance" not in defaults:
        return CampaignGovernancePolicy()
    try:
        raw = _ensure_mapping(
            defaults["autoresearchCampaignGovernance"],
            label="agents.defaults.autoresearchCampaignGovernance",
        )
        _require_exact_keys(
            raw,
            label="agents.defaults.autoresearchCampaignGovernance",
            expected=("stallConsecutiveNonKeep", "stallConsecutiveNoConsensus"),
        )
        non_keep = _require_int(raw, "stallConsecutiveNonKeep")
        no_consensus = _require_int(raw, "stallConsecutiveNoConsensus")
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc
    if not 1 <= non_keep <= 100:
        raise AutoresearchConfigError(
            "agents.defaults.autoresearchCampaignGovernance.stallConsecutiveNonKeep "
            "must be an integer from 1 through 100"
        )
    if not 1 <= no_consensus <= 100:
        raise AutoresearchConfigError(
            "agents.defaults.autoresearchCampaignGovernance.stallConsecutiveNoConsensus "
            "must be an integer from 1 through 100"
        )
    return CampaignGovernancePolicy(
        stall_consecutive_non_keep=non_keep,
        stall_consecutive_no_consensus=no_consensus,
    )


def _validate_codex_native_stage_agents(policy: AutoresearchPolicy) -> None:
    for stage in (
        policy.context_curator,
        *policy.debate_agents,
        policy.consensus,
        policy.implementer,
        policy.reviewer,
        policy.fixer,
    ):
        path = G2_OPENCLAW_REPO_ROOT / ".codex" / "agents" / f"{stage.agent_id}.toml"
        data = _load_codex_agent_toml(path)
        if _require_str(data, "name") != stage.agent_id:
            raise AutoresearchConfigError(
                f"native Codex stage agent {path} must be named {stage.agent_id}"
            )
        if _require_str(data, "model") != _codex_agent_model(stage.model):
            raise AutoresearchConfigError(
                f"native Codex stage agent {stage.agent_id} must use {stage.model}"
            )
        if _require_str(data, "model_reasoning_effort") != stage.reasoning:
            raise AutoresearchConfigError(
                f"native Codex stage agent {stage.agent_id} must use {stage.reasoning} reasoning"
            )
        if "mcp_servers" in data:
            raise AutoresearchConfigError(
                f"native Codex stage agent {stage.agent_id} must not override inherited MCP servers"
            )


def load_autoresearch_policy(
    config_path: Path = constants.DEFAULT_OPENCLAW_CONFIG_PATH,
) -> AutoresearchPolicy:
    config = _load_json(config_path)
    plugins = _ensure_mapping(config.get("plugins"), label="plugins")
    try:
        plugin_allow = _require_string_list(plugins, "allow")
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError("plugins.allow must explicitly include codex") from exc
    if "codex" not in plugin_allow:
        raise AutoresearchConfigError("plugins.allow must explicitly include codex")
    agents = _ensure_mapping(config.get("agents"), label="agents")
    defaults = _ensure_mapping(agents.get("defaults"), label="agents.defaults")
    compaction = _ensure_mapping(defaults.get("compaction"), label="agents.defaults.compaction")
    if _require_str(compaction, "mode") != "default":
        raise AutoresearchConfigError(
            "agents.defaults.compaction.mode must be default for the Codex OAuth route"
        )
    if defaults.get("maxConcurrent") != 2:
        raise AutoresearchConfigError(
            "agents.defaults.maxConcurrent must be 2 to cap the main lane with PM headroom"
        )
    default_subagents = _ensure_mapping(
        defaults.get("subagents"), label="agents.defaults.subagents"
    )
    if default_subagents.get("maxConcurrent") != 1:
        raise AutoresearchConfigError(
            "agents.defaults.subagents.maxConcurrent must be 1 to serialize heavy Codex stages"
        )
    if "maxChildrenPerAgent" in default_subagents:
        raise AutoresearchConfigError(
            "agents.defaults.subagents.maxChildrenPerAgent must not be configured"
        )
    campaign_governance = _campaign_governance_from_defaults(defaults)
    models = _ensure_mapping(config.get("models"), label="models")
    providers = _ensure_mapping(models.get("providers"), label="providers")
    openai_provider = _ensure_mapping(providers.get("openai"), label="providers.openai")
    if _require_str(openai_provider, "api") != "openai-responses":
        raise AutoresearchConfigError("providers.openai.api must be openai-responses")
    agent_runtime = _ensure_mapping(
        openai_provider.get("agentRuntime"), label="providers.openai.agentRuntime"
    )
    if _require_str(agent_runtime, "id") != "codex":
        raise AutoresearchConfigError("providers.openai.agentRuntime.id must be codex")
    openai_models_raw = openai_provider.get("models")
    if not isinstance(openai_models_raw, Sequence) or isinstance(openai_models_raw, str | bytes):
        raise AutoresearchConfigError("providers.openai.models must be a list")
    model_caps: dict[str, bool] = {}
    for item in openai_models_raw:
        data = _ensure_mapping(item, label="provider_model")
        model_caps[_require_str(data, "id")] = _require_bool(data, "reasoning")
    for required_model in ("gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra"):
        if model_caps.get(required_model) is not True:
            raise AutoresearchConfigError(f"openai/{required_model} must exist with reasoning=true")

    agent_list_raw = agents.get("list")
    if not isinstance(agent_list_raw, Sequence) or isinstance(agent_list_raw, str | bytes):
        raise AutoresearchConfigError("agents.list must be a list")
    agent_map: dict[str, Mapping[str, object]] = {}
    for item in agent_list_raw:
        data = _ensure_mapping(item, label="agent")
        agent_map[_require_str(data, "id")] = data

    policy = AutoresearchPolicy(
        pm=_agent_policy_from_json(agent_map, "autoresearch-pm"),
        main_interface=_agent_policy_from_json(agent_map, "main"),
        context_curator=_agent_policy_from_json(agent_map, "context_curator"),
        debate_agents=tuple(
            _agent_policy_from_json(agent_map, agent_id)
            for agent_id in (
                "debater_microstructure",
                "debater_data",
                "debater_skeptic",
                "debater_theory",
                "debater_implementation",
            )
        ),
        consensus=_agent_policy_from_json(agent_map, "consensus_arbiter"),
        implementer=_agent_policy_from_json(agent_map, "implementer"),
        reviewer=_agent_policy_from_json(agent_map, "reviewer"),
        fixer=_agent_policy_from_json(agent_map, "fixer"),
        campaign_governance=campaign_governance,
    )
    _validate_policy(policy, agent_map, config)
    return policy


def _validate_policy(
    policy: AutoresearchPolicy,
    agent_map: Mapping[str, Mapping[str, object]],
    config: Mapping[str, object],
) -> None:
    if policy.main_interface.model != "openai/gpt-5.4" or policy.main_interface.reasoning != "high":
        raise AutoresearchConfigError("main must be openai/gpt-5.4 with high reasoning")
    if tuple(policy.main_interface.skills) != ("mempalace-readonly",):
        raise AutoresearchConfigError("main must load exactly mempalace-readonly")
    main_raw = agent_map["main"]
    if main_raw.get("subagents") is not None:
        raise AutoresearchConfigError("main must not declare a subagent allowlist")
    main_tools = _ensure_mapping(main_raw.get("tools"), label="main.tools")
    if main_tools.get("profile") != "minimal":
        raise AutoresearchConfigError("main.tools.profile must be minimal")
    main_allowed_tool_list = _require_string_list(main_tools, "allow")
    if tuple(main_allowed_tool_list) != MAIN_OPENCLAW_TOOL_ALLOW_POLICY:
        raise AutoresearchConfigError(
            "main must allow exactly the direct Codex MCP control/read-only tools"
        )
    try:
        plugins = _ensure_mapping(config.get("plugins"), label="plugins")
        entries = _ensure_mapping(plugins.get("entries"), label="plugins.entries")
        codex = _ensure_mapping(entries.get("codex"), label="plugins.entries.codex")
        plugin_config = _ensure_mapping(codex.get("config"), label="plugins.entries.codex.config")
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc
    if "nativeToolSurfaceEnabled" in plugin_config:
        raise AutoresearchConfigError(
            "nativeToolSurfaceEnabled is not supported by the current Codex plugin schema"
        )
    if "codexDynamicToolsExclude" in plugin_config:
        raise AutoresearchConfigError(
            "codexDynamicToolsExclude must not be used as a native Codex tool guard"
        )
    main_denied_tool_list = set(_require_string_list(main_tools, "deny"))
    required_main_denies = {
        "exec",
        "sessions_spawn",
        "sessions_yield",
        "sessions_send",
        "sessions_list",
        "sessions_history",
        "agents_list",
    }
    if not required_main_denies <= main_denied_tool_list:
        raise AutoresearchConfigError("main must deny native exec and OpenClaw session/agent tools")
    if policy.pm.model != "openai/gpt-5.6-sol" or policy.pm.reasoning != "high":
        raise AutoresearchConfigError("PM must be openai/gpt-5.6-sol with high reasoning")
    if (
        policy.context_curator.model != "openai/gpt-5.4"
        or policy.context_curator.reasoning != "high"
    ):
        raise AutoresearchConfigError("context_curator must be openai/gpt-5.4 with high reasoning")

    expected_debate_models = {
        "debater_microstructure": "openai/gpt-5.5",
        "debater_data": "openai/gpt-5.6-terra",
        "debater_skeptic": "openai/gpt-5.5",
        "debater_theory": "openai/gpt-5.4",
        "debater_implementation": "openai/gpt-5.4",
    }
    for agent in policy.debate_agents:
        if agent.reasoning != "high":
            raise AutoresearchConfigError(f"{agent.agent_id} must use high reasoning")
        if agent.model != expected_debate_models[agent.agent_id]:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be {expected_debate_models[agent.agent_id]} "
                "with high reasoning"
            )

    if policy.consensus.model != "openai/gpt-5.6-sol" or policy.consensus.reasoning != "high":
        raise AutoresearchConfigError(
            "consensus_arbiter must be openai/gpt-5.6-sol with high reasoning"
        )
    for agent in (policy.implementer, policy.fixer):
        if agent.model != "openai/gpt-5.4" or agent.reasoning != "high":
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be openai/gpt-5.4 with high reasoning"
            )
    if policy.reviewer.model != "openai/gpt-5.6-sol" or policy.reviewer.reasoning != "high":
        raise AutoresearchConfigError("reviewer must be exactly one openai/gpt-5.6-sol high agent")
    if policy.reviewer.agent_id != "reviewer":
        raise AutoresearchConfigError("reviewer stage must be configured as agent id 'reviewer'")

    if tuple(policy.pm.skills) != ("mempalace-readonly", "autoresearch"):
        raise AutoresearchConfigError("PM must load exactly mempalace-readonly and autoresearch")
    pm_raw = agent_map["autoresearch-pm"]
    pm_tools = _ensure_mapping(pm_raw.get("tools"), label="autoresearch-pm.tools")
    pm_denied_tool_list = _require_string_list(pm_tools, "deny")
    if tuple(pm_denied_tool_list) != constants.PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS:
        raise AutoresearchConfigError(
            "PM must deny OpenClaw/session discovery and delegation tools "
            "for native Codex delegation"
        )
    if pm_raw.get("subagents") is not None:
        raise AutoresearchConfigError("PM must not declare OpenClaw subagents")
    _validate_codex_app_server_sandbox(config)
    _validate_mempalace_server_split(config, policy)
    _validate_codex_native_stage_agents(policy)
    for agent in (
        policy.context_curator,
        *policy.debate_agents,
        policy.consensus,
        policy.implementer,
        policy.reviewer,
        policy.fixer,
    ):
        if tuple(agent.skills) != (
            "mempalace-readonly",
            "quantipy-methodology",
            "quantipy-data-contract",
        ):
            raise AutoresearchConfigError(
                f"{agent.agent_id} must load exactly mempalace-readonly, "
                "quantipy-methodology, and quantipy-data-contract"
            )
        if agent_map[agent.agent_id].get("subagents") is not None:
            raise AutoresearchConfigError(f"{agent.agent_id} must not declare OpenClaw subagents")
        if agent_map[agent.agent_id].get("tools") is not None:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must not carry MemPalace write-tool policy remnants"
            )


def _validate_codex_app_server_sandbox(config: Mapping[str, object]) -> None:
    try:
        plugins = _ensure_mapping(config.get("plugins"), label="plugins")
        entries = _ensure_mapping(plugins.get("entries"), label="plugins.entries")
        codex = _ensure_mapping(entries.get("codex"), label="plugins.entries.codex")
        plugin_config = _ensure_mapping(codex.get("config"), label="plugins.entries.codex.config")
        app_server = _ensure_mapping(
            plugin_config.get("appServer"),
            label="plugins.entries.codex.config.appServer",
        )
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc
    if app_server.get("sandbox") != "workspace-write":
        raise AutoresearchConfigError("Codex app-server sandbox must be workspace-write")
    if app_server.get("defaultWorkspaceDir") != str(
        constants.DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT
    ):
        raise AutoresearchConfigError(
            "Codex app-server defaultWorkspaceDir must be "
            f"{constants.DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT}"
        )
    if app_server.get("networkProxy") is not None:
        raise AutoresearchConfigError(
            "Codex app-server networkProxy must not be configured; pinned Codex 0.144.3 "
            "rejects the plugin-generated :project_roots permissions profile"
        )


def _validate_mempalace_server_split(
    config: Mapping[str, object],
    policy: AutoresearchPolicy,
) -> None:
    try:
        mcp = _ensure_mapping(config.get("mcp"), label="mcp")
        servers = _ensure_mapping(mcp.get("servers"), label="mcp.servers")
        if set(servers) != {constants.MEMPALACE_READONLY_SERVER_ID, constants.G2_CONTROL_SERVER_ID}:
            raise AutoresearchConfigError(
                "mcp.servers must expose exactly mempalace-readonly and g2-control"
            )
        readonly_server = _ensure_mapping(
            servers.get(constants.MEMPALACE_READONLY_SERVER_ID),
            label=f"mcp.servers.{constants.MEMPALACE_READONLY_SERVER_ID}",
        )
        control_server = _ensure_mapping(
            servers.get(constants.G2_CONTROL_SERVER_ID),
            label=f"mcp.servers.{constants.G2_CONTROL_SERVER_ID}",
        )
        _validate_mempalace_server(
            readonly_server,
            server_id=constants.MEMPALACE_READONLY_SERVER_ID,
            expected_agents=(
                policy.main_interface.agent_id,
                policy.pm.agent_id,
                *policy.all_stage_agent_ids,
            ),
            expected_args_prefix=(constants.MEMPALACE_READONLY_WRAPPER_BASENAME, "--palace"),
        )
        _validate_mempalace_server(
            control_server,
            server_id=constants.G2_CONTROL_SERVER_ID,
            expected_agents=(policy.main_interface.agent_id,),
            expected_args_prefix=("-m", constants.G2_CONTROL_MODULE),
        )
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc


def _validate_mempalace_server(
    server: Mapping[str, object],
    *,
    server_id: str,
    expected_agents: tuple[str, ...],
    expected_args_prefix: tuple[str, ...],
) -> None:
    _require_str(server, "command")
    args = _require_string_sequence(server.get("args"), label=f"mcp.servers.{server_id}.args")
    codex = _ensure_mapping(server.get("codex"), label=f"mcp.servers.{server_id}.codex")
    agents = _require_string_list(codex, "agents")
    if tuple(agents) != expected_agents:
        raise AutoresearchConfigError(
            f"mcp.servers.{server_id}.codex.agents must exactly match {expected_agents}"
        )
    if server_id == constants.G2_CONTROL_SERVER_ID:
        if tuple(args) != expected_args_prefix:
            raise AutoresearchConfigError(
                "mcp.servers.g2-control.args must be ['-m', 'gateway.g2_control_mcp_server']"
            )
        if codex.get("defaultToolsApprovalMode") != "approve":
            raise AutoresearchConfigError(
                "mcp.servers.g2-control.codex.defaultToolsApprovalMode must be approve"
            )
        return
    if len(args) != 3 or args[1] != "--palace":
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args must be ['<wrapper>', '--palace', '<path>']"
        )
    readonly_entrypoint = args[0].strip()
    if not readonly_entrypoint:
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[0] must be a wrapper path"
        )
    if readonly_entrypoint != constants.MEMPALACE_CONFIG_PLACEHOLDER and (
        Path(readonly_entrypoint).name != constants.MEMPALACE_READONLY_WRAPPER_BASENAME
    ):
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[0] must point to "
            f"{constants.MEMPALACE_READONLY_WRAPPER_BASENAME}"
        )
    if not args[2].strip():
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[2] must be a palace path"
        )
