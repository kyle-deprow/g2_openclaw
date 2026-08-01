"""Regression tests for OpenClaw guards in repository shell scripts."""

from __future__ import annotations

import hashlib
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
CODEX_STATE_THREADS_TAIL = (
    ", cli_version TEXT NOT NULL DEFAULT '', "
    "first_user_message TEXT NOT NULL DEFAULT '', "
    "agent_nickname TEXT, agent_role TEXT, "
    "memory_mode TEXT NOT NULL DEFAULT 'enabled', "
    "model TEXT, reasoning_effort TEXT, agent_path TEXT, "
    "created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT, "
    "preview TEXT NOT NULL DEFAULT '', "
    "recency_at INTEGER NOT NULL DEFAULT 0, "
    "recency_at_ms INTEGER NOT NULL DEFAULT 0, "
    "history_mode TEXT NOT NULL DEFAULT 'legacy'"
)
CODEX_STATE_THREADS_ARCHIVED_CWD_CREATED_INDEX = (
    "CREATE INDEX idx_threads_archived_cwd_created_at_ms "
    "ON threads(archived, cwd, created_at_ms DESC, id DESC);"
)
CODEX_STATE_THREADS_ARCHIVED_CWD_UPDATED_INDEX = (
    "CREATE INDEX idx_threads_archived_cwd_updated_at_ms "
    "ON threads(archived, cwd, updated_at_ms DESC, id DESC);"
)
CODEX_STATE_DB_SCHEMA = f"""
CREATE TABLE _sqlx_migrations (
    version BIGINT PRIMARY KEY,
    description TEXT NOT NULL,
    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    success BOOLEAN NOT NULL,
    checksum BLOB NOT NULL,
    execution_time BIGINT NOT NULL
);
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT
{CODEX_STATE_THREADS_TAIL});
CREATE INDEX idx_threads_created_at ON threads(created_at DESC, id DESC);
CREATE INDEX idx_threads_updated_at ON threads(updated_at DESC, id DESC);
CREATE INDEX idx_threads_archived ON threads(archived);
CREATE INDEX idx_threads_source ON threads(source);
CREATE INDEX idx_threads_provider ON threads(model_provider);
CREATE TABLE thread_dynamic_tools (
    thread_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT 0, namespace TEXT,
    PRIMARY KEY(thread_id, position),
    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
);
CREATE INDEX idx_thread_dynamic_tools_thread ON thread_dynamic_tools(thread_id);
CREATE TABLE backfill_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    last_watermark TEXT,
    last_success_at INTEGER,
    updated_at INTEGER NOT NULL
);
CREATE TABLE agent_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    instruction TEXT NOT NULL,
    output_schema_json TEXT,
    input_headers_json TEXT NOT NULL,
    input_csv_path TEXT NOT NULL,
    output_csv_path TEXT NOT NULL,
    auto_export INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    last_error TEXT
, max_runtime_seconds INTEGER);
CREATE TABLE agent_job_items (
    job_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    source_id TEXT,
    row_json TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_thread_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    reported_at INTEGER,
    PRIMARY KEY (job_id, item_id),
    FOREIGN KEY(job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE
);
CREATE INDEX idx_agent_jobs_status ON agent_jobs(status, updated_at DESC);
CREATE INDEX idx_agent_job_items_status ON agent_job_items(job_id, status, row_index ASC);
CREATE TABLE thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE INDEX idx_thread_spawn_edges_parent_status
    ON thread_spawn_edges(parent_thread_id, status);
CREATE TABLE remote_control_enrollments (
    websocket_url TEXT NOT NULL,
    account_id TEXT NOT NULL,
    app_server_client_name TEXT NOT NULL,
    server_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    server_name TEXT NOT NULL,
    updated_at INTEGER NOT NULL, remote_control_enabled INTEGER,
    PRIMARY KEY (websocket_url, account_id, app_server_client_name)
);
CREATE TRIGGER threads_created_at_ms_after_insert
AFTER INSERT ON threads
WHEN NEW.created_at_ms IS NULL
BEGIN
    UPDATE threads
    SET created_at_ms = NEW.created_at * 1000
    WHERE id = NEW.id;
END;
CREATE TRIGGER threads_updated_at_ms_after_insert
AFTER INSERT ON threads
WHEN NEW.updated_at_ms IS NULL
BEGIN
    UPDATE threads
    SET updated_at_ms = NEW.updated_at * 1000
    WHERE id = NEW.id;
END;
CREATE TRIGGER threads_created_at_ms_after_update
AFTER UPDATE OF created_at ON threads
WHEN NEW.created_at != OLD.created_at
 AND NEW.created_at_ms IS OLD.created_at_ms
BEGIN
    UPDATE threads
    SET created_at_ms = NEW.created_at * 1000
    WHERE id = NEW.id;
END;
CREATE TRIGGER threads_updated_at_ms_after_update
AFTER UPDATE OF updated_at ON threads
WHEN NEW.updated_at != OLD.updated_at
 AND NEW.updated_at_ms IS OLD.updated_at_ms
BEGIN
    UPDATE threads
    SET updated_at_ms = NEW.updated_at * 1000
    WHERE id = NEW.id;
END;
CREATE INDEX idx_threads_created_at_ms ON threads(created_at_ms DESC, id DESC);
CREATE INDEX idx_threads_updated_at_ms ON threads(updated_at_ms DESC, id DESC);
{CODEX_STATE_THREADS_ARCHIVED_CWD_CREATED_INDEX}
{CODEX_STATE_THREADS_ARCHIVED_CWD_UPDATED_INDEX}
CREATE INDEX idx_threads_visible_created_at_ms
    ON threads(archived, created_at_ms DESC)
    WHERE preview <> '';
CREATE INDEX idx_threads_visible_updated_at_ms
    ON threads(archived, updated_at_ms DESC)
    WHERE preview <> '';
CREATE TABLE external_agent_config_imports (
    import_id TEXT PRIMARY KEY,
    completed_at_ms INTEGER NOT NULL,
    successes TEXT NOT NULL,
    failures TEXT NOT NULL
);
CREATE TRIGGER threads_recency_at_after_insert
AFTER INSERT ON threads
WHEN NEW.recency_at_ms = 0
BEGIN
    UPDATE threads
    SET recency_at = NEW.updated_at,
        recency_at_ms = COALESCE(NEW.updated_at_ms, NEW.updated_at * 1000)
    WHERE id = NEW.id;
END;
CREATE INDEX idx_threads_recency_at_ms
    ON threads(recency_at_ms DESC, id DESC);
CREATE INDEX idx_threads_archived_cwd_recency_at_ms
    ON threads(archived, cwd, recency_at_ms DESC, id DESC);
CREATE INDEX idx_threads_visible_recency_at_ms
    ON threads(archived, recency_at_ms DESC, id DESC)
    WHERE preview <> '';
""".strip()
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


