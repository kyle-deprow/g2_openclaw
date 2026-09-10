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
    research_v2_root: str | None = None,
) -> AssemblyInputs:
    home = tmp_path / "fake-home"
    push_home = home / ".openclaw"
    resolved_research_root = research_v2_root or str(push_home / "research-v2")
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
        mempalace_readonly_agents=READONLY_AGENTS,
        g2_agents=G2_AGENTS,
        provider=provider,
        model_primary=model_primary,
        model_provider=model_provider,
        model_id=model_id,
        orchestrator_model_primary="openai/gpt-6-astra",
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
            '    "env": {"PYTHONPATH": $repo, "RESEARCH_V2_ROOT": $research_root}\n'
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
    agents_list = repo_agents["list"]
    merged = _run_jq(
        [
            "--argjson",
            "agents_list",
            _json_arg(agents_list),
            ".agents.list = $agents_list",
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
            ".agents.defaults.model.primary = $primary | "
            ".agents.defaults.models = {($primary): {}}",
        ],
        input_bytes=merged,
    )
    merged = _run_jq(
        [
            "--arg",
            "owner",
            inputs.orchestrator_model_primary,
            '(.agents.list[] | select(.id == "research-orchestrator") | .model.primary) = $owner | '
            '(.agents.list[] | select(.id == "research-orchestrator") | '
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
            '"backend":"acpx","allowedAgents":["claude"],'
            '"maxConcurrentSessions":1} | '
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
            },
            "list": [],
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
    }
    assert python_config["acp"] == {
        "enabled": True,
        "dispatch": {"enabled": True},
        "backend": "acpx",
        "allowedAgents": ["claude"],
        "maxConcurrentSessions": 1,
    }
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
    environment["OPENCLAW_PROVIDER"] = ""
    environment["OPENAI_MODEL"] = "gpt-5.4"
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
