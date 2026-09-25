from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from gateway.research import worker
from gateway.research.containment import (
    PROVENANCE_RECORDER_SOURCE,
    ContainmentError,
    RuntimePins,
    bwrap_argv,
    host_bus_environment,
    require_regular,
    rewrite_targets_argv,
    runtime_pins,
    scope_argv,
    snapshot_sha256,
    stage_plan,
    stop_scope,
    target_file_ok,
    validate_targets_argv,
    verify_runtime_pins,
)
from gateway.research.contracts import AnalysisPlan, RunPlan, RunScenario


def _runtime(tmp_path: Path) -> tuple[RuntimePins, Path, Path, Path, Path]:
    snapshot = tmp_path / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION = 'v2'\n")
    (snapshot / "pyproject.toml").write_text("[project]\nname='quantipy'\n")
    (snapshot / "uv.lock").write_text("version = 1\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(str(Path(sys.executable).resolve()), venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).resolve().parent}\n")
    evaluator = venv / "bin" / "quantipy"
    evaluator.write_text("#!/bin/sh\nexit 0\n")
    evaluator.chmod(0o555)
    universe = tmp_path / "universe.json"
    universe.write_text('{"contract":"trusted-universe-v2"}\n')
    pins = runtime_pins(snapshot, venv / "bin" / "python", evaluator, universe)
    return pins, snapshot, evaluator, universe, venv


def test_snapshot_digest_is_deterministic_and_binds_types_and_bytes(tmp_path: Path) -> None:
    pins, snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    assert snapshot_sha256(snapshot) == pins.snapshot_sha256
    (snapshot / "src" / "quantipy" / "changed.py").write_text("changed\n")
    assert snapshot_sha256(snapshot) != pins.snapshot_sha256


def test_snapshot_digest_binds_hidden_files(tmp_path: Path) -> None:
    _pins, snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    before = snapshot_sha256(snapshot)
    (snapshot / "src" / "quantipy" / ".hidden-config").write_text("changed\n")
    assert snapshot_sha256(snapshot) != before


def test_snapshot_rejects_symlink_and_special_entries(tmp_path: Path) -> None:
    _pins, snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    os.symlink(snapshot / "pyproject.toml", snapshot / "src" / "quantipy" / "link")
    with pytest.raises(ContainmentError, match="symlinks"):
        snapshot_sha256(snapshot)
    (snapshot / "src" / "quantipy" / "link").unlink()
    fifo = snapshot / "src" / "quantipy" / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ContainmentError, match="regular or directory"):
        snapshot_sha256(snapshot)


def test_runtime_pins_preserve_configured_venv_and_reject_mutation(tmp_path: Path) -> None:
    pins, _snapshot, evaluator, _universe, venv = _runtime(tmp_path)
    assert pins.shared_python == venv / "bin" / "python"
    assert pins.resolved_python == Path(sys.executable).resolve()
    evaluator.chmod(0o755)
    evaluator.write_text("#!/bin/sh\nchanged\n")
    with pytest.raises(ContainmentError, match="changed"):
        verify_runtime_pins(pins)


def test_require_regular_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ContainmentError, match="symlinks"):
        require_regular(link, "fixture")


