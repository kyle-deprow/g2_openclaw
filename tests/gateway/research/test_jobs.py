from __future__ import annotations

# Compact subprocess fixture payloads are intentionally single-line scripts.
# ruff: noqa: E501
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.research import worker
from gateway.research.containment import runtime_pins
from gateway.research.contracts import AnalysisPlan, ImplementationRecord, RunPlan, RunScenario
from gateway.research.jobs import (
    JobError,
    JobRecord,
    _validate_targets,
    attach,
    cancel,
    cleanup_stage,
    launch,
    preflight,
)


def _fixture(
    tmp_path: Path, *, sleep: float = 0.0, mutate: bool = False, malicious_worker: bool = False
) -> tuple[Path, Path, Path, str, Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=worktree, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=worktree, check=True)
    target = worktree / "target.py"
    mutation = (
        f"pathlib.Path({str(worktree / 'mutated.txt')!r}).write_text('x')\n" if mutate else ""
    )
    target.write_text(
        f"import argparse, pathlib, time\np=argparse.ArgumentParser(); p.add_argument('--out'); a=p.parse_args(); time.sleep({sleep})\n{mutation}pathlib.Path(a.out).write_text('{{}}')\n",
        encoding="utf-8",
    )
    if malicious_worker:
        (worktree / "gateway" / "research").mkdir(parents=True)
        (worktree / "gateway" / "__init__.py").write_text("", encoding="utf-8")
        (worktree / "gateway" / "research" / "__init__.py").write_text("", encoding="utf-8")
        (worktree / "gateway" / "research" / "worker.py").write_text(
            "raise SystemExit('worktree worker hijack')\n", encoding="utf-8"
        )
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=worktree, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
    evaluator = tmp_path / "evaluator.py"
    eval_spec = tmp_path / "eval-spec.json"
    eval_spec.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(eval_spec.read_bytes()).hexdigest()
    evaluator.write_text(
        "#!/usr/bin/env python3\nimport argparse, json, pathlib\np=argparse.ArgumentParser(); p.add_argument('x',nargs='*'); p.add_argument('--out'); p.add_argument('--spec'); p.add_argument('--targets'); p.add_argument('--panel'); p.add_argument('--receipt'); a=p.parse_args(); pathlib.Path(a.out).mkdir(exist_ok=True); json.dump({'evaluator_version':'research-evaluator-v2','spec_sha256':'"
        + digest
        + "'}, open(pathlib.Path(a.out)/'result.json','w'))\n",
        encoding="utf-8",
    )
    return worktree, target, evaluator, commit, eval_spec, digest


