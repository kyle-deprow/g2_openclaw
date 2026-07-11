"""Human-facing owner-session control for Quantipy autoresearch."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from gateway.autoresearch_runner import AutoresearchState, AutoresearchValidationError
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
    DEFAULT_OPENCLAW_BIN,
    DEFAULT_OWNER_SESSIONS_PATH,
    DEFAULT_STATE_PATH,
    WAKE_MESSAGE,
    OpenClawRPC,
    SupervisorError,
    TaskProvenance,
    classify_autoresearch_task,
    make_idempotency_key,
)


class ControlError(SupervisorError):
    """Raised when an owner-session control operation cannot be proven safe."""


DEFAULT_SUPERVISOR_SERVICE_NAME = "quantipy-autoresearch-supervisor.service"
DEFAULT_SERVICE_CONTROL_COMMAND = ("systemctl", "--user")


class SupervisorServiceController(Protocol):
    """Strict lifecycle control for the autonomous supervisor service."""

    def ensure_started(self) -> None: ...

    def stop(self) -> None: ...

    def is_active(self) -> bool: ...


class SystemdSupervisorServiceController:
    """Runs only explicit systemd user-service lifecycle commands."""

    def __init__(
        self,
        *,
        command_prefix: tuple[str, ...],
        service_name: str,
        run_command: Callable[..., subprocess.CompletedProcess[str]],
    ) -> None:
        if not command_prefix:
            raise ControlError("supervisor service command prefix must not be empty")
        if not service_name.strip():
            raise ControlError("supervisor service name must not be empty")
        self._command_prefix = command_prefix
        self._service_name = service_name
        self._run_command = run_command

    def ensure_started(self) -> None:
        self._run_required("enable", "--now", self._service_name)

    def stop(self) -> None:
        self._run_required("disable", "--now", self._service_name)

    def is_active(self) -> bool:
        command = [*self._command_prefix, "is-active", "--quiet", self._service_name]
        result = self._run(command)
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        raise ControlError(
            f"supervisor service status command failed: {self._command_output(result)}"
        )

    def _run_required(self, *args: str) -> None:
        result = self._run([*self._command_prefix, *args])
        if result.returncode != 0:
            raise ControlError(f"supervisor service command failed: {self._command_output(result)}")

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._run_command(command, check=False, capture_output=True, text=True)
        except OSError as exc:
            raise ControlError(f"failed to execute supervisor service command: {exc}") from exc

    def _command_output(self, result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout.strip() or result.stderr.strip() or "no output"


@dataclass(frozen=True, slots=True)
class ControlConfig:
    state_path: Path = DEFAULT_STATE_PATH
    owner_sessions_path: Path = DEFAULT_OWNER_SESSIONS_PATH
    default_openclaw_bin: Path = DEFAULT_OPENCLAW_BIN
    supervisor_service_name: str = DEFAULT_SUPERVISOR_SERVICE_NAME
    service_control_command: tuple[str, ...] = DEFAULT_SERVICE_CONTROL_COMMAND


@dataclass(frozen=True, slots=True)
class TaskStatus:
    task_id: str
    agent_id: str
    owner_session_key: str


@dataclass(frozen=True, slots=True)
class ControlStatus:
    owner_agent_id: str
    owner_session_key: str
    phase: str
    iteration: int
    owner_lifecycle_status: str | None
    supervisor_active: bool
    tasks: tuple[TaskStatus, ...]


@dataclass(frozen=True, slots=True)
class StopResult:
    cancelled_task_ids: tuple[str, ...]
    deleted_session: bool


class AutoresearchControl:
    """Dispatches and stops work only for the dedicated autoresearch owner."""

    def __init__(
        self,
        config: ControlConfig | None = None,
        *,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        service_controller: SupervisorServiceController | None = None,
    ) -> None:
        self.config = config or ControlConfig()
        self._rpc = OpenClawRPC(self.config.default_openclaw_bin, run_command=run_command)
        self._service_controller = service_controller or SystemdSupervisorServiceController(
            command_prefix=self.config.service_control_command,
            service_name=self.config.supervisor_service_name,
            run_command=run_command,
        )

    def wake(self) -> str:
        executable = self._rpc.require_binary()
        state_material = self._state_material()
        run_id = self._rpc.wake(
            executable,
            message=WAKE_MESSAGE,
            idempotency_key=make_idempotency_key(purpose="manual-wake", material=state_material),
        )
        try:
            self._service_controller.ensure_started()
        except SupervisorError as exc:
            rollback_errors: list[str] = []
            try:
                self._service_controller.stop()
            except SupervisorError as rollback_error:
                rollback_errors.append(f"supervisor stop: {rollback_error}")
            try:
                self._rpc.abort_owner_run(executable, run_id=run_id)
            except SupervisorError as rollback_error:
                rollback_errors.append(f"owner run abort: {rollback_error}")
            try:
                self._rpc.delete_owner_session(executable)
            except SupervisorError as rollback_error:
                rollback_errors.append(f"owner session delete: {rollback_error}")
            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise ControlError(f"supervisor start failed; rollback failed: {details}") from exc
            raise ControlError(
                "supervisor start failed; accepted owner wake was rolled back"
            ) from exc
        return run_id

    def status(self) -> ControlStatus:
        executable = self._rpc.require_binary()
        state = self._load_state()
        tasks = tuple(
            self._task_status(task) for task in self._owned_tasks(self._running_tasks(executable))
        )
        lifecycle_status = self._owner_lifecycle_status()
        return ControlStatus(
            owner_agent_id=AUTORESEARCH_OWNER_AGENT_ID,
            owner_session_key=AUTORESEARCH_OWNER_SESSION_KEY,
            phase=state.phase.value,
            iteration=state.iteration,
            owner_lifecycle_status=lifecycle_status,
            supervisor_active=self._service_controller.is_active(),
            tasks=tasks,
        )

    def stop(self) -> StopResult:
        executable = self._rpc.require_binary()
        # Validate the live task schema and ownership before disabling recovery.
        self._owned_tasks(self._running_tasks(executable))
        self._service_controller.stop()
        # Re-read after the supervisor is stopped so a task launched during the
        # preflight window is included in cancellation.
        owned_tasks = self._owned_tasks(self._running_tasks(executable))
        task_ids = tuple(self._task_id(task) for task in owned_tasks)
        for task_id in task_ids:
            try:
                self._rpc.cancel_task(executable, task_id=task_id)
            except SupervisorError as exc:
                raise ControlError(f"failed to cancel owned task {task_id}: {exc}") from exc
        try:
            deleted_session = self._rpc.delete_owner_session(executable)
        except SupervisorError as exc:
            raise ControlError(f"failed to delete autoresearch owner session: {exc}") from exc
        return StopResult(cancelled_task_ids=task_ids, deleted_session=deleted_session)

    def _state_material(self) -> str:
        try:
            return self.config.state_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ControlError(
                f"missing autoresearch state file: {self.config.state_path}"
            ) from exc
        except OSError as exc:
            raise ControlError(
                f"failed to read autoresearch state file: {self.config.state_path}"
            ) from exc

    def _load_state(self) -> AutoresearchState:
        try:
            return AutoresearchState.from_dict(json.loads(self._state_material()))
        except json.JSONDecodeError as exc:
            raise ControlError(
                f"invalid autoresearch state JSON: {self.config.state_path}"
            ) from exc
        except AutoresearchValidationError as exc:
            raise ControlError(f"invalid autoresearch state: {exc}") from exc

    def _running_tasks(self, executable: Path) -> tuple[Mapping[str, object], ...]:
        payload = self._rpc.run_json(executable, ["tasks", "list", "--status", "running", "--json"])
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, str | bytes):
            raise ControlError("OpenClaw tasks JSON missing tasks array")
        if not all(isinstance(task, Mapping) for task in raw_tasks):
            raise ControlError("OpenClaw tasks JSON contains a non-object task")
        return tuple(task for task in raw_tasks if isinstance(task, Mapping))

    def _owned_tasks(
        self, tasks: Sequence[Mapping[str, object]]
    ) -> tuple[Mapping[str, object], ...]:
        owned: list[Mapping[str, object]] = []
        for task in tasks:
            provenance = classify_autoresearch_task(task)
            if provenance is TaskProvenance.UNRELATED:
                continue
            if provenance is TaskProvenance.AMBIGUOUS:
                raise ControlError("ambiguous task session provenance prevents safe cancellation")
            self._task_id(task)
            owned.append(task)
        return tuple(owned)

    def _task_id(self, task: Mapping[str, object]) -> str:
        task_id = task.get("taskId")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ControlError("owned task is missing a non-empty taskId")
        legacy_id = task.get("id")
        if legacy_id is not None and (
            not isinstance(legacy_id, str) or not legacy_id.strip() or legacy_id != task_id
        ):
            raise ControlError("owned task id must agree with canonical taskId")
        return task_id

    def _task_status(self, task: Mapping[str, object]) -> TaskStatus:
        agent_id = task.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            raise ControlError("owned task is missing agentId")
        return TaskStatus(
            task_id=self._task_id(task),
            agent_id=agent_id,
            owner_session_key=AUTORESEARCH_OWNER_SESSION_KEY,
        )

    def _owner_lifecycle_status(self) -> str | None:
        try:
            raw = json.loads(self.config.owner_sessions_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ControlError(f"failed to read owner session store: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ControlError("invalid owner session store JSON") from exc
        if not isinstance(raw, Mapping):
            raise ControlError("invalid owner session store payload")
        lifecycle = raw.get(AUTORESEARCH_OWNER_SESSION_KEY)
        if lifecycle is None:
            return None
        if not isinstance(lifecycle, Mapping):
            raise ControlError("owner session lifecycle is malformed")
        status = lifecycle.get("status")
        if not isinstance(status, str) or not status:
            raise ControlError("owner session lifecycle status is malformed")
        return status


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("wake", "status", "stop"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    control = AutoresearchControl()
    try:
        if args.command == "wake":
            print(json.dumps({"runId": control.wake()}, sort_keys=True))
        elif args.command == "status":
            print(json.dumps(asdict(control.status()), sort_keys=True))
        else:
            print(json.dumps(asdict(control.stop()), sort_keys=True))
    except SupervisorError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
