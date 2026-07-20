#!/usr/bin/env bash
# ── scripts/bootstrap.sh ─────────────────────────────────────────────────────
# One-shot setup script for the G2 OpenClaw project.
# Safe to re-run (idempotent). Run from anywhere — auto-detects repo root.
#
# Usage:
#   ./scripts/bootstrap.sh              # full interactive setup
#   ./scripts/bootstrap.sh --skip-optional   # skip optional prompts
#   ./scripts/bootstrap.sh --help
#
# Make executable:  chmod +x scripts/bootstrap.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Resolve repo root ────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── Flags ─────────────────────────────────────────────────────────────────────
SKIP_OPTIONAL=false
HAS_GPU=false
GPU_NAME=""
SUMMARY_ITEMS=()
REQUIRED_OPENCLAW_VERSION="2026.7.1-2"
OPENCLAW_BIN_RESOLVED=""
OPENCLAW_VERSION_RESOLVED=""
OPENCLAW_INSTALLED_PATH=""

for arg in "$@"; do
  case "$arg" in
    --skip-optional) SKIP_OPTIONAL=true ;;
    --help|-h)
      cat <<'EOF'
Usage: scripts/bootstrap.sh [OPTIONS]

One-shot setup for the G2 OpenClaw project.

Options:
  --skip-optional   Skip optional tool installs (evenhub-simulator, evenhub-cli)
  --help, -h        Show this help message

What it does:
  1. Checks system prerequisites (Python ≥3.13, uv, Node.js ≥22, npm)
  2. Installs Python dependencies via uv
  3. Installs TypeScript dependencies (g2_app)
  4. Installs OpenClaw CLI, onboards, installs MemPalace MCP, and pushes repo config
  5. Generates environment config via gateway init-env
  6. Installs pre-commit hooks
  7. Optionally installs EvenHub global tools
  8. Checks Tailscale, file permissions, and security posture
  9. Runs smoke tests to verify the Python stack
 10. Prints a summary of what was set up
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)"
      exit 1
      ;;
  esac
done

# ── Colors & helpers ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  BLUE='\033[0;34m'
  BOLD='\033[1m'
  DIM='\033[2m'
  RESET='\033[0m'
else
  GREEN='' RED='' YELLOW='' BLUE='' BOLD='' DIM='' RESET=''
fi

ok()   { echo -e " ${GREEN}✓${RESET} $*"; }
fail() { echo -e " ${RED}✗${RESET} $*"; }
warn() { echo -e " ${YELLOW}⚠${RESET} $*"; }
info() { echo -e " ${BLUE}→${RESET} $*"; }

section() {
  echo ""
  echo -e "${BOLD}━━━ $* ━━━${RESET}"
}

summary_add() {
  SUMMARY_ITEMS+=("$1")
}

prompt_yn() {
  if $SKIP_OPTIONAL; then
    return 1
  fi
  local prompt="$1"
  read -r -p "   $prompt [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

version_gte() {
  local have="$1" need="$2"
  [[ "$(printf '%s\n%s\n' "${need}" "${have}" | sort -V | head -n1)" == "${need}" ]]
}

expand_user_path() {
  local path="$1"
  case "${path}" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${path:2}" ;;
    *) printf '%s\n' "${path}" ;;
  esac
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
  OPENCLAW_BIN_RESOLVED=""
  return 1
}

