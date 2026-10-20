from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "switch-agent-profile.sh"

FAKE_PYTHON = f"""#!/usr/bin/env bash
[ "$1" = -m ] && echo "python $*" >> "$FAKE_LOG"
case "$*" in
  "-m gateway.agent_profiles show --profile "*)
    case "$5" in codex|claude) exit 0 ;; *) echo "unknown profile" >&2; exit 2 ;; esac ;;
  "-m gateway.agent_profiles resolve"*)
    [ "${{FAKE_RESOLVE:-ok}}" = ok ] || {{ echo "bad grammar" >&2; exit 2; }}
    echo "${{G2_AGENT_PROFILE:-unset}}" >> "$FAKE_RESOLVE_ENV"; exit 0 ;;
  "-m gateway.deployment.claude_runtime install"*)
    [ "${{FAKE_INSTALL:-ok}}" = ok ] || {{ echo "install exploded" >&2; exit 1; }}
    exit 0 ;;
  "-m gateway.deployment.claude_doctor check"*)
    if [ "${{FAKE_DOCTOR:-ok}}" = ok ]; then echo "[OK] doctor"; exit 0; fi
    echo "[FAIL] claude auth: not logged in"; exit 1 ;;
  "-m gateway.agent_profiles active --json"*)
    [ "${{FAKE_ACTIVE_FAIL:-0}}" = 1 ] && exit 1
    echo "{{\\"name\\": \\"$(cat "$FAKE_DEPLOYED")\\"}}"; exit 0 ;;
esac
exec {sys.executable!r} "$@"
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
[ "${FAKE_OWNER_FAIL:-0}" = 1 ] && exit 1
echo "${FAKE_OWNER_STATE:-inactive}"
"""

FAKE_GATEWAY_CLI = """#!/usr/bin/env bash
echo "gateway-cli $*" >> "$FAKE_LOG"
if [ -n "${FORCE_COLOR:-}" ]; then echo "FORCE_COLOR leaked" >&2; exit 9; fi
[ "${FAKE_CLI_FAIL:-0}" = 1 ] && exit 3
cat "$FAKE_STATUS"
"""

