from __future__ import annotations

import hashlib
import json
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
            3600,
            3600,
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
    for field, value in (("scenario_timeout_seconds", 0), ("analysis_timeout_seconds", 7201)):
        invalid = dict(raw)
        invalid[field] = value
        with pytest.raises(ValueError, match=r"positive|7200"):
            RunPlan.from_json(json.dumps(invalid))
