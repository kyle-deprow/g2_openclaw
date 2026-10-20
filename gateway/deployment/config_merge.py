"""jq-compatible OpenClaw configuration assembly.

The push script deliberately keeps jq as the reference implementation while
the Python implementation is being rolled out.  This module mirrors the
assembly operations and emits jq-style JSON so the published file bytes stay
stable across the two implementations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from ..agent_profiles import AgentProfile, AgentProfileError, load_profile
from .agent_guard import guard_config, guard_plugin_dir
from .guarded_fs import guard_destination_path_chain


@dataclass(frozen=True)
class JsonNumber:
    """A JSON number whose lexical form must survive jq-style re-encoding."""

    raw: str


JsonValue: TypeAlias = (  # noqa: UP040 (pre-commit mypy pin lacks PEP 695)
    "dict[str, JsonValue] | list[JsonValue] | str | bool | None | int | float | JsonNumber"
)
JsonObject: TypeAlias = "dict[str, JsonValue]"  # noqa: UP040


class ConfigMergeError(RuntimeError):
    """Raised when the managed config cannot be assembled safely."""


def _validate_contract_file(raw_path: str, *, private: bool, label: str) -> str:
    """Validate a renderer input without reading secret contents."""

    path = Path(raw_path)
    if not path.is_absolute():
        raise ConfigMergeError(f"{label} must be an absolute path")
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise ConfigMergeError(f"{label} is not readable: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ConfigMergeError(f"{label} must be a regular non-symlink file")
    if private:
        if path_stat.st_uid != os.getuid():
            raise ConfigMergeError(f"{label} must be owned by the deployment user")
        if stat.S_IMODE(path_stat.st_mode) & 0o077:
            raise ConfigMergeError(f"{label} must not be group/world accessible")
    return str(path)


STALE_CODING_PROVIDER_KEYS = frozenset({"github-copilot", "copilot-proxy", "copilot-cli"})

# These are native OpenAI/Codex routes used by the managed research owner and
# its bounded stage agents. The reviewer is native Sol and read-only.
NATIVE_LUNA_MODEL_REF = "openai/gpt-5.6-luna"
NATIVE_SOL_MODEL_REF = "openai/gpt-5.6-sol"
# Explicit non-Codex delivery routes remain available only for catalogued
# OpenAI models.  Keep this exact set in lockstep with openclaw.json; provider
# prefixes alone must never make an arbitrary model active.
OPENAI_ALTERNATE_MODEL_REFS = frozenset(
    {
        "azure-oai-g2/gpt-5.4",
        "azure-oai-g2-mini/gpt-5-mini",
        "openrouter/openai/gpt-4.1",
    }
)

# Claude profile roster: the three OpenClaw child agents that replace the Codex-native roles
# (implementer, experiment_runner, reviewer). The owner spawns them with sessions_spawn.
CLAUDE_RUNTIME_ID = "claude-cli"
CLAUDE_PLUGIN_ID = "anthropic"
OWNER_AGENT_ID = "research-orchestrator"
CLAUDE_CHILD_AGENTS: tuple[tuple[str, str], ...] = (
    ("research-implementer", "implementer"),
    ("research-runner", "runner"),
    ("research-reviewer", "reviewer"),
)
CLAUDE_CHILD_AGENT_IDS: tuple[str, ...] = tuple(agent_id for agent_id, _ in CLAUDE_CHILD_AGENTS)
# Children are leaves: they never orchestrate. These are OpenClaw-owned tools. Claude's native
# Write/Edit/Bash are not reachable through tools.deny; they are confined by the
# g2-agent-guard plugin and the dedicated CLAUDE_CONFIG_DIR settings (claude_runtime).
CLAUDE_CHILD_BASE_DENY: tuple[str, ...] = (
    "sessions_spawn",
    "sessions_yield",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "agents_list",
)
GUARD_PLUGIN_ID = "g2-agent-guard"
# claude-cli ignores the per-server ``codex.agents`` projection, so the managed MCP servers
# would otherwise reach every agent whose tool profile is "full". Only main may use them
# (through its explicit allow list); everyone else gets these server-wide denies.
CLAUDE_MCP_DENY: tuple[str, ...] = ("g2-control__*", "mempalace-readonly__*")
CLAUDE_OWNER_TOOLS: dict[str, JsonValue] = {
    "profile": "full",
    "deny": cast(JsonValue, list(CLAUDE_MCP_DENY)),
}
CLAUDE_CHILD_TOOL_DENY: tuple[str, ...] = (*CLAUDE_CHILD_BASE_DENY, *CLAUDE_MCP_DENY)
MAIN_TOOL_DENY_IDS: tuple[str, ...] = (
    "exec",
    "sessions_spawn",
    "sessions_yield",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "agents_list",
)

# These are the exact paths rejected by the OpenClaw 8.1 runtime schema.  The
# migration intentionally does not walk by key name: similarly named nested
# settings must survive the candidate unchanged.
UNSUPPORTED_OPENCLAW_8_1_PATHS: tuple[tuple[str, ...], ...] = (
    ("meta", "lastTouchedAt"),
    ("agents", "defaults", "memorySearch"),
    ("commands", "ownerDisplay"),
    ("memory", "backend"),
)

LEGACY_MEMORY_SEARCH_PATH = ("agents", "defaults", "memorySearch")
CANONICAL_MEMORY_SEARCH_ENABLED_PATH = ("memory", "search", "enabled")


@dataclass(frozen=True)
class AssemblyInputs:
    """Resolved values substituted into the managed configuration."""

    repo_root: str
    openclaw_state_root: str
    python_bin: str
    mempalace_python: str
    mempalace_palace: str
    mempalace_wrapper: str
    fastembed_cache: str
    mempalace_embedding_model: str
    hf_hub_offline: str
    g2_module: str
    research_v2_root: str
    research_core_database: str
    owner_env_file: str
    openclaw_host: str
    openclaw_port: str
    mempalace_readonly_agents: tuple[str, ...]
    g2_agents: tuple[str, ...]
    provider: str
    model_primary: str
    model_provider: str
    model_id: str
    orchestrator_model_primary: str
    # None selects the repository codex profile, which keeps the pre-profile assembly bytes.
    profile: AgentProfile | None = None
    hypothesis_worktrees_root: str = ""
    owner_workspace_root: str = ""
    gateway_cli: str = ""


_NUMBER_RE = re.compile(
    r"^(?P<sign>-?)(?P<int>[0-9]+)(?P<fraction>\.[0-9]+)?(?P<exponent>[eE][+-]?[0-9]+)?$"
)


def _parse_number(raw: str) -> JsonNumber:
    return JsonNumber(raw)


def _parse_constant(raw: str) -> JsonValue:
    raise ValueError(f"invalid JSON constant {raw!r}")


def load_json(path: str | Path) -> JsonValue:
    """Load JSON while retaining number spellings used by jq."""

    try:
        return cast(
            JsonValue,
            json.loads(
                Path(path).read_text(encoding="utf-8"),
                parse_int=_parse_number,
                parse_float=_parse_number,
                parse_constant=_parse_constant,
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConfigMergeError(f"could not read JSON config {path}: {exc}") from exc


def _copy(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _copy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy(child) for child in value]
    return value


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ConfigMergeError(f"{context} must be a JSON object")
    return value


def _get_object(value: JsonValue, key: str) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    child = value.get(key)
    return child if isinstance(child, dict) else None


def _get_path(root: JsonValue, path: Sequence[str]) -> JsonValue | None:
    current = root
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _read_path(root: JsonValue, path: Sequence[str]) -> tuple[bool, JsonValue | None]:
    """Read one exact path while preserving the distinction between absent/null."""

    current = root
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set_path(root: JsonObject, path: Sequence[str], value: JsonValue) -> None:
    current = root
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _pop_path(root: JsonObject, path: Sequence[str]) -> tuple[bool, JsonValue | None]:
    """Remove one exact object path, preserving whether its value was null."""

    current: JsonObject = root
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            return False, None
        current = child
    leaf = path[-1]
    if leaf not in current:
        return False, None
    return True, current.pop(leaf)


def _path_exists(root: JsonObject, path: Sequence[str]) -> bool:
    current: JsonValue = root
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _validate_legacy_memory_search(value: JsonValue) -> bool:
    """Return the exact supported legacy memory-search policy."""

    if not isinstance(value, dict) or set(value) != {"enabled"}:
        raise ConfigMergeError(
            "agents.defaults.memorySearch must contain only a boolean enabled field"
        )
    enabled = value["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigMergeError("agents.defaults.memorySearch.enabled must be a boolean")
    return enabled


def _validate_canonical_memory_shape(root: JsonObject) -> tuple[bool, bool | None]:
    """Validate canonical memory-search containers and return enabled presence/value."""

    memory_present, memory_value = _read_path(root, ("memory",))
    if not memory_present:
        return False, None
    if not isinstance(memory_value, dict):
        raise ConfigMergeError("memory must be an object when mapping memorySearch")
    search_present, search_value = _read_path(memory_value, ("search",))
    if not search_present:
        return False, None
    if not isinstance(search_value, dict):
        raise ConfigMergeError("memory.search must be an object when mapping memorySearch")
    enabled_present, enabled_value = _read_path(search_value, ("enabled",))
    if not enabled_present:
        return False, None
    if not isinstance(enabled_value, bool):
        raise ConfigMergeError("memory.search.enabled must be a boolean")
    return True, enabled_value


def _legacy_memory_search_mapping(
    local: JsonObject, repo: JsonObject
) -> tuple[JsonObject, bool] | None:
    """Validate and resolve the old policy before a memory overlay can hide it."""

    legacy_values: list[bool] = []
    for source in (local, repo):
        found, value = _read_path(source, LEGACY_MEMORY_SEARCH_PATH)
        if found:
            legacy_values.append(_validate_legacy_memory_search(value))

    if not legacy_values:
        return None
    if any(value != legacy_values[0] for value in legacy_values[1:]):
        raise ConfigMergeError(
            "conflicting agents.defaults.memorySearch.enabled values cannot be mapped"
        )

    enabled = legacy_values[0]
    for source in (local, repo):
        canonical_present, canonical_enabled = _validate_canonical_memory_shape(source)
        if canonical_present and canonical_enabled != enabled:
            raise ConfigMergeError(
                "conflicting memory.search.enabled and agents.defaults.memorySearch.enabled values"
            )
    return (
        {
            "path": ".".join(LEGACY_MEMORY_SEARCH_PATH),
            "value": {"enabled": enabled},
            "mapped_to": ".".join(CANONICAL_MEMORY_SEARCH_ENABLED_PATH),
        },
        enabled,
    )


def _set_canonical_memory_search_enabled(config: JsonObject, enabled: bool) -> None:
    """Set the mapped policy after validating its container shape."""

    _validate_canonical_memory_shape(config)
    memory = config.get("memory")
    if memory is None:
        memory = {}
        config["memory"] = memory
    if not isinstance(memory, dict):
        raise ConfigMergeError("memory must be an object when mapping memorySearch")
    search = memory.get("search")
    if search is None:
        search = {}
        memory["search"] = search
    if not isinstance(search, dict):
        raise ConfigMergeError("memory.search must be an object when mapping memorySearch")
    existing = search.get("enabled")
    if existing is not None and (not isinstance(existing, bool) or existing != enabled):
        raise ConfigMergeError(
            "conflicting memory.search.enabled and agents.defaults.memorySearch.enabled values"
        )
    search["enabled"] = enabled


def migrate_8_1_config(
    config: JsonObject,
    *,
    memory_search_mapping: tuple[JsonObject, bool] | None = None,
    memory_backend_record: JsonObject | None = None,
) -> tuple[JsonObject, JsonObject]:
    """Map the supported legacy policy, remove exact unsupported paths, and record it."""

    candidate = cast(JsonObject, _copy(config))
    if memory_search_mapping is None:
        memory_search_mapping = _legacy_memory_search_mapping(candidate, {})

    mapped_legacy_record: JsonObject | None = None
    if memory_search_mapping is not None:
        mapped_legacy_record, enabled = memory_search_mapping
        found, legacy_value = _read_path(candidate, LEGACY_MEMORY_SEARCH_PATH)
        if not found:
            raise ConfigMergeError(
                "8.1 schema migration could not find the legacy memorySearch policy"
            )
        if _validate_legacy_memory_search(legacy_value) != enabled:
            raise ConfigMergeError("conflicting memorySearch values changed during config assembly")
        _set_canonical_memory_search_enabled(candidate, enabled)

    removed: list[JsonValue] = []
    for path in UNSUPPORTED_OPENCLAW_8_1_PATHS:
        found, value = _pop_path(candidate, path)
        if found:
            if path == LEGACY_MEMORY_SEARCH_PATH:
                if mapped_legacy_record is None:
                    raise ConfigMergeError("legacy memorySearch policy was not mapped")
                removed.append(_copy(mapped_legacy_record))
            else:
                removed.append({"path": ".".join(path), "value": _copy(value)})
        elif path == ("memory", "backend") and memory_backend_record is not None:
            removed.append(_copy(memory_backend_record))

    if memory_search_mapping is not None:
        enabled = memory_search_mapping[1]
        canonical_present, canonical_enabled = _validate_canonical_memory_shape(candidate)
        if not canonical_present or canonical_enabled != enabled:
            raise ConfigMergeError("mapped memory.search.enabled policy was not preserved")

    record: JsonObject = {"removed": removed}
    remaining = [
        ".".join(path) for path in UNSUPPORTED_OPENCLAW_8_1_PATHS if _path_exists(candidate, path)
    ]
    if remaining:
        raise ConfigMergeError(
            "8.1 schema migration left unsupported paths: " + ", ".join(remaining)
        )
    return candidate, record


def write_migration_record(path: str | Path, record: JsonObject) -> None:
    """Write a deterministic migration record with owner-only permissions."""

    record_path = os.path.abspath(os.fspath(path))
    parent_path = os.path.dirname(record_path)
    guard_context = f"preparing migration record {record_path}"
    try:
        guard_destination_path_chain(parent_path, guard_context)
    except (OSError, RuntimeError) as exc:
        raise ConfigMergeError(f"could not prepare migration record {record_path}: {exc}") from exc
    try:
        parent_stat = os.lstat(parent_path)
    except OSError as exc:
        raise ConfigMergeError(
            f"migration record parent is not an existing directory: {parent_path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise ConfigMergeError(
            f"migration record parent is not an existing directory: {parent_path}"
        )
    try:
        guard_destination_path_chain(record_path, guard_context)
    except (OSError, RuntimeError) as exc:
        raise ConfigMergeError(f"could not prepare migration record {record_path}: {exc}") from exc

    encoded = serialize_json(record)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(record_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        os.chmod(record_path, 0o600)
    except OSError as exc:
        raise ConfigMergeError(f"could not write migration record {record_path}: {exc}") from exc


def _jq_truthy(value: JsonValue | object) -> bool:
    """Return jq's truth value for the `// empty` guards used by the script."""

    return value is not None and value is not False


