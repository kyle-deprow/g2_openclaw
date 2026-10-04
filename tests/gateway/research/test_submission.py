"""``submission-build`` / ``submission-preflight`` tests.

The fixture is a real golden-run hypothesis (real runtime pins, real git worktree, real committed
provenance records); only the contained execution is not run, because these commands never run
anything.  The produced files are accepted by the real ``implementation-submit``.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from gateway.cli import app
from gateway.research import review_evidence
from gateway.research.containment import PROVENANCE_CONTRACT, recorder_sha256
from gateway.research.contracts import (
    AttemptState,
    HypothesisState,
    ImplementationRecord,
    RunPlan,
    SubmissionInput,
    SubmissionScenarioInput,
    is_host_owned_target_flag,
)
from gateway.research.machine import freeze as machine_freeze
from gateway.research.provenance import validate_provenance_evidence
from gateway.research.review_evidence import (
    BundleCandidate,
    build_review_bundle,
    dry_build_review_bundle,
)
from gateway.research.store import ResearchStore, StoreConflict
from gateway.research.submission import run_preflight
from typer.testing import CliRunner

from tests.gateway.research.conftest import record_probe
from tests.gateway.research.test_golden_run import FIXTURES, GoldenDraft, golden_draft

runner = CliRunner()
SCENARIO_TIMEOUT = 10.0
ANALYSIS_TIMEOUT = 10.0
STAGES = ("targets-s000", "targets-s001", "evaluate-s000", "evaluate-s001", "analysis")

# The structural shape of the live H0008-A001 run-plan.json scenario argv.
LIVE_H0008_ARGV = [
    "/home/dev/repos/quantipy/.venv/bin/python",
    "/home/dev/.openclaw/research-v2/hypothesis-worktrees/H0008-A001/scripts/h0008_targets.py",
    "--panel",
    "/home/dev/autoresearch-reversal-20260911.9AbPOA/inputs/october-panel.parquet",
    "--receipt",
    "/home/dev/autoresearch-reversal-20260911.9AbPOA/receipts/october-panel-receipt.json",
    "--scenario-id",
    "s000",
    "--out",
    "/home/dev/.openclaw/research-v2/hypotheses/H0008/attempts/H0008-A001/run/scenarios/s000/"
    "targets-stage/targets.json",
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _skeleton(argv: list[str] | tuple[str, ...]) -> list[str]:
    """Flags kept, every value replaced by a placeholder named after its preceding token."""
    skeleton: list[str] = []
    for index, token in enumerate(argv):
        if token.startswith("--"):
            skeleton.append(token)
        else:
            skeleton.append(
                ("<interpreter>", "<program>")[index] if index < 2 else f"<{argv[index - 1]}>"
            )
    return skeleton


@dataclass
class Env:
    draft: GoldenDraft
    attempt_id: str
    commit: str
    input_path: Path
    out_dir: Path
    payload: dict[str, object]

    @property
    def store(self) -> ResearchStore:
        return self.draft.store

    @property
    def source(self) -> Path:
        return self.draft.source

    @property
    def record_path(self) -> Path:
        return self.out_dir / "implementation-record.json"

    @property
    def plan_path(self) -> Path:
        return self.out_dir / "run-plan.json"

    @property
    def provenance_path(self) -> Path:
        return self.out_dir / "provenance-evidence.json"


def _provenance_record(stage: str, env_pins: object, worktree: Path) -> dict[str, object]:
    snapshot = env_pins.snapshot_dir  # type: ignore[attr-defined]
    work_module = "golden_analysis" if stage == "analysis" else "golden_target"
    modules: list[dict[str, object]] = [
        {
            "name": work_module,
            "path": f"/work/{work_module}.py",
            "origin": "work",
            "sha256": hashlib.sha256((worktree / f"{work_module}.py").read_bytes()).hexdigest(),
        },
        {
            "name": "sitecustomize",
            "path": "/provenance/sitecustomize.py",
            "origin": "recorder",
            "sha256": recorder_sha256(),
        },
        {"name": "json", "path": "/usr/lib/python3.13/json/__init__.py", "origin": "runtime"},
        {
            "name": "quantipy",
            "path": "/snapshot/src/quantipy/__init__.py",
            "origin": "snapshot",
            "sha256": hashlib.sha256(
                (snapshot / "src" / "quantipy" / "__init__.py").read_bytes()
            ).hexdigest(),
        },
    ]
    return {
        "contract": PROVENANCE_CONTRACT,
        "stage": stage,
        "pid": 101,
        "ppid": 1,
        "argv": ["/venv/bin/python", f"/work/{work_module}.py"],
        "executable": "/venv/bin/python",
        "cwd": "/work",
        "python_version": "3.13.0",
        "flags": {"safe_path": True, "no_user_site": True},
        "pythonpath": "/provenance:/snapshot/src:/work",
        "sys_path": ["/provenance", "/snapshot/src", "/work"],
        "recorder": {"path": "/provenance/sitecustomize.py", "sha256": recorder_sha256()},
        "modules": modules,
        "written_at": "2026-09-22T00:00:00Z",
    }


def make_env(
    tmp_path: Path,
    *,
    probe_wall: float | None = 0.5,
    scenario_timeout: float = SCENARIO_TIMEOUT,
) -> Env:
    from gateway.research.containment import runtime_pins_from_record

    draft = golden_draft(tmp_path)
    store, source, hypothesis = draft.store, draft.source, draft.hypothesis
    hypothesis_id = hypothesis.hypothesis_id
    if probe_wall is None:
        # A hypothesis frozen before the probe existed has no probe evidence at all.
        store._update_hypothesis(
            machine_freeze(store.get_hypothesis(hypothesis_id)),
            "hypothesis_frozen",
            expected=HypothesisState.DRAFT,
        )
    else:
        if 3 * math.ceil(1.5 * probe_wall) + 1 > 120:
            # The fixture's compute.max_wall_seconds (120) cannot cover a long measured stage.
            store.set_draft_compute(hypothesis_id, 900.0, 1024)
        record_probe(store, hypothesis_id, wall_seconds=probe_wall)
        store.freeze(hypothesis_id)
    attempt = store.open_attempt(hypothesis_id, source)
    for name in ("golden_target.py", "golden_analysis.py"):
        shutil.copyfile(FIXTURES / name, source / name)
    (source / "proof").mkdir()
    (source / "proof" / "unit-test.txt").write_text("42 passed\n", encoding="utf-8")
    pins = runtime_pins_from_record(dict(store.config()))
    for stage in STAGES:
        directory = source / "provenance" / stage
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(
            json.dumps(_provenance_record(stage, pins, source), sort_keys=True), encoding="utf-8"
        )
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "implementation")
    commit = _git(source, "rev-parse", "HEAD")
    payload: dict[str, object] = {
        "contract": "research-submission-input-v1",
        "commit": commit,
        "test_evidence_path": "proof/unit-test.txt",
        "reported_coder_model": "Luna",
        "coder_effort": "xhigh",
        "coder_service_tier": "unknown",
        "interpreter": str(draft.venv / "bin" / "python"),
        "target_script": "golden_target.py",
        "scenarios": [
            {"scenario_id": "s000", "spec_id": "c000", "extra_args": ["--variant", "baseline"]},
            {"scenario_id": "s001", "spec_id": "c001", "extra_args": ["--variant", "wider"]},
        ],
        "primary_scenario_id": "s000",
        "analysis": {
            "module": "golden_analysis",
            "args": [],
            "artifacts": ["analysis/result.json"],
            "max_artifact_bytes": 1 << 20,
        },
        "scenario_timeout_seconds": scenario_timeout,
        "analysis_timeout_seconds": ANALYSIS_TIMEOUT,
        "provenance_stages": [
            {"stage": stage, "records_dir": f"provenance/{stage}"} for stage in STAGES
        ],
    }
    input_path = tmp_path / "submission-input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    return Env(draft, attempt.attempt_id, commit, input_path, tmp_path / "submission", payload)


def build(env: Env, *, expect: int = 0, out_dir: Path | None = None) -> str:
    result = runner.invoke(
        app,
        [
            "research",
            "submission-build",
            env.attempt_id,
            "--root",
            str(env.store.root),
            "--input",
            str(env.input_path),
            "--out-dir",
            str(out_dir or env.out_dir),
        ],
    )
    assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result.output


def preflight_cli(env: Env, *extra: str, expect: int | None = None) -> tuple[int, str]:
    result = runner.invoke(
        app,
        [
            "research",
            "submission-preflight",
            env.attempt_id,
            "--root",
            str(env.store.root),
            "--file",
            str(env.record_path),
            "--run-plan",
            str(env.plan_path),
            "--provenance-evidence",
            str(env.provenance_path),
            *extra,
        ],
    )
    if expect is not None:
        assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result.exit_code, result.output


def statuses(output: str) -> dict[str, str]:
    """Map check name -> ``PASS``/``FAIL`` from the plain-text report."""
    return {line.split(" ", 2)[1]: line.split(" ", 1)[0] for line in output.splitlines()[-20:]}


def submit_cli(env: Env, expect: int = 0) -> str:
    result = runner.invoke(
        app,
        [
            "research",
            "implementation-submit",
            env.attempt_id,
            "--root",
            str(env.store.root),
            "--file",
            str(env.record_path),
            "--run-plan",
            str(env.plan_path),
            "--provenance-evidence",
            str(env.provenance_path),
        ],
    )
    assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result.output


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def rewrite(
    env: Env,
    *,
    record: Callable[[Any], None] | None = None,
    plan: Callable[[Any], None] | None = None,
) -> None:
    """Mutate the built files; the plan's implementation digest is kept consistent."""
    record_value = json.loads(env.record_path.read_text(encoding="utf-8"))
    plan_value = json.loads(env.plan_path.read_text(encoding="utf-8"))
    if record is not None:
        record(record_value)
    if plan is not None:
        plan(plan_value)
    record_text = ImplementationRecord.from_json(json.dumps(record_value)).to_json()
    env.record_path.write_text(record_text, encoding="utf-8")
    plan_value["implementation_sha256"] = hashlib.sha256(record_text.encode()).hexdigest()
    env.plan_path.write_text(RunPlan.from_json(json.dumps(plan_value)).to_json(), encoding="utf-8")


