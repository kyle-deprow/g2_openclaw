#!/usr/bin/env bash
# push-openclaw-config.sh — Merge repo-maintained OpenClaw config into the local installation.
#
# Architecture:
#   Bash owns orchestration, temporary recovery directories, and trap/signal wiring.
#   gateway/deployment owns all deployment logic; switched functions below are
#   single-path Python wrappers.
#
# Usage (from repo root):
#   bash scripts/push-openclaw-config.sh
#
# Prerequisites:
#   - jq (https://jqlang.github.io/jq/)
#   - OpenClaw CLI exactly 2026.7.1-2
#   - MemPalace installed with 'make mempalace-install'
#   - For codex: run 'openclaw models auth login --provider openai' for main;
#     this script syncs that OpenClaw-managed Codex OAuth profile into managed
#     agent auth stores.
#   - For azure: run 'az login' to authenticate (Entra ID tokens acquired automatically)

set -euo pipefail

OPENCLAW_PUSH_IMPL="${OPENCLAW_PUSH_IMPL:-python}"
if [[ "${OPENCLAW_PUSH_IMPL}" != "python" ]]; then
  echo "ERROR: the bash implementation was removed in the P4 cutover; unset OPENCLAW_PUSH_IMPL." >&2
  exit 1
fi

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
QUANTIPY_API_UNIT_TEMPLATE="${REPO_ROOT}/gateway/openclaw_config/quantipy-api.service.template"
QUANTIPY_API_SERVICE_NAME="quantipy-api.service"
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
QUANTIPY_API_UNIT_DST="${SYSTEMD_USER_DIR}/quantipy-api.service"
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
quote_sqlite_literal() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\'}"
}

sync_managed_agent_codex_auth() {
  local source_agent_dir="${OPENCLAW_PUSH_HOME}/agents/main/agent"
  local source_db="${source_agent_dir}/openclaw-agent.sqlite"
  local source_profiles="${source_agent_dir}/auth-profiles.json"
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.auth_sync sync \
    -- "${OPENCLAW_PUSH_HOME}" "${REPO_CONFIG}" "${OPENCLAW_BIN_RESOLVED}"
}

build_string_array_json() {
  printf '%s\n' "$@" | jq -Rsc 'split("\n")[:-1]'
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
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.identity file-sha256 -- "$1"
}

file_bytes() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.identity file-bytes -- "$1"
}

path_exists_or_symlink() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs path-exists-or-symlink -- "$1"
}

guarded_regular_file_identity() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.identity guarded-regular-file-identity -- "$1" "$2"
}

verify_guarded_regular_file_identity_unchanged() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.identity \
    verify-guarded-regular-file-identity-unchanged -- "$1" "$2" "$3"
}

guard_destination_path_chain() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guard-destination-path-chain -- "$1" "$2"
}

guard_destination_parent_path_chain() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guard-destination-parent-path-chain -- "$1" "$2"
}

guarded_mkdir_p() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs guarded-mkdir-p -- "$1" "$2"
}

copy_path_topology() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs copy-path-topology -- "$1" "$2"
}

guarded_copy_path_topology() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guarded-copy-path-topology -- "$1" "$2" "$3"
}

guarded_copy_path_topology_preserving_final_symlink_topology() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guarded-copy-path-topology-preserving-final-symlink-topology -- "$1" "$2" "$3"
}

guarded_cp_file() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guarded-cp-file -- "$1" "$2" "$3"
}

guarded_chmod() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs guarded-chmod -- "$1" "$2" "$3"
}

guarded_chmod_reference() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guarded-chmod-reference -- "$1" "$2" "$3"
}

collect_find_results_null() {
  local output_path="$1"
  local scan_root="$2"
  local context="$3"
  shift 3

  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    collect-find-results-null -- "${output_path}" "${scan_root}" "${context}" "$@"
}

guarded_rm_rf() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs guarded-rm-rf -- "$1" "$2"
}

guarded_rm_f() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs guarded-rm-f -- "$1" "$2"
}

guarded_rm() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs guarded-rm -- "$1" "$2"
}

guarded_rmdir() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs guarded-rmdir -- "$1" "$2"
}

