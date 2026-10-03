from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.research.contracts import (
    AnalysisPlan,
    Attempt,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    Event,
    HypothesisSpec,
    HypothesisState,
    ImplementationRecord,
    ReviewRecord,
    RunOutcome,
    RunPlan,
    RunScenario,
)


def test_hypothesis_round_trip_is_strict() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    value = HypothesisSpec(
        "H0001",
        "title",
        '{"x":1}',
        digest,
        "/panel",
        "/receipt",
        "/eval",
        digest,
        digest,
        digest,
        3,
        "a" * 40,
        "2026-01-01T00:00:00Z",
        "/dividends",
        digest,
        HypothesisState.DRAFT,
    )
    assert HypothesisSpec.from_json(value.to_json()) == value
    raw = json.loads(value.to_json())
    raw["unknown"] = True
    with pytest.raises(ValueError):
        HypothesisSpec.from_json(json.dumps(raw))
    assert value.state == HypothesisState.DRAFT


def test_all_wire_records_round_trip_and_validate_utc() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    attempt = Attempt(
        "H0001-A001",
        "H0001",
        1,
        AttemptState.OPENED,
        "/worktree",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )
    implementation = ImplementationRecord(
        attempt.attempt_id,
        "a" * 40,
        ("/usr/bin/python3", "-m", "target"),
        "/evidence",
        "coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    review = ReviewRecord(
        attempt.attempt_id,
        "a" * 40,
        digest,
        "PASS",
        (),
        "reviewer",
        "actual",
        "session",
        "2026-01-01T00:00:00Z",
    )
    outcome = RunOutcome(
        attempt.attempt_id,
        "job-1",
        0,
        True,
        False,
        True,
        "accepted",
        "driver",
        "/run/result.json",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
    )
    event = Event(1, "2026-01-01T00:00:00Z", "H0001", None, "kind", "{}", "driver")
    records: tuple[Attempt | ImplementationRecord | ReviewRecord | RunOutcome | Event, ...] = (
        attempt,
        implementation,
        review,
        outcome,
        event,
    )
    for record in records:
        assert type(record).from_json(record.to_json()) == record
    with pytest.raises(ValueError):
        Event(1, "2026-01-01T00:00:00+01:00", "H0001", None, "kind", "{}", "driver")


def test_reviewed_run_plan_binds_distinct_specs_and_analysis(tmp_path: Path) -> None:
    root = tmp_path
    primary = root / "primary.json"
    cost = root / "cost.json"
    primary.write_text('{"cost":0}', encoding="utf-8")
    cost.write_text('{"cost":3}', encoding="utf-8")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    spec_set = EvaluationSpecSet(
        "research-evaluation-spec-set-v1",
        "H0002",
        "c000",
        (
            EvaluationSpecEntry("c000", str(primary), digest(primary)),
            EvaluationSpecEntry("c001", str(cost), digest(cost)),
        ),
        "2026-01-01T00:00:00Z",
    )
    plan = RunPlan(
        "research-run-plan-v1",
        "H0002-A001",
        "a" * 40,
        digest(primary),
        hashlib.sha256(spec_set.to_json().encode()).hexdigest(),
        "s000",
        (
            RunScenario("s000", ("/usr/bin/python3", "-m", "targets"), "c000", digest(primary)),
            RunScenario("s001", ("/usr/bin/python3", "-m", "targets"), "c001", digest(cost)),
        ),
        AnalysisPlan("analysis.module", ("summary",), ("analysis/summary.json",), 1024),
        300,
        300,
    )
    assert EvaluationSpecSet.from_json(spec_set.to_json()) == spec_set
    assert RunPlan.from_json(plan.to_json()) == plan
    with pytest.raises(ValueError, match="direct files"):
        AnalysisPlan("analysis.module", (), ("analysis/nested/result.json",), 1024)
    with pytest.raises(ValueError, match="aggregate"):
        RunPlan(
            plan.contract,
            plan.attempt_id,
            plan.commit,
            plan.implementation_sha256,
            plan.evaluation_spec_set_sha256,
            plan.primary_scenario_id,
            plan.scenarios,
            plan.analysis,
            7200,
            7200,
        )