read_openclaw_version() {
  local version_line
  [[ -n "${OPENCLAW_BIN_RESOLVED}" ]] || return 1
  if ! version_line="$("${OPENCLAW_BIN_RESOLVED}" --version 2>&1)"; then
    OPENCLAW_VERSION_RESOLVED=""
    return 1
  fi
  version_line="${version_line%%$'\n'*}"
  if [[ "${version_line}" =~ (^|[[:space:]])([0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*)($|[[:space:]]) ]]; then
    OPENCLAW_VERSION_RESOLVED="${BASH_REMATCH[2]}"
    return 0
  fi
  OPENCLAW_VERSION_RESOLVED=""
  return 1
}

require_openclaw_exact_path() {
  local executable="$1"
  OPENCLAW_BIN_RESOLVED="${executable}"
  if [[ ! -f "${OPENCLAW_BIN_RESOLVED}" || ! -x "${OPENCLAW_BIN_RESOLVED}" ]]; then
    fail "OpenClaw executable is missing or non-executable: ${OPENCLAW_BIN_RESOLVED}"
    return 1
  fi
  if ! read_openclaw_version; then
    fail "Could not parse OpenClaw version from ${OPENCLAW_BIN_RESOLVED}"
    return 1
  fi
  if [[ "${OPENCLAW_VERSION_RESOLVED}" != "${REQUIRED_OPENCLAW_VERSION}" ]]; then
    fail "OpenClaw ${OPENCLAW_VERSION_RESOLVED} at ${OPENCLAW_BIN_RESOLVED} is unsupported — need exactly ${REQUIRED_OPENCLAW_VERSION}"
    return 1
  fi
  export OPENCLAW_BIN="${OPENCLAW_BIN_RESOLVED}"
  ok "OpenClaw CLI ${OPENCLAW_VERSION_RESOLVED} (${OPENCLAW_BIN_RESOLVED})"
}

install_openclaw_exact() {
  local package="openclaw@${REQUIRED_OPENCLAW_VERSION}"
  OPENCLAW_INSTALLED_PATH=""
  if command -v pnpm &>/dev/null; then
    local pnpm_home
    pnpm_home="$(expand_user_path "${PNPM_HOME:-$HOME/.local/share/pnpm}")"
    mkdir -p "${pnpm_home}"
    export PNPM_HOME="${pnpm_home}"
    export PATH="${PNPM_HOME}:${PATH}"
    info "Installing ${package} via pnpm (${PNPM_HOME})"
    if ! pnpm add -g "${package}"; then
      return 1
    fi
    OPENCLAW_INSTALLED_PATH="${PNPM_HOME}/openclaw"
  else
    local npm_prefix
    npm_prefix="${NPM_CONFIG_PREFIX:-$HOME/.local}"
    npm_prefix="$(expand_user_path "${npm_prefix}")"
    mkdir -p "${npm_prefix}/bin"
    export PATH="${npm_prefix}/bin:${PATH}"
    info "Installing ${package} via npm (${npm_prefix})"
    if ! npm install -g --prefix "${npm_prefix}" "${package}"; then
      return 1
    fi
    OPENCLAW_INSTALLED_PATH="${npm_prefix}/bin/openclaw"
  fi
  hash -r
}

ensure_openclaw_exact_version() {
  if [[ -n "${OPENCLAW_BIN:-}" ]]; then
    preflight_openclaw_override
    return
  fi

  if resolve_openclaw_bin && read_openclaw_version; then
    if [[ "${OPENCLAW_VERSION_RESOLVED}" == "${REQUIRED_OPENCLAW_VERSION}" ]]; then
      export OPENCLAW_BIN="${OPENCLAW_BIN_RESOLVED}"
      ok "OpenClaw CLI ${OPENCLAW_VERSION_RESOLVED} (${OPENCLAW_BIN_RESOLVED})"
      return 0
    fi
    info "OpenClaw ${OPENCLAW_VERSION_RESOLVED} at ${OPENCLAW_BIN_RESOLVED} — installing ${REQUIRED_OPENCLAW_VERSION}"
  else
    info "OpenClaw CLI not found — installing ${REQUIRED_OPENCLAW_VERSION}"
  fi

  if ! install_openclaw_exact; then
    return 1
  fi
  require_openclaw_exact_path "${OPENCLAW_INSTALLED_PATH}"
}

preflight_openclaw_override() {
  local override
  [[ -n "${OPENCLAW_BIN:-}" ]] || return 0
  override="$(expand_user_path "${OPENCLAW_BIN}")"
  if ! require_openclaw_exact_path "${override}"; then
    fail "OPENCLAW_BIN must name an executable at exactly version ${REQUIRED_OPENCLAW_VERSION}; bootstrap did not install or upgrade anything"
    return 1
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 1. Check system prerequisites
# ══════════════════════════════════════════════════════════════════════════════
check_prerequisites() {
  section "1/9  Checking prerequisites"
  local fatal=false

  # ── Python ≥ 3.13 ──────────────────────────────────────────────────────────
  if command -v python3 &>/dev/null; then
    local pyver
    pyver="$(python3 --version 2>&1 | grep -oP '\d+\.\d+(\.\d+)?')"
    if version_gte "$pyver" "3.13"; then
      ok "Python $pyver"
    else
      fail "Python $pyver found — need ≥ 3.13"
      fatal=true
    fi
  else
    fail "Python 3 not found"
    fatal=true
  fi

  # ── uv ──────────────────────────────────────────────────────────────────────
  if command -v uv &>/dev/null; then
    ok "uv $(uv --version 2>&1 | head -1)"
  else
    warn "uv not found"
    if prompt_yn "Install uv now via official installer?"; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
      # Reload PATH so uv is available immediately
      export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      if command -v uv &>/dev/null; then
        ok "uv installed: $(uv --version 2>&1 | head -1)"
        summary_add "Installed uv"
      else
        fail "uv installation failed"
        fatal=true
      fi
    else
      fail "uv is required — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
      fatal=true
    fi
  fi

  # ── Node.js ≥ 22 ───────────────────────────────────────────────────────────
  if command -v node &>/dev/null; then
    local nodever
    nodever="$(node --version 2>&1 | sed 's/^v//')"
    if version_gte "$nodever" "22.0"; then
      ok "Node.js $nodever"
    else
      fail "Node.js $nodever found — need ≥ 22"
      fatal=true
    fi
  else
    fail "Node.js not found"
    fatal=true
  fi

  # ── npm ─────────────────────────────────────────────────────────────────────
  if command -v npm &>/dev/null; then
    ok "npm $(npm --version 2>&1)"
  else
    fail "npm not found (usually bundled with Node.js)"
    fatal=true
  fi

  # ── Optional tools ─────────────────────────────────────────────────────────
  for tool in espeak-ng ffmpeg jq; do
    if command -v "$tool" &>/dev/null; then
      ok "$tool (optional)"
    else
      warn "$tool not found (optional — some features may be limited)"
    fi
  done

  # ── NVIDIA GPU ──────────────────────────────────────────────────────────────
  if command -v nvidia-smi &>/dev/null; then
    local gpu_info
    gpu_info="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null || true)"
    if [[ -n "$gpu_info" ]]; then
      GPU_NAME="$(echo "$gpu_info" | head -1)"
      HAS_GPU=true
      ok "NVIDIA GPU: $GPU_NAME"
      summary_add "GPU detected: $GPU_NAME"
    else
      info "nvidia-smi found but no GPU info available"
    fi
  else
    info "No NVIDIA GPU detected (will use CPU for Whisper)"
  fi

  if $fatal; then
    echo ""
    fail "Missing required prerequisites — fix the above and re-run."
    exit 1
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 2. Install Python dependencies
# ══════════════════════════════════════════════════════════════════════════════
install_python_deps() {
  section "2/9  Installing Python dependencies"

  uv sync --extra dev  # runtime + dev tools (ruff, pytest, mypy)
  if $HAS_GPU; then
    summary_add "Python deps installed (CUDA GPU)"
  else
    info "No NVIDIA GPU — Whisper will use CPU (slower but functional)"
    summary_add "Python deps installed (CPU mode)"
  fi
  ok "Python dependencies ready"
}

# ══════════════════════════════════════════════════════════════════════════════
# 3. Install TypeScript dependencies
# ══════════════════════════════════════════════════════════════════════════════
install_ts_deps() {
  section "3/9  Installing TypeScript dependencies"

  if [[ -d "$REPO_ROOT/g2_app" ]]; then
    info "Installing g2_app dependencies..."
    (cd "$REPO_ROOT/g2_app" && npm install)
    ok "g2_app npm install"
    summary_add "g2_app: npm packages installed"
  else
    warn "g2_app/ directory not found — skipping"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 4. OpenClaw setup
# ══════════════════════════════════════════════════════════════════════════════
setup_openclaw() {
  section "4/9  OpenClaw setup"

  if ! ensure_openclaw_exact_version; then
    exit 1
  fi
  summary_add "OpenClaw: ${OPENCLAW_VERSION_RESOLVED} at ${OPENCLAW_BIN_RESOLVED}"

  # --- Check if onboarded ---
  local oc_config="$HOME/.openclaw/openclaw.json"
  if [[ ! -f "$oc_config" ]]; then
    info "OpenClaw not onboarded yet (~/.openclaw/openclaw.json not found)"
    "${OPENCLAW_BIN_RESOLVED}" onboard --local
    if [[ -f "$oc_config" ]]; then
      ok "OpenClaw onboarded"
      summary_add "OpenClaw: onboarded"
    else
      fail "OpenClaw onboarding did not create ${oc_config}"
      exit 1
    fi
  else
    ok "OpenClaw config found: $oc_config"
  fi

  # --- Install/enable required Codex plugin and remove Copilot routes ---
  info "Using OpenClaw ${OPENCLAW_VERSION_RESOLVED} at ${OPENCLAW_BIN_RESOLVED}"
  if "${OPENCLAW_BIN_RESOLVED}" plugins install @openclaw/codex; then
    ok "Codex plugin installed/upgraded"
    summary_add "OpenClaw: Codex plugin installed/upgraded"
  else
    fail "Codex plugin install failed — required for OpenAI/Codex routing"
    exit 1
  fi
  "${OPENCLAW_BIN_RESOLVED}" plugins enable codex
  "${OPENCLAW_BIN_RESOLVED}" plugins disable github-copilot || true
  "${OPENCLAW_BIN_RESOLVED}" plugins disable copilot-proxy || true

  # --- Install required MemPalace MCP server ---
  if make -C "$REPO_ROOT" mempalace-install; then
    ok "MemPalace installed/upgraded"
    summary_add "MemPalace: installed/upgraded"
  else
    fail "MemPalace install failed — required for OpenClaw research memory"
    exit 1
  fi

  local mempalace_python="$HOME/.local/share/mempalace/venv/bin/python"
  local mempalace_health="$REPO_ROOT/scripts/check-mempalace-health.py"
  if FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}" \
    MEMPALACE_EMBEDDING_MODEL="${MEMPALACE_EMBEDDING_MODEL:-bge-base}" \
    HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" \
    "$mempalace_python" "$mempalace_health"; then
    ok "MemPalace healthcheck passed"
    summary_add "MemPalace: healthcheck passed"
  else
    fail "MemPalace healthcheck failed — fix the palace explicitly; bootstrap will not repair or fall back"
    exit 1
  fi

  # --- Push repo config ---
  local push_script="$REPO_ROOT/scripts/push-openclaw-config.sh"
  if [[ -f "$push_script" ]]; then
    if OPENCLAW_BIN="${OPENCLAW_BIN_RESOLVED}" bash "$push_script"; then
      ok "OpenClaw config pushed"
      summary_add "OpenClaw: config pushed"
    else
      fail "OpenClaw config push failed — required for Codex and MemPalace routing"
      exit 1
    fi
  else
    fail "push-openclaw-config.sh not found at $push_script"
    exit 1
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 5. Generate environment config
# ══════════════════════════════════════════════════════════════════════════════
generate_env() {
  section "5/9  Generating environment config"

  # gateway init-env creates .env and g2_app/.env.local (skips if exists)
  if [[ -f "$REPO_ROOT/.env" ]]; then
    warn ".env already exists — skipping init-env (use 'uv run python -m gateway init-env --force' to regenerate)"
    summary_add ".env: already existed (kept)"
  else
    info "Running gateway init-env..."
    uv run python -m gateway init-env
    ok "Generated .env and g2_app/.env.local"
    summary_add ".env: generated via init-env"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 6. Install pre-commit hooks
# ══════════════════════════════════════════════════════════════════════════════
install_precommit() {
  section "6/9  Installing pre-commit hooks"

  if [[ -f "$REPO_ROOT/.pre-commit-config.yaml" ]]; then
    uv run pre-commit install
    ok "pre-commit hooks installed"
    summary_add "pre-commit hooks installed"
  else
    warn ".pre-commit-config.yaml not found — skipping"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 7. Install optional global tools
# ══════════════════════════════════════════════════════════════════════════════
install_optional_tools() {
  section "7/9  Optional global tools"

  if $SKIP_OPTIONAL; then
    info "Skipping optional tools (--skip-optional)"
    return
  fi

  if prompt_yn "Install @evenrealities/evenhub-simulator globally?"; then
    npm i -g @evenrealities/evenhub-simulator@latest
    ok "evenhub-simulator installed"
    summary_add "Installed evenhub-simulator (global)"
  else
    info "Skipped evenhub-simulator"
  fi

  if prompt_yn "Install @evenrealities/evenhub-cli globally?"; then
    npm i -g @evenrealities/evenhub-cli@latest
    ok "evenhub-cli installed"
    summary_add "Installed evenhub-cli (global)"
  else
    info "Skipped evenhub-cli"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 8. Tailscale (remote access)
# ══════════════════════════════════════════════════════════════════════════════
check_tailscale() {
  section "8/9  Network & Security"

  # --- Tailscale ---
  if command -v tailscale &>/dev/null; then
    local ts_ip
    ts_ip="$(tailscale ip -4 2>/dev/null || true)"
    if [[ -n "$ts_ip" ]]; then
      ok "Tailscale connected: $ts_ip"
      summary_add "Tailscale: $ts_ip (remote access ready)"
      info "G2 app can reach the gateway at ws://$ts_ip:8765 from any network"
    else
      warn "Tailscale installed but not connected"
      info "Run: sudo tailscale up"
      summary_add "Tailscale: installed but not connected"
    fi
  else
    info "Tailscale not installed — gateway will be LAN-only"
    if ! $SKIP_OPTIONAL; then
      info "Install for remote access: https://tailscale.com/download"
    fi
    summary_add "Tailscale: not installed (LAN-only mode)"
  fi

  # --- Device identity file permissions ---
  local id_file="$HOME/.openclaw/state/device-identity.json"
  if [[ -f "$id_file" ]]; then
    local perms
    perms="$(stat -c '%a' "$id_file" 2>/dev/null || stat -f '%Lp' "$id_file" 2>/dev/null || echo "unknown")"
    if [[ "$perms" == "600" ]]; then
      ok "Device identity file permissions: $perms"
    else
      warn "Device identity file permissions: $perms (should be 600) — fixing"
      chmod 600 "$id_file"
      ok "Fixed device identity file permissions to 600"
      summary_add "Security: fixed device-identity.json permissions"
    fi
  fi

  # --- OpenClaw loopback check ---
  if command -v ss &>/dev/null && ss -tlnp 2>/dev/null | grep -q ":18789"; then
    local oc_bind
    oc_bind="$(ss -tlnp 2>/dev/null | grep ':18789' | awk '{print $4}' | head -1)"
    if echo "$oc_bind" | grep -qE '^(127\.0\.0\.1|::1|\[::1\])'; then
      ok "OpenClaw listening on loopback only ($oc_bind)"
    else
      warn "OpenClaw is NOT on loopback ($oc_bind) — consider restricting to 127.0.0.1"
      summary_add "Security: OpenClaw exposed on $oc_bind (should be loopback)"
    fi
  fi

  # --- Gateway token strength in .env ---
  if [[ -f "$REPO_ROOT/.env" ]]; then
    local token_line
    token_line="$(grep -E '^GATEWAY_TOKEN=' "$REPO_ROOT/.env" 2>/dev/null | head -1)"
    if [[ -n "$token_line" ]]; then
      local token_val="${token_line#GATEWAY_TOKEN=}"
      local token_len="${#token_val}"
      if [[ "$token_len" -ge 32 ]]; then
        ok "Gateway token: ${token_len} chars (strong)"
      elif [[ "$token_len" -ge 16 ]]; then
        warn "Gateway token: ${token_len} chars (acceptable, 32+ recommended)"
      elif [[ "$token_len" -gt 0 ]]; then
        warn "Gateway token: ${token_len} chars (weak — regenerate with init-env --force)"
        summary_add "Security: gateway token is weak ($token_len chars)"
      fi
    else
      warn "No GATEWAY_TOKEN in .env — gateway startup will fail"
      summary_add "Security: no gateway token set"
    fi
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 9. Run smoke tests
# ══════════════════════════════════════════════════════════════════════════════
run_smoke_tests() {
  section "9/9  Running smoke tests"

  info "Running gateway unit tests..."
  if uv run pytest tests/gateway/ -q; then
    ok "All gateway smoke tests passed"
    summary_add "Smoke tests: PASSED"
  else
    warn "Some tests failed — check output above"
    summary_add "Smoke tests: SOME FAILURES (non-blocking)"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
print_summary() {
  echo ""
  echo -e "${BOLD}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓${RESET}"
  echo -e "${BOLD}┃                    G2 OpenClaw — Setup Complete                     ┃${RESET}"
  echo -e "${BOLD}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛${RESET}"
  echo ""

  echo -e "${BOLD}  What was done:${RESET}"
  for item in "${SUMMARY_ITEMS[@]}"; do
    echo -e "    ${GREEN}✓${RESET} $item"
  done

  echo ""
  echo -e "${BOLD}  Next steps:${RESET}"
  echo -e "    ${BLUE}1.${RESET} Edit ${BOLD}.env${RESET} — ensure GATEWAY_TOKEN is 32+ chars and review Whisper settings"
  echo -e "    ${BLUE}2.${RESET} Verify security:  ${DIM}re-run this script to check file permissions & token strength${RESET}"
  echo -e "    ${BLUE}3.${RESET} Start OpenClaw daemon:  ${DIM}${OPENCLAW_BIN_RESOLVED:-openclaw}${RESET}"
  echo -e "    ${BLUE}4.${RESET} Launch gateway:  ${DIM}uv run python -m gateway launch${RESET}"
  echo -e "    ${BLUE}5.${RESET} Start G2 app:    ${DIM}cd g2_app && npm run dev${RESET}"
  echo -e "    ${BLUE}6.${RESET} ${DIM}(Optional)${RESET} Install Tailscale for remote access: ${DIM}https://tailscale.com/download${RESET}"
  echo ""
  echo -e "  ${DIM}Docs: docs/guides/getting-started.md${RESET}"
  echo -e "  ${DIM}Re-run this script any time — it's idempotent.${RESET}"
  echo ""
}

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
main() {
  if ! preflight_openclaw_override; then
    return 1
  fi

  echo ""
  echo -e "${BOLD}🦀 G2 OpenClaw Bootstrap${RESET}"
  echo -e "${DIM}   Repo: $REPO_ROOT${RESET}"
  echo ""

  check_prerequisites
  install_python_deps
  install_ts_deps
  setup_openclaw
  generate_env
  install_precommit
  install_optional_tools
  check_tailscale
  run_smoke_tests
  print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main
fi
