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
    JsonObject,
    JsonValue,
    assemble_config,
    deep_merge,
    load_json,
    serialize_json,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_CONFIG = REPO_ROOT / "gateway/openclaw_config/openclaw.json"
JQ = shutil.which("jq")

STAGE_AGENT_IDS = (
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
)
READONLY_AGENTS = ("main", "autoresearch-pm", *STAGE_AGENT_IDS)
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


def _assembly_inputs(
    tmp_path: Path,
    *,
    provider: str = "codex",
    model_primary: str = "openai/gpt-5.4",
    model_provider: str = "openai",
    model_id: str = "gpt-5.4",
) -> AssemblyInputs:
    home = tmp_path / "fake-home"
    push_home = home / ".openclaw"
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
        mempalace_readonly_agents=READONLY_AGENTS,
        g2_agents=G2_AGENTS,
        provider=provider,
        model_primary=model_primary,
        model_provider=model_provider,
        model_id=model_id,
        pm_model_primary="openai/gpt-5.6-sol",
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
            '    "codex": {"agents": $readonly_agents},\n'
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
            '    "env": {"PYTHONPATH": $repo}\n'
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
    memory_search = repo_defaults.get("memorySearch")
    merged = _run_jq(
        [
            "--argjson",
            "memory_search",
            _json_arg(memory_search),
            ".agents.defaults.memorySearch = $memory_search",
        ],
        input_bytes=merged,
    )
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
            "pm",
            inputs.pm_model_primary,
            '(.agents.list[] | select(.id == "autoresearch-pm") | .model.primary) = $pm | '
            '(.agents.list[] | select(.id == "autoresearch-pm") | .thinkingDefault) = "high"',
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
        "agents": {"defaults": {"model": {"primary": "local/model"}}, "list": []},
        "tools": {"allow": ["stale"]},
        "memory": {"legacy": {"enabled": True}},
        "plugins": {
            "entries": {
                "codex": {
                    "config": {
                        "nativeToolSurfaceEnabled": True,
                        "codexDynamicToolsExclude": ["stale"],
                    }
                }
            }
        },
        "mcp": {"servers": {"machine-local": {"command": "stale"}}},
    }
    local_path = tmp_path / "live.json"
    _write_json(local_path, local)
    inputs = _assembly_inputs(tmp_path)

    python_config = assemble_config(
        cast(JsonObject, load_json(local_path)),
        cast(JsonObject, load_json(REPO_CONFIG)),
        inputs,
    )

    assert serialize_json(python_config) == _jq_full_assembly(local_path, REPO_CONFIG, inputs)


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
    _write_json(local_path, {})
    home = tmp_path / "fake-home"
    push_home = home / ".openclaw"
    arguments = [
        "assemble",
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
        json.dumps(list(READONLY_AGENTS)),
        json.dumps(list(G2_AGENTS)),
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


def test_python_serializer_uses_jq_style_unicode_and_newline() -> None:
    value: JsonObject = {"unicode": "café 中", "control": "line\nfeed", "number": 1.0}

    assert (
        serialize_json(value)
        == (
            '{\n  "unicode": "café 中",\n  "control": "line\\nfeed",\n  "number": 1.0\n}\n'
        ).encode()
    )