def deep_merge(left: JsonValue, right: JsonValue) -> JsonValue:
    """Implement jq's `*`: recursive object merge, with right-wins leaves."""

    if isinstance(left, dict) and isinstance(right, dict):
        merged = cast(JsonObject, _copy(left))
        for key, right_value in right.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], right_value)
            else:
                merged[key] = _copy(right_value)
        return merged
    return _copy(right)


def _walk_remove_keys(value: JsonValue, forbidden: frozenset[str]) -> JsonValue:
    if isinstance(value, list):
        return [_walk_remove_keys(child, forbidden) for child in value]
    if isinstance(value, dict):
        return {
            key: _walk_remove_keys(child, forbidden)
            for key, child in value.items()
            if key not in forbidden
        }
    return value


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        chr(0x7F), "\\u007f"
    )


def _jq_number(raw: str) -> str:
    """Format exponent notation in the same fixed/scientific bands as jq.

    OpenClaw's checked-in configuration uses integers.  The lexical-preserving
    path is nevertheless important for machine-local JSON and the golden
    corpus.  jq retains significant zeroes and uses fixed notation when the
    expanded decimal point remains above the -6 boundary and does not require
    trailing zeroes; outside that band its exponent marker is uppercase with
    an explicit plus sign for positive exponents.
    """

    match = _NUMBER_RE.match(raw)
    if match is None or match.group("exponent") is None:
        return raw

    sign = match.group("sign")
    integer = match.group("int")
    fraction = match.group("fraction") or ""
    exponent = int(match.group("exponent")[1:])
    digits = integer + fraction[1:]
    decimal_position = len(integer) + exponent

    if -6 < decimal_position <= len(digits):
        if decimal_position <= 0:
            return f"{sign}0.{('0' * -decimal_position)}{digits}"
        if decimal_position < len(digits):
            return f"{sign}{digits[:decimal_position]}.{digits[decimal_position:]}"
        return f"{sign}{digits}{'0' * (decimal_position - len(digits))}"

    coefficient = integer.rstrip("0") or "0"
    trailing_integer_zeroes = len(integer) - len(coefficient)
    if fraction:
        coefficient = coefficient + "." + fraction[1:]
    scientific_exponent = len(integer) - 1 + exponent
    if trailing_integer_zeroes and not fraction:
        coefficient = integer[0] + ("." + integer[1:] if len(integer) > 1 else "")
    exponent_sign = "+" if scientific_exponent >= 0 else "-"
    return f"{sign}{coefficient}E{exponent_sign}{abs(scientific_exponent)}"


