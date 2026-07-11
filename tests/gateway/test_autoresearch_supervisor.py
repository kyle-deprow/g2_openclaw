from __future__ import annotations

import json
import logging
import multiprocessing
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from gateway.autoresearch_runner import (
    AutoresearchState,
    ImplementationResultArtifact,
    Phase,
    ResearchMode,
)
from gateway.autoresearch_supervisor import (
    AutoresearchSupervisor,
    DevAPIError,
    G2Snapshot,
    OpenClawVersionError,
    SupervisorConfig,
    SupervisorError,
    SupervisorOutcome,
    SupervisorResult,
    _build_arg_parser,
    _is_main_g2_session_key,
)


def _write_state(
    path: Path,
    *,
    phase: Phase,
    iteration: int,
    implementation_result: ImplementationResultArtifact | None = None,
) -> None:
    state = AutoresearchState(
        phase=phase,
        iteration=iteration,
        mode=ResearchMode.ALPHA_RESEARCH,
        implementation_result=implementation_result,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _write_git_markers(repo_root: Path) -> None:
    git_dir = repo_root / ".git"
    (git_dir / "logs").mkdir(parents=True, exist_ok=True)
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "index").write_text("index\n", encoding="utf-8")
    (git_dir / "logs" / "HEAD").write_text("head-log\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("deadbeef\n", encoding="utf-8")


def _write_openclaw_bin(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _touch_old(paths: list[Path], *, now_seconds: float, age_seconds: float) -> None:
    timestamp = now_seconds - age_seconds
    for path in paths:
        os.utime(path, (timestamp, timestamp))


def _make_state_stale(env: dict[str, Any]) -> None:
    _touch_old(
        [
            env["state_path"],
            env["repo_root"] / ".git" / "HEAD",
            env["repo_root"] / ".git" / "index",
            env["repo_root"] / ".git" / "logs" / "HEAD",
            env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=env["now_seconds"],
        age_seconds=600.0,
    )


def _write_main_sessions_store(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _implementation_result(workspace_path: Path) -> ImplementationResultArtifact:
    return ImplementationResultArtifact(
        summary="implementation complete",
        workspace_path=str(workspace_path),
        commit_sha="deadbeef",
        module_path="src/quantipy/alpha/example.py",
        notebook_path="notebooks/example.ipynb",
        tests_added_or_updated=(),
        commands_run=(),
    )


def _write_proc_process(
    proc_root: Path,
    *,
    pid: int,
    command: tuple[str, ...],
    cwd: Path,
) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir()
    (proc_dir / "cmdline").write_bytes(b"\x00".join(part.encode() for part in command) + b"\x00")
    (proc_dir / "cwd").symlink_to(cwd, target_is_directory=True)


def _running_main_session_row(
    now_ms: int,
    *,
    updated_age_ms: int,
    last_interaction_age_ms: int,
    started_age_ms: int,
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "status": "running",
        "updatedAt": now_ms - updated_age_ms,
        "lastInteractionAt": now_ms - last_interaction_age_ms,
        "startedAt": now_ms - started_age_ms,
    }
    row.update(extra)
    return row


def _forbid_live_http(*args: object, **kwargs: object) -> Any:
    del args, kwargs
    raise AssertionError("tests must not access the live G2 Dev API")


class FakeRunCommand:
    def __init__(
        self,
        *,
        now_ms: int,
        tasks: list[dict[str, object]],
        sessions: list[dict[str, object]],
        version: str = "OpenClaw 2026.6.11 (e085fa1)",
        delete_payload: dict[str, object] | None = None,
        delete_returncode: int = 0,
        events: list[str] | None = None,
    ) -> None:
        self.now_ms = now_ms
        self.tasks = tasks
        self.sessions = sessions
        self.version = version
        self.delete_payload = delete_payload
        self.delete_returncode = delete_returncode
        self.events = events
        self.calls: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        self.calls.append(command)
        if self.events is not None:
            self.events.append(f"cli:{' '.join(command[1:4])}")
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout=self.version, stderr="")
        if command[1:] == ["tasks", "list", "--status", "running", "--json"]:
            payload = {"count": len(self.tasks), "status": "running", "tasks": self.tasks}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if command[:4] == [command[0], "sessions", "--agent", "main"] and command[-1] == "--json":
            payload = {
                "count": len(self.sessions),
                "activeMinutes": 120,
                "sessions": self.sessions,
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if command[1:4] == ["gateway", "call", "sessions.delete"]:
            if self.delete_returncode != 0:
                return subprocess.CompletedProcess(
                    command,
                    self.delete_returncode,
                    stdout="",
                    stderr="session deletion failed",
                )
            params = json.loads(command[6])
            payload = self.delete_payload or {
                "ok": True,
                "deleted": True,
                "key": params["key"],
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"unexpected command: {command}")


@pytest.fixture()
def supervisor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.delenv("OPENCLAW_BIN", raising=False)
    now_seconds = 1_000_000.0
    now_ms = int(now_seconds * 1000)
    state_dir = tmp_path / "autoresearch"
    state_path = state_dir / "quantipy-state.json"
    checkpoint_path = state_dir / "supervisor-state.json"
    repo_root = tmp_path / "quantipy"
    repo_root.mkdir(parents=True, exist_ok=True)
    _write_git_markers(repo_root)
    openclaw_bin = tmp_path / "bin" / "openclaw"
    _write_openclaw_bin(openclaw_bin)
    sessions_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
    _write_main_sessions_store(sessions_path, {})
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    return {
        "now_seconds": now_seconds,
        "now_ms": now_ms,
        "state_dir": state_dir,
        "state_path": state_path,
        "checkpoint_path": checkpoint_path,
        "repo_root": repo_root,
        "openclaw_bin": openclaw_bin,
        "sessions_path": sessions_path,
        "proc_root": proc_root,
    }


def _make_supervisor(
    env: dict[str, Any],
    *,
    runner: FakeRunCommand,
    expected_stage_task_stale_seconds: float = 300.0,
    grace_period_seconds: float = 120.0,
    max_recovery_attempts: int = 2,
) -> AutoresearchSupervisor:
    config = SupervisorConfig(
        state_path=env["state_path"],
        checkpoint_path=env["checkpoint_path"],
        autoresearch_dir=env["state_dir"],
        main_sessions_path=env["sessions_path"],
        target_repo=env["repo_root"],
        proc_root=env["proc_root"],
        default_openclaw_bin=env["openclaw_bin"],
        grace_period_seconds=grace_period_seconds,
        expected_stage_task_stale_seconds=expected_stage_task_stale_seconds,
        max_recovery_attempts=max_recovery_attempts,
    )
    return AutoresearchSupervisor(
        config,
        now=lambda: env["now_seconds"],
        sleep=lambda _: None,
        run_command=runner,
        urlopen=_forbid_live_http,
    )


def _install_rotation_dev_api(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: AutoresearchSupervisor,
    *,
    session_list: list[object],
    events: list[str],
) -> None:
    states = iter(("menu", "idle"))

    monkeypatch.setattr(supervisor, "_dev_get_state", lambda: next(states))

    def fake_dev_command(command: str, args: object = None) -> object:
        del args
        events.append(f"dev:{command}")
        if command == "getSessionList":
            return session_list
        return None

    monkeypatch.setattr(supervisor, "_dev_command", fake_dev_command)


def test_rotation_retires_the_prior_active_g2_session_after_creating_the_new_one(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[],
        events=events,
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    old_key = "agent:main:g2:old-parent"
    _install_rotation_dev_api(
        monkeypatch,
        supervisor,
        session_list=[{"sessionKey": old_key, "isActive": True}],
        events=events,
    )

    supervisor._rotate_g2_session("idle")

    assert events == [
        "dev:openSessionMenu",
        "dev:getSessionList",
        "dev:selectSession",
        "cli:--version",
        "cli:gateway call sessions.delete",
    ]
    assert runner.calls[-1] == [
        str(supervisor_env["openclaw_bin"]),
        "gateway",
        "call",
        "sessions.delete",
        "--json",
        "--params",
        json.dumps(
            {"key": old_key, "agentId": "main", "deleteTranscript": False},
            separators=(",", ":"),
        ),
        "--timeout",
        "30000",
    ]


def test_rotation_allows_an_unused_g2_session_menu_without_retiring_a_session(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[],
        events=events,
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    _install_rotation_dev_api(monkeypatch, supervisor, session_list=[], events=events)

    supervisor._rotate_g2_session("idle")

    assert events == ["dev:openSessionMenu", "dev:getSessionList", "dev:selectSession"]


@pytest.mark.parametrize(
    "session_list",
    [
        ["not-an-entry"],
        [{"isActive": True}],
        [
            {"sessionKey": "agent:main:g2:first", "isActive": True},
            {"sessionKey": "agent:main:g2:second", "isActive": True},
        ],
        [{"sessionKey": "agent:main:other", "isActive": True}],
    ],
)
def test_rotation_fails_closed_for_an_invalid_active_menu_entry(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    session_list: list[object],
) -> None:
    events: list[str] = []
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[], events=events)
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    _install_rotation_dev_api(monkeypatch, supervisor, session_list=session_list, events=events)

    with pytest.raises(DevAPIError):
        supervisor._rotate_g2_session("idle")

    assert events == ["dev:openSessionMenu", "dev:getSessionList"]


@pytest.mark.parametrize(
    ("delete_payload", "delete_returncode"),
    [
        ({"ok": False, "deleted": True, "key": "agent:main:g2:old-parent"}, 0),
        ({"ok": True, "deleted": False, "key": "agent:main:g2:old-parent"}, 0),
        ({"ok": True, "deleted": True, "key": "agent:main:g2:wrong-parent"}, 0),
        (None, 1),
    ],
)
def test_rotation_rejects_an_unsuccessful_or_mismatched_session_retirement(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    delete_payload: dict[str, object] | None,
    delete_returncode: int,
) -> None:
    events: list[str] = []
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[],
        delete_payload=delete_payload,
        delete_returncode=delete_returncode,
        events=events,
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    _install_rotation_dev_api(
        monkeypatch,
        supervisor,
        session_list=[{"sessionKey": "agent:main:g2:old-parent", "isActive": True}],
        events=events,
    )

    with pytest.raises(SupervisorError):
        supervisor._rotate_g2_session("idle")

    assert events[-1] == "cli:gateway call sessions.delete"


def test_retirement_failure_prevents_the_recovery_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.SETUP_CONTEXT, iteration=31)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    events: list[str] = []
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[],
        delete_payload={"ok": True, "deleted": False, "key": "agent:main:g2:old-parent"},
        events=events,
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    _install_rotation_dev_api(
        monkeypatch,
        supervisor,
        session_list=[{"sessionKey": "agent:main:g2:old-parent", "isActive": True}],
        events=events,
    )
    monkeypatch.setattr(
        supervisor,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(AssertionError("nudge must not follow failed retirement")),
    )

    with pytest.raises(SupervisorError):
        supervisor.run_once()

    assert events[-1] == "cli:gateway call sessions.delete"


def _run_running_main_session_row_case(
    env: dict[str, Any],
    row: dict[str, object],
) -> SupervisorResult:
    _write_state(env["state_path"], phase=Phase.VERIFICATION, iteration=20)
    _touch_old(
        [
            env["state_path"],
            env["repo_root"] / ".git" / "HEAD",
            env["repo_root"] / ".git" / "index",
            env["repo_root"] / ".git" / "logs" / "HEAD",
            env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        env["sessions_path"],
        {"agent:main:g2:timestamp-schema-pm": row},
    )
    runner = FakeRunCommand(now_ms=env["now_ms"], tasks=[], sessions=[])
    return _make_supervisor(env, runner=runner).run_once()


@pytest.mark.parametrize(
    ("key", "matches"),
    [
        ("agent:main:g2", True),
        ("agent:main:g2:child", True),
        ("agent:main:g2backup", False),
    ],
)
def test_main_g2_session_key_matcher_accepts_only_base_key_or_children(
    key: str,
    matches: bool,
) -> None:
    result = _is_main_g2_session_key(key)

    assert result is matches


def test_active_expected_stage_task_suppresses_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=19)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[
            {
                "agentId": "main",
                "requesterAgentId": "main",
                "requesterSessionKey": "agent:main:g2:abc",
                "ownerKey": "agent:main:g2:abc",
                "childSessionKey": "agent:main:g2:abc",
                "task": "Continue Quantipy autoresearch from the authoritative state.",
                "startedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
        sessions=[],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_stage_task"


def test_stale_duplicate_expected_stage_rows_alert_without_recovery(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=19)
    original_state = supervisor_env["state_path"].read_text(encoding="utf-8")
    stale_task = {
        "agentId": "main",
        "requesterAgentId": "main",
        "task": "Continue Quantipy autoresearch.",
        "startedAt": supervisor_env["now_ms"] - 301_000,
    }
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[stale_task, dict(stale_task)],
        sessions=[],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_expected_stage_task"
    assert result.rotated_session is False
    assert result.sent_nudge is False
    assert supervisor_env["state_path"].read_text(encoding="utf-8") == original_state
    assert not supervisor_env["checkpoint_path"].exists()


@pytest.mark.parametrize(
    "timestamp_fields",
    [
        {},
        {"startedAt": "invalid"},
        {"startedAt": float("nan")},
        {"startedAt": True},
    ],
)
def test_expected_stage_task_with_missing_or_malformed_timestamp_alerts_fail_closed(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    timestamp_fields: dict[str, object],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=19)
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[
            {
                "agentId": "main",
                "requesterAgentId": "main",
                "task": "Continue Quantipy autoresearch.",
                **timestamp_fields,
            }
        ],
        sessions=[],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_expected_stage_task"
    assert result.rotated_session is False
    assert result.sent_nudge is False


def test_configurable_stale_threshold_detects_expected_stage_task(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=19)
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[
            {
                "agentId": "main",
                "requesterAgentId": "main",
                "task": "Continue Quantipy autoresearch.",
                "startedAt": supervisor_env["now_ms"] - 61_000,
            }
        ],
        sessions=[],
    )
    supervisor = _make_supervisor(
        supervisor_env,
        runner=runner,
        expected_stage_task_stale_seconds=60.0,
    )
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_expected_stage_task"


@pytest.mark.parametrize("threshold", [0.0, -1.0, float("nan"), float("inf")])
def test_stale_threshold_must_be_finite_and_positive(
    supervisor_env: dict[str, Any],
    threshold: float,
) -> None:
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])

    with pytest.raises(SupervisorError, match="expected_stage_task_stale_seconds"):
        _make_supervisor(
            supervisor_env,
            runner=runner,
            expected_stage_task_stale_seconds=threshold,
        )


@pytest.mark.parametrize("grace_period_seconds", [0.0, -1.0, float("nan"), float("inf")])
def test_recovery_settling_period_must_be_finite_and_positive(
    supervisor_env: dict[str, Any],
    grace_period_seconds: float,
) -> None:
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])

    with pytest.raises(SupervisorError, match="grace_period_seconds"):
        _make_supervisor(
            supervisor_env,
            runner=runner,
            grace_period_seconds=grace_period_seconds,
        )


@pytest.mark.parametrize("threshold", ["0", "nan", "inf"])
def test_cli_rejects_invalid_stale_threshold(threshold: str) -> None:
    with pytest.raises(SystemExit) as raised:
        _build_arg_parser().parse_args(["--expected-stage-task-stale", threshold])

    assert raised.value.code == 2


def test_cli_accepts_valid_stale_threshold() -> None:
    args = _build_arg_parser().parse_args(["--expected-stage-task-stale", "45.5"])

    assert args.expected_stage_task_stale == 45.5


def test_unrelated_expected_agent_task_does_not_suppress_recovery(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[
            {
                "agentId": "reviewer",
                "requesterAgentId": "main",
                "requesterSessionKey": "agent:main:cli:unrelated",
                "task": "Review release notes for an unrelated project.",
                "startedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
        sessions=[],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]


def test_fresh_main_g2_session_activity_suppresses_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:abc": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=5_000,
                last_interaction_age_ms=5_000,
                started_age_ms=10_000,
            ),
        },
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:abc",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "fresh_main_g2_session"


@pytest.mark.parametrize(
    "session",
    [
        {"key": "agent:main:g2:compaction"},
        {"key": "agent:main:g2:compaction", "updatedAt": True},
        {"key": "agent:main:g2:compaction", "updatedAt": "invalid"},
    ],
)
def test_main_g2_cli_session_with_invalid_freshness_evidence_fails_closed(
    supervisor_env: dict[str, Any],
    session: dict[str, object],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[session],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    with pytest.raises(SupervisorError, match="invalid fresh main G2 CLI session updatedAt"):
        supervisor.run_once()


def test_main_g2_cli_prefix_collision_is_ignored(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2backup",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_non_running_authoritative_lifecycle_does_not_count_fresh_cli_session(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {"agent:main:g2:compaction": {"status": status}},
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]


def test_fresh_cli_session_requires_fresh_authoritative_lifecycle_lease(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:compaction": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=121_000,
                last_interaction_age_ms=121_000,
                started_age_ms=121_000,
            ),
        },
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, "missing authoritative lifecycle entry"),
        ({"agent:main:g2:compaction": {"status": 1}}, "missing string status"),
    ],
)
def test_fresh_cli_session_requires_valid_authoritative_lifecycle(
    supervisor_env: dict[str, Any],
    payload: dict[str, object],
    error: str,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _write_main_sessions_store(supervisor_env["sessions_path"], payload)
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    with pytest.raises(SupervisorError, match=error):
        supervisor.run_once()


def test_fresh_cli_session_rejects_duplicate_authoritative_lifecycle_entry(
    supervisor_env: dict[str, Any],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    supervisor_env["sessions_path"].write_text(
        (
            '{"agent:main:g2:compaction":{"status":"running"},'
            '"agent:main:g2:compaction":{"status":"failed"}}'
        ),
        encoding="utf-8",
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    with pytest.raises(SupervisorError, match="duplicate OpenClaw main sessions key"):
        supervisor.run_once()


@pytest.mark.parametrize(
    ("lifecycle", "error"),
    [
        (
            {
                "status": "running",
                "updatedAt": 999_995_000,
                "lastInteractionAt": 999_995_000,
            },
            "missing integer startedAt",
        ),
        (
            {
                "status": "running",
                "updatedAt": 1_000_000_001,
                "lastInteractionAt": 999_995_000,
                "startedAt": 999_990_000,
            },
            "timestamp is in the future",
        ),
    ],
)
def test_fresh_cli_session_rejects_invalid_running_authoritative_lifecycle(
    supervisor_env: dict[str, Any],
    lifecycle: dict[str, object],
    error: str,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {"agent:main:g2:compaction": lifecycle},
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    with pytest.raises(SupervisorError, match=error):
        supervisor.run_once()


def test_active_reviewer_task_precedes_failed_main_lifecycle_reconciliation(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=19)
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {"agent:main:g2:compaction": {"status": "failed"}},
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[
            {
                "agentId": "reviewer",
                "requesterAgentId": "main",
                "requesterSessionKey": "agent:main:g2:compaction",
                "task": "Review the Quantipy autoresearch change.",
                "startedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_stage_task"


@pytest.mark.parametrize(
    "phase",
    [Phase.SETUP_CONTEXT, Phase.VERIFICATION, Phase.DECISION_LOG],
)
def test_running_main_session_store_row_suppresses_recovery_for_main_owned_phases(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    phase: Phase,
) -> None:
    _write_state(supervisor_env["state_path"], phase=phase, iteration=19)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:live-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=240_000,
                last_interaction_age_ms=5_000,
                started_age_ms=301_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_main_session"


def test_running_main_session_store_row_at_lease_boundary_remains_active(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:boundary-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=300_000,
                last_interaction_age_ms=300_000,
                started_age_ms=300_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_main_session"


def test_running_main_session_store_row_at_exact_now_remains_active(
    supervisor_env: dict[str, Any],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:exact-now-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=0,
                last_interaction_age_ms=0,
                started_age_ms=0,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_main_session"


def test_running_main_session_store_row_rejects_fractional_future_timestamp(
    supervisor_env: dict[str, Any],
) -> None:
    result = _run_running_main_session_row_case(
        supervisor_env,
        {
            "status": "running",
            "updatedAt": supervisor_env["now_ms"] + 0.5,
            "lastInteractionAt": supervisor_env["now_ms"] - 1_000,
            "startedAt": supervisor_env["now_ms"] - 2_000,
        },
    )

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "invalid_expected_main_session_store"


def test_running_main_session_store_row_rejects_fractional_past_timestamp(
    supervisor_env: dict[str, Any],
) -> None:
    result = _run_running_main_session_row_case(
        supervisor_env,
        {
            "status": "running",
            "updatedAt": supervisor_env["now_ms"] - 0.5,
            "lastInteractionAt": supervisor_env["now_ms"] - 1_000,
            "startedAt": supervisor_env["now_ms"] - 2_000,
        },
    )

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "invalid_expected_main_session_store"


def test_running_main_session_store_row_rejects_integral_float_timestamp(
    supervisor_env: dict[str, Any],
) -> None:
    result = _run_running_main_session_row_case(
        supervisor_env,
        {
            "status": "running",
            "updatedAt": float(supervisor_env["now_ms"]),
            "lastInteractionAt": supervisor_env["now_ms"] - 1_000,
            "startedAt": supervisor_env["now_ms"] - 2_000,
        },
    )

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "invalid_expected_main_session_store"


def test_running_main_session_store_row_with_one_future_timestamp_alerts(
    supervisor_env: dict[str, Any],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:future-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=5_000,
                last_interaction_age_ms=-1,
                started_age_ms=10_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "contradictory_running_expected_main_session"


def test_running_main_session_store_row_with_all_future_timestamps_alerts(
    supervisor_env: dict[str, Any],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:future-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=-1,
                last_interaction_age_ms=-2,
                started_age_ms=-3,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "contradictory_running_expected_main_session"


def test_running_main_session_store_row_with_huge_integer_alerts(
    supervisor_env: dict[str, Any],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:huge-timestamp-pm": {
                "status": "running",
                "updatedAt": 10**400,
                "lastInteractionAt": supervisor_env["now_ms"] - 1_000,
                "startedAt": supervisor_env["now_ms"] - 2_000,
            },
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "contradictory_running_expected_main_session"


def test_non_running_main_session_store_row_does_not_suppress_recovery(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=21)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:finished-pm": {
                "status": "done",
                "endedAt": supervisor_env["now_ms"] - 10_000,
            }
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]


def test_multiple_running_main_session_store_rows_alert_fail_closed(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.DECISION_LOG, iteration=22)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:pm-a": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=10_000,
                last_interaction_age_ms=12_000,
                started_age_ms=20_000,
            ),
            "agent:main:g2:pm-b": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=5_000,
                last_interaction_age_ms=7_000,
                started_age_ms=15_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "multiple_running_expected_main_sessions"
    assert result.rotated_session is False
    assert result.sent_nudge is False


def test_stale_running_main_session_store_row_alerts_fail_closed(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=23)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_running_expected_main_session"
    assert result.rotated_session is False
    assert result.sent_nudge is False


def test_stale_running_main_session_with_current_workspace_pytest_stays_active(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = supervisor_env["state_dir"] / "quantipy-i21-base"
    workspace.mkdir(parents=True)
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")
    _write_state(
        supervisor_env["state_path"],
        phase=Phase.VERIFICATION,
        iteration=21,
        implementation_result=_implementation_result(workspace),
    )
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
        },
    )
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("uv", "run", "pytest", "--tb=short", "-q"),
        cwd=workspace,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_main_process"
    assert any("424242 uv run pytest --tb=short -q" in record.message for record in caplog.records)


def test_stale_running_main_session_with_unrelated_pytest_alerts(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = supervisor_env["state_dir"] / "quantipy-workspace"
    workspace.mkdir(parents=True)
    unrelated_workspace = supervisor_env["state_dir"] / "quantipy-workspace-copy"
    unrelated_workspace.mkdir()
    _write_state(
        supervisor_env["state_path"],
        phase=Phase.VERIFICATION,
        iteration=21,
        implementation_result=_implementation_result(workspace),
    )
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
        },
    )
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("uv", "run", "pytest", "--tb=short", "-q", str(unrelated_workspace)),
        cwd=unrelated_workspace,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_running_expected_main_session"


def test_malformed_persisted_workspace_state_raises_supervisor_error(
    supervisor_env: dict[str, Any],
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=21)
    raw_state = json.loads(supervisor_env["state_path"].read_text(encoding="utf-8"))
    assert isinstance(raw_state, dict)
    raw_state["implementation_result"] = {
        "summary": "implementation complete",
        "workspace_path": "",
        "commit_sha": "deadbeef",
        "module_path": "src/quantipy/alpha/example.py",
        "notebook_path": "notebooks/example.ipynb",
        "tests_added_or_updated": [],
        "commands_run": [],
    }
    supervisor_env["state_path"].write_text(json.dumps(raw_state), encoding="utf-8")
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    with pytest.raises(SupervisorError, match="invalid autoresearch state"):
        supervisor.run_once()


def test_stale_running_main_session_with_missing_workspace_alerts_fail_closed(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_workspace = supervisor_env["state_dir"] / "missing-workspace"
    _write_state(
        supervisor_env["state_path"],
        phase=Phase.VERIFICATION,
        iteration=21,
        implementation_result=_implementation_result(missing_workspace),
    )
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "invalid_expected_main_workspace"


@pytest.mark.parametrize("workspace_argument", (".", "tests"))
def test_stale_running_main_session_with_workspace_path_argument_stays_active(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    workspace_argument: str,
) -> None:
    workspace = supervisor_env["state_dir"] / "quantipy-workspace"
    workspace.mkdir(parents=True)
    (workspace / "tests").mkdir()
    unrelated_workspace = supervisor_env["state_dir"] / "unrelated"
    unrelated_workspace.mkdir()
    _write_state(
        supervisor_env["state_path"],
        phase=Phase.VERIFICATION,
        iteration=21,
        implementation_result=_implementation_result(workspace),
    )
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
        },
    )
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("uv", "run", "pytest", str(workspace / workspace_argument)),
        cwd=unrelated_workspace,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_main_process"


def test_stale_running_main_session_with_base_repo_pytest_stays_active(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=21)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
        },
    )
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("python", "-m", "pytest"),
        cwd=supervisor_env["repo_root"],
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "active_expected_main_process"


def test_stale_running_main_session_store_row_takes_precedence_over_fresh_row(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=24)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:stale-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=301_000,
                last_interaction_age_ms=301_000,
                started_age_ms=301_000,
            ),
            "agent:main:g2:fresh-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=5_000,
                last_interaction_age_ms=5_000,
                started_age_ms=30_000,
            ),
        },
    )
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("python", "-m", "pytest"),
        cwd=supervisor_env["repo_root"],
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_running_expected_main_session"
    assert result.rotated_session is False
    assert result.sent_nudge is False


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        (
            {
                "agent:main:g2:live-pm": {
                    "status": "running",
                    "endedAt": 999_000_000,
                }
            },
            "contradictory_running_expected_main_session",
        ),
        (
            {
                "agent:main:g2:live-pm": {
                    "status": "running",
                    "abortedLastRun": True,
                }
            },
            "contradictory_running_expected_main_session",
        ),
        (
            {
                "agent:main:g2:live-pm": {
                    "status": 123,
                }
            },
            "invalid_expected_main_session_store",
        ),
        (
            {
                "agent:main:g2:live-pm": "not-an-object",
            },
            "invalid_expected_main_session_store",
        ),
    ],
)
def test_invalid_or_contradictory_running_main_session_store_rows_alert(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected_reason: str,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=25)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(supervisor_env["sessions_path"], payload)
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("python", "-m", "pytest"),
        cwd=supervisor_env["repo_root"],
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == expected_reason
    assert result.rotated_session is False
    assert result.sent_nudge is False


