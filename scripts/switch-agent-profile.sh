#!/usr/bin/env bash
# Switch every OpenClaw agent between the codex and claude profiles:
# validate, refuse while research work is in flight, set G2_AGENT_PROFILE in the
# operator env file, run the push script, verify the deployed profile, and roll the
# env file back if the push or the verification fails.
#
# Test/override hooks (defaults are the real binaries and paths):
#   SWITCH_PYTHON, SWITCH_SYSTEMCTL, SWITCH_GATEWAY_CLI, SWITCH_PUSH_SCRIPT
#   OPENCLAW_PUSH_ENV_FILE, OPENCLAW_PUSH_HOME, OPENCLAW_BIN pass through to the push script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON="${SWITCH_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
SYSTEMCTL="${SWITCH_SYSTEMCTL:-systemctl}"
GATEWAY_CLI="${SWITCH_GATEWAY_CLI:-${REPO_ROOT}/.venv/bin/gateway-cli}"
PUSH_SCRIPT="${SWITCH_PUSH_SCRIPT:-${REPO_ROOT}/scripts/push-openclaw-config.sh}"
OWNER_UNIT="research-owner.service"

usage() {
  echo "usage: $(basename "$0") <codex|claude> [--dry-run] [--env-file PATH] [--no-research-root] [--skip-smoke-ack]" >&2
  exit 64
}

PROFILE=""
DRY_RUN=0
SKIP_SMOKE_ACK=0
NO_RESEARCH_ROOT=0
ENV_FILE="${OPENCLAW_PUSH_ENV_FILE:-${REPO_ROOT}/gateway/openclaw_config/.env}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-smoke-ack) SKIP_SMOKE_ACK=1 ;;
    --no-research-root) NO_RESEARCH_ROOT=1 ;;
    --env-file)
      [ "$#" -ge 2 ] || usage
      ENV_FILE="$2"
      shift
      ;;
    -h | --help) usage ;;
    -*) usage ;;
    *)
      [ -z "${PROFILE}" ] || usage
      PROFILE="$1"
      ;;
  esac
  shift
done
[ -n "${PROFILE}" ] || usage

step() { echo "==> $*"; }
refuse() {
  echo "REFUSED: $*" >&2
  exit 1
}

# Every python runs with the working directory off sys.path and the repo importable.
py() { PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}" "${PYTHON}" "$@"; }
sq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

STATE_DIR="${OPENCLAW_PUSH_HOME:-${HOME}/.openclaw}"
case "${STATE_DIR}" in
  "~") STATE_DIR="${HOME}" ;;
  "~/"*) STATE_DIR="${HOME}/${STATE_DIR#"~/"}" ;;
esac
CLAUDE_DIR="${STATE_DIR}/claude-code"

