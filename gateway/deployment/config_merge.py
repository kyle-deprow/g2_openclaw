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
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast


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


STALE_CODING_PROVIDER_KEYS = frozenset({"github-copilot", "copilot-proxy", "copilot-cli"})


@dataclass(frozen=True)
class AssemblyInputs:
    """Resolved values substituted into the managed configuration."""

    repo_root: str
    python_bin: str
    mempalace_python: str
    mempalace_palace: str
    mempalace_wrapper: str
    fastembed_cache: str
    mempalace_embedding_model: str
    hf_hub_offline: str
    g2_module: str
    mempalace_readonly_agents: tuple[str, ...]
    g2_agents: tuple[str, ...]
    provider: str
    model_primary: str
    model_provider: str
    model_id: str
    pm_model_primary: str


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


def _set_path(root: JsonObject, path: Sequence[str], value: JsonValue) -> None:
    current = root
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


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
        model_id = openrouter_model or "anthropic/claude-sonnet-4-20250514"
        return f"openrouter/{model_id}", "openrouter", model_id
    raise ConfigMergeError(
        f"ERROR: Unknown OPENCLAW_PROVIDER '{provider}'. Use 'codex', 'azure', or 'openrouter'."
    )


def pm_model_primary(repo: JsonObject) -> str:
    agents = _get_object(repo, "agents")
    if agents is None:
        return ""
    raw_list = agents.get("list")
    if not isinstance(raw_list, list):
        return ""
    for item in raw_list:
        if not isinstance(item, dict) or item.get("id") != "autoresearch-pm":
            continue
        model = item.get("model")
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


def assemble_config(local: JsonObject, repo: JsonObject, inputs: AssemblyInputs) -> JsonObject:
    """Apply every managed jq assembly operation to two decoded configs."""

    merged = _object(deep_merge(local, repo), "merged config")
    merged.pop("mcp", None)

    merged["mcp"] = {
        "servers": {
            "mempalace-readonly": {
                "command": inputs.mempalace_python,
                "args": [inputs.mempalace_wrapper, "--palace", inputs.mempalace_palace],
                "codex": {
                    "agents": list(inputs.mempalace_readonly_agents),
                    "defaultToolsApprovalMode": "approve",
                },
                "env": {
                    "FASTEMBED_CACHE_PATH": inputs.fastembed_cache,
                    "MEMPALACE_EMBEDDING_MODEL": inputs.mempalace_embedding_model,
                    "HF_HUB_OFFLINE": inputs.hf_hub_offline,
                },
            },
            "g2-control": {
                "command": inputs.python_bin,
                "args": ["-m", inputs.g2_module],
                "codex": {
                    "agents": list(inputs.g2_agents),
                    "defaultToolsApprovalMode": "approve",
                },
                "env": {"PYTHONPATH": inputs.repo_root},
            },
        }
    }

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
        memory_search = repo_defaults.get("memorySearch")
        if _jq_truthy(memory_search):
            defaults["memorySearch"] = _copy(memory_search)
        compaction = _get_object(repo_defaults, "compaction")
        memory_flush = compaction.get("memoryFlush") if compaction is not None else None
        if _jq_truthy(memory_flush):
            compaction_target = _get_object(defaults, "compaction")
            if compaction_target is None:
                compaction_target = {}
                defaults["compaction"] = compaction_target
            compaction_target["memoryFlush"] = _copy(memory_flush)

    repo_agents = _get_object(repo, "agents")
    agents_list = repo_agents.get("list") if repo_agents is not None else None
    if _jq_truthy(agents_list):
        agents = _object(merged.setdefault("agents", {}), "merged agents")
        agents["list"] = _copy(agents_list)

    models = _object(merged.setdefault("models", {}), "merged models")
    providers = _object(models.setdefault("providers", {}), "merged model providers")
    if inputs.provider == "openrouter" and os.environ.get("OPENROUTER_API_KEY", ""):
        for provider in providers.values():
            if isinstance(provider, dict) and provider.get("apiKey") == "env:OPENROUTER_API_KEY":
                provider["apiKey"] = os.environ["OPENROUTER_API_KEY"]

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

    if not _model_declared(merged, inputs.model_provider, inputs.model_id):
        raise ConfigMergeError(
            f"ERROR: Selected model '{inputs.model_primary}' is not declared in repo config.\n"
            "       Add it to gateway/openclaw_config/openclaw.json or choose a configured model."
        )

    if not inputs.pm_model_primary:
        raise ConfigMergeError(
            'ERROR: Repo config must pin agents.list[].id == "autoresearch-pm" to a model.primary.'
        )
    if not inputs.pm_model_primary.startswith("openai/"):
        raise ConfigMergeError(
            f"ERROR: PM model '{inputs.pm_model_primary}' must use the OpenAI/Codex provider."
        )
    pm_model_id = inputs.pm_model_primary.removeprefix("openai/")
    if not _model_declared(merged, "openai", pm_model_id):
        raise ConfigMergeError(
            f"ERROR: PM model '{inputs.pm_model_primary}' is not declared in repo config.\n"
            "       Add it to gateway/openclaw_config/openclaw.json before pushing."
        )

    defaults["model"] = _get_object(defaults, "model") or {}
    cast(JsonObject, defaults["model"])["primary"] = inputs.model_primary
    defaults["models"] = {inputs.model_primary: {}}

    agents = _object(merged.get("agents"), "merged agents")
    managed_agents = agents.get("list")
    if isinstance(managed_agents, list):
        for item in managed_agents:
            if not isinstance(item, dict) or item.get("id") != "autoresearch-pm":
                continue
            model = item.get("model")
            if not isinstance(model, dict):
                model = {}
                item["model"] = model
            model["primary"] = inputs.pm_model_primary
            item["thinkingDefault"] = "high"

    merged = _object(
        _walk_remove_keys(
            merged,
            STALE_CODING_PROVIDER_KEYS,
        ),
        "sanitized merged config",
    )
    plugins = _get_object(merged, "plugins")
    entries = _get_object(plugins if plugins is not None else {}, "entries")
    codex = _get_object(entries if entries is not None else {}, "codex")
    codex_config = _get_object(codex if codex is not None else {}, "config")
    if codex_config is not None:
        codex_config.pop("codexDynamicToolsExclude", None)
        codex_config.pop("nativeToolSurfaceEnabled", None)
    return merged