def _run_bootstrap_tailscale_check(home: Path) -> subprocess.CompletedProcess[str]:
    command = """
bootstrap_script="$1"
set -- --skip-optional
source "$bootstrap_script"
SKIP_OPTIONAL=true
check_tailscale
"""
    return subprocess.run(
        ["bash", "-c", command, "bootstrap-tailscale-test", str(BOOTSTRAP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=_base_subprocess_env(home),
    )


def _run_mocked_bootstrap_openclaw_flow(
    tmp_path: Path,
    *,
    daemon_install_exit: int = 0,
    create_live_config: bool = True,
    repo_validate_exit: int = 0,
    candidate_validate_exit: int = 0,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "home"
    flow_log = tmp_path / "bootstrap-flow.log"
    mock_openclaw = tmp_path / "mock-bin/openclaw"
    (home / ".openclaw").mkdir(parents=True)
    if create_live_config:
        (home / ".openclaw/openclaw.json").write_text("{}\n", encoding="utf-8")
    _write_executable(
        mock_openclaw,
        r"""
if [[ -v OPENCLAW_HOME ]]; then
  printf 'leaked OPENCLAW_HOME=%s\n' "$OPENCLAW_HOME" >&2
  exit 60
fi
if [[ -v OPENCLAW_PUSH_HOME ]]; then
  printf 'leaked OPENCLAW_PUSH_HOME=%s\n' "$OPENCLAW_PUSH_HOME" >&2
  exit 61
fi
if [[ -n "${NODE_OPTIONS:-}" ]]; then
  printf 'leaked NODE_OPTIONS=%s\n' "$NODE_OPTIONS" >&2
  exit 62
fi
printf 'openclaw %s\n' "$*" >> "$FLOW_LOG"
printf '%s\t%s\t%s\n' \
  "$*" \
  "${OPENCLAW_STATE_DIR:-<unset>}" \
  "${OPENCLAW_CONFIG_PATH:-<unset>}" >> "${FLOW_LOG}.contexts"
is_bootstrap_repo_config() {
  case "${OPENCLAW_CONFIG_PATH:-}" in
    "$TEST_ROOT"/g2-openclaw-bootstrap.*/openclaw.repo-preflight.json)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}
is_bootstrap_candidate_config() {
  case "${OPENCLAW_CONFIG_PATH:-}" in
    "$TEST_ROOT"/g2-openclaw-bootstrap.*/openclaw.candidate.json)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}
require_repo_config() {
  if [[ "${OPENCLAW_CONFIG_PATH:-}" == "$EXPECTED_REPO_OPENCLAW_CONFIG_PATH" ]]; then
    printf 'repo command used tracked repo overlay\n' >&2
    exit 65
  fi
  case "${OPENCLAW_STATE_DIR:-}" in
    "$TEST_ROOT"/g2-openclaw-bootstrap.*/state)
      ;;
    *)
      printf 'repo command used unexpected OPENCLAW_STATE_DIR=%s\n' \
        "${OPENCLAW_STATE_DIR:-<unset>}" >&2
      exit 69
      ;;
  esac
  if ! is_bootstrap_repo_config; then
    printf 'unexpected repo OPENCLAW_CONFIG_PATH=%s\n' "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
    exit 70
  fi
  plugin_artifacts_are_complete || {
    printf 'repo validation ran before disposable plugin availability: %s\n' \
      "$(plugin_state_dir)" >&2
    exit 71
  }
}
require_candidate_config() {
  if [[ "${OPENCLAW_CONFIG_PATH:-}" == "$EXPECTED_REPO_OPENCLAW_CONFIG_PATH" ]]; then
    printf 'lifecycle command used tracked repo overlay\n' >&2
    exit 63
  fi
  case "${OPENCLAW_STATE_DIR:-}" in
    "$EXPECTED_LIVE_OPENCLAW_STATE_DIR"|"$TEST_ROOT"/g2-openclaw-bootstrap.*/state)
      ;;
    *)
      printf 'candidate command used unexpected OPENCLAW_STATE_DIR=%s\n' \
        "${OPENCLAW_STATE_DIR:-<unset>}" >&2
      exit 66
      ;;
  esac
  if ! is_bootstrap_candidate_config; then
      printf 'unexpected candidate OPENCLAW_CONFIG_PATH=%s\n' "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
      exit 64
  fi
}
require_live_config() {
  if [[ "${OPENCLAW_STATE_DIR:-}" != "$EXPECTED_LIVE_OPENCLAW_STATE_DIR" ]]; then
    printf 'live command used non-live OPENCLAW_STATE_DIR=%s\n' "${OPENCLAW_STATE_DIR:-<unset>}" >&2
    exit 67
  fi
  if [[ "${OPENCLAW_CONFIG_PATH:-}" != "$EXPECTED_LIVE_OPENCLAW_CONFIG_PATH" ]]; then
    printf 'live command used non-live OPENCLAW_CONFIG_PATH=%s\n' \
      "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
    exit 68
  fi
}
plugin_state_dir() {
  printf '%s/plugins/codex' "$OPENCLAW_STATE_DIR"
}
plugin_artifacts_are_complete() {
  for artifact in installed updated enabled; do
    [[ -f "$(plugin_state_dir)/$artifact" ]] || return 1
  done
}
alias_openclaw_config_path() {
  local mode="$1"
  local alias_target
  [[ -n "$mode" ]] || return 0
  alias_target="$TEST_ROOT/bootstrap-alias-$(basename "$OPENCLAW_CONFIG_PATH").target"
  cp "$OPENCLAW_CONFIG_PATH" "$alias_target"
  rm -f "$OPENCLAW_CONFIG_PATH"
  case "$mode" in
    symlink)
      ln -s "$alias_target" "$OPENCLAW_CONFIG_PATH"
      ;;
    hardlink)
      ln "$alias_target" "$OPENCLAW_CONFIG_PATH"
      ;;
    *)
      printf 'unknown bootstrap alias mode: %s\n' "$mode" >&2
      exit 87
      ;;
  esac
}
record_plugin_artifact() {
  local artifact_dir
  artifact_dir="$(plugin_state_dir)"
  mkdir -p "$artifact_dir"
  printf '%s\n' "$*" > "$artifact_dir/$1"
}
case "${1:-}" in
  config)
    [[ "${2:-}" == "validate" && "${3:-}" == "--json" ]] || exit 90
    if is_bootstrap_repo_config; then
      require_repo_config
      if [[ "${REPO_VALIDATE_PREFIX_INVALID_THEN_VALID:-0}" == "1" ]]; then
        cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "repo.prefix", "message": "prefix invalidity injected by test"}],
  "warnings": []
}
{"valid":true,"warnings":[]}
JSON
        exit 0
      fi
      if [[ "${REPO_VALIDATE_PREFIX_WARNING_THEN_VALID:-0}" == "1" ]]; then
        cat <<'JSON'
{
  "valid": true,
  "warnings": [{"path": "repo.prefix.warning", "message": "prefix warning injected by test"}]
}
{"valid":true,"warnings":[]}
JSON
        exit 0
      fi
      if [[ "$REPO_VALIDATE_EXIT" != "0" ]]; then
        cat <<'JSON'
{"valid":false,"errors":[{"path":"repo.injected","message":"invalid repo overlay"}],"warnings":[]}
JSON
        exit "$REPO_VALIDATE_EXIT"
      fi
      if [[ "${REPO_VALIDATE_WARNINGS:-0}" == "1" ]]; then
        cat <<'JSON'
{"valid":true,"warnings":[{"path":"repo.warning","message":"warning injected by test"}]}
JSON
      else
        printf '{"valid":true,"warnings":[]}\n'
      fi
      if [[ "${MUTATE_BOOTSTRAP_REPO_COPY:-0}" == "1" ]]; then
        printf '\n{"mutated":true}\n' >> "$OPENCLAW_CONFIG_PATH"
      fi
      alias_openclaw_config_path "${ALIAS_BOOTSTRAP_REPO_CONFIG_AFTER_VALIDATE:-}"
    elif is_bootstrap_candidate_config; then
      require_candidate_config "$@"
      if [[ "${CANDIDATE_VALIDATE_PREFIX_INVALID_THEN_VALID:-0}" == "1" ]]; then
        cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "candidate.prefix", "message": "prefix invalidity injected by test"}],
  "warnings": []
}
{"valid":true,"warnings":[]}
JSON
        exit 0
      fi
      if [[ "${CANDIDATE_VALIDATE_PREFIX_WARNING_THEN_VALID:-0}" == "1" ]]; then
        cat <<'JSON'
{
  "valid": true,
  "warnings": [
    {"path": "candidate.prefix.warning", "message": "prefix warning injected by test"}
  ]
}
{"valid":true,"warnings":[]}
JSON
        exit 0
      fi
      if [[ "$CANDIDATE_VALIDATE_EXIT" != "0" ]]; then
        cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "candidate.injected", "message": "invalid candidate overlay"}],
  "warnings": []
}
JSON
        exit "$CANDIDATE_VALIDATE_EXIT"
      fi
      if [[ "${CANDIDATE_VALIDATE_WARNINGS:-0}" == "1" ]]; then
        cat <<'JSON'
{"valid":true,"warnings":[{"path":"candidate.warning","message":"warning injected by test"}]}
JSON
      elif ! plugin_artifacts_are_complete; then
        cat <<'JSON'
{
  "valid": true,
  "warnings": [
    {
      "path": "plugins.entries.codex",
      "message": "plugin codex is declared but is not installed in this validation context"
    }
  ]
}
JSON
      else
        printf '{"valid":true,"warnings":[]}\n'
      fi
      alias_openclaw_config_path "${ALIAS_BOOTSTRAP_CANDIDATE_CONFIG_AFTER_VALIDATE:-}"
    else
      printf 'validate used unexpected OPENCLAW_CONFIG_PATH=%s\n' \
        "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
      exit 99
    fi
    ;;
  plugins)
    require_candidate_config "$@"
    case "${2:-}" in
      install)
        record_plugin_artifact installed "$@"
        ;;
      update)
        [[ -f "$(plugin_state_dir)/installed" ]] || exit 94
        record_plugin_artifact updated "$@"
        ;;
      enable)
        [[ -f "$(plugin_state_dir)/updated" ]] || exit 95
        record_plugin_artifact enabled "$@"
        ;;
      inspect)
        [[ "${3:-}" == "codex" && "${4:-}" == "--json" ]] || exit 96
        plugin_artifacts_are_complete || {
          printf 'missing plugin artifact in validation context: %s\n' "$(plugin_state_dir)" >&2
          exit 97
        }
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
        ;;
      *)
        exit 98
        ;;
    esac
    ;;
  onboard)
    require_live_config "$@"
    [[ "$*" == "onboard --local" ]] || exit 89
    mkdir -p "$HOME/.openclaw"
    printf '{}\n' > "$HOME/.openclaw/openclaw.json"
    ;;
  daemon)
    require_live_config "$@"
    [[ "$*" == "daemon install --force --port 18789 --json" ]] || exit 91
    unit="$HOME/.config/systemd/user/openclaw-gateway.service"
    mkdir -p "$(dirname "$unit")"
    {
      printf '[Service]\n'
      printf 'Environment=OPENCLAW_STATE_DIR=%s\n' "$OPENCLAW_STATE_DIR"
      printf 'Environment=OPENCLAW_CONFIG_PATH=%s\n' "$OPENCLAW_CONFIG_PATH"
    } > "$unit"
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
check_prerequisites() { :; }
install_python_deps() { :; }
install_ts_deps() { :; }
generate_env() { :; }
install_precommit() { :; }
install_optional_tools() { :; }
check_tailscale() { :; }
run_smoke_tests() { :; }
print_summary() { :; }
make() {
  printf 'make %s\n' "$*" >> "$FLOW_LOG"
}
bash() {
  printf 'push-config %s\n' "$*" >> "$FLOW_LOG"
}
main
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
                "REPO_VALIDATE_EXIT": str(repo_validate_exit),
                "CANDIDATE_VALIDATE_EXIT": str(candidate_validate_exit),
                "EXPECTED_REPO_OPENCLAW_CONFIG_PATH": str(OPENCLAW_CONFIG),
                "EXPECTED_LIVE_OPENCLAW_STATE_DIR": str(home / ".openclaw"),
                "EXPECTED_LIVE_OPENCLAW_CONFIG_PATH": str(home / ".openclaw/openclaw.json"),
                "TEST_ROOT": str(tmp_path),
                "TMPDIR": str(tmp_path),
                **(extra_env or {}),
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
    codex_package = (
        home / "mock-openclaw-project/node_modules/@openclaw/codex/node_modules/@openai/codex"
    )
    codex_bin = codex_package / "bin"
    codex_bin.mkdir(parents=True)
    (codex_bin / "codex.js").write_text(
        """
const fs = require("fs");
const childProcess = require("child_process");
if (process.env.CODEX_DOCTOR_LOG) {
  fs.appendFileSync(
    process.env.CODEX_DOCTOR_LOG,
    `${process.env.CODEX_HOME || "<unset>"} ${process.argv.slice(2).join(" ")}\\n`
  );
}
const codexHome = process.env.CODEX_HOME || "<unset>";
const packageRoot = process.env.MOCK_CODEX_RESOLVED_PATH || "<unset>";
const nativePackageRoot = `${packageRoot}-linux-x64/vendor/x86_64-unknown-linux-musl`;
const npmPackageRoot =
  process.env.MOCK_CODEX_DOCTOR_NPM_PACKAGE_ROOT || `${codexHome}/npm-global/@openai/codex`;
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
    details: {
      "PATH codex #1": "/usr/bin/codex",
      "PATH codex #2": "/bin/codex",
      "PATH codex entries": "2",
      "current executable": `${nativePackageRoot}/bin/codex`,
      "install context":
        `npm (package ${nativePackageRoot}, bin ${nativePackageRoot}/bin, ` +
        `resources ${nativePackageRoot}/codex-resources, path ${nativePackageRoot}/codex-path)`,
      "managed by bun": "false",
      "managed by npm": "true",
      "managed by pnpm": "false",
      "managed package root": packageRoot,
      "npm package root": npmPackageRoot,
      "running package root": packageRoot
    }
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
      "DNS": "2 IPv4, 0 IPv6, first IPv4",
      "auth mode": process.env.MOCK_CODEX_DOCTOR_WEBSOCKET_AUTH_MODE || "none",
      "connect timeout": "15000 ms",
      endpoint: "wss://api.openai.com/v1/<redacted>",
      "handshake transport error": "http 401 Unauthorized: missing bearer",
      "model provider": "openai",
      "provider name": "OpenAI",
      "proxy env vars": "none",
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
    details: {
      "check for update on startup": "true",
      "latest version": "0.146.0",
      "latest version status": "newer version is available",
      "npm package root": npmPackageRoot,
      "running package root": packageRoot,
      "update action": "npm install -g @openai/codex",
      "version cache": [`${codexHome}/version.json`, "missing"]
    }
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
if (process.env.MOCK_CODEX_DOCTOR_ROLLOUT_DB_PARITY_STATE_DB) {
  const stateDb = process.env.MOCK_CODEX_DOCTOR_ROLLOUT_DB_PARITY_STATE_DB;
  const rows = JSON.parse(childProcess.execFileSync(
    "/usr/bin/sqlite3",
    ["-json", stateDb, "SELECT id, rollout_path FROM threads ORDER BY id;"],
    {encoding: "utf8"}
  ) || "[]");
  const staleRows = rows.filter((row) => !fs.existsSync(row.rollout_path));
  if (staleRows.length > 0) {
    checks["state.rollout_db_parity"] = {
      id: "state.rollout_db_parity",
      status: "warning",
      category: "state",
      summary: "thread rows reference missing rollout files",
      details: {"stale thread rows": String(staleRows.length)}
    };
  }
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
  "$EXPECTED_OPENCLAW_CONFIG_PATH"|"$TEST_ROOT"/push-openclaw-config-preflight.*/openclaw.repo-preflight.json|"$EXPECTED_OPENCLAW_STATE_DIR"/.openclaw.generated.*.json)
    ;;
  *)
  printf 'unexpected OPENCLAW_CONFIG_PATH=%s\n' "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
  exit 69
    ;;
esac
is_guarded_repo_config() {
  case "${OPENCLAW_CONFIG_PATH:-}" in
    "$TEST_ROOT"/push-openclaw-config-preflight.*/openclaw.repo-preflight.json)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}
is_generated_config() {
  [[ "${OPENCLAW_CONFIG_PATH:-}" == "$EXPECTED_OPENCLAW_STATE_DIR"/.openclaw.generated.*.json ]]
}
alias_config_path_if_requested() {
  local mode="$1"
  local alias_target
  [[ -n "$mode" ]] || return 0
  alias_target="$TEST_ROOT/push-alias-$(basename "$OPENCLAW_CONFIG_PATH").target"
  cp "$OPENCLAW_CONFIG_PATH" "$alias_target"
  rm -f "$OPENCLAW_CONFIG_PATH"
  case "$mode" in
    symlink)
      ln -s "$alias_target" "$OPENCLAW_CONFIG_PATH"
      ;;
    hardlink)
      ln "$alias_target" "$OPENCLAW_CONFIG_PATH"
      ;;
    *)
      printf 'unknown push alias mode: %s\n' "$mode" >&2
      exit 87
      ;;
  esac
}
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
    if [[ "$OPENCLAW_CONFIG_PATH" == "$EXPECTED_REPO_OPENCLAW_CONFIG_PATH" ]]; then
      printf 'refusing tracked repo overlay for external OpenClaw CLI\n' >&2
      exit 72
    fi
    if is_guarded_repo_config \
      && [[ "${MOCK_REPO_OPENCLAW_CONFIG_PREFIX_INVALID_THEN_VALID:-0}" == "1" ]]; then
      cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "repo.prefix", "message": "prefix invalidity injected by test"}],
  "warnings": []
}
{"valid":true,"warnings":[]}
JSON
      exit 0
    fi
    if is_guarded_repo_config \
      && [[ "${MOCK_REPO_OPENCLAW_CONFIG_PREFIX_WARNING_THEN_VALID:-0}" == "1" ]]; then
      cat <<'JSON'
{
  "valid": true,
  "warnings": [{"path": "repo.prefix.warning", "message": "prefix warning injected by test"}]
}
{"valid":true,"warnings":[]}
JSON
      exit 0
    fi
    if is_guarded_repo_config && [[ "${MOCK_REPO_OPENCLAW_CONFIG_VALIDATE_FAIL:-0}" == "1" ]]; then
      if [[ "${3:-}" == "--json" ]]; then
        cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "repo.injected", "message": "arbitrary repo invalidity"}],
  "warnings": []
}
JSON
      else
        printf 'invalid repo config injected by test\n' >&2
      fi
      exit 12
    fi
    if is_guarded_repo_config && [[ "${MOCK_REPO_OPENCLAW_CONFIG_VALIDATE_WARN:-0}" == "1" ]]; then
      jq -n --arg path "$OPENCLAW_CONFIG_PATH" \
        '{
          "valid": true,
          "path": $path,
          "warnings": [{"path": "repo.warning", "message": "warning injected by test"}]
        }'
      exit 0
    fi
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
    if is_generated_config && [[ "${MOCK_OPENCLAW_CONFIG_VALIDATE_FAIL:-0}" == "1" ]]; then
      if [[ "${3:-}" == "--json" ]]; then
        cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "injected", "message": "test failure"}],
  "warnings": []
}
JSON
      else
        printf 'invalid config injected by test\n' >&2
      fi
      exit 12
    fi
    if is_generated_config \
      && [[ "${MOCK_OPENCLAW_CONFIG_PREFIX_INVALID_THEN_VALID:-0}" == "1" ]]; then
      cat <<'JSON'
{
  "valid": false,
  "errors": [{"path": "generated.prefix", "message": "prefix invalidity injected by test"}],
  "warnings": []
}
{"valid":true,"warnings":[]}
JSON
      exit 0
    fi
    if is_generated_config \
      && [[ "${MOCK_OPENCLAW_CONFIG_PREFIX_WARNING_THEN_VALID:-0}" == "1" ]]; then
      cat <<'JSON'
{
  "valid": true,
  "warnings": [
    {"path": "generated.prefix.warning", "message": "prefix warning injected by test"}
  ]
}
{"valid":true,"warnings":[]}
JSON
      exit 0
    fi
    if is_generated_config && [[ "${MOCK_OPENCLAW_CONFIG_VALIDATE_WARN:-0}" == "1" ]]; then
      jq -n --arg path "$OPENCLAW_CONFIG_PATH" \
        '{
          "valid": true,
          "path": $path,
          "warnings": [{"path": "generated.warning", "message": "warning injected by test"}]
        }'
      exit 0
    fi
    if [[ "$OPENCLAW_CONFIG_PATH" == "$EXPECTED_OPENCLAW_CONFIG_PATH" \
      && "${MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN:-0}" == "1" ]]; then
      jq -n --arg path "$OPENCLAW_CONFIG_PATH" \
        '{
          "valid": true,
          "path": $path,
          "warnings": [{"path": "live.warning", "message": "warning injected by test"}]
        }'
      exit 0
    fi
    if [[ "${3:-}" == "--json" ]]; then
      printf '{"valid":true,"path":"%s","warnings":[]}\n' "$OPENCLAW_CONFIG_PATH"
    else
      printf 'config ok\n'
    fi
    if is_guarded_repo_config && [[ "${MOCK_MUTATE_REPO_PREFLIGHT_CONFIG:-0}" == "1" ]]; then
      printf '\n{"mutated":"repo-preflight"}\n' >> "$OPENCLAW_CONFIG_PATH"
    fi
    if is_guarded_repo_config; then
      alias_config_path_if_requested "${MOCK_ALIAS_REPO_PREFLIGHT_CONFIG_AFTER_VALIDATE:-}"
    fi
    if is_generated_config && [[ "${MOCK_MUTATE_GENERATED_CONFIG:-0}" == "1" ]]; then
      printf '\n{"mutated":"generated"}\n' >> "$OPENCLAW_CONFIG_PATH"
    fi
    if is_generated_config; then
      alias_config_path_if_requested "${MOCK_ALIAS_GENERATED_CONFIG_AFTER_VALIDATE:-}"
    fi
    if [[ "$OPENCLAW_CONFIG_PATH" == "$EXPECTED_OPENCLAW_CONFIG_PATH" \
      && "${MOCK_MUTATE_LIVE_CONFIG_DURING_VALIDATE:-0}" == "1" ]]; then
      printf '\n{"mutated":"live"}\n' >> "$OPENCLAW_CONFIG_PATH"
    fi
    if [[ "$OPENCLAW_CONFIG_PATH" == "$EXPECTED_OPENCLAW_CONFIG_PATH" ]]; then
      alias_config_path_if_requested "${MOCK_ALIAS_LIVE_CONFIG_AFTER_VALIDATE:-}"
    fi
    ;;
  plugins)
    [[ "${2:-}" == "inspect" && "${3:-}" == "codex" && "${4:-}" == "--json" ]] || exit 45
    if [[ "$OPENCLAW_CONFIG_PATH" == "$EXPECTED_REPO_OPENCLAW_CONFIG_PATH" ]]; then
      printf 'plugin inspect used tracked repo overlay\n' >&2
      exit 72
    fi
    if [[ "${MOCK_REQUIRE_PLUGIN_INSPECT_REPO_CONFIG:-0}" == "1" ]] \
      && ! is_guarded_repo_config; then
      printf 'plugin inspect used non-repo config: %s\n' "$OPENCLAW_CONFIG_PATH" >&2
      exit 70
    fi
    if jq -e '.plugins.entries.codex.config.nativeToolSurfaceEnabled? != null' \
      "$OPENCLAW_CONFIG_PATH" >/dev/null; then
      printf 'plugin inspect rejected stale nativeToolSurfaceEnabled config\n' >&2
      exit 71
    fi
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
    if is_guarded_repo_config && [[ "${MOCK_MUTATE_REPO_PREFLIGHT_CONFIG:-0}" == "1" ]]; then
      printf '\n{"mutated":"repo-preflight-inspect"}\n' >> "$OPENCLAW_CONFIG_PATH"
    fi
    if is_guarded_repo_config; then
      alias_config_path_if_requested "${MOCK_ALIAS_REPO_PREFLIGHT_CONFIG_AFTER_INSPECT:-}"
    fi
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
systemd_c_quote_value() {{
  local value="$1"
  local output="" char
  local index
  for ((index = 0; index < ${{#value}}; index++)); do
    char="${{value:index:1}}"
    case "$char" in
      $'\n')
        output="${{output}}\\n"
        ;;
      $'\t')
        output="${{output}}\\t"
        ;;
      $'\r')
        output="${{output}}\\r"
        ;;
      $'\b')
        output="${{output}}\\b"
        ;;
      $'\f')
        output="${{output}}\\f"
        ;;
      $'\v')
        output="${{output}}\\v"
        ;;
      $'\a')
        output="${{output}}\\a"
        ;;
      $'\e')
        output="${{output}}\\e"
        ;;
      "\\")
        output="${{output}}\\\\"
        ;;
      "'")
        output="${{output}}\\'"
        ;;
      *)
        output="${{output}}${{char}}"
        ;;
    esac
  done
  printf "%s" "$output"
}}
manager_node_options_state_dir="${{SYSTEMCTL_STATE_DIR:-$(dirname "$SYSTEMCTL_LOG")}}"
manager_node_options_unset_marker="$manager_node_options_state_dir/node-options.unset"
manager_node_options_should_emit() {{
  if [[ "${{SYSTEMD_MANAGER_NODE_OPTIONS_PERSIST_AFTER_UNSET:-0}}" == "1" ]]; then
    return 0
  fi
  [[ ! -f "$manager_node_options_unset_marker" ]]
}}
printf 'systemctl %s\n' "$*" >> "$SYSTEMCTL_LOG"
case "$*" in
  "--user show-environment")
    if [[ -n "${{SYSTEMD_MANAGER_EXTRA_ENV:-}}" ]]; then
      printf '%s\n' "$SYSTEMD_MANAGER_EXTRA_ENV"
    fi
    if [[ -n "${{SYSTEMD_MANAGER_NODE_OPTIONS_RAW:-}}" ]] && manager_node_options_should_emit; then
      printf '%s\n' "$SYSTEMD_MANAGER_NODE_OPTIONS_RAW"
      exit 0
    fi
    if [[ -n "${{SYSTEMD_MANAGER_NODE_OPTIONS:-}}" ]] && manager_node_options_should_emit; then
      printf "NODE_OPTIONS=$'%s'\n" "$(systemd_c_quote_value "$SYSTEMD_MANAGER_NODE_OPTIONS")"
    fi
    exit 0
    ;;
  "--user unset-environment NODE_OPTIONS")
    printf 'unset NODE_OPTIONS\n' >> "$SYSTEMCTL_LOG"
    if [[ "${{FAIL_MANAGER_NODE_OPTIONS_UNSET_AFTER_MUTATION:-0}}" == "1" ]]; then
      printf 'unset NODE_OPTIONS failed after mutation by test\n' >&2
      exit 25
    fi
    mkdir -p "$(dirname "$manager_node_options_unset_marker")"
    : > "$manager_node_options_unset_marker"
    exit 0
    ;;
  --user\ set-environment\ NODE_OPTIONS=*)
    printf 'restore NODE_OPTIONS\n' >> "$SYSTEMCTL_LOG"
    if [[ "${{FAIL_MANAGER_NODE_OPTIONS_RESTORE:-0}}" == "1" ]]; then
      printf 'restore NODE_OPTIONS failed by test\n' >&2
      exit 24
    fi
    printf '%s' "${{3#NODE_OPTIONS=}}" > "${{SYSTEMD_MANAGER_RESTORE_VALUE_FILE:-/dev/null}}"
    rm -f "$manager_node_options_unset_marker"
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
src="${@: -2:1}"
dest="${@: -1}"
case "$dest" in
  "$TEST_ROOT"/*)
    ;;
  *)
    printf 'cp destination escaped tmp_path: %s\n' "$dest" >&2
    exit 88
    ;;
esac
if [[ -n "${FAIL_RESTORE_DEST_BASENAME:-}" \
  && "$(basename "$dest")" == "$FAIL_RESTORE_DEST_BASENAME" \
  && "$src" == "$TEST_ROOT"*/.push-openclaw-config-*/* ]]; then
  printf 'restore cp failed by test for %s\n' "$dest" >&2
  exit 78
fi
if [[ -n "${FAIL_STAGE_RESTORE_IF_BACKUP_CONTAINS_BASENAME:-}" \
  && "$(basename "$dest")" == restore.* \
  && "$src" == "$TEST_ROOT"*/.push-openclaw-config-*/* \
  && -e "$src/$FAIL_STAGE_RESTORE_IF_BACKUP_CONTAINS_BASENAME" ]]; then
  printf 'staged restore cp failed by test for %s\n' "$dest" >&2
  exit 76
fi
if [[ -n "${FAIL_BACKUP_SOURCE_BASENAME:-}" \
  && "$(basename "$src")" == "$FAIL_BACKUP_SOURCE_BASENAME" \
  && "$dest" == "$TEST_ROOT"*/.push-openclaw-config-*/* ]]; then
  printf 'backup cp failed by test for %s\n' "$src" >&2
  exit 77
fi
/usr/bin/cp "$@"
        """.strip(),
    )
    _write_executable(
        mock_bin / "mv",
        r"""
printf 'mv %s\n' "$*" >> "${MV_LOG:-/dev/null}"
src="${@: -2:1}"
dest="${@: -1}"
if [[ -n "${FAIL_RESTORE_COMMIT_DEST_BASENAME:-}" \
  && "$(basename "$dest")" == "$FAIL_RESTORE_COMMIT_DEST_BASENAME" \
  && "$(basename "$src")" == restore.* ]]; then
  printf 'restore mv failed by test for %s\n' "$dest" >&2
  exit 75
fi
/usr/bin/mv "$@"
        """.strip(),
    )
    _write_executable(
        mock_bin / "rm",
        r"""
printf 'rm %s\n' "$*" >> "${RM_LOG:-/dev/null}"
target="${@: -1}"
if [[ "${FAIL_BACKUP_CLEANUP:-0}" == "1" \
  && "$target" == "$TEST_ROOT"*/.push-openclaw-config-* ]]; then
  printf 'backup cleanup failed by test for %s\n' "$target" >&2
  exit 79
