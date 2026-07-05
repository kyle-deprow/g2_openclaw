#!/usr/bin/env bash
# push-openclaw-config.sh — Merge repo-maintained OpenClaw config into the local installation.
#
# Usage (from repo root):
#   bash scripts/push-openclaw-config.sh
#
# Prerequisites:
#   - jq (https://jqlang.github.io/jq/)
#   - openclaw CLI on PATH
#   - MemPalace installed with 'make mempalace-install'
#   - For codex: run 'openclaw models auth login --provider openai'
#   - For azure: run 'az login' to authenticate (Entra ID tokens acquired automatically)

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_CONFIG="${REPO_ROOT}/gateway/openclaw_config/openclaw.json"
ENV_FILE="${REPO_ROOT}/gateway/openclaw_config/.env"
QUANTIPY_ROOT="/home/dev/repos/quantipy"
SKILLS_SRC="${REPO_ROOT}/gateway/agent_config/skills"

OPENCLAW_HOME="${OPENCLAW_HOME:-${HOME}/.openclaw}"
LOCAL_CONFIG="${OPENCLAW_HOME}/openclaw.json"

# ── Pre-flight checks ───────────────────────────────────────────────────────
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
PRESERVE_ENV_VARS=(OPENCLAW_PROVIDER OPENAI_MODEL OPENROUTER_MODEL OPENROUTER_API_KEY AZURE_OAI_API_KEY)
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