def _launch(
    tmp_path: Path, *, sleep: float = 0.0, mutate: bool = False, timeout: float = 5.0
) -> tuple[JobRecord, Path]:
    worktree, target, _evaluator, commit, eval_spec, _digest = _fixture(
        tmp_path, sleep=sleep, mutate=mutate
    )
    eval_spec.chmod(0o444)
    attempt_dir = tmp_path / "H0001-A001"
    (attempt_dir / "run" / "logs").mkdir(parents=True)
    out = attempt_dir / "run" / "targets.json"
    spec = tmp_path / "spec.json"
    panel = tmp_path / "panel.json"
    receipt = tmp_path / "receipt.json"
    dividends = tmp_path / "dividends.json"
    universe = tmp_path / "universe.json"
    spec.write_text("{}", encoding="utf-8")
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    dividends.write_text('{"contract":"trusted-dividends-v2"}', encoding="utf-8")
    universe.write_text('{"contract":"trusted-universe-v2"}', encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION = 'v2'\n")
    (snapshot / "pyproject.toml").write_text("[project]\nname='quantipy'\n")
    (snapshot / "uv.lock").write_text("version = 1\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(str(Path(sys.executable).resolve()), venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n", encoding="utf-8"
    )
    evaluator = venv / "bin" / "quantipy"
    semantic = "a" * 64
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, pathlib, sys\n"
        "p=argparse.ArgumentParser(); p.add_argument('command', nargs='*'); p.add_argument('--out'); p.add_argument('--spec'); p.add_argument('--targets'); p.add_argument('--panel'); p.add_argument('--receipt'); p.add_argument('--dividends'); p.add_argument('--universe'); p.add_argument('--require-source-root'); a=p.parse_args()\n"
        "if 'validate-inputs' in a.command:\n"
        " print(json.dumps({'verdict':'PASS','reasons':[],'spec_sha256_semantic':'"
        + semantic
        + "','spec_sha256_raw':'"
        + hashlib.sha256(eval_spec.read_bytes()).hexdigest()
        + "','panel_sha256':'"
        + hashlib.sha256(panel.read_bytes()).hexdigest()
        + "','receipt_sha256':'"
        + hashlib.sha256(receipt.read_bytes()).hexdigest()
        + "','universe_file_sha256':'"
        + hashlib.sha256(universe.read_bytes()).hexdigest()
        + "','dividends_sha256':'"
        + hashlib.sha256(dividends.read_bytes()).hexdigest()
        + "'})); sys.exit(0)\n"
        "pathlib.Path(a.out).mkdir(parents=True, exist_ok=True); json.dump({'evaluator_version':'research-evaluator-v2','spec_sha256':'"
        + semantic
        + "','dividends_sha256':'"
        + hashlib.sha256(dividends.read_bytes()).hexdigest()
        + "','compliant':True}, open(pathlib.Path(a.out)/'result.json','w'))\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    spec.chmod(0o444)
    panel.chmod(0o444)
    receipt.chmod(0o444)
    dividends.chmod(0o444)
    universe.chmod(0o444)
    worktree.chmod(0o555)
    pins = runtime_pins(snapshot, venv / "bin" / "python", evaluator, universe)
    artifact_paths = {
        "spec": spec,
        "panel": panel,
        "receipt": receipt,
        "evaluation_spec": eval_spec,
        "dividends": dividends,
    }
    artifact_digests = {
        key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in artifact_paths.items()
    }
    targets_argv = (str(pins.shared_python), str(target), "--out", str(out))
    implementation = ImplementationRecord(
        attempt_dir.name,
        commit,
        targets_argv,
        "/tmp/test-evidence.json",
        "fixture-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    plan = RunPlan(
        "research-run-plan-v1",
        attempt_dir.name,
        commit,
        hashlib.sha256(implementation.to_json().encode()).hexdigest(),
        "a" * 64,
        "s000",
        (
            RunScenario(
                "s000", targets_argv, "c000", hashlib.sha256(eval_spec.read_bytes()).hexdigest()
            ),
        ),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        0.05 if timeout < 1 else 1,
        0.05 if timeout < 1 else 1,
    )
    job = launch(
        attempt_dir,
        worktree,
        targets_argv,
        timeout,
        512,
        shared_python=pins.shared_python,
        expected_commit=commit,
        artifact_paths=artifact_paths,
        artifact_digests=artifact_digests,
        evaluator_source=evaluator,
        evaluator_source_sha256=pins.evaluator_sha256,
        configured_pins=pins,
        dividends_path=dividends,
        run_plan=plan,
        evaluation_spec_paths={"c000": eval_spec},
        evaluation_spec_digests={"c000": hashlib.sha256(eval_spec.read_bytes()).hexdigest()},
    )
    return job, worktree


def _wait(job: JobRecord, seconds: float = 10.0) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = attach(job)
        if state != "ATTACHED":
            return state
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_preflight_rejects_dirty_and_head_mismatch(tmp_path: Path) -> None:
    worktree, _target, _evaluator, commit, _eval, _digest = _fixture(tmp_path)
    (worktree / "dirty").write_text("x", encoding="utf-8")
    with pytest.raises(JobError):
        preflight(worktree, commit)
    (worktree / "dirty").unlink()
    with pytest.raises(JobError):
        preflight(worktree, "f" * 40)


@pytest.mark.parametrize("pin", ["snapshot", "universe", "pyvenv", "evaluator", "shared_python"])
def test_launch_rejects_each_configured_pin_mutation_before_stage_spawn(
    tmp_path: Path, pin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, target, _untrusted_evaluator, commit, eval_spec, _digest = _fixture(tmp_path)
    snapshot = tmp_path / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION='v2'\n")
    (snapshot / "pyproject.toml").write_text("[project]\nname='quantipy'\n")
    (snapshot / "uv.lock").write_text("version = 1\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(str(Path(sys.executable).resolve()), venv / "bin" / "python")
    pyvenv = venv / "pyvenv.cfg"
    pyvenv.write_text(f"home = {Path(sys.executable).resolve().parent}\n", encoding="utf-8")
    evaluator = venv / "bin" / "quantipy"
    evaluator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    evaluator.chmod(0o555)
    universe = tmp_path / "universe.json"
    universe.write_text("{}", encoding="utf-8")
    dividends = tmp_path / "dividends.json"
    dividends.write_text("{}", encoding="utf-8")
    pins = runtime_pins(snapshot, venv / "bin" / "python", evaluator, universe)
    artifacts = {
        "spec": tmp_path / "spec.json",
        "panel": tmp_path / "panel.json",
        "receipt": tmp_path / "receipt.json",
        "evaluation_spec": eval_spec,
        "dividends": dividends,
    }
    for path in artifacts.values():
        if not path.exists():
            path.write_text("{}", encoding="utf-8")
    digests = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in artifacts.items()
    }
    if pin == "snapshot":
        (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION='changed'\n")
    elif pin == "universe":
        universe.write_text('{"changed":true}', encoding="utf-8")
    elif pin == "pyvenv":
        pyvenv.write_text(
            f"home = {Path(sys.executable).resolve().parent}\nprompt = changed\n", encoding="utf-8"
        )
    elif pin == "evaluator":
        evaluator.chmod(0o755)
        evaluator.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    else:
        shared_python = venv / "bin" / "python"
        shared_python.unlink()
        shutil.copy2(sys.executable, shared_python)
        shared_python.chmod(0o755)
    real_popen = cast(Callable[..., Any], subprocess.Popen)

    def no_stage_spawn(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, (tuple, list)) and argv and argv[0] == "git":
            return real_popen(argv, *args, **kwargs)
        pytest.fail("pin mutation reached stage worker spawn")

    monkeypatch.setattr(subprocess, "Popen", no_stage_spawn)
    with pytest.raises(JobError, match=r"configured trusted runtime pin changed|distribution"):
        launch(
            tmp_path / "attempt",
            worktree,
            (str(pins.shared_python), str(target)),
            5,
            256,
            shared_python=pins.shared_python,
            expected_commit=commit,
            artifact_paths=artifacts,
            artifact_digests=digests,
            evaluator_source=pins.evaluator,
            evaluator_source_sha256=pins.evaluator_sha256,
            configured_pins=pins,
            dividends_path=dividends,
        )


def test_targets_reject_secret_name_and_wrong_interpreter(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    interpreter.write_text("x", encoding="utf-8")
    interpreter.chmod(0o755)
    with pytest.raises(JobError):
        _validate_targets(
            (str(interpreter), "-m", "api_token_value"), interpreter, tmp_path, tmp_path / "run"
        )
    with pytest.raises(JobError):
        _validate_targets(
            ("/wrong/python", "-m", "module"), interpreter, tmp_path, tmp_path / "run"
        )
    with pytest.raises(JobError):
        _validate_targets(
            (str(interpreter), "-m", "not-a-module"), interpreter, tmp_path, tmp_path / "run"
        )
    with pytest.raises(JobError):
        _validate_targets(
            (str(interpreter), str(tmp_path.parent / "outside.py")),
            interpreter,
            tmp_path,
            tmp_path / "run",
        )
    with pytest.raises(JobError, match="program file is missing"):
        _validate_targets(
            (str(interpreter), str(tmp_path / "missing.py")),
            interpreter,
            tmp_path,
            tmp_path / "run",
        )


def test_contained_worker_timeout_is_terminal(tmp_path: Path) -> None:
    job, _worktree = _launch(tmp_path / "timeout", sleep=10, timeout=0.2)
    assert _wait(job) == "TIMED_OUT"
    terminal = json.loads((Path(job.run_dir) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "timed_out"
    assert terminal["run_evidence_sha256"]
    evidence = json.loads((Path(job.run_dir) / "run-evidence.json").read_text(encoding="utf-8"))
    assert evidence["checks"][0]["name"] == "source_before"


def test_cancel_identity_and_orphan(tmp_path: Path) -> None:
    job, _ = _launch(tmp_path / "cancel", sleep=10)
    assert cancel(job) == "CANCELLED"
    assert attach(job) == "CANCELLED"
    assert not (Path(job.run_dir) / "terminal.json").exists()
    assert (
        json.loads((Path(job.run_dir) / "cancel.json").read_text(encoding="utf-8"))["status"]
        == "cancelled"
    )
    with pytest.raises(JobError, match="stale_job_identity"):
        cancel(
            JobRecord(
                job.job_id, job.attempt_id, job.worker_pid, job.worker_starttime + 1, job.run_dir
            )
        )
    dead = JobRecord("job-dead", "H0001-A001", os.getpid(), 0, str(tmp_path / "missing"))
    assert attach(dead) == "ORPHANED"


def test_cancel_after_worker_terminal_preserves_worker_evidence(tmp_path: Path) -> None:
    job, _ = _launch(tmp_path / "cancel-terminal")
    assert _wait(job) == "EXITED"
    terminal = Path(job.run_dir) / "terminal.json"
    before = terminal.read_bytes()
    assert cancel(job) == "ALREADY_FINISHED"
    assert terminal.read_bytes() == before
    assert not (Path(job.run_dir) / "cancel.json").exists()


def test_cancellation_authority_blocks_late_worker_terminalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = JobRecord("job-interleave", "H0001-A001", 4242, 77, str(run_dir))
    alive = True

    def fake_starttime(_pid: int) -> int | None:
        return 77 if alive else None

    def fake_killpg(_pgid: int, _signal: signal.Signals) -> None:
        nonlocal alive
        alive = False

    def late_worker_terminal(_job: JobRecord) -> None:
        cancel_path = run_dir / "cancel.json"
        assert cancel_path.is_file()
        worker._write_terminal_if_authorized(
            run_dir,
            {"job_id": job.job_id, "status": "targets_failed"},
            {},
        )

    monkeypatch.setattr("gateway.research.jobs._starttime", fake_starttime)
    monkeypatch.setattr("gateway.research.jobs.cleanup_stage", late_worker_terminal)
    monkeypatch.setattr("gateway.research.jobs.os.getsid", lambda _pid: 4242)
    monkeypatch.setattr("gateway.research.jobs.os.killpg", fake_killpg)

    assert cancel(job) == "CANCELLED"
    assert not (run_dir / "terminal.json").exists()
    assert json.loads((run_dir / "cancel.json").read_text())["status"] == "cancelled"


def test_cancel_recovers_exact_cancelling_marker_after_canceller_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = JobRecord("job-recover", "H0001-A001", 4242, 77, str(run_dir))
    (run_dir / "cancel.json").write_text(
        json.dumps(
            {
                "job_id": job.job_id,
                "worker_pid": job.worker_pid,
                "worker_starttime": job.worker_starttime,
                "status": "cancelling",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "active-stage.json").write_text(
        json.dumps(
            {
                "pid": 5252,
                "starttime": 88,
                "pgid": 5252,
                "scope_unit": "research-job-recover-targets",
            }
        ),
        encoding="utf-8",
    )
    stopped: list[str] = []

    def fake_starttime(pid: int) -> int | None:
        return None if pid in {4242, 5252} else 999

    def fake_stop_scope(unit: str) -> bool:
        stopped.append(unit)
        return True

    monkeypatch.setattr("gateway.research.jobs._starttime", fake_starttime)
    monkeypatch.setattr("gateway.research.jobs.stop_scope", fake_stop_scope)
    monkeypatch.setattr(
        "gateway.research.jobs.os.killpg",
        lambda *_args: pytest.fail("recovery must not signal a dead or reused PID"),
    )

    assert cancel(job) == "CANCELLED"
    assert stopped == ["research-job-recover-targets"]
    assert json.loads((run_dir / "cancel.json").read_text(encoding="utf-8"))["status"] == (
        "cancelled"
    )


def test_cancel_rejects_exact_marker_when_worker_pid_was_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = JobRecord("job-reused", "H0001-A001", 4242, 77, str(run_dir))
    (run_dir / "cancel.json").write_text(
        json.dumps(
            {
                "job_id": job.job_id,
                "worker_pid": job.worker_pid,
                "worker_starttime": job.worker_starttime,
                "status": "cancelling",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.research.jobs._starttime", lambda _pid: 99)
    with pytest.raises(JobError, match="stale_job_identity"):
        cancel(job)
    assert json.loads((run_dir / "cancel.json").read_text(encoding="utf-8"))["status"] == (
        "cancelling"
    )


def test_cancel_keeps_cancelling_evidence_when_scope_stop_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = JobRecord("job-scope-failure", "H0001-A001", 4242, 77, str(run_dir))
    (run_dir / "active-stage.json").write_text(
        json.dumps(
            {
                "pid": 5252,
                "starttime": 88,
                "pgid": 5252,
                "scope_unit": "research-job-scope-failure-targets",
            }
        ),
        encoding="utf-8",
    )
    alive = {4242: True, 5252: True}

    def fake_starttime(pid: int) -> int | None:
        if pid == 4242:
            return 77 if alive[pid] else None
        if pid == 5252:
            return 88 if alive[pid] else None
        return None

    def fake_killpg(pgid: int, _signal: object) -> None:
        if pgid in alive:
            alive[pgid] = False

    monkeypatch.setattr("gateway.research.jobs._starttime", fake_starttime)
    monkeypatch.setattr("gateway.research.jobs.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("gateway.research.jobs.os.killpg", fake_killpg)
    monkeypatch.setattr("gateway.research.jobs.stop_scope", lambda _unit: False)

    with pytest.raises(JobError, match="scope termination could not be verified"):
        cancel(job)
    assert json.loads((run_dir / "cancel.json").read_text(encoding="utf-8"))["status"] == (
        "cancelling"
    )
    assert (run_dir / "active-stage.json").is_file()
    assert not (run_dir / "terminal.json").exists()


def test_worker_launch_uses_trusted_package_not_worktree_shadow(
    tmp_path: Path,
) -> None:
    worktree, target, evaluator, commit, eval_spec, _digest = _fixture(
        tmp_path / "shadow", malicious_worker=True
    )
    evaluator.chmod(0o555)
    eval_spec.chmod(0o444)
    worktree.chmod(0o555)
    attempt_dir = tmp_path / "shadow" / "H0001-A001"
    (attempt_dir / "run" / "logs").mkdir(parents=True)
    artifacts = {}
    for name, content in (("spec", "{}"), ("panel", "panel"), ("receipt", "receipt")):
        path = tmp_path / "shadow" / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o444)
        artifacts[name] = path
    artifacts["evaluation_spec"] = eval_spec
    artifacts["dividends"] = tmp_path / "shadow" / "dividends.json"
    artifacts["dividends"].write_text("{}", encoding="utf-8")
    artifacts["dividends"].chmod(0o444)
    universe = tmp_path / "shadow" / "universe.json"
    universe.write_text("{}", encoding="utf-8")
    universe.chmod(0o444)
    snapshot = tmp_path / "shadow" / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION='v2'\n")
    (snapshot / "pyproject.toml").write_text("[project]\nname='quantipy'\n")
    (snapshot / "uv.lock").write_text("version = 1\n")
    venv = tmp_path / "shadow" / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(str(Path(sys.executable).resolve()), venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n", encoding="utf-8"
    )
    trusted_evaluator = venv / "bin" / "quantipy"
    trusted_evaluator.write_bytes(evaluator.read_bytes())
    trusted_evaluator.chmod(0o555)
    pins = runtime_pins(snapshot, venv / "bin" / "python", trusted_evaluator, universe)
    targets_argv = (
        str(pins.shared_python),
        str(target),
        "--out",
        str(attempt_dir / "run" / "targets.json"),
    )
    implementation = ImplementationRecord(
        attempt_dir.name,
        commit,
        targets_argv,
        "/tmp/test-evidence.json",
        "fixture-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    plan = RunPlan(
        "research-run-plan-v1",
        attempt_dir.name,
        commit,
        hashlib.sha256(implementation.to_json().encode()).hexdigest(),
        "a" * 64,
        "s000",
        (
            RunScenario(
                "s000", targets_argv, "c000", hashlib.sha256(eval_spec.read_bytes()).hexdigest()
            ),
        ),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        1,
        1,
    )
    job = launch(
        attempt_dir,
        worktree,
        targets_argv,
        5,
        512,
        shared_python=pins.shared_python,
        expected_commit=commit,
        artifact_paths=artifacts,
        artifact_digests={
            key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in artifacts.items()
        },
        evaluator_source=trusted_evaluator,
        evaluator_source_sha256=pins.evaluator_sha256,
        configured_pins=pins,
        dividends_path=artifacts["dividends"],
        run_plan=plan,
        evaluation_spec_paths={"c000": eval_spec},
        evaluation_spec_digests={"c000": hashlib.sha256(eval_spec.read_bytes()).hexdigest()},
    )
    assert _wait(job) == "EXITED"
    terminal = json.loads((Path(job.run_dir) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["job_id"] == job.job_id
    assert isinstance(terminal["status"], str) and terminal["status"]
    evidence = json.loads((Path(job.run_dir) / "run-evidence.json").read_text(encoding="utf-8"))
    assert evidence["checks"][0]["name"] == "source_before"
    assert not (worktree / "hijacked").exists()


def test_worker_death_does_not_leave_owned_stage_running(tmp_path: Path) -> None:
    job, _worktree = _launch(tmp_path / "worker-death", sleep=10, timeout=0.2)
    terminal_path = Path(job.run_dir) / "terminal.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not terminal_path.is_file():
        time.sleep(0.02)
    assert terminal_path.is_file()
    assert json.loads(terminal_path.read_text(encoding="utf-8"))["status"] == "timed_out"
    assert not cleanup_stage(job)


def test_real_launcher_exit_does_not_kill_detached_worker(tmp_path: Path) -> None:
    # This is an actual process-tree check: a transient launcher starts a
    # child that invokes the trusted worker entrypoint, then exits before the
    # worker's explicitly injected delayed _run completes.  The delay is only
    # a test-local monkeypatch; no production environment or CLI bypass is
    # involved.
    root = tmp_path / "parent-exit"
    root.mkdir()
    marker = root / "finished"
    job_file = root / "job.json"
    job_file.write_text(
        json.dumps({"max_rss_mb": 256, "run_dir": str(root), "job_id": "job-test"}),
        encoding="utf-8",
    )
    launcher = root / "worker-launcher.py"
    launcher.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from gateway.research import worker\n"
        "marker = Path(sys.argv[2])\n"
        "def delayed_run(job):\n"
        "    time.sleep(0.5)\n"
        "    marker.write_text('survived')\n"
        "    return {'status': 'succeeded'}\n"
        "worker._run = delayed_run\n"
        "sys.argv = [sys.argv[0], '--job-file', sys.argv[1]]\n"
        "raise SystemExit(worker.main())\n",
        encoding="utf-8",
    )
    parent = root / "launcher-parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],\n"
        "                 start_new_session=True)\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    completed = subprocess.run(
        [sys.executable, str(parent), str(launcher), str(job_file), str(marker)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.is_file():
        time.sleep(0.05)
    assert marker.read_text(encoding="utf-8") == "survived"
