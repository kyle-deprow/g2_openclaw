#!/usr/bin/env bash

set -euo pipefail

export XDG_RUNTIME_DIR=/run/user/1000

readonly HEALTH_URL="http://127.0.0.1:18789/health"
readonly LOCK_FILE="${XDG_RUNTIME_DIR}/openclaw-gateway-healthcheck.lock"

healthcheck_lock_fd=""

log_event() {
  logger -t openclaw-healthcheck "$*" >/dev/null 2>&1 || true
}

acquire_healthcheck_lock() {
  local lock_fd

  if ! exec {lock_fd}>"$LOCK_FILE"; then
    log_event "could not open healthcheck lock: ${LOCK_FILE}"
    return 1
  fi
  if ! flock -n "$lock_fd" 2>/dev/null; then
    log_event "another healthcheck owns the restart lock"
    exec {lock_fd}>&- || true
    return 1
  fi
  healthcheck_lock_fd="$lock_fd"
}

release_healthcheck_lock() {
  if [[ -n "$healthcheck_lock_fd" ]]; then
    exec {healthcheck_lock_fd}>&- || true
    healthcheck_lock_fd=""
  fi
}

if (( $# > 0 )); then
  log_event "ignoring unsupported healthcheck arguments"
  exit 0
fi

failure_count=0
for attempt in 1 2 3; do
  if timeout 8 curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
    :
  else
    failure_count=$((failure_count + 1))
  fi
  if (( attempt < 3 )); then
    sleep 5 || true
  fi
done

if (( failure_count == 3 )); then
  log_event "gateway health probe failed three times; restarting gateway"
  if ! acquire_healthcheck_lock; then
    exit 0
  fi
  systemctl --user restart openclaw-gateway.service
  sleep 10 || true
  release_healthcheck_lock
fi

exit 0