def test_bwrap_validate_has_only_readonly_inputs_and_no_worktree(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    spec = tmp_path / "spec"
    dividends = tmp_path / "dividends"
    provenance = tmp_path / "provenance"
    for path in (panel, receipt, spec, dividends):
        path.write_text("{}")
    provenance.mkdir()
    command = (str(pins.shared_python), "-P", "-s", str(pins.evaluator), "research")
    argv = bwrap_argv(
        pins,
        "validate",
        command,
        panel=panel,
        receipt=receipt,
        spec=spec,
        dividends=dividends,
    )
    assert argv[:7] == (
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
    )
    assert "--bind" not in argv
    assert "/work" not in argv
    assert ("--ro-bind", str(PROVENANCE_RECORDER_SOURCE), "/provenance") == tuple(
        argv[
            argv.index(str(PROVENANCE_RECORDER_SOURCE)) - 1 : argv.index(
                str(PROVENANCE_RECORDER_SOURCE)
            )
            + 2
        ]
    )
    assert "/provenance:/snapshot/src" in argv
    panel_index = argv.index(str(panel))
    assert tuple(argv[panel_index - 1 : panel_index + 2]) == (
        "--ro-bind",
        str(panel),
        "/inputs/panel.parquet",
    )
    assert "/inputs/dividends.json" in argv
    assert "/universe.json" in argv


def test_bwrap_targets_and_evaluate_have_separate_writable_stage(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    panel, receipt, spec, dividends = (
        tmp_path / name for name in ("panel", "receipt", "spec", "dividends")
    )
    for path in (panel, receipt, spec, dividends):
        path.write_text("{}")
    targets_stage = tmp_path / "targets-stage"
    evaluator_stage = tmp_path / "evaluator-stage"
    targets_provenance = targets_stage / "provenance"
    evaluator_provenance = evaluator_stage / "provenance"
    targets_stage.mkdir()
    evaluator_stage.mkdir()
    targets_provenance.mkdir()
    evaluator_provenance.mkdir()
    target = bwrap_argv(
        pins,
        "targets",
        (str(pins.shared_python), "-m", "strategy"),
        panel=panel,
        receipt=receipt,
        worktree=worktree,
        targets_stage=targets_stage,
        provenance_dir=targets_provenance,
    )
    evaluate = bwrap_argv(
        pins,
        "evaluate",
        (str(pins.shared_python), "-P", "-s", str(pins.evaluator)),
        panel=panel,
        receipt=receipt,
        spec=spec,
        dividends=dividends,
        evaluator_stage=evaluator_stage,
        targets_file=targets_stage / "targets.json",
        provenance_dir=evaluator_provenance,
    )
    assert "/work" in target
    bind_index = target.index("--bind")
    assert tuple(target[bind_index : bind_index + 3]) == (
        "--bind",
        str(targets_stage),
        "/stage",
    )
    assert "/work" not in evaluate
    target_index = evaluate.index(str(targets_stage / "targets.json"))
    assert tuple(evaluate[target_index - 1 : target_index + 2]) == (
        "--ro-bind",
        str(targets_stage / "targets.json"),
        "/targets.json",
    )
    assert ("--bind", str(evaluator_stage), "/stage") == tuple(
        evaluate[evaluate.index("--bind") : evaluate.index("--bind") + 3]
    )
    assert evaluate[-13:-4] == (
        "--bind",
        str(evaluator_provenance),
        "/stage/provenance",
        "--setenv",
        "RESEARCH_PROVENANCE_DIR",
        "/stage/provenance",
        "--setenv",
        "RESEARCH_PROVENANCE_STAGE",
        "evaluate",
    )
    assert "/inputs/dividends.json" not in target
    assert "/universe.json" not in target
    assert "/inputs/spec.json" not in target


def test_bwrap_analysis_mount_is_scoped_to_declared_output_subdirectory(
    tmp_path: Path,
) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    scenarios = tmp_path / "scenarios"
    analysis_stage = tmp_path / "analysis-stage"
    provenance = analysis_stage / "provenance"
    worktree.mkdir()
    scenarios.mkdir()
    analysis_stage.mkdir()
    provenance.mkdir()
    panel = tmp_path / "panel"
    spec = tmp_path / "spec"
    panel.write_text("panel")
    spec.write_text("spec")
    command = bwrap_argv(
        pins,
        "analysis",
        (str(pins.shared_python), "-m", "analysis.module"),
        worktree=worktree,
        scenarios_dir=scenarios,
        analysis_stage=analysis_stage,
        analysis_inputs={"panel.parquet": panel},
        evaluation_specs={"c000": spec},
        provenance_dir=provenance,
    )
    assert ("--dir", "/stage") in pairwise(command)
    pythonpath = "/provenance:/snapshot/src:/work"
    assert command[command.index(pythonpath) - 1 : command.index(pythonpath) + 1] == (
        "PYTHONPATH",
        pythonpath,
    )
    bind = ("--bind", str(analysis_stage / "analysis"), "/stage/analysis")
    assert bind == tuple(command[command.index("--bind") : command.index("--bind") + 3])
    assert not any(
        command[index : index + 3] == ("--bind", str(analysis_stage), "/stage")
        for index in range(len(command) - 2)
    )
    assert ("--ro-bind", str(panel), "/inputs/panel.parquet") == tuple(
        command[command.index(str(panel)) - 1 : command.index(str(panel)) + 2]
    )


def test_bwrap_synthetic_env_is_target_only_and_preserves_command_suffix(
    tmp_path: Path,
) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    targets_stage = tmp_path / "targets-stage"
    provenance = targets_stage / "provenance"
    worktree.mkdir()
    targets_stage.mkdir()
    provenance.mkdir()
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    panel.write_text("panel")
    receipt.write_text("receipt")
    command = (str(pins.shared_python), "-m", "strategy", "--out", "/stage/targets.json")
    production = bwrap_argv(
        pins,
        "targets-s000",
        command,
        panel=panel,
        receipt=receipt,
        worktree=worktree,
        targets_stage=targets_stage,
        provenance_dir=provenance,
    )
    synthetic = {"H0006_SYNTHETIC_MODE": "1"}
    candidate = bwrap_argv(
        pins,
        "targets-s000",
        command,
        synthetic_env=synthetic,
        panel=panel,
        receipt=receipt,
        worktree=worktree,
        targets_stage=targets_stage,
        provenance_dir=provenance,
    )

    assert "H0006_SYNTHETIC_MODE" not in production
    assert production[-len(command) :] == command
    assert candidate[-len(command) :] == command
    assert candidate[-len(command) - 3 : -len(command)] == (
        "--setenv",
        "H0006_SYNTHETIC_MODE",
        "1",
    )
    scoped = stage_plan(
        pins,
        "job-test",
        "targets-s000",
        256,
        command,
        synthetic_env=synthetic,
        panel=panel,
        receipt=receipt,
        worktree=worktree,
        targets_stage=targets_stage,
        provenance_dir=provenance,
    )
    assert scoped.argv[-len(command) :] == command
    assert scoped.argv[-len(command) - 3 : -len(command)] == (
        "--setenv",
        "H0006_SYNTHETIC_MODE",
        "1",
    )
    assert synthetic == {"H0006_SYNTHETIC_MODE": "1"}


def test_bwrap_synthetic_env_is_supported_for_analysis(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    scenarios = tmp_path / "scenarios"
    analysis_stage = tmp_path / "analysis-stage"
    provenance = analysis_stage / "provenance"
    worktree.mkdir()
    scenarios.mkdir()
    analysis_stage.mkdir()
    provenance.mkdir()
    panel = tmp_path / "panel"
    panel.write_text("panel")
    command = (str(pins.shared_python), "-m", "analysis.module", "--flag")
    production = bwrap_argv(
        pins,
        "analysis",
        command,
        worktree=worktree,
        scenarios_dir=scenarios,
        analysis_stage=analysis_stage,
        analysis_inputs={"panel.parquet": panel},
        provenance_dir=provenance,
    )
    candidate = bwrap_argv(
        pins,
        "analysis",
        command,
        synthetic_env={"H0006_SYNTHETIC_MODE": "1"},
        worktree=worktree,
        scenarios_dir=scenarios,
        analysis_stage=analysis_stage,
        analysis_inputs={"panel.parquet": panel},
        provenance_dir=provenance,
    )

    assert "H0006_SYNTHETIC_MODE" not in production
    assert production[-len(command) :] == command
    assert candidate[-len(command) :] == command
    assert candidate[-len(command) - 3 : -len(command)] == (
        "--setenv",
        "H0006_SYNTHETIC_MODE",
        "1",
    )


@pytest.mark.parametrize(
    "synthetic_env",
    [
        [],
        {},
        {"H0006_SYNTHETIC_MODE": "1", "H0007_SYNTHETIC_MODE": "1"},
        {"bad": "1"},
        {"PATH": "1"},
        {"H0006_SYNTHETIC_MODE": "0"},
        {"H0006_SYNTHETIC_MODE": 1},
        {1: "1"},
        object(),
    ],
)
def test_bwrap_rejects_invalid_synthetic_env(tmp_path: Path, synthetic_env: object) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    targets_stage = tmp_path / "targets-stage"
    provenance = targets_stage / "provenance"
    worktree.mkdir()
    targets_stage.mkdir()
    provenance.mkdir()
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    panel.write_text("panel")
    receipt.write_text("receipt")

    with pytest.raises(ContainmentError):
        bwrap_argv(
            pins,
            "targets-s000",
            (str(pins.shared_python), "-m", "strategy"),
            synthetic_env=synthetic_env,  # type: ignore[arg-type]
            panel=panel,
            receipt=receipt,
            worktree=worktree,
            targets_stage=targets_stage,
            provenance_dir=provenance,
        )


@pytest.mark.parametrize("stage", ["validate", "validate-c000", "evaluate", "evaluate-s000"])
def test_bwrap_rejects_synthetic_env_for_validation_and_evaluation(
    tmp_path: Path, stage: str
) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    spec = tmp_path / "spec"
    dividends = tmp_path / "dividends"
    evaluator_stage = tmp_path / "evaluator-stage"
    provenance = tmp_path / "provenance"
    targets_file = tmp_path / "targets.json"
    for path in (panel, receipt, spec, dividends, targets_file):
        path.write_text("{}")
    evaluator_stage.mkdir()
    provenance.mkdir()

    if stage.startswith("evaluate"):
        with pytest.raises(ContainmentError):
            bwrap_argv(
                pins,
                stage,
                (str(pins.shared_python),),
                synthetic_env={"H0006_SYNTHETIC_MODE": "1"},
                panel=panel,
                receipt=receipt,
                spec=spec,
                dividends=dividends,
                evaluator_stage=evaluator_stage,
                targets_file=targets_file,
                provenance_dir=provenance,
            )
    else:
        with pytest.raises(ContainmentError):
            bwrap_argv(
                pins,
                stage,
                (str(pins.shared_python),),
                synthetic_env={"H0006_SYNTHETIC_MODE": "1"},
                panel=panel,
                receipt=receipt,
                spec=spec,
                dividends=dividends,
            )


def test_provenance_directory_is_required_and_scoped_to_stage(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    panel.write_text("panel")
    receipt.write_text("receipt")
    with pytest.raises(ContainmentError, match="required"):
        bwrap_argv(
            pins,
            "targets-s000",
            (str(pins.shared_python), "-m", "strategy"),
            panel=panel,
            receipt=receipt,
            worktree=tmp_path,
            targets_stage=tmp_path,
        )
    provenance = tmp_path / "provenance"
    provenance.mkdir()
    with pytest.raises(ContainmentError, match="not allowed"):
        bwrap_argv(
            pins,
            "validate-c000",
            (str(pins.shared_python),),
            panel=panel,
            receipt=receipt,
            provenance_dir=provenance,
        )
    with pytest.raises(ContainmentError, match="absolute"):
        bwrap_argv(
            pins,
            "targets-s000",
            (str(pins.shared_python), "-m", "strategy"),
            panel=panel,
            receipt=receipt,
            worktree=tmp_path,
            targets_stage=tmp_path,
            provenance_dir=Path("relative"),
        )


def test_target_rewrite_maps_only_known_roots_and_rejects_escape(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    run_dir = tmp_path / "run"
    worktree.mkdir()
    run_dir.mkdir()
    target = worktree / "target.py"
    target.write_text("pass\n")
    argv = rewrite_targets_argv(
        (str(pins.shared_python), str(target), str(run_dir / "targets.json")),
        shared_python=pins.shared_python,
        worktree=worktree,
        run_dir=run_dir,
    )
    assert argv == (str(pins.shared_python), "/work/target.py", "/stage/targets.json")
    with pytest.raises(ContainmentError, match="outside"):
        rewrite_targets_argv(
            (str(pins.shared_python), "/etc/passwd"),
            shared_python=pins.shared_python,
            worktree=worktree,
            run_dir=run_dir,
        )


def test_target_inputs_are_rewritten_to_readonly_input_mounts(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    run_dir = tmp_path / "run"
    worktree.mkdir()
    run_dir.mkdir()
    target = worktree / "target.py"
    target.write_text("pass\n")
    panel = tmp_path / "panel"
    receipt = tmp_path / "receipt"
    panel.write_text("panel")
    receipt.write_text("receipt")
    argv = (str(pins.shared_python), str(target), str(panel), str(receipt))
    validate_targets_argv(
        argv,
        shared_python=pins.shared_python,
        worktree=worktree,
        run_dir=run_dir,
        panel=panel,
        receipt=receipt,
    )
    assert rewrite_targets_argv(
        argv,
        shared_python=pins.shared_python,
        worktree=worktree,
        run_dir=run_dir,
        panel=panel,
        receipt=receipt,
    )[-2:] == ("/inputs/panel.parquet", "/inputs/receipt.json")


def test_scope_argv_has_fixed_resource_bound_and_unique_stage() -> None:
    plan = scope_argv("job-abcd", "evaluate", 256, ("/usr/bin/bwrap", "--clearenv"))
    assert plan.scope_unit == "research-job-abcd-evaluate"
    assert plan.argv[:8] == (
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--unit=research-job-abcd-evaluate",
        "--property=MemoryMax=256M",
        "--property=MemorySwapMax=0",
    )
    assert "--property=TasksMax=64" in plan.argv
    assert plan.argv[-2:] == ("/usr/bin/bwrap", "--clearenv")


def test_stage_plan_rejects_unknown_stage(tmp_path: Path) -> None:
    pins, _snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    with pytest.raises(ContainmentError, match="unknown"):
        stage_plan(pins, "job-test", "unknown", 1, ("/bin/true",))


def test_target_output_must_be_regular_non_symlink_and_small(tmp_path: Path) -> None:
    target = tmp_path / "targets.json"
    target.write_text("{}")
    assert target_file_ok(target)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert not target_file_ok(link)
    target.write_bytes(b"x" * 8)
    assert not target_file_ok(target, max_bytes=4)


def test_host_bus_environment_is_fixed_to_current_user() -> None:
    env = host_bus_environment()
    uid = os.getuid()
    assert env == {
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }


def test_stop_scope_verifies_the_exact_scope_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...] | list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(tuple(argv))
        return SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")

    monkeypatch.setattr("gateway.research.containment.subprocess.run", fake_run)
    assert stop_scope("research-job-test-evaluate")
    assert any("kill" in call for call in calls)
    assert any("stop" in call for call in calls)
    assert any("show" in call and "--property=ActiveState" in call for call in calls)


def test_stop_scope_refuses_to_claim_completion_when_scope_stays_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...] | list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(tuple(argv))
        if "--property=LoadState" in argv:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    clock = iter((0.0, 6.0))
    monkeypatch.setattr("gateway.research.containment.subprocess.run", fake_run)
    monkeypatch.setattr("gateway.research.containment.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("gateway.research.containment.time.sleep", lambda _seconds: None)
    assert not stop_scope("research-job-test-evaluate")
    assert any("--property=ActiveState" in call for call in calls)


@pytest.mark.parametrize(
    ("failure_mode", "expected_status", "expected_calls"),
    [
        (None, "succeeded", ["validate-c000", "targets-s000", "evaluate-s000", "analysis"]),
        ("validate", "input_validation_failed", ["validate-c000"]),
        ("targets", "scenario_failed", ["validate-c000", "targets-s000"]),
        ("evaluate", "scenario_failed", ["validate-c000", "targets-s000", "evaluate-s000"]),
        ("result", "scenario_failed", ["validate-c000", "targets-s000", "evaluate-s000"]),
    ],
)
def test_contained_worker_runs_fixed_stages_and_binds_v2_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str | None,
    expected_status: str,
    expected_calls: list[str],
) -> None:
    pins, _snapshot, _evaluator, universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "target.py").write_text("pass\n")
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=worktree, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=worktree, check=True)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=worktree, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    artifacts: dict[str, Path] = {}
    for name in ("spec", "panel", "receipt", "evaluation_spec", "dividends"):
        path = tmp_path / name
        path.write_text("{}")
        artifacts[name] = path
    digests = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in artifacts.items()
    }
    semantic = "a" * 64
    attempt_id = "H0001-A001"
    targets_argv = (
        str(pins.shared_python),
        str(worktree / "target.py"),
        str(run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"),
    )
    plan = RunPlan(
        "research-run-plan-v1",
        attempt_id,
        commit,
        "a" * 64,
        "b" * 64,
        "s000",
        (RunScenario("s000", targets_argv, "c000", digests["evaluation_spec"]),),
        AnalysisPlan("fixture.analysis", (), ("analysis/result.json",), 1024),
        1,
        1,
    )
    job: dict[str, object] = {
        "job_id": "job-contained",
        "run_dir": str(run_dir),
        "worktree": str(worktree),
        "expected_commit": commit,
        "timeout_seconds": 5,
        "max_rss_mb": 256,
        "targets_argv": list(targets_argv),
        "artifact_paths": {name: str(path) for name, path in artifacts.items()},
        "artifact_digests": digests,
        "snapshot_dir": str(pins.snapshot_dir),
        "snapshot_sha256": pins.snapshot_sha256,
        "shared_python": str(pins.shared_python),
        "shared_python_resolved": str(pins.resolved_python),
        "resolved_python": str(pins.resolved_python),
        "shared_python_sha256": pins.shared_python_sha256,
        "pyvenv_cfg": str(pins.pyvenv_cfg),
        "pyvenv_sha256": pins.pyvenv_sha256,
        "distribution_dir": str(pins.distribution_dir),
        "evaluator_source": str(pins.evaluator),
        "evaluator_sha256": pins.evaluator_sha256,
        "universe": str(universe),
        "universe_sha256": pins.universe_sha256,
        "run_plan": json.loads(plan.to_json()),
        "run_plan_sha256": hashlib.sha256(plan.to_json().encode()).hexdigest(),
        "evaluation_spec_paths": {"c000": str(artifacts["evaluation_spec"])},
        "evaluation_spec_digests": {"c000": digests["evaluation_spec"]},
    }

    def fake_plan(
        _pins: RuntimePins,
        _job: str,
        stage: str,
        _rss: int,
        _command: object,
        **_mounts: object,
    ) -> object:
        from gateway.research.containment import StagePlan

        return StagePlan(stage, f"unit-{stage}", ("/bin/true",))

    calls: list[str] = []

    def fake_stage(plan: object, _cwd: Path, out: Path, _deadline: float) -> tuple[int, bool]:
        assert hasattr(plan, "stage")
        stage = str(plan.stage)
        calls.append(stage)
        if stage.startswith("validate"):
            out.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "reasons": [],
                        "spec_sha256_semantic": semantic,
                        "spec_sha256_raw": digests["evaluation_spec"],
                        "panel_sha256": digests["panel"],
                        "receipt_sha256": digests["receipt"],
                        "universe_file_sha256": pins.universe_sha256,
                        "dividends_sha256": digests["dividends"],
                    }
                )
            )
            if failure_mode == "validate":
                return 7, False
        elif stage.startswith("targets"):
            output = run_dir / "scenarios" / "s000" / "targets-stage" / "targets.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text("{}")
            if failure_mode == "targets":
                return 7, False
        elif stage.startswith("evaluate"):
            output = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out" / "result.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "evaluator_version": "research-evaluator-v2",
                        "spec_sha256": (
                            digests["evaluation_spec"] if failure_mode == "result" else semantic
                        ),
                        "dividends_sha256": digests["dividends"],
                        "compliant": True,
                    }
                )
            )
            output.with_name("trades.parquet").write_bytes(b"trades")
            output.with_name("daily.parquet").write_bytes(b"daily")
            if failure_mode == "evaluate":
                return 7, False
        else:
            output = run_dir / "analysis-stage" / "analysis" / "result.json"
            output.write_text("{}")
        return 0, False

    monkeypatch.setattr(worker, "stage_plan", fake_plan)
    monkeypatch.setattr(worker, "_stage", fake_stage)
    monkeypatch.setattr(
        worker,
        "verify_stage_provenance",
        lambda _records_dir, *, stage, **_kwargs: {"stage": stage, "record_count": 1},
    )
    assert worker._contained_run_plan(job)["status"] == expected_status
    assert calls == expected_calls
    terminal = json.loads((run_dir / "terminal.json").read_text())
    assert terminal["status"] == expected_status
    assert (run_dir / "validated-inputs.json").is_file() is not (failure_mode == "validate")