def _json_object_arg(raw: str, name: str) -> JsonObject | list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigMergeError(f"invalid {name} JSON: {exc}") from exc
    if name.endswith("agents"):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigMergeError(f"{name} must be a JSON string array")
        return cast(list[str], value)
    if not isinstance(value, dict):
        raise ConfigMergeError(f"{name} must be a JSON object")
    return cast(JsonObject, value)


def _assemble_from_files(args: argparse.Namespace) -> bytes:
    local = _object(load_json(args.local_config), "local config")
    repo = _object(load_json(args.repo_config), "repo config")
    readonly_agents = _json_object_arg(args.readonly_agents_json, "readonly agents")
    g2_agents = _json_object_arg(args.g2_agents_json, "g2 agents")
    provider = os.environ.get("OPENCLAW_PROVIDER") or "codex"
    model_primary, model_provider, model_id = _provider_selection(
        provider,
        os.environ.get("OPENAI_MODEL", "gpt-5.4"),
        os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-20250514"),
    )
    pm_primary = pm_model_primary(repo)
    inputs = AssemblyInputs(
        repo_root=args.repo_root,
        python_bin=args.python_bin,
        mempalace_python=args.mempalace_python,
        mempalace_palace=args.mempalace_palace,
        mempalace_wrapper=args.mempalace_wrapper,
        fastembed_cache=args.fastembed_cache,
        mempalace_embedding_model=args.mempalace_embedding_model,
        hf_hub_offline=args.hf_hub_offline,
        g2_module=args.g2_module,
        mempalace_readonly_agents=tuple(cast(list[str], readonly_agents)),
        g2_agents=tuple(cast(list[str], g2_agents)),
        provider=provider,
        model_primary=model_primary,
        model_provider=model_provider,
        model_id=model_id,
        pm_model_primary=pm_primary,
    )
    return serialize_json(assemble_config(local, repo, inputs))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("local_config")
    assemble.add_argument("repo_config")
    assemble.add_argument("repo_root")
    assemble.add_argument("python_bin")
    assemble.add_argument("mempalace_python")
    assemble.add_argument("mempalace_palace")
    assemble.add_argument("mempalace_wrapper")
    assemble.add_argument("fastembed_cache")
    assemble.add_argument("mempalace_embedding_model")
    assemble.add_argument("hf_hub_offline")
    assemble.add_argument("g2_module")
    assemble.add_argument("readonly_agents_json")
    assemble.add_argument("g2_agents_json")

    pm_parser = subparsers.add_parser("pm-model")
    pm_parser.add_argument("repo_config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "assemble":
            sys.stdout.buffer.write(_assemble_from_files(args))
        elif args.command == "pm-model":
            repo = _object(load_json(args.repo_config), "repo config")
            primary = pm_model_primary(repo)
            if primary:
                print(primary)
    except (ConfigMergeError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