def _encode(value: JsonValue, level: int) -> str:
    indent = "  " * level
    child_indent = "  " * (level + 1)
    if isinstance(value, JsonNumber):
        return _jq_number(value.raw)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not (value == value and abs(value) != float("inf")):
            raise ConfigMergeError("JSON cannot contain NaN or infinity")
        return _jq_number(json.dumps(value, allow_nan=False))
    if isinstance(value, list):
        if not value:
            return "[]"
        children = [f"{child_indent}{_encode(child, level + 1)}" for child in value]
        return "[\n" + ",\n".join(children) + f"\n{indent}]"
    if not value:
        return "{}"
    children = [
        f"{child_indent}{_json_string(key)}: {_encode(child, level + 1)}"
        for key, child in value.items()
    ]
    return "{\n" + ",\n".join(children) + f"\n{indent}}}"


def serialize_json(value: JsonValue) -> bytes:
    """Serialize JSON using jq's two-space pretty printer and final newline."""

    return (_encode(value, 0) + "\n").encode("utf-8")


def _provider_selection(
    provider: str, openai_model: str, openrouter_model: str
) -> tuple[str, str, str]:
    if provider == "codex":
        model_id = openai_model or "gpt-5.4"
        return f"openai/{model_id}", "openai", model_id
    if provider == "azure":
        return "azure-oai-g2/gpt-5.4", "azure-oai-g2", "gpt-5.4"
    if provider == "openrouter":
        model_id = openrouter_model
        if not model_id:
            raise ConfigMergeError(
                "ERROR: OPENROUTER_MODEL is required when OPENCLAW_PROVIDER=openrouter; "
                "refusing an implicit model fallback."
            )
        return f"openrouter/{model_id}", "openrouter", model_id
    raise ConfigMergeError(
        f"ERROR: Unknown OPENCLAW_PROVIDER '{provider}'. Use 'codex', 'azure', or 'openrouter'."
    )


def orchestrator_model_primary(repo: JsonObject) -> str:
    agents = _get_object(repo, "agents")
    if agents is None:
        return ""
    entries = agents.get("entries")
    if not isinstance(entries, dict):
        return ""
    owner = entries.get("research-orchestrator")
    if isinstance(owner, dict):
        model = owner.get("model")
        if isinstance(model, dict):
            primary = model.get("primary")
            if isinstance(primary, str):
                return primary
    return ""


def _model_declared(config: JsonObject, provider: str, model_id: str) -> bool:
    models = _get_object(config, "models")
    providers = _get_object(models if models is not None else {}, "providers")
    provider_config = _get_object(providers if providers is not None else {}, provider)
    declared = provider_config.get("models") if provider_config is not None else None
    if not isinstance(declared, list):
        return False
    return any(isinstance(item, dict) and item.get("id") == model_id for item in declared)


def _is_approved_openai_model_route(model_ref: str) -> bool:
    return model_ref.startswith("openai/") or model_ref in OPENAI_ALTERNATE_MODEL_REFS


def _check_codex_routes(merged: JsonObject, inputs: AssemblyInputs) -> None:
    """Fail closed unless every native Codex route is declared and approved."""

    if not _model_declared(merged, inputs.model_provider, inputs.model_id):
        raise ConfigMergeError(
            f"ERROR: Selected model '{inputs.model_primary}' is not declared in repo config.\n"
            "       Add it to gateway/openclaw_config/openclaw.json or choose a configured model."
        )

    if not inputs.orchestrator_model_primary:
        raise ConfigMergeError(
            "ERROR: Repo config must pin agents.entries.research-orchestrator "
            '"research-orchestrator" to a model.primary.'
        )
    if not inputs.orchestrator_model_primary.startswith("openai/"):
        raise ConfigMergeError(
            f"ERROR: Research orchestrator model '{inputs.orchestrator_model_primary}' "
            "must use the OpenAI/Codex provider."
        )
    orchestrator_model_id = inputs.orchestrator_model_primary.removeprefix("openai/")
    if not _model_declared(merged, "openai", orchestrator_model_id):
        raise ConfigMergeError(
            f"ERROR: Research orchestrator model '{inputs.orchestrator_model_primary}' "
            "is not declared in repo config.\n"
            "       Add it to gateway/openclaw_config/openclaw.json before pushing."
        )
    if not _model_declared(merged, "openai", NATIVE_LUNA_MODEL_REF.removeprefix("openai/")):
        raise ConfigMergeError(
            f"ERROR: Native research model '{NATIVE_LUNA_MODEL_REF}' is not declared in "
            "repo config.\n"
            "       Add it to gateway/openclaw_config/openclaw.json before pushing."
        )
    if not _model_declared(merged, "openai", NATIVE_SOL_MODEL_REF.removeprefix("openai/")):
        raise ConfigMergeError(
            f"ERROR: Native research model '{NATIVE_SOL_MODEL_REF}' is not declared in "
            "repo config.\n"
            "       Add it to gateway/openclaw_config/openclaw.json before pushing."
        )
    if not _is_approved_openai_model_route(inputs.model_primary):
        raise ConfigMergeError(
            f"ERROR: Active model '{inputs.model_primary}' must be an approved OpenAI model "
            "route; refusing a provider fallback."
        )
    if not _is_approved_openai_model_route(inputs.orchestrator_model_primary):
        raise ConfigMergeError(
            f"ERROR: Research orchestrator model '{inputs.orchestrator_model_primary}' "
            "must be an approved OpenAI model route."
        )


def _profile_for(inputs: AssemblyInputs) -> AgentProfile:
    try:
        profile = inputs.profile if inputs.profile is not None else load_profile("codex")
    except AgentProfileError as exc:
        raise ConfigMergeError(f"ERROR: {exc}") from exc
    if profile.runtime == "codex":
        expected_provider = "openai"
    elif profile.runtime == CLAUDE_RUNTIME_ID:
        expected_provider = CLAUDE_PLUGIN_ID
    else:
        raise ConfigMergeError(f"ERROR: Unsupported agent profile runtime '{profile.runtime}'.")
    for role_name, spec in profile.roles.items():
        if spec.provider != expected_provider:
            raise ConfigMergeError(
                f"ERROR: Agent profile '{profile.name}' role '{role_name}' uses "
                f"'{spec.model_ref}'; the {profile.runtime} runtime only runs "
                f"{expected_provider}/* models."
            )
    return profile


def _check_claude_routes(profile: AgentProfile) -> None:
    """Claude models are not catalogued in models.providers; the profile is the contract."""

    for role_name, spec in profile.roles.items():
        if spec.effort is None:
            raise ConfigMergeError(
                f"ERROR: Agent profile '{profile.name}' role '{role_name}' must declare an effort."
            )


def profile_model_refs(profile: AgentProfile) -> list[str]:
    """Every distinct model ref of the profile, in role order."""

    refs: list[str] = []
    for role_name in ("main", "owner", "implementer", "runner", "reviewer"):
        ref = profile.role(role_name).model_ref
        if ref not in refs:
            refs.append(ref)
    return refs


def _apply_claude_plugins(merged: JsonObject, plugins: JsonObject, entries: JsonObject) -> None:
    """Enable the bundled anthropic plugin and keep the Codex/OpenAI surface out."""

    for stale in ("codex", "openai"):
        entries.pop(stale, None)
    # The claude surface is exactly anthropic plus the guard (added by _apply_guard_plugin).
    plugins["allow"] = [CLAUDE_PLUGIN_ID]
    deny = plugins.get("deny")
    if isinstance(deny, list):
        kept = [item for item in deny if item not in (CLAUDE_PLUGIN_ID, GUARD_PLUGIN_ID)]
        if kept:
            plugins["deny"] = kept
        else:
            plugins.pop("deny")
    if plugins.get("enabled") is False:
        plugins.pop("enabled")
    anthropic = entries.get(CLAUDE_PLUGIN_ID)
    if not isinstance(anthropic, dict):
        anthropic = {}
        entries[CLAUDE_PLUGIN_ID] = anthropic
    anthropic["enabled"] = True
    # The openai provider block pins agentRuntime codex; it has no role under claude.
    models = _get_object(merged, "models")
    providers = _get_object(models if models is not None else {}, "providers")
    if providers is not None:
        providers.pop("openai", None)