def test_running_main_session_store_row_missing_timestamp_alerts_fail_closed(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=26)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:live-pm": {
                "status": "running",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
                "startedAt": supervisor_env["now_ms"] - 10_000,
            }
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "invalid_expected_main_session_store"
    assert result.rotated_session is False
    assert result.sent_nudge is False


def test_invalid_main_session_store_json_propagates_supervisor_error(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=27)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    supervisor_env["sessions_path"].write_text("{not valid json", encoding="utf-8")
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    with pytest.raises(SupervisorError, match="invalid OpenClaw main sessions JSON"):
        supervisor.run_once()


def test_multiple_fresh_main_g2_sessions_alert_without_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.FIX_TEST, iteration=19)
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:old-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=5_000,
                last_interaction_age_ms=5_000,
                started_age_ms=10_000,
            ),
            "agent:main:g2:new-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=1_000,
                last_interaction_age_ms=1_000,
                started_age_ms=10_000,
            ),
        },
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:old-pm",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            },
            {
                "key": "agent:main:g2:new-pm",
                "updatedAt": supervisor_env["now_ms"] - 1_000,
            },
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_read_g2_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("G2 should not be queried")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "multiple_fresh_main_g2_sessions"
    assert result.rotated_session is False
    assert result.sent_nudge is False


