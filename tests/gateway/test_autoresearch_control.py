from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from gateway.autoresearch_control import (
    DEFAULT_SUPERVISOR_SERVICE_NAME,
    AutoresearchControl,
    ControlConfig,
    ControlError,
    SystemdSupervisorServiceController,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
)


def _operator_precondition_state_json() -> str:
    return json.dumps(
        {
            "phase": "implementation",
            "iteration": 26,
            "mode": "data_infra_g0",
            "consensus_history": [
                {
                    "round_number": 1,
                    "status": "MAJORITY",
                    "winner_theory_id": "i26-operator-evidence-precondition",
                    "winner_theory_family": "no-code-operator-evidence-precondition",
                    "majority_count": 5,
                    "majority_agent_ids": [
                        "debater-microstructure",
                        "debater-data",
                        "debater-skeptic",
                        "debater-theory",
                        "debater-implementation",
                    ],
                    "dissenting_positions": [],
                    "novelty_score": 1.0,
                    "theory_score": 9.0,
                    "implementation_risk_score": 1.0,
                    "data_adequacy_score": 1.0,
                    "overfit_risk_score": 1.0,
                    "expected_net_sharpe": 0.0,
                    "rejection_reasons": ["missing operator evidence"],
                    "implementation_brief": (
                        "Do not enter ENGINEER and do not modify Quantipy. "
                        "The operator must supply the manifest."
                    ),
                    "dissent_summary": "No semantic dissent.",
                }
            ],
        }
    )


class FakeOpenClaw:
    def __init__(
        self,
        *,
        tasks: list[dict[str, object]] | None = None,
        task_snapshots: list[list[dict[str, object]]] | None = None,
        shown_tasks: dict[str, dict[str, object]] | None = None,
        cancel_response: dict[str, object] | None = None,
        abort_response: dict[str, object] | None = None,
        events: list[str] | None = None,
        task_list_failures_before_success: int = 0,
    ) -> None:
        self.tasks = tasks or []
        self.task_snapshots = task_snapshots
        self.shown_tasks = shown_tasks
        self.cancel_response = cancel_response
        self.abort_response = abort_response
        self.task_list_failures_before_success = task_list_failures_before_success
        self.task_list_calls = 0
        self.calls: list[list[str]] = []
        self.events = events

    def __call__(
        self, command: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        self.calls.append(command)
        event_names = {
            "agent": "rpc:wake",
            "sessions.abort": "rpc:abort",
            "tasks.cancel": "rpc:cancel",
            "sessions.delete": "rpc:delete",
        }
        if self.events is not None and len(command) > 3:
            event_name = event_names.get(command[3])
            if event_name is not None:
                self.events.append(event_name)
        if command[-1] == "--version":
            if self.events is not None:
                self.events.append("rpc:version")
            return subprocess.CompletedProcess(command, 0, "OpenClaw 2026.6.11", "")
        if command[1:] == ["tasks", "list", "--status", "running", "--json"]:
            if self.events is not None:
                self.events.append("rpc:list")
            if self.task_list_calls < self.task_list_failures_before_success:
                self.task_list_calls += 1
                return subprocess.CompletedProcess(command, 1, "", "")
            if self.task_snapshots is None:
                tasks = self.tasks
            else:
                index = min(self.task_list_calls, len(self.task_snapshots) - 1)
                tasks = self.task_snapshots[index]
            self.task_list_calls += 1
            return subprocess.CompletedProcess(command, 0, json.dumps({"tasks": tasks}), "")
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
                snapshots = self.task_snapshots or [self.tasks]
                task = next(
                    task
                    for snapshot in snapshots
                    for task in snapshot
                    if task.get("taskId") == task_id
                ).copy()
                task.setdefault("status", "running")
            return subprocess.CompletedProcess(command, 0, json.dumps(task), "")
        if command[1:4] == ["gateway", "call", "agent"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "status": "accepted",
                        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                        "runId": "wake-1",
                    }
                ),
                "",
            )
        if command[1:4] == ["gateway", "call", "tasks.cancel"]:
            params = json.loads(command[6])
            response = self.cancel_response or {
                "found": True,
                "cancelled": True,
                "task": {"id": params["taskId"], "taskId": params["taskId"]},
            }
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(response),
                "",
            )
        if command[1:4] == ["gateway", "call", "sessions.abort"]:
            params = json.loads(command[6])
            response = self.abort_response or {
                "ok": True,
                "abortedRunId": params["runId"],
                "status": "aborted",
            }
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(response),
                "",
            )
        if command[1:4] == ["gateway", "call", "sessions.delete"]:
            params = json.loads(command[6])
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"ok": True, "deleted": False, "absent": True, "key": params["key"]}),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")