# -- build ---------------------------------------------------------------------------------


def test_build_files_pass_preflight_and_are_accepted_by_implementation_submit(env: Env) -> None:
    output = build(env)
    assert [path.name for path in sorted(env.out_dir.iterdir())] == [
        "implementation-record.json",
        "provenance-evidence.json",
        "run-plan.json",
    ]
    assert "FAIL" not in output
    code, report = preflight_cli(env)
    assert code == 0, report
    assert set(statuses(report).values()) == {"PASS"}
    expected = {
        "implementation-file",
        "run-plan-file",
        "provenance",
        "attempt-state",
        "plan-binding",
        "spec-set",
        "spec-set-digest",
        "primary-argv",
        "scenario-specs",
        "compute-probe",
        "worktree",
        "argv-shape",
        "launch",
        "stage-budget",
        "bundle",
    }
    assert set(statuses(report)) == expected

    record = ImplementationRecord.from_json(env.record_path.read_text(encoding="utf-8"))
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    spec_set = env.store.evaluation_spec_set(env.draft.hypothesis.hypothesis_id)
    assert plan.implementation_sha256 == hashlib.sha256(env.record_path.read_bytes()).hexdigest()
    assert (
        plan.evaluation_spec_set_sha256 == hashlib.sha256(spec_set.to_json().encode()).hexdigest()
    )
    assert [scenario.evaluation_spec_sha256 for scenario in plan.scenarios] == [
        entry.sha256 for entry in spec_set.specs
    ]
    assert record.targets_argv == plan.scenarios[0].targets_argv
    assert record.test_evidence_path == str(env.source / "proof" / "unit-test.txt")

    assert submit_cli(env).strip() == "IMPLEMENTED"
    attempt = env.store.get_attempt(env.attempt_id)
    assert attempt.state == AttemptState.IMPLEMENTED
    assert attempt.implementation_sha256 == plan.implementation_sha256
    # Preflight of an identical replay still passes, and so does the real submit.
    code, report = preflight_cli(env)
    assert code == 0 and "identical replay" in report, report
    assert submit_cli(env).strip() == "IMPLEMENTED"


