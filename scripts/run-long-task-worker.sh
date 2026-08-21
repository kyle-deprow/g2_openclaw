#!/usr/bin/env bash

set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $# -eq 5 ]] || die "worker requires run directory, runs root, startup marker, unit name, and identity JSON"
run_dir="$1"
runs_root="$2"
startup_marker_file="$3"
unit_name="$4"
identity_json="$5"
[[ "$run_dir" = /* && "$runs_root" = /* ]] || die "worker paths must be absolute"

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Direct venv argv matches the launcher: no uv cache or repo .venv writes.
runtime_python=("${repo_root}/.venv/bin/python" -m gateway.autoresearch_runs)
[[ -x "${runtime_python[0]}" ]] || die "repo venv python is missing: ${runtime_python[0]}"
"${runtime_python[@]}" validate-prepared-identity \
  --run-dir "$run_dir" --runs-root "$runs_root" --identity-json "$identity_json" ||
  die "prepared run identity validation failed"
supervisor_pid=""
monitor_pid=""
timeout_pid=""
stdout_capture_pid=""
stderr_capture_pid=""
termination_requested=0
capture_incomplete=0
timeout_marker_file="${run_dir}/.timeout-fired"
operator_stop_marker_file="${run_dir}/.operator-stop-fired"
stdout_capture_fifo="${run_dir}/.stdout.capture.pipe"
stderr_capture_fifo="${run_dir}/.stderr.capture.pipe"
capture_completion_marker="${run_dir}/.capture-completion"
timeout_grace_seconds="${AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS:-10}"
readonly capture_incomplete_exit_code=3

[[ "$startup_marker_file" == "${run_dir}/.startup-published.json" ]] ||
  die "startup marker must use the fixed run-local path"
"${runtime_python[@]}" prepare-output-capture --run-dir "$run_dir" --runs-root "$runs_root"

cleanup_capture_fifos() {
  rm -f -- "$stdout_capture_fifo" "$stderr_capture_fifo"
  rm -f -- "$capture_completion_marker"
}

trap cleanup_capture_fifos EXIT

for capture_fifo in "$stdout_capture_fifo" "$stderr_capture_fifo"; do
  [[ ! -L "$capture_fifo" && ! -e "$capture_fifo" ]] || die "capture fifo already exists: $capture_fifo"
  umask 077
  mkfifo -m 600 -- "$capture_fifo"
done

(
  trap '' TERM INT HUP
  exec "${runtime_python[@]}" capture-output-stream \
    --run-dir "$run_dir" --runs-root "$runs_root" --stream stdout
) <"$stdout_capture_fifo" &
stdout_capture_pid=$!
(
  trap '' TERM INT HUP
  exec "${runtime_python[@]}" capture-output-stream \
    --run-dir "$run_dir" --runs-root "$runs_root" --stream stderr
) <"$stderr_capture_fifo" &
stderr_capture_pid=$!

peak_rss_bytes() {
  local peak
  peak="$(systemctl --user show "$unit_name" --no-pager --property=MemoryPeak --value 2>/dev/null || true)"
  if [[ "$peak" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$peak"
  fi
}

publish_heartbeat() {
  local peak
  peak="$(peak_rss_bytes)"
  local heartbeat_args=(heartbeat --run-dir "$run_dir" --runs-root "$runs_root")
  if [[ -n "$peak" ]]; then
    heartbeat_args+=(--peak-rss-bytes "$peak")
  fi
  "${runtime_python[@]}" "${heartbeat_args[@]}" >/dev/null 2>&1 || true
}

handle_termination() {
  termination_requested=1
  umask 077
  : > "$operator_stop_marker_file"
}

trap handle_termination TERM INT HUP

timeout_seconds="$(python3 -c 'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8"))["timeout_seconds"]; print("" if value is None else value)' "${run_dir}/manifest.json")"

(
  trap '' TERM INT HUP
  exec "${runtime_python[@]}" supervise-command \
    --run-dir "$run_dir" \
    --runs-root "$runs_root" \
    --systemd-unit "$unit_name" \
    --termination-grace-seconds "$timeout_grace_seconds" \
    3>"$stdout_capture_fifo" 4>"$stderr_capture_fifo"
) &
supervisor_pid=$!

for _ in $(seq 1 100); do
  if [[ -s "$startup_marker_file" ]]; then
    break
  fi
  kill -0 "$supervisor_pid" 2>/dev/null ||
    die "command supervisor exited before publishing startup"
  sleep 0.05 || true
done
[[ -s "$startup_marker_file" ]] || die "command supervisor did not publish startup in time"

if [[ -n "$timeout_seconds" ]]; then
  (
    sleep "$timeout_seconds"
    if kill -0 "$supervisor_pid" 2>/dev/null; then
      umask 077
      : > "$timeout_marker_file"
    fi
  ) &
  timeout_pid=$!
fi

while kill -0 "$supervisor_pid" 2>/dev/null; do
  publish_heartbeat
  sleep 1
done &
monitor_pid=$!

# A trapped signal interrupts bash's wait before the supervisor necessarily exits.
while :; do
  set +e
  wait "$supervisor_pid"
  supervisor_exit_code=$?
  set -e
  if (( termination_requested && supervisor_exit_code >= 128 )); then
    set +e
    wait "$supervisor_pid"
    resumed_wait_exit_code=$?
    set -e
    if (( resumed_wait_exit_code != 127 )); then
      supervisor_exit_code=$resumed_wait_exit_code
    fi
  fi
  if kill -0 "$supervisor_pid" 2>/dev/null; then
    continue
  fi
  break
done

(( supervisor_exit_code == 0 )) || die "identity-bound command supervisor failed"

flush_output_capture() {
  local stdout_capture_exit_code=0 stderr_capture_exit_code=0
  set +e
  wait "$stdout_capture_pid"
  stdout_capture_exit_code=$?
  wait "$stderr_capture_pid"
  stderr_capture_exit_code=$?
  set -e
  for capture_exit_code in "$stdout_capture_exit_code" "$stderr_capture_exit_code"; do
    if (( capture_exit_code == capture_incomplete_exit_code )); then
      capture_incomplete=1
    elif (( capture_exit_code != 0 )); then
      die "output capture failed while draining child streams"
    fi
  done
  if (( capture_incomplete )); then
    printf 'ERROR: output capture reached its bounded close deadline before FIFO EOF\n' >&2
  fi
}

flush_output_capture

if [[ -n "$timeout_pid" ]]; then
  kill "$timeout_pid" 2>/dev/null || true
  wait "$timeout_pid" 2>/dev/null || true
fi
kill "$monitor_pid" 2>/dev/null || true
wait "$monitor_pid" 2>/dev/null || true
trap '' TERM INT HUP

mapfile -t supervised_result < <(
  "${runtime_python[@]}" consume-supervised-command-result \
    --run-dir "$run_dir" --runs-root "$runs_root"
)
[[ ${#supervised_result[@]} -eq 2 ]] || die "supervised command result is incomplete"
child_exit_code="${supervised_result[0]}"
signal_number="${supervised_result[1]}"
archival_failed=0
archival_diagnostic=""
if [[ -f "$timeout_marker_file" ]]; then
  set +e
  archival_diagnostic="$("${runtime_python[@]}" archive-timeout-partial-run \
    --run-dir "$run_dir" --runs-root "$runs_root" 2>&1 >/dev/null)"
  archival_exit_code=$?
  set -e
  if (( archival_exit_code != 0 )); then
    archival_failed=1
  fi
fi
complete_args=(complete --run-dir "$run_dir" --runs-root "$runs_root" --exit-code "$child_exit_code")
peak="$(peak_rss_bytes)"
if [[ -n "$peak" ]]; then
  complete_args+=(--peak-rss-bytes "$peak")
fi
if [[ -n "$signal_number" ]]; then
  complete_args+=(--signal-number "$signal_number")
fi
if [[ -f "$timeout_marker_file" ]]; then
  complete_args+=(--timed-out)
elif (( termination_requested )) || [[ -f "$operator_stop_marker_file" ]]; then
  complete_args+=(--operator-stopped)
elif [[ "$(systemctl --user show "$unit_name" --no-pager --property=Result --value 2>/dev/null || true)" == "oom-kill" ]]; then
  complete_args+=(--resource-exhausted)
elif (( capture_incomplete )); then
  complete_args+=(--output-capture-failed)
fi
rm -f -- "$timeout_marker_file"
rm -f -- "$operator_stop_marker_file"
cleanup_capture_fifos
trap - EXIT
set +e
"${runtime_python[@]}" "${complete_args[@]}" >/dev/null
complete_exit_code=$?
set -e
if (( archival_failed )); then
  if [[ -n "$archival_diagnostic" ]]; then
    printf 'ERROR: timed-out partial-run archival failed: %s\n' "$archival_diagnostic" >&2
  else
    printf 'ERROR: timed-out partial-run archival failed with exit code %s\n' \
      "$archival_exit_code" >&2
  fi
fi
if (( complete_exit_code != 0 )); then
  exit "$complete_exit_code"
fi
if (( archival_failed )); then
  exit 1
fi
exit 0