def test_running_main_session_store_row_does_not_affect_subagent_owned_phase(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=28)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:live-pm": _running_main_session_row(
                supervisor_env["now_ms"],
                updated_age_ms=5_000,
                last_interaction_age_ms=5_000,
                started_age_ms=20_000,
            ),
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]


def test_target_repo_writer_suppresses_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=20)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    _write_proc_process(
        supervisor_env["proc_root"],
        pid=424242,
        command=("python", "-m", "pytest"),
        cwd=supervisor_env["repo_root"],
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(AssertionError("nudge should not be sent")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "target_repo_writer_active"


def test_idle_setup_context_nudges_after_fresh_session_rotation(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.SETUP_CONTEXT, iteration=7)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    actions: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_rotate_g2_session",
        lambda state: actions.append(f"rotate:{state}"),
    )
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: actions.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert result.rotated_session is True
    assert result.sent_nudge is True
    assert actions == ["rotate:idle", "send"]
    checkpoint = json.loads(supervisor_env["checkpoint_path"].read_text(encoding="utf-8"))
    assert checkpoint["setup_iteration_rotations"]["7"] == supervisor_env["now_seconds"]
    record = next(iter(checkpoint["recovery_records"].values()))
    assert record["status"] == "succeeded"
    assert record["attempt_count"] == 1
    assert record["rotated"] is True


def test_successful_recovery_settles_without_a_second_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=9)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_send_recovery_message",
        lambda: sends.append("send"),
    )

    first = supervisor.run_once()
    second = supervisor.run_once()

    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.NO_ACTION
    assert second.reason == "recovery_settling"
    assert sends == ["send"]


