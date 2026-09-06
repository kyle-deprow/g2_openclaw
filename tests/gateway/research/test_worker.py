from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from gateway.research.worker import _run as run


def test_worker_always_writes_terminal_on_digest_mismatch(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    executable = tmp_path / "python"
    executable.write_text("bad", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "run_dir": str(run_dir),
        "worktree": str(tmp_path),
        "shared_python": str(executable),
        "shared_python_sha256": "0" * 64,
        "evaluator_source": str(executable),
        "evaluator_source_sha256": "0" * 64,
        "evaluator_implementation_pinned": True,
        "artifact_paths": {},
        "artifact_digests": {},
        "timeout_seconds": 1,
        "targets_argv": [],
        "evaluator_argv": [],
    }
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((run_dir / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def _job(tmp_path: Path, *, sleep: float = 0.0) -> tuple[dict[str, object], Path, Path, str]:
    # Reuse the disposable git/script fixture from the job tests.
    worktree, target, evaluator, _commit, eval_spec, digest = __import__(
        "tests.gateway.research.test_jobs", fromlist=["_fixture"]
    )._fixture(tmp_path, sleep=sleep)
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    targets = run_dir / "targets.json"
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    spec = tmp_path / "spec"
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    spec.write_text("{}", encoding="utf-8")
    paths = {"spec": spec, "panel": panel, "receipt": receipt, "evaluation_spec": eval_spec}
    digests = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    job: dict[str, object] = {
        "job_id": "job-worker",
        "run_dir": str(run_dir),
        "worktree": str(worktree),
        "shared_python": sys.executable,
        "shared_python_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "evaluator_source": str(evaluator),
        "evaluator_source_sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        "evaluator_implementation_pinned": True,
        "artifact_paths": {key: str(path) for key, path in paths.items()},
        "artifact_digests": digests,
        "timeout_seconds": 0.2 if sleep else 5,
        "targets_argv": [sys.executable, str(target), "--out", str(targets)],
        "evaluator_argv": [
            str(evaluator),
            "research",
            "evaluate",
            "--spec",
            str(eval_spec),
            "--out",
            str(run_dir / "out"),
        ],
    }
    return job, evaluator, eval_spec, digest


@pytest.mark.parametrize("artifact", ["panel", "receipt", "evaluation_spec"])
def test_worker_rejects_each_frozen_artifact_digest(tmp_path: Path, artifact: str) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path / artifact)
    raw_paths = job["artifact_paths"]
    assert isinstance(raw_paths, dict)
    path = Path(str(raw_paths[artifact]))
    path.chmod(0o644)
    path.write_text("tampered", encoding="utf-8")
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_rejects_evaluator_digest_mismatch(tmp_path: Path) -> None:
    job, evaluator, _eval_spec, _digest = _job(tmp_path)
    evaluator.chmod(0o755)
    job["evaluator_source_sha256"] = "0" * 64
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_rejects_wrong_evaluator_version(tmp_path: Path) -> None:
    job, evaluator, _eval_spec, _digest = _job(tmp_path)
    evaluator.chmod(0o755)
    evaluator.write_text(
        evaluator.read_text(encoding="utf-8").replace("research-evaluator-v1", "wrong"),
        encoding="utf-8",
    )
    job["evaluator_source_sha256"] = hashlib.sha256(evaluator.read_bytes()).hexdigest()
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "evaluator_failed"


def test_worker_rejects_non_raw_evaluator_spec_digest_until_p3b(tmp_path: Path) -> None:
    job, evaluator, _eval_spec, digest = _job(tmp_path)
    evaluator.write_text(
        evaluator.read_text(encoding="utf-8").replace(digest, "a" * 64), encoding="utf-8"
    )
    job["evaluator_source_sha256"] = hashlib.sha256(evaluator.read_bytes()).hexdigest()
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "evaluator_failed"


def test_worker_timeout_writes_terminal(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path, sleep=10)
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "timed_out"


def test_worker_rechecks_frozen_inputs_between_stages(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    raw_paths = job["artifact_paths"]
    assert isinstance(raw_paths, dict)
    panel = Path(str(raw_paths["panel"]))
    targets_value = job["targets_argv"]
    assert isinstance(targets_value, list)
    target = Path(str(targets_value[1]))
    target.write_text(
        "import pathlib\n"
        f"pathlib.Path({str(panel)!r}).write_text('tampered')\n"
        f"pathlib.Path({str(job['run_dir'])!r}, 'targets.json').write_text('{{}}')\n",
        encoding="utf-8",
    )
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"
    assert any(
        check["name"] == "inputs_between" and not check["ok"] for check in terminal["checks"]
    )


def test_worker_refuses_writable_runtime_without_injected_boundary(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "containment_unavailable"
    assert any(check["name"] == "containment" and not check["ok"] for check in terminal["checks"])
    assert not (Path(str(job["run_dir"])) / "targets.json").exists()


def test_worker_refuses_unpinned_evaluator_even_with_test_boundary(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    job.pop("evaluator_implementation_pinned")
    run(job, containment_check=lambda *_args: True)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "evaluator_failed"
    assert any(
        check["name"] == "evaluator_implementation_pin" and not check["ok"]
        for check in terminal["checks"]
    )
