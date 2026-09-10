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
#   - OpenClaw CLI exactly 2026.9.2
#   - MemPalace installed with 'make mempalace-install'
#   - For codex: run 'openclaw models auth login --provider openai' for main;
#     this script syncs that OpenClaw-managed Codex OAuth profile into managed
#     agent auth stores.
#   - For azure: run 'az login' to authenticate (Entra ID tokens acquired automatically)

set -euo pipefail

# The push mode is deliberately selected before the optional env file is
# sourced.  An env file may configure provider credentials and roots, but it
# must never be able to opt an ordinary push into paused deployment semantics.
# Capture both the value and whether the caller supplied the selector so a
# sourced env file cannot unset, replace, or smuggle a second selector.
if [[ -v OPENCLAW_PUSH_MODE_REQUESTED ]]; then
  echo "ERROR: OPENCLAW_PUSH_MODE_REQUESTED is reserved and must not be supplied by the invoking environment." >&2
  exit 1
fi
if [[ -v OPENCLAW_PUSH_MODE ]]; then
  readonly OPENCLAW_PUSH_MODE_INVOCATION_SET=1
  readonly OPENCLAW_PUSH_MODE_INVOCATION_VALUE="${OPENCLAW_PUSH_MODE}"
else
  readonly OPENCLAW_PUSH_MODE_INVOCATION_SET=0
  readonly OPENCLAW_PUSH_MODE_INVOCATION_VALUE=""
fi
readonly OPENCLAW_PUSH_MODE_REQUESTED="${OPENCLAW_PUSH_MODE:-normal}"
case "${OPENCLAW_PUSH_MODE_REQUESTED}" in
  normal | paused)
    ;;
  *)
    echo "ERROR: Unsupported OPENCLAW_PUSH_MODE '${OPENCLAW_PUSH_MODE_REQUESTED}'. Use 'normal' or 'paused'." >&2
    exit 1
    ;;
esac
OPENCLAW_PUSH_MODE="${OPENCLAW_PUSH_MODE_REQUESTED}"
if [[ "${OPENCLAW_PUSH_MODE}" == "paused" && -z "${XDG_RUNTIME_DIR:-}" ]]; then
  echo "ERROR: Paused mode requires XDG_RUNTIME_DIR to inspect the runtime gateway mask." >&2
  exit 1
fi

OPENCLAW_PUSH_IMPL="${OPENCLAW_PUSH_IMPL:-python}"
if [[ "${OPENCLAW_PUSH_IMPL}" != "python" ]]; then
  echo "ERROR: the bash implementation was removed in the P4 cutover; unset OPENCLAW_PUSH_IMPL." >&2
  exit 1
fi

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_CONFIG="${REPO_ROOT}/gateway/openclaw_config/openclaw.json"
ENV_FILE="${OPENCLAW_PUSH_ENV_FILE:-${REPO_ROOT}/gateway/openclaw_config/.env}"
APPROVED_OWNER_ENV_FILE="${ENV_FILE}"
QUANTIPY_ROOT="/home/dev/repos/quantipy"
SKILLS_SRC="${SKILLS_SRC:-${REPO_ROOT}/gateway/agent_config/skills}"
CODEX_AGENTS_SRC="${REPO_ROOT}/.codex/agents"
CODEX_AGENT_CONFIGS_SRC="${REPO_ROOT}/.codex/agent-configs"
RESEARCH_OWNER_PERSONA_SRC="${RESEARCH_OWNER_PERSONA_SRC:-${REPO_ROOT}/gateway/agent_config/research-orchestrator}"
MEMPALACE_READONLY_WRAPPER_SRC="${REPO_ROOT}/gateway/mempalace_readonly_server.py"
G2_CONTROL_MCP_MODULE="gateway.g2_control_mcp_server"
RESEARCH_OWNER_UNIT_TEMPLATE="${REPO_ROOT}/gateway/openclaw_config/research-owner.service.template"
RESEARCH_OWNER_SERVICE_NAME="research-owner.service"
QUANTIPY_API_UNIT_TEMPLATE="${REPO_ROOT}/gateway/openclaw_config/quantipy-api.service.template"
QUANTIPY_API_SERVICE_NAME="quantipy-api.service"
GATEWAY_RUNTIME_CAPS_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/openclaw-gateway-runtime-caps.conf"
NATIVE_CRASH_HARDENING_DROPIN_SRC="${REPO_ROOT}/gateway/openclaw_config/openclaw-gateway-native-crash-hardening.conf"
GATEWAY_SERVICE_NAME="openclaw-gateway.service"
HEALTHCHECK_TIMER_NAME="openclaw-gateway-healthcheck.timer"
HEALTHCHECK_SERVICE_NAME="openclaw-gateway-healthcheck.service"
GATEWAY_RUNTIME_CAPS_DROPIN_NAME="10-quantipy-runtime-caps.conf"
NATIVE_CRASH_HARDENING_DROPIN_NAME="30-openclaw-native-crash-hardening.conf"
STALE_AZURE_PRELOAD_PATTERN="azure-api-version-preload.cjs"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
RESEARCH_OWNER_UNIT_DST="${SYSTEMD_USER_DIR}/${RESEARCH_OWNER_SERVICE_NAME}"
QUANTIPY_API_UNIT_DST="${SYSTEMD_USER_DIR}/quantipy-api.service"
GATEWAY_RUNTIME_CAPS_DROPIN_DIR="${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}.d"
GATEWAY_RUNTIME_CAPS_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/${GATEWAY_RUNTIME_CAPS_DROPIN_NAME}"
NATIVE_CRASH_HARDENING_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/${NATIVE_CRASH_HARDENING_DROPIN_NAME}"
OBSOLETE_CODEX_RUNTIME_DROPIN_DST="${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/20-openclaw-codex-runtime.conf"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"