FAKE_PUSH = """#!/usr/bin/env bash
echo "push env=$OPENCLAW_PUSH_ENV_FILE home=${OPENCLAW_PUSH_HOME:-}" \\
  "bin=${OPENCLAW_BIN:-} profile=${G2_AGENT_PROFILE:-unset}" \
  "change=${OPENCLAW_PUSH_PROFILE_CHANGE:-unset}" >> "$FAKE_LOG"
cp "$OPENCLAW_PUSH_ENV_FILE" "$FAKE_PUSH_SNAPSHOT"
case "${FAKE_PUSH:-ok}" in
  fail) exit 1 ;;
  term) kill -TERM $PPID; sleep 1; exit 1 ;;
  term_deployed)
    printf '%s' "$G2_AGENT_PROFILE" > "$FAKE_DEPLOYED"
    kill -TERM $PPID; sleep 1; exit 1 ;;
  stale) exit 0 ;;
esac
printf '%s' "$G2_AGENT_PROFILE" > "$FAKE_DEPLOYED"
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.log = tmp_path / "calls.log"
        self.log.write_text("")
        self.env_file = tmp_path / "op.env"
        self.state = tmp_path / "state"
        self.root = tmp_path / "research-root"
        self.root.mkdir()
        (self.root / "state.sqlite3").write_text("")
        self.status = tmp_path / "status.json"
        self.set_status([])
        self.snapshot = tmp_path / "push-snapshot.env"
        self.deployed = tmp_path / "deployed"
        self.deployed.write_text("codex")
        self.resolve_env = tmp_path / "resolve-env.log"
        self.extra: dict[str, str] = {}
        for name, body in (
            ("python", FAKE_PYTHON),
            ("systemctl", FAKE_SYSTEMCTL),
            ("gateway-cli", FAKE_GATEWAY_CLI),
            ("push", FAKE_PUSH),
        ):
            path = tmp_path / f"fake-{name}"
            path.write_text(body)
            path.chmod(0o755)

    def set_status(self, attempts: list[dict[str, object]]) -> None:
        doc = {"hypotheses": [{"hypothesis_id": "H1", "attempts": attempts}]}
        self.status.write_text(json.dumps(doc))

    def write_env(self, text: str) -> None:
        self.env_file.write_text(text.replace("@ROOT@", str(self.root)))
        self.env_file.chmod(0o600)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def run(
        self,
        *args: str,
        env_arg: str | None = None,
        cwd: Path | None = None,
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        base = {
            k: v for k, v in os.environ.items() if k not in ("RESEARCH_V2_ROOT", "G2_AGENT_PROFILE")
        }
        full = {
            **base,
            "FAKE_DEPLOYED": str(self.deployed),
            "FAKE_RESOLVE_ENV": str(self.resolve_env),
            "SWITCH_PYTHON": str(self.tmp / "fake-python"),
            "SWITCH_SYSTEMCTL": str(self.tmp / "fake-systemctl"),
            "SWITCH_GATEWAY_CLI": str(self.tmp / "fake-gateway-cli"),
            "SWITCH_PUSH_SCRIPT": str(self.tmp / "fake-push"),
            "OPENCLAW_PUSH_HOME": str(self.state),
            "FAKE_LOG": str(self.log),
            "FAKE_STATUS": str(self.status),
            "FAKE_PUSH_SNAPSHOT": str(self.snapshot),
            "FORCE_COLOR": "3",
            **env,
        }
        full.pop("OPENCLAW_PUSH_ENV_FILE", None)
        return subprocess.run(
            ["bash", str(SCRIPT), *args, "--env-file", env_arg or str(self.env_file)],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=full,
            check=False,
        )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    harness = Harness(tmp_path)
    harness.write_env("# keep\nRESEARCH_V2_ROOT=@ROOT@\nOTHER=1  \nG2_AGENT_PROFILE=codex\nLAST=z")
    return harness


def pushes(h: Harness) -> list[str]:
    return [c for c in h.calls() if c.startswith("push")]


def test_dry_run_changes_nothing(h: Harness) -> None:
    before = h.env_file.read_bytes()
    result = h.run("claude", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "claude -p --settings" in result.stdout
    assert "claude_runtime install" in result.stdout
    assert "G2_AGENT_PROFILE=claude" in result.stdout
    assert h.env_file.read_bytes() == before
    calls = h.calls()
    assert not pushes(h)
    assert not any("claude_runtime" in c or "claude_doctor" in c for c in calls)
    # Read-only checks still ran.
    assert any(c.startswith("systemctl --user show -p ActiveState --value") for c in calls)
    assert any(c.startswith("gateway-cli research status") for c in calls)
    assert not (h.state / ".switch-agent-profile.lock").exists()


def test_dry_run_runs_refusals(h: Harness) -> None:
    assert h.run("codex", "--dry-run", FAKE_OWNER_STATE="active").returncode == 1


def test_unknown_profile(h: Harness) -> None:
    result = h.run("gemini")
    assert result.returncode == 1
    assert "gemini" in result.stderr
    assert not pushes(h)


def test_usage_error(h: Harness) -> None:
    assert subprocess.run(["bash", str(SCRIPT)], capture_output=True, check=False).returncode == 64


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "reloading"])
def test_owner_not_stopped_refuses(h: Harness, state: str) -> None:
    result = h.run("codex", FAKE_OWNER_STATE=state)
    assert result.returncode == 1
    assert f"research-owner.service is {state}" in result.stderr
    assert "G2_AGENT_PROFILE=codex" in h.env_file.read_text()
    assert not pushes(h)


def test_owner_failed_state_is_allowed(h: Harness) -> None:
    assert h.run("codex", FAKE_OWNER_STATE="failed").returncode == 0


def test_owner_state_unreadable_refuses(h: Harness) -> None:
    result = h.run("codex", FAKE_OWNER_FAIL="1")
    assert result.returncode == 1
    assert "cannot read" in result.stderr
    assert not pushes(h)


def test_implemented_attempt_refuses(h: Harness) -> None:
    h.set_status(
        [{"attempt_id": "A1", "state": "CLOSED"}, {"attempt_id": "A2", "state": "IMPLEMENTED"}]
    )
    result = h.run("claude")
    assert result.returncode == 1
    assert "H1/A2 (IMPLEMENTED" in result.stderr
    assert not pushes(h)
    assert any(c.startswith("gateway-cli research status --root") for c in h.calls())


def test_gateway_cli_failure_refuses(h: Harness) -> None:
    result = h.run("codex", FAKE_CLI_FAIL="1")
    assert result.returncode == 1
    assert "cannot read research status" in result.stderr
    assert not pushes(h)


def test_non_json_status_refuses(h: Harness) -> None:
    h.status.write_text("not json at all")
    result = h.run("codex")
    assert result.returncode == 1
    assert "not the expected JSON" in result.stderr
    assert not pushes(h)


def test_missing_research_root_refuses(h: Harness) -> None:
    shutil.rmtree(h.root)
    result = h.run("codex")
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert not pushes(h)


def test_no_research_root_flag_skips(h: Harness) -> None:
    shutil.rmtree(h.root)
    result = h.run("codex", "--no-research-root")
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("gateway-cli") for c in h.calls())


def test_caller_research_root_overrides_env_file(h: Harness, tmp_path: Path) -> None:
    other = tmp_path / "other-root"
    other.mkdir()
    (other / "state.sqlite3").write_text("")
    assert h.run("codex", RESEARCH_V2_ROOT=str(other)).returncode == 0
    assert f"--root {other} " in " ".join(h.calls())


def test_doctor_failure_prints_remediation(h: Harness) -> None:
    result = h.run("claude", FAKE_DOCTOR="fail")
    assert result.returncode != 0
    assert "[FAIL] claude auth" in result.stderr
    assert "sudo apt install socat" in result.stderr
    assert "sudo systemctl reload apparmor" in result.stderr
    assert f"CLAUDE_CONFIG_DIR={h.state}/claude-code claude auth login" in result.stderr
    assert not pushes(h)


def test_smoke_ack_gate(h: Harness) -> None:
    result = h.run("claude")
    assert result.returncode != 0
    assert "--skip-smoke-ack" in result.stderr
    assert f"claude -p --settings {h.state}/claude-code/settings.json" in result.stderr
    assert "research --help" in result.stderr
    assert "G2_AGENT_PROFILE=codex" in h.env_file.read_text()
    assert not pushes(h)


def test_success_preserves_other_lines_and_pushes(h: Harness) -> None:
    result = h.run("claude", "--skip-smoke-ack", OPENCLAW_BIN="/x/openclaw")
    assert result.returncode == 0, result.stderr
    assert h.env_file.read_text() == (
        f"# keep\nRESEARCH_V2_ROOT={h.root}\nOTHER=1  \nG2_AGENT_PROFILE=claude\nLAST=z"
    )
    assert "G2_AGENT_PROFILE=claude" in h.snapshot.read_text()
    calls = h.calls()
    push = pushes(h)[0]
    assert f"env={h.env_file}" in push
    assert f"home={h.state}" in push
    assert "bin=/x/openclaw" in push
    assert "profile=claude" in push
    assert calls[-1].startswith("python -m gateway.agent_profiles active --json --state-dir")
    assert "Deployed profile: claude" in result.stdout
    assert any(c.startswith("python -m gateway.deployment.claude_doctor") for c in calls)
    assert list(h.state.glob(".switch-env-backup.*")) == []


def test_appends_key_when_absent(h: Harness) -> None:
    h.write_env("A=1")
    result = h.run("codex", "--no-research-root")
    assert result.returncode == 0, result.stderr
    assert h.env_file.read_text() == "A=1\nG2_AGENT_PROFILE=codex\n"


def test_duplicate_key_refuses(h: Harness) -> None:
    text = "G2_AGENT_PROFILE=codex\nexport G2_AGENT_PROFILE=claude\n"
    h.env_file.write_text(text)
    result = h.run("codex")
    assert result.returncode == 1
    assert "duplicate key G2_AGENT_PROFILE" in result.stderr
    assert h.env_file.read_text() == text
    assert not pushes(h)


def test_exported_caller_profile_does_not_win(h: Harness) -> None:
    result = h.run("claude", "--skip-smoke-ack", G2_AGENT_PROFILE="codex")
    assert result.returncode == 0, result.stderr
    assert "profile=claude" in pushes(h)[0]
    # The grammar check ignores an exported G2_AGENT_PROFILE.
    assert set(h.resolve_env.read_text().split()) == {"unset"}


def test_push_failure_rolls_back_env_file(h: Harness) -> None:
    before = h.env_file.read_bytes()
    result = h.run("claude", "--skip-smoke-ack", FAKE_PUSH="fail")
    assert result.returncode == 1
    assert "push failed" in result.stderr
    assert "Restored" in result.stderr
    assert "Deployed profile: codex" in result.stderr
    assert h.env_file.read_bytes() == before


def test_stale_deploy_is_detected_and_rolled_back(h: Harness) -> None:
    before = h.env_file.read_bytes()
    result = h.run("claude", "--skip-smoke-ack", FAKE_PUSH="stale")
    assert result.returncode == 1
    assert "deployed profile is codex, not claude" in result.stderr
    assert h.env_file.read_bytes() == before


def test_failed_push_keeps_env_when_new_profile_already_deployed(h: Harness) -> None:
    h.deployed.write_text("claude")
    result = h.run("claude", "--skip-smoke-ack", FAKE_PUSH="fail")
    assert result.returncode == 1
    assert "already claude" in result.stderr
    assert "G2_AGENT_PROFILE=claude" in h.env_file.read_text()


def test_relative_env_file_is_resolved(h: Harness) -> None:
    result = h.run("codex", env_arg=h.env_file.name, cwd=h.env_file.parent)
    assert result.returncode == 0, result.stderr
    assert f"env={h.env_file}" in pushes(h)[0]


def test_symlinked_env_file_refuses(h: Harness, tmp_path: Path) -> None:
    link = tmp_path / "link.env"
    link.symlink_to(h.env_file)
    result = h.run("codex", env_arg=str(link))
    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert not pushes(h)


def test_missing_and_writable_env_file_refuse(h: Harness, tmp_path: Path) -> None:
    assert h.run("codex", env_arg=str(tmp_path / "nope.env")).returncode == 1
    for mode in (0o666, 0o640, 0o604):
        h.env_file.chmod(mode)
        result = h.run("codex")
        assert result.returncode == 1
        assert "accessible by group or others" in result.stderr


def test_env_file_carriage_return_and_grammar_refuse(h: Harness) -> None:
    h.env_file.write_bytes(b"A=1\r\n")
    assert h.run("codex").returncode == 1
    h.write_env("G2_AGENT_PROFILE=codex")
    assert h.run("codex", FAKE_RESOLVE="bad").returncode == 1
    assert not pushes(h)


def test_install_runs_before_doctor_with_vars(h: Harness) -> None:
    result = h.run("claude", "--skip-smoke-ack")
    assert result.returncode == 0, result.stderr
    calls = h.calls()
    install = next(i for i, c in enumerate(calls) if "claude_runtime install" in c)
    doctor = next(i for i, c in enumerate(calls) if "claude_doctor check" in c)
    assert install < doctor
    line = calls[install]
    assert f"--state-dir {h.state} --profile claude" in line
    assert f"--var RESEARCH_V2_ROOT={h.root}" in line
    assert f"--var HYPOTHESIS_WORKTREES_ROOT={h.root}/hypothesis-worktrees" in line
    assert f"--var OPERATOR_ENV_FILE={h.env_file}" in line
    assert f"--var REPO_ROOT={SCRIPT.parents[1]}" in line
    assert f"--var OPENCLAW_STATE_DIR={h.state}" in line
    assert "--var HOME=" in line


def test_install_failure_stops_switch(h: Harness) -> None:
    result = h.run("claude", "--skip-smoke-ack", FAKE_INSTALL="fail")
    assert result.returncode == 1
    assert "install exploded" in result.stderr
    assert not any("claude_doctor" in c for c in h.calls())
    assert not pushes(h)
    assert "G2_AGENT_PROFILE=codex" in h.env_file.read_text()


def test_codex_does_not_install(h: Harness) -> None:
    assert h.run("codex").returncode == 0
    assert not any("claude_runtime" in c for c in h.calls())


def test_interrupted_push_restores_env_file(h: Harness) -> None:
    before = h.env_file.read_bytes()
    result = h.run("claude", "--skip-smoke-ack", FAKE_PUSH="term")
    assert result.returncode == 1
    assert "interrupted" in result.stderr
    assert "Restored" in result.stderr
    assert "re-run" in result.stderr
    assert h.env_file.read_bytes() == before
    assert list(h.state.glob(".switch-env-backup.*")) == []


def test_interrupted_after_deploy_keeps_switch_and_backup(h: Harness) -> None:
    result = h.run("claude", "--skip-smoke-ack", FAKE_PUSH="term_deployed")
    assert result.returncode == 1
    assert "already claude" in result.stderr
    assert "G2_AGENT_PROFILE=claude" in h.env_file.read_text()
    backups = list(h.state.glob(".switch-env-backup.*"))
    assert len(backups) == 1
    assert str(backups[0]) in result.stderr


def test_missing_store_refuses_without_status_call(h: Harness) -> None:
    (h.root / "state.sqlite3").unlink()
    result = h.run("codex", "--dry-run")
    assert result.returncode == 1
    assert "--no-research-root" in result.stderr
    assert not any(c.startswith("gateway-cli") for c in h.calls())


def test_symlinked_lock_refuses(h: Harness, tmp_path: Path) -> None:
    h.state.mkdir()
    target = tmp_path / "victim"
    target.write_text("keep")
    (h.state / ".switch-agent-profile.lock").symlink_to(target)
    result = h.run("codex")
    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert target.read_text() == "keep"


def test_push_is_told_the_profile_change_is_intended(h: Harness) -> None:
    result = h.run("claude", "--skip-smoke-ack")
    assert result.returncode == 0, result.stderr
    assert "change=claude" in pushes(h)[0]


def test_default_prints_restart_command_without_running_it(h: Harness) -> None:
    result = h.run("codex")
    assert result.returncode == 0, result.stderr
    assert "systemctl --user restart openclaw-gateway.service" in result.stdout
    assert not any("restart" in c for c in h.calls() if c.startswith("systemctl"))


def test_restart_gateway_flag_restarts_after_verify(h: Harness) -> None:
    result = h.run("codex", "--restart-gateway")
    assert result.returncode == 0, result.stderr
    calls = h.calls()
    assert calls[-1] == "systemctl --user restart openclaw-gateway.service"
    assert any(c.startswith("python -m gateway.agent_profiles active --json") for c in calls)


def test_dry_run_mentions_restart_plan_and_does_not_restart(h: Harness) -> None:
    result = h.run("codex", "--dry-run", "--restart-gateway")
    assert result.returncode == 0, result.stderr
    assert "would run" in result.stdout and "restart openclaw-gateway.service" in result.stdout
    assert not any("restart" in c for c in h.calls() if c.startswith("systemctl"))
