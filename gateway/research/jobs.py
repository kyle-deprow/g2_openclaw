"""Detached worker launcher and conservative process identity handling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


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


def _validate_executable(path: Path, worktree: Path) -> None:
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise JobError(f"executable is missing or not executable: {path}")
    resolved = path.resolve()
    allowed = (
        worktree.resolve() in resolved.parents or (worktree / ".venv").resolve() in resolved.parents
    )
    if not allowed:
        raise JobError(f"executable is outside worktree/.venv: {path}")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _active_stage(run_dir: Path) -> tuple[int, int] | None:
    path = run_dir / "active-stage.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        starttime = int(payload["starttime"])
        pgid = int(payload["pgid"])
        if _starttime(pid) != starttime or os.getpgid(pid) != pgid or pgid <= 1:
            return None
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return pid, pgid


def cleanup_stage(job_record: JobRecord) -> bool:
    """Terminate a still-owned stage after a worker dies without terminal evidence."""
    active = _active_stage(Path(job_record.run_dir))
    if active is None:
        return False
    pid, pgid = active
    try:
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _starttime(pid) is not None:
            time.sleep(0.05)
        if _starttime(pid) is not None:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def launch(
    attempt_dir: Path,
    worktree: Path,
    targets_argv: Sequence[str],
    evaluator_argv: Sequence[str],
    timeout_seconds: int | float,
    max_rss_mb: int,
    *,
    shared_python: Path | None = None,
    expected_commit: str | None = None,
    artifact_paths: dict[str, Path] | None = None,
    artifact_digests: dict[str, str] | None = None,
    evaluator_source: Path | None = None,
    evaluator_source_sha256: str | None = None,
    evaluator_implementation_pinned: bool = False,
    job_id: str | None = None,
) -> JobRecord:
    """Validate a clean worktree and launch exactly one detached worker."""
    worktree = worktree.resolve()
    attempt_dir = attempt_dir.resolve()
    preflight(worktree, expected_commit)
    head = _run(("git", "rev-parse", "HEAD"), worktree)
    if timeout_seconds <= 0 or max_rss_mb <= 0:
        raise JobError("timeout and max RSS must be positive")
    if shared_python is None:
        if not targets_argv:
            raise JobError("targets argv is empty")
        shared_python = Path(targets_argv[0]).resolve()
    else:
        shared_python = shared_python.resolve()
    if (
        not shared_python.is_absolute()
        or not shared_python.is_file()
        or not os.access(shared_python, os.X_OK)
    ):
        raise JobError("shared interpreter is missing or not executable")
    run_dir = attempt_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    _validate_targets(targets_argv, shared_python, worktree, run_dir)
    if evaluator_argv:
        evaluator_path = Path(evaluator_argv[0]).resolve()
        try:
            _validate_executable(evaluator_path, worktree)
        except JobError:
            if evaluator_source is None or evaluator_path != evaluator_source.resolve():
                raise
    for token in evaluator_argv:
        _validate_token(token)
    job_id = job_id or new_job_id()
    attempt_id = attempt_dir.name
    config: dict[str, object] = {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "targets_argv": list(targets_argv),
        "evaluator_argv": list(evaluator_argv),
        "shared_python": str(shared_python),
        "shared_python_sha256": _sha(shared_python),
        "expected_commit": expected_commit or head,
        "timeout_seconds": float(timeout_seconds),
        "max_rss_mb": int(max_rss_mb),
        "artifact_paths": {
            key: str(value.resolve()) for key, value in (artifact_paths or {}).items()
        },
        "artifact_digests": artifact_digests or {},
        "evaluator_source": str(evaluator_source.resolve())
        if evaluator_source
        else (evaluator_argv[0] if evaluator_argv else ""),
        "evaluator_source_sha256": evaluator_source_sha256
        or (_sha(Path(evaluator_argv[0])) if evaluator_argv else ""),
        # The value comes from the trusted driver configuration.  ``init``
        # records false until P3b supplies an implementation attestation;
        # disposable tests may inject a pinned fixture explicitly.
        "evaluator_implementation_pinned": evaluator_implementation_pinned,
    }
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
    if (Path(job_record.run_dir) / "cancel.json").is_file():
        return "CANCELLED"
    return "ORPHANED"


def cancel(job_record: JobRecord) -> str:
    terminal = Path(job_record.run_dir) / "terminal.json"
    if terminal.is_file():
        return "CANCELLED"
    if (
        _starttime(job_record.worker_pid) != job_record.worker_starttime
        or not job_record.worker_starttime
    ):
        raise JobError("stale_job_identity")
    try:
        cleanup_stage(job_record)
        if os.getsid(job_record.worker_pid) != job_record.worker_pid:
            raise JobError("stale_job_identity")
        os.killpg(job_record.worker_pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _starttime(job_record.worker_pid) is not None:
            time.sleep(0.05)
        if _starttime(job_record.worker_pid) is not None:
            os.killpg(job_record.worker_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    terminal_status: str | None = None
    if terminal.is_file():
        try:
            value = json.loads(terminal.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("status"), str):
                terminal_status = value["status"]
        except (OSError, json.JSONDecodeError):
            pass
    if terminal_status is None:
        _write_json(
            Path(job_record.run_dir) / "cancel.json",
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
                "status": "cancelled",
            },
        )
    return "CANCELLED"