REQUIRED_OPENCLAW_VERSION="2026.9.2"
REQUIRED_CODEX_PLUGIN_VERSION="2026.9.2"
REQUIRED_CODEX_APP_SERVER_VERSION="0.153.4"
OPENCLAW_BIN_RESOLVED=""
OPENCLAW_VERSION_RESOLVED=""
CODEX_APP_SERVER_CLI_RESOLVED=""
ACPX_ADAPTER_BIN=""
RESEARCH_REVIEWER_LAUNCHER="${REPO_ROOT}/scripts/research-reviewer-cli.py"
MEMPALACE_READONLY_WRAPPER_BASENAME="mempalace-readonly-server.py"
RESEARCH_ORCHESTRATOR_DENY_TOOL_IDS=(
  "sessions_yield"
  "agents_list"
  "sessions_list"
  "sessions_history"
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
MEMPALACE_READONLY_AGENT_IDS=()
CODEX_NATIVE_STAGE_AGENT_IDS=("implementer" "experiment_runner")
# These are the exact research-role files that earlier route deployments may
# have installed in a scoped Codex runtime.  The owner config registers the
# active child layers; remove only this known managed duplicate set from old
# runtime homes. Manual or unrelated Codex agent files must survive a config
# push.
RETIRED_CODEX_RUNTIME_AGENT_FILES=(
  "context-curator.toml"
  "debater-microstructure.toml"
  "debater-data.toml"
  "debater-skeptic.toml"
  "debater-theory.toml"
  "debater-implementation.toml"
  "consensus-arbiter.toml"
  "context_curator.toml"
  "debater_microstructure.toml"
  "debater_data.toml"
  "debater_skeptic.toml"
  "debater_theory.toml"
  "debater_implementation.toml"
  "consensus_arbiter.toml"
  "implementer.toml"
  "experiment_runner.toml"
  "reviewer.toml"
  "fixer.toml"
)
# The owner workspace layer directory is a managed route directory, so prune
# only these exact old research layers there. Unknown/manual TOMLs remain
# untouched because ownership of them is not established by this route.
RETIRED_CODEX_RESEARCH_LAYER_FILES=(
  "context-curator.toml"
  "debater-microstructure.toml"
  "debater-data.toml"
  "debater-skeptic.toml"
  "debater-theory.toml"
  "debater-implementation.toml"
  "consensus-arbiter.toml"
  "context_curator.toml"
  "debater_microstructure.toml"
  "debater_data.toml"
  "debater_skeptic.toml"
  "debater_theory.toml"
  "debater_implementation.toml"
  "consensus_arbiter.toml"
  "reviewer.toml"
  "fixer.toml"
)
MEMPALACE_READONLY_SERVER_AGENT_IDS=("main")
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
RUNTIME_CAP_ENV_LINES=(
  '[Service]'
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
# An OOM kill is SIGKILL; omitting it from RestartPreventExitStatus lets Restart=always recover the gateway. StartLimitBurst=5/StartLimitIntervalSec=60 still protects against restart loops.
# MemoryHigh is a reclaim-pressure throttle and MemoryMax is the kill point. The observed peaks are 6.2G for the gateway and 2.99G for long tasks; simultaneous ceilings of 10G + 12G = 22G against 30.25 GiB RAM leave headroom for the Quantipy API and OS, while throttle onset at 8G + 8G = 16G stays well below host pressure.
NATIVE_CRASH_HARDENING_LINES=(
  "[Service]"
  "MemoryHigh=8G"
  "MemoryMax=10G"
  "OOMPolicy=kill"
  "RestartPreventExitStatus=SIGABRT SIGBUS SIGFPE SIGILL SIGQUIT SIGSEGV SIGSYS SIGTRAP SIGXCPU SIGXFSZ"
)
sync_managed_agent_codex_auth() {
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

validate_bounded_research_roots() {
  local path
  for path in "${RESEARCH_V2_ROOT}" "${HYPOTHESIS_WORKTREES_ROOT}"; do
    if [[ "${path}" != /* || "${path}" == "/" ]]; then
      echo "ERROR: Research route writable roots must be absolute, non-root task directories: ${path}" >&2
      return 1
    fi
    case "${path}" in
      "/home"|"/home/"|"/home/dev"|"/home/dev/"|"/home/dev/repos"|"/home/dev/repos/"|"/repos"|"/repos/"|"/tmp"|"/tmp/"|"/var"|"/var/"|"/opt"|"/opt/")
        echo "ERROR: Research route writable root is too broad: ${path}" >&2
        return 1
        ;;
    esac
    case "${path}" in
      *$'\n'*|*$'\r'*|*$'\t'*|*'"'*|*'\\'*|*'@'*|*'%'*)
        echo "ERROR: Research route writable root contains an unsafe control, quoting, or unresolved-placeholder character: ${path}" >&2
        return 1
        ;;
    esac
  done
  if [[ "${HYPOTHESIS_WORKTREES_ROOT}" != "${RESEARCH_V2_ROOT}/hypothesis-worktrees" ]]; then
    echo "ERROR: Hypothesis worktrees root must remain nested under the canonical research root." >&2
    return 1
  fi
}

validate_research_core_database() {
  local expected_path="${OPENCLAW_PUSH_HOME}/state/openclaw.sqlite"
  if [[ -z "${RESEARCH_CORE_DATABASE:-}" ]]; then
    echo "ERROR: RESEARCH_CORE_DATABASE is required; refusing to infer a task database path." >&2
    return 1
  fi
  if [[ "${RESEARCH_CORE_DATABASE}" != "${expected_path}" ]]; then
    echo "ERROR: RESEARCH_CORE_DATABASE must equal the configured OpenClaw state database ${expected_path}." >&2
    return 1
  fi
  if [[ "${RESEARCH_CORE_DATABASE}" != /* || "${RESEARCH_CORE_DATABASE}" == "/" ]]; then
    echo "ERROR: RESEARCH_CORE_DATABASE must be an absolute non-root path." >&2
    return 1
  fi
  case "${RESEARCH_CORE_DATABASE}" in
    *$'\n'*|*$'\r'*|*$'\t'*|*'"'*|*'\\'*|*'@'*|*'%'*)
      echo "ERROR: RESEARCH_CORE_DATABASE contains an unsafe control, quoting, or unresolved-placeholder character." >&2
      return 1
      ;;
  esac
  if [[ -L "${RESEARCH_CORE_DATABASE}" || ! -f "${RESEARCH_CORE_DATABASE}" ]]; then
    echo "ERROR: RESEARCH_CORE_DATABASE must be an existing regular non-symlink file: ${RESEARCH_CORE_DATABASE}" >&2
    return 1
  fi
}

validate_owner_env_file() {
  local path="$1" mode owner expected_owner
  if [[ "${path}" != /* || "${path}" == "/" ]]; then
    echo "ERROR: G2_OWNER_ENV_FILE must be an absolute non-root path." >&2
    return 1
  fi
  case "${path}" in
    *$'\n'*|*$'\r'*|*$'\t'*|*' '*|*'@'*|*'%'*)
      echo "ERROR: G2_OWNER_ENV_FILE contains unsupported path characters." >&2
      return 1
      ;;
  esac
  if [[ -L "${path}" || ! -f "${path}" ]]; then
    echo "ERROR: G2_OWNER_ENV_FILE must be an existing regular non-symlink file: ${path}" >&2
    return 1
  fi
  if ! owner="$(stat -c '%u' -- "${path}")" || ! expected_owner="$(id -u)"; then
    echo "ERROR: Could not inspect G2_OWNER_ENV_FILE ownership: ${path}" >&2
    return 1
  fi
  if [[ "${owner}" != "${expected_owner}" ]]; then
    echo "ERROR: G2_OWNER_ENV_FILE must be owned by the deployment user: ${path}" >&2
    return 1
  fi
  if ! mode="$(stat -c '%a' -- "${path}")" || (( 8#${mode} & 077 )); then
    echo "ERROR: G2_OWNER_ENV_FILE must not be group/world accessible: ${path}" >&2
    return 1
  fi
}

# Read the approved deployment env file as data.  Never evaluate its contents:
# the file is also consumed by the owner unit and may contain unrelated
# provider/runtime settings, but it must not redefine shell functions/options
# or execute command substitutions in this process.
OWNER_ENV_DATA_KEYS=(
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
  RESEARCH_V2_ROOT
  HYPOTHESIS_WORKTREES_ROOT
  RESEARCH_CORE_DATABASE
  OPENCLAW_HOST
  OPENCLAW_PORT
)
declare -A OWNER_ENV_DATA_ALLOWED=()
for ENV_KEY in "${OWNER_ENV_DATA_KEYS[@]}"; do
  OWNER_ENV_DATA_ALLOWED["${ENV_KEY}"]=1
done

load_owner_env_data_only() {
  local path="$1" line line_number=0 key raw value trimmed
  declare -A seen=()

  while IFS= read -r line || [[ -n "${line}" ]]; do
    ((line_number += 1))
    if [[ "${line}" == *$'\r'* ]]; then
      echo "ERROR: ${path}:${line_number}: carriage returns are not allowed in G2_OWNER_ENV_FILE." >&2
      return 1
    fi
    trimmed="${line#"${line%%[![:space:]]*}"}"
    if [[ -z "${trimmed}" || "${trimmed:0:1}" == "#" ]]; then
      continue
    fi
    if [[ "${trimmed}" == export[[:space:]]* ]]; then
      trimmed="${trimmed#export}"
      trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    fi
    if [[ "${trimmed}" != *=* ]]; then
      echo "ERROR: ${path}:${line_number}: expected KEY=VALUE in G2_OWNER_ENV_FILE." >&2
      return 1
    fi
    key="${trimmed%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    if [[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "ERROR: ${path}:${line_number}: invalid environment key in G2_OWNER_ENV_FILE." >&2
      return 1
    fi
    if [[ "${key}" == OPENCLAW_PUSH_MODE || "${key}" == OPENCLAW_PUSH_MODE_REQUESTED ]]; then
      echo "ERROR: ${path}:${line_number}: ${key} must be selected in the invoking environment, not OPENCLAW_PUSH_ENV_FILE." >&2
      return 1
    fi
    # Unknown keys remain data for systemd/the owner loader and are ignored by
    # this shell process.  Never parse or assign their values.
    if [[ -z "${OWNER_ENV_DATA_ALLOWED[${key}]+x}" ]]; then
      continue
    fi
    if [[ -n "${seen[${key}]+x}" ]]; then
      echo "ERROR: ${path}:${line_number}: duplicate selected key ${key} in G2_OWNER_ENV_FILE." >&2
      return 1
    fi
    seen["${key}"]=1
    raw="${trimmed#*=}"
    raw="${raw#"${raw%%[![:space:]]*}"}"
    if [[ "${raw:0:1}" == "'" ]]; then
      if [[ "${#raw}" -lt 2 || "${raw: -1}" != "'" ]]; then
        echo "ERROR: ${path}:${line_number}: unterminated single-quoted value for ${key}." >&2
        return 1
      fi
      value="${raw:1:${#raw}-2}"
    elif [[ "${raw:0:1}" == '"' ]]; then
      if [[ "${#raw}" -lt 2 || "${raw: -1}" != '"' ]]; then
        echo "ERROR: ${path}:${line_number}: unterminated double-quoted value for ${key}." >&2
        return 1
      fi
      value="${raw:1:${#raw}-2}"
    else
      value="${raw%"${raw##*[![:space:]]}"}"
    fi
    if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* || "${value}" == *$'\t'* ]]; then
      echo "ERROR: ${path}:${line_number}: control characters are not allowed for ${key}." >&2
      return 1
    fi
    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done < "${path}"
}

OPENCLAW_PUSH_HOME="$(expand_user_path "${OPENCLAW_PUSH_HOME:-${HOME}/.openclaw}")"
LOCAL_CONFIG="${OPENCLAW_PUSH_HOME}/openclaw.json"
MIGRATION_RECORD_DST="${OPENCLAW_PUSH_HOME}/.openclaw.migration-record.json"
GENERATED_OPENCLAW_CONFIG_TMP=""
GENERATED_OPENCLAW_CONFIG_HASH=""
GENERATED_OPENCLAW_CONFIG_BYTES=""
GENERATED_OPENCLAW_CONFIG_IDENTITY=""
REPO_CONFIG_PREFLIGHT_COPY=""
REPO_CONFIG_PREFLIGHT_DIR=""
REPO_CONFIG_PREFLIGHT_HASH=""
REPO_CONFIG_PREFLIGHT_BYTES=""
REPO_CONFIG_PREFLIGHT_IDENTITY=""
ACPX_CONFIG_PREFLIGHT_COPY=""
ACPX_CONFIG_PREFLIGHT_IDENTITY=""
ACPX_CONFIG_PREFLIGHT_HASH=""
ACPX_CONFIG_PREFLIGHT_BYTES=""
ACPX_CONFIG_SOURCE_IDENTITY=""
ACPX_CONFIG_SOURCE_HASH=""
ACPX_CONFIG_SOURCE_BYTES=""
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
    def accepted_disabled_heartbeat_warning:
      .path == "agents.defaults.heartbeat.agentId"
      and .message == "Multi-agent config has no ambient heartbeat owner; heartbeats stay disabled until agents.defaults.heartbeat.agentId or agents.defaults.systemAgent.agentId is set.";
    length == 1
    and (.[0] | type == "object")
    and (.[0].valid == true)
    and ((.[0].errors // []) | type == "array" and length == 0)
    and (
      (.[0].warnings | type == "array" and length == 0)
      or (
        (.[0].warnings | length == 1)
        and (.[0].warnings[0] | accepted_disabled_heartbeat_warning)
      )
    )
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

prepare_acpx_config_preflight_copy() {
  local load_paths_state local_identity local_hash local_bytes
  if [[ -n "${ACPX_CONFIG_PREFLIGHT_COPY:-}" ]]; then
    return 0
  fi
  ACPX_CONFIG_SOURCE_IDENTITY="$(guarded_regular_file_identity "${LOCAL_CONFIG}" "capturing local OpenClaw config identity before ACPX preflight")" || return 1
  ACPX_CONFIG_SOURCE_HASH="$(file_sha256 "${LOCAL_CONFIG}")"
  ACPX_CONFIG_SOURCE_BYTES="$(file_bytes "${LOCAL_CONFIG}")"
  if ! load_paths_state="$(jq -e -r '
    if .plugins?.load?.paths? == null then "absent" else "present" end
  ' "${LOCAL_CONFIG}" 2>/dev/null)"; then
    echo "ERROR: Local OpenClaw config cannot be parsed for ACPX preflight." >&2
    return 1
  fi
  if [[ "${load_paths_state}" == "absent" ]]; then
    ACPX_CONFIG_PREFLIGHT_COPY="${LOCAL_CONFIG}"
    return 0
  fi

  prepare_repo_config_preflight_copy || return 1
  ACPX_CONFIG_PREFLIGHT_COPY="$(mktemp "${REPO_CONFIG_PREFLIGHT_DIR}/openclaw.acpx-preflight.XXXXXX.json")"
  guard_destination_path_chain "${ACPX_CONFIG_PREFLIGHT_COPY}" "writing ACPX preflight config without machine-local plugin load paths" || return 1
  if ! jq '
    del(.plugins.load.paths)
    | if .plugins.load == {} then del(.plugins.load) else . end
  ' "${LOCAL_CONFIG}" > "${ACPX_CONFIG_PREFLIGHT_COPY}"; then
    echo "ERROR: Could not create the ACPX preflight config without local plugin load paths." >&2
    return 1
  fi
  if ! verify_guarded_regular_file_identity_unchanged "${LOCAL_CONFIG}" "${ACPX_CONFIG_SOURCE_IDENTITY}" "creating ACPX preflight config"; then
    return 1
  fi
  local_hash="$(file_sha256 "${LOCAL_CONFIG}")"
  local_bytes="$(file_bytes "${LOCAL_CONFIG}")"
  if [[ "${local_hash}" != "${ACPX_CONFIG_SOURCE_HASH}" || "${local_bytes}" != "${ACPX_CONFIG_SOURCE_BYTES}" ]]; then
    echo "ERROR: Local OpenClaw config changed while creating ACPX preflight config." >&2
    return 1
  fi
  guarded_chmod 0600 "${ACPX_CONFIG_PREFLIGHT_COPY}" "chmod ACPX preflight config ${ACPX_CONFIG_PREFLIGHT_COPY}" || return 1
  ACPX_CONFIG_PREFLIGHT_IDENTITY="$(guarded_regular_file_identity "${ACPX_CONFIG_PREFLIGHT_COPY}" "capturing ACPX preflight config identity")" || return 1
  ACPX_CONFIG_PREFLIGHT_HASH="$(file_sha256 "${ACPX_CONFIG_PREFLIGHT_COPY}")"
  ACPX_CONFIG_PREFLIGHT_BYTES="$(file_bytes "${ACPX_CONFIG_PREFLIGHT_COPY}")"
}

verify_acpx_config_preflight_unchanged() {
  local context="$1" current_hash current_bytes
  if ! verify_guarded_regular_file_identity_unchanged "${LOCAL_CONFIG}" "${ACPX_CONFIG_SOURCE_IDENTITY}" "${context} local config"; then
    return 1
  fi
  current_hash="$(file_sha256 "${LOCAL_CONFIG}")"
  current_bytes="$(file_bytes "${LOCAL_CONFIG}")"
  if [[ "${current_hash}" != "${ACPX_CONFIG_SOURCE_HASH}" || "${current_bytes}" != "${ACPX_CONFIG_SOURCE_BYTES}" ]]; then
    echo "ERROR: External OpenClaw CLI changed local config during ${context}." >&2
    return 1
  fi
  if [[ "${ACPX_CONFIG_PREFLIGHT_COPY}" != "${LOCAL_CONFIG}" ]]; then
    if ! verify_guarded_regular_file_identity_unchanged "${ACPX_CONFIG_PREFLIGHT_COPY}" "${ACPX_CONFIG_PREFLIGHT_IDENTITY}" "${context} preflight config"; then
      return 1
    fi
    current_hash="$(file_sha256 "${ACPX_CONFIG_PREFLIGHT_COPY}")"
    current_bytes="$(file_bytes "${ACPX_CONFIG_PREFLIGHT_COPY}")"
    if [[ "${current_hash}" != "${ACPX_CONFIG_PREFLIGHT_HASH}" || "${current_bytes}" != "${ACPX_CONFIG_PREFLIGHT_BYTES}" ]]; then
      echo "ERROR: External OpenClaw CLI changed ACPX preflight config during ${context}." >&2
      return 1
    fi
  fi
}

cleanup_repo_config_preflight_copy() {
  if [[ -n "${REPO_CONFIG_PREFLIGHT_DIR:-}" ]]; then
    guarded_rm_rf "${REPO_CONFIG_PREFLIGHT_DIR}" "cleaning guarded repo OpenClaw config preflight directory ${REPO_CONFIG_PREFLIGHT_DIR}" || return 1
  fi
  REPO_CONFIG_PREFLIGHT_COPY=""
  REPO_CONFIG_PREFLIGHT_DIR=""
  ACPX_CONFIG_PREFLIGHT_COPY=""
  ACPX_CONFIG_PREFLIGHT_IDENTITY=""
  ACPX_CONFIG_PREFLIGHT_HASH=""
  ACPX_CONFIG_PREFLIGHT_BYTES=""
  ACPX_CONFIG_SOURCE_IDENTITY=""
  ACPX_CONFIG_SOURCE_HASH=""
  ACPX_CONFIG_SOURCE_BYTES=""
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
  if ! diff -u <(printf '%s\n' "${RUNTIME_CAP_ENV_LINES[@]}") "${path}" >&2; then
    echo "ERROR: OpenClaw gateway runtime caps drop-in must match the repo-managed numerical runtime cap set exactly." >&2
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

retire_obsolete_codex_runtime_dropin() {
  if [[ ! -e "${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" \
    && ! -L "${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" ]]; then
    return 0
  fi
  if [[ ! -f "${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" || -L "${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" ]]; then
    echo "ERROR: Obsolete OpenClaw Codex runtime drop-in is not a regular file: ${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" >&2
    return 1
  fi
  snapshot_managed_artifact_path "${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" || return 1
  guarded_rm_f \
    "${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" \
    "retiring obsolete OpenClaw Codex runtime drop-in ${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}" \
    || return 1
  echo "Retired obsolete OpenClaw Codex runtime drop-in → ${OBSOLETE_CODEX_RUNTIME_DROPIN_DST}"
}

validate_research_owner_unit_file() {
  local path="$1" expected_root="${2:-}" expected_core_database="${3:-}" \
    expected_env_file="${4:-}" exec_start_ok=0 environment_file_ok=0 core_database_ok=0
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: Repo-managed research owner unit not found at ${path}" >&2
    return 1
  fi
  if grep -Fxq 'ExecStart=@REPO_ROOT@/.venv/bin/gateway-cli research serve --root "@RESEARCH_V2_ROOT@" --session-key agent:research-orchestrator:autoresearch:quantipy-v2 --poll-seconds 60' "${path}"; then
    exec_start_ok=1
  elif [[ -n "${expected_root}" ]] && grep -Fxq "ExecStart=${REPO_ROOT}/.venv/bin/gateway-cli research serve --root \"${expected_root}\" --session-key agent:research-orchestrator:autoresearch:quantipy-v2 --poll-seconds 60" "${path}"; then
    exec_start_ok=1
  fi
  if grep -Fxq "EnvironmentFile=@OWNER_ENV_FILE@" "${path}" \
    || { [[ -n "${expected_env_file}" ]] \
      && grep -Fxq "EnvironmentFile=${expected_env_file}" "${path}"; }; then
    environment_file_ok=1
  fi
  if grep -Fxq "Environment=RESEARCH_CORE_DATABASE=@RESEARCH_CORE_DATABASE@" "${path}" \
    || { [[ -n "${expected_core_database}" ]] \
      && grep -Fxq "Environment=RESEARCH_CORE_DATABASE=${expected_core_database}" "${path}"; }; then
    core_database_ok=1
  fi
  if ! grep -Fxq "Description=G2 research owner loop" "${path}" \
    || ! grep -Fxq "BindsTo=${GATEWAY_SERVICE_NAME}" "${path}" \
    || ! grep -Fxq "After=${GATEWAY_SERVICE_NAME}" "${path}" \
    || ! grep -Fxq "Type=simple" "${path}" \
    || [[ "${exec_start_ok}" -ne 1 ]] \
    || [[ "${environment_file_ok}" -ne 1 ]] \
    || [[ "${core_database_ok}" -ne 1 ]] \
    || ! grep -Fxq "Restart=on-failure" "${path}" \
    || ! grep -Fxq "RestartSec=30" "${path}" \
    || ! grep -Fxq "RestartPreventExitStatus=78" "${path}" \
    || ! grep -Fxq "KillMode=process" "${path}" \
    || ! grep -Fxq "UMask=0077" "${path}" \
    || ! grep -Fxq "WantedBy=default.target" "${path}" \
    || grep -Fxq "Requires=${GATEWAY_SERVICE_NAME}" "${path}" \
    || grep -Fxq "Restart=always" "${path}"; then
    echo "ERROR: Research owner unit has an invalid lifecycle or command contract." >&2
    return 1
  fi
}

validate_research_owner_persona() {
  local file
  if [[ ! -d "${RESEARCH_OWNER_PERSONA_SRC}" ]]; then
    echo "ERROR: research-orchestrator persona not yet delivered (P4)" >&2
    return 1
  fi
  for file in AGENTS.md SOUL.md TOOLS.md BOOTSTRAP.md; do
    if [[ ! -f "${RESEARCH_OWNER_PERSONA_SRC}/${file}" ]]; then
      echo "ERROR: research-orchestrator persona not yet delivered (P4): missing ${file}" >&2
      return 1
    fi
  done
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
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    require_paused_deployment_preconditions || return 1
    return 0
  fi
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

systemd_show_value() {
  local output="$1" property="$2"
  printf '%s\n' "${output}" | awk -F= -v property="${property}" '$1 == property { print substr($0, index($0, "=") + 1); exit }'
}

require_paused_mask_path() {
  local path="$1" label="$2" resolved
  if [[ ! -L "${path}" ]] || ! resolved="$(readlink -e -- "${path}" 2>/dev/null)" || [[ "${resolved}" != "/dev/null" ]]; then
    echo "ERROR: Paused mode requires ${label} to be a symlink resolving to /dev/null: ${path}" >&2
    return 1
  fi
}

require_paused_systemd_state() {
  local unit="$1" label="$2" expected_load_states="$3" expected_active_states="$4" expected_sub_states="$5" expected_unit_file_states="$6" require_main_pid="$7"
  local state load_state active_state sub_state unit_file_state main_pid state_details
  local -a show_properties=(
    --property=LoadState
    --property=ActiveState
    --property=SubState
    --property=UnitFileState
  )
  if [[ "${require_main_pid}" == "1" ]]; then
    show_properties+=(--property=MainPID)
  fi
  if ! state="$(systemctl --user show "${unit}" \
    "${show_properties[@]}" 2>&1)"; then
    echo "ERROR: Could not inspect ${label} in paused mode." >&2
    echo "       systemctl output: ${state}" >&2
    return 1
  fi
  load_state="$(systemd_show_value "${state}" LoadState)"
  active_state="$(systemd_show_value "${state}" ActiveState)"
  sub_state="$(systemd_show_value "${state}" SubState)"
  unit_file_state="$(systemd_show_value "${state}" UnitFileState)"
  main_pid=""
  if [[ "${require_main_pid}" == "1" ]]; then
    main_pid="$(systemd_show_value "${state}" MainPID)"
  fi
  state_details="LoadState=${load_state:-<missing>} ActiveState=${active_state:-<missing>} SubState=${sub_state:-<missing>} UnitFileState=${unit_file_state:-<missing>}"
  if [[ "${require_main_pid}" == "1" ]]; then
    state_details+=" MainPID=${main_pid:-<missing>}"
  fi
  if [[ " ${expected_load_states} " != *" ${load_state} "* \
    || " ${expected_active_states} " != *" ${active_state} "* \
    || ( -n "${expected_sub_states}" && " ${expected_sub_states} " != *" ${sub_state} "* ) \
    || ( "${require_main_pid}" == "1" && "${main_pid}" != "0" ) ]]; then
    echo "ERROR: Paused mode requires ${label} to be quiescent." >&2
    echo "       ${state_details}" >&2
    return 1
  fi
  if [[ -n "${expected_unit_file_states}" \
    && " ${expected_unit_file_states} " != *" ${unit_file_state} "* ]]; then
    if [[ "${load_state}" != "not-found" \
      || -n "${unit_file_state}" \
      || " ${expected_unit_file_states} " != *" not-found "* ]]; then
      echo "ERROR: Paused mode requires ${label} to be quiescent." >&2
      echo "       ${state_details}" >&2
      return 1
    fi
  fi
}

require_paused_deployment_preconditions() {
  local runtime_gateway_unit
  if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    echo "ERROR: Paused mode requires XDG_RUNTIME_DIR to inspect the runtime gateway mask." >&2
    return 1
  fi
  runtime_gateway_unit="${XDG_RUNTIME_DIR}/systemd/user/${GATEWAY_SERVICE_NAME}"
  require_paused_systemd_state \
    "${GATEWAY_SERVICE_NAME}" "${GATEWAY_SERVICE_NAME}" \
    "masked" "inactive" "dead" "" 1 || return 1
  require_paused_mask_path "${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}" "persistent gateway mask" || return 1
  require_paused_mask_path "${runtime_gateway_unit}" "runtime gateway mask" || return 1
  require_paused_systemd_state \
    "${HEALTHCHECK_TIMER_NAME}" "${HEALTHCHECK_TIMER_NAME}" \
    "loaded not-found" "inactive" "dead inactive" "" 0 || return 1
  require_paused_systemd_state \
    "${HEALTHCHECK_SERVICE_NAME}" "${HEALTHCHECK_SERVICE_NAME}" \
    "loaded not-found" "inactive failed" "dead failed inactive" "" 1 || return 1
  require_paused_systemd_state \
    "${RESEARCH_OWNER_SERVICE_NAME}" "${RESEARCH_OWNER_SERVICE_NAME}" \
    "loaded not-found" "inactive" "dead inactive" "disabled masked not-found masked-runtime" 1 || return 1
  echo "Verified paused deployment preconditions: masked gateway, stopped healthcheck, and quiescent research owner."
}

validate_migration_record() {
  local path="$1" mode
  guard_destination_path_chain "${path}" "validating generated migration record ${path}" || return 1
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    echo "ERROR: Generated migration record is not a regular non-symlink file: ${path}" >&2
    return 1
  fi
  if ! mode="$(stat -c '%a' -- "${path}")" || [[ "${mode}" != "600" ]]; then
    echo "ERROR: Generated migration record must have mode 0600: ${path}" >&2
    return 1
  fi
  if ! jq -e 'type == "object" and (.removed | type == "array")' "${path}" >/dev/null; then
    echo "ERROR: Generated migration record must contain a .removed array: ${path}" >&2
    return 1
  fi
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

snapshot_stale_systemd_environment_paths() {
  local service_path="${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}"
  local path
  local nullglob_was_set=0
  local dotglob_was_set=0
  local -a candidates=()
  if [[ -e "${service_path}" || -L "${service_path}" ]]; then
    if [[ ! -f "${service_path}" || -L "${service_path}" ]]; then
      echo "ERROR: Managed systemd service path is not a regular file: ${service_path}" >&2
      return 1
    fi
    candidates+=("${service_path}")
  fi
  if [[ -e "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" || -L "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    if [[ ! -d "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" || -L "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
      echo "ERROR: Managed systemd drop-in path is not a real directory: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
      return 1
    fi
    guard_destination_path_chain "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "scanning managed systemd drop-in paths for rollback snapshots" || return 1
    if shopt -q nullglob; then
      nullglob_was_set=1
    fi
    if shopt -q dotglob; then
      dotglob_was_set=1
    fi
    shopt -s nullglob
    shopt -s dotglob
    for path in "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"/*.conf; do
      if [[ -d "${path}" && ! -L "${path}" ]]; then
        continue
      fi
      if [[ ! -f "${path}" && ! -L "${path}" ]]; then
        echo "ERROR: Managed systemd drop-in path is not a regular file: ${path}" >&2
        [[ "${nullglob_was_set}" -eq 1 ]] || shopt -u nullglob
        [[ "${dotglob_was_set}" -eq 1 ]] || shopt -u dotglob
        return 1
      fi
      if [[ "${path}" == "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}" \
        || "${path}" == "${NATIVE_CRASH_HARDENING_DROPIN_DST}" ]]; then
        continue
      fi
      candidates+=("${path}")
    done
    [[ "${nullglob_was_set}" -eq 1 ]] || shopt -u nullglob
    [[ "${dotglob_was_set}" -eq 1 ]] || shopt -u dotglob
  fi
  for path in "${candidates[@]}"; do
    if [[ -L "${path}" ]]; then
      echo "ERROR: Managed systemd file ${path} is a symlink to $(readlink -- "${path}" 2>/dev/null || printf '<unreadable>');" >&2
      echo "       Refusing before mutating managed systemd files." >&2
      return 1
    fi
    guard_destination_path_chain "${path}" "rewriting managed systemd environment file ${path}" || return 1
    snapshot_managed_artifact_path "${path}" || return 1
  done
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

require_acpx_plugin_exact() {
  local inventory plugin_path resolved
  if ! prepare_acpx_config_preflight_copy; then
    return 1
  fi
  if ! inventory="$(run_openclaw_cli_for_config "${ACPX_CONFIG_PREFLIGHT_COPY}" plugins inspect acpx --runtime --json)"; then
    echo "ERROR: Unable to inspect the installed ACPX plugin; refusing network or install fallback." >&2
    printf '%s\n' "${inventory}" >&2
    return 1
  fi
  if ! verify_acpx_config_preflight_unchanged "ACPX plugin inspection"; then
    return 1
  fi
  if plugin_path="$(printf '%s\n' "${inventory}" | jq -se -r --arg version "2026.8.1" '
    if length != 1 then
      empty
    else
      [
        .[0].plugin
        | select(
            .id == "acpx"
            and .packageName == "@openclaw/acpx"
            and .packageVersion == $version
            and .version == $version
            and .enabled == true
            and .status == "loaded"
            and (.rootDir | strings | startswith("/"))
          )
        | .rootDir
      ]
      | first // empty
    end
  ' 2>/dev/null)"; then
    :
  else
    plugin_path=""
  fi
  if [[ -z "${plugin_path}" ]]; then
    echo "ERROR: Installed @openclaw/acpx version 2026.8.1 was not found; refusing network or install fallback." >&2
    return 1
  fi
  if ! resolved="$(env -u NODE_OPTIONS node --input-type=module - "${plugin_path}" <<'NODE'
import fs from "node:fs";
import path from "node:path";

const reported = process.argv[2];
if (!path.isAbsolute(reported)) throw new Error("reported ACPX rootDir must be absolute");
const packageRoot = path.resolve(reported);
const packageJsonPath = path.join(packageRoot, "package.json");
const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
if (packageJson.name !== "@openclaw/acpx" || packageJson.version !== "2026.8.1") {
  throw new Error("reported ACPX path does not resolve to @openclaw/acpx 2026.8.1");
}
const adapterDependency = packageJson.dependencies?.["@agentclientprotocol/claude-agent-acp"];
if (adapterDependency !== "0.70.0") {
  throw new Error(`ACPX Claude adapter dependency must be 0.70.0, got ${adapterDependency || "<missing>"}`);
}
function stageRootFor(root) {
  let nodeModulesRoot = root;
  while (path.basename(nodeModulesRoot) !== "node_modules") {
    const parent = path.dirname(nodeModulesRoot);
    if (parent === nodeModulesRoot) throw new Error("reported ACPX rootDir is not inside node_modules");
    nodeModulesRoot = parent;
  }
  return path.dirname(nodeModulesRoot);
}
const frozenRoot = stageRootFor(packageRoot);
function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return !path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`);
}
function packageRootFor(packageName, ownerRoot) {
  const segments = packageName.startsWith("@") ? packageName.split("/") : [packageName];
  let searchRoot = path.resolve(ownerRoot);
  if (!isWithin(frozenRoot, searchRoot)) {
    throw new Error(`${packageName} owner is outside the frozen dependency tree`);
  }
  while (true) {
    const candidate = path.join(searchRoot, "node_modules", ...segments);
    if (fs.existsSync(path.join(candidate, "package.json"))) return candidate;
    if (searchRoot === frozenRoot) break;
    const parent = path.dirname(searchRoot);
    if (!isWithin(frozenRoot, parent)) break;
    searchRoot = parent;
  }
  throw new Error(`${packageName} is not installed in the declared dependency tree`);
}
function packageJsonAt(packageName, ownerRoot) {
  const root = packageRootFor(packageName, ownerRoot);
  return { root, path: path.join(root, "package.json") };
}
const adapter = packageJsonAt("@agentclientprotocol/claude-agent-acp", packageRoot);
const adapterRoot = adapter.root;
const adapterPackageJsonPath = adapter.path;
const adapterPackageJson = JSON.parse(fs.readFileSync(adapterPackageJsonPath, "utf8"));
if (adapterPackageJson.name !== "@agentclientprotocol/claude-agent-acp" || adapterPackageJson.version !== "0.70.0") {
  throw new Error(`Claude ACP adapter must be @agentclientprotocol/claude-agent-acp 0.70.0, got ${adapterPackageJson.version || "<missing>"}`);
}
function resolveAdapterOwned(packageName) {
  const declared = adapterPackageJson.dependencies?.[packageName];
  if (typeof declared !== "string") {
    throw new Error(`${packageName} must be declared by the Claude ACP adapter`);
  }
  const expectedVersions = {
    "@agentclientprotocol/sdk": "1.3.0",
    "@anthropic-ai/claude-agent-sdk": "0.3.232",
  };
  const expected = expectedVersions[packageName];
  if (declared !== expected) {
    const label = packageName === "@agentclientprotocol/sdk" ? "ACP SDK" : "Claude Agent SDK";
    throw new Error(`Adapter ${label} dependency declaration does not match expected ${expected}, got ${declared}`);
  }
  return packageJsonAt(packageName, adapterRoot).path;
}
const pluginAcpxDeclarations = [
  packageJson.dependencies?.acpx,
  packageJson.optionalDependencies?.acpx,
  packageJson.peerDependencies?.acpx,
].filter((value) => typeof value === "string");
for (const declared of pluginAcpxDeclarations) {
  if (declared !== "0.13.1") {
    throw new Error(`ACPX plugin dependency acpx must be 0.13.1, got ${declared || "<missing>"}`);
  }
}
if (pluginAcpxDeclarations.length > 0) {
  const pluginAcpxPackageJsonPath = packageJsonAt("acpx", packageRoot).path;
  const pluginAcpxPackageJson = JSON.parse(fs.readFileSync(pluginAcpxPackageJsonPath, "utf8"));
  if (pluginAcpxPackageJson.name !== "acpx" || pluginAcpxPackageJson.version !== "0.13.1") {
    throw new Error(`ACPX plugin package must be acpx 0.13.1, got ${pluginAcpxPackageJson.version || "<missing>"}`);
  }
}
const sdkPackageJsonPath = resolveAdapterOwned("@agentclientprotocol/sdk");
const sdkPackageJson = JSON.parse(fs.readFileSync(sdkPackageJsonPath, "utf8"));
if (sdkPackageJson.name !== "@agentclientprotocol/sdk" || sdkPackageJson.version !== "1.3.0") {
  throw new Error(`Agent Client Protocol SDK must be 1.3.0, got ${sdkPackageJson.version || "<missing>"}`);
}
if (adapterPackageJson.dependencies?.["@agentclientprotocol/sdk"] !== sdkPackageJson.version) {
  throw new Error("Adapter ACP SDK dependency declaration does not match the resolved package");
}
const claudeSdkPackageJsonPath = resolveAdapterOwned("@anthropic-ai/claude-agent-sdk");
const claudeSdkPackageJson = JSON.parse(fs.readFileSync(claudeSdkPackageJsonPath, "utf8"));
if (claudeSdkPackageJson.name !== "@anthropic-ai/claude-agent-sdk" || claudeSdkPackageJson.version !== "0.3.232") {
  throw new Error(`Claude Agent SDK must be 0.3.232, got ${claudeSdkPackageJson.version || "<missing>"}`);
}
if (adapterPackageJson.dependencies?.["@anthropic-ai/claude-agent-sdk"] !== claudeSdkPackageJson.version) {
  throw new Error("Adapter Claude Agent SDK dependency declaration does not match the resolved package");
}
const bin = typeof adapterPackageJson.bin === "string"
  ? adapterPackageJson.bin
  : adapterPackageJson.bin?.["claude-agent-acp"];
if (typeof bin !== "string" || bin.length === 0) throw new Error("Claude ACP adapter has no claude-agent-acp bin entry");
const adapterBin = path.resolve(adapterRoot, bin);
if (!fs.existsSync(adapterBin) || !fs.statSync(adapterBin).isFile() || (fs.statSync(adapterBin).mode & 0o111) === 0) {
  throw new Error(`Claude ACP adapter binary is missing or not executable: ${adapterBin}`);
}
process.stdout.write(`${adapterBin}\n`);
NODE
)"; then
    echo "ERROR: Could not resolve the installed Claude ACP adapter from @openclaw/acpx; refusing network or install fallback." >&2
    return 1
  fi
  ACPX_ADAPTER_BIN="${resolved}"
  if [[ ! -x "${RESEARCH_REVIEWER_LAUNCHER}" ]]; then
    echo "ERROR: Research reviewer launcher is missing or not executable: ${RESEARCH_REVIEWER_LAUNCHER}" >&2
    return 1
  fi
  echo "ACPX plugin validated: @openclaw/acpx 2026.8.1; Claude adapter ${ACPX_ADAPTER_BIN}"
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
  "${RESEARCH_OWNER_UNIT_DST}"
  "${QUANTIPY_API_UNIT_DST}"
  "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}"
  "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
)
MANAGED_ARTIFACT_TRANSACTION_ARMED=0
MANAGED_ARTIFACT_BACKUP_DIR=""
MANAGED_ARTIFACT_RESTORED_SYSTEMD=0
RUNTIME_CAPS_DROPIN_DIR_EXISTED=0
RUNTIME_CAPS_DROPIN_DIR_MODE=""
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

capture_runtime_caps_dropin_dir_state() {
  if [[ ! -e "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" && ! -L "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    RUNTIME_CAPS_DROPIN_DIR_EXISTED=0
    RUNTIME_CAPS_DROPIN_DIR_MODE=""
    return 0
  fi
  if [[ ! -d "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" || -L "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    echo "ERROR: Managed systemd drop-in path exists but is not a real directory: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
    return 1
  fi
  guard_destination_path_chain "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "capturing managed systemd drop-in directory state ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" || return 1
  if ! RUNTIME_CAPS_DROPIN_DIR_MODE="$(stat -c '%a' -- "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}")"; then
    echo "ERROR: Could not capture managed systemd drop-in directory mode: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
    return 1
  fi
  RUNTIME_CAPS_DROPIN_DIR_EXISTED=1
}

rollback_runtime_caps_dropin_dir_state() {
  if [[ "${RUNTIME_CAPS_DROPIN_DIR_EXISTED:-0}" -eq 1 ]]; then
    if ! guarded_chmod "${RUNTIME_CAPS_DROPIN_DIR_MODE}" "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "restoring managed systemd drop-in directory mode ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"; then
      echo "ERROR: Failed to restore managed systemd drop-in directory mode ${RUNTIME_CAPS_DROPIN_DIR_MODE} for ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}." >&2
      ROLLBACK_FAILED=1
      return 1
    fi
    return 0
  fi
  if [[ -e "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" || -L "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" ]]; then
    if ! guarded_rmdir "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" "removing newly created empty managed systemd drop-in directory ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}"; then
      echo "ERROR: Failed to remove newly created managed systemd drop-in directory; it was not empty or could not be guarded: ${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}" >&2
      ROLLBACK_FAILED=1
      return 1
    fi
  fi
  return 0
}

begin_managed_unit_transaction() {
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    # Paused publication keeps the existing gateway masks untouched.  The
    # artifact transaction still snapshots every file this script may write,
    # while avoiding the unit transaction's unconditional daemon-reload during
    # rollback.
    local path
    for path in \
      "${RESEARCH_OWNER_UNIT_DST}" \
      "${QUANTIPY_API_UNIT_DST}" \
      "${GATEWAY_RUNTIME_CAPS_DROPIN_DST}" \
      "${NATIVE_CRASH_HARDENING_DROPIN_DST}"; do
      snapshot_managed_artifact_path "${path}" || return 1
    done
    return 0
  fi
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
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" && "${MANAGED_UNIT_TRANSACTION_ARMED:-0}" -ne 1 ]]; then
    return 0
  fi
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
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    return 0
  fi
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
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    SYSTEMD_MANAGER_NODE_OPTIONS_CHANGED=0
    SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL_PRESENT=0
    SYSTEMD_MANAGER_NODE_OPTIONS_ORIGINAL=""
    return 0
  fi
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
  if ! cleanup_deployment_temp_file "${RESEARCH_OWNER_UNIT_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! cleanup_deployment_temp_file "${QUANTIPY_API_UNIT_TMP:-}"; then
    rollback_step_failed=1
  fi
  if ! cleanup_deployment_temp_file "${GATEWAY_RUNTIME_CAPS_DROPIN_TMP:-}"; then
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
  if ! rollback_runtime_caps_dropin_dir_state; then
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

if [[ ! -f "${REPO_CONFIG}" ]]; then
  echo "ERROR: Repo config not found at ${REPO_CONFIG}" >&2
  exit 1
fi

if [[ ! -f "${LOCAL_CONFIG}" ]]; then
  echo "ERROR: Local OpenClaw config not found at ${LOCAL_CONFIG}" >&2
  echo "       Run 'openclaw onboard' first." >&2
  exit 1
fi

if ! require_acpx_plugin_exact; then
  exit 1
fi

MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON="$(build_string_array_json "${MEMPALACE_READONLY_SERVER_AGENT_IDS[@]}")"
G2_CONTROL_SERVER_AGENT_IDS_JSON="$(build_string_array_json "${G2_CONTROL_SERVER_AGENT_IDS[@]}")"
RESEARCH_ORCHESTRATOR_DENY_IDS_JSON="$(build_string_array_json "${RESEARCH_ORCHESTRATOR_DENY_TOOL_IDS[@]}")"
MAIN_OPENCLAW_TOOL_ALLOW_IDS_JSON="$(build_string_array_json "${MAIN_OPENCLAW_TOOL_ALLOW_IDS[@]}")"

# The route overlay only requires the repository contract and backend skill.
REQUIRED_QUANTIPY_FILES=(
  "AGENTS.md"
  ".agents/skills/backend-python/SKILL.md"
)
for FILE in "${REQUIRED_QUANTIPY_FILES[@]}"; do
  if [[ ! -f "${QUANTIPY_ROOT}/${FILE}" ]]; then
    echo "ERROR: Required Quantipy route file not found at ${QUANTIPY_ROOT}/${FILE}" >&2
    exit 1
  fi
done
echo "Verified required route source files in ${QUANTIPY_ROOT}"

if [[ ! -d "${SKILLS_SRC}" ]]; then
  echo "ERROR: Repo-managed skills directory not found at ${SKILLS_SRC}" >&2
  exit 1
fi

if ! validate_research_owner_persona; then
  exit 1
fi

if [[ ! -f "${MEMPALACE_READONLY_WRAPPER_SRC}" ]]; then
  echo "ERROR: Repo-managed MemPalace read-only wrapper not found at ${MEMPALACE_READONLY_WRAPPER_SRC}" >&2
  exit 1
fi

if [[ ! -f "${RESEARCH_OWNER_UNIT_TEMPLATE}" ]]; then
  echo "ERROR: Repo-managed research owner unit template not found at ${RESEARCH_OWNER_UNIT_TEMPLATE}" >&2
  exit 1
fi
if ! validate_research_owner_unit_file "${RESEARCH_OWNER_UNIT_TEMPLATE}"; then
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
if ! validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_SRC}"; then
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Repo Python is missing or not executable at ${PYTHON_BIN}. Run 'uv sync' first." >&2
  exit 1
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "ERROR: The systemd user manager is unavailable; cannot install ${RESEARCH_OWNER_SERVICE_NAME}." >&2
  exit 1
fi

if ! require_gateway_service_loadable; then
  exit 1
fi

if ! jq -e '
  [(.agents.defaults.skills // [])[], ((.agents.entries // {}) | .[]?.skills // [])[]]
  | all(.[]; type == "string" and length > 0)
' "${REPO_CONFIG}" >/dev/null; then
  echo "ERROR: Every configured skill in ${REPO_CONFIG} must be a non-empty string." >&2
  exit 1
fi

REQUIRED_REPO_SKILLS=(
  "codex-subagents"
  "mempalace-readonly"
  "research-loop"
)

mapfile -t CONFIGURED_SKILLS < <(jq -r '
  [(.agents.defaults.skills // [])[], ((.agents.entries // {}) | .[]?.skills // [])[]]
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

# ── Load selected env data from .env ─────────────────────────────────────────
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
  RESEARCH_V2_ROOT
  HYPOTHESIS_WORKTREES_ROOT
)
declare -A PRESERVED_ENV=()
for VAR_NAME in "${PRESERVE_ENV_VARS[@]}"; do
  if [[ -v "${VAR_NAME}" ]]; then
    PRESERVED_ENV["${VAR_NAME}"]="${!VAR_NAME}"
  fi
done

# The explicit core database contract comes only from the approved file; a
# caller export must not silently fill a missing file key.
unset RESEARCH_CORE_DATABASE
if ! validate_owner_env_file "${APPROVED_OWNER_ENV_FILE}"; then
  exit 1
fi

if [[ -f "${APPROVED_OWNER_ENV_FILE}" ]]; then
  if ! load_owner_env_data_only "${APPROVED_OWNER_ENV_FILE}"; then
    exit 1
  fi
fi

if [[ "${OPENCLAW_PUSH_MODE_INVOCATION_SET}" -eq 1 ]]; then
  if [[ ! -v OPENCLAW_PUSH_MODE \
    || "${OPENCLAW_PUSH_MODE}" != "${OPENCLAW_PUSH_MODE_INVOCATION_VALUE}" ]]; then
    echo "ERROR: OPENCLAW_PUSH_MODE must be selected in the invoking environment, not OPENCLAW_PUSH_ENV_FILE." >&2
    exit 1
  fi
else
  if [[ ! -v OPENCLAW_PUSH_MODE || "${OPENCLAW_PUSH_MODE}" != "normal" ]]; then
    echo "ERROR: OPENCLAW_PUSH_MODE must be selected in the invoking environment, not OPENCLAW_PUSH_ENV_FILE." >&2
    exit 1
  fi
fi
OPENCLAW_PUSH_MODE="${OPENCLAW_PUSH_MODE_REQUESTED}"

for VAR_NAME in "${!PRESERVED_ENV[@]}"; do
  printf -v "${VAR_NAME}" '%s' "${PRESERVED_ENV[${VAR_NAME}]}"
  export "${VAR_NAME}"
done
unset OPENCLAW_HOME
# Resolve the bounded route roots after the approved env file has loaded. An
# explicitly exported value still wins via PRESERVED_ENV above; the owner MCP
# receives only this fixed path and loads a strict allowlist at runtime.
RESEARCH_V2_ROOT="${RESEARCH_V2_ROOT:-${OPENCLAW_PUSH_HOME}/research-v2}"
HYPOTHESIS_WORKTREES_ROOT="${HYPOTHESIS_WORKTREES_ROOT:-${RESEARCH_V2_ROOT}/hypothesis-worktrees}"
OPENCLAW_HOST="${OPENCLAW_HOST:-127.0.0.1}"
OPENCLAW_PORT="${OPENCLAW_PORT:-18789}"
G2_OWNER_ENV_FILE="${APPROVED_OWNER_ENV_FILE}"
export RESEARCH_V2_ROOT HYPOTHESIS_WORKTREES_ROOT
export OPENCLAW_HOST OPENCLAW_PORT G2_OWNER_ENV_FILE

if ! validate_bounded_research_roots; then
  exit 1
fi
if ! validate_research_core_database; then
  exit 1
fi

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
capture_runtime_caps_dropin_dir_state || exit 1
guard_destination_path_chain "${MIGRATION_RECORD_DST}" "preparing managed OpenClaw migration record ${MIGRATION_RECORD_DST}" || exit 1
snapshot_managed_artifact_path "${MIGRATION_RECORD_DST}" || exit 1

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

  if ! RESEARCH_ORCHESTRATOR_MODEL_PRIMARY="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.config_merge orchestrator-model \
    -- "${REPO_CONFIG}")"; then
    return 1
  fi
  if [[ -z "${RESEARCH_ORCHESTRATOR_MODEL_PRIMARY}" ]]; then
    echo "ERROR: Repo config must pin agents.entries.research-orchestrator to a model.primary." >&2
    return 1
  fi
  if [[ "${RESEARCH_ORCHESTRATOR_MODEL_PRIMARY}" != openai/* ]]; then
    echo "ERROR: Research orchestrator model '${RESEARCH_ORCHESTRATOR_MODEL_PRIMARY}' must use the OpenAI/Codex provider." >&2
    return 1
  fi

  if ! MERGED="$(PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.config_merge assemble \
    --migration-record "${MIGRATION_RECORD_DST}" \
    -- "${LOCAL_CONFIG}" "${REPO_CONFIG}" "${REPO_ROOT}" "${PYTHON_BIN}" \
    "${MEMPALACE_PYTHON}" "${MEMPALACE_PALACE}" "${MEMPALACE_READONLY_WRAPPER_DST}" \
    "${FASTEMBED_CACHE_PATH}" "${MEMPALACE_EMBEDDING_MODEL}" "${HF_HUB_OFFLINE}" \
    "${G2_CONTROL_MCP_MODULE}" "${RESEARCH_V2_ROOT}" \
    "${MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON}" \
    "${G2_CONTROL_SERVER_AGENT_IDS_JSON}" "${RESEARCH_REVIEWER_LAUNCHER}" \
    "${ACPX_ADAPTER_BIN}")"; then
    return 1
  fi
  validate_migration_record "${MIGRATION_RECORD_DST}" || return 1
  echo "Resolved read-only MemPalace MCP wrapper: ${MEMPALACE_READONLY_WRAPPER_DST}"
  echo "Resolved MemPalace embedding: ${MEMPALACE_EMBEDDING_MODEL} (cache: ${FASTEMBED_CACHE_PATH})"
  if [[ "${PROVIDER}" == "openrouter" ]] && [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    echo "Resolved env:OPENROUTER_API_KEY (${#OPENROUTER_API_KEY} chars)."
  fi
  echo "Sanitized stale coding-provider config keys: github-copilot copilot-proxy copilot-cli"
  echo "Active provider: ${PROVIDER} → default model: ${MODEL_PRIMARY}; research owner: ${RESEARCH_ORCHESTRATOR_MODEL_PRIMARY}"
}

assemble_openclaw_config

PAUSED_PRE_FIELDS_PROVIDERS=""
PAUSED_PRE_FIELDS_CODEX_PLUGIN=""

apply_paused_config_gates() {
  PAUSED_PRE_FIELDS_PROVIDERS="$(printf '%s\n' "${MERGED}" | jq -c '.models.providers')" || return 1
  PAUSED_PRE_FIELDS_CODEX_PLUGIN="$(printf '%s\n' "${MERGED}" | jq -c '.plugins.entries.codex')" || return 1
  if ! MERGED="$(printf '%s\n' "${MERGED}" | jq '
    .cron = ((.cron // {}) | .enabled = false)
    | .agents.defaults = ((.agents.defaults // {})
        | .heartbeat = ((.heartbeat // {}) | .every = "0m"))
    | .agents.entries = ((.agents.entries // {})
        | with_entries(if (.value | has("heartbeat")) then .value.heartbeat.every = "0m" else . end))
  ')"; then
    echo "ERROR: Could not apply paused cron and heartbeat gates to the assembled OpenClaw config." >&2
    return 1
  fi
  if ! printf '%s\n' "${MERGED}" | jq -e \
    --argjson providers "${PAUSED_PRE_FIELDS_PROVIDERS}" \
    --argjson codex_plugin "${PAUSED_PRE_FIELDS_CODEX_PLUGIN}" '
      (.models.providers == $providers)
      and (.plugins.entries.codex == $codex_plugin)
      and (.cron.enabled == false)
      and (.agents.defaults.heartbeat.every == "0m")
      and all((.agents.entries // {}) | .[]?;
        (.heartbeat? == null)
        or ((.heartbeat | type) == "object" and .heartbeat.every == "0m")
      )
    ' >/dev/null; then
    echo "ERROR: Paused config gates changed provider/plugin policy or are incomplete." >&2
    return 1
  fi
  echo "Applied paused config gates: cron disabled and all heartbeat cadences set to 0m."
}

if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
  apply_paused_config_gates || exit 1
fi

# No autoresearch model writes MemPalace. Durable research records are the
# research store and artifact receipts, so stage tool-deny compatibility lists
# must not survive in the managed config.
if ! echo "${MERGED}" | jq -e \
  --argjson owner_denies "${RESEARCH_ORCHESTRATOR_DENY_IDS_JSON}" '
  def denies: (.tools.deny // []);
  ([.agents.entries["research-orchestrator"] | select(type == "object")
    | select(denies == $owner_denies)
    | select((.tools.allow // []) == ["sessions_spawn"])
  ] | length) == 1
' >/dev/null; then
  echo "ERROR: Research orchestrator tool policy is not the exact bounded route." >&2
  exit 1
fi

# ── Managed invariant validation ─────────────────────────────────────────────
# Fail before writing if a local merge or env selection would violate the
# repo-managed autoresearch target shape.
if ! echo "${MERGED}" | jq -e \
  --arg owner "${RESEARCH_ORCHESTRATOR_MODEL_PRIMARY}" \
  --arg cmd "${MEMPALACE_PYTHON}" \
  --arg palace "${MEMPALACE_PALACE}" \
  --arg wrapper "${MEMPALACE_READONLY_WRAPPER_DST}" \
  --arg cache "${FASTEMBED_CACHE_PATH}" \
  --arg model "${MEMPALACE_EMBEDDING_MODEL}" \
  --arg offline "${HF_HUB_OFFLINE}" \
  --arg repo "${REPO_ROOT}" \
  --arg python "${PYTHON_BIN}" \
  --arg g2_module "${G2_CONTROL_MCP_MODULE}" \
  --arg research_root "${RESEARCH_V2_ROOT}" \
  --arg research_core_database "${RESEARCH_CORE_DATABASE}" \
  --arg owner_env_file "${G2_OWNER_ENV_FILE}" \
  --arg openclaw_host "${OPENCLAW_HOST}" \
  --arg openclaw_port "${OPENCLAW_PORT}" \
  --argjson readonly_server_agents "${MEMPALACE_READONLY_SERVER_AGENT_IDS_JSON}" \
  --argjson g2_server_agents "${G2_CONTROL_SERVER_AGENT_IDS_JSON}" \
  --argjson owner_denies "${RESEARCH_ORCHESTRATOR_DENY_IDS_JSON}" \
  --arg launcher "${RESEARCH_REVIEWER_LAUNCHER}" \
  --arg adapter "${ACPX_ADAPTER_BIN}" \
  --argjson main_openclaw_allow "${MAIN_OPENCLAW_TOOL_ALLOW_IDS_JSON}" '
  def denies: (.tools.deny // []);
  def main_allow: (.tools.allow // []);
  def expected_models: {"main": "openai/gpt-5.4", "research-orchestrator": $owner};
  (.agents.defaults.thinkingDefault == "high")
  and (.agents.ownership == "explicit")
  and ((.agents | has("list")) | not)
  and ((.agents.entries | type) == "object")
  and all(.agents.entries | to_entries[]; (.value | type) == "object" and ((.value | has("id")) | not))
  and ((.plugins.allow // []) | contains(["codex", "acpx"]))
  and (.plugins.load.paths? == null)
  and (.plugins.entries.codex.enabled == true)
  and (.plugins.entries.codex.config.nativeToolSurfaceEnabled? == null)
  and (.plugins.entries.codex.config.codexDynamicToolsExclude? == null)
  and (.plugins.entries.codex.config.appServer.sandbox == "workspace-write")
  and (.plugins.entries.codex.config.appServer.sandbox != "danger-full-access")
  and (.plugins.entries.codex.config.appServer.defaultWorkspaceDir? != "/home/dev/.openclaw/autoresearch/model-workspaces")
  and (.plugins.entries.codex.config.appServer.networkProxy? == null)
  and (.plugins.entries.acpx.enabled == true)
  and .plugins.entries.acpx.config == {
    "agents":{"claude":{"command":"/usr/bin/env","args":[("CLAUDE_CODE_EXECUTABLE=" + $launcher),$adapter]}},
    "permissionMode":"approve-reads",
    "nonInteractivePermissions":"fail",
    "pluginToolsMcpBridge":false,
    "openClawToolsMcpBridge":false,
    "mcpServers":{}
  }
  and .acp == {"enabled":true,"dispatch":{"enabled":true},"backend":"acpx","allowedAgents":["claude"]}
  and (.agents.defaults.maxConcurrent == 2)
  and (.agents.defaults.subagents.maxConcurrent == 1)
  and (.agents.defaults.subagents.maxSpawnDepth == 1)
  and (.agents.defaults.subagents.runTimeoutSeconds == 1800)
  and (.memory.search.enabled == false)
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
  and (.mcp.servers."g2-control".env == {
    "PYTHONPATH": $repo,
    "RESEARCH_V2_ROOT": $research_root,
    "RESEARCH_CORE_DATABASE": $research_core_database,
    "OPENCLAW_HOST": $openclaw_host,
    "OPENCLAW_PORT": $openclaw_port,
    "G2_OWNER_ENV_FILE": $owner_env_file
  })
  and (((.agents.entries // {}) | keys | sort) == (expected_models | keys | sort))
  and all((.agents.entries // {}) | to_entries[]; .value.model.primary == expected_models[.key])
  and all((.agents.entries // {}) | to_entries[]; .value.thinkingDefault == "high")
  and ([.agents.entries.main | select(.tools.profile == "minimal" and main_allow == $main_openclaw_allow and (denies | contains(["exec", "sessions_spawn", "sessions_yield", "sessions_send", "sessions_list", "sessions_history", "agents_list"])))] | length) == 1
  and ([.agents.entries["research-orchestrator"] | select(
    .model.primary == $owner
    and .thinkingDefault == "high"
    and ((.skills // []) == ["research-loop"])
    and denies == $owner_denies
    and ((.tools.allow // []) == ["sessions_spawn"])
    and ((.subagents.allowAgents? // []) == ["claude"])
  )] | length) == 1
  and ([.agents.entries.main | select(
    .model.primary == "openai/gpt-5.4"
    and .thinkingDefault == "high"
    and (((.skills // []) | index("mempalace")) == null)
    and (((.skills // []) | index("autoresearch")) == null)
    and ((.skills // []) == ["mempalace-readonly"])
    and (((.subagents.allowAgents? // []) | length) == 0)
  )] | length) == 1
  ' >/dev/null; then
  echo "ERROR: Generated OpenClaw config violates the managed route invariants." >&2
  echo "       Check ACPX wiring, research-orchestrator policy, main restrictions, model catalog, and bounded MCP projection." >&2
  exit 1
fi
echo "Managed invariants validated: main interface, Astra research owner, ACPX Claude route, native Luna roster, strict concurrency caps, and bounded MCP projection."

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
if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]] && ! jq -e \
  --argjson providers "${PAUSED_PRE_FIELDS_PROVIDERS}" \
  --argjson codex_plugin "${PAUSED_PRE_FIELDS_CODEX_PLUGIN}" '
    (.models.providers == $providers)
    and (.plugins.entries.codex == $codex_plugin)
    and (.cron.enabled == false)
    and (.agents.defaults.heartbeat.every == "0m")
    and all((.agents.entries // {}) | .[]?;
      (.heartbeat? == null)
      or ((.heartbeat | type) == "object" and .heartbeat.every == "0m")
    )
  ' "${LOCAL_CONFIG}" >/dev/null; then
  echo "ERROR: Published paused config failed provider/plugin preservation or pause-field assertions." >&2
  exit 1
fi
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

workspace_is_research_orchestrator() {
  local agents_csv="$1"
  [[ "${agents_csv}" == "research-orchestrator" ]]
}

validate_codex_native_stage_agent_sources() {
  local agents_dir="$1"
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_BIN}" -m gateway.deployment.codex_agents validate-stage-agent-sources -- "${agents_dir}"
}

validate_codex_native_research_runtime_roles() {
  local config_path="$1"
  local source_dir="$2"
  local layer_dir="$3"
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.codex_agents validate-native-research-runtime-roles \
    -- "${config_path}" "${source_dir}" "${layer_dir}" \
    "${RESEARCH_V2_ROOT}" "${HYPOTHESIS_WORKTREES_ROOT}"
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
  CODEX_RUNTIME_RESEARCH_V2_ROOT="${RESEARCH_V2_ROOT}" \
  CODEX_RUNTIME_HYPOTHESIS_WORKTREES_ROOT="${HYPOTHESIS_WORKTREES_ROOT}" \
  CODEX_RUNTIME_OWNER_ENV_FILE="${G2_OWNER_ENV_FILE}" \
  CODEX_RUNTIME_RESEARCH_CORE_DATABASE="${RESEARCH_CORE_DATABASE}" \
  CODEX_RUNTIME_OPENCLAW_HOST="${OPENCLAW_HOST}" \
  CODEX_RUNTIME_OPENCLAW_PORT="${OPENCLAW_PORT}" \
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
    "${PYTHON_BIN}" "${G2_CONTROL_MCP_MODULE}" "${REPO_ROOT}" \
    "${G2_OWNER_ENV_FILE}" "${RESEARCH_CORE_DATABASE}" "${OPENCLAW_HOST}" "${OPENCLAW_PORT}" \
    "${RESEARCH_V2_ROOT}" "${HYPOTHESIS_WORKTREES_ROOT}"
  validate_codex_doctor_owned_checks "${codex_home}" "${config_path}"
}

validate_codex_doctor_owned_checks() {
  local codex_home="$1"
  local config_path="$2"
  local doctor_stdout doctor_stderr doctor_status app_server_package_root

  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    echo "DEFERRED: paused mode skipped Codex doctor/update/websocket probes."
    return 0
  fi

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

remove_stale_native_research_agents() {
  local agents_dir="$1"
  [[ -d "${agents_dir}" ]] || return 0
  local stale_name stale_path
  for stale_name in "${RETIRED_CODEX_RUNTIME_AGENT_FILES[@]}"; do
    stale_path="${agents_dir}/${stale_name}"
    if [[ -f "${stale_path}" || -L "${stale_path}" ]]; then
      guarded_rm_f "${stale_path}" "removing duplicate native Codex research agent ${stale_path}"
      echo "Removed duplicate native Codex research agent ${stale_path}"
    fi
  done
}

remove_retired_codex_runtime_agents() {
  local agents_dir="$1"
  [[ -d "${agents_dir}" ]] || return 0
  local retired_name retired_path
  for retired_name in "${RETIRED_CODEX_RUNTIME_AGENT_FILES[@]}"; do
    retired_path="${agents_dir}/${retired_name}"
    if [[ -f "${retired_path}" || -L "${retired_path}" ]]; then
      guarded_rm_f "${retired_path}" "removing retired native Codex runtime research agent ${retired_path}"
      echo "Removed retired native Codex runtime research agent ${retired_path}"
    fi
  done
}

remove_stale_native_research_layers() {
  local layer_dir="$1"
  [[ -d "${layer_dir}" ]] || return 0
  local stale_name stale_path
  for stale_name in "${RETIRED_CODEX_RESEARCH_LAYER_FILES[@]}"; do
    stale_path="${layer_dir}/${stale_name}"
    if [[ -f "${stale_path}" || -L "${stale_path}" ]]; then
      guarded_rm_f "${stale_path}" "removing stale native Codex research layer ${stale_path}"
      echo "Removed stale native Codex research layer ${stale_path}"
    fi
  done
}

render_native_research_layer() {
  local agent_id="$1"
  local destination_dir="$2"
  local source="${CODEX_AGENT_CONFIGS_SRC}/${agent_id}.toml"
  local temporary
  temporary="$(mktemp "${destination_dir}/.${agent_id}.XXXXXX.toml")"
  guard_destination_path_chain "${temporary}" "writing rendered native Codex research layer ${temporary}"
  sed -e "s|@RESEARCH_V2_ROOT@|$(escape_sed_replacement "${RESEARCH_V2_ROOT}")|g" \
    -e "s|@HYPOTHESIS_WORKTREES_ROOT@|$(escape_sed_replacement "${HYPOTHESIS_WORKTREES_ROOT}")|g" \
    "${source}" > "${temporary}"
  guarded_chmod 0600 "${temporary}" "chmod rendered native Codex research layer ${temporary}"
  guarded_mv_replace "${temporary}" "${destination_dir}/${agent_id}.toml" \
    "publishing rendered native Codex research layer ${destination_dir}/${agent_id}.toml"
}

render_native_research_role_config() {
  local agents_dir="$1"
  local layer_dir="$2"
  local config_path="$3"
  PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.codex_agents write-native-research-role-config \
    -- "${agents_dir}" "${layer_dir}" "${config_path}"
  guard_destination_path_chain "${config_path}" "wrote rendered native Codex role config ${config_path}"
}

validate_codex_native_stage_agent_sources "${CODEX_AGENTS_SRC}"

mapfile -t BOOTSTRAP_TARGETS < <(jq -r '
  def workspace_target:
    if ((.workspace? | type) == "string" and (.workspace | length) > 0) then
      .workspace
    elif .id == "main" then
      "__OPENCLAW_DEFAULT_WORKSPACE__"
    else
      "workspace-\(.id)"
    end;
  [((.agents.entries // {}) | to_entries[] | .value + {id: .key})
    | {agent: .id, workspace: workspace_target}]
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
RESEARCH_OWNER_WORKSPACE_DST=""
echo "Copying managed bootstrap files to ${#BOOTSTRAP_TARGETS[@]} configured OpenClaw workspaces:"
for TARGET in "${BOOTSTRAP_TARGETS[@]}"; do
  IFS=$'\t' read -r WORKSPACE_ID AGENTS <<< "${TARGET}"
  BOOTSTRAP_DST="$(workspace_dir_for_target "${WORKSPACE_ID}")"
  snapshot_managed_artifact_path "${BOOTSTRAP_DST}"
  BOOTSTRAP_TARGET_DIRS["${BOOTSTRAP_DST}"]=1
  guarded_mkdir_p "${BOOTSTRAP_DST}" "creating managed OpenClaw workspace ${BOOTSTRAP_DST}"
  if workspace_is_research_orchestrator "${AGENTS}"; then
    BOOTSTRAP_SRC="${RESEARCH_OWNER_PERSONA_SRC}"
    BOOTSTRAP_FILES_FOR_TARGET=(AGENTS.md SOUL.md TOOLS.md BOOTSTRAP.md)
  elif [[ "${AGENTS}" == "main" ]]; then
    BOOTSTRAP_SRC="${REPO_ROOT}/gateway/agent_config"
    BOOTSTRAP_FILES_FOR_TARGET=(AGENTS.md SOUL.md TOOLS.md BOOTSTRAP.md)
  else
    echo "ERROR: Unsupported OpenClaw workspace agent roster: ${AGENTS}" >&2
    exit 1
  fi
  for FILE in "${BOOTSTRAP_FILES_FOR_TARGET[@]}"; do
    guarded_cp_file "${BOOTSTRAP_SRC}/${FILE}" "${BOOTSTRAP_DST}/${FILE}" "copying managed bootstrap file ${BOOTSTRAP_DST}/${FILE}"
  done
  if workspace_is_research_orchestrator "${AGENTS}"; then
    CODEX_AGENTS_DST="${BOOTSTRAP_DST}/.codex/agents"
    if [[ -d "${CODEX_AGENTS_DST}" ]]; then
      remove_stale_native_research_agents "${CODEX_AGENTS_DST}"
    fi
    CODEX_AGENT_CONFIGS_DST="${BOOTSTRAP_DST}/.codex/agent-configs"
    guarded_mkdir_p "${CODEX_AGENT_CONFIGS_DST}" "creating managed workspace Codex agent config directory ${CODEX_AGENT_CONFIGS_DST}"
    remove_stale_native_research_layers "${CODEX_AGENT_CONFIGS_DST}"
    for AGENT_ID in "${CODEX_NATIVE_STAGE_AGENT_IDS[@]}"; do
      render_native_research_layer "${AGENT_ID}" "${CODEX_AGENT_CONFIGS_DST}"
    done
    RESEARCH_OWNER_WORKSPACE_DST="${BOOTSTRAP_DST}"
  fi
  echo "  ${AGENTS} → ${BOOTSTRAP_DST} (${BOOTSTRAP_FILES_FOR_TARGET[*]})"
done
echo "Local workspace files such as USER.md and IDENTITY.md were left untouched."

if [[ -z "${RESEARCH_OWNER_WORKSPACE_DST:-}" ]]; then
  echo "ERROR: research-orchestrator workspace destination was not recorded." >&2
  exit 1
fi

# Native Codex resolves the active research roles from the owner agent's scoped
# CODEX_HOME/config.toml. The repository .codex/agents files are source-only
# metadata templates; rendered layers live in the owner workspace and are
# registered with absolute paths. Remove only known obsolete duplicate role
# TOMLs from old workspace/runtime locations; generic manual roles survive.
CODEX_NATIVE_RUNTIME_AGENT_IDS=("main" "research-orchestrator")
declare -A MANAGED_AGENT_DIR_SNAPSHOT_IDS=()
for CODEX_RUNTIME_AGENT_ID in "${CODEX_NATIVE_RUNTIME_AGENT_IDS[@]}"; do
  MANAGED_AGENT_DIR_SNAPSHOT_IDS["${CODEX_RUNTIME_AGENT_ID}"]=1
done
mapfile -t OPENAI_AGENT_IDS_FOR_SNAPSHOT < <(jq -r '
  ((.agents.entries // {}) | to_entries[] | .value + {id: .key})
  | select((.model.primary // "") | startswith("openai/"))
  | .id
' "${REPO_CONFIG}")
for OPENAI_AGENT_ID_FOR_SNAPSHOT in "${OPENAI_AGENT_IDS_FOR_SNAPSHOT[@]}"; do
  MANAGED_AGENT_DIR_SNAPSHOT_IDS["${OPENAI_AGENT_ID_FOR_SNAPSHOT}"]=1
done
for MANAGED_AGENT_ID_FOR_SNAPSHOT in "${!MANAGED_AGENT_DIR_SNAPSHOT_IDS[@]}"; do
  snapshot_managed_artifact_path "${OPENCLAW_PUSH_HOME}/agents/${MANAGED_AGENT_ID_FOR_SNAPSHOT}/agent"
done
echo "Writing native Codex runtime configs to ${#CODEX_NATIVE_RUNTIME_AGENT_IDS[@]} scoped Codex homes:"
for CODEX_RUNTIME_AGENT_ID in "${CODEX_NATIVE_RUNTIME_AGENT_IDS[@]}"; do
  CODEX_RUNTIME_HOME="${OPENCLAW_PUSH_HOME}/agents/${CODEX_RUNTIME_AGENT_ID}/agent/codex-home"
  write_codex_runtime_config "${CODEX_RUNTIME_HOME}" "${CODEX_RUNTIME_AGENT_ID}"
  if [[ "${CODEX_RUNTIME_AGENT_ID}" == "research-orchestrator" ]]; then
    render_native_research_role_config \
      "${CODEX_AGENTS_SRC}" \
      "${RESEARCH_OWNER_WORKSPACE_DST}/.codex/agent-configs" \
      "${CODEX_RUNTIME_HOME}/config.toml"
    validate_codex_native_research_runtime_roles \
      "${CODEX_RUNTIME_HOME}/config.toml" \
      "${CODEX_AGENTS_SRC}" \
      "${RESEARCH_OWNER_WORKSPACE_DST}/.codex/agent-configs"
  fi
  if [[ "${CODEX_RUNTIME_AGENT_ID}" == "main" || "${CODEX_RUNTIME_AGENT_ID}" == "research-orchestrator" ]]; then
    CODEX_RUNTIME_AGENTS_DST="${CODEX_RUNTIME_HOME}/agents"
    guarded_mkdir_p "${CODEX_RUNTIME_AGENTS_DST}" "creating managed Codex runtime agents directory ${CODEX_RUNTIME_AGENTS_DST}"
    remove_retired_codex_runtime_agents "${CODEX_RUNTIME_AGENTS_DST}"
  fi
  validate_codex_runtime_config "${CODEX_RUNTIME_HOME}" "${CODEX_RUNTIME_AGENT_ID}"
  echo "  ${CODEX_RUNTIME_AGENT_ID} → ${CODEX_RUNTIME_HOME}"
done

run_research_owner_command_contract_probe() {
  local codex_home="${OPENCLAW_PUSH_HOME}/agents/research-orchestrator/agent/codex-home"
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    echo "DEFERRED: paused mode skipped research-owner command-contract and sandbox probes."
    return 0
  fi
  if ! PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m gateway.deployment.command_probe probe \
    -- "${codex_home}" "${CODEX_APP_SERVER_CLI_RESOLVED}"; then
    echo "ERROR: Research owner command-contract probe failed; rolling back managed deployment from backup ${BACKUP}." >&2
    return 1
  fi
}

if [[ "${PROVIDER}" == "codex" ]]; then
  run_research_owner_command_contract_probe || run_deployment_rollback_and_exit 1
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
  if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
    echo "DEFERRED: paused mode skipped stale Azure user-manager environment cleanup."
    echo "DEFERRED: paused mode skipped Codex auth-file synchronization."
  else
    snapshot_managed_artifact_path "${SYSTEMD_USER_DIR}/${GATEWAY_SERVICE_NAME}"
    snapshot_stale_systemd_environment_paths
    remove_stale_azure_node_options_for_codex
    sync_managed_agent_codex_auth
  fi
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

# Install the research owner definition without enabling or starting it. The
# operator owns activation and stop transitions.
guarded_mkdir_p "${SYSTEMD_USER_DIR}" "creating managed systemd user directory ${SYSTEMD_USER_DIR}"
RESEARCH_OWNER_UNIT_TMP="$(mktemp "${SYSTEMD_USER_DIR}/.${RESEARCH_OWNER_SERVICE_NAME}.XXXXXX")"
guard_destination_path_chain "${RESEARCH_OWNER_UNIT_TMP}" "writing generated research owner unit ${RESEARCH_OWNER_UNIT_TMP}"
sed \
  -e "s|@REPO_ROOT@|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|@HOME@|$(escape_sed_replacement "${HOME}")|g" \
  -e "s|@PATH@|$(escape_sed_replacement "${PATH}")|g" \
  -e "s|@RESEARCH_V2_ROOT@|$(escape_sed_replacement "${RESEARCH_V2_ROOT}")|g" \
  -e "s|@RESEARCH_CORE_DATABASE@|$(escape_sed_replacement "${RESEARCH_CORE_DATABASE}")|g" \
  -e "s|@OWNER_ENV_FILE@|$(escape_sed_replacement "${APPROVED_OWNER_ENV_FILE}")|g" \
  -e "s|@PYTHON_BIN@|$(escape_sed_replacement "${PYTHON_BIN}")|g" \
  "${RESEARCH_OWNER_UNIT_TEMPLATE}" > "${RESEARCH_OWNER_UNIT_TMP}"
guard_destination_path_chain "${RESEARCH_OWNER_UNIT_TMP}" "wrote generated research owner unit ${RESEARCH_OWNER_UNIT_TMP}"
if grep -q '@[A-Z_][A-Z_]*@' "${RESEARCH_OWNER_UNIT_TMP}"; then
  echo "ERROR: Unresolved placeholder in generated ${RESEARCH_OWNER_SERVICE_NAME}." >&2
  exit 1
fi
if ! validate_research_owner_unit_file "${RESEARCH_OWNER_UNIT_TMP}" "${RESEARCH_V2_ROOT}" "${RESEARCH_CORE_DATABASE}" "${APPROVED_OWNER_ENV_FILE}"; then
  exit 1
fi
guarded_chmod 0644 "${RESEARCH_OWNER_UNIT_TMP}" "chmod generated research owner unit ${RESEARCH_OWNER_UNIT_TMP}"
begin_managed_unit_transaction
guarded_mv_replace "${RESEARCH_OWNER_UNIT_TMP}" "${RESEARCH_OWNER_UNIT_DST}" "publishing managed research owner unit ${RESEARCH_OWNER_UNIT_DST}"
RESEARCH_OWNER_UNIT_TMP=""
echo "Installed ${RESEARCH_OWNER_SERVICE_NAME} (not enabled or started)."

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
NATIVE_CRASH_HARDENING_DROPIN_TMP="$(mktemp "${GATEWAY_RUNTIME_CAPS_DROPIN_DIR}/.${NATIVE_CRASH_HARDENING_DROPIN_NAME}.XXXXXX")"
guarded_cp_file "${NATIVE_CRASH_HARDENING_DROPIN_SRC}" "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "staging managed native-crash hardening drop-in ${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
guarded_chmod 0644 "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "chmod staged managed native-crash hardening drop-in ${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_TMP}"
guarded_mv_replace "${NATIVE_CRASH_HARDENING_DROPIN_TMP}" "${NATIVE_CRASH_HARDENING_DROPIN_DST}" "publishing managed native-crash hardening drop-in ${NATIVE_CRASH_HARDENING_DROPIN_DST}"
NATIVE_CRASH_HARDENING_DROPIN_TMP=""
validate_native_crash_hardening_dropin_file "${NATIVE_CRASH_HARDENING_DROPIN_DST}"
if ! retire_obsolete_codex_runtime_dropin; then
  run_deployment_rollback_and_exit 1
fi
if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
  echo "DEFERRED: paused mode skipped systemd user-manager daemon-reload."
else
  if ! systemctl --user daemon-reload; then
    run_deployment_rollback_and_exit 1
  fi
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
echo "Installed ${GATEWAY_SERVICE_NAME} native-crash hardening → ${NATIVE_CRASH_HARDENING_DROPIN_DST}"
if [[ "${OPENCLAW_PUSH_MODE}" == "paused" ]]; then
  echo "Deferred user systemd reload; restart ${GATEWAY_SERVICE_NAME} externally after leaving paused mode."
else
  echo "Reloaded user systemd units; restart ${GATEWAY_SERVICE_NAME} externally for a running gateway to inherit these caps."
fi

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