def _apply_guard_plugin(
    plugins: JsonObject,
    entries: JsonObject,
    profile: AgentProfile,
    paths: tuple[Path, Path, Path],
    research_root: str,
) -> None:
    """Load g2-agent-guard, the per-agent tool and CLI policy for Claude's native tools."""

    allow = cast(list[JsonValue], plugins.setdefault("allow", []))
    if GUARD_PLUGIN_ID not in allow:
        allow.append(GUARD_PLUGIN_ID)
    load = _object(plugins.setdefault("load", {}), "plugins.load")
    load["paths"] = [str(guard_plugin_dir())]
    entries[GUARD_PLUGIN_ID] = {
        "enabled": True,
        "config": expected_guard_config(profile, paths, research_root),
    }


def claude_child_workspace(state_root: str | Path, agent_id: str) -> str:
    """Dedicated workspace of one claude child agent (never the worktrees root)."""

    return str(Path(state_root) / f"workspace-{agent_id}")


def claude_child_entries(profile: AgentProfile, state_root: str | Path) -> dict[str, JsonValue]:
    """The OpenClaw child agents the claude-profile owner delegates to.

    Each child owns a dedicated workspace under the state root so OpenClaw bootstrap files
    are per role. The attempt checkout arrives as the spawn ``cwd``. There is deliberately
    no OpenClaw ``sandbox`` key: it cannot reach Claude's native tools.
    """

    if not Path(state_root).is_absolute():
        raise ConfigMergeError("ERROR: OpenClaw state root must be an absolute path.")
    children: dict[str, JsonValue] = {}
    for agent_id, role_name in CLAUDE_CHILD_AGENTS:
        spec = profile.role(role_name)
        children[agent_id] = {
            "workspace": claude_child_workspace(state_root, agent_id),
            "model": {"primary": spec.model_ref},
            "thinkingDefault": spec.effort,
            # tools.fs.workspaceOnly is deliberately omitted: it does not confine Claude's native
            # Write/Edit/Bash. The g2-agent-guard plugin does.
            "tools": {"profile": "full", "deny": cast(JsonValue, list(CLAUDE_CHILD_TOOL_DENY))},
        }
    return children


def _require_claude_paths(inputs: AssemblyInputs) -> tuple[Path, Path, Path]:
    named = (
        ("--owner-workspace-root", inputs.owner_workspace_root),
        ("--hypothesis-worktrees-root", inputs.hypothesis_worktrees_root),
        ("--gateway-cli", inputs.gateway_cli),
    )
    for flag, value in named:
        if not value or not Path(value).is_absolute():
            raise ConfigMergeError(
                f"ERROR: The claude profile requires {flag} as an absolute path."
            )
    return (
        Path(inputs.owner_workspace_root),
        Path(inputs.hypothesis_worktrees_root),
        Path(inputs.gateway_cli),
    )


def expected_guard_config(
    profile: AgentProfile, paths: tuple[Path, Path, Path], root: str
) -> JsonValue:
    owner_ws, worktrees, cli = paths
    return cast(
        JsonValue,
        guard_config(
            profile,
            owner_workspace_root=owner_ws,
            hypothesis_worktrees_root=worktrees,
            research_root=Path(root),
            gateway_cli=cli,
        ),
    )


