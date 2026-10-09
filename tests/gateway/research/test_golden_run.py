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
import importlib.util
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
from gateway.research.admission import (
    BORROW_COST_MISSING_REASON,
    COMMON_STOCK_REFUSAL,
    NO_SHORTABLE_INSTRUMENT_REASON,
)
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
from gateway.research.hypothesis import MAX_HOLDING_SESSIONS
from gateway.research.status import read_status
from gateway.research.store import ResearchStore
from typer.testing import CliRunner, Result

from tests.gateway.research.conftest import provenance_evidence, review, verified_review
from tests.gateway.research.stock_fixtures import (
    daily_receipt_text,
    earnings_bytes,
    membership_bytes,
    stock_eval_spec_text,
    stock_payload,
)
from tests.gateway.research.test_admission import (
    _admit,
    _bounds,
    _document,
    _payload,
    _set_events,
)
from tests.gateway.research.test_readiness import configure_real_readiness

FIXTURES = Path(__file__).parent / "fixtures" / "golden"
LEDGER_FIXTURE = Path(__file__).parent / "fixtures" / "exposure-ledger.json"
HEDGED_SESSIONS = 20
HEDGED_BORROW_BPS = 50.0
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
    path.unlink(missing_ok=True)  # a variant may replace an earlier read-only default
    path.write_text(text, encoding="utf-8")
    path.chmod(0o444)
    return path


@dataclass(frozen=True)
class GoldenDraft:
    """A configured store with one DRAFT golden hypothesis (not yet probed or frozen)."""

    store: ResearchStore
    source: Path
    hypothesis: HypothesisSpec
    venv: Path
    panel: Path
    receipt: Path
    specs: tuple[Path, ...]
    earnings: Path | None = None
    membership: Path | None = None


def _hedged_payload() -> dict[str, object]:
    """The golden hypothesis with a 20-session forward label (above the old five-session cap)."""
    payload = _payload()
    payload["forward_label_sessions"] = HEDGED_SESSIONS
    # 262 evaluation sessions x 1 instrument / 20 = 13 non-overlapping events at most.
    _set_events(payload, 10)
    return payload


def _borrow_bps(cost_bps: int) -> float:
    """c000 (cost 1x) carries the base borrow; c001 (cost 3x) scales it with every cost."""
    return HEDGED_BORROW_BPS * cost_bps


def _spec_text(cost_bps: int, *, failing: bool, hedged: bool) -> str:
    """Golden evaluator spec; ``hedged`` makes it a v3 long-short spec with a 20-session hold."""
    document: dict[str, object] = {"cost_bps": cost_bps}
    if failing:
        document["golden_fail"] = True
    if hedged:
        document.update(
            {
                "instruments": [{"ticker": "SPY", "instrument_class": "etf", "shortable": True}],
                "long_only": False,
                "holding": {"max_sessions": HEDGED_SESSIONS},
                "costs": {"borrow_bps_annual": _borrow_bps(cost_bps)},
            }
        )
    return json.dumps(document, separators=(",", ":"))


def golden_draft(
    tmp_path: Path,
    *,
    failing_second_spec: bool = False,
    failing_first_spec: bool = False,
    hedged: bool = False,
    stock: bool = False,
    with_earnings: bool = True,
) -> GoldenDraft:
    """Configure real runtime pins and create (without freezing) the golden hypothesis.

    ``stock`` swaps in a daily-panel receipt, a membership file, an earnings snapshot and a
    real-parseable evaluator spec with one ``common_stock`` instrument; ``with_earnings=False``
    omits the snapshot so create must refuse.
    """
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
    spec.write_text(json.dumps(_hedged_payload() if hedged else _payload()), encoding="utf-8")
    specs = (
        _write_readonly(
            inputs / "eval-c000.json",
            _spec_text(1, failing=failing_first_spec, hedged=hedged),
        ),
        _write_readonly(
            inputs / "eval-c001.json",
            _spec_text(3, failing=failing_second_spec, hedged=hedged),
        ),
    )
    earnings: Path | None = None
    membership: Path | None = None
    if stock:
        membership_data = membership_bytes()
        membership = _write_readonly(inputs / "membership.json", membership_data.decode())
        if with_earnings:
            earnings = _write_readonly(inputs / "earnings.json", earnings_bytes().decode())
        receipt = _write_readonly(
            inputs / "receipt.json",
            daily_receipt_text(
                hashlib.sha256(panel.read_bytes()).hexdigest(),
                hashlib.sha256(membership_data).hexdigest(),
            ),
        )
        spec.write_text(json.dumps(stock_payload()), encoding="utf-8")
        specs = (
            _write_readonly(inputs / "eval-c000.json", stock_eval_spec_text(1)),
            _write_readonly(inputs / "eval-c001.json", stock_eval_spec_text(3)),
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
        earnings=earnings,
        membership=membership,
    )
    return GoldenDraft(store, source, hypothesis, venv, panel, receipt, specs, earnings, membership)


