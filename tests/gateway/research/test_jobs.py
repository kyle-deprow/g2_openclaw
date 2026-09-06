from __future__ import annotations

# Compact subprocess fixture payloads are intentionally single-line scripts.
# ruff: noqa: E501
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from gateway.research.jobs import (
    JobError,
    JobRecord,
    _starttime,
    _validate_targets,
    attach,
    cancel,
    cleanup_stage,
    launch,
    preflight,
)
from gateway.research.worker import _run as run_worker


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
        "#!/usr/bin/env python3\nimport argparse, json, pathlib\np=argparse.ArgumentParser(); p.add_argument('x',nargs='*'); p.add_argument('--out'); p.add_argument('--spec'); p.add_argument('--targets'); p.add_argument('--panel'); p.add_argument('--receipt'); a=p.parse_args(); pathlib.Path(a.out).mkdir(exist_ok=True); json.dump({'evaluator_version':'research-evaluator-v1','spec_sha256':'"
        + digest
        + "'}, open(pathlib.Path(a.out)/'result.json','w'))\n",
        encoding="utf-8",
    )
    return worktree, target, evaluator, commit, eval_spec, digest


def _launch(
    tmp_path: Path, *, sleep: float = 0.0, mutate: bool = False, timeout: float = 5.0
) -> tuple[JobRecord, Path]:
    worktree, target, evaluator, commit, eval_spec, _digest = _fixture(
        tmp_path, sleep=sleep, mutate=mutate
    )
    evaluator.chmod(0o555)
    eval_spec.chmod(0o444)
    worktree.chmod(0o555)
    attempt_dir = tmp_path / "attempt"
    (attempt_dir / "run" / "logs").mkdir(parents=True)
    out = attempt_dir / "run" / "targets.json"
    spec = tmp_path / "spec.json"
    panel = tmp_path / "panel.json"
    receipt = tmp_path / "receipt.json"
    spec.write_text("{}", encoding="utf-8")
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    spec.chmod(0o444)
    panel.chmod(0o444)
    receipt.chmod(0o444)
    artifact_paths = {
        "spec": spec,
        "panel": panel,
        "receipt": receipt,
        "evaluation_spec": eval_spec,
    }
    artifact_digests = {
        key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in artifact_paths.items()
    }
    job = launch(
        attempt_dir,
        worktree,
        ("/usr/bin/python3", str(target), "--out", str(out)),
        (
            str(evaluator),
            "research",
            "evaluate",
            "--spec",
            str(eval_spec),
            "--out",
            str(attempt_dir / "run" / "out"),
        ),
        timeout,
        512,
        shared_python=Path("/usr/bin/python3"),
        expected_commit=commit,
        artifact_paths=artifact_paths,
        artifact_digests=artifact_digests,
        evaluator_source=evaluator,
        evaluator_source_sha256=hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        evaluator_implementation_pinned=True,
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


def test_timeout_is_terminal(tmp_path: Path) -> None:
    job, _worktree = _launch(tmp_path / "timeout", sleep=10, timeout=0.2)
    assert _wait(job) == "TIMED_OUT"
    terminal = json.loads((Path(job.run_dir) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "timed_out"
    assert terminal["source_writable"] is False
    assert terminal["shared_env_writable"] is False


def test_worker_detects_source_mutation_with_injected_fixture_boundary(tmp_path: Path) -> None:
    worktree, target, evaluator, _commit, eval_spec, _digest = _fixture(
        tmp_path / "mutate", mutate=True
    )
    worktree.chmod(0o755)
    run_dir = tmp_path / "mutate" / "direct-run"
    (run_dir / "logs").mkdir(parents=True)
    panel = tmp_path / "mutate" / "panel"
    panel.write_text("panel", encoding="utf-8")
    receipt = tmp_path / "mutate" / "receipt"
    receipt.write_text("receipt", encoding="utf-8")
    spec = tmp_path / "mutate" / "spec"
    spec.write_text("{}", encoding="utf-8")
    paths = {"spec": spec, "panel": panel, "receipt": receipt, "evaluation_spec": eval_spec}
    digests = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    job = {
        "job_id": "job-source-mutation",
        "run_dir": str(run_dir),
        "worktree": str(worktree),
        "shared_python": "/usr/bin/python3",
        "shared_python_sha256": hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest(),
        "evaluator_source": str(evaluator),
        "evaluator_source_sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        "evaluator_implementation_pinned": True,
        "artifact_paths": {key: str(path) for key, path in paths.items()},
        "artifact_digests": digests,
        "timeout_seconds": 5,
        "targets_argv": [
            "/usr/bin/python3",
            str(target),
            "--out",
            str(run_dir / "targets.json"),
        ],
        "evaluator_argv": [str(evaluator), "--out", str(run_dir / "out")],
    }
    run_worker(job, containment_check=lambda *_args: True)
    terminal = json.loads((run_dir / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "source_mutated"


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
    assert cancel(job) == "CANCELLED"
    assert terminal.read_bytes() == before
    assert not (Path(job.run_dir) / "cancel.json").exists()


def test_worker_launch_uses_trusted_package_not_worktree_shadow(
    tmp_path: Path,
) -> None:
    worktree, target, evaluator, commit, eval_spec, _digest = _fixture(
        tmp_path / "shadow", malicious_worker=True
    )
    evaluator.chmod(0o555)
    eval_spec.chmod(0o444)
    worktree.chmod(0o555)
    attempt_dir = tmp_path / "shadow" / "attempt"
    (attempt_dir / "run" / "logs").mkdir(parents=True)
    artifacts = {}
    for name, content in (("spec", "{}"), ("panel", "panel"), ("receipt", "receipt")):
        path = tmp_path / "shadow" / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o444)
        artifacts[name] = path
    artifacts["evaluation_spec"] = eval_spec
    job = launch(
        attempt_dir,
        worktree,
        ("/usr/bin/python3", str(target), "--out", str(attempt_dir / "run" / "targets.json")),
        (str(evaluator), "--out", str(attempt_dir / "run" / "out")),
        5,
        512,
        shared_python=Path("/usr/bin/python3"),
        expected_commit=commit,
        artifact_paths=artifacts,
        artifact_digests={
            key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in artifacts.items()
        },
        evaluator_source=evaluator,
        evaluator_source_sha256=hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        evaluator_implementation_pinned=True,
    )
    assert _wait(job) == "EXITED"
    terminal = json.loads((Path(job.run_dir) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "succeeded"
    assert not (worktree / "hijacked").exists()


def test_worker_death_does_not_leave_owned_stage_running(tmp_path: Path) -> None:
    job, _worktree = _launch(tmp_path / "worker-death", sleep=10, timeout=30)
    active_path = Path(job.run_dir) / "active-stage.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not active_path.is_file():
        time.sleep(0.02)
    assert active_path.is_file()
    active = json.loads(active_path.read_text(encoding="utf-8"))
    stage_pid = int(active["pid"])
    os.kill(job.worker_pid, 9)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and attach(job) == "ATTACHED":
        time.sleep(0.02)
    assert attach(job) == "ORPHANED"
    assert cleanup_stage(job)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _starttime(stage_pid) is not None:
        time.sleep(0.02)
    assert _starttime(stage_pid) is None


def test_real_launcher_exit_does_not_kill_detached_worker(tmp_path: Path) -> None:
    worktree, target, evaluator, commit, eval_spec, _digest = _fixture(
        tmp_path / "parent-exit", sleep=0.5
    )
    evaluator.chmod(0o555)
    eval_spec.chmod(0o444)
    worktree.chmod(0o555)
    attempt_dir = tmp_path / "parent-exit" / "attempt"
    artifacts: dict[str, Path] = {}
    for name, content in (("spec", "{}"), ("panel", "panel"), ("receipt", "receipt")):
        path = tmp_path / "parent-exit" / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o444)
        artifacts[name] = path
    artifacts["evaluation_spec"] = eval_spec
    config = {
        "attempt_dir": str(attempt_dir),
        "worktree": str(worktree),
        "target": str(target),
        "evaluator": str(evaluator),
        "commit": commit,
        "eval_spec": str(eval_spec),
        "artifacts": {key: str(value) for key, value in artifacts.items()},
        "digests": {
            key: hashlib.sha256(value.read_bytes()).hexdigest() for key, value in artifacts.items()
        },
        "evaluator_digest": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        "job_out": str(tmp_path / "parent-exit" / "job.json"),
    }
    config_path = tmp_path / "parent-exit" / "launch-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    launcher = tmp_path / "parent-exit" / "launcher.py"
    launcher.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from gateway.research.jobs import launch\n"
        "cfg = json.loads(Path(sys.argv[1]).read_text())\n"
        "paths = {key: Path(value) for key, value in cfg['artifacts'].items()}\n"
        "job = launch(Path(cfg['attempt_dir']), Path(cfg['worktree']),\n"
        "    ['/usr/bin/python3', cfg['target'], '--out',\n"
        "     str(Path(cfg['attempt_dir']) / 'run' / 'targets.json')],\n"
        "    [cfg['evaluator'], '--out', str(Path(cfg['attempt_dir']) / 'run' / 'out')],\n"
        "    5, 512, shared_python=Path('/usr/bin/python3'), expected_commit=cfg['commit'],\n"
        "    artifact_paths=paths, artifact_digests=cfg['digests'],\n"
        "    evaluator_source=Path(cfg['evaluator']),\n"
        "    evaluator_source_sha256=cfg['evaluator_digest'],\n"
        "    evaluator_implementation_pinned=True)\n"
        "Path(cfg['job_out']).write_text(job.to_json())\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    completed = subprocess.run(
        [sys.executable, str(launcher), str(config_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    job_data = json.loads(Path(str(config["job_out"])).read_text(encoding="utf-8"))
    job = JobRecord(
        str(job_data["job_id"]),
        str(job_data["attempt_id"]),
        int(job_data["worker_pid"]),
        int(job_data["worker_starttime"]),
        str(job_data["run_dir"]),
    )
    deadline = time.monotonic() + 10
    terminal_path = Path(job.run_dir) / "terminal.json"
    while time.monotonic() < deadline and not terminal_path.is_file():
        time.sleep(0.05)
    assert terminal_path.is_file()
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    assert terminal["status"] == "succeeded"
