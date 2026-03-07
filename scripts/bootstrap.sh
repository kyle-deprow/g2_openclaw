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
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Flags ─────────────────────────────────────────────────────────────────────
SKIP_OPTIONAL=false
HAS_GPU=false
GPU_NAME=""
SUMMARY_ITEMS=()

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
  3. Installs TypeScript dependencies (g2_app, copilot_bridge)
  4. Generates environment config via gateway init-env
  5. Installs pre-commit hooks
  6. Optionally installs EvenHub global tools
  7. Runs smoke tests to verify the Python stack
  8. Prints a summary of what was set up
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
  # Returns 0 if $1 >= $2 (semver-ish, compares major.minor)
  local have="$1" need="$2"
  local have_major have_minor need_major need_minor
  have_major="${have%%.*}"
  have_minor="${have#*.}"; have_minor="${have_minor%%.*}"
  need_major="${need%%.*}"
  need_minor="${need#*.}"; need_minor="${need_minor%%.*}"
  if (( have_major > need_major )); then return 0; fi
  if (( have_major == need_major && have_minor >= need_minor )); then return 0; fi
  return 1
}

# ══════════════════════════════════════════════════════════════════════════════
# 1. Check system prerequisites
# ══════════════════════════════════════════════════════════════════════════════
check_prerequisites() {
  section "1/7  Checking prerequisites"
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
  section "2/7  Installing Python dependencies"

  if $HAS_GPU; then
    info "GPU detected — installing with whisper extra"
    uv sync --extra dev --extra whisper
    summary_add "Python deps installed (dev + whisper)"
  else
    info "CPU mode — installing dev dependencies"
    uv sync --extra dev
    summary_add "Python deps installed (dev)"
  fi
  ok "Python dependencies ready"
}

# ══════════════════════════════════════════════════════════════════════════════
# 3. Install TypeScript dependencies
# ══════════════════════════════════════════════════════════════════════════════
install_ts_deps() {
  section "3/7  Installing TypeScript dependencies"

  if [[ -d "$REPO_ROOT/g2_app" ]]; then
    info "Installing g2_app dependencies..."
    (cd "$REPO_ROOT/g2_app" && npm install)
    ok "g2_app npm install"
    summary_add "g2_app: npm packages installed"
  else
    warn "g2_app/ directory not found — skipping"
  fi

  if [[ -d "$REPO_ROOT/copilot_bridge" ]]; then
    info "Installing copilot_bridge dependencies..."
    (cd "$REPO_ROOT/copilot_bridge" && npm install)
    ok "copilot_bridge npm install"
    summary_add "copilot_bridge: npm packages installed"
  else
    warn "copilot_bridge/ directory not found — skipping"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 4. Generate environment config
# ══════════════════════════════════════════════════════════════════════════════
generate_env() {
  section "4/7  Generating environment config"

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

  # copilot_bridge .env
  if [[ -d "$REPO_ROOT/copilot_bridge" ]]; then
    if [[ -f "$REPO_ROOT/copilot_bridge/.env" ]]; then
      warn "copilot_bridge/.env already exists — keeping"
      summary_add "copilot_bridge/.env: already existed (kept)"
    elif [[ -f "$REPO_ROOT/copilot_bridge/.env.example" ]]; then
      cp "$REPO_ROOT/copilot_bridge/.env.example" "$REPO_ROOT/copilot_bridge/.env"
      ok "Copied copilot_bridge/.env.example → .env"
      summary_add "copilot_bridge/.env: created from example"
    else
      warn "No copilot_bridge/.env.example found — skipping"
    fi
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 5. Install pre-commit hooks
# ══════════════════════════════════════════════════════════════════════════════
install_precommit() {
  section "5/7  Installing pre-commit hooks"

  if [[ -f "$REPO_ROOT/.pre-commit-config.yaml" ]]; then
    uv run pre-commit install
    ok "pre-commit hooks installed"
    summary_add "pre-commit hooks installed"
  else
    warn ".pre-commit-config.yaml not found — skipping"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 6. Install optional global tools
# ══════════════════════════════════════════════════════════════════════════════
install_optional_tools() {
  section "6/7  Optional global tools"

  if $SKIP_OPTIONAL; then
    info "Skipping optional tools (--skip-optional)"
    return
  fi

  if prompt_yn "Install @evenrealities/evenhub-simulator globally?"; then
    npm i -g @evenrealities/evenhub-simulator
    ok "evenhub-simulator installed"
    summary_add "Installed evenhub-simulator (global)"
  else
    info "Skipped evenhub-simulator"
  fi

  if prompt_yn "Install @evenrealities/evenhub-cli globally?"; then
    npm i -g @evenrealities/evenhub-cli
    ok "evenhub-cli installed"
    summary_add "Installed evenhub-cli (global)"
  else
    info "Skipped evenhub-cli"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# 7. Run smoke tests
# ══════════════════════════════════════════════════════════════════════════════
run_smoke_tests() {
  section "7/7  Running smoke tests"

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
  echo -e "    ${BLUE}1.${RESET} Edit ${BOLD}.env${RESET} — set GATEWAY_TOKEN and review Whisper settings"
  echo -e "    ${BLUE}2.${RESET} Edit ${BOLD}copilot_bridge/.env${RESET} — configure BYOK or GitHub Copilot token"
  echo -e "    ${BLUE}3.${RESET} Start OpenClaw:  ${DIM}openclaw${RESET}"
  echo -e "    ${BLUE}4.${RESET} Launch gateway:  ${DIM}uv run python -m gateway launch${RESET}"
  echo -e "    ${BLUE}5.${RESET} Start G2 app:    ${DIM}cd g2_app && npm run dev${RESET}"
  echo ""
  echo -e "  ${DIM}Docs: docs/guides/getting-started.md${RESET}"
  echo -e "  ${DIM}Re-run this script any time — it's idempotent.${RESET}"
  echo ""
}

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
main() {
  echo ""
  echo -e "${BOLD}🦀 G2 OpenClaw Bootstrap${RESET}"
  echo -e "${DIM}   Repo: $REPO_ROOT${RESET}"
  echo ""

  check_prerequisites
  install_python_deps
  install_ts_deps
  generate_env
  install_precommit
  install_optional_tools
  run_smoke_tests
  print_summary
}

main