guarded_mv_replace() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  shift 3
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guarded-mv-replace -- "${source_path}" "${destination_path}" "${context}" "$@"
}

guarded_mv_replace_preserving_final_symlink_topology() {
  local source_path="$1"
  local destination_path="$2"
  local context="$3"
  shift 3
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    guarded-mv-replace-preserving-final-symlink-topology \
    -- "${source_path}" "${destination_path}" "${context}" "$@"
}

restore_path_topology_from_backup() {
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs \
    restore-path-topology-from-backup -- "$1" "$2" "$3"
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

validate_quantipy_api_unit_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Repo-managed Quantipy API unit not found at ${path}" >&2
    return 1
  fi
  if ! grep -Fxq "Description=Quantipy Data API" "${path}" \
    || ! grep -Fxq "Type=simple" "${path}" \
    || ! grep -Fxq "WorkingDirectory=/home/dev/repos/quantipy" "${path}" \
    || ! grep -Fxq "ExecStart=/home/dev/repos/quantipy/.venv/bin/python -m quantipy.api --host 127.0.0.1 --port 8000" "${path}" \
    || ! grep -Fxq "Restart=on-failure" "${path}" \
    || grep -Fxq "Restart=always" "${path}" \
    || ! grep -Fxq "RestartSec=10" "${path}" \
    || ! grep -Fxq "TimeoutStopSec=30" "${path}" \
    || ! grep -Fxq "KillMode=control-group" "${path}" \
    || ! grep -Fxq "WantedBy=default.target" "${path}" \
    || grep -Fxq "Requires=${GATEWAY_SERVICE_NAME}" "${path}" \
    || grep -Fxq "BindsTo=${GATEWAY_SERVICE_NAME}" "${path}"; then
    echo "ERROR: Quantipy API unit must describe the independent data API and must not use Restart=always or bind to ${GATEWAY_SERVICE_NAME}." >&2
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
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.guarded_fs path-owned-by-effective-user \
    -- "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"; then
    echo "ERROR: Runtime caps drop-in directory is not owned by the current user: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
    return 1
  fi
  guarded_chmod 0755 "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "chmod managed systemd drop-in directory ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"
}

decode_systemd_show_environment_word() {
  local encoded="$1"
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.systemd_env decode-word -- "${encoded}"
}

decode_systemd_show_environment_node_options() {
  local manager_env="$1"
  local result present encoded
  SYSTEMD_DECODED_NODE_OPTIONS_PRESENT=0
  SYSTEMD_DECODED_NODE_OPTIONS_VALUE=""
  if ! result="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.systemd_env decode-node-options -- "${manager_env}" 2>&1)"; then
    printf '%s\n' "${result}" >&2
    return 1
  fi
  IFS=$'\t' read -r present encoded <<< "${result}"
  SYSTEMD_DECODED_NODE_OPTIONS_PRESENT="${present}"
  if [[ "${present}" -eq 1 ]]; then
    SYSTEMD_DECODED_NODE_OPTIONS_VALUE="$(printf '%s' "${encoded}" | base64 -d)"
  fi
}

verify_systemd_manager_node_options_stale_preload_absent() {
  local result
  if ! result="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.systemd_env verify-node-options \
    -- "${STALE_AZURE_PRELOAD_PATTERN}" 2>&1)"; then
    printf '%s\n' "${result}" >&2
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
  local module_output module_status line marker present state_changed encoded
  # systemctl --user daemon-reload is performed by the deployment helper.

  if module_output="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.systemd_env remove-stale-azure-node-options \
    -- "${service_path}" "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "${STALE_AZURE_PRELOAD_PATTERN}" 2>&1)"; then
    module_status=0
  else
    module_status=$?
  fi

  if [[ -n "${module_output}" ]]; then
    while IFS= read -r line; do
      case "${line}" in
        __G2_SYSTEMD_ENV_STATE__$'\t'*)
          IFS=$'\t' read -r marker present state_changed encoded <<< "${line}"
          SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT="${present}"
          SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED="${state_changed}"
          SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL="$(printf '%s' "${encoded}" | base64 -d)"
          ;;
        *)
          if [[ "${module_status}" -eq 0 ]]; then
            printf '%s\n' "${line}"
          else
            printf '%s\n' "${line}" >&2
          fi
          ;;
      esac
    done <<< "${module_output}"
  fi
  return "${module_status}"
}
resolve_openclaw_bin() {
  local -a candidates=()
  local candidate path_entry
  declare -A seen=()
  local resolved

  if ! resolved="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.versions resolve-openclaw-bin 2>&1)"; then
    printf '%s\n' "${resolved}" >&2
    return 1
  fi
  OPENCLAW_BIN_RESOLVED="${resolved}"
  return 0
}

