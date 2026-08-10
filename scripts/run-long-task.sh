#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/run-long-task.sh --run-dir ABSOLUTE_DIR --manifest MANIFEST.json [--runs-root ROOT] --command-file COMMAND.json
       scripts/run-long-task.sh --launch-prepared --run-dir ABSOLUTE_DIR [--runs-root ROOT]

The manifest is copied verbatim in canonical JSON form and contains only the
command digest, never command arguments. Production ROOT is fixed at
/home/dev/.openclaw/autoresearch/model-workspaces/long-runs; tests may inject a root explicitly.
The command input must be an already-created one-time 0600 JSON file:
{"command":["program","arg"]}. Create it with gateway-cli
autoresearch-create-command-file. Do not pass secrets in command arguments; use
credential files, env references, or inherited authentication.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

run_dir=""
manifest=""
command_file=""
launch_prepared=0
runs_root="${AUTORESEARCH_RUNS_ROOT:-/home/dev/.openclaw/autoresearch/model-workspaces/long-runs}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      [[ $# -ge 2 ]] || die "--run-dir requires a value"
      run_dir="$2"
      shift 2
      ;;
    --manifest)
      [[ $# -ge 2 ]] || die "--manifest requires a value"
      manifest="$2"
      shift 2
      ;;
    --command-file)
      [[ $# -ge 2 ]] || die "--command-file requires a value"
      command_file="$2"
      shift 2
      ;;
    --launch-prepared)
      launch_prepared=1
      shift
      ;;
    --runs-root)
      [[ $# -ge 2 ]] || die "--runs-root requires a value"
      runs_root="$2"
      shift 2
      ;;
    --) die "positional command payloads are not supported; use --command-file" ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$run_dir" = /* ]] || die "--run-dir must be absolute"
[[ "$runs_root" = /* ]] || die "--runs-root must be absolute"
if [[ "$launch_prepared" -eq 1 ]]; then
  [[ -z "$manifest" ]] || die "--manifest is not valid with --launch-prepared"
  [[ -z "$command_file" ]] || die "--command-file is not valid with --launch-prepared"
  [[ -d "$run_dir" && ! -L "$run_dir" ]] || die "prepared run directory is missing or is a symlink"
  [[ -f "$run_dir/manifest.json" && ! -L "$run_dir/manifest.json" ]] ||
    die "prepared run manifest is missing or is a symlink"
  [[ -f "$run_dir/.command-handoff.json" && ! -L "$run_dir/.command-handoff.json" ]] ||
    die "prepared command handoff is missing or is a symlink"
  [[ ! -e "$run_dir/status.json" && ! -L "$run_dir/status.json" ]] ||
    die "prepared run has already started: status.json exists"
else
  [[ -n "$manifest" ]] || die "--manifest is required"
  [[ -n "$command_file" ]] || { usage; die "--command-file is required"; }
  [[ "$command_file" = /* ]] || die "--command-file must be absolute"
fi
[[ $# -eq 0 ]] || die "unexpected positional arguments; use --command-file"
command -v setsid >/dev/null 2>&1 || die "setsid is required for detached launch"
command -v systemd-run >/dev/null 2>&1 || die "systemd-run is required for isolated detached launch"
command -v timeout >/dev/null 2>&1 || die "timeout is required for launch probing"
command -v systemctl >/dev/null 2>&1 || die "systemctl is required for detached launch validation"
uv_path="$(command -v uv 2>/dev/null)" || die "uv is required for detached launch"
uv_bin_dir="$(dirname -- "$uv_path")"
transient_path="${PATH}:${uv_bin_dir}"
timeout_term_grace_seconds="${AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS:-10}"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Direct venv argv: sandboxed stage agents cannot write the uv cache or repo
# .venv, so `uv run` fails with EROFS inside the launch sandbox.
runtime_python=("${repo_root}/.venv/bin/python" -m gateway.autoresearch_runs)
[[ -x "${runtime_python[0]}" ]] || die "repo venv python is missing: ${runtime_python[0]}"

if [[ -L "$run_dir" || ( -n "$manifest" && -L "$manifest" ) || ( -n "$command_file" && -L "$command_file" ) ]]; then
  die "run directory, manifest, and command input must not be symlinks"
fi

umask 077
if [[ "$launch_prepared" -eq 0 ]]; then
  "${runtime_python[@]}" prepare-with-command-file \
    --manifest "$manifest" \
    --run-dir "$run_dir" \
    --runs-root "$runs_root" \
    --command-file "$command_file" || die "immutable manifest and protected command handoff preparation failed"
fi

launch_requests_dir="${AUTORESEARCH_LAUNCH_REQUESTS_DIR:-/home/dev/.openclaw/autoresearch/stage-inbox/launch-requests}"
if [[ "$launch_prepared" -eq 0 ]]; then
  launch_queue_unreachable=0
  if [[ "${AUTORESEARCH_FORCE_LAUNCH_QUEUE:-0}" == "1" ]]; then
    launch_queue_unreachable=1
  elif ! timeout 2s systemd-run --user --collect --quiet -- true >/dev/null 2>&1; then
    launch_queue_unreachable=1
  fi
  if [[ "$launch_queue_unreachable" -eq 1 ]]; then
    [[ ! -L "$launch_requests_dir" ]] || die "launch request inbox must not be a symlink"
    mkdir -m 700 -p -- "$launch_requests_dir" || die "cannot create launch request inbox"
    chmod 700 -- "$launch_requests_dir" || die "cannot secure launch request inbox"
    run_name="$(basename -- "$run_dir")"
    request_name="${run_name}-$(date -u +%Y%m%dT%H%M%S%N)-$$.json"
    request_tmp="${launch_requests_dir}/.${request_name}.tmp"
    request_path="${launch_requests_dir}/${request_name}"
    request_payload="$(python3 - "$run_dir" "$runs_root" <<'PY'
import json
import sys

print(json.dumps({"schema_version": 1, "run_dir": sys.argv[1], "runs_root": sys.argv[2]}, separators=(",", ":")))
PY
)" || die "cannot encode launch request"
    printf '%s\n' "$request_payload" >"$request_tmp" || die "cannot write launch request"
    chmod 600 -- "$request_tmp" || die "cannot secure launch request"
    mv -- "$request_tmp" "$request_path" || die "cannot publish launch request"
    printf 'LAUNCH_QUEUED: %s\n' "$run_dir"
    exit 0
  fi
fi

working_directory="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["working_directory"])' "${run_dir}/manifest.json")"

readonly startup_marker_file="${run_dir}/.startup-published.json"
readonly command_handoff_file="${run_dir}/.command-handoff.json"
launch_enqueued=0
unit_name="openclaw-long-task-$(date +%s%N)-$$.service"
worker_script="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/run-long-task-worker.sh"
[[ -x "$worker_script" ]] || die "long-task worker is missing or not executable: $worker_script"

cleanup_startup_marker() {
  rm -f -- "$startup_marker_file"
  if [[ "$launch_enqueued" -eq 1 ]]; then
    rm -f -- "$command_handoff_file"
  fi
}

trap cleanup_startup_marker EXIT

unit_start_failed() {
  local properties load_state="" active_state="" result="" property value
  properties="$(systemctl --user show "$unit_name" --no-pager \
    --property=LoadState --property=ActiveState --property=Result 2>/dev/null)" || return 1
  while IFS='=' read -r property value; do
    case "$property" in
      LoadState) load_state="$value" ;;
      ActiveState) active_state="$value" ;;
      Result) result="$value" ;;
    esac
  done <<<"$properties"
  [[ "$load_state" == "not-found" || "$active_state" == "failed" || "$result" == "failed" ]]
}

stop_transient_unit() {
  systemctl --user stop "$unit_name" >/dev/null 2>&1 || true
}

# Torch/CUDA host-side allocations add pressure. MemoryHigh is a reclaim
# throttle and MemoryMax is the kill point. Raising only the throttle to 10G
# gives headroom without changing host-OOM arithmetic: gateway MemoryMax=10G
# + long task 12G = 22G against 30.25 GiB of RAM. Do not raise MemoryMax; a
# prior combined-ceiling change created a host-OOM path and was reverted.
if ! setsid systemd-run \
  --user --no-block --collect --unit="$unit_name" --service-type=exec \
  --setenv=PATH="$transient_path" --working-directory="$working_directory" \
  --setenv=AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS="$timeout_term_grace_seconds" \
  --property=MemoryHigh=10G --property=MemoryMax=12G --property=KillMode=control-group \
  -- "$worker_script" "$run_dir" "$runs_root" "$startup_marker_file" "$unit_name" \
  </dev/null >/dev/null 2>&1; then
  die "detached systemd unit could not be enqueued: $unit_name"
fi
launch_enqueued=1

for _ in $(seq 1 40); do
  if [[ -s "$startup_marker_file" ]]; then
    if "${runtime_python[@]}" validate-startup \
      --run-dir "$run_dir" --runs-root "$runs_root" --marker "$startup_marker_file" \
      >/dev/null 2>&1; then
      exit 0
    fi
    stop_transient_unit
    die "detached launch published incoherent startup metadata: $unit_name"
  fi
  if unit_start_failed; then
    stop_transient_unit
    die "detached systemd unit failed before publishing startup metadata: $unit_name"
  fi
  sleep 0.05
done

stop_transient_unit
die "detached launch did not publish startup metadata in time"
