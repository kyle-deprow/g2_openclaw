"""Black-box tests for pinned runtime version checks."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.deployment.versions import (
    REQUIRED_CODEX_APP_SERVER_VERSION,
    REQUIRED_CODEX_PLUGIN_VERSION,
    REQUIRED_OPENCLAW_VERSION,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_MODULE = "gateway.deployment.versions"


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_module(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", VERSIONS_MODULE, *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


@pytest.mark.parametrize("version", ["2026.7.1-1", "2026.7.2"])
def test_openclaw_version_rejects_older_and_newer_pins(tmp_path: Path, version: str) -> None:
    executable = tmp_path / "openclaw"
    _write_executable(executable, f"printf 'openclaw {version}\\n'")

    result = _run_module(
        ["require-openclaw-supported", str(executable), str(tmp_path / "config"), str(tmp_path)]
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: OpenClaw {version} at {executable} is unsupported; "
        f"need exactly {REQUIRED_OPENCLAW_VERSION}.\n"
    )


def test_openclaw_version_accepts_exact_pin(tmp_path: Path) -> None:
    executable = tmp_path / "openclaw"
    _write_executable(executable, f"printf 'openclaw {REQUIRED_OPENCLAW_VERSION}\\n'")

    result = _run_module(
        ["require-openclaw-supported", str(executable), str(tmp_path / "config"), str(tmp_path)]
    )

    assert result.returncode == 0
    assert result.stdout == f"{REQUIRED_OPENCLAW_VERSION}\n"
    assert result.stderr == ""


def _codex_executable(
    tmp_path: Path, plugin_version: str, app_server_version: str, *, stderr: str = ""
) -> tuple[Path, Path]:
    package_root = tmp_path / "codex-package"
    (package_root / "bin/codex.js").parent.mkdir(parents=True)
    (package_root / "bin/codex.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    payload = {
        "plugin": {
            "id": "codex",
            "enabled": True,
            "status": "loaded",
            "version": plugin_version,
            "dependencyStatus": {
                "dependencies": [
                    {
                        "name": "@openai/codex",
                        "spec": app_server_version,
                        "resolvedPath": str(package_root),
                    }
                ]
            },
        }
    }
    payload_text = shlex.quote(json.dumps(payload, separators=(",", ":")))
    stderr_command = f"printf '%s' {shlex.quote(stderr)} >&2" if stderr else ":"
    executable = tmp_path / "openclaw"
    _write_executable(
        executable,
        f"if [[ \"$1\" == plugins ]]; then printf '%s\\n' {payload_text}; {stderr_command}; fi",
    )
    return executable, package_root


@pytest.mark.parametrize("plugin_version", ["2026.7.1-0", "2026.7.1-2"])
def test_codex_plugin_pin_rejects_older_and_newer_versions(
    tmp_path: Path, plugin_version: str
) -> None:
    executable, _ = _codex_executable(tmp_path, plugin_version, REQUIRED_CODEX_APP_SERVER_VERSION)

    result = _run_module(
        [
            "require-codex-runtime-exact",
            str(executable),
            str(tmp_path / "config"),
            str(tmp_path),
        ]
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: Codex plugin {plugin_version} is unsupported; "
        f"need exactly {REQUIRED_CODEX_PLUGIN_VERSION}.\n"
        "       Run bootstrap to reinstall the pinned plugin and gateway service.\n"
    )


@pytest.mark.parametrize("app_server_version", ["0.144.2", "0.144.4"])
def test_codex_app_server_pin_rejects_older_and_newer_versions(
    tmp_path: Path, app_server_version: str
) -> None:
    executable, _ = _codex_executable(tmp_path, REQUIRED_CODEX_PLUGIN_VERSION, app_server_version)

    result = _run_module(
        [
            "require-codex-runtime-exact",
            str(executable),
            str(tmp_path / "config"),
            str(tmp_path),
        ]
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: Embedded @openai/codex {app_server_version} is unsupported; "
        f"need exactly {REQUIRED_CODEX_APP_SERVER_VERSION}.\n"
        "       Run bootstrap to reinstall the pinned plugin and gateway service.\n"
    )


def test_codex_runtime_accepts_exact_tuple_and_keeps_stderr_separate(tmp_path: Path) -> None:
    executable, package_root = _codex_executable(
        tmp_path,
        REQUIRED_CODEX_PLUGIN_VERSION,
        REQUIRED_CODEX_APP_SERVER_VERSION,
        stderr="plugin diagnostic should not contaminate JSON",
    )

    result = _run_module(
        [
            "require-codex-runtime-exact",
            str(executable),
            str(tmp_path / "config"),
            str(tmp_path),
        ]
    )

    assert result.returncode == 0
    assert result.stdout == (
        f"{REQUIRED_CODEX_PLUGIN_VERSION}\t{REQUIRED_CODEX_APP_SERVER_VERSION}\t{package_root}\n"
    )
    assert result.stderr == ""


def test_script_version_literals_drift_guard() -> None:
    script = (REPO_ROOT / "scripts/push-openclaw-config.sh").read_text(encoding="utf-8")
    expected = {
        "REQUIRED_OPENCLAW_VERSION": REQUIRED_OPENCLAW_VERSION,
        "REQUIRED_CODEX_PLUGIN_VERSION": REQUIRED_CODEX_PLUGIN_VERSION,
        "REQUIRED_CODEX_APP_SERVER_VERSION": REQUIRED_CODEX_APP_SERVER_VERSION,
    }
    for name, value in expected.items():
        assert f'{name}="{value}"' in script