class FakeSupervisorService:
    def __init__(self, events: list[str], *, active: bool = False) -> None:
        self.events = events
        self._active = active

    def ensure_started(self) -> None:
        self.events.append("service:start")
        self._active = True

    def stop(self) -> None:
        self.events.append("service:stop")
        self._active = False

    def is_active(self) -> bool:
        self.events.append("service:status")
        return self._active


class FailingSupervisorService(FakeSupervisorService):
    def __init__(self, events: list[str], *, fail_on: str) -> None:
        super().__init__(events)
        self._fail_on = fail_on

    def ensure_started(self) -> None:
        super().ensure_started()
        if self._fail_on == "start":
            raise ControlError("service start failed")

    def stop(self) -> None:
        super().stop()
        if self._fail_on == "stop":
            raise ControlError("service stop failed")


def test_systemd_controller_uses_durable_enable_and_disable_transitions() -> None:
    calls: list[list[str]] = []

    def run_command(
        command: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = SystemdSupervisorServiceController(
        command_prefix=("systemctl", "--user"),
        service_name=DEFAULT_SUPERVISOR_SERVICE_NAME,
        run_command=run_command,
    )

    controller.ensure_started()
    controller.stop()

    assert calls == [
        [
            "systemctl",
            "--user",
            "enable",
            "--now",
            DEFAULT_SUPERVISOR_SERVICE_NAME,
        ],
        [
            "systemctl",
            "--user",
            "disable",
            "--now",
            DEFAULT_SUPERVISOR_SERVICE_NAME,
        ],
    ]


@pytest.fixture()
def control_env(tmp_path: Path) -> tuple[ControlConfig, Path]:
    executable = tmp_path / "openclaw"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(
        '{"phase":"review","iteration":7,"mode":"alpha_research"}', encoding="utf-8"
    )
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text("{}", encoding="utf-8")
    return ControlConfig(
        state_path=state_path, owner_sessions_path=sessions_path, default_openclaw_bin=executable
    ), executable


def test_wake_dispatches_to_the_dedicated_session_without_waiting_for_final(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, executable = control_env
    events: list[str] = []
    fake = FakeOpenClaw(events=events)
    service = FakeSupervisorService(events)

    AutoresearchControl(config, run_command=fake, service_controller=service).wake()

    command = fake.calls[-1]
    assert command[:6] == [str(executable), "gateway", "call", "agent", "--json", "--params"]
    params = json.loads(command[6])
    assert params["sessionKey"] == AUTORESEARCH_OWNER_SESSION_KEY
    assert (
        "cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next "
        "/home/dev/.openclaw/autoresearch/quantipy-state.json"
    ) in params["message"]
    assert "--expect-final" not in command
    assert events == ["rpc:version", "rpc:wake", "service:start"]


def test_each_manual_wake_is_a_new_logical_idempotency_request(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    first = FakeOpenClaw()
    second = FakeOpenClaw()

    AutoresearchControl(
        config, run_command=first, service_controller=FakeSupervisorService([])
    ).wake()
    AutoresearchControl(
        config, run_command=second, service_controller=FakeSupervisorService([])
    ).wake()

    first_key = json.loads(first.calls[-1][6])["idempotencyKey"]
    second_key = json.loads(second.calls[-1][6])["idempotencyKey"]
    assert first_key.startswith("autoresearch-manual-wake-")
    assert second_key.startswith("autoresearch-manual-wake-")
    assert first_key != second_key


def test_control_uses_the_installed_quantipy_supervisor_unit() -> None:
    assert DEFAULT_SUPERVISOR_SERVICE_NAME == "quantipy-autoresearch-supervisor.service"


def test_wake_rolls_back_the_owner_session_when_supervisor_start_fails(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(events=events)
    service = FailingSupervisorService(events, fail_on="start")

    with pytest.raises(ControlError, match="rolled back"):
        AutoresearchControl(config, run_command=fake, service_controller=service).wake()

    assert events == [
        "rpc:version",
        "rpc:wake",
        "service:start",
        "service:stop",
        "rpc:abort",
        "rpc:delete",
    ]


def test_stop_does_not_cancel_or_delete_when_supervisor_stop_fails(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(events=events)
    service = FailingSupervisorService(events, fail_on="stop")

    with pytest.raises(ControlError, match="service stop failed"):
        AutoresearchControl(config, run_command=fake, service_controller=service).stop()

    assert events == ["rpc:version", "rpc:list", "service:stop"]


def test_wake_reports_abort_failure_but_still_deletes_owner_session(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(
        events=events,
        abort_response={"ok": True, "abortedRunId": "wrong", "status": "aborted"},
    )
    service = FailingSupervisorService(events, fail_on="start")

    with pytest.raises(ControlError, match="owner run abort"):
        AutoresearchControl(config, run_command=fake, service_controller=service).wake()

    assert events[-2:] == ["rpc:abort", "rpc:delete"]


def test_stop_cancels_only_tasks_with_the_exact_owner_session(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "owned",
                "id": "owned",
                "agentId": "reviewer",
                "requesterSessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
            },
            {
                "taskId": "other",
                "agentId": "reviewer",
                "requesterSessionKey": "agent:other:session",
            },
        ]
    )
    events: list[str] = []
    fake.events = events
    service = FakeSupervisorService(events, active=True)

    result = AutoresearchControl(config, run_command=fake, service_controller=service).stop()

    cancel_calls = [call for call in fake.calls if call[1:4] == ["gateway", "call", "tasks.cancel"]]
    delete_calls = [
        call for call in fake.calls if call[1:4] == ["gateway", "call", "sessions.delete"]
    ]
    assert result.cancelled_task_ids == ("owned",)
    assert result.deleted_session is False
    assert len(cancel_calls) == 1
    assert json.loads(cancel_calls[0][6])["taskId"] == "owned"
    assert json.loads(delete_calls[0][6]) == {
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "deleteTranscript": False,
        "key": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    assert events == [
        "rpc:version",
        "rpc:list",
        "service:stop",
        "rpc:list",
        "rpc:cancel",
        "rpc:delete",
    ]


def test_stop_revalidates_tasks_after_stopping_the_supervisor(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    task: dict[str, object] = {
        "taskId": "owned",
        "id": "owned",
        "agentId": "reviewer",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:reviewer:task-child",
    }
    events: list[str] = []
    fake = FakeOpenClaw(task_snapshots=[[task], [task]], events=events)

    AutoresearchControl(
        config, run_command=fake, service_controller=FakeSupervisorService(events)
    ).stop()

    assert events[:4] == ["rpc:version", "rpc:list", "service:stop", "rpc:list"]


def test_stop_rejects_a_cancel_response_with_a_mismatched_returned_task(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "owned",
                "id": "owned",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
            }
        ],
        cancel_response={
            "found": True,
            "cancelled": True,
            "task": {"id": "other", "taskId": "other"},
        },
    )

    with pytest.raises(ControlError, match="cancellation response"):
        AutoresearchControl(
            config, run_command=fake, service_controller=FakeSupervisorService([])
        ).stop()


def test_status_is_read_only_and_reports_only_owner_scoped_tasks(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "owned",
                "id": "owned",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
            },
            {
                "taskId": "other",
                "agentId": "reviewer",
                "sessionKey": "agent:other:session",
            },
        ]
    )

    status = AutoresearchControl(
        config,
        run_command=fake,
        service_controller=FakeSupervisorService([], active=True),
    ).status()

    assert status.tasks[0].task_id == "owned"
    assert status.supervisor_active is True
    assert all(call[1:4] != ["gateway", "call", "tasks.cancel"] for call in fake.calls)
    assert all(call[1:4] != ["gateway", "call", "sessions.delete"] for call in fake.calls)


def test_status_normalizes_operator_precondition_implementation_state(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    config.state_path.write_text(_operator_precondition_state_json(), encoding="utf-8")

    status = AutoresearchControl(
        config,
        run_command=FakeOpenClaw(),
        service_controller=FakeSupervisorService([], active=False),
    ).status()

    assert status.phase == "decision_log"


def test_status_retries_a_transient_empty_task_list_failure(
    control_env: tuple[ControlConfig, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gateway.autoresearch_supervisor.time.sleep", lambda _seconds: None)
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "owned",
                "id": "owned",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
            }
        ],
        task_list_failures_before_success=1,
    )

    status = AutoresearchControl(
        config,
        run_command=fake,
        service_controller=FakeSupervisorService([], active=True),
    ).status()

    assert status.tasks[0].task_id == "owned"
    assert fake.task_list_calls == 2


def test_stop_fails_closed_when_a_owned_task_has_conflicting_session_provenance(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "ambiguous",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": "agent:other:session",
            }
        ]
    )

    with pytest.raises(ControlError, match="ambiguous"):
        AutoresearchControl(
            config, run_command=fake, service_controller=FakeSupervisorService([])
        ).stop()


