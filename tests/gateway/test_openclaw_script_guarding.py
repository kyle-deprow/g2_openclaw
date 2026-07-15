"""Regression tests for OpenClaw guards in repository shell scripts."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts/bootstrap.sh"
PUSH_SCRIPT = REPO_ROOT / "scripts/push-openclaw-config.sh"
OPENCLAW_CONFIG = REPO_ROOT / "gateway/openclaw_config/openclaw.json"
SUPERVISOR_UNIT_TEMPLATE = (
    REPO_ROOT / "gateway/openclaw_config/quantipy-autoresearch-supervisor.service.template"
)
GATEWAY_RUNTIME_CAPS_DROPIN = (
    REPO_ROOT / "gateway/openclaw_config/openclaw-gateway-runtime-caps.conf"
)
CODEX_RUNTIME_DROPIN = REPO_ROOT / "gateway/openclaw_config/openclaw-codex-runtime.conf"

STAGE_AGENT_IDS = [
    "context-curator",
    "debater-microstructure",
    "debater-data",
    "debater-skeptic",
    "debater-theory",
    "debater-implementation",
    "consensus-arbiter",
    "implementer",
    "reviewer",
    "fixer",
]
EXPECTED_RUNTIME_CAP_LINES = [
    "[Service]",
    'Environment="LOKY_MAX_CPU_COUNT=1"',
    'Environment="OMP_NUM_THREADS=1"',
    'Environment="OPENBLAS_NUM_THREADS=1"',
    'Environment="MKL_NUM_THREADS=1"',
    'Environment="BLIS_NUM_THREADS=1"',
    'Environment="NUMEXPR_NUM_THREADS=1"',
    'Environment="VECLIB_MAXIMUM_THREADS=1"',
    'Environment="PYTHONFAULTHANDLER=1"',
]
EXPECTED_RUNTIME_CAP_TEXT = "\n".join(EXPECTED_RUNTIME_CAP_LINES) + "\n"
EXPECTED_CODEX_RUNTIME_TEXT = CODEX_RUNTIME_DROPIN.read_text(encoding="utf-8")
EXPECTED_CODEX_RUNTIME_EXECSTARTPRE = (
    "ExecStartPre=/usr/bin/env node "
    "/home/dev/repos/g2_openclaw/scripts/ensure-openclaw-codex-runtime.mjs"
)
SUBPROCESS_ENV_ALLOWLIST = ("LANG", "LC_ALL", "TZ", "TERM")


def _base_subprocess_env(home: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    test_env = {name: os.environ[name] for name in SUBPROCESS_ENV_ALLOWLIST if name in os.environ}
    test_env.update(
        {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
        }
    )
    if env is not None:
        test_env.update(env)
    return test_env


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
    test_env = _base_subprocess_env(home, env)
    return subprocess.run(
        ["bash", "-c", command, "bootstrap-test", str(BOOTSTRAP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=test_env,
    )


def _runtime_caps_dropin_dst(home: Path) -> Path:
    return home / ".config/systemd/user/openclaw-gateway.service.d/10-quantipy-runtime-caps.conf"


def _codex_runtime_dropin_dst(home: Path) -> Path:
    return home / ".config/systemd/user/openclaw-gateway.service.d/20-openclaw-codex-runtime.conf"


def _write_push_script_fixture_bin(
    home: Path,
    *,
    gateway_load_state: str = "loaded",
    gateway_active_state: str = "inactive",
) -> Path:
    mock_bin = home / "mock-bin"
    openclaw = mock_bin / "openclaw"
    _write_executable(
        openclaw,
        r"""
if [[ -v OPENCLAW_HOME ]]; then
  printf 'refusing leaked OPENCLAW_HOME=%s\n' "$OPENCLAW_HOME" >&2
  exit 66
fi
if [[ -v OPENCLAW_PUSH_HOME ]]; then
  printf 'refusing leaked OPENCLAW_PUSH_HOME=%s\n' "$OPENCLAW_PUSH_HOME" >&2
  exit 67
fi
if [[ "${OPENCLAW_STATE_DIR:-}" != "$EXPECTED_OPENCLAW_STATE_DIR" ]]; then
  printf 'unexpected OPENCLAW_STATE_DIR=%s\n' "${OPENCLAW_STATE_DIR:-<unset>}" >&2
  exit 68
