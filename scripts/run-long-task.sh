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
command -v systemd-run >/dev/null 2>&1 || die "systemd-run is required for isolated detached launch"

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

working_directory="$PWD"
unit_name="openclaw-long-task-$(date +%s%N)-$$.service"
worker_script="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/run-long-task-worker.sh"
[[ -x "$worker_script" ]] || die "long-task worker is missing or not executable: $worker_script"

setsid systemd-run \
  --user \
  --wait \
  --collect \
  --unit="$unit_name" \
  --service-type=exec \
  --working-directory="$working_directory" \
  --property=MemoryHigh=16G \
  --property=MemoryMax=24G \
  --property=KillMode=control-group \
  -- "$worker_script" "$RUN_DIR" "$STARTUP_MARKER_FILE" "$@" \
  </dev/null >/dev/null 2>&1 &
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
