from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from gateway.cli import app
from gateway.openclaw_client import OpenClawTransportError
from gateway.research import cli as research_cli
from gateway.research.contracts import (
    AnalysisPlan,
    Attempt,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisDecision,
    ImplementationRecord,
    JobState,
    RunPlan,
    RunScenario,
)
from gateway.research.jobs import JobError, JobRecord, _starttime
from gateway.research.provenance import ProvenanceError
from gateway.research.store import ResearchStore
from gateway.research.wake import compose_wake
from typer.testing import CliRunner, Result

from tests.gateway.research.conftest import (
    freeze_with_probe,
    provenance_evidence,
    review,
    run_plan,
    verified_review,
)
from tests.gateway.research.test_admission import _admit, _document, _payload
from tests.gateway.research.test_readiness import configure_real_readiness

runner = CliRunner()


def _install_dispatch_admission(
    store: ResearchStore, attempt: Attempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_set_digest = hashlib.sha256(
        store.evaluation_spec_set(attempt.hypothesis_id).to_json().encode()
    ).hexdigest()
    decision = _admit(_document(_payload()), evaluation_spec_set_sha256=spec_set_digest)
    store.insert_admission_decision(attempt.attempt_id, decision)
    monkeypatch.setattr(research_cli, "_admission_for_hypothesis", lambda *_args: decision)


def _run_plan_bindings(store: ResearchStore, attempt_id: str) -> tuple[str, str]:
    plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    return hashlib.sha256(plan.to_json().encode()).hexdigest(), plan.evaluation_spec_set_sha256


def _fake_worker_evidence(
    run_dir: Path,
    *,
    job_id: str,
    attempt_id: str,
    status: str,
    run_plan_sha256: str,
    evaluation_spec_set_sha256: str,
    scenario_spec_sha256: str | None = None,
    result_path: Path | None = None,
) -> None:
    scenarios: dict[str, object] = {}
    if result_path is not None:
        scenario_dir = run_dir / "scenarios" / "s000"
        target_path = scenario_dir / "targets-stage" / "targets.json"
        trades_path = scenario_dir / "evaluator-stage" / "out" / "trades.parquet"
        daily_path = scenario_dir / "evaluator-stage" / "out" / "daily.parquet"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("{}", encoding="utf-8")
        trades_path.parent.mkdir(parents=True, exist_ok=True)
        trades_path.write_bytes(b"trades")
        daily_path.write_bytes(b"daily")
        scenarios["s000"] = {
            "status": "succeeded",
            "spec_id": "c000",
            "spec_sha256": scenario_spec_sha256 or "0" * 64,
            "targets_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
            "targets_exit": 0,
            "evaluator_exit": 0,
            "result_path": str(result_path),
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "trades_sha256": hashlib.sha256(trades_path.read_bytes()).hexdigest(),
            "daily_sha256": hashlib.sha256(daily_path.read_bytes()).hexdigest(),
        }
    validated_inputs = run_dir / "validated-inputs.json"
    validated_inputs.write_text('{"fixture":true}', encoding="utf-8")
    analysis = run_dir / "analysis-stage" / "analysis" / "result.json"
    analysis.parent.mkdir(parents=True, exist_ok=True)
    analysis.write_text("{}", encoding="utf-8")
    evidence = {
        "contract": "research-run-evidence-v1",
        "status": status,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "run_plan_sha256": run_plan_sha256,
        "evaluation_spec_set_sha256": evaluation_spec_set_sha256,
        "primary_scenario_id": "s000",
        "validated_inputs_sha256": hashlib.sha256(validated_inputs.read_bytes()).hexdigest(),
        "scenarios": scenarios,
        "completed_scenarios": ["s000"] if status == "succeeded" else [],
        "analysis": {"analysis/result.json": hashlib.sha256(analysis.read_bytes()).hexdigest()},
        "analysis_exit": 0,
        "checks": [
            {"name": "source_before", "value": "fixture", "ok": True},
            {"name": "source_after", "value": "fixture", "ok": True},
        ],
        "stages": [],
    }
    evidence_path = run_dir / "run-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    (run_dir / "terminal.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "worker_pid": 0,
                "worker_starttime": 0,
                "status": status,
                "targets_exit": 0,
                "evaluator_exit": 0,
                "run_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )


def _call(root: Path, *args: str, expect: int = 0) -> Result | Any:
    result = runner.invoke(app, ["research", *args, "--root", str(root)])
    assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result


def _ready(
    store: ResearchStore,
    source: Path,
    hypothesis: Any,
    *,
    scenarios: tuple[RunScenario, ...] | None = None,
) -> Attempt:
    freeze_with_probe(store, hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    record = ImplementationRecord(
        attempt.attempt_id,
        commit,
        (sys.executable, "-m", "fixture_target"),
        "/tmp/evidence.json",
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    store.submit_implementation(
        attempt.attempt_id,
        record,
        run_plan=run_plan(store, attempt, record, scenarios=scenarios),
        containment_provenance=provenance_evidence(attempt.attempt_id, record.commit),
    )
    verified_review(store, review(attempt.attempt_id, commit, hypothesis.spec_sha256))
    return store.get_attempt(attempt.attempt_id)


def test_implementation_submit_validates_provenance_before_persisting(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    record = ImplementationRecord(
        attempt.attempt_id,
        commit,
        (sys.executable, "-m", "fixture_target"),
        "/tmp/evidence.json",
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    implementation_path = tmp_path / "implementation.json"
    run_plan_path = tmp_path / "run-plan.json"
    implementation_path.write_text(record.to_json(), encoding="utf-8")
    plan = run_plan(store, attempt, record)
    run_plan_path.write_text(plan.to_json(), encoding="utf-8")

    def reject_provenance(*_args: object) -> str:
        raise ProvenanceError("invalid provenance")

    monkeypatch.setattr(research_cli, "validate_provenance_evidence", reject_provenance)
    result = runner.invoke(
        app,
        [
            "research",
            "implementation-submit",
            attempt.attempt_id,
            "--root",
            str(store.root),
            "--file",
            str(implementation_path),
            "--run-plan",
            str(run_plan_path),
            "--provenance-evidence",
            str(tmp_path / "provenance.json"),
        ],
    )

    assert result.exit_code == 1
    assert "invalid provenance" in result.output
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.OPENED


def _queue(
    store: ResearchStore,
    attempt_id: str,
    job_id: str = "job-cli-test",
    *,
    timeout_seconds: int = 30,
) -> None:
    attempt = store.get_attempt(attempt_id)
    run_dir = store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id / "run"
    plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    store.queue_run_request(attempt_id, job_id, run_dir, timeout_seconds, 256, run_plan=plan)


def _verified(store: ResearchStore, attempt_id: str) -> None:
    import hashlib

    for kind in ("host_execution", "native_execution"):
        payload = '{"verified":true}'
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                (attempt_id, kind, payload, hashlib.sha256(payload.encode()).hexdigest()),
            )
            conn.commit()


def _canonical_terminal_fixture(
    campaign: tuple[ResearchStore, Path, Any],
) -> tuple[ResearchStore, Attempt, RunPlan, Path, dict[str, object], dict[str, object]]:
    """Build the complete evidence shape emitted by the contained worker."""
    store, source, hypothesis = campaign
    first = RunScenario(
        "s000",
        (sys.executable, "-m", "fixture_target"),
        "c000",
        hashlib.sha256(b"eval").hexdigest(),
    )
    second = RunScenario(
        "s001",
        (sys.executable, "-m", "fixture_target"),
        "c000",
        first.evaluation_spec_sha256,
    )
    attempt = _ready(store, source, hypothesis, scenarios=(first, second))
    _queue(store, attempt.attempt_id, job_id="job-terminal-canonical", timeout_seconds=60)
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    plan_digest = hashlib.sha256(plan.to_json().encode()).hexdigest()
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    validated_inputs = run_dir / "validated-inputs.json"
    validated_inputs.write_text(
        json.dumps({"specs": {"c000": hashlib.sha256(b"semantic-c000").hexdigest()}}),
        encoding="utf-8",
    )
    analysis_path = run_dir / "analysis-stage" / "analysis" / "result.json"
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text('{"summary":"fixture"}', encoding="utf-8")
    scenarios: dict[str, object] = {}
    for scenario in plan.scenarios:
        scenario_dir = run_dir / "scenarios" / scenario.scenario_id
        target_path = scenario_dir / "targets-stage" / "targets.json"
        result_path = scenario_dir / "evaluator-stage" / "out" / "result.json"
        trades_path = scenario_dir / "evaluator-stage" / "out" / "trades.parquet"
        daily_path = scenario_dir / "evaluator-stage" / "out" / "daily.parquet"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text('{"rows":1}', encoding="utf-8")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "evaluator_version": "research-evaluator-v2",
                    "spec_sha256": hashlib.sha256(b"semantic-c000").hexdigest(),
                    "dividends_sha256": hypothesis.dividends_sha256,
                    "compliant": True,
                    "zero_trade": False,
                    "metrics_available": True,
                    "acceptance_class": "accepted",
                    "earnings_provenance": "fixture",
                }
            ),
            encoding="utf-8",
        )
        trades_path.write_bytes(b"trades")
        daily_path.write_bytes(b"daily")
        # Exactly the keys worker.py records for a completed scenario: there is no
        # per-scenario ``status`` (the worker sets one only on failure).
        scenarios[scenario.scenario_id] = {
            "spec_id": scenario.spec_id,
            "spec_sha256": scenario.evaluation_spec_sha256,
            "targets_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
            "targets_exit": 0,
            "targets_provenance": {"stage": f"targets-{scenario.scenario_id}"},
            "evaluator_provenance": {"stage": f"evaluate-{scenario.scenario_id}"},
            "evaluator_exit": 0,
            "result_path": str(result_path),
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "trades_sha256": hashlib.sha256(trades_path.read_bytes()).hexdigest(),
            "daily_sha256": hashlib.sha256(daily_path.read_bytes()).hexdigest(),
        }
    evidence: dict[str, object] = {
        "contract": "research-run-evidence-v1",
        "status": "succeeded",
        "job_id": "job-terminal-canonical",
        "attempt_id": attempt.attempt_id,
        "run_plan_sha256": plan_digest,
        "evaluation_spec_set_sha256": plan.evaluation_spec_set_sha256,
        "primary_scenario_id": plan.primary_scenario_id,
        "validated_inputs_sha256": hashlib.sha256(validated_inputs.read_bytes()).hexdigest(),
        "scenarios": scenarios,
        "completed_scenarios": [item.scenario_id for item in plan.scenarios],
        "analysis": {
            "analysis/result.json": hashlib.sha256(analysis_path.read_bytes()).hexdigest()
        },
        "analysis_exit": 0,
        "checks": [
            {"name": "source_before", "value": "fixture-source", "ok": True},
            {"name": "source_after_validate-c000", "value": "fixture-source", "ok": True},
            {"name": "source_after_s000", "value": "fixture-source", "ok": True},
            {"name": "source_after_s001", "value": "fixture-source", "ok": True},
            {"name": "source_after", "value": "fixture-source", "ok": True},
        ],
        "stages": [],
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
    }
    evidence_path = run_dir / "run-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    terminal: dict[str, object] = {
        "attempt_id": attempt.attempt_id,
        "job_id": "job-terminal-canonical",
        "worker_pid": 4242,
        "worker_starttime": 77,
        "status": "succeeded",
        "run_plan_sha256": plan_digest,
        "evaluation_spec_set_sha256": plan.evaluation_spec_set_sha256,
        "run_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "targets_exit": 0,
        "evaluator_exit": 0,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
    }
    return store, attempt, plan, run_dir, evidence, terminal


