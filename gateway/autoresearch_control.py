"""Human-facing owner-session control for Quantipy autoresearch."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import gateway.autoresearch_runs as detached_runs
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.state import (
    AutoresearchState,
)
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference,
)
from gateway.autoresearch_readiness import (
    DEFAULT_PLATFORM_READINESS_PATH,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
    DEFAULT_CHECKPOINT_PATH,
    DEFAULT_OWNER_SESSIONS_PATH,
    DEFAULT_STATE_PATH,
    OpenClawRPC,
    SupervisorError,
    TaskGateway,
    TaskProvenance,
    classify_autoresearch_task,
    reconcile_relevant_running_tasks,
)
from gateway.autoresearch_systemd import SystemdUnitStateError, systemd_unit_is_active


class ServiceCommandRunner(Protocol):
    def __call__(
        self, command: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]: ...


class ControlError(SupervisorError):
    """Raised when an owner-session control operation cannot be proven safe."""


DEFAULT_SUPERVISOR_SERVICE_NAME = "quantipy-autoresearch-supervisor.service"
DEFAULT_SERVICE_CONTROL_COMMAND = ("systemctl", "--user")
DEFAULT_WAKE_LOCK_PATH = Path.home() / ".openclaw" / "autoresearch" / "control-wake.lock"
DEFAULT_DETACHED_STOP_TIMEOUT_SECONDS = 20.0
DEFAULT_DETACHED_STOP_POLL_SECONDS = 0.1
DEFAULT_DETACHED_STOP_QUIESCENCE_SECONDS = 0.25


class SupervisorServiceController(Protocol):
    """Strict lifecycle control for the autonomous supervisor service."""

    def ensure_started(self) -> None: ...

    def stop(self) -> None: ...

    def is_active(self) -> bool: ...


class DetachedRunController(Protocol):
    """Stops and verifies only a manifest-bound transient user unit."""

    def stop_unit(self, unit: str) -> None: ...

    def is_active(self, unit: str) -> bool: ...

    def is_pid_alive(self, pid: int | None) -> bool: ...


def _run_default_command(
    command: list[str], *, check: bool, capture_output: bool, text: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=capture_output, text=text)


class SystemdSupervisorServiceController:
    """Runs only explicit systemd user-service lifecycle commands."""

    def __init__(
        self,
        *,
        command_prefix: tuple[str, ...],
        service_name: str,
        run_command: ServiceCommandRunner,
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


class SystemdDetachedRunController:
    """Uses exact transient-unit names from sealed detached records only."""

    def __init__(self, run_command: ServiceCommandRunner) -> None:
        self._run_command = run_command

    def stop_unit(self, unit: str) -> None:
        result = self._run(("systemctl", "--user", "stop", unit))
        if result.returncode != 0:
            raise ControlError(f"detached unit stop failed for {unit}: {self._output(result)}")

    def is_active(self, unit: str) -> bool:
        try:
            return systemd_unit_is_active(unit, run_command=self._run)
        except SystemdUnitStateError as exc:
            raise ControlError(f"detached unit status failed for {unit}: {exc}") from exc

    def is_pid_alive(self, pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ControlError(f"failed to inspect detached pid {pid}: {exc}") from exc
        closing = raw.rfind(")")
        fields = raw[closing + 1 :].split() if closing >= 0 else []
        if not fields:
            raise ControlError(f"malformed process stat for detached pid {pid}")
        return fields[0] != "Z"

    def _run(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        try:
            return self._run_command(list(command), check=False, capture_output=True, text=True)
        except OSError as exc:
            raise ControlError(f"failed to execute detached unit command: {exc}") from exc

    @staticmethod
    def _output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout.strip() or result.stderr.strip() or "no output"


@dataclass(frozen=True, slots=True)
class ControlConfig:
    state_path: Path = DEFAULT_STATE_PATH
    owner_sessions_path: Path = DEFAULT_OWNER_SESSIONS_PATH
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH
    supervisor_service_name: str = DEFAULT_SUPERVISOR_SERVICE_NAME
    service_control_command: tuple[str, ...] = DEFAULT_SERVICE_CONTROL_COMMAND
    wake_lock_path: Path = DEFAULT_WAKE_LOCK_PATH
    readiness_manifest_path: Path = DEFAULT_PLATFORM_READINESS_PATH
    runs_root: Path = detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
    detached_stop_timeout_seconds: float = DEFAULT_DETACHED_STOP_TIMEOUT_SECONDS
    detached_stop_poll_seconds: float = DEFAULT_DETACHED_STOP_POLL_SECONDS
    detached_stop_quiescence_seconds: float = DEFAULT_DETACHED_STOP_QUIESCENCE_SECONDS


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
    stopped_detached_run_directories: tuple[str, ...] = ()


class AutoresearchControl:
    """Dispatches and stops work only for the dedicated autoresearch owner."""

    def __init__(
        self,
        config: ControlConfig | None = None,
        *,
        service_command_runner: ServiceCommandRunner | None = None,
        task_gateway: TaskGateway | None = None,
        service_controller: SupervisorServiceController | None = None,
        detached_run_controller: DetachedRunController | None = None,
        runs_root: Path | None = None,
    ) -> None:
        self.config = config or ControlConfig()
        self._rpc = OpenClawRPC(task_gateway)
        service_run_command: ServiceCommandRunner = (
            _run_default_command if service_command_runner is None else service_command_runner
        )
        self._service_controller = service_controller or SystemdSupervisorServiceController(
            command_prefix=self.config.service_control_command,
            service_name=self.config.supervisor_service_name,
            run_command=service_run_command,
        )
        self._detached_run_controller = detached_run_controller or SystemdDetachedRunController(
            service_run_command
        )
        self._runs_root = self.config.runs_root if runs_root is None else runs_root

    def start(self) -> None:
        """Enable the supervisor; its first poll owns all state transitions and wakes."""
        with self._wake_lock():
            self._service_controller.ensure_started()

    @contextmanager
    def _wake_lock(self) -> Iterator[None]:
        lock_path = self.config.wake_lock_path.expanduser()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ControlError(
                        f"autoresearch wake is already in progress: {lock_path}"
                    ) from exc
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ControlError(f"failed to acquire autoresearch wake lock: {exc}") from exc

    def status(self) -> ControlStatus:
        state = self._load_state()
        tasks = tuple(self._task_status(task) for task in self._owned_running_tasks())
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
        with self._wake_lock():
            # Freeze new supervisor work before inspecting records that may be mid-launch.
            self._service_controller.stop()
            owned_tasks = self._owned_running_tasks()
            task_ids = tuple(self._task_id(task) for task in owned_tasks)
            for task_id in task_ids:
                try:
                    self._rpc.cancel_task(task_id=task_id)
                except SupervisorError as exc:
                    raise ControlError(f"failed to cancel owned task {task_id}: {exc}") from exc
            state = self._load_state()
            stopped_directories = self._stop_detached_runs(state)
            try:
                deleted_session = self._rpc.delete_owner_session()
            except SupervisorError as exc:
                raise ControlError(f"failed to delete autoresearch owner session: {exc}") from exc
            return StopResult(
                cancelled_task_ids=task_ids,
                deleted_session=deleted_session,
                stopped_detached_run_directories=stopped_directories,
            )

    def _owned_detached_launches(
        self, state: AutoresearchState
    ) -> tuple[tuple[detached_runs.RunRecord, ...], tuple[Path, ...]]:
        """Return state-bound running records and manifest-only launch windows."""
        state_reference = build_authoritative_state_reference(
            state, state_path=self.config.state_path
        ).sha256()
        try:
            root_metadata = self._runs_root.lstat()
        except FileNotFoundError:
            return (), ()
        except OSError as exc:
            raise ControlError(f"cannot inspect detached runs root: {exc}") from exc
        if not root_metadata or self._runs_root.is_symlink() or not self._runs_root.is_dir():
            raise ControlError("detached runs root must be a non-symlink directory")
        records: list[detached_runs.RunRecord] = []
        pending_manifest_directories: list[Path] = []
        units: set[str] = set()
        for directory, child_directories, files in os.walk(self._runs_root, followlinks=False):
            child_directories.sort()
            files.sort()
            parent = Path(directory)
            if any((parent / child).is_symlink() for child in child_directories):
                raise ControlError("symlinked detached run directory prevents safe stop")
            if "manifest.json" not in files:
                continue
            try:
                canonical_directory, manifest, _digest = detached_runs._load_manifest(
                    parent, self._runs_root
                )
            except detached_runs.AutoresearchRunRecordError as exc:
                raise ControlError(
                    f"malformed detached run record prevents safe stop: {parent}: {exc}"
                ) from exc
            if (
                manifest.iteration != state.iteration
                or manifest.phase is not state.phase
                or manifest.state_reference_sha256 != state_reference
            ):
                continue
            status_path = canonical_directory / "status.json"
            if not status_path.exists():
                if status_path.is_symlink():
                    raise ControlError("symlinked detached status prevents safe stop")
                pending_manifest_directories.append(canonical_directory)
                continue
            try:
                record = detached_runs.read_run_record(
                    run_dir=canonical_directory, runs_root=self._runs_root
                )
            except detached_runs.AutoresearchRunRecordError as exc:
                raise ControlError(
                    f"malformed detached run record prevents safe stop: {parent}: {exc}"
                ) from exc
            if record.status.state is not detached_runs.RunState.RUNNING:
                continue
            unit = record.status.systemd_unit
            if (
                unit is None
                or re.fullmatch(r"openclaw-long-task-[0-9]+-[0-9]+\.service", unit) is None
            ):
                raise ControlError("owned detached run has an unsafe systemd unit identity")
            if unit in units:
                raise ControlError("multiple owned detached runs claim the same systemd unit")
            units.add(unit)
            records.append(record)
        return tuple(records), tuple(pending_manifest_directories)

    def _stop_detached_runs(self, state: AutoresearchState) -> tuple[str, ...]:
        if (
            self.config.detached_stop_timeout_seconds <= 0
            or self.config.detached_stop_poll_seconds <= 0
            or self.config.detached_stop_quiescence_seconds <= 0
        ):
            raise ControlError(
                "detached stop timeout, poll, and quiescence intervals must be positive"
            )
        deadline = time.monotonic() + self.config.detached_stop_timeout_seconds
        stopped_units: dict[Path, str] = {}
        quiescent_since: float | None = None
        while True:
            running, pending = self._owned_detached_launches(state)
            for record in running:
                unit = record.status.systemd_unit
                assert unit is not None
                existing_unit = stopped_units.get(record.run_directory)
                if existing_unit is not None and existing_unit != unit:
                    raise ControlError("owned detached run changed its systemd unit identity")
                if existing_unit is None:
                    self._detached_run_controller.stop_unit(unit)
                    stopped_units[record.run_directory] = unit

            terminal_pending = False
            for run_directory, unit in stopped_units.items():
                try:
                    record = detached_runs.read_run_record(
                        run_dir=run_directory, runs_root=self._runs_root
                    )
                except detached_runs.AutoresearchRunRecordError as exc:
                    raise ControlError(
                        f"owned detached run became unreadable during stop: {run_directory}: {exc}"
                    ) from exc
                status = record.status
                if status.systemd_unit != unit:
                    raise ControlError("owned detached run changed its systemd unit identity")
                if status.state is detached_runs.RunState.RUNNING:
                    terminal_pending = True
                    continue
                if (
                    status.state is not detached_runs.RunState.FAILED
                    or status.exit_code != 143
                    or status.signal_number is not None
                    or status.failure_classification
                    is not detached_runs.RunFailureClassification.OPERATOR_STOPPED
                ):
                    raise ControlError(
                        "owned detached run did not seal the required operator_stopped "
                        "terminal status"
                    )
                if self._detached_run_controller.is_active(
                    unit
                ) or self._detached_run_controller.is_pid_alive(status.pid):
                    terminal_pending = True

            now = time.monotonic()
            if not running and not pending and not terminal_pending:
                if quiescent_since is None:
                    quiescent_since = now
                elif now - quiescent_since >= self.config.detached_stop_quiescence_seconds:
                    return tuple(str(directory) for directory in stopped_units)
            else:
                quiescent_since = None
            if now >= deadline:
                raise ControlError("timed out waiting for owned detached runs to quiesce")
            time.sleep(self.config.detached_stop_poll_seconds)

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

    def _parse_state_material(self, state_material: str) -> AutoresearchState:
        try:
            return AutoresearchState.from_dict(json.loads(state_material))
        except json.JSONDecodeError as exc:
            raise ControlError(
                f"invalid autoresearch state JSON: {self.config.state_path}"
            ) from exc
        except AutoresearchValidationError as exc:
            raise ControlError(f"invalid autoresearch state: {exc}") from exc

    def _load_state(self) -> AutoresearchState:
        return self._parse_state_material(self._state_material())

    def _running_tasks(self) -> tuple[Mapping[str, object], ...]:
        payload = self._rpc.list_running_tasks()
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

    def _owned_running_tasks(self) -> tuple[Mapping[str, object], ...]:
        owned_tasks = self._owned_tasks(self._running_tasks())
        try:
            reconciled = reconcile_relevant_running_tasks(self._rpc, owned_tasks)
        except SupervisorError as exc:
            raise ControlError(f"task reconciliation failed: {exc}") from exc
        return reconciled.running_tasks

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
        if status is None:
            return None
        if not isinstance(status, str) or not status:
            raise ControlError("owner session lifecycle status is malformed")
        return status


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("wake", "status", "stop"))
    parser.add_argument(
        "--readiness-manifest",
        type=Path,
        default=DEFAULT_PLATFORM_READINESS_PATH,
        help="Operator-owned platform readiness manifest used before wake.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    control = AutoresearchControl(ControlConfig(readiness_manifest_path=args.readiness_manifest))
    try:
        if args.command == "wake":
            control.start()
            print(json.dumps({"started": True}, sort_keys=True))
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