# Resolve the env file to an absolute path (parent resolved, final component kept so a
# symlinked file is still detected).
case "${ENV_FILE}" in /*) ;; *) ENV_FILE="${PWD}/${ENV_FILE}" ;; esac
ENV_DIR="$(cd "$(dirname "${ENV_FILE}")" 2>/dev/null && pwd -P)" ||
  refuse "env file directory for ${ENV_FILE} does not exist."
ENV_FILE="${ENV_DIR}/$(basename "${ENV_FILE}")"

# --- env file helper (python: byte-exact, atomic, same grammar as the push script) -------
read -r -d '' ENV_PY <<'PY' || true
import os
import re
import stat
import sys
import tempfile

KEY = "G2_AGENT_PROFILE"
SELECTED = (KEY, "RESEARCH_V2_ROOT")
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def fail(message):
    print("ERROR: " + message, file=sys.stderr)
    sys.exit(1)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def parse(path):
    """Return {key: value} for SELECTED keys; enforce the push script's data grammar."""
    text = read(path).decode("utf-8")
    found = {}
    for number, line in enumerate(text.split("\n"), start=1):
        where = "%s:%d" % (path, number)
        if "\r" in line:
            fail(where + ": carriage returns are not allowed")
        trimmed = line.lstrip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if re.match(r"export\s", trimmed):
            trimmed = trimmed[len("export"):].lstrip()
        if "=" not in trimmed:
            fail(where + ": expected KEY=VALUE")
        key, _, raw = trimmed.partition("=")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            fail(where + ": invalid environment key")
        if key in ("OPENCLAW_PUSH_MODE", "OPENCLAW_PUSH_MODE_REQUESTED"):
            fail(where + ": " + key + " must not be set in the env file")
        if key not in SELECTED:
            continue
        if key in found:
            fail(where + ": duplicate key " + key)
        raw = raw.lstrip()
        if raw[:1] in ("'", '"'):
            if len(raw) < 2 or raw[-1] != raw[0]:
                fail(where + ": unterminated quoted value for " + key)
            value = raw[1:-1]
        else:
            value = raw.rstrip()
        if any(ch in value for ch in "\n\r\t"):
            fail(where + ": control characters are not allowed for " + key)
        found[key] = value
    return found


def fsync_dir(path):
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, data, mode):
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


mode, path = sys.argv[1], sys.argv[2]
if mode == "validate":
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        fail(path + " does not exist")
    if stat.S_ISLNK(st.st_mode):
        fail(path + " is a symlink")
    if not stat.S_ISREG(st.st_mode):
        fail(path + " is not a regular file")
    if st.st_uid != os.geteuid():
        fail(path + " is not owned by the invoking user")
    if st.st_mode & 0o077:
        fail(path + " is accessible by group or others (mode must have no group/other bits)")
    if b"\r" in read(path):
        fail(path + " contains carriage returns")
    parse(path)
elif mode == "get":
    sys.stdout.write(parse(path).get(sys.argv[3], ""))
elif mode == "set":
    pattern = re.compile(rb"^[ \t]*(?:export\s+)?" + KEY.encode() + rb"[ \t]*=")
    data = read(path)
    st_mode = os.stat(path).st_mode & 0o777
    lines = data.split(b"\n")
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(hits) > 1:
        fail("%s has %d %s lines; fix it by hand" % (path, len(hits), KEY))
    new = ("%s=%s" % (KEY, sys.argv[3])).encode()
    if hits:
        lines[hits[0]] = new
        out = b"\n".join(lines)
    else:
        out = data
        if out and not out.endswith(b"\n"):
            out += b"\n"
        out += new + b"\n"
    write_atomic(path, out, st_mode)
elif mode == "restore":
    backup = sys.argv[3]
    write_atomic(path, read(backup), os.stat(backup).st_mode & 0o777)
else:
    fail("unknown mode " + mode)
PY
env_py() { py -c "${ENV_PY}" "$@"; }

step "validate profile ${PROFILE}"
if ! py -m gateway.agent_profiles show --profile "${PROFILE}" >/dev/null; then
  echo "ERROR: unknown or invalid profile '${PROFILE}'" >&2
  exit 1
fi

step "validate env file ${ENV_FILE}"
env_py validate "${ENV_FILE}" || refuse "env file ${ENV_FILE} failed validation."
if ! env -u G2_AGENT_PROFILE PYTHONSAFEPATH=1 PYTHONPATH="${REPO_ROOT}" \
  "${PYTHON}" -m gateway.agent_profiles resolve --env-file "${ENV_FILE}" >/dev/null; then
  refuse "env file ${ENV_FILE} does not resolve to a valid profile."
fi

RESEARCH_ROOT="${RESEARCH_V2_ROOT:-$(env_py get "${ENV_FILE}" RESEARCH_V2_ROOT)}"
RESEARCH_ROOT="${RESEARCH_ROOT:-${STATE_DIR}/research-v2}"

INSTALL_ARGS=(-m gateway.deployment.claude_runtime install --state-dir "${STATE_DIR}" --profile claude
  --var "HOME=${HOME}" --var "OPENCLAW_STATE_DIR=${STATE_DIR}"
  --var "RESEARCH_V2_ROOT=${RESEARCH_ROOT}"
  --var "HYPOTHESIS_WORKTREES_ROOT=${RESEARCH_ROOT}/hypothesis-worktrees"
  --var "REPO_ROOT=${REPO_ROOT}" --var "OPERATOR_ENV_FILE=${ENV_FILE}")

SMOKE_PROMPT="In the current directory write a file named smoke-ok.txt containing ok. Then try to write the file ${STATE_DIR}/openclaw.json and report that it was refused. Then run: ${REPO_ROOT}/.venv/bin/gateway-cli research --help and report its first line."
printf -v Q_CLAUDE_DIR '%q' "${CLAUDE_DIR}"
printf -v Q_SETTINGS '%q' "${CLAUDE_DIR}/settings.json"
SMOKE_CMD="CLAUDE_CONFIG_DIR=${Q_CLAUDE_DIR} claude -p --settings ${Q_SETTINGS} --permission-mode acceptEdits $(sq "${SMOKE_PROMPT}")"

# A review reservation requires an IMPLEMENTED attempt (store.py: "review reservation
# requires an implemented attempt"), so IMPLEMENTED covers reserved-but-uncollected reviews.
blocking_attempts() {
  py -c '
import json, sys
data = json.load(sys.stdin)
for h in data["hypotheses"]:
    for a in h["attempts"]:
        if a["state"] == "IMPLEMENTED":
            print(str(h["hypothesis_id"]) + "/" + str(a["attempt_id"]) + " (IMPLEMENTED, possibly review-reserved)")
'
}

check_owner_stopped() {
  step "check ${OWNER_UNIT} is stopped"
  local state
  state="$("${SYSTEMCTL}" --user show -p ActiveState --value "${OWNER_UNIT}")" ||
    refuse "cannot read ${OWNER_UNIT} state."
  case "${state}" in
    inactive | failed) ;;
    *) refuse "${OWNER_UNIT} is ${state}; stop it first (systemctl --user stop ${OWNER_UNIT})." ;;
  esac
}

check_research_root() {
  if [ "${NO_RESEARCH_ROOT}" -eq 1 ]; then
    step "skip research root check (--no-research-root)"
    return 0
  fi
  step "check research root ${RESEARCH_ROOT}"
  [ -d "${RESEARCH_ROOT}" ] ||
    refuse "research root ${RESEARCH_ROOT} does not exist (pass --no-research-root to skip the check)."
  [ -f "${RESEARCH_ROOT}/state.sqlite3" ] ||
    refuse "research root ${RESEARCH_ROOT} has no state.sqlite3 (status would create a store); pass --no-research-root if no research store exists."
  local status_json blockers
  status_json="$(env -u FORCE_COLOR "${GATEWAY_CLI}" research status --root "${RESEARCH_ROOT}" --json)" ||
    refuse "cannot read research status for ${RESEARCH_ROOT}; resolve that before switching."
  blockers="$(printf '%s' "${status_json}" | blocking_attempts)" ||
    refuse "research status for ${RESEARCH_ROOT} is not the expected JSON."
  if [ -n "${blockers}" ]; then
    refuse "in-flight research attempt(s) block the switch: ${blockers//$'\n'/, }. Finish or collect them first."
  fi
}

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "DRY RUN: nothing will be changed. Read-only checks follow, then the plan for ${PROFILE}."
  check_owner_stopped
  check_research_root
  if [ "${PROFILE}" = "claude" ]; then
    step "would install dedicated Claude settings: ${PYTHON} ${INSTALL_ARGS[*]}"
    step "would run ${PYTHON} -m gateway.deployment.claude_doctor check --state-dir ${STATE_DIR}"
    if [ "${SKIP_SMOKE_ACK}" -eq 1 ]; then
      step "sandbox smoke acknowledged (--skip-smoke-ack)"
    else
      step "would require the operator sandbox smoke command (printed on a real run):"
      echo "    ${SMOKE_CMD}"
    fi
  fi
  step "would set G2_AGENT_PROFILE=${PROFILE} in ${ENV_FILE} (atomic; other lines untouched)"
  step "would run ${PUSH_SCRIPT} with G2_AGENT_PROFILE=${PROFILE}"
  step "would verify ${PYTHON} -m gateway.agent_profiles active --json --state-dir ${STATE_DIR} reports ${PROFILE}"
  exit 0
fi

mkdir -p "${STATE_DIR}"
LOCK_FILE="${STATE_DIR}/.switch-agent-profile.lock"
[ ! -L "${LOCK_FILE}" ] || refuse "${LOCK_FILE} is a symlink."
exec 9>>"${LOCK_FILE}"
command -v flock >/dev/null 2>&1 || refuse "flock is required."
flock -n 9 || refuse "another switch-agent-profile run holds ${STATE_DIR}/.switch-agent-profile.lock."

check_owner_stopped
check_research_root

if [ "${PROFILE}" = "claude" ]; then
  step "install dedicated Claude settings"
  if ! py "${INSTALL_ARGS[@]}"; then
    refuse "claude_runtime install failed; fix the error above before switching."
  fi
  step "claude doctor"
  if ! DOCTOR_OUT="$(py -m gateway.deployment.claude_doctor check --state-dir "${STATE_DIR}" 2>&1)"; then
    echo "${DOCTOR_OUT}" >&2
    cat >&2 <<REMEDIATION

The Claude runtime is not ready. Operator remediation (run these yourself, then re-run this script):
  sudo apt install socat
  # AppArmor profile permitting bwrap user namespaces: use the profile from the Claude Code
  # sandbox docs, install it, then:
  sudo systemctl reload apparmor
  CLAUDE_CONFIG_DIR=${Q_CLAUDE_DIR} claude auth login
REMEDIATION
    exit 1
  fi
  echo "${DOCTOR_OUT}"
  if [ "${SKIP_SMOKE_ACK}" -ne 1 ]; then
    cat >&2 <<SMOKE
REFUSED: the one-time sandbox smoke has not been acknowledged.
Run this yourself in a scratch directory and check that the file write succeeds, the
denied path is refused, and the gateway-cli help prints:

  ${SMOKE_CMD}

Then re-run: $(basename "$0") ${PROFILE} --skip-smoke-ack
SMOKE
    exit 1
  fi
fi

BACKUP="$(mktemp "${STATE_DIR}/.switch-env-backup.XXXXXX")"
KEEP_BACKUP=0
cleanup_backup() { [ "${KEEP_BACKUP}" -eq 1 ] || rm -f "${BACKUP}"; }
trap cleanup_backup EXIT
cp -p "${ENV_FILE}" "${BACKUP}"

active_name() {
  local out
  out="$(py -m gateway.agent_profiles active --json --state-dir "${STATE_DIR}" 2>/dev/null)" || return 1
  printf '%s' "${out}" | py -c 'import json,sys; print(json.load(sys.stdin)["name"])'
}

fail_after_edit() {
  local message="$1" deployed
  trap '' INT TERM HUP
  echo "ERROR: ${message}" >&2
  deployed="$(active_name)" || deployed="unknown"
  if [ "${deployed}" = "${PROFILE}" ]; then
    KEEP_BACKUP=1
    echo "The deployed profile is already ${PROFILE}; leaving ${ENV_FILE} as switched (previous contents kept at ${BACKUP})." >&2
  elif env_py restore "${ENV_FILE}" "${BACKUP}"; then
    echo "Restored ${ENV_FILE} to its previous contents. Deployed profile: ${deployed}." >&2
  else
    KEEP_BACKUP=1
    echo "FAILED to restore ${ENV_FILE}; restore it from ${BACKUP}. Deployed profile: ${deployed}." >&2
  fi
  echo "A partial publish may have occurred: re-run ${REPO_ROOT}/scripts/push-openclaw-config.sh to realign the deployed config before starting ${OWNER_UNIT}." >&2
  exit 1
}

step "set G2_AGENT_PROFILE=${PROFILE} in ${ENV_FILE}"
# Arm the rollback before the edit: restoring an unchanged file rewrites the same bytes.
trap 'fail_after_edit "interrupted."' INT TERM HUP
env_py set "${ENV_FILE}" "${PROFILE}"

step "push ${PUSH_SCRIPT}"
if ! G2_AGENT_PROFILE="${PROFILE}" OPENCLAW_PUSH_ENV_FILE="${ENV_FILE}" "${PUSH_SCRIPT}" 9>&-; then
  fail_after_edit "push failed."
fi

step "verify deployed profile"
DEPLOYED="$(active_name)" || fail_after_edit "cannot read the deployed profile after the push."
if [ "${DEPLOYED}" != "${PROFILE}" ]; then
  fail_after_edit "push finished but the deployed profile is ${DEPLOYED}, not ${PROFILE}."
fi
echo "Deployed profile: ${DEPLOYED}"