def test_build_argv_has_the_live_h0008_shape(env: Env) -> None:
    build(env)
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    hypothesis = env.draft.hypothesis
    run_dir = (
        env.store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / env.attempt_id
        / "run"
    )
    live = _skeleton(LIVE_H0008_ARGV)
    for scenario in plan.scenarios:
        argv = list(scenario.targets_argv)
        # [interpreter, script, --panel P, --receipt R, --scenario-id sNNN, *extra, --out OUT]
        head = argv[: argv.index("--scenario-id") + 2]
        extra = argv[len(head) : -2]
        assert extra[0] == "--variant" and len(extra) == 2  # the fixture's own extra args
        assert _skeleton(head + argv[-2:]) == live
        assert head[0] == str(env.draft.venv / "bin" / "python")
        assert head[1] == str(env.source / "golden_target.py")
        assert head[3] == hypothesis.panel_path and head[5] == hypothesis.receipt_path
        assert head[7] == scenario.scenario_id
        assert argv[-1] == str(
            run_dir / "scenarios" / scenario.scenario_id / "targets-stage" / "targets.json"
        )
    # The live H0008 argv has the same fixed structure with no extra args.
    assert LIVE_H0008_ARGV[2::2][:3] == ["--panel", "--receipt", "--scenario-id"]
    assert LIVE_H0008_ARGV[-2] == "--out"


