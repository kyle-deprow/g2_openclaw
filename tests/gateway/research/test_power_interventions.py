"""Power-gated hypothesis creation, ``power-check``, and the operator-intervention metric."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from gateway.cli import app
from gateway.research.admission import AdmissionReason, admit_hypothesis
from gateway.research.contracts import (
    AttemptState,
    HypothesisDecision,
    HypothesisSpec,
    HypothesisState,
)
from gateway.research.hypothesis import HypothesisDocument
from gateway.research.status import build_status_frame, read_status
from gateway.research.store import ResearchStore
from gateway.research.wake import compose_wake
from typer.testing import CliRunner

from tests.gateway.research.conftest import freeze_with_probe
from tests.gateway.research.test_admission import _payload, _payload_v1, _power_block

runner = CliRunner()
Campaign = tuple[ResearchStore, Path, HypothesisSpec]


def _invoke(*args: str, expect: int = 0) -> str:
    result = runner.invoke(app, ["research", *args])
    assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result.output


def _note(root: Path, hypothesis_id: str, *extra: str, expect: int = 0, **overrides: str) -> str:
    fields = {"--kind": "audit", "--reason": "checked the run", "--operator-reference": "turn-1"}
    fields.update({f"--{key.replace('_', '-')}": value for key, value in overrides.items()})
    args = [item for pair in fields.items() for item in pair]
    return _invoke(
        "operator-note", hypothesis_id, "--root", str(root), *args, *extra, expect=expect
    )


# --- hypothesis-create power gate ------------------------------------------------------------


def _decided(campaign: Campaign) -> ResearchStore:
    store, _source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.FINISHED, "done")
    return store


def _create_refused(campaign: Campaign, tmp_path: Path, payload: dict[str, object]) -> str:
    store = _decided(campaign)
    hypothesis = campaign[2]
    spec_file = tmp_path / "next-spec.json"
    spec_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        store.create_hypothesis(
            "next",
            spec_file,
            Path(hypothesis.panel_path),
            Path(hypothesis.receipt_path),
            Path(hypothesis.evaluation_spec_path),
            "b" * 40,
            dividends=Path(hypothesis.dividends_path),
            evaluation_spec_set=tmp_path / "never-read.json",
        )
    assert [h.hypothesis_id for h in store.hypotheses()] == ["H0001"]
    return str(error.value)


def test_create_accepts_v2_with_a_plausible_design(campaign: Campaign) -> None:
    store, _source, hypothesis = campaign

    document = json.loads(hypothesis.spec_json)
    assert document["contract"] == "research-hypothesis-v2"
    assert document["power"]["minimum_detectable_effect_bps"] == 24.86
    assert store.get_hypothesis(hypothesis.hypothesis_id).state == HypothesisState.DRAFT


def test_create_refuses_a_v1_spec(campaign: Campaign, tmp_path: Path) -> None:
    message = _create_refused(campaign, tmp_path, _payload_v1())

    assert "requires contract research-hypothesis-v2" in message


def test_create_refuses_an_underpowered_v2_design(campaign: Campaign, tmp_path: Path) -> None:
    payload = _payload()
    power = _power_block()
    power["plausible_effect_bps"] = 24.0  # MDE 24.86 exceeds a 24 bp plausible effect
    payload["power"] = power

    message = _create_refused(campaign, tmp_path, payload)

    assert message == "underpowered design: MDE 24.9 bps exceeds plausible effect 24 bps"


def test_create_gate_uses_the_recomputed_mde_not_a_lowered_declaration(
    campaign: Campaign, tmp_path: Path
) -> None:
    payload = _payload()
    power = _power_block()
    power["minimum_detectable_effect_bps"] = 24.4  # within 0.5 bp tolerance of 24.86
    power["plausible_effect_bps"] = 24.6
    payload["power"] = power

    assert "underpowered design" in _create_refused(campaign, tmp_path, payload)


def test_create_refuses_an_invalid_power_block(campaign: Campaign, tmp_path: Path) -> None:
    payload = _payload()
    power = _power_block()
    power["minimum_detectable_effect_bps"] = 5.0
    payload["power"] = power

    assert "does not match the recomputed" in _create_refused(campaign, tmp_path, payload)


def test_create_refuses_a_non_canonical_spec(campaign: Campaign, tmp_path: Path) -> None:
    payload = _payload()
    payload["compute"] = {"max_wall_seconds": 120, "max_rss_mb": 1024}  # parses, but 120 != 120.0

    message = _create_refused(campaign, tmp_path, payload)

    assert message.startswith("spec is not in canonical form; re-serialize it")
    assert "max_wall_seconds" in message


@pytest.fixture
def integer_bps_campaign(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Campaign:
    """A campaign whose v2 spec writes its bps values as JSON integers (100, 25, 30)."""
    from tests.gateway.research import test_admission

    def integer_payload() -> dict[str, object]:
        payload = _payload()
        power = _power_block()
        power.update(
            {
                "per_event_sd_bps": 100,
                "minimum_detectable_effect_bps": 25,
                "plausible_effect_bps": 30,
            }
        )
        payload["power"] = power
        return payload

    monkeypatch.setattr(test_admission, "_payload", integer_payload)
    return request.getfixturevalue("campaign")  # type: ignore[no-any-return]


def test_integer_bps_values_create_freeze_and_admit(integer_bps_campaign: Campaign) -> None:
    store, _source, hypothesis = integer_bps_campaign
    stored = json.loads(hypothesis.spec_json)["power"]
    assert stored["per_event_sd_bps"] == 100 and isinstance(stored["per_event_sd_bps"], int)
    assert '"per_event_sd_bps":100,' in hypothesis.spec_json
    document = HypothesisDocument.from_json(hypothesis.spec_json)
    assert document.to_json() == hypothesis.spec_json
    assert document.sha256 == hypothesis.spec_sha256

    frozen = freeze_with_probe(store, hypothesis.hypothesis_id)

    assert frozen.state == HypothesisState.FROZEN
    from gateway.research.admission import CampaignPolicy, ExecutionCapability

    from tests.gateway.research.test_admission import _bounds, _ledger, _receipt

    decision = admit_hypothesis(
        document,
        frozen,
        _receipt(),
        _ledger(),
        _bounds(),
        CampaignPolicy(True, 3),
        ExecutionCapability(frozenset({"panel"})),
        evaluation_spec_set_sha256="0" * 64,
    )
    assert decision.reason not in {
        AdmissionReason.HYPOTHESIS_SPEC_MISMATCH,
        AdmissionReason.SPEC_DIGEST_MISMATCH,
    }


def test_set_draft_compute_preserves_the_power_block(campaign: Campaign) -> None:
    store, _source, hypothesis = campaign

    updated = store.set_draft_compute(hypothesis.hypothesis_id, 90.0, 2048)

    assert json.loads(updated.spec_json)["power"] == _power_block()


# --- power-check CLI -------------------------------------------------------------------------


def test_power_check_prints_the_h0008_mde() -> None:
    output = _invoke("power-check", "--events", "107", "--sd-bps", "194")

    data = json.loads(output)
    assert data["minimum_detectable_effect_bps"] == pytest.approx(46.7, abs=0.1)
    assert data == {
        "alpha": 0.05,
        "expected_events": 107,
        "minimum_detectable_effect_bps": data["minimum_detectable_effect_bps"],
        "per_event_sd_bps": 194.0,
        "power": 0.8,
        "sided": "one",
    }


def test_power_check_two_sided_and_options() -> None:
    one = json.loads(_invoke("power-check", "--events", "100", "--sd-bps", "100"))
    two = json.loads(_invoke("power-check", "--events", "100", "--sd-bps", "100", "--sided", "two"))
    strict = json.loads(
        _invoke("power-check", "--events", "100", "--sd-bps", "100", "--alpha", "0.01")
    )

    assert one["minimum_detectable_effect_bps"] == pytest.approx(24.865, abs=0.001)
    assert two["minimum_detectable_effect_bps"] > one["minimum_detectable_effect_bps"]
    assert strict["minimum_detectable_effect_bps"] > one["minimum_detectable_effect_bps"]


@pytest.mark.parametrize(
    "args",
    [
        ("--events", "0", "--sd-bps", "100"),
        ("--events", "10", "--sd-bps", "-1"),
        ("--events", "10", "--sd-bps", "100", "--alpha", "0.9"),
        ("--events", "10", "--sd-bps", "100", "--power", "0.2"),
        ("--events", "10", "--sd-bps", "100", "--sided", "both"),
    ],
)
def test_power_check_refuses_invalid_input(args: tuple[str, ...]) -> None:
    output = _invoke("power-check", *args, expect=1)

    assert output.startswith(("power.", "invalid power."))
    assert "minimum_detectable_effect_bps" not in output


def test_power_check_needs_no_root_and_creates_no_files(tmp_path: Path) -> None:
    before = set(Path.cwd().iterdir())
    _invoke("power-check", "--events", "10", "--sd-bps", "100")

    assert set(Path.cwd().iterdir()) == before


# --- operator-note and the operatorInterventions metric --------------------------------------


def _attempt(campaign: Campaign) -> tuple[ResearchStore, HypothesisSpec, str]:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    return store, hypothesis, attempt.attempt_id


def _counts(store: ResearchStore) -> dict[str, int] | None:
    return read_status(store.root, unit_state=lambda _: "inactive").operator_interventions


def test_operator_note_records_one_event_and_changes_nothing_else(campaign: Campaign) -> None:
    store, hypothesis, attempt_id = _attempt(campaign)
    hypothesis_before = store.get_hypothesis(hypothesis.hypothesis_id)
    attempt_before = store.get_attempt(attempt_id)
    campaign_before = store.campaign()
    wake_before = compose_wake(store)
    status_before = read_status(store.root, unit_state=lambda _: "inactive")
    events_before = store.events()

    output = _note(store.root, hypothesis.hypothesis_id, "--attempt", attempt_id)

    assert output.startswith("recorded operator_intervention seq=")
    events = store.events()
    assert len(events) == len(events_before) + 1
    event = events[-1]
    assert (event.kind, event.actor, event.hypothesis_id, event.attempt_id) == (
        "operator_intervention",
        "operator",
        hypothesis.hypothesis_id,
        attempt_id,
    )
    assert json.loads(event.detail) == {
        "kind": "audit",
        "reason": "checked the run",
        "operator_reference": "turn-1",
    }
    assert store.get_hypothesis(hypothesis.hypothesis_id) == hypothesis_before
    assert store.get_attempt(attempt_id) == attempt_before
    assert store.campaign() == campaign_before
    assert compose_wake(store) == wake_before
    status_after = read_status(store.root, unit_state=lambda _: "inactive")
    assert (status_after.stage, status_after.boundary_failure, status_after.attempt_state) == (
        status_before.stage,
        status_before.boundary_failure,
        status_before.attempt_state,
    )
    assert status_after.last_astra_decision == status_before.last_astra_decision
    assert status_after.operator_interventions == {
        "hypothesis": (status_before.operator_interventions or {})["hypothesis"] + 1,
        "attempt": (status_before.operator_interventions or {})["attempt"] + 1,
    }


def test_operator_note_without_attempt_counts_for_the_hypothesis_only(
    campaign: Campaign,
) -> None:
    store, hypothesis, _attempt_id = _attempt(campaign)
    before = _counts(store)
    assert before is not None

    _note(store.root, hypothesis.hypothesis_id, kind="brief")

    assert _counts(store) == {"hypothesis": before["hypothesis"] + 1, "attempt": before["attempt"]}
    assert store.events()[-1].attempt_id is None


@pytest.mark.parametrize("kind", ["brief", "audit", "hold", "abort", "other"])
def test_operator_note_accepts_every_documented_kind(campaign: Campaign, kind: str) -> None:
    store, _source, hypothesis = campaign

    _note(store.root, hypothesis.hypothesis_id, kind=kind)

    assert json.loads(store.events()[-1].detail)["kind"] == kind


def test_operator_note_kind_is_a_typer_choice(campaign: Campaign) -> None:
    store, _source, hypothesis = campaign
    before = len(store.events())

    result = runner.invoke(
        app,
        [
            "research",
            "operator-note",
            hypothesis.hypothesis_id,
            "--root",
            str(store.root),
            "--kind",
            "bogus",
            "--reason",
            "r",
            "--operator-reference",
            "ref",
        ],
    )

    assert result.exit_code != 0
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "Invalid value for '--kind'" in output
    assert "'bogus' is not one of" in output
    assert len(store.events()) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": ""},
        {"reason": "   "},
        {"operator_reference": ""},
        {"operator_reference": " "},
    ],
)
def test_operator_note_refuses_bad_fields_without_recording(
    campaign: Campaign, overrides: dict[str, str]
) -> None:
    store, _source, hypothesis = campaign
    before = len(store.events())

    _note(store.root, hypothesis.hypothesis_id, expect=1, **overrides)

    assert len(store.events()) == before


def test_operator_note_refuses_unknown_hypothesis_and_attempt(campaign: Campaign) -> None:
    store, hypothesis, _attempt_id = _attempt(campaign)
    before = len(store.events())

    _note(store.root, "H0099", expect=1)
    _note(store.root, hypothesis.hypothesis_id, "--attempt", "H0001-A099", expect=1)

    assert len(store.events()) == before


def test_operator_note_refuses_an_attempt_of_another_hypothesis(
    campaign: Campaign, tmp_path: Path
) -> None:
    store, hypothesis, attempt_id = _attempt(campaign)
    other = "H0002"
    with sqlite3.connect(store.root / "state.sqlite3") as conn:
        conn.execute(
            "INSERT INTO hypotheses SELECT ?,state,title,spec_json,spec_sha256,panel_path,"
            "receipt_path,evaluation_spec_path,evaluation_spec_sha256,panel_sha256,receipt_sha256,"
            "dividends_path,dividends_sha256,max_attempts,base_commit,created_at,payload_json,"
            "payload_sha256 FROM hypotheses WHERE hypothesis_id=?",
            (other, hypothesis.hypothesis_id),
        )
    before = len(store.events())

    _note(store.root, other, "--attempt", attempt_id, expect=1)

    assert len(store.events()) == before


def test_status_counts_only_operator_actor_events_for_the_current_hypothesis(
    tmp_path: Path,
) -> None:
    from tests.gateway.research.test_status_control import _db

    conn = _db(tmp_path)
    conn.execute("INSERT INTO hypotheses VALUES ('H0001','DECIDED','2026-09-06T00:00:00Z')")
    conn.execute("INSERT INTO hypotheses VALUES ('H0002','FROZEN','2026-09-07T00:00:00Z')")
    for attempt_id, hypothesis_id in (("H0001-A001", "H0001"), ("H0002-A001", "H0002")):
        conn.execute(
            "INSERT INTO attempts VALUES (?,?, 'OPENED','2026-09-07T00:01:00Z')",
            (attempt_id, hypothesis_id),
        )
    conn.execute(
        "INSERT INTO attempts VALUES ('H0002-A002','H0002','OPENED','2026-09-07T00:02:00Z')"
    )
    rows = [
        ("H0001", None, "operator_intervention", "operator"),  # earlier hypothesis: excluded
        ("H0001", "H0001-A001", "run_reverified", "operator"),  # earlier hypothesis: excluded
        ("H0002", None, "operator_intervention", "operator"),
        ("H0002", "H0002-A001", "operator_intervention", "operator"),
        ("H0002", "H0002-A002", "operator_intervention", "operator"),
        ("H0002", "H0002-A002", "run_reverified", "operator"),
        ("H0002", "H0002-A002", "attempt_closed", "astra"),
        ("H0002", "H0002-A002", "run_finished", "driver"),
        ("H0002", None, "owner_turn_failed", "driver"),
    ]
    for seq, (hypothesis_id, event_attempt, kind, actor) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,'{}',?)",
            (seq, f"2026-09-07T00:03:{seq:02d}Z", hypothesis_id, event_attempt, kind, actor),
        )
    conn.commit()
    conn.close()

    status = read_status(tmp_path, unit_state=lambda _: "inactive")

    assert status.attempt_id == "H0002-A002"
    assert status.operator_interventions == {"hypothesis": 4, "attempt": 2}
    assert build_status_frame(status)["operatorInterventions"] == {"hypothesis": 4, "attempt": 2}


def test_status_without_a_hypothesis_reports_zero_counts(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path)

    status = read_status(store.root, unit_state=lambda _: "inactive")

    assert status.operator_interventions == {"hypothesis": 0, "attempt": 0}
    assert status.power is None


def test_operator_intervention_never_changes_stage_boundary_failure_or_decision(
    tmp_path: Path,
) -> None:
    from tests.gateway.research.test_status_control import _db

    conn = _db(tmp_path)
    conn.execute("INSERT INTO hypotheses VALUES ('H0001','FROZEN','2026-09-06T00:00:00Z')")
    conn.execute(
        "INSERT INTO attempts VALUES ('H0001-A001','H0001','RUN_FAILED','2026-09-06T00:01:00Z')"
    )
    conn.execute(
        "INSERT INTO events VALUES (1,'2026-09-06T00:02:00Z','H0001','H0001-A001',"
        '\'run_finished\',\'{"status":"failed","state":"RUN_FAILED"}\',\'driver\')'
    )
    conn.execute(
        "INSERT INTO events VALUES (2,'2026-09-06T00:03:00Z','H0001','H0001-A001',"
        "'attempt_closed','{\"decision\":\"RETRY\"}','astra')"
    )
    conn.commit()
    before = read_status(tmp_path, unit_state=lambda _: "inactive")
    conn.execute(
        "INSERT INTO events VALUES (3,'2026-09-06T00:04:00Z','H0001','H0001-A001',"
        '\'operator_intervention\',\'{"kind":"hold","reason":"r","decision":"FINISH",'
        '"status":"owner_unavailable"}\',\'operator\')'
    )
    conn.commit()
    conn.close()

    after = read_status(tmp_path, unit_state=lambda _: "inactive")

    assert (
        (after.stage, after.boundary_failure, after.last_astra_decision)
        == (
            before.stage,
            before.boundary_failure,
            before.last_astra_decision,
        )
        == ("awaiting_decision", "failed", "RETRY")
    )
    assert after.operator_interventions == {"hypothesis": 1, "attempt": 1}


def test_operator_intervention_does_not_wake_a_paused_or_running_campaign(
    campaign: Campaign,
) -> None:
    store, hypothesis, attempt_id = _attempt(campaign)
    store.pause("operator hold")
    assert compose_wake(store) is None

    _note(store.root, hypothesis.hypothesis_id, "--attempt", attempt_id, kind="hold")

    assert store.campaign()[0] == "PAUSED"
    assert compose_wake(store) is None
    assert store.get_attempt(attempt_id).state == AttemptState.OPENED


def test_cli_status_reports_operator_interventions(campaign: Campaign) -> None:
    store, hypothesis, attempt_id = _attempt(campaign)
    _note(store.root, hypothesis.hypothesis_id, "--attempt", attempt_id)

    data = json.loads(_invoke("status", "--root", str(store.root), "--json"))

    assert data["operator_interventions"] == {"hypothesis": 1, "attempt": 1}


# --- status power ----------------------------------------------------------------------------


def test_status_reports_power_for_the_current_v2_hypothesis(campaign: Campaign) -> None:
    store, _source, _hypothesis = campaign

    status = read_status(store.root, unit_state=lambda _: "inactive")

    assert status.power == {"minimumDetectableEffectBps": 24.86, "plausibleEffectBps": 30.0}
    assert build_status_frame(status)["power"] == status.power


def test_cli_status_reports_power_for_a_v2_hypothesis(campaign: Campaign) -> None:
    store, _source, _hypothesis = campaign

    data = json.loads(_invoke("status", "--root", str(store.root), "--json"))

    assert data["hypotheses"][0]["power"] == {
        "minimumDetectableEffectBps": 24.86,
        "plausibleEffectBps": 30.0,
    }


def test_status_omits_power_for_a_v1_hypothesis(campaign: Campaign) -> None:
    store, _source, hypothesis = campaign
    v1 = json.dumps(_payload_v1(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with sqlite3.connect(store.root / "state.sqlite3") as conn:
        conn.execute(
            "UPDATE hypotheses SET spec_json=? WHERE hypothesis_id=?",
            (v1, hypothesis.hypothesis_id),
        )

    status = read_status(store.root, unit_state=lambda _: "inactive")

    assert status.power is None
    assert "power" not in build_status_frame(status)


def test_store_refuses_an_unknown_operator_note_kind(campaign: Campaign) -> None:
    store, hypothesis, attempt_id = _attempt(campaign)
    before = len(store.events())

    with pytest.raises(ValueError, match="kind must be one of"):
        store.record_operator_intervention(
            hypothesis.hypothesis_id, "Audit", "reason", "ref", attempt_id
        )

    assert len(store.events()) == before
