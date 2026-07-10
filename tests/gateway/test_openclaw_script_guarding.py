"""Regression tests for OpenClaw guards in repository shell scripts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts/bootstrap.sh"
PUSH_SCRIPT = REPO_ROOT / "scripts/push-openclaw-config.sh"


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_bootstrap_guard(home: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = """
bootstrap_script="$1"
set --
source "$bootstrap_script"
if [[ "${TEST_DISABLE_PNPM:-0}" == "1" ]]; then
  command() {
    if [[ "$#" == "2" && "$1" == "-v" && "$2" == "pnpm" ]]; then
      return 1
    fi
    builtin command "$@"
  }
fi
if ensure_openclaw_exact_version; then
  printf 'RESOLVED=%s\nVERSION=%s\n' "$OPENCLAW_BIN_RESOLVED" "$OPENCLAW_VERSION_RESOLVED"
else
  exit 1
fi
"""
    test_env = {**os.environ, "HOME": str(home), **env}
    for name in ("OPENCLAW_BIN", "PNPM_HOME", "NPM_CONFIG_PREFIX"):
        if name not in env:
            test_env.pop(name, None)
    return subprocess.run(
        ["bash", "-c", command, "bootstrap-test", str(BOOTSTRAP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=test_env,
    )


def test_bootstrap_npm_install_bypasses_stale_pnpm_candidate(tmp_path: Path) -> None:
    stale = tmp_path / ".local/share/pnpm/openclaw"
    _write_executable(stale, "printf 'openclaw 2026.6.10\\n'")

    mock_bin = tmp_path / "mock-bin"
    npm_log = tmp_path / "npm.log"
    pnpm_log = tmp_path / "pnpm.log"
    npm = mock_bin / "npm"
    _write_executable(
        npm,
        """