fi
/usr/bin/rm "$@"
        """.strip(),
    )
    _write_executable(
        mock_bin / "find",
        r"""
printf 'find %s\n' "$*" >> "${FIND_LOG:-/dev/null}"
root="${1:-}"
if [[ -n "${FAIL_FIND_ROOT_BASENAME:-}" \
  && "$(basename "$root")" == "$FAIL_FIND_ROOT_BASENAME" ]]; then
  printf 'find failed by test for %s\n' "$root" >&2
  exit 74
fi
/usr/bin/find "$@"
""".strip(),
    )
    _write_executable(
        mock_bin / "sqlite3",
        r"""
printf 'sqlite3 %s\n' "$*" >> "${SQLITE_LOG:-/dev/null}"
rebuild_sql="DROP INDEX idx_logs_thread_id; CREATE INDEX idx_logs_thread_id ON logs(thread_id); "
rebuild_sql+="PRAGMA integrity_check;"
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
      if [[ "${MOCK_CODEX_LOG_DB_REINDEX_REPAIR:-fail}" == "fail" ]]; then
        printf 'row 4135 missing from index idx_logs_thread_id\n'
      else
        printf 'ok\n'
      fi
      ;;
    "$rebuild_sql")
      if [[ "${MOCK_CODEX_LOG_DB_REBUILD:-ok}" == "fail" ]]; then
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
            "EXPECTED_REPO_OPENCLAW_CONFIG_PATH": str(OPENCLAW_CONFIG),
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
            "MV_LOG": str(home / "mv.log"),
            "RM_LOG": str(home / "rm.log"),
            "FIND_LOG": str(home / "find.log"),
            "OPENCLAW_LOG": str(home / "openclaw.log"),
            "MOCK_CODEX_RESOLVED_PATH": str(
                home
                / "mock-openclaw-project/node_modules/@openclaw/codex/node_modules/@openai/codex"
            ),
            "CODEX_DOCTOR_LOG": str(home / "codex-doctor.log"),
            "SQLITE_LOG": str(home / "sqlite.log"),
            "TEST_ROOT": str(tmp_path),
            "TMPDIR": str(tmp_path),
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


def _create_codex_state_db(path: Path, schema: str = CODEX_STATE_DB_SCHEMA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)


def _insert_codex_thread(
    connection: sqlite3.Connection,
    thread_id: str,
    rollout_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO threads (
            id,
            rollout_path,
            created_at,
            updated_at,
            source,
            model_provider,
            cwd,
            title,
            sandbox_policy,
            approval_mode
        ) VALUES (?, ?, 1, 1, 'cli', 'openai', '/tmp', 'test', 'workspace-write', 'never')
        """,
        (thread_id, str(rollout_path)),
    )


def _codex_thread_ids(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [
            row[0] for row in connection.execute("SELECT id FROM threads ORDER BY id;").fetchall()
        ]


def _insert_codex_agent_job_item(
    connection: sqlite3.Connection,
    assigned_thread_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO agent_jobs (
            id,
            name,
            status,
            instruction,
            input_headers_json,
            input_csv_path,
            output_csv_path,
            created_at,
            updated_at
        ) VALUES ('job-1', 'job', 'running', 'do work', '[]', '/tmp/in.csv', '/tmp/out.csv', 1, 1)
        """
    )
    connection.execute(
        """
        INSERT INTO agent_job_items (
            job_id,
            item_id,
            row_index,
            row_json,
            status,
            assigned_thread_id,
            created_at,
            updated_at
        ) VALUES ('job-1', 'item-1', 0, '{}', 'running', ?, 1, 1)
        """,
        (assigned_thread_id,),
    )


