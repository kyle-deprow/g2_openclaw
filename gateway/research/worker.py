"""Detached trusted worker for the fixed three-stage research boundary."""

# The containment gate is intentionally quoted verbatim in the module docs.

# Terminal records are deliberately assembled as one auditable object.

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .containment import (
    ContainmentError,
    RuntimePins,
    StagePlan,
    host_bus_environment,
    stage_plan,
    target_file_ok,
    validate_targets_argv,
    verify_runtime_pins,
)
from .jobs import lifecycle_lock


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
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_if_absent(path: Path, value: object) -> None:
    """Create first-writer terminal evidence without replacing an owner record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
        stream.flush()
        os.fsync(stream.fileno())


def _path_present(path: Path) -> bool:
    """Return true for regular paths and dangling symlinks alike."""
    return path.exists() or path.is_symlink()


def _write_terminal_if_authorized(run_dir: Path, terminal: dict[str, object]) -> bool:
    """Publish terminal evidence unless cancellation owns this run."""
    with lifecycle_lock(run_dir):
        if _path_present(run_dir / "cancel.json"):
            return False
        _write_if_absent(run_dir / "terminal.json", terminal)
        return True


def _owned_directory(path: Path, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ContainmentError(f"{label} is not a worker-owned directory: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _identity(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
    except (OSError, ValueError, IndexError):
        return 0


def _host_writable(path: Path) -> bool:
    """Report mode-bit writability without treating root's access as evidence."""
    try:
        return bool(path.stat().st_mode & 0o222)
    except OSError:
        return False


_ACTIVE_SCOPE: str | None = None


def _stop(process: subprocess.Popen[Any], scope_unit: str | None = None) -> None:
    scope_stopped = True
    if scope_unit:
        from .containment import stop_scope

        scope_stopped = stop_scope(scope_unit)
    if process.poll() is not None:
        if not scope_stopped:
            raise ContainmentError("stage scope termination could not be verified")
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if not scope_stopped:
            raise ContainmentError("stage scope termination could not be verified") from None
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    if not scope_stopped:
        raise ContainmentError("stage scope termination could not be verified")


_ACTIVE_STAGE: subprocess.Popen[Any] | None = None
_WORKER_INTERRUPTED = False


def _worker_term(_signum: int, _frame: object) -> None:
    """Stop the owned stage before the worker exits on parent/cancel signal."""
    global _WORKER_INTERRUPTED
    _WORKER_INTERRUPTED = True
    if _ACTIVE_STAGE is not None:
        _stop(_ACTIVE_STAGE, _ACTIVE_SCOPE)
    raise SystemExit(143)


def _stage(plan: StagePlan, cwd: Path, out: Path, deadline: float) -> tuple[int, bool]:
    global _ACTIVE_SCOPE, _ACTIVE_STAGE
    scope_unit = plan.scope_unit
    argv = plan.argv
    _owned_directory(out.parent, "stage logs")
    err = out.with_suffix(".err")
    if out.is_symlink() or err.is_symlink():
        raise ContainmentError(f"stage log is a symlink: {out}")
    active_path = out.parent.parent / "active-stage.json"
    with out.open("wb") as stdout, err.open("wb") as stderr:
        remove_active_evidence = True
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
                env={
                    **host_bus_environment(),
                    "LANG": "C",
                },
                start_new_session=True,
                close_fds=True,
                shell=False,
            )
        except OSError as exc:
            raise ContainmentError(
                f"{scope_unit} stage launch failed: {exc}; inspect {err}"
            ) from exc
        _ACTIVE_STAGE = process
        _ACTIVE_SCOPE = scope_unit
        try:
            _write(
                active_path,
                {
                    "pid": process.pid,
                    "starttime": _identity(process.pid),
                    "pgid": os.getpgid(process.pid),
                    "scope_unit": scope_unit,
                },
            )
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=max(0.01, remaining))
            except subprocess.TimeoutExpired:
                _stop(process, scope_unit)
                return (
                    process.returncode if process.returncode is not None else -signal.SIGTERM,
                    True,
                )
        except BaseException:
            try:
                _stop(process, scope_unit)
            except BaseException:
                # Keep the durable identity for an external canceller to
                # retry.  Removing it would hide an unverified scope whose
                # setsid descendants could still be alive.
                remove_active_evidence = False
                raise
            raise
        finally:
            _ACTIVE_STAGE = None
            _ACTIVE_SCOPE = None
            if remove_active_evidence:
                with contextlib.suppress(FileNotFoundError):
                    active_path.unlink()
        return process.returncode or 0, False


