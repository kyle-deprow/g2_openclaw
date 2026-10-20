"""Tests for the Claude Code readiness doctor; the subprocess layer is always faked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from gateway.deployment import claude_doctor
from gateway.deployment.claude_doctor import ProcessResult

TOKEN = "sk-ant-oat01-SECRETSECRET_xyz"
LOGGED_IN = json.dumps(
    {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max", "email": "a@b.c"}
)
LOGGED_OUT = json.dumps({"loggedIn": False, "authMethod": "none"})


class FakeRunner:
    def __init__(self, responses: Mapping[str, ProcessResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> ProcessResult:
        self.calls.append((list(argv), dict(env)))
        exe = os.path.basename(argv[0])
        key = " ".join(argv[:3]) if exe == "claude" and argv[1] == "auth" else exe
        if exe == "claude" and argv[1] == "--version":
            key = "claude --version"
        key = key.replace(argv[0], exe, 1)
        return self.responses[key]


def _ok(stdout: str) -> ProcessResult:
    return ProcessResult(0, stdout, "")


def _state(tmp_path: Path, *, settings: bool = True) -> Path:
    state = tmp_path / "state"
    config = state / "claude-code"
    config.mkdir(parents=True)
    if settings:
        (config / "settings.json").write_text("{}", encoding="utf-8")
    return state


def _runner(
    version: str = "2.1.304 (Claude Code)", auth: str = LOGGED_IN, health: str | None = None
) -> FakeRunner:
    responses = {
        "claude --version": _ok(version),
        "claude auth status": _ok(auth),
    }
    if health is not None:
        responses["openclaw"] = _ok(health)
    return FakeRunner(responses)


def _by_name(results: Sequence[claude_doctor.CheckResult]) -> dict[str, str]:
    return {item.name: item.status for item in results}


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, validator: object | None) -> None:
    module = types.ModuleType("gateway.deployment.claude_runtime")
    if validator is not None:
        module.validate_settings_file = validator  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway.deployment.claude_runtime", module)


def test_all_pass_with_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def validator(path: Path) -> list[str]:
        seen.append(path)
        return []

    _stub_runtime(monkeypatch, validator)
    state = _state(tmp_path)
    runner = _runner()

    results = claude_doctor.run_checks(state, runner=runner)

    assert _by_name(results) == {
        "claude_version": "pass",
        "claude_auth": "pass",
        "claude_settings": "pass",
    }
    assert seen == [state / "claude-code" / "settings.json"]
    assert claude_doctor.exit_code(results) == 0


def test_subprocess_env_is_minimal_and_points_at_dedicated_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    _stub_runtime(monkeypatch, lambda path: [])
    state = _state(tmp_path)
    runner = _runner()

    claude_doctor.run_checks(state, runner=runner)

    for argv, env in runner.calls:
        assert set(env) == {"PATH", "HOME", "CLAUDE_CONFIG_DIR"}
        assert env["CLAUDE_CONFIG_DIR"] == str(state / "claude-code")
        assert os.path.basename(argv[0]) == "claude"


def test_old_version_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    results = claude_doctor.run_checks(
        _state(tmp_path), runner=_runner(version="2.1.97 (Claude Code)")
    )
    assert _by_name(results)["claude_version"] == "fail"
    assert claude_doctor.exit_code(results) == 1


def test_minimum_version_boundary_and_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    state = _state(tmp_path)
    ok = claude_doctor.run_checks(state, runner=_runner(version="2.1.98"))
    bad = claude_doctor.run_checks(state, runner=_runner(version="nonsense"))
    assert _by_name(ok)["claude_version"] == "pass"
    assert _by_name(bad)["claude_version"] == "fail"


def test_not_logged_in_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    results = claude_doctor.run_checks(_state(tmp_path), runner=_runner(auth=LOGGED_OUT))
    assert _by_name(results)["claude_auth"] == "fail"


def test_auth_output_never_leaks_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    runner = FakeRunner(
        {
            "claude --version": _ok("2.1.304"),
            "claude auth status": ProcessResult(1, f"oops {TOKEN}", f"stderr {TOKEN}"),
        }
    )
    results = claude_doctor.run_checks(_state(tmp_path), runner=runner)
    rendered = json.dumps([item.to_dict() for item in results])
    assert TOKEN not in rendered
    assert "SECRETSECRET" not in rendered
    assert _by_name(results)["claude_auth"] == "fail"


def test_logged_in_detail_omits_email(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    results = claude_doctor.run_checks(_state(tmp_path), runner=_runner())
    detail = next(i.detail for i in results if i.name == "claude_auth")
    assert "a@b.c" not in detail


def test_missing_settings_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    results = claude_doctor.run_checks(_state(tmp_path, settings=False), runner=_runner())
    assert _by_name(results)["claude_settings"] == "fail"


def test_settings_validator_empty_list_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    results = claude_doctor.run_checks(_state(tmp_path), runner=_runner())
    assert _by_name(results)["claude_settings"] == "pass"


def test_settings_validator_problems_fail_and_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_runtime(monkeypatch, lambda path: ["missing deny rule", f"leaked {TOKEN}"])
    results = claude_doctor.run_checks(_state(tmp_path), runner=_runner())
    settings = next(i for i in results if i.name == "claude_settings")
    assert settings.status == "fail"
    assert "missing deny rule" in settings.detail
    assert TOKEN not in settings.detail
    assert claude_doctor.exit_code(results) == 1


def test_settings_validator_exception_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(path: Path) -> list[str]:
        raise ValueError(f"bad rule {TOKEN}")

    _stub_runtime(monkeypatch, reject)
    results = claude_doctor.run_checks(_state(tmp_path), runner=_runner())
    assert _by_name(results)["claude_settings"] == "fail"
    assert TOKEN not in json.dumps([i.to_dict() for i in results])


def test_absent_runtime_module_skips_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "gateway.deployment.claude_runtime", None)
    results = claude_doctor.run_checks(_state(tmp_path), runner=_runner())
    settings = next(i for i in results if i.name == "claude_settings")
    assert settings.status == "skip"
    assert "warning" in settings.detail
    assert claude_doctor.exit_code(results) == 0


def test_gateway_requires_anthropic_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    monkeypatch.setattr(claude_doctor, "resolve_openclaw_bin", lambda **kw: "/fake/openclaw")
    state = _state(tmp_path)
    good = json.dumps({"plugins": {"loaded": ["anthropic"]}})
    codex = json.dumps({"plugins": {"loaded": ["codex", "openai"]}})

    runner = _runner(health=good)
    assert (
        _by_name(claude_doctor.run_checks(state, gateway=True, runner=runner))["gateway_plugins"]
        == "pass"
    )
    argv, env = runner.calls[-1]
    assert argv[1:4] == ["gateway", "call", "health"]
    assert argv[0] == "/fake/openclaw"
    assert set(env) == {
        "PATH",
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_CONFIG_PATH",
    }
    assert env["OPENCLAW_STATE_DIR"] == str(state)
    assert env["OPENCLAW_CONFIG_PATH"] == str(state / "openclaw.json")

    assert (
        _by_name(claude_doctor.run_checks(state, gateway=True, runner=_runner(health=codex)))[
            "gateway_plugins"
        ]
        == "fail"
    )
    assert (
        _by_name(claude_doctor.run_checks(state, gateway=True, runner=_runner(health="{}")))[
            "gateway_plugins"
        ]
        == "fail"
    )
    assert "gateway_plugins" not in _by_name(claude_doctor.run_checks(state, runner=_runner()))


def test_readiness_failures_list(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert claude_doctor.check_claude_auth_readiness(state, runner=_runner()) == []
    failures = claude_doctor.check_claude_auth_readiness(state, runner=_runner(auth=LOGGED_OUT))
    assert len(failures) == 1 and failures[0].startswith("claude_auth:")


def test_cli_json_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    state = _state(tmp_path)
    original = claude_doctor.run_checks

    def patched(
        state_dir: Path, *, gateway: bool = False, claude_bin: str | None = None
    ) -> list[claude_doctor.CheckResult]:
        return original(state_dir, gateway=gateway, claude_bin=claude_bin, runner=_runner())

    monkeypatch.setattr(claude_doctor, "run_checks", patched)
    assert claude_doctor.main(["check", "--state-dir", str(state), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert [c["name"] for c in payload["checks"]] == [
        "claude_version",
        "claude_auth",
        "claude_settings",
    ]

    def failing(
        state_dir: Path, *, gateway: bool = False, claude_bin: str | None = None
    ) -> list[claude_doctor.CheckResult]:
        return original(
            state_dir, gateway=gateway, claude_bin=claude_bin, runner=_runner(auth=LOGGED_OUT)
        )

    monkeypatch.setattr(claude_doctor, "run_checks", failing)
    assert claude_doctor.main(["check", "--state-dir", str(state)]) == 1
    assert "[FAIL] claude_auth" in capsys.readouterr().out


def test_cli_usage_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        claude_doctor.main(["check"])
    assert excinfo.value.code == 2


def test_cli_relative_state_dir_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert claude_doctor.main(["check", "--state-dir", "relative/dir"]) == 2


def test_run_process_reports_missing_binary_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = claude_doctor.run_process(["definitely-not-a-binary-xyz"], {"PATH": "/nonexistent"})
    assert missing.returncode == 127

    def boom(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_doctor.run_process(["claude"], {}).returncode == 124


def _fake_proc(root: Path, pid: int, argv: list[str], env: dict[str, str] | None) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if env is not None:
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))


_CHILD_ARGV = [
    "/home/dev/.local/bin/claude",
    "--mcp-config",
    "/tmp/openclaw-cli-mcp-AbC/mcp.json",
    "--effort",
    "high",
    "--print",
    "--input-format",
    "stream-json",
]


def test_gateway_claude_children_match_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import cli

    state = tmp_path / "state"
    dedicated = state / "claude-code"
    dedicated.mkdir(parents=True)
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(cli, "_PROC_ROOT", proc)
    good = {"CLAUDE_CONFIG_DIR": str(dedicated), "OPENCLAW_MCP_TOKEN": "t"}
    _fake_proc(proc, 100, _CHILD_ARGV, good)  # live child
    _fake_proc(proc, 101, _CHILD_ARGV, good)  # orphan: ppid is irrelevant
    _fake_proc(proc, 102, ["claude", "-p", "hi"], {"CLAUDE_CONFIG_DIR": str(dedicated)})
    _fake_proc(
        proc,
        103,
        ["/bin/bash", "-c", "cd /x/g2_openclaw/gateway && claude --print hi"],
        {"CLAUDE_CONFIG_DIR": str(dedicated)},
    )
    _fake_proc(proc, 104, ["claude", "--print", "hi"], {"CLAUDE_CONFIG_DIR": "/home/dev/.claude"})
    _fake_proc(proc, 105, ["claude", "--print", "hi"], {"HOME": "/home/dev"})
    _fake_proc(proc, 106, _CHILD_ARGV, None)  # environ unreadable
    _fake_proc(proc, 107, _CHILD_ARGV, good)
    _fake_proc(
        proc,
        108,
        ["claude", "--print", "--plugin-dir", "/tmp/openclaw/openclaw-claude-skills-Z/x"],
        {"CLAUDE_CONFIG_DIR": str(dedicated)},
    )
    _fake_proc(proc, 109, ["claude", "--print", "--plugin-dir", "/tmp/other"], good | {})
    (proc / "109" / "environ").write_bytes(f"CLAUDE_CONFIG_DIR={dedicated}".encode())
    (proc / "notapid").mkdir()

    assert cli._gateway_claude_pids({107}) == {100, 101, 108}


def test_stop_collects_claude_pids_before_daemon_kill_and_signals_single_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import cli

    events: list[tuple[object, ...]] = []

    def collect(exclude: set[int] | None = None) -> set[int]:
        events.append(("collect",))
        return {777}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        pattern = argv[-1]
        if pattern == "openclaw.*daemon":
            return subprocess.CompletedProcess(argv, 0, "5000\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    def fake_group(pid: int, sig: object) -> None:
        events.append(("group", pid, sig))

    def fake_kill(pid: int, sig: int) -> None:
        events.append(("kill", pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(cli, "_gateway_claude_pids", collect)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_signal_process_group", fake_group)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_remove_pid_file", lambda: None)

    cli.stop()

    assert events[0] == ("collect",)
    daemon_kill = next(i for i, e in enumerate(events) if e[:2] == ("group", 5000))
    assert daemon_kill > 0
    assert ("kill", 777, 15) in events
    assert not any(e[0] == "group" and e[1] == 777 for e in events)


def test_explicit_claude_bin_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, lambda path: [])
    runner = _runner()
    claude_doctor.run_checks(_state(tmp_path), claude_bin="/opt/x/claude", runner=runner)
    assert {argv[0] for argv, _ in runner.calls} == {"/opt/x/claude"}