def freeze_golden(draft: GoldenDraft) -> None:
    """Record a REAL compute probe (real containment) via the CLI, then freeze."""
    result = _call(draft.store.root, "compute-probe", draft.hypothesis.hypothesis_id)
    probe = json.loads(result.output)
    assert probe["requirements"]["min_rss_mb"] <= 1024, probe
    assert probe["requirements"]["min_scenario_timeout_seconds"] <= SCENARIO_TIMEOUT_SECONDS, probe
    draft.store.freeze(draft.hypothesis.hypothesis_id)


def _build_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_second_spec: bool,
    hedged: bool = False,
    stock: bool = False,
) -> Golden:
    """Configure, freeze, implement, review and admit a two-scenario attempt.

    The stock variant admits through the REAL typed admission wiring (daily receipt,
    membership, bound earnings snapshot); every other variant uses the synthetic decision.
    """
    draft = golden_draft(
        tmp_path, failing_second_spec=failing_second_spec, hedged=hedged, stock=stock
    )
    store, source, hypothesis, venv = draft.store, draft.source, draft.hypothesis, draft.venv
    panel, receipt = draft.panel, draft.receipt
    freeze_golden(draft)
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

    # Hedged variants short SPY, so the evaluator only accepts them under a hedged spec.
    baseline, wider = ("hedged", "hedged-wider") if hedged else ("baseline", "wider")

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
        targets_argv("s000", baseline),
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
            RunScenario("s001", targets_argv("s001", wider), "c001", entries["c001"].sha256),
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

    if stock:
        configure_real_readiness(store, tmp_path, monkeypatch)
        store.register_exposure_ledger(
            LEDGER_FIXTURE, hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
        )
        real = research_cli._admission_for_hypothesis(store, hypothesis.hypothesis_id)
        assert real.admitted, real.detail
        store.insert_admission_decision(attempt.attempt_id, real)
        return Golden(store, source, hypothesis, attempt.attempt_id, plan, run_dir, panel, receipt)

    # Synthetic typed admission and native readiness record, as in the dispatch tests.
    decision = _admit(
        _document(_hedged_payload() if hedged else _payload()),
        bounds=(
            _bounds(
                max_holding=HEDGED_SESSIONS,
                long_only=False,
                shortable=True,
                borrow_bps_annual=HEDGED_BORROW_BPS,
            )
            if hedged
            else None
        ),
        evaluation_spec_set_sha256=hashlib.sha256(set_value.to_json().encode()).hexdigest(),
    )
    assert decision.admitted, decision.detail
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