def test_stale_fingerprint_claims_a_second_recovery_after_settling(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=10)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_rotate_g2_session", lambda state: None)
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    first = supervisor.run_once()
    supervisor_env["now_seconds"] += 120.0
    second = supervisor.run_once()

    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send", "send"]


def test_unobserved_retry_forces_one_session_rotation_before_the_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=11)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    actions: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_rotate_g2_session",
        lambda state: actions.append(f"rotate:{state}"),
    )
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: actions.append("send"))

    supervisor.run_once()
    supervisor_env["now_seconds"] += 120.0
    retry = supervisor.run_once()

    assert retry.rotated_session is True
    assert actions == ["send", "rotate:idle", "send"]


def test_already_rotated_recovery_retries_without_another_rotation(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.SETUP_CONTEXT, iteration=12)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    actions: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_rotate_g2_session",
        lambda state: actions.append(f"rotate:{state}"),
    )
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: actions.append("send"))

    supervisor.run_once()
    supervisor_env["now_seconds"] += 120.0
    retry = supervisor.run_once()

    assert retry.rotated_session is True
    assert actions == ["rotate:idle", "send", "send"]


def test_stale_fingerprint_alerts_once_after_successful_attempts_are_exhausted(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=13)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_rotate_g2_session", lambda state: None)
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    supervisor.run_once()
    supervisor_env["now_seconds"] += 120.0
    supervisor.run_once()
    supervisor_env["now_seconds"] += 120.0
    exhausted = supervisor.run_once()
    already_alerted = supervisor.run_once()

    assert exhausted.reason == "recovery_attempts_exhausted"
    assert already_alerted.reason == "alert_already_emitted"
    assert sends == ["send", "send"]


