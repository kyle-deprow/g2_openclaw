#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/run-long-task.sh --run-dir ABSOLUTE_DIR -- COMMAND [ARGS...]

Starts COMMAND detached and records run metadata in ABSOLUTE_DIR:
  stdout.log
  stderr.log
  pid
  started_at
  exit_code
  status.json
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

RUN_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      [[ $# -ge 2 ]] || die "--run-dir requires a value"
      RUN_DIR="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      die "unexpected positional argument before --: $1"
      ;;
  esac
done

[[ -n "$RUN_DIR" ]] || {
  usage
  die "--run-dir is required"
}
[[ "$RUN_DIR" = /* ]] || die "--run-dir must be an absolute path"
[[ $# -gt 0 ]] || {
  usage
  die "command is required after --"
}
command -v setsid >/dev/null 2>&1 || die "setsid is required for detached launch"
command -v python3 >/dev/null 2>&1 || die "python3 is required for detached status validation"

if [[ -L "$RUN_DIR" ]]; then
  die "--run-dir must not be a symlink"
fi

umask 077
mkdir -p -- "$RUN_DIR"
[[ -d "$RUN_DIR" ]] || die "--run-dir is not a directory: $RUN_DIR"

readonly PID_FILE="${RUN_DIR}/pid"
readonly STARTED_AT_FILE="${RUN_DIR}/started_at"
readonly EXIT_CODE_FILE="${RUN_DIR}/exit_code"
readonly STATUS_FILE="${RUN_DIR}/status.json"
readonly STDOUT_FILE="${RUN_DIR}/stdout.log"
readonly STDERR_FILE="${RUN_DIR}/stderr.log"
readonly STARTUP_MARKER_FILE="${RUN_DIR}/.startup-published.json"

for required_path in \
  "$PID_FILE" \
  "$STARTED_AT_FILE" \
  "$EXIT_CODE_FILE" \
  "$STATUS_FILE" \
  "$STDOUT_FILE" \
  "$STDERR_FILE" \
  "$STARTUP_MARKER_FILE"; do
  [[ ! -e "$required_path" ]] || die "run directory already contains $(basename "$required_path")"
done

: >"$STDOUT_FILE"
: >"$STDERR_FILE"

read -r -d '' WRAPPER_SCRIPT <<'EOF' || true
set -euo pipefail

run_dir="$1"
startup_marker_file="$2"
shift 2

pid_file="${run_dir}/pid"
started_at_file="${run_dir}/started_at"
exit_code_file="${run_dir}/exit_code"
status_file="${run_dir}/status.json"
stdout_file="${run_dir}/stdout.log"
stderr_file="${run_dir}/stderr.log"
startup_marker_path="${startup_marker_file}"

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
  atomic_write "$startup_marker_path" <<JSON
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
EOF

startup_metadata_is_coherent() {
  python3 - "$STARTUP_MARKER_FILE" "$STATUS_FILE" "$PID_FILE" "$STARTED_AT_FILE" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

startup_path = Path(sys.argv[1])
status_path = Path(sys.argv[2])
pid_path = Path(sys.argv[3])
started_at_path = Path(sys.argv[4])


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


startup = load_json(startup_path)
status = load_json(status_path)
pid_text = pid_path.read_text(encoding="utf-8").strip()
started_at_text = started_at_path.read_text(encoding="utf-8").strip()

pid = startup.get("pid")
started_at = startup.get("started_at")

if startup.get("status") != "running":
    raise SystemExit(1)
if not isinstance(pid, int) or pid <= 0:
    raise SystemExit(1)
if not isinstance(started_at, str):
    raise SystemExit(1)
if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", started_at) is None:
    raise SystemExit(1)
if pid_text != str(pid):
    raise SystemExit(1)
if started_at_text != started_at:
    raise SystemExit(1)
if status.get("pid") != pid:
    raise SystemExit(1)
if status.get("started_at") != started_at:
    raise SystemExit(1)
if status.get("status") == "running":
    if status.get("exit_code") is not None:
        raise SystemExit(1)
elif status.get("status") in {"succeeded", "failed"}:
    if not isinstance(status.get("exit_code"), int):
        raise SystemExit(1)
else:
    raise SystemExit(1)
PY
}

cleanup_startup_marker() {
  rm -f -- "$STARTUP_MARKER_FILE"
}

trap cleanup_startup_marker EXIT

setsid bash -lc "$WRAPPER_SCRIPT" bash "$RUN_DIR" "$STARTUP_MARKER_FILE" "$@" </dev/null >/dev/null 2>&1 &
launcher_pid=$!
disown "$launcher_pid" 2>/dev/null || true

for _ in $(seq 1 40); do
  if [[ -s "$STARTUP_MARKER_FILE" ]] && startup_metadata_is_coherent; then
    exit 0
  fi
  if ! kill -0 "$launcher_pid" 2>/dev/null; then
    break
  fi
  sleep 0.05
done

kill -TERM -- "-$launcher_pid" 2>/dev/null || true
die "detached launch did not publish coherent startup metadata in time"