def _digest_matches(path: Path, expected: str | None) -> bool:
    try:
        return path.is_file() and expected is not None and _sha(path) == expected
    except OSError:
        return False


def _pins_from_job(job: dict[str, object]) -> RuntimePins:
    names = (
        "snapshot_dir",
        "snapshot_sha256",
        "shared_python",
        "resolved_python",
        "shared_python_sha256",
        "pyvenv_cfg",
        "pyvenv_sha256",
        "distribution_dir",
        "evaluator_source",
        "evaluator_sha256",
        "universe",
        "universe_sha256",
    )
    values = {name: job.get(name) for name in names}
    if not values["resolved_python"]:
        values["resolved_python"] = job.get("shared_python_resolved")
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ContainmentError("complete trusted containment pins are required")
    return RuntimePins(
        Path(str(values["snapshot_dir"])),
        str(values["snapshot_sha256"]),
        Path(str(values["shared_python"])),
        Path(str(values["resolved_python"])),
        str(values["shared_python_sha256"]),
        Path(str(values["shared_python"])).parent.parent,
        Path(str(values["pyvenv_cfg"])),
        str(values["pyvenv_sha256"]),
        Path(str(values["distribution_dir"])),
        Path(str(values["evaluator_source"])),
        str(values["evaluator_sha256"]),
        Path(str(values["universe"])),
        str(values["universe_sha256"]),
    )


def _p3b_artifacts(job: dict[str, object]) -> tuple[dict[str, Path], dict[str, str]]:
    raw_paths = job.get("artifact_paths")
    raw_digests = job.get("artifact_digests")
    if not isinstance(raw_paths, dict) or not isinstance(raw_digests, dict):
        raise ContainmentError("frozen input artifacts are missing")
    paths = {str(key): Path(str(value)) for key, value in raw_paths.items()}
    digests = {str(key): str(value) for key, value in raw_digests.items()}
    required = {"spec", "panel", "receipt", "evaluation_spec", "dividends"}
    if set(paths) != required or set(digests) != required:
        raise ContainmentError("panel, receipt, spec, evaluator spec, and dividends are required")
    for key, path in paths.items():
        if path.is_symlink() or not path.is_file() or not _digest_matches(path, digests.get(key)):
            raise ContainmentError(f"frozen input changed or is not regular: {key}")
    return paths, digests


def _trusted_source(worktree: Path, expected_commit: object) -> dict[str, str]:
    if (
        not isinstance(expected_commit, str)
        or len(expected_commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in expected_commit)
    ):
        raise ContainmentError("expected source commit pin is required")
    observed = _source(worktree)
    if observed["porcelain"]:
        raise ContainmentError("worktree is dirty during contained execution")
    if observed["head"] != expected_commit:
        raise ContainmentError("worktree HEAD changed during contained execution")
    return observed


def _stage_evidence(run_dir: Path, stage: str, exit_code: int, wall: float) -> dict[str, object]:
    stdout = run_dir / "logs" / f"{stage}.out"
    stderr = run_dir / "logs" / f"{stage}.err"
    evidence: dict[str, object] = {
        "stage": stage,
        "exit": exit_code,
        "wall_seconds": wall,
        "stdout_sha256": None,
        "stderr_sha256": None,
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
    }
    if stdout.is_file():
        evidence["stdout_sha256"] = _sha(stdout)
    if stderr.is_file():
        evidence["stderr_sha256"] = _sha(stderr)
    return evidence