fi
if [[ "${OPENCLAW_CONFIG_PATH:-}" != "$EXPECTED_OPENCLAW_CONFIG_PATH" ]]; then
  printf 'unexpected OPENCLAW_CONFIG_PATH=%s\n' "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
  exit 69
fi
printf '%s %s %s %s %s\n' \
  "openclaw $*" \
  "OPENCLAW_HOME=<unset>" \
  "OPENCLAW_PUSH_HOME=<unset>" \
  "OPENCLAW_STATE_DIR=$OPENCLAW_STATE_DIR" \
  "OPENCLAW_CONFIG_PATH=$OPENCLAW_CONFIG_PATH" >> "$OPENCLAW_LOG"
case "${1:-}" in
  --version)
    printf 'openclaw 2026.6.11\n'
    ;;
  config)
    [[ "${2:-}" == "validate" ]] || exit 44
    printf 'config ok\n'
    ;;
  plugins)
    [[ "${2:-}" == "inspect" && "${3:-}" == "codex" && "${4:-}" == "--json" ]] || exit 45
    cat <<'JSON'
{
  "plugin": {
    "id": "codex",
    "enabled": true,
    "status": "loaded",
    "dependencyStatus": {
      "dependencies": [
        {"name": "@openai/codex", "spec": "0.144.1"}
      ]
    }
  }
}
JSON
    ;;
  *)
    printf 'unexpected openclaw command: %s\n' "$*" >&2
    exit 46
    ;;
esac
""".strip(),
    )

    mempalace_python = home / ".local/share/mempalace/venv/bin/python"
    _write_executable(
        mempalace_python,
        r"""
case "${1:-}" in
  -c)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
""".strip(),
    )

    _write_executable(
        mock_bin / "systemctl",
        rf"""
printf 'systemctl %s\n' "$*" >> "$SYSTEMCTL_LOG"
case "$*" in
  "--user show-environment")
    exit 0
    ;;
  "--user show openclaw-gateway.service --property=LoadState --property=ActiveState")
    load_state="${{GATEWAY_LOAD_STATE:-{gateway_load_state}}}"
    active_state="${{GATEWAY_ACTIVE_STATE:-{gateway_active_state}}}"
    printf 'LoadState=%s\nActiveState=%s\n' "$load_state" "$active_state"
    exit 0
    ;;
  "--user daemon-reload")
    dropin="$HOME/.config/systemd/user/openclaw-gateway.service.d"
    dropin="$dropin/10-quantipy-runtime-caps.conf"
    if [[ -f "$dropin" ]]; then
      printf 'daemon-reload saw dropin\n' >> "$SYSTEMCTL_LOG"
    else
      printf 'daemon-reload missing dropin\n' >> "$SYSTEMCTL_LOG"
    fi
    if [[ "${{FAIL_DAEMON_RELOAD:-0}}" == "1" ]]; then
      printf 'daemon-reload failed by test\n' >&2
      exit 23
    fi
    exit 0
    ;;
  *)
    printf 'unexpected systemctl command: %s\n' "$*" >&2
    exit 47
    ;;
esac
""".strip(),
    )

    _write_executable(
        mock_bin / "cp",
        r"""
