"""Regression tests for OpenClaw guards in repository shell scripts."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import tomllib
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
NATIVE_CRASH_HARDENING_DROPIN = (
    REPO_ROOT / "gateway/openclaw_config/openclaw-gateway-native-crash-hardening.conf"
)

STAGE_AGENT_IDS = [
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
]
EXPECTED_MAIN_ALLOW = [
    "g2-control__g2_autoresearch_status",
    "g2-control__g2_autoresearch_start",
    "g2-control__g2_autoresearch_stop",
    "mempalace-readonly__mempalace_status",
    "mempalace-readonly__mempalace_search",
    "mempalace-readonly__mempalace_get_drawer",
    "mempalace-readonly__mempalace_list_drawers",
    "mempalace-readonly__mempalace_list_wings",
    "mempalace-readonly__mempalace_list_rooms",
    "mempalace-readonly__mempalace_get_taxonomy",
    "mempalace-readonly__mempalace_get_aaak_spec",
    "mempalace-readonly__mempalace_diary_read",
    "mempalace-readonly__mempalace_kg_query",
    "mempalace-readonly__mempalace_kg_timeline",
    "mempalace-readonly__mempalace_kg_stats",
    "mempalace-readonly__mempalace_traverse",
    "mempalace-readonly__mempalace_find_tunnels",
    "mempalace-readonly__mempalace_follow_tunnels",
    "mempalace-readonly__mempalace_graph_stats",
    "mempalace-readonly__mempalace_list_tunnels",
    "mempalace-readonly__mempalace_list_hallways",
    "mempalace-readonly__mempalace_memories_filed_away",
]
EXPECTED_RUNTIME_CAP_LINES = [
    "[Service]",
    "UMask=0077",
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
EXPECTED_NATIVE_CRASH_HARDENING_LINES = [
    "[Service]",
    "MemoryHigh=6G",
    "MemoryMax=7G",
    "OOMPolicy=kill",
    (
        "RestartPreventExitStatus=SIGABRT SIGBUS SIGFPE SIGILL SIGQUIT SIGSEGV "
        "SIGSYS SIGTRAP SIGXCPU SIGXFSZ SIGKILL"
    ),
]
EXPECTED_NATIVE_CRASH_HARDENING_TEXT = "\n".join(EXPECTED_NATIVE_CRASH_HARDENING_LINES) + "\n"
CODEX_LOG_DB_SCHEMA = (
    """
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ts_nanos INTEGER NOT NULL,
    level TEXT NOT NULL,
    target TEXT NOT NULL,
    feedback_log_body TEXT,
    module_path TEXT,
    file TEXT,
    line INTEGER,
    thread_id TEXT,
    process_uuid TEXT,
    estimated_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_logs_ts ON logs(ts DESC, ts_nanos DESC, id DESC);
CREATE INDEX idx_logs_thread_id ON logs(thread_id);
CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id, ts DESC, ts_nanos DESC, id DESC);
"""
    "CREATE INDEX idx_logs_process_uuid_threadless_ts "
    "ON logs(process_uuid, ts DESC, ts_nanos DESC, id DESC)\n"
    "WHERE thread_id IS NULL;"
).strip()
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


def _run_mocked_bootstrap_openclaw_flow(
    tmp_path: Path,
    *,
    daemon_install_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "home"
    flow_log = tmp_path / "bootstrap-flow.log"
    mock_openclaw = tmp_path / "mock-bin/openclaw"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw/openclaw.json").write_text("{}\n", encoding="utf-8")
    _write_executable(
        mock_openclaw,
        r"""
printf 'openclaw %s\n' "$*" >> "$FLOW_LOG"
case "${1:-}" in
  plugins)
    if [[ "${2:-}" == "inspect" && "${3:-}" == "codex" && "${4:-}" == "--json" ]]; then
      cat <<'JSON'
{
  "plugin": {
    "id": "codex",
    "version": "2026.7.1-1",
    "enabled": true,
    "status": "loaded",
    "dependencyStatus": {
      "dependencies": [{"name": "@openai/codex", "spec": "0.144.3"}]
    }
  }
}
JSON
    fi
    ;;
  daemon)
    [[ "$*" == "daemon install --force --port 18789 --json" ]] || exit 91
    exit "$DAEMON_INSTALL_EXIT"
    ;;
  *)
    exit 92
    ;;
esac
""".strip(),
    )
    mempalace_python = home / ".local/share/mempalace/venv/bin/python"
    _write_executable(
        mempalace_python,
        'printf \'mempalace %s\\n\' "$*" >> "$FLOW_LOG"',
    )
    command = r"""
bootstrap_script="$1"
mock_openclaw="$2"
set --
source "$bootstrap_script"
ensure_openclaw_exact_version() {
  OPENCLAW_BIN_RESOLVED="$mock_openclaw"
  OPENCLAW_VERSION_RESOLVED="$REQUIRED_OPENCLAW_VERSION"
  export OPENCLAW_BIN="$OPENCLAW_BIN_RESOLVED"
}
make() {
  printf 'make %s\n' "$*" >> "$FLOW_LOG"
}
bash() {
  printf 'push-config %s\n' "$*" >> "$FLOW_LOG"
}
setup_openclaw
"""
    result = subprocess.run(
        ["bash", "-c", command, "bootstrap-flow-test", str(BOOTSTRAP_SCRIPT), str(mock_openclaw)],
        check=False,
        capture_output=True,
        text=True,
        env=_base_subprocess_env(
            home,
            {
                "DAEMON_INSTALL_EXIT": str(daemon_install_exit),
                "FLOW_LOG": str(flow_log),
            },
        ),
    )
    return result, flow_log


def _runtime_caps_dropin_dst(home: Path) -> Path:
    return home / ".config/systemd/user/openclaw-gateway.service.d/10-quantipy-runtime-caps.conf"


def _codex_runtime_dropin_dst(home: Path) -> Path:
    return home / ".config/systemd/user/openclaw-gateway.service.d/20-openclaw-codex-runtime.conf"


def _native_crash_hardening_dropin_dst(home: Path) -> Path:
    return (
        home
        / ".config/systemd/user/openclaw-gateway.service.d"
        / "30-openclaw-native-crash-hardening.conf"
    )


def _supervisor_unit_dst(home: Path) -> Path:
    return home / ".config/systemd/user/quantipy-autoresearch-supervisor.service"


def _write_push_script_fixture_bin(
    home: Path,
    *,
    gateway_load_state: str = "loaded",
    gateway_active_state: str = "inactive",
) -> Path:
    mock_bin = home / "mock-bin"
    codex_package = home / "mock-codex-package"
    codex_bin = codex_package / "bin"
    codex_bin.mkdir(parents=True)
    (codex_bin / "codex.js").write_text(
        """