def _write_sqlite_commit_failure_sitecustomize(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "sitecustomize.py").write_text(
        """
import os
import sqlite3

_original_connect = sqlite3.connect


def _matches_target(database):
    target = os.environ.get("MOCK_CODEX_STATE_DB_COMMIT_FAIL_PATH")
    if not target:
        return False
    try:
        database_path = os.fspath(database)
    except TypeError:
        return False
    return os.path.abspath(database_path) == os.path.abspath(target)


class _CommitFailConnection:
    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        raise sqlite3.OperationalError("injected state DB commit failure")

    def rollback(self):
        rollback_log = os.environ.get("MOCK_CODEX_STATE_DB_ROLLBACK_LOG")
        if rollback_log:
            with open(rollback_log, "a", encoding="utf-8") as handle:
                handle.write("rollback\\n")
        return self._connection.rollback()


def _connect(database, *args, **kwargs):
    connection = _original_connect(database, *args, **kwargs)
    if _matches_target(database):
        return _CommitFailConnection(connection)
    return connection


sqlite3.connect = _connect
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _read_sqlite_log(env: dict[str, str]) -> str:
    sqlite_log = Path(env["SQLITE_LOG"])
    if not sqlite_log.exists():
        return ""
    return sqlite_log.read_text(encoding="utf-8")


def _read_cp_log(env: dict[str, str]) -> str:
    cp_log = Path(env["CP_LOG"])
    if not cp_log.exists():
        return ""
    return cp_log.read_text(encoding="utf-8")


def _assert_alias_mode_reported(stderr: str, alias_mode: str) -> None:
    if alias_mode == "symlink":
        assert "symlink" in stderr
    else:
        assert alias_mode == "hardlink"
        assert "hard-linked" in stderr


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
    cp_log = _read_cp_log(env)
    assert "openclaw.json.bak" not in cp_log
    assert "mempalace-readonly-server.py" not in cp_log
    assert "/gateway/agent_config/" not in cp_log


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
    inspect = script.index(
        "if ! require_codex_plugin_exact run_openclaw_cli_for_candidate_config; then", update
    )
    daemon_install = script.index('daemon install --force --port "${OPENCLAW_GATEWAY_PORT}" --json')
    push = script.index('local push_script="$REPO_ROOT/scripts/push-openclaw-config.sh"')

    assert install < update < inspect < daemon_install < push
    assert "daemon restart" not in script
    assert "daemon start" not in script


def test_mocked_bootstrap_openclaw_flow_runs_upgrade_steps_in_order(tmp_path: Path) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(tmp_path)

    assert result.returncode == 0, result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
        "openclaw config validate --json",
        "openclaw plugins inspect codex --json",
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
        "openclaw plugins inspect codex --json",
        "openclaw daemon install --force --port 18789 --json",
        f"make -C {REPO_ROOT} mempalace-install",
        f"mempalace {REPO_ROOT}/scripts/check-mempalace-health.py",
        f"push-config {PUSH_SCRIPT}",
    ]
    context_rows = [
        line.split("\t")
        for line in (tmp_path / "bootstrap-flow.log.contexts")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    validate_contexts = [row for row in context_rows if row[0] == "config validate --json"]
    repo_validate_contexts = [
        row for row in validate_contexts if row[2].endswith("/openclaw.repo-preflight.json")
    ]
    candidate_validate_contexts = [
        row for row in validate_contexts if row[2].endswith("/openclaw.candidate.json")
    ]
    assert len(repo_validate_contexts) == 1
    assert len(candidate_validate_contexts) == 2
    assert repo_validate_contexts[0][2] != candidate_validate_contexts[0][2]
    tracked_config_path = str(OPENCLAW_CONFIG)
    assert all(tracked_config_path != row[2] for row in context_rows)
    assert repo_validate_contexts[0][1].endswith("/state")
    assert {row[1] for row in candidate_validate_contexts} == {
        repo_validate_contexts[0][1],
        str(tmp_path / "home/.openclaw"),
    }
    live_plugin_state = tmp_path / "home/.openclaw/plugins/codex"
    assert (live_plugin_state / "installed").is_file()
    assert (live_plugin_state / "updated").is_file()
    assert (live_plugin_state / "enabled").is_file()
    unit = tmp_path / "home/.config/systemd/user/openclaw-gateway.service"
    unit_text = unit.read_text(encoding="utf-8")
    assert f"Environment=OPENCLAW_STATE_DIR={tmp_path / 'home/.openclaw'}" in unit_text
    assert (
        f"Environment=OPENCLAW_CONFIG_PATH={tmp_path / 'home/.openclaw/openclaw.json'}" in unit_text
    )
    assert "g2-openclaw-bootstrap" not in unit_text
    assert not list(tmp_path.glob("g2-openclaw-bootstrap.*"))


def test_bootstrap_invalid_repo_overlay_aborts_before_onboarding_or_live_writes(
    tmp_path: Path,
) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        create_live_config=False,
        repo_validate_exit=12,
    )

    assert result.returncode == 1
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
    ]
    expected_error = "Repo OpenClaw config failed schema validation before plugin/runtime preflight"
    assert expected_error in result.stdout
    assert "invalid repo overlay" in result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.openclaw/openclaw.json").exists()
    assert not list(tmp_path.glob("g2-openclaw-bootstrap.*"))


def test_bootstrap_rejects_repo_schema_warnings_before_live_writes(tmp_path: Path) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        create_live_config=False,
        extra_env={"REPO_VALIDATE_WARNINGS": "1"},
    )

    assert result.returncode == 1
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
    ]
    expected_error = "Repo OpenClaw config failed schema validation before plugin/runtime preflight"
    assert expected_error in result.stdout
    assert "repo.warning" in result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.openclaw/openclaw.json").exists()


def test_bootstrap_invalid_candidate_overlay_aborts_before_onboarding_or_live_writes(
    tmp_path: Path,
) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        create_live_config=False,
        candidate_validate_exit=12,
    )

    assert result.returncode == 1
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
        "openclaw config validate --json",
    ]
    assert "Candidate OpenClaw config failed schema validation" in result.stdout
    assert "invalid candidate overlay" in result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.openclaw/openclaw.json").exists()


def test_bootstrap_rejects_candidate_schema_warnings_before_inspect_or_daemon(
    tmp_path: Path,
) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        extra_env={"CANDIDATE_VALIDATE_WARNINGS": "1"},
    )

    assert result.returncode == 1
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
        "openclaw config validate --json",
    ]
    assert "Candidate OpenClaw config failed schema validation" in result.stdout
    assert "candidate.warning" in result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.config/systemd/user/openclaw-gateway.service").exists()
    assert not any(
        command.startswith("push-config ")
        for command in flow_log.read_text(encoding="utf-8").splitlines()
    )


@pytest.mark.parametrize("alias_mode", ["symlink", "hardlink"])
def test_bootstrap_rejects_repo_preflight_topology_replacement_with_identical_bytes(
    tmp_path: Path,
    alias_mode: str,
) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        create_live_config=False,
        extra_env={"ALIAS_BOOTSTRAP_REPO_CONFIG_AFTER_VALIDATE": alias_mode},
    )

    assert result.returncode == 1
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
    ]
    assert "Repo OpenClaw config failed schema validation" in result.stdout
    assert "Guarded file" in result.stderr
    _assert_alias_mode_reported(result.stderr, alias_mode)
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.openclaw/openclaw.json").exists()


@pytest.mark.parametrize("alias_mode", ["symlink", "hardlink"])
def test_bootstrap_rejects_candidate_preflight_topology_replacement_with_identical_bytes(
    tmp_path: Path,
    alias_mode: str,
) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        create_live_config=False,
        extra_env={"ALIAS_BOOTSTRAP_CANDIDATE_CONFIG_AFTER_VALIDATE": alias_mode},
    )

    assert result.returncode == 1
    assert flow_log.read_text(encoding="utf-8").splitlines() == [
        "openclaw plugins install @openclaw/codex@2026.7.1-1 --force --pin",
        "openclaw plugins update codex",
        "openclaw plugins enable codex",
        "openclaw config validate --json",
        "openclaw config validate --json",
    ]
    assert "Candidate OpenClaw config failed schema validation" in result.stdout
    assert "Guarded file" in result.stderr
    _assert_alias_mode_reported(result.stderr, alias_mode)
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.openclaw/openclaw.json").exists()


@pytest.mark.parametrize(
    ("extra_env", "expected_error", "expected_detail"),
    [
        (
            {"REPO_VALIDATE_PREFIX_INVALID_THEN_VALID": "1"},
            "Repo OpenClaw config failed schema validation",
            "repo.prefix",
        ),
        (
            {"CANDIDATE_VALIDATE_PREFIX_WARNING_THEN_VALID": "1"},
            "Candidate OpenClaw config failed schema validation",
            "candidate.prefix.warning",
        ),
    ],
)
def test_bootstrap_rejects_multi_document_schema_validation_output(
    tmp_path: Path,
    extra_env: dict[str, str],
    expected_error: str,
    expected_detail: str,
) -> None:
    repo_config_before = OPENCLAW_CONFIG.read_bytes()

    result, _flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        create_live_config=False,
        extra_env=extra_env,
    )

    assert result.returncode == 1
    assert expected_error in result.stdout
    assert expected_detail in result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == repo_config_before
    assert not (tmp_path / "home/.openclaw/openclaw.json").exists()


def test_bootstrap_openclaw_prewrite_invocations_clear_leaked_env_and_use_candidate_config(
    tmp_path: Path,
) -> None:
    result, _flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        extra_env={
            "OPENCLAW_HOME": str(tmp_path / "leaked-openclaw-home"),
            "OPENCLAW_PUSH_HOME": str(tmp_path / "leaked-push-home"),
            "OPENCLAW_STATE_DIR": str(tmp_path / "leaked-state"),
            "OPENCLAW_CONFIG_PATH": str(tmp_path / "leaked-config.json"),
            "NODE_OPTIONS": "--require /tmp/stale-runtime-selector.cjs",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "leaked" not in result.stderr
    assert "lifecycle command used tracked repo overlay" not in result.stderr
    assert "non-live OPENCLAW_STATE_DIR" not in result.stderr


def test_bootstrap_version_check_uses_isolated_candidate_context(tmp_path: Path) -> None:
    openclaw = tmp_path / "bin/openclaw"
    _write_executable(
        openclaw,
        r"""
if [[ -v OPENCLAW_HOME || -v OPENCLAW_PUSH_HOME ]]; then
  printf 'leaked openclaw home env\n' >&2
  exit 60
fi
if [[ "${OPENCLAW_STATE_DIR:-}" != "$TEST_ROOT"/g2-openclaw-bootstrap.*/state ]]; then
  printf 'unexpected OPENCLAW_STATE_DIR=%s\n' "${OPENCLAW_STATE_DIR:-<unset>}" >&2
  exit 61
fi
case "${OPENCLAW_CONFIG_PATH:-}" in
  "$TEST_ROOT"/g2-openclaw-bootstrap.*/openclaw.candidate.json)
    ;;
  *)
    printf 'unexpected OPENCLAW_CONFIG_PATH=%s\n' "${OPENCLAW_CONFIG_PATH:-<unset>}" >&2
    exit 62
    ;;
esac
if [[ -n "${NODE_OPTIONS:-}" ]]; then
  printf 'leaked NODE_OPTIONS=%s\n' "$NODE_OPTIONS" >&2
  exit 63
