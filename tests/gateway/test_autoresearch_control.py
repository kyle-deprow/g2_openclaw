from __future__ import annotations

import fcntl
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import gateway.autoresearch_runs as autoresearch_runs
import pytest
from gateway.autoresearch_control import (
    DEFAULT_SUPERVISOR_SERVICE_NAME,
    AutoresearchControl,
    ControlConfig,
    ControlError,
    SystemdDetachedRunController,
    SystemdSupervisorServiceController,
)
from gateway.autoresearch_readiness import (
    PLATFORM_READINESS_SCHEMA_VERSION,
    EvidenceId,
    PlatformReadinessManifest,
    canonical_platform_capabilities,
)
from gateway.autoresearch_runner import (
    AutoresearchState,
    ConsensusResultArtifact,
    ConsensusStatus,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    Phase,
    ResearchMode,
    build_authoritative_state_reference,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
    OpenClawUnavailableError,
    RecoveryRecord,
    RecoveryStatus,
    ShutdownRequested,
    SupervisorCheckpoint,
    SupervisorError,
)

from tests.gateway.autoresearch_fixtures import write_xnys_calendar_evidence


def _ready_manifest(path: Path) -> PlatformReadinessManifest:
    evidence: dict[str, dict[str, str | None]] = {}
    path.mkdir(parents=True, exist_ok=True)
    for evidence_id in EvidenceId:
        evidence_path = path / f"{evidence_id.value}.json"
        if evidence_id is EvidenceId.XNYS_TRADING_CALENDAR:
            write_xnys_calendar_evidence(evidence_path)
        elif evidence_id is EvidenceId.QUANTIPY_DATA_CONTRACT:
            evidence_path.write_text(json.dumps({"quantipy_commit": "a" * 40}), encoding="utf-8")
        else:
            evidence_path.write_text(f"{evidence_id.value}\n", encoding="utf-8")
        evidence[evidence_id.value] = {
            "path": str(evidence_path),
            "sha256": sha256(evidence_path.read_bytes()).hexdigest(),
            "reason": None,
        }
    return PlatformReadinessManifest.from_dict(
        {
            "schema_version": PLATFORM_READINESS_SCHEMA_VERSION,
            "status": "READY",
            "manifest_id": "control-manifest-1",
            "snapshot_id": "control-snapshot-1",
            "evidence": evidence,
            "capabilities": canonical_platform_capabilities().to_dict(),
            "reason": None,
        }
    )


def _operator_precondition_state_json() -> str:
    return json.dumps(
        AutoresearchState(
            phase=Phase.IMPLEMENTATION,
            iteration=26,
            mode=ResearchMode.DATA_INFRA_G0,
            consensus_history=(
                ConsensusResultArtifact(
                    round_number=1,
                    status=ConsensusStatus.MAJORITY,
                    winner_theory_id="i26-operator-evidence-precondition",
                    winner_theory_family="no-code-operator-evidence-precondition",
                    majority_count=5,
                    majority_agent_ids=(
                        "debater_microstructure",
                        "debater_data",
                        "debater_skeptic",
                        "debater_theory",
                        "debater_implementation",
                    ),
                    dissenting_positions=(),
                    novelty_score=1.0,
                    theory_score=9.0,
                    implementation_risk_score=1.0,
                    data_adequacy_score=1.0,
                    overfit_risk_score=1.0,
                    expected_net_sharpe=0.0,
                    rejection_reasons=("missing operator evidence",),
                    implementation_brief=(
                        "Do not enter ENGINEER and do not modify Quantipy. "
                        "The operator must supply the manifest."
                    ),
                    dissent_summary="No semantic dissent.",
                ),
            ),
        ).to_dict()
    )