def test_build_refuses_unless_the_attempt_accepts_submission(env: Env) -> None:
    build(env)
    submit_cli(env)
    output = build(env, expect=1, out_dir=env.out_dir.parent / "second")
    assert "illegal transition: IMPLEMENTED -> IMPLEMENTED" in output
    assert not (env.out_dir.parent / "second").exists()


def test_build_refuses_a_dirty_worktree_and_a_moved_head(env: Env) -> None:
    (env.source / "stray.txt").write_text("x", encoding="utf-8")
    assert "worktree is dirty" in build(env, expect=1)
    (env.source / "stray.txt").unlink()
    (env.source / "second.txt").write_text("x", encoding="utf-8")
    _git(env.source, "add", ".")
    _git(env.source, "commit", "-qm", "later")
    assert "HEAD does not match the implementation commit" in build(env, expect=1)
    assert not env.out_dir.exists()


@pytest.mark.parametrize("where", ["inside-worktree", "non-empty", "relative"])
def test_build_refuses_a_bad_out_dir(env: Env, where: str) -> None:
    if where == "inside-worktree":
        output = build(env, expect=1, out_dir=env.source / "out")
        assert "outside the attempt worktree" in output
        assert not (env.source / "out").exists()
    elif where == "non-empty":
        env.out_dir.mkdir()
        (env.out_dir / "old.json").write_text("{}", encoding="utf-8")
        assert "empty or absent" in build(env, expect=1)
    else:
        assert "absolute" in build(env, expect=1, out_dir=Path("relative-out"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("interpreter", "interpreter does not match"),
        ("script", "target_script is not tracked"),
        ("evidence", "test_evidence_path is not tracked"),
        ("spec", "names unknown spec c009"),
    ],
)
def test_build_refuses_bad_declarative_input(env: Env, mutation: str, message: str) -> None:
    payload = dict(env.payload)
    if mutation == "interpreter":
        payload["interpreter"] = "/bin/sh"
    elif mutation == "script":
        payload["target_script"] = "missing.py"
    elif mutation == "evidence":
        payload["test_evidence_path"] = "proof/missing.txt"
    else:
        scenarios = [dict(item) for item in env.payload["scenarios"]]  # type: ignore[attr-defined]
        scenarios[1]["spec_id"] = "c009"
        payload["scenarios"] = scenarios
    env.input_path.write_text(json.dumps(payload), encoding="utf-8")
    assert message in build(env, expect=1)
    assert not env.out_dir.exists()


RESERVED_FORMS = [
    "--panel",
    "--panel=/work/fake.parquet",
    "--receipt",
    "--receipt=/work/fake.json",
    "--scenario-id",
    "--scenario-id=s001",
    "--out",
    "--out=/tmp/x.json",
    "--pan",
    "--pan=/work/fake.parquet",
    "--rec",
    "--sce",
    "--scenario",
    "--o",
    "--ou=/tmp/x.json",
    "--",
]


@pytest.mark.parametrize("token", RESERVED_FORMS)
def test_extra_args_cannot_set_host_owned_flags_in_any_spelling(token: str) -> None:
    assert is_host_owned_target_flag(token)
    with pytest.raises(ValueError, match="host-owned"):
        SubmissionScenarioInput("s000", "c000", ("--variant", token))


@pytest.mark.parametrize("token", ["--variant", "--variant=x", "--outx", "-o", "x", "--panels"])
def test_unrelated_extra_args_are_allowed(token: str) -> None:
    assert not is_host_owned_target_flag(token)
    SubmissionScenarioInput("s000", "c000", (token,))


@pytest.mark.parametrize("token", RESERVED_FORMS)
def test_argv_shape_rejects_every_spelling_of_a_host_owned_flag(env: Env, token: str) -> None:
    build(env)

    def inject(argv: list[str]) -> None:
        argv[-2:-2] = [token]

    def change_plan(plan: Any) -> None:
        inject(plan["scenarios"][0]["targets_argv"])

    def change_record(record: Any) -> None:
        inject(record["targets_argv"])

    rewrite(env, record=change_record, plan=change_plan)
    _, output = preflight_cli(env)
    assert "FAIL argv-shape" in output and "repeats a host-owned flag" in output


def test_build_emits_the_canonical_pinned_interpreter_as_argv0(env: Env, tmp_path: Path) -> None:
    alias = tmp_path / "alias-python"
    alias.symlink_to(env.draft.venv / "bin" / "python")
    payload = {**env.payload, "interpreter": str(alias)}
    env.input_path.write_text(json.dumps(payload), encoding="utf-8")
    build(env)
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    pinned = str(env.draft.venv / "bin" / "python")
    assert all(scenario.targets_argv[0] == pinned for scenario in plan.scenarios)
    assert ImplementationRecord.from_json(env.record_path.read_text()).targets_argv[0] == pinned
    code, report = preflight_cli(env)
    assert code == 0, report