@pytest.mark.parametrize(
    ("nudged_at", "error"),
    [
        (None, "missing nudged_at"),
        (1_000_001.0, "future nudged_at"),
    ],
)
def test_succeeded_recovery_with_invalid_nudge_timestamp_fails_closed(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    nudged_at: float | None,
    error: str,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=14)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    state = supervisor._load_state()
    probe = supervisor._build_state_probe(state)
    recovery_key = f"{state.iteration}:{state.phase.value}:{probe.fingerprint}"
    supervisor_env["checkpoint_path"].write_text(
        json.dumps(
            {
                "setup_iteration_rotations": {},
                "recovery_records": {
                    recovery_key: {
                        "status": "succeeded",
                        "attempt_count": 1,
                        "nudged_at": nudged_at,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))

    with pytest.raises(SupervisorError, match=error):
        supervisor.run_once()


def test_state_fingerprint_progress_creates_an_independent_recovery_key(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=15)
    _make_state_stale(supervisor_env)
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    first = supervisor.run_once()
    _write_state(supervisor_env["state_path"], phase=Phase.CONSENSUS, iteration=15)
    _make_state_stale(supervisor_env)
    progressed = supervisor.run_once()

    assert first.recovery_key != progressed.recovery_key
    assert sends == ["send", "send"]


def test_concurrent_supervisors_allow_only_one_in_flight_send(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=21)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    first = _make_supervisor(supervisor_env, runner=runner)
    second = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(first, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(second, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    context = multiprocessing.get_context("fork")
    send_started = context.Event()
    release_send = context.Event()
    first_send_path = supervisor_env["state_dir"] / "first-send"
    unexpected_send_path = supervisor_env["state_dir"] / "unexpected-send"
    first_result_path = supervisor_env["state_dir"] / "first-result.json"
    second_result_path = supervisor_env["state_dir"] / "second-result.json"

    def blocking_send() -> None:
        first_send_path.write_text(str(os.getpid()), encoding="utf-8")
        send_started.set()
        if not release_send.wait(timeout=2.0):
            raise AssertionError("timed out waiting to release first send")

    def run_first() -> None:
        try:
            result = first.run_once()
            payload = {"outcome": result.outcome.value, "reason": result.reason}
        except BaseException as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        first_result_path.write_text(json.dumps(payload), encoding="utf-8")

    def run_second() -> None:
        try:
            result = second.run_once()
            payload = {"outcome": result.outcome.value, "reason": result.reason}
        except BaseException as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        second_result_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(first, "_send_recovery_message", blocking_send)
    monkeypatch.setattr(
        second,
        "_send_recovery_message",
        lambda: unexpected_send_path.write_text("sent", encoding="utf-8"),
    )
    first_process = context.Process(target=run_first)
    second_process = context.Process(target=run_second)
    first_process.start()
    assert send_started.wait(timeout=2.0)

    second_process.start()
    second_process.join(timeout=2.0)
    release_send.set()
    first_process.join(timeout=2.0)

    assert first_process.exitcode == 0
    assert second_process.exitcode == 0
    assert json.loads(first_result_path.read_text(encoding="utf-8"))["outcome"] == "nudged"
    assert json.loads(second_result_path.read_text(encoding="utf-8")) == {
        "outcome": "no_action",
        "reason": "recovery_in_flight",
    }
    assert first_send_path.exists()
    assert not unexpected_send_path.exists()


def test_failed_send_releases_claim_for_one_bounded_retry(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=22)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    first = _make_supervisor(supervisor_env, runner=runner)
    second = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(first, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(second, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        first,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(DevAPIError("send failed")),
    )
    sends: list[str] = []
    monkeypatch.setattr(second, "_send_recovery_message", lambda: sends.append("retry"))

    with pytest.raises(DevAPIError, match="send failed"):
        first.run_once()
    retry_result = second.run_once()

    assert retry_result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["retry"]
    checkpoint = json.loads(supervisor_env["checkpoint_path"].read_text(encoding="utf-8"))
    record = next(iter(checkpoint["recovery_records"].values()))
    assert record["status"] == "succeeded"
    assert record["attempt_count"] == 2


def test_failed_rotation_releases_claim_for_one_bounded_retry(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.SETUP_CONTEXT, iteration=27)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    first = _make_supervisor(supervisor_env, runner=runner)
    second = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(first, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(second, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        first,
        "_rotate_g2_session",
        lambda state: (_ for _ in ()).throw(DevAPIError(f"rotation failed from {state}")),
    )
    monkeypatch.setattr(
        first,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(AssertionError("send must follow successful rotation")),
    )
    actions: list[str] = []
    monkeypatch.setattr(
        second,
        "_rotate_g2_session",
        lambda state: actions.append(f"rotate:{state}"),
    )
    monkeypatch.setattr(second, "_send_recovery_message", lambda: actions.append("send"))

    with pytest.raises(DevAPIError, match="rotation failed"):
        first.run_once()
    retry_result = second.run_once()

    assert retry_result.outcome is SupervisorOutcome.NUDGED
    assert retry_result.rotated_session is True
    assert actions == ["rotate:idle", "send"]
    checkpoint = json.loads(supervisor_env["checkpoint_path"].read_text(encoding="utf-8"))
    record = next(iter(checkpoint["recovery_records"].values()))
    assert record["status"] == "succeeded"
    assert record["attempt_count"] == 2


def test_stale_claim_from_dead_process_is_reclaimed_once(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=24)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    state = supervisor._load_state()
    probe = supervisor._build_state_probe(state)
    recovery_key = f"{state.iteration}:{state.phase.value}:{probe.fingerprint}"
    checkpoint = {
        "setup_iteration_rotations": {},
        "recovery_records": {
            recovery_key: {
                "status": "in_flight",
                "attempt_count": 1,
                "claim_token": "dead-owner",
                "claim_pid": 424242,
                "claim_process_identity": "old-start-time",
                "claim_started_at": supervisor_env["now_seconds"] - 600.0,
            }
        },
    }
    supervisor_env["checkpoint_path"].write_text(json.dumps(checkpoint), encoding="utf-8")
    sends: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: sends.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert sends == ["send"]
    updated = json.loads(supervisor_env["checkpoint_path"].read_text(encoding="utf-8"))
    assert updated["recovery_records"][recovery_key]["attempt_count"] == 2


def test_stale_claim_with_live_owner_alerts_without_takeover(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=25)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    state = supervisor._load_state()
    probe = supervisor._build_state_probe(state)
    recovery_key = f"{state.iteration}:{state.phase.value}:{probe.fingerprint}"
    checkpoint = {
        "setup_iteration_rotations": {},
        "recovery_records": {
            recovery_key: {
                "status": "in_flight",
                "attempt_count": 1,
                "claim_token": "live-owner",
                "claim_pid": os.getpid(),
                "claim_started_at": supervisor_env["now_seconds"] - 600.0,
            }
        },
    }
    supervisor_env["checkpoint_path"].write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(AssertionError("live owner must not be overlapped")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "stale_recovery_claim_owner_alive"


def test_exhausted_stale_claim_alerts_without_send(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=26)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    state = supervisor._load_state()
    probe = supervisor._build_state_probe(state)
    recovery_key = f"{state.iteration}:{state.phase.value}:{probe.fingerprint}"
    checkpoint = {
        "setup_iteration_rotations": {},
        "recovery_records": {
            recovery_key: {
                "status": "in_flight",
                "attempt_count": 2,
                "claim_token": "dead-owner",
                "claim_pid": 424242,
                "claim_process_identity": "old-start-time",
                "claim_started_at": supervisor_env["now_seconds"] - 600.0,
            }
        },
    }
    supervisor_env["checkpoint_path"].write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(AssertionError("exhausted claim must not send")),
    )

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "recovery_attempts_exhausted"


def test_atomic_checkpoint_replace_failure_preserves_previous_file(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=23)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    original = '{"recovery_records": {}, "setup_iteration_rotations": {}}\n'
    supervisor_env["checkpoint_path"].write_text(original, encoding="utf-8")
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("idle", "", None))
    monkeypatch.setattr(
        supervisor,
        "_send_recovery_message",
        lambda: (_ for _ in ()).throw(AssertionError("live send must remain unreachable")),
    )

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr("gateway.autoresearch_supervisor.os.replace", fail_replace)

    with pytest.raises(SupervisorError, match="checkpoint"):
        supervisor.run_once()

    assert supervisor_env["checkpoint_path"].read_text(encoding="utf-8") == original
    assert list(supervisor_env["state_dir"].glob(".supervisor-state.json.*.tmp")) == []


def test_compaction_error_text_rotates_then_nudges(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=11)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    transcript = supervisor_env["sessions_path"].parent / "compaction.jsonl"
    transcript.write_text(
        (
            '{"type":"message","message":{"role":"assistant",'
            '"content":"CLI transcript compaction failed"}}\n'
        ),
        encoding="utf-8",
    )
    _write_main_sessions_store(
        supervisor_env["sessions_path"],
        {
            "agent:main:g2:compaction": {
                "status": "failed",
                "updatedAt": supervisor_env["now_ms"] - 500_000,
                "sessionFile": str(transcript),
            }
        },
    )
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[
            {
                "key": "agent:main:g2:compaction",
                "updatedAt": supervisor_env["now_ms"] - 5_000,
            }
        ],
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    actions: list[str] = []

    def fake_dev_http_json(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del payload
        if method == "GET" and path == "/_dev/state":
            return {"result": "idle"}
        if method == "GET" and path == "/_dev/display":
            return {"result": ""}
        raise AssertionError(f"unexpected dev request: {method} {path}")

    monkeypatch.setattr(supervisor, "_dev_http_json", fake_dev_http_json)
    monkeypatch.setattr(
        supervisor,
        "_rotate_g2_session",
        lambda state: actions.append(f"rotate:{state}"),
    )
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: actions.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert result.rotated_session is True
    assert actions == ["rotate:idle", "send"]


def test_g2_error_state_rotates_then_nudges(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.REVIEW, iteration=12)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    actions: list[str] = []
    monkeypatch.setattr(supervisor, "_read_g2_snapshot", lambda: G2Snapshot("error", "", None))
    monkeypatch.setattr(
        supervisor,
        "_rotate_g2_session",
        lambda state: actions.append(f"rotate:{state}"),
    )
    monkeypatch.setattr(supervisor, "_send_recovery_message", lambda: actions.append("send"))

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert result.rotated_session is True
    assert actions == ["rotate:error", "send"]


@pytest.mark.parametrize(
    "version",
    [
        "OpenClaw 2026.6.10 (oldbuild)",
        "OpenClaw 2026.6.12 (newbuild)",
    ],
)
def test_rejects_non_exact_openclaw_cli(
    supervisor_env: dict[str, Any],
    version: str,
) -> None:
    runner = FakeRunCommand(
        now_ms=supervisor_env["now_ms"],
        tasks=[],
        sessions=[],
        version=version,
    )
    supervisor = _make_supervisor(supervisor_env, runner=runner)

    with pytest.raises(OpenClawVersionError):
        supervisor.run_once()


def test_dev_api_unavailable_is_strict_failure(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.IMPLEMENTATION, iteration=12)
    _touch_old(
        [
            supervisor_env["state_path"],
            supervisor_env["repo_root"] / ".git" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "index",
            supervisor_env["repo_root"] / ".git" / "logs" / "HEAD",
            supervisor_env["repo_root"] / ".git" / "refs" / "heads" / "main",
        ],
        now_seconds=supervisor_env["now_seconds"],
        age_seconds=600.0,
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
    supervisor = _make_supervisor(supervisor_env, runner=runner)
    monkeypatch.setattr(
        supervisor,
        "_dev_http_json",
        lambda method, path, payload=None: (_ for _ in ()).throw(
            DevAPIError(f"unavailable: {method} {path}")
        ),
    )

    with pytest.raises(DevAPIError, match="unavailable"):
        supervisor.run_once()
