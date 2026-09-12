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
from .contracts import RunPlan
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
    active_path = out.parent.parent / "active-stage.json"
    if _path_present(out) or _path_present(err) or _path_present(active_path):
        raise ContainmentError(f"stage output already exists from an earlier run: {out}")
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


def _bounded_regular(path: Path, limit: int) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    return value.st_mode & 0o170000 == 0o100000 and not path.is_symlink() and value.st_size <= limit


def _plan_artifacts(job: dict[str, object]) -> tuple[dict[str, Path], dict[str, str]]:
    paths, digests = _p3b_artifacts(job)
    raw_paths = job.get("evaluation_spec_paths")
    raw_digests = job.get("evaluation_spec_digests")
    if not isinstance(raw_paths, dict) or not isinstance(raw_digests, dict):
        raise ContainmentError("run plan evaluation spec bindings are missing")
    for spec_id, raw_path in raw_paths.items():
        path = Path(str(raw_path))
        digest = str(raw_digests.get(spec_id, ""))
        if path.is_symlink() or not path.is_file() or not _digest_matches(path, digest):
            raise ContainmentError(f"evaluation spec changed or is not regular: {spec_id}")
        paths[f"evaluation:{spec_id}"] = path
        digests[f"evaluation:{spec_id}"] = digest
    return paths, digests


def _scenario_outputs(scenario_dir: Path) -> tuple[Path, Path, Path, Path]:
    target = scenario_dir / "targets-stage" / "targets.json"
    output = scenario_dir / "evaluator-stage" / "out"
    return target, output / "result.json", output / "trades.parquet", output / "daily.parquet"


def _stage_deadline(global_deadline: float, stage_seconds: float) -> float:
    return min(global_deadline, time.monotonic() + stage_seconds)


def _require_empty_directory(path: Path, label: str) -> None:
    _owned_directory(path, label)
    if any(path.iterdir()):
        raise ContainmentError(f"{label} contains output from an earlier stage")


def _write_run_evidence(run_dir: Path, evidence: dict[str, object]) -> str | None:
    with lifecycle_lock(run_dir):
        if _path_present(run_dir / "cancel.json"):
            return None
        path = run_dir / "run-evidence.json"
        _write(path, evidence)
        return _sha(path)


