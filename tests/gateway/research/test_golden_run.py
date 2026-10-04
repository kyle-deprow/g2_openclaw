"""Golden end-to-end research driver tests.

These tests drive the production path with only the two external programs
replaced by deterministic fakes (the strategy target script and the pinned
evaluator console script, both under ``fixtures/golden``):

``research run`` (queue) -> ``research serve`` loop body (pre-claim
``validate_launch``, claim, ``jobs.launch``) -> the real detached worker ->
real bwrap/systemd-run stages -> real worker provenance verification -> real
host ``_terminal_outcome`` verification -> ``attempt-close`` / ``hypothesis-decide``.

Deliberately NOT faked here: the worker, containment, provenance recorder and
verification, the job launcher, the host success verifier, the store and the CLI.
There is deliberately no skip guard: like the worker's real-containment test, a host
without bwrap/systemd-run user scopes fails loudly rather than silently skipping.
All pinned paths here live under ``/tmp`` (pytest ``tmp_path``), so the bubblewrap
``/home`` parent-directory mount handling used for production pins is not exercised.
Faked by the same synthetic helpers the other dispatch tests use: typed admission
(``_admission_for_hypothesis``), the native runtime/core-database record, the campaign
policy, the wake sender and owner-poll network edges, and the review verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research import jobs
from gateway.research.contracts import (
    AnalysisPlan,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisSpec,
    HypothesisState,
    ImplementationRecord,
    JobState,
    RunPlan,
    RunScenario,
)
from gateway.research.status import read_status
from gateway.research.store import ResearchStore
from typer.testing import CliRunner, Result

from tests.gateway.research.conftest import provenance_evidence, review, verified_review
from tests.gateway.research.test_admission import _admit, _document, _payload
from tests.gateway.research.test_readiness import configure_real_readiness

FIXTURES = Path(__file__).parent / "fixtures" / "golden"
SCENARIO_TIMEOUT_SECONDS = 30.0
ANALYSIS_TIMEOUT_SECONDS = 30.0
RUN_OVERHEAD_SECONDS = 300.0
WAIT_BUDGET_SECONDS = 55.0
runner = CliRunner()


@dataclass(frozen=True)
class Golden:
    store: ResearchStore
    source: Path
    hypothesis: HypothesisSpec
    attempt_id: str
    plan: RunPlan
    run_dir: Path
    panel: Path
    receipt: Path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_readonly(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o444)
    return path


def _build_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failing_second_spec: bool
) -> Golden:
    """Configure, freeze, implement, review and admit a two-scenario attempt."""
    inputs = tmp_path / "inputs"
    # Real runtime pins: shared venv python, pinned evaluator console script, snapshot.
    snapshot = tmp_path / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION = 'golden'\n")
    (snapshot / "pyproject.toml").write_text("[project]\nname='quantipy'\n")
    (snapshot / "uv.lock").write_text("version = 1\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(str(Path(sys.executable).resolve()), venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n", encoding="utf-8"
    )
    evaluator = venv / "bin" / "quantipy"
    shutil.copyfile(FIXTURES / "golden_evaluator.py", evaluator)
    evaluator.chmod(0o555)
    universe = _write_readonly(tmp_path / "universe.json", '{"contract":"trusted-universe-v2"}\n')
    store = ResearchStore(tmp_path / "driver")
    store.configure(venv / "bin" / "python", evaluator, snapshot, universe)

    # Frozen inputs live outside both the worktree and the run directory.
    panel = _write_readonly(inputs / "panel.parquet", "PAR1golden-panelPAR1")
    receipt = _write_readonly(inputs / "receipt.json", '{"contract":"golden-receipt"}')
    dividends = _write_readonly(inputs / "dividends.json", '{"contract":"trusted-dividends-v2"}')
    spec = inputs / "spec.json"
    spec.write_text(json.dumps(_payload()), encoding="utf-8")
    specs = (
        _write_readonly(inputs / "eval-c000.json", '{"cost_bps":1}'),
        _write_readonly(
            inputs / "eval-c001.json",
            '{"cost_bps":3,"golden_fail":true}' if failing_second_spec else '{"cost_bps":3}',
        ),
    )
    spec_set = inputs / "evaluation-spec-set.json"
    spec_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0001",
            "c000",
            tuple(
                EvaluationSpecEntry(
                    f"c{index:03d}", str(path), hashlib.sha256(path.read_bytes()).hexdigest()
                )
                for index, path in enumerate(specs)
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )

    source = tmp_path / "worktree"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "golden@example.invalid")
    _git(source, "config", "user.name", "Golden Run")
    (source / "tracked.txt").write_text("tracked", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "base")
    base_commit = _git(source, "rev-parse", "HEAD")

    hypothesis = store.create_hypothesis(
        "golden",
        spec,
        panel,
        receipt,
        specs[0],
        base_commit,
        dividends=dividends,
        evaluation_spec_set=spec_set,
    )
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )

    # The implementation: a real committed target script and analysis module.
    for name in ("golden_target.py", "golden_analysis.py"):
        shutil.copyfile(FIXTURES / name, source / name)
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "golden implementation")
    commit = _git(source, "rev-parse", "HEAD")

    def targets_argv(scenario_id: str, variant: str) -> tuple[str, ...]:
        return (
            str(venv / "bin" / "python"),
            str(source / "golden_target.py"),
            "--panel",
            hypothesis.panel_path,
            "--receipt",
            hypothesis.receipt_path,
            "--variant",
            variant,
            "--out",
            str(run_dir / "scenarios" / scenario_id / "targets-stage" / "targets.json"),
        )

    record = ImplementationRecord(
        attempt.attempt_id,
        commit,
        targets_argv("s000", "baseline"),
        "/tmp/evidence.json",
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    set_value = store.evaluation_spec_set(hypothesis.hypothesis_id)
    entries = {entry.spec_id: entry for entry in set_value.specs}
    plan = RunPlan(
        "research-run-plan-v1",
        attempt.attempt_id,
        commit,
        hashlib.sha256(record.to_json().encode()).hexdigest(),
        hashlib.sha256(set_value.to_json().encode()).hexdigest(),
        "s000",
        (
            RunScenario("s000", record.targets_argv, "c000", entries["c000"].sha256),
            RunScenario("s001", targets_argv("s001", "wider"), "c001", entries["c001"].sha256),
        ),
        AnalysisPlan("golden_analysis", (), ("analysis/result.json",), 1 << 20),
        SCENARIO_TIMEOUT_SECONDS,
        ANALYSIS_TIMEOUT_SECONDS,
    )
    store.submit_implementation(
        attempt.attempt_id,
        record,
        run_plan=plan,
        containment_provenance=provenance_evidence(attempt.attempt_id, commit),
    )
    verified_review(store, review(attempt.attempt_id, commit, hypothesis.spec_sha256))
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.REVIEW_PASSED

    # Synthetic typed admission and native readiness record, as in the dispatch tests.
    decision = _admit(
        _document(_payload()),
        evaluation_spec_set_sha256=hashlib.sha256(set_value.to_json().encode()).hexdigest(),
    )
    store.insert_admission_decision(attempt.attempt_id, decision)
    monkeypatch.setattr(research_cli, "_admission_for_hypothesis", lambda *_args: decision)
    configure_real_readiness(store, tmp_path, monkeypatch)
    return Golden(store, source, hypothesis, attempt.attempt_id, plan, run_dir, panel, receipt)


def _call(root: Path, *args: str, expect: int = 0) -> Result:
    result = runner.invoke(app, ["research", *args, "--root", str(root)])
    assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result


def _queue_via_cli(golden: Golden) -> str:
    """Queue through the real ``research run`` command with no ``--timeout-seconds``."""
    result = _call(golden.store.root, "run", golden.attempt_id, "--no-wait")
    assert "state=QUEUED" in result.output
    assert golden.store.get_attempt(golden.attempt_id).state == AttemptState.RUN_QUEUED
    row = golden.store.job_for(golden.attempt_id)
    assert row is not None and row["state"] == JobState.QUEUED.value
    payload = json.loads(row["payload_json"])
    # The plan-derived timeout and the frozen hypothesis compute bound, not a flag.
    # Literal derivation: 30 s x (2 scenarios x 2 stages + 2 validate specs) + 30 s analysis
    # = 210 s stage budget, plus the fixed 300 s driver overhead = 510 s.
    assert payload["timeout_seconds"] == 510.0
    assert payload["max_rss_mb"] == 1024
    assert not (golden.run_dir / "job.json").exists()
    return str(row["job_id"])


def _serve_once(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run one real serve loop body; only the wake/poll network edges are stubbed."""
    monkeypatch.setattr(research_cli, "OpenClawWakeSender", lambda *_a: object())
    monkeypatch.setattr(research_cli, "compose_wake", lambda _store: None)
    monkeypatch.setattr(research_cli, "poll_owner_turn", lambda *_a: None)
    _call(root, "serve", "--session-key", "agent:research-orchestrator:golden", "--once")