def _contained_run(job: dict[str, object]) -> dict[str, object]:
    global _WORKER_INTERRUPTED
    _WORKER_INTERRUPTED = False
    run_dir = Path(str(job["run_dir"]))
    worktree = Path(str(job["worktree"]))
    started = _now()
    checks: list[dict[str, object]] = []
    stages: list[dict[str, object]] = []
    output_hashes: dict[str, str | None] = {
        "targets_sha256": None,
        "result_sha256": None,
        "validated_inputs_sha256": None,
    }
    targets_exit: int | None = None
    evaluator_exit: int | None = None
    status = "containment_unavailable"
    error: str | None = None
    pins: RuntimePins | None = None
    source_writable = _host_writable(worktree)
    shared_env_writable = _host_writable(Path(str(job.get("shared_python", ""))).parent)
    try:
        _owned_directory(run_dir, "run directory")
        _owned_directory(run_dir / "logs", "run logs")
        pins = _pins_from_job(job)
        verify_runtime_pins(pins)
        artifacts, digests = _p3b_artifacts(job)
        before = _trusted_source(worktree, job.get("expected_commit"))
        checks.append({"name": "source_before", "value": before, "ok": True})
        timeout_value = job.get("timeout_seconds")
        max_rss_value = job.get("max_rss_mb")
        if (
            isinstance(timeout_value, bool)
            or not isinstance(timeout_value, (int, float))
            or not math.isfinite(float(timeout_value))
            or not 0 < float(timeout_value) <= 7200
            or isinstance(max_rss_value, bool)
            or not isinstance(max_rss_value, int)
            or not 0 < max_rss_value <= 8192
        ):
            raise ContainmentError("invalid resource limits")
        deadline = time.monotonic() + float(timeout_value)
        panel = artifacts["panel"]
        receipt = artifacts["receipt"]
        eval_spec = artifacts["evaluation_spec"]
        dividends = artifacts["dividends"]
        validation_command = (
            str(pins.shared_python),
            "-P",
            "-s",
            str(pins.evaluator),
            "research",
            "validate-inputs",
            "--panel",
            "/inputs/panel.parquet",
            "--receipt",
            "/inputs/receipt.json",
            "--spec",
            "/inputs/spec.json",
            "--dividends",
            "/inputs/dividends.json",
            "--universe",
            "/universe.json",
        )
        validation_plan = stage_plan(
            pins,
            str(job["job_id"]),
            "validate",
            max_rss_value,
            validation_command,
            panel=panel,
            receipt=receipt,
            spec=eval_spec,
            dividends=dividends,
        )
        tick = time.monotonic()
        validation_exit, timed_out = _stage(
            validation_plan, run_dir, run_dir / "logs" / "validate.out", deadline
        )
        stages.append(
            _stage_evidence(run_dir, "validate", validation_exit, time.monotonic() - tick)
        )
        containment_check: dict[str, object] = {
            "name": "containment",
            "ok": True,
            "stages": ["validate", "targets", "evaluate"],
            "exercised": "validate",
        }
        checks.append(containment_check)
        if timed_out:
            status = "timed_out"
            return {"status": status}
        validation_text = (run_dir / "logs" / "validate.out").read_text(encoding="utf-8")
        if validation_exit != 0 and not validation_text.strip():
            status = "containment_unavailable"
            error = (
                f"validate stage launch failed before structured output (exit={validation_exit}); "
                "inspect logs/validate.out and logs/validate.err"
            )
            containment_check.update({"ok": False, "reason": error})
            return {"status": status, "error": error, "targets_exit": validation_exit}
        try:
            validation = json.loads(validation_text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContainmentError(
                f"validate-inputs did not return structured JSON: {exc}"
            ) from exc
        if validation_exit != 0 or not isinstance(validation, dict):
            status = "input_validation_failed"
            error = "validate-inputs exited unsuccessfully"
            return {"status": status, "error": error}
        semantic = validation.get("spec_sha256_semantic")
        if (
            validation.get("verdict") != "PASS"
            or not isinstance(semantic, str)
            or len(semantic) != 64
            or any(character not in "0123456789abcdef" for character in semantic)
        ):
            status = "input_validation_failed"
            error = "trusted input validation failed"
            return {"status": status, "error": error}
        expected_validation_digests = {
            "spec_sha256_raw": digests["evaluation_spec"],
            "panel_sha256": digests["panel"],
            "receipt_sha256": digests["receipt"],
            "universe_file_sha256": pins.universe_sha256,
            "dividends_sha256": digests["dividends"],
        }
        if any(validation.get(key) != value for key, value in expected_validation_digests.items()):
            status = "input_validation_failed"
            error = "validation digests do not bind frozen inputs"
            return {"status": status, "error": error}
        _write(
            run_dir / "validated-inputs.json",
            {
                "validation": validation,
                "spec_sha256_raw": digests["evaluation_spec"],
                "spec_sha256_semantic": semantic,
                "dividends_sha256": digests["dividends"],
                "snapshot_sha256": pins.snapshot_sha256,
                "universe_sha256": pins.universe_sha256,
            },
        )
        output_hashes["validated_inputs_sha256"] = _sha(run_dir / "validated-inputs.json")
        _p3b_artifacts(job)
        verify_runtime_pins(pins)
        middle = _trusted_source(worktree, job.get("expected_commit"))
        checks.append({"name": "source_after_validate", "value": middle, "ok": middle == before})
        targets_value = job.get("targets_argv")
        if not isinstance(targets_value, list):
            raise ContainmentError("invalid target argv")
        from .containment import rewrite_targets_argv

        validate_targets_argv(
            tuple(str(item) for item in targets_value),
            shared_python=pins.shared_python,
            worktree=worktree,
            run_dir=run_dir,
            panel=panel,
            receipt=receipt,
        )
        targets = rewrite_targets_argv(
            tuple(str(item) for item in targets_value),
            shared_python=pins.shared_python,
            worktree=worktree,
            run_dir=run_dir,
            panel=panel,
            receipt=receipt,
        )
        targets_dir = run_dir / "targets-stage"
        _owned_directory(targets_dir, "targets stage")
        target_file = targets_dir / "targets.json"
        if target_file.exists() or target_file.is_symlink():
            raise ContainmentError("targets output already exists from an earlier stage")
        target_plan = stage_plan(
            pins,
            str(job["job_id"]),
            "targets",
            max_rss_value,
            targets,
            panel=panel,
            receipt=receipt,
            worktree=worktree,
            targets_stage=targets_dir,
        )
        tick = time.monotonic()
        targets_exit, timed_out = _stage(
            target_plan, run_dir, run_dir / "logs" / "targets.out", deadline
        )
        stages.append(_stage_evidence(run_dir, "targets", targets_exit, time.monotonic() - tick))
        if not target_file_ok(target_file):
            status = "timed_out" if timed_out else "targets_failed"
            error = "targets output is missing, non-regular, or oversized"
            return {"status": status, "error": error, "targets_exit": targets_exit}
        output_hashes["targets_sha256"] = _sha(target_file)
        if timed_out or targets_exit != 0:
            status = "timed_out" if timed_out else "targets_failed"
            return {"status": status, "targets_exit": targets_exit}
        verify_runtime_pins(pins)
        _p3b_artifacts(job)
        middle = _trusted_source(worktree, job.get("expected_commit"))
        checks.append({"name": "source_after_targets", "value": middle, "ok": middle == before})
        evaluator_command = (
            str(pins.shared_python),
            "-P",
            "-s",
            str(pins.evaluator),
            "research",
            "evaluate",
            "--panel",
            "/inputs/panel.parquet",
            "--receipt",
            "/inputs/receipt.json",
            "--spec",
            "/inputs/spec.json",
            "--targets",
            "/targets.json",
            "--dividends",
            "/inputs/dividends.json",
            "--out",
            "/stage/out",
            "--require-source-root",
            "/snapshot/src",
        )
        evaluator_dir = run_dir / "evaluator-stage"
        _owned_directory(evaluator_dir, "evaluator stage")
        evaluator_output = evaluator_dir / "out"
        # Quantipy creates its output directory itself and deliberately refuses
        # to overwrite an existing one.  Never pre-create, remove, or reuse it;
        # reject regular files, directories, and dangling symlinks alike.
        if _path_present(evaluator_output):
            raise ContainmentError("evaluator output already exists from an earlier stage")
        result_path = evaluator_output / "result.json"
        evaluator_plan = stage_plan(
            pins,
            str(job["job_id"]),
            "evaluate",
            max_rss_value,
            evaluator_command,
            panel=panel,
            receipt=receipt,
            spec=eval_spec,
            dividends=dividends,
            evaluator_stage=evaluator_dir,
            targets_file=target_file,
        )
        tick = time.monotonic()
        evaluator_exit, timed_out = _stage(
            evaluator_plan, run_dir, run_dir / "logs" / "evaluate.out", deadline
        )
        stages.append(_stage_evidence(run_dir, "evaluate", evaluator_exit, time.monotonic() - tick))
        if result_path.is_file() and not result_path.is_symlink():
            output_hashes["result_sha256"] = _sha(result_path)
        if timed_out:
            status = "timed_out"
            return {"status": status, "evaluator_exit": evaluator_exit}
        if evaluator_exit != 0 or not result_path.is_file() or result_path.is_symlink():
            if (
                evaluator_exit != 0
                and not (run_dir / "logs" / "evaluate.out").read_text(encoding="utf-8").strip()
            ):
                status = "containment_unavailable"
                error = (
                    f"evaluate stage launch failed before result output (exit={evaluator_exit}); "
                    "inspect logs/evaluate.out and logs/evaluate.err"
                )
            else:
                status = "evaluator_failed"
                error = "trusted evaluator did not produce a result"
            return {"status": status, "evaluator_exit": evaluator_exit, "error": error}
        _p3b_artifacts(job)
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContainmentError(f"invalid evaluator result: {exc}") from exc
        if not isinstance(loaded, dict):
            status = "evaluator_failed"
            error = "evaluator result is not an object"
            return {"status": status, "evaluator_exit": evaluator_exit, "error": error}
        result = loaded
        if (
            result.get("evaluator_version") != "research-evaluator-v2"
            or result.get("spec_sha256") != semantic
            or result.get("dividends_sha256") != digests["dividends"]
        ):
            status = "evaluator_failed"
            error = "evaluator result does not bind validated semantic inputs"
            return {"status": status, "evaluator_exit": evaluator_exit, "error": error}
        verify_runtime_pins(pins)
        after = _trusted_source(worktree, job.get("expected_commit"))
        checks.append({"name": "source_after", "value": after, "ok": after == before})
        status = "succeeded"
        return {"status": status, "targets_exit": targets_exit, "evaluator_exit": evaluator_exit}
    except ContainmentError as exc:
        error = str(exc)
        if "worktree" in error and ("dirty" in error or "HEAD changed" in error):
            status = "source_mutated"
        elif "trusted runtime or input pin changed" in error or "frozen input changed" in error:
            status = "input_digest_mismatch"
        else:
            status = "containment_unavailable"
        failed_containment_check: dict[str, object] | None = next(
            (check for check in checks if check.get("name") == "containment"), None
        )
        if failed_containment_check is None:
            checks.append({"name": "containment", "ok": False, "reason": error})
        elif status == "containment_unavailable" and not failed_containment_check.get("exercised"):
            failed_containment_check.update({"ok": False, "reason": error})
        return {"status": status, "error": error}
    except (OSError, subprocess.CalledProcessError, KeyError, TypeError, ValueError) as exc:
        error = str(exc)
        status = "evaluator_failed"
        return {"status": status, "error": error}
    finally:
        # Cancellation owns a sibling cancel.json written by the controlling
        # process.  Never let a late SIGTERM replace a worker terminal record
        # (or manufacture one that hides the cancellation evidence).
        if not _WORKER_INTERRUPTED:
            terminal: dict[str, object] = {
                "job_id": job.get("job_id", ""),
                "worker_pid": os.getpid(),
                "worker_starttime": _identity(os.getpid()),
                "targets_exit": targets_exit,
                "evaluator_exit": evaluator_exit,
                "checks": checks,
                "stages": stages,
                "outputs": output_hashes,
                "verified_evaluator_sha256": pins.evaluator_sha256 if pins else None,
                "verified_evaluator_source_snapshot_sha256": (
                    pins.snapshot_sha256 if pins else None
                ),
                "evaluator_stub_sha256": pins.evaluator_sha256 if pins else None,
                "source_writable": source_writable,
                "shared_env_writable": shared_env_writable,
                "started_at": started,
                "finished_at": _now(),
                "status": status,
            }
            if error is not None:
                terminal["error"] = error
            # A trusted host may have finalized a launch failure while this
            # detached child was unwinding.  Never replace that first terminal
            # record with a late worker result.  Cancellation and this write
            # share a per-job lock and cancellation authority is checked while
            # holding it, so a killed target cannot become targets_failed.
            _write_terminal_if_authorized(run_dir, terminal)


def _run(job: dict[str, object]) -> dict[str, object]:
    """Run the fixed boundary, with no uncontained execution path."""
    outcome = _contained_run(job)
    run_dir = Path(str(job["run_dir"]))
    # The stage can finish computing a result after cancellation has acquired
    # authority but before the worker reaches its finally block.  The terminal
    # writer correctly refuses that late record; make the worker's own result
    # and exit status reflect the same cancellation instead of reporting 0.
    if not _path_present(run_dir / "terminal.json") and _path_present(run_dir / "cancel.json"):
        return {
            **outcome,
            "status": "cancelled",
            "error": "cancellation authority won before terminalization",
        }
    return outcome


def run(job: dict[str, object]) -> dict[str, object]:
    """Run through the fixed namespace boundary, with no uncontained fallback."""
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
    outcome = run(job)
    return 0 if outcome.get("status") == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