require_openclaw_supported() {
  local version_line
  if ! resolve_openclaw_bin; then
    return 1
  fi
  if ! prepare_repo_config_preflight_copy; then
    return 1
  fi
  if ! version_line="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.versions require-openclaw-supported \
    -- "${OPENCLAW_BIN_RESOLVED}" "${REPO_CONFIG_PREFLIGHT_COPY}" "${OPENCLAW_PUSH_HOME}" 2>&1)"; then
    if ! verify_repo_config_preflight_copy_unchanged "openclaw --version"; then
      return 1
    fi
    printf '%s\n' "${version_line}" >&2
    return 1
  fi
  if ! verify_repo_config_preflight_copy_unchanged "openclaw --version"; then
    return 1
  fi
  OPENCLAW_VERSION_RESOLVED="${version_line}"
  export OPENCLAW_BIN="${OPENCLAW_BIN_RESOLVED}"
  return 0
}

require_codex_runtime_exact() {
  local inspect_json plugin_version app_server_version app_server_path
  if ! prepare_repo_config_preflight_copy; then
    return 1
  fi
  if ! inspect_json="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.versions require-codex-runtime-exact \
    -- "${OPENCLAW_BIN_RESOLVED}" "${REPO_CONFIG_PREFLIGHT_COPY}" "${OPENCLAW_PUSH_HOME}" 2>&1)"; then
    if ! verify_repo_config_preflight_copy_unchanged "openclaw plugins inspect codex --json"; then
      return 1
    fi
    printf '%s\n' "${inspect_json}" >&2
    return 1
  fi
  if ! verify_repo_config_preflight_copy_unchanged "openclaw plugins inspect codex --json"; then
    return 1
  fi
  IFS=$'\t' read -r plugin_version app_server_version app_server_path <<< "${inspect_json}"
  CODEX_APP_SERVER_CLI_RESOLVED="${app_server_path}/bin/codex.js"
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
  "${QUANTIPY_API_UNIT_DST}"
  "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
  "${CODEX_RUNTIME_DROPIN_DST}"
  "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
)
MANAGED_ARTIFACT_TRANSACTION_ARMED=0
MANAGED_ARTIFACT_BACKUP_DIR=""
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
  guard_destination_path_chain "${SYSTEMD_USER_DIR}" "creating managed systemd transaction backup directory under ${SYSTEMD_USER_DIR}" || return 1
  MANAGED_UNIT_BACKUP_DIR="$(mktemp -d "${SYSTEMD_USER_DIR}/.push-openclaw-config-units.XXXXXX")"
  guard_destination_path_chain "${MANAGED_UNIT_BACKUP_DIR}" "created managed systemd transaction backup directory ${MANAGED_UNIT_BACKUP_DIR}" || return 1
  local transaction_status
  if PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions begin-unit-tx \
    -- "${MANAGED_UNIT_BACKUP_DIR}" "${MANAGED_UNIT_PATHS[@]}"; then
    transaction_status=0
  else
    transaction_status=$?
  fi
  if [[ "${transaction_status}" -eq 0 ]]; then
    local path
    for path in "${MANAGED_UNIT_PATHS[@]}"; do
      if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON_BIN}" -m gateway.deployment.transactions snapshot-unit \
        -- "${MANAGED_UNIT_BACKUP_DIR}" "${path}"; then
        transaction_status=1
      fi
    done
  fi
  MANAGED_UNIT_TRANSACTION_ARMED=1
  if [[ "${transaction_status}" -ne 0 ]]; then
    ROLLBACK_FAILED=1
    return 1
  fi
  return 0
}

