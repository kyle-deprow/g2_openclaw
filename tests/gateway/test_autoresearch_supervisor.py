from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from gateway.autoresearch_runner import (
    AutoresearchState,
    ImplementationResultArtifact,
    Phase,
    ResearchMode,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_SESSION_KEY,
    AutoresearchSupervisor,
    SupervisorConfig,
    SupervisorError,
    SupervisorOutcome,
)


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
    def __init__(self, *, tasks: list[dict[str, object]] | None = None) -> None:
        self.tasks = tasks or []
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
            return subprocess.CompletedProcess(command, 0, json.dumps({"tasks": self.tasks}), "")
        if command[1:4] == ["gateway", "call", "agent"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.agent_payload), "")
        raise AssertionError(f"unexpected command: {command}")


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


def test_stage_task_with_ambiguous_child_agent_is_ignored(
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

    assert result.reason == "recovery_message_sent"


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


def test_supervisor_source_contains_no_g2_dev_surface() -> None:
    source = Path("gateway/autoresearch_supervisor.py").read_text(encoding="utf-8").lower()

    assert "/_dev" not in source
    assert "localhost:5173" not in source
    assert "agent:main:g2" not in source
    assert "g2" not in source
