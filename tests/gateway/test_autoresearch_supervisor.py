from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from gateway.autoresearch_runner import AutoresearchState, Phase, ResearchMode
from gateway.autoresearch_supervisor import (
    AutoresearchSupervisor,
    DevAPIError,
    G2Snapshot,
    OpenClawVersionError,
    SupervisorConfig,
    SupervisorError,
    SupervisorOutcome,
)


def _write_state(
    path: Path,
    *,
    phase: Phase,
    iteration: int,
) -> None:
    state = AutoresearchState(
        phase=phase,
        iteration=iteration,
        mode=ResearchMode.ALPHA_RESEARCH,
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


def _write_main_sessions_store(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


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
    ) -> None:
        self.now_ms = now_ms
        self.tasks = tasks
        self.sessions = sessions
        self.version = version
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
        raise AssertionError(f"unexpected command: {command}")


@pytest.fixture()
def supervisor_env(tmp_path: Path) -> dict[str, Any]:
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
) -> AutoresearchSupervisor:
    config = SupervisorConfig(
        state_path=env["state_path"],
        checkpoint_path=env["checkpoint_path"],
        autoresearch_dir=env["state_dir"],
        main_sessions_path=env["sessions_path"],
        target_repo=env["repo_root"],
        proc_root=env["proc_root"],
        default_openclaw_bin=env["openclaw_bin"],
        grace_period_seconds=120.0,
    )
    return AutoresearchSupervisor(
        config,
        now=lambda: env["now_seconds"],
        sleep=lambda _: None,
        run_command=runner,
        urlopen=_forbid_live_http,
    )


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
                "startedAt": supervisor_env["now_ms"] - 500_000,
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


def test_multiple_fresh_main_g2_sessions_alert_without_nudge(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.FIX_TEST, iteration=19)
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
    proc_dir = supervisor_env["proc_root"] / "424242"
    proc_dir.mkdir()
    (proc_dir / "cmdline").write_bytes(b"python\x00-m\x00pytest\x00")
    (proc_dir / "cwd").symlink_to(supervisor_env["repo_root"], target_is_directory=True)
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


def test_duplicate_fingerprint_alerts_without_retry(
    supervisor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(supervisor_env["state_path"], phase=Phase.VERIFICATION, iteration=9)
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
    assert second.outcome is SupervisorOutcome.ALERT
    assert second.reason == "duplicate_recovery_blocked"
    assert sends == ["send"]


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
                "updatedAt": supervisor_env["now_ms"] - 500_000,
                "sessionFile": str(transcript),
            }
        },
    )
    runner = FakeRunCommand(now_ms=supervisor_env["now_ms"], tasks=[], sessions=[])
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
