"""Compute probe: real-containment measurement, record, freeze gate and submit gate."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.cli import app as root_app
from gateway.research import cli as research_cli
from gateway.research import compute_probe as compute_probe_module
from gateway.research import containment, worker
from gateway.research.contracts import (
    COMPUTE_PROBE_CONTRACT,
    ComputeProbe,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisDecision,
    HypothesisSpec,
    HypothesisState,
    ProbeStage,
    compute_requirements,
)
from gateway.research.jobs import JobError
from gateway.research.machine import freeze as machine_freeze
from gateway.research.status import read_status
from gateway.research.store import ResearchStore, StoreConflict
from gateway.research.wake import compose_wake
from typer.testing import CliRunner

from tests.gateway.research.conftest import (
    freeze_with_probe,
    implementation,
    provenance_evidence,
    record_probe,
    run_plan,
)
from tests.gateway.research.test_golden_run import GoldenDraft, _call, golden_draft

MIB = 1024 * 1024
runner = CliRunner()

ALLOCATE = """\
import sys, time
block = bytearray(int(sys.argv[1]) * 1024 * 1024)
for offset in range(0, len(block), 4096):
    block[offset] = 1
time.sleep(float(sys.argv[2]))
"""


def _probe_stages(probe: ComputeProbe) -> dict[str, ProbeStage]:
    return {stage.stage: stage for stage in probe.stages}


def _fail_call(root: Path, *args: str) -> str:
    result = runner.invoke(root_app, ["research", *args, "--root", str(root)])
    assert result.exit_code == 1, f"{result.output}"
    return result.output


def _hid(draft: GoldenDraft) -> str:
    return draft.hypothesis.hypothesis_id


# -- measurement -------------------------------------------------------------------


def _measure(tmp_path: Path, megabytes: int) -> containment.StagePeak:
    script = tmp_path / "allocate.py"
    script.write_text(ALLOCATE, encoding="utf-8")
    plan = containment.scope_argv(
        f"peak{uuid.uuid4().hex[:10]}",
        "validate-c000",
        2048,
        (sys.executable, str(script), str(megabytes), "0.6"),
    )
    peak = containment.StagePeak()
    exit_code, timed_out = worker._stage(
        plan,
        tmp_path,
        tmp_path / f"logs{megabytes}" / "validate-c000.out",
        time.monotonic() + 60,
        peak,
    )
    assert (exit_code, timed_out) == (0, False)
    return peak


def test_stage_peak_measures_a_child_that_allocates_a_known_amount(tmp_path: Path) -> None:
    small = _measure(tmp_path, 100)
    large = _measure(tmp_path, 400)

    # Both independent sources see the allocation (python itself adds a few tens of MiB).
    assert 100 * MIB <= small.cgroup_bytes < 100 * MIB + 150 * MIB
    assert 100 * MIB <= small.rusage_bytes < 100 * MIB + 150 * MIB
    assert 400 * MIB <= large.cgroup_bytes < 400 * MIB + 150 * MIB
    assert 400 * MIB <= large.rusage_bytes < 400 * MIB + 150 * MIB
    assert 100 <= small.peak_mb < 250
    assert 400 <= large.peak_mb < 550
    # The measurement scales with what the child allocates.
    assert large.peak_mb - small.peak_mb > 250


def test_stage_peak_falls_back_to_rusage_without_cgroup_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(containment, "_CGROUP_ROOT", tmp_path / "no-cgroup-here")

    peak = _measure(tmp_path, 200)

    assert peak.cgroup_bytes == 0
    assert 200 * MIB <= peak.rusage_bytes < 350 * MIB
    assert peak.peak_mb == math.ceil(peak.rusage_bytes / MIB)


def test_stage_peak_ignores_a_cgroup_that_is_not_the_stage_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = next(
        line[3:]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    fake = tmp_path / "cgroup"
    directory = fake / own.lstrip("/")
    directory.mkdir(parents=True)
    (directory / "memory.peak").write_text("12345\n")
    monkeypatch.setattr(containment, "_CGROUP_ROOT", fake)
    peak = containment.StagePeak()

    # Right after launch the stage is still in the caller's cgroup: never read that one.
    peak.sample_cgroup(os.getpid(), "research-job-validate-c000")
    assert peak.cgroup_bytes == 0
    # Once the process is in the named scope its memory.peak is read.
    scope = own.rsplit("/", 1)[1].removesuffix(".scope")
    peak.sample_cgroup(os.getpid(), scope)
    assert peak.cgroup_bytes == 12345


# -- requirements ------------------------------------------------------------------


def _stage_set(peak: int, wall: float) -> tuple[ProbeStage, ...]:
    return (
        ProbeStage("validate-c000", "c000", 0, wall / 2, peak // 2 or 1),
        ProbeStage("evaluate-s000", "c000", 0, wall, peak),
    )


@pytest.mark.parametrize(
    ("peak", "minimum"), [(100, 125), (101, 127), (6300, 7875), (1, 2), (4, 5), (5, 7)]
)
def test_rss_requirement_is_the_integer_ceiling_of_1_25_times_peak(peak: int, minimum: int) -> None:
    probe = _probe(_stage_set(peak, 10.0))

    assert compute_requirements(probe).min_rss_mb == minimum


@pytest.mark.parametrize(("wall", "minimum"), [(40.0, 60), (40.1, 61), (642.0, 963), (0.2, 1)])
def test_timeout_requirement_is_the_ceiling_of_1_5_times_max_wall(
    wall: float, minimum: int
) -> None:
    probe = _probe(_stage_set(100, wall))

    assert compute_requirements(probe).min_scenario_timeout_seconds == minimum


def _probe(stages: tuple[ProbeStage, ...]) -> ComputeProbe:
    digest = hashlib.sha256(b"x").hexdigest()
    return ComputeProbe(
        COMPUTE_PROBE_CONTRACT,
        "P000000000001",
        "H0001",
        {
            key: digest
            for key in (
                "evaluator_sha256",
                "pyvenv_sha256",
                "shared_python_sha256",
                "snapshot_sha256",
                "universe_sha256",
            )
        },
        {
            key: digest
            for key in (
                "dividends_sha256",
                "evaluation_spec_set_sha256",
                "panel_sha256",
                "receipt_sha256",
            )
        },
        stages,
        "2026-01-01T00:00:00Z",
    )


def test_probe_record_round_trips_and_rejects_bad_stages() -> None:
    probe = _probe(_stage_set(100, 10.0))

    assert ComputeProbe.from_json(probe.to_json()) == probe
    with pytest.raises(ValueError, match="exit 0"):
        ProbeStage("validate-c000", "c000", 1, 1.0, 5)
    with pytest.raises(ValueError, match="positive"):
        ProbeStage("validate-c000", "c000", 0, 1.0, 0)
    with pytest.raises(ValueError, match="end with exactly one evaluate"):
        _probe(
            (
                ProbeStage("validate-c000", "c000", 0, 1.0, 5),
                ProbeStage("validate-c001", "c001", 0, 1.0, 5),
            )
        )


# -- real-containment probe --------------------------------------------------------


def test_probe_happy_path_measures_under_real_containment_and_records_evidence(
    tmp_path: Path,
) -> None:
    draft = golden_draft(tmp_path)
    store = draft.store
    hid = _hid(draft)

    result = _call(store.root, "compute-probe", hid)

    printed = json.loads(result.output)
    probe = store.compute_probe(hid)
    assert probe is not None and printed["probe_id"] == probe.probe_id
    assert probe.contract == COMPUTE_PROBE_CONTRACT and probe.hypothesis_id == hid
    stages = _probe_stages(probe)
    assert list(stages) == ["validate-c000", "validate-c001", "evaluate-s000"]
    assert stages["evaluate-s000"].spec_id == "c000"
    for stage in stages.values():
        assert stage.exit == 0
        assert 0 < stage.wall_seconds < 60
        # A real python interpreter inside bwrap/systemd-run: tens of MiB, far below the cap.
        assert 5 <= stage.peak_rss_mb < 1024
    assert printed["requirements"] == compute_requirements(probe).as_dict()
    assert printed["stages"] == [stage.to_json_value() for stage in probe.stages]

    pins, inputs = store.probe_bindings(hid)
    assert probe.pins == pins and probe.inputs == inputs
    assert probe.pins["evaluator_sha256"] == store.config()["evaluator_sha256"]
    assert probe.pins["snapshot_sha256"] == store.config()["snapshot_sha256"]
    assert probe.inputs["panel_sha256"] == draft.hypothesis.panel_sha256

    events = [event for event in store.events() if event.kind == "compute_probe_recorded"]
    assert len(events) == 1 and events[0].hypothesis_id == hid
    assert json.loads(events[0].detail)["probe_id"] == probe.probe_id
    assert (store.root / "hypotheses" / hid / "compute_probe.json").read_text() == probe.to_json()
    assert store.get_hypothesis(hid).state == HypothesisState.DRAFT

    # Scratch is under the hypothesis, never under an attempt run dir.
    scratch = store.root / "hypotheses" / hid / "compute-probe" / probe.probe_id
    assert (scratch / "empty-targets.json").read_text() == '{"targets":[]}'
    assert (scratch / "evaluator-stage" / "out" / "result.json").is_file()
    assert (scratch / "logs" / "validate-c001.out").is_file()
    assert not (store.root / "hypotheses" / hid / "attempts").exists()
    # The run lock was released.
    store.acquire_run_lock()
    store.release_run_lock()
    # The golden pins are loose enough: the recorded probe lets the frozen bounds through.
    assert store.freeze(hid).state == HypothesisState.FROZEN


def test_probe_evidence_is_immutable(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    _call(draft.store.root, "compute-probe", _hid(draft))

    with draft.store._connect() as conn, pytest.raises(Exception, match="insert-only"):
        conn.execute("UPDATE hypothesis_evidence SET payload_json='{}' WHERE kind='compute_probe'")
    with draft.store._connect() as conn, pytest.raises(Exception, match="insert-only"):
        conn.execute("DELETE FROM hypothesis_evidence WHERE kind='compute_probe'")


def test_second_probe_is_refused(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    _call(draft.store.root, "compute-probe", _hid(draft))
    first = draft.store.compute_probe(_hid(draft))

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "already recorded" in output
    assert draft.store.compute_probe(_hid(draft)) == first
    assert len([e for e in draft.store.events() if e.kind == "compute_probe_recorded"]) == 1


def test_probe_refused_when_not_draft(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    freeze_with_probe(draft.store, _hid(draft))

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "needs a DRAFT" in output
    assert not (draft.store.root / "hypotheses" / _hid(draft) / "compute-probe").exists()


def test_probe_refused_while_run_lock_is_held(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    holder = ResearchStore(draft.store.root)
    holder.acquire_run_lock()
    try:
        output = _fail_call(draft.store.root, "compute-probe", _hid(draft))
    finally:
        holder.release_run_lock()

    assert "run lock held" in output
    assert draft.store.compute_probe(_hid(draft)) is None
    assert not (draft.store.root / "hypotheses" / _hid(draft) / "compute-probe").exists()
    # And the lock the refused probe never took is still free for the next try.
    _call(draft.store.root, "compute-probe", _hid(draft))


def test_probe_stage_failure_records_nothing(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path, failing_first_spec=True)  # evaluate genuinely exits 1

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "evaluate-s000 exited 1" in output
    assert draft.store.compute_probe(_hid(draft)) is None
    assert all(e.kind != "compute_probe_recorded" for e in draft.store.events())
    assert draft.store.get_hypothesis(_hid(draft)).state == HypothesisState.DRAFT
    draft.store.acquire_run_lock()  # released after failure
    draft.store.release_run_lock()


def test_probe_stage_timeout_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = golden_draft(tmp_path)
    monkeypatch.setattr(compute_probe_module, "MAX_RUN_TIMEOUT_SECONDS", 0.01)

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "overall probe deadline" in output
    assert draft.store.compute_probe(_hid(draft)) is None
    assert all(e.kind != "compute_probe_recorded" for e in draft.store.events())


def test_probe_refuses_changed_input_file(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    Path(draft.hypothesis.panel_path).chmod(0o644)
    Path(draft.hypothesis.panel_path).write_text("tampered", encoding="utf-8")

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "panel changed" in output
    assert draft.store.compute_probe(_hid(draft)) is None


# -- freeze gate -------------------------------------------------------------------


def test_freeze_refused_without_a_probe(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)

    with pytest.raises(StoreConflict, match="requires a compute probe"):
        draft.store.freeze(_hid(draft))
    assert "compute probe" in _fail_call(draft.store.root, "hypothesis-freeze", _hid(draft))
    assert draft.store.get_hypothesis(_hid(draft)).state == HypothesisState.DRAFT


def _set_rss(draft: GoldenDraft, rss: int) -> None:
    _call(
        draft.store.root,
        "hypothesis-set-compute",
        _hid(draft),
        "--max-rss-mb",
        str(rss),
        "--max-wall-seconds",
        "120",
    )


def test_freeze_gate_requires_rss_headroom_and_allows_the_boundary(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    store = draft.store
    record_probe(store, _hid(draft), peak_rss_mb=101)  # ceil(1.25 * 101) = 127

    _set_rss(draft, 126)
    with pytest.raises(StoreConflict) as raised:
        store.freeze(_hid(draft))
    message = str(raised.value)
    assert "compute.max_rss_mb=126" in message
    assert "required minimum 127" in message
    assert "measured peak 101 MB" in message
    assert store.get_hypothesis(_hid(draft)).state == HypothesisState.DRAFT

    _set_rss(draft, 127)
    assert store.freeze(_hid(draft)).state == HypothesisState.FROZEN


def test_freeze_gate_rejects_probe_with_mismatched_pins_or_inputs(tmp_path: Path) -> None:
    for field, group in (("snapshot_sha256", "pins"), ("panel_sha256", "inputs")):
        sub = tmp_path / field
        sub.mkdir()
        draft = golden_draft(sub)
        store = draft.store
        probe = record_probe(store, _hid(draft))
        other = hashlib.sha256(b"other").hexdigest()
        forged = (
            replace(probe, pins={**probe.pins, field: other})
            if group == "pins"
            else replace(probe, inputs={**probe.inputs, field: other})
        )
        payload = forged.to_json()
        with store._connect() as conn:
            conn.execute("DROP TRIGGER immutable_hypothesis_evidence")
            conn.execute(
                "UPDATE hypothesis_evidence SET payload_json=?,payload_sha256=? "
                "WHERE kind='compute_probe'",
                (payload, hashlib.sha256(payload.encode()).hexdigest()),
            )
            conn.commit()

        with pytest.raises(StoreConflict, match="do not match"):
            store.freeze(_hid(draft))
        assert store.get_hypothesis(_hid(draft)).state == HypothesisState.DRAFT


def test_record_refuses_a_probe_bound_to_other_digests(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    probe = record_probe(draft.store, _hid(draft))
    other = golden_draft(tmp_path / "other")
    bad = replace(
        probe,
        probe_id="P000000000002",
        hypothesis_id=_hid(other),
        inputs={**probe.inputs, "panel_sha256": hashlib.sha256(b"x").hexdigest()},
    )

    with pytest.raises(StoreConflict, match="do not match"):
        other.store.record_compute_probe(bad)
    with pytest.raises(StoreConflict, match="already recorded"):
        draft.store.record_compute_probe(probe)


def test_set_compute_changes_only_the_compute_block_of_a_draft(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    before = json.loads(draft.hypothesis.spec_json)

    _call(
        draft.store.root,
        "hypothesis-set-compute",
        _hid(draft),
        "--max-rss-mb",
        "2048",
        "--max-wall-seconds",
        "900",
    )

    after_spec = draft.store.get_hypothesis(_hid(draft))
    after = json.loads(after_spec.spec_json)
    assert after.pop("compute") == {"max_rss_mb": 2048, "max_wall_seconds": 900.0}
    before.pop("compute")
    assert after == before
    assert after_spec.spec_sha256 == hashlib.sha256(after_spec.spec_json.encode()).hexdigest()
    projected = draft.store.root / "hypotheses" / _hid(draft) / "spec.json"
    assert projected.read_text() == after_spec.spec_json

    freeze_with_probe(draft.store, _hid(draft))
    assert "DRAFT" in _fail_call(
        draft.store.root,
        "hypothesis-set-compute",
        _hid(draft),
        "--max-rss-mb",
        "4096",
        "--max-wall-seconds",
        "900",
    )


# -- submit gate -------------------------------------------------------------------


def _submit(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], timeout: float, *, wall: float | None
) -> None:
    store, source, hypothesis = campaign
    if wall is None:
        # A hypothesis frozen before the probe existed has no probe evidence at all.
        store._update_hypothesis(
            machine_freeze(store.get_hypothesis(hypothesis.hypothesis_id)),
            "hypothesis_frozen",
            expected=HypothesisState.DRAFT,
        )
    else:
        # The fixture's compute.max_wall_seconds (120) is below what the freeze cross-check
        # needs for a 40 s measured stage (3 x 60 + 1); raise it as the owner would.
        store.set_draft_compute(hypothesis.hypothesis_id, 900.0, 1024)
        record_probe(store, hypothesis.hypothesis_id, wall_seconds=wall)
        store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    record = implementation(attempt.attempt_id, "a" * 40)
    plan = replace(run_plan(store, attempt, record), scenario_timeout_seconds=timeout)
    store.submit_implementation(
        attempt.attempt_id,
        record,
        run_plan=plan,
        containment_provenance=provenance_evidence(attempt.attempt_id, record.commit),
    )


def test_submit_refused_below_one_and_a_half_times_measured_wall(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign

    with pytest.raises(StoreConflict) as raised:
        _submit(campaign, 59.0, wall=40.0)

    message = str(raised.value)
    assert "scenario_timeout_seconds=59" in message and "required minimum 60" in message
    attempt = store.attempts_for(hypothesis.hypothesis_id)[0]
    assert attempt.state.value == "OPENED"  # no mutation happened
    with pytest.raises(ValueError, match="missing run_plan"):
        store.evidence(attempt.attempt_id, "run_plan")


def test_submit_allowed_at_the_boundary(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign

    _submit(campaign, 60.0, wall=40.0)

    assert store.attempts_for(hypothesis.hypothesis_id)[0].state.value == "IMPLEMENTED"


def test_hypothesis_without_a_probe_keeps_the_old_submit_behaviour(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign

    _submit(campaign, 1.0, wall=None)

    assert store.compute_probe(hypothesis.hypothesis_id) is None
    assert store.attempts_for(hypothesis.hypothesis_id)[0].state.value == "IMPLEMENTED"


# -- status ------------------------------------------------------------------------


def test_status_reports_probe_state_for_a_draft(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    store = draft.store
    hid = _hid(draft)

    def cli_probe() -> dict[str, object]:
        result = runner.invoke(
            root_app, ["research", "status", "--root", str(store.root), "--json"]
        )
        assert result.exit_code == 0, result.output
        entry = json.loads(result.output)["hypotheses"][0]
        assert entry["hypothesis_id"] == hid
        probe = entry["compute_probe"]
        assert isinstance(probe, dict)
        return probe

    def frame() -> dict[str, object]:
        result = runner.invoke(root_app, ["research-status", "--root", str(store.root)])
        assert result.exit_code == 0, result.output
        # rich colours the JSON when FORCE_COLOR is set.
        loaded = json.loads(re.sub(r"\x1b\[[0-9;]*m", "", result.output))
        assert isinstance(loaded, dict)
        return loaded

    assert cli_probe() == {"recorded": False}
    assert frame()["computeProbe"] == {"recorded": False}

    record_probe(store, hid, peak_rss_mb=100, wall_seconds=40.0)

    probe = cli_probe()
    stored = store.compute_probe(hid)
    assert stored is not None
    requirements = compute_requirements(stored)
    assert probe["recorded"] is True and probe["requirements"] == requirements.as_dict()
    assert requirements.min_rss_mb == 125 and requirements.min_scenario_timeout_seconds == 60
    framed = frame()["computeProbe"]
    assert isinstance(framed, dict) and framed["requirements"] == requirements.as_dict()
    assert read_status(store.root, unit_state=None).compute_probe == framed

    store.set_draft_compute(hid, 900.0, 1024)  # 3 x 60 + 1 > the fixture's 120 s
    store.freeze(hid)
    # Once frozen the additive field disappears again.
    assert (
        "compute_probe"
        not in json.loads(
            runner.invoke(
                root_app, ["research", "status", "--root", str(store.root), "--json"]
            ).output
        )["hypotheses"][0]
    )
    assert "computeProbe" not in frame()


# -- DRAFT abandon exit ------------------------------------------------------------


def _second_spec_set(draft: GoldenDraft, tmp_path: Path) -> tuple[Path, Path]:
    primary = Path(draft.hypothesis.evaluation_spec_path)
    manifest = tmp_path / "second-set.json"
    manifest.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0002",
            "c000",
            (
                EvaluationSpecEntry(
                    "c000", str(primary), hashlib.sha256(primary.read_bytes()).hexdigest()
                ),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    spec = tmp_path / "second-spec.json"
    spec.write_text(draft.hypothesis.spec_json, encoding="utf-8")
    return manifest, spec


def test_draft_can_be_abandoned_and_unblocks_the_next_hypothesis(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    store = draft.store
    hid = _hid(draft)
    with pytest.raises(StoreConflict, match="compute probe"):
        store.freeze(hid)  # the DRAFT cannot freeze ...
    with pytest.raises(ValueError, match="requires every existing hypothesis to be DECIDED"):
        manifest, spec = _second_spec_set(draft, tmp_path)  # ... and blocks creation
        store.create_hypothesis(
            "second",
            spec,
            Path(draft.hypothesis.panel_path),
            Path(draft.hypothesis.receipt_path),
            Path(draft.hypothesis.evaluation_spec_path),
            draft.hypothesis.base_commit,
            dividends=Path(draft.hypothesis.dividends_path),
            evaluation_spec_set=manifest,
        )

    result = _call(
        store.root,
        "hypothesis-decide",
        hid,
        "--decision",
        "ABANDONED",
        "--reason",
        "infeasible probe",
    )

    assert result.output.strip() == "DECIDED"
    assert store.get_hypothesis(hid).state == HypothesisState.DECIDED
    decided = [e for e in store.events() if e.kind == "hypothesis_decided"]
    assert len(decided) == 1
    assert json.loads(decided[0].detail) == {
        "decision": "ABANDONED",
        "from_state": "DRAFT",
        "reason": "infeasible probe",
    }
    second = store.create_hypothesis(
        "second",
        spec,
        Path(draft.hypothesis.panel_path),
        Path(draft.hypothesis.receipt_path),
        Path(draft.hypothesis.evaluation_spec_path),
        draft.hypothesis.base_commit,
        dividends=Path(draft.hypothesis.dividends_path),
        evaluation_spec_set=manifest,
    )
    assert second.hypothesis_id == "H0002" and second.state == HypothesisState.DRAFT


def test_draft_cannot_be_finished(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)

    output = _fail_call(
        draft.store.root,
        "hypothesis-decide",
        _hid(draft),
        "--decision",
        "FINISHED",
        "--reason",
        "no evidence",
    )

    assert "illegal transition" in output
    assert draft.store.get_hypothesis(_hid(draft)).state == HypothesisState.DRAFT
    assert all(e.kind != "hypothesis_decided" for e in draft.store.events())


def test_frozen_hypothesis_decision_is_unchanged_and_has_no_from_state(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    freeze_with_probe(draft.store, _hid(draft))

    draft.store.decide_hypothesis(_hid(draft), HypothesisDecision.FINISHED, "ok")

    detail = json.loads(
        next(e for e in draft.store.events() if e.kind == "hypothesis_decided").detail
    )
    assert detail == {"decision": "FINISHED", "reason": "ok"}


@pytest.mark.parametrize(
    ("peak", "wall", "needle"),
    [(14000, 1.0, "needs compute.max_rss_mb >= 17500"), (10, 20000.0, "needs scenario_timeout")],
)
def test_unmeetable_probe_requirements_name_the_abandon_exit(
    tmp_path: Path, peak: int, wall: float, needle: str
) -> None:
    draft = golden_draft(tmp_path)
    record_probe(draft.store, _hid(draft), peak_rss_mb=peak, wall_seconds=wall)

    with pytest.raises(StoreConflict) as raised:
        draft.store.freeze(_hid(draft))

    message = str(raised.value)
    assert "infeasible within driver bounds" in message and needle in message
    assert f"hypothesis-decide {_hid(draft)}" in message and "ABANDONED" in message


def test_unparseable_spec_refusal_names_the_abandon_exit(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    record_probe(draft.store, _hid(draft))
    with draft.store._connect() as conn:  # a legacy-style draft whose spec is not a document
        spec = replace(draft.hypothesis, spec_json='{"objective":"x"}')
        payload = spec.to_json()
        conn.execute(
            "UPDATE hypotheses SET spec_json=?,payload_json=?,payload_sha256=? "
            "WHERE hypothesis_id=?",
            (spec.spec_json, payload, hashlib.sha256(payload.encode()).hexdigest(), _hid(draft)),
        )
        conn.commit()

    with pytest.raises(StoreConflict, match="ABANDONED"):
        draft.store.freeze(_hid(draft))


def test_probe_failure_text_names_the_abandon_exit(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path, failing_first_spec=True)

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "infeasible within driver bounds" in output and "ABANDONED" in output


# -- run --max-rss-mb --------------------------------------------------------------


def test_explicit_run_rss_flag_cannot_undercut_probe_or_frozen_bound(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    store, hid = draft.store, _hid(draft)
    record_probe(store, hid, peak_rss_mb=100)  # min 125
    _set_rss(draft, 200)
    store.freeze(hid)

    for too_small in (1, 124, 199):
        with pytest.raises(JobError, match="below the minimum 200"):
            research_cli._resolve_run_max_rss(store, hid, too_small)
    assert research_cli._resolve_run_max_rss(store, hid, 200) == 200
    assert research_cli._resolve_run_max_rss(store, hid, 4096) == 4096
    assert research_cli._resolve_run_max_rss(store, hid, None) == 200


def test_explicit_run_rss_flag_is_unchanged_for_a_hypothesis_without_a_probe(
    tmp_path: Path,
) -> None:
    draft = golden_draft(tmp_path)
    store, hid = draft.store, _hid(draft)
    store._update_hypothesis(
        machine_freeze(store.get_hypothesis(hid)),
        "hypothesis_frozen",
        expected=HypothesisState.DRAFT,
    )

    assert research_cli._resolve_run_max_rss(store, hid, 1) == 1


# -- probe record validation -------------------------------------------------------


def test_validate_stage_name_must_match_its_spec_id() -> None:
    with pytest.raises(ValueError, match="must match its spec_id"):
        ProbeStage("validate-c000", "c001", 0, 1.0, 5)


def test_record_requires_validate_stages_to_cover_exactly_the_spec_set(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)  # spec set is c000, c001
    pins, inputs = draft.store.probe_bindings(_hid(draft))

    def probe_of(*stages: ProbeStage) -> ComputeProbe:
        return ComputeProbe(
            COMPUTE_PROBE_CONTRACT,
            "P000000000001",
            _hid(draft),
            pins,
            inputs,
            stages,
            "2026-01-01T00:00:00Z",
        )

    v0 = ProbeStage("validate-c000", "c000", 0, 1.0, 5)
    v1 = ProbeStage("validate-c001", "c001", 0, 1.0, 5)
    evaluate = ProbeStage("evaluate-s000", "c000", 0, 1.0, 5)
    for bad in (
        probe_of(v0, evaluate),  # a spec was not validated
        probe_of(v1, v0, evaluate),  # wrong order
        probe_of(v0, v1, ProbeStage("evaluate-s000", "c001", 0, 1.0, 5)),  # not the first spec
        probe_of(v0, v1, ProbeStage("validate-c002", "c002", 0, 1.0, 5), evaluate),  # extra spec
    ):
        with pytest.raises(StoreConflict, match="cover exactly"):
            draft.store.record_compute_probe(bad)
    assert draft.store.compute_probe(_hid(draft)) is None
    draft.store.record_compute_probe(probe_of(v0, v1, evaluate))


# -- status digest, scratch directories, deadline, wake ----------------------------


def test_status_ignores_a_probe_whose_payload_digest_does_not_match(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    record_probe(draft.store, _hid(draft))
    with draft.store._connect() as conn:
        conn.execute("DROP TRIGGER immutable_hypothesis_evidence")
        conn.execute(
            "UPDATE hypothesis_evidence SET payload_sha256=? WHERE kind='compute_probe'",
            ("0" * 64,),
        )
        conn.commit()

    status = read_status(draft.store.root, unit_state=None)

    assert status.compute_probe == {
        "recorded": False,
        "unavailable_reason": "compute probe payload digest mismatch",
    }


def test_probe_refuses_a_symlinked_scratch_directory(tmp_path: Path) -> None:
    draft = golden_draft(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (draft.store.root / "hypotheses" / _hid(draft) / "compute-probe").symlink_to(elsewhere)

    output = _fail_call(draft.store.root, "compute-probe", _hid(draft))

    assert "scratch is not usable" in output
    assert list(elsewhere.iterdir()) == []
    assert draft.store.compute_probe(_hid(draft)) is None


def test_probe_uses_one_overall_deadline_for_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = golden_draft(tmp_path)
    deadlines: list[float] = []
    real = worker._stage

    def spy(
        plan: object, cwd: Path, out: Path, deadline: float, peak: object = None
    ) -> tuple[int, bool]:
        deadlines.append(deadline)
        return real(plan, cwd, out, deadline, peak)  # type: ignore[arg-type]

    monkeypatch.setattr(compute_probe_module, "run_contained_stage", spy)

    _call(draft.store.root, "compute-probe", _hid(draft))

    assert len(deadlines) == 3 and len(set(deadlines)) == 1


def test_wake_texts_give_the_real_freeze_sequence(
    tmp_path: Path, campaign: tuple[ResearchStore, Path, HypothesisSpec]
) -> None:
    empty = ResearchStore(tmp_path / "empty-driver")
    first = compose_wake(empty)
    assert first is not None and first.state == "NO_HYPOTHESIS"
    for text in (first.message,):
        for needle in (
            "hypothesis-create",
            "compute-probe H0001",
            "hours",
            "hypothesis-set-compute",
            "hypothesis-freeze",
            "ABANDONED",
        ):
            assert needle in text, (needle, text)

    store, _source, hypothesis = campaign
    hid = hypothesis.hypothesis_id
    draft = compose_wake(store)
    assert draft is not None and draft.state == "DRAFT"
    for needle in (
        f"compute-probe {hid}",
        "no compute probe",
        "hours",
        "hypothesis-set-compute",
        "hypothesis-freeze",
        "ABANDONED",
    ):
        assert needle in draft.message, (needle, draft.message)

    record_probe(store, hid, peak_rss_mb=100, wall_seconds=40.0)
    probed = compose_wake(store)
    assert probed is not None and probed.state == "DRAFT"
    for needle in (
        "min_rss_mb=125",
        "min_scenario_timeout_seconds=60",
        "hypothesis-set-compute",
        "hypothesis-freeze",
        "ABANDONED",
    ):
        assert needle in probed.message, (needle, probed.message)
    assert "compute-probe" not in probed.message

    store.set_draft_compute(hid, 900.0, 1024)  # 3 x 60 + 1 > the fixture's 120 s
    store.freeze(hid)
    store.decide_hypothesis(hid, HypothesisDecision.FINISHED, "done")
    after = compose_wake(store)
    assert after is not None and after.state == "ALL_DECIDED"
    for needle in (
        "compute-probe H0002",
        "hypothesis-set-compute",
        "hypothesis-freeze",
        "ABANDONED",
    ):
        assert needle in after.message, (needle, after.message)


# -- freeze/abandon race and timeout feasibility -----------------------------------


def test_freeze_cannot_resurrect_a_draft_abandoned_during_its_slow_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = golden_draft(tmp_path)
    store, hid = draft.store, _hid(draft)
    record_probe(store, hid)
    real_gate = store._require_compute_probe

    def gate_then_abandon(hypothesis: HypothesisSpec) -> None:
        real_gate(hypothesis)  # freeze has read DRAFT and passed its checks ...
        ResearchStore(store.root).decide_hypothesis(  # ... then the abandon commits
            hid, HypothesisDecision.ABANDONED, "raced"
        )

    monkeypatch.setattr(store, "_require_compute_probe", gate_then_abandon)

    with pytest.raises(StoreConflict, match="no longer DRAFT"):
        store.freeze(hid)

    assert store.get_hypothesis(hid).state == HypothesisState.DECIDED
    with store._connect() as conn:
        assert conn.execute("SELECT state FROM hypotheses").fetchone()[0] == "DECIDED"
    assert all(e.kind != "hypothesis_frozen" for e in store.events())


@pytest.mark.parametrize(
    ("minimum", "feasible"), [(9499, True), (9500, False), (28800, False), (1, True)]
)
def test_timeout_feasibility_is_the_smallest_run_plan_budget(minimum: int, feasible: bool) -> None:
    # 3 stages x t + 1 s analysis + 300 s overhead <= 28800  <=>  t <= 9499
    probe = _probe(_stage_set(100, 1.0))
    requirements = replace(compute_requirements(probe), min_scenario_timeout_seconds=minimum)

    assert requirements.timeout_feasible is feasible


def test_freeze_gate_allows_the_timeout_boundary_and_refuses_one_second_more(
    tmp_path: Path,
) -> None:
    ok = golden_draft(tmp_path / "ok")
    ok.store.set_draft_compute(_hid(ok), 28800.0, 1024)  # 3 x 9499 + 1 = 28498
    record_probe(ok.store, _hid(ok), wall_seconds=6332.6)  # ceil(1.5 x) = 9499
    assert ok.store.freeze(_hid(ok)).state == HypothesisState.FROZEN

    bad = golden_draft(tmp_path / "bad")
    bad.store.set_draft_compute(_hid(bad), 28800.0, 1024)
    record_probe(bad.store, _hid(bad), wall_seconds=6333.0)  # ceil(1.5 x) = 9500
    with pytest.raises(StoreConflict) as raised:
        bad.store.freeze(_hid(bad))
    message = str(raised.value)
    assert "infeasible within driver bounds" in message and "smallest run plan" in message
    assert "needs scenario_timeout_seconds >= 9500" in message
    assert f"hypothesis-decide {_hid(bad)}" in message and "ABANDONED" in message
    assert bad.store.get_hypothesis(_hid(bad)).state == HypothesisState.DRAFT


@pytest.mark.parametrize(
    ("max_wall", "allowed"),
    [(181.0, True), (180.0, False), (180.9, False), (900.0, True)],
)
def test_freeze_requires_max_wall_to_cover_the_smallest_plan_the_probe_allows(
    tmp_path: Path, max_wall: float, allowed: bool
) -> None:
    # Measured stage wall 40 s -> min_scenario_timeout_seconds 60 -> smallest plan budget
    # MIN_PLAN_SCENARIO_STAGES (3) x 60 + MIN_ANALYSIS_SECONDS (1) = 181 s.
    draft = golden_draft(tmp_path)
    store, hid = draft.store, _hid(draft)
    store.set_draft_compute(hid, max_wall, 1024)
    record_probe(store, hid, wall_seconds=40.0)
    if allowed:
        assert store.freeze(hid).state == HypothesisState.FROZEN
        return
    with pytest.raises(StoreConflict) as raised:
        store.freeze(hid)
    message = str(raised.value)
    assert f"compute.max_wall_seconds={max_wall:g} is below 181" in message
    assert "3 stages x min_scenario_timeout_seconds 60 + 1s analysis" in message
    assert "raise --max-wall-seconds" in message and "hypothesis-set-compute" in message
    assert f"hypothesis-decide {hid}" in message and "ABANDONED" in message
    assert store.get_hypothesis(hid).state == HypothesisState.DRAFT


def test_freeze_passes_the_max_wall_cross_check_for_a_small_measured_wall(
    tmp_path: Path,
) -> None:
    # A tiny measured wall needs 3 x 1 + 1 = 4 s, far below the fixture's 120 s.
    draft = golden_draft(tmp_path)
    record_probe(draft.store, _hid(draft), wall_seconds=0.5)
    assert draft.store.freeze(_hid(draft)).state == HypothesisState.FROZEN


def _raising_stage(error: Exception, sleep_until: float | None = None):  # type: ignore[no-untyped-def]
    def fake(*_args: object) -> tuple[int, bool]:
        if sleep_until is not None:
            while time.monotonic() < sleep_until:
                time.sleep(0.001)
        raise error

    return fake


def test_run_stage_reports_deadline_when_stop_fails_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = time.monotonic() + 0.01
    error = containment.ContainmentError("stage scope termination could not be verified")
    monkeypatch.setattr(
        compute_probe_module, "run_contained_stage", _raising_stage(error, deadline)
    )

    with pytest.raises(compute_probe_module.ComputeProbeError) as raised:
        compute_probe_module._run_stage(
            object(),  # type: ignore[arg-type]
            tmp_path,
            "validate-c000",
            "c000",
            deadline,
            "hint",
        )

    message = str(raised.value)
    assert "overall probe deadline" in message
    assert "stage scope termination could not be verified" in message


def test_run_stage_reraises_containment_errors_before_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = containment.ContainmentError("bubblewrap/systemd-run is unavailable")
    monkeypatch.setattr(compute_probe_module, "run_contained_stage", _raising_stage(error))

    with pytest.raises(containment.ContainmentError) as raised:
        compute_probe_module._run_stage(
            object(),  # type: ignore[arg-type]
            tmp_path,
            "validate-c000",
            "c000",
            time.monotonic() + 3600,
            "hint",
        )

    assert raised.value is error
