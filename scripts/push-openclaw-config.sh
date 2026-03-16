#!/usr/bin/env bash
# push-openclaw-config.sh — Merge repo-maintained OpenClaw config into the local installation.
#
# Usage (from repo root):
#   bash scripts/push-openclaw-config.sh
#
# Prerequisites:
#   - jq (https://jqlang.github.io/jq/)
#   - openclaw CLI on PATH
#   - For copilot: run 'openclaw github-copilot login' (or set GH_TOKEN env var)
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
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

if [[ "${OPENCLAW_PROVIDER:-copilot}" == "openrouter" ]] && [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "WARNING: OPENROUTER_API_KEY is not set but OPENCLAW_PROVIDER=openrouter." >&2
  echo "         Set it in ${ENV_FILE} or export it before running this script." >&2
fi

# ── Backup ───────────────────────────────────────────────────────────────────
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${LOCAL_CONFIG}.bak.${TIMESTAMP}"
cp "${LOCAL_CONFIG}" "${BACKUP}"
echo "Backed up local config → ${BACKUP}"

# ── Deep merge ───────────────────────────────────────────────────────────────
# jq's * operator does recursive object merge (right wins on conflicts).
# We read the local config as base and overlay the repo config on top.
# Then remove stale model references that don't match the repo's primary model.
REPO_PRIMARY=$(jq -r '.agents.defaults.model.primary // empty' "${REPO_CONFIG}")
MERGED=$(jq -s --arg primary "${REPO_PRIMARY}" '
  .[0] * .[1]
  | if $primary != "" then
      # Build models allowlist from ALL configured providers (not just primary).
      # Each provider/modelId pair becomes an allowed model for agent use + cron.
      .agents.defaults.models = (
        [.models.providers | to_entries[] | .key as $p | .value.models[]? | {("\($p)/\(.id)"): {}}]
        | add // { ($primary): {} }
      )
    else . end
' "${LOCAL_CONFIG}" "${REPO_CONFIG}")

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

# ── Resolve env: references in provider apiKey fields ────────────────────────
# The repo config uses "env:VAR_NAME" placeholders for secrets. OpenClaw does
# NOT resolve these natively for custom provider apiKey fields — the literal
# string is passed to the SDK. We must substitute the actual value here.
# Note: Azure OpenAI uses Entra ID auth (injected by the preload) — no apiKey needed.
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --arg key "${OPENROUTER_API_KEY}" '
    (.models.providers // {}) |= with_entries(
      if .value.apiKey == "env:OPENROUTER_API_KEY" then
        .value.apiKey = $key
      else . end
    )
  ')
  echo "Resolved env:OPENROUTER_API_KEY (${#OPENROUTER_API_KEY} chars)."
fi

# ── Provider selection ───────────────────────────────────────────────────────
PROVIDER="${OPENCLAW_PROVIDER:-copilot}"
case "${PROVIDER}" in
  copilot)
    MODEL_PRIMARY="github-copilot/${COPILOT_MODEL:-claude-sonnet-4.6}"
    ;;
  azure)
    MODEL_PRIMARY="azure-oai-g2/gpt-5.4"
    ;;
  openrouter)
    MODEL_PRIMARY="openrouter/${OPENROUTER_MODEL:-anthropic/claude-sonnet-4-20250514}"
    ;;
  *)
    echo "ERROR: Unknown OPENCLAW_PROVIDER '${PROVIDER}'. Use 'copilot', 'azure', or 'openrouter'." >&2
    exit 1
    ;;
esac

MERGED=$(echo "${MERGED}" | jq --arg primary "${MODEL_PRIMARY}" '
  .agents.defaults.model.primary = $primary
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

# ── Copy skills ──────────────────────────────────────────────────────────────
SKILLS_SRC="${REPO_ROOT}/gateway/agent_config/skills"
SKILLS_DST="${OPENCLAW_HOME}/skills"
if [[ -d "${SKILLS_SRC}" ]]; then
  for SKILL_DIR in "${SKILLS_SRC}"/*/; do
    SKILL_NAME="$(basename "${SKILL_DIR}")"
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

# ── Validate ─────────────────────────────────────────────────────────────────
echo ""
echo "── Validating config ──"
if command -v openclaw &>/dev/null; then
  echo "Running: openclaw config validate"
  openclaw config validate || echo "WARNING: 'openclaw config validate' returned non-zero."
else
  echo "openclaw CLI not found on PATH — skipping validation."
  echo "Verify manually: openclaw models status"
fi

echo ""
echo "Done. Config pushed successfully."
echo ""
if [[ "${PROVIDER}" == "azure" ]]; then
  echo "── Azure Entra preload ──"
  echo "Ensure 'az login' has been run and NODE_OPTIONS is set before starting the daemon:"
  echo ""
  echo "  az login"
  echo "  export NODE_OPTIONS=\"--require \$HOME/.openclaw/azure-api-version-preload.cjs\""
  echo ""
elif [[ "${PROVIDER}" == "copilot" ]]; then
  echo "── GitHub Copilot ──"
  echo "Using model: ${MODEL_PRIMARY}"
  echo "The daemon will auto-exchange your GitHub token for Copilot API tokens."
  echo "No NODE_OPTIONS preload needed."
  echo ""
fi
