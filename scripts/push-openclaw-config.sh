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
MEMPALACE_READONLY_WRAPPER_BASENAME="mempalace-readonly-server.py"
MEMPALACE_FULL_AGENT_IDS=(
  "autoresearch-pm"
)
PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS=(
  "sessions_spawn"
  "sessions_yield"
)
MEMPALACE_READONLY_AGENT_IDS=(
  "context-curator"
  "debater-microstructure"
  "debater-data"
  "debater-skeptic"
  "debater-theory"
  "debater-implementation"
  "consensus-arbiter"
  "implementer"
  "reviewer"
  "fixer"
)
CODEX_NATIVE_STAGE_AGENT_IDS=("${MEMPALACE_READONLY_AGENT_IDS[@]}")
MEMPALACE_MUTATION_TOOL_NAMES=(
  "mempalace_add_drawer"
  "mempalace_check_duplicate"
  "mempalace_checkpoint"
  "mempalace_create_tunnel"
  "mempalace_delete_by_source"
  "mempalace_delete_drawer"
  "mempalace_delete_hallway"
  "mempalace_delete_tunnel"
  "mempalace_diary_write"
  "mempalace_hook_settings"
  "mempalace_kg_add"
  "mempalace_kg_invalidate"
  "mempalace_mine"
  "mempalace_reconnect"
  "mempalace_sync"
  "mempalace_update_drawer"
)
RUNTIME_CAP_ENV_LINES=(
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

build_mempalace_mutation_policy_ids_json() {
  local tool_names_json
  tool_names_json="$(printf '%s\n' "${MEMPALACE_MUTATION_TOOL_NAMES[@]}" | jq -Rsc 'split("\n")[:-1]')"
  jq -cn --argjson tool_names "${tool_names_json}" '
    [$tool_names[] | "mempalace__\(.)"]
  '
}

build_mempalace_obsolete_mutation_alias_ids_json() {
  local tool_names_json
  tool_names_json="$(printf '%s\n' "${MEMPALACE_MUTATION_TOOL_NAMES[@]}" | jq -Rsc 'split("\n")[:-1]')"
  jq -cn --argjson tool_names "${tool_names_json}" '
    def obsolete_aliases($tool_name):
      [
        $tool_name,
        "mempalace.\($tool_name)",
        "mcp__mempalace__\($tool_name)"
      ];
    [$tool_names[] | obsolete_aliases(.)[]]
  '
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
    OPENCLAW_CONFIG_PATH="${LOCAL_CONFIG}" \
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
  local inspect_json plugin_version app_server_version
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

MEMPALACE_MUTATION_DENY_IDS_JSON="$(build_mempalace_mutation_policy_ids_json)"
MEMPALACE_OBSOLETE_MUTATION_ALIAS_IDS_JSON="$(build_mempalace_obsolete_mutation_alias_ids_json)"
MEMPALACE_FULL_AGENT_IDS_JSON="$(build_string_array_json "${MEMPALACE_FULL_AGENT_IDS[@]}")"
MEMPALACE_READONLY_AGENT_IDS_JSON="$(build_string_array_json "${MEMPALACE_READONLY_AGENT_IDS[@]}")"
PM_NATIVE_CODEX_DELEGATION_DENY_IDS_JSON="$(build_string_array_json "${PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS[@]}")"

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
  "mempalace"
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
  --argjson full_agents "${MEMPALACE_FULL_AGENT_IDS_JSON}" \
  --argjson readonly_agents "${MEMPALACE_READONLY_AGENT_IDS_JSON}" '
  .mcp.servers.mempalace = {
    "command": $cmd,
    "args": ["-m", "mempalace.mcp_server", "--palace", $palace],
    "codex": {
      "agents": $full_agents
    },
    "env": {
      "FASTEMBED_CACHE_PATH": $cache,
      "MEMPALACE_EMBEDDING_MODEL": $model,
      "HF_HUB_OFFLINE": $offline
    }
  }
  | .mcp.servers."mempalace-readonly" = {
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
  }
')
echo "Resolved required MemPalace MCP: ${MEMPALACE_PYTHON} --palace ${MEMPALACE_PALACE}"
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

echo "Active provider: ${PROVIDER} → default model: ${MODEL_PRIMARY}; PM model: ${PM_MODEL_PRIMARY}"

# Read-only stage agents must deny exactly the internal OpenClaw MCP tool.name ids.
# Dotted names are Codex-facing display/docs ids only, and historical bare/mcp__
# aliases are rejected instead of accepted as compatibility forms.
if ! echo "${MERGED}" | jq -e \
  --argjson pm_native_codex_denies "${PM_NATIVE_CODEX_DELEGATION_DENY_IDS_JSON}" \
  --argjson mempalace_mutation_denies "${MEMPALACE_MUTATION_DENY_IDS_JSON}" \
  --argjson obsolete_mempalace_mutation_aliases "${MEMPALACE_OBSOLETE_MUTATION_ALIAS_IDS_JSON}" \
  --argjson readonly_agents "${MEMPALACE_READONLY_AGENT_IDS_JSON}" '
  def denies: (.tools.deny // []);
  def is_stage: (.id as $id | ($readonly_agents | index($id)) != null);
  ([.agents.list[] | select(.id == "autoresearch-pm") | select(denies == $pm_native_codex_denies)] | length) == 1
  and
  ([.agents.list[] | select(is_stage) | select(denies != $mempalace_mutation_denies)] | length) == 0
  and (([
    .agents.list[] | denies[]?
  ] | map(select(. as $tool | $obsolete_mempalace_mutation_aliases | index($tool))) | length) == 0)
  and (([
    .agents.list[] | select(.id == "autoresearch-pm") | denies[]?
  ] | map(select(. as $tool | ($mempalace_mutation_denies + $obsolete_mempalace_mutation_aliases) | index($tool))) | length) == 0)
' >/dev/null; then
  echo "ERROR: Every read-only autoresearch stage agent must deny exactly the 16 canonical MemPalace mutation policy IDs." >&2
  echo "       Canonical IDs use internal server__tool form: mempalace__mempalace_<mutation>." >&2
  echo "       Bare, dotted, and mcp__ MemPalace mutation aliases are obsolete and forbidden." >&2
  echo "       autoresearch-pm must deny exactly sessions_spawn and sessions_yield while retaining MemPalace mutator access for final experiment logging." >&2
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
  --argjson full_agents "${MEMPALACE_FULL_AGENT_IDS_JSON}" \
  --argjson readonly_agents "${MEMPALACE_READONLY_AGENT_IDS_JSON}" \
  --argjson pm_native_codex_denies "${PM_NATIVE_CODEX_DELEGATION_DENY_IDS_JSON}" \
  --argjson mempalace_mutation_denies "${MEMPALACE_MUTATION_DENY_IDS_JSON}" \
  --argjson obsolete_mempalace_mutation_aliases "${MEMPALACE_OBSOLETE_MUTATION_ALIAS_IDS_JSON}" '
  def denies: (.tools.deny // []);
  def is_stage: (.id as $id | ($readonly_agents | index($id)) != null);
  def expected_models: {
    "main": "openai/gpt-5.4",
    "autoresearch-pm": $pm,
    "context-curator": "openai/gpt-5.4",
    "debater-microstructure": "openai/gpt-5.5",
    "debater-data": "openai/gpt-5.6-terra",
    "debater-skeptic": "openai/gpt-5.5",
    "debater-theory": "openai/gpt-5.4",
    "debater-implementation": "openai/gpt-5.4",
    "consensus-arbiter": "openai/gpt-5.6-sol",
    "implementer": "openai/gpt-5.4",
    "reviewer": "openai/gpt-5.6-sol",
    "fixer": "openai/gpt-5.4"
  };
  (.agents.defaults.thinkingDefault == "high")
  and ((.plugins.allow // []) | contains(["codex"]))
  and (.agents.defaults.maxConcurrent == 2)
  and (.agents.defaults.subagents.maxConcurrent == 1)
  and (.agents.defaults.subagents.maxChildrenPerAgent? == null)
  and (.agents.defaults.memorySearch.enabled == false)
  and (.agents.defaults.compaction.mode == "default")
  and (.agents.defaults.compaction.memoryFlush.enabled == false)
  and ((.tools.deny // []) | contains(["memory_search", "memory_get"]))
  and (.mcp.servers.mempalace.command == $cmd)
  and (.mcp.servers.mempalace.args == ["-m", "mempalace.mcp_server", "--palace", $palace])
  and ((.mcp.servers.mempalace.codex.agents // []) == $full_agents)
  and (.mcp.servers.mempalace.env == {
    "FASTEMBED_CACHE_PATH": $cache,
    "MEMPALACE_EMBEDDING_MODEL": $model,
    "HF_HUB_OFFLINE": $offline
  })
  and (.mcp.servers."mempalace-readonly".command == $cmd)
  and (.mcp.servers."mempalace-readonly".args == [$wrapper, "--palace", $palace])
  and ((.mcp.servers."mempalace-readonly".codex.agents // []) == $readonly_agents)
  and (.mcp.servers."mempalace-readonly".env == {
    "FASTEMBED_CACHE_PATH": $cache,
    "MEMPALACE_EMBEDDING_MODEL": $model,
    "HF_HUB_OFFLINE": $offline
  })
  and (([.agents.list[].id] | sort) == (expected_models | keys | sort))
  and all(.agents.list[]; .model.primary == expected_models[.id])
  and all(.agents.list[]; .thinkingDefault == "high")
  and ([.agents.list[] | select(
    .id == "autoresearch-pm"
    and .model.primary == $pm
    and .thinkingDefault == "high"
    and ((.skills // []) | contains(["mempalace", "autoresearch"]))
    and denies == $pm_native_codex_denies
    and (((.subagents.allowAgents? // []) | length) == 0)
  )] | length) == 1
  and ([.agents.list[] | select(
    .id == "main"
    and .model.primary == "openai/gpt-5.4"
    and .thinkingDefault == "high"
    and (((.skills // []) | index("mempalace")) == null)
    and (((.skills // []) | index("autoresearch")) == null)
    and (((.skills // []) | index("mempalace-readonly")) == null)
    and (((.subagents.allowAgents? // []) | length) == 0)
  )] | length) == 1
  and ([.agents.list[] | select((.subagents.allowAgents? // []) | length > 0)] | length) == 0
  and ([.agents.list[] | select(.id != "autoresearch-pm") | select(((.skills // []) | index("mempalace")) != null)] | length) == 0
  and ([.agents.list[] | select(.id != "autoresearch-pm") | select(((.skills // []) | index("autoresearch")) != null)] | length) == 0
  and ([.agents.list[] | select(is_stage) | select(((.skills // []) | index("mempalace-readonly")) == null)] | length) == 0
  and ([.agents.list[] | select(is_stage) | select(((.skills // []) | index("quantipy-methodology")) == null)] | length) == 0
  and ([.agents.list[] | select(is_stage) | select(denies != $mempalace_mutation_denies)] | length) == 0
  and (([
    .agents.list[] | denies[]?
  ] | map(select(. as $tool | $obsolete_mempalace_mutation_aliases | index($tool))) | length) == 0)
  and (([
    .agents.list[] | select(.id == "autoresearch-pm") | denies[]?
  ] | map(select(. as $tool | ($mempalace_mutation_denies + $obsolete_mempalace_mutation_aliases) | index($tool))) | length) == 0)
' >/dev/null; then
  echo "ERROR: Generated OpenClaw config violates repo-managed autoresearch invariants." >&2
  echo "       Check plugins.allow, autoresearch-pm model/skills/native Codex delegation denies, main interface restrictions, strict concurrency caps, MemPalace full/read-only MCP split, stage skill scopes, Quantipy methodology skill, and memory tool denies." >&2
  exit 1
fi
echo "Managed invariants validated: main interface split, autoresearch-pm model and native Codex delegation denies, exact stage models, high reasoning, strict concurrency caps, MemPalace split, Quantipy methodology skill, built-in memory disabled."

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
    "context-curator": "gpt-5.4",
    "debater-microstructure": "gpt-5.5",
    "debater-data": "gpt-5.6-terra",
    "debater-skeptic": "gpt-5.5",
    "debater-theory": "gpt-5.4",
    "debater-implementation": "gpt-5.4",
    "consensus-arbiter": "gpt-5.6-sol",
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
PY
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
    for AGENT_ID in "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}"; do
      cp "${CODEX_AGENTS_SRC}/${AGENT_ID}.toml" "${CODEX_AGENTS_DST}/${AGENT_ID}.toml"
    done
    validate_codex_native_stage_agents_dir "${CODEX_AGENTS_DST}"
  fi
  echo "  ${AGENTS} → ${BOOTSTRAP_DST} (${BOOTSTRAP_FILES[*]})"
done
echo "Local workspace files such as USER.md and IDENTITY.md were left untouched."

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
