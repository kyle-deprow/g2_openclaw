#!/usr/bin/env bash

set -euo pipefail

export XDG_RUNTIME_DIR=/run/user/1000

readonly HEALTH_URL="http://127.0.0.1:18789/health"
readonly CODEX_HOME="/home/dev/.openclaw/agents/autoresearch-pm/agent/codex-home"
readonly LOG_DB="${CODEX_HOME}/logs_2.sqlite"
readonly LOG_DB_SHM="${CODEX_HOME}/logs_2.sqlite-shm"
readonly LOG_DB_WAL="${CODEX_HOME}/logs_2.sqlite-wal"
readonly ARCHIVE_DIR="/home/dev/.openclaw/agents/autoresearch-pm/agent/codex-home/.archived-logs"
readonly MAX_LOG_DB_BYTES=$((256 * 1024 * 1024))
readonly LOCK_FILE="${XDG_RUNTIME_DIR}/openclaw-gateway-healthcheck.lock"

maintenance_lock_fd=""

log_event() {
  logger -t openclaw-healthcheck "$*" >/dev/null 2>&1 || true
}

rotate_logs_while_gateway_stopped() {
  local archive_timestamp source source_name
  local -a log_files=("$LOG_DB" "$LOG_DB_SHM" "$LOG_DB_WAL")

  if ! mkdir -m 700 -p -- "$ARCHIVE_DIR"; then
    log_event "could not create log archive directory: ${ARCHIVE_DIR}"
    return 0
  fi

  if ! archive_timestamp="$(date -u +%Y%m%dT%H%M%S%NZ 2>/dev/null)"; then
    archive_timestamp="unknown-$$"
  fi

  for source in "${log_files[@]}"; do
    if [[ -e "$source" ]]; then
      source_name="${source##*/}"
      if ! mv -- "$source" "${ARCHIVE_DIR}/${source_name}.${archive_timestamp}"; then
        log_event "could not archive log file: ${source}"
      fi
    fi
  done
}

acquire_maintenance_lock() {
  local lock_fd

  if ! exec {lock_fd}>"$LOCK_FILE"; then
    log_event "could not open healthcheck lock: ${LOCK_FILE}"
    return 1
  fi
  if ! flock -n "$lock_fd" 2>/dev/null; then
    log_event "another healthcheck owns the maintenance lock"
    exec {lock_fd}>&- || true
    return 1
  fi
  maintenance_lock_fd="$lock_fd"
}

release_maintenance_lock() {
  if [[ -n "$maintenance_lock_fd" ]]; then
    exec {maintenance_lock_fd}>&- || true
    maintenance_lock_fd=""
  fi
}

rotate_logs_requested=0
if (( $# > 0 )); then
  if [[ $# -eq 1 && "$1" == "--rotate-logs" ]]; then
    rotate_logs_requested=1
  else
    log_event "ignoring unsupported healthcheck arguments"
    exit 0
  fi
fi

log_db_size_text=""
log_db_bytes=0
if log_db_size_text="$(stat -c '%s' -- "$LOG_DB" 2>/dev/null)" &&
  [[ "$log_db_size_text" =~ ^[0-9]+$ ]]; then
  log_db_bytes="$log_db_size_text"
else
  log_event "could not determine log database size: ${LOG_DB}"
fi

if (( rotate_logs_requested )); then
  if ! acquire_maintenance_lock; then
    exit 0
  fi
  systemctl --user stop quantipy-autoresearch-supervisor.service
  systemctl --user stop openclaw-gateway.service
  if (( log_db_bytes > MAX_LOG_DB_BYTES )); then
    rotate_logs_while_gateway_stopped
  else
    log_event "log database is not over the rotation threshold"
  fi
  systemctl --user start openclaw-gateway.service
  systemctl --user start quantipy-autoresearch-supervisor.service
  release_maintenance_lock
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
  if ! acquire_maintenance_lock; then
    exit 0
  fi
  if (( log_db_bytes > MAX_LOG_DB_BYTES )); then
    systemctl --user stop quantipy-autoresearch-supervisor.service
    systemctl --user stop openclaw-gateway.service
    rotate_logs_while_gateway_stopped
  fi
  systemctl --user restart openclaw-gateway.service
  sleep 10 || true
  systemctl --user start quantipy-autoresearch-supervisor.service
  release_maintenance_lock
fi

exit 0