def _assemble_config(local: JsonObject, repo: JsonObject, inputs: AssemblyInputs) -> JsonObject:
    """Apply every managed jq assembly operation to two decoded configs."""

    profile = _profile_for(inputs)
    is_codex = profile.runtime == "codex"
    merged = _object(deep_merge(local, repo), "merged config")
    merged.pop("mcp", None)

    mempalace_server: JsonObject = {
        "command": inputs.mempalace_python,
        "args": [inputs.mempalace_wrapper, "--palace", inputs.mempalace_palace],
    }
    g2_server: JsonObject = {
        "command": inputs.python_bin,
        "args": ["-m", inputs.g2_module],
    }
    if is_codex:
        # The codex.agents projection is a Codex app-server concept; the claude profile
        # has no equivalent key and keeps the servers without it.
        mempalace_server["codex"] = {
            "agents": list(inputs.mempalace_readonly_agents),
            "defaultToolsApprovalMode": "approve",
        }
        g2_server["codex"] = {
            "agents": list(inputs.g2_agents),
            "defaultToolsApprovalMode": "approve",
        }
    mempalace_server["env"] = {
        "FASTEMBED_CACHE_PATH": inputs.fastembed_cache,
        "MEMPALACE_EMBEDDING_MODEL": inputs.mempalace_embedding_model,
        "HF_HUB_OFFLINE": inputs.hf_hub_offline,
    }
    g2_server["env"] = {
        "PYTHONPATH": inputs.repo_root,
        "RESEARCH_V2_ROOT": inputs.research_v2_root,
        "RESEARCH_CORE_DATABASE": inputs.research_core_database,
        "OPENCLAW_HOST": inputs.openclaw_host,
        "OPENCLAW_PORT": inputs.openclaw_port,
        "G2_OWNER_ENV_FILE": inputs.owner_env_file,
    }
    merged["mcp"] = {"servers": {"mempalace-readonly": mempalace_server, "g2-control": g2_server}}

    for key in ("tools", "memory"):
        value = repo.get(key)
        if _jq_truthy(value):
            merged[key] = _copy(value)

    defaults = _get_object(_get_object(merged, "agents") or {}, "defaults")
    repo_defaults = _get_object(_get_object(repo, "agents") or {}, "defaults")
    if defaults is None:
        defaults = {}
        merged.setdefault("agents", {})
        agents = _object(merged["agents"], "merged agents")
        agents["defaults"] = defaults
    if repo_defaults is not None:
        compaction = _get_object(repo_defaults, "compaction")
        memory_flush = compaction.get("memoryFlush") if compaction is not None else None
        if _jq_truthy(memory_flush):
            compaction_target = _get_object(defaults, "compaction")
            if compaction_target is None:
                compaction_target = {}
                defaults["compaction"] = compaction_target
            compaction_target["memoryFlush"] = _copy(memory_flush)

    repo_agents = _get_object(repo, "agents")
    repo_entries = repo_agents.get("entries") if repo_agents is not None else None
    if not isinstance(repo_entries, dict) or not repo_entries:
        raise ConfigMergeError("ERROR: Repo config must define a non-empty agents.entries map.")
    if any(not isinstance(agent_id, str) for agent_id in repo_entries):
        raise ConfigMergeError("ERROR: Repo config agents.entries keys must be strings.")
    if any(not isinstance(agent, dict) or "id" in agent for agent in repo_entries.values()):
        raise ConfigMergeError(
            "ERROR: Repo config agents.entries values must be objects without authored id fields."
        )
    agents = _object(merged.setdefault("agents", {}), "merged agents")
    # The managed roster is authoritative. Discard a machine-local legacy list
    # once while replacing it with the canonical keyed entries map.
    agents.pop("list", None)
    agents["ownership"] = "explicit"
    agents["entries"] = _copy(repo_entries)

    models = _object(merged.setdefault("models", {}), "merged models")
    providers = _object(models.setdefault("providers", {}), "merged model providers")
    if inputs.provider == "openrouter" and os.environ.get("OPENROUTER_API_KEY", ""):
        for provider in providers.values():
            if isinstance(provider, dict) and provider.get("apiKey") == "env:OPENROUTER_API_KEY":
                provider["apiKey"] = os.environ["OPENROUTER_API_KEY"]

    openai_provider = providers.get("openai")
    if isinstance(openai_provider, dict) and openai_provider.get("auth") == "oauth":
        # The managed OpenAI route is subscription-backed Codex OAuth.  Do not
        # let a machine-local API key survive the right-biased config merge and
        # turn this route into a mixed credential surface.
        openai_provider.pop("apiKey", None)

    azure_key: JsonValue | None = None
    for key, value in providers.items():
        if not key.startswith("azure-oai-") or not isinstance(value, dict):
            continue
        candidate = value.get("apiKey")
        if candidate is not None and candidate != "":
            azure_key = candidate
            break
    if azure_key is not None:
        for key, value in providers.items():
            if not key.startswith("azure-oai-") or not isinstance(value, dict):
                continue
            if value.get("apiKey") is None or value.get("apiKey") == "":
                value["apiKey"] = _copy(azure_key)

    if is_codex:
        _check_codex_routes(merged, inputs)
    else:
        _check_claude_routes(profile)

    defaults["model"] = _get_object(defaults, "model") or {}
    if is_codex:
        cast(JsonObject, defaults["model"])["primary"] = inputs.model_primary
        model_policy_allow: list[JsonValue] = []
        for model_ref in (
            inputs.model_primary,
            inputs.orchestrator_model_primary,
            NATIVE_LUNA_MODEL_REF,
            NATIVE_SOL_MODEL_REF,
        ):
            if model_ref not in model_policy_allow:
                model_policy_allow.append(model_ref)
        defaults["modelPolicy"] = {"allow": model_policy_allow}
        # OpenClaw 8.1 treats defaults.models as a legacy override restriction until
        # it is migrated.  The native policy above is authoritative, so retaining
        # the legacy map would reintroduce migration and stale fallback semantics.
        defaults.pop("models", None)
    else:
        profile_refs = profile_model_refs(profile)
        cast(JsonObject, defaults["model"])["primary"] = profile.role("main").model_ref
        defaults["modelPolicy"] = {"allow": cast(JsonValue, list(profile_refs))}
        # The claude-cli runtime is selected per model, not per provider.
        defaults["models"] = {
            ref: {"agentRuntime": {"id": CLAUDE_RUNTIME_ID}} for ref in profile_refs
        }

    agents = _object(merged.get("agents"), "merged agents")
    managed_entries = _get_object(agents, "entries")
    if managed_entries is None:
        raise ConfigMergeError("ERROR: Assembled config lost the canonical agents.entries map.")
    owner_entry = managed_entries.get("research-orchestrator")
    if not isinstance(owner_entry, dict):
        raise ConfigMergeError(
            "ERROR: Assembled config must contain agents.entries.research-orchestrator."
        )
    model = owner_entry.get("model")
    if not isinstance(model, dict):
        model = {}
        owner_entry["model"] = model
    owner_spec = profile.role("owner")
    model["primary"] = inputs.orchestrator_model_primary if is_codex else owner_spec.model_ref
    owner_entry["thinkingDefault"] = owner_spec.effort or "high"
    owner_workspace = owner_entry.get("workspace")
    if not isinstance(owner_workspace, str) or not owner_workspace:
        owner_workspace = "workspace-research-orchestrator"
    state_root = Path(inputs.openclaw_state_root)
    if not state_root.is_absolute():
        raise ConfigMergeError("ERROR: OpenClaw state root must be an absolute path.")
    owner_entry["workspace"] = str(
        Path(owner_workspace)
        if Path(owner_workspace).is_absolute()
        else state_root / owner_workspace
    )
    # The native Codex harness treats finite owner allowlists and unsafe denies
    # as a restricted tool policy and disables multi-agent delegation. Keep the
    # owner on the full native profile; MCP exposure remains bounded by each
    # server's explicit agent projection below.
    owner_tools = owner_entry.get("tools")
    if not isinstance(owner_tools, dict):
        owner_tools = {}
        owner_entry["tools"] = owner_tools
    owner_tools["profile"] = "full"
    owner_tools.pop("allow", None)
    owner_tools.pop("deny", None)
    if not is_codex:
        owner_tools["deny"] = cast(JsonValue, list(CLAUDE_MCP_DENY))
        if Path(str(owner_entry["workspace"])) != Path(inputs.owner_workspace_root or "/"):
            raise ConfigMergeError(
                f"ERROR: Owner workspace '{owner_entry['workspace']}' differs from "
                f"--owner-workspace-root '{inputs.owner_workspace_root}'."
            )

    if not is_codex:
        main_entry = managed_entries.get("main")
        if not isinstance(main_entry, dict):
            raise ConfigMergeError("ERROR: Assembled config must contain agents.entries.main.")
        main_spec = profile.role("main")
        main_model = main_entry.get("model")
        if not isinstance(main_model, dict):
            main_model = {}
            main_entry["model"] = main_model
        main_model["primary"] = main_spec.model_ref
        main_entry["thinkingDefault"] = main_spec.effort
        children = claude_child_entries(profile, state_root)
        owner_subagents = owner_entry.get("subagents")
        if not isinstance(owner_subagents, dict):
            owner_subagents = {}
            owner_entry["subagents"] = owner_subagents
        owner_subagents["allowAgents"] = cast(JsonValue, list(CLAUDE_CHILD_AGENT_IDS))
        for child_id, child_entry in children.items():
            managed_entries[child_id] = child_entry

    merged = _object(
        _walk_remove_keys(
            merged,
            STALE_CODING_PROVIDER_KEYS,
        ),
        "sanitized merged config",
    )
    plugins = _get_object(merged, "plugins")
    if plugins is not None:
        # Plugin load paths are machine-local source overrides. Remove them so
        # stale linked packages cannot survive the managed merge.
        plugin_load = _get_object(plugins, "load")
        if plugin_load is not None:
            plugin_load.pop("paths", None)
            if not plugin_load:
                plugins.pop("load", None)
    entries = _get_object(plugins if plugins is not None else {}, "entries")
    codex = _get_object(entries if entries is not None else {}, "codex")
    codex_config = _get_object(codex if codex is not None else {}, "config")
    if codex_config is not None:
        codex_config.pop("codexDynamicToolsExclude", None)
        codex_config.pop("nativeToolSurfaceEnabled", None)
        app_server = _get_object(codex_config, "appServer")
        if app_server is not None and app_server.get("defaultWorkspaceDir") == (
            "/home/dev/.openclaw/autoresearch/model-workspaces"
        ):
            app_server.pop("defaultWorkspaceDir", None)
    # The active reviewer is a native Codex role. Remove inherited dispatch
    # surfaces instead of allowing stale machine-local wiring to survive.
    merged.pop("acp", None)
    plugins = _object(merged.setdefault("plugins", {}), "merged plugins")
    entries = _object(plugins.setdefault("entries", {}), "merged plugin entries")
    entries.pop("acpx", None)
    plugins["allow"] = [
        plugin for plugin in cast(list[JsonValue], plugins.get("allow", [])) if plugin != "acpx"
    ]
    if not plugins["allow"]:
        plugins.pop("allow", None)
    if not is_codex:
        _apply_claude_plugins(merged, plugins, entries)
        guard_paths = _require_claude_paths(inputs)
        _apply_guard_plugin(plugins, entries, profile, guard_paths, inputs.research_v2_root)
    return merged


@dataclass(frozen=True)
class ValidationExpectations:
    """Resolved push values the assembled config must reproduce exactly."""

    repo_root: str
    python_bin: str
    mempalace_python: str
    mempalace_palace: str
    mempalace_wrapper: str
    fastembed_cache: str
    mempalace_embedding_model: str
    hf_hub_offline: str
    g2_module: str
    research_v2_root: str
    research_core_database: str
    owner_env_file: str
    openclaw_host: str
    openclaw_port: str
    readonly_server_agents: tuple[str, ...]
    g2_server_agents: tuple[str, ...]
    main_tool_allow: tuple[str, ...]
    hypothesis_worktrees_root: str = ""
    openclaw_state_root: str = ""
    owner_workspace_root: str = ""
    gateway_cli: str = ""
    # Codex only: the repo-pinned owner model; defaults to the profile owner model.
    owner_model_primary: str = ""


def _at(root: object, *path: str) -> object:
    """jq-style optional path read: any missing step yields None."""

    current = root
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _alt(value: object, default: object) -> object:
    """jq's `//` operator."""

    return default if value is None or value is False else value


