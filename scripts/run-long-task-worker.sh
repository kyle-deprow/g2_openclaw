#!/usr/bin/env bash

set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $# -eq 4 ]] || die "worker requires run directory, runs root, startup marker, and unit name"
run_dir="$1"
runs_root="$2"
startup_marker_file="$3"
unit_name="$4"
[[ "$run_dir" = /* && "$runs_root" = /* ]] || die "worker paths must be absolute"

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_python=(uv run --project "$repo_root" --directory "$repo_root" python -m gateway.autoresearch_runs)
child_pid=""
monitor_pid=""
timeout_pid=""
termination_requested=0
timeout_marker_file="${run_dir}/.timeout-fired"
operator_stop_marker_file="${run_dir}/.operator-stop-fired"
timeout_grace_seconds="${AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS:-10}"

mapfile -d '' -t command < <(
  "${runtime_python[@]}" consume-command-handoff --run-dir "$run_dir" --runs-root "$runs_root"
)
[[ ${#command[@]} -gt 0 ]] || die "protected command handoff was empty"

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

terminate_child_bounded() {
  [[ -n "$child_pid" ]] || return 0
  kill -TERM "$child_pid" 2>/dev/null || true
  deadline="$(python3 -c 'import sys, time; print(time.monotonic() + float(sys.argv[1]))' "$timeout_grace_seconds")"
  while kill -0 "$child_pid" 2>/dev/null; do
    if python3 -c 'import sys, time; raise SystemExit(0 if time.monotonic() >= float(sys.argv[1]) else 1)' "$deadline"; then
      kill -KILL "$child_pid" 2>/dev/null || true
      break
    fi
    sleep 0.05
  done
}

handle_termination() {
  termination_requested=1
  umask 077
  : > "$operator_stop_marker_file"
  if [[ -n "$child_pid" ]]; then
    terminate_child_bounded
  fi
}

trap handle_termination TERM INT HUP

timeout_seconds="$(python3 -c 'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8"))["timeout_seconds"]; print("" if value is None else value)' "${run_dir}/manifest.json")"

"${command[@]}" </dev/null >/dev/null 2>&1 &
child_pid=$!
"${runtime_python[@]}" start \
  --run-dir "$run_dir" \
  --runs-root "$runs_root" \
  --pid "$child_pid" \
  --systemd-unit "$unit_name" >/dev/null
cp -- "${run_dir}/status.json" "$startup_marker_file"

if [[ -n "$timeout_seconds" ]]; then
  (
    sleep "$timeout_seconds"
    if kill -0 "$child_pid" 2>/dev/null; then
      umask 077
      : > "$timeout_marker_file"
      terminate_child_bounded
    fi
  ) &
  timeout_pid=$!
fi

while kill -0 "$child_pid" 2>/dev/null; do
  publish_heartbeat
  sleep 1
done &
monitor_pid=$!

# A trapped signal interrupts bash's wait before the child necessarily exits.
# Keep waiting until the child has been reaped, then stop the heartbeat writer
# before publishing the one terminal record.
while :; do
  set +e
  wait "$child_pid"
  child_exit_code=$?
  set -e
  if kill -0 "$child_pid" 2>/dev/null; then
    continue
  fi
  break
done

if [[ -n "$timeout_pid" ]]; then
  kill "$timeout_pid" 2>/dev/null || true
  wait "$timeout_pid" 2>/dev/null || true
fi
kill "$monitor_pid" 2>/dev/null || true
wait "$monitor_pid" 2>/dev/null || true
trap '' TERM INT HUP

signal_number=""
if (( child_exit_code >= 128 )); then
  signal_number=$((child_exit_code - 128))
fi
artifact_missing=0
expected_artifact_path="$(python3 -c 'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8"))["expected_artifact_path"]; print("" if value is None else value)' "${run_dir}/manifest.json")"
if [[ "$child_exit_code" -eq 0 && -n "$expected_artifact_path" && ! -f "$expected_artifact_path" ]]; then
  artifact_missing=1
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
fi
if (( artifact_missing )); then
  complete_args+=(--artifact-missing)
fi
rm -f -- "$timeout_marker_file"
rm -f -- "$operator_stop_marker_file"
"${runtime_python[@]}" "${complete_args[@]}" >/dev/null
exit 0