const fs = require("fs");
if (process.env.CODEX_DOCTOR_LOG) {
  fs.appendFileSync(
    process.env.CODEX_DOCTOR_LOG,
    `${process.env.CODEX_HOME || "<unset>"} ${process.argv.slice(2).join(" ")}\\n`
  );
}
const codexHome = process.env.CODEX_HOME || "<unset>";
const packageRoot = process.env.MOCK_CODEX_RESOLVED_PATH || "<unset>";
const checks = {
  "auth.credentials": {
    id: "auth.credentials",
    status: process.env.MOCK_CODEX_DOCTOR_AUTH_STATUS || "fail",
    category: "auth",
    summary: "no Codex credentials were found",
    details: {
      "auth file": `${codexHome}/auth.json`,
      "auth storage mode": process.env.MOCK_CODEX_DOCTOR_AUTH_STORAGE_MODE || "File"
    }
  },
  "config.load": {
    id: "config.load",
    status: process.env.MOCK_CODEX_DOCTOR_CONFIG_STATUS || "ok",
    category: "config",
    summary: "config loaded",
    details: {"config.toml": `${codexHome}/config.toml`}
  },
  "installation": {
    id: "installation",
    status: process.env.MOCK_CODEX_DOCTOR_INSTALL_STATUS || "fail",
    category: "install",
    summary: process.env.MOCK_CODEX_DOCTOR_INSTALL_SUMMARY ||
      "npm install -g @openai/codex would update a different install",
    details: {"running package root": packageRoot}
  },
  "mcp.config": {
    id: "mcp.config",
    status: process.env.MOCK_CODEX_DOCTOR_MCP_STATUS || "ok",
    category: "mcp",
    summary: "MCP config loaded",
    details: {}
  },
  "network.websocket_reachability": {
    id: "network.websocket_reachability",
    status: process.env.MOCK_CODEX_DOCTOR_WEBSOCKET_STATUS || "warning",
    category: "websocket",
    summary: "Responses WebSocket failed; HTTPS fallback may still work",
    details: {
      "auth mode": process.env.MOCK_CODEX_DOCTOR_WEBSOCKET_AUTH_MODE || "none",
      endpoint: "wss://api.openai.com/v1/<redacted>",
      "model provider": "openai",
      "provider name": "OpenAI",
      "supports websockets": "true",
      "wire API": "responses"
    }
  },
  "runtime.provenance": {
    id: "runtime.provenance",
    status: process.env.MOCK_CODEX_DOCTOR_RUNTIME_STATUS || "ok",
    category: "runtime",
    summary: "running npm on linux-x86_64",
    details: {version: process.env.MOCK_CODEX_DOCTOR_RUNTIME_VERSION || "0.144.3"}
  },
  "sandbox.helpers": {
    id: "sandbox.helpers",
    status: process.env.MOCK_CODEX_DOCTOR_SANDBOX_STATUS || "ok",
    category: "sandbox",
    summary: "sandbox configuration is readable",
    details: {}
  },
  "updates.status": {
    id: "updates.status",
    status: process.env.MOCK_CODEX_DOCTOR_UPDATE_STATUS || "fail",
    category: "updates",
    summary: process.env.MOCK_CODEX_DOCTOR_UPDATE_SUMMARY ||
      "update would target a different npm install",
    details: {"running package root": packageRoot}
  }
};
if (process.env.MOCK_CODEX_DOCTOR_EXTRA_FAIL) {
  checks[process.env.MOCK_CODEX_DOCTOR_EXTRA_FAIL] = {
    id: process.env.MOCK_CODEX_DOCTOR_EXTRA_FAIL,
    status: "fail",
    category: "state",
    summary: "unexpected failure injected by test",
    details: {}
  };
}
if (process.env.MOCK_CODEX_DOCTOR_EXTRA_WARNING) {
  checks[process.env.MOCK_CODEX_DOCTOR_EXTRA_WARNING] = {
    id: process.env.MOCK_CODEX_DOCTOR_EXTRA_WARNING,
    status: "warning",
    category: "network",
    summary: "unexpected warning injected by test",
    details: {}
  };
}
if (process.env.MOCK_CODEX_DOCTOR_EXTRA_DETAIL_CHECK) {
  const check = checks[process.env.MOCK_CODEX_DOCTOR_EXTRA_DETAIL_CHECK];
  if (check) {
    check.details[process.env.MOCK_CODEX_DOCTOR_DETAIL_KEY || "extra"] =
      process.env.MOCK_CODEX_DOCTOR_DETAIL_VALUE || "unexpected";
  }
}
if (process.env.MOCK_CODEX_DOCTOR_DELETE_DETAIL_CHECK) {
  const check = checks[process.env.MOCK_CODEX_DOCTOR_DELETE_DETAIL_CHECK];
  if (check) {
    delete check.details[process.env.MOCK_CODEX_DOCTOR_DETAIL_KEY || "running package root"];
  }
}
console.log(JSON.stringify({
  schemaVersion: 1,
  overallStatus: "fail",
  codexVersion: "0.144.3",
  checks
}));
process.exit(Number(process.env.MOCK_CODEX_DOCTOR_EXIT_STATUS || "1"));
""".strip()
        + "\n",
        encoding="utf-8",
    )
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
case "${OPENCLAW_CONFIG_PATH:-}" in
  "$EXPECTED_OPENCLAW_CONFIG_PATH"|"$EXPECTED_OPENCLAW_STATE_DIR"/.openclaw.generated.*.json)
    ;;
  *)
  printf 'unexpected OPENCLAW_CONFIG_PATH=%s\n' "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
  exit 69
    ;;
esac
printf '%s %s %s %s %s %s\n' \
  "openclaw $*" \
  "OPENCLAW_HOME=<unset>" \
  "OPENCLAW_PUSH_HOME=<unset>" \
  "OPENCLAW_STATE_DIR=$OPENCLAW_STATE_DIR" \
  "OPENCLAW_CONFIG_PATH=$OPENCLAW_CONFIG_PATH" \
  "NODE_OPTIONS=${NODE_OPTIONS:-<unset>}" >> "$OPENCLAW_LOG"
case "${1:-}" in
  --version)
    printf 'openclaw 2026.7.1-2\n'
    ;;
  config)
    [[ "${2:-}" == "validate" ]] || exit 44
    if jq -e '.plugins.entries.codex.config.nativeToolSurfaceEnabled? != null' \
      "$OPENCLAW_CONFIG_PATH" >/dev/null; then
      if [[ "${3:-}" == "--json" ]]; then
        cat <<'JSON'
{
  "valid": false,
  "errors": [
    {
      "path": "plugins.entries.codex.config",
      "message": "must not have additional properties: nativeToolSurfaceEnabled"
    }
  ]
}
JSON
      else
        printf 'invalid config: nativeToolSurfaceEnabled\n' >&2
      fi
      exit 12
    fi
    if [[ "${MOCK_OPENCLAW_CONFIG_VALIDATE_FAIL:-0}" == "1" ]]; then
      if [[ "${3:-}" == "--json" ]]; then
        printf '{"valid":false,"errors":[{"path":"injected","message":"test failure"}]}\n'
      else
        printf 'invalid config injected by test\n' >&2
      fi
      exit 12
    fi
    if [[ "${3:-}" == "--json" ]]; then
      printf '{"valid":true,"path":"%s","warnings":[]}\n' "$OPENCLAW_CONFIG_PATH"
    else
      printf 'config ok\n'
    fi
    ;;
  plugins)
    [[ "${2:-}" == "inspect" && "${3:-}" == "codex" && "${4:-}" == "--json" ]] || exit 45
    cat <<JSON
{
  "plugin": {
    "id": "codex",
    "version": "${MOCK_CODEX_PLUGIN_VERSION:-2026.7.1-1}",
    "enabled": true,
    "status": "loaded",
    "dependencyStatus": {
      "dependencies": [
        {
          "name": "@openai/codex",
          "spec": "${MOCK_CODEX_APP_SERVER_VERSION:-0.144.3}",
          "resolvedPath": "${MOCK_CODEX_RESOLVED_PATH}"
        }
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
    if [[ -n "${{SYSTEMD_MANAGER_NODE_OPTIONS:-}}" ]]; then
      printf 'NODE_OPTIONS=%s\n' "$SYSTEMD_MANAGER_NODE_OPTIONS"
    fi
    exit 0
    ;;
  "--user unset-environment NODE_OPTIONS")
    printf 'unset NODE_OPTIONS\n' >> "$SYSTEMCTL_LOG"
    exit 0
    ;;
  "--user show openclaw-gateway.service --property=LoadState --property=ActiveState")
    load_state="${{GATEWAY_LOAD_STATE:-{gateway_load_state}}}"
    active_state="${{GATEWAY_ACTIVE_STATE:-{gateway_active_state}}}"
    printf 'LoadState=%s\nActiveState=%s\n' "$load_state" "$active_state"
    exit 0
    ;;
  "--user daemon-reload")
    dropin_dir="$HOME/.config/systemd/user/openclaw-gateway.service.d"
    if [[ -f "$dropin_dir/10-quantipy-runtime-caps.conf" \
      && -f "$dropin_dir/20-openclaw-codex-runtime.conf" \
      && -f "$dropin_dir/30-openclaw-native-crash-hardening.conf" ]]; then
      printf 'daemon-reload saw managed dropins\n' >> "$SYSTEMCTL_LOG"
    else
      printf 'daemon-reload missing managed dropin\n' >> "$SYSTEMCTL_LOG"
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
printf 'sqlite3 %s\n' "$*" >> "${SQLITE_LOG:-/dev/null}"
if [[ "${1:-}" == "-json" && "$#" -ge 3 && "$(basename "$2")" == "logs_2.sqlite" ]]; then
  /usr/bin/sqlite3 "$@"
elif [[ "$#" -ge 2 && "$(basename "$1")" == "logs_2.sqlite" ]]; then
  case "$2" in
    "PRAGMA integrity_check;")
      if [[ "${MOCK_CODEX_LOG_DB_INTEGRITY:-ok}" == "corrupt" ]]; then
        printf 'row 4135 missing from index idx_logs_thread_id\n'
      elif [[ "${MOCK_CODEX_LOG_DB_INTEGRITY:-ok}" == "other_index" ]]; then
        printf 'row 4135 missing from index idx_logs_ts\n'
      elif [[ "${MOCK_CODEX_LOG_DB_INTEGRITY:-ok}" == "table" ]]; then
        printf 'database disk image is malformed\n'
      else
        printf 'ok\n'
      fi
      ;;
    "REINDEX idx_logs_thread_id; PRAGMA integrity_check;")
      if [[ "${MOCK_CODEX_LOG_DB_REPAIR:-ok}" == "fail" ]]; then
        printf 'row 4135 missing from index idx_logs_thread_id\n'
      else
        printf 'ok\n'
      fi
      ;;
    *)
      printf 'unexpected logs_2.sqlite sqlite command: %s\n' "$2" >&2
      exit 90
      ;;
  esac
elif [[ "$#" -ge 2 ]]; then
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
            "MOCK_CODEX_RESOLVED_PATH": str(home / "mock-codex-package"),
            "CODEX_DOCTOR_LOG": str(home / "codex-doctor.log"),
            "SQLITE_LOG": str(home / "sqlite.log"),
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


def _create_codex_logs_db(path: Path, schema: str = CODEX_LOG_DB_SCHEMA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)


def _read_sqlite_log(env: dict[str, str]) -> str:
    sqlite_log = Path(env["SQLITE_LOG"])
    if not sqlite_log.exists():
        return ""
    return sqlite_log.read_text(encoding="utf-8")


def _contains_forbidden_key(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in forbidden or _contains_forbidden_key(child, forbidden)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item, forbidden) for item in value)
    return False


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
    assert config["agents"]["defaults"]["maxConcurrent"] == 2
    assert config["agents"]["defaults"]["subagents"]["maxConcurrent"] == 1
    assert "maxChildrenPerAgent" not in config["agents"]["defaults"]["subagents"]

    agents = {agent["id"]: agent for agent in config["agents"]["list"]}

    main = agents["main"]
    assert main["model"]["primary"] == "openai/gpt-5.4"
    assert main["skills"] == ["mempalace-readonly"]
    assert "autoresearch" not in main.get("skills", [])
    assert main.get("subagents", {}).get("allowAgents", []) == []
    assert main["tools"]["profile"] == "minimal"
    assert main["tools"]["allow"] == EXPECTED_MAIN_ALLOW
    assert "exec" in main["tools"]["deny"]
    assert "sessions_spawn" in main["tools"]["deny"]

    pm = agents["autoresearch-pm"]
    assert pm["model"]["primary"] == "openai/gpt-5.6-sol"
    assert pm["thinkingDefault"] == "high"
    assert pm["skills"] == ["mempalace-readonly", "autoresearch"]
    assert pm["tools"]["deny"] == [
        "sessions_spawn",
        "sessions_yield",
        "agents_list",
        "sessions_list",
        "sessions_history",
    ]
    assert "subagents" not in pm
    assert all(agent.get("subagents", {}).get("allowAgents", []) == [] for agent in agents.values())

    servers = config["mcp"]["servers"]
    assert list(servers) == ["mempalace-readonly", "g2-control"]
    assert servers["mempalace-readonly"]["codex"]["agents"] == [
        "main",
        "autoresearch-pm",
        *STAGE_AGENT_IDS,
    ]
    assert servers["g2-control"]["codex"]["agents"] == ["main"]
    assert servers["g2-control"]["codex"]["defaultToolsApprovalMode"] == "approve"
    assert servers["g2-control"]["args"] == ["-m", "gateway.g2_control_mcp_server"]
    codex_entry = config["plugins"]["entries"]["codex"]
    assert codex_entry["enabled"] is True
    app_server = codex_entry["config"]["appServer"]
    assert app_server["sandbox"] == "workspace-write"
    assert app_server["defaultWorkspaceDir"] == "/home/dev/.openclaw/autoresearch/model-workspaces"
    assert "networkProxy" not in app_server
    assert "nativeToolSurfaceEnabled" not in codex_entry["config"]
    assert "codexDynamicToolsExclude" not in codex_entry["config"]
    assert "danger-full-access" not in json.dumps(codex_entry)


def test_push_script_invariants_target_autoresearch_pm_not_main() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    assert "STALE_CODING_PROVIDER_KEYS" in script
    assert '"github-copilot"' in script
    assert '"copilot-proxy"' in script
    assert '"copilot-cli"' in script
    assert "sanitize_stale_coding_provider_keys" in script
    assert '  "main"\n  "autoresearch-pm"\n  "${MEMPALACE_READONLY_AGENT_IDS[@]}"' in script
    assert 'select(.id == "autoresearch-pm") | .model.primary' in script
    assert 'select(.id == "main") | .model.primary) = $pm' not in script
    assert "main interface split, read-only-only MemPalace projection" in script
    assert "PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS" in script
    assert "autoresearch-pm model/skills/native Codex delegation denies" in script
    assert ".agents.defaults.maxConcurrent == 2" in script
    assert ".agents.defaults.subagents.maxConcurrent == 1" in script
    assert ".agents.defaults.subagents.maxChildrenPerAgent? == null" in script
    assert "sessions_spawn" in script
    assert "G2_CONTROL_MCP_MODULE" in script
    assert "MAIN_OPENCLAW_TOOL_ALLOW_IDS" in script
    assert "main_allow == $main_openclaw_allow" in script
    assert '.tools.profile == "minimal"' in script
    assert "networkProxy? == null" in script
    assert "validate_codex_runtime_config" in script
    assert ".subagents.allowAgents?" in script
    assert "strict concurrency caps" in script
    assert "main interface restrictions" in script


def test_installed_codex_native_surface_is_not_enabled_by_main_wildcard_allow() -> None:
    config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    agents = {agent["id"]: agent for agent in config["agents"]["list"]}
    main_tools = agents["main"]["tools"]
    assert main_tools["profile"] == "minimal"
    assert main_tools["allow"] == EXPECTED_MAIN_ALLOW
    assert "*" not in main_tools["allow"]
    assert "exec" in main_tools["deny"]
    codex_config = config["plugins"]["entries"]["codex"]["config"]
    assert "nativeToolSurfaceEnabled" not in codex_config
    assert "codexDynamicToolsExclude" not in codex_config

    candidates = sorted(
        Path.home().glob(
            ".openclaw/npm/projects/*/node_modules/@openclaw/codex/dist/provider-capabilities-*.js"
        )
    )
    if not candidates:
        pytest.skip("installed @openclaw/codex provider-capabilities bundle not found")
    source = candidates[-1].read_text(encoding="utf-8")
    function_start = source.index("function shouldEnableCodexAppServerNativeToolSurface")
    function_body = source[function_start : source.index("function ", function_start + 1)]
    assert "if (toolsAllow === void 0)" in function_body
    assert "return hasWildcardCodexToolsAllow(toolsAllow)" in function_body


def test_installed_runtime_projects_main_mcp_servers_from_codex_agent_scope() -> None:
    config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    agents = {agent["id"]: agent for agent in config["agents"]["list"]}
    main_allow = agents["main"]["tools"]["allow"]
    assert main_allow == EXPECTED_MAIN_ALLOW
    servers = config["mcp"]["servers"]
    assert servers["g2-control"]["codex"]["agents"] == ["main"]
    assert "main" in servers["mempalace-readonly"]["codex"]["agents"]

    candidates = sorted(
        Path.home().glob(
            ".openclaw/npm/projects/*/node_modules/@openclaw/codex/dist/thread-lifecycle-*.js"
        )
    )
    if not candidates:
        pytest.skip("installed @openclaw/codex thread-lifecycle bundle not found")
    mcp_source = candidates[-1].read_text(encoding="utf-8")
    assert "buildCodexUserMcpServersThreadConfigPatch" in mcp_source
    assert "userMcpServersConfigPatch" in mcp_source


def test_bootstrap_reconciles_exact_codex_runtime_and_reinstalls_daemon() -> None:
    script = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert 'REQUIRED_OPENCLAW_VERSION="2026.7.1-2"' in script
    assert 'REQUIRED_CODEX_PLUGIN_VERSION="2026.7.1-1"' in script
    assert 'REQUIRED_CODEX_APP_SERVER_VERSION="0.144.3"' in script
    install = script.index(
        'plugins install "@openclaw/codex@${REQUIRED_CODEX_PLUGIN_VERSION}" --force --pin'
    )
    update = script.index("plugins update codex")
    inspect = script.index("if ! require_codex_plugin_exact; then", update)
    daemon_install = script.index('daemon install --force --port "${OPENCLAW_GATEWAY_PORT}" --json')
    push = script.index('local push_script="$REPO_ROOT/scripts/push-openclaw-config.sh"')

    assert install < update < inspect < daemon_install < push
    assert "daemon restart" not in script
    assert "daemon start" not in script


def test_mocked_bootstrap_openclaw_flow_runs_upgrade_steps_in_order(tmp_path: Path) -> None:
    result, flow_log = _run_mocked_bootstrap_openclaw_flow(tmp_path)

    assert result.returncode == 0, result.stderr
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw plugins inspect codex --json",
        "openclaw daemon install --force --port 18789 --json",
        f"make -C {REPO_ROOT} mempalace-install",
        f"mempalace {REPO_ROOT}/scripts/check-mempalace-health.py",
        f"push-config {PUSH_SCRIPT}",
    ]


def test_mocked_bootstrap_daemon_install_failure_aborts_before_push(tmp_path: Path) -> None:
    result, flow_log = _run_mocked_bootstrap_openclaw_flow(tmp_path, daemon_install_exit=73)

    assert result.returncode == 1
    commands = flow_log.read_text(encoding="utf-8").splitlines()
    assert commands[-1] == "openclaw daemon install --force --port 18789 --json"
    assert not any(command.startswith("make ") for command in commands)
    assert not any(command.startswith("mempalace ") for command in commands)
    assert not any(command.startswith("push-config ") for command in commands)
    assert "OpenClaw gateway service installation failed" in result.stdout


@pytest.mark.parametrize(
    ("env_name", "actual", "expected_error"),
    [
        (
            "MOCK_CODEX_PLUGIN_VERSION",
            "2026.7.1-2",
            "Codex plugin 2026.7.1-2 is unsupported; need exactly 2026.7.1-1",
        ),
        (
            "MOCK_CODEX_APP_SERVER_VERSION",
            "0.144.4",
            "Embedded @openai/codex 0.144.4 is unsupported; need exactly 0.144.3",
        ),
    ],
)
def test_push_script_rejects_non_exact_codex_runtime(
    tmp_path: Path,
    env_name: str,
    actual: str,
    expected_error: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env[env_name] = actual

    result = _run_push_script(env)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert not Path(env["CP_LOG"]).exists()


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
    assert "After=openclaw-gateway.service" in template
    assert "Requires=openclaw-gateway.service" in template
    assert "BindsTo=openclaw-gateway.service" in template
    assert "Restart=on-failure" in template
    assert "Restart=always" not in template


def test_gateway_runtime_caps_dropin_declares_exact_operator_caps() -> None:
    assert GATEWAY_RUNTIME_CAPS_DROPIN.read_text(encoding="utf-8").splitlines() == (
        EXPECTED_RUNTIME_CAP_LINES
    )


def test_codex_runtime_dropin_declares_prestart_verifier() -> None:
    assert EXPECTED_CODEX_RUNTIME_TEXT.splitlines() == [
        "[Service]",
        EXPECTED_CODEX_RUNTIME_EXECSTARTPRE,
    ]


def test_native_crash_hardening_dropin_contains_memory_and_restart_policy() -> None:
    assert NATIVE_CRASH_HARDENING_DROPIN.read_text(encoding="utf-8").splitlines() == (
        EXPECTED_NATIVE_CRASH_HARDENING_LINES
    )
    assert "OOMPolicy=continue" not in NATIVE_CRASH_HARDENING_DROPIN.read_text(encoding="utf-8")


def test_push_script_installs_gateway_runtime_caps_dropin_fail_closed() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    assert (
        'GATEWAY_RUNTIME_CAPS_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/'
        'openclaw-gateway-runtime-caps.conf"'
    ) in script
    assert 'GATEWAY_SERVICE_NAME="openclaw-gateway.service"' in script
    assert 'GATEWAY_RUNTIME_CAPS_DROPIN_NAME="10-quantipy-runtime-caps.conf"' in script
    assert 'CODEX_RUNTIME_DROPIN_NAME="20-openclaw-codex-runtime.conf"' in script
    assert 'NATIVE_CRASH_HARDENING_DROPIN_NAME="30-openclaw-native-crash-hardening.conf"' in script
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
    assert script.count("systemctl --user daemon-reload") == 3
    assert "GATEWAY_RUNTIME_CAPS_DROPIN" in script
    assert "GATEWAY_RUNTIME_CAPS_DROPIN_TMP:-" in script
    assert "GATEWAY_RUNTIME_CAPS_DROPIN_DST" in script
    assert "validate_codex_runtime_dropin_file" in script
    assert "CODEX_RUNTIME_DROPIN_DST" in script
    assert "validate_native_crash_hardening_dropin_file" in script
    assert "NATIVE_CRASH_HARDENING_DROPIN_DST" in script
    assert "validate_supervisor_unit_file" in script
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
    env["NODE_OPTIONS"] = "--require /home/dev/.openclaw/azure-api-version-preload.cjs"
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
    native_crash_hardening = _native_crash_hardening_dropin_dst(home)
    assert (
        native_crash_hardening.read_text(encoding="utf-8") == EXPECTED_NATIVE_CRASH_HARDENING_TEXT
    )
    supervisor_unit = home / ".config/systemd/user/quantipy-autoresearch-supervisor.service"
    supervisor_text = supervisor_unit.read_text(encoding="utf-8")
    assert "Requires=openclaw-gateway.service" in supervisor_text
    assert "BindsTo=openclaw-gateway.service" in supervisor_text
    assert "Restart=on-failure" in supervisor_text
    assert "Restart=always" not in supervisor_text
    assert _mode(dropin_dir) == 0o755
    assert _mode(dropin) == 0o644
    assert _mode(native_crash_hardening) == 0o644
    assert not list(dropin_dir.glob(".10-quantipy-runtime-caps.conf.*"))
    assert not (Path(env["OPENCLAW_PUSH_HOME"]) / "azure-api-version-preload.cjs").exists()
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user show openclaw-gateway.service" in systemctl_log
    assert "systemctl --user daemon-reload" in systemctl_log
    assert "restart openclaw-gateway.service" not in systemctl_log
    assert "start openclaw-gateway.service" not in systemctl_log
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert cp_log.count(str(GATEWAY_RUNTIME_CAPS_DROPIN)) == 1
    openclaw_log = Path(env["OPENCLAW_LOG"]).read_text(encoding="utf-8")
    assert openclaw_log.count("OPENCLAW_HOME=<unset>") == 4
    assert openclaw_log.count("OPENCLAW_PUSH_HOME=<unset>") == 4
    assert openclaw_log.count(f"OPENCLAW_STATE_DIR={env['EXPECTED_OPENCLAW_STATE_DIR']}") == 4
    assert openclaw_log.count(f"OPENCLAW_CONFIG_PATH={env['EXPECTED_OPENCLAW_CONFIG_PATH']}") == 3
    assert "config validate --json" in openclaw_log
    assert openclaw_log.count("NODE_OPTIONS=<unset>") == 4
    assert "inherited-openclaw-home" not in openclaw_log
    assert "inherited-state-dir" not in openclaw_log
    assert "inherited-config.json" not in openclaw_log
    assert "env-file-push-home" not in openclaw_log
    assert "env-file-state-dir" not in openclaw_log
    assert "env-file-config.json" not in openclaw_log


def test_push_script_installs_native_codex_stage_agents_to_autoresearch_workspaces(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    for workspace_name in ("workspace-autoresearch-pm", "workspace-reviewer"):
        agents_dir = openclaw_home / workspace_name / ".codex/agents"
        assert agents_dir.is_dir()
        for agent_id in STAGE_AGENT_IDS:
            copied = agents_dir / f"{agent_id}.toml"
            source = REPO_ROOT / ".codex/agents" / f"{agent_id}.toml"
            assert copied.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert not (openclaw_home / "workspace/.codex/agents").exists()
    for agent_id in ["main", "autoresearch-pm", *STAGE_AGENT_IDS]:
        codex_home = openclaw_home / "agents" / agent_id / "agent/codex-home"
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        assert config["approval_policy"] == "never"
        assert config["sandbox_mode"] == "workspace-write"
        workspace_write = config["sandbox_workspace_write"]
        assert workspace_write["network_access"] is True
        assert workspace_write["writable_roots"] == [
            "/home/dev/.openclaw/autoresearch/model-workspaces",
            "/home/dev/.openclaw/autoresearch/stage-inbox",
        ]
        assert "permissions" not in config
        assert "network_proxy" not in config
        mcp_servers = config["mcp_servers"]
        expected_servers = (
            {"mempalace-readonly", "g2-control"} if agent_id == "main" else {"mempalace-readonly"}
        )
        assert set(mcp_servers) == expected_servers
        assert mcp_servers["mempalace-readonly"]["args"][-2:] == [
            "--palace",
            str(Path(env["HOME"]) / ".mempalace/palace"),
        ]
        if agent_id == "main":
            assert mcp_servers["g2-control"]["args"] == ["-m", "gateway.g2_control_mcp_server"]
            assert mcp_servers["g2-control"]["default_tools_approval_mode"] == "approve"
        agents_dir = codex_home / "agents"
        assert agents_dir.is_dir()
        if agent_id == "main":
            assert list(agents_dir.glob("*.toml")) == []
        else:
            for stage_id in STAGE_AGENT_IDS:
                copied = agents_dir / f"{stage_id}.toml"
                source = REPO_ROOT / ".codex/agents" / f"{stage_id}.toml"
                assert copied.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    doctor_log = Path(env["CODEX_DOCTOR_LOG"]).read_text(encoding="utf-8")
    for agent_id in ["main", "autoresearch-pm", *STAGE_AGENT_IDS]:
        codex_home = openclaw_home / "agents" / agent_id / "agent/codex-home"
        assert f"{codex_home} --strict-config doctor --json" in doctor_log


def test_push_script_allows_expected_non_owned_codex_doctor_failures(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    assert "Codex doctor non-owned failures ignored" in result.stdout
    assert "auth.credentials" in result.stdout
    assert "installation" in result.stdout
    assert "network.websocket_reachability" in result.stdout
    assert "updates.status" in result.stdout
    assert "OpenClaw owns OAuth in openclaw-agent.sqlite" in result.stdout


@pytest.mark.parametrize(
    ("env_name", "env_value", "expected_check"),
    [
        ("MOCK_CODEX_DOCTOR_CONFIG_STATUS", "fail", "config.load=fail"),
        ("MOCK_CODEX_DOCTOR_MCP_STATUS", "fail", "mcp.config=fail"),
        ("MOCK_CODEX_DOCTOR_SANDBOX_STATUS", "fail", "sandbox.helpers=fail"),
        ("MOCK_CODEX_DOCTOR_RUNTIME_STATUS", "fail", "runtime.provenance=fail"),
        ("MOCK_CODEX_DOCTOR_RUNTIME_STATUS", "warning", "runtime.provenance=warning"),
    ],
)
def test_push_script_rejects_owned_codex_doctor_failures(
    tmp_path: Path,
    env_name: str,
    env_value: str,
    expected_check: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env[env_name] = env_value

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "owned Codex doctor checks failed" in result.stderr
    assert expected_check in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_accepts_zero_codex_doctor_exit_only_when_all_checks_ok(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_EXIT_STATUS"] = "0"
    env["MOCK_CODEX_DOCTOR_AUTH_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_INSTALL_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_UPDATE_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_WEBSOCKET_STATUS"] = "ok"

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    assert "Codex doctor non-owned failures ignored" not in result.stdout


def test_push_script_rejects_zero_codex_doctor_exit_with_allowed_non_owned_checks(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_EXIT_STATUS"] = "0"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "doctor exited 0" in result.stderr
    assert "auth.credentials" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_accepts_one_codex_doctor_exit_for_allowed_non_owned_checks(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    assert "Codex doctor non-owned failures ignored" in result.stdout


def test_push_script_rejects_one_codex_doctor_exit_without_allowed_non_owned_checks(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_AUTH_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_INSTALL_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_UPDATE_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_WEBSOCKET_STATUS"] = "ok"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "doctor exited 1" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_seven_codex_doctor_exit_even_with_allowed_non_owned_checks(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_EXIT_STATUS"] = "7"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "doctor exited 7" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_unexpected_fatal_codex_doctor_failure(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_EXTRA_FAIL"] = "state.paths"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "unexpected fatal Codex doctor checks" in result.stderr
    assert "state.paths=fail" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_unexpected_codex_doctor_warning(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_EXTRA_WARNING"] = "network.proxy"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "unexpected fatal Codex doctor checks" in result.stderr
    assert "network.proxy=warning" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_validates_generated_openclaw_config_before_write(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    env["MOCK_OPENCLAW_CONFIG_VALIDATE_FAIL"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Generated OpenClaw config failed schema validation before write" in result.stderr
    assert "config validate --json" in Path(env["OPENCLAW_LOG"]).read_text(encoding="utf-8")
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize(
    ("env_name", "value", "expected_check"),
    [
        ("MOCK_CODEX_DOCTOR_AUTH_STORAGE_MODE", "SQLite", "auth.credentials=fail"),
        (
            "MOCK_CODEX_DOCTOR_INSTALL_SUMMARY",
            "a different install was detected",
            "installation=fail",
        ),
        ("MOCK_CODEX_DOCTOR_UPDATE_SUMMARY", "update timed out", "updates.status=fail"),
        (
            "MOCK_CODEX_DOCTOR_WEBSOCKET_AUTH_MODE",
            "bearer",
            "network.websocket_reachability=warning",
        ),
    ],
)
def test_push_script_rejects_allowed_codex_doctor_shape_drift(
    tmp_path: Path,
    env_name: str,
    value: str,
    expected_check: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env[env_name] = value

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "unexpected fatal Codex doctor checks" in result.stderr
    assert expected_check in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize(
    ("check_id", "detail_key", "mutation"),
    [
        ("installation", "extra", "extra"),
        ("updates.status", "running package root", "missing"),
    ],
)
def test_push_script_rejects_allowed_codex_doctor_detail_key_drift(
    tmp_path: Path,
    check_id: str,
    detail_key: str,
    mutation: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_DETAIL_KEY"] = detail_key
    if mutation == "extra":
        env["MOCK_CODEX_DOCTOR_EXTRA_DETAIL_CHECK"] = check_id
    else:
        env["MOCK_CODEX_DOCTOR_DELETE_DETAIL_CHECK"] = check_id

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "unexpected fatal Codex doctor checks" in result.stderr
    assert f"{check_id}=" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_accepts_missing_scoped_codex_log_db_without_sqlite_access(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    sqlite_log = _read_sqlite_log(env)
    assert str(log_db) not in sqlite_log
    assert not log_db.exists()


def test_push_script_repairs_scoped_codex_log_db_index_corruption(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db)
    env["MOCK_CODEX_LOG_DB_INTEGRITY"] = "corrupt"

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    sqlite_log = _read_sqlite_log(env)
    assert f"sqlite3 {log_db} PRAGMA integrity_check;" in sqlite_log
    assert f"sqlite3 {log_db} REINDEX idx_logs_thread_id; PRAGMA integrity_check;" in sqlite_log
    assert "REINDEX;" not in sqlite_log
    assert f"Repaired scoped Codex log DB idx_logs_thread_id: {log_db}" in result.stdout


def test_push_script_fails_when_scoped_codex_log_db_reindex_cannot_repair(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db)
    env["MOCK_CODEX_LOG_DB_INTEGRITY"] = "corrupt"
    env["MOCK_CODEX_LOG_DB_REPAIR"] = "fail"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert f"Scoped Codex log DB validation/repair failed for {log_db}" in result.stderr
    assert "remains corrupt after REINDEX idx_logs_thread_id" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_scoped_codex_log_db_symlink(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    real_db = tmp_path / "real-logs.sqlite"
    _create_codex_logs_db(real_db)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    log_db.parent.mkdir(parents=True)
    log_db.symlink_to(real_db)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_dangling_scoped_codex_log_db_symlink(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    log_db.parent.mkdir(parents=True)
    log_db.symlink_to(tmp_path / "missing-logs.sqlite")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert str(log_db) not in _read_sqlite_log(env)
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_nonregular_scoped_codex_log_db_path(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    log_db.mkdir(parents=True)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "must be a regular file" in result.stderr
    assert str(log_db) not in _read_sqlite_log(env)
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_hardlinked_scoped_codex_log_db_before_sqlite_access(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db)
    hardlink = tmp_path / "logs-hardlink.sqlite"
    os.link(log_db, hardlink)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "must not have hard links" in result.stderr
    assert str(log_db) not in _read_sqlite_log(env)
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_scoped_codex_log_db_wrong_owner(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db)
    wrong_uid = 65534 if os.geteuid() != 65534 else 0
    try:
        os.chown(log_db, wrong_uid, -1)
    except PermissionError:
        pytest.skip("changing file owner requires elevated test privileges")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "does not match current uid" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_wrong_scoped_codex_log_db_schema(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db, "CREATE TABLE logs (id INTEGER PRIMARY KEY, thread_id TEXT);")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "schema does not match the pinned logs_2.sqlite schema" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize("integrity", ["other_index", "table"])
def test_push_script_rejects_non_idx_logs_thread_id_corruption(
    tmp_path: Path,
    integrity: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db)
    env["MOCK_CODEX_LOG_DB_INTEGRITY"] = integrity

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "non-repairable integrity errors" in result.stderr
    sqlite_log = _read_sqlite_log(env)
    assert "REINDEX idx_logs_thread_id" not in sqlite_log
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_removes_legacy_native_codex_stage_agents_in_place(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    legacy_agent_ids = [
        "context-curator",
        "debater-microstructure",
        "debater-data",
        "debater-skeptic",
        "debater-theory",
        "debater-implementation",
        "consensus-arbiter",
    ]
    managed_agent_dirs = [
        *(
            openclaw_home / workspace / ".codex/agents"
            for workspace in ("workspace-autoresearch-pm", "workspace-reviewer")
        ),
        *(
            openclaw_home / "agents" / agent_id / "agent/codex-home/agents"
            for agent_id in ["main", "autoresearch-pm", *STAGE_AGENT_IDS]
        ),
    ]
    for agents_dir in managed_agent_dirs:
        agents_dir.mkdir(parents=True)
        for legacy_agent_id in legacy_agent_ids:
            (agents_dir / f"{legacy_agent_id}.toml").write_text("legacy\n", encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    for agents_dir in managed_agent_dirs:
        assert not any((agents_dir / f"{agent_id}.toml").exists() for agent_id in legacy_agent_ids)


def test_push_script_removes_stale_deployed_mempalace_write_skill(tmp_path: Path) -> None:
    # Arrange
    env = _prepare_push_script_home(tmp_path)
    stale_skill = Path(env["OPENCLAW_PUSH_HOME"]) / "skills" / "mempalace"
    stale_skill.mkdir(parents=True)
    (stale_skill / "SKILL.md").write_text("stale write skill\n", encoding="utf-8")

    # Act
    result = _run_push_script(env)

    # Assert
    assert result.returncode == 0, result.stderr
    assert not stale_skill.exists()


def test_push_script_invariants_validate_native_codex_stage_agent_roster() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    assert 'CODEX_AGENTS_SRC="${REPO_ROOT}/.codex/agents"' in script
    assert "validate_codex_native_stage_agents_dir" in script
    assert 'validate_codex_native_stage_agents_dir "${CODEX_AGENTS_SRC}"' in script
    assert 'validate_codex_native_stage_agents_dir "${CODEX_AGENTS_DST}"' in script
    assert '"${OPENCLAW_PUSH_HOME}/agents/${CODEX_RUNTIME_AGENT_ID}/agent/codex-home"' in script
    assert 'validate_codex_native_stage_agents_dir "${CODEX_RUNTIME_AGENTS_DST}"' in script
    assert 'CODEX_NATIVE_RUNTIME_AGENT_IDS=("main" "autoresearch-pm"' in script
    assert 'if [[ "${CODEX_RUNTIME_AGENT_ID}" == "main" ]]; then' in script
    assert "CODEX_NATIVE_LEGACY_STAGE_AGENT_IDS" in script
    assert "remove_legacy_codex_stage_agents" in script
    assert "must not override inherited MCP servers" in script
    for agent_id in STAGE_AGENT_IDS:
        assert f'"{agent_id}"' in script


def test_repo_native_codex_stage_agents_have_no_mcp_overrides() -> None:
    for agent_id in STAGE_AGENT_IDS:
        path = REPO_ROOT / ".codex/agents" / f"{agent_id}.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert "mcp_servers" not in data, agent_id


def test_push_script_removes_stale_copilot_provider_keys_from_local_config(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    stale_config = {
        "models": {
            "providers": {
                "github-copilot": {
                    "enabled": True,
                    "models": [{"id": "legacy", "reasoning": False}],
                },
                "copilot-proxy": {
                    "enabled": True,
                    "models": [{"id": "legacy", "reasoning": False}],
                },
                "copilot-cli": {
                    "enabled": True,
                    "models": [{"id": "legacy", "reasoning": False}],
                },
            }
        },
        "agents": {
            "defaults": {
                "models": {
                    "github-copilot/legacy": {},
                    "copilot-proxy/legacy": {},
                    "copilot-cli/legacy": {},
                },
                "legacy": {"github-copilot": {"enabled": True}},
            }
        },
    }
    openclaw_config.write_text(json.dumps(stale_config), encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    published = json.loads(openclaw_config.read_text(encoding="utf-8"))
    forbidden = {"github-copilot", "copilot-proxy", "copilot-cli"}
    assert _contains_forbidden_key(published, forbidden) is False
    assert set(published["models"]["providers"]) >= {"openai", "azure-oai-g2", "openrouter"}
    assert "Sanitized stale coding-provider config keys" in result.stdout


def test_push_script_codex_removes_stale_azure_node_options_from_gateway_unit(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["SYSTEMD_MANAGER_NODE_OPTIONS"] = (
        "--require /home/dev/.openclaw/azure-api-version-preload.cjs"
    )
    home = Path(env["HOME"])
    service_path = home / ".config/systemd/user/openclaw-gateway.service"
    service_path.parent.mkdir(parents=True)
    service_path.write_text(
        "\n".join(
            [
                "[Service]",
                'Environment="NODE_OPTIONS=--require '
                '/home/dev/.openclaw/azure-api-version-preload.cjs"',
                "ExecStart=/usr/bin/node /tmp/openclaw/dist/index.js gateway --port 18789",
                "",
            ]
        ),
        encoding="utf-8",
    )
    stale_dropin = service_path.parent / "openclaw-gateway.service.d/05-legacy-azure.conf"
    stale_dropin.parent.mkdir()
    stale_dropin.write_text(
        "\n".join(
            [
                "[Service]",
                'Environment="NODE_OPTIONS=--require '
                '/home/dev/.openclaw/azure-api-version-preload.cjs"',
                "Environment=KEEP_ME=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    service_text = service_path.read_text(encoding="utf-8")
    assert "azure-api-version-preload.cjs" not in service_text
    assert "NODE_OPTIONS" not in service_text
    assert "ExecStart=/usr/bin/node" in service_text
    stale_dropin_text = stale_dropin.read_text(encoding="utf-8")
    assert "azure-api-version-preload.cjs" not in stale_dropin_text
    assert "NODE_OPTIONS" not in stale_dropin_text
    assert "Environment=KEEP_ME=1" in stale_dropin_text
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" in systemctl_log
    assert "unset NODE_OPTIONS" in systemctl_log
    assert systemctl_log.count("systemctl --user daemon-reload") == 2


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
    assert (
        _native_crash_hardening_dropin_dst(home).read_text(encoding="utf-8")
        == EXPECTED_NATIVE_CRASH_HARDENING_TEXT
    )
    assert _mode(dropin.parent) == 0o755
    assert _mode(dropin) == 0o644
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert systemctl_log.count("systemctl --user daemon-reload") == 2
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert cp_log.count(str(GATEWAY_RUNTIME_CAPS_DROPIN)) == 2


def test_gateway_cli_does_not_inject_azure_preload_into_codex_daemon() -> None:
    cli_source = (REPO_ROOT / "gateway/cli.py").read_text(encoding="utf-8")

    assert "Injected Azure preload into systemd service" not in cli_source
    assert "without Azure api-version preload" not in cli_source
    assert "NODE_OPTIONS=--require" not in cli_source


def test_push_script_daemon_reload_runs_after_dropin_install_and_failure_aborts(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_DAEMON_RELOAD"] = "1"
    home = Path(env["HOME"])
    initial_config = (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 23
    assert "daemon-reload failed by test" in result.stderr
    assert "Restoring managed systemd files after failed publication." in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert not _supervisor_unit_dst(home).exists()
    assert not _runtime_caps_dropin_dst(home).exists()
    assert not _codex_runtime_dropin_dst(home).exists()
    assert not _native_crash_hardening_dropin_dst(home).exists()
    assert (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8") == (
        initial_config
    )
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "daemon-reload saw managed dropins" in systemctl_log
    assert systemctl_log.index("systemctl --user show openclaw-gateway.service") < (
        systemctl_log.index("systemctl --user daemon-reload")
    )
    assert "restart openclaw-gateway.service" not in systemctl_log
    assert "start openclaw-gateway.service" not in systemctl_log


def test_push_script_managed_systemd_publication_rolls_back_existing_files(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_DAEMON_RELOAD"] = "1"
    home = Path(env["HOME"])
    initial_config = (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8")
    prior_files = {
        _supervisor_unit_dst(home): "[Unit]\nDescription=prior supervisor\n",
        _runtime_caps_dropin_dst(home): "[Service]\nEnvironment=PRIOR_CAP=1\n",
        _codex_runtime_dropin_dst(home): "[Service]\nExecStartPre=/bin/true\n",
        _native_crash_hardening_dropin_dst(home): "[Service]\nOOMPolicy=continue\n",
    }
    for path, content in prior_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    result = _run_push_script(env)

    assert result.returncode == 23
    assert "Restoring managed systemd files after failed publication." in result.stderr
    for path, content in prior_files.items():
        assert path.read_text(encoding="utf-8") == content
        assert _mode(path) == 0o600
    assert (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8") == (
        initial_config
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
printf '#!/usr/bin/env bash\nprintf "openclaw 2026.7.1-2\\\\n"\n' > "$prefix/bin/openclaw"
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
    assert "VERSION=2026.7.1-2" in result.stdout
    assert npm_log.read_text(encoding="utf-8").strip() == (
        f"install -g --prefix {tmp_path / 'npm-global'} openclaw@2026.7.1-2"
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
printf '#!/usr/bin/env bash\nprintf "openclaw 2026.7.1-2\\\\n"\n' > "$PNPM_HOME/openclaw"
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
    assert "VERSION=2026.7.1-2" in result.stdout
    assert pnpm_log.read_text(encoding="utf-8").strip() == "add -g openclaw@2026.7.1-2"


def test_bootstrap_keeps_exact_automatic_candidate_without_installing(tmp_path: Path) -> None:
    preferred = tmp_path / ".local/share/pnpm/openclaw"
    _write_executable(preferred, "printf 'openclaw 2026.7.1-2\\n'")
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
    _write_executable(override, "printf 'openclaw 2026.7.1-2\\n'")
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
    ["2026.7.1-2-beta.1", "2026.7.1-2+build", "2026.7.1-2.1"],
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
    assert "need exactly 2026.7.1-2" in result.stdout


def test_push_script_rejects_newer_openclaw_before_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    openclaw_home = tmp_path / "openclaw-home"
    executable = home / "openclaw"
    _write_executable(executable, "printf 'openclaw 2026.7.2\\n'")

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
    assert "unsupported; need exactly 2026.7.1-2" in result.stderr
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
printf 'openclaw 2026.7.1-2\n'
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
    _write_executable(executable, "printf 'openclaw 2026.7.1-2\\n'")

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
    assert f"Using OpenClaw: {executable} (version 2026.7.1-2)" in result.stdout
    assert f"Local OpenClaw config not found at {openclaw_home / 'openclaw.json'}" in result.stderr


@pytest.mark.parametrize(
    "version_token",
    ["2026.7.1-2-beta.1", "2026.7.1-2+build", "2026.7.1-2.1"],
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
    assert "need exactly 2026.7.1-2" in result.stderr
    assert not openclaw_home.exists()