def _drive_to_terminal(golden: Golden, monkeypatch: pytest.MonkeyPatch, job_id: str) -> float:
    """Serve the queued job through the real worker until the host records the outcome."""
    store = golden.store
    started = time.monotonic()
    try:
        _serve_once(store.root, monkeypatch)
        launched = store.get_attempt(golden.attempt_id)
        row = store.job_for(golden.attempt_id)
        latest_event = store.events()[-1] if store.events() else None
        context = (
            f"state={launched.state.value} job_row="
            f"{dict(row) if row is not None else None} latest_event={latest_event}"
        )
        assert launched.state == AttemptState.RUNNING, context
        assert row is not None and row["state"] == JobState.LAUNCHED.value, context
        payload = json.loads(row["payload_json"])
        assert payload["job_id"] == job_id and int(payload["worker_pid"]) > 0, context
        deadline = started + WAIT_BUDGET_SECONDS
        while store.get_attempt(golden.attempt_id).state == AttemptState.RUNNING:
            if time.monotonic() > deadline:
                worker_log = golden.run_dir / "logs" / "worker.log"
                detail = worker_log.read_text() if worker_log.is_file() else "<no worker log>"
                pytest.fail(f"golden run did not finish in {WAIT_BUDGET_SECONDS}s: {detail}")
            time.sleep(0.25)
            _serve_once(store.root, monkeypatch)
    finally:
        # Never leak a detached worker; a cancel error must not mask the original failure.
        try:
            if store.get_attempt(golden.attempt_id).state == AttemptState.RUNNING:
                current = store.job_for(golden.attempt_id)
                if current is not None:
                    jobs.cancel(research_cli._job_from_row(current))
        except Exception as exc:
            print(f"golden cleanup: worker cancel failed: {exc!r}", file=sys.stderr)
    return time.monotonic() - started