def test_stop_fails_closed_when_the_owner_agent_task_has_no_session_provenance(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(tasks=[{"taskId": "ambiguous", "agentId": AUTORESEARCH_OWNER_AGENT_ID}])

    with pytest.raises(ControlError, match="ambiguous"):
        AutoresearchControl(
            config, run_command=fake, service_controller=FakeSupervisorService([])
        ).stop()


def test_pm_owner_turn_without_a_child_session_is_supported(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "pm-turn",
                "id": "pm-turn",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
            }
        ]
    )

    result = AutoresearchControl(
        config, run_command=fake, service_controller=FakeSupervisorService([])
    ).stop()

    assert result.cancelled_task_ids == ("pm-turn",)


def test_control_rejects_disagreeing_legacy_and_canonical_task_ids(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "canonical",
                "id": "legacy",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
            }
        ]
    )

    with pytest.raises(ControlError, match="taskId"):
        AutoresearchControl(
            config, run_command=fake, service_controller=FakeSupervisorService([])
        ).stop()


def test_status_excludes_a_lost_canonical_task_projection(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    task: dict[str, object] = {
        "taskId": "owned",
        "id": "owned",
        "agentId": "reviewer",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:reviewer:task-child",
    }
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owned": {**task, "status": "lost"}})

    status = AutoresearchControl(
        config,
        run_command=fake,
        service_controller=FakeSupervisorService([], active=True),
    ).status()

    assert status.tasks == ()


def test_stop_does_not_cancel_a_lost_canonical_task_projection(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    task: dict[str, object] = {
        "taskId": "owned",
        "id": "owned",
        "agentId": "reviewer",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:reviewer:task-child",
    }
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owned": {**task, "status": "lost"}})

    result = AutoresearchControl(
        config, run_command=fake, service_controller=FakeSupervisorService([])
    ).stop()

    assert result.cancelled_task_ids == ()
    assert not any(call[1:4] == ["gateway", "call", "tasks.cancel"] for call in fake.calls)


def test_control_source_contains_no_g2_dev_surface() -> None:
    source = Path("gateway/autoresearch_control.py").read_text(encoding="utf-8").lower()

    assert "/_dev" not in source
    assert "localhost:5173" not in source
    assert "agent:main:g2" not in source
    assert "g2" not in source
