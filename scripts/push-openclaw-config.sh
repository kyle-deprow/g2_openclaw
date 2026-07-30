#!/usr/bin/env bash
# push-openclaw-config.sh — Merge repo-maintained OpenClaw config into the local installation.
#
# Usage (from repo root):
#   bash scripts/push-openclaw-config.sh
#
# Prerequisites:
#   - jq (https://jqlang.github.io/jq/)
#   - sqlite3 CLI
#   - OpenClaw CLI exactly 2026.7.1-2
#   - MemPalace installed with 'make mempalace-install'
#   - For codex: run 'openclaw models auth login --provider openai' for main;
#     this script syncs that OpenClaw-managed Codex OAuth profile into managed
#     agent auth stores.
#   - For azure: run 'az login' to authenticate (Entra ID tokens acquired automatically)

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_CONFIG="${REPO_ROOT}/gateway/openclaw_config/openclaw.json"
ENV_FILE="${OPENCLAW_PUSH_ENV_FILE:-${REPO_ROOT}/gateway/openclaw_config/.env}"
QUANTIPY_ROOT="/home/dev/repos/quantipy"
SKILLS_SRC="${REPO_ROOT}/gateway/agent_config/skills"
CODEX_AGENTS_SRC="${REPO_ROOT}/.codex/agents"
MEMPALACE_READONLY_WRAPPER_SRC="${REPO_ROOT}/gateway/mempalace_readonly_server.py"
G2_CONTROL_MCP_MODULE="gateway.g2_control_mcp_server"
SUPERVISOR_UNIT_TEMPLATE="${REPO_ROOT}/gateway/openclaw_config/quantipy-autoresearch-supervisor.service.template"
SUPERVISOR_SERVICE_NAME="quantipy-autoresearch-supervisor.service"
GATEWAY_RUNTIME_CAPS_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/openclaw-gateway-runtime-caps.conf"
CODEX_RUNTIME_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/openclaw-codex-runtime.conf"
NATIVE_CRASH_HARDENING_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/openclaw-gateway-native-crash-hardening.conf"
GATEWAY_SERVICE_NAME="openclaw-gateway.service"
GATEWAY_RUNTIME_CAPS_DROPIN_NAME="10-quantipy-runtime-caps.conf"
CODEX_RUNTIME_DROPIN_NAME="20-openclaw-codex-runtime.conf"
NATIVE_CRASH_HARDENING_DROPIN_NAME="30-openclaw-native-crash-hardening.conf"
STALE_AZURE_PRELOAD_PATTERN="azure-api-version-preload.cjs"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SUPERVISOR_UNIT_DST="${SYSTEMD_USER_DIR}/quantipy-autoresearch-supervisor.service"
GATEWAY_RUNTIME_CAPS_DROPIN_DIR="${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}.d"
GATEWAY_RUNTIME_CAPS_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/${GATEWAY_RUNTIME_CAPS_DROPIN_NAME}"
CODEX_RUNTIME_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/${CODEX_RUNTIME_DROPIN_NAME}"
NATIVE_CRASH_HARDENING_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/${NATIVE_CRASH_HARDENING_DROPIN_NAME}"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"

REQUIRED_OPENCLAW_VERSION="2026.7.1-2"
REQUIRED_CODEX_PLUGIN_VERSION="2026.7.1-1"
REQUIRED_CODEX_APP_SERVER_VERSION="0.144.3"
OPENCLAW_BIN_RESOLVED=""
OPENCLAW_VERSION_RESOLVED=""
CODEX_APP_SERVER_CLI_RESOLVED=""
MEMPALACE_READONLY_WRAPPER_BASENAME="mempalace-readonly-server.py"
PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS=(
  "sessions_spawn"
  "sessions_yield"
  "agents_list"
  "sessions_list"
  "sessions_history"
)
MEMPALACE_READONLY_AGENT_IDS=(
  "context_curator"
  "debater_microstructure"
  "debater_data"
  "debater_skeptic"
  "debater_theory"
  "debater_implementation"
  "consensus_arbiter"
  "implementer"
  "reviewer"
  "fixer"
)
CODEX_NATIVE_STAGE_AGENT_IDS=("${MEMPALACE_READONLY_AGENT_IDS[@]}")
MEMPALACE_READONLY_SERVER_AGENT_IDS=(
  "main"
  "autoresearch-pm"
  "${MEMPALACE_READONLY_AGENT_IDS[@]}"
)
G2_CONTROL_SERVER_AGENT_IDS=("main")
MAIN_OPENCLAW_TOOL_ALLOW_IDS=(
  "g2-control__g2_autoresearch_status"
  "g2-control__g2_autoresearch_start"
  "g2-control__g2_autoresearch_stop"
  "mempalace-readonly__mempalace_status"
  "mempalace-readonly__mempalace_search"
  "mempalace-readonly__mempalace_get_drawer"
  "mempalace-readonly__mempalace_list_drawers"
  "mempalace-readonly__mempalace_list_wings"
  "mempalace-readonly__mempalace_list_rooms"
  "mempalace-readonly__mempalace_get_taxonomy"
  "mempalace-readonly__mempalace_get_aaak_spec"
  "mempalace-readonly__mempalace_diary_read"
  "mempalace-readonly__mempalace_kg_query"
  "mempalace-readonly__mempalace_kg_timeline"
  "mempalace-readonly__mempalace_kg_stats"
  "mempalace-readonly__mempalace_traverse"
  "mempalace-readonly__mempalace_find_tunnels"
  "mempalace-readonly__mempalace_follow_tunnels"
  "mempalace-readonly__mempalace_graph_stats"
  "mempalace-readonly__mempalace_list_tunnels"
  "mempalace-readonly__mempalace_list_hallways"
  "mempalace-readonly__mempalace_memories_filed_away"
)
CODEX_NATIVE_LEGACY_STAGE_AGENT_IDS=(
  "context-curator"
  "debater-microstructure"
  "debater-data"
  "debater-skeptic"
  "debater-theory"
  "debater-implementation"
  "consensus-arbiter"
)
RUNTIME_CAP_ENV_LINES=(
  'UMask=0077'
  'Environment="LOKY_MAX_CPU_COUNT=1"'
  'Environment="OMP_NUM_THREADS=1"'
  'Environment="OPENBLAS_NUM_THREADS=1"'
  'Environment="MKL_NUM_THREADS=1"'
  'Environment="BLIS_NUM_THREADS=1"'
  'Environment="NUMEXPR_NUM_THREADS=1"'
  'Environment="VECLIB_MAXIMUM_THREADS=1"'
  'Environment="PYTHONFAULTHANDLER=1"'
)
NATIVE_CRASH_HARDENING_LINES=(
  "[Service]"
  "MemoryHigh=6G"
  "MemoryMax=7G"
  "OOMPolicy=kill"
  "RestartPreventExitStatus=SIGABRT SIGBUS SIGFPE SIGILL SIGQUIT SIGSEGV SIGSYS SIGTRAP SIGXCPU SIGXFSZ SIGKILL"
)
STALE_CODING_PROVIDER_KEYS=(
  "github-copilot"
  "copilot-proxy"
  "copilot-cli"
)

quote_sqlite_literal() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\'}"
}

sync_managed_agent_codex_auth() {
  local source_agent_dir="${OPENCLAW_PUSH_HOME}/agents/main/agent"
  local source_db="${source_agent_dir}/openclaw-agent.sqlite"
  local source_profiles="${source_agent_dir}/auth-profiles.json"

  if [[ ! -f "${source_db}" ]]; then
    echo "ERROR: Missing main OpenClaw auth store ${source_db}." >&2
    echo "       Run: ${OPENCLAW_BIN_RESOLVED} models auth login --provider openai" >&2
    exit 1
  fi
  if ! sqlite3 "${source_db}" \
    "select 1 from auth_profile_store where store_json like '%\"provider\":\"openai\"%' limit 1;" \
    | grep -qx '1'; then
    echo "ERROR: Main OpenClaw auth store has no OpenAI/Codex OAuth profile." >&2
    echo "       Run: ${OPENCLAW_BIN_RESOLVED} models auth login --provider openai" >&2
    exit 1
  fi

  mapfile -t OPENAI_AGENT_IDS < <(jq -r '
    .agents.list[]?
    | select((.model.primary // "") | startswith("openai/"))
    | .id
  ' "${REPO_CONFIG}")
  if [[ "${#OPENAI_AGENT_IDS[@]}" -eq 0 ]]; then
    echo "ERROR: No OpenAI/Codex-managed agents found in ${REPO_CONFIG}" >&2
    exit 1
  fi

  echo "Syncing OpenClaw-managed Codex OAuth profile to ${#OPENAI_AGENT_IDS[@]} agent auth stores:"
  local source_db_sql
  source_db_sql="$(quote_sqlite_literal "${source_db}")"
  for AGENT_ID in "${OPENAI_AGENT_IDS[@]}"; do
    local agent_dir="${OPENCLAW_PUSH_HOME}/agents/${AGENT_ID}/agent"
    local target_db="${agent_dir}/openclaw-agent.sqlite"
    if [[ "${AGENT_ID}" == "main" ]]; then
      echo "  ${AGENT_ID} → ${target_db} (source)"
      continue
    fi
    mkdir -p "${agent_dir}"
    if [[ -f "${source_profiles}" ]]; then
      cp "${source_profiles}" "${agent_dir}/auth-profiles.json"
      chmod 0600 "${agent_dir}/auth-profiles.json"
    fi
    sqlite3 "${target_db}" <<SQL
ATTACH DATABASE ${source_db_sql} AS source_auth;
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS auth_profile_store (
  store_key TEXT NOT NULL PRIMARY KEY,
  store_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_profile_state (
  state_key TEXT NOT NULL PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
DELETE FROM auth_profile_store;
INSERT INTO auth_profile_store SELECT store_key, store_json, updated_at FROM source_auth.auth_profile_store;
DELETE FROM auth_profile_state;
INSERT INTO auth_profile_state SELECT state_key, state_json, updated_at FROM source_auth.auth_profile_state;
COMMIT;
SQL
    chmod 0600 "${target_db}"
    if ! sqlite3 "${target_db}" \
      "select 1 from auth_profile_store where store_json like '%\"provider\":\"openai\"%' limit 1;" \
      | grep -qx '1'; then
      echo "ERROR: Failed to sync OpenAI/Codex auth into ${target_db}" >&2
      exit 1
    fi
    echo "  ${AGENT_ID} → ${target_db}"
  done
}

build_string_array_json() {
  printf '%s\n' "$@" | jq -Rsc 'split("\n")[:-1]'
}

sanitize_stale_coding_provider_keys() {
  local stale_keys_json
  stale_keys_json="$(build_string_array_json "${STALE_CODING_PROVIDER_KEYS[@]}")"
  MERGED=$(echo "${MERGED}" | jq --argjson stale_keys "${stale_keys_json}" '
    walk(
      if type == "object" then
        with_entries(select((.key as $key | $stale_keys | index($key)) | not))
      else
        .
      end
    )
  ')
  echo "Sanitized stale coding-provider config keys: ${STALE_CODING_PROVIDER_KEYS[*]}"
}

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

expand_user_path() {
  local path="$1"
  case "${path}" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${path:2}" ;;
    *) printf '%s\n' "${path}" ;;
  esac
}

OPENCLAW_PUSH_HOME="$(expand_user_path "${OPENCLAW_PUSH_HOME:-${HOME}/.openclaw}")"
LOCAL_CONFIG="${OPENCLAW_PUSH_HOME}/openclaw.json"

run_openclaw_cli() {
  run_openclaw_cli_for_config "${LOCAL_CONFIG}" "$@"
}

run_openclaw_cli_for_config() {
  local config_path="$1"
  shift
  local -a env_args=(
    -u OPENCLAW_HOME
    -u OPENCLAW_PUSH_HOME
    -u OPENCLAW_STATE_DIR
    -u OPENCLAW_CONFIG_PATH
  )
  if [[ "${OPENCLAW_PROVIDER:-codex}" != "azure" ]]; then
    env_args+=(-u NODE_OPTIONS)
  fi
  env \
    "${env_args[@]}" \
    OPENCLAW_STATE_DIR="${OPENCLAW_PUSH_HOME}" \
    OPENCLAW_CONFIG_PATH="${config_path}" \
    "${OPENCLAW_BIN_RESOLVED}" "$@"
}

validate_runtime_caps_dropin_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Repo-managed OpenClaw gateway runtime caps drop-in not found at ${path}" >&2
    return 1
  fi
  if ! diff -u <(printf '[Service]\n'; printf '%s\n' "${RUNTIME_CAP_ENV_LINES[@]}") "${path}" >&2; then
    echo "ERROR: OpenClaw gateway runtime caps drop-in must match the repo-managed numerical runtime cap set exactly." >&2
    return 1
  fi
}

validate_codex_runtime_dropin_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Repo-managed OpenClaw Codex runtime drop-in not found at ${path}" >&2
    return 1
  fi
  if ! diff -u "${CODEX_RUNTIME_DROPIN_SRC}" "${path}" >&2; then
    echo "ERROR: OpenClaw Codex runtime drop-in must match the repo-managed pre-start verifier exactly." >&2
    return 1
  fi
}

validate_native_crash_hardening_dropin_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Repo-managed OpenClaw native-crash hardening drop-in not found at ${path}" >&2
    return 1
  fi
  if ! diff -u <(printf '%s\n' "${NATIVE_CRASH_HARDENING_LINES[@]}") "${path}" >&2; then
    echo "ERROR: OpenClaw native-crash hardening drop-in must match the repo-managed memory, OOM, and restart policy exactly." >&2
    return 1
  fi
}

validate_supervisor_unit_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Repo-managed supervisor unit not found at ${path}" >&2
    return 1
  fi
  if ! grep -Fxq "Requires=${GATEWAY_SERVICE_NAME}" "${path}" \
    || ! grep -Fxq "BindsTo=${GATEWAY_SERVICE_NAME}" "${path}" \
    || ! grep -Fxq "After=${GATEWAY_SERVICE_NAME}" "${path}" \
    || ! grep -Fxq "Restart=on-failure" "${path}" \
    || grep -Fxq "Restart=always" "${path}"; then
    echo "ERROR: Supervisor unit must bind to ${GATEWAY_SERVICE_NAME} and must not use Restart=always." >&2
    return 1
  fi
}

require_gateway_service_loadable() {
  local service_state load_state active_state
  if ! service_state="$(systemctl --user show "${GATEWAY_SERVICE_NAME}" --property=LoadState --property=ActiveState 2>&1)"; then
    echo "ERROR: Could not inspect ${GATEWAY_SERVICE_NAME} as a user service." >&2
    echo "       systemctl output: ${service_state}" >&2
    return 1
  fi

  load_state="$(printf '%s\n' "${service_state}" | awk -F= '$1 == "LoadState" { print $2; exit }')"
  active_state="$(printf '%s\n' "${service_state}" | awk -F= '$1 == "ActiveState" { print $2; exit }')"
  case "${load_state}" in
    loaded)
      echo "Verified ${GATEWAY_SERVICE_NAME} is loadable (ActiveState=${active_state:-unknown})."
      ;;
    not-found | "")
      echo "ERROR: ${GATEWAY_SERVICE_NAME} is not installed as a loadable user unit (LoadState=${load_state:-unknown})." >&2
      echo "       Refusing to install ${GATEWAY_RUNTIME_CAPS_DROPIN_NAME}; run OpenClaw onboarding or restore the user unit first." >&2
      return 1
      ;;
    *)
      echo "ERROR: ${GATEWAY_SERVICE_NAME} is not loadable (LoadState=${load_state}, ActiveState=${active_state:-unknown})." >&2
      echo "       Refusing to install ${GATEWAY_RUNTIME_CAPS_DROPIN_NAME} until the user unit is loadable." >&2
      return 1
      ;;
  esac
}