def test_failed_validation_writes_nothing_and_the_rerun_reuses_the_out_dir(env: Env) -> None:
    good = dict(env.payload)
    stages = [dict(item) for item in env.payload["provenance_stages"]]  # type: ignore[attr-defined]
    stages[-1]["records_dir"] = "provenance/missing"
    env.input_path.write_text(json.dumps({**good, "provenance_stages": stages}), encoding="utf-8")
    env.out_dir.mkdir()  # a pre-created empty directory must be left alone and empty
    build(env, expect=1)
    assert list(env.out_dir.iterdir()) == []
    env.input_path.write_text(json.dumps(good), encoding="utf-8")
    build(env)
    assert len(list(env.out_dir.iterdir())) == 3
    # Absent directory: a failure must not leave one behind either.
    fresh = env.out_dir.parent / "fresh"
    env.input_path.write_text(json.dumps({**good, "provenance_stages": stages}), encoding="utf-8")
    build(env, expect=1, out_dir=fresh)
    assert not fresh.exists()


def test_build_requires_a_blob_at_the_commit(env: Env) -> None:
    # ``proof`` is a tree at the commit, not a blob.
    for key in ("target_script", "test_evidence_path"):
        env.input_path.write_text(json.dumps({**env.payload, key: "proof"}), encoding="utf-8")
        assert f"{key} is not tracked" in build(env, expect=1)
    assert not env.out_dir.exists()


def test_submission_input_is_strict() -> None:
    base = {
        "contract": "research-submission-input-v1",
        "commit": "a" * 40,
        "test_evidence_path": "proof/t.txt",
        "reported_coder_model": "Luna",
        "coder_effort": "xhigh",
        "coder_service_tier": "unknown",
        "interpreter": "/venv/bin/python",
        "target_script": "scripts/t.py",
        "scenarios": [{"scenario_id": "s000", "spec_id": "c000", "extra_args": []}],
        "primary_scenario_id": "s000",
        "analysis": {
            "module": "m.a",
            "args": [],
            "artifacts": ["analysis/x.json"],
            "max_artifact_bytes": 10,
        },
        "scenario_timeout_seconds": 5,
        "analysis_timeout_seconds": 5,
        "provenance_stages": [{"stage": "analysis", "records_dir": "p/a"}],
    }
    parsed = SubmissionInput.from_json(json.dumps(base))
    assert parsed.scenario_timeout_seconds == 5.0

    def broken(**changes: object) -> str:
        return json.dumps({**base, **changes})

    bad_inputs: list[dict[str, object]] = [
        {"extra": 1},
        {"target_script": "/abs/t.py"},
        {"test_evidence_path": "../escape"},
        {"scenarios": [{"scenario_id": "s001", "spec_id": "c000", "extra_args": []}]},
        {"scenarios": [{"scenario_id": "s000", "spec_id": "c000", "extra_args": ["--out"]}]},
        {"interpreter": "python"},
        {"provenance_stages": []},
        {"scenario_timeout_seconds": True},
    ]
    for changes in bad_inputs:
        with pytest.raises(ValueError):
            SubmissionInput.from_json(broken(**changes))
    missing = dict(base)
    del missing["commit"]
    with pytest.raises(ValueError):
        SubmissionInput.from_json(json.dumps(missing))


# -- preflight: every check fails independently -------------------------------------------


def failing(env: Env) -> dict[str, str]:
    code, output = preflight_cli(env)
    assert code == 1, output
    return {name: status for name, status in statuses(output).items() if status == "FAIL"}


def test_wrong_spec_digest_fails_scenario_specs(env: Env) -> None:
    build(env)
    wrong = hashlib.sha256(b"a file hash, not the store spec-set entry").hexdigest()

    def change(plan: Any) -> None:
        plan["scenarios"][1]["evaluation_spec_sha256"] = wrong

    rewrite(env, plan=change)
    assert set(failing(env)) == {"scenario-specs"}


def test_wrong_spec_set_digest_fails_spec_set_digest(env: Env) -> None:
    build(env)
    rewrite(env, plan=lambda plan: plan.update(evaluation_spec_set_sha256="b" * 64))
    # The bundle dry-build refuses the same wrong digest, so it is the only other failure.
    assert set(failing(env)) == {"spec-set-digest", "bundle"}