class FakeOpenClaw:
    def __init__(
        self,
        *,
        tasks: list[dict[str, object]] | None = None,
        task_snapshots: list[list[dict[str, object]]] | None = None,
        shown_tasks: dict[str, dict[str, object]] | None = None,
        wake_error: str | None = None,
        cancel_response: dict[str, object] | None = None,
        abort_response: dict[str, object] | None = None,
        events: list[str] | None = None,
        task_list_failures_before_success: int = 0,
    ) -> None:
        self.tasks = tasks or []
        self.task_snapshots = task_snapshots
        self.shown_tasks = shown_tasks
        self.wake_error = wake_error
        self.cancel_response = cancel_response
        self.abort_response = abort_response
        self.task_list_failures_before_success = task_list_failures_before_success
        self.task_list_calls = 0
        self.rpc_calls: list[tuple[str, Mapping[str, object]]] = []
        self.events = events

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        del shutdown_requested
        self.rpc_calls.append((method, params))
        if self.events is not None:
            event_names = {
                "agent": "rpc:wake",
                "sessions.abort": "rpc:abort",
                "tasks.cancel": "rpc:cancel",
                "sessions.delete": "rpc:delete",
                "tasks.list": "rpc:list",
            }
            event_name = event_names.get(method)
            if event_name is not None:
                self.events.append(event_name)
        if method == "tasks.list":
            if self.task_list_calls < self.task_list_failures_before_success:
                self.task_list_calls += 1
                raise OpenClawUnavailableError("poll failed")
            tasks = (
                self.tasks
                if self.task_snapshots is None
                else self.task_snapshots[min(self.task_list_calls, len(self.task_snapshots) - 1)]
            )
            self.task_list_calls += 1
            return {"tasks": tasks}
        if method == "tasks.get":
            task_id = params["taskId"]
            assert isinstance(task_id, str)
            if self.shown_tasks is not None:
                return {"task": self.shown_tasks[task_id]}
            snapshots = self.task_snapshots or [self.tasks]
            task = next(
                task for snapshot in snapshots for task in snapshot if task.get("taskId") == task_id
            ).copy()
            task.setdefault("status", "running")
            return {"task": task}
        if method == "agent":
            if self.wake_error is not None:
                raise SupervisorError(self.wake_error)
            return {
                "status": "accepted",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "runId": "wake-1",
            }
        if method == "tasks.cancel":
            task_id = params["taskId"]
            return self.cancel_response or {
                "found": True,
                "cancelled": True,
                "task": {"id": task_id, "taskId": task_id},
            }
        if method == "sessions.abort":
            return self.abort_response or {
                "ok": True,
                "abortedRunId": params["runId"],
                "status": "aborted",
            }
        if method == "sessions.delete":
            return {"ok": True, "deleted": False, "absent": True, "key": params["key"]}
        raise AssertionError(f"unexpected RPC method: {method}")


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


class FakeDetachedRunController:
    def __init__(
        self,
        *,
        runs_root: Path,
        on_unit_check: Callable[[], None] | None = None,
        operator_stop_signal_number: int | None = None,
    ) -> None:
        self.runs_root = runs_root
        self.stopped_units: list[str] = []
        self._on_unit_check = on_unit_check
        self._operator_stop_signal_number = operator_stop_signal_number

    def stop_unit(self, unit: str) -> None:
        self.stopped_units.append(unit)
        for run_dir in self.runs_root.iterdir():
            try:
                record = autoresearch_runs.read_run_record(
                    run_dir=run_dir, runs_root=self.runs_root
                )
            except autoresearch_runs.AutoresearchRunRecordError:
                continue
            if record.status.systemd_unit == unit:
                autoresearch_runs.complete_run(
                    run_dir=run_dir,
                    runs_root=self.runs_root,
                    exit_code=143,
                    signal_number=self._operator_stop_signal_number,
                    peak_rss_bytes=None,
                    failure_classification=autoresearch_runs.RunFailureClassification.OPERATOR_STOPPED,
                )

    def is_active(self, unit: str) -> bool:
        if self._on_unit_check is not None:
            callback = self._on_unit_check
            self._on_unit_check = None
            callback()
        return False

    def is_pid_alive(self, pid: int | None) -> bool:
        return False


