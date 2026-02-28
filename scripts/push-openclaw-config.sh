#!/usr/bin/env bash
# push-openclaw-config.sh — Merge repo-maintained OpenClaw config into the local installation.
#
# Usage (from repo root):
#   bash scripts/push-openclaw-config.sh
#
# Prerequisites:
#   - jq (https://jqlang.github.io/jq/)
#   - openclaw CLI on PATH
#   - AZURE_AI_SERVICES_API_KEY set in env or in gateway/openclaw_config/.env

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

# ── Load API key from .env or environment ────────────────────────────────────
if [[ -z "${AZURE_AI_SERVICES_API_KEY:-}" ]] && [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

if [[ -z "${AZURE_AI_SERVICES_API_KEY:-}" ]]; then
  echo "WARNING: AZURE_AI_SERVICES_API_KEY is not set." >&2
  echo "         The provider config will use the env: reference but auth may fail." >&2
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
      .agents.defaults.models = { ($primary): (.agents.defaults.models[$primary] // {}) }
    else . end
' "${LOCAL_CONFIG}" "${REPO_CONFIG}")

# ── Resolve env: references in provider apiKey fields ────────────────────────
# The repo config uses "env:VAR_NAME" placeholders for secrets. OpenClaw does
# NOT resolve these natively for custom provider apiKey fields — the literal
# string is passed to the SDK. We must substitute the actual value here.
if [[ -n "${AZURE_AI_SERVICES_API_KEY:-}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --arg key "${AZURE_AI_SERVICES_API_KEY}" '
    (.models.providers // {}) |= with_entries(
      if .value.apiKey == "env:AZURE_AI_SERVICES_API_KEY" then
        .value.apiKey = $key
      else . end
    )
  ')
  echo "Resolved env:AZURE_AI_SERVICES_API_KEY (${#AZURE_AI_SERVICES_API_KEY} chars)."
else
  echo "WARNING: AZURE_AI_SERVICES_API_KEY not set — apiKey will contain the literal 'env:' placeholder." >&2
  echo "         Auth will fail until the key is resolved." >&2
fi

# ── Write merged config ─────────────────────────────────────────────────────
echo "${MERGED}" | jq . > "${LOCAL_CONFIG}"
echo "Merged repo config into ${LOCAL_CONFIG}"

# ── Copy SOUL.md if present ─────────────────────────────────────────────────
SOUL_SRC="${REPO_ROOT}/gateway/agent_config/SOUL.md"
SOUL_DST="${OPENCLAW_HOME}/SOUL.md"
if [[ -f "${SOUL_SRC}" ]]; then
  cp "${SOUL_SRC}" "${SOUL_DST}"
  echo "Copied SOUL.md → ${SOUL_DST}"
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
  echo "Running: openclaw models status"
  openclaw models status || echo "WARNING: 'openclaw models status' returned non-zero."
else
  echo "openclaw CLI not found on PATH — skipping validation."
  echo "Verify manually: openclaw models status"
fi

echo ""
echo "Done. Config pushed successfully."
echo ""
echo "── Azure API-version preload ──"
echo "If using Azure OpenAI, ensure NODE_OPTIONS is set before starting the daemon:"
echo ""
echo "  export NODE_OPTIONS=\"--require \$HOME/.openclaw/azure-api-version-preload.cjs\""
echo ""
