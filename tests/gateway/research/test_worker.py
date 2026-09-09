from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest
from gateway.research import jobs, worker
from gateway.research.containment import StagePlan

run = worker._run


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
        "artifact_paths": {},
        "artifact_digests": {},
        "timeout_seconds": 1,
        "targets_argv": [],
    }
    run(job)
    terminal = json.loads((run_dir / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "containment_unavailable"


def _job(tmp_path: Path, *, sleep: float = 0.0) -> tuple[dict[str, object], Path, Path, str]:
    # Reuse the disposable git/script fixture from the job tests.
    worktree, target, evaluator, _commit, eval_spec, digest = __import__(
        "tests.gateway.research.test_jobs", fromlist=["_fixture"]
    )._fixture(tmp_path, sleep=sleep)
    from tests.gateway.research.test_containment import _runtime

    pins, _snapshot, trusted_evaluator, universe, venv = _runtime(tmp_path / "runtime")
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    targets = run_dir / "targets.json"
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    spec = tmp_path / "spec"
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    spec.write_text("{}", encoding="utf-8")
    dividends = tmp_path / "dividends"
    dividends.write_text("{}", encoding="utf-8")
    paths = {
        "spec": spec,
        "panel": panel,
        "receipt": receipt,
        "evaluation_spec": eval_spec,
        "dividends": dividends,
    }
    digests = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    job: dict[str, object] = {
        "job_id": "job-worker",
        "run_dir": str(run_dir),
        "worktree": str(worktree),
        "expected_commit": _commit,
        "snapshot_dir": str(pins.snapshot_dir),
        "snapshot_sha256": pins.snapshot_sha256,
        "shared_python": str(pins.shared_python),
        "shared_python_resolved": str(pins.resolved_python),
        "resolved_python": str(pins.resolved_python),
        "shared_python_sha256": pins.shared_python_sha256,
        "pyvenv_cfg": str(pins.pyvenv_cfg),
        "pyvenv_sha256": pins.pyvenv_sha256,
        "distribution_dir": str(pins.distribution_dir),
        "evaluator_source": str(trusted_evaluator),
        "evaluator_sha256": pins.evaluator_sha256,
        "universe": str(universe),
        "universe_sha256": pins.universe_sha256,
        "artifact_paths": {key: str(path) for key, path in paths.items()},
        "artifact_digests": digests,
        "timeout_seconds": 0.2 if sleep else 5,
        "max_rss_mb": 256,
        "targets_argv": [str(venv / "bin" / "python"), str(target), "--out", str(targets)],
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
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_rejects_evaluator_digest_mismatch(tmp_path: Path) -> None:
    job, evaluator, _eval_spec, _digest = _job(tmp_path)
    del evaluator
    job["evaluator_sha256"] = "0" * 64
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_rejects_wrong_evaluator_version(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    job["evaluator_sha256"] = "f" * 64
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_rejects_non_raw_evaluator_spec_digest_until_p3b(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, digest = _job(tmp_path)
    assert digest
    job["artifact_digests"] = {
        **job["artifact_digests"],  # type: ignore[dict-item]
        "evaluation_spec": "a" * 64,
    }
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_timeout_writes_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path, sleep=10)
    monkeypatch.setattr(worker, "_stage", lambda *_args: (0, True))
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "timed_out"


def test_worker_rechecks_frozen_inputs_between_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    raw_paths = job["artifact_paths"]
    assert isinstance(raw_paths, dict)
    panel = Path(str(raw_paths["panel"]))
    targets_value = job["targets_argv"]
    assert isinstance(targets_value, list)
    calls: list[str] = []

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        stage = plan.stage
        calls.append(stage)
        if stage == "validate":
            paths = job["artifact_digests"]
            assert isinstance(paths, dict)
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": "a" * 64,
                        "spec_sha256_raw": paths["evaluation_spec"],
                        "panel_sha256": paths["panel"],
                        "receipt_sha256": paths["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": paths["dividends"],
                    }
                ),
                encoding="utf-8",
            )
            panel.write_text("tampered", encoding="utf-8")
        elif stage == "targets":
            target_output = Path(str(job["run_dir"])) / "targets-stage" / "targets.json"
            target_output.parent.mkdir(parents=True, exist_ok=True)
            target_output.write_text("{}", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"
    assert calls == ["validate"]
    assert (Path(str(job["run_dir"])) / "validated-inputs.json").is_file()
    assert "frozen input changed" in str(terminal.get("error"))


def test_evaluator_output_is_absent_until_evaluator_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    semantic = "a" * 64
    run_dir = Path(str(job["run_dir"]))

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        if plan.stage == "validate":
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": semantic,
                        "spec_sha256_raw": artifact_digests["evaluation_spec"],
                        "panel_sha256": artifact_digests["panel"],
                        "receipt_sha256": artifact_digests["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        elif plan.stage == "targets":
            target = run_dir / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        else:
            output = run_dir / "evaluator-stage" / "out"
            assert not output.exists()
            output.mkdir(parents=True)
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "evaluator_version": "research-evaluator-v2",
                        "spec_sha256": semantic,
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    assert run(job)["status"] == "succeeded"


def test_cancellation_wins_before_worker_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    run_dir = Path(str(job["run_dir"]))
    worker_job = jobs.JobRecord("job-worker", "H0001-A001", 4242, 77, str(run_dir))
    alive = True
    cancelled = False

    def fake_starttime(_pid: int) -> int | None:
        return 77 if alive else None

    def fake_killpg(_pgid: int, _signal: object) -> None:
        nonlocal alive
        alive = False

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        nonlocal cancelled
        if plan.stage == "validate":
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": "a" * 64,
                        "spec_sha256_raw": artifact_digests["evaluation_spec"],
                        "panel_sha256": artifact_digests["panel"],
                        "receipt_sha256": artifact_digests["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
            cancelled = jobs.cancel(worker_job) == "CANCELLED"
        elif plan.stage == "targets":
            target = run_dir / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        else:
            output = run_dir / "evaluator-stage" / "out"
            output.mkdir(parents=True)
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "evaluator_version": "research-evaluator-v2",
                        "spec_sha256": "a" * 64,
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        return 0, False

    monkeypatch.setattr(jobs, "_starttime", fake_starttime)
    monkeypatch.setattr(jobs, "cleanup_stage", lambda _job: False)
    monkeypatch.setattr(os, "getsid", lambda _pid: 4242)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(worker, "_stage", fake_stage)

    assert run(job)["status"] == "cancelled"
    assert cancelled
    assert not (run_dir / "terminal.json").exists()
    assert (
        json.loads((run_dir / "cancel.json").read_text(encoding="utf-8"))["status"] == "cancelled"
    )


@pytest.mark.parametrize("existing_kind", ["directory", "file", "symlink"])
def test_worker_refuses_existing_evaluator_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_kind: str
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path / existing_kind)
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    run_dir = Path(str(job["run_dir"]))
    output = run_dir / "evaluator-stage" / "out"
    output.parent.mkdir(parents=True, exist_ok=True)
    if existing_kind == "directory":
        output.mkdir()
        (output / "sentinel").write_text("keep", encoding="utf-8")
    elif existing_kind == "file":
        output.write_text("keep", encoding="utf-8")
    else:
        output.symlink_to(run_dir / "missing-output")

    calls: list[str] = []

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        calls.append(plan.stage)
        if plan.stage == "validate":
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": "a" * 64,
                        "spec_sha256_raw": artifact_digests["evaluation_spec"],
                        "panel_sha256": artifact_digests["panel"],
                        "receipt_sha256": artifact_digests["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        elif plan.stage == "targets":
            target = run_dir / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    assert run(job)["status"] == "containment_unavailable"
    assert calls == ["validate", "targets"]
    if existing_kind == "directory":
        assert (output / "sentinel").read_text(encoding="utf-8") == "keep"
    elif existing_kind == "file":
        assert output.read_text(encoding="utf-8") == "keep"
    else:
        assert output.is_symlink()


@pytest.mark.parametrize("unsafe_output", ["oversized", "symlink"])
def test_worker_rejects_unsafe_target_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_output: str
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path / unsafe_output)
    calls: list[str] = []
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        calls.append(plan.stage)
        if plan.stage == "validate":
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": "a" * 64,
                        "spec_sha256_raw": artifact_digests["evaluation_spec"],
                        "panel_sha256": artifact_digests["panel"],
                        "receipt_sha256": artifact_digests["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        elif plan.stage == "targets":
            target = Path(str(job["run_dir"])) / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            if unsafe_output == "oversized":
                target.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
            else:
                target.symlink_to(Path(str(job["run_dir"])) / "missing-target.json")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "targets_failed"
    assert calls == ["validate", "targets"]


def test_worker_refuses_writable_runtime_without_injected_boundary(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "containment_unavailable"
    containment_checks = [check for check in terminal["checks"] if check["name"] == "containment"]
    assert containment_checks and containment_checks[0]["ok"] is False
    assert not (Path(str(job["run_dir"])) / "targets.json").exists()


def test_worker_refuses_unpinned_evaluator_even_with_test_boundary(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    job.pop("evaluator_sha256")
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "containment_unavailable"


def test_worker_has_no_public_injected_containment_bypass() -> None:
    assert "containment_check" not in inspect.signature(worker.run).parameters
    assert "containment_check" not in inspect.signature(worker._run).parameters


def test_worker_interrupt_leaves_cancellation_to_cancel_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)

    def interrupted(*_args: object) -> tuple[int, bool]:
        worker._WORKER_INTERRUPTED = True
        raise SystemExit(143)

    monkeypatch.setattr(worker, "_stage", interrupted)
    with pytest.raises(SystemExit, match="143"):
        run(job)
    assert not (Path(str(job["run_dir"])) / "terminal.json").exists()