def test_plan_not_bound_to_the_record_fails_plan_binding(env: Env) -> None:
    build(env)
    value = json.loads(env.plan_path.read_text(encoding="utf-8"))
    value["implementation_sha256"] = "c" * 64
    env.plan_path.write_text(RunPlan.from_json(json.dumps(value)).to_json(), encoding="utf-8")
    assert set(failing(env)) == {"plan-binding"}


def test_primary_argv_mismatch_fails_primary_argv(env: Env) -> None:
    build(env)

    def change(record: Any) -> None:
        record["targets_argv"] = [
            *record["targets_argv"][:-2],
            "--variant",
            "x",
            *record["targets_argv"][-2:],
        ]

    rewrite(env, record=change)
    # ``jobs.validate_launch`` independently requires the primary argv to equal the launch argv.
    assert set(failing(env)) == {"primary-argv", "launch"}


@pytest.mark.parametrize("target", ["in-worktree", "other-scenario", "elsewhere"])
def test_non_production_out_fails_argv_shape(env: Env, target: str) -> None:
    build(env)
    outputs: dict[str, Callable[[list[str]], str]] = {
        "in-worktree": lambda argv: str(env.source / "targets.json"),
        "other-scenario": lambda argv: argv[-1].replace("/s000/", "/s009/"),
        "elsewhere": lambda argv: "/tmp/targets.json",
    }

    def both(argv: list[str]) -> None:
        argv[-1] = outputs[target](argv)

    def change_record(record: Any) -> None:
        both(record["targets_argv"])

    def change_plan(plan: Any) -> None:
        both(plan["scenarios"][0]["targets_argv"])

    rewrite(env, record=change_record, plan=change_plan)
    fails = failing(env)
    assert "argv-shape" in fails
    # ``launch`` independently refuses an --out outside the run directory.
    if target == "elsewhere":
        assert "launch" in fails
    assert "plan-binding" not in fails and "primary-argv" not in fails


def test_dirty_worktree_fails_worktree(env: Env) -> None:
    build(env)
    (env.source / "stray.txt").write_text("x", encoding="utf-8")
    fails = failing(env)
    assert "worktree" in fails
    assert "plan-binding" not in fails and "argv-shape" not in fails
    _, output = preflight_cli(env)
    assert "FAIL worktree worktree is dirty" in output


def test_head_not_at_commit_fails_worktree(env: Env) -> None:
    build(env)
    (env.source / "later.txt").write_text("x", encoding="utf-8")
    _git(env.source, "add", ".")
    _git(env.source, "commit", "-qm", "later")
    fails = failing(env)
    assert "worktree" in fails
    _, output = preflight_cli(env)
    assert "FAIL worktree HEAD does not match the implementation commit" in output


def test_stage_budget_over_the_run_limit_fails_stage_budget(tmp_path: Path) -> None:
    env = make_env(tmp_path, scenario_timeout=4750.0)
    # Only a derived run timeout (budget + 300 s) above 28800 s is refused here: 28500 + 10 s.
    out = build(env, expect=1)
    assert "FAIL stage-budget derived run timeout 28810s" in out
    assert "exceeds the 28800s limit" in out


def test_stage_budget_over_compute_max_wall_fails_stage_budget(tmp_path: Path) -> None:
    env = make_env(tmp_path, scenario_timeout=30.0)  # 30 x 6 + 10 = 190 s > compute 120 s
    out = build(env, expect=1)
    assert "FAIL stage-budget stage budget 190s exceeds compute.max_wall_seconds=120" in out


def test_probe_gate_violation_fails_compute_probe(tmp_path: Path) -> None:
    env = make_env(tmp_path, probe_wall=40.0)  # requires scenario_timeout >= 60 s
    out = build(env, expect=1)
    assert "FAIL compute-probe scenario_timeout_seconds=10 is below the required minimum 60" in out
    fails = {name for name, status in statuses(out).items() if status == "FAIL"}
    assert fails == {"compute-probe"}


def test_compute_probe_gate_is_not_applicable_without_a_probe(tmp_path: Path) -> None:
    env = make_env(tmp_path, probe_wall=None)
    build(env)
    _, report = preflight_cli(env, expect=0)
    assert "PASS compute-probe no compute probe recorded; gate not applicable" in report