def _run_evidence(golden: Golden) -> dict[str, object]:
    loaded = json.loads((golden.run_dir / "run-evidence.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_success_runs_queue_launch_worker_verify_close_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=False)
    store = golden.store
    job_id = _queue_via_cli(golden)

    elapsed = _drive_to_terminal(golden, monkeypatch, job_id)

    assert elapsed < 60, f"golden success run took {elapsed:.1f}s"
    attempt = store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.RUN_SUCCEEDED, attempt.run_outcome
    assert attempt.run_outcome is not None
    outcome = json.loads(attempt.run_outcome)
    assert outcome["status"] == "succeeded"
    canonical = golden.run_dir / "scenarios" / "s000" / "evaluator-stage" / "out" / "result.json"
    assert outcome["result_path"] == str(canonical)
    assert outcome["compliant"] is True and outcome["metrics_available"] is True
    row = store.job_for(golden.attempt_id)
    assert row is not None and row["state"] == JobState.EXITED.value

    # Real producer output: the worker records no per-scenario status on success.
    terminal = json.loads((golden.run_dir / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "succeeded"
    evidence = _run_evidence(golden)
    assert evidence["status"] == "succeeded"
    assert evidence["completed_scenarios"] == ["s000", "s001"]
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict) and set(scenarios) == {"s000", "s001"}
    for scenario_id, scenario in scenarios.items():
        assert isinstance(scenario, dict)
        assert "status" not in scenario, scenario_id
        assert scenario["targets_exit"] == 0 and scenario["evaluator_exit"] == 0
    stage_names = [str(stage["stage"]) for stage in evidence["stages"]]  # type: ignore[attr-defined]
    # Tie the plan budget to the stages the worker actually ran: every non-analysis
    # stage may spend one scenario timeout, plus one analysis timeout.
    assert golden.plan.stage_budget_seconds == (
        SCENARIO_TIMEOUT_SECONDS * (len(stage_names) - 1) + ANALYSIS_TIMEOUT_SECONDS
    )
    assert stage_names == [
        "validate-c000",
        "validate-c001",
        "targets-s000",
        "evaluate-s000",
        "targets-s001",
        "evaluate-s001",
        "analysis",
    ]
    for scenario_id in ("s000", "s001"):
        scenario_dir = golden.run_dir / "scenarios" / scenario_id
        targets = json.loads((scenario_dir / "targets-stage" / "targets.json").read_text())
        assert targets["work_writable"] is False  # /work was read-only in the sandbox
        assert targets["panel_sha256"] == golden.hypothesis.panel_sha256
        assert sorted(
            path.name for path in (scenario_dir / "evaluator-stage" / "out").iterdir()
        ) == [
            "daily.parquet",
            "result.json",
            "trades.parquet",
        ]
        assert list((scenario_dir / "targets-stage" / "provenance").glob("*.json"))
        assert list((scenario_dir / "evaluator-stage" / "provenance").glob("*.json"))
    analysis = json.loads(
        (golden.run_dir / "analysis-stage" / "analysis" / "result.json").read_text()
    )
    assert set(analysis["scenarios"]) == {"s000", "s001"}
    assert "evaluation-specs/c001.json" in analysis["inputs"]
    assert list((golden.run_dir / "analysis-stage" / "provenance").glob("*.json"))

    # Host projection: no boundary failure and no operator re-verification involved.
    status = read_status(store.root, unit_state=None)
    assert status.boundary_failure is None
    assert status.attempt_state == "RUN_SUCCEEDED"
    assert all(event.kind != "run_reverified" for event in store.events())

    # Close and decide through the CLI.
    closed = _call(
        store.root, "attempt-close", golden.attempt_id, "--decision", "FINISH", "--reason", "golden"
    )
    assert closed.output.strip() == "CLOSED"
    assert store.get_attempt(golden.attempt_id).state == AttemptState.CLOSED
    decided = _call(
        store.root,
        "hypothesis-decide",
        golden.hypothesis.hypothesis_id,
        "--decision",
        "FINISHED",
        "--reason",
        "golden",
    )
    assert decided.output.strip() == "DECIDED"
    assert store.get_hypothesis(golden.hypothesis.hypothesis_id).state == HypothesisState.DECIDED
    assert _git(golden.source, "status", "--porcelain") == ""


def test_golden_genuine_evaluator_failure_is_a_worker_reported_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=True)
    store = golden.store
    job_id = _queue_via_cli(golden)

    elapsed = _drive_to_terminal(golden, monkeypatch, job_id)

    assert elapsed < 60, f"golden failure run took {elapsed:.1f}s"
    attempt = store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.RUN_FAILED, attempt.run_outcome
    assert attempt.run_outcome is not None
    outcome = json.loads(attempt.run_outcome)
    # The worker's own failure status survives verification; it is not a host mismatch.
    assert outcome["status"] == "scenario_failed"
    assert outcome["status"] != "run_evidence_mismatch"
    assert outcome["result_path"] == ""
    terminal = json.loads((golden.run_dir / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "scenario_failed"
    evidence = _run_evidence(golden)
    assert evidence["status"] == "scenario_failed"
    assert evidence["completed_scenarios"] == ["s000"]
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    assert "status" not in scenarios["s000"]
    assert scenarios["s001"]["status"] == "failed"
    assert "evaluator failed for s001" in str(evidence["error"])
    assert not (golden.run_dir / "analysis-stage").exists()
    status = read_status(store.root, unit_state=None)
    assert status.boundary_failure == "scenario_failed"
    assert all(event.kind != "run_reverified" for event in store.events())
