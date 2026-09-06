"""Owned research worker.

This slice proves detection and refusal, not containment.
Containment is the P3b deployment gate: read-only bind or ownership of the worktree and shared environment for the runner identity, writable only `RUN/`.
The evaluator implementation pin is also unavailable until that gate is met.
"""

# The containment gate is intentionally quoted verbatim in the module docs.
# ruff: noqa: E501

# Terminal records are deliberately assembled as one auditable object.

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(worktree: Path) -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ("git", *args), cwd=worktree, capture_output=True, text=True, check=True
        ).stdout

    untracked = git("ls-files", "-o", "--exclude-standard", "-z").encode()
    return {
        "head": git("rev-parse", "HEAD").strip(),
        "porcelain": git("status", "--porcelain", "--untracked-files=all"),
        "untracked_sha256": hashlib.sha256(untracked).hexdigest(),
    }


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _identity(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
    except (OSError, ValueError, IndexError):
        return 0


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


_ACTIVE_STAGE: subprocess.Popen[Any] | None = None
_WORKER_INTERRUPTED = False


def _worker_term(_signum: int, _frame: object) -> None:
    """Stop the owned stage before the worker exits on parent/cancel signal."""
    global _WORKER_INTERRUPTED
    _WORKER_INTERRUPTED = True
    if _ACTIVE_STAGE is not None:
        _stop(_ACTIVE_STAGE)
    raise SystemExit(143)


def _stage(argv: list[str], cwd: Path, out: Path, deadline: float) -> tuple[int, bool]:
    global _ACTIVE_STAGE
    active_path = out.parent.parent / "active-stage.json"
    with out.open("wb") as stdout, (out.with_suffix(".err")).open("wb") as stderr:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
            shell=False,
        )
        _ACTIVE_STAGE = process
        try:
            _write(
                active_path,
                {
                    "pid": process.pid,
                    "starttime": _identity(process.pid),
                    "pgid": os.getpgid(process.pid),
                },
            )
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=max(0.01, remaining))
            except subprocess.TimeoutExpired:
                _stop(process)
                return (
                    process.returncode if process.returncode is not None else -signal.SIGTERM,
                    True,
                )
        except BaseException:
            _stop(process)
            raise
        finally:
            _ACTIVE_STAGE = None
            with contextlib.suppress(FileNotFoundError):
                active_path.unlink()
        return process.returncode or 0, False


def _contained(
    worktree: Path,
    shared_python: Path,
    evaluator_source: Path,
    artifact_paths: dict[str, Path],
) -> bool:
    """Require the P3b read-only ownership boundary before running stages."""
    writable = [
        os.access(worktree, os.W_OK),
        os.access(shared_python.parent, os.W_OK),
        os.access(evaluator_source, os.W_OK),
    ]
    writable.extend(os.access(path, os.W_OK) for path in artifact_paths.values())
    return not any(writable)


def _artifacts_match(artifact_paths: dict[str, Path], artifact_digests: dict[str, str]) -> bool:
    if set(artifact_paths) != {"spec", "panel", "receipt", "evaluation_spec"}:
        return False
    return all(
        _digest_matches(path, artifact_digests.get(key)) for key, path in artifact_paths.items()
    )


def _digest_matches(path: Path, expected: str | None) -> bool:
    try:
        return path.is_file() and expected is not None and _sha(path) == expected
    except OSError:
        return False