def _prepare_state_bound_detached_run(
    config: ControlConfig,
    tmp_path: Path,
    *,
    name: str,
    unit: str,
    start: bool,
) -> Path:
    state = AutoresearchState.from_dict(json.loads(config.state_path.read_text(encoding="utf-8")))
    run_dir = config.runs_root / name
    command = ("uv", "run", "pytest")
    manifest_path = tmp_path / f"{name}-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": state.iteration,
                "phase": state.phase.value,
                "attempt": 1,
                "task_label": "autoresearch-i7-review-r1-a1",
                "state_reference_sha256": build_authoritative_state_reference(
                    state, state_path=config.state_path
                ).sha256(),
                "instruction_manifest_sha256": "a" * 64,
                "run_directory": str(run_dir),
                "working_directory": str(tmp_path),
                "command_sha256": autoresearch_runs.command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=config.runs_root,
        command=command,
    )
    if start:
        autoresearch_runs.start_run(
            run_dir=run_dir,
            runs_root=config.runs_root,
            pid=999_999,
            systemd_unit=unit,
        )
    return run_dir


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


@pytest.mark.parametrize(
    ("is_active_returncode", "show_output", "expected_active"),
    (
        (3, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n", False),
        (4, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", False),
    ),
)
def test_detached_systemd_controller_accepts_only_known_inactive_unit_states(
    is_active_returncode: int,
    show_output: str,
    expected_active: bool,
) -> None:
    # Arrange
    calls: list[list[str]] = []

    def run_command(
        command: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        calls.append(command)
        if command[2] == "is-active":
            return subprocess.CompletedProcess(command, is_active_returncode, "", "")
        return subprocess.CompletedProcess(command, 0, show_output, "")

    controller = SystemdDetachedRunController(run_command)

    # Act
    active = controller.is_active("openclaw-long-task-1-1.service")

    # Assert
    assert active is expected_active


def test_stop_rejects_a_signal_terminated_operator_stopped_record(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, tmp_path = control_env
    _prepare_state_bound_detached_run(
        config,
        tmp_path,
        name="signal-terminated",
        unit="openclaw-long-task-1-1.service",
        start=True,
    )
    # Act / Assert
    with pytest.raises(ControlError, match="operator_stopped terminal status"):
        AutoresearchControl(
            config,
            task_gateway=FakeOpenClaw(),
            service_controller=FakeSupervisorService([]),
            detached_run_controller=FakeDetachedRunController(
                runs_root=config.runs_root,
                operator_stop_signal_number=15,
            ),
        ).stop()


@pytest.mark.parametrize(
    "show_result",
    (
        subprocess.CompletedProcess([], 1, "", "unit lookup failed"),
        subprocess.CompletedProcess(
            [], 0, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n", ""
        ),
        subprocess.CompletedProcess(
            [], 0, "LoadState=not-found\nActiveState=active\nSubState=dead\n", ""
        ),
        subprocess.CompletedProcess(
            [], 0, "LoadState=not-found\nActiveState=inactive\nSubState=exited\n", ""
        ),
        subprocess.CompletedProcess([], 0, "LoadState=not-found\nActiveState=inactive\n", ""),
    ),
)
def test_detached_systemd_controller_rejects_unknown_collected_unit_state(
    show_result: subprocess.CompletedProcess[str],
) -> None:
    # Arrange
    def run_command(
        command: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        if command[2] == "is-active":
            return subprocess.CompletedProcess(command, 4, "", "")
        return subprocess.CompletedProcess(
            command,
            show_result.returncode,
            show_result.stdout,
            show_result.stderr,
        )

    controller = SystemdDetachedRunController(run_command)

    # Act / Assert
    with pytest.raises(ControlError, match="detached unit status failed"):
        controller.is_active("openclaw-long-task-1-1.service")


@pytest.fixture()
def control_env(tmp_path: Path) -> tuple[ControlConfig, Path]:
    state_path = tmp_path / "quantipy-state.json"
    readiness = _ready_manifest(tmp_path / "readiness-evidence")
    state_path.write_text(
        json.dumps(
            AutoresearchState(
                phase=Phase.REVIEW,
                iteration=7,
                mode=ResearchMode.ALPHA_RESEARCH,
                platform_readiness=readiness.identity(),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    readiness_path = tmp_path / "readiness-manifest.json"
    readiness_path.write_text(json.dumps(readiness.to_dict()), encoding="utf-8")
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text("{}", encoding="utf-8")
    return ControlConfig(
        state_path=state_path,
        owner_sessions_path=sessions_path,
        checkpoint_path=tmp_path / "owner-recovery.json",
        wake_lock_path=tmp_path / "control-wake.lock",
        readiness_manifest_path=readiness_path,
        runs_root=tmp_path / "detached-runs",
    ), tmp_path


def test_start_does_not_inspect_or_wake_an_existing_owner_task(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "owned",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
            }
        ],
        events=events,
    )
    service = FakeSupervisorService(events)

    # Act
    AutoresearchControl(config, task_gateway=fake, service_controller=service).start()

    # Assert
    assert fake.rpc_calls == []
    assert events == ["service:start"]


def test_start_enables_the_supervisor_without_mutating_or_waking(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, _ = control_env
    state_before = config.state_path.read_text(encoding="utf-8")
    events: list[str] = []
    fake = FakeOpenClaw(events=events)

    # Act
    AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService(events),
    ).start()

    # Assert
    assert config.state_path.read_text(encoding="utf-8") == state_before
    assert fake.rpc_calls == []
    assert events == ["service:start"]


def test_start_does_not_reset_supervisor_recovery_state(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    checkpoint = SupervisorCheckpoint(
        recovery_records={
            "stale_state:7:review:failed": RecoveryRecord(
                status=RecoveryStatus.EXHAUSTED,
                attempt_count=2,
                alerted=True,
            ),
            "stale_state:7:review:succeeded": RecoveryRecord(
                status=RecoveryStatus.SUCCEEDED,
                attempt_count=1,
            ),
            "stale_state:6:review:failed": RecoveryRecord(
                status=RecoveryStatus.EXHAUSTED,
                attempt_count=2,
                alerted=True,
            ),
        }
    )
    checkpoint.save(config.checkpoint_path)

    # Act
    AutoresearchControl(
        config,
        task_gateway=FakeOpenClaw(),
        service_controller=FakeSupervisorService([]),
    ).start()

    # Assert
    current = SupervisorCheckpoint.load(config.checkpoint_path).recovery_records
    assert "stale_state:7:review:failed" in current
    assert "stale_state:7:review:succeeded" in current
    assert "stale_state:6:review:failed" in current


def test_start_defers_unpinned_state_validation_to_the_supervisor(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    config.state_path.write_text(
        json.dumps(
            AutoresearchState(
                phase=Phase.REVIEW, iteration=7, mode=ResearchMode.ALPHA_RESEARCH
            ).to_dict()
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    events: list[str] = []

    AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService(events),
    ).start()

    assert fake.rpc_calls == []
    assert events == ["service:start"]
    assert fake.rpc_calls == []


def test_start_defers_suspended_state_handling_to_the_supervisor(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    suspended = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=7,
        mode=ResearchMode.DATA_INFRA_G0,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-7",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="Evidence is unavailable.",
            log_summary="Suspended.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Operator must publish evidence.",
        ),
        suspended=True,
        suspension_reason="Operator must publish evidence.",
    )
    config.state_path.write_text(json.dumps(suspended.to_dict()), encoding="utf-8")
    fake = FakeOpenClaw()

    events: list[str] = []

    AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService(events),
    ).start()

    assert fake.rpc_calls == []
    assert events == ["service:start"]


def test_start_does_not_query_for_duplicate_owner_tasks(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "owned",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "requesterSessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
            }
        ],
        events=events,
    )

    AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService(events),
    ).start()

    assert fake.rpc_calls == []
    assert events == ["service:start"]


def test_start_rejects_concurrent_local_control_before_starting_service(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    config.wake_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with config.wake_lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ControlError, match="wake is already in progress"):
            AutoresearchControl(
                config,
                task_gateway=FakeOpenClaw(events=events),
                service_controller=FakeSupervisorService(events),
            ).start()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert events == []


def test_repeated_start_never_creates_an_owner_wake(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, _ = control_env
    events: list[str] = []
    first = FakeOpenClaw()
    second = FakeOpenClaw()

    # Act
    AutoresearchControl(
        config, task_gateway=first, service_controller=FakeSupervisorService(events)
    ).start()
    AutoresearchControl(
        config, task_gateway=second, service_controller=FakeSupervisorService(events)
    ).start()

    # Assert
    assert first.rpc_calls == []
    assert second.rpc_calls == []
    assert events == ["service:start", "service:start"]


def test_start_ignores_an_unavailable_owner_rpc(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, _ = control_env
    failed = FakeOpenClaw(wake_error="ambiguous")

    # Act
    AutoresearchControl(
        config,
        task_gateway=failed,
        service_controller=FakeSupervisorService([]),
    ).start()

    # Assert
    assert failed.rpc_calls == []


def test_control_uses_the_installed_quantipy_supervisor_unit() -> None:
    assert DEFAULT_SUPERVISOR_SERVICE_NAME == "quantipy-autoresearch-supervisor.service"


def test_start_failure_does_not_attempt_owner_run_rollback(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(events=events)
    service = FailingSupervisorService(events, fail_on="start")

    with pytest.raises(ControlError, match="service start failed"):
        AutoresearchControl(config, task_gateway=fake, service_controller=service).start()

    assert events == ["service:start"]


def test_stop_does_not_cancel_or_delete_when_supervisor_stop_fails(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(events=events)
    service = FailingSupervisorService(events, fail_on="stop")

    with pytest.raises(ControlError, match="service stop failed"):
        AutoresearchControl(config, task_gateway=fake, service_controller=service).stop()

    assert events == ["service:stop"]


def test_start_failure_does_not_delete_the_owner_session(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    fake = FakeOpenClaw(
        events=events,
        abort_response={"ok": True, "abortedRunId": "wrong", "status": "aborted"},
    )
    service = FailingSupervisorService(events, fail_on="start")

    with pytest.raises(ControlError, match="service start failed"):
        AutoresearchControl(config, task_gateway=fake, service_controller=service).start()

    assert events == ["service:start"]


def test_stop_rejects_concurrent_local_wake(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    events: list[str] = []
    config.wake_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with config.wake_lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ControlError, match="wake is already in progress"):
            AutoresearchControl(
                config,
                task_gateway=FakeOpenClaw(events=events),
                service_controller=FakeSupervisorService(events),
            ).stop()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert events == []


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

    result = AutoresearchControl(config, task_gateway=fake, service_controller=service).stop()

    cancel_calls = [params for method, params in fake.rpc_calls if method == "tasks.cancel"]
    delete_calls = [params for method, params in fake.rpc_calls if method == "sessions.delete"]
    assert result.cancelled_task_ids == ("owned",)
    assert result.deleted_session is False
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["taskId"] == "owned"
    assert delete_calls[0] == {
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "deleteTranscript": False,
        "key": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    assert events == [
        "service:stop",
        "rpc:list",
        "rpc:cancel",
        "rpc:delete",
    ]


def test_stop_stops_only_the_current_state_bound_detached_unit_before_session_delete(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, tmp_path = control_env
    state = AutoresearchState.from_dict(json.loads(config.state_path.read_text(encoding="utf-8")))
    runs_root = tmp_path / "detached-runs"
    run_dir = runs_root / "owned"
    command = ("uv", "run", "pytest")
    manifest_path = tmp_path / "owned-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": state.iteration,
                "phase": state.phase.value,
                "attempt": 1,
                "task_label": "autoresearch-i7-review-r1-a1",
                "state_reference_sha256": build_authoritative_state_reference(
                    state, state_path=config.state_path
                ).sha256(),
                "instruction_manifest_sha256": "a" * 64,
                "run_directory": str(run_dir),
                "working_directory": str(tmp_path),
                "command_sha256": autoresearch_runs.command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    autoresearch_runs.prepare_run(
        manifest_path=manifest_path, run_dir=run_dir, runs_root=runs_root, command=command
    )
    autoresearch_runs.start_run(
        run_dir=run_dir,
        runs_root=runs_root,
        pid=999_999,
        systemd_unit="openclaw-long-task-1-1.service",
    )
    detached = FakeDetachedRunController(runs_root=runs_root)
    fake = FakeOpenClaw()

    # Act
    result = AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService([]),
        detached_run_controller=detached,
        runs_root=runs_root,
    ).stop()

    # Assert
    assert detached.stopped_units == ["openclaw-long-task-1-1.service"]
    assert result.stopped_detached_run_directories == (str(run_dir),)
    assert any(method == "sessions.delete" for method, _ in fake.rpc_calls)


def test_stop_waits_for_a_state_bound_manifest_to_publish_its_running_status(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, tmp_path = control_env
    config = replace(
        config,
        detached_stop_timeout_seconds=1.0,
        detached_stop_poll_seconds=0.001,
        detached_stop_quiescence_seconds=0.001,
    )
    waiting_run_dir = _prepare_state_bound_detached_run(
        config,
        tmp_path,
        name="waiting-launch",
        unit="openclaw-long-task-2-2.service",
        start=False,
    )
    _prepare_state_bound_detached_run(
        config,
        tmp_path,
        name="seed-launch",
        unit="openclaw-long-task-1-1.service",
        start=True,
    )

    def publish_waiting_status() -> None:
        autoresearch_runs.start_run(
            run_dir=waiting_run_dir,
            runs_root=config.runs_root,
            pid=999_998,
            systemd_unit="openclaw-long-task-2-2.service",
        )

    detached = FakeDetachedRunController(
        runs_root=config.runs_root,
        on_unit_check=publish_waiting_status,
    )

    # Act
    result = AutoresearchControl(
        config,
        task_gateway=FakeOpenClaw(),
        service_controller=FakeSupervisorService([]),
        detached_run_controller=detached,
    ).stop()

    # Assert
    assert detached.stopped_units == [
        "openclaw-long-task-1-1.service",
        "openclaw-long-task-2-2.service",
    ]
    assert result.stopped_detached_run_directories == (
        str(config.runs_root / "seed-launch"),
        str(waiting_run_dir),
    )


def test_stop_rescans_for_a_launcher_that_appears_after_an_empty_scan(
    control_env: tuple[ControlConfig, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    config, tmp_path = control_env
    config = replace(
        config,
        detached_stop_timeout_seconds=1.0,
        detached_stop_poll_seconds=0.001,
        detached_stop_quiescence_seconds=0.002,
    )
    launched = False

    def publish_after_first_scan(_seconds: float) -> None:
        nonlocal launched
        if launched:
            return
        launched = True
        _prepare_state_bound_detached_run(
            config,
            tmp_path,
            name="late-launch",
            unit="openclaw-long-task-3-3.service",
            start=True,
        )

    monkeypatch.setattr("gateway.autoresearch_control.time.sleep", publish_after_first_scan)
    detached = FakeDetachedRunController(runs_root=config.runs_root)

    # Act
    result = AutoresearchControl(
        config,
        task_gateway=FakeOpenClaw(),
        service_controller=FakeSupervisorService([]),
        detached_run_controller=detached,
    ).stop()

    # Assert
    assert detached.stopped_units == ["openclaw-long-task-3-3.service"]
    assert result.stopped_detached_run_directories == (str(config.runs_root / "late-launch"),)


def test_stop_disables_and_cancels_before_failing_closed_on_a_malformed_detached_record(
    control_env: tuple[ControlConfig, Path],
) -> None:
    # Arrange
    config, _ = control_env
    (config.runs_root / "malformed").mkdir(parents=True)
    (config.runs_root / "malformed" / "manifest.json").write_text("{}", encoding="utf-8")
    events: list[str] = []
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
        events=events,
    )

    # Act / Assert
    with pytest.raises(ControlError, match="malformed detached run record"):
        AutoresearchControl(
            config,
            task_gateway=fake,
            service_controller=FakeSupervisorService(events),
        ).stop()

    assert events == ["service:stop", "rpc:list", "rpc:cancel"]


def test_stop_reads_and_cancels_tasks_after_stopping_the_supervisor(
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
        config, task_gateway=fake, service_controller=FakeSupervisorService(events)
    ).stop()

    assert events[:4] == ["service:stop", "rpc:list", "rpc:cancel", "rpc:delete"]


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
            config, task_gateway=fake, service_controller=FakeSupervisorService([])
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
        task_gateway=fake,
        service_controller=FakeSupervisorService([], active=True),
    ).status()

    assert status.tasks[0].task_id == "owned"
    assert status.supervisor_active is True
    assert all(method != "tasks.cancel" for method, _ in fake.rpc_calls)
    assert all(method != "sessions.delete" for method, _ in fake.rpc_calls)


def test_status_reports_codex_native_subagent_tasks_under_pm_owner(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "native-review",
                "id": "native-review",
                "status": "running",
                "runtime": "subagent",
                "taskKind": "codex-native",
                "runId": "codex-thread:review-1",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
            },
            {
                "taskId": "other-native",
                "status": "running",
                "runtime": "subagent",
                "taskKind": "codex-native",
                "runId": "codex-thread:other",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": "agent:other:session",
                "ownerKey": "agent:other:session",
            },
        ]
    )

    status = AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService([], active=True),
    ).status()

    assert [task.task_id for task in status.tasks] == ["native-review"]


def test_stop_cancels_codex_native_subagent_tasks_under_pm_owner(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "native-review",
                "id": "native-review",
                "status": "running",
                "runtime": "subagent",
                "taskKind": "codex-native",
                "runId": "codex-thread:review-1",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
            }
        ]
    )

    result = AutoresearchControl(
        config,
        task_gateway=fake,
        service_controller=FakeSupervisorService([]),
    ).stop()

    assert result.cancelled_task_ids == ("native-review",)


def test_status_tolerates_statusless_stale_owner_session(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    config.owner_sessions_path.write_text(
        json.dumps({AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-session"}}),
        encoding="utf-8",
    )

    status = AutoresearchControl(
        config,
        task_gateway=FakeOpenClaw(),
        service_controller=FakeSupervisorService([], active=False),
    ).status()

    assert status.owner_lifecycle_status is None
    assert status.tasks == ()


def test_status_preserves_operator_precondition_implementation_state(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    config.state_path.write_text(_operator_precondition_state_json(), encoding="utf-8")

    status = AutoresearchControl(
        config,
        task_gateway=FakeOpenClaw(),
        service_controller=FakeSupervisorService([], active=False),
    ).status()

    assert status.phase == "implementation"


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
        task_gateway=fake,
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
            config, task_gateway=fake, service_controller=FakeSupervisorService([])
        ).stop()


def test_stop_fails_closed_when_the_owner_agent_task_has_no_session_provenance(
    control_env: tuple[ControlConfig, Path],
) -> None:
    config, _ = control_env
    fake = FakeOpenClaw(tasks=[{"taskId": "ambiguous", "agentId": AUTORESEARCH_OWNER_AGENT_ID}])

    with pytest.raises(ControlError, match="ambiguous"):
        AutoresearchControl(
            config, task_gateway=fake, service_controller=FakeSupervisorService([])
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
        config, task_gateway=fake, service_controller=FakeSupervisorService([])
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
            config, task_gateway=fake, service_controller=FakeSupervisorService([])
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
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owned": {**task, "status": "failed"}})

    status = AutoresearchControl(
        config,
        task_gateway=fake,
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
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owned": {**task, "status": "failed"}})

    result = AutoresearchControl(
        config, task_gateway=fake, service_controller=FakeSupervisorService([])
    ).stop()

    assert result.cancelled_task_ids == ()
    assert not any(method == "tasks.cancel" for method, _ in fake.rpc_calls)


def test_control_source_contains_no_g2_dev_surface() -> None:
    source = Path("gateway/autoresearch_control.py").read_text(encoding="utf-8").lower()

    assert "/_dev" not in source
    assert "localhost:5173" not in source
    assert "agent:main:g2" not in source
    assert "g2" not in source