def test_evaluation_spec_set_rejects_overflow_gaps_order_and_duplicate_paths(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    entries: list[EvaluationSpecEntry] = []
    for index in range(16):
        path = tmp_path / f"spec-{index}.json"
        path.write_text(json.dumps({"cost": index}), encoding="utf-8")
        paths.append(path)
        entries.append(
            EvaluationSpecEntry(
                f"c{index:03d}",
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    valid = EvaluationSpecSet(
        "research-evaluation-spec-set-v1",
        "H0001",
        "c000",
        tuple(entries),
        "2026-01-01T00:00:00Z",
    )
    assert len(valid.specs) == 16
    with pytest.raises(ValueError, match="at most 16"):
        overflow = tmp_path / "spec-16.json"
        overflow.write_text("overflow", encoding="utf-8")
        EvaluationSpecSet(
            valid.contract,
            valid.hypothesis_id,
            valid.primary_spec_id,
            tuple(
                [
                    *entries,
                    EvaluationSpecEntry(
                        "c016",
                        str(tmp_path / "spec-16.json"),
                        hashlib.sha256(b"overflow").hexdigest(),
                    ),
                ]
            ),
            valid.created_at,
        )

    with pytest.raises(ValueError, match="contiguous"):
        EvaluationSpecSet(
            valid.contract,
            valid.hypothesis_id,
            valid.primary_spec_id,
            (entries[0], entries[2]),
            valid.created_at,
        )
    with pytest.raises(ValueError, match="contiguous"):
        EvaluationSpecSet(
            valid.contract,
            valid.hypothesis_id,
            valid.primary_spec_id,
            (entries[1], entries[0]),
            valid.created_at,
        )
    with pytest.raises(ValueError, match="paths must be unique"):
        EvaluationSpecSet(
            valid.contract,
            valid.hypothesis_id,
            valid.primary_spec_id,
            (
                entries[0],
                EvaluationSpecEntry("c001", entries[0].path, entries[1].sha256),
            ),
            valid.created_at,
        )


def _contract_run_plan(tmp_path: Path, *, scenarios: int = 1) -> RunPlan:
    spec = tmp_path / "evaluation.json"
    spec.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    scenario_values = tuple(
        RunScenario(
            f"s{index:03d}",
            ("/usr/bin/python3", "-m", "targets"),
            "c000",
            digest,
        )
        for index in range(scenarios)
    )
    return RunPlan(
        "research-run-plan-v1",
        "H0001-A001",
        "a" * 40,
        "b" * 64,
        "c" * 64,
        "s000",
        scenario_values,
        AnalysisPlan("analysis.module", (), ("analysis/result.json",), 1024),
        10,
        10,
    )


def test_run_plan_rejects_seventeenth_scenario_contract_entry(tmp_path: Path) -> None:
    raw = json.loads(_contract_run_plan(tmp_path).to_json())
    raw["scenarios"] = [
        {**raw["scenarios"][0], "scenario_id": f"s{index:03d}"} for index in range(17)
    ]
    with pytest.raises(ValueError, match=r"1\.\.16"):
        RunPlan.from_json(json.dumps(raw))


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("gap", "contiguous"),
        ("order", "contiguous"),
        ("primary", "primary_scenario_id"),
    ],
)
def test_run_plan_rejects_gap_order_and_unknown_primary(
    tmp_path: Path, mutation: str, match: str
) -> None:
    raw = json.loads(_contract_run_plan(tmp_path, scenarios=2).to_json())
    if mutation == "gap":
        raw["scenarios"][1]["scenario_id"] = "s002"
    elif mutation == "order":
        raw["scenarios"] = list(reversed(raw["scenarios"]))
    else:
        raw["primary_scenario_id"] = "s009"
    with pytest.raises(ValueError, match=match):
        RunPlan.from_json(json.dumps(raw))


@pytest.mark.parametrize(
    "artifact", ["/tmp/result.json", "analysis/nested/result.json", "analysis/../result.json"]
)
def test_run_plan_rejects_unsafe_analysis_artifact_path(tmp_path: Path, artifact: str) -> None:
    raw = json.loads(_contract_run_plan(tmp_path).to_json())
    raw["analysis"]["artifacts"] = [artifact]
    with pytest.raises(ValueError, match="direct files"):
        RunPlan.from_json(json.dumps(raw))


@pytest.mark.parametrize("token", ["api_token", "password", "secret", "/tmp/input.json"])
def test_run_plan_rejects_analysis_secret_or_absolute_argument(tmp_path: Path, token: str) -> None:
    raw = json.loads(_contract_run_plan(tmp_path).to_json())
    raw["analysis"]["args"] = [token]
    with pytest.raises(ValueError, match=r"plain|absolute|secret"):
        RunPlan.from_json(json.dumps(raw))


def test_run_plan_rejects_missing_unknown_and_timeout_contract_fields(tmp_path: Path) -> None:
    raw = json.loads(_contract_run_plan(tmp_path).to_json())
    missing = dict(raw)
    del missing["analysis_timeout_seconds"]
    with pytest.raises(ValueError, match="keys must be exactly"):
        RunPlan.from_json(json.dumps(missing))
    unknown = dict(raw)
    unknown["unreviewed"] = True
    with pytest.raises(ValueError, match="keys must be exactly"):
        RunPlan.from_json(json.dumps(unknown))
    for field, value in (("scenario_timeout_seconds", 0), ("analysis_timeout_seconds", 28801)):
        invalid = dict(raw)
        invalid[field] = value
        with pytest.raises(ValueError, match=r"positive|28800"):
            RunPlan.from_json(json.dumps(invalid))


def _multi_spec_plan(
    tmp_path: Path,
    spec_ids: tuple[str, ...],
    *,
    scenario_timeout: float,
    analysis_timeout: float,
) -> RunPlan:
    base = _contract_run_plan(tmp_path)
    scenarios = tuple(
        replace(base.scenarios[0], scenario_id=f"s{index:03d}", spec_id=spec_id)
        for index, spec_id in enumerate(spec_ids)
    )
    return replace(
        base,
        scenarios=scenarios,
        scenario_timeout_seconds=scenario_timeout,
        analysis_timeout_seconds=analysis_timeout,
    )


def test_run_plan_accepts_the_28800_second_aggregate_and_rejects_just_above(
    tmp_path: Path,
) -> None:
    # One scenario: validate + targets + evaluate at 9000 s each, plus 1800 s of analysis.
    at_limit = _multi_spec_plan(tmp_path, ("c000",), scenario_timeout=9000, analysis_timeout=1800)
    assert at_limit.stage_budget_seconds == 28800
    assert RunPlan.from_json(at_limit.to_json()) == at_limit
    with pytest.raises(ValueError, match="28800 second aggregate"):
        _multi_spec_plan(tmp_path, ("c000",), scenario_timeout=9000, analysis_timeout=1800.5)


def test_run_plan_per_field_limit_is_28800(tmp_path: Path) -> None:
    raw = json.loads(_contract_run_plan(tmp_path).to_json())
    raw["scenario_timeout_seconds"] = 28800
    raw["analysis_timeout_seconds"] = 28800
    # The aggregate (not the per-field) limit rejects the pair, but 28800 alone is a legal field.
    with pytest.raises(ValueError, match="aggregate"):
        RunPlan.from_json(json.dumps(raw))
    raw["scenario_timeout_seconds"] = 10
    raw["analysis_timeout_seconds"] = 28800 - 30
    assert RunPlan.from_json(json.dumps(raw)).stage_budget_seconds == 28800
    raw["scenario_timeout_seconds"] = 28801
    with pytest.raises(ValueError, match="28800 second limit"):
        RunPlan.from_json(json.dumps(raw))


def test_run_plan_budget_counts_two_stages_per_scenario_and_one_per_distinct_spec(
    tmp_path: Path,
) -> None:
    # Counting only scenarios (or scenarios + specs) would let this plan through.
    with pytest.raises(ValueError, match="aggregate"):
        _multi_spec_plan(tmp_path, ("c000",), scenario_timeout=9600, analysis_timeout=100)
    same_spec = _multi_spec_plan(
        tmp_path, ("c000", "c000"), scenario_timeout=5000, analysis_timeout=1000
    )
    assert same_spec.spec_ids == ("c000",)
    # validate-c000, targets-s000/s001, evaluate-s000/s001.
    assert same_spec.stage_budget_seconds == 5000 * 5 + 1000
    # Two specs add a validation stage: (4 + 2) * 4000 + 4800 is exactly the limit.
    two_specs = _multi_spec_plan(
        tmp_path, ("c000", "c001"), scenario_timeout=4000, analysis_timeout=4800
    )
    assert two_specs.spec_ids == ("c000", "c001")
    assert two_specs.stage_budget_seconds == 28800
    with pytest.raises(ValueError, match="aggregate"):
        _multi_spec_plan(tmp_path, ("c000", "c001"), scenario_timeout=4000, analysis_timeout=4801)


def test_historical_run_plan_shapes_still_parse(tmp_path: Path) -> None:
    # Persisted H0002..H0007 plans: 6 (or 16) scenarios over 3 specs, 300/1800 or 150/1800 s,
    # some with integer timeouts.  The stricter aggregate must not reject any of them.
    for scenarios, timeout, analysis in ((6, 300.0, 1800.0), (6, 300, 1800), (16, 150.0, 1800.0)):
        spec_ids = tuple(("c000", "c001", "c002")[index % 3] for index in range(scenarios))
        plan = _multi_spec_plan(
            tmp_path, spec_ids, scenario_timeout=timeout, analysis_timeout=analysis
        )
        reloaded = RunPlan.from_json(plan.to_json())
        assert reloaded == plan
        assert reloaded.stage_budget_seconds <= 28800