printf 'cp %s\n' "$*" >> "$CP_LOG"
dest="${@: -1}"
case "$dest" in
  "$TEST_ROOT"/*)
    ;;
  *)
    printf 'cp destination escaped tmp_path: %s\n' "$dest" >&2
    exit 88
    ;;
esac
/usr/bin/cp "$@"
        """.strip(),
    )
    _write_executable(
        mock_bin / "sqlite3",
        r"""
if [[ "$#" -ge 2 ]]; then
  printf '1\n'
else
  cat >/dev/null
  : > "$1"
fi
""".strip(),
    )
    return mock_bin


def _prepare_push_script_home(
    tmp_path: Path,
    *,
    gateway_load_state: str = "loaded",
    gateway_active_state: str = "inactive",
) -> dict[str, str]:
    home = tmp_path / "home"
    openclaw_home = home / "isolated push root with spaces $literal"
    leaked_openclaw_home = tmp_path / "inherited-openclaw-home"
    leaked_state_dir = tmp_path / "inherited-state-dir"
    leaked_config_path = tmp_path / "inherited-config.json"
    home.mkdir()
    openclaw_home.mkdir()
    auth_db = openclaw_home / "agents/main/agent/openclaw-agent.sqlite"
    auth_db.parent.mkdir(parents=True)
    with sqlite3.connect(auth_db) as connection:
        connection.executescript(
            """
            CREATE TABLE auth_profile_store (
                store_key TEXT NOT NULL PRIMARY KEY,
                store_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE auth_profile_state (
                state_key TEXT NOT NULL PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO auth_profile_store VALUES
                ('openai:test', '{"provider":"openai","mode":"oauth"}', 1);
            """
        )
    leaked_openclaw_home.mkdir()
    leaked_state_dir.mkdir()
    leaked_config_path.write_text("{}", encoding="utf-8")
    (openclaw_home / "openclaw.json").write_text(
        json.dumps({}),
        encoding="utf-8",
    )
    mock_bin = _write_push_script_fixture_bin(
        home,
        gateway_load_state=gateway_load_state,
        gateway_active_state=gateway_active_state,
    )
    env_file = tmp_path / "openclaw-push.env"
    env_file.write_text(
        "\n".join(
            [
                f"OPENCLAW_HOME={tmp_path / 'env-file-openclaw-home'}",
                f"OPENCLAW_PUSH_HOME={tmp_path / 'env-file-push-home'}",
                f"OPENCLAW_STATE_DIR={tmp_path / 'env-file-state-dir'}",
                f"OPENCLAW_CONFIG_PATH={tmp_path / 'env-file-config.json'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return _base_subprocess_env(
        home,
        {
            "OPENCLAW_PUSH_HOME": str(openclaw_home),
            "OPENCLAW_HOME": str(leaked_openclaw_home),
            "OPENCLAW_STATE_DIR": str(leaked_state_dir),
            "OPENCLAW_CONFIG_PATH": str(leaked_config_path),
            "EXPECTED_OPENCLAW_STATE_DIR": str(openclaw_home),
            "EXPECTED_OPENCLAW_CONFIG_PATH": str(openclaw_home / "openclaw.json"),
            "OPENCLAW_BIN": str(mock_bin / "openclaw"),
            "OPENCLAW_PROVIDER": "codex",
            "OPENAI_MODEL": "gpt-5.4",
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            "OPENCLAW_PUSH_ENV_FILE": str(env_file),
            "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed-cache"),
            "HF_HUB_OFFLINE": "1",
            "MEMPALACE_EMBEDDING_MODEL": "bge-base",
            "MEMPALACE_EXPECTED_EMBEDDING_MODEL": "bge-base",
            "MEMPALACE_EXPECTED_EMBEDDING_DIMENSION": "768",
            "SYSTEMCTL_LOG": str(home / "systemctl.log"),
            "CP_LOG": str(home / "cp.log"),
            "OPENCLAW_LOG": str(home / "openclaw.log"),
            "TEST_ROOT": str(tmp_path),
        },
    )


def _run_push_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_missing_gateway_left_managed_destinations_untouched(
    env: dict[str, str],
    initial_config: str,
) -> None:
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])

    assert (openclaw_home / "openclaw.json").read_text(encoding="utf-8") == initial_config
    assert not list(openclaw_home.glob("openclaw.json.bak.*"))
    assert not (openclaw_home / "mempalace-readonly-server.py").exists()
    assert not (openclaw_home / "azure-api-version-preload.cjs").exists()
    assert not (openclaw_home / "skills").exists()
    assert not (openclaw_home / "workspace").exists()
    assert not list(openclaw_home.glob("workspace-*"))
    assert not (home / ".config/systemd/user").exists()
    assert not (home / ".mempalace").exists()
    assert not Path(env["FASTEMBED_CACHE_PATH"]).exists()
    assert not Path(env["CP_LOG"]).exists()


def test_repo_openclaw_config_splits_g2_interface_from_autoresearch_pm() -> None:
    config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    agents = {agent["id"]: agent for agent in config["agents"]["list"]}

    main = agents["main"]
    assert main["model"]["primary"] == "openai/gpt-5.4"
    assert "mempalace" not in main.get("skills", [])
    assert "autoresearch" not in main.get("skills", [])
    assert "mempalace-readonly" not in main.get("skills", [])
    assert main.get("subagents", {}).get("allowAgents", []) == []

    pm = agents["autoresearch-pm"]
    assert pm["model"]["primary"] == "openai/gpt-5.6-sol"
    assert pm["thinkingDefault"] == "high"
    assert pm["skills"] == ["mempalace", "autoresearch"]
    assert pm["subagents"]["allowAgents"] == STAGE_AGENT_IDS

    servers = config["mcp"]["servers"]
    assert servers["mempalace"]["codex"]["agents"] == ["autoresearch-pm"]
    assert servers["mempalace-readonly"]["codex"]["agents"] == STAGE_AGENT_IDS


def test_push_script_invariants_target_autoresearch_pm_not_main() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    assert '  "autoresearch-pm"\n)' in script
    assert 'select(.id == "autoresearch-pm") | .model.primary' in script
    assert 'select(.id == "main") | .model.primary) = $pm' not in script
    assert "main interface split, autoresearch-pm model" in script
    assert "main interface restrictions" in script


def test_push_script_installs_but_does_not_start_the_supervisor_service() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")
    template = SUPERVISOR_UNIT_TEMPLATE.read_text(encoding="utf-8")

    assert "quantipy-autoresearch-supervisor.service" in script
    assert "systemctl --user daemon-reload" in script
    assert '"${SYSTEMD_USER_DIR}/quantipy-autoresearch-supervisor.service"' in script
    assert "enable --now" not in script
    assert "start quantipy-autoresearch-supervisor.service" not in script
    assert "@REPO_ROOT@" in template
    assert "@HOME@" in template
    assert "@OPENCLAW_BIN@" not in template
    assert "Environment=OPENCLAW_BIN=" not in template
    assert "Environment=OPENCLAW_HOME=" not in template
    assert "-m gateway.autoresearch_supervisor" in template


def test_gateway_runtime_caps_dropin_declares_exact_operator_caps() -> None:
    assert GATEWAY_RUNTIME_CAPS_DROPIN.read_text(encoding="utf-8").splitlines() == (
        EXPECTED_RUNTIME_CAP_LINES
    )


def test_codex_runtime_dropin_declares_prestart_verifier() -> None:
    assert EXPECTED_CODEX_RUNTIME_TEXT.splitlines() == [
        "[Service]",
        EXPECTED_CODEX_RUNTIME_EXECSTARTPRE,
    ]


def test_push_script_installs_gateway_runtime_caps_dropin_fail_closed() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    assert (
        'GATEWAY_RUNTIME_CAPS_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/'
        'openclaw-gateway-runtime-caps.conf"'
    ) in script
    assert 'GATEWAY_SERVICE_NAME="openclaw-gateway.service"' in script
    assert 'GATEWAY_RUNTIME_CAPS_DROPIN_NAME="10-quantipy-runtime-caps.conf"' in script
    assert 'CODEX_RUNTIME_DROPIN_NAME="20-openclaw-codex-runtime.conf"' in script
    assert (
        'GATEWAY_RUNTIME_CAPS_DROPIN_DIR="${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}.d"'
    ) in script
    assert (
        'GATEWAY_RUNTIME_CAPS_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/'
        '${GATEWAY_RUNTIME_CAPS_DROPIN_NAME}"'
    ) in script
    assert ('validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}"') in script
    assert (
        'cp "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}" "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"'
    ) in script
    assert (
        'mv "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"'
    ) in script
    assert "require_gateway_service_loadable" in script
    assert "prepare_runtime_caps_dropin_dir" in script
    assert ('chmod 0755 "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"') in script
    assert ('validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"') in script
    assert script.count("systemctl --user daemon-reload") == 1
    assert "GATEWAY_RUNTIME_CAPS_DROPIN" in script
    assert "GATEWAY_RUNTIME_CAPS_DROPIN_TMP:-" in script
    assert "GATEWAY_RUNTIME_CAPS_DROPIN_DST" in script
    assert "validate_codex_runtime_dropin_file" in script
    assert "CODEX_RUNTIME_DROPIN_DST" in script
    assert "daemon-reload ||" not in script
    assert (
        'cp "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}" "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" ||'
    ) not in script
    assert (
        'validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}" ||'
    ) not in script
    preflight_loadable_check = script.index("if ! require_gateway_service_loadable; then")
    backup_write = script.index('cp "${LOCAL_CONFIG}" "${BACKUP}"')
    supervisor_unit_write = script.index('mv "${SUPERVISOR_UNIT_TMP}" "${SUPERVISOR_UNIT_DST}"')
    runtime_caps_dir_prepare = script.rindex("\nprepare_runtime_caps_dropin_dir\n")
    assert preflight_loadable_check < backup_write
    assert preflight_loadable_check < supervisor_unit_write
    assert preflight_loadable_check < runtime_caps_dir_prepare


def test_push_script_fails_before_dropin_when_gateway_service_missing(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path, gateway_load_state="not-found")
    initial_config = (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "not installed as a loadable user unit" in result.stderr
    _assert_missing_gateway_left_managed_destinations_untouched(env, initial_config)
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user show openclaw-gateway.service" in systemctl_log
    assert "daemon-reload" not in systemctl_log


def test_push_script_installs_runtime_caps_exactly_with_safe_modes_and_no_restart(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    dropin_dir = _runtime_caps_dropin_dst(home).parent
    dropin_dir.mkdir(parents=True)
    dropin_dir.chmod(0o777)

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    dropin = _runtime_caps_dropin_dst(home)
    assert dropin.read_text(encoding="utf-8") == EXPECTED_RUNTIME_CAP_TEXT
    codex_dropin = _codex_runtime_dropin_dst(home)
    assert codex_dropin.read_text(encoding="utf-8") == EXPECTED_CODEX_RUNTIME_TEXT
    assert _mode(dropin_dir) == 0o755
    assert _mode(dropin) == 0o644
    assert not list(dropin_dir.glob(".10-quantipy-runtime-caps.conf.*"))
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user show openclaw-gateway.service" in systemctl_log
    assert "systemctl --user daemon-reload" in systemctl_log
    assert "restart openclaw-gateway.service" not in systemctl_log
    assert "start openclaw-gateway.service" not in systemctl_log
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert cp_log.count(str(GATEWAY_RUNTIME_CAPS_DROPIN)) == 1
    openclaw_log = Path(env["OPENCLAW_LOG"]).read_text(encoding="utf-8")
    assert openclaw_log.count("OPENCLAW_HOME=<unset>") == 3
    assert openclaw_log.count("OPENCLAW_PUSH_HOME=<unset>") == 3
    assert openclaw_log.count(f"OPENCLAW_STATE_DIR={env['EXPECTED_OPENCLAW_STATE_DIR']}") == 3
    assert openclaw_log.count(f"OPENCLAW_CONFIG_PATH={env['EXPECTED_OPENCLAW_CONFIG_PATH']}") == 3
    assert "inherited-openclaw-home" not in openclaw_log
    assert "inherited-state-dir" not in openclaw_log
    assert "inherited-config.json" not in openclaw_log
    assert "env-file-push-home" not in openclaw_log
    assert "env-file-state-dir" not in openclaw_log
    assert "env-file-config.json" not in openclaw_log


def test_push_script_runtime_caps_install_is_idempotent(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])

    first = _run_push_script(env)
    second = _run_push_script(env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    dropin = _runtime_caps_dropin_dst(home)
    assert dropin.read_text(encoding="utf-8") == EXPECTED_RUNTIME_CAP_TEXT
    assert (
        _codex_runtime_dropin_dst(home).read_text(encoding="utf-8") == EXPECTED_CODEX_RUNTIME_TEXT
    )
    assert _mode(dropin.parent) == 0o755
    assert _mode(dropin) == 0o644
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert systemctl_log.count("systemctl --user daemon-reload") == 2
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert cp_log.count(str(GATEWAY_RUNTIME_CAPS_DROPIN)) == 2


def test_push_script_daemon_reload_runs_after_dropin_install_and_failure_aborts(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_DAEMON_RELOAD"] = "1"
    home = Path(env["HOME"])

    result = _run_push_script(env)

    assert result.returncode == 23
    assert "daemon-reload failed by test" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert _runtime_caps_dropin_dst(home).read_text(encoding="utf-8") == (EXPECTED_RUNTIME_CAP_TEXT)
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "daemon-reload saw dropin" in systemctl_log
    assert systemctl_log.index("systemctl --user show openclaw-gateway.service") < (
        systemctl_log.index("systemctl --user daemon-reload")
    )
    assert "restart openclaw-gateway.service" not in systemctl_log
    assert "start openclaw-gateway.service" not in systemctl_log


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
        env=_base_subprocess_env(
            tmp_path,
            {
                "OPENCLAW_BIN": str(override),
                "MUTATION_LOG": str(mutation_log),
            },
        ),
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
    home = tmp_path / "home"
    openclaw_home = tmp_path / "openclaw-home"
    executable = home / "openclaw"
    _write_executable(executable, "printf 'openclaw 2026.6.12\\n'")

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=_base_subprocess_env(
            home,
            {
                "OPENCLAW_PUSH_HOME": str(openclaw_home),
                "OPENCLAW_HOME": str(tmp_path / "inherited-openclaw-home"),
                "OPENCLAW_BIN": str(executable),
            },
        ),
    )

    assert result.returncode == 1
    assert "unsupported; need exactly 2026.6.11" in result.stderr
    assert not openclaw_home.exists()


def test_push_script_defaults_to_home_openclaw_not_inherited_openclaw_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    inherited_openclaw_home = tmp_path / "inherited-openclaw-home"
    inherited_openclaw_home.mkdir()
    (inherited_openclaw_home / "openclaw.json").write_text("{}", encoding="utf-8")
    executable = home / "openclaw"
    _write_executable(
        executable,
        """
if [[ -v OPENCLAW_HOME ]]; then
  printf 'leaked OPENCLAW_HOME=%s\n' "$OPENCLAW_HOME" >&2
  exit 66
fi
printf 'openclaw 2026.6.11\n'
""".strip(),
    )

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=_base_subprocess_env(
            home,
            {
                "OPENCLAW_HOME": str(inherited_openclaw_home),
                "OPENCLAW_BIN": str(executable),
            },
        ),
    )

    expected_config = home / ".openclaw/openclaw.json"
    assert result.returncode == 1
    assert "leaked OPENCLAW_HOME" not in result.stderr
    assert f"Local OpenClaw config not found at {expected_config}" in result.stderr


def test_push_script_expands_literal_push_home_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    openclaw_home = home / "openclaw-home"
    executable = home / "bin/openclaw"
    _write_executable(executable, "printf 'openclaw 2026.6.11\\n'")

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=_base_subprocess_env(
            home,
            {
                "OPENCLAW_PUSH_HOME": "~/openclaw-home",
                "OPENCLAW_HOME": str(tmp_path / "inherited-openclaw-home"),
                "OPENCLAW_BIN": "~/bin/openclaw",
            },
        ),
    )

    assert result.returncode == 1
    assert f"Using OpenClaw: {executable} (version 2026.6.11)" in result.stdout
    assert f"Local OpenClaw config not found at {openclaw_home / 'openclaw.json'}" in result.stderr


@pytest.mark.parametrize(
    "version_token",
    ["2026.6.11-beta.1", "2026.6.11+build", "2026.6.11.1"],
)
def test_push_script_rejects_unstable_exact_prefix_version(
    tmp_path: Path, version_token: str
) -> None:
    home = tmp_path / "home"
    openclaw_home = tmp_path / "openclaw-home"
    executable = home / "openclaw"
    _write_executable(executable, f"printf 'openclaw {version_token}\\n'")

    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=_base_subprocess_env(
            home,
            {
                "OPENCLAW_PUSH_HOME": str(openclaw_home),
                "OPENCLAW_HOME": str(tmp_path / "inherited-openclaw-home"),
                "OPENCLAW_BIN": str(executable),
            },
        ),
    )

    assert result.returncode == 1
    assert "need exactly 2026.6.11" in result.stderr
    assert not openclaw_home.exists()