rollback_managed_unit_transaction() {
  if [[ "${MANAGED_UNIT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions rollback-unit-tx \
    -- "${MANAGED_UNIT_BACKUP_DIR}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  MANAGED_UNIT_TRANSACTION_ARMED=0
  return 0
}

finalize_managed_unit_transaction() {
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions finalize-unit-tx \
    -- "${MANAGED_UNIT_BACKUP_DIR:-}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  return 0
}

cleanup_managed_unit_backup_dir() {
  if [[ -n "${MANAGED_UNIT_BACKUP_DIR:-}" ]]; then
    if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
      "${PYTHON_BIN}" -m gateway.deployment.transactions cleanup-tx \
      -- unit committed "${MANAGED_UNIT_BACKUP_DIR}"; then
      POST_COMMIT_CLEANUP_FAILED=1
      return 1
    fi
  fi
  MANAGED_UNIT_BACKUP_DIR=""
  return 0
}

begin_managed_artifact_transaction() {
  if [[ "${MANAGED_ARTIFACT_TRANSACTION_ARMED:-0}" -eq 1 ]]; then
    return 0
  fi
  guard_destination_path_chain "${OPENCLAW_PUSH_HOME}" "creating managed OpenClaw artifact backup directory under ${OPENCLAW_PUSH_HOME}" || return 1
  MANAGED_ARTIFACT_BACKUP_DIR="$(mktemp -d "${OPENCLAW_PUSH_HOME}/.push-openclaw-config-artifacts.XXXXXX")"
  guard_destination_path_chain "${MANAGED_ARTIFACT_BACKUP_DIR}" "created managed OpenClaw artifact backup directory ${MANAGED_ARTIFACT_BACKUP_DIR}" || return 1
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions begin-artifact-tx \
    -- "${MANAGED_ARTIFACT_BACKUP_DIR}"; then
    ROLLBACK_FAILED=1
    MANAGED_ARTIFACT_TRANSACTION_ARMED=1
    return 1
  fi
  MANAGED_ARTIFACT_TRANSACTION_ARMED=1
  return 0
}

snapshot_managed_artifact_path() {
  local path="${1}"
  begin_managed_artifact_transaction || return 1
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions snapshot-artifact \
    -- "${MANAGED_ARTIFACT_BACKUP_DIR}" "${path}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  return 0
}

is_systemd_managed_artifact_path() {
  local path="$1"
  [[ "${path}" == "${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}" \
    || "${path}" == "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" \
    || "${path}" == "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/"* ]]
}

rollback_managed_artifact_transaction() {
  if [[ "${MANAGED_ARTIFACT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi
  local module_output
  if ! module_output="$(SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR}" \
    GATEWAY_SERVICE_NAME="${GATEWAY_SERVICE_NAME}" \
    PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions rollback-artifact-tx \
    -- "${MANAGED_ARTIFACT_BACKUP_DIR}")"; then
    ROLLBACK_FAILED=1
    if [[ "${module_output}" == "${ARTIFACT_SYSTEMD_MARKER:-__G2_ARTIFACT_RESTORED_SYSTEMD__}" ]]; then
      MANAGED_ARTIFACT_RESTORED_SYSTEMD=1
    fi
    return 1
  fi
  if [[ "${module_output}" == "${ARTIFACT_SYSTEMD_MARKER:-__G2_ARTIFACT_RESTORED_SYSTEMD__}" ]]; then
    MANAGED_ARTIFACT_RESTORED_SYSTEMD=1
  fi
  MANAGED_ARTIFACT_TRANSACTION_ARMED=0
  return 0
}

final_systemd_reload_after_artifact_rollback() {
  if [[ "${MANAGED_ARTIFACT_RESTORED_SYSTEMD:-0}" -ne 1 ]]; then
    return 0
  fi
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions \
    final-systemd-reload-after-artifact-rollback \
    -- "${MANAGED_ARTIFACT_RESTORED_SYSTEMD}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  MANAGED_ARTIFACT_RESTORED_SYSTEMD=0
  return 0
}

finalize_managed_artifact_transaction() {
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions finalize-artifact-tx \
    -- "${MANAGED_ARTIFACT_BACKUP_DIR:-}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  return 0
}

cleanup_managed_artifact_backup_dir() {
  if [[ -n "${MANAGED_ARTIFACT_BACKUP_DIR:-}" ]]; then
    if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
      "${PYTHON_BIN}" -m gateway.deployment.transactions cleanup-tx \
      -- artifact committed "${MANAGED_ARTIFACT_BACKUP_DIR}"; then
      POST_COMMIT_CLEANUP_FAILED=1
      return 1
    fi
  fi
  MANAGED_ARTIFACT_BACKUP_DIR=""
  MANAGED_ARTIFACT_RESTORED_SYSTEMD=0
  return 0
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
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions validate-local-config \
    -- "${ROLLBACK_ARMED:-0}" "${BACKUP:-}" "${LOCAL_CONFIG}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  return 0
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
    if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
      "${PYTHON_BIN}" -m gateway.deployment.transactions cleanup-tx \
      -- unit rollback "${MANAGED_UNIT_BACKUP_DIR}"; then
      cleanup_failed=1
    else
      MANAGED_UNIT_BACKUP_DIR=""
    fi
  fi
  if [[ -n "${MANAGED_ARTIFACT_BACKUP_DIR:-}" ]]; then
    if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
      "${PYTHON_BIN}" -m gateway.deployment.transactions cleanup-tx \
      -- artifact rollback "${MANAGED_ARTIFACT_BACKUP_DIR}"; then
      cleanup_failed=1
    else
      MANAGED_ARTIFACT_BACKUP_DIR=""
    fi
  fi
  if [[ -n "${REPO_CONFIG_PREFLIGHT_DIR:-}" ]]; then
    if ! guarded_rm_rf "${REPO_CONFIG_PREFLIGHT_DIR}" "removing guarded repo OpenClaw config copy ${REPO_CONFIG_PREFLIGHT_DIR} after rollback"; then
      echo "ERROR: Failed to remove guarded repo OpenClaw config copy ${REPO_CONFIG_PREFLIGHT_DIR} after rollback." >&2
      cleanup_failed=1
    else
      REPO_CONFIG_PREFLIGHT_COPY=""
      REPO_CONFIG_PREFLIGHT_DIR=""
    fi
  fi
  if [[ "${cleanup_failed}" -ne 0 ]]; then
    ROLLBACK_FAILED=1
    return 1
  fi
  return 0
}

report_retained_recovery_paths() {
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions report-retained-recovery-paths \
    -- "${ROLLBACK_ARMED:-0}" "${BACKUP:-}" \
    "${MANAGED_UNIT_BACKUP_DIR:-}" "${MANAGED_ARTIFACT_BACKUP_DIR:-}" \
    "${REPO_CONFIG_PREFLIGHT_DIR:-}"; then
    :
  fi
  return 0
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
  if [[ "${ROLLBACK_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.transactions restore-local-config \
    -- "${BACKUP}" "${LOCAL_CONFIG}" "${OPENCLAW_PUSH_HOME}"; then
    ROLLBACK_FAILED=1
    return 1
  fi
  ROLLBACK_ARMED=0
  return 0
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
  if ! cleanup_deployment_temp_file "${QUANTIPY_API_UNIT_TMP:-}"; then
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
if [[ ! -f "${QUANTIPY_API_UNIT_TEMPLATE}" ]]; then
  echo "ERROR: Repo-managed Quantipy API unit template not found at ${QUANTIPY_API_UNIT_TEMPLATE}" >&2
  exit 1
fi
if ! validate_quantipy_api_unit_file "${QUANTIPY_API_UNIT_TEMPLATE}"; then
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

assemble_openclaw_config() {
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
    return 1
  fi

  if ! "${MEMPALACE_PYTHON}" -c 'import mempalace.mcp_server' >/dev/null 2>&1; then
    echo "ERROR: MemPalace is installed but the MCP server module cannot be imported." >&2
    echo "       Run 'make mempalace-install' to upgrade/reinstall MemPalace." >&2
    return 1
  fi

  guarded_mkdir_p "${MEMPALACE_PALACE}" "creating managed MemPalace palace directory ${MEMPALACE_PALACE}"
  guarded_mkdir_p "${FASTEMBED_CACHE_PATH}" "creating managed FastEmbed cache directory ${FASTEMBED_CACHE_PATH}"

  if ! "${MEMPALACE_PYTHON}" "${REPO_ROOT}/scripts/check-mempalace-health.py"; then
    echo "ERROR: MemPalace healthcheck failed. Refusing to push OpenClaw config." >&2
    echo "       Fix the palace explicitly; startup will not auto-repair or fall back." >&2
    return 1
  fi

  guarded_mkdir_p "${OPENCLAW_PUSH_HOME}" "creating managed OpenClaw home ${OPENCLAW_PUSH_HOME}"
  snapshot_managed_artifact_path "${MEMPALACE_READONLY_WRAPPER_DST}"
  guarded_cp_file "${MEMPALACE_READONLY_WRAPPER_SRC}" "${MEMPALACE_READONLY_WRAPPER_DST}" "installing MemPalace read-only wrapper ${MEMPALACE_READONLY_WRAPPER_DST}"
  echo "Installed MemPalace read-only wrapper → ${MEMPALACE_READONLY_WRAPPER_DST}"

  PROVIDER="${OPENCLAW_PROVIDER:-codex}"
  case "${PROVIDER}" in
    codex)
      MODEL_PRIMARY="openai/${OPENAI_MODEL:-gpt-5.4}"
      ;;
    azure)
      MODEL_PRIMARY="azure-oai-g2/gpt-5.4"
      ;;
    openrouter)
      MODEL_PRIMARY="openrouter/${OPENROUTER_MODEL:-anthropic/claude-sonnet-4-20250514}"
      ;;
    *)
      echo "ERROR: Unknown OPENCLAW_PROVIDER '${PROVIDER}'. Use 'codex', 'azure', or 'openrouter'." >&2
      return 1
      ;;
  esac

  if ! PM_MODEL_PRIMARY="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.config_merge pm-model \
    -- "${REPO_CONFIG}")"; then
    return 1
  fi
  if [[ -z "${PM_MODEL_PRIMARY}" ]]; then
    echo "ERROR: Repo config must pin agents.list[].id == \"autoresearch-pm\" to a model.primary." >&2
    return 1
  fi
  if [[ "${PM_MODEL_PRIMARY}" != openai/* ]]; then
    echo "ERROR: PM model '${PM_MODEL_PRIMARY}' must use the OpenAI/Codex provider." >&2
    return 1
  fi

  if ! MERGED="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.config_merge assemble \
    -- "${LOCAL_CONFIG}" "${REPO_CONFIG}" "${REPO_ROOT}" "${PYTHON_BIN}" \
    "${MEMPALACE_PYTHON}" "${MEMPALACE_PALACE}" "${MEMPALACE_READONLY_WRAPPER_DST}" \
    "${FASTEMBED_CACHE_PATH}" "${MEMPALACE_EMBEDDING_MODEL}" "${HF_HUB_OFFLINE}" \
    "${G2_CONTROL_MCP_MODULE}" "${MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON}" \
    "${G2_CONTROL_SERVER_AGENT_IDS_JSON}")"; then
    return 1
  fi
  echo "Resolved read-only MemPalace MCP wrapper: ${MEMPALACE_READONLY_WRAPPER_DST}"
  echo "Resolved MemPalace embedding: ${MEMPALACE_EMBEDDING_MODEL} (cache: ${FASTEMBED_CACHE_PATH})"
  if [[ "${PROVIDER}" == "openrouter" ]] && [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    echo "Resolved env:OPENROUTER_API_KEY (${#OPENROUTER_API_KEY} chars)."
  fi
  echo "Sanitized stale coding-provider config keys: github-copilot copilot-proxy copilot-cli"
  echo "Active provider: ${PROVIDER} → default model: ${MODEL_PRIMARY}; PM model: ${PM_MODEL_PRIMARY}"
}

assemble_openclaw_config

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
  printf '%s\n' "${MERGED}" > "${temp_config}"
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
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_BIN}" -m gateway.deployment.codex_agents validate-stage-agents -- "${agents_dir}"
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
  PYTHONSAFEPATH=1 \
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PYTHON_BIN}" -m gateway.deployment.codex_agents write-runtime-config
  guard_destination_path_chain "${codex_home}/config.toml" "wrote managed Codex runtime config ${codex_home}/config.toml"
}

validate_codex_runtime_config() {
  local codex_home="$1"
  local agent_id="$2"
  local config_path="${codex_home}/config.toml"
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PYTHON_BIN}" -m gateway.deployment.codex_agents validate-mcp-wiring \
    -- "${config_path}" "${agent_id}" \
    "${MEMPALACE_PYTHON}" "${MEMPALACE_READONLY_WRAPPER_DST}" "${MEMPALACE_PALACE}" \
    "${PYTHON_BIN}" "${G2_CONTROL_MCP_MODULE}" "${REPO_ROOT}"
  repair_codex_runtime_log_db "${codex_home}"
  repair_codex_runtime_state_db "${codex_home}"
  validate_codex_doctor_owned_checks "${codex_home}" "${config_path}"
}

repair_codex_runtime_log_db() {
  local codex_home="$1"
  local log_db="${codex_home}/logs_2.sqlite"
  local repair_output

  if ! repair_output="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_BIN}" -m gateway.deployment.codex_db_repair repair-log-db -- "${log_db}" 2>&1)"; then
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

  if ! repair_output="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_BIN}" -m gateway.deployment.codex_db_repair repair-state-db -- "${state_db}" 2>&1)"; then
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

  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_BIN}" -m gateway.deployment.doctor validate -- "${doctor_stdout}" "${doctor_stderr}" "${codex_home}" "${config_path}" "${REQUIRED_CODEX_APP_SERVER_VERSION}" "${app_server_package_root}" "${doctor_status}"
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

run_autoresearch_pm_command_contract_probe() {
  local codex_home="${OPENCLAW_PUSH_HOME}/agents/autoresearch-pm/agent/codex-home"
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.command_probe probe \
    -- "${codex_home}" "${CODEX_APP_SERVER_CLI_RESOLVED}"; then
    echo "ERROR: Autoresearch PM command-contract probe failed; rolling back managed deployment from backup ${BACKUP}." >&2
    return 1
  fi
}

if [[ "${PROVIDER}" == "codex" ]]; then
  run_autoresearch_pm_command_contract_probe || run_deployment_rollback_and_exit 1
fi

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

QUANTIPY_API_UNIT_TMP="$(mktemp "${SYSTEMD_USER_DIR}/.${QUANTIPY_API_SERVICE_NAME}.XXXXXX")"
guard_destination_path_chain "${QUANTIPY_API_UNIT_TMP}" "writing generated Quantipy API unit ${QUANTIPY_API_UNIT_TMP}"
sed \
  -e "s|@REPO_ROOT@|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|@HOME@|$(escape_sed_replacement "${HOME}")|g" \
  -e "s|@PATH@|$(escape_sed_replacement "${PATH}")|g" \
  -e "s|@PYTHON_BIN@|$(escape_sed_replacement "${PYTHON_BIN}")|g" \
  "${QUANTIPY_API_UNIT_TEMPLATE}" > "${QUANTIPY_API_UNIT_TMP}"
guard_destination_path_chain "${QUANTIPY_API_UNIT_TMP}" "wrote generated Quantipy API unit ${QUANTIPY_API_UNIT_TMP}"
if grep -q '@[A-Z_][A-Z_]*@' "${QUANTIPY_API_UNIT_TMP}"; then
  echo "ERROR: Unresolved placeholder in generated ${QUANTIPY_API_SERVICE_NAME}." >&2
  exit 1
fi
if ! validate_quantipy_api_unit_file "${QUANTIPY_API_UNIT_TMP}"; then
  exit 1
fi
guarded_chmod 0644 "${QUANTIPY_API_UNIT_TMP}" "chmod generated Quantipy API unit ${QUANTIPY_API_UNIT_TMP}"
guarded_mv_replace "${QUANTIPY_API_UNIT_TMP}" "${QUANTIPY_API_UNIT_DST}" "publishing managed Quantipy API unit ${QUANTIPY_API_UNIT_DST}"
QUANTIPY_API_UNIT_TMP=""
echo "Installed ${QUANTIPY_API_SERVICE_NAME} (not started)."

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
