"""Refused hypothesis creates are recorded and re-wake the owner exactly once each."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research.codec import to_json
from gateway.research.contracts import (
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisDecision,
    HypothesisSpec,
)
from gateway.research.refusals import create_refusal_summary
from gateway.research.status import build_status_frame, read_status
from gateway.research.store import ResearchStore
from gateway.research.wake import _FREEZE_SEQUENCE, compose_wake
from typer.testing import CliRunner

from tests.gateway.research.conftest import freeze_with_probe
from tests.gateway.research.test_admission import (
    _payload,
    _payload_v1,
    _power_block,
    _set_events,
)
from tests.gateway.research.test_status_control import _configured_store

runner = CliRunner()
Campaign = tuple[ResearchStore, Path, HypothesisSpec]
UNDERPOWERED = "underpowered design: MDE 24.9 bps exceeds plausible effect 24 bps"


def _underpowered_payload() -> dict[str, object]:
    payload = _payload()
    power = _power_block()
    power["plausible_effect_bps"] = 24.0  # MDE 24.86 exceeds a 24 bp plausible effect
    payload["power"] = power
    return payload


def _decided(campaign: Campaign) -> ResearchStore:
    store, _source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.FINISHED, "done")
    return store


def _args(campaign: Campaign, spec_file: Path) -> tuple[str, Path, Path, Path, Path, str]:
    hypothesis = campaign[2]
    return (
        "next",
        spec_file,
        Path(hypothesis.panel_path),
        Path(hypothesis.receipt_path),
        Path(hypothesis.evaluation_spec_path),
        "b" * 40,
    )


def _spec_file(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "next-spec.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _refuse(campaign: Campaign, spec_file: Path) -> str:
    store = campaign[0]
    hypothesis = campaign[2]
    with pytest.raises(ValueError) as error:
        store.create_hypothesis(
            *_args(campaign, spec_file),
            dividends=Path(hypothesis.dividends_path),
            evaluation_spec_set=spec_file.parent / "never-read.json",
        )
    return str(error.value)


def _refusal_events(store: ResearchStore) -> list[dict[str, object]]:
    return [
        json.loads(event.detail)
        for event in store.events()
        if event.kind == "hypothesis_create_refused"
    ]


def test_underpowered_create_records_one_event_and_nothing_else(
    campaign: Campaign, tmp_path: Path
) -> None:
    store = _decided(campaign)
    spec_file = _spec_file(tmp_path, _underpowered_payload())
    before_events = store.events()
    before_campaign = store.campaign()

    message = _refuse(campaign, spec_file)

    assert message == UNDERPOWERED
    assert [h.hypothesis_id for h in store.hypotheses()] == ["H0001"]
    assert store.campaign() == before_campaign
    new = store.events()[len(before_events) :]
    assert store.events()[: len(before_events)] == before_events
    assert [(e.kind, e.actor, e.hypothesis_id, e.attempt_id) for e in new] == [
        ("hypothesis_create_refused", "astra", "H0002", None)
    ]
    assert json.loads(new[0].detail) == {
        "reason": UNDERPOWERED,
        "spec_sha256": hashlib.sha256(
            to_json(json.loads(spec_file.read_text(encoding="utf-8"))).encode()
        ).hexdigest(),
        "next_hypothesis_id": "H0002",
    }
    assert not (store.root / "hypotheses" / "H0002").exists()


def test_v1_and_non_canonical_refusals_are_recorded(campaign: Campaign, tmp_path: Path) -> None:
    store = _decided(campaign)

    v1 = _refuse(campaign, _spec_file(tmp_path, _payload_v1()))
    payload = _payload()
    payload["compute"] = {"max_wall_seconds": 120, "max_rss_mb": 1024}
    canonical = _refuse(campaign, _spec_file(tmp_path, payload))

    assert [item["reason"] for item in _refusal_events(store)] == [v1, canonical]


def test_existing_draft_precondition_records_no_event(campaign: Campaign, tmp_path: Path) -> None:
    store, _source, _hypothesis = campaign
    spec_file = _spec_file(tmp_path, _underpowered_payload())
    before = store.events()

    message = _refuse(campaign, spec_file)

    assert message == "hypothesis-create requires every existing hypothesis to be DECIDED"
    assert store.events() == before
    assert create_refusal_summary([]) == (0, None)
    assert store.create_refusals() == (0, None)


def test_unreadable_spec_file_is_not_a_deterministic_refusal(
    campaign: Campaign, tmp_path: Path
) -> None:
    store = _decided(campaign)
    hypothesis = campaign[2]

    with pytest.raises(FileNotFoundError):
        store.create_hypothesis(
            *_args(campaign, tmp_path / "missing-spec.json"),
            dividends=Path(hypothesis.dividends_path),
            evaluation_spec_set=tmp_path / "never-read.json",
        )

    assert _refusal_events(store) == []


def test_unrecordable_refusal_keeps_the_original_error(
    campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _decided(campaign)
    spec_file = _spec_file(tmp_path, _underpowered_payload())

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ResearchStore, "_event", locked)

    assert _refuse(campaign, spec_file) == UNDERPOWERED
    monkeypatch.undo()
    assert _refusal_events(store) == []


def test_cli_create_refusal_exits_one_with_the_original_text_and_records_one_event(
    campaign: Campaign, tmp_path: Path
) -> None:
    store = _decided(campaign)
    hypothesis = campaign[2]
    spec_file = _spec_file(tmp_path, _underpowered_payload())

    result = runner.invoke(
        app,
        [
            "research",
            "hypothesis-create",
            "--root",
            str(store.root),
            "--title",
            "next",
            "--spec-file",
            str(spec_file),
            "--panel",
            hypothesis.panel_path,
            "--receipt",
            hypothesis.receipt_path,
            "--eval-spec",
            hypothesis.evaluation_spec_path,
            "--dividends",
            hypothesis.dividends_path,
            "--base-commit",
            "b" * 40,
            "--evaluation-spec-set",
            str(tmp_path / "never-read.json"),
        ],
    )

    assert result.exit_code == 1
    assert result.output.strip() == UNDERPOWERED
    assert len(_refusal_events(store)) == 1
    assert [h.hypothesis_id for h in store.hypotheses()] == ["H0001"]


def test_refusal_changes_the_wake_key_and_text_carries_reason_count_and_stop_condition(
    campaign: Campaign, tmp_path: Path
) -> None:
    store = _decided(campaign)
    spec_file = _spec_file(tmp_path, _underpowered_payload())
    before = compose_wake(store)
    assert before is not None and before.state == "ALL_DECIDED"
    assert "refused" not in before.message

    _refuse(campaign, spec_file)
    first = compose_wake(store)
    _refuse(campaign, spec_file)
    second = compose_wake(store)

    assert first is not None and second is not None
    assert len({before.pending_key, first.pending_key, second.pending_key}) == 3
    assert first.state == second.state == "ALL_DECIDED"
    assert first.hypothesis_id == "H0002"
    assert first.message.startswith(before.message)
    assert UNDERPOWERED in first.message
    assert (
        "1 hypothesis-create refusal(s) of any reason since the last create, decide or resume"
        in first.message
    )
    assert "2 hypothesis-create refusal(s) of any reason" in second.message
    for plan in (first, second):
        assert "campaign charter's stop condition is met" in plan.message
        assert "consecutive underpowered refusals" in plan.message
        assert "pause --root ROOT --owner --reason TEXT" in plan.message
        assert "do not resubmit the same design unchanged" in plan.message
    assert compose_wake(store) == second  # recomputation is stable without a new refusal


def test_no_hypothesis_wake_also_rewakes_after_a_refusal(tmp_path: Path) -> None:
    store = _configured_store(tmp_path / "empty-driver")
    first = compose_wake(store)
    assert first is not None and first.state == "NO_HYPOTHESIS"
    spec_file = _spec_file(tmp_path, _payload_v1())
    files = {name: tmp_path / name for name in ("panel", "receipt", "eval", "div", "set")}
    for path in files.values():
        path.write_text(path.name, encoding="utf-8")

    with pytest.raises(ValueError, match="requires contract research-hypothesis-v2") as error:
        store.create_hypothesis(
            "first",
            spec_file,
            files["panel"],
            files["receipt"],
            files["eval"],
            "b" * 40,
            dividends=files["div"],
            evaluation_spec_set=files["set"],
        )
    after = compose_wake(store)

    assert after is not None and after.pending_key != first.pending_key
    assert str(error.value) in after.message
    assert "1 hypothesis-create refusal(s)" in after.message
    assert store.create_refusals() == (1, str(error.value))


def test_unconfigured_runtime_is_not_recorded_as_an_owner_refusal(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "unconfigured")
    spec_file = _spec_file(tmp_path, _payload())
    placeholder = tmp_path / "placeholder"
    placeholder.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="contained runtime must be configured"):
        store.create_hypothesis(
            "first",
            spec_file,
            placeholder,
            placeholder,
            placeholder,
            "b" * 40,
            dividends=placeholder,
            evaluation_spec_set=placeholder,
        )

    assert store.events() == []


def test_old_roots_without_refusals_keep_identical_keys_and_texts(
    campaign: Campaign, tmp_path: Path
) -> None:
    def old_key(hid: str, state: str, resume_seq: int) -> str:
        return hashlib.sha256(f"{hid}||{state}|{resume_seq}".encode()).hexdigest()

    empty = compose_wake(ResearchStore(tmp_path / "empty-driver"))
    assert empty is not None
    assert empty.pending_key == old_key("H0001", "NO_HYPOTHESIS", 0)
    assert empty.message == (
        "Astra: create one hypothesis with `gateway-cli research hypothesis-create --root ROOT "
        f"...`, then {_FREEZE_SEQUENCE.format(hid='H0001')}"
    )

    store = _decided(campaign)
    decided = compose_wake(store)
    assert decided is not None
    assert decided.pending_key == old_key("H0002", "ALL_DECIDED", store.campaign()[1])
    assert decided.message == (
        "Astra: author the next hypothesis H0002 (all existing hypotheses are DECIDED), "
        f"then {_FREEZE_SEQUENCE.format(hid='H0002')}"
    )


def test_summary_resets_on_create_or_decision() -> None:
    rows = [
        ("hypothesis_create_refused", json.dumps({"reason": "a"})),
        ("hypothesis_create_refused", json.dumps({"reason": "b"})),
    ]

    assert create_refusal_summary(rows) == (2, "b")
    assert create_refusal_summary([*rows, ("hypothesis_created", "{}")]) == (0, None)
    assert create_refusal_summary([*rows, ("hypothesis_decided", "{}")]) == (0, None)
    assert create_refusal_summary([*rows, ("campaign_resumed", "{}")]) == (0, None)
    assert create_refusal_summary([("hypothesis_create_refused", "not json")]) == (1, None)


class _FakeSender:
    sent: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, *_args: object) -> None:
        pass

    def send(self, message: str, _session_key: str, idempotency_key: str) -> str:
        self.sent.append((message, idempotency_key))
        return f"run-{len(self.sent)}"

    def owner_task_status(self, run_id: str) -> dict[str, object]:
        return {"status": "ok", "runId": run_id}


class _Stop(BaseException):
    pass


def _serve_iterations(root: Path, iterations: int, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = 0

    def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= iterations:
            raise _Stop

    monkeypatch.setattr(research_cli, "OpenClawWakeSender", _FakeSender)
    monkeypatch.setattr("time.sleep", sleep)
    with pytest.raises(_Stop):
        research_cli.serve(session_key="agent:owner", root=root, poll_seconds=1, once=False)


def test_no_wake_loop_without_a_new_refusal(
    campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _decided(campaign)
    spec_file = _spec_file(tmp_path, _underpowered_payload())
    _FakeSender.sent = []
    _refuse(campaign, spec_file)

    _serve_iterations(store.root, 2, monkeypatch)
    assert len(_FakeSender.sent) == 1  # two iterations, no new refusal: one wake

    _serve_iterations(store.root, 2, monkeypatch)
    assert len(_FakeSender.sent) == 1

    _refuse(campaign, spec_file)
    _serve_iterations(store.root, 2, monkeypatch)
    assert len(_FakeSender.sent) == 2
    assert _FakeSender.sent[0][1] != _FakeSender.sent[1][1]
    assert "2 hypothesis-create refusal(s)" in _FakeSender.sent[1][0]


def test_status_reports_create_refusals_for_the_idle_stage(
    campaign: Campaign, tmp_path: Path
) -> None:
    store, _source, _hypothesis = campaign
    draft_frame = build_status_frame(read_status(store.root, unit_state=None))
    assert "createRefusals" not in draft_frame  # a DRAFT is not the NO_HYPOTHESIS/ALL_DECIDED stage

    _decided(campaign)
    quiet = build_status_frame(read_status(store.root, unit_state=None))
    assert quiet["createRefusals"] == {"count": 0, "latestReason": None}

    _refuse(campaign, _spec_file(tmp_path, _underpowered_payload()))
    frame = build_status_frame(read_status(store.root, unit_state=None))
    assert frame["createRefusals"] == {"count": 1, "latestReason": UNDERPOWERED}
    assert read_status(store.root, unit_state=None).create_refusals == frame["createRefusals"]
    assert frame["stage"] == "idle"

    cli_status = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])
    assert cli_status.exit_code == 0, cli_status.output
    assert json.loads(cli_status.output)["create_refusals"] == {
        "count": 1,
        "latestReason": UNDERPOWERED,
    }


def _spec_set_file(tmp_path: Path, hypothesis: HypothesisSpec, hypothesis_id: str) -> Path:
    evaluation = Path(hypothesis.evaluation_spec_path)
    path = tmp_path / f"set-{hypothesis_id}.json"
    path.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            hypothesis_id,
            "c000",
            (
                EvaluationSpecEntry(
                    "c000", str(evaluation), hashlib.sha256(evaluation.read_bytes()).hexdigest()
                ),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    return path


def _create_with_set(campaign: Campaign, spec_file: Path, spec_set: Path) -> HypothesisSpec:
    store = campaign[0]
    hypothesis = campaign[2]
    return store.create_hypothesis(
        *_args(campaign, spec_file),
        dividends=Path(hypothesis.dividends_path),
        evaluation_spec_set=spec_set,
    )


def test_refusal_inside_the_write_transaction_is_recorded_and_releases_the_lock(
    campaign: Campaign, tmp_path: Path
) -> None:
    store = _decided(campaign)
    spec_file = _spec_file(tmp_path, _payload())
    mismatched = _spec_set_file(tmp_path, campaign[2], "H0009")

    with pytest.raises(ValueError, match="hypothesis_id does not match new hypothesis"):
        _create_with_set(campaign, spec_file, mismatched)

    assert [h.hypothesis_id for h in store.hypotheses()] == ["H0001"]
    assert store.create_refusals()[0] == 1
    assert [item["next_hypothesis_id"] for item in _refusal_events(store)] == ["H0002"]
    # The write lock is free: a second writer proceeds immediately.
    store.pause("check", actor="astra")


def test_count_resets_after_a_real_create_and_after_a_real_decide(
    campaign: Campaign, tmp_path: Path
) -> None:
    store = _decided(campaign)
    bad = _spec_file(tmp_path, _underpowered_payload())
    _refuse(campaign, bad)
    _refuse(campaign, bad)
    assert store.create_refusals()[0] == 2

    good = tmp_path / "good-spec.json"
    good.write_text(json.dumps(_payload()), encoding="utf-8")
    created = _create_with_set(campaign, good, _spec_set_file(tmp_path, campaign[2], "H0002"))
    assert created.hypothesis_id == "H0002"
    assert store.create_refusals() == (0, None)

    # A refusal event that predates a decision (not reachable via create, which requires every
    # hypothesis DECIDED) must still be cleared by the decision itself.
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        store._event(conn, "H0003", None, "hypothesis_create_refused", {"reason": "stale"}, "astra")
        conn.commit()
    assert store.create_refusals() == (1, "stale")
    freeze_with_probe(store, "H0002")
    store.decide_hypothesis("H0002", HypothesisDecision.ABANDONED, "done")
    assert store.create_refusals() == (0, None)
    _refuse(campaign, bad)
    assert store.create_refusals() == (1, UNDERPOWERED)
    wake = compose_wake(store)
    assert wake is not None and wake.hypothesis_id == "H0003"
    assert "1 hypothesis-create refusal(s)" in wake.message


def test_resume_resets_the_refusal_count_and_wake_text(campaign: Campaign, tmp_path: Path) -> None:
    store = _decided(campaign)
    spec_file = _spec_file(tmp_path, _underpowered_payload())
    _refuse(campaign, spec_file)
    _refuse(campaign, spec_file)
    stalled = compose_wake(store)
    assert stalled is not None and "2 hypothesis-create refusal(s)" in stalled.message

    store.pause("charter stop", actor="astra")
    assert compose_wake(store) is None
    store.resume("operator reviewed")
    resumed = compose_wake(store)

    assert resumed is not None and resumed.state == "ALL_DECIDED"
    assert "refusal" not in resumed.message
    assert "charter" not in resumed.message
    assert store.create_refusals() == (0, None)
    seq = store.campaign()[1]
    assert resumed.pending_key == hashlib.sha256(f"H0002||ALL_DECIDED|{seq}".encode()).hexdigest()
    assert resumed.pending_key != stalled.pending_key
    frame = build_status_frame(read_status(store.root, unit_state=None))
    assert frame["createRefusals"] == {"count": 0, "latestReason": None}
    _refuse(campaign, spec_file)
    again = compose_wake(store)
    assert again is not None and "1 hypothesis-create refusal(s)" in again.message


def test_owner_pause_is_not_an_operator_intervention_but_operator_pause_is(
    campaign: Campaign,
) -> None:
    store = _decided(campaign)
    frame = build_status_frame(read_status(store.root, unit_state=None))
    assert frame["operatorInterventions"] == {"hypothesis": 0, "attempt": 0}

    store.pause("owner stop", actor="astra")
    owner_event = store.events()[-1]
    assert (owner_event.kind, owner_event.actor) == ("campaign_paused", "astra")
    after_owner = build_status_frame(read_status(store.root, unit_state=None))
    assert after_owner["operatorInterventions"] == {"hypothesis": 0, "attempt": 0}

    store.pause("operator hold")
    operator_event = store.events()[-1]
    assert (operator_event.kind, operator_event.actor) == ("campaign_paused", "operator")
    after_operator = build_status_frame(read_status(store.root, unit_state=None))
    assert after_operator["operatorInterventions"] == {"hypothesis": 1, "attempt": 0}


def test_pause_event_attaches_to_the_newest_hypothesis(campaign: Campaign, tmp_path: Path) -> None:
    store = _decided(campaign)
    good = tmp_path / "good-spec.json"
    good.write_text(json.dumps(_payload()), encoding="utf-8")
    _create_with_set(campaign, good, _spec_set_file(tmp_path, campaign[2], "H0002"))

    store.pause("hold")

    assert store.events()[-1].hypothesis_id == "H0002"


def test_cli_pause_owner_flag_records_astra_and_default_records_operator(
    campaign: Campaign,
) -> None:
    store = campaign[0]

    owner = runner.invoke(
        app, ["research", "pause", "--root", str(store.root), "--owner", "--reason", "stop"]
    )
    assert owner.exit_code == 0, owner.output
    assert store.events()[-1].actor == "astra"
    store.resume("again")

    operator = runner.invoke(
        app, ["research", "pause", "--root", str(store.root), "--reason", "hold"]
    )
    assert operator.exit_code == 0, operator.output
    assert store.events()[-1].actor == "operator"


def _bounded_create(
    campaign: Campaign,
    tmp_path: Path,
    evaluator_specs: list[dict[str, object]],
    forward_label_sessions: int,
    *,
    payload_changes: dict[str, object] | None = None,
    receipt: Path | None = None,
) -> str | None:
    """Create H0002 against a spec set; return the refusal text, or None on success."""
    store = _decided(campaign)
    hypothesis = campaign[2]
    payload = _payload()
    payload["forward_label_sessions"] = forward_label_sessions
    payload.update(payload_changes or {})
    spec_file = _spec_file(tmp_path, payload)
    paths = []
    for index, document in enumerate(evaluator_specs):
        path = tmp_path / f"bounded-eval-{index}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        paths.append(path)
    spec_set = tmp_path / "bounded-set.json"
    spec_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0002",
            "c000",
            tuple(
                EvaluationSpecEntry(
                    f"c{index:03d}", str(path), hashlib.sha256(path.read_bytes()).hexdigest()
                )
                for index, path in enumerate(paths)
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    try:
        store.create_hypothesis(
            "bounded",
            spec_file,
            Path(hypothesis.panel_path),
            receipt or Path(hypothesis.receipt_path),
            paths[0],
            "b" * 40,
            dividends=Path(hypothesis.dividends_path),
            evaluation_spec_set=spec_set,
        )
    except ValueError as exc:
        return str(exc)
    return None


def _evaluator_spec(
    max_sessions: int,
    *,
    long_only: bool = True,
    shortable: bool = False,
    borrow_bps_annual: float | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "instruments": [{"ticker": "SPY", "instrument_class": "etf", "shortable": shortable}],
        "start_session": "2025-09-15",
        "end_session": "2025-09-26",
        "holding": {"max_sessions": max_sessions},
        "long_only": long_only,
    }
    if borrow_bps_annual is not None:
        document["costs"] = {"borrow_bps_annual": borrow_bps_annual}
    return document


def test_create_refuses_forward_label_beyond_the_spec_max_sessions(
    campaign: Campaign, tmp_path: Path
) -> None:
    message = _bounded_create(campaign, tmp_path, [_evaluator_spec(5)], 6)

    assert message == (
        "evaluation spec c000: forward_label_sessions 6 exceeds the evaluator spec "
        "holding.max_sessions 5"
    )
    assert [item["reason"] for item in _refusal_events(campaign[0])] == [message]


def test_create_refuses_hedged_spec_without_a_shortable_instrument(
    campaign: Campaign, tmp_path: Path
) -> None:
    spec = _evaluator_spec(20, long_only=False, borrow_bps_annual=50.0)

    message = _bounded_create(campaign, tmp_path, [spec], 20)

    assert message == "evaluation spec c000: long_only is false but no instrument is shortable"


@pytest.mark.parametrize("borrow", [None, 0.0, -1.0])
def test_create_refuses_hedged_spec_without_a_borrow_cost(
    campaign: Campaign, tmp_path: Path, borrow: float | None
) -> None:
    spec = _evaluator_spec(20, long_only=False, shortable=True, borrow_bps_annual=borrow)

    message = _bounded_create(campaign, tmp_path, [spec], 20)

    assert message == (
        "evaluation spec c000: long_only is false but costs.borrow_bps_annual is not set above zero"
    )


def test_create_checks_every_spec_before_copying_any(campaign: Campaign, tmp_path: Path) -> None:
    good = _evaluator_spec(20, long_only=False, shortable=True, borrow_bps_annual=50.0)
    no_borrow = _evaluator_spec(20, long_only=False, shortable=True)

    message = _bounded_create(campaign, tmp_path, [good, no_borrow], 20)

    assert message is not None and message.startswith("evaluation spec c001: ")
    assert not (campaign[0].root / "hypotheses" / "H0002").exists()


@pytest.mark.parametrize("sessions", [5, 20, 60])
def test_create_accepts_matching_horizon_and_shortable_hedged_spec(
    campaign: Campaign, tmp_path: Path, sessions: int
) -> None:
    spec = _evaluator_spec(sessions, long_only=False, shortable=True, borrow_bps_annual=50.0)

    message = _bounded_create(campaign, tmp_path, [spec], sessions)

    assert message is None
    assert [h.hypothesis_id for h in campaign[0].hypotheses()] == ["H0001", "H0002"]


RECEIPT_FIXTURE = Path(__file__).parent / "fixtures" / "receipt.json"


def _panel_design(
    forward: int, events: int, *, training: bool = True, sd_bps: float = 10.0
) -> dict[str, object]:
    """A design inside the 10-session receipt panel (2025-09-15..26) with a chosen power block.

    Training ends 2025-09-16 and evaluation covers 2025-09-22..26 (5 sessions), leaving the
    3 sessions 09-17, 09-18 and 09-19 between them.
    """
    payload = _payload()
    _set_events(payload, events, sd_bps)
    return {
        "analysis": {"start": "2025-09-15", "end": "2025-09-26"},
        "evaluation": {"start": "2025-09-22", "end": "2025-09-26"},
        "training": {"start": "2025-09-15", "end": "2025-09-16"} if training else None,
        "power": payload["power"],
        "forward_label_sessions": forward,
    }


def _create_on_panel(campaign: Campaign, tmp_path: Path, changes: dict[str, object]) -> str | None:
    forward = changes["forward_label_sessions"]
    assert isinstance(forward, int)
    return _bounded_create(
        campaign,
        tmp_path,
        [_evaluator_spec(60)],
        forward,
        payload_changes=changes,
        receipt=RECEIPT_FIXTURE,
    )


@pytest.mark.parametrize(("forward", "refused"), [(3, False), (4, True)])
def test_create_refuses_a_purge_gap_shorter_than_the_forward_label(
    campaign: Campaign, tmp_path: Path, forward: int, refused: bool
) -> None:
    message = _create_on_panel(campaign, tmp_path, _panel_design(forward, 1))

    if refused:
        assert message == (
            "PURGE_GAP_INSUFFICIENT: only 3 panel sessions separate training from evaluation; "
            "the 4-session forward label needs at least that many"
        )
        assert [item["reason"] for item in _refusal_events(campaign[0])] == [message]
        assert not (campaign[0].root / "hypotheses" / "H0002").exists()
    else:
        assert message is None


@pytest.mark.parametrize(("events", "refused"), [(5, False), (6, True)])
def test_create_refuses_expected_events_above_the_panel_capacity(
    campaign: Campaign, tmp_path: Path, events: int, refused: bool
) -> None:
    changes = _panel_design(1, events, training=False)

    message = _create_on_panel(campaign, tmp_path, changes)

    if refused:
        assert message == (
            "POWER_EVENTS_EXCEED_CAPACITY: power.expected_events 6 exceeds the non-overlapping "
            "capacity of 5 (instruments x evaluation sessions / forward_label_sessions)"
        )
        assert [item["reason"] for item in _refusal_events(campaign[0])] == [message]
    else:
        assert message is None


def test_create_capacity_divides_by_the_forward_label(campaign: Campaign, tmp_path: Path) -> None:
    changes = _panel_design(2, 3, training=False)  # capacity 5 / 2 = 2.5 < 3

    message = _create_on_panel(campaign, tmp_path, changes)

    assert message is not None and message.startswith("POWER_EVENTS_EXCEED_CAPACITY: ")
