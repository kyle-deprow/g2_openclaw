#!/usr/bin/env bash

set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $# -ge 3 ]] || die "worker requires run directory, startup marker, and command"

run_dir="$1"
startup_marker_file="$2"
shift 2

[[ "$run_dir" = /* ]] || die "worker run directory must be absolute"
[[ -n "$startup_marker_file" ]] || die "worker startup marker is required"
[[ $# -gt 0 ]] || die "worker command is required"

pid_file="${run_dir}/pid"
started_at_file="${run_dir}/started_at"
exit_code_file="${run_dir}/exit_code"
status_file="${run_dir}/status.json"
stdout_file="${run_dir}/stdout.log"
stderr_file="${run_dir}/stderr.log"

atomic_write() {
  local target_file="$1"
  local tmp_file
  tmp_file="$(mktemp "${run_dir}/.$(basename "$target_file").tmp.XXXXXX")"
  cat >"$tmp_file"
  mv -f -- "$tmp_file" "$target_file"
}

write_status() {
  local state="$1"
  local pid="$2"
  local started_at="$3"
  local exit_code_json="$4"
  atomic_write "$status_file" <<JSON
{"status":"${state}","pid":${pid},"started_at":"${started_at}","exit_code":${exit_code_json}}
JSON
}

publish_startup() {
  local pid="$1"
  local started_at="$2"
  atomic_write "$startup_marker_file" <<JSON
{"status":"running","pid":${pid},"started_at":"${started_at}"}
JSON
}

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"$@" >"$stdout_file" 2>"$stderr_file" &
child_pid=$!

printf '%s\n' "$child_pid" | atomic_write "$pid_file"
printf '%s\n' "$started_at" | atomic_write "$started_at_file"
write_status "running" "$child_pid" "$started_at" "null"
publish_startup "$child_pid" "$started_at"

set +e
wait "$child_pid"
child_exit_code=$?
set -e

printf '%s\n' "$child_exit_code" | atomic_write "$exit_code_file"

if [[ "$child_exit_code" -eq 0 ]]; then
  terminal_state="succeeded"
else
  terminal_state="failed"
fi

write_status "$terminal_state" "$child_pid" "$started_at" "$child_exit_code"