def test_bundle_over_the_size_limit_fails_bundle(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    build(env)
    baseline = run_preflight(
        env.store,
        env.attempt_id,
        record_path=env.record_path,
        run_plan_path=env.plan_path,
        provenance_path=env.provenance_path,
    )
    assert baseline.ok and baseline.bundle is not None
    total = baseline.bundle.total_bytes
    assert total > baseline.bundle.diff_bytes
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", total - 1)
    fails = failing(env)
    assert set(fails) == {"bundle"}
    _, output = preflight_cli(env)
    assert f"total_bytes={total} of {total - 1}" in output
    assert "diff_bytes=" in output and "largest:" in output


def test_bad_provenance_fails_provenance_and_bundle(env: Env) -> None:
    build(env)
    value = json.loads(env.provenance_path.read_text(encoding="utf-8"))
    value["stages"] = value["stages"][:-1]
    env.provenance_path.write_text(json.dumps(value), encoding="utf-8")
    assert set(failing(env)) == {"provenance", "bundle"}


def test_malformed_files_stop_early_with_a_file_failure(env: Env) -> None:
    build(env)
    env.record_path.write_text("{", encoding="utf-8")
    code, output = preflight_cli(env)
    assert code == 1
    assert "FAIL implementation-file" in output and "PASS run-plan-file" in output
    assert "plan-binding" not in output


def test_json_output_reports_every_check_and_the_bundle_accounting(env: Env) -> None:
    build(env)
    code, output = preflight_cli(env, "--json")
    report = json.loads(output)
    assert code == 0 and report["ok"] is True
    assert {check["status"] for check in report["checks"]} == {"PASS"}
    bundle = report["bundle"]
    assert bundle["total_bytes"] > bundle["diff_bytes"] > 0
    assert bundle["deployed_estimate_bytes"] == (
        bundle["source_bytes"] + bundle["diff_bytes"] + 2 * 1024 * 1024
    )
    assert bundle["max_bundle_bytes"] == review_evidence.MAX_BUNDLE_BYTES
    assert bundle["largest_files"]
    # The digest quotes a random scratch path, so it is not part of the output at all.
    assert "digest" not in bundle
    code, again = preflight_cli(env, "--json")
    assert code == 0 and json.loads(again) == report


def test_preflight_requires_an_existing_store(env: Env, tmp_path: Path) -> None:
    build(env)
    empty = tmp_path / "no-store"
    result = runner.invoke(
        app,
        [
            "research",
            "submission-preflight",
            env.attempt_id,
            "--root",
            str(empty),
            "--file",
            str(env.record_path),
            "--run-plan",
            str(env.plan_path),
            "--provenance-evidence",
            str(env.provenance_path),
        ],
    )
    assert result.exit_code == 1 and "holds no research store" in result.output
    assert not empty.exists()


# -- preflight is read-only -----------------------------------------------------------------


def _snapshot(env: Env) -> dict[str, object]:
    store_root = env.store.root
    return {
        "db": hashlib.sha256((store_root / "state.sqlite3").read_bytes()).hexdigest(),
        "entries": sorted(
            (str(path.relative_to(store_root)), path.stat().st_size)
            for path in store_root.rglob("*")
            if path.is_file() and not path.name.endswith(("-wal", "-shm"))
        ),
        "head": _git(env.source, "rev-parse", "HEAD"),
        "status": _git(env.source, "status", "--porcelain", "--untracked-files=all", "--ignored"),
        "index": hashlib.sha256((env.source / ".git" / "index").read_bytes()).hexdigest(),
        "worktree": sorted(
            str(path.relative_to(env.source))
            for path in env.source.rglob("*")
            if ".git" not in path.parts
        ),
    }


def test_preflight_does_not_mutate_the_store_or_the_worktree(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(env)
    before = _snapshot(env)
    code, output = preflight_cli(env)
    assert code == 0, output
    assert _snapshot(env) == before
    assert env.store.get_attempt(env.attempt_id).state == AttemptState.OPENED
    # A failing preflight is read-only too.
    (env.source / "stray.txt").write_text("x", encoding="utf-8")
    dirty = _snapshot(env)
    code, _ = preflight_cli(env)
    assert code == 1
    assert _snapshot(env) == dirty


# -- preflight / submit_implementation parity ------------------------------------------------


def _parity(env: Env, failed_check: str) -> None:
    """The preflight detail of ``failed_check`` is exactly what the real submit refuses with."""
    report = run_preflight(
        env.store,
        env.attempt_id,
        record_path=env.record_path,
        run_plan_path=env.plan_path,
        provenance_path=env.provenance_path,
    )
    detail = {check.name: check.detail for check in report.checks if not check.ok}
    assert failed_check in detail, report.lines()
    record = ImplementationRecord.from_json(env.record_path.read_text(encoding="utf-8"))
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    text = validate_provenance_evidence(env.store, env.attempt_id, plan, env.provenance_path)
    with pytest.raises(StoreConflict) as raised:
        env.store.submit_implementation(env.attempt_id, record, plan, containment_provenance=text)
    assert str(raised.value) == detail[failed_check]
    assert env.store.get_attempt(env.attempt_id).state == AttemptState.OPENED


def test_refusal_parity_plan_binding(env: Env) -> None:
    build(env)
    value = json.loads(env.plan_path.read_text(encoding="utf-8"))
    value["implementation_sha256"] = "c" * 64
    env.plan_path.write_text(RunPlan.from_json(json.dumps(value)).to_json(), encoding="utf-8")
    _parity(env, "plan-binding")


def test_refusal_parity_spec_set_digest(env: Env) -> None:
    build(env)
    rewrite(env, plan=lambda plan: plan.update(evaluation_spec_set_sha256="b" * 64))
    _parity(env, "spec-set-digest")


def test_refusal_parity_scenario_specs(env: Env) -> None:
    build(env)

    def change(plan: Any) -> None:
        plan["scenarios"][1]["evaluation_spec_sha256"] = "d" * 64

    rewrite(env, plan=change)
    _parity(env, "scenario-specs")


def test_refusal_parity_primary_argv(env: Env) -> None:
    build(env)

    def change(record: Any) -> None:
        record["targets_argv"] = [
            *record["targets_argv"][:-2],
            "--variant",
            "x",
            *record["targets_argv"][-2:],
        ]

    rewrite(env, record=change)
    _parity(env, "primary-argv")


def test_refusal_parity_probe_gate(tmp_path: Path) -> None:
    env = make_env(tmp_path, probe_wall=40.0)
    build(env, expect=1)
    _parity(env, "compute-probe")


def test_refusal_parity_replay_with_a_different_payload(env: Env) -> None:
    build(env)
    submit_cli(env)
    rewrite(env, record=lambda record: record.update(coder_effort="high"))
    report = run_preflight(
        env.store,
        env.attempt_id,
        record_path=env.record_path,
        run_plan_path=env.plan_path,
        provenance_path=env.provenance_path,
    )
    detail = {check.name: check.detail for check in report.checks if not check.ok}
    record = ImplementationRecord.from_json(env.record_path.read_text(encoding="utf-8"))
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    text = validate_provenance_evidence(env.store, env.attempt_id, plan, env.provenance_path)
    with pytest.raises(StoreConflict) as raised:
        env.store.submit_implementation(env.attempt_id, record, plan, containment_provenance=text)
    assert str(raised.value) == detail["attempt-state"]
    assert "implementation payload differs from stored payload" in detail["attempt-state"]


# -- the dry-built bundle is the bundle review-bundle builds ----------------------------------


def test_dry_build_matches_the_real_review_bundle(env: Env, tmp_path: Path) -> None:
    build(env)
    record = ImplementationRecord.from_json(env.record_path.read_text(encoding="utf-8"))
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    text = validate_provenance_evidence(env.store, env.attempt_id, plan, env.provenance_path)
    bundle_dir = tmp_path / "bundles" / "real"
    before = sorted(path.name for path in tmp_path.iterdir())
    report = dry_build_review_bundle(
        env.store,
        env.attempt_id,
        BundleCandidate(record.commit, record.to_json(), plan, text),
        nominal_bundle_dir=bundle_dir,
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert not bundle_dir.exists()
    submit_cli(env)
    digest = build_review_bundle(env.store, env.attempt_id, bundle_dir)
    real_total = sum(path.stat().st_size for path in bundle_dir.rglob("*") if path.is_file())
    assert report.digest == digest
    assert report.total_bytes == real_total
    assert report.error is None and report.within_limits
    assert report.diff_bytes == (bundle_dir / "diff.patch").stat().st_size
    assert dict(report.files)["diff.patch"] == report.diff_bytes
    assert report.largest_files[0][1] >= report.largest_files[-1][1]
    shutil.rmtree(bundle_dir.parent, ignore_errors=True)


def test_dry_build_cleans_its_scratch_directory_on_failure(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(env)
    record = ImplementationRecord.from_json(env.record_path.read_text(encoding="utf-8"))
    plan = RunPlan.from_json(env.plan_path.read_text(encoding="utf-8"))
    created: list[Path] = []
    original = tempfile.mkdtemp

    def tracking(*args: object, **kwargs: object) -> str:
        path = original(*args, **kwargs)  # type: ignore[call-overload]
        created.append(Path(path))
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", tracking)
    candidate = BundleCandidate(record.commit, record.to_json(), plan, '{"contract":"x"}')
    with pytest.raises(review_evidence.BundleError, match="malformed"):
        dry_build_review_bundle(env.store, env.attempt_id, candidate)
    assert created and not any(path.exists() for path in created)