def _str_list(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return cast(list[str], value)
    return None


def _num_is(value: object, expected: int) -> bool:
    """jq-style numeric equality that does not let a boolean pass as a number."""

    return isinstance(value, int | float) and not isinstance(value, bool) and value == expected


def _has_all(value: object, required: Sequence[str]) -> bool:
    items = _str_list(_alt(value, []))
    return items is not None and all(item in items for item in required)


def _mcp_violations(
    config: dict[str, object], expect: ValidationExpectations, codex: bool
) -> list[str]:
    problems: list[str] = []
    servers = _at(config, "mcp", "servers")
    if not isinstance(servers, dict) or sorted(servers) != ["g2-control", "mempalace-readonly"]:
        return ["mcp.servers must be exactly g2-control and mempalace-readonly"]
    mempalace = servers["mempalace-readonly"]
    g2_control = servers["g2-control"]
    if _at(mempalace, "command") != expect.mempalace_python:
        problems.append("mempalace-readonly command is not the MemPalace venv python")
    if _at(mempalace, "args") != [expect.mempalace_wrapper, "--palace", expect.mempalace_palace]:
        problems.append("mempalace-readonly args do not bind the wrapper and palace")
    if _at(mempalace, "env") != {
        "FASTEMBED_CACHE_PATH": expect.fastembed_cache,
        "MEMPALACE_EMBEDDING_MODEL": expect.mempalace_embedding_model,
        "HF_HUB_OFFLINE": expect.hf_hub_offline,
    }:
        problems.append("mempalace-readonly env is not the managed embedding contract")
    if _at(g2_control, "command") != expect.python_bin:
        problems.append("g2-control command is not the repo python")
    if _at(g2_control, "args") != ["-m", expect.g2_module]:
        problems.append("g2-control args are not the managed module")
    if _at(g2_control, "env") != {
        "PYTHONPATH": expect.repo_root,
        "RESEARCH_V2_ROOT": expect.research_v2_root,
        "RESEARCH_CORE_DATABASE": expect.research_core_database,
        "OPENCLAW_HOST": expect.openclaw_host,
        "OPENCLAW_PORT": expect.openclaw_port,
        "G2_OWNER_ENV_FILE": expect.owner_env_file,
    }:
        problems.append("g2-control env is not the managed bounded contract")
    if codex:
        if _alt(_at(mempalace, "codex", "agents"), []) != list(expect.readonly_server_agents):
            problems.append("mempalace-readonly codex.agents projection drifted")
        if _alt(_at(g2_control, "codex", "agents"), []) != list(expect.g2_server_agents):
            problems.append("g2-control codex.agents projection drifted")
        if _at(g2_control, "codex", "defaultToolsApprovalMode") != "approve":
            problems.append("g2-control codex.defaultToolsApprovalMode must be approve")
    else:
        for name in ("mempalace-readonly", "g2-control"):
            if isinstance(servers[name], dict) and "codex" in servers[name]:
                problems.append(f"{name} must not carry a codex.agents projection under claude")
    return problems


def _main_entry_violations(
    entry: object, expect: ValidationExpectations, main_model: str, main_effort: str | None
) -> list[str]:
    problems: list[str] = []
    allow = _alt(_at(entry, "tools", "allow"), [])
    deny = _alt(_at(entry, "tools", "deny"), [])
    if not (
        _at(entry, "tools", "profile") == "minimal"
        and allow == list(expect.main_tool_allow)
        and _has_all(deny, MAIN_TOOL_DENY_IDS)
    ):
        problems.append("main tools must keep the minimal profile, bounded allow list, and denies")
    skills = _alt(_at(entry, "skills"), [])
    if not (
        _at(entry, "model", "primary") == main_model
        and _at(entry, "thinkingDefault") == main_effort
        and skills == ["mempalace-readonly"]
        and _alt(_at(entry, "subagents", "allowAgents"), []) == []
    ):
        problems.append("main must use the profile model/effort, mempalace-readonly, no subagents")
    return problems


def validate_config(
    config: object, profile: AgentProfile, expect: ValidationExpectations
) -> list[str]:
    """Return every violated managed-route invariant (empty means valid).

    Replaces the hard-coded jq program of scripts/push-openclaw-config.sh; the codex
    profile enforces the same invariants and the claude profile their equivalents.
    """

    if not isinstance(config, dict):
        return ["config must be a JSON object"]
    codex = profile.runtime == "codex"
    problems: list[str] = []
    agents = _at(config, "agents")
    entries = _at(config, "agents", "entries")
    defaults = _at(config, "agents", "defaults")

    if codex and _at(defaults, "thinkingDefault") != "high":
        problems.append("agents.defaults.thinkingDefault must be high")
    if _at(agents, "ownership") != "explicit":
        problems.append("agents.ownership must be explicit")
    if isinstance(agents, dict) and "list" in agents:
        problems.append("agents.list must not exist; the roster is agents.entries")
    if not isinstance(entries, dict):
        problems.append("agents.entries must be an object")
        entries = {}
    entries = cast(dict[str, object], entries)
    for agent_id, entry in entries.items():
        if not isinstance(entry, dict) or "id" in entry:
            problems.append(f"agents.entries.{agent_id} must be an object without an id field")

    plugins_allow = _alt(_at(config, "plugins", "allow"), [])
    plugin_entries = _at(config, "plugins", "entries")
    if "acpx" in cast(list[object], plugins_allow if isinstance(plugins_allow, list) else []):
        problems.append("plugins.allow must not contain acpx")
    if codex and _at(config, "plugins", "load", "paths") is not None:
        problems.append("plugins.load.paths must not exist")
    if _at(plugin_entries, "acpx") is not None:
        problems.append("plugins.entries.acpx must not exist")
    if _at(config, "acp") is not None:
        problems.append("acp must not exist")
    if codex:
        codex_config = _at(plugin_entries, "codex", "config")
        if not _has_all(plugins_allow, ["codex", "openai"]):
            problems.append("plugins.allow must contain codex and openai")
        if _at(plugin_entries, "codex", "enabled") is not True:
            problems.append("plugins.entries.codex.enabled must be true")
        if _at(codex_config, "nativeToolSurfaceEnabled") is not None:
            problems.append("codex nativeToolSurfaceEnabled must be absent")
        if _at(codex_config, "codexDynamicToolsExclude") is not None:
            problems.append("codex codexDynamicToolsExclude must be absent")
        sandbox = _at(codex_config, "appServer", "sandbox")
        if sandbox != "workspace-write":
            problems.append("codex appServer.sandbox must be workspace-write")
        if _at(codex_config, "appServer", "defaultWorkspaceDir") == (
            "/home/dev/.openclaw/autoresearch/model-workspaces"
        ):
            problems.append("codex appServer.defaultWorkspaceDir must not be the retired root")
        if _at(codex_config, "appServer", "networkProxy") is not None:
            problems.append("codex appServer.networkProxy must be absent")
    else:
        if not _has_all(plugins_allow, [CLAUDE_PLUGIN_ID]):
            problems.append("plugins.allow must contain anthropic")
        if any(
            plugin in ("codex", "openai")
            for plugin in cast(
                list[object], plugins_allow if isinstance(plugins_allow, list) else []
            )
        ):
            problems.append("plugins.allow must not contain codex or openai under claude")
        managed = {CLAUDE_PLUGIN_ID, GUARD_PLUGIN_ID}
        if not (
            isinstance(plugins_allow, list)
            and len(plugins_allow) == len(managed)
            and all(isinstance(item, str) and item in managed for item in plugins_allow)
            and len(set(plugins_allow)) == len(managed)
        ):
            problems.append(
                f"plugins.allow must be exactly {CLAUDE_PLUGIN_ID} and {GUARD_PLUGIN_ID} "
                "under claude"
            )
        plugins_deny = _at(config, "plugins", "deny")
        if isinstance(plugins_deny, list) and any(
            item in (CLAUDE_PLUGIN_ID, GUARD_PLUGIN_ID) for item in plugins_deny
        ):
            problems.append("plugins.deny must not contain anthropic or g2-agent-guard")
        elif plugins_deny is not None and not isinstance(plugins_deny, list):
            problems.append("plugins.deny must be a list")
        if _at(config, "plugins", "enabled") is False:
            problems.append("plugins.enabled must not be false")
        if _at(plugin_entries, CLAUDE_PLUGIN_ID, "enabled") is not True:
            problems.append("plugins.entries.anthropic.enabled must be true")
        for stale in ("codex", "openai"):
            if _at(plugin_entries, stale) is not None:
                problems.append(f"plugins.entries.{stale} must not exist under claude")

    if not _num_is(_at(defaults, "maxConcurrent"), 2):
        problems.append("agents.defaults.maxConcurrent must be 2")
    if not _num_is(_at(defaults, "subagents", "maxConcurrent"), 1):
        problems.append("agents.defaults.subagents.maxConcurrent must be 1")
    if not _num_is(_at(defaults, "subagents", "maxSpawnDepth"), 1):
        problems.append("agents.defaults.subagents.maxSpawnDepth must be 1")
    if not _num_is(_at(defaults, "subagents", "runTimeoutSeconds"), 1800):
        problems.append("agents.defaults.subagents.runTimeoutSeconds must be 1800")
    if _at(config, "memory", "search", "enabled") is not False:
        problems.append("memory.search.enabled must be false")
    if _at(defaults, "compaction", "mode") != "default":
        problems.append("agents.defaults.compaction.mode must be default")
    if _at(defaults, "compaction", "memoryFlush", "enabled") is not False:
        problems.append("agents.defaults.compaction.memoryFlush.enabled must be false")
    if not _has_all(_at(config, "tools", "deny"), ["memory_search", "memory_get"]):
        problems.append("tools.deny must contain memory_search and memory_get")
    problems.extend(_mcp_violations(config, expect, codex))

    owner_spec = profile.role("owner")
    main_spec = profile.role("main")
    owner_model = (
        (expect.owner_model_primary or owner_spec.model_ref) if codex else owner_spec.model_ref
    )
    expected_models: dict[str, tuple[str, str | None]] = {
        "main": (main_spec.model_ref, main_spec.effort),
        OWNER_AGENT_ID: (owner_model, owner_spec.effort),
    }
    if not codex:
        for agent_id, role_name in CLAUDE_CHILD_AGENTS:
            spec = profile.role(role_name)
            expected_models[agent_id] = (spec.model_ref, spec.effort)
    if sorted(entries) != sorted(expected_models):
        problems.append(
            f"agents.entries must be exactly {sorted(expected_models)}, got {sorted(entries)}"
        )
    for agent_id, (model_ref, effort) in expected_models.items():
        entry = entries.get(agent_id)
        if entry is None:
            continue
        if not codex and isinstance(entry, dict):
            for forbidden in ("models", "agentRuntime"):
                if forbidden in entry:
                    problems.append(
                        f"agents.entries.{agent_id} must not carry {forbidden}; "
                        "the runtime is selected in agents.defaults.models"
                    )
        if _at(entry, "model", "primary") != model_ref or _at(entry, "thinkingDefault") != effort:
            problems.append(f"agents.entries.{agent_id} must use {model_ref} at effort {effort}")
        if not model_ref.startswith(f"{profile.provider}/"):
            problems.append(f"agents.entries.{agent_id} model is not a {profile.provider} route")

    problems.extend(
        _main_entry_violations(entries.get("main"), expect, main_spec.model_ref, main_spec.effort)
    )
    owner = entries.get(OWNER_AGENT_ID)
    allow_agents = _alt(_at(owner, "subagents", "allowAgents"), [])
    expected_allow = [] if codex else list(CLAUDE_CHILD_AGENT_IDS)
    if not (
        _at(owner, "skills") == ["research-loop"]
        and _at(owner, "tools") == (CLAUDE_OWNER_TOOLS if not codex else {"profile": "full"})
        and allow_agents == expected_allow
    ):
        problems.append(
            "research-orchestrator must keep the research-loop skill, the native full tool "
            "profile without local filters (claude: only the managed MCP server denies), "
            "and the profile's subagent allow list"
        )

    if not codex:
        problems.extend(_claude_violations(config, defaults, entries, profile, expect))
    return problems


def _guard_violations(
    config: object, profile: AgentProfile, expect: ValidationExpectations
) -> list[str]:
    plugins_allow = _alt(_at(config, "plugins", "allow"), [])
    if not _has_all(plugins_allow, [GUARD_PLUGIN_ID]):
        return [f"plugins.allow must contain {GUARD_PLUGIN_ID}"]
    problems: list[str] = []
    if _at(config, "plugins", "load", "paths") != [str(guard_plugin_dir())]:
        problems.append(f"plugins.load.paths must be exactly [{guard_plugin_dir()}]")
    entry = _at(config, "plugins", "entries", GUARD_PLUGIN_ID)
    if _at(entry, "enabled") is not True:
        problems.append(f"plugins.entries.{GUARD_PLUGIN_ID}.enabled must be true")
    paths = (expect.owner_workspace_root, expect.hypothesis_worktrees_root, expect.gateway_cli)
    if not all(path and Path(path).is_absolute() for path in paths):
        problems.append(
            "validate needs absolute --owner-workspace-root, --hypothesis-worktrees-root and "
            "--gateway-cli to check the guard config"
        )
        return problems
    if not Path(expect.research_v2_root).is_absolute():
        problems.append("--research-v2-root must be absolute to check the guard config")
        return problems
    if paths[2] != str(Path(expect.repo_root) / ".venv/bin/gateway-cli"):
        problems.append("--gateway-cli must be <repo root>/.venv/bin/gateway-cli")
    expected = expected_guard_config(
        profile,
        (Path(paths[0]), Path(paths[1]), Path(paths[2])),
        expect.research_v2_root,
    )
    if _at(entry, "config") != expected:
        problems.append(
            f"plugins.entries.{GUARD_PLUGIN_ID}.config does not match the profile guard config"
        )
    return problems


def _claude_violations(
    config: object,
    defaults: object,
    entries: dict[str, object],
    profile: AgentProfile,
    expect: ValidationExpectations,
) -> list[str]:
    problems: list[str] = []
    refs = profile_model_refs(profile)
    if _at(defaults, "model", "primary") != profile.role("main").model_ref:
        problems.append("agents.defaults.model.primary must be the profile main model")
    if _at(defaults, "modelPolicy", "allow") != refs:
        problems.append("agents.defaults.modelPolicy.allow must list exactly the profile models")
    models = _at(defaults, "models")
    if not isinstance(models, dict) or sorted(models) != sorted(refs):
        problems.append("agents.defaults.models must list exactly the profile models")
    else:
        for ref in refs:
            if _at(models, ref, "agentRuntime", "id") != CLAUDE_RUNTIME_ID:
                problems.append(f"agents.defaults.models.{ref}.agentRuntime.id must be claude-cli")
    state_root = expect.openclaw_state_root
    if not state_root or not Path(state_root).is_absolute():
        problems.append("validate needs an absolute --openclaw-state-root under claude")
        state_root = ""
    if expect.owner_workspace_root and (
        _at(entries.get(OWNER_AGENT_ID), "workspace") != expect.owner_workspace_root
    ):
        problems.append(
            f"agents.entries.{OWNER_AGENT_ID}.workspace must be {expect.owner_workspace_root}"
        )
    for agent_id, _role_name in CLAUDE_CHILD_AGENTS:
        child = entries.get(agent_id)
        if child is None:
            continue
        workspace = _at(child, "workspace")
        if state_root and workspace != claude_child_workspace(state_root, agent_id):
            problems.append(
                f"agents.entries.{agent_id}.workspace must be the dedicated "
                f"{claude_child_workspace(state_root, agent_id)}"
            )
        if expect.hypothesis_worktrees_root and workspace == expect.hypothesis_worktrees_root:
            problems.append(
                f"agents.entries.{agent_id}.workspace must not be the hypothesis worktrees root"
            )
        if isinstance(child, dict) and "sandbox" in child:
            problems.append(f"agents.entries.{agent_id} must not carry an OpenClaw sandbox key")
        if (
            _at(child, "tools", "profile") != "full"
            or _str_list(_at(child, "tools", "deny")) != list(CLAUDE_CHILD_TOOL_DENY)
            or _at(child, "tools", "allow") is not None
            or _at(child, "skills") is not None
            or _at(child, "subagents") is not None
        ):
            problems.append(
                f"agents.entries.{agent_id} must be a leaf agent: full tool profile, "
                "orchestration tools and the managed MCP servers denied, no skills or subagents"
            )
    problems.extend(_guard_violations(config, profile, expect))
    return problems


def assemble_config_with_migration(
    local: JsonObject, repo: JsonObject, inputs: AssemblyInputs
) -> tuple[JsonObject, JsonObject]:
    """Assemble a candidate, then apply the one-time 8.1 schema migration."""

    memory_search_mapping = _legacy_memory_search_mapping(local, repo)
    memory_backend_present, memory_backend = _read_path(local, ("memory", "backend"))
    memory_backend_record = (
        {
            "path": "memory.backend",
            "value": _copy(memory_backend),
            "source": "local_pre_overlay",
        }
        if memory_backend_present
        else None
    )
    return migrate_8_1_config(
        _assemble_config(local, repo, inputs),
        memory_search_mapping=memory_search_mapping,
        memory_backend_record=memory_backend_record,
    )


def assemble_config(local: JsonObject, repo: JsonObject, inputs: AssemblyInputs) -> JsonObject:
    """Assemble and migrate a candidate while preserving the legacy return contract."""

    config, _ = assemble_config_with_migration(local, repo, inputs)
    return config


def _json_object_arg(raw: str, name: str, *, array: bool = False) -> JsonObject | list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigMergeError(f"invalid {name} JSON: {exc}") from exc
    if array:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigMergeError(f"{name} must be a JSON string array")
        return cast(list[str], value)
    if not isinstance(value, dict):
        raise ConfigMergeError(f"{name} must be a JSON object")
    return cast(JsonObject, value)


def _load_profile_arg(name: str | None) -> AgentProfile:
    # An omitted --profile is the codex profile (never the environment) so that callers
    # predating profiles keep their exact assembly.
    try:
        return load_profile(name or "codex")
    except AgentProfileError as exc:
        raise ConfigMergeError(f"ERROR: {exc}") from exc


def _assemble_from_files(args: argparse.Namespace) -> bytes:
    local = _object(load_json(args.local_config), "local config")
    repo = _object(load_json(args.repo_config), "repo config")
    readonly_agents = _json_object_arg(args.readonly_agents_json, "readonly agents", array=True)
    g2_agents = _json_object_arg(args.g2_agents_json, "g2 agents", array=True)
    profile = _load_profile_arg(getattr(args, "profile", None))
    if profile.runtime == "codex":
        provider = os.environ.get("OPENCLAW_PROVIDER") or "codex"
        model_primary, model_provider, model_id = _provider_selection(
            provider,
            os.environ.get("OPENAI_MODEL", "gpt-5.4"),
            os.environ.get("OPENROUTER_MODEL", ""),
        )
        orchestrator_primary = orchestrator_model_primary(repo)
    else:
        ignored = os.environ.get("OPENCLAW_PROVIDER", "")
        if ignored:
            print(
                f"WARNING: OPENCLAW_PROVIDER={ignored} is ignored under the {profile.name} "
                "agent profile; the profile selects every model.",
                file=sys.stderr,
            )
        provider = profile.name
        model_primary = profile.role("main").model_ref
        model_provider = profile.role("main").provider
        model_id = profile.role("main").model_id
        orchestrator_primary = profile.role("owner").model_ref
    owner_env_file = os.environ.get("G2_OWNER_ENV_FILE", "")
    research_core_database = os.environ.get("RESEARCH_CORE_DATABASE", "")
    openclaw_host = os.environ.get("OPENCLAW_HOST", "127.0.0.1")
    openclaw_port = os.environ.get("OPENCLAW_PORT", "18789")
    if not owner_env_file:
        raise ConfigMergeError("G2_OWNER_ENV_FILE is required; refusing an unbound MCP env")
    if not research_core_database:
        raise ConfigMergeError(
            "RESEARCH_CORE_DATABASE is required; refusing an unbound MCP database"
        )
    owner_env_file = _validate_contract_file(
        owner_env_file, private=True, label="G2_OWNER_ENV_FILE"
    )
    research_core_database = _validate_contract_file(
        research_core_database, private=False, label="RESEARCH_CORE_DATABASE"
    )
    if openclaw_host != "127.0.0.1":
        raise ConfigMergeError("OPENCLAW_HOST must be the loopback address 127.0.0.1")
    try:
        port = int(openclaw_port)
    except ValueError as exc:
        raise ConfigMergeError("OPENCLAW_PORT must be a decimal TCP port") from exc
    if not 1 <= port <= 65535 or str(port) != openclaw_port:
        raise ConfigMergeError("OPENCLAW_PORT must be a canonical port in range 1..65535")
    inputs = AssemblyInputs(
        repo_root=args.repo_root,
        openclaw_state_root=args.openclaw_state_root,
        python_bin=args.python_bin,
        mempalace_python=args.mempalace_python,
        mempalace_palace=args.mempalace_palace,
        mempalace_wrapper=args.mempalace_wrapper,
        fastembed_cache=args.fastembed_cache,
        mempalace_embedding_model=args.mempalace_embedding_model,
        hf_hub_offline=args.hf_hub_offline,
        g2_module=args.g2_module,
        research_v2_root=args.research_v2_root,
        research_core_database=research_core_database,
        owner_env_file=owner_env_file,
        openclaw_host=openclaw_host,
        openclaw_port=openclaw_port,
        mempalace_readonly_agents=tuple(cast(list[str], readonly_agents)),
        g2_agents=tuple(cast(list[str], g2_agents)),
        provider=provider,
        model_primary=model_primary,
        model_provider=model_provider,
        model_id=model_id,
        orchestrator_model_primary=orchestrator_primary,
        profile=profile,
        hypothesis_worktrees_root=getattr(args, "hypothesis_worktrees_root", None) or "",
        owner_workspace_root=getattr(args, "owner_workspace_root", None) or "",
        gateway_cli=getattr(args, "gateway_cli", None) or "",
    )
    config, migration_record = assemble_config_with_migration(local, repo, inputs)
    migration_record_path = getattr(args, "migration_record", None)
    if migration_record_path is not None:
        write_migration_record(migration_record_path, migration_record)
    return serialize_json(config)


_VALIDATE_STRING_FLAGS = (
    "repo_root",
    "python_bin",
    "mempalace_python",
    "mempalace_palace",
    "mempalace_wrapper",
    "fastembed_cache",
    "mempalace_embedding_model",
    "hf_hub_offline",
    "g2_module",
    "research_v2_root",
    "research_core_database",
    "owner_env_file",
    "openclaw_host",
    "openclaw_port",
)
_VALIDATE_JSON_FLAGS = ("readonly_server_agents", "g2_server_agents", "main_tool_allow")


def _validate_from_args(args: argparse.Namespace) -> list[str]:
    profile = _load_profile_arg(args.profile)
    if args.config == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(args.config).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigMergeError(f"could not read config {args.config}: {exc}") from exc
    try:
        config = json.loads(raw)
    except ValueError as exc:
        raise ConfigMergeError(f"config is not valid JSON: {exc}") from exc
    lists: dict[str, tuple[str, ...]] = {}
    for name in _VALIDATE_JSON_FLAGS:
        parsed = _json_object_arg(getattr(args, f"{name}_json"), name.replace("_", " "), array=True)
        lists[name] = tuple(cast(list[str], parsed))
    expect = ValidationExpectations(
        **{name: getattr(args, name) for name in _VALIDATE_STRING_FLAGS},
        readonly_server_agents=lists["readonly_server_agents"],
        g2_server_agents=lists["g2_server_agents"],
        main_tool_allow=lists["main_tool_allow"],
        hypothesis_worktrees_root=args.hypothesis_worktrees_root,
        openclaw_state_root=args.openclaw_state_root,
        owner_workspace_root=args.owner_workspace_root,
        gateway_cli=args.gateway_cli,
        owner_model_primary=args.owner_model_primary,
    )
    return validate_config(config, profile, expect)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("local_config")
    assemble.add_argument("repo_config")
    assemble.add_argument("repo_root")
    assemble.add_argument("openclaw_state_root")
    assemble.add_argument("python_bin")
    assemble.add_argument("mempalace_python")
    assemble.add_argument("mempalace_palace")
    assemble.add_argument("mempalace_wrapper")
    assemble.add_argument("fastembed_cache")
    assemble.add_argument("mempalace_embedding_model")
    assemble.add_argument("hf_hub_offline")
    assemble.add_argument("g2_module")
    assemble.add_argument("research_v2_root")
    assemble.add_argument("readonly_agents_json")
    assemble.add_argument("g2_agents_json")
    assemble.add_argument(
        "--migration-record",
        type=Path,
        help="write the private 8.1 schema migration record with mode 0600",
    )
    assemble.add_argument(
        "--profile",
        help="agent profile (codex or claude); defaults to codex, never the environment",
    )
    assemble.add_argument(
        "--hypothesis-worktrees-root",
        help="hypothesis worktrees root fed to the guard plugin (required under claude)",
    )
    assemble.add_argument(
        "--owner-workspace-root", help="owner agent workspace for the guard (required under claude)"
    )
    assemble.add_argument(
        "--gateway-cli", help="absolute gateway-cli path for the guard (required under claude)"
    )

    validate = subparsers.add_parser(
        "validate", help="check an assembled config against the managed route invariants"
    )
    validate.add_argument("--profile", required=True)
    validate.add_argument("--config", required=True, help="config file, or - for stdin")
    for name in _VALIDATE_STRING_FLAGS:
        validate.add_argument(f"--{name.replace('_', '-')}", required=True, dest=name)
    validate.add_argument("--hypothesis-worktrees-root", default="")
    validate.add_argument("--owner-workspace-root", default="")
    validate.add_argument("--gateway-cli", default="")
    validate.add_argument("--openclaw-state-root", default="")
    validate.add_argument("--owner-model-primary", default="")
    for name in _VALIDATE_JSON_FLAGS:
        validate.add_argument(
            f"--{name.replace('_', '-')}-json", required=True, dest=f"{name}_json"
        )

    orchestrator_parser = subparsers.add_parser("orchestrator-model")
    orchestrator_parser.add_argument("repo_config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "assemble":
            sys.stdout.buffer.write(_assemble_from_files(args))
        elif args.command == "validate":
            problems = _validate_from_args(args)
            if problems:
                for problem in problems:
                    print(f"INVARIANT: {problem}", file=sys.stderr)
                return 1
        elif args.command == "orchestrator-model":
            repo = _object(load_json(args.repo_config), "repo config")
            primary = orchestrator_model_primary(repo)
            if primary:
                print(primary)
    except (ConfigMergeError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