def test_golden_hedged_twenty_session_spec_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v3 spec (long_only false, one shortable instrument, 20 sessions) is accepted throughout.

    Create (spec-set bounds), compute probe and freeze, typed admission, submit, run, host
    verification, close and decide all run on the hedged spec; the fixture evaluator emits
    ``research-evaluator-v3`` with ``dividend_payable_total`` and the new daily columns.
    """
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=False, hedged=True)
    store = golden.store
    assert json.loads(golden.hypothesis.spec_json)["forward_label_sessions"] == HEDGED_SESSIONS
    job_id = _queue_via_cli(golden)

    _drive_to_terminal(golden, monkeypatch, job_id)

    attempt = store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.RUN_SUCCEEDED, attempt.run_outcome
    assert attempt.run_outcome is not None
    assert json.loads(attempt.run_outcome)["status"] == "succeeded"
    for scenario_id, cost_bps in (("s000", 1), ("s001", 3)):
        scenario_dir = golden.run_dir / "scenarios" / scenario_id
        targets = json.loads((scenario_dir / "targets-stage" / "targets.json").read_text())
        weights = [item["weight"] for item in targets["positions"]]
        assert weights == [-0.3, -0.5]  # the run really carried a short SPY target
        out = scenario_dir / "evaluator-stage" / "out"
        result = json.loads((out / "result.json").read_text(encoding="utf-8"))
        assert result["evaluator_version"] == "research-evaluator-v3"
        assert result["dividend_payable_total"] == 0.0
        # Borrow follows the 0.8 total short weight at the scenario's own annual rate.
        borrow = round(0.8 * _borrow_bps(cost_bps) / 1e4 / 252, 12)
        daily = (out / "daily.parquet").read_bytes()
        assert f":net_exposure=-0.4:borrow_costs={borrow}".encode() in daily
    _call(
        store.root, "attempt-close", golden.attempt_id, "--decision", "FINISH", "--reason", "golden"
    )
    _call(
        store.root,
        "hypothesis-decide",
        golden.hypothesis.hypothesis_id,
        "--decision",
        "FINISHED",
        "--reason",
        "golden",
    )
    assert store.get_hypothesis(golden.hypothesis.hypothesis_id).state == HypothesisState.DECIDED


def _fake_evaluator(
    tmp_path: Path, spec: dict[str, object], weight: float
) -> subprocess.CompletedProcess[str]:
    """Run the golden fake evaluator directly on one SPY target of ``weight``."""
    files = {
        "spec.json": json.dumps(spec),
        "targets.json": json.dumps(
            {"positions": [{"date": "2024-01-02", "symbol": "SPY", "weight": weight}]}
        ),
        "dividends.json": "{}",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    stub = tmp_path / "stub" / "quantipy"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(FIXTURES / "golden_evaluator.py"),
            "research",
            "evaluate",
            "--spec",
            str(tmp_path / "spec.json"),
            "--targets",
            str(tmp_path / "targets.json"),
            "--dividends",
            str(tmp_path / "dividends.json"),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "stub")},
    )


def _hedged_spec(**changes: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "cost_bps": 1,
        "instruments": [{"ticker": "SPY", "instrument_class": "etf", "shortable": True}],
        "long_only": False,
        "holding": {"max_sessions": 20},
        "costs": {"borrow_bps_annual": 50.0},
    }
    spec.update(changes)
    return spec


@pytest.mark.parametrize(
    "spec",
    [
        {"cost_bps": 1},
        _hedged_spec(long_only=True),
        _hedged_spec(instruments=[{"ticker": "SPY", "instrument_class": "etf"}, {"ticker": "X"}]),
        _hedged_spec(instruments=[{"ticker": "XLF", "instrument_class": "etf", "shortable": True}]),
    ],
)
def test_fake_evaluator_refuses_a_short_target_unless_hedged_and_shortable(
    tmp_path: Path, spec: dict[str, object]
) -> None:
    completed = _fake_evaluator(tmp_path, spec, -0.3)

    assert completed.returncode == 1
    assert "SHORT_NOT_PERMITTED" in completed.stderr or "no instrument is shortable" in (
        completed.stderr
    )


def test_fake_evaluator_accepts_a_short_target_under_a_hedged_spec(tmp_path: Path) -> None:
    completed = _fake_evaluator(tmp_path, _hedged_spec(), -0.3)

    assert completed.returncode == 0, completed.stderr
    daily = (tmp_path / "out" / "daily.parquet").read_bytes()
    assert f"borrow_costs={round(0.3 * 50.0 / 1e4 / 252, 12)}".encode() in daily


@pytest.mark.parametrize("costs", [{}, {"borrow_bps_annual": 0.0}])
def test_fake_evaluator_refuses_a_hedged_spec_without_borrow(
    tmp_path: Path, costs: dict[str, object]
) -> None:
    completed = _fake_evaluator(tmp_path, _hedged_spec(costs=costs), -0.3)

    assert completed.returncode == 1
    assert "costs.borrow_bps_annual is not set above zero" in completed.stderr


def test_fake_evaluator_mirrors_the_gateway_constants() -> None:
    fixture = importlib.util.spec_from_file_location(
        "golden_evaluator", FIXTURES / "golden_evaluator.py"
    )
    assert fixture is not None and fixture.loader is not None
    module = importlib.util.module_from_spec(fixture)
    fixture.loader.exec_module(module)

    assert module.MAX_SESSIONS == MAX_HOLDING_SESSIONS
    assert module.BORROW_COST_MISSING_REASON == BORROW_COST_MISSING_REASON
    assert module.NO_SHORTABLE_REASON == NO_SHORTABLE_INSTRUMENT_REASON
    assert module.COMMON_STOCK_REFUSAL == COMMON_STOCK_REFUSAL


def test_golden_stock_daily_run_binds_earnings_and_membership_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One common_stock instrument on a daily panel, with an earnings snapshot and membership.

    create (refusals and bindings) -> real compute probe -> freeze -> REAL typed admission
    (``SNAPSHOT_BOUND`` earnings, daily receipt superset universe) -> submit -> queue ->
    detached worker with the two extra read-only mounts -> host verification -> close/decide.
    """
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=False, stock=True)
    store = golden.store
    hypothesis_id = golden.hypothesis.hypothesis_id
    bindings = store.input_bindings(hypothesis_id)
    assert set(bindings) == {"earnings", "membership"}
    earnings_digest = bindings["earnings"].sha256
    membership_digest = bindings["membership"].sha256
    probe = store.compute_probe(hypothesis_id)
    assert probe is not None
    assert probe.inputs["earnings_sha256"] == earnings_digest
    assert probe.inputs["membership_sha256"] == membership_digest
    job_id = _queue_via_cli(golden)
    row = store.job_for(golden.attempt_id)
    assert row is not None
    queued = json.loads(row["payload_json"])
    assert queued["artifact_digests"]["earnings"] == earnings_digest
    assert queued["artifact_digests"]["membership"] == membership_digest
    assert queued["artifact_paths"]["earnings"] == bindings["earnings"].path

    _drive_to_terminal(golden, monkeypatch, job_id)

    attempt = store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.RUN_SUCCEEDED, attempt.run_outcome
    assert attempt.run_outcome is not None
    assert json.loads(attempt.run_outcome)["status"] == "succeeded"
    validated = json.loads((golden.run_dir / "validated-inputs.json").read_text(encoding="utf-8"))
    assert validated["earnings_sha256"] == earnings_digest
    assert validated["membership_sha256"] == membership_digest
    for scenario_id in ("s000", "s001"):
        out = golden.run_dir / "scenarios" / scenario_id / "evaluator-stage" / "out"
        result = json.loads((out / "result.json").read_text(encoding="utf-8"))
        # The fake hashed the files it read from /inputs inside the sandbox.
        assert result["earnings_sha256"] == earnings_digest
        assert result["membership_sha256"] == membership_digest
        assert result["acceptance_class"] == "exploratory_snapshot"
        assert result["delisting_policy"] == {"long_haircut": 0.3, "max_stale_sessions": 5}
    validation = json.loads((golden.run_dir / "logs" / "validate-c000.out").read_text())
    assert validation["earnings_sha256"] == earnings_digest
    analysis = json.loads(
        (golden.run_dir / "analysis-stage" / "analysis" / "result.json").read_text()
    )
    assert not any("earnings" in name or "membership" in name for name in analysis["inputs"])
    _call(
        store.root, "attempt-close", golden.attempt_id, "--decision", "FINISH", "--reason", "golden"
    )
    _call(
        store.root,
        "hypothesis-decide",
        hypothesis_id,
        "--decision",
        "FINISHED",
        "--reason",
        "golden",
    )
    assert store.get_hypothesis(hypothesis_id).state == HypothesisState.DECIDED