prepare_runtime_caps_dropin_dir() {
  mkdir -p "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"
  if [[ ! -O "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    echo "ERROR: Runtime caps drop-in directory is not owned by the current user: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
    return 1
  fi
  chmod 0755 "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"
}

remove_stale_azure_node_options_for_codex() {
  local changed=0
  local path temp
  local manager_env
  local service_path="${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}"
  local -a candidates=()

  if manager_env="$(systemctl --user show-environment 2>/dev/null)"; then
    if printf '%s\n' "${manager_env}" | grep -Fq "${STALE_AZURE_PRELOAD_PATTERN}"; then
      systemctl --user unset-environment NODE_OPTIONS
      changed=1
      echo "Unset stale Azure NODE_OPTIONS from systemd user manager environment"
    fi
  else
    echo "ERROR: Could not inspect systemd user manager environment for stale Azure NODE_OPTIONS." >&2
    return 1
  fi

  if [[ -f "${service_path}" ]]; then
    candidates+=("${service_path}")
  fi
  if [[ -d "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    while IFS= read -r -d '' path; do
      candidates+=("${path}")
    done < <(find "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" -maxdepth 1 -type f -name '*.conf' -print0)
  fi

  for path in "${candidates[@]}"; do
    if ! grep -Fq "${STALE_AZURE_PRELOAD_PATTERN}" "${path}"; then
      continue
    fi
    temp="$(mktemp "${path}.XXXXXX")"
    grep -Fv "${STALE_AZURE_PRELOAD_PATTERN}" "${path}" > "${temp}"
    chmod --reference="${path}" "${temp}"
    mv "${temp}" "${path}"
    changed=1
    echo "Removed stale Azure NODE_OPTIONS preload from ${path}"
  done

  if [[ "${changed}" -eq 1 ]]; then
    systemctl --user daemon-reload
  fi
}

resolve_openclaw_bin() {
  local -a candidates=()
  local candidate path_entry
  declare -A seen=()

  if [[ -n "${OPENCLAW_BIN:-}" ]]; then
    candidates+=("$(expand_user_path "${OPENCLAW_BIN}")")
  else
    candidates+=(
      "${HOME}/.local/share/pnpm/openclaw"
      "${HOME}/.local/bin/openclaw"
    )
    IFS=':' read -r -a path_entries <<< "${PATH:-}"
    for path_entry in "${path_entries[@]}"; do
      [[ -n "${path_entry}" ]] || continue
      candidates+=("${path_entry}/openclaw")
    done
  fi

  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if [[ -n "${seen[${candidate}]+x}" ]]; then
      continue
    fi
    seen["${candidate}"]=1
    if [[ -f "${candidate}" && -x "${candidate}" ]]; then
      OPENCLAW_BIN_RESOLVED="${candidate}"
      return 0
    fi
  done

  if [[ -n "${OPENCLAW_BIN:-}" ]]; then
    echo "ERROR: OPENCLAW_BIN points to a missing or non-executable path: $(expand_user_path "${OPENCLAW_BIN}")" >&2
  else
    echo "ERROR: OpenClaw executable not found. Checked ${HOME}/.local/share/pnpm/openclaw, ${HOME}/.local/bin/openclaw, and PATH entries." >&2
  fi
  return 1
}

require_openclaw_supported() {
  local version_line
  if ! resolve_openclaw_bin; then
    return 1
  fi
  if ! version_line="$(run_openclaw_cli --version 2>&1)"; then
    echo "ERROR: OpenClaw version check failed for ${OPENCLAW_BIN_RESOLVED}" >&2
    return 1
  fi
  version_line="${version_line%%$'\n'*}"
  if [[ "${version_line}" =~ (^|[[:space:]])([0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*)($|[[:space:]]) ]]; then
    OPENCLAW_VERSION_RESOLVED="${BASH_REMATCH[2]}"
  else
    echo "ERROR: Could not parse OpenClaw version from ${OPENCLAW_BIN_RESOLVED}: ${version_line:-<empty>}" >&2
    return 1
  fi
  if [[ "${OPENCLAW_VERSION_RESOLVED}" != "${REQUIRED_OPENCLAW_VERSION}" ]]; then
    echo "ERROR: OpenClaw ${OPENCLAW_VERSION_RESOLVED} at ${OPENCLAW_BIN_RESOLVED} is unsupported; need exactly ${REQUIRED_OPENCLAW_VERSION}." >&2
    return 1
  fi
  export OPENCLAW_BIN="${OPENCLAW_BIN_RESOLVED}"
  return 0
}

require_codex_runtime_exact() {
  local inspect_json plugin_version app_server_version app_server_path
  if ! inspect_json="$(run_openclaw_cli plugins inspect codex --json)"; then
    echo "ERROR: Could not inspect the required Codex plugin." >&2
    return 1
  fi
  if ! echo "${inspect_json}" | jq -e '
    .plugin.id == "codex"
    and .plugin.enabled == true
    and .plugin.status == "loaded"
  ' >/dev/null; then
    echo "ERROR: Required Codex plugin is not installed, enabled, and loaded." >&2
    echo "       Run bootstrap to reconcile the exact OpenClaw/Codex runtime tuple." >&2
    return 1
  fi
  plugin_version="$(echo "${inspect_json}" | jq -r '.plugin.version // empty')"
  app_server_version="$(echo "${inspect_json}" | jq -r '
    .plugin.dependencyStatus.dependencies[]?
    | select(.name == "@openai/codex")
    | .spec
  ' | head -n1)"
  app_server_path="$(echo "${inspect_json}" | jq -r '
    .plugin.dependencyStatus.dependencies[]?
    | select(.name == "@openai/codex")
    | .resolvedPath // empty
  ' | head -n1)"
  if [[ "${plugin_version}" != "${REQUIRED_CODEX_PLUGIN_VERSION}" ]]; then
    echo "ERROR: Codex plugin ${plugin_version:-<unknown>} is unsupported; need exactly ${REQUIRED_CODEX_PLUGIN_VERSION}." >&2
    echo "       Run bootstrap to reinstall the pinned plugin and gateway service." >&2
    return 1
  fi
  if [[ "${app_server_version}" != "${REQUIRED_CODEX_APP_SERVER_VERSION}" ]]; then
    echo "ERROR: Embedded @openai/codex ${app_server_version:-<unknown>} is unsupported; need exactly ${REQUIRED_CODEX_APP_SERVER_VERSION}." >&2
    echo "       Run bootstrap to reinstall the pinned plugin and gateway service." >&2
    return 1
  fi
  CODEX_APP_SERVER_CLI_RESOLVED="${app_server_path}/bin/codex.js"
  if [[ ! -f "${CODEX_APP_SERVER_CLI_RESOLVED}" ]]; then
    echo "ERROR: Embedded Codex CLI not found at ${CODEX_APP_SERVER_CLI_RESOLVED}." >&2
    return 1
  fi
  echo "Codex runtime validated: @openclaw/codex ${plugin_version} embeds @openai/codex ${app_server_version}"
}

ROLLBACK_ARMED=0
MANAGED_UNIT_TRANSACTION_ARMED=0
MANAGED_UNIT_BACKUP_DIR=""
MANAGED_UNIT_PATHS=(
  "${SUPERVISOR_UNIT_DST}"
  "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
  "${CODEX_RUNTIME_DROPIN_DST}"
  "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
)
MANAGED_UNIT_WAS_PRESENT=()

begin_managed_unit_transaction() {
  local index path backup_path

  MANAGED_UNIT_BACKUP_DIR="$(mktemp -d "${SYSTEMD_USER_DIR}/.push-openclaw-config-units.XXXXXX")"
  for index in "${!MANAGED_UNIT_PATHS[@]}"; do
    path="${MANAGED_UNIT_PATHS[${index}]}"
    backup_path="${MANAGED_UNIT_BACKUP_DIR}/${index}"
    if [[ -f "${path}" ]]; then
      cp -p "${path}" "${backup_path}"
      MANAGED_UNIT_WAS_PRESENT[${index}]=1
    else
      MANAGED_UNIT_WAS_PRESENT[${index}]=0
    fi
  done
  MANAGED_UNIT_TRANSACTION_ARMED=1
}

rollback_managed_unit_transaction() {
  local index path backup_path

  if [[ "${MANAGED_UNIT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    if [[ -n "${MANAGED_UNIT_BACKUP_DIR:-}" ]]; then
      rm -rf "${MANAGED_UNIT_BACKUP_DIR}"
    fi
    return
  fi

  echo "Restoring managed systemd files after failed publication." >&2
  for index in "${!MANAGED_UNIT_PATHS[@]}"; do
    path="${MANAGED_UNIT_PATHS[${index}]}"
    backup_path="${MANAGED_UNIT_BACKUP_DIR}/${index}"
    if [[ "${MANAGED_UNIT_WAS_PRESENT[${index}]:-0}" -eq 1 ]]; then
      if ! cp -p "${backup_path}" "${path}"; then
        echo "ERROR: Failed to restore managed systemd file ${path}." >&2
      fi
    elif ! rm -f "${path}"; then
      echo "ERROR: Failed to remove newly installed managed systemd file ${path}." >&2
    fi
  done
  if ! systemctl --user daemon-reload; then
    echo "ERROR: Failed to reload user systemd units after managed-file rollback." >&2
  fi
  rm -rf "${MANAGED_UNIT_BACKUP_DIR}"
  MANAGED_UNIT_BACKUP_DIR=""
  MANAGED_UNIT_TRANSACTION_ARMED=0
}

commit_managed_unit_transaction() {
  if [[ -n "${MANAGED_UNIT_BACKUP_DIR:-}" ]]; then
    rm -rf "${MANAGED_UNIT_BACKUP_DIR}"
  fi
  MANAGED_UNIT_BACKUP_DIR=""
  MANAGED_UNIT_TRANSACTION_ARMED=0
}

rollback_local_config_on_exit() {
  local exit_status=$?
  trap - EXIT
  rm -f "${SUPERVISOR_UNIT_TMP:-}"
  rm -f "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP:-}"
  rm -f "${CODEX_RUNTIME_DROPIN_TMP:-}"
  rm -f "${NATIVE_CRASH_HARDENING_DROPIN_TMP:-}"
  rollback_managed_unit_transaction
  if [[ "${ROLLBACK_ARMED:-0}" -eq 1 ]] && [[ "${exit_status}" -ne 0 ]]; then
    if ! cp "${BACKUP}" "${LOCAL_CONFIG}"; then
      echo "ERROR: Failed to restore backup ${BACKUP} to ${LOCAL_CONFIG} during rollback." >&2
    fi
  fi
  exit "${exit_status}"
}

# ── Pre-flight checks ───────────────────────────────────────────────────────
if ! require_openclaw_supported; then
  exit 1
fi
echo "Using OpenClaw: ${OPENCLAW_BIN_RESOLVED} (version ${OPENCLAW_VERSION_RESOLVED})"

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not found. Install it: https://jqlang.github.io/jq/" >&2
  exit 1
fi

MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON="$(build_string_array_json "${MEMPALACE_READONLY_SERVER_AGENT_IDS[@]}")"
G2_CONTROL_SERVER_AGENT_IDS_JSON="$(build_string_array_json "${G2_CONTROL_SERVER_AGENT_IDS[@]}")"
PM_NATIVE_CODEX_DELEGATION_DENY_IDS_JSON="$(build_string_array_json "${PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS[@]}")"
MAIN_OPENCLAW_TOOL_ALLOW_IDS_JSON="$(build_string_array_json "${MAIN_OPENCLAW_TOOL_ALLOW_IDS[@]}")"

if [[ ! -f "${REPO_CONFIG}" ]]; then
  echo "ERROR: Repo config not found at ${REPO_CONFIG}" >&2
  exit 1
fi

if [[ ! -f "${LOCAL_CONFIG}" ]]; then
  echo "ERROR: Local OpenClaw config not found at ${LOCAL_CONFIG}" >&2
  echo "       Run 'openclaw onboard' first." >&2
  exit 1
fi

# The quantipy-methodology skill is intentionally a thin pointer to the live
# Quantipy repo. Fail here if those source-of-truth files are unavailable.
REQUIRED_QUANTIPY_FILES=(
  "AGENTS.md"
  ".agents/skills/backend-python/SKILL.md"
  ".agents/skills/backtesting/SKILL.md"
  ".agents/skills/data-collection/SKILL.md"
  ".agents/skills/data-querying/SKILL.md"
  ".agents/skills/experiment-data/SKILL.md"
  ".codex/agents/backend-python.toml"
  ".codex/agents/contrarian.toml"
  ".codex/agents/explorer.toml"
  ".codex/agents/orchestrator.toml"
  ".codex/agents/researcher.toml"
  ".codex/agents/reviewer.toml"
  ".codex/agents/theorist.toml"
)
for FILE in "${REQUIRED_QUANTIPY_FILES[@]}"; do
  if [[ ! -f "${QUANTIPY_ROOT}/${FILE}" ]]; then
    echo "ERROR: Required Quantipy methodology file not found at ${QUANTIPY_ROOT}/${FILE}" >&2
    echo "       The quantipy-methodology skill depends on the live Quantipy repo; restore this file before pushing." >&2
    exit 1
  fi
done
echo "Verified Quantipy methodology source files in ${QUANTIPY_ROOT}"

if [[ ! -d "${SKILLS_SRC}" ]]; then
  echo "ERROR: Repo-managed skills directory not found at ${SKILLS_SRC}" >&2
  exit 1
fi

if [[ ! -f "${MEMPALACE_READONLY_WRAPPER_SRC}" ]]; then
  echo "ERROR: Repo-managed MemPalace read-only wrapper not found at ${MEMPALACE_READONLY_WRAPPER_SRC}" >&2
  exit 1
fi

if [[ ! -f "${SUPERVISOR_UNIT_TEMPLATE}" ]]; then
  echo "ERROR: Repo-managed supervisor unit template not found at ${SUPERVISOR_UNIT_TEMPLATE}" >&2
  exit 1
fi
if ! validate_supervisor_unit_file "${SUPERVISOR_UNIT_TEMPLATE}"; then
  exit 1
fi

if ! validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}"; then
  exit 1
fi
if [[ ! -x "${REPO_ROOT}/scripts/ensure-openclaw-codex-runtime.mjs" ]]; then
  echo "ERROR: Codex runtime verifier is missing or not executable at ${REPO_ROOT}/scripts/ensure-openclaw-codex-runtime.mjs" >&2
  exit 1
fi
if ! validate_codex_runtime_dropin_file "${CODEX_RUNTIME_DROPIN_SRC}"; then
  exit 1
fi
if ! validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_SRC}"; then
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Supervisor Python is missing or not executable at ${PYTHON_BIN}. Run 'uv sync' first." >&2
  exit 1
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "ERROR: The systemd user manager is unavailable; cannot install ${SUPERVISOR_SERVICE_NAME}." >&2
  exit 1
fi

if ! require_gateway_service_loadable; then
  exit 1
fi

if ! jq -e '
  [(.agents.defaults.skills // [])[], (.agents.list[]?.skills // [])[]]
  | all(.[]; type == "string" and length > 0)
' "${REPO_CONFIG}" >/dev/null; then
  echo "ERROR: Every configured skill in ${REPO_CONFIG} must be a non-empty string." >&2
  exit 1
fi

REQUIRED_REPO_SKILLS=(
  "autoresearch"
  "codex-subagents"
  "mempalace-readonly"
  "quantipy-methodology"
)

mapfile -t CONFIGURED_SKILLS < <(jq -r '
  [(.agents.defaults.skills // [])[], (.agents.list[]?.skills // [])[]]
  | map(select(type == "string" and length > 0))
  | unique[]
' "${REPO_CONFIG}")

declare -A REQUIRED_SKILL_FILES=()
for SKILL_NAME in "${REQUIRED_REPO_SKILLS[@]}" "${CONFIGURED_SKILLS[@]}"; do
  if [[ -n "${SKILL_NAME}" ]]; then
    REQUIRED_SKILL_FILES["${SKILL_NAME}"]="${SKILLS_SRC}/${SKILL_NAME}/SKILL.md"
  fi
done

mapfile -t SKILLS_TO_CHECK < <(printf '%s\n' "${!REQUIRED_SKILL_FILES[@]}" | sort)
MISSING_SKILL_FILES=()
for SKILL_NAME in "${SKILLS_TO_CHECK[@]}"; do
  if [[ ! -f "${REQUIRED_SKILL_FILES[${SKILL_NAME}]}" ]]; then
    MISSING_SKILL_FILES+=("${SKILL_NAME}")
  fi
done

if [[ "${#MISSING_SKILL_FILES[@]}" -gt 0 ]]; then
  echo "ERROR: Missing repo-managed skill definitions under ${SKILLS_SRC}:" >&2
  for SKILL_NAME in "${MISSING_SKILL_FILES[@]}"; do
    echo "       ${REQUIRED_SKILL_FILES[${SKILL_NAME}]}" >&2
  done
  echo "       Restore the missing skill directories before pushing OpenClaw config." >&2
  exit 1
fi
echo "Verified repo-managed skill definitions: ${SKILLS_TO_CHECK[*]}"

# ── Load env vars from .env ───────────────────────────────────────────────────
PRESERVE_ENV_VARS=(
  HOME
  PATH
  OPENCLAW_PUSH_HOME
  OPENCLAW_PROVIDER
  OPENAI_MODEL
  OPENROUTER_MODEL
  OPENROUTER_API_KEY
  AZURE_OAI_API_KEY
  FASTEMBED_CACHE_PATH
  HF_HUB_OFFLINE
  MEMPALACE_EMBEDDING_MODEL
  MEMPALACE_EXPECTED_EMBEDDING_MODEL
  MEMPALACE_EXPECTED_EMBEDDING_DIMENSION
)
declare -A PRESERVED_ENV=()
for VAR_NAME in "${PRESERVE_ENV_VARS[@]}"; do
  if [[ -v "${VAR_NAME}" ]]; then
    PRESERVED_ENV["${VAR_NAME}"]="${!VAR_NAME}"
  fi
done

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

for VAR_NAME in "${!PRESERVED_ENV[@]}"; do
  printf -v "${VAR_NAME}" '%s' "${PRESERVED_ENV[${VAR_NAME}]}"
  export "${VAR_NAME}"
done
unset OPENCLAW_HOME

if [[ "${OPENCLAW_PROVIDER:-codex}" == "openrouter" ]] && [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "ERROR: OPENROUTER_API_KEY is not set but OPENCLAW_PROVIDER=openrouter." >&2
  echo "       Set it in ${ENV_FILE} or export it before running this script." >&2
  exit 1
fi

if [[ "${OPENCLAW_PROVIDER:-codex}" == "codex" ]]; then
  echo "Running preflight: ${OPENCLAW_BIN_RESOLVED} plugins inspect codex --json"
  if ! require_codex_runtime_exact; then
    exit 1
  fi
fi

# ── Backup ───────────────────────────────────────────────────────────────────
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${LOCAL_CONFIG}.bak.${TIMESTAMP}"
cp "${LOCAL_CONFIG}" "${BACKUP}"
ROLLBACK_ARMED=1
trap 'rollback_local_config_on_exit' EXIT
echo "Backed up local config → ${BACKUP}"

# ── Deep merge ───────────────────────────────────────────────────────────────
# jq's * operator does recursive object merge (right wins on conflicts).
# We read the local config as base and overlay the repo config on top.
# Later provider selection installs a strict selected-model allowlist.
REPO_PRIMARY=$(jq -r '.agents.defaults.model.primary // empty' "${REPO_CONFIG}")
MERGED=$(jq -s --arg primary "${REPO_PRIMARY}" '
  .[0] * .[1]
  | del(.mcp)
' "${LOCAL_CONFIG}" "${REPO_CONFIG}")

# ── Resolve required MemPalace MCP server path ────────────────────────────────
MEMPALACE_VENV="${HOME}/.local/share/mempalace/venv"
MEMPALACE_PYTHON="${MEMPALACE_VENV}/bin/python"
MEMPALACE_PALACE="${HOME}/.mempalace/palace"
MEMPALACE_READONLY_WRAPPER_DST="${OPENCLAW_PUSH_HOME}/${MEMPALACE_READONLY_WRAPPER_BASENAME}"
MEMPALACE_EMBEDDING_MODEL="${MEMPALACE_EMBEDDING_MODEL:-bge-base}"
MEMPALACE_EXPECTED_EMBEDDING_MODEL="${MEMPALACE_EXPECTED_EMBEDDING_MODEL:-${MEMPALACE_EMBEDDING_MODEL}}"
MEMPALACE_EXPECTED_EMBEDDING_DIMENSION="${MEMPALACE_EXPECTED_EMBEDDING_DIMENSION:-768}"
FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-${HOME}/.cache/fastembed}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export FASTEMBED_CACHE_PATH MEMPALACE_EMBEDDING_MODEL
export MEMPALACE_EXPECTED_EMBEDDING_MODEL MEMPALACE_EXPECTED_EMBEDDING_DIMENSION
export HF_HUB_OFFLINE

if [[ ! -x "${MEMPALACE_PYTHON}" ]]; then
  echo "ERROR: MemPalace is required at ${MEMPALACE_VENV}." >&2
  echo "       Run 'make mempalace-install' before pushing OpenClaw config." >&2
  exit 1
fi

if ! "${MEMPALACE_PYTHON}" -c 'import mempalace.mcp_server' >/dev/null 2>&1; then
  echo "ERROR: MemPalace is installed but the MCP server module cannot be imported." >&2
  echo "       Run 'make mempalace-install' to upgrade/reinstall MemPalace." >&2
  exit 1
fi

mkdir -p "${MEMPALACE_PALACE}"
mkdir -p "${FASTEMBED_CACHE_PATH}"

if ! "${MEMPALACE_PYTHON}" "${REPO_ROOT}/scripts/check-mempalace-health.py"; then
  echo "ERROR: MemPalace healthcheck failed. Refusing to push OpenClaw config." >&2
  echo "       Fix the palace explicitly; startup will not auto-repair or fall back." >&2
  exit 1
fi

mkdir -p "${OPENCLAW_PUSH_HOME}"
cp "${MEMPALACE_READONLY_WRAPPER_SRC}" "${MEMPALACE_READONLY_WRAPPER_DST}"
echo "Installed MemPalace read-only wrapper → ${MEMPALACE_READONLY_WRAPPER_DST}"

MERGED=$(echo "${MERGED}" | jq \
  --arg cmd "${MEMPALACE_PYTHON}" \
  --arg palace "${MEMPALACE_PALACE}" \
  --arg wrapper "${MEMPALACE_READONLY_WRAPPER_DST}" \
  --arg cache "${FASTEMBED_CACHE_PATH}" \
  --arg model "${MEMPALACE_EMBEDDING_MODEL}" \
  --arg offline "${HF_HUB_OFFLINE}" \
  --arg repo "${REPO_ROOT}" \
  --arg python "${PYTHON_BIN}" \
  --arg g2_module "${G2_CONTROL_MCP_MODULE}" \
  --argjson readonly_agents "${MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON}" \
  --argjson g2_agents "${G2_CONTROL_SERVER_AGENT_IDS_JSON}" '
  .mcp.servers = {
    "mempalace-readonly": {
      "command": $cmd,
      "args": [$wrapper, "--palace", $palace],
      "codex": {
        "agents": $readonly_agents
      },
      "env": {
        "FASTEMBED_CACHE_PATH": $cache,
        "MEMPALACE_EMBEDDING_MODEL": $model,
        "HF_HUB_OFFLINE": $offline
      }
    },
    "g2-control": {
      "command": $python,
      "args": ["-m", $g2_module],
      "codex": {
        "agents": $g2_agents,
        "defaultToolsApprovalMode": "approve"
      },
      "env": {
        "PYTHONPATH": $repo
      }
    }
  }
')
echo "Resolved read-only MemPalace MCP wrapper: ${MEMPALACE_READONLY_WRAPPER_DST}"
echo "Resolved MemPalace embedding: ${MEMPALACE_EMBEDDING_MODEL} (cache: ${FASTEMBED_CACHE_PATH})"

# ── Force-set tools section from repo config ─────────────────────────────────
# Deep merge preserves stale keys (e.g. tools.allow from a previous push).
# Overwrite the entire tools section with the repo's version to avoid drift.
REPO_TOOLS=$(jq '.tools // empty' "${REPO_CONFIG}")
if [[ -n "${REPO_TOOLS}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --argjson tools "${REPO_TOOLS}" '.tools = $tools')
fi

# ── Force-set memory section from repo config ────────────────────────────────
# Deep merge preserves stale keys from old memory schemas.
# Overwrite the entire memory section with the repo's version.
REPO_MEMORY=$(jq '.memory // empty' "${REPO_CONFIG}")
if [[ -n "${REPO_MEMORY}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --argjson memory "${REPO_MEMORY}" '.memory = $memory')
fi

# ── Force-set disabled built-in agent memory controls ────────────────────────
# Recursive merge keeps stale vector-search/session-memory children even when
# memorySearch.enabled is false. Replace the managed memory controls exactly so
# MemPalace remains the only durable research memory layer.
REPO_AGENT_MEMORY_SEARCH=$(jq '.agents.defaults.memorySearch // empty' "${REPO_CONFIG}")
if [[ -n "${REPO_AGENT_MEMORY_SEARCH}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --argjson memory_search "${REPO_AGENT_MEMORY_SEARCH}" '
    .agents.defaults.memorySearch = $memory_search
  ')
fi

REPO_AGENT_MEMORY_FLUSH=$(jq '.agents.defaults.compaction.memoryFlush // empty' "${REPO_CONFIG}")
if [[ -n "${REPO_AGENT_MEMORY_FLUSH}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --argjson memory_flush "${REPO_AGENT_MEMORY_FLUSH}" '
    .agents.defaults.compaction.memoryFlush = $memory_flush
  ')
fi

# ── Force-set managed agent roster ──────────────────────────────────────────
# Autoresearch stage models, skills, native Codex delegation guards, and tool denies are
# repo-owned. Replace the local roster so hand edits in ~/.openclaw cannot alter
# the loop topology or silently change stage models.
REPO_AGENTS_LIST=$(jq '.agents.list // empty' "${REPO_CONFIG}")
if [[ -n "${REPO_AGENTS_LIST}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --argjson agents_list "${REPO_AGENTS_LIST}" '
    .agents.list = $agents_list
  ')
fi

# ── Resolve env: references in provider apiKey fields ────────────────────────
# The repo config uses "env:VAR_NAME" placeholders for secrets. OpenClaw does
# NOT resolve these natively for custom provider apiKey fields — the literal
# string is passed to the SDK. We must substitute the actual value here.
# Note: Azure OpenAI uses Entra ID auth (injected by the preload) — no apiKey needed.
if [[ "${OPENCLAW_PROVIDER:-codex}" == "openrouter" ]] && [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --arg key "${OPENROUTER_API_KEY}" '
    (.models.providers // {}) |= with_entries(
      if .value.apiKey == "env:OPENROUTER_API_KEY" then
        .value.apiKey = $key
      else . end
    )
  ')
  echo "Resolved env:OPENROUTER_API_KEY (${#OPENROUTER_API_KEY} chars)."
fi

# ── Propagate Azure API key to all azure-oai-* providers ─────────────────────
# The primary azure-oai-g2 provider gets an apiKey during onboarding or from
# the local config. Secondary Azure providers (e.g., azure-oai-g2-mini) share
# the same Azure resource and need the same key. The Entra preload overrides
# the key at HTTP time with a bearer token — the stored key just passes
# OpenClaw's internal provider auth validation.
MERGED=$(echo "${MERGED}" | jq '
  (.models.providers // {}) as $provs |
  ($provs | to_entries | map(select(.key | startswith("azure-oai-"))) | map(select(.value.apiKey != null and .value.apiKey != "")) | .[0].value.apiKey // null) as $azureKey |
  if $azureKey != null then
    .models.providers |= with_entries(
      if (.key | startswith("azure-oai-")) and (.value.apiKey == null or .value.apiKey == "") then
        .value.apiKey = $azureKey
      else . end
    )
  else . end
')

# ── Provider selection ───────────────────────────────────────────────────────
PROVIDER="${OPENCLAW_PROVIDER:-codex}"
case "${PROVIDER}" in
  codex)
    MODEL_PRIMARY="openai/${OPENAI_MODEL:-gpt-5.4}"
    MODEL_PROVIDER="openai"
    MODEL_ID="${OPENAI_MODEL:-gpt-5.4}"
    ;;
  azure)
    MODEL_PRIMARY="azure-oai-g2/gpt-5.4"
    MODEL_PROVIDER="azure-oai-g2"
    MODEL_ID="gpt-5.4"
    ;;
  openrouter)
    MODEL_PRIMARY="openrouter/${OPENROUTER_MODEL:-anthropic/claude-sonnet-4-20250514}"
    MODEL_PROVIDER="openrouter"
    MODEL_ID="${OPENROUTER_MODEL:-anthropic/claude-sonnet-4-20250514}"
    ;;
  *)
    echo "ERROR: Unknown OPENCLAW_PROVIDER '${PROVIDER}'. Use 'codex', 'azure', or 'openrouter'." >&2
    exit 1
    ;;
esac

if ! echo "${MERGED}" | jq -e --arg provider "${MODEL_PROVIDER}" --arg model "${MODEL_ID}" '
  any(.models.providers[$provider].models[]?; .id == $model)
' >/dev/null; then
  echo "ERROR: Selected model '${MODEL_PRIMARY}' is not declared in repo config." >&2
  echo "       Add it to gateway/openclaw_config/openclaw.json or choose a configured model." >&2
  exit 1
fi

PM_MODEL_PRIMARY=$(jq -r '.agents.list[] | select(.id == "autoresearch-pm") | .model.primary // empty' "${REPO_CONFIG}")
if [[ -z "${PM_MODEL_PRIMARY}" ]]; then
  echo "ERROR: Repo config must pin agents.list[].id == \"autoresearch-pm\" to a model.primary." >&2
  exit 1
fi
PM_MODEL_ID="${PM_MODEL_PRIMARY#openai/}"
if [[ "${PM_MODEL_PRIMARY}" != openai/* ]]; then
  echo "ERROR: PM model '${PM_MODEL_PRIMARY}' must use the OpenAI/Codex provider." >&2
  exit 1
fi
if ! echo "${MERGED}" | jq -e --arg model "${PM_MODEL_ID}" '
  any(.models.providers.openai.models[]?; .id == $model)
' >/dev/null; then
  echo "ERROR: PM model '${PM_MODEL_PRIMARY}' is not declared in repo config." >&2
  echo "       Add it to gateway/openclaw_config/openclaw.json before pushing." >&2
  exit 1
fi

MERGED=$(echo "${MERGED}" | jq --arg primary "${MODEL_PRIMARY}" '
  .agents.defaults.model.primary = $primary
  | .agents.defaults.models = { ($primary): {} }
')

MERGED=$(echo "${MERGED}" | jq --arg pm "${PM_MODEL_PRIMARY}" '
  (.agents.list[] | select(.id == "autoresearch-pm") | .model.primary) = $pm
  | (.agents.list[] | select(.id == "autoresearch-pm") | .thinkingDefault) = "high"
')

sanitize_stale_coding_provider_keys

MERGED=$(echo "${MERGED}" | jq '
  del(.plugins.entries.codex.config.codexDynamicToolsExclude)
  | del(.plugins.entries.codex.config.nativeToolSurfaceEnabled)
')

echo "Active provider: ${PROVIDER} → default model: ${MODEL_PRIMARY}; PM model: ${PM_MODEL_PRIMARY}"

# No model thread is projected a write-capable MemPalace server. The platform
# finalizer is the sole write boundary, so stage tool-deny compatibility lists
# must not survive in the managed config.
if ! echo "${MERGED}" | jq -e \
  --argjson pm_native_codex_denies "${PM_NATIVE_CODEX_DELEGATION_DENY_IDS_JSON}" \
  --argjson readonly_agents "${MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON}" '
  def denies: (.tools.deny // []);
  def is_stage: (.id != "main" and .id != "autoresearch-pm");
  ([.agents.list[] | select(.id == "autoresearch-pm") | select(denies == $pm_native_codex_denies)] | length) == 1
  and
  ([.agents.list[] | select(is_stage) | select(.tools? != null)] | length) == 0
' >/dev/null; then
  echo "ERROR: Autoresearch models must expose only read-only MemPalace and no stage write-tool remnants." >&2
  exit 1
fi

# ── Managed invariant validation ─────────────────────────────────────────────
# Fail before writing if a local merge or env selection would violate the
# repo-managed autoresearch target shape.
if ! echo "${MERGED}" | jq -e \
  --arg pm "${PM_MODEL_PRIMARY}" \
  --arg cmd "${MEMPALACE_PYTHON}" \
  --arg palace "${MEMPALACE_PALACE}" \
  --arg wrapper "${MEMPALACE_READONLY_WRAPPER_DST}" \
  --arg cache "${FASTEMBED_CACHE_PATH}" \
  --arg model "${MEMPALACE_EMBEDDING_MODEL}" \
  --arg offline "${HF_HUB_OFFLINE}" \
  --arg repo "${REPO_ROOT}" \
  --arg python "${PYTHON_BIN}" \
  --arg g2_module "${G2_CONTROL_MCP_MODULE}" \
  --argjson readonly_server_agents "${MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON}" \
  --argjson g2_server_agents "${G2_CONTROL_SERVER_AGENT_IDS_JSON}" \
  --argjson pm_native_codex_denies "${PM_NATIVE_CODEX_DELEGATION_DENY_IDS_JSON}" \
  --argjson main_openclaw_allow "${MAIN_OPENCLAW_TOOL_ALLOW_IDS_JSON}" '
  def denies: (.tools.deny // []);
  def main_allow: (.tools.allow // []);
  def is_stage: (.id != "main" and .id != "autoresearch-pm");
  def expected_models: {
    "main": "openai/gpt-5.4",
    "autoresearch-pm": $pm,
    "context_curator": "openai/gpt-5.4",
    "debater_microstructure": "openai/gpt-5.5",
    "debater_data": "openai/gpt-5.6-terra",
    "debater_skeptic": "openai/gpt-5.5",
    "debater_theory": "openai/gpt-5.4",
    "debater_implementation": "openai/gpt-5.4",
    "consensus_arbiter": "openai/gpt-5.6-sol",
    "implementer": "openai/gpt-5.4",
    "reviewer": "openai/gpt-5.6-sol",
    "fixer": "openai/gpt-5.4"
  };
  (.agents.defaults.thinkingDefault == "high")
  and ((.plugins.allow // []) | contains(["codex"]))
  and (.plugins.entries.codex.enabled == true)
  and (.plugins.entries.codex.config.nativeToolSurfaceEnabled? == null)
  and (.plugins.entries.codex.config.codexDynamicToolsExclude? == null)
  and (.plugins.entries.codex.config.appServer.sandbox == "workspace-write")
  and (.plugins.entries.codex.config.appServer.sandbox != "danger-full-access")
  and (.plugins.entries.codex.config.appServer.defaultWorkspaceDir == "/home/dev/.openclaw/autoresearch/model-workspaces")
  and (.plugins.entries.codex.config.appServer.networkProxy? == null)
  and (.agents.defaults.maxConcurrent == 2)
  and (.agents.defaults.subagents.maxConcurrent == 1)
  and (.agents.defaults.subagents.maxChildrenPerAgent? == null)
  and (.agents.defaults.memorySearch.enabled == false)
  and (.agents.defaults.compaction.mode == "default")
  and (.agents.defaults.compaction.memoryFlush.enabled == false)
  and ((.tools.deny // []) | contains(["memory_search", "memory_get"]))
  and ((.mcp.servers | keys | sort) == (["g2-control", "mempalace-readonly"] | sort))
  and (.mcp.servers."mempalace-readonly".command == $cmd)
  and (.mcp.servers."mempalace-readonly".args == [$wrapper, "--palace", $palace])
  and ((.mcp.servers."mempalace-readonly".codex.agents // []) == $readonly_server_agents)
  and (.mcp.servers."mempalace-readonly".env == {
    "FASTEMBED_CACHE_PATH": $cache,
    "MEMPALACE_EMBEDDING_MODEL": $model,
    "HF_HUB_OFFLINE": $offline
  })
  and (.mcp.servers."g2-control".command == $python)
  and (.mcp.servers."g2-control".args == ["-m", $g2_module])
  and ((.mcp.servers."g2-control".codex.agents // []) == $g2_server_agents)
  and (.mcp.servers."g2-control".codex.defaultToolsApprovalMode == "approve")
  and (.mcp.servers."g2-control".env == {"PYTHONPATH": $repo})
  and (([.agents.list[].id] | sort) == (expected_models | keys | sort))
  and all(.agents.list[]; .model.primary == expected_models[.id])
  and all(.agents.list[]; .thinkingDefault == "high")
  and ([.agents.list[] | select(.id == "main" and .tools.profile == "minimal" and main_allow == $main_openclaw_allow and (denies | contains(["exec", "sessions_spawn", "sessions_yield", "sessions_send", "sessions_list", "sessions_history", "agents_list"])))] | length) == 1
  and ([.agents.list[] | select(
    .id == "autoresearch-pm"
    and .model.primary == $pm
    and .thinkingDefault == "high"
    and ((.skills // []) == ["mempalace-readonly", "autoresearch"])
    and denies == $pm_native_codex_denies
    and (((.subagents.allowAgents? // []) | length) == 0)
  )] | length) == 1
  and ([.agents.list[] | select(
    .id == "main"
    and .model.primary == "openai/gpt-5.4"
    and .thinkingDefault == "high"
    and (((.skills // []) | index("mempalace")) == null)
    and (((.skills // []) | index("autoresearch")) == null)
    and ((.skills // []) == ["mempalace-readonly"])
    and (((.subagents.allowAgents? // []) | length) == 0)
  )] | length) == 1
  and ([.agents.list[] | select((.subagents.allowAgents? // []) | length > 0)] | length) == 0
  and ([.agents.list[] | select(((.skills // []) | index("mempalace-readonly")) == null)] | length) == 0
  and ([.agents.list[] | select(.id != "autoresearch-pm") | select(((.skills // []) | index("autoresearch")) != null)] | length) == 0
  and ([.agents.list[] | select(is_stage) | select(((.skills // []) | index("quantipy-methodology")) == null)] | length) == 0
  and ([.agents.list[] | select(.id != "autoresearch-pm" and .id != "main") | select(.tools? != null)] | length) == 0
' >/dev/null; then
  echo "ERROR: Generated OpenClaw config violates repo-managed autoresearch invariants." >&2
  echo "       Check plugins.allow, Codex app-server config schema, autoresearch-pm model/skills/native Codex delegation denies, main interface restrictions, strict concurrency caps, read-only MemPalace projection, and stage skill scopes." >&2
  exit 1
fi
echo "Managed invariants validated: main interface split, read-only-only MemPalace projection, autoresearch-pm model and native Codex delegation denies, exact stage models, high reasoning, strict concurrency caps, Quantipy methodology skill, built-in memory disabled."

validate_generated_openclaw_config() {
  local temp_config validate_json validate_status
  temp_config="$(mktemp "${OPENCLAW_PUSH_HOME}/.openclaw.generated.XXXXXX.json")"
  printf '%s\n' "${MERGED}" | jq . > "${temp_config}"
  if validate_json="$(run_openclaw_cli_for_config "${temp_config}" config validate --json 2>&1)"; then
    validate_status=0
  else
    validate_status=$?
  fi
  rm -f "${temp_config}"
  if [[ "${validate_status}" -ne 0 ]] || ! printf '%s\n' "${validate_json}" | jq -e '.valid == true' >/dev/null 2>&1; then
    echo "ERROR: Generated OpenClaw config failed schema validation before write." >&2
    printf '%s\n' "${validate_json}" >&2
    exit 1
  fi
  echo "Generated OpenClaw config schema validated with ${OPENCLAW_BIN_RESOLVED} config validate --json."
}

validate_generated_openclaw_config

# ── Write merged config ─────────────────────────────────────────────────────
echo "${MERGED}" | jq . > "${LOCAL_CONFIG}"
echo "Merged repo config into ${LOCAL_CONFIG}"

# ── Copy bootstrap files ────────────────────────────────────────────────────
# OpenClaw uses ~/.openclaw/workspace for the main agent when no workspace is
# configured. Other agents default to workspace-{agent_id}. An explicit
# .workspace value is used as-is relative to OPENCLAW_PUSH_HOME unless it is absolute.
BOOTSTRAP_FILES=(AGENTS.md SOUL.md TOOLS.md BOOTSTRAP.md)
for FILE in "${BOOTSTRAP_FILES[@]}"; do
  SRC="${REPO_ROOT}/gateway/agent_config/${FILE}"
  if [[ ! -f "${SRC}" ]]; then
    echo "ERROR: Required repo bootstrap file not found at ${SRC}" >&2
    exit 1
  fi
done

workspace_dir_for_target() {
  local workspace_target="$1"
  if [[ "${workspace_target}" == /* ]]; then
    printf '%s\n' "${workspace_target}"
  elif [[ "${workspace_target}" == "__OPENCLAW_DEFAULT_WORKSPACE__" ]]; then
    printf '%s/workspace\n' "${OPENCLAW_PUSH_HOME}"
  else
    printf '%s/%s\n' "${OPENCLAW_PUSH_HOME}" "${workspace_target}"
  fi
}

workspace_has_autoresearch_agent() {
  local agents_csv="$1"
  local agent
  IFS=',' read -ra agent_names <<< "${agents_csv}"
  for agent in "${agent_names[@]}"; do
    if [[ "${agent}" != "main" ]]; then
      return 0
    fi
  done
  return 1
}

validate_codex_native_stage_agents_dir() {
  local agents_dir="$1"
  "${PYTHON_BIN}" - "${agents_dir}" <<'PY'
import sys
import tomllib
from pathlib import Path

agents_dir = Path(sys.argv[1])
expected = {
    "context_curator": "gpt-5.4",
    "debater_microstructure": "gpt-5.5",
    "debater_data": "gpt-5.6-terra",
    "debater_skeptic": "gpt-5.5",
    "debater_theory": "gpt-5.4",
    "debater_implementation": "gpt-5.4",
    "consensus_arbiter": "gpt-5.6-sol",
    "implementer": "gpt-5.4",
    "reviewer": "gpt-5.6-sol",
    "fixer": "gpt-5.4",
}

if not agents_dir.is_dir():
    raise SystemExit(f"missing native Codex agents directory: {agents_dir}")
for name, model in expected.items():
    path = agents_dir / f"{name}.toml"
    if not path.is_file():
        raise SystemExit(f"missing native Codex stage agent: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"invalid native Codex stage agent TOML {path}: {exc}") from exc
    if data.get("name") != name:
        raise SystemExit(f"native Codex stage agent {path} must be named {name}")
    if data.get("model") != model:
        raise SystemExit(f"native Codex stage agent {name} must use {model}")
    if data.get("model_reasoning_effort") != "high":
        raise SystemExit(f"native Codex stage agent {name} must use high reasoning")
    if "mcp_servers" in data:
        raise SystemExit(f"native Codex stage agent {name} must not override inherited MCP servers")
PY
}

write_codex_runtime_config() {
  local codex_home="$1"
  local agent_id="$2"
  mkdir -p "${codex_home}"
  CODEX_RUNTIME_AGENT_ID="${agent_id}" \
  CODEX_RUNTIME_CONFIG_PATH="${codex_home}/config.toml" \
  CODEX_RUNTIME_MEMPALACE_PYTHON="${MEMPALACE_PYTHON}" \
  CODEX_RUNTIME_MEMPALACE_WRAPPER="${MEMPALACE_READONLY_WRAPPER_DST}" \
  CODEX_RUNTIME_MEMPALACE_PALACE="${MEMPALACE_PALACE}" \
  CODEX_RUNTIME_FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH}" \
  CODEX_RUNTIME_MEMPALACE_EMBEDDING_MODEL="${MEMPALACE_EMBEDDING_MODEL}" \
  CODEX_RUNTIME_HF_HUB_OFFLINE="${HF_HUB_OFFLINE}" \
  CODEX_RUNTIME_G2_PYTHON="${PYTHON_BIN}" \
  CODEX_RUNTIME_G2_MODULE="${G2_CONTROL_MCP_MODULE}" \
  CODEX_RUNTIME_REPO_ROOT="${REPO_ROOT}" \
  "${PYTHON_BIN}" <<'PY'
import json
import os
from pathlib import Path


def quoted(value: str) -> str:
    return json.dumps(value)


def array(values: list[str]) -> str:
    return "[" + ", ".join(quoted(value) for value in values) + "]"


agent_id = os.environ["CODEX_RUNTIME_AGENT_ID"]
config_path = Path(os.environ["CODEX_RUNTIME_CONFIG_PATH"])
mempalace_server = {
    "command": os.environ["CODEX_RUNTIME_MEMPALACE_PYTHON"],
    "args": [
        os.environ["CODEX_RUNTIME_MEMPALACE_WRAPPER"],
        "--palace",
        os.environ["CODEX_RUNTIME_MEMPALACE_PALACE"],
    ],
    "env": {
        "FASTEMBED_CACHE_PATH": os.environ["CODEX_RUNTIME_FASTEMBED_CACHE_PATH"],
        "MEMPALACE_EMBEDDING_MODEL": os.environ["CODEX_RUNTIME_MEMPALACE_EMBEDDING_MODEL"],
        "HF_HUB_OFFLINE": os.environ["CODEX_RUNTIME_HF_HUB_OFFLINE"],
    },
}
servers = {"mempalace-readonly": mempalace_server}
if agent_id == "main":
    servers["g2-control"] = {
        "command": os.environ["CODEX_RUNTIME_G2_PYTHON"],
        "args": ["-m", os.environ["CODEX_RUNTIME_G2_MODULE"]],
        "env": {"PYTHONPATH": os.environ["CODEX_RUNTIME_REPO_ROOT"]},
        "default_tools_approval_mode": "approve",
    }

lines = [
    'approval_policy = "never"',
    'sandbox_mode = "workspace-write"',
    "",
    "[sandbox_workspace_write]",
    "network_access = true",
    'writable_roots = ["/home/dev/.openclaw/autoresearch/model-workspaces", "/home/dev/.openclaw/autoresearch/stage-inbox"]',
    "exclude_tmpdir_env_var = false",
    "exclude_slash_tmp = false",
    "",
]
for server_name in sorted(servers):
    server = servers[server_name]
    lines.extend(
        [
            f"[mcp_servers.{quoted(server_name)}]",
            f"command = {quoted(server['command'])}",
            f"args = {array(server['args'])}",
        ]
    )
    default_tools_approval_mode = server.get("default_tools_approval_mode")
    if default_tools_approval_mode is not None:
        lines.append(f"default_tools_approval_mode = {quoted(default_tools_approval_mode)}")
    env = server.get("env", {})
    if env:
        lines.append(f"[mcp_servers.{quoted(server_name)}.env]")
        for key in sorted(env):
            lines.append(f"{key} = {quoted(env[key])}")
    lines.append("")
config_path.write_text("\n".join(lines), encoding="utf-8")
PY
}

validate_codex_runtime_config() {
  local codex_home="$1"
  local agent_id="$2"
  local config_path="${codex_home}/config.toml"
  "${PYTHON_BIN}" - "${config_path}" "${agent_id}" \
    "${MEMPALACE_PYTHON}" "${MEMPALACE_READONLY_WRAPPER_DST}" "${MEMPALACE_PALACE}" \
    "${PYTHON_BIN}" "${G2_CONTROL_MCP_MODULE}" "${REPO_ROOT}" <<'PY'
import sys
import tomllib
from pathlib import Path

config_path = Path(sys.argv[1])
agent_id = sys.argv[2]
mempalace_python = sys.argv[3]
mempalace_wrapper = sys.argv[4]
mempalace_palace = sys.argv[5]
g2_python = sys.argv[6]
g2_module = sys.argv[7]
repo_root = sys.argv[8]
data = tomllib.loads(config_path.read_text(encoding="utf-8"))
if data.get("approval_policy") != "never":
    raise SystemExit("Codex runtime config must set approval_policy=never")
if data.get("sandbox_mode") != "workspace-write":
    raise SystemExit("Codex runtime config must set sandbox_mode=workspace-write")
workspace = data.get("sandbox_workspace_write")
if not isinstance(workspace, dict):
    raise SystemExit("Codex runtime config missing [sandbox_workspace_write]")
if workspace.get("network_access") is not True:
    raise SystemExit("Codex runtime config must enable network_access for localhost Quantipy HTTP")
if workspace.get("writable_roots") != [
    "/home/dev/.openclaw/autoresearch/model-workspaces",
    "/home/dev/.openclaw/autoresearch/stage-inbox",
]:
    raise SystemExit("Codex runtime config must scope writable_roots to model workspace and stage inbox only")
if "permissions" in data or "default_permissions" in data or "network_proxy" in data:
    raise SystemExit("Codex runtime config must not use unsupported permissions/network_proxy profiles")
servers = data.get("mcp_servers")
if not isinstance(servers, dict):
    raise SystemExit("Codex runtime config must define direct mcp_servers")
expected_names = {"mempalace-readonly", "g2-control"} if agent_id == "main" else {"mempalace-readonly"}
if set(servers) != expected_names:
    raise SystemExit(f"Codex runtime {agent_id} has wrong direct MCP server set: {sorted(servers)}")
readonly = servers["mempalace-readonly"]
if readonly.get("command") != mempalace_python or readonly.get("args") != [
    mempalace_wrapper,
    "--palace",
    mempalace_palace,
]:
    raise SystemExit("Codex runtime MemPalace MCP command is not exact")
readonly_env = readonly.get("env")
if not isinstance(readonly_env, dict) or set(readonly_env) != {
    "FASTEMBED_CACHE_PATH",
    "HF_HUB_OFFLINE",
    "MEMPALACE_EMBEDDING_MODEL",
}:
    raise SystemExit("Codex runtime MemPalace MCP env is not exact")
if agent_id == "main":
    g2 = servers["g2-control"]
    if g2.get("command") != g2_python or g2.get("args") != ["-m", g2_module]:
        raise SystemExit("Codex runtime g2-control MCP command is not exact")
    if g2.get("default_tools_approval_mode") != "approve":
        raise SystemExit("Codex runtime g2-control MCP approval mode is not exact")
    if g2.get("env") != {"PYTHONPATH": repo_root}:
        raise SystemExit("Codex runtime g2-control MCP env is not exact")
PY
  repair_codex_runtime_log_db "${codex_home}"
  validate_codex_doctor_owned_checks "${codex_home}" "${config_path}"
}

repair_codex_runtime_log_db() {
  local codex_home="$1"
  local log_db="${codex_home}/logs_2.sqlite"
  local repair_output

  if ! repair_output="$("${PYTHON_BIN}" - "${log_db}" <<'PY' 2>&1
import os
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

log_db = Path(sys.argv[1])
expected_schema = {
    (
        "index",
        "idx_logs_process_uuid_threadless_ts",
        "logs",
        "CREATE INDEX idx_logs_process_uuid_threadless_ts ON logs(process_uuid, ts DESC, ts_nanos DESC, id DESC)\nWHERE thread_id IS NULL",
    ),
    (
        "index",
        "idx_logs_thread_id",
        "logs",
        "CREATE INDEX idx_logs_thread_id ON logs(thread_id)",
    ),
    (
        "index",
        "idx_logs_thread_id_ts",
        "logs",
        "CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id, ts DESC, ts_nanos DESC, id DESC)",
    ),
    (
        "index",
        "idx_logs_ts",
        "logs",
        "CREATE INDEX idx_logs_ts ON logs(ts DESC, ts_nanos DESC, id DESC)",
    ),
    (
        "table",
        "logs",
        "logs",
        """CREATE TABLE logs (
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
)""",
    ),
}
index_only_patterns = (
    re.compile(r"^row [0-9]+ missing from index idx_logs_thread_id$"),
    re.compile(r"^wrong # of entries in index idx_logs_thread_id$"),
)


def run_sql(sql: str) -> str:
    completed = subprocess.run(
        ["sqlite3", str(log_db), sql],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def run_sql_json(sql: str) -> object:
    completed = subprocess.run(
        ["sqlite3", "-json", str(log_db), sql],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
    try:
        return json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid sqlite JSON output for {log_db}: {exc}") from exc


def file_identity() -> tuple[int, int] | None:
    try:
        st = os.lstat(log_db)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit(f"Scoped Codex log DB must not be a symlink: {log_db}")
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(f"Scoped Codex log DB must be a regular file: {log_db}")
    if st.st_nlink != 1:
        raise SystemExit(
            f"Scoped Codex log DB must not have hard links; st_nlink={st.st_nlink}: {log_db}"
        )
    if st.st_uid != os.geteuid():
        raise SystemExit(
            f"Scoped Codex log DB owner uid {st.st_uid} does not match current uid {os.geteuid()}: {log_db}"
        )
    return (st.st_dev, st.st_ino)


def schema_rows() -> set[tuple[str, str, str, str]]:
    raw = run_sql_json(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE tbl_name = 'logs' AND type IN ('table', 'index') "
        "ORDER BY type, name;"
    )
    if not isinstance(raw, list):
        raise SystemExit(f"unexpected logs_2.sqlite schema JSON for {log_db}")
    rows: set[tuple[str, str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise SystemExit(f"unexpected logs_2.sqlite schema row: {item!r}")
        values = (item.get("type"), item.get("name"), item.get("tbl_name"), item.get("sql"))
        if not all(isinstance(value, str) for value in values):
            raise SystemExit(f"unexpected logs_2.sqlite schema row: {item!r}")
        rows.add(values)  # type: ignore[arg-type]
    return rows


def validate_schema() -> None:
    actual = schema_rows()
    if actual != expected_schema:
        missing = sorted(expected_schema - actual)
        extra = sorted(actual - expected_schema)
        raise SystemExit(
            f"Scoped Codex log DB schema does not match the pinned logs_2.sqlite schema at {log_db}; "
            f"missing={missing!r}; extra={extra!r}"
        )


def parse_integrity(output: str) -> list[str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise SystemExit(f"empty PRAGMA integrity_check output for {log_db}")
    return lines


def is_repairable_idx_logs_thread_id_only(lines: list[str]) -> bool:
    return all(any(pattern.fullmatch(line) for pattern in index_only_patterns) for line in lines)


identity_before = file_identity()
if identity_before is None:
    raise SystemExit(0)
validate_schema()
integrity_before = parse_integrity(run_sql("PRAGMA integrity_check;"))
if integrity_before == ["ok"]:
    raise SystemExit(0)
if not is_repairable_idx_logs_thread_id_only(integrity_before):
    raise SystemExit(
        f"Scoped Codex log DB {log_db} has non-repairable integrity errors: {integrity_before!r}"
    )

print(f"Repairing scoped Codex log DB idx_logs_thread_id with REINDEX: {log_db}")
integrity_after = parse_integrity(
    run_sql("REINDEX idx_logs_thread_id; PRAGMA integrity_check;")
)
if integrity_after != ["ok"]:
    raise SystemExit(
        f"Scoped Codex log DB {log_db} remains corrupt after REINDEX idx_logs_thread_id: {integrity_after!r}"
    )
if file_identity() != identity_before:
    raise SystemExit(f"Scoped Codex log DB identity changed during repair: {log_db}")
validate_schema()
print(f"Repaired scoped Codex log DB idx_logs_thread_id: {log_db}")
PY
)"; then
    echo "ERROR: Scoped Codex log DB validation/repair failed for ${log_db}." >&2
    printf '%s\n' "${repair_output}" >&2
    exit 1
  fi
  if [[ -n "${repair_output}" ]]; then
    printf '%s\n' "${repair_output}"
  fi
}

validate_codex_doctor_owned_checks() {
  local codex_home="$1"
  local config_path="$2"
  local doctor_stdout doctor_stderr doctor_status app_server_package_root

  doctor_stdout="$(mktemp)"
  doctor_stderr="$(mktemp)"
  app_server_package_root="$(dirname "$(dirname "${CODEX_APP_SERVER_CLI_RESOLVED}")")"
  if env -u NODE_OPTIONS CODEX_HOME="${codex_home}" \
    node "${CODEX_APP_SERVER_CLI_RESOLVED}" --strict-config doctor --json \
    >"${doctor_stdout}" 2>"${doctor_stderr}"; then
    doctor_status=0
  else
    doctor_status=$?
  fi

  if ! "${PYTHON_BIN}" - "${doctor_stdout}" "${doctor_stderr}" "${codex_home}" \
    "${config_path}" "${REQUIRED_CODEX_APP_SERVER_VERSION}" "${app_server_package_root}" \
    "${doctor_status}" <<'PY'
import json
import sys
from pathlib import Path

stdout_path = Path(sys.argv[1])
stderr_path = Path(sys.argv[2])
codex_home = Path(sys.argv[3])
config_path = Path(sys.argv[4])
required_version = sys.argv[5]
app_server_package_root = Path(sys.argv[6])
doctor_status = int(sys.argv[7])

try:
    report = json.loads(stdout_path.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"doctor did not emit valid JSON for {config_path}: {exc}") from exc

checks = report.get("checks")
if not isinstance(checks, dict):
    raise SystemExit(f"doctor JSON for {config_path} is missing checks")

required_checks = {
    "config.load",
    "mcp.config",
    "sandbox.helpers",
    "runtime.provenance",
}
missing = sorted(required_checks - set(checks))
if missing:
    raise SystemExit(f"doctor JSON for {config_path} is missing owned checks: {', '.join(missing)}")

failures: list[str] = []
for check_id in sorted(required_checks):
    check = checks[check_id]
    if not isinstance(check, dict):
        failures.append(f"{check_id}=invalid")
    elif check.get("status") != "ok":
        failures.append(f"{check_id}={check.get('status', '<missing>')}")

if report.get("codexVersion") != required_version:
    failures.append(f"codexVersion={report.get('codexVersion', '<missing>')} expected {required_version}")

runtime = checks["runtime.provenance"]
runtime_details = runtime.get("details") if isinstance(runtime, dict) else None
if not isinstance(runtime_details, dict) or runtime_details.get("version") != required_version:
    actual = runtime_details.get("version", "<missing>") if isinstance(runtime_details, dict) else "<missing>"
    failures.append(f"runtime.provenance.details.version={actual} expected {required_version}")

ignored: list[str] = []
unexpected: list[str] = []

def has_shape(
    check_id: str,
    check: dict[str, object],
    *,
    category: str,
    status: str,
    summary: str,
    details: dict[str, object],
) -> bool:
    if check.get("id") != check_id:
        return False
    if check.get("category") != category:
        return False
    if check.get("status") != status:
        return False
    if check.get("summary") != summary:
        return False
    actual_details = check.get("details")
    if not isinstance(actual_details, dict):
        return False
    return actual_details == details

def is_expected_openclaw_managed_auth_failure(check_id: str, check: dict[str, object]) -> bool:
    return (
        has_shape(
            check_id,
            check,
            category="auth",
            status="fail",
            summary="no Codex credentials were found",
            details={
                "auth file": str(codex_home / "auth.json"),
                "auth storage mode": "File",
            },
        )
        and not (codex_home / "auth.json").exists()
    )

def is_expected_embedded_installation_failure(check_id: str, check: dict[str, object]) -> bool:
    return has_shape(
        check_id,
        check,
        category="install",
        status="fail",
        summary="npm install -g @openai/codex would update a different install",
        details={"running package root": str(app_server_package_root)},
    )

def is_expected_update_probe_failure(check_id: str, check: dict[str, object]) -> bool:
    mismatch = has_shape(
        check_id,
        check,
        category="updates",
        status="fail",
        summary="update would target a different npm install",
        details={"running package root": str(app_server_package_root)},
    )
    timeout = has_shape(
        check_id,
        check,
        category="updates",
        status="warning",
        summary="update check timed out",
        details={"running package root": str(app_server_package_root)},
    )
    return mismatch or timeout

def is_expected_missing_auth_websocket_warning(check_id: str, check: dict[str, object]) -> bool:
    return (
        has_shape(
            check_id,
            check,
            category="websocket",
            status="warning",
            summary="Responses WebSocket failed; HTTPS fallback may still work",
            details={
                "auth mode": "none",
                "endpoint": "wss://api.openai.com/v1/<redacted>",
                "model provider": "openai",
                "provider name": "OpenAI",
                "supports websockets": "true",
                "wire API": "responses",
            },
        )
        and not (codex_home / "auth.json").exists()
    )

for check_id, raw_check in checks.items():
    if not isinstance(raw_check, dict):
        unexpected.append(f"{check_id}=invalid")
        continue
    status = raw_check.get("status")
    if status == "ok":
        continue
    if status not in {"fail", "warning"}:
        unexpected.append(f"{check_id}={status}")
        continue
    if check_id == "auth.credentials" and is_expected_openclaw_managed_auth_failure(check_id, raw_check):
        ignored.append(check_id)
    elif check_id == "installation" and is_expected_embedded_installation_failure(check_id, raw_check):
        ignored.append(check_id)
    elif check_id == "updates.status" and is_expected_update_probe_failure(check_id, raw_check):
        ignored.append(check_id)
    elif check_id == "network.websocket_reachability" and is_expected_missing_auth_websocket_warning(check_id, raw_check):
        ignored.append(check_id)
    else:
        unexpected.append(f"{check_id}={status}")

if failures:
    raise SystemExit(
        f"owned Codex doctor checks failed for {config_path}: {', '.join(failures)}"
    )
if unexpected:
    raise SystemExit(
        f"unexpected fatal Codex doctor checks for {config_path}: {', '.join(sorted(unexpected))}"
    )

if doctor_status == 0:
    if ignored:
        raise SystemExit(
            f"doctor exited 0 for {config_path} with non-ok checks: {', '.join(sorted(ignored))}"
        )
elif doctor_status == 1:
    if ignored:
        print(
            "Codex doctor non-owned failures ignored for "
            f"{config_path}: {', '.join(sorted(ignored))}. "
            "OpenClaw owns OAuth in openclaw-agent.sqlite and embeds the pinned Codex package."
        )
    else:
        raise SystemExit(
            f"doctor exited 1 for {config_path} without an allowed non-owned failure"
        )
else:
    raise SystemExit(f"doctor exited {doctor_status} for {config_path}; expected 0 or 1")

stderr = stderr_path.read_text(encoding="utf-8").strip()
if stderr:
    print(f"Codex doctor stderr for {config_path}: {stderr}")
PY
  then
    echo "ERROR: Embedded Codex owned validation failed for ${config_path}" >&2
    cat "${doctor_stderr}" >&2
    cat "${doctor_stdout}" >&2
    rm -f "${doctor_stdout}" "${doctor_stderr}"
    exit 1
  fi
  rm -f "${doctor_stdout}" "${doctor_stderr}"
}

remove_legacy_codex_stage_agents() {
  local agents_dir="$1"
  [[ -d "${agents_dir}" ]] || return 0
  for agent_id in "${CODEX_NATIVE_LEGACY_STAGE_AGENT_IDS[@]}"; do
    local stale_path="${agents_dir}/${agent_id}.toml"
    if [[ -f "${stale_path}" ]]; then
      rm -- "${stale_path}"
      echo "Removed stale native Codex stage agent ${stale_path}"
    fi
  done
}

validate_codex_native_stage_agents_dir "${CODEX_AGENTS_SRC}"

mapfile -t BOOTSTRAP_TARGETS < <(jq -r '
  def workspace_target:
    if ((.workspace? | type) == "string" and (.workspace | length) > 0) then
      .workspace
    elif .id == "main" then
      "__OPENCLAW_DEFAULT_WORKSPACE__"
    else
      "workspace-\(.id)"
    end;
  [.agents.list[]? | {agent: .id, workspace: workspace_target}]
  | group_by(.workspace)
  | map({workspace: .[0].workspace, agents: (map(.agent) | sort | join(","))})
  | sort_by(.workspace)
  | .[]
  | [.workspace, .agents]
  | @tsv
' "${REPO_CONFIG}")

if [[ "${#BOOTSTRAP_TARGETS[@]}" -eq 0 ]]; then
  echo "ERROR: No configured OpenClaw agents found in ${REPO_CONFIG}" >&2
  exit 1
fi

declare -A BOOTSTRAP_TARGET_DIRS=()
echo "Copying managed bootstrap files to ${#BOOTSTRAP_TARGETS[@]} configured OpenClaw workspaces:"
for TARGET in "${BOOTSTRAP_TARGETS[@]}"; do
  IFS=$'\t' read -r WORKSPACE_ID AGENTS <<< "${TARGET}"
  BOOTSTRAP_DST="$(workspace_dir_for_target "${WORKSPACE_ID}")"
  BOOTSTRAP_TARGET_DIRS["${BOOTSTRAP_DST}"]=1
  mkdir -p "${BOOTSTRAP_DST}"
  for FILE in "${BOOTSTRAP_FILES[@]}"; do
    cp "${REPO_ROOT}/gateway/agent_config/${FILE}" "${BOOTSTRAP_DST}/${FILE}"
  done
  if workspace_has_autoresearch_agent "${AGENTS}"; then
    CODEX_AGENTS_DST="${BOOTSTRAP_DST}/.codex/agents"
    mkdir -p "${CODEX_AGENTS_DST}"
    remove_legacy_codex_stage_agents "${CODEX_AGENTS_DST}"
    for AGENT_ID in "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}"; do
      cp "${CODEX_AGENTS_SRC}/${AGENT_ID}.toml" "${CODEX_AGENTS_DST}/${AGENT_ID}.toml"
    done
    validate_codex_native_stage_agents_dir "${CODEX_AGENTS_DST}"
  fi
  echo "  ${AGENTS} → ${BOOTSTRAP_DST} (${BOOTSTRAP_FILES[*]})"
done
echo "Local workspace files such as USER.md and IDENTITY.md were left untouched."

# Native Codex resolves agent definitions from the app-server's scoped
# CODEX_HOME, not from the OpenClaw workspace. Keep that runtime source
# synchronized for the PM and every configured stage agent.
CODEX_NATIVE_RUNTIME_AGENT_IDS=("main" "autoresearch-pm" "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}")
echo "Copying native Codex stage agents to ${#CODEX_NATIVE_RUNTIME_AGENT_IDS[@]} scoped Codex homes:"
for CODEX_RUNTIME_AGENT_ID in "${CODEX_NATIVE_RUNTIME_AGENT_IDS[@]}"; do
  CODEX_RUNTIME_HOME="${OPENCLAW_PUSH_HOME}/agents/${CODEX_RUNTIME_AGENT_ID}/agent/codex-home"
  CODEX_RUNTIME_AGENTS_DST="${CODEX_RUNTIME_HOME}/agents"
  mkdir -p "${CODEX_RUNTIME_AGENTS_DST}"
  write_codex_runtime_config "${CODEX_RUNTIME_HOME}" "${CODEX_RUNTIME_AGENT_ID}"
  remove_legacy_codex_stage_agents "${CODEX_RUNTIME_AGENTS_DST}"
  if [[ "${CODEX_RUNTIME_AGENT_ID}" == "main" ]]; then
    find "${CODEX_RUNTIME_AGENTS_DST}" -mindepth 1 -maxdepth 1 -type f -name '*.toml' -delete
  else
    for AGENT_ID in "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}"; do
      cp "${CODEX_AGENTS_SRC}/${AGENT_ID}.toml" "${CODEX_RUNTIME_AGENTS_DST}/${AGENT_ID}.toml"
    done
  fi
  validate_codex_runtime_config "${CODEX_RUNTIME_HOME}" "${CODEX_RUNTIME_AGENT_ID}"
  if [[ "${CODEX_RUNTIME_AGENT_ID}" != "main" ]]; then
    validate_codex_native_stage_agents_dir "${CODEX_RUNTIME_AGENTS_DST}"
  fi
  echo "  ${CODEX_RUNTIME_AGENT_ID} → ${CODEX_RUNTIME_HOME}"
done

# Clean stale copies from wrong bootstrap locations without touching local
# per-workspace files such as USER.md, IDENTITY.md, or other notes.
STALE_BOOTSTRAP_DIRS=(
  "${OPENCLAW_PUSH_HOME}"
  "${OPENCLAW_PUSH_HOME}/workspace"
)
for FILE in "${BOOTSTRAP_FILES[@]}"; do
  for STALE_DIR in "${STALE_BOOTSTRAP_DIRS[@]}"; do
    STALE="${STALE_DIR}/${FILE}"
    if [[ -f "${STALE}" ]] && [[ -z "${BOOTSTRAP_TARGET_DIRS[${STALE_DIR}]:-}" ]]; then
      rm "${STALE}"
      echo "Removed stale ${STALE}"
    fi
  done
done

# ── Copy repo skills ─────────────────────────────────────────────────────────
SKILLS_DST="${OPENCLAW_PUSH_HOME}/skills"
if [[ -d "${SKILLS_SRC}" ]]; then
  # Copy repo skills to local
  for SKILL_DIR in "${SKILLS_SRC}"/*/; do
    SKILL_NAME="$(basename "${SKILL_DIR}")"
    if [[ ! -f "${SKILL_DIR}SKILL.md" ]]; then
      continue
    fi
    mkdir -p "${SKILLS_DST}/${SKILL_NAME}"
    cp "${SKILL_DIR}"SKILL.md "${SKILLS_DST}/${SKILL_NAME}/SKILL.md"
    echo "Copied skill ${SKILL_NAME} → ${SKILLS_DST}/${SKILL_NAME}/SKILL.md"
  done
fi
STALE_MEMPALACE_WRITE_SKILL_DST="${SKILLS_DST}/mempalace"
if [[ -d "${STALE_MEMPALACE_WRITE_SKILL_DST}" ]]; then
  rm -rf -- "${STALE_MEMPALACE_WRITE_SKILL_DST}"
  echo "Removed stale write-capable MemPalace skill ${STALE_MEMPALACE_WRITE_SKILL_DST}"
fi

# ── Manage Azure API-version preload artifact ────────────────────────────────
PRELOAD_SRC="${REPO_ROOT}/gateway/openclaw_config/azure-api-version-preload.cjs"
PRELOAD_DST="${OPENCLAW_PUSH_HOME}/azure-api-version-preload.cjs"
if [[ "${OPENCLAW_PROVIDER:-codex}" == "azure" && -f "${PRELOAD_SRC}" ]]; then
  cp "${PRELOAD_SRC}" "${PRELOAD_DST}"
  echo "Copied azure-api-version-preload.cjs → ${PRELOAD_DST}"
elif [[ "${OPENCLAW_PROVIDER:-codex}" != "azure" && -f "${PRELOAD_DST}" ]]; then
  rm "${PRELOAD_DST}"
  echo "Removed Azure preload artifact from Codex/OpenRouter route: ${PRELOAD_DST}"
fi

if [[ "${PROVIDER}" == "codex" ]]; then
  remove_stale_azure_node_options_for_codex
  sync_managed_agent_codex_auth
fi

# ── Validate ─────────────────────────────────────────────────────────────────
echo ""
echo "── Validating config ──"
echo "Running: ${OPENCLAW_BIN_RESOLVED} config validate"
if ! run_openclaw_cli config validate; then
  echo "ERROR: '${OPENCLAW_BIN_RESOLVED} config validate' failed. Restored backup ${BACKUP}." >&2
  exit 1
fi

# Install the supervisor definition without starting autonomous work. The
# human-facing control command owns enable/start and stop transitions.
mkdir -p "${SYSTEMD_USER_DIR}"
SUPERVISOR_UNIT_TMP="$(mktemp "${SYSTEMD_USER_DIR}/.${SUPERVISOR_SERVICE_NAME}.XXXXXX")"
sed \
  -e "s|@REPO_ROOT@|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|@HOME@|$(escape_sed_replacement "${HOME}")|g" \
  -e "s|@PATH@|$(escape_sed_replacement "${PATH}")|g" \
  -e "s|@PYTHON_BIN@|$(escape_sed_replacement "${PYTHON_BIN}")|g" \
  "${SUPERVISOR_UNIT_TEMPLATE}" > "${SUPERVISOR_UNIT_TMP}"
if grep -q '@[A-Z_][A-Z_]*@' "${SUPERVISOR_UNIT_TMP}"; then
  echo "ERROR: Unresolved placeholder in generated ${SUPERVISOR_SERVICE_NAME}." >&2
  exit 1
fi
if ! validate_supervisor_unit_file "${SUPERVISOR_UNIT_TMP}"; then
  exit 1
fi
chmod 0644 "${SUPERVISOR_UNIT_TMP}"
begin_managed_unit_transaction
mv "${SUPERVISOR_UNIT_TMP}" "${SUPERVISOR_UNIT_DST}"
SUPERVISOR_UNIT_TMP=""
echo "Installed ${SUPERVISOR_SERVICE_NAME} (not started)."

# Install persistent numerical-runtime caps on the OpenClaw Gateway service so
# all OpenClaw-launched Quantipy child processes inherit bounded BLAS/joblib
# thread counts. This is shared operator infrastructure, not agent-owned state.
prepare_runtime_caps_dropin_dir
GATEWAY_RUNTIME_CAPS_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${GATEWAY_RUNTIME_CAPS_DROPIN_NAME}.XXXXXX")"
cp "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}" "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"
chmod 0644 "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"
validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"
mv "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
GATEWAY_RUNTIME_CAPS_DROPIN_TMP=""
validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
CODEX_RUNTIME_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${CODEX_RUNTIME_DROPIN_NAME}.XXXXXX")"
cp "${CODEX_RUNTIME_DROPIN_SRC}" "${CODEX_RUNTIME_DROPIN_TMP}"
chmod 0644 "${CODEX_RUNTIME_DROPIN_TMP}"
validate_codex_runtime_dropin_file "${CODEX_RUNTIME_DROPIN_TMP}"
mv "${CODEX_RUNTIME_DROPIN_TMP}" "${CODEX_RUNTIME_DROPIN_DST}"
CODEX_RUNTIME_DROPIN_TMP=""
validate_codex_runtime_dropin_file "${CODEX_RUNTIME_DROPIN_DST}"
NATIVE_CRASH_HARDENING_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${NATIVE_CRASH_HARDENING_DROPIN_NAME}.XXXXXX")"
cp "${NATIVE_CRASH_HARDENING_DROPIN_SRC}" "${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
chmod 0644 "${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
mv "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
NATIVE_CRASH_HARDENING_DROPIN_TMP=""
validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
systemctl --user daemon-reload
commit_managed_unit_transaction
echo "Installed ${GATEWAY_SERVICE_NAME} runtime caps drop-in → ${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
echo "Installed ${GATEWAY_SERVICE_NAME} Codex runtime verifier → ${CODEX_RUNTIME_DROPIN_DST}"
echo "Installed ${GATEWAY_SERVICE_NAME} native-crash hardening → ${NATIVE_CRASH_HARDENING_DROPIN_DST}"
echo "Reloaded user systemd units; restart ${GATEWAY_SERVICE_NAME} externally for a running gateway to inherit these caps."

ROLLBACK_ARMED=0
trap - EXIT

echo ""
echo "Done. Config pushed successfully."
echo ""
if [[ "${PROVIDER}" == "codex" ]]; then
  echo "── OpenAI / Codex ──"
  echo "Using model: ${MODEL_PRIMARY}"
  echo "Required auth: openclaw models auth login --provider openai"
  echo "Codex plugin is enabled; OpenAI provider runtime is pinned to codex."
  echo ""
elif [[ "${PROVIDER}" == "azure" ]]; then
  echo "── Azure Entra preload ──"
  echo "Ensure 'az login' has been run and NODE_OPTIONS is set before starting the daemon:"
  echo ""
  echo "  az login"
  echo "  export NODE_OPTIONS=\"--require \$HOME/.openclaw/azure-api-version-preload.cjs\""
  echo ""
fi