def _rewrite_terminal_evidence(
    run_dir: Path, evidence: dict[str, object], terminal: dict[str, object]
) -> None:
    evidence_path = run_dir / "run-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    terminal["run_evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()


def test_cli_run_bounded_wait_is_queue_observation(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    monkeypatch.setattr(research_cli, "launch", lambda *_a, **_k: pytest.fail("spawn"))
    result = _call(store.root, "run", attempt.attempt_id, "--wait-seconds", "0")
    assert "accepted" in result.output and "queued" in result.output
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_QUEUED


def _record_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, int]]:
    """Capture the limits `research run` queues, without starting anything."""
    queued: list[tuple[float, int]] = []
    real = ResearchStore.queue_run_request

    def spy(
        self: ResearchStore,
        attempt_id: str,
        job_id: str,
        run_dir: Path,
        timeout_seconds: int | float,
        max_rss_mb: int,
        run_plan: RunPlan | None = None,
    ) -> Attempt:
        queued.append((float(timeout_seconds), max_rss_mb))
        return real(self, attempt_id, job_id, run_dir, timeout_seconds, max_rss_mb, run_plan)

    monkeypatch.setattr(ResearchStore, "queue_run_request", spy)
    return queued


def test_cli_run_derives_timeout_from_run_plan_and_hypothesis_compute(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    first = RunScenario(
        "s000",
        (sys.executable, "-m", "fixture_target"),
        "c000",
        hashlib.sha256(b"eval").hexdigest(),
    )
    second = replace(first, scenario_id="s001")
    attempt = _ready(store, source, hypothesis, scenarios=(first, second))
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    queued = _record_queue(monkeypatch)
    _call(store.root, "run", attempt.attempt_id, "--wait-seconds", "0")
    # 1 s * (2 targets + 2 evaluate + 1 validate stage) + 1 s analysis + 300 s overhead.
    assert plan.scenario_timeout_seconds * 5 + plan.analysis_timeout_seconds + 300 == 306
    # The fixture hypothesis document freezes compute.max_rss_mb = 1024.
    assert queued == [(306.0, 1024)]


def test_cli_run_derived_timeout_ignores_unreferenced_specs_in_the_spec_set(
    campaign: tuple[ResearchStore, Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.ABANDONED, "superseded")
    # Create a hypothesis whose spec set holds an extra spec that no scenario references.
    extra = tmp_path / "eval-spec-unreferenced.json"
    extra.write_text("unreferenced", encoding="utf-8")
    extra.chmod(0o444)
    eval_spec = Path(hypothesis.evaluation_spec_path)
    spec_set_path = tmp_path / "two-spec-set.json"
    spec_set_path.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0002",
            "c000",
            (
                EvaluationSpecEntry(
                    "c000", str(eval_spec), hashlib.sha256(eval_spec.read_bytes()).hexdigest()
                ),
                EvaluationSpecEntry(
                    "c001", str(extra), hashlib.sha256(extra.read_bytes()).hexdigest()
                ),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    spec_file = tmp_path / "second-spec.json"
    spec_file.write_text(json.dumps(_payload()), encoding="utf-8")
    hypothesis = store.create_hypothesis(
        "two specs",
        spec_file,
        Path(hypothesis.panel_path),
        Path(hypothesis.receipt_path),
        eval_spec,
        hypothesis.base_commit,
        dividends=Path(hypothesis.dividends_path),
        evaluation_spec_set=spec_set_path,
    )
    attempt = _ready(store, source, hypothesis)
    plan = RunPlan.from_json(store.evidence(attempt.attempt_id, "run_plan"))
    assert plan.spec_ids == ("c000",)
    assert len(store.evaluation_spec_set(hypothesis.hypothesis_id).specs) == 2
    # Three stages for the one scenario plus analysis; the unreferenced spec adds nothing.
    assert plan.stage_budget_seconds == 4
    assert research_cli._resolve_run_timeout(plan, None) == 304
    queued = _record_queue(monkeypatch)
    # The store check counts only referenced specs: the exact budget is accepted, with the
    # default memory taken from the frozen hypothesis compute.
    _call(
        store.root,
        "run",
        attempt.attempt_id,
        "--timeout-seconds",
        str(plan.stage_budget_seconds),
        "--wait-seconds",
        "0",
    )
    assert queued == [(4.0, 1024)]
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_QUEUED


def test_cli_run_explicit_flags_override_derived_limits(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    queued = _record_queue(monkeypatch)
    _call(
        store.root,
        "run",
        attempt.attempt_id,
        "--timeout-seconds",
        "1234",
        "--max-rss-mb",
        "2048",
        "--wait-seconds",
        "0",
    )
    assert queued == [(1234.0, 2048)]
    # With a recorded probe an explicit flag may not undercut the frozen compute bound (1024).
    refused = runner.invoke(
        app,
        [
            "research",
            "run",
            attempt.attempt_id,
            "--root",
            str(store.root),
            "--max-rss-mb",
            "777",
            "--wait-seconds",
            "0",
        ],
    )
    assert refused.exit_code == 1 and "below the minimum 1024" in refused.output
    assert queued == [(1234.0, 2048)]


def test_cli_run_refuses_when_derived_timeout_exceeds_the_limit(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    real_from_json = RunPlan.from_json
    monkeypatch.setattr(
        RunPlan,
        "from_json",
        classmethod(
            lambda cls, value: replace(
                real_from_json(value), scenario_timeout_seconds=9000, analysis_timeout_seconds=1700
            )
        ),
    )
    queued = _record_queue(monkeypatch)
    result = runner.invoke(
        app,
        ["research", "run", attempt.attempt_id, "--root", str(store.root), "--wait-seconds", "0"],
    )
    assert result.exit_code == 1
    assert "derived run timeout 29000s" in result.output
    assert "exceeds the 28800s limit" in result.output
    assert queued == []
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.REVIEW_PASSED


def test_run_timeout_derivation_is_exact_and_boundary_checked(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"eval").hexdigest()
    base = RunPlan(
        "research-run-plan-v1",
        "H0001-A001",
        "a" * 40,
        "b" * 64,
        "c" * 64,
        "s000",
        (RunScenario("s000", ("/usr/bin/python3", "-m", "targets"), "c000", digest),),
        AnalysisPlan("analysis.module", (), ("analysis/result.json",), 1024),
        10,
        10,
    )
    at_limit = replace(base, scenario_timeout_seconds=9000, analysis_timeout_seconds=1500)
    # 9000 * (targets + evaluate + validate) + 1500 + 300 overhead is exactly 28800 s.
    assert research_cli._resolve_run_timeout(at_limit, None) == 28800
    over = replace(at_limit, analysis_timeout_seconds=1500.5)
    with pytest.raises(JobError, match="exceeds the 28800s limit"):
        research_cli._resolve_run_timeout(over, None)
    assert research_cli._resolve_run_timeout(over, 99.0) == 99.0


class _FakeHypothesisStore:
    def __init__(self, spec_json: str) -> None:
        self._spec_json = spec_json

    def get_hypothesis(self, _hypothesis_id: str) -> Any:
        return SimpleNamespace(spec_json=self._spec_json)

    def compute_probe(self, _hypothesis_id: str) -> None:
        return None  # a hypothesis frozen before the compute probe existed


def test_run_max_rss_uses_frozen_hypothesis_compute(tmp_path: Path) -> None:
    payload = _payload()
    payload["compute"] = {"max_wall_seconds": 120.0, "max_rss_mb": 4096}
    store = _FakeHypothesisStore(json.dumps(payload))
    assert research_cli._resolve_run_max_rss(store, "H0001", None) == 4096  # type: ignore[arg-type]
    assert research_cli._resolve_run_max_rss(store, "H0001", 2048) == 2048  # type: ignore[arg-type]
    unusable = _FakeHypothesisStore('{"title":"fixture"}')
    with pytest.raises(JobError, match="pass --max-rss-mb explicitly"):
        research_cli._resolve_run_max_rss(unusable, "H0001", None)  # type: ignore[arg-type]
    # An explicit flag does not need a readable hypothesis document.
    assert research_cli._resolve_run_max_rss(unusable, "H0001", 512) == 512  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_cli_run_rejects_nonfinite_or_negative_wait(
    campaign: tuple[ResearchStore, Path, Any], value: str
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _call(store.root, "run", attempt.attempt_id, "--wait-seconds", value, expect=1)
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.REVIEW_PASSED


def test_fatal_serve_iteration_pauses_and_repeated_invocation_stays_quiet(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _queue(store, attempt.attempt_id)

    def broken_reconcile(_store: ResearchStore) -> None:
        raise RuntimeError("unexpected reconcile failure")

    monkeypatch.setattr(research_cli, "_reconcile_jobs", broken_reconcile)
    first = runner.invoke(
        app,
        [
            "research",
            "serve",
            "--session-key",
            "owner",
            "--root",
            str(store.root),
            "--once",
        ],
    )
    assert first.exit_code == 78, first.output
    assert "campaign paused" in first.output
    assert store.campaign()[0] == "PAUSED"
    assert store.events()[-2].kind == "serve_failed"
    assert store.events()[-1].kind == "campaign_paused"

    reconciled: list[str] = []

    def observe_reconcile(_store: ResearchStore) -> None:
        reconciled.append("called")

    class QuietSender:
        def send(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("paused campaign must not wake Astra")

    monkeypatch.setattr(research_cli, "_reconcile_jobs", observe_reconcile)
    monkeypatch.setattr(research_cli, "OpenClawWakeSender", lambda *_args: QuietSender())
    second = runner.invoke(
        app,
        [
            "research",
            "serve",
            "--session-key",
            "owner",
            "--root",
            str(store.root),
            "--once",
        ],
    )
    assert second.exit_code == 0, second.output
    assert reconciled == ["called"]
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_QUEUED
    assert compose_wake(store) is None


def test_serve_once_paused_campaign_tolerates_unavailable_owner_poll(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    pending_key = "paused-poll-key"
    assert store.reserve_wake(pending_key, None, "DRAFT", store.campaign()[1])
    store.complete_wake(pending_key, "run-paused-poll")
    store.pause("operator pause")
    event_count_before = len(store.events())

    class UnavailableSender:
        def owner_task_status(self, _run_id: str) -> dict[str, object]:
            raise OpenClawTransportError("connection refused")

    monkeypatch.setattr(research_cli, "OpenClawWakeSender", lambda *_args: UnavailableSender())
    result = runner.invoke(
        app,
        [
            "research",
            "serve",
            "--session-key",
            "owner",
            "--root",
            str(store.root),
            "--once",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "owner poll unavailable (1/30)" in result.stderr
    assert store.campaign()[0] == "PAUSED"
    assert len(store.events()) == event_count_before
    assert not any(event.kind == "serve_failed" for event in store.events()[event_count_before:])
    assert not any(event.kind == "campaign_paused" for event in store.events()[event_count_before:])


def test_serve_owner_poll_unavailable_pauses_at_threshold(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    pending_key = "threshold-poll-key"
    assert store.reserve_wake(pending_key, None, "DRAFT", store.campaign()[1])
    store.complete_wake(pending_key, "run-threshold-poll")
    store.pause("operator pause")

    class UnavailableSender:
        def owner_task_status(self, _run_id: str) -> dict[str, object]:
            raise OpenClawTransportError("connection refused")

    monkeypatch.setattr(research_cli, "OpenClawWakeSender", lambda *_args: UnavailableSender())
    monkeypatch.setattr(research_cli, "_MAX_CONSECUTIVE_POLL_FAILURES", 2)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    result = runner.invoke(
        app,
        [
            "research",
            "serve",
            "--session-key",
            "owner",
            "--root",
            str(store.root),
            "--poll-seconds",
            "1",
        ],
    )

    assert result.exit_code == 78, result.output
    assert store.campaign()[0] == "PAUSED"
    failed_events = [event for event in store.events() if event.kind == "serve_failed"]
    assert len(failed_events) == 1
    assert "OwnerPollUnavailable" in failed_events[0].detail


def test_host_failure_terminal_uses_stable_check_name_and_detail(tmp_path: Path) -> None:
    outcome = research_cli._failure("H0001-A001", "job-failure", "launch_failed")
    research_cli._write_terminal(tmp_path, outcome, detail="worker exploded at stage launch")
    payload = json.loads((tmp_path / "terminal.json").read_text(encoding="utf-8"))
    assert payload["checks"] == [{"name": "launch_failed", "ok": False}]
    assert payload["error"] == "worker exploded at stage launch"


@pytest.mark.parametrize("status", ["cancelled", "launch_failed", "source_mutated"])
def test_terminal_outcome_accepts_host_terminal_without_run_evidence(
    campaign: tuple[ResearchStore, Path, Any], status: str
) -> None:
    """Host-owned terminal records are authoritative without worker evidence."""
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    run_dir.mkdir(parents=True)
    terminal: dict[str, object] = {
        "attempt_id": attempt.attempt_id,
        "job_id": "job-host-terminal",
        "status": status,
        "targets_exit": -15 if status == "cancelled" else -1,
        "evaluator_exit": None,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
    }

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == status
    assert outcome.job_id == "job-host-terminal"
    assert outcome.exit_code in {-15, -1}


def test_terminal_outcome_rejects_worker_failure_without_run_evidence(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    terminal = {
        "attempt_id": attempt.attempt_id,
        "job_id": "job-worker-failure",
        "worker_pid": 4242,
        "worker_starttime": 77,
        "status": "scenario_failed",
        "targets_exit": 1,
        "evaluator_exit": 1,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
    }

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


def test_terminal_outcome_rejects_tampered_run_evidence_digest(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    run_dir.mkdir(parents=True)
    evidence = run_dir / "run-evidence.json"
    evidence.write_text('{"status":"succeeded"}', encoding="utf-8")
    terminal = {
        "attempt_id": attempt.attempt_id,
        "job_id": "job-evidence-tamper",
        "worker_pid": 4242,
        "worker_starttime": 77,
        "status": "succeeded",
        "run_evidence_sha256": "0" * 64,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
    }

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


def test_terminal_outcome_rejects_tampered_primary_result_digest(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == "succeeded"
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    primary = scenarios["s000"]
    assert isinstance(primary, dict)
    primary["result_sha256"] = "0" * 64
    _rewrite_terminal_evidence(run_dir, evidence, terminal)

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


def test_terminal_outcome_rejects_tampered_primary_result_file(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, attempt, _plan, _run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == "succeeded"
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    primary = scenarios["s000"]
    assert isinstance(primary, dict)
    result_path = Path(str(primary["result_path"]))
    result_path.write_text('{"tampered":true}', encoding="utf-8")

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_id", "H0001-A999"),
        ("job_id", "job-embedded-evil"),
        ("run_plan_sha256", "0" * 64),
        ("evaluation_spec_set_sha256", "f" * 64),
    ],
)
def test_terminal_outcome_rejects_embedded_binding_mismatch(
    campaign: tuple[ResearchStore, Path, Any], field: str, value: str
) -> None:
    """A consistent file digest cannot authenticate mismatched run bindings."""
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == "succeeded"

    evidence[field] = value
    _rewrite_terminal_evidence(run_dir, evidence, terminal)
    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        ("primary", "s001"),
        ("missing_completion", ["s000"]),
        ("reordered_completion", ["s001", "s000"]),
        ("scenario_spec_id", "c999"),
        ("scenario_spec_sha256", "0" * 64),
        ("validated_inputs_sha256", "0" * 64),
        ("analysis_digest", "0" * 64),
        ("analysis_manifest_missing", None),
        ("analysis_manifest_extra", None),
        ("secondary_result_sha256", "0" * 64),
        ("failed_check", False),
        ("noncanonical_result_path", "outside-result.json"),
    ],
)
def test_terminal_outcome_rejects_worker_success_binding_tamper(
    campaign: tuple[ResearchStore, Path, Any], tamper: str, expected: object
) -> None:
    """A worker-shaped success must match every stored plan/run binding."""
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == "succeeded"

    if tamper == "primary":
        evidence["primary_scenario_id"] = expected
    elif tamper in {"missing_completion", "reordered_completion"}:
        evidence["completed_scenarios"] = expected
    elif tamper == "scenario_spec_id":
        scenarios = evidence["scenarios"]
        assert isinstance(scenarios, dict)
        scenario = scenarios["s000"]
        assert isinstance(scenario, dict)
        scenario["spec_id"] = expected
    elif tamper == "scenario_spec_sha256":
        scenarios = evidence["scenarios"]
        assert isinstance(scenarios, dict)
        scenario = scenarios["s000"]
        assert isinstance(scenario, dict)
        scenario["spec_sha256"] = expected
    elif tamper == "validated_inputs_sha256":
        evidence["validated_inputs_sha256"] = expected
    elif tamper == "analysis_digest":
        analysis = evidence["analysis"]
        assert isinstance(analysis, dict)
        analysis["analysis/result.json"] = expected
    elif tamper == "analysis_manifest_missing":
        analysis = evidence["analysis"]
        assert isinstance(analysis, dict)
        analysis.pop("analysis/result.json")
    elif tamper == "analysis_manifest_extra":
        analysis = evidence["analysis"]
        assert isinstance(analysis, dict)
        analysis["analysis/extra.json"] = "0" * 64
    elif tamper == "secondary_result_sha256":
        scenarios = evidence["scenarios"]
        assert isinstance(scenarios, dict)
        scenario = scenarios["s001"]
        assert isinstance(scenario, dict)
        scenario["result_sha256"] = expected
    elif tamper == "failed_check":
        checks = evidence["checks"]
        assert isinstance(checks, list)
        check = checks[0]
        assert isinstance(check, dict)
        check["ok"] = expected
    elif tamper == "noncanonical_result_path":
        scenarios = evidence["scenarios"]
        assert isinstance(scenarios, dict)
        scenario = scenarios["s000"]
        assert isinstance(scenario, dict)
        original = Path(str(scenario["result_path"]))
        outside = run_dir / str(expected)
        outside.write_bytes(original.read_bytes())
        scenario["result_path"] = str(outside)
    else:
        raise AssertionError(f"unhandled tamper: {tamper}")

    _rewrite_terminal_evidence(run_dir, evidence, terminal)
    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


def test_terminal_outcome_rejects_tampered_secondary_result_file(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, attempt, _plan, _run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == "succeeded"
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    secondary = scenarios["s001"]
    assert isinstance(secondary, dict)
    result_path = Path(str(secondary["result_path"]))
    result_path.write_text('{"tampered":true}', encoding="utf-8")

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


@pytest.mark.parametrize("output_kind", ["missing", "directory", "symlink"])
def test_terminal_outcome_rejects_nonregular_primary_result_output(
    campaign: tuple[ResearchStore, Path, Any], output_kind: str
) -> None:
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == "succeeded"
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    primary = scenarios["s000"]
    assert isinstance(primary, dict)
    result_path = Path(str(primary["result_path"]))
    result_bytes = result_path.read_bytes()
    result_path.unlink()
    if output_kind == "missing":
        pass
    elif output_kind == "directory":
        result_path.mkdir()
    elif output_kind == "symlink":
        target = run_dir.parent / "result-symlink-target.json"
        target.write_bytes(result_bytes)
        result_path.symlink_to(target)
    else:
        raise AssertionError(output_kind)

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


@pytest.mark.parametrize("status", ["scenario_failed", "timed_out"])
def test_terminal_outcome_non_success_requires_primary_but_not_full_coverage(
    campaign: tuple[ResearchStore, Path, Any], status: str
) -> None:
    store, attempt, plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    evidence["status"] = status
    evidence["completed_scenarios"] = []
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    scenarios.pop("s001")
    terminal["status"] = status
    _rewrite_terminal_evidence(run_dir, evidence, terminal)

    baseline = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)
    assert baseline.status == status

    evidence["primary_scenario_id"] = "s001"
    _rewrite_terminal_evidence(run_dir, evidence, terminal)
    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert plan.primary_scenario_id == "s000"
    assert outcome.status == "run_evidence_mismatch"


def test_host_dispatch_revalidates_dirty_source_after_queue(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)
    source.chmod(0o755)
    (source / "dirty.txt").write_text("changed", encoding="utf-8")
    store.acquire_owner_lock()
    try:
        result = research_cli._dispatch_queued_job(store)
    finally:
        store.release_owner_lock()
    assert result == "source_mutated"
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    row = store.job_for(attempt.attempt_id)
    assert row is not None and row["state"] == JobState.EXITED.value
    terminal = Path(json.loads(row["payload_json"])["run_dir"]) / "terminal.json"
    assert json.loads(terminal.read_text())["status"] == "source_mutated"


def test_host_dispatch_runtime_pin_change_is_terminal_before_worker(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)
    row = store.job_for(attempt.attempt_id)
    assert row is not None
    payload = json.loads(row["payload_json"])
    payload["snapshot_sha256"] = "0" * 64
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    import hashlib

    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET payload_json=?,payload_sha256=? WHERE job_id=?",
            (text, hashlib.sha256(text.encode()).hexdigest(), payload["job_id"]),
        )
        conn.commit()
    monkeypatch.setattr(research_cli, "launch", lambda *_a, **_k: pytest.fail("spawn"))
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == "runtime_pin_mismatch"
    finally:
        store.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED


def test_serve_once_dispatches_queue_while_wake_is_pending(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)
    seen: list[str] = []

    def fake_launch(attempt_dir: Path, *_a: object, **kwargs: object) -> JobRecord:
        seen.append(str(kwargs["job_id"]))
        return JobRecord(
            str(kwargs["job_id"]),
            attempt.attempt_id,
            os.getpid(),
            _starttime(os.getpid()) or 0,
            str(attempt_dir / "run"),
        )

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    monkeypatch.setattr(research_cli, "compose_wake", lambda _store: None)
    monkeypatch.setattr(research_cli, "poll_owner_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(research_cli, "OpenClawWakeSender", lambda *_a: object())
    _call(store.root, "serve", "--session-key", "owner", "--once")
    assert seen == ["job-cli-test"]
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUNNING


def test_reserved_job_recovers_identity_without_duplicate_launch(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    store.acquire_owner_lock()
    try:
        store.acquire_run_lock()
        try:
            store.claim_queued_job(attempt.attempt_id, "job-cli-test")
        finally:
            store.release_run_lock()
        row = store.job_for(attempt.attempt_id)
        assert row is not None
        payload = json.loads(row["payload_json"])
        pid = os.getpid()
        starttime = _starttime(pid)
        assert starttime is not None
        payload.update(worker_pid=pid, worker_starttime=starttime, state="LAUNCHED")
        run_dir = Path(payload["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "job.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(research_cli, "launch", lambda *_a, **_k: pytest.fail("duplicate"))
        research_cli._reconcile_jobs(store)
        research_cli._reconcile_jobs(store)
    finally:
        store.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUNNING
    job = store.job_for(attempt.attempt_id)
    assert job is not None
    assert job["state"] == JobState.LAUNCHED.value


def test_terminal_worker_failure_is_not_replaced_by_late_cancel(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)
    plan_digest, spec_set_digest = _run_plan_bindings(store, attempt.attempt_id)

    def fake_launch(attempt_dir: Path, *_a: object, **kwargs: object) -> JobRecord:
        run_dir = attempt_dir / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        _fake_worker_evidence(
            run_dir,
            job_id=str(kwargs["job_id"]),
            attempt_id=attempt.attempt_id,
            status="source_mutated",
            run_plan_sha256=plan_digest,
            evaluation_spec_set_sha256=spec_set_digest,
        )
        return JobRecord(str(kwargs["job_id"]), attempt.attempt_id, 0, 0, str(run_dir))

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == "job-cli-test"
        research_cli._reconcile_jobs(store)
    finally:
        store.release_owner_lock()
    outcome = store.get_attempt(attempt.attempt_id).run_outcome or ""
    assert '"status":"source_mutated"' in outcome


def test_dispatch_success_finishes_store_and_wakes_astra(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)
    plan_digest, spec_set_digest = _run_plan_bindings(store, attempt.attempt_id)

    def fake_launch(attempt_dir: Path, *_a: object, **kwargs: object) -> JobRecord:
        run_dir = attempt_dir / "run"
        result_path = run_dir / "scenarios" / "s000" / "evaluator-stage" / "out" / "result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "evaluator_version": "research-evaluator-v2",
                    "spec_sha256": hypothesis.spec_sha256,
                    "dividends_sha256": hypothesis.dividends_sha256,
                    "compliant": True,
                    "zero_trade": False,
                    "metrics_available": True,
                    "acceptance_class": "accepted",
                    "earnings_provenance": "fixture",
                }
            ),
            encoding="utf-8",
        )
        _fake_worker_evidence(
            run_dir,
            job_id=str(kwargs["job_id"]),
            attempt_id=attempt.attempt_id,
            status="succeeded",
            run_plan_sha256=plan_digest,
            evaluation_spec_set_sha256=spec_set_digest,
            scenario_spec_sha256=hypothesis.evaluation_spec_sha256,
            result_path=result_path,
        )
        return JobRecord(str(kwargs["job_id"]), attempt.attempt_id, 0, 0, str(run_dir))

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) == "job-cli-test"
        research_cli._reconcile_jobs(store)
        first = store.get_attempt(attempt.attempt_id)
        research_cli._reconcile_jobs(store)
        second = store.get_attempt(attempt.attempt_id)
    finally:
        store.release_owner_lock()
    assert first.state == AttemptState.RUN_SUCCEEDED
    assert first.run_outcome is not None and json.loads(first.run_outcome)["result_path"]
    assert second.run_outcome == first.run_outcome
    assert compose_wake(store) is not None


def test_dispatch_success_without_evaluator_exit_stays_failed(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)

    def fake_launch(attempt_dir: Path, *_a: object, **kwargs: object) -> JobRecord:
        run_dir = attempt_dir / "run"
        result_path = run_dir / "evaluator-stage" / "out" / "result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("{}", encoding="utf-8")
        (run_dir / "terminal.json").write_text(
            json.dumps({"job_id": kwargs["job_id"], "status": "succeeded"}),
            encoding="utf-8",
        )
        return JobRecord(str(kwargs["job_id"]), attempt.attempt_id, 0, 0, str(run_dir))

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    store.acquire_owner_lock()
    try:
        research_cli._dispatch_queued_job(store)
        research_cli._reconcile_jobs(store)
    finally:
        store.release_owner_lock()
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED


def test_stale_reject_after_cancel_keeps_canonical_terminal(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    cancelled = False

    def cancel_during_precheck(_store: ResearchStore, _attempt_id: str) -> str | None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            result = CliRunner().invoke(
                app,
                ["research", "cancel", attempt.attempt_id, "--root", str(store.root)],
            )
            assert result.exit_code == 0, result.output
        return "cancelled_during_precheck"

    monkeypatch.setattr(research_cli, "_host_execution_ready", cancel_during_precheck)
    store.acquire_owner_lock()
    try:
        assert research_cli._dispatch_queued_job(store) is None
        row = store.job_for(attempt.attempt_id)
        assert row is not None
        terminal = Path(json.loads(row["payload_json"])["run_dir"]) / "terminal.json"
        before = terminal.read_bytes()
        assert research_cli._dispatch_queued_job(store) is None
        assert terminal.read_bytes() == before
    finally:
        store.release_owner_lock()
    assert cancelled
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    assert '"status":"cancelled"' in (store.get_attempt(attempt.attempt_id).run_outcome or "")


def test_running_job_is_not_duplicated_when_owner_turn_is_pending(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, hypothesis = campaign
    attempt = _ready(store, source, hypothesis)
    _install_dispatch_admission(store, attempt, monkeypatch)
    _queue(store, attempt.attempt_id)
    _verified(store, attempt.attempt_id)
    configure_real_readiness(store, store.root.parent, monkeypatch)
    launches = 0

    def fake_launch(attempt_dir: Path, *_a: object, **kwargs: object) -> JobRecord:
        nonlocal launches
        launches += 1
        return JobRecord(
            str(kwargs["job_id"]),
            attempt.attempt_id,
            os.getpid(),
            _starttime(os.getpid()) or 0,
            str(attempt_dir / "run"),
        )

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    store.acquire_owner_lock()
    try:
        research_cli._dispatch_queued_job(store)
        assert research_cli._dispatch_queued_job(store) is None
    finally:
        store.release_owner_lock()
    assert launches == 1
    assert compose_wake(store) is None


def test_terminal_outcome_accepts_real_worker_success_shape(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    """The worker's success evidence has no per-scenario status key."""
    store, attempt, plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    assert all("status" not in scenario for scenario in scenarios.values())
    assert set(scenarios["s000"]) == {
        "spec_id",
        "spec_sha256",
        "targets_sha256",
        "targets_exit",
        "targets_provenance",
        "evaluator_provenance",
        "evaluator_exit",
        "result_path",
        "result_sha256",
        "trades_sha256",
        "daily_sha256",
    }

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.result_path == str(
        run_dir / "scenarios" / plan.primary_scenario_id / "evaluator-stage" / "out" / "result.json"
    )


def test_terminal_outcome_still_accepts_explicit_scenario_success_status(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    for scenario in scenarios.values():
        scenario["status"] = "succeeded"
    _rewrite_terminal_evidence(run_dir, evidence, terminal)

    assert research_cli._terminal_outcome(store, attempt.attempt_id, terminal).status == (
        "succeeded"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "failed"),
        ("status", "scenario_failed"),
        ("status", ""),
        ("targets_exit", 1),
        ("evaluator_exit", 3),
        ("evaluator_exit", -9),
        ("evaluator_exit", True),
        ("evaluator_exit", "0"),
        ("targets_exit", None),
    ],
)
@pytest.mark.parametrize("scenario_id", ["s000", "s001"])
def test_terminal_outcome_rejects_failed_scenario_in_success_run(
    campaign: tuple[ResearchStore, Path, Any], scenario_id: str, key: str, value: object
) -> None:
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    scenario = scenarios[scenario_id]
    assert isinstance(scenario, dict)
    scenario[key] = value
    _rewrite_terminal_evidence(run_dir, evidence, terminal)

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"


@pytest.mark.parametrize(
    "missing", [("targets_exit",), ("evaluator_exit",), ("targets_exit", "evaluator_exit")]
)
def test_terminal_outcome_rejects_scenario_without_recorded_exits(
    campaign: tuple[ResearchStore, Path, Any], missing: tuple[str, ...]
) -> None:
    store, attempt, _plan, run_dir, evidence, terminal = _canonical_terminal_fixture(campaign)
    scenarios = evidence["scenarios"]
    assert isinstance(scenarios, dict)
    scenario = scenarios["s001"]
    assert isinstance(scenario, dict)
    for key in missing:
        scenario.pop(key)
    _rewrite_terminal_evidence(run_dir, evidence, terminal)

    outcome = research_cli._terminal_outcome(store, attempt.attempt_id, terminal)

    assert outcome.status == "run_evidence_mismatch"