fi
printf 'openclaw 2026.7.1-2\n'
""".strip(),
    )

    result = _run_bootstrap_guard(
        tmp_path,
        {
            "OPENCLAW_BIN": str(openclaw),
            "OPENCLAW_HOME": str(tmp_path / "leaked-openclaw-home"),
            "OPENCLAW_PUSH_HOME": str(tmp_path / "leaked-push-home"),
            "OPENCLAW_STATE_DIR": str(tmp_path / "leaked-state"),
            "OPENCLAW_CONFIG_PATH": str(tmp_path / "leaked-config.json"),
            "NODE_OPTIONS": "--require /tmp/stale-runtime-selector.cjs",
            "TEST_ROOT": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"RESOLVED={openclaw}" in result.stdout


def test_bootstrap_rejects_device_identity_symlink_before_chmod(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state_dir = home / ".openclaw/state"
    state_dir.mkdir(parents=True)
    external_target = tmp_path / "external-device-identity.json"
    external_target.write_text('{"device":"external"}\n', encoding="utf-8")
    external_target.chmod(0o644)
    (state_dir / "device-identity.json").symlink_to(external_target)

    result = _run_bootstrap_tailscale_check(home)

    assert result.returncode == 1
    assert "device identity file permissions" in result.stderr
    assert "symlink" in result.stderr
    assert external_target.read_text(encoding="utf-8") == '{"device":"external"}\n'
    assert _mode(external_target) == 0o644


def test_bootstrap_rejects_hardlinked_device_identity_before_chmod(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state_dir = home / ".openclaw/state"
    state_dir.mkdir(parents=True)
    external_alias = tmp_path / "external-device-identity.json"
    external_alias.write_text('{"device":"external"}\n', encoding="utf-8")
    external_alias.chmod(0o644)
    os.link(external_alias, state_dir / "device-identity.json")

    result = _run_bootstrap_tailscale_check(home)

    assert result.returncode == 1
    assert "device identity file permissions" in result.stderr
    assert "hard-linked" in result.stderr
    assert external_alias.read_text(encoding="utf-8") == '{"device":"external"}\n'
    assert _mode(external_alias) == 0o644


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
    cp_log = _read_cp_log(env)
    assert "openclaw.json.bak" not in cp_log
    assert "mempalace-readonly-server.py" not in cp_log


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
        'guarded_cp_file "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}" '
        '"${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" '
        '"staging managed runtime caps drop-in ${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"'
    ) in script
    assert (
        'guarded_mv_replace "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" '
        '"${GATEWAY_RUNTIME_CAPS_DROPIN_DST}" '
        '"publishing managed runtime caps drop-in ${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"'
    ) in script
    assert "require_gateway_service_loadable" in script
    assert "prepare_runtime_caps_dropin_dir" in script
    assert ('chmod 0755 "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"') in script
    assert ('validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"') in script
    assert script.count("systemctl --user daemon-reload") == 4
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
    backup_write = script.index(
        'guarded_copy_path_topology_preserving_final_symlink_topology "${LOCAL_CONFIG}" "${BACKUP}"'
    )
    supervisor_unit_write = script.index(
        'guarded_mv_replace "${SUPERVISOR_UNIT_TMP}" "${SUPERVISOR_UNIT_DST}"'
    )
    runtime_caps_dir_prepare = script.rindex("\nprepare_runtime_caps_dropin_dir\n")
    assert preflight_loadable_check < backup_write
    assert preflight_loadable_check < supervisor_unit_write
    assert preflight_loadable_check < runtime_caps_dir_prepare


def test_push_script_final_commit_boundary_disarms_only_after_all_finalizers() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    boundary_start = script.index("commit_deployment_boundary() {")
    boundary_end = script.index("\n}\n\nrestore_local_config_backup", boundary_start)
    boundary = script[boundary_start:boundary_end]
    end_sequence = script[script.rindex("if ! systemctl --user daemon-reload; then") :]
    pre_commit_call = end_sequence[: end_sequence.index("commit_deployment_boundary")]
    mark_start = script.index("mark_deployment_committed() {")
    mark_end = script.index("\n}\n\ncleanup_committed_recovery_paths", mark_start)
    mark = script[mark_start:mark_end]

    assert "trap '' HUP INT TERM" in boundary
    assert boundary.index("trap '' HUP INT TERM") < boundary.index("finalize_local_config_backup")
    assert boundary.index("finalize_local_config_backup") < boundary.index(
        "finalize_managed_unit_transaction"
    )
    assert boundary.index("finalize_managed_unit_transaction") < boundary.index(
        "finalize_managed_artifact_transaction"
    )
    assert boundary.index("finalize_managed_artifact_transaction") < boundary.index(
        "finalize_systemd_manager_environment_snapshot"
    )
    assert boundary.index("finalize_systemd_manager_environment_snapshot") < boundary.index(
        "mark_deployment_committed"
    )
    assert boundary.index("mark_deployment_committed") < boundary.index("trap - EXIT")
    assert boundary.index("trap - EXIT") < boundary.index("cleanup_committed_recovery_paths")
    assert "ROLLBACK_ARMED=0" not in pre_commit_call
    assert "MANAGED_UNIT_TRANSACTION_ARMED=0" not in pre_commit_call
    assert "MANAGED_ARTIFACT_TRANSACTION_ARMED=0" not in pre_commit_call
    assert "ROLLBACK_ARMED=0" in mark
    assert "MANAGED_UNIT_TRANSACTION_ARMED=0" in mark
    assert "MANAGED_ARTIFACT_TRANSACTION_ARMED=0" in mark
    assert "|| true" not in script


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
    assert openclaw_log.count("OPENCLAW_HOME=<unset>") == 5
    assert openclaw_log.count("OPENCLAW_PUSH_HOME=<unset>") == 5
    assert openclaw_log.count(f"OPENCLAW_STATE_DIR={env['EXPECTED_OPENCLAW_STATE_DIR']}") == 5
    assert openclaw_log.count(f"OPENCLAW_CONFIG_PATH={env['EXPECTED_OPENCLAW_CONFIG_PATH']}") == 1
    assert "OPENCLAW_CONFIG_PATH=" + env["EXPECTED_REPO_OPENCLAW_CONFIG_PATH"] not in openclaw_log
    assert openclaw_log.count("push-openclaw-config-preflight.") == 3
    assert "config validate --json" in openclaw_log
    assert openclaw_log.count("NODE_OPTIONS=<unset>") == 5
    assert "inherited-openclaw-home" not in openclaw_log
    assert "inherited-state-dir" not in openclaw_log
    assert "inherited-config.json" not in openclaw_log
    assert "env-file-push-home" not in openclaw_log
    assert "env-file-state-dir" not in openclaw_log
    assert "env-file-config.json" not in openclaw_log


def test_push_script_ignores_signal_during_final_commit_boundary(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    checkpoint_log = tmp_path / "checkpoints.log"
    env["OPENCLAW_PUSH_TEST_CHECKPOINT_LOG"] = str(checkpoint_log)
    env["OPENCLAW_PUSH_TEST_SIGNAL_AT"] = "commit-boundary-entered"
    env["OPENCLAW_PUSH_TEST_SIGNAL"] = "INT"

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    assert checkpoint_log.read_text(encoding="utf-8").splitlines() == [
        "before-config-publication",
        "commit-boundary-entered",
    ]
    assert "Done. Config pushed successfully." in result.stdout


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


def test_push_script_rejects_zero_codex_doctor_exit_when_codex_auth_is_ok(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_CODEX_DOCTOR_EXIT_STATUS"] = "0"
    env["MOCK_CODEX_DOCTOR_AUTH_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_INSTALL_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_UPDATE_STATUS"] = "ok"
    env["MOCK_CODEX_DOCTOR_WEBSOCKET_STATUS"] = "ok"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "unexpected fatal Codex doctor checks" in result.stderr
    assert "auth.credentials=ok" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


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
    assert "unexpected fatal Codex doctor checks" in result.stderr
    assert "auth.credentials=ok" in result.stderr
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
    assert not list(Path(env["OPENCLAW_PUSH_HOME"]).glob(".openclaw.generated.*.json"))


@pytest.mark.parametrize("alias_mode", ["symlink", "hardlink"])
def test_push_script_rejects_repo_preflight_topology_replacement_with_identical_bytes(
    tmp_path: Path,
    alias_mode: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    env["MOCK_ALIAS_REPO_PREFLIGHT_CONFIG_AFTER_VALIDATE"] = alias_mode

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Repo OpenClaw config failed schema validation before runtime preflight" in (
        result.stderr
    )
    assert "Guarded file" in result.stderr
    _assert_alias_mode_reported(result.stderr, alias_mode)
    assert openclaw_config.read_bytes() == initial_config
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize("alias_mode", ["symlink", "hardlink"])
def test_push_script_rejects_generated_config_topology_replacement_with_identical_bytes(
    tmp_path: Path,
    alias_mode: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    env["MOCK_ALIAS_GENERATED_CONFIG_AFTER_VALIDATE"] = alias_mode

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "changed generated config identity/topology during validation" in result.stderr
    _assert_alias_mode_reported(result.stderr, alias_mode)
    assert openclaw_config.read_bytes() == initial_config
    assert "Atomically published validated repo config" not in result.stdout
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_generated_openclaw_config_warnings_before_write(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    env["MOCK_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Generated OpenClaw config failed schema validation before write" in result.stderr
    assert "generated.warning" in result.stderr
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert "Done. Config pushed successfully." not in result.stdout
    assert not list(Path(env["OPENCLAW_PUSH_HOME"]).glob(".openclaw.generated.*.json"))


@pytest.mark.parametrize(
    ("env_name", "expected_detail"),
    [
        ("MOCK_OPENCLAW_CONFIG_PREFIX_INVALID_THEN_VALID", "generated.prefix"),
        ("MOCK_OPENCLAW_CONFIG_PREFIX_WARNING_THEN_VALID", "generated.prefix.warning"),
    ],
)
def test_push_script_rejects_multi_document_generated_schema_validation(
    tmp_path: Path,
    env_name: str,
    expected_detail: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    env[env_name] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Generated OpenClaw config failed schema validation before write" in result.stderr
    assert expected_detail in result.stderr
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert "Done. Config pushed successfully." not in result.stdout
    assert not list(Path(env["OPENCLAW_PUSH_HOME"]).glob(".openclaw.generated.*.json"))


def test_push_script_cleans_generated_config_without_leaking_openrouter_secret(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    fixture_value = "fixture-key-1234567890abcdef"
    env["OPENCLAW_PROVIDER"] = "openrouter"
    env["OPENROUTER_API_KEY"] = fixture_value
    env["MOCK_OPENCLAW_CONFIG_VALIDATE_FAIL"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Generated OpenClaw config failed schema validation before write" in result.stderr
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert not list(openclaw_home.glob(".openclaw.generated.*.json"))
    assert fixture_value not in result.stdout
    assert fixture_value not in result.stderr
    assert fixture_value not in Path(env["OPENCLAW_LOG"]).read_text(encoding="utf-8")


def test_push_script_uses_repo_config_for_runtime_preflight_and_replaces_stale_live_config(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    openclaw_config.write_text(
        json.dumps(
            {
                "plugins": {
                    "entries": {
                        "codex": {
                            "config": {
                                "nativeToolSurfaceEnabled": True,
                            },
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    env["MOCK_REQUIRE_PLUGIN_INSPECT_REPO_CONFIG"] = "1"

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    deployed = json.loads(openclaw_config.read_text(encoding="utf-8"))
    assert "nativeToolSurfaceEnabled" not in deployed["plugins"]["entries"]["codex"]["config"]
    assert {agent["id"] for agent in deployed["agents"]["list"]} >= {"main", "autoresearch-pm"}
    openclaw_log = Path(env["OPENCLAW_LOG"]).read_text(encoding="utf-8")
    assert (
        "openclaw plugins inspect codex --json "
        f"OPENCLAW_HOME=<unset> OPENCLAW_PUSH_HOME=<unset> "
        f"OPENCLAW_STATE_DIR={env['EXPECTED_OPENCLAW_STATE_DIR']} "
        "OPENCLAW_CONFIG_PATH="
    ) in openclaw_log
    assert "push-openclaw-config-preflight." in openclaw_log
    assert env["EXPECTED_REPO_OPENCLAW_CONFIG_PATH"] not in openclaw_log


def test_push_script_rejects_arbitrary_repo_config_invalidity_before_live_write(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    env["MOCK_REPO_OPENCLAW_CONFIG_VALIDATE_FAIL"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Repo OpenClaw config failed schema validation before runtime preflight" in result.stderr
    assert "arbitrary repo invalidity" in result.stderr
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_repo_config_warnings_before_live_write(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    env["MOCK_REPO_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Repo OpenClaw config failed schema validation before runtime preflight" in result.stderr
    assert "repo.warning" in result.stderr
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert "Done. Config pushed successfully." not in result.stdout


def test_bootstrap_rejects_external_mutation_of_guarded_repo_config_copy(
    tmp_path: Path,
) -> None:
    tracked_before = OPENCLAW_CONFIG.read_bytes()
    result, _flow_log = _run_mocked_bootstrap_openclaw_flow(
        tmp_path,
        extra_env={"MUTATE_BOOTSTRAP_REPO_COPY": "1"},
    )

    assert result.returncode != 0
    assert "modified guarded repo config copy" in result.stderr
    assert OPENCLAW_CONFIG.read_bytes() == tracked_before
    assert not list(tmp_path.glob("g2-openclaw-bootstrap.*"))


def test_push_script_rejects_external_mutation_of_guarded_repo_config_copy(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    tracked_before = OPENCLAW_CONFIG.read_bytes()
    env["MOCK_MUTATE_REPO_PREFLIGHT_CONFIG"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "modified guarded repo config copy" in result.stderr
    assert openclaw_config.read_bytes() == initial_config
    assert OPENCLAW_CONFIG.read_bytes() == tracked_before
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_generated_config_mutation_before_publication(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    env["MOCK_MUTATE_GENERATED_CONFIG"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "modified generated config during validation" in result.stderr
    assert openclaw_config.read_bytes() == initial_config
    assert "Atomically published validated repo config" not in result.stdout


def test_push_script_publishes_exact_generated_bytes_validated_by_openclaw(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    published = openclaw_config.read_bytes()
    published_hash = hashlib.sha256(published).hexdigest()
    assert f"({len(published)} bytes, sha256 {published_hash})" in result.stdout
    assert "Atomically published validated repo config" in result.stdout


def test_push_script_signal_before_publication_rolls_back_without_publishing(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    checkpoint_log = tmp_path / "checkpoints.log"
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    initial_mode = _mode(openclaw_config)
    env["OPENCLAW_PUSH_TEST_CHECKPOINT_LOG"] = str(checkpoint_log)
    env["OPENCLAW_PUSH_TEST_SIGNAL_AT"] = "before-config-publication"
    env["OPENCLAW_PUSH_TEST_SIGNAL"] = "INT"

    result = _run_push_script(env)

    assert result.returncode == 130
    assert checkpoint_log.read_text(encoding="utf-8").splitlines() == ["before-config-publication"]
    assert openclaw_config.read_bytes() == initial_config
    assert _mode(openclaw_config) == initial_mode
    assert "Atomically published validated repo config" not in result.stdout


def test_push_script_rejects_final_live_config_warnings_and_rolls_back(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    initial_config = openclaw_config.read_text(encoding="utf-8")
    env["MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "config validate --json" in result.stderr
    assert "live.warning" in result.stderr
    assert openclaw_config.read_text(encoding="utf-8") == initial_config
    assert "Done. Config pushed successfully." not in result.stdout
    assert not (openclaw_home / "mempalace-readonly-server.py").exists()
    assert not (openclaw_home / "skills").exists()
    assert not (openclaw_home / "workspace").exists()
    assert not list(openclaw_home.glob("workspace-*"))
    assert not (openclaw_home / "agents/autoresearch-pm/agent").exists()
    assert not (openclaw_home / "agents/reviewer/agent").exists()
    assert not _supervisor_unit_dst(home).exists()
    assert not _runtime_caps_dropin_dst(home).exists()
    assert not _codex_runtime_dropin_dst(home).exists()
    assert not _native_crash_hardening_dropin_dst(home).exists()


def test_push_script_rejects_live_config_mutation_during_final_validation(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    env["MOCK_MUTATE_LIVE_CONFIG_DURING_VALIDATE"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "modified live config during final validation" in result.stderr
    assert openclaw_config.read_bytes() == initial_config
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize("alias_mode", ["symlink", "hardlink"])
def test_push_script_rejects_live_config_topology_replacement_during_final_validation(
    tmp_path: Path,
    alias_mode: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    env["MOCK_ALIAS_LIVE_CONFIG_AFTER_VALIDATE"] = alias_mode

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "changed live config identity/topology during final validation" in result.stderr
    _assert_alias_mode_reported(result.stderr, alias_mode)
    assert openclaw_config.read_bytes() == initial_config
    assert not openclaw_config.is_symlink()
    assert openclaw_config.stat().st_nlink == 1
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_local_config_rollback_preserves_mode_and_bytes(tmp_path: Path) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_config = Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json"
    openclaw_config.write_bytes(b'{"local":"before"}\n')
    openclaw_config.chmod(0o640)
    initial_config = openclaw_config.read_bytes()
    env["MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert openclaw_config.read_bytes() == initial_config
    assert _mode(openclaw_config) == 0o640
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert f"cp -aT -- {openclaw_config} " in cp_log
    assert ".openclaw.rollback." in cp_log


def test_push_script_local_config_rollback_preserves_symlink_topology(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    symlink_target = openclaw_home / "openclaw-target.json"
    initial_config = b'{"local":"symlink-target"}\n'
    symlink_target.write_bytes(initial_config)
    symlink_target.chmod(0o640)
    openclaw_config.unlink()
    openclaw_config.symlink_to(symlink_target)
    env["MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "live.warning" in result.stderr
    assert openclaw_config.is_symlink()
    assert os.readlink(openclaw_config) == str(symlink_target)
    assert openclaw_config.read_bytes() == initial_config
    assert symlink_target.read_bytes() == initial_config
    assert _mode(symlink_target) == 0o640
    assert not (openclaw_home / "mempalace-readonly-server.py").exists()
    assert not _supervisor_unit_dst(home).exists()
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert f"cp -aT -- {openclaw_config} {openclaw_config}.bak." in cp_log
    assert ".openclaw.rollback." in cp_log


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
    assert (
        f"sqlite3 {log_db} DROP INDEX idx_logs_thread_id; "
        "CREATE INDEX idx_logs_thread_id ON logs(thread_id); PRAGMA integrity_check;"
    ) in sqlite_log
    assert "REINDEX;" not in sqlite_log
    assert f"Rebuilding scoped Codex log DB idx_logs_thread_id: {log_db}" in result.stdout
    assert f"Repaired scoped Codex log DB idx_logs_thread_id: {log_db}" in result.stdout


def test_push_script_fails_when_scoped_codex_log_db_rebuild_cannot_repair(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    log_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/logs_2.sqlite"
    _create_codex_logs_db(log_db)
    env["MOCK_CODEX_LOG_DB_INTEGRITY"] = "corrupt"
    env["MOCK_CODEX_LOG_DB_REBUILD"] = "fail"

    result = _run_push_script(env)

    assert result.returncode != 0
    assert f"Scoped Codex log DB validation/repair failed for {log_db}" in result.stderr
    assert "remains corrupt after rebuilding idx_logs_thread_id" in result.stderr
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


def test_push_script_repairs_codex_state_db_stale_missing_rollout_paths(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    existing_rollout = state_db.parent / "sessions/2026/08/01/rollout-existing.jsonl"
    missing_rollout = state_db.parent / "sessions/2026/08/01/rollout-missing.jsonl"
    existing_rollout.parent.mkdir(parents=True)
    existing_rollout.write_text("{}\n", encoding="utf-8")
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "existing-thread", existing_rollout)
        _insert_codex_thread(connection, "stale-thread", missing_rollout)
        connection.execute(
            """
            INSERT INTO thread_dynamic_tools (
                thread_id,
                position,
                name,
                description,
                input_schema
            ) VALUES ('stale-thread', 0, 'stale_tool', 'stale tool', '{}')
            """
        )
    env["MOCK_CODEX_DOCTOR_ROLLOUT_DB_PARITY_STATE_DB"] = str(state_db)

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    assert _codex_thread_ids(state_db) == ["existing-thread"]
    with sqlite3.connect(state_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM thread_dynamic_tools;").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check;").fetchone() == ("ok",)
    assert (
        f"Repairing scoped Codex state DB stale thread rows: {state_db} (1 rows)" in result.stdout
    )
    assert f"Repaired scoped Codex state DB stale thread rows: {state_db} (1 rows)" in result.stdout
    assert "state.rollout_db_parity" not in result.stderr


def test_push_script_rejects_codex_state_db_stale_rollout_path_outside_expected_tree_before_delete(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    existing_rollout = state_db.parent / "sessions/2026/08/01/rollout-existing.jsonl"
    outside_rollout = tmp_path / "outside/rollout-stale.jsonl"
    existing_rollout.parent.mkdir(parents=True)
    existing_rollout.write_text("{}\n", encoding="utf-8")
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "existing-thread", existing_rollout)
        _insert_codex_thread(connection, "stale-thread", outside_rollout)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "rollout_path is outside expected Codex rollout tree" in result.stderr
    assert _codex_thread_ids(state_db) == ["existing-thread", "stale-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize(
    ("rollout_path", "expected_message"),
    (
        (
            Path("sessions/2026/02/31/rollout-invalid-date.jsonl"),
            "rollout_path has invalid calendar date",
        ),
        (
            Path("sessions/2026/08/01/not-a-rollout.jsonl"),
            "rollout_path filename is not rollout-*.jsonl",
        ),
    ),
)
def test_push_script_rejects_codex_state_db_invalid_rollout_scope_before_delete(
    tmp_path: Path,
    rollout_path: Path,
    expected_message: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    candidate_rollout = state_db.parent / rollout_path
    candidate_rollout.parent.mkdir(parents=True, exist_ok=True)
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "thread-with-invalid-rollout", candidate_rollout)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert expected_message in result.stderr
    assert _codex_thread_ids(state_db) == ["thread-with-invalid-rollout"]
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize(
    ("existing_kind", "expected_message"),
    (
        ("symlink", "existing rollout_path must not be a symlink"),
        ("directory", "existing rollout_path must be a regular file"),
    ),
)
def test_push_script_rejects_codex_state_db_unusable_existing_rollout_path_before_delete(
    tmp_path: Path,
    existing_kind: str,
    expected_message: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    existing_rollout = state_db.parent / "sessions/2026/08/01/rollout-existing.jsonl"
    existing_rollout.parent.mkdir(parents=True)
    if existing_kind == "symlink":
        target = tmp_path / "real-rollout.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        existing_rollout.symlink_to(target)
    else:
        existing_rollout.mkdir()
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "existing-thread", existing_rollout)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert expected_message in result.stderr
    assert _codex_thread_ids(state_db) == ["existing-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_codex_state_db_stale_rollout_path_with_symlinked_parent_before_delete(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    month_dir = state_db.parent / "sessions/2026/08"
    month_dir.mkdir(parents=True)
    real_day_dir = tmp_path / "real-day-dir"
    real_day_dir.mkdir()
    (month_dir / "01").symlink_to(real_day_dir, target_is_directory=True)
    stale_rollout = month_dir / "01/rollout-stale.jsonl"
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "stale-thread", stale_rollout)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "parent topology is not a normal sessions tree" in result.stderr
    assert "symlinked day directory" in result.stderr
    assert _codex_thread_ids(state_db) == ["stale-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize(
    ("dependent_column", "expected_message"),
    (
        ("parent_thread_id", "thread_spawn_edges"),
        ("child_thread_id", "thread_spawn_edges"),
    ),
)
def test_push_script_rejects_codex_state_db_stale_thread_spawn_edge_reference_before_delete(
    tmp_path: Path,
    dependent_column: str,
    expected_message: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    stale_rollout = state_db.parent / "sessions/2026/08/01/rollout-stale.jsonl"
    stale_rollout.parent.mkdir(parents=True)
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "stale-thread", stale_rollout)
        if dependent_column == "parent_thread_id":
            connection.execute(
                """
                INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status)
                VALUES ('stale-thread', 'child-thread', 'running')
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status)
                VALUES ('parent-thread', 'stale-thread', 'running')
                """
            )

    result = _run_push_script(env)

    assert result.returncode != 0
    assert expected_message in result.stderr
    assert _codex_thread_ids(state_db) == ["stale-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


@pytest.mark.parametrize(
    ("edge_parent", "edge_child"),
    (
        ("missing-parent", "existing-thread"),
        ("existing-thread", "missing-child"),
    ),
)
def test_push_script_rejects_codex_state_db_global_orphaned_spawn_edges(
    tmp_path: Path,
    edge_parent: str,
    edge_child: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    existing_rollout = state_db.parent / "sessions/2026/08/01/rollout-existing.jsonl"
    existing_rollout.parent.mkdir(parents=True)
    existing_rollout.write_text("{}\n", encoding="utf-8")
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "existing-thread", existing_rollout)
        connection.execute(
            """
            INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status)
            VALUES (?, ?, 'running')
            """,
            (edge_parent, edge_child),
        )

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "orphaned non-cascading thread_spawn_edges" in result.stderr
    assert _codex_thread_ids(state_db) == ["existing-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_allows_codex_state_db_global_valid_assigned_job_item_thread(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    existing_rollout = state_db.parent / "sessions/2026/08/01/rollout-existing.jsonl"
    existing_rollout.parent.mkdir(parents=True)
    existing_rollout.write_text("{}\n", encoding="utf-8")
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "existing-thread", existing_rollout)
        _insert_codex_agent_job_item(connection, "existing-thread")

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    assert _codex_thread_ids(state_db) == ["existing-thread"]


def test_push_script_rejects_codex_state_db_global_orphaned_job_item_thread(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    existing_rollout = state_db.parent / "sessions/2026/08/01/rollout-existing.jsonl"
    existing_rollout.parent.mkdir(parents=True)
    existing_rollout.write_text("{}\n", encoding="utf-8")
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "existing-thread", existing_rollout)
        _insert_codex_agent_job_item(connection, "missing-thread")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "orphaned non-cascading agent_job_items.assigned_thread_id" in result.stderr
    assert _codex_thread_ids(state_db) == ["existing-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_codex_state_db_stale_agent_job_item_reference_before_delete(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    stale_rollout = state_db.parent / "sessions/2026/08/01/rollout-stale.jsonl"
    stale_rollout.parent.mkdir(parents=True)
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "stale-thread", stale_rollout)
        _insert_codex_agent_job_item(connection, "stale-thread")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "agent_job_items" in result.stderr
    assert _codex_thread_ids(state_db) == ["stale-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_codex_state_db_multi_stale_rows_atomically_on_dependent_reference(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    first_stale_rollout = state_db.parent / "sessions/2026/08/01/rollout-stale-first.jsonl"
    second_stale_rollout = state_db.parent / "sessions/2026/08/01/rollout-stale-second.jsonl"
    first_stale_rollout.parent.mkdir(parents=True)
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "stale-a-thread", first_stale_rollout)
        _insert_codex_thread(connection, "stale-b-thread", second_stale_rollout)
        _insert_codex_agent_job_item(connection, "stale-b-thread")

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "agent_job_items" in result.stderr
    assert _codex_thread_ids(state_db) == ["stale-a-thread", "stale-b-thread"]
    assert "Repairing scoped Codex state DB stale thread rows" not in result.stdout
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_codex_state_db_with_wrong_schema_before_delete(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    missing_rollout = state_db.parent / "sessions/2026/08/01/rollout-missing.jsonl"
    _create_codex_state_db(
        state_db,
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL
        );
        """,
    )
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            "INSERT INTO threads (id, rollout_path) VALUES ('stale-thread', ?)",
            (str(missing_rollout),),
        )

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "state DB schema does not match the pinned state_5.sqlite schema" in result.stderr
    assert _codex_thread_ids(state_db) == ["stale-thread"]
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_codex_state_db_symlink_before_sqlite_access(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    real_db = tmp_path / "real-state.sqlite"
    _create_codex_state_db(real_db)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    state_db.parent.mkdir(parents=True)
    state_db.symlink_to(real_db)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Scoped Codex state DB must not be a symlink" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rejects_codex_state_db_hardlink_before_sqlite_access(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    _create_codex_state_db(state_db)
    hardlink = tmp_path / "state-hardlink.sqlite"
    hardlink.hardlink_to(state_db)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "Scoped Codex state DB must not have hard links" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout


def test_push_script_rolls_back_codex_state_db_transaction_when_commit_fails(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    state_db = Path(env["OPENCLAW_PUSH_HOME"]) / "agents/main/agent/codex-home/state_5.sqlite"
    missing_rollout = state_db.parent / "sessions/2026/08/01/rollout-missing.jsonl"
    rollback_log = tmp_path / "rollback.log"
    sitecustomize_dir = tmp_path / "sitecustomize"
    missing_rollout.parent.mkdir(parents=True)
    _create_codex_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        _insert_codex_thread(connection, "stale-thread", missing_rollout)
    _write_sqlite_commit_failure_sitecustomize(sitecustomize_dir)
    env["PYTHONPATH"] = str(sitecustomize_dir)
    env["MOCK_CODEX_STATE_DB_COMMIT_FAIL_PATH"] = str(state_db)
    env["MOCK_CODEX_STATE_DB_ROLLBACK_LOG"] = str(rollback_log)

    result = _run_push_script(env)

    assert result.returncode != 0
    assert "injected state DB commit failure" in result.stderr
    assert rollback_log.read_text(encoding="utf-8") == "rollback\n"
    assert _codex_thread_ids(state_db) == ["stale-thread"]
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


def test_push_script_codex_rewrites_only_stale_node_options_assignment_lines(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["SYSTEMD_MANAGER_EXTRA_ENV"] = "\n".join(
        [
            "OTHER=--require /home/dev/.openclaw/azure-api-version-preload.cjs",
            "ANOTHER=1",
        ]
    )
    home = Path(env["HOME"])
    service_path = home / ".config/systemd/user/openclaw-gateway.service"
    service_path.parent.mkdir(parents=True)
    unrelated_env_line = (
        'Environment="OTHER=--require /home/dev/.openclaw/azure-api-version-preload.cjs"'
    )
    exec_line = (
        "ExecStart=/usr/bin/node "
        "/home/dev/.openclaw/azure-api-version-preload.cjs --not-node-options"
    )
    service_path.write_text(
        "\n".join(
            [
                "[Service]",
                unrelated_env_line,
                'Environment="NODE_OPTIONS=--require '
                '/home/dev/.openclaw/azure-api-version-preload.cjs"',
                exec_line,
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    service_lines = service_path.read_text(encoding="utf-8").splitlines()
    assert unrelated_env_line in service_lines
    assert exec_line in service_lines
    assert not any("NODE_OPTIONS=" in line for line in service_lines)
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" not in systemctl_log
    assert "restore NODE_OPTIONS" not in systemctl_log
    assert systemctl_log.count("systemctl --user daemon-reload") == 2


def test_push_script_codex_rewrites_compound_environment_node_options_assignment(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    service_path = home / ".config/systemd/user/openclaw-gateway.service"
    service_path.parent.mkdir(parents=True)
    compound_line = (
        'Environment="KEEP=1" "NODE_OPTIONS=--require '
        '$HOME/.openclaw/azure-api-version-preload.cjs" "ALSO=two words"'
    )
    service_path.write_text(
        "\n".join(
            [
                "[Service]",
                compound_line,
                "Environment=UNCHANGED=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    service_lines = service_path.read_text(encoding="utf-8").splitlines()
    assert 'Environment="KEEP=1" "ALSO=two words"' in service_lines
    assert "Environment=UNCHANGED=1" in service_lines
    assert not any("NODE_OPTIONS=" in line for line in service_lines)
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" not in systemctl_log
    assert systemctl_log.count("systemctl --user daemon-reload") == 2


def test_push_script_codex_rewrites_multiline_environment_node_options_assignment(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    service_path = home / ".config/systemd/user/openclaw-gateway.service"
    service_path.parent.mkdir(parents=True)
    unrelated_stale_path_line = (
        'Environment="OTHER=/home/dev/.openclaw/azure-api-version-preload.cjs"'
    )
    service_path.write_text(
        "\n".join(
            [
                "[Service]",
                'Environment="KEEP=1" \\',
                '  "NODE_OPTIONS=--require \\',
                '  /home/dev/.openclaw/azure-api-version-preload.cjs" \\',
                '  "ALSO=two words"',
                unrelated_stale_path_line,
                "Environment=UNCHANGED=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    service_text = service_path.read_text(encoding="utf-8")
    assert "NODE_OPTIONS=" not in service_text
    assert '"KEEP=1"' in service_text
    assert '"ALSO=two words"' in service_text
    assert unrelated_stale_path_line in service_text
    assert "Environment=UNCHANGED=1" in service_text
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" not in systemctl_log
    assert systemctl_log.count("systemctl --user daemon-reload") == 2


def test_push_script_codex_rewrites_multiline_environment_across_comment_and_blank(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    service_path = home / ".config/systemd/user/openclaw-gateway.service"
    service_path.parent.mkdir(parents=True)
    service_path.write_text(
        "\n".join(
            [
                "[Service]",
                'Environment="KEEP=1" \\',
                "  # ignored inside a systemd continuation",
                "",
                '  "NODE_OPTIONS=--require \\',
                '  /home/dev/.openclaw/azure-api-version-preload.cjs" \\',
                '  "ALSO=two words"',
                "Environment=UNCHANGED=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_push_script(env)

    assert result.returncode == 0, result.stderr
    service_text = service_path.read_text(encoding="utf-8")
    assert "NODE_OPTIONS=" not in service_text
    assert '"KEEP=1"' in service_text
    assert '"ALSO=two words"' in service_text
    assert "# ignored inside a systemd continuation" in service_text
    assert "Environment=UNCHANGED=1" in service_text
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" not in systemctl_log
    assert systemctl_log.count("systemctl --user daemon-reload") == 2


def test_push_script_rolls_back_original_manager_node_options_on_failure(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    original_node_options = "--require /home/dev/.openclaw/azure-api-version-preload.cjs"
    env["SYSTEMD_MANAGER_NODE_OPTIONS"] = original_node_options
    env["FAIL_DAEMON_RELOAD"] = "1"
    initial_config = (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode != 0
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    unset = "systemctl --user unset-environment NODE_OPTIONS"
    restore = f"systemctl --user set-environment NODE_OPTIONS={original_node_options}"
    assert unset in systemctl_log
    assert restore in systemctl_log
    assert f"NODE_OPTIONS=$'{original_node_options}'" not in systemctl_log
    assert systemctl_log.index(unset) < systemctl_log.index(restore)
    assert (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8") == (
        initial_config
    )


def test_push_script_restores_manager_node_options_if_unset_fails_after_mutation(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    original_node_options = "--require /home/dev/.openclaw/azure-api-version-preload.cjs"
    env["SYSTEMD_MANAGER_NODE_OPTIONS"] = original_node_options
    env["FAIL_MANAGER_NODE_OPTIONS_UNSET_AFTER_MUTATION"] = "1"
    initial_config = (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "unset NODE_OPTIONS failed after mutation by test" in result.stderr
    assert "Failed to unset stale Azure NODE_OPTIONS" in result.stderr
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    unset = "systemctl --user unset-environment NODE_OPTIONS"
    restore = f"systemctl --user set-environment NODE_OPTIONS={original_node_options}"
    assert unset in systemctl_log
    assert restore in systemctl_log
    assert f"NODE_OPTIONS=$'{original_node_options}'" not in systemctl_log
    assert systemctl_log.index(unset) < systemctl_log.index(restore)
    assert (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8") == (
        initial_config
    )


def test_push_script_fails_closed_when_manager_node_options_persists_after_unset(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    original_node_options = "--require /home/dev/.openclaw/azure-api-version-preload.cjs"
    env["SYSTEMD_MANAGER_NODE_OPTIONS"] = original_node_options
    env["SYSTEMD_MANAGER_NODE_OPTIONS_PERSIST_AFTER_UNSET"] = "1"
    initial_config = (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "still exposes stale Azure NODE_OPTIONS after unset-environment" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" in systemctl_log
    assert systemctl_log.count("systemctl --user show-environment") >= 2
    assert "systemctl --user set-environment NODE_OPTIONS=" in systemctl_log
    assert (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8") == (
        initial_config
    )


def test_push_script_restores_c_style_manager_node_options_escapes_exactly(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    restore_value_file = tmp_path / "restored-node-options.bin"
    original_node_options = (
        "--require\t/home/dev/.openclaw/azure-api-version-preload.cjs\n"
        "quote=' double=\" backslash=\\ bell=\a esc=\x1b octal=S"
    )
    env["SYSTEMD_MANAGER_NODE_OPTIONS_RAW"] = (
        r"NODE_OPTIONS=$'--require\t/home/dev/.openclaw/azure-api-version-preload.cjs\n"
        r"quote=\' double=\" backslash=\\ bell=\a esc=\e octal=\123'"
    )
    env["SYSTEMD_MANAGER_RESTORE_VALUE_FILE"] = str(restore_value_file)
    env["FAIL_DAEMON_RELOAD"] = "1"

    result = _run_push_script(env)

    assert result.returncode == 1
    assert restore_value_file.read_bytes() == original_node_options.encode()


def test_push_script_refuses_malformed_manager_node_options_encoding(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["SYSTEMD_MANAGER_NODE_OPTIONS_RAW"] = (
        r"NODE_OPTIONS=$'--require\x0G/home/dev/.openclaw/azure-api-version-preload.cjs'"
    )

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Could not safely decode systemd user manager NODE_OPTIONS assignment" in result.stderr
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert "systemctl --user unset-environment NODE_OPTIONS" not in systemctl_log


def test_push_script_manager_node_options_restore_failure_retains_recovery_dir(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["SYSTEMD_MANAGER_NODE_OPTIONS"] = (
        "--require /home/dev/.openclaw/azure-api-version-preload.cjs"
    )
    env["FAIL_DAEMON_RELOAD"] = "1"
    env["FAIL_MANAGER_NODE_OPTIONS_RESTORE"] = "1"
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    initial_config = (openclaw_home / "openclaw.json").read_text(encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "restore NODE_OPTIONS failed by test" in result.stderr
    assert "Failed to restore systemd user manager NODE_OPTIONS during rollback" in result.stderr
    recovery_dirs = sorted(openclaw_home.glob(".push-openclaw-config-artifacts.*"))
    assert len(recovery_dirs) == 1
    assert (
        f"Managed OpenClaw artifact recovery directory preserved at {recovery_dirs[0]}"
        in result.stderr
    )
    assert (openclaw_home / "openclaw.json").read_text(encoding="utf-8") == initial_config


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

    assert result.returncode == 1
    assert "daemon-reload failed by test" in result.stderr
    assert "Restoring managed systemd files after failed publication." in result.stderr
    assert "Failed to reload user systemd units after managed-file rollback" in result.stderr
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

    assert result.returncode == 1
    assert "Restoring managed systemd files after failed publication." in result.stderr
    assert "Failed to reload user systemd units after managed-file rollback" in result.stderr
    for path, content in prior_files.items():
        assert path.read_text(encoding="utf-8") == content
        assert _mode(path) == 0o600
    assert (Path(env["OPENCLAW_PUSH_HOME"]) / "openclaw.json").read_text(encoding="utf-8") == (
        initial_config
    )


def test_push_script_final_daemon_reload_runs_after_systemd_artifact_restore(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"
    home = Path(env["HOME"])
    service_path = home / ".config/systemd/user/openclaw-gateway.service"
    dropin_dir = service_path.parent / "openclaw-gateway.service.d"
    legacy_dropin = dropin_dir / "05-legacy-azure.conf"
    service_path.parent.mkdir(parents=True)
    dropin_dir.mkdir()
    service_text = "\n".join(
        [
            "[Service]",
            'Environment="NODE_OPTIONS=--require '
            '/home/dev/.openclaw/azure-api-version-preload.cjs"',
            "",
        ]
    )
    dropin_text = "\n".join(
        [
            "[Service]",
            'Environment="NODE_OPTIONS=--require '
            '/home/dev/.openclaw/azure-api-version-preload.cjs"',
            "",
        ]
    )
    service_path.write_text(service_text, encoding="utf-8")
    legacy_dropin.write_text(dropin_text, encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 1
    assert service_path.read_text(encoding="utf-8") == service_text
    assert legacy_dropin.read_text(encoding="utf-8") == dropin_text
    systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8")
    assert systemctl_log.count("systemctl --user daemon-reload") == 2
    assert "Failed final user systemd daemon-reload" not in result.stderr


def test_push_script_failed_unit_snapshot_state_retains_original_unit(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_BACKUP_SOURCE_BASENAME"] = "20-openclaw-codex-runtime.conf"
    home = Path(env["HOME"])
    codex_dropin = _codex_runtime_dropin_dst(home)
    codex_dropin.parent.mkdir(parents=True)
    prior_text = "[Service]\nExecStartPre=/bin/true\n"
    codex_dropin.write_text(prior_text, encoding="utf-8")
    codex_dropin.chmod(0o600)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Failed to snapshot managed systemd file" in result.stderr
    assert "managed systemd snapshot did not complete" in result.stderr
    assert "Failed to remove newly installed managed systemd file" not in result.stderr
    assert codex_dropin.read_text(encoding="utf-8") == prior_text
    assert _mode(codex_dropin) == 0o600
    recovery_dirs = sorted((home / ".config/systemd/user").glob(".push-openclaw-config-units.*"))
    assert len(recovery_dirs) == 1


def test_push_script_managed_systemd_symlink_fails_closed_and_rolls_back_artifacts(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    wrapper = openclaw_home / "mempalace-readonly-server.py"
    wrapper.write_text("prior wrapper\n", encoding="utf-8")
    codex_dropin = _codex_runtime_dropin_dst(home)
    codex_target = home / "linked-codex-runtime.conf"
    codex_dropin.parent.mkdir(parents=True)
    codex_target.write_text("[Service]\nExecStartPre=/bin/true\n", encoding="utf-8")
    codex_target.chmod(0o600)
    codex_dropin.symlink_to(codex_target)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert f"Managed systemd file {codex_dropin} is a symlink" in result.stderr
    assert "Refusing before mutating managed systemd files" in result.stderr
    assert "Restoring managed OpenClaw artifacts after failed publication." in result.stderr
    assert codex_dropin.is_symlink()
    assert os.readlink(codex_dropin) == str(codex_target)
    assert codex_target.read_text(encoding="utf-8") == "[Service]\nExecStartPre=/bin/true\n"
    assert _mode(codex_target) == 0o600
    assert wrapper.read_text(encoding="utf-8") == "prior wrapper\n"
    assert openclaw_config.read_bytes() == initial_config


def test_push_script_systemd_restore_failure_keeps_recovery_dir_and_rolls_back_artifacts(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_DAEMON_RELOAD"] = "1"
    env["FAIL_RESTORE_COMMIT_DEST_BASENAME"] = "20-openclaw-codex-runtime.conf"
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    wrapper = openclaw_home / "mempalace-readonly-server.py"
    wrapper.write_text("prior wrapper\n", encoding="utf-8")
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

    assert result.returncode == 1
    recovery_dirs = sorted((home / ".config/systemd/user").glob(".push-openclaw-config-units.*"))
    assert len(recovery_dirs) == 1
    assert f"Managed systemd recovery directory preserved at {recovery_dirs[0]}" in result.stderr
    assert "restore mv failed by test" in result.stderr
    assert (
        _supervisor_unit_dst(home).read_text(encoding="utf-8")
        == prior_files[_supervisor_unit_dst(home)]
    )
    assert (
        _runtime_caps_dropin_dst(home).read_text(encoding="utf-8")
        == prior_files[_runtime_caps_dropin_dst(home)]
    )
    assert (
        _native_crash_hardening_dropin_dst(home).read_text(encoding="utf-8")
        == prior_files[_native_crash_hardening_dropin_dst(home)]
    )
    assert wrapper.read_text(encoding="utf-8") == "prior wrapper\n"


def test_push_script_artifact_restore_failure_keeps_recovery_dir_and_continues_paths(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"
    env["FAIL_RESTORE_COMMIT_DEST_BASENAME"] = "skills"
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    wrapper = openclaw_home / "mempalace-readonly-server.py"
    skills = openclaw_home / "skills"
    wrapper.write_text("prior wrapper\n", encoding="utf-8")
    skills.mkdir()
    (skills / "prior.txt").write_text("prior skills\n", encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 1
    recovery_dirs = sorted(openclaw_home.glob(".push-openclaw-config-artifacts.*"))
    assert len(recovery_dirs) == 1
    assert (
        f"Managed OpenClaw artifact recovery directory preserved at {recovery_dirs[0]}"
        in result.stderr
    )
    assert "restore mv failed by test" in result.stderr
    assert "staged restore copy preserved" in result.stderr
    assert wrapper.read_text(encoding="utf-8") == "prior wrapper\n"
    assert not skills.exists()
    staged_restores = sorted(recovery_dirs[0].glob("restore.*"))
    assert staged_restores


def test_push_script_artifact_stage_copy_failure_preserves_original_path(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["MOCK_LIVE_OPENCLAW_CONFIG_VALIDATE_WARN"] = "1"
    env["FAIL_STAGE_RESTORE_IF_BACKUP_CONTAINS_BASENAME"] = "prior.txt"
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    skills = openclaw_home / "skills"
    skills.mkdir()
    skills.chmod(0o750)
    prior = skills / "prior.txt"
    prior.write_text("prior skills\n", encoding="utf-8")
    prior.chmod(0o640)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "staged restore cp failed by test" in result.stderr
    assert "Original artifact path left intact" in result.stderr
    assert skills.is_dir()
    assert _mode(skills) == 0o750
    assert prior.read_text(encoding="utf-8") == "prior skills\n"
    assert _mode(prior) == 0o640


def test_push_script_failed_artifact_snapshot_never_overwrites_original_and_continues_rollback(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_BACKUP_SOURCE_BASENAME"] = "skills"
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    wrapper = openclaw_home / "mempalace-readonly-server.py"
    wrapper.write_bytes(b"prior wrapper bytes\n")
    wrapper.chmod(0o640)
    skills = openclaw_home / "skills"
    skills.mkdir()
    skills.chmod(0o750)
    skill_file = skills / "prior.bin"
    skill_file.write_bytes(b"\x00prior skills bytes\n")
    skill_file.chmod(0o640)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "backup cp failed by test" in result.stderr
    assert "Failed to snapshot managed OpenClaw artifact" in result.stderr
    assert "Skipping rollback" in result.stderr
    recovery_dirs = sorted(openclaw_home.glob(".push-openclaw-config-artifacts.*"))
    assert len(recovery_dirs) == 1
    assert (
        f"Managed OpenClaw artifact recovery directory preserved at {recovery_dirs[0]}"
        in result.stderr
    )
    assert skills.is_dir()
    assert _mode(skills) == 0o750
    assert skill_file.read_bytes() == b"\x00prior skills bytes\n"
    assert _mode(skill_file) == 0o640
    assert wrapper.read_bytes() == b"prior wrapper bytes\n"
    assert _mode(wrapper) == 0o640
    assert openclaw_config.read_bytes() == initial_config
    cp_log = Path(env["CP_LOG"]).read_text(encoding="utf-8")
    assert ".openclaw.rollback." in cp_log


def test_push_script_managed_artifact_symlink_fails_closed_and_rolls_back_prior_paths(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    wrapper = openclaw_home / "mempalace-readonly-server.py"
    wrapper.write_text("prior wrapper\n", encoding="utf-8")
    skills = openclaw_home / "skills"
    skills_target = tmp_path / "external-skills-target"
    skills_target.mkdir()
    target_file = skills_target / "target.txt"
    target_file.write_text("external target stays unmanaged\n", encoding="utf-8")
    skills.symlink_to(skills_target, target_is_directory=True)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert f"Managed OpenClaw artifact {skills} is a symlink" in result.stderr
    assert "Refusing before mutating the managed artifact path" in result.stderr
    assert "Restoring managed OpenClaw artifacts after failed publication." in result.stderr
    assert skills.is_symlink()
    assert os.readlink(skills) == str(skills_target)
    assert target_file.read_text(encoding="utf-8") == "external target stays unmanaged\n"
    assert wrapper.read_text(encoding="utf-8") == "prior wrapper\n"
    assert openclaw_config.read_bytes() == initial_config


def test_push_script_nested_workspace_bootstrap_symlink_fails_closed_before_copy(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    workspace = openclaw_home / "workspace"
    workspace.mkdir()
    external_target = tmp_path / "external-workspace-agents.md"
    external_bytes = b"external workspace bootstrap target\n"
    external_target.write_bytes(external_bytes)
    nested_symlink = workspace / "AGENTS.md"
    nested_symlink.symlink_to(external_target)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path chain contains symlink" in result.stderr
    assert str(nested_symlink) in result.stderr
    assert "copying managed bootstrap file" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert nested_symlink.is_symlink()
    assert os.readlink(nested_symlink) == str(external_target)
    assert external_target.read_bytes() == external_bytes
    assert not (workspace / "SOUL.md").exists()
    assert not (workspace / "TOOLS.md").exists()
    assert not (workspace / "BOOTSTRAP.md").exists()
    assert not (openclaw_home / "mempalace-readonly-server.py").exists()
    assert not _supervisor_unit_dst(home).exists()
    assert openclaw_config.read_bytes() == initial_config


def test_push_script_workspace_bootstrap_directory_destination_fails_before_cp(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    workspace = openclaw_home / "workspace"
    agents_destination = workspace / "AGENTS.md"
    workspace.mkdir()
    agents_destination.mkdir()
    marker = agents_destination / "marker.txt"
    marker.write_text("directory destination stays untouched\n", encoding="utf-8")

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path is an existing directory" in result.stderr
    assert "Refusing before cp to avoid source-to-destination-directory behavior" in result.stderr
    assert "copying managed bootstrap file" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert marker.read_text(encoding="utf-8") == "directory destination stays untouched\n"
    assert not (workspace / "SOUL.md").exists()
    cp_log = _read_cp_log(env)
    assert f"cp {REPO_ROOT}/gateway/agent_config/AGENTS.md {agents_destination}" not in cp_log


@pytest.mark.parametrize(
    ("path_parts", "expected_context"),
    [
        (("workspace", "AGENTS.md"), "copying managed bootstrap file"),
        (
            ("agents", "main", "agent", "codex-home", "config.toml"),
            "writing managed Codex runtime config",
        ),
        (
            (
                "agents",
                "autoresearch-pm",
                "agent",
                "codex-home",
                "agents",
                "context_curator.toml",
            ),
            "copying managed Codex runtime agent",
        ),
    ],
)
def test_push_script_rejects_hardlinked_managed_file_destinations_before_mutation(
    tmp_path: Path,
    path_parts: tuple[str, ...],
    expected_context: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    hardlinked_destination = openclaw_home / Path(*path_parts)
    hardlinked_destination.parent.mkdir(parents=True)
    external_alias = tmp_path / ("external-" + "-".join(path_parts).replace("/", "-"))
    external_alias.write_bytes(b"external hard-link alias bytes\n")
    external_alias.chmod(0o640)
    os.link(external_alias, hardlinked_destination)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path is a hard-linked regular file" in result.stderr
    assert str(hardlinked_destination) in result.stderr
    assert expected_context in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert external_alias.read_bytes() == b"external hard-link alias bytes\n"
    assert _mode(external_alias) == 0o640


def test_push_script_rejects_hardlinked_managed_sqlite_auth_store_before_mutation(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    target_db = openclaw_home / "agents/reviewer/agent/openclaw-agent.sqlite"
    target_db.parent.mkdir(parents=True)
    external_alias = tmp_path / "external-openclaw-agent.sqlite"
    external_alias.write_bytes(b"external sqlite alias bytes\n")
    external_alias.chmod(0o640)
    os.link(external_alias, target_db)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path is a hard-linked regular file" in result.stderr
    assert str(target_db) in result.stderr
    assert "syncing managed OpenClaw agent auth database" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert external_alias.read_bytes() == b"external sqlite alias bytes\n"
    assert _mode(external_alias) == 0o640
    assert f"sqlite3 {target_db}" not in _read_sqlite_log(env)


def test_push_script_rejects_hardlinked_managed_systemd_file_before_chmod_or_publish(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    codex_dropin = _codex_runtime_dropin_dst(home)
    codex_dropin.parent.mkdir(parents=True)
    external_alias = tmp_path / "external-codex-runtime.conf"
    external_alias.write_text("[Service]\nExecStartPre=/bin/true\n", encoding="utf-8")
    external_alias.chmod(0o640)
    os.link(external_alias, codex_dropin)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path is a hard-linked regular file" in result.stderr
    assert str(codex_dropin) in result.stderr
    assert "rewriting managed systemd environment file" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert "Codex runtime verifier" not in result.stdout
    assert external_alias.read_text(encoding="utf-8") == "[Service]\nExecStartPre=/bin/true\n"
    assert _mode(external_alias) == 0o640
    mv_log = Path(env["MV_LOG"])
    if mv_log.exists():
        assert str(codex_dropin) not in mv_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path_parts", "expected_context"),
    [
        (
            ("main", "agent", "codex-home", "config.toml"),
            "writing managed Codex runtime config",
        ),
        (
            (
                "autoresearch-pm",
                "agent",
                "codex-home",
                "agents",
                "context_curator.toml",
            ),
            "copying managed Codex runtime agent",
        ),
    ],
)
def test_push_script_nested_codex_runtime_symlink_fails_closed_before_write(
    tmp_path: Path,
    path_parts: tuple[str, ...],
    expected_context: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    external_target = tmp_path / "external-codex-runtime-target"
    external_bytes = b"external codex runtime target\n"
    external_target.write_bytes(external_bytes)
    nested_symlink = openclaw_home / "agents" / Path(*path_parts)
    nested_symlink.parent.mkdir(parents=True)
    nested_symlink.symlink_to(external_target)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path chain contains symlink" in result.stderr
    assert str(nested_symlink) in result.stderr
    assert expected_context in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert nested_symlink.is_symlink()
    assert os.readlink(nested_symlink) == str(external_target)
    assert external_target.read_bytes() == external_bytes
    assert not (openclaw_home / "mempalace-readonly-server.py").exists()
    assert not (openclaw_home / "workspace").exists()
    assert not list(openclaw_home.glob("workspace-*"))
    assert not _supervisor_unit_dst(home).exists()
    assert openclaw_config.read_bytes() == initial_config


def test_push_script_nested_skill_file_symlink_fails_closed_before_copy(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"
    initial_config = openclaw_config.read_bytes()
    skill_dir = openclaw_home / "skills" / "autoresearch"
    skill_dir.mkdir(parents=True)
    external_target = tmp_path / "external-skill.md"
    external_bytes = b"external skill target\n"
    external_target.write_bytes(external_bytes)
    nested_symlink = skill_dir / "SKILL.md"
    nested_symlink.symlink_to(external_target)

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "Destination path chain contains symlink" in result.stderr
    assert str(nested_symlink) in result.stderr
    assert "copying managed skill file" in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert nested_symlink.is_symlink()
    assert os.readlink(nested_symlink) == str(external_target)
    assert external_target.read_bytes() == external_bytes
    assert not (openclaw_home / "skills/codex-subagents").exists()
    assert not (openclaw_home / "mempalace-readonly-server.py").exists()
    assert not (openclaw_home / "workspace").exists()
    assert not list(openclaw_home.glob("workspace-*"))
    assert not _supervisor_unit_dst(home).exists()
    assert openclaw_config.read_bytes() == initial_config


@pytest.mark.parametrize(
    ("fail_root_basename", "stale_path_parts", "expected_context"),
    [
        (
            "openclaw-gateway.service.d",
            (
                ".config",
                "systemd",
                "user",
                "openclaw-gateway.service.d",
                "05-legacy-azure.conf",
            ),
            "scanning managed systemd drop-in directory",
        ),
        (
            "agents",
            (
                "isolated push root with spaces $literal",
                "agents",
                "main",
                "agent",
                "codex-home",
                "agents",
                "stale-main.toml",
            ),
            "scanning stale main Codex runtime agents",
        ),
    ],
)
def test_push_script_find_scan_failures_abort_without_silently_leaving_stale_files(
    tmp_path: Path,
    fail_root_basename: str,
    stale_path_parts: tuple[str, ...],
    expected_context: str,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    home = Path(env["HOME"])
    stale_path = home / Path(*stale_path_parts)
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text("stale managed file\n", encoding="utf-8")
    env["FAIL_FIND_ROOT_BASENAME"] = fail_root_basename

    result = _run_push_script(env)

    assert result.returncode == 1
    assert "find failed by test" in result.stderr
    assert "Failed to scan" in result.stderr
    assert expected_context in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    assert stale_path.read_text(encoding="utf-8") == "stale managed file\n"


def test_push_script_managed_find_scans_are_status_propagating() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")

    assert "< <(find" not in script
    assert "collect_find_results_null" in script
    assert "refusing to continue with partial results" in script


def test_push_script_backup_cleanup_failure_keeps_exact_recovery_directories(
    tmp_path: Path,
) -> None:
    env = _prepare_push_script_home(tmp_path)
    env["FAIL_BACKUP_CLEANUP"] = "1"
    home = Path(env["HOME"])
    openclaw_home = Path(env["OPENCLAW_PUSH_HOME"])
    openclaw_config = openclaw_home / "openclaw.json"

    result = _run_push_script(env)

    assert result.returncode == 1
    unit_recovery_dirs = sorted(
        (home / ".config/systemd/user").glob(".push-openclaw-config-units.*")
    )
    artifact_recovery_dirs = sorted(openclaw_home.glob(".push-openclaw-config-artifacts.*"))
    assert len(unit_recovery_dirs) == 1
    assert len(artifact_recovery_dirs) == 1
    assert f"Managed systemd recovery directory preserved at {unit_recovery_dirs[0]}" in (
        result.stderr
    )
    assert (
        f"Managed OpenClaw artifact recovery directory preserved at {artifact_recovery_dirs[0]}"
        in result.stderr
    )
    assert "backup cleanup failed by test" in result.stderr
    assert "Restoring managed systemd files after failed publication." not in result.stderr
    assert "Restoring managed OpenClaw artifacts after failed publication." not in result.stderr
    assert "Done. Config pushed successfully." not in result.stdout
    deployed = json.loads(openclaw_config.read_text(encoding="utf-8"))
    assert {agent["id"] for agent in deployed["agents"]["list"]} >= {"main", "autoresearch-pm"}
    assert _runtime_caps_dropin_dst(home).read_text(encoding="utf-8") == EXPECTED_RUNTIME_CAP_TEXT
    assert _codex_runtime_dropin_dst(home).read_text(encoding="utf-8") == (
        EXPECTED_CODEX_RUNTIME_TEXT
    )
    assert _native_crash_hardening_dropin_dst(home).read_text(encoding="utf-8") == (
        EXPECTED_NATIVE_CRASH_HARDENING_TEXT
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
