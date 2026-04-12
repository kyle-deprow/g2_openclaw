#!/usr/bin/env bash
# run-in-scope.sh — Run a command inside a systemd transient service
#
# Provides automatic resource limits and guaranteed child cleanup.
# When the main process exits (normally, crash, or OOM), systemd kills
# ALL child processes in the cgroup via KillMode=control-group. Zero orphans.
#
# Usage:
#   scripts/run-in-scope.sh [OPTIONS] -- COMMAND [ARGS...]
#
# Options:
#   --mem LIMIT    Memory limit (default: 40G)
#   --cpu QUOTA    CPU quota as percentage (default: 800%, i.e. 8 cores)
#   --timeout SEC  Wall-clock timeout in seconds (default: 3600)
#   --name NAME    Unit name suffix (default: auto-generated from timestamp)
#
# Examples:
#   scripts/run-in-scope.sh -- uv run jupyter nbconvert --execute notebook.ipynb
#   scripts/run-in-scope.sh --mem 20G --timeout 1800 -- copilot agent --local --agent main
#   scripts/run-in-scope.sh --name vlc-experiment -- uv run pytest tests/ -v

set -uo pipefail

MEM_LIMIT="40G"
CPU_QUOTA="800%"
TIMEOUT_SEC=3600
UNIT_NAME=""

# Parse options
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mem)    MEM_LIMIT="$2"; shift 2 ;;
    --cpu)    CPU_QUOTA="$2"; shift 2 ;;
    --timeout) TIMEOUT_SEC="$2"; shift 2 ;;
    --name)   UNIT_NAME="$2"; shift 2 ;;
    --)       shift; break ;;
    *)        break ;;
  esac
done

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 [OPTIONS] -- COMMAND [ARGS...]" >&2
  exit 1
fi

[[ "$TIMEOUT_SEC" =~ ^[0-9]+$ ]] || { echo "ERROR: --timeout must be numeric" >&2; exit 1; }

# Generate unit name if not provided
if [[ -z "$UNIT_NAME" ]]; then
  UNIT_NAME="scope-$(date +%s)-$$"
fi

# Validate systemd-run is available
if ! command -v systemd-run &>/dev/null; then
  echo "WARN: systemd-run not available — falling back to direct execution" >&2
  exec timeout "${TIMEOUT_SEC}s" "$@"
fi

# Validate user session is responsive (degraded is ok — a failed unit doesn't block scopes)
if ! systemctl --user list-units --no-pager &>/dev/null; then
  echo "WARN: systemd user session not responsive — falling back to direct execution" >&2
  exec timeout "${TIMEOUT_SEC}s" "$@"
fi

# Use --wait (service mode): when the main process exits, systemd sends
# KillSignal to ALL remaining children in the cgroup. --collect auto-unloads
# the transient unit after exit. This guarantees zero orphans.
exec timeout "${TIMEOUT_SEC}s" systemd-run \
  --user \
  --wait \
  --collect \
  --unit="$UNIT_NAME" \
  -p Type=exec \
  -p MemoryMax="$MEM_LIMIT" \
  -p CPUQuota="$CPU_QUOTA" \
  -p KillMode=control-group \
  -- "$@"