printf '%s\n' "$*" > "$NPM_LOG"
prefix=''
while (($#)); do
  if [[ "$1" == '--prefix' ]]; then
    prefix="$2"
    break
  fi
  shift
done
/usr/bin/mkdir -p "$prefix/bin"
printf '#!/usr/bin/env bash\nprintf "openclaw 2026.6.11\\\\n"\n' > "$prefix/bin/openclaw"
/usr/bin/chmod 755 "$prefix/bin/openclaw"
        """.strip(),
    )
    _write_executable(mock_bin / "pnpm", "printf 'invoked\\n' > \"$PNPM_LOG\"; exit 99")

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            "NPM_LOG": str(npm_log),
            "NPM_CONFIG_PREFIX": "~/npm-global",
            "PNPM_LOG": str(pnpm_log),
            "TEST_DISABLE_PNPM": "1",
        },
    )

    expected = tmp_path / "npm-global/bin/openclaw"
    assert result.returncode == 0, result.stderr
    assert f"RESOLVED={expected}" in result.stdout
    assert "VERSION=2026.6.11" in result.stdout
    assert npm_log.read_text(encoding="utf-8").strip() == (
        f"install -g --prefix {tmp_path / 'npm-global'} openclaw@2026.6.11"
    )
    assert not pnpm_log.exists()


def test_bootstrap_pnpm_install_selects_exact_installed_path(tmp_path: Path) -> None:
    mock_bin = tmp_path / "mock-bin"
    pnpm_log = tmp_path / "pnpm.log"
    _write_executable(
        mock_bin / "pnpm",
        """
printf '%s\n' "$*" > "$PNPM_LOG"
/usr/bin/mkdir -p "$PNPM_HOME"
printf '#!/usr/bin/env bash\nprintf "openclaw 2026.6.11\\\\n"\n' > "$PNPM_HOME/openclaw"
/usr/bin/chmod 755 "$PNPM_HOME/openclaw"
""".strip(),
    )

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            "PNPM_HOME": "~/pnpm-home",
            "PNPM_LOG": str(pnpm_log),
        },
    )

    expected = tmp_path / "pnpm-home/openclaw"
    assert result.returncode == 0, result.stderr
    assert f"RESOLVED={expected}" in result.stdout
    assert "VERSION=2026.6.11" in result.stdout
    assert pnpm_log.read_text(encoding="utf-8").strip() == "add -g openclaw@2026.6.11"


def test_bootstrap_keeps_exact_automatic_candidate_without_installing(tmp_path: Path) -> None:
    preferred = tmp_path / ".local/share/pnpm/openclaw"
    _write_executable(preferred, "printf 'openclaw 2026.6.11\\n'")
    mock_bin = tmp_path / "mock-bin"
    install_log = tmp_path / "install.log"
    for manager in ("npm", "pnpm"):
        _write_executable(mock_bin / manager, "printf '%s\\n' called > \"$INSTALL_LOG\"")

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            "INSTALL_LOG": str(install_log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"RESOLVED={preferred}" in result.stdout
    assert not install_log.exists()


def test_bootstrap_accepts_exact_explicit_override_without_installing(tmp_path: Path) -> None:
    override = tmp_path / "override/openclaw"
    _write_executable(override, "printf 'openclaw 2026.6.11\\n'")
    mock_bin = tmp_path / "mock-bin"
    install_log = tmp_path / "install.log"
    for manager in ("npm", "pnpm"):
        _write_executable(mock_bin / manager, "printf '%s\\n' called > \"$INSTALL_LOG\"")

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "OPENCLAW_BIN": "~/override/openclaw",
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            "INSTALL_LOG": str(install_log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"RESOLVED={override}" in result.stdout
    assert not install_log.exists()


@pytest.mark.parametrize("override_exists", [True, False])
def test_bootstrap_rejects_explicit_override_without_installing(
    tmp_path: Path, override_exists: bool
) -> None:
    override = tmp_path / "override/openclaw"
    if override_exists:
        _write_executable(override, "printf 'openclaw 2026.6.10\\n'")

    mock_bin = tmp_path / "mock-bin"
    install_log = tmp_path / "install.log"
    for manager in ("npm", "pnpm"):
        _write_executable(mock_bin / manager, "printf '%s\\n' called > \"$INSTALL_LOG\"")

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "OPENCLAW_BIN": str(override),
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            "INSTALL_LOG": str(install_log),
        },
    )

    assert result.returncode == 1
    assert "bootstrap did not install or upgrade anything" in result.stdout
    assert not install_log.exists()


def test_bootstrap_rejects_explicit_override_before_dependency_mutation(
    tmp_path: Path,
) -> None:
    override = tmp_path / "override/openclaw"
    _write_executable(override, "printf 'openclaw 2026.6.10\\n'")
    mutation_log = tmp_path / "mutation.log"
    command = """
bootstrap_script="$1"
set --
source "$bootstrap_script"
check_prerequisites() { printf 'check_prerequisites\n' >> "$MUTATION_LOG"; }
install_python_deps() { printf 'install_python_deps\n' >> "$MUTATION_LOG"; }
install_ts_deps() { printf 'install_ts_deps\n' >> "$MUTATION_LOG"; }
main
"""

    result = subprocess.run(
        ["bash", "-c", command, "bootstrap-test", str(BOOTSTRAP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "OPENCLAW_BIN": str(override),
            "MUTATION_LOG": str(mutation_log),
        },
    )

    assert result.returncode == 1
    assert "bootstrap did not install or upgrade anything" in result.stdout
    assert not mutation_log.exists()


@pytest.mark.parametrize(
    "version_token",
    ["2026.6.11-beta.1", "2026.6.11+build", "2026.6.11.1"],
)
def test_bootstrap_rejects_unstable_exact_prefix_version(
    tmp_path: Path, version_token: str
) -> None:
    override = tmp_path / "openclaw"
    _write_executable(override, f"printf 'openclaw {version_token}\\n'")

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "OPENCLAW_BIN": str(override),
            "PATH": "/usr/bin:/bin",
        },
    )

    assert result.returncode == 1
    assert "need exactly 2026.6.11" in result.stdout


def test_push_script_rejects_newer_openclaw_before_mutation(tmp_path: Path) -> None:
    executable = tmp_path / "openclaw"
    _write_executable(executable, "printf 'openclaw 2026.6.12\\n'")

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path), "OPENCLAW_BIN": str(executable)},
    )

    assert result.returncode == 1
    assert "unsupported; need exactly 2026.6.11" in result.stderr
    assert not (tmp_path / ".openclaw").exists()


def test_push_script_expands_literal_home_override(tmp_path: Path) -> None:
    executable = tmp_path / "bin/openclaw"
    _write_executable(executable, "printf 'openclaw 2026.6.11\\n'")

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path), "OPENCLAW_BIN": "~/bin/openclaw"},
    )

    assert result.returncode == 1
    assert f"Using OpenClaw: {executable} (version 2026.6.11)" in result.stdout
    assert "Local OpenClaw config not found" in result.stderr


@pytest.mark.parametrize(
    "version_token",
    ["2026.6.11-beta.1", "2026.6.11+build", "2026.6.11.1"],
)
def test_push_script_rejects_unstable_exact_prefix_version(
    tmp_path: Path, version_token: str
) -> None:
    executable = tmp_path / "openclaw"
    _write_executable(executable, f"printf 'openclaw {version_token}\\n'")

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path), "OPENCLAW_BIN": str(executable)},
    )

    assert result.returncode == 1
    assert "need exactly 2026.6.11" in result.stderr
    assert not (tmp_path / ".openclaw").exists()
