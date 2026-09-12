"""Detached worker launcher and conservative process identity handling."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .containment import (
    ContainmentError,
    RuntimePins,
    stop_scope,
    verify_configured_runtime_pins,
)
from .contracts import RunPlan


class JobError(RuntimeError):
    """A job cannot be launched or safely controlled."""


_DOTTED_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SECRET_HINT = re.compile(r"(?i)(?:api[_-]?key|password|secret|token)")


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    attempt_id: str
    worker_pid: int
    worker_starttime: int
    run_dir: str
    state: str = "LAUNCHED"

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "attempt_id": self.attempt_id,
                "worker_pid": self.worker_pid,
                "worker_starttime": self.worker_starttime,
                "run_dir": self.run_dir,
                "state": self.state,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _starttime(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if fields[2] == "Z":
            return None
        return int(fields[21])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def _run(argv: Sequence[str], cwd: Path) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise JobError(f"git pre-launch check failed: {exc}") from exc
    return result.stdout.strip()


def _clean(worktree: Path) -> bool:
    return _run(("git", "status", "--porcelain", "--untracked-files=all"), worktree) == ""


def preflight(worktree: Path, expected_commit: str | None = None) -> None:
    """Run the non-mutating launch checks before a state is queued."""
    worktree = worktree.resolve()
    if (
        not worktree.is_dir()
        or _run(("git", "rev-parse", "--is-inside-work-tree"), worktree) != "true"
    ):
        raise JobError("worktree is not a git worktree")
    if (
        expected_commit is not None
        and _run(("git", "rev-parse", "HEAD"), worktree) != expected_commit
    ):
        raise JobError("HEAD does not match the implementation commit")
    if not _clean(worktree):
        raise JobError("worktree is dirty")


def _validate_token(token: str) -> None:
    if _SECRET_HINT.search(token):
        raise JobError("target arguments may not contain secret values or names")


def _validate_targets(
    argv: Sequence[str], shared_python: Path, worktree: Path, run_dir: Path
) -> None:
    if not argv or Path(argv[0]).resolve() != shared_python.resolve():
        raise JobError("targets argv[0] must equal the trusted shared interpreter")
    if (
        len(argv) < 2
        or (argv[1] == "-m" and len(argv) < 3)
        or (argv[1] != "-m" and not Path(argv[1]).is_absolute())
    ):
        raise JobError("targets must use -m module or an absolute in-worktree program")
    if argv[1] == "-m" and _DOTTED_MODULE.fullmatch(argv[2]) is None:
        raise JobError("targets -m value must be a dotted module name")
    if argv[1] != "-m" and not Path(argv[1]).is_file():
        raise JobError("target program file is missing")
    for token in argv:
        _validate_token(token)
    for token in argv[2:] if len(argv) >= 2 and argv[1] == "-m" else argv[1:]:
        path = Path(token)
        if path.is_absolute() and not (
            path.resolve() == run_dir.resolve()
            or run_dir.resolve() in path.resolve().parents
            or worktree.resolve() in path.resolve().parents
        ):
            raise JobError(f"target path is outside the worktree/run directory: {token}")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def lifecycle_lock(run_dir: Path) -> Iterator[None]:
    """Serialize worker terminalization and cancellation for one run.

    The durable SQLite run lock cannot be used by the detached worker, while a
    process-local lock would not protect a CLI cancellation in another process.
    This small per-run flock is the shared authority for the two filesystem
    evidence writers.  Cancellation deliberately releases it before signalling
    the worker so the worker's signal handler cannot deadlock waiting for the
    same lock while the canceller waits for that worker to exit.
    """
    if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
        raise JobError(f"run directory is not a regular directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "lifecycle.lock"
    lock = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _path_present(path: Path) -> bool:
    """Return true for regular paths and dangling symlinks alike."""
    return path.exists() or path.is_symlink()


def _terminal_status(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str):
            return status
    return None


def _wait_identity_gone(pid: int, starttime: int, deadline: float) -> bool:
    """Wait until this exact process identity is absent, not merely this PID."""
    while time.monotonic() < deadline:
        if _starttime(pid) != starttime:
            return True
        time.sleep(0.05)
    return _starttime(pid) != starttime


def _active_stage_evidence(run_dir: Path) -> tuple[int, int, int, str] | None:
    """Read durable stage identity, including a leader that already exited."""
    path = run_dir / "active-stage.json"
    if not _path_present(path):
        return None
    if path.is_symlink():
        raise JobError("active stage evidence is a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise JobError("active stage evidence is not an object")
        pid = int(payload["pid"])
        starttime = int(payload["starttime"])
        pgid = int(payload["pgid"])
        unit = payload["scope_unit"]
    except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise JobError("active stage evidence is invalid") from exc
    if pid <= 1 or starttime <= 0 or pgid <= 1 or not isinstance(unit, str) or not unit:
        raise JobError("active stage evidence is invalid")
    return pid, starttime, pgid, unit


def _active_stage(run_dir: Path) -> tuple[int, int] | None:
    """Return a currently matching stage leader for lifecycle recovery."""
    evidence = _active_stage_evidence(run_dir)
    if evidence is None:
        return None
    pid, starttime, pgid, _unit = evidence
    try:
        if _starttime(pid) != starttime or os.getpgid(pid) != pgid:
            return None
    except OSError:
        return None
    return pid, pgid


def _owned_scope(job_id: str, unit: str) -> bool:
    prefix = f"research-{job_id}-"
    suffix = unit.removeprefix(prefix)
    return unit.startswith(prefix) and (
        suffix in {"validate", "targets", "evaluate", "analysis"}
        or re.fullmatch(r"(?:validate-c\d{3}|targets-s\d{3}|evaluate-s\d{3})", suffix) is not None
    )


def cleanup_stage(job_record: JobRecord) -> bool:
    """Terminate a still-owned stage after a worker dies without terminal evidence."""
    run_dir = Path(job_record.run_dir)
    evidence = _active_stage_evidence(run_dir)
    if evidence is None:
        return False
    pid, stage_starttime, pgid, unit = evidence
    if not _owned_scope(job_record.job_id, unit):
        raise JobError("active stage scope is not owned by this job")

    try:
        scope_stopped = stop_scope(unit)
    except (ContainmentError, OSError) as exc:
        raise JobError(f"stage scope termination could not be verified: {exc}") from exc

    current_starttime = _starttime(pid)
    if current_starttime == stage_starttime:
        try:
            if os.getpgid(pid) != pgid:
                raise JobError("stage identity changed process group")
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            if not _wait_identity_gone(pid, stage_starttime, deadline):
                os.killpg(pgid, signal.SIGKILL)
                if not _wait_identity_gone(pid, stage_starttime, time.monotonic() + 2):
                    raise JobError("stage identity did not terminate")
        except ProcessLookupError:
            if _starttime(pid) == stage_starttime:
                raise JobError("stage identity did not terminate") from None
    elif current_starttime is not None:
        # Never signal a PID that has been recycled since the durable record.
        raise JobError("stage identity changed while cancelling")

    if not scope_stopped:
        raise JobError("stage scope termination could not be verified")
    return True


def launch(
    attempt_dir: Path,
    worktree: Path,
    targets_argv: Sequence[str],
    timeout_seconds: int | float,
    max_rss_mb: int,
    *,
    shared_python: Path,
    expected_commit: str,
    artifact_paths: dict[str, Path],
    artifact_digests: dict[str, str],
    evaluator_source: Path,
    evaluator_source_sha256: str,
    configured_pins: RuntimePins,
    dividends_path: Path,
    job_id: str | None = None,
    run_plan: RunPlan | None = None,
    evaluation_spec_paths: dict[str, Path] | None = None,
    evaluation_spec_digests: dict[str, str] | None = None,
) -> JobRecord:
    """Validate a clean worktree and launch exactly one detached worker."""
    worktree = worktree.resolve()
    attempt_dir = attempt_dir.resolve()
    preflight(worktree, expected_commit)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0 < timeout_seconds <= 7200
        or isinstance(max_rss_mb, bool)
        or not isinstance(max_rss_mb, int)
        or not 0 < max_rss_mb <= 8192
    ):
        raise JobError("timeout and max RSS must be positive")
    shared_python = shared_python.absolute()
    if (
        not shared_python.is_absolute()
        or not shared_python.is_file()
        or not os.access(shared_python.resolve(), os.X_OK)
    ):
        raise JobError("shared interpreter is missing or not executable")
    run_dir = attempt_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    pins = configured_pins
    try:
        verify_configured_runtime_pins(pins)
    except ContainmentError as exc:
        raise JobError(str(exc)) from exc
    if shared_python != pins.shared_python:
        raise JobError("shared interpreter does not match the configured runtime pin")
    if evaluator_source.resolve() != pins.evaluator:
        raise JobError("evaluator source does not match the configured runtime pin")
    if evaluator_source_sha256 != pins.evaluator_sha256:
        raise JobError("evaluator digest does not match the configured runtime pin")
    if run_plan is None:
        raise JobError("immutable run plan is required for launch")
    if not dividends_path.is_file():
        raise JobError("dividends file is missing")
    required_artifacts = {"spec", "panel", "receipt", "evaluation_spec", "dividends"}
    if set(artifact_paths) != required_artifacts or set(artifact_digests) != required_artifacts:
        raise JobError("all frozen input artifacts and digests are required")
    if artifact_digests.get("dividends") != _sha(dividends_path):
        raise JobError("dividends digest does not match frozen artifact")
    if run_plan.attempt_id != attempt_dir.name:
        raise JobError("run plan attempt_id does not match launch directory")
    if run_plan.commit != expected_commit:
        raise JobError("run plan commit does not match expected commit")
    if evaluation_spec_paths is None or evaluation_spec_digests is None:
        raise JobError("run plan evaluation spec paths and digests are required")
    expected_spec_ids = {scenario.spec_id for scenario in run_plan.scenarios}
    if (
        set(evaluation_spec_paths) != expected_spec_ids
        or set(evaluation_spec_digests) != expected_spec_ids
    ):
        raise JobError("run plan evaluation spec bindings contain unexpected entries")
    if (
        run_plan.scenario_timeout_seconds * (len(run_plan.scenarios) + len(evaluation_spec_paths))
        + run_plan.analysis_timeout_seconds
        > timeout_seconds
    ):
        raise JobError("run plan stage timeouts exceed the job timeout")
    primary = next(
        scenario
        for scenario in run_plan.scenarios
        if scenario.scenario_id == run_plan.primary_scenario_id
    )
    if tuple(targets_argv) != primary.targets_argv:
        raise JobError("primary scenario argv does not match launch argv")
    for scenario in run_plan.scenarios:
        target_stage = run_dir / "scenarios" / scenario.scenario_id / "targets-stage"
        _validate_targets(scenario.targets_argv, shared_python, worktree, target_stage)
        spec_path = evaluation_spec_paths.get(scenario.spec_id)
        if spec_path is None or not spec_path.is_file() or spec_path.is_symlink():
            raise JobError(f"missing evaluation spec {scenario.spec_id}")
        if evaluation_spec_digests.get(scenario.spec_id) != _sha(spec_path):
            raise JobError(f"evaluation spec digest mismatch: {scenario.spec_id}")
    job_id = job_id or new_job_id()
    attempt_id = attempt_dir.name
    config: dict[str, object] = {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "targets_argv": list(targets_argv),
        "shared_python": str(shared_python),
        "shared_python_sha256": _sha(shared_python),
        "expected_commit": expected_commit,
        "timeout_seconds": float(timeout_seconds),
        "max_rss_mb": int(max_rss_mb),
        "artifact_paths": {key: str(value.resolve()) for key, value in artifact_paths.items()},
        "artifact_digests": artifact_digests,
        "evaluator_source": str(evaluator_source.resolve()),
        "evaluator_source_sha256": evaluator_source_sha256,
        "snapshot_dir": str(pins.snapshot_dir),
        "snapshot_sha256": pins.snapshot_sha256,
        "resolved_python": str(pins.resolved_python),
        "shared_python_resolved": str(pins.resolved_python),
        "pyvenv_cfg": str(pins.pyvenv_cfg),
        "pyvenv_sha256": pins.pyvenv_sha256,
        "distribution_dir": str(pins.distribution_dir),
        "evaluator_sha256": pins.evaluator_sha256,
        "universe": str(pins.universe),
        "universe_sha256": pins.universe_sha256,
        "dividends_path": str(dividends_path.resolve()),
        "dividends_sha256": _sha(dividends_path),
    }
    plan_json = json.loads(run_plan.to_json())
    config.update(
        {
            "run_plan": plan_json,
            "run_plan_sha256": _sha_text(run_plan.to_json()),
            "evaluation_spec_paths": {
                key: str(value.resolve()) for key, value in evaluation_spec_paths.items()
            },
            "evaluation_spec_digests": evaluation_spec_digests,
            "evaluation_spec_set_sha256": run_plan.evaluation_spec_set_sha256,
        }
    )
    job_file = run_dir / "job.json"
    _write_json(job_file, config)
    log = (run_dir / "logs").resolve()
    log.mkdir(parents=True, exist_ok=True)
    trusted_root = Path(__file__).resolve().parents[2]
    trusted_python = Path(sys.executable).resolve()
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(trusted_root),
    }
    try:
        process = subprocess.Popen(
            [
                str(trusted_python),
                "-P",
                "-s",
                "-m",
                "gateway.research.worker",
                "--job-file",
                str(job_file),
            ],
            cwd=trusted_root,
            stdin=subprocess.DEVNULL,
            stdout=(log / "worker.log").open("ab"),
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
            close_fds=True,
            shell=False,
        )
    except OSError as exc:
        raise JobError(f"worker launch failed: {exc}") from exc
    record = JobRecord(job_id, attempt_id, process.pid, _starttime(process.pid) or 0, str(run_dir))
    _write_json(
        job_file,
        {
            **config,
            "worker_pid": process.pid,
            "worker_starttime": record.worker_starttime,
            "state": record.state,
        },
    )
    return record


def attach(job_record: JobRecord) -> str:
    terminal = Path(job_record.run_dir) / "terminal.json"
    if terminal.is_file():
        try:
            status = json.loads(terminal.read_text(encoding="utf-8")).get("status")
        except (OSError, json.JSONDecodeError):
            status = None
        if status is not None:
            return {
                "succeeded": "EXITED",
                "timed_out": "TIMED_OUT",
                "cancelled": "CANCELLED",
            }.get(str(status), "EXITED")
    if (
        _starttime(job_record.worker_pid) == job_record.worker_starttime
        and job_record.worker_starttime
    ):
        return "ATTACHED"
    cancel_status = _terminal_status(Path(job_record.run_dir) / "cancel.json")
    if cancel_status == "cancelled":
        return "CANCELLED"
    if cancel_status == "cancelling":
        # Cancellation has authority but has not yet proved exact process
        # death.  Reconciliation must not convert that in-flight request into
        # an orphaned or stage-failure outcome.
        return "CANCELLING"
    return "ORPHANED"


def _cancel_marker(path: Path) -> dict[str, object] | None:
    if not _path_present(path):
        return None
    if path.is_symlink():
        raise JobError("cancellation evidence is a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobError("cancellation evidence is invalid") from exc
    if not isinstance(payload, dict):
        raise JobError("cancellation evidence is not an object")
    return {str(key): value for key, value in payload.items()}


def _cancel_marker_matches(marker: dict[str, object], job_record: JobRecord) -> bool:
    return (
        marker.get("job_id") == job_record.job_id
        and type(marker.get("worker_pid")) is int
        and marker.get("worker_pid") == job_record.worker_pid
        and type(marker.get("worker_starttime")) is int
        and marker.get("worker_starttime") == job_record.worker_starttime
        and marker.get("status") in {"cancelling", "cancelled"}
    )


def cancel(job_record: JobRecord) -> str:
    run_dir = Path(job_record.run_dir)
    terminal = run_dir / "terminal.json"
    cancel_path = run_dir / "cancel.json"

    # The lock covers the authority decision and marker publication.  It is
    # released before process termination: the worker's SIGTERM handler also
    # runs cleanup and its finally block needs to take this lock to suppress a
    # late stage-failure terminal record.
    with lifecycle_lock(run_dir):
        terminal_status = _terminal_status(terminal) if terminal.is_file() else None
        if terminal_status is not None:
            return "ALREADY_FINISHED"
        marker = _cancel_marker(cancel_path)
        observed_worker_starttime = _starttime(job_record.worker_pid)
        worker_alive = (
            bool(job_record.worker_starttime)
            and observed_worker_starttime == job_record.worker_starttime
        )
        if marker is not None and not _cancel_marker_matches(marker, job_record):
            raise JobError("stale_job_identity")
        if not worker_alive and observed_worker_starttime is not None:
            raise JobError("stale_job_identity")
        # A caller that died after publishing "cancelling" may be replayed;
        # the matching durable identity is the only authority that permits
        # recovery without signalling a PID that may now belong to another job.
        recovering = marker is not None and not worker_alive
        if not worker_alive and not recovering:
            raise JobError("stale_job_identity")
        if worker_alive:
            _write_json(
                cancel_path,
                {
                    "job_id": job_record.job_id,
                    "worker_pid": job_record.worker_pid,
                    "worker_starttime": job_record.worker_starttime,
                    "targets_exit": -signal.SIGTERM,
                    "evaluator_exit": None,
                    "checks": [{"name": "cancelled", "ok": False}],
                    "outputs": {"targets_sha256": None, "result_sha256": None},
                    "verified_evaluator_sha256": None,
                    "source_writable": None,
                    "shared_env_writable": None,
                    "started_at": _now(),
                    "finished_at": _now(),
                    # Presence is the worker-terminalization authority; the
                    # completed status is published only after exact identities
                    # have been observed dead below.
                    "status": "cancelling",
                },
            )

    try:
        active_before = _active_stage_evidence(run_dir)
        cleanup_stage(job_record)
        if recovering:
            worker_gone = _starttime(job_record.worker_pid) != job_record.worker_starttime
        elif _starttime(job_record.worker_pid) != job_record.worker_starttime:
            # The exact worker has already gone away.  That is a successful
            # cancellation only when no replacement process has the PID.
            worker_gone = True
        else:
            if os.getsid(job_record.worker_pid) != job_record.worker_pid:
                raise JobError("stale_job_identity")
            os.killpg(job_record.worker_pid, signal.SIGTERM)
            worker_gone = _wait_identity_gone(
                job_record.worker_pid, job_record.worker_starttime, time.monotonic() + 10
            )
            if not worker_gone:
                os.killpg(job_record.worker_pid, signal.SIGKILL)
                worker_gone = _wait_identity_gone(
                    job_record.worker_pid, job_record.worker_starttime, time.monotonic() + 2
                )
        if not worker_gone:
            raise JobError("worker identity did not terminate")
        # Re-read durable stage evidence after the worker is gone.  This also
        # makes recovery safe when a canceller died after killing the worker
        # but before it could promote the sidecar to "cancelled".  The first
        # cleanup already proved the same identity; only a newly published
        # stage record needs another scope stop.  Leave the record for the
        # worker's owner cleanup instead of unlinking a concurrently new one.
        active = _active_stage_evidence(run_dir)
        if active is not None and active != active_before:
            cleanup_stage(job_record)
            active = _active_stage_evidence(run_dir)
        if active is not None:
            stage_pid, stage_starttime, _stage_pgid, _stage_unit = active
            if _starttime(stage_pid) == stage_starttime:
                raise JobError("stage identity did not terminate")
            if _starttime(stage_pid) is not None:
                raise JobError("stage identity changed while cancelling")
    except ProcessLookupError:
        if _starttime(job_record.worker_pid) == job_record.worker_starttime:
            raise JobError("worker identity did not terminate") from None
    with lifecycle_lock(run_dir):
        if _terminal_status(terminal) is not None:
            return "ALREADY_FINISHED"
        cancellation = _cancel_marker(cancel_path)
        if cancellation is None:
            raise JobError("cancellation evidence disappeared")
        if not _cancel_marker_matches(cancellation, job_record):
            raise JobError("cancellation evidence identity mismatch")
        cancellation["finished_at"] = _now()
        cancellation["status"] = "cancelled"
        _write_json(cancel_path, cancellation)
    return "CANCELLED"
