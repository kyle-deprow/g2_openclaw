"""Golden tests for the jq-to-Python OpenClaw config assembly port."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.agent_profiles import load_profile
from gateway.deployment.agent_guard import guard_config, guard_plugin_dir
from gateway.deployment.config_merge import (
    AssemblyInputs,
    ConfigMergeError,
    JsonNumber,
    JsonObject,
    JsonValue,
    ValidationExpectations,
    _profile_for,
    assemble_config,
    assemble_config_with_migration,
    deep_merge,
    load_json,
    serialize_json,
    validate_config,
    write_migration_record,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_CONFIG = REPO_ROOT / "gateway/openclaw_config/openclaw.json"
JQ = shutil.which("jq")

STAGE_AGENT_IDS = ("implementer", "experiment_runner")
READONLY_AGENTS = ("main",)
G2_AGENTS = ("main",)


def _run_jq(arguments: list[str], *, input_bytes: bytes | None = None) -> bytes:
    assert JQ is not None
    result = subprocess.run(
        [JQ, *arguments],
        input=input_bytes,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _json_arg(value: JsonValue) -> str:
    return serialize_json(value).decode("utf-8").rstrip("\n")


def _jq_merge(local_path: Path, repo_path: Path) -> bytes:
    return _run_jq(["-s", ".[0] * .[1]", str(local_path), str(repo_path)])


@pytest.mark.skipif(JQ is None, reason="jq is required for byte-equivalence golden tests")
@pytest.mark.parametrize(
    ("local", "overlay"),
    [
        (
            {"nested": {"keep": 1, "replace": "local"}, "same": "local"},
            {"nested": {"replace": "repo", "new": True}, "same": "repo"},
        ),
        ({"items": [1, 2], "nested": {"items": [3]}}, {"items": [9], "nested": {}}),
        ({"value": {"present": True}, "null": "local"}, {"value": None, "null": None}),
        ({"text": "café", "escaped": "\u4e2d"}, {"text": "雪", "array": ["é", "中"]}),
        ({"integer": 9007199254740993123456789}, {"integer": 9223372036854775808}),
        ({"float": 1.25, "negative": -0.5, "unit": 1.0}, {"small": 1e-6}),
        ({"empty": {"local": True}, "array": [1]}, {"empty": {}, "array": []}),
        ({chr(0x7F): "DEL value"}, {"DEL key value": chr(0x7F)}),
    ],
    ids=[
        "nested-right-wins",
        "arrays-replace",
        "nulls",
        "unicode",
        "large-integers",
        "floats",
        "empty-objects",
        "del-character",
    ],
)
def test_deep_merge_serialization_matches_real_jq(
    tmp_path: Path, local: JsonObject, overlay: JsonObject
) -> None:
    local_path = tmp_path / "local.json"
    overlay_path = tmp_path / "overlay.json"
    _write_json(local_path, local)
    _write_json(overlay_path, overlay)

    python_value = deep_merge(
        cast(JsonObject, load_json(local_path)), cast(JsonObject, load_json(overlay_path))
    )

    assert serialize_json(python_value) == _jq_merge(local_path, overlay_path)


def test_assembly_drops_retired_main_codex_workspace_root(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {
            "plugins": {
                "entries": {
                    "codex": {
                        "config": {
                            "appServer": {
                                "sandbox": "workspace-write",
                                "defaultWorkspaceDir": (
                                    "/home/dev/.openclaw/autoresearch/model-workspaces"
                                ),
                            }
                        }
                    }
                }
            }
        },
    )
    repo = cast(JsonObject, load_json(REPO_CONFIG))

    merged = assemble_config(local, repo, _assembly_inputs(tmp_path))

    plugins = cast(JsonObject, merged["plugins"])
    entries = cast(JsonObject, plugins["entries"])
    codex = cast(JsonObject, entries["codex"])
    codex_config = cast(JsonObject, codex["config"])
    app_server = cast(JsonObject, codex_config["appServer"])
    assert "defaultWorkspaceDir" not in app_server


def test_assembly_projects_owner_workspace_under_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "fake-home/.openclaw"
    merged = assemble_config(
        cast(JsonObject, {}),
        cast(JsonObject, load_json(REPO_CONFIG)),
        _assembly_inputs(tmp_path),
    )

    owner = cast(JsonObject, cast(JsonObject, merged["agents"])["entries"])["research-orchestrator"]
    assert cast(JsonObject, owner)["workspace"] == str(
        state_root / "workspace-research-orchestrator"
    )
    workspace = cast(JsonObject, owner)["workspace"]
    assert isinstance(workspace, str)
    assert Path(workspace).is_absolute()


def test_assembly_disables_implicit_and_explicit_memory_plugin(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {"plugins": {"entries": {"memory-core": {"enabled": True}}}},
    )
    assembled = assemble_config(
        local,
        cast(JsonObject, load_json(REPO_CONFIG)),
        _assembly_inputs(tmp_path),
    )

    plugins = cast(JsonObject, assembled["plugins"])
    entries = cast(JsonObject, plugins["entries"])
    slots = cast(JsonObject, plugins["slots"])
    assert cast(JsonObject, entries["memory-core"])["enabled"] is False
    assert slots["memory"] == "none"


def test_assembly_removes_local_openai_api_key_for_managed_oauth_route(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {
            "models": {
                "providers": {
                    "openai": {"apiKey": "fixture-value"}  # pragma: allowlist secret
                }
            }
        },
    )
    local_path = tmp_path / "local.json"
    _write_json(local_path, local)
    inputs = _assembly_inputs(tmp_path)
    assembled = assemble_config(
        local,
        cast(JsonObject, load_json(REPO_CONFIG)),
        inputs,
    )

    providers = cast(JsonObject, cast(JsonObject, assembled["models"])["providers"])
    assert "apiKey" not in cast(JsonObject, providers["openai"])
    assert serialize_json(assembled) == _jq_full_assembly(local_path, REPO_CONFIG, inputs)


def test_assembly_emits_native_model_policy_and_drops_inherited_legacy_map(
    tmp_path: Path,
) -> None:
    local = cast(
        JsonObject,
        {
            "gateway": {"auth": {"token": "machine-local-token"}},
            "agents": {
                "defaults": {
                    "models": {"openai/unselected": {}},
                    "modelPolicy": {"allow": ["openrouter/unselected"]},
                }
            },
            "models": {
                "providers": {
                    "private": {
                        "apiKey": "private-key",  # pragma: allowlist secret
                        "headers": {"X-Auth": "value"},
                    }
                }
            },
        },
    )

    assembled, migration_record = assemble_config_with_migration(
        local, cast(JsonObject, load_json(REPO_CONFIG)), _assembly_inputs(tmp_path)
    )

    defaults = cast(JsonObject, cast(JsonObject, assembled["agents"])["defaults"])
    assert "models" not in defaults
    assert migration_record == {"removed": []}
    assert defaults["modelPolicy"] == {
        "allow": [
            "openai/gpt-5.4",
            "openai/gpt-6-astra",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-sol",
        ]
    }
    assert cast(JsonObject, cast(JsonObject, assembled["gateway"])["auth"]) == {
        "token": "machine-local-token"
    }
    providers = cast(JsonObject, cast(JsonObject, assembled["models"])["providers"])
    assert cast(JsonObject, providers["private"]) == {
        "apiKey": "private-key",  # pragma: allowlist secret
        "headers": {"X-Auth": "value"},
    }
    entries = cast(JsonObject, cast(JsonObject, assembled["agents"])["entries"])
    assert cast(JsonObject, entries["main"])["model"] == {"primary": "openai/gpt-5.4"}
    assert cast(JsonObject, entries["research-orchestrator"])["model"] == {
        "primary": "openai/gpt-6-astra"
    }


def test_assembly_removes_restrictive_owner_tool_filters(tmp_path: Path) -> None:
    repo = cast(JsonObject, load_json(REPO_CONFIG))
    agents = cast(JsonObject, repo["agents"])
    entries = cast(JsonObject, agents["entries"])
    owner = cast(JsonObject, entries["research-orchestrator"])
    owner["tools"] = {
        "profile": "full",
        "allow": ["sessions_spawn"],
        "deny": ["sessions_yield", "g2-control__g2_autoresearch_status"],
    }

    assembled = assemble_config(cast(JsonObject, {}), repo, _assembly_inputs(tmp_path))

    assembled_agents = cast(JsonObject, assembled["agents"])
    assembled_entries = cast(JsonObject, assembled_agents["entries"])
    assert cast(JsonObject, assembled_entries["research-orchestrator"])["tools"] == {
        "profile": "full"
    }
    mcp = cast(JsonObject, assembled["mcp"])
    servers = cast(JsonObject, mcp["servers"])
    readonly = cast(JsonObject, servers["mempalace-readonly"])
    readonly_codex = cast(JsonObject, readonly["codex"])
    g2_control = cast(JsonObject, servers["g2-control"])
    g2_codex = cast(JsonObject, g2_control["codex"])
    assert readonly_codex["agents"] == ["main"]
    assert g2_codex["agents"] == ["main"]


def test_assembly_rejects_explicit_non_openai_interface_route(
    tmp_path: Path,
) -> None:
    inputs = _assembly_inputs(
        tmp_path,
        model_primary="anthropic/claude-sonnet-4",
        model_provider="openai",
        model_id="gpt-5.4",
    )

    with pytest.raises(ConfigMergeError, match="approved OpenAI model route"):
        assemble_config(
            cast(JsonObject, {"agents": {"defaults": {"models": {"openai/unselected": {}}}}}),
            cast(JsonObject, load_json(REPO_CONFIG)),
            inputs,
        )


def test_assembly_deduplicates_native_model_policy_and_jq_oracle(tmp_path: Path) -> None:
    inputs = _assembly_inputs(
        tmp_path,
        model_primary="openai/gpt-6-astra",
        model_provider="openai",
        model_id="gpt-6-astra",
        orchestrator_model_primary="openai/gpt-6-astra",
    )
    local = cast(JsonObject, {"agents": {"defaults": {"models": {"openai/unselected": {}}}}})

    assembled = assemble_config(local, cast(JsonObject, load_json(REPO_CONFIG)), inputs)

    defaults = cast(JsonObject, cast(JsonObject, assembled["agents"])["defaults"])
    assert defaults["modelPolicy"] == {
        "allow": ["openai/gpt-6-astra", "openai/gpt-5.6-luna", "openai/gpt-5.6-sol"]
    }

    local_path = tmp_path / "local.json"
    _write_json(local_path, local)
    assert serialize_json(assembled) == _jq_full_assembly(local_path, REPO_CONFIG, inputs)


def test_assembly_fails_closed_when_native_luna_route_is_undeclared(tmp_path: Path) -> None:
    repo = cast(JsonObject, load_json(REPO_CONFIG))
    providers = cast(JsonObject, cast(JsonObject, repo["models"])["providers"])
    openai_models = cast(list[JsonValue], cast(JsonObject, providers["openai"])["models"])
    cast(JsonObject, providers["openai"])["models"] = [
        item
        for item in openai_models
        if not (isinstance(item, dict) and item.get("id") == "gpt-5.6-luna")
    ]

    with pytest.raises(ConfigMergeError, match=r"Native research model 'openai/gpt-5\.6-luna'"):
        assemble_config(cast(JsonObject, {}), repo, _assembly_inputs(tmp_path))


def test_schema_migration_removes_exact_8_1_paths_and_records_verbatim_values(
    tmp_path: Path,
) -> None:
    local = cast(
        JsonObject,
        {
            "meta": {"lastTouchedAt": "local-timestamp", "lastTouchedVersion": "local"},
            "agents": {
                "defaults": {
                    "memorySearch": {"enabled": False},
                    "nested": {"memorySearch": {"enabled": True}},
                }
            },
            "commands": {"ownerDisplay": "hash", "nested": {"ownerDisplay": "raw"}},
            "memory": {"backend": None, "citations": "off", "nested": {"backend": "keep"}},
        },
    )

    repo = cast(JsonObject, load_json(REPO_CONFIG))
    repo["memory"] = {
        "backend": None,
        "citations": "off",
        "search": {"enabled": False},
        "nested": {"backend": "keep"},
    }
    migrated, record = assemble_config_with_migration(local, repo, _assembly_inputs(tmp_path))

    assert record == {
        "removed": [
            {"path": "meta.lastTouchedAt", "value": "local-timestamp"},
            {
                "path": "agents.defaults.memorySearch",
                "value": {"enabled": False},
                "mapped_to": "memory.search.enabled",
            },
            {"path": "commands.ownerDisplay", "value": "hash"},
            {"path": "memory.backend", "value": None},
        ]
    }
    assert "lastTouchedAt" not in cast(JsonObject, migrated["meta"])
    assert cast(JsonObject, migrated["meta"])["lastTouchedVersion"] == "2026.9.2"
    defaults = cast(JsonObject, cast(JsonObject, migrated["agents"])["defaults"])
    assert "memorySearch" not in defaults
    assert cast(JsonObject, defaults["nested"])["memorySearch"] == {"enabled": True}
    assert "ownerDisplay" not in cast(JsonObject, migrated["commands"])
    memory = cast(JsonObject, migrated["memory"])
    assert "backend" not in memory
    assert memory["citations"] == "off"
    assert cast(JsonObject, memory["search"])["enabled"] is False
    assert cast(JsonObject, memory["nested"])["backend"] == "keep"


def test_local_memory_backend_is_recorded_before_repository_memory_overlay(
    tmp_path: Path,
) -> None:
    local = cast(JsonObject, {"memory": {"backend": "builtin"}})
    repo = cast(JsonObject, load_json(REPO_CONFIG))
    repo["memory"] = {"citations": "off", "search": {"enabled": False}}

    migrated, record = assemble_config_with_migration(local, repo, _assembly_inputs(tmp_path))

    assert "backend" not in cast(JsonObject, migrated["memory"])
    assert record["removed"] == [
        {
            "path": "memory.backend",
            "value": "builtin",
            "source": "local_pre_overlay",
        }
    ]


def test_absent_local_memory_backend_is_not_recorded(tmp_path: Path) -> None:
    local = cast(JsonObject, {"memory": {"citations": "local"}})
    repo = cast(JsonObject, load_json(REPO_CONFIG))
    repo["memory"] = {"citations": "off", "search": {"enabled": False}}

    _, record = assemble_config_with_migration(local, repo, _assembly_inputs(tmp_path))

    assert record == {"removed": []}


def test_legacy_memory_search_extra_keys_fail_closed(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {"agents": {"defaults": {"memorySearch": {"enabled": False, "provider": "old"}}}},
    )

    with pytest.raises(ConfigMergeError, match=r"agents\.defaults\.memorySearch"):
        assemble_config_with_migration(
            local, cast(JsonObject, load_json(REPO_CONFIG)), _assembly_inputs(tmp_path)
        )


@pytest.mark.parametrize(
    "legacy_value",
    [{}, {"enabled": "false"}, None],
    ids=["missing-enabled", "non-boolean-enabled", "non-object"],
)
def test_legacy_memory_search_requires_exact_boolean_enabled(
    tmp_path: Path, legacy_value: object
) -> None:
    local = cast(
        JsonObject,
        {"agents": {"defaults": {"memorySearch": legacy_value}}},
    )

    with pytest.raises(ConfigMergeError, match=r"agents\.defaults\.memorySearch"):
        assemble_config_with_migration(
            local, cast(JsonObject, load_json(REPO_CONFIG)), _assembly_inputs(tmp_path)
        )


def test_legacy_memory_search_conflict_is_checked_before_memory_overlay(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {
            "agents": {"defaults": {"memorySearch": {"enabled": False}}},
            "memory": {"search": {"enabled": True}},
        },
    )

    with pytest.raises(ConfigMergeError, match=r"memory\.search\.enabled"):
        assemble_config_with_migration(
            local, cast(JsonObject, load_json(REPO_CONFIG)), _assembly_inputs(tmp_path)
        )


def test_schema_migration_preserves_provider_and_auth_objects(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {
            "gateway": {"auth": {"token": "machine-local-token"}},
            "models": {
                "providers": {
                    "azure-oai-g2": {"apiKey": "local-azure-key"},  # pragma: allowlist secret
                    "private": {
                        "apiKey": "private-key",  # pragma: allowlist secret
                        "headers": {"X-Auth": "value"},
                    },
                }
            },
        },
    )

    migrated, _ = assemble_config_with_migration(
        local, cast(JsonObject, load_json(REPO_CONFIG)), _assembly_inputs(tmp_path)
    )

    assert cast(JsonObject, migrated["gateway"])["auth"] == {"token": "machine-local-token"}
    providers = cast(JsonObject, cast(JsonObject, migrated["models"])["providers"])
    azure = cast(JsonObject, providers["azure-oai-g2"])
    assert azure["apiKey"] == "local-azure-key"  # pragma: allowlist secret
    assert cast(JsonObject, providers["private"]) == {
        "apiKey": "private-key",  # pragma: allowlist secret
        "headers": {"X-Auth": "value"},
    }


def test_migration_record_file_is_deterministic_and_private(tmp_path: Path) -> None:
    local = cast(
        JsonObject,
        {
            "meta": {"lastTouchedAt": "machine-local"},
            "commands": {"ownerDisplay": "raw"},
        },
    )
    _, record = assemble_config_with_migration(
        local, cast(JsonObject, load_json(REPO_CONFIG)), _assembly_inputs(tmp_path)
    )
    record_path = tmp_path / "migration-record.json"

    write_migration_record(record_path, record)

    assert record_path.read_bytes() == serialize_json(record)
    assert record_path.stat().st_mode & 0o777 == 0o600


def test_migration_record_rejects_symlinked_immediate_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "record-parent"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigMergeError, match="symlink"):
        write_migration_record(parent / "migration-record.json", {"removed": []})

    assert parent.is_symlink()
    assert not (outside / "migration-record.json").exists()


def test_migration_record_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    ancestor = tmp_path / "record-root"
    ancestor.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigMergeError, match="symlink"):
        write_migration_record(ancestor / "nested/migration-record.json", {"removed": []})

    assert ancestor.is_symlink()
    assert not (outside / "nested/migration-record.json").exists()


def test_migration_record_rejects_preexisting_final_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "record-parent"
    parent.mkdir()
    outside = tmp_path / "outside-record.json"
    outside.write_bytes(b"outside\n")
    record_path = parent / "migration-record.json"
    record_path.symlink_to(outside)

    with pytest.raises(ConfigMergeError, match="symlink"):
        write_migration_record(record_path, {"removed": []})

    assert record_path.is_symlink()
    assert outside.read_bytes() == b"outside\n"


def test_migration_record_rejects_absent_parent(tmp_path: Path) -> None:
    record_path = tmp_path / "missing-parent/migration-record.json"

    with pytest.raises(ConfigMergeError, match="parent"):
        write_migration_record(record_path, {"removed": []})

    assert not record_path.parent.exists()


def test_migration_record_safely_rewrites_existing_private_record(tmp_path: Path) -> None:
    record_path = tmp_path / "migration-record.json"
    record_path.write_bytes(b'{"removed": []}\n')
    record_path.chmod(0o600)
    record = cast(JsonObject, {"removed": [{"path": "memory.backend", "value": "builtin"}]})

    write_migration_record(record_path, record)

    assert record_path.read_bytes() == serialize_json(record)
    assert record_path.stat().st_mode & 0o777 == 0o600


def test_overlay_does_not_contain_removed_8_1_paths() -> None:
    overlay = cast(JsonObject, load_json(REPO_CONFIG))

    assert "memorySearch" not in cast(JsonObject, cast(JsonObject, overlay["agents"])["defaults"])
    assert "backend" not in cast(JsonObject, overlay["memory"])
    assert "ownerDisplay" not in cast(JsonObject, overlay["commands"])
    assert "lastTouchedAt" not in cast(JsonObject, overlay["meta"])
    memory = cast(JsonObject, overlay["memory"])
    assert memory["citations"] == "off"
    assert cast(JsonObject, memory["search"])["enabled"] is False


def _assembly_inputs(
    tmp_path: Path,
    *,
    provider: str = "codex",
    model_primary: str = "openai/gpt-5.4",
    model_provider: str = "openai",
    model_id: str = "gpt-5.4",
    orchestrator_model_primary: str = "openai/gpt-6-astra",
    research_v2_root: str | None = None,
) -> AssemblyInputs:
    home = tmp_path / "fake-home"
    push_home = home / ".openclaw"
    resolved_research_root = research_v2_root or str(push_home / "research-v2")
    owner_env_file = tmp_path / "owner.env"
    owner_env_file.write_text("OPENCLAW_GATEWAY_TOKEN=fixture\n", encoding="utf-8")
    owner_env_file.chmod(0o600)
    core_database = push_home / "state/openclaw.sqlite"
    core_database.parent.mkdir(parents=True, exist_ok=True)
    core_database.write_bytes(b"fixture core database\n")
    return AssemblyInputs(
        repo_root=str(REPO_ROOT),
        openclaw_state_root=str(push_home),
        python_bin=str(REPO_ROOT / ".venv/bin/python"),
        mempalace_python=str(home / ".local/share/mempalace/venv/bin/python"),
        mempalace_palace=str(home / ".mempalace/palace"),
        mempalace_wrapper=str(push_home / "mempalace-readonly-server.py"),
        fastembed_cache=str(tmp_path / "fastembed-cache"),
        mempalace_embedding_model="bge-base",
        hf_hub_offline="1",
        g2_module="gateway.g2_control_mcp_server",
        research_v2_root=resolved_research_root,
        research_core_database=str(core_database),
        owner_env_file=str(owner_env_file),
        openclaw_host="127.0.0.1",
        openclaw_port="18789",
        mempalace_readonly_agents=READONLY_AGENTS,
        g2_agents=G2_AGENTS,
        provider=provider,
        model_primary=model_primary,
        model_provider=model_provider,
        model_id=model_id,
        orchestrator_model_primary=orchestrator_model_primary,
    )


def _jq_full_assembly(
    local_path: Path,
    repo_path: Path,
    inputs: AssemblyInputs,
    *,
    openrouter_api_key: str | None = None,
) -> bytes:
    merged = _run_jq(["-s", ".[0] * .[1] | del(.mcp)", str(local_path), str(repo_path)])
    readonly_agents = json.dumps(list(inputs.mempalace_readonly_agents))
    g2_agents = json.dumps(list(inputs.g2_agents))
    merged = _run_jq(
        [
            "--arg",
            "state_root",
            inputs.openclaw_state_root,
            "--arg",
            "cmd",
            inputs.mempalace_python,
            "--arg",
            "palace",
            inputs.mempalace_palace,
            "--arg",
            "wrapper",
            inputs.mempalace_wrapper,
            "--arg",
            "cache",
            inputs.fastembed_cache,
            "--arg",
            "model",
            inputs.mempalace_embedding_model,
            "--arg",
            "offline",
            inputs.hf_hub_offline,
            "--arg",
            "repo",
            inputs.repo_root,
            "--arg",
            "python",
            inputs.python_bin,
            "--arg",
            "g2_module",
            inputs.g2_module,
            "--arg",
            "research_root",
            inputs.research_v2_root,
            "--arg",
            "research_core_database",
            inputs.research_core_database,
            "--arg",
            "owner_env_file",
            inputs.owner_env_file,
            "--arg",
            "openclaw_host",
            inputs.openclaw_host,
            "--arg",
            "openclaw_port",
            inputs.openclaw_port,
            "--argjson",
            "readonly_agents",
            readonly_agents,
            "--argjson",
            "g2_agents",
            g2_agents,
            ".mcp.servers = {\n"
            '  "mempalace-readonly": {\n'
            '    "command": $cmd,\n'
            '    "args": [$wrapper, "--palace", $palace],\n'
            '    "codex": {"agents": $readonly_agents, "defaultToolsApprovalMode": "approve"},\n'
            '    "env": {\n'
            '      "FASTEMBED_CACHE_PATH": $cache,\n'
            '      "MEMPALACE_EMBEDDING_MODEL": $model,\n'
            '      "HF_HUB_OFFLINE": $offline\n'
            "    }\n"
            "  },\n"
            '  "g2-control": {\n'
            '    "command": $python,\n'
            '    "args": ["-m", $g2_module],\n'
            '    "codex": {\n'
            '      "agents": $g2_agents,\n'
            '      "defaultToolsApprovalMode": "approve"\n'
            "    },\n"
            '    "env": {"PYTHONPATH": $repo, "RESEARCH_V2_ROOT": $research_root, '
            '"RESEARCH_CORE_DATABASE": $research_core_database, "OPENCLAW_HOST": $openclaw_host, '
            '"OPENCLAW_PORT": $openclaw_port, "G2_OWNER_ENV_FILE": $owner_env_file}\n'
            "  }\n"
            "}",
        ],
        input_bytes=merged,
    )

    repo = cast(JsonObject, load_json(repo_path))
    for key in ("tools", "memory"):
        value = repo.get(key)
        if value is not None and value is not False:
            merged = _run_jq(
                ["--argjson", key, _json_arg(value), f".{key} = ${key}"],
                input_bytes=merged,
            )
    repo_agents = cast(JsonObject, repo["agents"])
    repo_defaults = cast(JsonObject, repo_agents["defaults"])
    repo_compaction = cast(JsonObject, repo_defaults["compaction"])
    memory_flush = repo_compaction["memoryFlush"]
    merged = _run_jq(
        [
            "--argjson",
            "memory_flush",
            _json_arg(memory_flush),
            ".agents.defaults.compaction.memoryFlush = $memory_flush",
        ],
        input_bytes=merged,
    )
    agents_entries = repo_agents["entries"]
    merged = _run_jq(
        [
            "--argjson",
            "agents_entries",
            _json_arg(agents_entries),
            '.agents.ownership = "explicit" | del(.agents.list) | '
            ".agents.entries = $agents_entries",
        ],
        input_bytes=merged,
    )
    if inputs.provider == "openrouter" and openrouter_api_key:
        merged = _run_jq(
            [
                "--arg",
                "key",
                openrouter_api_key,
                "(.models.providers // {}) |= with_entries("
                'if .value.apiKey == "env:OPENROUTER_API_KEY" then '  # pragma: allowlist secret
                ".value.apiKey = $key else . end)",  # pragma: allowlist secret
            ],
            input_bytes=merged,
        )
    merged = _run_jq(
        [
            'if .models.providers.openai.auth == "oauth" then '
            "del(.models.providers.openai.apiKey) else . end"
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "(.models.providers // {}) as $provs | "
            '($provs | to_entries | map(select(.key | startswith("azure-oai-"))) | '
            'map(select(.value.apiKey != null and .value.apiKey != "")) | '
            ".[0].value.apiKey // null) as $azureKey | "
            "if $azureKey != null then "
            '.models.providers |= with_entries(if (.key | startswith("azure-oai-")) '
            'and (.value.apiKey == null or .value.apiKey == "") then '
            ".value.apiKey = $azureKey else . end) else . end",
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "--arg",
            "primary",
            inputs.model_primary,
            "--arg",
            "owner",
            inputs.orchestrator_model_primary,
            ".agents.defaults.model.primary = $primary | "
            ".agents.defaults.modelPolicy = "
            '{"allow": ([$primary, $owner, "openai/gpt-5.6-luna", "openai/gpt-5.6-sol"] | '
            "reduce .[] as $ref ([]; if index($ref) then . else . + [$ref] end))} | "
            "del(.agents.defaults.models)",
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "--arg",
            "owner",
            inputs.orchestrator_model_primary,
            "--arg",
            "state_root",
            inputs.openclaw_state_root,
            '(.agents.entries["research-orchestrator"] | .model.primary) = $owner | '
            '(.agents.entries["research-orchestrator"] | '
            '.thinkingDefault) = "high" | '
            '(.agents.entries["research-orchestrator"] | .workspace) = '
            '(if ((.agents.entries["research-orchestrator"].workspace? | type) == "string" '
            'and (.agents.entries["research-orchestrator"].workspace | length) > 0) '
            'then .agents.entries["research-orchestrator"].workspace '
            'else "workspace-research-orchestrator" end '
            '| if startswith("/") then . else ($state_root + "/" + .) end) | '
            "if .plugins.entries.codex.config.appServer.defaultWorkspaceDir == "
            '"/home/dev/.openclaw/autoresearch/model-workspaces" then '
            "del(.plugins.entries.codex.config.appServer.defaultWorkspaceDir) else . end",
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "--argjson",
            "stale_keys",
            '["github-copilot", "copilot-proxy", "copilot-cli"]',
            'walk(if type == "object" then '
            "with_entries(select((.key as $key | $stale_keys | index($key)) | not)) "
            "else . end)",
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "del(.plugins.entries.codex.config.codexDynamicToolsExclude) | "
            "del(.plugins.entries.codex.config.nativeToolSurfaceEnabled)"
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        ["del(.plugins.load.paths) | if .plugins.load == {} then del(.plugins.load) else . end"],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "del(.acp) | del(.plugins.entries.acpx) | "
            '.plugins.allow = ((.plugins.allow // []) | map(select(. != "acpx")))'
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "(if .agents.defaults.memorySearch? != null then "
            ".memory.search.enabled = .agents.defaults.memorySearch.enabled else . end) | "
            "del(.meta.lastTouchedAt) | "
            "del(.agents.defaults.memorySearch) | "
            "del(.commands.ownerDisplay) | "
            "del(.memory.backend)"
        ],
        input_bytes=merged,
    )
    return _run_jq(["."], input_bytes=merged)


@pytest.mark.skipif(JQ is None, reason="jq is required for byte-equivalence golden tests")
def test_actual_repo_overlay_full_assembly_is_byte_identical_to_jq(tmp_path: Path) -> None:
    local = {
        "gateway": {"auth": {"token": "machine-local-token"}},
        "wizard": {"lastRun": "machine-local"},
        "meta": {"instanceId": "machine-local-instance"},
        "models": {
            "providers": {
                "azure-oai-g2": {"apiKey": "local-azure-key"},  # pragma: allowlist secret
                "azure-oai-g2-mini": {"apiKey": ""},
                "github-copilot": {"apiKey": "stale"},  # pragma: allowlist secret
            }
        },
        "agents": {
            "defaults": {
                "model": {"primary": "local/model"},
                "subagents": {"archiveAfterMinutes": 7, "requireAgentId": True},
                "memorySearch": {"enabled": False},
            },
            "list": [
                {"id": "stale-local", "model": {"primary": "local/stale"}},
            ],
        },
        "acp": {
            "stream": {"coalesceIdleMs": 99},
            "defaultAgent": "legacy-reviewer",
            "probeAgent": "legacy-probe",
        },
        "tools": {"allow": ["stale"]},
        "memory": {"legacy": {"enabled": True}},
        "plugins": {
            "load": {"paths": ["~/.openclaw/plugins/cohorts/2026.8.1/node_modules/@openclaw/acpx"]},
            "entries": {
                "codex": {
                    "config": {
                        "nativeToolSurfaceEnabled": True,
                        "codexDynamicToolsExclude": ["stale"],
                    }
                },
                "acpx": {
                    "config": {
                        "stream": {"coalesceIdleMs": 99},
                        "defaultAgent": "legacy-reviewer",
                        "probeAgent": "legacy-probe",
                    },
                    "machineOnly": True,
                },
            },
        },
        "mcp": {"servers": {"machine-local": {"command": "stale"}}},
    }
    local_path = tmp_path / "live.json"
    _write_json(local_path, local)
    inputs = _assembly_inputs(
        tmp_path,
        research_v2_root=str(tmp_path / "custom research root with spaces"),
    )

    python_config = assemble_config(
        cast(JsonObject, load_json(local_path)),
        cast(JsonObject, load_json(REPO_CONFIG)),
        inputs,
    )

    assert serialize_json(python_config) == _jq_full_assembly(local_path, REPO_CONFIG, inputs)
    mcp = cast(JsonObject, python_config["mcp"])
    mcp_servers = cast(JsonObject, mcp["servers"])
    g2_control = cast(JsonObject, mcp_servers["g2-control"])
    assert g2_control["env"] == {
        "PYTHONPATH": inputs.repo_root,
        "RESEARCH_V2_ROOT": inputs.research_v2_root,
        "RESEARCH_CORE_DATABASE": inputs.research_core_database,
        "OPENCLAW_HOST": inputs.openclaw_host,
        "OPENCLAW_PORT": inputs.openclaw_port,
        "G2_OWNER_ENV_FILE": inputs.owner_env_file,
    }
    assert "acp" not in python_config
    assembled_agents = cast(JsonObject, python_config["agents"])
    assert assembled_agents["ownership"] == "explicit"
    assert "list" not in assembled_agents
    assembled_entries = cast(JsonObject, assembled_agents["entries"])
    assert set(assembled_entries) == {"main", "research-orchestrator"}
    assert all(
        isinstance(entry, dict) and "id" not in entry for entry in assembled_entries.values()
    )
    defaults = cast(JsonObject, cast(JsonObject, python_config["agents"])["defaults"])
    subagents = cast(JsonObject, defaults["subagents"])
    assert subagents["archiveAfterMinutes"] == JsonNumber("7")
    assert subagents["requireAgentId"] is True
    plugins = cast(JsonObject, python_config["plugins"])
    assert "load" not in plugins
    entries = cast(JsonObject, plugins["entries"])
    assert "acpx" not in entries


@pytest.mark.parametrize(
    ("provider", "model_primary", "model_provider", "model_id"),
    [
        ("azure", "azure-oai-g2/gpt-5.4", "azure-oai-g2", "gpt-5.4"),
        ("openrouter", "openrouter/openai/gpt-4.1", "openrouter", "openai/gpt-4.1"),
    ],
)
def test_explicit_alternate_openai_routes_remain_available(
    tmp_path: Path,
    provider: str,
    model_primary: str,
    model_provider: str,
    model_id: str,
) -> None:
    inputs = _assembly_inputs(
        tmp_path,
        provider=provider,
        model_primary=model_primary,
        model_provider=model_provider,
        model_id=model_id,
    )

    assembled = assemble_config(
        cast(JsonObject, {}), cast(JsonObject, load_json(REPO_CONFIG)), inputs
    )
    defaults = cast(JsonObject, cast(JsonObject, assembled["agents"])["defaults"])
    assert cast(JsonObject, defaults["model"])["primary"] == model_primary


def test_empty_provider_environment_defaults_to_codex_in_python_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_path = tmp_path / "live.json"
    _write_json(
        local_path,
        {
            "meta": {"lastTouchedAt": "machine-local"},
            "agents": {"defaults": {"memorySearch": {"enabled": False}}},
            "commands": {"ownerDisplay": "raw"},
        },
    )
    home = tmp_path / "fake-home"
    push_home = home / ".openclaw"
    arguments = [
        "assemble",
        "--migration-record",
        str(tmp_path / "migration-record.json"),
        "--",
        str(local_path),
        str(REPO_CONFIG),
        str(REPO_ROOT),
        str(push_home),
        str(REPO_ROOT / ".venv/bin/python"),
        str(home / ".local/share/mempalace/venv/bin/python"),
        str(home / ".mempalace/palace"),
        str(push_home / "mempalace-readonly-server.py"),
        str(tmp_path / "fastembed-cache"),
        "bge-base",
        "1",
        "gateway.g2_control_mcp_server",
        str(tmp_path / "custom research root with spaces"),
        json.dumps(list(READONLY_AGENTS)),
        json.dumps(list(G2_AGENTS)),
    ]
    environment = os.environ.copy()
    owner_env_file = tmp_path / "owner.env"
    owner_env_file.write_text("OPENCLAW_GATEWAY_TOKEN=fixture\n", encoding="utf-8")
    owner_env_file.chmod(0o600)
    core_database = push_home / "state/openclaw.sqlite"
    core_database.parent.mkdir(parents=True)
    core_database.write_bytes(b"fixture core database\n")
    environment["OPENCLAW_PROVIDER"] = ""
    environment["OPENAI_MODEL"] = "gpt-5.4"
    environment["G2_OWNER_ENV_FILE"] = str(owner_env_file)
    environment["RESEARCH_CORE_DATABASE"] = str(core_database)
    environment["OPENCLAW_HOST"] = "127.0.0.1"
    environment["OPENCLAW_PORT"] = "18789"
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONPATH"] = str(REPO_ROOT)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = subprocess.run(
        [sys.executable, "-m", "gateway.deployment.config_merge", *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    published = json.loads(result.stdout)
    assert published["agents"]["defaults"]["model"]["primary"] == "openai/gpt-5.4"
    assert published["memory"]["search"]["enabled"] is False
    assert json.loads((tmp_path / "migration-record.json").read_text()) == {
        "removed": [
            {"path": "meta.lastTouchedAt", "value": "machine-local"},
            {
                "path": "agents.defaults.memorySearch",
                "value": {"enabled": False},
                "mapped_to": "memory.search.enabled",
            },
            {"path": "commands.ownerDisplay", "value": "raw"},
        ]
    }
    assert (tmp_path / "migration-record.json").stat().st_mode & 0o777 == 0o600


def test_python_serializer_uses_jq_style_unicode_and_newline() -> None:
    value: JsonObject = {"unicode": "café 中", "control": "line\nfeed", "number": 1.0}

    assert (
        serialize_json(value)
        == (
            '{\n  "unicode": "café 中",\n  "control": "line\\nfeed",\n  "number": 1.0\n}\n'
        ).encode()
    )


# ---------------------------------------------------------------------------
# Claude profile assembly and validation
# ---------------------------------------------------------------------------

Cfg = dict[str, Any]
CLAUDE = load_profile("claude")
CODEX = load_profile("codex")
CHILD_IDS = ("research-implementer", "research-runner", "research-reviewer")


def _claude_inputs(tmp_path: Path) -> AssemblyInputs:
    base = _assembly_inputs(
        tmp_path,
        provider="claude",
        model_primary=CLAUDE.role("main").model_ref,
        model_provider="anthropic",
        model_id=CLAUDE.role("main").model_id,
        orchestrator_model_primary=CLAUDE.role("owner").model_ref,
    )
    state = Path(base.openclaw_state_root)
    return dataclasses.replace(
        base,
        profile=CLAUDE,
        owner_workspace_root=str(state / "workspace-research-orchestrator"),
        hypothesis_worktrees_root=str(Path(base.research_v2_root) / "hypothesis-worktrees"),
        gateway_cli=str(REPO_ROOT / ".venv/bin/gateway-cli"),
    )


def _expectations(inputs: AssemblyInputs) -> ValidationExpectations:
    return ValidationExpectations(
        repo_root=inputs.repo_root,
        python_bin=inputs.python_bin,
        mempalace_python=inputs.mempalace_python,
        mempalace_palace=inputs.mempalace_palace,
        mempalace_wrapper=inputs.mempalace_wrapper,
        fastembed_cache=inputs.fastembed_cache,
        mempalace_embedding_model=inputs.mempalace_embedding_model,
        hf_hub_offline=inputs.hf_hub_offline,
        g2_module=inputs.g2_module,
        research_v2_root=inputs.research_v2_root,
        research_core_database=inputs.research_core_database,
        owner_env_file=inputs.owner_env_file,
        openclaw_host=inputs.openclaw_host,
        openclaw_port=inputs.openclaw_port,
        readonly_server_agents=inputs.mempalace_readonly_agents,
        g2_server_agents=inputs.g2_agents,
        main_tool_allow=tuple(
            json.loads(REPO_CONFIG.read_text(encoding="utf-8"))["agents"]["entries"]["main"][
                "tools"
            ]["allow"]
        ),
        hypothesis_worktrees_root=inputs.hypothesis_worktrees_root,
        openclaw_state_root=inputs.openclaw_state_root,
        owner_workspace_root=inputs.owner_workspace_root,
        gateway_cli=inputs.gateway_cli,
    )


def _assemble_claude(tmp_path: Path) -> tuple[JsonObject, AssemblyInputs]:
    inputs = _claude_inputs(tmp_path)
    config = assemble_config(cast(JsonObject, {}), cast(JsonObject, load_json(REPO_CONFIG)), inputs)
    return config, inputs


def test_codex_profile_assembly_is_unchanged_by_explicit_profile(tmp_path: Path) -> None:
    repo = cast(JsonObject, load_json(REPO_CONFIG))
    implicit = _assembly_inputs(tmp_path)
    explicit = dataclasses.replace(implicit, profile=CODEX)
    first = serialize_json(assemble_config({}, repo, implicit))
    assert first == serialize_json(assemble_config({}, repo, explicit))
    assert b"g2-agent-guard" not in first
    assert b"claude-cli" not in first


def test_codex_validate_accepts_codex_assembly_and_rejects_drift(tmp_path: Path) -> None:
    inputs = _assembly_inputs(tmp_path)
    config = assemble_config({}, cast(JsonObject, load_json(REPO_CONFIG)), inputs)
    expect = _expectations(inputs)
    plain = json.loads(serialize_json(config))
    assert validate_config(plain, CODEX, expect) == []
    plain["plugins"]["allow"] = ["codex"]
    plain["plugins"]["load"] = {"paths": ["/x"]}
    problems = validate_config(plain, CODEX, expect)
    assert any("codex and openai" in problem for problem in problems)
    assert any("plugins.load.paths" in problem for problem in problems)


def test_claude_assembly_from_real_repo_overlay(tmp_path: Path) -> None:
    config, inputs = _assemble_claude(tmp_path)
    plain = json.loads(serialize_json(config))
    state = Path(inputs.openclaw_state_root)
    agents = plain["agents"]
    defaults = agents["defaults"]
    refs = [
        CLAUDE.role(name).model_ref
        for name in ("main", "owner", "implementer", "runner", "reviewer")
    ]
    distinct = list(dict.fromkeys(refs))
    assert defaults["model"]["primary"] == CLAUDE.role("main").model_ref
    assert defaults["modelPolicy"]["allow"] == distinct
    assert defaults["models"] == {ref: {"agentRuntime": {"id": "claude-cli"}} for ref in distinct}
    entries = agents["entries"]
    assert sorted(entries) == sorted(["main", "research-orchestrator", *CHILD_IDS])
    assert entries["main"]["model"]["primary"] == CLAUDE.role("main").model_ref
    assert entries["main"]["thinkingDefault"] == CLAUDE.role("main").effort
    owner = entries["research-orchestrator"]
    assert owner["model"]["primary"] == CLAUDE.role("owner").model_ref
    assert owner["thinkingDefault"] == CLAUDE.role("owner").effort
    assert owner["subagents"]["allowAgents"] == list(CHILD_IDS)
    assert owner["tools"] == {"profile": "full", "deny": ["g2-control__*", "mempalace-readonly__*"]}
    assert (
        entries["main"]["tools"]["allow"]
        == json.loads(REPO_CONFIG.read_text())["agents"]["entries"]["main"]["tools"]["allow"]
    )
    for agent_id, role in zip(CHILD_IDS, ("implementer", "runner", "reviewer"), strict=True):
        child = entries[agent_id]
        assert child["workspace"] == str(state / f"workspace-{agent_id}")
        assert child["workspace"] != inputs.hypothesis_worktrees_root
        assert "sandbox" not in child
        assert child["model"] == {"primary": CLAUDE.role(role).model_ref}
        assert child["thinkingDefault"] == CLAUDE.role(role).effort
        assert child["tools"]["deny"] == [
            "sessions_spawn",
            "sessions_yield",
            "sessions_send",
            "sessions_list",
            "sessions_history",
            "agents_list",
            "g2-control__*",
            "mempalace-readonly__*",
        ]
        assert child["tools"]["profile"] == "full"
    plugins = plain["plugins"]
    assert sorted(plugins["allow"]) == ["anthropic", "g2-agent-guard"]
    assert "codex" not in plugins["allow"]
    assert "openai" not in plugins["allow"]
    assert plugins["entries"]["anthropic"]["enabled"] is True
    assert "codex" not in plugins["entries"]
    assert plugins["load"]["paths"] == [str(guard_plugin_dir())]
    assert "g2-agent-guard" in plugins["allow"]
    assert plugins["entries"]["g2-agent-guard"] == {
        "enabled": True,
        "config": json.loads(
            json.dumps(
                guard_config(
                    CLAUDE,
                    owner_workspace_root=Path(inputs.owner_workspace_root),
                    hypothesis_worktrees_root=Path(inputs.hypothesis_worktrees_root),
                    research_root=Path(inputs.research_v2_root),
                    gateway_cli=Path(inputs.gateway_cli),
                )
            )
        ),
    }
    for server in plain["mcp"]["servers"].values():
        assert "codex" not in server
    assert sorted(plain["mcp"]["servers"]) == ["g2-control", "mempalace-readonly"]
    assert validate_config(plain, CLAUDE, _expectations(inputs)) == []


def test_claude_assembly_requires_guard_paths(tmp_path: Path) -> None:
    inputs = dataclasses.replace(_claude_inputs(tmp_path), gateway_cli="")
    with pytest.raises(ConfigMergeError, match="--gateway-cli"):
        assemble_config({}, cast(JsonObject, load_json(REPO_CONFIG)), inputs)


def _plain_claude(tmp_path: Path) -> tuple[Cfg, ValidationExpectations]:
    config, inputs = _assemble_claude(tmp_path)
    return json.loads(serialize_json(config)), _expectations(inputs)


def _mutate_child_sandbox(c: Cfg) -> None:
    c["agents"]["entries"]["research-runner"]["sandbox"] = {"mode": "all"}


def _mutate_guard_config(c: Cfg) -> None:
    c["plugins"]["entries"]["g2-agent-guard"]["config"]["agents"] = {}


def _mutate_guard_missing(c: Cfg) -> None:
    del c["plugins"]["entries"]["g2-agent-guard"]


def _mutate_guard_not_allowed(c: Cfg) -> None:
    c["plugins"]["allow"].remove("g2-agent-guard")


def _mutate_guard_path(c: Cfg) -> None:
    c["plugins"]["load"]["paths"] = ["/elsewhere"]


def _mutate_no_anthropic(c: Cfg) -> None:
    c["plugins"]["allow"].remove("anthropic")


def _mutate_codex_allowed(c: Cfg) -> None:
    c["plugins"]["allow"].append("codex")


def _mutate_openai_allowed(c: Cfg) -> None:
    c["plugins"]["allow"].append("openai")


def _mutate_policy(c: Cfg) -> None:
    c["agents"]["defaults"]["modelPolicy"]["allow"] = ["anthropic/other"]


def _mutate_runtime(c: Cfg) -> None:
    models = c["agents"]["defaults"]["models"]
    del models[next(iter(models))]


def _mutate_runtime_id(c: Cfg) -> None:
    models = c["agents"]["defaults"]["models"]
    models[next(iter(models))]["agentRuntime"]["id"] = "codex"


def _mutate_worktrees_workspace(c: Cfg) -> None:
    entries = c["agents"]["entries"]
    root = c["mcp"]["servers"]["g2-control"]["env"]["RESEARCH_V2_ROOT"]
    entries["research-implementer"]["workspace"] = f"{root}/hypothesis-worktrees"


def _mutate_missing_child(c: Cfg) -> None:
    del c["agents"]["entries"]["research-reviewer"]


def _mutate_allow_agents(c: Cfg) -> None:
    c["agents"]["entries"]["research-orchestrator"]["subagents"]["allowAgents"] = []


def _mutate_child_model(c: Cfg) -> None:
    c["agents"]["entries"]["research-reviewer"]["model"]["primary"] = "anthropic/x"


def _mutate_codex_projection(c: Cfg) -> None:
    c["mcp"]["servers"]["g2-control"]["codex"] = {"agents": ["main"]}


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_mutate_child_sandbox, "sandbox key"),
        (_mutate_guard_config, "guard config"),
        (_mutate_guard_missing, "g2-agent-guard.enabled"),
        (_mutate_guard_not_allowed, "plugins.allow must contain g2-agent-guard"),
        (_mutate_guard_path, "plugins.load.paths"),
        (_mutate_no_anthropic, "must contain anthropic"),
        (_mutate_codex_allowed, "must not contain codex or openai"),
        (_mutate_openai_allowed, "must not contain codex or openai"),
        (_mutate_policy, "modelPolicy.allow"),
        (_mutate_runtime, "agents.defaults.models"),
        (_mutate_runtime_id, "agentRuntime.id must be claude-cli"),
        (_mutate_worktrees_workspace, "must not be the hypothesis worktrees root"),
        (_mutate_missing_child, "agents.entries must be exactly"),
        (_mutate_allow_agents, "subagent allow list"),
        (_mutate_child_model, "research-reviewer must use"),
        (_mutate_codex_projection, "codex.agents projection"),
    ],
)
def test_claude_validate_rejects_violation(
    tmp_path: Path, mutate: Callable[[Cfg], None], needle: str
) -> None:
    plain, expect = _plain_claude(tmp_path)
    broken = copy.deepcopy(plain)
    mutate(broken)
    problems = validate_config(broken, CLAUDE, expect)
    assert any(needle in problem for problem in problems), problems


def test_claude_validate_requires_guard_flags(tmp_path: Path) -> None:
    plain, expect = _plain_claude(tmp_path)
    problems = validate_config(plain, CLAUDE, dataclasses.replace(expect, gateway_cli=""))
    assert any("--gateway-cli" in problem for problem in problems)


def _cli_args(tmp_path: Path, inputs: AssemblyInputs, local: Path) -> list[str]:
    return [
        str(local),
        str(REPO_CONFIG),
        inputs.repo_root,
        inputs.openclaw_state_root,
        inputs.python_bin,
        inputs.mempalace_python,
        inputs.mempalace_palace,
        inputs.mempalace_wrapper,
        inputs.fastembed_cache,
        inputs.mempalace_embedding_model,
        inputs.hf_hub_offline,
        inputs.g2_module,
        inputs.research_v2_root,
        json.dumps(list(READONLY_AGENTS)),
        json.dumps(list(G2_AGENTS)),
    ]


def _cli_env(inputs: AssemblyInputs, **extra: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        G2_OWNER_ENV_FILE=inputs.owner_env_file,
        RESEARCH_CORE_DATABASE=inputs.research_core_database,
        OPENCLAW_HOST="127.0.0.1",
        OPENCLAW_PORT="18789",
        PYTHONSAFEPATH="1",
        PYTHONPATH=str(REPO_ROOT),
    )
    environment.pop("OPENCLAW_PROVIDER", None)
    environment.update(extra)
    return environment


def _run_cli(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "gateway.deployment.config_merge", *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def test_claude_cli_assemble_then_validate_roundtrip(tmp_path: Path) -> None:
    inputs = _claude_inputs(tmp_path)
    local = tmp_path / "live.json"
    _write_json(local, {})
    path_flags = [
        "--hypothesis-worktrees-root",
        inputs.hypothesis_worktrees_root,
        "--owner-workspace-root",
        inputs.owner_workspace_root,
        "--gateway-cli",
        inputs.gateway_cli,
    ]
    env = _cli_env(inputs, OPENCLAW_PROVIDER="azure")
    assembled = _run_cli(
        ["assemble", "--profile", "claude", *path_flags, "--", *_cli_args(tmp_path, inputs, local)],
        env,
    )
    assert assembled.returncode == 0, assembled.stderr
    assert "OPENCLAW_PROVIDER=azure is ignored" in assembled.stderr
    published = json.loads(assembled.stdout)
    assert published["agents"]["defaults"]["model"]["primary"] == CLAUDE.role("main").model_ref
    config_path = tmp_path / "assembled.json"
    config_path.write_text(assembled.stdout, encoding="utf-8")
    expect = _expectations(inputs)
    validate_args = [
        "validate",
        "--profile",
        "claude",
        "--config",
        str(config_path),
        "--repo-root",
        inputs.repo_root,
        "--python-bin",
        inputs.python_bin,
        "--mempalace-python",
        inputs.mempalace_python,
        "--mempalace-palace",
        inputs.mempalace_palace,
        "--mempalace-wrapper",
        inputs.mempalace_wrapper,
        "--fastembed-cache",
        inputs.fastembed_cache,
        "--mempalace-embedding-model",
        inputs.mempalace_embedding_model,
        "--hf-hub-offline",
        inputs.hf_hub_offline,
        "--g2-module",
        inputs.g2_module,
        "--research-v2-root",
        inputs.research_v2_root,
        "--research-core-database",
        inputs.research_core_database,
        "--owner-env-file",
        inputs.owner_env_file,
        "--openclaw-host",
        inputs.openclaw_host,
        "--openclaw-port",
        inputs.openclaw_port,
        "--openclaw-state-root",
        inputs.openclaw_state_root,
        "--readonly-server-agents-json",
        json.dumps(list(READONLY_AGENTS)),
        "--g2-server-agents-json",
        json.dumps(list(G2_AGENTS)),
        "--main-tool-allow-json",
        json.dumps(list(expect.main_tool_allow)),
        *path_flags,
    ]
    ok = _run_cli(validate_args, env)
    assert ok.returncode == 0, ok.stderr
    published["agents"]["entries"]["research-runner"]["sandbox"] = {"mode": "all"}
    config_path.write_text(json.dumps(published), encoding="utf-8")
    bad = _run_cli(validate_args, env)
    assert bad.returncode == 1
    assert "sandbox key" in bad.stderr


def test_claude_assembly_strips_codex_acpx_and_plugin_denies_from_local_config(
    tmp_path: Path,
) -> None:
    inputs = _claude_inputs(tmp_path)
    local = cast(
        JsonObject,
        {
            "acp": {"defaultAgent": "x"},
            "plugins": {
                "enabled": False,
                "allow": ["codex", "openai", "acpx", "other"],
                "deny": ["anthropic", "g2-agent-guard", "keep-me"],
                "load": {"paths": ["/stale/plugin"]},
                "entries": {
                    "codex": {"enabled": True, "config": {"appServer": {"sandbox": "x"}}},
                    "openai": {"enabled": True},
                    "acpx": {"enabled": True},
                },
            },
        },
    )
    config = assemble_config(local, cast(JsonObject, load_json(REPO_CONFIG)), inputs)
    plain = json.loads(serialize_json(config))
    plugins = plain["plugins"]
    assert sorted(plugins["allow"]) == ["anthropic", "g2-agent-guard"]
    assert plugins["deny"] == ["keep-me"]
    assert "enabled" not in plugins
    assert plugins["load"]["paths"] == [str(guard_plugin_dir())]
    assert {"codex", "openai", "acpx"}.isdisjoint(plugins["entries"])
    assert "acp" not in plain
    assert validate_config(plain, CLAUDE, _expectations(inputs)) == []


def test_claude_plugin_deny_dropped_entirely_when_only_managed_ids(tmp_path: Path) -> None:
    inputs = _claude_inputs(tmp_path)
    local = cast(JsonObject, {"plugins": {"deny": ["anthropic", "g2-agent-guard"]}})
    config = assemble_config(local, cast(JsonObject, load_json(REPO_CONFIG)), inputs)
    assert "deny" not in cast(JsonObject, config["plugins"])


def test_claude_assembly_fails_when_owner_workspace_differs_from_flag(tmp_path: Path) -> None:
    inputs = dataclasses.replace(
        _claude_inputs(tmp_path), owner_workspace_root=str(tmp_path / "elsewhere")
    )
    with pytest.raises(ConfigMergeError, match="differs from --owner-workspace-root"):
        assemble_config({}, cast(JsonObject, load_json(REPO_CONFIG)), inputs)


def _mutate_plugins_deny(c: Cfg) -> None:
    c["plugins"]["deny"] = ["anthropic"]


def _mutate_plugins_deny_guard(c: Cfg) -> None:
    c["plugins"]["deny"] = ["g2-agent-guard"]


def _mutate_plugins_disabled(c: Cfg) -> None:
    c["plugins"]["enabled"] = False


def _mutate_extra_plugin(c: Cfg) -> None:
    c["plugins"]["allow"].append("extra")


def _mutate_owner_workspace(c: Cfg) -> None:
    c["agents"]["entries"]["research-orchestrator"]["workspace"] = "/somewhere/else"


def _mutate_acpx_entry(c: Cfg) -> None:
    c["plugins"]["entries"]["acpx"] = {"enabled": True}


def _mutate_acpx_allow(c: Cfg) -> None:
    c["plugins"]["allow"].append("acpx")


def _mutate_acp(c: Cfg) -> None:
    c["acp"] = {"defaultAgent": "x"}


def _mutate_main_allow(c: Cfg) -> None:
    c["agents"]["entries"]["main"]["tools"]["allow"].append("exec")


def _mutate_main_deny(c: Cfg) -> None:
    c["agents"]["entries"]["main"]["tools"]["deny"].remove("exec")


def _mutate_owner_mcp_deny(c: Cfg) -> None:
    del c["agents"]["entries"]["research-orchestrator"]["tools"]["deny"]


def _mutate_child_mcp_deny(c: Cfg) -> None:
    deny = c["agents"]["entries"]["research-runner"]["tools"]["deny"]
    deny.remove("g2-control__*")


def _mutate_child_models_key(c: Cfg) -> None:
    c["agents"]["entries"]["research-runner"]["models"] = {}


def _mutate_entry_agent_runtime(c: Cfg) -> None:
    c["agents"]["entries"]["main"]["agentRuntime"] = {"id": "codex"}


def _mutate_bool_as_number(c: Cfg) -> None:
    c["agents"]["defaults"]["subagents"]["maxConcurrent"] = True


def _mutate_allow_agents_string(c: Cfg) -> None:
    c["agents"]["entries"]["research-orchestrator"]["subagents"]["allowAgents"] = "x"


def _mutate_main_allow_agents_number(c: Cfg) -> None:
    c["agents"]["entries"]["main"]["subagents"] = {"allowAgents": 5}


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_mutate_plugins_deny, "plugins.deny"),
        (_mutate_plugins_deny_guard, "plugins.deny"),
        (_mutate_plugins_disabled, "plugins.enabled"),
        (_mutate_extra_plugin, "plugins.allow must be exactly"),
        (_mutate_owner_workspace, "research-orchestrator.workspace"),
        (_mutate_acpx_entry, "acpx"),
        (_mutate_acpx_allow, "acpx"),
        (_mutate_acp, "acp must not exist"),
        (_mutate_main_allow, "main tools must keep"),
        (_mutate_main_deny, "main tools must keep"),
        (_mutate_owner_mcp_deny, "managed MCP server denies"),
        (_mutate_child_mcp_deny, "research-runner must be a leaf agent"),
        (_mutate_child_models_key, "must not carry models"),
        (_mutate_entry_agent_runtime, "must not carry agentRuntime"),
        (_mutate_bool_as_number, "subagents.maxConcurrent"),
        (_mutate_allow_agents_string, "subagent allow list"),
        (_mutate_main_allow_agents_number, "no subagents"),
    ],
)
def test_claude_validate_rejects_more_violations(
    tmp_path: Path, mutate: Callable[[Cfg], None], needle: str
) -> None:
    plain, expect = _plain_claude(tmp_path)
    broken = copy.deepcopy(plain)
    mutate(broken)
    problems = validate_config(broken, CLAUDE, expect)
    assert any(needle in problem for problem in problems), problems


def test_claude_validate_requires_repo_gateway_cli_and_absolute_research_root(
    tmp_path: Path,
) -> None:
    plain, expect = _plain_claude(tmp_path)
    wrong_cli = dataclasses.replace(expect, gateway_cli="/usr/local/bin/gateway-cli")
    assert any(
        ".venv/bin/gateway-cli" in problem for problem in validate_config(plain, CLAUDE, wrong_cli)
    )
    relative = dataclasses.replace(expect, research_v2_root="relative/root")
    problems = validate_config(plain, CLAUDE, relative)
    assert any("must be absolute" in problem for problem in problems)


def test_unsupported_profile_runtime_is_a_config_merge_error(tmp_path: Path) -> None:
    bogus = dataclasses.replace(CLAUDE, runtime="bogus")
    with pytest.raises(ConfigMergeError, match="Unsupported agent profile runtime"):
        _profile_for(dataclasses.replace(_claude_inputs(tmp_path), profile=bogus))


def test_cli_labels_main_tool_allow_errors(tmp_path: Path) -> None:
    inputs = _claude_inputs(tmp_path)
    config_path = tmp_path / "c.json"
    config_path.write_text("{}", encoding="utf-8")
    result = _run_cli(
        [
            "validate",
            "--profile",
            "codex",
            "--config",
            str(config_path),
            *_validate_string_args(inputs),
            "--readonly-server-agents-json",
            "[]",
            "--g2-server-agents-json",
            "[]",
            "--main-tool-allow-json",
            "{}",
        ],
        _cli_env(inputs),
    )
    assert result.returncode == 1
    assert "main tool allow must be a JSON string array" in result.stderr
    assert "Traceback" not in result.stderr


def _validate_string_args(inputs: AssemblyInputs) -> list[str]:
    pairs = {
        "--repo-root": inputs.repo_root,
        "--python-bin": inputs.python_bin,
        "--mempalace-python": inputs.mempalace_python,
        "--mempalace-palace": inputs.mempalace_palace,
        "--mempalace-wrapper": inputs.mempalace_wrapper,
        "--fastembed-cache": inputs.fastembed_cache,
        "--mempalace-embedding-model": inputs.mempalace_embedding_model,
        "--hf-hub-offline": inputs.hf_hub_offline,
        "--g2-module": inputs.g2_module,
        "--research-v2-root": inputs.research_v2_root,
        "--research-core-database": inputs.research_core_database,
        "--owner-env-file": inputs.owner_env_file,
        "--openclaw-host": inputs.openclaw_host,
        "--openclaw-port": inputs.openclaw_port,
    }
    return [item for pair in pairs.items() for item in pair]


# ---------------------------------------------------------------------------
# Codex parity with the push script's jq invariant program
# ---------------------------------------------------------------------------


def _push_script_jq_program() -> str:
    lines = (REPO_ROOT / "scripts/push-openclaw-config.sh").read_text(encoding="utf-8").split("\n")
    start = next(
        i for i, line in enumerate(lines) if line.strip() == "def denies: (.tools.deny // []);"
    )
    end = next(
        i for i in range(start, len(lines)) if lines[i].strip().startswith("' >/dev/null; then")
    )
    return "\n".join(lines[start:end])


def _jq_verdict(config: object, inputs: AssemblyInputs, expect: ValidationExpectations) -> bool:
    assert JQ is not None
    arguments = [
        ("owner", inputs.orchestrator_model_primary),
        ("cmd", inputs.mempalace_python),
        ("palace", inputs.mempalace_palace),
        ("wrapper", inputs.mempalace_wrapper),
        ("cache", inputs.fastembed_cache),
        ("model", inputs.mempalace_embedding_model),
        ("offline", inputs.hf_hub_offline),
        ("repo", inputs.repo_root),
        ("python", inputs.python_bin),
        ("g2_module", inputs.g2_module),
        ("research_root", inputs.research_v2_root),
        ("research_core_database", inputs.research_core_database),
        ("owner_env_file", inputs.owner_env_file),
        ("openclaw_host", inputs.openclaw_host),
        ("openclaw_port", inputs.openclaw_port),
    ]
    command = [JQ, "-e"]
    for name, value in arguments:
        command += ["--arg", name, value]
    command += [
        "--argjson",
        "readonly_server_agents",
        json.dumps(list(expect.readonly_server_agents)),
        "--argjson",
        "g2_server_agents",
        json.dumps(list(expect.g2_server_agents)),
        "--argjson",
        "main_openclaw_allow",
        json.dumps(list(expect.main_tool_allow)),
        _push_script_jq_program(),
    ]
    result = subprocess.run(
        command, input=json.dumps(config), text=True, capture_output=True, check=False
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def _codex_mutations() -> dict[str, Callable[[Cfg], None]]:
    def at(c: Cfg, *path: str) -> Any:
        current: Any = c
        for key in path:
            current = current[key]
        return current

    return {
        "none": lambda c: None,
        "thinking": lambda c: at(c, "agents", "defaults").update(thinkingDefault="low"),
        "ownership": lambda c: at(c, "agents").update(ownership="implicit"),
        "agents-list": lambda c: at(c, "agents").update(list=[]),
        "entry-id": lambda c: at(c, "agents", "entries", "main").update(id="main"),
        "extra-entry": lambda c: at(c, "agents", "entries").update(extra={}),
        "allow-acpx": lambda c: at(c, "plugins", "allow").append("acpx"),
        "allow-missing-openai": lambda c: at(c, "plugins").update(allow=["codex"]),
        "load-paths": lambda c: at(c, "plugins").update(load={"paths": ["/x"]}),
        "codex-disabled": lambda c: at(c, "plugins", "entries", "codex").update(enabled=False),
        "native-tool-surface": lambda c: at(c, "plugins", "entries", "codex", "config").update(
            nativeToolSurfaceEnabled=True
        ),
        "sandbox": lambda c: at(c, "plugins", "entries", "codex", "config", "appServer").update(
            sandbox="danger-full-access"
        ),
        "proxy": lambda c: at(c, "plugins", "entries", "codex", "config", "appServer").update(
            networkProxy="x"
        ),
        "retired-root": lambda c: at(
            c, "plugins", "entries", "codex", "config", "appServer"
        ).update(defaultWorkspaceDir="/home/dev/.openclaw/autoresearch/model-workspaces"),
        "acpx-entry": lambda c: at(c, "plugins", "entries").update(acpx={}),
        "acp": lambda c: c.update(acp={}),
        "max-concurrent": lambda c: at(c, "agents", "defaults").update(maxConcurrent=3),
        "sub-concurrent": lambda c: at(c, "agents", "defaults", "subagents").update(
            maxConcurrent=2
        ),
        "depth": lambda c: at(c, "agents", "defaults", "subagents").update(maxSpawnDepth=2),
        "timeout": lambda c: at(c, "agents", "defaults", "subagents").update(runTimeoutSeconds=9),
        "memory-search": lambda c: at(c, "memory", "search").update(enabled=True),
        "compaction": lambda c: at(c, "agents", "defaults", "compaction").update(mode="safeguard"),
        "flush": lambda c: at(c, "agents", "defaults", "compaction", "memoryFlush").update(
            enabled=True
        ),
        "tools-deny": lambda c: at(c, "tools", "deny").remove("memory_get"),
        "extra-server": lambda c: at(c, "mcp", "servers").update(extra={}),
        "mempalace-command": lambda c: at(c, "mcp", "servers", "mempalace-readonly").update(
            command="x"
        ),
        "mempalace-agents": lambda c: at(c, "mcp", "servers", "mempalace-readonly", "codex").update(
            agents=["main", "x"]
        ),
        "g2-env": lambda c: at(c, "mcp", "servers", "g2-control", "env").update(OPENCLAW_PORT="1"),
        "g2-approval": lambda c: at(c, "mcp", "servers", "g2-control", "codex").update(
            defaultToolsApprovalMode="ask"
        ),
        "main-model": lambda c: at(c, "agents", "entries", "main", "model").update(primary="x"),
        "main-profile": lambda c: at(c, "agents", "entries", "main", "tools").update(
            profile="full"
        ),
        "main-allow": lambda c: at(c, "agents", "entries", "main", "tools", "allow").append("x"),
        "main-deny": lambda c: at(c, "agents", "entries", "main", "tools", "deny").remove("exec"),
        "main-skills": lambda c: at(c, "agents", "entries", "main").update(skills=["mempalace"]),
        "main-subagents": lambda c: at(c, "agents", "entries", "main").update(
            subagents={"allowAgents": ["x"]}
        ),
        "owner-model": lambda c: at(
            c, "agents", "entries", "research-orchestrator", "model"
        ).update(primary="openai/gpt-5.4"),
        "owner-tools": lambda c: at(
            c, "agents", "entries", "research-orchestrator", "tools"
        ).update(deny=["exec"]),
        "owner-subagents": lambda c: at(c, "agents", "entries", "research-orchestrator").update(
            subagents={"allowAgents": ["main"]}
        ),
        "owner-skills": lambda c: at(c, "agents", "entries", "research-orchestrator").update(
            skills=[]
        ),
    }


@pytest.mark.skipif(JQ is None, reason="jq is required for the invariant parity test")
@pytest.mark.parametrize("name", list(_codex_mutations()))
def test_codex_validate_has_the_same_verdict_as_the_push_script_jq(
    tmp_path: Path, name: str
) -> None:
    inputs = _assembly_inputs(tmp_path)
    config = assemble_config({}, cast(JsonObject, load_json(REPO_CONFIG)), inputs)
    plain = json.loads(serialize_json(config))
    expect = _expectations(inputs)
    _codex_mutations()[name](plain)
    jq_ok = _jq_verdict(plain, inputs, expect)
    python_ok = validate_config(plain, CODEX, expect) == []
    assert python_ok == jq_ok
    assert jq_ok == (name == "none")
