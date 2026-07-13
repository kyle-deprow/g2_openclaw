from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

import pytest
from gateway.autoresearch_runner import (
    AutoresearchState,
    ImplementationResultArtifact,
    Phase,
    ResearchMode,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
    AutoresearchSupervisor,
    ShutdownInterrupted,
    SupervisorConfig,
    SupervisorError,
    SupervisorOutcome,
    SupervisorResult,
    main,
)

SignalHandler = Callable[[int, FrameType | None], None]
SignalDisposition = SignalHandler | signal.Handlers


@dataclass(slots=True)
class SignalHarness:
    """Installs and invokes supervisor signal handlers without OS-level signals."""

    handlers: dict[int, SignalDisposition] = field(default_factory=dict)

    def install(self, signum: int, handler: SignalDisposition) -> SignalDisposition:
        previous = self.handlers.get(signum, signal.SIG_DFL)
        self.handlers[signum] = handler
        return previous

    def trigger(self, signum: int) -> None:
        handler = self.handlers[signum]
        assert callable(handler)
        handler(signum, None)


def _write_state(
    path: Path,
    *,
    phase: Phase = Phase.VERIFICATION,
    implementation_result: ImplementationResultArtifact | None = None,
) -> None:
    state = AutoresearchState(
        phase=phase,
        iteration=4,
        mode=ResearchMode.ALPHA_RESEARCH,
        implementation_result=implementation_result,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")


def _write_git_markers(repo_root: Path) -> list[Path]:
    git_dir = repo_root / ".git"
    paths = [git_dir / "HEAD", git_dir / "index", git_dir / "logs" / "HEAD"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("marker", encoding="utf-8")
    return paths


def _make_stale(paths: list[Path], *, now: float) -> None:
    for path in paths:
        os.utime(path, (now - 600.0, now - 600.0))


class FakeOpenClaw:
    def __init__(
        self,
        *,
        tasks: list[dict[str, object]] | None = None,
        shown_tasks: dict[str, dict[str, object]] | None = None,
        task_list_failures_before_success: int = 0,
    ) -> None:
        self.tasks = tasks or []
        self.shown_tasks = shown_tasks
        self.task_list_failures_before_success = task_list_failures_before_success
        self.task_list_calls = 0
        self.calls: list[list[str]] = []
        self.agent_payload: dict[str, object] = {
            "status": "accepted",
            "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
            "runId": "run-4",
        }

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
            return subprocess.CompletedProcess(command, 0, "OpenClaw 2026.6.11", "")
        if command[1:] == ["tasks", "list", "--status", "running", "--json"]:
            if self.task_list_calls < self.task_list_failures_before_success:
                self.task_list_calls += 1
                return subprocess.CompletedProcess(command, 1, "", "")
            self.task_list_calls += 1
            return subprocess.CompletedProcess(command, 0, json.dumps({"tasks": self.tasks}), "")
        if (
            len(command) == 5
            and command[1] == "tasks"
            and command[2] == "show"
            and command[4] == "--json"
        ):
            task_id = command[3]
            if self.shown_tasks is not None:
                task = self.shown_tasks[task_id]
            else:
                task = next(task for task in self.tasks if task.get("taskId") == task_id).copy()
                task.setdefault("status", "running")
            return subprocess.CompletedProcess(command, 0, json.dumps(task), "")
        if command[1:4] == ["gateway", "call", "agent"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.agent_payload), "")
        raise AssertionError(f"unexpected command: {command}")


class FailingTaskListOpenClaw(FakeOpenClaw):
    def __init__(self, *, before_failure: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._before_failure = before_failure

    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["tasks", "list", "--status", "running", "--json"]:
            del check, capture_output, text
            self.calls.append(command)
            if self._before_failure is not None:
                self._before_failure()
            return subprocess.CompletedProcess(command, 1, "", "poll failed")
        return super().__call__(command, check=check, capture_output=capture_output, text=text)


class FailingTaskShowOpenClaw(FakeOpenClaw):
    def __init__(
        self,
        *,
        tasks: list[dict[str, object]],
        before_failure: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(tasks=tasks)
        self._before_failure = before_failure

    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        if (
            len(command) == 5
            and command[1] == "tasks"
            and command[2] == "show"
            and command[4] == "--json"
        ):
            del check, capture_output, text
            self.calls.append(command)
            if self._before_failure is not None:
                self._before_failure()
            return subprocess.CompletedProcess(command, 1, "", "task missing")
        return super().__call__(command, check=check, capture_output=capture_output, text=text)


@dataclass(frozen=True, slots=True)
class SupervisorEnv:
    now: float
    state_path: Path
    repo_root: Path
    marker_paths: list[Path]
    sessions_path: Path
    executable: Path
    proc_root: Path
    checkpoint_path: Path


@pytest.fixture()
def supervisor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SupervisorEnv:
    monkeypatch.delenv("OPENCLAW_BIN", raising=False)
    now = 1_000_000.0
    state_path = tmp_path / "autoresearch" / "quantipy-state.json"
    repo_root = tmp_path / "quantipy"
    repo_root.mkdir()
    marker_paths = _write_git_markers(repo_root)
    sessions_path = tmp_path / "owner-sessions.json"
    sessions_path.write_text("{}", encoding="utf-8")
    executable = tmp_path / "bin" / "openclaw"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    return SupervisorEnv(
        now=now,
        state_path=state_path,
        repo_root=repo_root,
        marker_paths=marker_paths,
        sessions_path=sessions_path,
        executable=executable,
        proc_root=proc_root,
        checkpoint_path=tmp_path / "autoresearch" / "owner-recovery.json",
    )


def _supervisor(env: SupervisorEnv, fake: FakeOpenClaw) -> AutoresearchSupervisor:
    return AutoresearchSupervisor(
        SupervisorConfig(
            state_path=env.state_path,
            checkpoint_path=env.checkpoint_path,
            autoresearch_dir=env.state_path.parent,
            owner_sessions_path=env.sessions_path,
            target_repo=env.repo_root,
            proc_root=env.proc_root,
            default_openclaw_bin=env.executable,
        ),
        now=lambda: env.now,
        sleep=lambda _: None,
        run_command=fake,
    )


def _prepare_stale_state(env: SupervisorEnv, *, phase: Phase = Phase.VERIFICATION) -> None:
    _write_state(env.state_path, phase=phase)
    _make_stale([env.state_path, *env.marker_paths], now=env.now)


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


def test_supervisor_wakes_the_dedicated_owner_session_by_direct_rpc(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)

    result = supervisor.run_once()

    command = fake.calls[-1]
    assert result.outcome is SupervisorOutcome.NUDGED
    assert command[:6] == [
        str(supervisor_env.executable),
        "gateway",
        "call",
        "agent",
        "--json",
        "--params",
    ]
    payload = json.loads(command[6])
    assert payload["sessionKey"] == AUTORESEARCH_OWNER_SESSION_KEY
    assert payload["message"]
    assert payload["idempotencyKey"].startswith("autoresearch-")
    assert "--expect-final" not in command


def test_recovery_retries_use_distinct_idempotency_keys(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    clock = [supervisor_env.now]
    supervisor = AutoresearchSupervisor(
        SupervisorConfig(
            state_path=supervisor_env.state_path,
            checkpoint_path=supervisor_env.checkpoint_path,
            autoresearch_dir=supervisor_env.state_path.parent,
            owner_sessions_path=supervisor_env.sessions_path,
            target_repo=supervisor_env.repo_root,
            proc_root=supervisor_env.proc_root,
            default_openclaw_bin=supervisor_env.executable,
        ),
        now=lambda: clock[0],
        sleep=lambda _: None,
        run_command=fake,
    )

    first = supervisor.run_once()
    clock[0] += 121.0
    second = supervisor.run_once()

    agent_calls = [call for call in fake.calls if call[1:4] == ["gateway", "call", "agent"]]
    idempotency_keys = [json.loads(call[6])["idempotencyKey"] for call in agent_calls]
    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.NUDGED
    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] != idempotency_keys[1]


@pytest.mark.parametrize(
    "response",
    [
        {"status": "rejected", "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY, "runId": "run"},
        {"status": "accepted", "sessionKey": "agent:other:session", "runId": "run"},
        {"status": "accepted", "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY, "runId": ""},
    ],
)
def test_supervisor_fails_closed_for_an_invalid_wake_response(
    supervisor_env: SupervisorEnv, response: dict[str, object]
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    fake.agent_payload = response
    supervisor = _supervisor(supervisor_env, fake)

    with pytest.raises(SupervisorError, match="wake response"):
        supervisor.run_once()


def test_active_owner_lifecycle_for_the_exact_session_suppresses_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    now = supervisor_env.now
    sessions_path = supervisor_env.sessions_path
    sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(now * 1000) - 1_000,
                    "lastInteractionAt": int(now * 1000) - 1_000,
                    "startedAt": int(now * 1000) - 2_000,
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_owner_session"


def test_error_detection_reads_only_the_dedicated_owner_transcript(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    sessions_path = supervisor_env.sessions_path
    owner_transcript = sessions_path.parent / "owner.jsonl"
    owner_transcript.write_text("context overflow", encoding="utf-8")
    other_transcript = sessions_path.parent / "other.jsonl"
    other_transcript.write_text("maximum context length", encoding="utf-8")
    sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "idle",
                    "sessionFile": str(owner_transcript),
                },
                "agent:other:session": {"sessionFile": str(other_transcript)},
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "owner_session_error_recovery"


def test_stage_task_uses_the_public_task_summary_requester_and_owner_mapping(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    now = supervisor_env.now
    fake = FakeOpenClaw(
        tasks=[
            {
                "id": "review-1",
                "taskId": "review-1",
                "status": "running",
                "runtime": "subagent",
                "agentId": "reviewer",
                # OpenClaw 2026.6.11 maps TaskRecord.requesterSessionKey here.
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                # The normal sessions_spawn path sets ownerKey to that same requester.
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "task": "review the Quantipy autoresearch result",
                "updatedAt": int(now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"


def test_supervisor_retries_a_transient_empty_task_list_failure(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gateway.autoresearch_supervisor.time.sleep", lambda _seconds: None)
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "id": "review-1",
                "taskId": "review-1",
                "status": "running",
                "runtime": "subagent",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "updatedAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ],
        task_list_failures_before_success=1,
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"
    assert fake.task_list_calls == 2


def test_stage_task_uses_the_raw_cli_requester_and_owner_fields(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "review-raw-1",
                "status": "running",
                "runtime": "subagent",
                "agentId": "reviewer",
                "requesterSessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "lastEventAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"


def test_task_with_disagreeing_raw_and_summary_requester_keys_is_ambiguous(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "review-conflict",
                "agentId": "reviewer",
                "requesterSessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "sessionKey": "agent:other:session",
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "lastEventAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_stage_task_with_ambiguous_child_agent_fails_closed(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "review-1",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:implementer:task-child",
                "updatedAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_recovery_attempts_remain_bounded_after_repeated_wake_failures(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    fake.agent_payload = {
        "status": "rejected",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "runId": "run",
    }
    supervisor = _supervisor(supervisor_env, fake)

    with pytest.raises(SupervisorError):
        supervisor.run_once()
    with pytest.raises(SupervisorError):
        supervisor.run_once()

    result = supervisor.run_once()

    assert result.reason == "recovery_attempts_exhausted"


def test_active_target_writer_process_suppresses_owner_wake(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    process_dir = supervisor_env.proc_root / "1234"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"uv\x00run\x00pytest\x00")
    (process_dir / "cwd").symlink_to(supervisor_env.repo_root, target_is_directory=True)
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "target_repo_writer_active"


def test_active_writer_in_the_verified_implementation_workspace_suppresses_owner_wake(
    supervisor_env: SupervisorEnv,
) -> None:
    workspace = supervisor_env.repo_root.parent / "quantipy-worktree"
    workspace.mkdir()
    _write_state(
        supervisor_env.state_path,
        implementation_result=_implementation_result(workspace),
    )
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    process_dir = supervisor_env.proc_root / "1234"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"uv\x00run\x00pytest\x00")
    (process_dir / "cwd").symlink_to(workspace, target_is_directory=True)
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "target_repo_writer_active"


def test_lost_task_projection_with_active_persisted_writer_suppresses_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    workspace = supervisor_env.repo_root.parent / "quantipy-worktree"
    workspace.mkdir()
    _write_state(
        supervisor_env.state_path,
        implementation_result=_implementation_result(workspace),
    )
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(supervisor_env.now * 1000) - 600_000,
                    "lastInteractionAt": int(supervisor_env.now * 1000) - 600_000,
                    "startedAt": int(supervisor_env.now * 1000) - 700_000,
                }
            }
        ),
        encoding="utf-8",
    )
    process_dir = supervisor_env.proc_root / "1234"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"uv\x00run\x00pytest\x00")
    (process_dir / "cwd").symlink_to(workspace, target_is_directory=True)
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owner-turn": {**task, "status": "lost"}})

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "target_repo_writer_active"
    assert any(call[1:3] == ["tasks", "show"] for call in fake.calls)


def test_lost_task_projection_permits_recovery_after_persisted_writer_exits(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(supervisor_env.now * 1000) - 600_000,
                    "lastInteractionAt": int(supervisor_env.now * 1000) - 600_000,
                    "startedAt": int(supervisor_env.now * 1000) - 700_000,
                }
            }
        ),
        encoding="utf-8",
    )
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owner-turn": {**task, "status": "lost"}})

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED


def test_mismatched_canonical_task_show_fails_closed_with_an_alert(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FakeOpenClaw(
        tasks=[task],
        shown_tasks={"owner-turn": {**task, "taskId": "different", "status": "running"}},
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_task_show_failure_during_reconciliation_returns_a_controlled_alert(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FailingTaskShowOpenClaw(tasks=[task])

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_task_show_failure_preserves_shutdown_interruption(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    stop_requested = False

    def request_shutdown() -> None:
        nonlocal stop_requested
        stop_requested = True

    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FailingTaskShowOpenClaw(tasks=[task], before_failure=request_shutdown)

    with pytest.raises(ShutdownInterrupted):
        _supervisor(supervisor_env, fake).run_once(shutdown_requested=lambda: stop_requested)


def test_supervisor_rejects_non_object_task_list_entries(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    fake.tasks = [
        {
            "taskId": "owner-turn",
            "agentId": AUTORESEARCH_OWNER_AGENT_ID,
            "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
            "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        },
        "corrupted-task-entry",  # type: ignore[list-item]
    ]

    with pytest.raises(SupervisorError, match="non-object task"):
        _supervisor(supervisor_env, fake).run_once()


def test_supervisor_source_contains_no_g2_dev_surface() -> None:
    source = Path("gateway/autoresearch_supervisor.py").read_text(encoding="utf-8").lower()

    assert "/_dev" not in source
    assert "localhost:5173" not in source
    assert "agent:main:g2" not in source


def test_supervisor_source_does_not_manipulate_python_tracing() -> None:
    source = Path("gateway/autoresearch_supervisor.py").read_text(encoding="utf-8")

    assert "sys.settrace" not in source
    assert "sys.setprofile" not in source


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_treats_a_signal_before_command_failure_detection_as_clean_shutdown(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    signum: int,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(
        supervisor_env,
        FailingTaskListOpenClaw(before_failure=lambda: signal_harness.trigger(signum)),
    )
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    exit_code = supervisor.run_forever()

    shutdown_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "supervisor.shutdown_interrupted"
    ]
    assert exit_code == 0
    assert shutdown_events == [
        {
            "detail": (
                f"OpenClaw command failed ({supervisor_env.executable} tasks list "
                "--status running --json): poll failed"
            ),
            "event": "supervisor.shutdown_interrupted",
        }
    ]


def test_run_forever_reraises_a_command_failure_when_shutdown_was_not_requested(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FailingTaskListOpenClaw())

    with pytest.raises(SupervisorError) as raised:
        supervisor.run_forever()

    assert str(raised.value).endswith(": poll failed")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_preserves_command_failure_when_signal_follows_it_during_unwinding(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FailingTaskListOpenClaw())
    run_once = supervisor.run_once

    def fail_poll(*, shutdown_requested: Callable[[], bool]) -> None:
        try:
            run_once(shutdown_requested=shutdown_requested)
        finally:
            signal_harness.trigger(signum)

    monkeypatch.setattr(supervisor, "run_once", fail_poll)

    with pytest.raises(SupervisorError) as raised:
        supervisor.run_forever()

    assert str(raised.value).endswith(": poll failed")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_preserves_task_list_failure_when_signal_arrives_during_retry_delay(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FailingTaskListOpenClaw())

    def request_shutdown(_seconds: float) -> None:
        signal_harness.trigger(signum)

    monkeypatch.setattr("gateway.autoresearch_supervisor.time.sleep", request_shutdown)

    with pytest.raises(SupervisorError) as raised:
        supervisor.run_forever()

    assert "failed before shutdown during retry" in str(raised.value)
    assert str(raised.value).endswith(": poll failed")


def test_run_forever_keeps_shutdown_classification_after_repeated_mixed_signals(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)

    def request_shutdown() -> None:
        signal_harness.trigger(signal.SIGINT)

    supervisor = _supervisor(
        supervisor_env,
        FailingTaskListOpenClaw(before_failure=request_shutdown),
    )
    run_once = supervisor.run_once

    def preserve_classified_shutdown(*, shutdown_requested: Callable[[], bool]) -> None:
        try:
            run_once(shutdown_requested=shutdown_requested)
        except ShutdownInterrupted:
            signal_harness.trigger(signal.SIGTERM)
            signal_harness.trigger(signal.SIGINT)
            raise

    monkeypatch.setattr(supervisor, "run_once", preserve_classified_shutdown)

    exit_code = supervisor.run_forever()

    assert exit_code == 0


def test_main_returns_error_for_a_poll_failure_without_a_shutdown_signal(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_openclaw = supervisor_env.executable.parent / "missing-openclaw"
    monkeypatch.setenv("OPENCLAW_BIN", str(missing_openclaw))

    exit_code = main(["--state-path", str(supervisor_env.state_path)])

    assert exit_code == 1


def test_main_once_returns_error_for_a_poll_failure(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_openclaw = supervisor_env.executable.parent / "missing-openclaw"
    monkeypatch.setenv("OPENCLAW_BIN", str(missing_openclaw))

    exit_code = main(["--once", "--state-path", str(supervisor_env.state_path)])

    assert exit_code == 1


def test_run_forever_does_not_poll_again_after_shutdown_during_sleep(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_harness = SignalHarness()
    poll_count = 0
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())

    def poll_once(*, shutdown_requested: Callable[[], bool]) -> None:
        nonlocal poll_count
        poll_count += 1

    def request_stop_while_sleeping(_: float) -> None:
        signal_harness.trigger(signal.SIGTERM)

    monkeypatch.setattr(supervisor, "run_once", poll_once)
    monkeypatch.setattr(supervisor, "_sleep", request_stop_while_sleeping)

    exit_code = supervisor.run_forever()

    assert exit_code == 0
    assert poll_count == 1
