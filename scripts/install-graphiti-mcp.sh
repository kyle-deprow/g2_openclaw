#!/usr/bin/env bash
# install-graphiti-mcp.sh — Idempotent install of the Graphiti MCP server.
#
# Clones the Graphiti repo and installs the MCP server into a dedicated venv.
# Safe to re-run: pulls latest on existing clone, skips venv if present.
#
# Usage:
#   bash scripts/install-graphiti-mcp.sh

set -euo pipefail

# Pin to a known-good release tag after first successful install.
# To pin: change GRAPHITI_REF to a tag like "v0.28.2" and use
#   git clone --branch "${GRAPHITI_REF}" --depth 1 ...
GRAPHITI_REF=""  # empty = latest main

GRAPHITI_HOME="${HOME}/.local/share/graphiti-mcp"
REPO_DIR="${GRAPHITI_HOME}/repo"
VENV_DIR="${GRAPHITI_HOME}/venv"
VENV_PYTHON="${VENV_DIR}/bin/python"

if [[ -d "${REPO_DIR}" ]]; then
  echo "Graphiti repo exists, pulling latest..."
  git -C "${REPO_DIR}" pull --ff-only 2>/dev/null || echo "Pull failed, using existing"
else
  echo "Cloning Graphiti..."
  mkdir -p "${GRAPHITI_HOME}"
  CLONE_ARGS=(--depth 1)
  [[ -n "${GRAPHITI_REF}" ]] && CLONE_ARGS+=(--branch "${GRAPHITI_REF}")
  git clone "${CLONE_ARGS[@]}" https://github.com/getzep/graphiti.git "${REPO_DIR}"
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Creating venv..."
  uv venv "${VENV_DIR}" --python 3.13
fi

echo "Installing MCP server dependencies..."
cd "${REPO_DIR}/mcp_server"
uv sync --python "${VENV_PYTHON}"

echo ""
echo "Installed. Entry point:"
echo "  cd ${REPO_DIR}/mcp_server && ${VENV_PYTHON} main.py --help"
