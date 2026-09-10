"""Golden tests for the jq-to-Python OpenClaw config assembly port."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from gateway.deployment.config_merge import (
    AssemblyInputs,
    ConfigMergeError,
    JsonNumber,
    JsonObject,
    JsonValue,
    assemble_config,
    assemble_config_with_migration,
    deep_merge,
    load_json,
    serialize_json,
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


def test_assembly_keeps_explicit_alternate_interface_route_without_opus_fallback(
    tmp_path: Path,
) -> None:
    inputs = _assembly_inputs(
        tmp_path,
        provider="openrouter",
        model_primary="openrouter/anthropic/claude-sonnet-4-20250514",
        model_provider="openrouter",
        model_id="anthropic/claude-sonnet-4-20250514",
    )

    assembled = assemble_config(
        cast(JsonObject, {"agents": {"defaults": {"models": {"openai/unselected": {}}}}}),
        cast(JsonObject, load_json(REPO_CONFIG)),
        inputs,
    )

    defaults = cast(JsonObject, cast(JsonObject, assembled["agents"])["defaults"])
    assert "models" not in defaults
    assert defaults["modelPolicy"] == {
        "allow": [
            "openrouter/anthropic/claude-sonnet-4-20250514",
            "openai/gpt-6-astra",
            "openai/gpt-5.6-luna",
        ]
    }
    policy = defaults["modelPolicy"]
    assert isinstance(policy, dict)
    allow = cast(list[JsonValue], policy["allow"])
    assert all(isinstance(ref, str) and "opus" not in ref.lower() for ref in allow)


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
    assert defaults["modelPolicy"] == {"allow": ["openai/gpt-6-astra", "openai/gpt-5.6-luna"]}

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
    assert cast(JsonObject, migrated["meta"])["lastTouchedVersion"] == "2026.8.1"
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
        research_reviewer_launcher=str(REPO_ROOT / "scripts/research-reviewer-cli.py"),
        acpx_adapter_bin="/opt/acpx/claude-agent-acp",
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
            '{"allow": ([$primary, $owner, "openai/gpt-5.6-luna"] | '
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
            '(.agents.entries["research-orchestrator"] | .model.primary) = $owner | '
            '(.agents.entries["research-orchestrator"] | '
            '.thinkingDefault) = "high" | '
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
        [
            "--arg",
            "launcher",
            inputs.research_reviewer_launcher,
            "--arg",
            "adapter",
            inputs.acpx_adapter_bin,
            '.acp = {"enabled":true,"dispatch":{"enabled":true},'
            '"backend":"acpx","allowedAgents":["claude"]} | '
            ".plugins.entries.acpx.config = {"
            '"agents":{"claude":{"command":"/usr/bin/env","args":[('
            '"CLAUDE_CODE_EXECUTABLE=" + $launcher),$adapter]}},'
            '"permissionMode":"approve-reads","nonInteractivePermissions":"fail",'
            '"pluginToolsMcpBridge":false,"openClawToolsMcpBridge":false,"mcpServers":{}'
            "}",
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
            }
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
    assert python_config["acp"] == {
        "enabled": True,
        "dispatch": {"enabled": True},
        "backend": "acpx",
        "allowedAgents": ["claude"],
    }
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
    entries = cast(JsonObject, plugins["entries"])
    acpx = cast(JsonObject, entries["acpx"])
    assert acpx["machineOnly"] is True
    assert acpx["config"] == {
        "agents": {
            "claude": {
                "command": "/usr/bin/env",
                "args": [
                    "CLAUDE_CODE_EXECUTABLE=" + inputs.research_reviewer_launcher,
                    inputs.acpx_adapter_bin,
                ],
            }
        },
        "permissionMode": "approve-reads",
        "nonInteractivePermissions": "fail",
        "pluginToolsMcpBridge": False,
        "openClawToolsMcpBridge": False,
        "mcpServers": {},
    }


@pytest.mark.skipif(JQ is None, reason="jq is required for byte-equivalence golden tests")
def test_openrouter_api_key_substitution_is_byte_identical_to_jq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_key = "openrouter-test-key"  # pragma: allowlist secret
    monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
    local_path = tmp_path / "live.json"
    _write_json(local_path, {})
    inputs = _assembly_inputs(
        tmp_path,
        provider="openrouter",
        model_primary="openrouter/anthropic/claude-sonnet-4-20250514",
        model_provider="openrouter",
        model_id="anthropic/claude-sonnet-4-20250514",
    )

    python_config = assemble_config(
        cast(JsonObject, load_json(local_path)),
        cast(JsonObject, load_json(REPO_CONFIG)),
        inputs,
    )

    assert serialize_json(python_config) == _jq_full_assembly(
        local_path, REPO_CONFIG, inputs, openrouter_api_key=api_key
    )
    models = cast(JsonObject, python_config["models"])
    providers = cast(JsonObject, models["providers"])
    openrouter = cast(JsonObject, providers["openrouter"])
    assert openrouter["apiKey"] == api_key


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
        str(REPO_ROOT / "scripts/research-reviewer-cli.py"),
        "/opt/acpx/claude-agent-acp",
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
