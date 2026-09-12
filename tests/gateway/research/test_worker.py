from __future__ import annotations

import hashlib
import inspect
import json
import os
import socket
import time
from pathlib import Path
from typing import cast

import pytest
from gateway.research import jobs, worker
from gateway.research.containment import StagePlan
from gateway.research.contracts import AnalysisPlan, RunPlan, RunScenario

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
    assert terminal["status"] == "run_plan_mismatch"


def _job(tmp_path: Path, *, sleep: float = 0.0) -> tuple[dict[str, object], Path, Path, str]:
    # Reuse the disposable git/script fixture from the job tests.
    worktree, target, evaluator, _commit, eval_spec, digest = __import__(
        "tests.gateway.research.test_jobs", fromlist=["_fixture"]
    )._fixture(tmp_path, sleep=sleep)
    from tests.gateway.research.test_containment import _runtime

    pins, _snapshot, trusted_evaluator, universe, _venv = _runtime(tmp_path / "runtime")
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    targets = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
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
    attempt_id = "H0001-A001"
    targets_argv = (str(pins.shared_python), str(target), "--out", str(targets))
    plan = RunPlan(
        "research-run-plan-v1",
        attempt_id,
        _commit,
        "a" * 64,
        "b" * 64,
        "s000",
        (RunScenario("s000", targets_argv, "c000", digest),),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        0.05 if sleep else 1,
        0.05 if sleep else 1,
    )
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
        "targets_argv": list(targets_argv),
        "run_plan": json.loads(plan.to_json()),
        "run_plan_sha256": hashlib.sha256(plan.to_json().encode()).hexdigest(),
        "evaluation_spec_paths": {"c000": str(eval_spec)},
        "evaluation_spec_digests": {"c000": digest},
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
        if stage.startswith("validate"):
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
        elif stage.startswith("targets"):
            target_output = (
                Path(str(job["run_dir"])) / "scenarios" / "s000" / "targets-stage" / "targets.json"
            )
            target_output.parent.mkdir(parents=True, exist_ok=True)
            target_output.write_text("{}", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"
    assert calls == ["validate-c000"]
    assert not (Path(str(job["run_dir"])) / "validated-inputs.json").exists()
    evidence = json.loads(
        (Path(str(job["run_dir"])) / "run-evidence.json").read_text(encoding="utf-8")
    )
    assert "frozen input changed" in str(evidence.get("error"))


def test_worker_executes_two_distinct_cost_scenarios_and_records_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, eval_spec, _digest = _job(tmp_path)
    run_dir = Path(str(job["run_dir"]))
    second_spec = tmp_path / "eval-spec-second.json"
    second_spec.write_text('{"cost":3}', encoding="utf-8")
    second_digest = hashlib.sha256(second_spec.read_bytes()).hexdigest()
    first_argv = tuple(cast(list[str], job["targets_argv"]))
    second_argv = (
        first_argv[0],
        first_argv[1],
        "--out",
        str(run_dir / "scenarios" / "s001" / "targets-stage" / "targets.json"),
    )
    first_digest = hashlib.sha256(eval_spec.read_bytes()).hexdigest()
    plan = RunPlan(
        "research-run-plan-v1",
        "H0001-A001",
        str(job["expected_commit"]),
        "a" * 64,
        "b" * 64,
        "s000",
        (
            RunScenario("s000", first_argv, "c000", first_digest),
            RunScenario("s001", second_argv, "c001", second_digest),
        ),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        1,
        1,
    )
    job["run_plan"] = json.loads(plan.to_json())
    job["run_plan_sha256"] = hashlib.sha256(plan.to_json().encode()).hexdigest()
    job["evaluation_spec_paths"] = {"c000": str(eval_spec), "c001": str(second_spec)}
    job["evaluation_spec_digests"] = {"c000": first_digest, "c001": second_digest}
    deadlines: list[float] = []

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, deadline: float) -> tuple[int, bool]:
        deadlines.append(deadline)
        assert deadline <= time.monotonic() + 1.1
        if plan.stage.startswith("validate"):
            spec_id = "c001" if plan.stage.endswith("c001") else "c000"
            spec_digest = second_digest if spec_id == "c001" else first_digest
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": spec_id,
                        "spec_sha256_raw": spec_digest,
                        "panel_sha256": job["artifact_digests"]["panel"],  # type: ignore[index]
                        "receipt_sha256": job["artifact_digests"]["receipt"],  # type: ignore[index]
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": job["artifact_digests"]["dividends"],  # type: ignore[index]
                    }
                ),
                encoding="utf-8",
            )
        elif plan.stage.startswith("targets"):
            scenario_id = plan.stage.removeprefix("targets-")
            target = run_dir / "scenarios" / scenario_id / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif plan.stage.startswith("evaluate"):
            scenario_id = plan.stage.removeprefix("evaluate-")
            spec_id = "c001" if scenario_id == "s001" else "c000"
            output = run_dir / "scenarios" / scenario_id / "evaluator-stage" / "out"
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "evaluator_version": "research-evaluator-v2",
                        "spec_sha256": spec_id,
                        "dividends_sha256": job["artifact_digests"]["dividends"],  # type: ignore[index]
                    }
                ),
                encoding="utf-8",
            )
            (output / "trades.parquet").write_bytes(b"trades")
            (output / "daily.parquet").write_bytes(b"daily")
        else:
            output = run_dir / "analysis-stage" / "analysis" / "result.json"
            output.write_text("{}", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    outcome = run(job)
    assert outcome["status"] == "succeeded"
    assert outcome["completed_scenarios"] == ["s000", "s001"]
    scenarios = outcome["scenarios"]
    assert isinstance(scenarios, dict)
    assert set(scenarios) == {"s000", "s001"}
    assert len(deadlines) == 7


def test_worker_analysis_mount_uses_primary_scenario_spec_when_not_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, first_spec, _digest = _job(tmp_path)
    run_dir = Path(str(job["run_dir"]))
    second_spec = tmp_path / "eval-spec-primary.json"
    second_spec.write_text('{"cost":3}', encoding="utf-8")
    first_digest = hashlib.sha256(first_spec.read_bytes()).hexdigest()
    second_digest = hashlib.sha256(second_spec.read_bytes()).hexdigest()
    first_argv = tuple(cast(list[str], job["targets_argv"]))
    second_argv = (
        first_argv[0],
        first_argv[1],
        "--out",
        str(run_dir / "scenarios" / "s001" / "targets-stage" / "targets.json"),
    )
    plan = RunPlan(
        "research-run-plan-v1",
        "H0001-A001",
        str(job["expected_commit"]),
        "a" * 64,
        "b" * 64,
        "s001",
        (
            RunScenario("s000", first_argv, "c000", first_digest),
            RunScenario("s001", second_argv, "c001", second_digest),
        ),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        1,
        1,
    )
    job["targets_argv"] = list(second_argv)
    job["run_plan"] = json.loads(plan.to_json())
    job["run_plan_sha256"] = hashlib.sha256(plan.to_json().encode()).hexdigest()
    job["evaluation_spec_paths"] = {"c000": str(first_spec), "c001": str(second_spec)}
    job["evaluation_spec_digests"] = {"c000": first_digest, "c001": second_digest}
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    analysis_argv: list[str] = []

    def fake_stage(stage: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        if stage.stage.startswith("validate"):
            spec_id = "c001" if stage.stage.endswith("c001") else "c000"
            spec_digest = second_digest if spec_id == "c001" else first_digest
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": spec_id,
                        "spec_sha256_raw": spec_digest,
                        "panel_sha256": artifact_digests["panel"],
                        "receipt_sha256": artifact_digests["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        elif stage.stage.startswith("targets"):
            scenario_id = stage.stage.removeprefix("targets-")
            target = run_dir / "scenarios" / scenario_id / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif stage.stage.startswith("evaluate"):
            scenario_id = stage.stage.removeprefix("evaluate-")
            spec_id = "c001" if scenario_id == "s001" else "c000"
            output = run_dir / "scenarios" / scenario_id / "evaluator-stage" / "out"
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "evaluator_version": "research-evaluator-v2",
                        "spec_sha256": spec_id,
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
            (output / "trades.parquet").write_bytes(b"trades")
            (output / "daily.parquet").write_bytes(b"daily")
        else:
            analysis_argv.extend(stage.argv)
            output = run_dir / "analysis-stage" / "analysis"
            (output / "result.json").write_text("{}", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    outcome = run(job)

    assert outcome["status"] == "succeeded"
    triples = [tuple(analysis_argv[index : index + 3]) for index in range(len(analysis_argv) - 2)]
    assert ("--ro-bind", str(second_spec), "/inputs/spec.json") in triples
    assert ("--ro-bind", str(first_spec), "/inputs/spec.json") not in triples


def test_worker_partial_scenario_failure_records_restart_safe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, first_spec, _digest = _job(tmp_path)
    run_dir = Path(str(job["run_dir"]))
    second_spec = tmp_path / "eval-spec-second.json"
    second_spec.write_text('{"cost":3}', encoding="utf-8")
    first_digest = hashlib.sha256(first_spec.read_bytes()).hexdigest()
    second_digest = hashlib.sha256(second_spec.read_bytes()).hexdigest()
    first_argv = tuple(cast(list[str], job["targets_argv"]))
    second_argv = (
        first_argv[0],
        first_argv[1],
        "--out",
        str(run_dir / "scenarios" / "s001" / "targets-stage" / "targets.json"),
    )
    plan = RunPlan(
        "research-run-plan-v1",
        "H0001-A001",
        str(job["expected_commit"]),
        "a" * 64,
        "b" * 64,
        "s000",
        (
            RunScenario("s000", first_argv, "c000", first_digest),
            RunScenario("s001", second_argv, "c001", second_digest),
        ),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        1,
        1,
    )
    job["run_plan"] = json.loads(plan.to_json())
    job["run_plan_sha256"] = hashlib.sha256(plan.to_json().encode()).hexdigest()
    job["evaluation_spec_paths"] = {"c000": str(first_spec), "c001": str(second_spec)}
    job["evaluation_spec_digests"] = {"c000": first_digest, "c001": second_digest}
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    stages: list[str] = []

    def fake_stage(stage: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        stages.append(stage.stage)
        if stage.stage.startswith("validate"):
            spec_id = "c001" if stage.stage.endswith("c001") else "c000"
            spec_digest = second_digest if spec_id == "c001" else first_digest
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "spec_sha256_semantic": spec_id,
                        "spec_sha256_raw": spec_digest,
                        "panel_sha256": artifact_digests["panel"],
                        "receipt_sha256": artifact_digests["receipt"],
                        "universe_file_sha256": job["universe_sha256"],
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
        elif stage.stage == "targets-s000":
            target = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif stage.stage == "targets-s001":
            return 1, False
        elif stage.stage == "evaluate-s000":
            output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out"
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "evaluator_version": "research-evaluator-v2",
                        "spec_sha256": "c000",
                        "dividends_sha256": artifact_digests["dividends"],
                    }
                ),
                encoding="utf-8",
            )
            (output / "trades.parquet").write_bytes(b"trades")
            (output / "daily.parquet").write_bytes(b"daily")
        else:
            pytest.fail("partial scenario failure must not enter analysis")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    outcome = run(job)

    assert outcome["status"] == "scenario_failed"
    assert stages == [
        "validate-c000",
        "validate-c001",
        "targets-s000",
        "evaluate-s000",
        "targets-s001",
    ]
    assert outcome["completed_scenarios"] == ["s000"]
    assert not (run_dir / "analysis-stage").exists()
    evidence = json.loads((run_dir / "run-evidence.json").read_text(encoding="utf-8"))
    assert evidence["completed_scenarios"] == ["s000"]
    assert evidence["scenarios"]["s001"]["status"] == "failed"


@pytest.mark.parametrize(
    "analysis_mode",
    ["extra", "missing", "oversized", "symlink", "fifo", "socket", "emptydir"],
)
def test_worker_rejects_invalid_analysis_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, analysis_mode: str
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    run_dir = Path(str(job["run_dir"]))
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    socket_handle: socket.socket | None = None

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        if plan.stage.startswith("validate"):
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
        elif plan.stage.startswith("targets"):
            target = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif plan.stage.startswith("evaluate"):
            output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out"
            output.mkdir(parents=True, exist_ok=True)
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
            (output / "trades.parquet").write_bytes(b"trades")
            (output / "daily.parquet").write_bytes(b"daily")
        else:
            output = run_dir / "analysis-stage" / "analysis"
            if analysis_mode != "missing":
                output_file = output / "result.json"
                output_file.write_bytes(b"x" * (1025 if analysis_mode == "oversized" else 2))
            if analysis_mode == "extra":
                (run_dir / "analysis-stage" / "outside.json").write_text(
                    "unexpected", encoding="utf-8"
                )
            elif analysis_mode == "symlink":
                target = run_dir / "analysis-target.txt"
                target.write_text("outside", encoding="utf-8")
                (output / "unsafe-link").symlink_to(target)
            elif analysis_mode == "fifo":
                os.mkfifo(output / "unsafe.fifo")
            elif analysis_mode == "socket":
                nonlocal socket_handle
                socket_handle = socket.socket(socket.AF_UNIX)
                socket_handle.bind(str(output / "unsafe.sock"))
            elif analysis_mode == "emptydir":
                (output / "empty").mkdir()
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    try:
        outcome = run(job)
    finally:
        if socket_handle is not None:
            socket_handle.close()
    assert outcome["status"] == "analysis_output_invalid"
    assert (run_dir / "terminal.json").is_file()
    evidence = json.loads((run_dir / "run-evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "analysis_output_invalid"
    assert evidence["completed_scenarios"] == ["s000"]


def test_worker_rechecks_frozen_inputs_after_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    run_dir = Path(str(job["run_dir"]))
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    artifact_paths = job["artifact_paths"]
    assert isinstance(artifact_paths, dict)
    panel = Path(str(artifact_paths["panel"]))

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        if plan.stage.startswith("validate"):
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
        elif plan.stage.startswith("targets"):
            target = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif plan.stage.startswith("evaluate"):
            output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out"
            output.mkdir(parents=True, exist_ok=True)
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
            (output / "trades.parquet").write_bytes(b"trades")
            (output / "daily.parquet").write_bytes(b"daily")
        else:
            output = run_dir / "analysis-stage" / "analysis"
            (output / "result.json").write_text("{}", encoding="utf-8")
            panel.write_text("mutated after analysis", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    outcome = run(job)

    assert outcome["status"] == "input_digest_mismatch"
    terminal = json.loads((run_dir / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "input_digest_mismatch"


def test_worker_rejects_stage_timeout_aggregate_above_job_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    plan = RunPlan.from_json(json.dumps(job["run_plan"]))
    oversized = RunPlan(
        plan.contract,
        plan.attempt_id,
        plan.commit,
        plan.implementation_sha256,
        plan.evaluation_spec_set_sha256,
        plan.primary_scenario_id,
        plan.scenarios,
        plan.analysis,
        4,
        2,
    )
    job["run_plan"] = json.loads(oversized.to_json())
    job["run_plan_sha256"] = hashlib.sha256(oversized.to_json().encode()).hexdigest()
    job["timeout_seconds"] = 5
    monkeypatch.setattr(worker, "_stage", lambda *_args: pytest.fail("aggregate must refuse"))

    outcome = run(job)

    assert outcome["status"] == "run_plan_mismatch"
    assert json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text())["status"] == (
        "run_plan_mismatch"
    )


def test_evaluator_output_is_absent_until_evaluator_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    artifact_digests = job["artifact_digests"]
    assert isinstance(artifact_digests, dict)
    semantic = "a" * 64
    run_dir = Path(str(job["run_dir"]))

    def fake_stage(plan: StagePlan, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        if plan.stage.startswith("validate"):
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
        elif plan.stage.startswith("targets"):
            target = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif plan.stage.startswith("evaluate"):
            output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out"
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
            (output / "trades.parquet").write_bytes(b"trades")
            (output / "daily.parquet").write_bytes(b"daily")
        else:
            output = run_dir / "analysis-stage" / "analysis"
            (output / "result.json").write_text("{}", encoding="utf-8")
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
        if plan.stage.startswith("validate"):
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
        elif plan.stage.startswith("targets"):
            target = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        elif plan.stage.startswith("evaluate"):
            output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out"
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
    output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out"
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
        if plan.stage.startswith("validate"):
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
        elif plan.stage.startswith("targets"):
            target = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    assert run(job)["status"] == "containment_unavailable"
    assert calls == ["validate-c000"]
    if existing_kind == "directory":
        assert (output / "sentinel").read_text(encoding="utf-8") == "keep"
    elif existing_kind == "file":
        assert output.read_text(encoding="utf-8") == "keep"
    else:
        assert output.is_symlink()
    assert not (run_dir / "analysis-stage").exists()


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
        if plan.stage.startswith("validate"):
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
        elif plan.stage.startswith("targets"):
            target = (
                Path(str(job["run_dir"])) / "scenarios" / "s000" / "targets-stage" / "targets.json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if unsafe_output == "oversized":
                target.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
            else:
                target.symlink_to(Path(str(job["run_dir"])) / "missing-target.json")
        return 0, False

    monkeypatch.setattr(worker, "_stage", fake_stage)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "scenario_failed"
    assert calls == ["validate-c000", "targets-s000"]
    evidence = json.loads(
        (Path(str(job["run_dir"])) / "run-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["scenarios"]["s000"]["status"] == "failed"
    assert not (Path(str(job["run_dir"])) / "analysis-stage").exists()


def test_worker_refuses_writable_runtime_without_injected_boundary(tmp_path: Path) -> None:
    job, _evaluator, _eval_spec, _digest = _job(tmp_path)
    run(job)
    terminal = json.loads((Path(str(job["run_dir"])) / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "containment_unavailable"
    evidence = json.loads(
        (Path(str(job["run_dir"])) / "run-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["checks"]
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