def _contained_run_plan(job: dict[str, object]) -> dict[str, object]:
    """Execute one immutable multi-spec plan through the fixed stage boundary."""
    global _WORKER_INTERRUPTED
    _WORKER_INTERRUPTED = False
    run_dir = Path(str(job["run_dir"]))
    worktree = Path(str(job["worktree"]))
    started = _now()
    stages: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []
    scenarios_evidence: dict[str, object] = {}
    completed: list[str] = []
    status = "containment_unavailable"
    error: str | None = None
    pins: RuntimePins | None = None
    plan: RunPlan | None = None
    plan_digest = str(job.get("run_plan_sha256", ""))
    output_hashes: dict[str, object] = {"scenarios": {}, "analysis": {}}
    validated_digest: str | None = None
    run_evidence_digest: str | None = None
    active_scenario_id: str | None = None
    try:
        _owned_directory(run_dir, "run directory")
        _owned_directory(run_dir / "logs", "run logs")
        raw_plan = job.get("run_plan")
        if not isinstance(raw_plan, dict):
            raise ContainmentError("run plan is missing")
        plan = RunPlan.from_json(json.dumps(raw_plan, sort_keys=True, separators=(",", ":")))
        if hashlib.sha256(plan.to_json().encode()).hexdigest() != plan_digest:
            status = "run_plan_mismatch"
            raise ContainmentError("run plan digest does not match job binding")
        pins = _pins_from_job(job)
        verify_runtime_pins(pins)
        artifacts, digests = _plan_artifacts(job)
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
        if (
            plan.scenario_timeout_seconds > float(timeout_value)
            or plan.analysis_timeout_seconds > float(timeout_value)
            or (
                plan.scenario_timeout_seconds * len(plan.scenarios) + plan.analysis_timeout_seconds
                > float(timeout_value)
            )
        ):
            status = "run_plan_mismatch"
            raise ContainmentError("run plan stage timeouts exceed the job timeout")
        panel, receipt, dividends = artifacts["panel"], artifacts["receipt"], artifacts["dividends"]
        spec_paths = {
            key.removeprefix("evaluation:"): path
            for key, path in artifacts.items()
            if key.startswith("evaluation:")
        }
        spec_digests = {
            key.removeprefix("evaluation:"): digest
            for key, digest in digests.items()
            if key.startswith("evaluation:")
        }
        referenced_specs = {scenario.spec_id for scenario in plan.scenarios}
        if set(spec_paths) != set(spec_digests) or not referenced_specs.issubset(spec_paths):
            status = "run_plan_mismatch"
            raise ContainmentError("run plan scenario spec bindings are incomplete")
        if any(
            scenario.evaluation_spec_sha256 != spec_digests.get(scenario.spec_id)
            for scenario in plan.scenarios
        ):
            status = "run_plan_mismatch"
            raise ContainmentError("run plan scenario spec digest mismatch")
        semantic_by_spec: dict[str, str] = {}
        for spec_id, spec_path in sorted(spec_paths.items()):
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
            stage_name = f"validate-{spec_id}"
            validation_plan = stage_plan(
                pins,
                str(job["job_id"]),
                stage_name,
                max_rss_value,
                validation_command,
                panel=panel,
                receipt=receipt,
                spec=spec_path,
                dividends=dividends,
            )
            tick = time.monotonic()
            validation_exit, timed_out = _stage(
                validation_plan,
                run_dir,
                run_dir / "logs" / f"{stage_name}.out",
                _stage_deadline(deadline, plan.scenario_timeout_seconds),
            )
            stages.append(
                _stage_evidence(run_dir, stage_name, validation_exit, time.monotonic() - tick)
            )
            if timed_out:
                status = "timed_out"
                raise ContainmentError("evaluation spec validation timed out")
            text = (run_dir / "logs" / f"{stage_name}.out").read_text(encoding="utf-8")
            try:
                validation = json.loads(text)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContainmentError(f"validation output is not JSON for {spec_id}") from exc
            if (
                validation_exit != 0
                or not isinstance(validation, dict)
                or validation.get("verdict") != "PASS"
                or not isinstance(validation.get("spec_sha256_semantic"), str)
            ):
                status = "input_validation_failed"
                raise ContainmentError(f"validation failed for {spec_id}")
            expected = {
                "spec_sha256_raw": spec_digests[spec_id],
                "panel_sha256": digests["panel"],
                "receipt_sha256": digests["receipt"],
                "universe_file_sha256": pins.universe_sha256,
                "dividends_sha256": digests["dividends"],
            }
            if any(validation.get(key) != value for key, value in expected.items()):
                status = "input_validation_failed"
                raise ContainmentError(f"validation digests do not bind {spec_id}")
            semantic_by_spec[spec_id] = str(validation["spec_sha256_semantic"])
            verify_runtime_pins(pins)
            _plan_artifacts(job)
            current = _trusted_source(worktree, job.get("expected_commit"))
            checks.append(
                {"name": f"source_after_{stage_name}", "value": current, "ok": current == before}
            )
        _write(
            run_dir / "validated-inputs.json",
            {
                "specs": semantic_by_spec,
                "panel_sha256": digests["panel"],
                "receipt_sha256": digests["receipt"],
                "dividends_sha256": digests["dividends"],
                "snapshot_sha256": pins.snapshot_sha256,
                "universe_sha256": pins.universe_sha256,
            },
        )
        validated_digest = _sha(run_dir / "validated-inputs.json")
        for scenario in plan.scenarios:
            active_scenario_id = scenario.scenario_id
            scenario_dir = run_dir / "scenarios" / scenario.scenario_id
            _require_empty_directory(scenario_dir, "scenario directory")
            target_path, result_path, trades_path, daily_path = _scenario_outputs(scenario_dir)
            from .containment import rewrite_targets_argv

            validate_targets_argv(
                scenario.targets_argv,
                shared_python=pins.shared_python,
                worktree=worktree,
                run_dir=scenario_dir,
                panel=panel,
                receipt=receipt,
            )
            targets = rewrite_targets_argv(
                scenario.targets_argv,
                shared_python=pins.shared_python,
                worktree=worktree,
                run_dir=scenario_dir,
                panel=panel,
                receipt=receipt,
            )
            target_dir = target_path.parent
            _require_empty_directory(target_dir, "scenario targets stage")
            target_plan = stage_plan(
                pins,
                str(job["job_id"]),
                f"targets-{scenario.scenario_id}",
                max_rss_value,
                targets,
                panel=panel,
                receipt=receipt,
                worktree=worktree,
                targets_stage=target_dir,
            )
            tick = time.monotonic()
            target_exit, target_timeout = _stage(
                target_plan,
                run_dir,
                run_dir / "logs" / f"targets-{scenario.scenario_id}.out",
                _stage_deadline(deadline, plan.scenario_timeout_seconds),
            )
            stages.append(
                _stage_evidence(
                    run_dir, f"targets-{scenario.scenario_id}", target_exit, time.monotonic() - tick
                )
            )
            if target_timeout or target_exit != 0 or not target_file_ok(target_path):
                status = "timed_out" if target_timeout else "scenario_failed"
                scenarios_evidence[scenario.scenario_id] = {
                    "status": "failed",
                    "spec_id": scenario.spec_id,
                    "spec_sha256": spec_digests[scenario.spec_id],
                    "targets_exit": target_exit,
                }
                raise ContainmentError(f"targets failed for {scenario.scenario_id}")
            target_digest = _sha(target_path)
            evaluator_dir = scenario_dir / "evaluator-stage"
            _require_empty_directory(evaluator_dir, "scenario evaluator stage")
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
            evaluator_plan = stage_plan(
                pins,
                str(job["job_id"]),
                f"evaluate-{scenario.scenario_id}",
                max_rss_value,
                evaluator_command,
                panel=panel,
                receipt=receipt,
                spec=spec_paths[scenario.spec_id],
                dividends=dividends,
                evaluator_stage=evaluator_dir,
                targets_file=target_path,
            )
            tick = time.monotonic()
            evaluator_exit, evaluator_timeout = _stage(
                evaluator_plan,
                run_dir,
                run_dir / "logs" / f"evaluate-{scenario.scenario_id}.out",
                _stage_deadline(deadline, plan.scenario_timeout_seconds),
            )
            stages.append(
                _stage_evidence(
                    run_dir,
                    f"evaluate-{scenario.scenario_id}",
                    evaluator_exit,
                    time.monotonic() - tick,
                )
            )
            if evaluator_timeout or evaluator_exit != 0:
                status = "timed_out" if evaluator_timeout else "scenario_failed"
                raise ContainmentError(f"evaluator failed for {scenario.scenario_id}")
            output_dir = result_path.parent
            output_entries = list(output_dir.iterdir()) if output_dir.is_dir() else []
            if {path.name for path in output_entries} != {
                "result.json",
                "trades.parquet",
                "daily.parquet",
            } or any(not _bounded_regular(path, 64 * 1024 * 1024) for path in output_entries):
                status = "scenario_failed"
                raise ContainmentError(f"scenario outputs invalid for {scenario.scenario_id}")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if (
                not isinstance(result, dict)
                or result.get("evaluator_version") != "research-evaluator-v2"
                or result.get("spec_sha256") != semantic_by_spec[scenario.spec_id]
                or result.get("dividends_sha256") != digests["dividends"]
            ):
                status = "scenario_failed"
                raise ContainmentError(
                    f"scenario result binding invalid for {scenario.scenario_id}"
                )
            scenarios_evidence[scenario.scenario_id] = {
                "spec_id": scenario.spec_id,
                "spec_sha256": spec_digests[scenario.spec_id],
                "targets_sha256": target_digest,
                "targets_exit": target_exit,
                "evaluator_exit": evaluator_exit,
                "result_path": str(result_path),
                "result_sha256": _sha(result_path),
                "trades_sha256": _sha(trades_path),
                "daily_sha256": _sha(daily_path),
            }
            completed.append(scenario.scenario_id)
            verify_runtime_pins(pins)
            _plan_artifacts(job)
            current = _trusted_source(worktree, job.get("expected_commit"))
            checks.append(
                {
                    "name": f"source_after_{scenario.scenario_id}",
                    "value": current,
                    "ok": current == before,
                }
            )
        analysis_dir = run_dir / "analysis-stage"
        _require_empty_directory(analysis_dir, "analysis stage")
        analysis_out = analysis_dir / "analysis"
        _owned_directory(analysis_out, "analysis output stage")
        analysis_inputs = {
            "panel.parquet": panel,
            "receipt.json": receipt,
            "spec.json": spec_paths[plan.scenarios[0].spec_id],
            "dividends.json": dividends,
            "universe.json": pins.universe,
        }
        analysis_command = (
            str(pins.shared_python),
            "-P",
            "-s",
            "-m",
            plan.analysis.module,
            "--scenarios",
            "/scenarios",
            "--inputs",
            "/inputs",
            "--out",
            "/stage/analysis",
            *plan.analysis.args,
        )
        analysis_plan = stage_plan(
            pins,
            str(job["job_id"]),
            "analysis",
            max_rss_value,
            analysis_command,
            worktree=worktree,
            scenarios_dir=run_dir / "scenarios",
            analysis_stage=analysis_dir,
            analysis_inputs=analysis_inputs,
            evaluation_specs=spec_paths,
        )
        tick = time.monotonic()
        analysis_exit, analysis_timeout = _stage(
            analysis_plan,
            run_dir,
            run_dir / "logs" / "analysis.out",
            _stage_deadline(deadline, plan.analysis_timeout_seconds),
        )
        stages.append(_stage_evidence(run_dir, "analysis", analysis_exit, time.monotonic() - tick))
        declared = set(plan.analysis.artifacts)
        actual: set[str] = set()
        for path in analysis_dir.rglob("*"):
            if path.is_symlink():
                raise ContainmentError("analysis output contains unsafe entry")
            if path.is_file():
                actual.add(path.relative_to(analysis_dir).as_posix())
        if analysis_timeout:
            status = "timed_out"
            raise ContainmentError("analysis timed out")
        if analysis_exit != 0:
            status = "analysis_failed"
            raise ContainmentError("analysis stage failed")
        if actual != declared or any(
            not _bounded_regular(analysis_dir / rel, plan.analysis.max_artifact_bytes)
            for rel in declared
        ):
            status = "analysis_output_invalid"
            raise ContainmentError("analysis output manifest does not match plan")
        output_hashes["analysis"] = {rel: _sha(analysis_dir / rel) for rel in sorted(declared)}
        after = _trusted_source(worktree, job.get("expected_commit"))
        checks.append({"name": "source_after", "value": after, "ok": after == before})
        status = "succeeded"
        evidence = {
            "contract": "research-run-evidence-v1",
            "status": status,
            "job_id": job.get("job_id", ""),
            "attempt_id": plan.attempt_id,
            "run_plan_sha256": plan_digest,
            "evaluation_spec_set_sha256": plan.evaluation_spec_set_sha256,
            "primary_scenario_id": plan.primary_scenario_id,
            "validated_inputs_sha256": validated_digest,
            "scenarios": scenarios_evidence,
            "completed_scenarios": completed,
            "analysis": output_hashes["analysis"],
            "analysis_exit": analysis_exit,
            "checks": checks,
            "stages": stages,
            "started_at": started,
            "finished_at": _now(),
        }
        run_evidence_digest = _write_run_evidence(run_dir, evidence)
        return evidence
    except (
        ContainmentError,
        OSError,
        subprocess.CalledProcessError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        error = str(exc)
        if status == "containment_unavailable":
            if "worktree is dirty" in error or "HEAD changed" in error:
                status = "source_mutated"
            elif (
                "trusted runtime" in error
                or "frozen input" in error
                or "evaluation spec changed" in error
            ):
                status = "input_digest_mismatch"
            elif "run plan" in error:
                status = "run_plan_mismatch"
        if active_scenario_id is not None and active_scenario_id not in scenarios_evidence:
            scenarios_evidence[active_scenario_id] = {
                "status": "failed",
                "spec_id": next(
                    scenario.spec_id
                    for scenario in plan.scenarios
                    if scenario.scenario_id == active_scenario_id
                )
                if plan is not None
                else None,
                "error": error,
            }
        evidence = {
            "contract": "research-run-evidence-v1",
            "status": status,
            "job_id": job.get("job_id", ""),
            "attempt_id": plan.attempt_id if plan else job.get("attempt_id", ""),
            "run_plan_sha256": plan_digest,
            "evaluation_spec_set_sha256": plan.evaluation_spec_set_sha256 if plan else None,
            "primary_scenario_id": plan.primary_scenario_id if plan else None,
            "validated_inputs_sha256": validated_digest,
            "scenarios": scenarios_evidence,
            "completed_scenarios": completed,
            "analysis": output_hashes["analysis"],
            "checks": checks,
            "stages": stages,
            "error": error,
            "started_at": started,
            "finished_at": _now(),
        }
        run_evidence_digest = _write_run_evidence(run_dir, evidence)
        return evidence
    finally:
        if not _WORKER_INTERRUPTED:
            # The run-evidence file is the authoritative terminal record for a
            # planned run; terminal.json only carries the lifecycle summary.
            terminal = {
                "job_id": job.get("job_id", ""),
                "worker_pid": os.getpid(),
                "worker_starttime": _identity(os.getpid()),
                "status": status,
                "run_plan_sha256": plan_digest,
                "completed_scenarios": completed,
                "stages": stages,
                "started_at": started,
                "finished_at": _now(),
            }
            primary_evidence = scenarios_evidence.get(plan.primary_scenario_id) if plan else None
            if isinstance(primary_evidence, dict):
                terminal["targets_exit"] = primary_evidence.get("targets_exit")
                terminal["evaluator_exit"] = primary_evidence.get("evaluator_exit")
            if run_evidence_digest is None and _path_present(run_dir / "run-evidence.json"):
                run_evidence_digest = _sha(run_dir / "run-evidence.json")
            terminal["run_evidence_sha256"] = run_evidence_digest
            _write_terminal_if_authorized(run_dir, terminal)


def _run(job: dict[str, object]) -> dict[str, object]:
    """Run the fixed boundary, with no uncontained execution path."""
    outcome = _contained_run_plan(job)
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
