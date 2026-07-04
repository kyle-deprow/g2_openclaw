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
MERGED=$(echo "${MERGED}" | jq \
  --arg cmd "${MEMPALACE_PYTHON}" \
  --arg palace "${MEMPALACE_PALACE}" '
  .mcp.servers.mempalace = {
    "command": $cmd,
    "args": ["-m", "mempalace.mcp_server", "--palace", $palace]
  }
')
echo "Resolved required MemPalace MCP: ${MEMPALACE_PYTHON} --palace ${MEMPALACE_PALACE}"

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

MERGED=$(echo "${MERGED}" | jq --arg primary "${MODEL_PRIMARY}" '
  .agents.defaults.model.primary = $primary
  | .agents.defaults.models = { ($primary): {} }
')
echo "Active provider: ${PROVIDER} → model: ${MODEL_PRIMARY}"

# ── Write merged config ─────────────────────────────────────────────────────
echo "${MERGED}" | jq . > "${LOCAL_CONFIG}"
echo "Merged repo config into ${LOCAL_CONFIG}"

# ── Copy bootstrap files ────────────────────────────────────────────────────
# OpenClaw resolves per-agent workspaces as {workspace_base}-{agent_id}.
# Default agent is "claw" → workspace-claw/
AGENT_ID="claw"
BOOTSTRAP_DST="${OPENCLAW_HOME}/workspace-${AGENT_ID}"
mkdir -p "${BOOTSTRAP_DST}"
for FILE in SOUL.md AGENTS.md TOOLS.md BOOTSTRAP.md; do
  SRC="${REPO_ROOT}/gateway/agent_config/${FILE}"
  DST="${BOOTSTRAP_DST}/${FILE}"
  if [[ -f "${SRC}" ]]; then
    cp "${SRC}" "${DST}"
    echo "Copied ${FILE} → ${DST}"
  fi
done

# Clean stale copies from wrong locations
for FILE in SOUL.md AGENTS.md TOOLS.md BOOTSTRAP.md; do
  for STALE_DIR in "${OPENCLAW_HOME}" "${OPENCLAW_HOME}/workspace"; do
    STALE="${STALE_DIR}/${FILE}"
    if [[ -f "${STALE}" ]] && [[ "${STALE_DIR}" != "${BOOTSTRAP_DST}" ]]; then
      rm "${STALE}"
      echo "Removed stale ${STALE}"
    fi
  done
done

# ── Copy repo skills ─────────────────────────────────────────────────────────
SKILLS_SRC="${REPO_ROOT}/gateway/agent_config/skills"
SKILLS_DST="${OPENCLAW_HOME}/skills"
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
