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
    guarded_mkdir_p "${agent_dir}" "creating managed OpenClaw agent auth directory ${agent_dir}"
    if [[ -f "${source_profiles}" ]]; then
      guarded_cp_file "${source_profiles}" "${agent_dir}/auth-profiles.json" "copying managed OpenClaw auth profile to ${agent_dir}/auth-profiles.json"
      guarded_chmod 0600 "${agent_dir}/auth-profiles.json" "chmod managed OpenClaw auth profile ${agent_dir}/auth-profiles.json"
    fi
    guard_destination_path_chain "${target_db}" "syncing managed OpenClaw agent auth database ${target_db}"
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
    guarded_chmod 0600 "${target_db}" "chmod managed OpenClaw agent auth database ${target_db}"
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
GENERATED_OPENCLAW_CONFIG_TMP=""
GENERATED_OPENCLAW_CONFIG_HASH=""
GENERATED_OPENCLAW_CONFIG_BYTES=""
GENERATED_OPENCLAW_CONFIG_IDENTITY=""
REPO_CONFIG_PREFLIGHT_COPY=""
REPO_CONFIG_PREFLIGHT_DIR=""
REPO_CONFIG_PREFLIGHT_HASH=""
REPO_CONFIG_PREFLIGHT_BYTES=""
REPO_CONFIG_PREFLIGHT_IDENTITY=""
PUBLISHED_OPENCLAW_CONFIG_IDENTITY=""

run_openclaw_cli() {
  run_openclaw_cli_for_config "${LOCAL_CONFIG}" "$@"
}

run_openclaw_cli_for_repo_config() {
  run_openclaw_cli_for_guarded_repo_config "$@"
}

run_openclaw_cli_for_config() {
  local config_path="$1"
  shift
  local -a env_args=(
    -u OPENCLAW_HOME
    -u OPENCLAW_PUSH_HOME
    -u OPENCLAW_STATE_DIR
    -u OPENCLAW_CONFIG_PATH
    -u NODE_OPTIONS
  )
  env \
    "${env_args[@]}" \
    OPENCLAW_STATE_DIR="${OPENCLAW_PUSH_HOME}" \
    OPENCLAW_CONFIG_PATH="${config_path}" \
    "${OPENCLAW_BIN_RESOLVED}" "$@"
}

openclaw_schema_validation_is_clean() {
  jq -se '
    length == 1
    and (.[0] | type == "object")
    and (.[0].valid == true)
    and (.[0].warnings | type == "array" and length == 0)
  ' >/dev/null 2>&1
}

file_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

file_bytes() {
  wc -c < "$1" | tr -d '[:space:]'
}

path_exists_or_symlink() {
  [[ -e "$1" || -L "$1" ]]
}

file_link_count() {
  stat -c '%h' -- "$1"
}

guarded_regular_file_identity() {
  local path="$1"
  local context="$2"
  local identity nlink

  if [[ -L "${path}" ]]; then
    echo "ERROR: Guarded file is a symlink while ${context}: ${path} -> $(readlink "${path}" 2>/dev/null || printf '<unreadable>')" >&2
    echo "       Refusing to trust followed bytes for guarded config publication." >&2
    return 1
  fi
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Guarded file is not a regular file while ${context}: ${path}" >&2
    echo "       Refusing to trust followed bytes for guarded config publication." >&2
    return 1
  fi
  if ! identity="$(stat -c '%d:%i:%f:%h' -- "${path}")"; then
    echo "ERROR: Could not capture lstat identity while ${context}: ${path}" >&2
    return 1
  fi
  nlink="${identity##*:}"
  if [[ "${nlink}" != "1" ]]; then
    echo "ERROR: Guarded file is hard-linked while ${context}: ${path} (link count ${nlink})." >&2
    echo "       Refusing to trust followed bytes for guarded config publication." >&2
    return 1
  fi
  printf '%s\n' "${identity}"
}

verify_guarded_regular_file_identity_unchanged() {
  local path="$1"
  local expected_identity="$2"
  local context="$3"
  local current_identity

  if ! current_identity="$(guarded_regular_file_identity "${path}" "${context}")"; then
    return 1
  fi
  if [[ "${current_identity}" != "${expected_identity}" ]]; then
    echo "ERROR: Guarded file identity/topology changed during ${context}: ${path}." >&2
    echo "       expected lstat ${expected_identity}; got ${current_identity}." >&2
    return 1
  fi
}

guard_no_hardlinked_regular_file() {
  local path="$1"
  local context="$2"
  local link_count
  if [[ -f "${path}" && ! -L "${path}" ]]; then
    if ! link_count="$(file_link_count "${path}")"; then
      echo "ERROR: Could not inspect link count for destination while ${context}: ${path}" >&2
      echo "       Refusing before mutation." >&2
      return 1
    fi
    if ((link_count > 1)); then
      echo "ERROR: Destination path is a hard-linked regular file while ${context}: ${path} (link count ${link_count})." >&2
      echo "       Refusing before mutation to avoid modifying external hard-link aliases." >&2
      return 1
    fi
  fi
}