def _run(
    job: dict[str, object],
    *,
    containment_check: Callable[[Path, Path, Path, dict[str, Path]], bool] | None = None,
) -> dict[str, object]:
    global _WORKER_INTERRUPTED
    _WORKER_INTERRUPTED = False
    run_dir = Path(str(job["run_dir"]))
    worktree = Path(str(job["worktree"]))
    started = _now()
    checks: list[dict[str, object]] = []
    targets_exit: int | None = None
    evaluator_exit: int | None = None
    status = "evaluator_failed"
    output_hashes: dict[str, str | None] = {"targets_sha256": None, "result_sha256": None}
    source_writable = os.access(worktree, os.W_OK)
    shared_python = Path(str(job["shared_python"]))
    shared_env_writable = os.access(shared_python.parent, os.W_OK)
    try:
        raw_paths = job.get("artifact_paths", {})
        raw_digests = job.get("artifact_digests", {})
        artifact_paths = {
            str(key): Path(str(value))
            for key, value in (raw_paths.items() if isinstance(raw_paths, dict) else [])
        }
        artifact_digests = {
            str(key): str(value)
            for key, value in (raw_digests.items() if isinstance(raw_digests, dict) else [])
        }
        executable_mismatch = not _digest_matches(shared_python, str(job["shared_python_sha256"]))
        source_path = Path(str(job.get("evaluator_source", "")))
        executable_mismatch = executable_mismatch or not _digest_matches(
            source_path, str(job.get("evaluator_source_sha256", ""))
        )
        artifact_mismatch = not _artifacts_match(artifact_paths, artifact_digests)
        checks.append(
            {"name": "input_digests", "ok": not (executable_mismatch or artifact_mismatch)}
        )
        if executable_mismatch or artifact_mismatch:
            status = "input_digest_mismatch"
            return {"status": status}
        contained = (
            containment_check(worktree, shared_python, source_path, artifact_paths)
            if containment_check is not None
            else _contained(worktree, shared_python, source_path, artifact_paths)
        )
        checks.append({"name": "containment", "ok": contained})
        if not contained:
            status = "containment_unavailable"
            return {"status": status, "error": "read-only runtime required"}
        implementation_pinned = job.get("evaluator_implementation_pinned") is True
        checks.append(
            {
                "name": "evaluator_implementation_pin",
                "ok": implementation_pinned,
                "reason": "P3b gate unavailable" if not implementation_pinned else None,
            }
        )
        if not implementation_pinned:
            status = "evaluator_failed"
            return {"status": status, "error": "evaluator implementation pin unavailable"}
        before = _source(worktree)
        checks.append({"name": "source_before", "value": before, "ok": True})
        timeout_value = job["timeout_seconds"]
        if not isinstance(timeout_value, (int, float)):
            raise ValueError("invalid timeout")
        deadline = time.monotonic() + float(timeout_value)
        targets_value = job["targets_argv"]
        if not isinstance(targets_value, list):
            raise ValueError("invalid targets argv")
        targets = [str(item) for item in targets_value]
        targets_exit, timed_out = _stage(
            targets, worktree, run_dir / "logs" / "targets.out", deadline
        )
        middle = _source(worktree)
        checks.append({"name": "source_between", "value": middle, "ok": middle == before})
        if middle != before:
            status = "source_mutated"
            return {"status": status, "targets_exit": targets_exit}
        if timed_out:
            status = "timed_out"
            return {"status": status, "targets_exit": targets_exit}
        targets_path = run_dir / "targets.json"
        if targets_exit != 0 or not targets_path.is_file():
            status = "targets_failed"
            return {"status": status, "targets_exit": targets_exit}
        output_hashes["targets_sha256"] = _sha(targets_path)
        inputs_between = _artifacts_match(artifact_paths, artifact_digests)
        checks.append({"name": "inputs_between", "ok": inputs_between})
        if not inputs_between:
            status = "input_digest_mismatch"
            return {"status": status, "targets_exit": targets_exit}
        evaluator_value = job["evaluator_argv"]
        if not isinstance(evaluator_value, list):
            raise ValueError("invalid evaluator argv")
        evaluator = [str(item) for item in evaluator_value]
        evaluator_exit, timed_out = _stage(
            evaluator, worktree, run_dir / "logs" / "evaluator.out", deadline
        )
        after = _source(worktree)
        checks.append({"name": "source_after", "value": after, "ok": after == before})
        if after != before:
            status = "source_mutated"
            return {
                "status": status,
                "targets_exit": targets_exit,
                "evaluator_exit": evaluator_exit,
            }
        inputs_after = _artifacts_match(artifact_paths, artifact_digests)
        checks.append({"name": "inputs_after", "ok": inputs_after})
        if not inputs_after:
            status = "input_digest_mismatch"
            return {
                "status": status,
                "targets_exit": targets_exit,
                "evaluator_exit": evaluator_exit,
            }
        if timed_out:
            status = "timed_out"
            return {
                "status": status,
                "targets_exit": targets_exit,
                "evaluator_exit": evaluator_exit,
            }
        result_path = run_dir / "out" / "result.json"
        if result_path.is_file():
            output_hashes["result_sha256"] = _sha(result_path)
        if evaluator_exit != 0 or not result_path.is_file():
            status = "evaluator_failed"
            return {
                "status": status,
                "targets_exit": targets_exit,
                "evaluator_exit": evaluator_exit,
            }
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None
        if (
            not isinstance(result, dict)
            or result.get("evaluator_version") != "research-evaluator-v1"
            or result.get("spec_sha256") != artifact_digests.get("evaluation_spec")
        ):
            status = "evaluator_failed"
            return {
                "status": status,
                "targets_exit": targets_exit,
                "evaluator_exit": evaluator_exit,
            }
        status = "succeeded"
        return {"status": status, "targets_exit": targets_exit, "evaluator_exit": evaluator_exit}
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        checks.append({"name": "worker_error", "error": str(exc), "ok": False})
        status = "evaluator_failed"
        return {
            "status": status,
            "error": str(exc),
            "targets_exit": targets_exit,
            "evaluator_exit": evaluator_exit,
        }
    finally:
        if _WORKER_INTERRUPTED and status not in {"succeeded", "source_mutated"}:
            status = "timed_out"
        terminal = {
            "job_id": job["job_id"],
            "worker_pid": os.getpid(),
            "worker_starttime": _identity(os.getpid()),
            "targets_exit": targets_exit,
            "evaluator_exit": evaluator_exit,
            "checks": checks,
            "outputs": output_hashes,
            "verified_evaluator_sha256": job.get("evaluator_source_sha256"),
            "evaluator_implementation_pinned": job.get("evaluator_implementation_pinned") is True,
            "source_writable": source_writable,
            "shared_env_writable": shared_env_writable,
            "started_at": started,
            "finished_at": _now(),
            "status": status,
        }
        _write(run_dir / "terminal.json", terminal)


def run(job: dict[str, object]) -> dict[str, object]:
    """Run only when the deployed read-only containment boundary is present."""
    return _run(job)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-file", required=True)
    args = parser.parse_args()
    job_file = Path(args.job_file)
    job = json.loads(job_file.read_text(encoding="utf-8"))
    if not isinstance(job, dict):
        raise SystemExit("job file must contain an object")
    signal.signal(signal.SIGTERM, _worker_term)
    with contextlib.suppress(OSError, ValueError):
        resource.setrlimit(
            resource.RLIMIT_AS,
            (int(job["max_rss_mb"]) * 1024 * 1024, int(job["max_rss_mb"]) * 1024 * 1024),
        )
    run(job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