def test_golden_stock_spec_without_earnings_is_refused_at_create(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as refused:
        golden_draft(tmp_path, stock=True, with_earnings=False)

    assert COMMON_STOCK_REFUSAL in str(refused.value)
    assert "--earnings" in str(refused.value)
    store = ResearchStore(tmp_path / "driver")
    assert store.hypotheses() == []
    recorded = [event for event in store.events() if event.kind == "hypothesis_create_refused"]
    assert len(recorded) == 1 and COMMON_STOCK_REFUSAL in recorded[0].detail


def _requeue_payload(golden: Golden, change: dict[str, object]) -> None:
    """Rewrite the queued job payload in this disposable store (a corrupted queue row)."""
    row = golden.store.job_for(golden.attempt_id)
    assert row is not None
    payload = json.loads(row["payload_json"])
    for key, value in change.items():
        payload[key] = value
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with golden.store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET payload_json=?,payload_sha256=? WHERE job_id=?",
            (text, hashlib.sha256(text.encode()).hexdigest(), row["job_id"]),
        )


def test_golden_stock_dispatch_refuses_a_queue_row_missing_a_bound_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=False, stock=True)
    _queue_via_cli(golden)
    row = golden.store.job_for(golden.attempt_id)
    assert row is not None
    payload = json.loads(row["payload_json"])
    paths = {k: v for k, v in payload["artifact_paths"].items() if k != "earnings"}
    digests = {k: v for k, v in payload["artifact_digests"].items() if k != "earnings"}
    _requeue_payload(golden, {"artifact_paths": paths, "artifact_digests": digests})

    _serve_once(golden.store.root, monkeypatch)

    attempt = golden.store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.RUN_FAILED
    assert attempt.run_outcome is not None
    assert json.loads(attempt.run_outcome)["status"] == "input_binding_mismatch"