guard_destination_path_chain() {
  local path="$1"
  local context="$2"
  local current component target
  local -a PATH_CHAIN_COMPONENTS

  if [[ -z "${path}" ]]; then
    echo "ERROR: Empty destination path while ${context}; refusing before mutation." >&2
    return 1
  fi
  if [[ "${path}" != /* ]]; then
    echo "ERROR: Destination path is not absolute while ${context}: ${path}" >&2
    echo "       Refusing before mutation." >&2
    return 1
  fi

  current=""
  IFS='/' read -ra PATH_CHAIN_COMPONENTS <<< "${path#/}"
  for component in "${PATH_CHAIN_COMPONENTS[@]}"; do
    [[ -n "${component}" && "${component}" != "." ]] || continue
    if [[ "${component}" == ".." ]]; then
      echo "ERROR: Destination path contains '..' while ${context}: ${path}" >&2
      echo "       Refusing before mutation." >&2
      return 1
    fi
    current="${current}/${component}"
    if [[ -L "${current}" ]]; then
      target="$(readlink "${current}" 2>/dev/null || printf '<unreadable>')"
      echo "ERROR: Destination path chain contains symlink while ${context}: ${current} -> ${target}" >&2
      echo "       Full destination: ${path}" >&2
      echo "       Refusing before mutation to avoid following nested symlinks outside the managed root." >&2
      return 1
    fi
    guard_no_hardlinked_regular_file "${current}" "${context}" || return 1
  done
}

guard_destination_parent_path_chain() {
  local path="$1"
  local context="$2"
  local parent_path

  if [[ -z "${path}" ]]; then
    echo "ERROR: Empty destination path while ${context}; refusing before mutation." >&2
    return 1
  fi
  parent_path="$(dirname "${path}")"
  guard_destination_path_chain "${parent_path}" "${context}" || return 1
}

guarded_mkdir_p() {
  local destination_path="$1"
  local context="$2"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  mkdir -p "${destination_path}" || return 1
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
}

copy_path_topology() {
  local source_path="$1"
  local destination_path="$2"
  cp -aT -- "${source_path}" "${destination_path}"
}

guarded_copy_path_topology() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  copy_path_topology "${source_path}" "${destination_path}" || return 1
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
}

guarded_copy_path_topology_preserving_final_symlink_topology() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  guard_destination_parent_path_chain "${destination_path}" "${context}" || return 1
  copy_path_topology "${source_path}" "${destination_path}" || return 1
  guard_destination_parent_path_chain "${destination_path}" "${context}" || return 1
}

guarded_cp_file() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  if [[ -d "${destination_path}" && ! -L "${destination_path}" ]]; then
    echo "ERROR: Destination path is an existing directory while ${context}: ${destination_path}" >&2
    echo "       Refusing before cp to avoid source-to-destination-directory behavior." >&2
    return 1
  fi
  cp "${source_path}" "${destination_path}" || return 1
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
}

guarded_chmod() {
  local mode="$1"
  local destination_path="$2"
  local context="$3"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  chmod "${mode}" "${destination_path}" || return 1
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
}

guarded_chmod_reference() {
  local reference_path="$1"
  local destination_path="$2"
  local context="$3"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  chmod --reference="${reference_path}" "${destination_path}" || return 1
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
}

collect_find_results_null() {
  local output_path="$1"
  local scan_root="$2"
  local context="$3"
  shift 3

  if ! find "${scan_root}" "$@" -print0 > "${output_path}"; then
    echo "ERROR: Failed to scan ${scan_root} while ${context}; refusing to continue with partial results." >&2
    if ! guarded_rm_f "${output_path}" "removing failed managed scan output ${output_path}"; then
      echo "ERROR: Failed to remove failed managed scan output ${output_path}." >&2
      echo "Managed scan output preserved at ${output_path}" >&2
    fi
    return 1
  fi
}

guarded_rm_rf() {
  local destination_path="$1"
  local context="$2"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  rm -rf -- "${destination_path}"
}

guarded_rm_f() {
  local destination_path="$1"
  local context="$2"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  rm -f -- "${destination_path}"
}

guarded_rm() {
  local destination_path="$1"
  local context="$2"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  rm -- "${destination_path}"
}

guarded_rmdir() {
  local destination_path="$1"
  local context="$2"
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  rmdir "${destination_path}"
}

guarded_mv_replace() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  shift 3
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
  mv "$@" "${source_path}" "${destination_path}" || return 1
  guard_destination_path_chain "${destination_path}" "${context}" || return 1
}

guarded_mv_replace_preserving_final_symlink_topology() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  shift 3
  guard_destination_parent_path_chain "${destination_path}" "${context}" || return 1
  mv "$@" "${source_path}" "${destination_path}" || return 1
  guard_destination_parent_path_chain "${destination_path}" "${context}" || return 1
}

restore_path_topology_from_backup() {
  local backup_path="$1"
  local destination_path="$2"
  local restore_stage="$3"
  local destination_parent

  if ! guarded_rm_rf "${restore_stage}" "clearing staged restore path ${restore_stage}"; then
    echo "ERROR: Failed to clear staged restore path ${restore_stage} during rollback." >&2
    return 1
  fi
  if ! guarded_copy_path_topology "${backup_path}" "${restore_stage}" "staging rollback restore for ${destination_path}"; then
    echo "ERROR: Failed to stage backup ${backup_path} for rollback to ${destination_path}." >&2
    echo "       Original artifact path left intact; recoverable backup preserved at ${backup_path}." >&2
    return 1
  fi
  destination_parent="$(dirname "${destination_path}")"
  if ! guarded_mkdir_p "${destination_parent}" "recreating parent directory ${destination_parent} during rollback"; then
    echo "ERROR: Failed to recreate parent directory ${destination_parent} during rollback." >&2
    echo "       staged restore copy preserved at ${restore_stage}." >&2
    return 1
  fi
  if ! guarded_rm_rf "${destination_path}" "removing changed path ${destination_path} during rollback"; then
    echo "ERROR: Failed to remove changed path ${destination_path} during rollback." >&2
    echo "       staged restore copy preserved at ${restore_stage}." >&2
    return 1
  fi
  if ! guarded_mv_replace "${restore_stage}" "${destination_path}" "restoring ${destination_path} from rollback stage" -T; then
    echo "ERROR: Failed to replace ${destination_path} with staged restore ${restore_stage}." >&2
    echo "       Recoverable backup preserved at ${backup_path}; staged restore copy preserved at ${restore_stage}." >&2
    return 1
  fi
}

prepare_repo_config_preflight_copy() {
  if [[ -n "${REPO_CONFIG_PREFLIGHT_COPY:-}" ]]; then
    return 0
  fi
  REPO_CONFIG_PREFLIGHT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/push-openclaw-config-preflight.XXXXXX")"
  REPO_CONFIG_PREFLIGHT_COPY="${REPO_CONFIG_PREFLIGHT_DIR}/openclaw.repo-preflight.json"
  guarded_regular_file_identity "${REPO_CONFIG}" "creating guarded repo OpenClaw config copy from ${REPO_CONFIG}" >/dev/null || return 1
  if ! copy_path_topology "${REPO_CONFIG}" "${REPO_CONFIG_PREFLIGHT_COPY}"; then
    echo "ERROR: Failed to create guarded repo OpenClaw config copy at ${REPO_CONFIG_PREFLIGHT_COPY}." >&2
    return 1
  fi
  guarded_chmod 0600 "${REPO_CONFIG_PREFLIGHT_COPY}" "chmod guarded repo OpenClaw config copy ${REPO_CONFIG_PREFLIGHT_COPY}"
  REPO_CONFIG_PREFLIGHT_IDENTITY="$(guarded_regular_file_identity "${REPO_CONFIG_PREFLIGHT_COPY}" "capturing guarded repo OpenClaw config copy identity ${REPO_CONFIG_PREFLIGHT_COPY}")" || return 1
  REPO_CONFIG_PREFLIGHT_HASH="$(file_sha256 "${REPO_CONFIG_PREFLIGHT_COPY}")"
  REPO_CONFIG_PREFLIGHT_BYTES="$(file_bytes "${REPO_CONFIG_PREFLIGHT_COPY}")"
}

verify_repo_config_preflight_copy_unchanged() {
  local context="$1"
  local current_hash current_bytes
  if [[ -z "${REPO_CONFIG_PREFLIGHT_COPY:-}" ]]; then
    echo "ERROR: Guarded repo OpenClaw config copy is missing after ${context}: ${REPO_CONFIG_PREFLIGHT_COPY:-<unset>}." >&2
    return 1
  fi
  if ! verify_guarded_regular_file_identity_unchanged "${REPO_CONFIG_PREFLIGHT_COPY}" "${REPO_CONFIG_PREFLIGHT_IDENTITY}" "${context}"; then
    return 1
  fi
  current_hash="$(file_sha256 "${REPO_CONFIG_PREFLIGHT_COPY}")"
  current_bytes="$(file_bytes "${REPO_CONFIG_PREFLIGHT_COPY}")"
  if [[ "${current_hash}" != "${REPO_CONFIG_PREFLIGHT_HASH}" || "${current_bytes}" != "${REPO_CONFIG_PREFLIGHT_BYTES}" ]]; then
    echo "ERROR: External OpenClaw CLI modified guarded repo config copy during ${context}." >&2
    echo "       expected ${REPO_CONFIG_PREFLIGHT_BYTES} bytes sha256 ${REPO_CONFIG_PREFLIGHT_HASH}; got ${current_bytes} bytes sha256 ${current_hash}." >&2
    return 1
  fi
}

run_openclaw_cli_for_guarded_repo_config() {
  local status
  prepare_repo_config_preflight_copy || return 1
  if run_openclaw_cli_for_config "${REPO_CONFIG_PREFLIGHT_COPY}" "$@"; then
    status=0
  else
    status=$?
  fi
  if ! verify_repo_config_preflight_copy_unchanged "openclaw $*"; then
    return 1
  fi
  return "${status}"
}

cleanup_repo_config_preflight_copy() {
  if [[ -n "${REPO_CONFIG_PREFLIGHT_DIR:-}" ]]; then
    guarded_rm_rf "${REPO_CONFIG_PREFLIGHT_DIR}" "cleaning guarded repo OpenClaw config preflight directory ${REPO_CONFIG_PREFLIGHT_DIR}" || return 1
  fi
  REPO_CONFIG_PREFLIGHT_COPY=""
  REPO_CONFIG_PREFLIGHT_DIR=""
}

push_test_checkpoint() {
  local name="$1"
  if [[ -n "${OPENCLAW_PUSH_TEST_CHECKPOINT_LOG:-}" ]]; then
    printf '%s\n' "${name}" >> "${OPENCLAW_PUSH_TEST_CHECKPOINT_LOG}"
  fi
  if [[ "${OPENCLAW_PUSH_TEST_SIGNAL_AT:-}" == "${name}" ]]; then
    kill "-${OPENCLAW_PUSH_TEST_SIGNAL:-INT}" "$$"
  fi
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
  guarded_mkdir_p "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "creating managed systemd drop-in directory ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"
  if [[ ! -O "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    echo "ERROR: Runtime caps drop-in directory is not owned by the current user: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
    return 1
  fi
  guarded_chmod 0755 "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "chmod managed systemd drop-in directory ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"
}

decode_systemd_show_environment_word() {
  local encoded="$1"
  local decoded="" char next i len escape_digits code

  if [[ "${encoded}" == \$\'* ]]; then
    len="${#encoded}"
    i=2
    while ((i < len)); do
      char="${encoded:i:1}"
      if [[ "${char}" == "'" ]]; then
        ((i++))
        [[ "${i}" -eq "${len}" ]] || return 1
        printf '%s' "${decoded}"
        return 0
      fi
      if [[ "${char}" == '\' ]]; then
        ((i++))
        ((i < len)) || return 1
        next="${encoded:i:1}"
        case "${next}" in
          "n")
            decoded+=$'\n'
            ;;
          "t")
            decoded+=$'\t'
            ;;
          "r")
            decoded+=$'\r'
            ;;
          "b")
            decoded+=$'\b'
            ;;
          "f")
            decoded+=$'\f'
            ;;
          "v")
            decoded+=$'\v'
            ;;
          "a")
            decoded+=$'\a'
            ;;
          "e"|"E")
            decoded+=$'\e'
            ;;
          "\\"|"'"|'"'|"?")
            decoded+="${next}"
            ;;
          "x")
            ((i + 2 < len)) || return 1
            escape_digits="${encoded:i+1:2}"
            [[ "${escape_digits}" =~ ^[[:xdigit:]]{2}$ ]] || return 1
            if ((i + 3 < len)) && [[ "${encoded:i+3:1}" =~ [[:xdigit:]] ]]; then
              return 1
            fi
            code=$((16#${escape_digits}))
            ((code != 0)) || return 1
            printf -v char "\\$(printf '%03o' "${code}")"
            decoded+="${char}"
            ((i += 2))
            ;;
          [0-7])
            ((i + 2 < len)) || return 1
            escape_digits="${encoded:i:3}"
            [[ "${escape_digits}" =~ ^[0-7]{3}$ ]] || return 1
            code=$((8#${escape_digits}))
            ((code != 0 && code <= 255)) || return 1
            printf -v char "\\$(printf '%03o' "${code}")"
            decoded+="${char}"
            ((i += 2))
            ;;
          *)
            return 1
            ;;
        esac
      else
        decoded+="${char}"
      fi
      ((i++))
    done
    return 1
  fi

  if [[ "${encoded}" == "'"*"'" ]]; then
    decoded="${encoded:1:${#encoded}-2}"
    [[ "${decoded}" != *"'"* ]] || return 1
    printf '%s' "${decoded}"
    return 0
  fi

  if [[ "${encoded}" =~ ^[-._~/:@%+=,A-Za-z0-9]*$ ]]; then
    printf '%s' "${encoded}"
    return 0
  fi

  return 1
}

decode_systemd_show_environment_node_options() {
  local manager_env="$1"
  local manager_line decoded_node_options=""
  SYSTEMD_DECODED_NODE_OPTIONS_PRESENT=0
  SYSTEMD_DECODED_NODE_OPTIONS_VALUE=""
  while IFS= read -r manager_line; do
    case "${manager_line}" in
      NODE_OPTIONS=*)
        if [[ "${SYSTEMD_DECODED_NODE_OPTIONS_PRESENT}" -eq 1 ]]; then
          echo "ERROR: systemd user manager emitted multiple NODE_OPTIONS assignments; refusing ambiguous cleanup." >&2
          return 1
        fi
        SYSTEMD_DECODED_NODE_OPTIONS_PRESENT=1
        if ! decoded_node_options="$(decode_systemd_show_environment_word "${manager_line#NODE_OPTIONS=}")"; then
          echo "ERROR: Could not safely decode systemd user manager NODE_OPTIONS assignment; refusing ambiguous cleanup." >&2
          return 1
        fi
        SYSTEMD_DECODED_NODE_OPTIONS_VALUE="${decoded_node_options}"
        ;;
    esac
  done <<< "${manager_env}"
}

verify_systemd_manager_node_options_stale_preload_absent() {
  local manager_env
  if ! manager_env="$(systemctl --user show-environment 2>/dev/null)"; then
    echo "ERROR: Could not re-check systemd user manager environment after unsetting NODE_OPTIONS." >&2
    return 1
  fi
  if ! decode_systemd_show_environment_node_options "${manager_env}"; then
    return 1
  fi
  if [[ "${SYSTEMD_DECODED_NODE_OPTIONS_PRESENT}" -eq 1 \
    && "${SYSTEMD_DECODED_NODE_OPTIONS_VALUE}" == *"${STALE_AZURE_PRELOAD_PATTERN}"* ]]; then
    echo "ERROR: systemd user manager still exposes stale Azure NODE_OPTIONS after unset-environment; refusing to continue." >&2
    return 1
  fi
}

remove_stale_azure_node_options_for_codex() {
  local changed=0
  local path temp rewrite_status
  local manager_env
  local service_path="${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}"
  local dropin_scan_output=""
  local -a candidates=()

  if manager_env="$(systemctl --user show-environment 2>/dev/null)"; then
    if ! decode_systemd_show_environment_node_options "${manager_env}"; then
      return 1
    fi
    if [[ "${SYSTEMD_DECODED_NODE_OPTIONS_PRESENT}" -eq 1 \
      && "${SYSTEMD_DECODED_NODE_OPTIONS_VALUE}" == *"${STALE_AZURE_PRELOAD_PATTERN}"* ]]; then
      SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT=1
      SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL="${SYSTEMD_DECODED_NODE_OPTIONS_VALUE}"
      SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED=1
      if ! systemctl --user unset-environment NODE_OPTIONS; then
        echo "ERROR: Failed to unset stale Azure NODE_OPTIONS from systemd user manager environment." >&2
        return 1
      fi
      if ! verify_systemd_manager_node_options_stale_preload_absent; then
        return 1
      fi
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
    guard_destination_path_chain "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "scanning managed systemd drop-in directory ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" || return 1
    dropin_scan_output="$(mktemp "${TMPDIR:-/tmp}/push-openclaw-systemd-dropins.XXXXXX")"
    guard_destination_path_chain "${dropin_scan_output}" "creating managed systemd scan output ${dropin_scan_output}" || return 1
    if ! collect_find_results_null \
      "${dropin_scan_output}" \
      "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" \
      "scanning managed systemd drop-in directory ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" \
      -maxdepth 1 -type f -name '*.conf'; then
      return 1
    fi
    while IFS= read -r -d '' path; do
      candidates+=("${path}")
    done < "${dropin_scan_output}"
    guarded_rm_f "${dropin_scan_output}" "removing managed systemd scan output ${dropin_scan_output}" || return 1
  fi

  for path in "${candidates[@]}"; do
    guard_destination_path_chain "${path}" "rewriting managed systemd environment file ${path}" || return 1
    temp="$(mktemp "${path}.XXXXXX")"
    guard_destination_path_chain "${temp}" "creating temporary managed systemd rewrite file ${temp}" || return 1
    if awk -v stale="${STALE_AZURE_PRELOAD_PATTERN}" '
      function append_char(value, char) {
        return value char
      }
      function read_environment_token(rest, start, result,    pos, len, char, quote, next_char) {
        pos = start
        len = length(rest)
        quote = ""
        token_raw = ""
        token_value = ""
        while (pos <= len) {
          char = substr(rest, pos, 1)
          if (quote == "" && char ~ /[[:space:]]/) {
            break
          }
          token_raw = append_char(token_raw, char)
          if (quote != "") {
            if (char == "\\") {
              if (pos < len) {
                next_char = substr(rest, pos + 1, 1)
                token_raw = append_char(token_raw, next_char)
                token_value = append_char(token_value, next_char)
                pos += 2
                continue
              }
            } else if (char == quote) {
              quote = ""
              pos++
              continue
            } else {
              token_value = append_char(token_value, char)
            }
          } else if (char == "\"" || char == "'"'"'") {
            quote = char
          } else if (char == "\\") {
            if (pos < len) {
              next_char = substr(rest, pos + 1, 1)
              token_raw = append_char(token_raw, next_char)
              token_value = append_char(token_value, next_char)
              pos += 2
              continue
            }
          } else {
            token_value = append_char(token_value, char)
          }
          pos++
        }
        result["raw"] = token_raw
        result["value"] = token_value
        result["next"] = pos
        result["ok"] = quote == "" && token_raw != ""
      }
      function rewrite_environment_line(line,    prefix, rest, output, kept, removed, pos, ws_start, whitespace) {
        rewrite_output = ""
        if (!match(line, /^[[:space:]]*Environment=/)) {
          return 0
        }
        prefix = substr(line, 1, RLENGTH)
        rest = substr(line, RLENGTH + 1)
        output = ""
        kept = 0
        removed = 0
        pos = 1
        while (pos <= length(rest)) {
          ws_start = pos
          while (pos <= length(rest) && substr(rest, pos, 1) ~ /[[:space:]]/) {
            pos++
          }
          whitespace = substr(rest, ws_start, pos - ws_start)
          if (pos > length(rest)) {
            break
          }
          delete token
          read_environment_token(rest, pos, token)
          if (!token["ok"]) {
            if (index(line, stale) > 0) {
              parse_error = 1
            }
            return 0
          }
          if (token["value"] ~ /^NODE_OPTIONS=/ && index(token["value"], stale) > 0) {
            removed = 1
          } else if (kept == 0) {
            output = prefix token["raw"]
            kept = 1
          } else {
            output = output whitespace token["raw"]
          }
          pos = token["next"]
        }
        if (removed) {
          rewrite_output = output
          return 1
        }
        return 0
      }
      function flush_block() {
        if (rewrite_environment_line(logical)) {
          changed = 1
          if (rewrite_output != "") {
            print rewrite_output
          }
          printf "%s", ignored_block
        } else {
          printf "%s", block
        }
        block = ""
        logical = ""
        ignored_block = ""
      }
      function is_ignored_continuation_line(line) {
        return line ~ /^[[:space:]]*($|#|;)/
      }
      {
        if (logical != "" && is_ignored_continuation_line($0)) {
          block = block $0 ORS
          ignored_block = ignored_block $0 ORS
          next
        }
        if (logical == "" && is_ignored_continuation_line($0)) {
          print
          next
        }
        continued = $0 ~ /\\$/
        part = continued ? substr($0, 1, length($0) - 1) : $0
        block = block $0 ORS
        if (logical == "") {
          logical = part
        } else {
          logical = logical " " part
        }
        if (!continued) {
          flush_block()
        }
      }
      END {
        if (block != "") {
          parse_error = 1
        }
        if (parse_error) {
          exit 20
        }
        exit changed ? 10 : 0
      }
    ' "${path}" > "${temp}"; then
      rewrite_status=0
    else
      rewrite_status=$?
    fi
    case "${rewrite_status}" in
      0)
        guarded_rm_f "${temp}" "removing unchanged temporary systemd rewrite file ${temp}"
        continue
        ;;
      10)
        ;;
      *)
        guarded_rm_f "${temp}" "removing failed temporary systemd rewrite file ${temp}"
        echo "ERROR: Failed to inspect ${path} for stale Azure NODE_OPTIONS assignment lines." >&2
        return 1
        ;;
    esac
    if ! guarded_chmod_reference "${path}" "${temp}" "preserving permissions while rewriting ${path} via ${temp}"; then
      guarded_rm_f "${temp}" "removing temporary systemd rewrite file ${temp} after chmod failure"
      echo "ERROR: Failed to preserve permissions while rewriting ${path}." >&2
      return 1
    fi
    if ! guarded_mv_replace "${temp}" "${path}" "publishing rewritten managed systemd environment file ${path}"; then
      guarded_rm_f "${temp}" "removing temporary systemd rewrite file ${temp} after publish failure"
      echo "ERROR: Failed to rewrite stale Azure NODE_OPTIONS assignment lines in ${path}." >&2
      return 1
    fi
    changed=1
    echo "Removed stale Azure NODE_OPTIONS assignment line from ${path}"
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
  if ! version_line="$(run_openclaw_cli_for_repo_config --version 2>&1)"; then
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
  if ! inspect_json="$(run_openclaw_cli_for_repo_config plugins inspect codex --json)"; then
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

validate_repo_openclaw_config() {
  local validate_json validate_status
  if validate_json="$(run_openclaw_cli_for_repo_config config validate --json 2>&1)"; then
    validate_status=0
  else
    validate_status=$?
  fi
  if [[ "${validate_status}" -ne 0 ]] || ! printf '%s\n' "${validate_json}" | openclaw_schema_validation_is_clean; then
    echo "ERROR: Repo OpenClaw config failed schema validation before runtime preflight." >&2
    printf '%s\n' "${validate_json}" >&2
    return 1
  fi
  echo "Repo OpenClaw config schema validated with ${OPENCLAW_BIN_RESOLVED} config validate --json."
}

ROLLBACK_ARMED=0
ROLLBACK_FAILED=0
DEPLOYMENT_COMMITTED=0
POST_COMMIT_CLEANUP_FAILED=0
MANAGED_UNIT_TRANSACTION_ARMED=0
MANAGED_UNIT_BACKUP_DIR=""
MANAGED_UNIT_PATHS=(
  "${SUPERVISOR_UNIT_DST}"
  "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
  "${CODEX_RUNTIME_DROPIN_DST}"
  "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
)
MANAGED_UNIT_WAS_PRESENT=()
MANAGED_UNIT_SNAPSHOT_STATE=()
MANAGED_ARTIFACT_TRANSACTION_ARMED=0
MANAGED_ARTIFACT_BACKUP_DIR=""
MANAGED_ARTIFACT_PATHS=()
MANAGED_ARTIFACT_WAS_PRESENT=()
MANAGED_ARTIFACT_SNAPSHOT_STATE=()
declare -A MANAGED_ARTIFACT_SEEN=()
MANAGED_ARTIFACT_RESTORED_SYSTEMD=0
SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED=0
SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT=0
SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL=""

cleanup_deployment_temp_file() {
  local path="${1:-}"
  [[ -n "${path}" ]] || return 0
  if ! guarded_rm_f "${path}" "removing temporary deployment file ${path}"; then
    echo "ERROR: Failed to remove temporary deployment file ${path}." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
}

begin_managed_unit_transaction() {
  local index path backup_path transaction_failed=0

  guard_destination_path_chain "${SYSTEMD_USER_DIR}" "creating managed systemd transaction backup directory under ${SYSTEMD_USER_DIR}" || return 1
  MANAGED_UNIT_BACKUP_DIR="$(mktemp -d "${SYSTEMD_USER_DIR}/.push-openclaw-config-units.XXXXXX")"
  guard_destination_path_chain "${MANAGED_UNIT_BACKUP_DIR}" "created managed systemd transaction backup directory ${MANAGED_UNIT_BACKUP_DIR}" || return 1
  for index in "${!MANAGED_UNIT_PATHS[@]}"; do
    path="${MANAGED_UNIT_PATHS[${index}]}"
    backup_path="${MANAGED_UNIT_BACKUP_DIR}/${index}"
    MANAGED_UNIT_SNAPSHOT_STATE[${index}]="failed"
    if path_exists_or_symlink "${path}"; then
      if [[ -L "${path}" ]]; then
        echo "ERROR: Managed systemd file ${path} is a symlink to $(readlink "${path}"); this publication path cannot preserve symlink topology safely." >&2
        echo "       Refusing before mutating managed systemd files." >&2
        echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
        continue
      fi
      if [[ ! -f "${path}" ]]; then
        echo "ERROR: Managed systemd file ${path} exists but is not a regular file; refusing before mutation." >&2
        echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
        continue
      fi
      if ! guard_destination_path_chain "${path}" "snapshotting managed systemd file ${path}"; then
        echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
        continue
      fi
      if ! guarded_copy_path_topology "${path}" "${backup_path}" "snapshotting managed systemd file ${path}"; then
        echo "ERROR: Failed to snapshot managed systemd file ${path} to ${backup_path}." >&2
        echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
        continue
      fi
      MANAGED_UNIT_WAS_PRESENT[${index}]=1
      MANAGED_UNIT_SNAPSHOT_STATE[${index}]="present"
    else
      MANAGED_UNIT_WAS_PRESENT[${index}]=0
      MANAGED_UNIT_SNAPSHOT_STATE[${index}]="absent"
    fi
  done
  MANAGED_UNIT_TRANSACTION_ARMED=1
  if [[ "${transaction_failed}" -ne 0 ]]; then
    return 1
  fi
}

rollback_managed_unit_transaction() {
  local index path backup_path state transaction_failed=0

  if [[ "${MANAGED_UNIT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi

  echo "Restoring managed systemd files after failed publication." >&2
  for index in "${!MANAGED_UNIT_PATHS[@]}"; do
    path="${MANAGED_UNIT_PATHS[${index}]}"
    backup_path="${MANAGED_UNIT_BACKUP_DIR}/${index}"
    state="${MANAGED_UNIT_SNAPSHOT_STATE[${index}]:-failed}"
    if [[ "${state}" == "failed" ]]; then
      echo "ERROR: Skipping rollback for ${path}; its managed systemd snapshot did not complete." >&2
      ROLLBACK_FAILED=1
      transaction_failed=1
      continue
    fi
    if [[ "${state}" == "present" ]]; then
      if ! restore_path_topology_from_backup "${backup_path}" "${path}" "${MANAGED_UNIT_BACKUP_DIR}/restore.${index}"; then
        echo "ERROR: Failed to restore managed systemd file ${path}." >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
      fi
    elif [[ "${state}" == "absent" ]]; then
      if ! guarded_rm_f "${path}" "removing newly installed managed systemd file ${path} during rollback"; then
        echo "ERROR: Failed to remove newly installed managed systemd file ${path}." >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
      fi
    else
      echo "ERROR: Unknown managed systemd snapshot state ${state} for ${path}; leaving it untouched." >&2
      ROLLBACK_FAILED=1
      transaction_failed=1
    fi
  done
  if ! systemctl --user daemon-reload; then
    echo "ERROR: Failed to reload user systemd units after managed-file rollback." >&2
    ROLLBACK_FAILED=1
    transaction_failed=1
  fi
  if [[ "${transaction_failed}" -eq 1 ]]; then
    echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
    return 1
  fi
  MANAGED_UNIT_TRANSACTION_ARMED=0
}

finalize_managed_unit_transaction() {
  local index backup_path state
  if [[ "${MANAGED_UNIT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    echo "ERROR: Managed systemd transaction was not armed at deployment commit." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  if [[ -z "${MANAGED_UNIT_BACKUP_DIR:-}" || ! -d "${MANAGED_UNIT_BACKUP_DIR}" ]]; then
    echo "ERROR: Managed systemd backup directory is missing at deployment commit: ${MANAGED_UNIT_BACKUP_DIR:-<unset>}" >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  for index in "${!MANAGED_UNIT_PATHS[@]}"; do
    backup_path="${MANAGED_UNIT_BACKUP_DIR}/${index}"
    state="${MANAGED_UNIT_SNAPSHOT_STATE[${index}]:-failed}"
    case "${state}" in
      present)
        if ! path_exists_or_symlink "${backup_path}"; then
          echo "ERROR: Managed systemd backup component is missing at deployment commit: ${backup_path}" >&2
          ROLLBACK_FAILED=1
          return 1
        fi
        ;;
      absent)
        ;;
      failed)
        echo "ERROR: Managed systemd snapshot did not complete before deployment commit: ${MANAGED_UNIT_PATHS[${index}]}" >&2
        ROLLBACK_FAILED=1
        return 1
        ;;
      *)
        echo "ERROR: Managed systemd snapshot state is invalid for ${MANAGED_UNIT_PATHS[${index}]}: ${state}" >&2
        ROLLBACK_FAILED=1
        return 1
        ;;
    esac
  done
}

cleanup_managed_unit_backup_dir() {
  if [[ -n "${MANAGED_UNIT_BACKUP_DIR:-}" ]]; then
    if ! guarded_rm_rf "${MANAGED_UNIT_BACKUP_DIR}" "removing managed systemd backup directory ${MANAGED_UNIT_BACKUP_DIR}"; then
      echo "ERROR: Failed to remove managed systemd backup directory ${MANAGED_UNIT_BACKUP_DIR}." >&2
      echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
      POST_COMMIT_CLEANUP_FAILED=1
      return 1
    fi
  fi
  MANAGED_UNIT_BACKUP_DIR=""
  MANAGED_UNIT_WAS_PRESENT=()
  MANAGED_UNIT_SNAPSHOT_STATE=()
}

begin_managed_artifact_transaction() {
  if [[ "${MANAGED_ARTIFACT_TRANSACTION_ARMED:-0}" -eq 1 ]]; then
    return 0
  fi
  guard_destination_path_chain "${OPENCLAW_PUSH_HOME}" "creating managed OpenClaw artifact backup directory under ${OPENCLAW_PUSH_HOME}" || return 1
  MANAGED_ARTIFACT_BACKUP_DIR="$(mktemp -d "${OPENCLAW_PUSH_HOME}/.push-openclaw-config-artifacts.XXXXXX")"
  guard_destination_path_chain "${MANAGED_ARTIFACT_BACKUP_DIR}" "created managed OpenClaw artifact backup directory ${MANAGED_ARTIFACT_BACKUP_DIR}" || return 1
  MANAGED_ARTIFACT_TRANSACTION_ARMED=1
}

snapshot_managed_artifact_path() {
  local path="$1"
  local index backup_path state

  begin_managed_artifact_transaction || return 1
  if [[ -n "${MANAGED_ARTIFACT_SEEN[${path}]+x}" ]]; then
    index="${MANAGED_ARTIFACT_SEEN[${path}]}"
    state="${MANAGED_ARTIFACT_SNAPSHOT_STATE[${index}]:-failed}"
    if [[ "${state}" == "failed" ]]; then
      return 1
    fi
    return 0
  fi
  index="${#MANAGED_ARTIFACT_PATHS[@]}"
  MANAGED_ARTIFACT_PATHS+=("${path}")
  MANAGED_ARTIFACT_SEEN["${path}"]="${index}"
  MANAGED_ARTIFACT_SNAPSHOT_STATE[${index}]="failed"
  backup_path="${MANAGED_ARTIFACT_BACKUP_DIR}/${index}"
  if path_exists_or_symlink "${path}"; then
    if [[ -L "${path}" ]]; then
      echo "ERROR: Managed OpenClaw artifact ${path} is a symlink to $(readlink "${path}"); this publication path cannot preserve symlink topology safely." >&2
      echo "       Refusing before mutating the managed artifact path." >&2
      echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
      ROLLBACK_FAILED=1
      return 1
    fi
    if ! guard_destination_path_chain "${path}" "snapshotting managed OpenClaw artifact ${path}"; then
      echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
      ROLLBACK_FAILED=1
      return 1
    fi
    if ! guarded_copy_path_topology "${path}" "${backup_path}" "snapshotting managed OpenClaw artifact ${path}"; then
      echo "ERROR: Failed to snapshot managed OpenClaw artifact ${path} to ${backup_path}." >&2
      echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
      ROLLBACK_FAILED=1
      return 1
    fi
    MANAGED_ARTIFACT_WAS_PRESENT[${index}]=1
    MANAGED_ARTIFACT_SNAPSHOT_STATE[${index}]="present"
  else
    MANAGED_ARTIFACT_WAS_PRESENT[${index}]=0
    MANAGED_ARTIFACT_SNAPSHOT_STATE[${index}]="absent"
  fi
}

is_systemd_managed_artifact_path() {
  local path="$1"
  [[ "${path}" == "${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}" \
    || "${path}" == "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" \
    || "${path}" == "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/"* ]]
}

rollback_managed_artifact_transaction() {
  local index path backup_path restore_stage state transaction_failed=0

  if [[ "${MANAGED_ARTIFACT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi

  echo "Restoring managed OpenClaw artifacts after failed publication." >&2
  for ((index=${#MANAGED_ARTIFACT_PATHS[@]} - 1; index >= 0; index--)); do
    path="${MANAGED_ARTIFACT_PATHS[${index}]}"
    backup_path="${MANAGED_ARTIFACT_BACKUP_DIR}/${index}"
    restore_stage="${MANAGED_ARTIFACT_BACKUP_DIR}/restore.${index}"
    state="${MANAGED_ARTIFACT_SNAPSHOT_STATE[${index}]:-failed}"
    if [[ "${state}" == "failed" ]]; then
      echo "ERROR: Skipping rollback for ${path}; its managed artifact snapshot did not complete." >&2
      ROLLBACK_FAILED=1
      transaction_failed=1
      continue
    fi
    if [[ "${state}" == "present" ]]; then
      if ! path_exists_or_symlink "${backup_path}"; then
        echo "ERROR: Managed artifact backup is missing for ${path}: ${backup_path}" >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
        continue
      fi
      if is_systemd_managed_artifact_path "${path}"; then
        MANAGED_ARTIFACT_RESTORED_SYSTEMD=1
      fi
      if ! restore_path_topology_from_backup "${backup_path}" "${path}" "${restore_stage}"; then
        echo "ERROR: Failed to restore managed artifact ${path}." >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
      fi
    elif [[ "${state}" == "absent" ]]; then
      if is_systemd_managed_artifact_path "${path}"; then
        MANAGED_ARTIFACT_RESTORED_SYSTEMD=1
      fi
      if ! guarded_rm_rf "${path}" "removing newly installed managed artifact ${path} during rollback"; then
        echo "ERROR: Failed to remove newly installed managed artifact ${path} during rollback." >&2
        ROLLBACK_FAILED=1
        transaction_failed=1
      fi
    else
      echo "ERROR: Unknown managed artifact snapshot state ${state} for ${path}; leaving it untouched." >&2
      ROLLBACK_FAILED=1
      transaction_failed=1
    fi
  done
  if [[ "${transaction_failed}" -eq 1 ]]; then
    echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
    return 1
  fi
  MANAGED_ARTIFACT_TRANSACTION_ARMED=0
}

final_systemd_reload_after_artifact_rollback() {
  if [[ "${MANAGED_ARTIFACT_RESTORED_SYSTEMD:-0}" -ne 1 ]]; then
    return 0
  fi
  if ! systemctl --user daemon-reload; then
    echo "ERROR: Failed final user systemd daemon-reload after managed artifact rollback restored systemd files." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  MANAGED_ARTIFACT_RESTORED_SYSTEMD=0
}

finalize_managed_artifact_transaction() {
  local index backup_path path state
  if [[ "${MANAGED_ARTIFACT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    echo "ERROR: Managed OpenClaw artifact transaction was not armed at deployment commit." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  if [[ -z "${MANAGED_ARTIFACT_BACKUP_DIR:-}" || ! -d "${MANAGED_ARTIFACT_BACKUP_DIR}" ]]; then
    echo "ERROR: Managed OpenClaw artifact backup directory is missing at deployment commit: ${MANAGED_ARTIFACT_BACKUP_DIR:-<unset>}" >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  for index in "${!MANAGED_ARTIFACT_PATHS[@]}"; do
    path="${MANAGED_ARTIFACT_PATHS[${index}]}"
    backup_path="${MANAGED_ARTIFACT_BACKUP_DIR}/${index}"
    state="${MANAGED_ARTIFACT_SNAPSHOT_STATE[${index}]:-failed}"
    case "${state}" in
      present)
        if ! path_exists_or_symlink "${backup_path}"; then
          echo "ERROR: Managed OpenClaw artifact backup component is missing at deployment commit: ${backup_path}" >&2
          ROLLBACK_FAILED=1
          return 1
        fi
        ;;
      absent)
        ;;
      failed)
        echo "ERROR: Managed OpenClaw artifact snapshot did not complete before deployment commit: ${path}" >&2
        ROLLBACK_FAILED=1
        return 1
        ;;
      *)
        echo "ERROR: Managed OpenClaw artifact snapshot state is invalid for ${path}: ${state}" >&2
        ROLLBACK_FAILED=1
        return 1
        ;;
    esac
  done
}

cleanup_managed_artifact_backup_dir() {
  if [[ -n "${MANAGED_ARTIFACT_BACKUP_DIR:-}" ]]; then
    if ! guarded_rm_rf "${MANAGED_ARTIFACT_BACKUP_DIR}" "removing managed OpenClaw artifact backup directory ${MANAGED_ARTIFACT_BACKUP_DIR}"; then
      echo "ERROR: Failed to remove managed OpenClaw artifact backup directory ${MANAGED_ARTIFACT_BACKUP_DIR}." >&2
      echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
      POST_COMMIT_CLEANUP_FAILED=1
      return 1
    fi
  fi
  MANAGED_ARTIFACT_BACKUP_DIR=""
  MANAGED_ARTIFACT_PATHS=()
  MANAGED_ARTIFACT_WAS_PRESENT=()
  MANAGED_ARTIFACT_SNAPSHOT_STATE=()
  MANAGED_ARTIFACT_SEEN=()
  MANAGED_ARTIFACT_RESTORED_SYSTEMD=0
}

restore_systemd_manager_environment_snapshot() {
  if [[ "${SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED:-0}" -ne 1 ]]; then
    return 0
  fi
  if [[ "${SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT:-0}" -eq 1 ]]; then
    if ! systemctl --user set-environment "NODE_OPTIONS=${SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL}"; then
      echo "ERROR: Failed to restore systemd user manager NODE_OPTIONS during rollback." >&2
      ROLLBACK_FAILED=1
      return 1
    fi
  elif ! systemctl --user unset-environment NODE_OPTIONS; then
    echo "ERROR: Failed to unset systemd user manager NODE_OPTIONS during rollback." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED=0
  SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT=0
  SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL=""
}

finalize_systemd_manager_environment_snapshot() {
  return 0
}

finalize_local_config_backup() {
  if [[ "${ROLLBACK_ARMED:-0}" -ne 1 ]]; then
    echo "ERROR: Local OpenClaw config rollback was not armed at deployment commit." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  if [[ -z "${BACKUP:-}" ]] || ! path_exists_or_symlink "${BACKUP}"; then
    echo "ERROR: Local OpenClaw config backup is missing at deployment commit: ${BACKUP:-<unset>}" >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  if [[ ! -f "${LOCAL_CONFIG}" ]]; then
    echo "ERROR: Local OpenClaw config is missing at deployment commit: ${LOCAL_CONFIG}" >&2
    ROLLBACK_FAILED=1
    return 1
  fi
}

mark_deployment_committed() {
  ROLLBACK_ARMED=0
  MANAGED_UNIT_TRANSACTION_ARMED=0
  MANAGED_ARTIFACT_TRANSACTION_ARMED=0
  SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED=0
  SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT=0
  SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL=""
  DEPLOYMENT_COMMITTED=1
}

cleanup_committed_recovery_paths() {
  local cleanup_failed=0

  if ! cleanup_repo_config_preflight_copy; then
    echo "ERROR: Failed to remove guarded repo OpenClaw config copy ${REPO_CONFIG_PREFLIGHT_DIR:-<unset>}." >&2
    POST_COMMIT_CLEANUP_FAILED=1
    cleanup_failed=1
  fi
  if ! cleanup_managed_unit_backup_dir; then
    cleanup_failed=1
  fi
  if ! cleanup_managed_artifact_backup_dir; then
    cleanup_failed=1
  fi
  if [[ "${POST_COMMIT_CLEANUP_FAILED:-0}" -ne 0 || "${cleanup_failed}" -ne 0 ]]; then
    return 1
  fi
}

cleanup_rollback_recovery_paths() {
  local cleanup_failed=0

  if [[ -n "${MANAGED_UNIT_BACKUP_DIR:-}" ]]; then
    if ! guarded_rm_rf "${MANAGED_UNIT_BACKUP_DIR}" "removing managed systemd backup directory ${MANAGED_UNIT_BACKUP_DIR} after rollback"; then
      echo "ERROR: Failed to remove managed systemd backup directory ${MANAGED_UNIT_BACKUP_DIR} after rollback." >&2
      echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
      ROLLBACK_FAILED=1
      cleanup_failed=1
    else
      MANAGED_UNIT_BACKUP_DIR=""
    fi
  fi
  if [[ -n "${MANAGED_ARTIFACT_BACKUP_DIR:-}" ]]; then
    if ! guarded_rm_rf "${MANAGED_ARTIFACT_BACKUP_DIR}" "removing managed OpenClaw artifact backup directory ${MANAGED_ARTIFACT_BACKUP_DIR} after rollback"; then
      echo "ERROR: Failed to remove managed OpenClaw artifact backup directory ${MANAGED_ARTIFACT_BACKUP_DIR} after rollback." >&2
      echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
      ROLLBACK_FAILED=1
      cleanup_failed=1
    else
      MANAGED_ARTIFACT_BACKUP_DIR=""
    fi
  fi
  if [[ -n "${REPO_CONFIG_PREFLIGHT_DIR:-}" ]]; then
    if ! guarded_rm_rf "${REPO_CONFIG_PREFLIGHT_DIR}" "removing guarded repo OpenClaw config copy ${REPO_CONFIG_PREFLIGHT_DIR} after rollback"; then
      echo "ERROR: Failed to remove guarded repo OpenClaw config copy ${REPO_CONFIG_PREFLIGHT_DIR} after rollback." >&2
      ROLLBACK_FAILED=1
      cleanup_failed=1
    else
      REPO_CONFIG_PREFLIGHT_COPY=""
      REPO_CONFIG_PREFLIGHT_DIR=""
    fi
  fi
  return "${cleanup_failed}"
}

report_retained_recovery_paths() {
  if [[ "${ROLLBACK_ARMED:-0}" -eq 1 && -n "${BACKUP:-}" ]] && path_exists_or_symlink "${BACKUP}"; then
    echo "Local OpenClaw config recoverable backup preserved at ${BACKUP}" >&2
  fi
  if [[ -n "${MANAGED_UNIT_BACKUP_DIR:-}" && -d "${MANAGED_UNIT_BACKUP_DIR}" ]]; then
    echo "Managed systemd recovery directory preserved at ${MANAGED_UNIT_BACKUP_DIR}" >&2
  fi
  if [[ -n "${MANAGED_ARTIFACT_BACKUP_DIR:-}" && -d "${MANAGED_ARTIFACT_BACKUP_DIR}" ]]; then
    echo "Managed OpenClaw artifact recovery directory preserved at ${MANAGED_ARTIFACT_BACKUP_DIR}" >&2
  fi
  if [[ -n "${REPO_CONFIG_PREFLIGHT_DIR:-}" && -d "${REPO_CONFIG_PREFLIGHT_DIR}" ]]; then
    echo "Guarded repo OpenClaw config copy preserved at ${REPO_CONFIG_PREFLIGHT_DIR}" >&2
  fi
}

commit_deployment_boundary() {
  local finalize_failed=0

  trap '' HUP INT TERM
  push_test_checkpoint "commit-boundary-entered"
  finalize_local_config_backup || finalize_failed=1
  finalize_managed_unit_transaction || finalize_failed=1
  finalize_managed_artifact_transaction || finalize_failed=1
  finalize_systemd_manager_environment_snapshot || finalize_failed=1
  if [[ "${finalize_failed}" -ne 0 ]]; then
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    return 1
  fi

  mark_deployment_committed
  trap - EXIT
  trap - HUP INT TERM

  cleanup_committed_recovery_paths
}

restore_local_config_backup() {
  local restore_stage restore_stage_dir
  if [[ "${ROLLBACK_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi
  guard_destination_path_chain "${OPENCLAW_PUSH_HOME}" "creating local OpenClaw config rollback staging directory under ${OPENCLAW_PUSH_HOME}" || return 1
  restore_stage_dir="$(mktemp -d "${OPENCLAW_PUSH_HOME}/.openclaw.rollback.XXXXXX")"
  guard_destination_path_chain "${restore_stage_dir}" "created local OpenClaw config rollback staging directory ${restore_stage_dir}" || return 1
  restore_stage="${restore_stage_dir}/openclaw.json"
  if ! guarded_copy_path_topology_preserving_final_symlink_topology "${BACKUP}" "${restore_stage}" "staging local OpenClaw config rollback file ${restore_stage}"; then
    echo "ERROR: Failed to stage backup ${BACKUP} for rollback to ${LOCAL_CONFIG}." >&2
    if ! guarded_rm_rf "${restore_stage_dir}" "removing local OpenClaw config rollback staging directory ${restore_stage_dir} after stage failure"; then
      echo "ERROR: Failed to remove local OpenClaw config rollback staging directory ${restore_stage_dir} after stage failure." >&2
    fi
    ROLLBACK_FAILED=1
    return 1
  fi
  if ! guarded_mv_replace_preserving_final_symlink_topology "${restore_stage}" "${LOCAL_CONFIG}" "restoring local OpenClaw config ${LOCAL_CONFIG} from rollback stage" -Tf; then
    echo "ERROR: Failed to atomically restore backup ${BACKUP} to ${LOCAL_CONFIG} during rollback." >&2
    echo "       Recoverable backup preserved at ${BACKUP}; staged rollback file preserved at ${restore_stage}." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  if ! guarded_rmdir "${restore_stage_dir}" "removing local OpenClaw config rollback staging directory ${restore_stage_dir} after restoring ${LOCAL_CONFIG}"; then
    echo "ERROR: Failed to remove local OpenClaw config rollback staging directory ${restore_stage_dir} after restoring ${LOCAL_CONFIG}." >&2
    echo "       Recoverable backup preserved at ${BACKUP}; rollback staging directory preserved at ${restore_stage_dir}." >&2
    ROLLBACK_FAILED=1
    return 1
  fi
  ROLLBACK_ARMED=0
}

run_deployment_rollback_and_exit() {
  local exit_status="$1"
  local rollback_step_failed=0
  trap - EXIT
  trap - HUP INT TERM
  if ! cleanup_deployment_temp_file "${GENERATED_OPENCLAW_CONFIG_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! cleanup_deployment_temp_file "${SUPERVISOR_UNIT_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! cleanup_deployment_temp_file "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! cleanup_deployment_temp_file "${CODEX_RUNTIME_DROPIN_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! cleanup_deployment_temp_file "${NATIVE_CRASH_HARDENING_DROPIN_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! rollback_managed_unit_transaction; then
    rollback_step_failed=1
  fi
  if ! rollback_managed_artifact_transaction; then
    rollback_step_failed=1
  fi
  if ! final_systemd_reload_after_artifact_rollback; then
    rollback_step_failed=1
  fi
  if ! restore_systemd_manager_environment_snapshot; then
    rollback_step_failed=1
  fi
  if ! restore_local_config_backup; then
    rollback_step_failed=1
  fi
  if [[ "${ROLLBACK_FAILED:-0}" -ne 0 || "${rollback_step_failed}" -ne 0 ]]; then
    report_retained_recovery_paths
    exit 1
  fi
  if ! cleanup_rollback_recovery_paths; then
    exit 1
  fi
  exit "${exit_status}"
}

rollback_local_config_on_exit() {
  local exit_status=$?
  run_deployment_rollback_and_exit "${exit_status}"
}

# ── Pre-flight checks ───────────────────────────────────────────────────────
trap 'cleanup_repo_config_preflight_copy' EXIT
trap 'cleanup_repo_config_preflight_copy; exit 129' HUP
trap 'cleanup_repo_config_preflight_copy; exit 130' INT
trap 'cleanup_repo_config_preflight_copy; exit 143' TERM

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

if ! validate_repo_openclaw_config; then
  exit 1
fi

if [[ "${OPENCLAW_PROVIDER:-codex}" == "codex" ]]; then
  echo "Running preflight: guarded repo OpenClaw config copy with ${OPENCLAW_BIN_RESOLVED} plugins inspect codex --json"
  if ! require_codex_runtime_exact; then
    exit 1
  fi
fi

# ── Backup ───────────────────────────────────────────────────────────────────
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${LOCAL_CONFIG}.bak.${TIMESTAMP}"
guard_destination_parent_path_chain "${LOCAL_CONFIG}" "snapshotting local OpenClaw config ${LOCAL_CONFIG}" || exit 1
guard_destination_parent_path_chain "${BACKUP}" "creating local OpenClaw config backup ${BACKUP}" || exit 1
guarded_copy_path_topology_preserving_final_symlink_topology "${LOCAL_CONFIG}" "${BACKUP}" "creating local OpenClaw config backup ${BACKUP}"
ROLLBACK_ARMED=1
trap 'rollback_local_config_on_exit' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
echo "Backed up local config → ${BACKUP}"
begin_managed_artifact_transaction

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

guarded_mkdir_p "${MEMPALACE_PALACE}" "creating managed MemPalace palace directory ${MEMPALACE_PALACE}"
guarded_mkdir_p "${FASTEMBED_CACHE_PATH}" "creating managed FastEmbed cache directory ${FASTEMBED_CACHE_PATH}"

if ! "${MEMPALACE_PYTHON}" "${REPO_ROOT}/scripts/check-mempalace-health.py"; then
  echo "ERROR: MemPalace healthcheck failed. Refusing to push OpenClaw config." >&2
  echo "       Fix the palace explicitly; startup will not auto-repair or fall back." >&2
  exit 1
fi

guarded_mkdir_p "${OPENCLAW_PUSH_HOME}" "creating managed OpenClaw home ${OPENCLAW_PUSH_HOME}"
snapshot_managed_artifact_path "${MEMPALACE_READONLY_WRAPPER_DST}"
guarded_cp_file "${MEMPALACE_READONLY_WRAPPER_SRC}" "${MEMPALACE_READONLY_WRAPPER_DST}" "installing MemPalace read-only wrapper ${MEMPALACE_READONLY_WRAPPER_DST}"
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
  local temp_config validate_json validate_status current_hash current_bytes
  guard_destination_path_chain "${OPENCLAW_PUSH_HOME}" "creating generated OpenClaw config temp file under ${OPENCLAW_PUSH_HOME}" || exit 1
  temp_config="$(mktemp "${OPENCLAW_PUSH_HOME}/.openclaw.generated.XXXXXX.json")"
  GENERATED_OPENCLAW_CONFIG_TMP="${temp_config}"
  guard_destination_path_chain "${temp_config}" "writing generated OpenClaw config temp file ${temp_config}" || exit 1
  printf '%s\n' "${MERGED}" | jq . > "${temp_config}"
  guard_destination_path_chain "${temp_config}" "wrote generated OpenClaw config temp file ${temp_config}" || exit 1
  guarded_chmod 0600 "${temp_config}" "chmod generated OpenClaw config temp file ${temp_config}"
  GENERATED_OPENCLAW_CONFIG_IDENTITY="$(guarded_regular_file_identity "${temp_config}" "capturing generated OpenClaw config identity before validation ${temp_config}")" || exit 1
  GENERATED_OPENCLAW_CONFIG_HASH="$(file_sha256 "${temp_config}")"
  GENERATED_OPENCLAW_CONFIG_BYTES="$(file_bytes "${temp_config}")"
  if validate_json="$(run_openclaw_cli_for_config "${temp_config}" config validate --json 2>&1)"; then
    validate_status=0
  else
    validate_status=$?
  fi
  if ! verify_guarded_regular_file_identity_unchanged "${temp_config}" "${GENERATED_OPENCLAW_CONFIG_IDENTITY}" "generated config validation"; then
    echo "ERROR: External OpenClaw CLI changed generated config identity/topology during validation: ${temp_config}." >&2
    exit 1
  fi
  current_hash="$(file_sha256 "${temp_config}")"
  current_bytes="$(file_bytes "${temp_config}")"
  if [[ "${current_hash}" != "${GENERATED_OPENCLAW_CONFIG_HASH}" || "${current_bytes}" != "${GENERATED_OPENCLAW_CONFIG_BYTES}" ]]; then
    echo "ERROR: External OpenClaw CLI modified generated config during validation." >&2
    echo "       expected ${GENERATED_OPENCLAW_CONFIG_BYTES} bytes sha256 ${GENERATED_OPENCLAW_CONFIG_HASH}; got ${current_bytes} bytes sha256 ${current_hash}." >&2
    exit 1
  fi
  if [[ "${validate_status}" -ne 0 ]] || ! printf '%s\n' "${validate_json}" | openclaw_schema_validation_is_clean; then
    echo "ERROR: Generated OpenClaw config failed schema validation before write." >&2
    printf '%s\n' "${validate_json}" >&2
    exit 1
  fi
  echo "Generated OpenClaw config schema validated with ${OPENCLAW_BIN_RESOLVED} config validate --json (${GENERATED_OPENCLAW_CONFIG_BYTES} bytes, sha256 ${GENERATED_OPENCLAW_CONFIG_HASH})."
}

validate_generated_openclaw_config

# ── Publish validated config ────────────────────────────────────────────────
push_test_checkpoint "before-config-publication"
guarded_mv_replace_preserving_final_symlink_topology "${GENERATED_OPENCLAW_CONFIG_TMP}" "${LOCAL_CONFIG}" "publishing validated local OpenClaw config ${LOCAL_CONFIG}" -f
GENERATED_OPENCLAW_CONFIG_TMP=""
echo "Atomically published validated repo config to ${LOCAL_CONFIG}"

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
  guarded_mkdir_p "${codex_home}" "creating managed Codex runtime home ${codex_home}"
  guard_destination_path_chain "${codex_home}/config.toml" "writing managed Codex runtime config ${codex_home}/config.toml"
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
  guard_destination_path_chain "${codex_home}/config.toml" "wrote managed Codex runtime config ${codex_home}/config.toml"
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
  repair_codex_runtime_state_db "${codex_home}"
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
    if not is_repairable_idx_logs_thread_id_only(integrity_after):
        raise SystemExit(
            f"Scoped Codex log DB {log_db} has non-repairable integrity errors "
            f"after REINDEX idx_logs_thread_id: {integrity_after!r}"
        )
    print(f"Rebuilding scoped Codex log DB idx_logs_thread_id: {log_db}")
    integrity_after = parse_integrity(
        run_sql(
            "DROP INDEX idx_logs_thread_id; "
            "CREATE INDEX idx_logs_thread_id ON logs(thread_id); "
            "PRAGMA integrity_check;"
        )
    )
    if integrity_after != ["ok"]:
        raise SystemExit(
            f"Scoped Codex log DB {log_db} remains corrupt after rebuilding "
            f"idx_logs_thread_id: {integrity_after!r}"
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

repair_codex_runtime_state_db() {
  local codex_home="$1"
  local state_db="${codex_home}/state_5.sqlite"
  local repair_output

  if ! repair_output="$("${PYTHON_BIN}" - "${state_db}" <<'PY' 2>&1
import datetime as dt
import os
import sqlite3
import stat
import sys
from pathlib import Path

state_db = Path(sys.argv[1])
codex_home = state_db.parent
sessions_root = codex_home / "sessions"
expected_schema = {
    (
        "index",
        "idx_agent_job_items_status",
        "agent_job_items",
        "CREATE INDEX idx_agent_job_items_status ON agent_job_items(job_id, status, row_index ASC)",
    ),
    (
        "index",
        "idx_agent_jobs_status",
        "agent_jobs",
        "CREATE INDEX idx_agent_jobs_status ON agent_jobs(status, updated_at DESC)",
    ),
    (
        "index",
        "idx_thread_dynamic_tools_thread",
        "thread_dynamic_tools",
        "CREATE INDEX idx_thread_dynamic_tools_thread ON thread_dynamic_tools(thread_id)",
    ),
    (
        "index",
        "idx_thread_spawn_edges_parent_status",
        "thread_spawn_edges",
        "CREATE INDEX idx_thread_spawn_edges_parent_status\n    ON thread_spawn_edges(parent_thread_id, status)",
    ),
    ("index", "idx_threads_archived", "threads", "CREATE INDEX idx_threads_archived ON threads(archived)"),
    (
        "index",
        "idx_threads_archived_cwd_created_at_ms",
        "threads",
        "CREATE INDEX idx_threads_archived_cwd_created_at_ms ON threads(archived, cwd, created_at_ms DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_archived_cwd_recency_at_ms",
        "threads",
        "CREATE INDEX idx_threads_archived_cwd_recency_at_ms\n    ON threads(archived, cwd, recency_at_ms DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_archived_cwd_updated_at_ms",
        "threads",
        "CREATE INDEX idx_threads_archived_cwd_updated_at_ms ON threads(archived, cwd, updated_at_ms DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_created_at",
        "threads",
        "CREATE INDEX idx_threads_created_at ON threads(created_at DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_created_at_ms",
        "threads",
        "CREATE INDEX idx_threads_created_at_ms ON threads(created_at_ms DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_provider",
        "threads",
        "CREATE INDEX idx_threads_provider ON threads(model_provider)",
    ),
    (
        "index",
        "idx_threads_recency_at_ms",
        "threads",
        "CREATE INDEX idx_threads_recency_at_ms\n    ON threads(recency_at_ms DESC, id DESC)",
    ),
    ("index", "idx_threads_source", "threads", "CREATE INDEX idx_threads_source ON threads(source)"),
    (
        "index",
        "idx_threads_updated_at",
        "threads",
        "CREATE INDEX idx_threads_updated_at ON threads(updated_at DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_updated_at_ms",
        "threads",
        "CREATE INDEX idx_threads_updated_at_ms ON threads(updated_at_ms DESC, id DESC)",
    ),
    (
        "index",
        "idx_threads_visible_created_at_ms",
        "threads",
        "CREATE INDEX idx_threads_visible_created_at_ms\n    ON threads(archived, created_at_ms DESC)\n    WHERE preview <> ''",
    ),
    (
        "index",
        "idx_threads_visible_recency_at_ms",
        "threads",
        "CREATE INDEX idx_threads_visible_recency_at_ms\n    ON threads(archived, recency_at_ms DESC, id DESC)\n    WHERE preview <> ''",
    ),
    (
        "index",
        "idx_threads_visible_updated_at_ms",
        "threads",
        "CREATE INDEX idx_threads_visible_updated_at_ms\n    ON threads(archived, updated_at_ms DESC)\n    WHERE preview <> ''",
    ),
    (
        "table",
        "_sqlx_migrations",
        "_sqlx_migrations",
        """CREATE TABLE _sqlx_migrations (
    version BIGINT PRIMARY KEY,
    description TEXT NOT NULL,
    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    success BOOLEAN NOT NULL,
    checksum BLOB NOT NULL,
    execution_time BIGINT NOT NULL
)""",
    ),
    (
        "table",
        "agent_job_items",
        "agent_job_items",
        """CREATE TABLE agent_job_items (
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
)""",
    ),
    (
        "table",
        "agent_jobs",
        "agent_jobs",
        """CREATE TABLE agent_jobs (
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
, max_runtime_seconds INTEGER)""",
    ),
    (
        "table",
        "backfill_state",
        "backfill_state",
        """CREATE TABLE backfill_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    last_watermark TEXT,
    last_success_at INTEGER,
    updated_at INTEGER NOT NULL
)""",
    ),
    (
        "table",
        "external_agent_config_imports",
        "external_agent_config_imports",
        """CREATE TABLE external_agent_config_imports (
    import_id TEXT PRIMARY KEY,
    completed_at_ms INTEGER NOT NULL,
    successes TEXT NOT NULL,
    failures TEXT NOT NULL
)""",
    ),
    (
        "table",
        "remote_control_enrollments",
        "remote_control_enrollments",
        """CREATE TABLE remote_control_enrollments (
    websocket_url TEXT NOT NULL,
    account_id TEXT NOT NULL,
    app_server_client_name TEXT NOT NULL,
    server_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    server_name TEXT NOT NULL,
    updated_at INTEGER NOT NULL, remote_control_enabled INTEGER,
    PRIMARY KEY (websocket_url, account_id, app_server_client_name)
)""",
    ),
    (
        "table",
        "thread_dynamic_tools",
        "thread_dynamic_tools",
        """CREATE TABLE thread_dynamic_tools (
    thread_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT 0, namespace TEXT,
    PRIMARY KEY(thread_id, position),
    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
)""",
    ),
    (
        "table",
        "thread_spawn_edges",
        "thread_spawn_edges",
        "CREATE TABLE thread_spawn_edges (\n    parent_thread_id TEXT NOT NULL,\n    child_thread_id TEXT NOT NULL PRIMARY KEY,\n    status TEXT NOT NULL\n)",
    ),
    (
        "table",
        "threads",
        "threads",
        """CREATE TABLE threads (
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
, cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '', agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled', model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT '', recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0, history_mode TEXT NOT NULL DEFAULT 'legacy')""",
    ),
    (
        "trigger",
        "threads_created_at_ms_after_insert",
        "threads",
        """CREATE TRIGGER threads_created_at_ms_after_insert
AFTER INSERT ON threads
WHEN NEW.created_at_ms IS NULL
BEGIN
    UPDATE threads
    SET created_at_ms = NEW.created_at * 1000
    WHERE id = NEW.id;
END""",
    ),
    (
        "trigger",
        "threads_created_at_ms_after_update",
        "threads",
        """CREATE TRIGGER threads_created_at_ms_after_update
AFTER UPDATE OF created_at ON threads
WHEN NEW.created_at != OLD.created_at
 AND NEW.created_at_ms IS OLD.created_at_ms
BEGIN
    UPDATE threads
    SET created_at_ms = NEW.created_at * 1000
    WHERE id = NEW.id;
END""",
    ),
    (
        "trigger",
        "threads_recency_at_after_insert",
        "threads",
        """CREATE TRIGGER threads_recency_at_after_insert
AFTER INSERT ON threads
WHEN NEW.recency_at_ms = 0
BEGIN
    UPDATE threads
    SET recency_at = NEW.updated_at,
        recency_at_ms = COALESCE(NEW.updated_at_ms, NEW.updated_at * 1000)
    WHERE id = NEW.id;
END""",
    ),
    (
        "trigger",
        "threads_updated_at_ms_after_insert",
        "threads",
        """CREATE TRIGGER threads_updated_at_ms_after_insert
AFTER INSERT ON threads
WHEN NEW.updated_at_ms IS NULL
BEGIN
    UPDATE threads
    SET updated_at_ms = NEW.updated_at * 1000
    WHERE id = NEW.id;
END""",
    ),
    (
        "trigger",
        "threads_updated_at_ms_after_update",
        "threads",
        """CREATE TRIGGER threads_updated_at_ms_after_update
AFTER UPDATE OF updated_at ON threads
WHEN NEW.updated_at != OLD.updated_at
 AND NEW.updated_at_ms IS OLD.updated_at_ms
BEGIN
    UPDATE threads
    SET updated_at_ms = NEW.updated_at * 1000
    WHERE id = NEW.id;
END""",
    ),
}


def file_identity() -> tuple[int, int] | None:
    try:
        st = os.lstat(state_db)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit(f"Scoped Codex state DB must not be a symlink: {state_db}")
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(f"Scoped Codex state DB must be a regular file: {state_db}")
    if st.st_nlink != 1:
        raise SystemExit(
            f"Scoped Codex state DB must not have hard links; st_nlink={st.st_nlink}: {state_db}"
        )
    if st.st_uid != os.geteuid():
        raise SystemExit(
            f"Scoped Codex state DB owner uid {st.st_uid} does not match current uid {os.geteuid()}: {state_db}"
        )
    return (st.st_dev, st.st_ino)


def parse_integrity(rows: list[tuple[object, ...]]) -> list[str]:
    lines = [str(row[0]).strip() for row in rows if row and str(row[0]).strip()]
    if not lines:
        raise SystemExit(f"empty PRAGMA integrity_check output for {state_db}")
    return lines


def validate_integrity(connection: sqlite3.Connection, context: str) -> None:
    integrity = parse_integrity(connection.execute("PRAGMA integrity_check;").fetchall())
    if integrity != ["ok"]:
        raise SystemExit(
            f"Scoped Codex state DB {state_db} has non-repairable integrity errors {context}: {integrity!r}"
        )


def validate_foreign_keys(connection: sqlite3.Connection, context: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON;")
    status = connection.execute("PRAGMA foreign_keys;").fetchone()
    if status != (1,):
        raise SystemExit(
            f"Scoped Codex state DB could not enable foreign_keys {context}: {status!r}"
        )
    rows = connection.execute("PRAGMA foreign_key_check;").fetchall()
    violations: list[tuple[str, int | None, str, int]] = []
    for item in rows:
        if (
            len(item) != 4
            or not isinstance(item[0], str)
            or not (isinstance(item[1], int) or item[1] is None)
            or not isinstance(item[2], str)
            or not isinstance(item[3], int)
        ):
            raise SystemExit(
                f"unexpected PRAGMA foreign_key_check row for scoped Codex state DB "
                f"{context}: {item!r}"
            )
        violations.append(item)
    if violations:
        raise SystemExit(
            f"Scoped Codex state DB {state_db} has foreign-key violations "
            f"{context}: {violations!r}"
        )


def schema_rows(connection: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    raw = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND type IN ('table', 'index', 'trigger') "
        "ORDER BY type, name;"
    ).fetchall()
    rows: set[tuple[str, str, str, str]] = set()
    for item in raw:
        if len(item) != 4 or not all(isinstance(value, str) for value in item):
            raise SystemExit(f"unexpected state_5.sqlite schema row: {item!r}")
        rows.add(item)
    return rows


def validate_schema(connection: sqlite3.Connection) -> None:
    actual = schema_rows(connection)
    if actual != expected_schema:
        missing = sorted(expected_schema - actual)
        extra = sorted(actual - expected_schema)
        raise SystemExit(
            f"Scoped Codex state DB schema does not match the pinned state_5.sqlite schema at {state_db}; "
            f"missing={missing!r}; extra={extra!r}"
        )


def load_threads(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute("SELECT id, rollout_path FROM threads ORDER BY id;").fetchall()
    threads: list[tuple[str, str]] = []
    for item in rows:
        if (
            len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or not item[0]
            or not item[1]
        ):
            raise SystemExit(f"unexpected state_5.sqlite thread row: {item!r}")
        if "\x00" in item[1]:
            raise SystemExit(f"Scoped Codex state DB has NUL in rollout_path for thread {item[0]!r}")
        rollout_path = Path(item[1])
        if not rollout_path.is_absolute():
            raise SystemExit(
                f"Scoped Codex state DB rollout_path is not absolute for thread {item[0]!r}: {item[1]}"
            )
        threads.append((item[0], item[1]))
    return threads


def require_normal_directory(path: Path, context: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SystemExit(
            f"Scoped Codex state DB rollout_path parent topology is not a normal sessions tree; "
            f"missing {context}: {path}"
        ) from None
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit(
            f"Scoped Codex state DB rollout_path parent topology is not a normal sessions tree; "
            f"symlinked {context}: {path}"
        )
    if not stat.S_ISDIR(st.st_mode):
        raise SystemExit(
            f"Scoped Codex state DB rollout_path parent topology is not a normal sessions tree; "
            f"non-directory {context}: {path}"
        )


def validate_rollout_scope(thread_id: str, rollout_path: Path) -> None:
    try:
        relative = rollout_path.relative_to(sessions_root)
    except ValueError:
        raise SystemExit(
            f"Scoped Codex state DB rollout_path is outside expected Codex rollout tree "
            f"{sessions_root}/YYYY/MM/DD/rollout-*.jsonl for thread {thread_id!r}: {rollout_path}"
        ) from None

    parts = relative.parts
    if len(parts) != 4:
        raise SystemExit(
            f"Scoped Codex state DB rollout_path is not in expected Codex rollout tree "
            f"{sessions_root}/YYYY/MM/DD/rollout-*.jsonl for thread {thread_id!r}: {rollout_path}"
        )
    year, month, day, filename = parts
    if (
        len(year) != 4
        or len(month) != 2
        or len(day) != 2
        or not year.isdecimal()
        or not month.isdecimal()
        or not day.isdecimal()
    ):
        raise SystemExit(
            f"Scoped Codex state DB rollout_path has invalid YYYY/MM/DD path for "
            f"thread {thread_id!r}: {rollout_path}"
        )
    try:
        dt.date(int(year), int(month), int(day))
    except ValueError as exc:
        raise SystemExit(
            f"Scoped Codex state DB rollout_path has invalid calendar date for "
            f"thread {thread_id!r}: {rollout_path}"
        ) from exc
    if not filename.startswith("rollout-") or not filename.endswith(".jsonl") or filename == "rollout-.jsonl":
        raise SystemExit(
            f"Scoped Codex state DB rollout_path filename is not rollout-*.jsonl for "
            f"thread {thread_id!r}: {rollout_path}"
        )

    require_normal_directory(codex_home, "Codex home")
    require_normal_directory(sessions_root, "sessions root")
    require_normal_directory(sessions_root / year, "year directory")
    require_normal_directory(sessions_root / year / month, "month directory")
    require_normal_directory(sessions_root / year / month / day, "day directory")


def rollout_path_is_missing(thread_id: str, path: str) -> bool:
    rollout_path = Path(path)
    validate_rollout_scope(thread_id, rollout_path)
    try:
        st = os.lstat(rollout_path)
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit(
            f"Scoped Codex state DB existing rollout_path must not be a symlink for "
            f"thread {thread_id!r}: {rollout_path}"
        )
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(
            f"Scoped Codex state DB existing rollout_path must be a regular file for "
            f"thread {thread_id!r}: {rollout_path}"
        )
    return False


def missing_rollout_threads(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (thread_id, path)
        for thread_id, path in load_threads(connection)
        if rollout_path_is_missing(thread_id, path)
    ]


def fail_if_global_non_cascading_references_are_unsafe(
    connection: sqlite3.Connection,
    context: str,
) -> None:
    spawn_rows = connection.execute(
        """
        SELECT
            edges.parent_thread_id,
            edges.child_thread_id,
            parent_threads.id IS NULL AS parent_missing,
            child_threads.id IS NULL AS child_missing
        FROM thread_spawn_edges AS edges
        LEFT JOIN threads AS parent_threads
            ON parent_threads.id = edges.parent_thread_id
        LEFT JOIN threads AS child_threads
            ON child_threads.id = edges.child_thread_id
        WHERE parent_threads.id IS NULL
           OR child_threads.id IS NULL
        ORDER BY edges.parent_thread_id, edges.child_thread_id;
        """
    ).fetchall()
    if spawn_rows:
        raise SystemExit(
            "Scoped Codex state DB has orphaned non-cascading thread_spawn_edges "
            f"{context}; refusing validation/repair: {spawn_rows!r}"
        )

    job_item_rows = connection.execute(
        """
        SELECT items.job_id, items.item_id, items.assigned_thread_id
        FROM agent_job_items AS items
        LEFT JOIN threads
            ON threads.id = items.assigned_thread_id
        WHERE items.assigned_thread_id IS NOT NULL
          AND threads.id IS NULL
        ORDER BY items.job_id, items.item_id;
        """
    ).fetchall()
    if job_item_rows:
        raise SystemExit(
            "Scoped Codex state DB has orphaned non-cascading agent_job_items.assigned_thread_id "
            f"{context}; refusing validation/repair: {job_item_rows!r}"
        )


def collect_stale_thread_spawn_edges_to_delete(
    connection: sqlite3.Connection,
    stale_threads: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    thread_ids = [thread_id for thread_id, _ in stale_threads]
    stale_thread_ids = set(thread_ids)
    placeholders = ",".join("?" for _ in thread_ids)
    spawn_rows = connection.execute(
        f"""
        SELECT parent_thread_id, child_thread_id
        FROM thread_spawn_edges
        WHERE parent_thread_id IN ({placeholders})
           OR child_thread_id IN ({placeholders})
        ORDER BY parent_thread_id, child_thread_id;
        """,
        (*thread_ids, *thread_ids),
    ).fetchall()
    mixed_rows: list[tuple[str, str]] = []
    deletable_rows: list[tuple[str, str]] = []
    for item in spawn_rows:
        if (
            len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or not item[0]
            or not item[1]
        ):
            raise SystemExit(f"unexpected state_5.sqlite thread_spawn_edges row: {item!r}")
        parent_thread_id, child_thread_id = item
        parent_is_stale = parent_thread_id in stale_thread_ids
        child_is_stale = child_thread_id in stale_thread_ids
        if parent_is_stale and child_is_stale:
            deletable_rows.append((parent_thread_id, child_thread_id))
        else:
            mixed_rows.append((parent_thread_id, child_thread_id))
    if mixed_rows:
        raise SystemExit(
            "Scoped Codex state DB stale thread rows are referenced by mixed stale/non-stale "
            f"thread_spawn_edges state; refusing delete: {mixed_rows!r}"
        )
    return deletable_rows


def fail_if_stale_threads_have_agent_job_items(
    connection: sqlite3.Connection,
    stale_threads: list[tuple[str, str]],
) -> None:
    thread_ids = [thread_id for thread_id, _ in stale_threads]
    placeholders = ",".join("?" for _ in thread_ids)
    job_item_rows = connection.execute(
        f"""
        SELECT job_id, item_id, assigned_thread_id
        FROM agent_job_items
        WHERE assigned_thread_id IN ({placeholders})
        ORDER BY job_id, item_id;
        """,
        thread_ids,
    ).fetchall()
    if job_item_rows:
        raise SystemExit(
            "Scoped Codex state DB stale thread rows are referenced by non-cascading "
            f"agent_job_items state; refusing delete: {job_item_rows!r}"
        )


identity_before = file_identity()
if identity_before is None:
    raise SystemExit(0)

connection = sqlite3.connect(state_db)
try:
    validate_schema(connection)
    validate_integrity(connection, "before stale rollout_path repair")
    validate_foreign_keys(connection, "before stale rollout_path repair")
    fail_if_global_non_cascading_references_are_unsafe(
        connection,
        "before stale rollout_path repair",
    )
    stale_threads = missing_rollout_threads(connection)
    if not stale_threads:
        raise SystemExit(0)

    for thread_id, rollout_path in stale_threads:
        if not rollout_path_is_missing(thread_id, rollout_path):
            raise SystemExit(f"refusing to delete non-stale rollout_path that exists: {rollout_path}")

    print(f"Repairing scoped Codex state DB stale thread rows: {state_db} ({len(stale_threads)} rows)")
    try:
        connection.execute("BEGIN IMMEDIATE;")
        fail_if_global_non_cascading_references_are_unsafe(
            connection,
            "inside stale rollout_path repair transaction before delete",
        )
        stale_spawn_edges = collect_stale_thread_spawn_edges_to_delete(connection, stale_threads)
        fail_if_stale_threads_have_agent_job_items(connection, stale_threads)

        edge_deleted = 0
        for parent_thread_id, child_thread_id in stale_spawn_edges:
            cursor = connection.execute(
                """
                DELETE FROM thread_spawn_edges
                WHERE parent_thread_id = ?
                  AND child_thread_id = ?;
                """,
                (parent_thread_id, child_thread_id),
            )
            edge_deleted += cursor.rowcount
        if edge_deleted != len(stale_spawn_edges):
            raise RuntimeError(
                f"deleted {edge_deleted} stale-to-stale thread_spawn_edges rows, "
                f"expected {len(stale_spawn_edges)}"
            )

        deleted = 0
        for thread_id, rollout_path in stale_threads:
            if not rollout_path_is_missing(thread_id, rollout_path):
                raise RuntimeError(f"rollout_path appeared before delete for thread {thread_id}: {rollout_path}")
            cursor = connection.execute(
                "DELETE FROM threads WHERE id = ? AND rollout_path = ?;",
                (thread_id, rollout_path),
            )
            deleted += cursor.rowcount
        if deleted != len(stale_threads):
            raise RuntimeError(
                f"deleted {deleted} stale thread rows, expected {len(stale_threads)}"
            )
        connection.commit()
    except BaseException as exc:
        try:
            connection.rollback()
        except Exception as rollback_exc:
            exc.add_note(
                f"rollback failed after scoped Codex state DB transaction error: {rollback_exc!r}"
            )
        raise

    validate_integrity(connection, "after stale rollout_path repair")
    validate_foreign_keys(connection, "after stale rollout_path repair")
    validate_schema(connection)
    remaining_stale = missing_rollout_threads(connection)
    if remaining_stale:
        raise SystemExit(
            f"Scoped Codex state DB still has stale rollout_path rows after repair: {remaining_stale!r}"
        )
    fail_if_global_non_cascading_references_are_unsafe(
        connection,
        "after stale rollout_path repair",
    )
    if file_identity() != identity_before:
        raise SystemExit(f"Scoped Codex state DB identity changed during repair: {state_db}")
    if edge_deleted:
        print(
            f"Repaired scoped Codex state DB stale-to-stale thread_spawn_edges: "
            f"{state_db} ({edge_deleted} rows)"
        )
    print(f"Repaired scoped Codex state DB stale thread rows: {state_db} ({deleted} rows)")
finally:
    connection.close()
PY
)"; then
    echo "ERROR: Scoped Codex state DB validation/repair failed for ${state_db}." >&2
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
import re
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

def is_abs_path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/")

def is_semver(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", value) is not None

def is_known_curl_timeout(value: object) -> bool:
    return value == "curl: (28) Resolving timed out after 5000 milliseconds"

def detail_string(details: dict[str, object], key: str) -> str | None:
    value = details.get(key)
    return value if isinstance(value, str) else None

def path_codex_entries_are_structural(details: dict[str, object]) -> bool:
    raw_count = detail_string(details, "PATH codex entries")
    if raw_count is None or not raw_count.isdecimal():
        return False
    count = int(raw_count)
    path_keys: list[tuple[int, str]] = []
    for key, value in details.items():
        match = re.fullmatch(r"PATH codex #([0-9]+)", key)
        if match is None:
            continue
        if not is_abs_path(value) or Path(value).name != "codex":
            return False
        path_keys.append((int(match.group(1)), key))
    if len(path_keys) != count:
        return False
    return sorted(number for number, _ in path_keys) == list(range(1, count + 1))

def install_context_native_root(details: dict[str, object]) -> str | None:
    install_context = detail_string(details, "install context")
    if install_context is None:
        return None
    match = re.fullmatch(
        r"npm \(package (/.+), bin \1/bin, resources \1/codex-resources, path \1/codex-path\)",
        install_context,
    )
    if match is None:
        return None
    native_root = match.group(1)
    if "/@openai/codex-" not in native_root or "/vendor/" not in native_root:
        return None
    return native_root

def details_have_exact_keys(details: dict[str, object], expected: set[str]) -> bool:
    return set(details) == expected

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
    if check.get("id") != check_id:
        return False
    if check.get("category") != "install":
        return False
    if check.get("status") != "fail":
        return False
    if check.get("summary") != "npm install -g @openai/codex would update a different install":
        return False
    details = check.get("details")
    if not isinstance(details, dict) or not path_codex_entries_are_structural(details):
        return False
    path_keys = {key for key in details if re.fullmatch(r"PATH codex #[0-9]+", key)}
    expected_keys = {
        "PATH codex entries",
        "current executable",
        "install context",
        "managed by bun",
        "managed by npm",
        "managed by pnpm",
        "managed package root",
        "npm package root",
        "running package root",
        *path_keys,
    }
    native_root = install_context_native_root(details)
    return (
        details_have_exact_keys(details, expected_keys)
        and native_root is not None
        and details.get("current executable") == f"{native_root}/bin/codex"
        and details.get("managed by bun") == "false"
        and details.get("managed by npm") == "true"
        and details.get("managed by pnpm") == "false"
        and details.get("managed package root") == str(app_server_package_root)
        and details.get("running package root") == str(app_server_package_root)
        and is_abs_path(details.get("npm package root"))
        and details.get("npm package root") != str(app_server_package_root)
    )

def is_expected_update_probe_failure(check_id: str, check: dict[str, object]) -> bool:
    if check.get("id") != check_id:
        return False
    details = check.get("details")
    if not isinstance(details, dict):
        return False
    mismatch = (
        check.get("category") == "updates"
        and check.get("status") == "fail"
        and check.get("summary") == "update would target a different npm install"
        and details_have_exact_keys(
            details,
            {
                "check for update on startup",
                "latest version",
                "latest version status",
                "npm package root",
                "running package root",
                "update action",
                "version cache",
            },
        )
        and details.get("check for update on startup") == "true"
        and is_semver(details.get("latest version"))
        and details.get("latest version status") == "newer version is available"
        and is_abs_path(details.get("npm package root"))
        and details.get("npm package root") != str(app_server_package_root)
        and details.get("running package root") == str(app_server_package_root)
        and details.get("update action") == "npm install -g @openai/codex"
        and details.get("version cache") == [str(codex_home / "version.json"), "missing"]
    )
    probe_timeout = (
        check.get("category") == "updates"
        and check.get("status") == "fail"
        and check.get("summary") == "update would target a different npm install"
        and details_have_exact_keys(
            details,
            {
                "check for update on startup",
                "latest version probe",
                "npm package root",
                "running package root",
                "update action",
                "version cache",
            },
        )
        and details.get("check for update on startup") == "true"
        and is_known_curl_timeout(details.get("latest version probe"))
        and is_abs_path(details.get("npm package root"))
        and details.get("npm package root") != str(app_server_package_root)
        and details.get("running package root") == str(app_server_package_root)
        and details.get("update action") == "npm install -g @openai/codex"
        and details.get("version cache") == [str(codex_home / "version.json"), "missing"]
    )
    timeout = has_shape(
        check_id,
        check,
        category="updates",
        status="warning",
        summary="update check timed out",
        details={"running package root": str(app_server_package_root)},
    )
    return mismatch or probe_timeout or timeout

def is_expected_missing_auth_websocket_warning(check_id: str, check: dict[str, object]) -> bool:
    if check.get("id") != check_id:
        return False
    if check.get("category") != "websocket":
        return False
    if check.get("status") != "warning":
        return False
    if check.get("summary") != "Responses WebSocket failed; HTTPS fallback may still work":
        return False
    details = check.get("details")
    if not isinstance(details, dict):
        return False
    return (
        details_have_exact_keys(
            details,
            {
                "DNS",
                "auth mode",
                "connect timeout",
                "endpoint",
                "handshake transport error",
                "model provider",
                "provider name",
                "proxy env vars",
                "supports websockets",
                "wire API",
            },
        )
        and isinstance(details.get("DNS"), str)
        and re.fullmatch(r"[0-9]+ IPv4, [0-9]+ IPv6, first (IPv4|IPv6|none)", details["DNS"]) is not None
        and details.get("auth mode") == "none"
        and isinstance(details.get("connect timeout"), str)
        and re.fullmatch(r"[0-9]+ ms", details["connect timeout"]) is not None
        and details.get("endpoint") == "wss://api.openai.com/v1/<redacted>"
        and isinstance(details.get("handshake transport error"), str)
        and details["handshake transport error"].startswith("http 401 Unauthorized:")
        and details.get("model provider") == "openai"
        and details.get("provider name") == "OpenAI"
        and details.get("proxy env vars") == "none"
        and details.get("supports websockets") == "true"
        and details.get("wire API") == "responses"
        and not (codex_home / "auth.json").exists()
    )

for check_id, raw_check in checks.items():
    if not isinstance(raw_check, dict):
        unexpected.append(f"{check_id}=invalid")
        continue
    status = raw_check.get("status")
    if check_id == "auth.credentials":
        if status == "fail" and is_expected_openclaw_managed_auth_failure(check_id, raw_check):
            ignored.append(check_id)
        else:
            unexpected.append(f"{check_id}={status}")
        continue
    if status == "ok":
        continue
    if status not in {"fail", "warning"}:
        unexpected.append(f"{check_id}={status}")
        continue
    if check_id == "installation" and is_expected_embedded_installation_failure(check_id, raw_check):
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
    if [[ -f "${stale_path}" || -L "${stale_path}" ]]; then
      guarded_rm "${stale_path}" "removing stale native Codex stage agent ${stale_path}"
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
  snapshot_managed_artifact_path "${BOOTSTRAP_DST}"
  BOOTSTRAP_TARGET_DIRS["${BOOTSTRAP_DST}"]=1
  guarded_mkdir_p "${BOOTSTRAP_DST}" "creating managed OpenClaw workspace ${BOOTSTRAP_DST}"
  for FILE in "${BOOTSTRAP_FILES[@]}"; do
    guarded_cp_file "${REPO_ROOT}/gateway/agent_config/${FILE}" "${BOOTSTRAP_DST}/${FILE}" "copying managed bootstrap file ${BOOTSTRAP_DST}/${FILE}"
  done
  if workspace_has_autoresearch_agent "${AGENTS}"; then
    CODEX_AGENTS_DST="${BOOTSTRAP_DST}/.codex/agents"
    guarded_mkdir_p "${CODEX_AGENTS_DST}" "creating managed workspace Codex agents directory ${CODEX_AGENTS_DST}"
    remove_legacy_codex_stage_agents "${CODEX_AGENTS_DST}"
    for AGENT_ID in "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}"; do
      guarded_cp_file "${CODEX_AGENTS_SRC}/${AGENT_ID}.toml" "${CODEX_AGENTS_DST}/${AGENT_ID}.toml" "copying managed workspace Codex agent ${CODEX_AGENTS_DST}/${AGENT_ID}.toml"
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
declare -A MANAGED_AGENT_DIR_SNAPSHOT_IDS=()
for CODEX_RUNTIME_AGENT_ID in "${CODEX_NATIVE_RUNTIME_AGENT_IDS[@]}"; do
  MANAGED_AGENT_DIR_SNAPSHOT_IDS["${CODEX_RUNTIME_AGENT_ID}"]=1
done
mapfile -t OPENAI_AGENT_IDS_FOR_SNAPSHOT < <(jq -r '
  .agents.list[]?
  | select((.model.primary // "") | startswith("openai/"))
  | .id
' "${REPO_CONFIG}")
for OPENAI_AGENT_ID_FOR_SNAPSHOT in "${OPENAI_AGENT_IDS_FOR_SNAPSHOT[@]}"; do
  MANAGED_AGENT_DIR_SNAPSHOT_IDS["${OPENAI_AGENT_ID_FOR_SNAPSHOT}"]=1
done
for MANAGED_AGENT_ID_FOR_SNAPSHOT in "${!MANAGED_AGENT_DIR_SNAPSHOT_IDS[@]}"; do
  snapshot_managed_artifact_path "${OPENCLAW_PUSH_HOME}/agents/${MANAGED_AGENT_ID_FOR_SNAPSHOT}/agent"
done
echo "Copying native Codex stage agents to ${#CODEX_NATIVE_RUNTIME_AGENT_IDS[@]} scoped Codex homes:"
for CODEX_RUNTIME_AGENT_ID in "${CODEX_NATIVE_RUNTIME_AGENT_IDS[@]}"; do
  CODEX_RUNTIME_HOME="${OPENCLAW_PUSH_HOME}/agents/${CODEX_RUNTIME_AGENT_ID}/agent/codex-home"
  CODEX_RUNTIME_AGENTS_DST="${CODEX_RUNTIME_HOME}/agents"
  guarded_mkdir_p "${CODEX_RUNTIME_AGENTS_DST}" "creating managed Codex runtime agents directory ${CODEX_RUNTIME_AGENTS_DST}"
  write_codex_runtime_config "${CODEX_RUNTIME_HOME}" "${CODEX_RUNTIME_AGENT_ID}"
  remove_legacy_codex_stage_agents "${CODEX_RUNTIME_AGENTS_DST}"
  if [[ "${CODEX_RUNTIME_AGENT_ID}" == "main" ]]; then
    STALE_CODEX_AGENT_SCAN_OUTPUT="$(mktemp "${TMPDIR:-/tmp}/push-openclaw-main-codex-agents.XXXXXX")"
    guard_destination_path_chain "${STALE_CODEX_AGENT_SCAN_OUTPUT}" "creating stale main Codex runtime agent scan output ${STALE_CODEX_AGENT_SCAN_OUTPUT}" || exit 1
    if ! collect_find_results_null \
      "${STALE_CODEX_AGENT_SCAN_OUTPUT}" \
      "${CODEX_RUNTIME_AGENTS_DST}" \
      "scanning stale main Codex runtime agents in ${CODEX_RUNTIME_AGENTS_DST}" \
      -mindepth 1 -maxdepth 1 -type f -name '*.toml'; then
      exit 1
    fi
    while IFS= read -r -d '' STALE_CODEX_AGENT_FILE; do
      guarded_rm "${STALE_CODEX_AGENT_FILE}" "removing stale main Codex runtime agent ${STALE_CODEX_AGENT_FILE}"
    done < "${STALE_CODEX_AGENT_SCAN_OUTPUT}"
    guarded_rm_f "${STALE_CODEX_AGENT_SCAN_OUTPUT}" "removing stale main Codex runtime agent scan output ${STALE_CODEX_AGENT_SCAN_OUTPUT}" || exit 1
  else
    for AGENT_ID in "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}"; do
      guarded_cp_file "${CODEX_AGENTS_SRC}/${AGENT_ID}.toml" "${CODEX_RUNTIME_AGENTS_DST}/${AGENT_ID}.toml" "copying managed Codex runtime agent ${CODEX_RUNTIME_AGENTS_DST}/${AGENT_ID}.toml"
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
      if [[ "${STALE_DIR}" == "${OPENCLAW_PUSH_HOME}" ]]; then
        snapshot_managed_artifact_path "${STALE}"
      else
        snapshot_managed_artifact_path "${STALE_DIR}"
      fi
      guarded_rm "${STALE}" "removing stale managed bootstrap file ${STALE}"
      echo "Removed stale ${STALE}"
    fi
  done
done

# ── Copy repo skills ─────────────────────────────────────────────────────────
SKILLS_DST="${OPENCLAW_PUSH_HOME}/skills"
if [[ -d "${SKILLS_SRC}" ]]; then
  snapshot_managed_artifact_path "${SKILLS_DST}"
  # Copy repo skills to local
  for SKILL_DIR in "${SKILLS_SRC}"/*/; do
    SKILL_NAME="$(basename "${SKILL_DIR}")"
    if [[ ! -f "${SKILL_DIR}SKILL.md" ]]; then
      continue
    fi
    snapshot_managed_artifact_path "${SKILLS_DST}/${SKILL_NAME}"
    guarded_mkdir_p "${SKILLS_DST}/${SKILL_NAME}" "creating managed skill directory ${SKILLS_DST}/${SKILL_NAME}"
    guarded_cp_file "${SKILL_DIR}"SKILL.md "${SKILLS_DST}/${SKILL_NAME}/SKILL.md" "copying managed skill file ${SKILLS_DST}/${SKILL_NAME}/SKILL.md"
    echo "Copied skill ${SKILL_NAME} → ${SKILLS_DST}/${SKILL_NAME}/SKILL.md"
  done
fi
STALE_MEMPALACE_WRITE_SKILL_DST="${SKILLS_DST}/mempalace"
if [[ -d "${STALE_MEMPALACE_WRITE_SKILL_DST}" ]]; then
  snapshot_managed_artifact_path "${STALE_MEMPALACE_WRITE_SKILL_DST}"
  guarded_rm_rf "${STALE_MEMPALACE_WRITE_SKILL_DST}" "removing stale write-capable MemPalace skill ${STALE_MEMPALACE_WRITE_SKILL_DST}"
  echo "Removed stale write-capable MemPalace skill ${STALE_MEMPALACE_WRITE_SKILL_DST}"
fi

# ── Manage Azure API-version preload artifact ────────────────────────────────
PRELOAD_SRC="${REPO_ROOT}/gateway/openclaw_config/azure-api-version-preload.cjs"
PRELOAD_DST="${OPENCLAW_PUSH_HOME}/azure-api-version-preload.cjs"
if [[ "${OPENCLAW_PROVIDER:-codex}" == "azure" && -f "${PRELOAD_SRC}" ]]; then
  snapshot_managed_artifact_path "${PRELOAD_DST}"
  guarded_cp_file "${PRELOAD_SRC}" "${PRELOAD_DST}" "copying managed Azure preload artifact ${PRELOAD_DST}"
  echo "Copied azure-api-version-preload.cjs → ${PRELOAD_DST}"
elif [[ "${OPENCLAW_PROVIDER:-codex}" != "azure" && -f "${PRELOAD_DST}" ]]; then
  snapshot_managed_artifact_path "${PRELOAD_DST}"
  guarded_rm "${PRELOAD_DST}" "removing managed Azure preload artifact ${PRELOAD_DST}"
  echo "Removed Azure preload artifact from Codex/OpenRouter route: ${PRELOAD_DST}"
fi

if [[ "${PROVIDER}" == "codex" ]]; then
  snapshot_managed_artifact_path "${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}"
  snapshot_managed_artifact_path "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"
  remove_stale_azure_node_options_for_codex
  sync_managed_agent_codex_auth
fi

# ── Validate ─────────────────────────────────────────────────────────────────
echo ""
echo "── Validating config ──"
echo "Running: ${OPENCLAW_BIN_RESOLVED} config validate --json"
PUBLISHED_OPENCLAW_CONFIG_IDENTITY="$(guarded_regular_file_identity "${LOCAL_CONFIG}" "capturing live OpenClaw config identity before final validation ${LOCAL_CONFIG}")" || run_deployment_rollback_and_exit 1
PUBLISHED_OPENCLAW_CONFIG_HASH="$(file_sha256 "${LOCAL_CONFIG}")"
PUBLISHED_OPENCLAW_CONFIG_BYTES="$(file_bytes "${LOCAL_CONFIG}")"
if live_validate_json="$(run_openclaw_cli config validate --json 2>&1)"; then
  live_validate_status=0
else
  live_validate_status=$?
fi
if [[ "${live_validate_status}" -ne 0 ]] || ! printf '%s\n' "${live_validate_json}" | openclaw_schema_validation_is_clean; then
  echo "ERROR: '${OPENCLAW_BIN_RESOLVED} config validate --json' failed. Rolling back managed deployment from backup ${BACKUP}." >&2
  printf '%s\n' "${live_validate_json}" >&2
  run_deployment_rollback_and_exit 1
fi
if ! verify_guarded_regular_file_identity_unchanged "${LOCAL_CONFIG}" "${PUBLISHED_OPENCLAW_CONFIG_IDENTITY}" "final live config validation"; then
  echo "ERROR: External OpenClaw CLI changed live config identity/topology during final validation: ${LOCAL_CONFIG}. Rolling back managed deployment from backup ${BACKUP}." >&2
  run_deployment_rollback_and_exit 1
fi
current_live_config_hash="$(file_sha256 "${LOCAL_CONFIG}")"
current_live_config_bytes="$(file_bytes "${LOCAL_CONFIG}")"
if [[ "${current_live_config_hash}" != "${PUBLISHED_OPENCLAW_CONFIG_HASH}" \
  || "${current_live_config_bytes}" != "${PUBLISHED_OPENCLAW_CONFIG_BYTES}" ]]; then
  echo "ERROR: External OpenClaw CLI modified live config during final validation. Rolling back managed deployment from backup ${BACKUP}." >&2
  echo "       expected ${PUBLISHED_OPENCLAW_CONFIG_BYTES} bytes sha256 ${PUBLISHED_OPENCLAW_CONFIG_HASH}; got ${current_live_config_bytes} bytes sha256 ${current_live_config_hash}." >&2
  run_deployment_rollback_and_exit 1
fi

# Install the supervisor definition without starting autonomous work. The
# human-facing control command owns enable/start and stop transitions.
guarded_mkdir_p "${SYSTEMD_USER_DIR}" "creating managed systemd user directory ${SYSTEMD_USER_DIR}"
SUPERVISOR_UNIT_TMP="$(mktemp "${SYSTEMD_USER_DIR}/.${SUPERVISOR_SERVICE_NAME}.XXXXXX")"
guard_destination_path_chain "${SUPERVISOR_UNIT_TMP}" "writing generated supervisor unit ${SUPERVISOR_UNIT_TMP}"
sed \
  -e "s|@REPO_ROOT@|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|@HOME@|$(escape_sed_replacement "${HOME}")|g" \
  -e "s|@PATH@|$(escape_sed_replacement "${PATH}")|g" \
  -e "s|@PYTHON_BIN@|$(escape_sed_replacement "${PYTHON_BIN}")|g" \
  "${SUPERVISOR_UNIT_TEMPLATE}" > "${SUPERVISOR_UNIT_TMP}"
guard_destination_path_chain "${SUPERVISOR_UNIT_TMP}" "wrote generated supervisor unit ${SUPERVISOR_UNIT_TMP}"
if grep -q '@[A-Z_][A-Z_]*@' "${SUPERVISOR_UNIT_TMP}"; then
  echo "ERROR: Unresolved placeholder in generated ${SUPERVISOR_SERVICE_NAME}." >&2
  exit 1
fi
if ! validate_supervisor_unit_file "${SUPERVISOR_UNIT_TMP}"; then
  exit 1
fi
guarded_chmod 0644 "${SUPERVISOR_UNIT_TMP}" "chmod generated supervisor unit ${SUPERVISOR_UNIT_TMP}"
begin_managed_unit_transaction
guarded_mv_replace "${SUPERVISOR_UNIT_TMP}" "${SUPERVISOR_UNIT_DST}" "publishing managed supervisor unit ${SUPERVISOR_UNIT_DST}"
SUPERVISOR_UNIT_TMP=""
echo "Installed ${SUPERVISOR_SERVICE_NAME} (not started)."

# Install persistent numerical-runtime caps on the OpenClaw Gateway service so
# all OpenClaw-launched Quantipy child processes inherit bounded BLAS/joblib
# thread counts. This is shared operator infrastructure, not agent-owned state.
prepare_runtime_caps_dropin_dir
GATEWAY_RUNTIME_CAPS_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${GATEWAY_RUNTIME_CAPS_DROPIN_NAME}.XXXXXX")"
guarded_cp_file "${GATEWAY_RUNTIME_CAPS_DROPIN_SRC}" "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" "staging managed runtime caps drop-in ${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"
guarded_chmod 0644 "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" "chmod staged managed runtime caps drop-in ${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"
validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}"
guarded_mv_replace "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP}" "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}" "publishing managed runtime caps drop-in ${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
GATEWAY_RUNTIME_CAPS_DROPIN_TMP=""
validate_runtime_caps_dropin_file "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
CODEX_RUNTIME_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${CODEX_RUNTIME_DROPIN_NAME}.XXXXXX")"
guarded_cp_file "${CODEX_RUNTIME_DROPIN_SRC}" "${CODEX_RUNTIME_DROPIN_TMP}" "staging managed Codex runtime drop-in ${CODEX_RUNTIME_DROPIN_TMP}"
guarded_chmod 0644 "${CODEX_RUNTIME_DROPIN_TMP}" "chmod staged managed Codex runtime drop-in ${CODEX_RUNTIME_DROPIN_TMP}"
validate_codex_runtime_dropin_file "${CODEX_RUNTIME_DROPIN_TMP}"
guarded_mv_replace "${CODEX_RUNTIME_DROPIN_TMP}" "${CODEX_RUNTIME_DROPIN_DST}" "publishing managed Codex runtime drop-in ${CODEX_RUNTIME_DROPIN_DST}"
CODEX_RUNTIME_DROPIN_TMP=""
validate_codex_runtime_dropin_file "${CODEX_RUNTIME_DROPIN_DST}"
NATIVE_CRASH_HARDENING_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${NATIVE_CRASH_HARDENING_DROPIN_NAME}.XXXXXX")"
guarded_cp_file "${NATIVE_CRASH_HARDENING_DROPIN_SRC}" "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "staging managed native-crash hardening drop-in ${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
guarded_chmod 0644 "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "chmod staged managed native-crash hardening drop-in ${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
guarded_mv_replace "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "${NATIVE_CRASH_HARDENING_DROPIN_DST}" "publishing managed native-crash hardening drop-in ${NATIVE_CRASH_HARDENING_DROPIN_DST}"
NATIVE_CRASH_HARDENING_DROPIN_TMP=""
validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
if ! systemctl --user daemon-reload; then
  run_deployment_rollback_and_exit 1
fi
if ! commit_deployment_boundary; then
  if [[ "${DEPLOYMENT_COMMITTED:-0}" -eq 1 ]]; then
    exit 1
  fi
  run_deployment_rollback_and_exit 1
fi
if [[ "${POST_COMMIT_CLEANUP_FAILED:-0}" -ne 0 ]]; then
  exit 1
fi
echo "Installed ${GATEWAY_SERVICE_NAME} runtime caps drop-in → ${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
echo "Installed ${GATEWAY_SERVICE_NAME} Codex runtime verifier → ${CODEX_RUNTIME_DROPIN_DST}"
echo "Installed ${GATEWAY_SERVICE_NAME} native-crash hardening → ${NATIVE_CRASH_HARDENING_DROPIN_DST}"
echo "Reloaded user systemd units; restart ${GATEWAY_SERVICE_NAME} externally for a running gateway to inherit these caps."

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