if [[ "${OPENCLAW_PROVIDER:-codex}" == "openrouter" ]] && [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "ERROR: OPENROUTER_API_KEY is not set but OPENCLAW_PROVIDER=openrouter." >&2
  echo "       Set it in ${ENV_FILE} or export it before running this script." >&2
  exit 1
fi

if [[ "${OPENCLAW_PROVIDER:-codex}" == "codex" ]] && ! command -v openclaw &>/dev/null; then
  echo "ERROR: openclaw CLI is required for OPENCLAW_PROVIDER=codex." >&2
  exit 1
fi

# ── Backup ───────────────────────────────────────────────────────────────────
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${LOCAL_CONFIG}.bak.${TIMESTAMP}"
cp "${LOCAL_CONFIG}" "${BACKUP}"
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
  cp "${BACKUP}" "${LOCAL_CONFIG}"
  exit 1
fi

if ! "${MEMPALACE_PYTHON}" -c 'import mempalace.mcp_server' >/dev/null 2>&1; then
  echo "ERROR: MemPalace is installed but the MCP server module cannot be imported." >&2
  echo "       Run 'make mempalace-install' to upgrade/reinstall MemPalace." >&2
  cp "${BACKUP}" "${LOCAL_CONFIG}"
  exit 1
fi

mkdir -p "${MEMPALACE_PALACE}"
mkdir -p "${FASTEMBED_CACHE_PATH}"

if ! "${MEMPALACE_PYTHON}" "${REPO_ROOT}/scripts/check-mempalace-health.py"; then
  echo "ERROR: MemPalace healthcheck failed. Refusing to push OpenClaw config." >&2
  echo "       Fix the palace explicitly; startup will not auto-repair or fall back." >&2
  cp "${BACKUP}" "${LOCAL_CONFIG}"
  exit 1
fi

MERGED=$(echo "${MERGED}" | jq \
  --arg cmd "${MEMPALACE_PYTHON}" \
  --arg palace "${MEMPALACE_PALACE}" \
  --arg cache "${FASTEMBED_CACHE_PATH}" \
  --arg model "${MEMPALACE_EMBEDDING_MODEL}" \
  --arg offline "${HF_HUB_OFFLINE}" '
  .mcp.servers.mempalace = {
    "command": $cmd,
    "args": ["-m", "mempalace.mcp_server", "--palace", $palace],
    "env": {
      "FASTEMBED_CACHE_PATH": $cache,
      "MEMPALACE_EMBEDDING_MODEL": $model,
      "HF_HUB_OFFLINE": $offline
    }
  }
')
echo "Resolved required MemPalace MCP: ${MEMPALACE_PYTHON} --palace ${MEMPALACE_PALACE}"
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
# Autoresearch stage models, skills, subagent allowlists, and tool denies are
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

PM_MODEL_PRIMARY=$(jq -r '.agents.list[] | select(.id == "main") | .model.primary // empty' "${REPO_CONFIG}")
if [[ -z "${PM_MODEL_PRIMARY}" ]]; then
  echo "ERROR: Repo config must pin agents.list[].id == \"main\" to a model.primary." >&2
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
  (.agents.list[] | select(.id == "main") | .model.primary) = $pm
  | (.agents.list[] | select(.id == "main") | .thinkingDefault) = "high"
')

echo "Active provider: ${PROVIDER} → default model: ${MODEL_PRIMARY}; PM model: ${PM_MODEL_PRIMARY}"

# ── Managed invariant validation ─────────────────────────────────────────────
# Fail before writing if a local merge or env selection would violate the
# repo-managed autoresearch target shape.
if ! echo "${MERGED}" | jq -e --arg pm "${PM_MODEL_PRIMARY}" '
  def mutators: [
    "mempalace_add_drawer",
    "mempalace_check_duplicate",
    "mempalace_checkpoint",
    "mempalace_create_tunnel",
    "mempalace_delete_by_source",
    "mempalace_delete_drawer",
    "mempalace_delete_hallway",
    "mempalace_delete_tunnel",
    "mempalace_diary_write",
    "mempalace_hook_settings",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_mine",
    "mempalace_reconnect",
    "mempalace_sync",
    "mempalace_update_drawer"
  ];
  def expected_models: {
    "main": $pm,
    "context-curator": "openai/gpt-5.4",
    "debater-microstructure": "openai/gpt-5.5",
    "debater-data": "openai/gpt-5.5",
    "debater-skeptic": "openai/gpt-5.5",
    "debater-theory": "openai/gpt-5.4",
    "debater-implementation": "openai/gpt-5.4",
    "consensus-arbiter": "openai/gpt-5.4",
    "implementer": "openai/gpt-5.4",
    "reviewer": "openai/gpt-5.5",
    "fixer": "openai/gpt-5.4"
  };
  (.agents.defaults.thinkingDefault == "high")
  and ((.plugins.allow // []) | contains(["codex"]))
  and (.agents.defaults.memorySearch.enabled == false)
  and (.agents.defaults.compaction.memoryFlush.enabled == false)
  and ((.tools.deny // []) | contains(["memory_search", "memory_get"]))
  and (([.agents.list[].id] | sort) == (expected_models | keys | sort))
  and all(.agents.list[]; .model.primary == expected_models[.id])
  and all(.agents.list[]; .thinkingDefault == "high")
  and ([.agents.list[] | select(
    .id == "main"
    and .model.primary == $pm
    and .thinkingDefault == "high"
    and ((.skills // []) | contains(["mempalace", "autoresearch"]))
  )] | length) == 1
  and ([.agents.list[] | select(.id != "main") | select(((.skills // []) | index("mempalace")) != null)] | length) == 0
  and ([.agents.list[] | select(.id != "main") | select(((.skills // []) | index("mempalace-readonly")) == null)] | length) == 0
  and ([.agents.list[] | select(.id != "main") | select(((.skills // []) | index("quantipy-methodology")) == null)] | length) == 0
  and ([.agents.list[] | select(.id != "main") | select((((.tools.deny // []) | contains(mutators)) | not))] | length) == 0
  and (((.agents.list[] | select(.id == "main") | .tools.deny // []) | index("mempalace_add_drawer")) | not)
' >/dev/null; then
  echo "ERROR: Generated OpenClaw config violates repo-managed autoresearch invariants." >&2
  echo "       Check plugins.allow, main PM model/skills, MemPalace read-only stage agents, Quantipy methodology skill, and memory tool denies." >&2
  exit 1
fi
echo "Managed invariants validated: exact stage models, high reasoning, MemPalace split, Quantipy methodology skill, built-in memory disabled."

# ── Write merged config ─────────────────────────────────────────────────────
echo "${MERGED}" | jq . > "${LOCAL_CONFIG}"
echo "Merged repo config into ${LOCAL_CONFIG}"

# ── Copy bootstrap files ────────────────────────────────────────────────────
# OpenClaw uses ~/.openclaw/workspace for the main agent when no workspace is
# configured. Other agents default to workspace-{agent_id}. An explicit
# .workspace value is used as-is relative to OPENCLAW_HOME unless it is absolute.
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
    printf '%s/workspace\n' "${OPENCLAW_HOME}"
  else
    printf '%s/%s\n' "${OPENCLAW_HOME}" "${workspace_target}"
  fi
}

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
  echo "  ${AGENTS} → ${BOOTSTRAP_DST} (${BOOTSTRAP_FILES[*]})"
done
echo "Local workspace files such as USER.md and IDENTITY.md were left untouched."

# Clean stale copies from wrong bootstrap locations without touching local
# per-workspace files such as USER.md, IDENTITY.md, or other notes.
STALE_BOOTSTRAP_DIRS=(
  "${OPENCLAW_HOME}"
  "${OPENCLAW_HOME}/workspace"
  "${OPENCLAW_HOME}/workspace-claw"
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
SKILLS_DST="${OPENCLAW_HOME}/skills"
STALE_SKILLS=(
  "copilot-cli"
)
for SKILL_NAME in "${STALE_SKILLS[@]}"; do
  if [[ -d "${SKILLS_DST}/${SKILL_NAME}" ]]; then
    rm -rf "${SKILLS_DST:?}/${SKILL_NAME}"
    echo "Removed stale skill ${SKILL_NAME} from ${SKILLS_DST}"
  fi
done

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

# ── Copy Azure API-version preload if present ────────────────────────────────
PRELOAD_SRC="${REPO_ROOT}/gateway/openclaw_config/azure-api-version-preload.cjs"
PRELOAD_DST="${OPENCLAW_HOME}/azure-api-version-preload.cjs"
if [[ -f "${PRELOAD_SRC}" ]]; then
  cp "${PRELOAD_SRC}" "${PRELOAD_DST}"
  echo "Copied azure-api-version-preload.cjs → ${PRELOAD_DST}"
fi

# ── Fix per-agent model overrides ────────────────────────────────────────────
# OpenClaw may generate per-agent models.json with stale URLs (e.g. model-router).
# Force the azure-oai-g2 provider to use the direct deployment URL from our config.
AGENT_MODELS="${OPENCLAW_HOME}/agents/claw/agent/models.json"
if [[ -f "${AGENT_MODELS}" ]] && command -v python3 &>/dev/null; then
  CORRECT_BASE_URL=$(jq -r '.models.providers["azure-oai-g2"].baseUrl // empty' "${LOCAL_CONFIG}")
  if [[ -n "${CORRECT_BASE_URL}" ]]; then
    python3 -c "
import json, sys
with open('${AGENT_MODELS}') as f:
    c = json.load(f)
changed = False
for name in ['azure-oai-g2', 'azure-oai-g2-mini']:
    p = c.get('providers', {}).get(name)
    if not p: continue
    if 'apiKey' in p:
        del p['apiKey']  # Entra tokens via preload, not API keys
        changed = True
if 'azure-oai-g2' in c.get('providers', {}):
    p = c['providers']['azure-oai-g2']
    if p.get('baseUrl') != '${CORRECT_BASE_URL}':
        p['baseUrl'] = '${CORRECT_BASE_URL}'
        changed = True
if changed:
    with open('${AGENT_MODELS}', 'w') as f:
        json.dump(c, f, indent=2)
    print('Fixed per-agent models.json: baseUrl + removed apiKeys')
else:
    print('Per-agent models.json already correct')
"
  fi
fi

# ── Validate ─────────────────────────────────────────────────────────────────
echo ""
echo "── Validating config ──"
if command -v openclaw &>/dev/null; then
  echo "Running: openclaw config validate"
  if ! openclaw config validate; then
    cp "${BACKUP}" "${LOCAL_CONFIG}"
    echo "ERROR: 'openclaw config validate' failed. Restored backup ${BACKUP}." >&2
    exit 1
  fi
  if [[ "${PROVIDER}" == "codex" ]]; then
    echo "Running: openclaw plugins inspect codex --json"
    if ! openclaw plugins inspect codex --json | jq -e '
      .plugin.id == "codex"
      and .plugin.enabled == true
      and .plugin.status == "loaded"
    ' >/dev/null; then
      cp "${BACKUP}" "${LOCAL_CONFIG}"
      echo "ERROR: Required Codex plugin is not installed, enabled, and loaded. Restored backup ${BACKUP}." >&2
      echo "       Run: openclaw plugins install @openclaw/codex" >&2
      exit 1
    fi
  fi
else
  cp "${BACKUP}" "${LOCAL_CONFIG}"
  echo "ERROR: openclaw CLI not found on PATH. Restored backup ${BACKUP}." >&2
  echo "       Install OpenClaw and rerun validation." >&2
  exit 1
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