def test_golden_stock_dispatch_refuses_an_unbound_extra_input_in_the_queue_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=False)
    _queue_via_cli(golden)
    row = golden.store.job_for(golden.attempt_id)
    assert row is not None
    payload = json.loads(row["payload_json"])
    extra = golden.panel  # any real file: the hypothesis binds no earnings input
    _requeue_payload(
        golden,
        {
            "artifact_paths": {**payload["artifact_paths"], "earnings": str(extra)},
            "artifact_digests": {
                **payload["artifact_digests"],
                "earnings": hashlib.sha256(extra.read_bytes()).hexdigest(),
            },
        },
    )

    _serve_once(golden.store.root, monkeypatch)

    attempt = golden.store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.RUN_FAILED
    assert attempt.run_outcome is not None
    assert json.loads(attempt.run_outcome)["status"] == "input_binding_mismatch"


def test_golden_stock_dispatch_refuses_a_bound_input_changed_after_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _build_golden(tmp_path, monkeypatch, failing_second_spec=False, stock=True)
    _queue_via_cli(golden)
    earnings = Path(golden.store.input_bindings(golden.hypothesis.hypothesis_id)["earnings"].path)
    earnings.chmod(0o644)
    earnings.write_text("{}", encoding="utf-8")

    _serve_once(golden.store.root, monkeypatch)

    # Typed admission re-reads the bound snapshot and fails closed before any launch.
    attempt = golden.store.get_attempt(golden.attempt_id)
    assert attempt.state == AttemptState.REVIEW_PASSED
    refusal = golden.store.latest_admission_refusal(golden.hypothesis.hypothesis_id)
    assert refusal is not None and refusal[0] == "EARNINGS_DIGEST_MISMATCH"
    assert not (golden.run_dir / "job.json").exists()
