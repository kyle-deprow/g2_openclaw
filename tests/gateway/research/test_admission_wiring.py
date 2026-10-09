from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research.admission import (
    AdmissionReason,
    CampaignPolicy,
    ExecutionCapability,
)
from gateway.research.cli import (
    _admission_for_hypothesis,
    _AdmissionInputError,
    _parse_evaluator_bounds,
    _parse_exposure_ledger,
    _record_admission_refusal,
    _same_evaluator_bounds,
)
from gateway.research.contracts import (
    AttemptDecision,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisDecision,
    HypothesisSpec,
    HypothesisState,
    RunOutcome,
    RunPlan,
)
from gateway.research.receipt_sessions import decode_panel_sessions
from gateway.research.store import MAX_EXPOSURE_LEDGER_BYTES, ResearchStore, StoreConflict
from gateway.research.wake import compose_wake
from typer.testing import CliRunner

from tests.gateway.research.conftest import (
    freeze_with_probe,
    implementation,
    provenance_evidence,
    review,
    run_plan,
    verified_review,
)
from tests.gateway.research.native_review_fixtures import prepare_review
from tests.gateway.research.test_admission import _admit, _document, _payload, _set_events

LEDGER_FIXTURE = Path(__file__).parent / "fixtures" / "exposure-ledger.json"
RECEIPT_FIXTURE = Path(__file__).parent / "fixtures" / "receipt.json"
runner = CliRunner()


def _wired_spec(
    stored_hypothesis: HypothesisSpec,
    tmp_path: Path,
    *,
    instrument_class: str = "etf",
    feature_source: str = "panel",
    evaluator_document: dict[str, object] | None = None,
) -> HypothesisSpec:
    eval_spec = tmp_path / f"evaluator-{instrument_class}.json"
    eval_spec.write_text(
        json.dumps(
            evaluator_document
            or {
                "instruments": [{"ticker": "SPY", "instrument_class": instrument_class}],
                "start_session": "2025-09-15",
                "end_session": "2025-09-26",
                "holding": {"max_sessions": 5},
            }
        ),
        encoding="utf-8",
    )
    receipt_wire = json.loads(RECEIPT_FIXTURE.read_text(encoding="utf-8"))
    document_payload = copy.deepcopy(_payload())
    document_payload["analysis"] = {"start": "2025-09-15", "end": "2025-09-26"}
    document_payload["evaluation"] = {"start": "2025-09-15", "end": "2025-09-26"}
    document_payload["training"] = None
    document_payload["features"][0]["lookback_sessions"] = 0  # type: ignore[index]
    _set_events(document_payload, 5)  # a 10-session window cannot hold the default 100 events
    document_payload["features"][0]["source"] = feature_source  # type: ignore[index]
    document = _document(document_payload)
    return replace(
        stored_hypothesis,
        spec_json=document.to_json(),
        spec_sha256=document.sha256,
        state=stored_hypothesis.state,
        receipt_path=str(RECEIPT_FIXTURE.absolute()),
        receipt_sha256=hashlib.sha256(RECEIPT_FIXTURE.read_bytes()).hexdigest(),
        evaluation_spec_path=str(eval_spec),
        evaluation_spec_sha256=hashlib.sha256(eval_spec.read_bytes()).hexdigest(),
        panel_sha256=str(receipt_wire["panel_sha256"]),
        dividends_path=str(RECEIPT_FIXTURE.absolute()),
        dividends_sha256=hashlib.sha256(RECEIPT_FIXTURE.read_bytes()).hexdigest(),
    )


def _wire_evaluation_spec_set(store: ResearchStore, spec: HypothesisSpec) -> None:
    entry = EvaluationSpecEntry("c000", spec.evaluation_spec_path, spec.evaluation_spec_sha256)
    manifest = EvaluationSpecSet(
        "research-evaluation-spec-set-v1",
        spec.hypothesis_id,
        "c000",
        (entry,),
        "2026-01-01T00:00:00Z",
    )

    def fake_spec_set(hypothesis_id: str) -> EvaluationSpecSet:
        return manifest

    store.evaluation_spec_set = fake_spec_set  # type: ignore[method-assign]


def _stored_spec_set_digest(store: ResearchStore, hypothesis_id: str = "H0001") -> str:
    return hashlib.sha256(store.evaluation_spec_set(hypothesis_id).to_json().encode()).hexdigest()


def _replace_spec_set_evidence(
    store: ResearchStore, hypothesis_id: str, payload: str, payload_sha256: str
) -> None:
    """Install controlled corrupt evidence in this disposable SQLite fixture."""
    with store._connect() as conn:
        conn.execute("DROP TRIGGER immutable_hypothesis_evidence")
        conn.execute("DROP TRIGGER immutable_hypothesis_evidence_delete")
        conn.execute(
            "UPDATE hypothesis_evidence SET payload_json=?,payload_sha256=? "
            "WHERE hypothesis_id=? AND kind='evaluation_spec_set'",
            (payload, payload_sha256, hypothesis_id),
        )
        conn.executescript(
            """
            CREATE TRIGGER immutable_hypothesis_evidence
            BEFORE UPDATE ON hypothesis_evidence
            BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END;
            CREATE TRIGGER immutable_hypothesis_evidence_delete
            BEFORE DELETE ON hypothesis_evidence
            BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END;
            """
        )
        conn.commit()


def test_campaign_policy_is_unset_without_a_default(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign

    assert store.campaign_policy() == CampaignPolicy(False)
    assert store._campaign_row()["attempt_cap"] is None
    assert store._campaign_row()["policy_set_at"] is None


def test_historical_h1_without_spec_set_remains_visible_in_full_status(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    projection = store.root / "hypotheses" / hypothesis.hypothesis_id / "evaluation-spec-set.json"
    with store._connect() as conn:
        conn.execute("DROP TRIGGER immutable_hypothesis_evidence_delete")
        conn.execute(
            "DELETE FROM hypothesis_evidence WHERE hypothesis_id=? AND kind='evaluation_spec_set'",
            (hypothesis.hypothesis_id,),
        )
        conn.executescript(
            """
            CREATE TRIGGER immutable_hypothesis_evidence_delete
            BEFORE DELETE ON hypothesis_evidence
            BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END;
            """
        )
        conn.commit()
    projection.unlink()

    result = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["hypotheses"][0]["hypothesis_id"] == hypothesis.hypothesis_id
    assert payload["campaign"][0] in {"ACTIVE", "PAUSED"}


def test_policy_cli_store_round_trip_is_explicit(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign

    policy = store.set_campaign_policy(3, 120.5, "operator-test")

    assert policy == CampaignPolicy(True, 3, 120.5)
    row = store._campaign_row()
    assert row["policy_operator_reference"] == "operator-test"
    assert row["policy_set_at"]
    assert store.campaign_policy() == policy
    assert store.events()[-1].hypothesis_id == hypothesis.hypothesis_id


def test_policy_cli_status_round_trip_keeps_decision_and_latest_refusal(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    store.insert_admission_decision(
        attempt.attempt_id,
        _admit(
            _document(_payload()),
            evaluation_spec_set_sha256=_stored_spec_set_digest(store, hypothesis.hypothesis_id),
        ),
    )
    refusal = _admit(_document(_payload()), capability=ExecutionCapability(frozenset({"reddit"})))
    store.record_admission_refusal(hypothesis.hypothesis_id, refusal)

    before = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])
    policy = runner.invoke(
        app,
        [
            "research",
            "campaign-policy-set",
            "--root",
            str(store.root),
            "--attempt-cap",
            "3",
            "--operator-reference",
            "operator-test",
        ],
    )
    after = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])

    assert before.exit_code == 0, before.output
    assert json.loads(before.output)["policy_set"] is False
    assert policy.exit_code == 0, policy.output
    assert after.exit_code == 0, after.output
    after_payload = json.loads(after.output)
    assert after_payload["policy_set"] is True
    assert after_payload["hypotheses"][0]["admission_refusal"]["reason"] == (
        "INPUT_CAPABILITY_UNSUPPORTED"
    )
    assert after_payload["hypotheses"][0]["attempts"][0]["admission"]["admitted"] is True


def test_exposure_ledger_cli_registration_and_refusals(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    valid_sha = hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    valid = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(LEDGER_FIXTURE),
            "--sha256",
            valid_sha,
        ],
    )
    malformed = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(LEDGER_FIXTURE),
            "--sha256",
            "not-a-sha",
        ],
    )
    registered = store.exposure_ledger_registration()
    assert registered == (str(LEDGER_FIXTURE.absolute()), valid_sha)

    assert valid.exit_code == 0, valid.output
    assert _parse_exposure_ledger(store).valid  # type: ignore[union-attr]
    assert malformed.exit_code == 1
    assert "SHA256" in malformed.output
    assert store.exposure_ledger_registration() == registered

    mismatch = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(LEDGER_FIXTURE),
            "--sha256",
            "0" * 64,
        ],
    )
    assert mismatch.exit_code == 1, mismatch.output
    assert "does not match" in mismatch.output
    assert store.exposure_ledger_registration() == registered

    missing = store.root / "missing-ledger.json"
    missing_register = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(missing),
            "--sha256",
            "0" * 64,
        ],
    )
    assert missing_register.exit_code == 1, missing_register.output
    assert "missing or unreadable" in missing_register.output
    assert store.exposure_ledger_registration() == registered
    parsed = _parse_exposure_ledger(store)
    assert parsed is not None and parsed.valid

    symlink = store.root / "ledger-link.json"
    symlink.symlink_to(LEDGER_FIXTURE)
    symlink_register = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(symlink),
            "--sha256",
            valid_sha,
        ],
    )
    assert symlink_register.exit_code == 1, symlink_register.output
    assert "must not be a symlink" in symlink_register.output
    assert store.exposure_ledger_registration() == registered

    directory = store.root / "ledger-directory.json"
    directory.mkdir()
    directory_register = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(directory),
            "--sha256",
            valid_sha,
        ],
    )
    assert directory_register.exit_code == 1, directory_register.output
    assert "bounded regular file" in directory_register.output
    assert store.exposure_ledger_registration() == registered

    oversized = store.root / "oversized-ledger.json"
    oversized.write_bytes(b"x" * (MAX_EXPOSURE_LEDGER_BYTES + 1))
    oversized_register = runner.invoke(
        app,
        [
            "research",
            "exposure-ledger-register",
            "--root",
            str(store.root),
            "--path",
            str(oversized),
            "--sha256",
            hashlib.sha256(oversized.read_bytes()).hexdigest(),
        ],
    )
    assert oversized_register.exit_code == 1, oversized_register.output
    assert "bounded regular file" in oversized_register.output
    assert store.exposure_ledger_registration() == registered


def test_registered_synthetic_ledger_is_typed_and_digest_bound(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    registered = store.register_exposure_ledger(
        LEDGER_FIXTURE, hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    )

    assert registered[0] == str(LEDGER_FIXTURE.absolute())
    ledger = _parse_exposure_ledger(store)
    assert ledger is not None
    assert ledger.known_exposed_ranges == ()
    assert ledger.trial_count == 0
    assert ledger.valid


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "unreadable",
        "digest-mismatch",
    ],
)
def test_invalid_registered_ledger_is_fail_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], mutation: str
) -> None:
    store, _source, _hypothesis = campaign
    path = store.root / f"{mutation}-ledger.json"
    path.write_bytes(LEDGER_FIXTURE.read_bytes())
    digest = hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    store.register_exposure_ledger(path, digest)
    if mutation == "missing":
        path.unlink()
    elif mutation == "unreadable":
        path.unlink()
        path.mkdir()
    else:
        path.write_bytes(LEDGER_FIXTURE.read_bytes() + b"corrupt")

    ledger = _parse_exposure_ledger(store)

    assert ledger is not None
    assert ledger.valid is False


def test_corrupt_file_cannot_match_canonical_empty_ledger_digest(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, _hypothesis = campaign
    corrupt = store.root / "corrupt-empty-ledger.json"
    canonical_empty = json.dumps(
        {"history_unknown": False, "known_exposed_ranges": [], "trial_count": 0},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    corrupt.write_bytes(canonical_empty)
    from gateway.research.admission import ExposureLedger

    expected_empty_digest = ExposureLedger((), False, 0, "").computed_sha256
    store.register_exposure_ledger(corrupt, expected_empty_digest)
    corrupt.write_bytes(canonical_empty + b"corrupt")

    ledger = _parse_exposure_ledger(store)

    assert ledger is not None
    assert ledger.valid is False
    assert ledger.ledger_sha256 == ""


def test_pinned_receipt_supplies_ordered_sessions_without_calendar_inference() -> None:
    raw = json.loads(RECEIPT_FIXTURE.read_text(encoding="utf-8"))
    from gateway.research.admission import ValidationReceipt

    receipt = ValidationReceipt.from_wire(
        raw,
        acceptance_class="wire",
        receipt_sha256=hashlib.sha256(RECEIPT_FIXTURE.read_bytes()).hexdigest(),
    )

    sessions = decode_panel_sessions(raw, receipt)

    assert sessions == (
        "2025-09-15",
        "2025-09-16",
        "2025-09-17",
        "2025-09-18",
        "2025-09-19",
        "2025-09-22",
        "2025-09-23",
        "2025-09-24",
        "2025-09-25",
        "2025-09-26",
    )
    assert "2025-09-20" not in sessions
    assert "2025-09-21" not in sessions


def test_wired_admission_uses_registered_policy_ledger_and_receipt_sessions(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, stored_hypothesis = campaign
    payload = _payload()
    eval_spec = tmp_path / "evaluator.json"
    eval_payload = {
        "instruments": [{"ticker": "SPY", "instrument_class": "etf"}],
        "start_session": "2025-09-15",
        "end_session": "2025-09-26",
        "holding": {"max_sessions": 5},
    }
    eval_spec.write_text(json.dumps(eval_payload), encoding="utf-8")
    receipt_wire = json.loads(RECEIPT_FIXTURE.read_text(encoding="utf-8"))
    document_payload = copy.deepcopy(payload)
    document_payload["analysis"] = {"start": "2025-09-15", "end": "2025-09-26"}
    document_payload["evaluation"] = {"start": "2025-09-15", "end": "2025-09-26"}
    document_payload["training"] = None
    document_payload["features"][0]["lookback_sessions"] = 0  # type: ignore[index]
    _set_events(document_payload, 5)  # a 10-session window cannot hold the default 100 events
    document = _document(document_payload)
    spec = replace(
        stored_hypothesis,
        spec_json=document.to_json(),
        spec_sha256=document.sha256,
        receipt_path=str(RECEIPT_FIXTURE.absolute()),
        receipt_sha256=hashlib.sha256(RECEIPT_FIXTURE.read_bytes()).hexdigest(),
        evaluation_spec_path=str(eval_spec),
        evaluation_spec_sha256=hashlib.sha256(eval_spec.read_bytes()).hexdigest(),
        panel_sha256=str(receipt_wire["panel_sha256"]),
        dividends_path=str(RECEIPT_FIXTURE.absolute()),
        dividends_sha256=hashlib.sha256(RECEIPT_FIXTURE.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(store, "get_hypothesis", lambda _hypothesis_id: spec)
    _wire_evaluation_spec_set(store, spec)
    store.set_campaign_policy(3, None, "operator-test")
    store.register_exposure_ledger(
        LEDGER_FIXTURE, hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    )

    decision = _admission_for_hypothesis(store, spec.hypothesis_id)

    assert decision.admitted
    assert decision.execution_capability.sources == frozenset({"panel"})
    assert (
        decision.receipt.receipt_sha256 == hashlib.sha256(RECEIPT_FIXTURE.read_bytes()).hexdigest()
    )


SESSIONS = ("2025-09-15", "2025-09-16", "2025-09-26")
SEMANTIC = "a" * 64
SESSIONS_H0008 = ("2021-10-01", "2021-10-04", "2026-07-31")


def _write_evaluator(tmp_path: Path, name: str, document: dict[str, object]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _v1_evaluator_document() -> dict[str, object]:
    return {
        "instruments": [{"ticker": "SPY", "instrument_class": "etf"}],
        "start_session": "2025-09-15",
        "end_session": "2025-09-26",
        "holding": {"max_sessions": 5},
        "costs": {"half_spread_bps": 1, "slippage_bps": 3, "commission_bps": 1},
    }


def _v3_evaluator_document(**changes: object) -> dict[str, object]:
    document = _v1_evaluator_document()
    document["instruments"] = [
        {"ticker": "SPY", "instrument_class": "etf", "shortable": True},
        {"ticker": "XLF", "instrument_class": "etf"},
    ]
    document["holding"] = {"max_sessions": 60}
    document["long_only"] = False
    document["costs"] = {
        "half_spread_bps": 1,
        "slippage_bps": 3,
        "commission_bps": 1,
        "borrow_bps_annual": 75.5,
    }
    document.update(changes)
    return document


def test_v1_evaluator_spec_parses_unchanged_and_compares_equal_to_itself(
    tmp_path: Path,
) -> None:
    path = _write_evaluator(tmp_path, "v1.json", _v1_evaluator_document())

    left = _parse_evaluator_bounds(path, SEMANTIC, SESSIONS)
    right = _parse_evaluator_bounds(path, SEMANTIC, SESSIONS)

    assert left.long_only is True
    assert left.borrow_bps_annual == 0.0
    assert left.shortable_tickers == frozenset()
    assert left.max_holding_sessions == 5
    assert all(item.shortable is False for item in left.instruments)
    assert _same_evaluator_bounds(left, right)


def test_v3_evaluator_fields_round_trip_into_the_bounds(tmp_path: Path) -> None:
    path = _write_evaluator(tmp_path, "v3.json", _v3_evaluator_document())

    bounds = _parse_evaluator_bounds(path, SEMANTIC, SESSIONS)

    assert bounds.long_only is False
    assert bounds.borrow_bps_annual == 75.5
    assert bounds.max_holding_sessions == 60
    assert bounds.shortable_tickers == frozenset({"SPY"})
    assert _same_evaluator_bounds(bounds, _parse_evaluator_bounds(path, SEMANTIC, SESSIONS))


@pytest.mark.parametrize(
    "changes",
    [
        {
            "instruments": [
                {"ticker": "SPY", "instrument_class": "etf"},
                {"ticker": "XLF", "instrument_class": "etf", "shortable": True},
            ]
        },
        {"long_only": True},
    ],
)
def test_spec_set_equality_compares_shortable_and_long_only(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    base = _parse_evaluator_bounds(
        _write_evaluator(tmp_path, "base.json", _v3_evaluator_document()), SEMANTIC, SESSIONS
    )
    other = _parse_evaluator_bounds(
        _write_evaluator(tmp_path, "other.json", _v3_evaluator_document(**changes)),
        SEMANTIC,
        SESSIONS,
    )

    assert not _same_evaluator_bounds(base, other)


def test_borrow_is_a_cost_field_and_does_not_break_spec_set_equality(tmp_path: Path) -> None:
    base = _parse_evaluator_bounds(
        _write_evaluator(tmp_path, "base.json", _v3_evaluator_document()), SEMANTIC, SESSIONS
    )
    doubled = _parse_evaluator_bounds(
        _write_evaluator(
            tmp_path,
            "doubled.json",
            _v3_evaluator_document(
                costs={
                    "half_spread_bps": 2,
                    "slippage_bps": 6,
                    "commission_bps": 2,
                    "borrow_bps_annual": 151.0,
                }
            ),
        ),
        SEMANTIC,
        SESSIONS,
    )

    assert doubled.borrow_bps_annual == 151.0
    assert _same_evaluator_bounds(base, doubled)


@pytest.mark.parametrize(
    "changes",
    [
        {"costs": {"borrow_bps_annual": -1}},
        {"costs": {"borrow_bps_annual": True}},
        {"costs": {"borrow_bps_annual": "5"}},
        {"costs": {"unknown_bps": 1}},
        {"instruments": [{"ticker": "SPY", "instrument_class": "etf", "shortable": "yes"}]},
        {"long_only": "no"},
    ],
)
def test_malformed_v3_evaluator_fields_are_refused(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    path = _write_evaluator(tmp_path, "bad.json", _v3_evaluator_document(**changes))

    with pytest.raises(_AdmissionInputError):
        _parse_evaluator_bounds(path, SEMANTIC, SESSIONS)


def test_admission_rejects_second_spec_non_cost_bound_change(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, stored_hypothesis = campaign
    spec = _wired_spec(stored_hypothesis, tmp_path)
    alternate = tmp_path / "alternate-evaluator.json"
    alternate.write_text(
        json.dumps(
            {
                "instruments": [{"ticker": "SPY", "instrument_class": "etf"}],
                "start_session": "2025-09-15",
                "end_session": "2025-09-26",
                "holding": {"max_sessions": 6},
            }
        ),
        encoding="utf-8",
    )
    manifest = EvaluationSpecSet(
        "research-evaluation-spec-set-v1",
        spec.hypothesis_id,
        "c000",
        (
            EvaluationSpecEntry("c000", spec.evaluation_spec_path, spec.evaluation_spec_sha256),
            EvaluationSpecEntry(
                "c001", str(alternate), hashlib.sha256(alternate.read_bytes()).hexdigest()
            ),
        ),
        "2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(store, "get_hypothesis", lambda _hypothesis_id: spec)
    monkeypatch.setattr(store, "evaluation_spec_set", lambda _hypothesis_id: manifest)
    with pytest.raises(_AdmissionInputError, match="non-cost bound"):
        _admission_for_hypothesis(store, spec.hypothesis_id)


def _hedged_evaluator_document(
    *, scale: int = 1, borrow: float | None = 50.0, shortable: bool = True
) -> dict[str, object]:
    """A real-shaped hedged spec; ``scale`` multiplies every cost, borrow included."""
    document: dict[str, object] = {
        "instruments": [{"ticker": "SPY", "instrument_class": "etf", "shortable": shortable}],
        "start_session": "2025-09-15",
        "end_session": "2025-09-26",
        "holding": {"max_sessions": 5},
        "long_only": False,
        "costs": {
            "half_spread_bps": 1.0 * scale,
            "slippage_bps": 3.0 * scale,
            "commission_bps": 1.0 * scale,
        },
    }
    if borrow is not None:
        document["costs"]["borrow_bps_annual"] = borrow * scale  # type: ignore[index]
    return document


def _wire_candidate_set(
    store: ResearchStore,
    stored: HypothesisSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    documents: list[dict[str, object]],
) -> HypothesisSpec:
    """Wire a spec set whose c000 is the primary and the rest are candidates."""
    spec = _wired_spec(stored, tmp_path, evaluator_document=documents[0])
    entries = [EvaluationSpecEntry("c000", spec.evaluation_spec_path, spec.evaluation_spec_sha256)]
    for index, document in enumerate(documents[1:], start=1):
        path = tmp_path / f"candidate-{index}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        entries.append(
            EvaluationSpecEntry(
                f"c{index:03d}", str(path), hashlib.sha256(path.read_bytes()).hexdigest()
            )
        )
    manifest = EvaluationSpecSet(
        "research-evaluation-spec-set-v1",
        spec.hypothesis_id,
        "c000",
        tuple(entries),
        "2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(store, "get_hypothesis", lambda _hypothesis_id: spec)
    monkeypatch.setattr(store, "evaluation_spec_set", lambda _hypothesis_id: manifest)
    store.set_campaign_policy(3, None, "operator-test")
    store.register_exposure_ledger(
        LEDGER_FIXTURE, hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    )
    return spec


def test_wired_hedged_spec_set_with_cost_scaled_borrow_is_admitted(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, stored = campaign
    documents = [_hedged_evaluator_document(), _hedged_evaluator_document(scale=2)]
    spec = _wire_candidate_set(store, stored, tmp_path, monkeypatch, documents)

    decision = _admission_for_hypothesis(store, spec.hypothesis_id)

    assert decision.admitted, decision.detail


def test_wired_hedged_primary_without_borrow_is_refused_with_the_typed_code(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, stored = campaign
    spec = _wire_candidate_set(
        store, stored, tmp_path, monkeypatch, [_hedged_evaluator_document(borrow=None)]
    )

    # The per-spec loop covers the primary too, so the wiring boundary refuses first.
    with pytest.raises(_AdmissionInputError, match="c000") as refusal:
        _admission_for_hypothesis(store, spec.hypothesis_id)

    assert refusal.value.reason == AdmissionReason.BORROW_COST_MISSING.value


def test_wired_hedged_candidate_without_borrow_is_refused_even_if_the_primary_has_it(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, stored = campaign
    documents = [_hedged_evaluator_document(), _hedged_evaluator_document(scale=2, borrow=None)]
    spec = _wire_candidate_set(store, stored, tmp_path, monkeypatch, documents)

    with pytest.raises(_AdmissionInputError, match="c001") as refusal:
        _admission_for_hypothesis(store, spec.hypothesis_id)

    assert refusal.value.reason == "BORROW_COST_MISSING"


def test_wired_hedged_spec_without_a_shortable_instrument_is_refused(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, stored = campaign
    spec = _wire_candidate_set(
        store, stored, tmp_path, monkeypatch, [_hedged_evaluator_document(shortable=False)]
    )

    decision = _admission_for_hypothesis(store, spec.hypothesis_id)

    assert decision.reason is AdmissionReason.SHORTABLE_INSTRUMENT_MISSING


H0008_SPECS = Path(__file__).parent / "fixtures" / "h0008-evaluation-specs"


def test_real_h0008_evaluator_specs_stay_long_only_and_compare_equal() -> None:
    # Copies of the three frozen H0008 specs: costs 1x/2x/3x are the only variation.
    parsed = [
        _parse_evaluator_bounds(H0008_SPECS / f"c00{index}.json", SEMANTIC, SESSIONS_H0008)
        for index in range(3)
    ]

    for bounds in parsed:
        assert bounds.long_only is True
        assert bounds.borrow_bps_annual == 0.0
        assert bounds.shortable_tickers == frozenset()
        assert bounds.max_holding_sessions == 5
        assert len(bounds.instruments) == 12
    assert _same_evaluator_bounds(parsed[0], parsed[1])
    assert _same_evaluator_bounds(parsed[0], parsed[2])
    costs = [
        json.loads((H0008_SPECS / f"c00{index}.json").read_text(encoding="utf-8"))["costs"]
        for index in range(3)
    ]
    assert [item["half_spread_bps"] for item in costs] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("mutation", ["missing", "malformed", "mutated", "symlink"])
def test_admission_rejects_missing_malformed_mutated_or_symlinked_spec_set(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store, _source, stored_hypothesis = campaign
    spec = _wired_spec(stored_hypothesis, tmp_path)
    evaluation_path = Path(spec.evaluation_spec_path)
    evaluation_digest = hashlib.sha256(evaluation_path.read_bytes()).hexdigest()
    set_path = evaluation_path
    if mutation == "symlink":
        set_path = tmp_path / "evaluation-spec-link.json"
        set_path.symlink_to(evaluation_path)
        spec = replace(
            spec,
            evaluation_spec_path=str(set_path),
            evaluation_spec_sha256=evaluation_digest,
        )
    manifest = EvaluationSpecSet(
        "research-evaluation-spec-set-v1",
        spec.hypothesis_id,
        "c000",
        (EvaluationSpecEntry("c000", str(evaluation_path), evaluation_digest),),
        "2026-01-01T00:00:00Z",
    )
    manifest_raw = json.loads(manifest.to_json())
    if mutation == "symlink":
        manifest_raw["specs"][0]["path"] = str(set_path)
    payload = json.dumps(manifest_raw, sort_keys=True, separators=(",", ":"))
    if mutation == "missing":
        _replace_spec_set_evidence(
            store,
            spec.hypothesis_id,
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
        )
        with store._connect() as conn:
            conn.execute("DROP TRIGGER immutable_hypothesis_evidence_delete")
            conn.execute(
                "DELETE FROM hypothesis_evidence WHERE hypothesis_id=? "
                "AND kind='evaluation_spec_set'",
                (spec.hypothesis_id,),
            )
            conn.executescript(
                """
                CREATE TRIGGER immutable_hypothesis_evidence_delete
                BEFORE DELETE ON hypothesis_evidence
                BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END;
                """
            )
            conn.commit()
    elif mutation == "malformed":
        _replace_spec_set_evidence(
            store, spec.hypothesis_id, "not-json", hashlib.sha256(b"not-json").hexdigest()
        )
    elif mutation == "mutated":
        _replace_spec_set_evidence(store, spec.hypothesis_id, payload, "0" * 64)
    else:
        _replace_spec_set_evidence(
            store, spec.hypothesis_id, payload, hashlib.sha256(payload.encode()).hexdigest()
        )
    monkeypatch.setattr(store, "get_hypothesis", lambda _hypothesis_id: spec)

    with pytest.raises(_AdmissionInputError) as caught:
        _admission_for_hypothesis(store, spec.hypothesis_id)
    assert caught.value.reason in {
        "EVALUATION_SPEC_SET_REJECTED",
        "EVALUATION_SPEC_DIGEST_MISMATCH",
    }


@pytest.mark.parametrize(
    ("instrument_class", "feature_source", "expected"),
    [
        ("etf", "panel", None),
        ("etf", "reddit", "INPUT_CAPABILITY_UNSUPPORTED"),
        ("stock", "panel", "STOCK_EARNINGS_UNAVAILABLE"),
    ],
)
def test_attempt_open_cli_wires_capability_and_earnings_refusals(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instrument_class: str,
    feature_source: str,
    expected: str | None,
) -> None:
    store, source, stored_hypothesis = campaign
    freeze_with_probe(store, stored_hypothesis.hypothesis_id)
    spec = _wired_spec(
        replace(stored_hypothesis, state=HypothesisState.FROZEN),
        tmp_path,
        instrument_class=instrument_class,
        feature_source=feature_source,
    )
    monkeypatch.setattr(store, "get_hypothesis", lambda _hypothesis_id: spec)
    _wire_evaluation_spec_set(store, spec)
    store.set_campaign_policy(3, None, "operator-test")
    store.register_exposure_ledger(
        LEDGER_FIXTURE, hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    )
    monkeypatch.setattr("gateway.research.cli.ResearchStore", lambda _root: store)

    result = runner.invoke(
        app,
        [
            "research",
            "attempt-open",
            spec.hypothesis_id,
            "--root",
            str(store.root),
            "--worktree",
            str(source),
        ],
    )

    if expected is None:
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "H0001-A001"
        assert store.get_attempt("H0001-A001").state.value == "OPENED"
    else:
        assert result.exit_code == 1
        assert expected in result.output
        refusal = store.latest_admission_refusal(spec.hypothesis_id)
        assert refusal is not None and refusal[0] == expected


def test_dispatch_policy_race_releases_pending_attempt_without_readiness_or_budget(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    admitted = _admit(
        _document(_payload()),
        evaluation_spec_set_sha256=_stored_spec_set_digest(store, hypothesis.hypothesis_id),
    )
    attempt = store.open_attempt(hypothesis.hypothesis_id, source, admission=admitted)
    record = implementation(attempt.attempt_id, "a" * 40)
    plan = run_plan(store, attempt, record)
    store.submit_implementation(
        attempt.attempt_id,
        record,
        run_plan=plan,
        containment_provenance=provenance_evidence(attempt.attempt_id, record.commit),
    )
    verified_review(store, review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256))
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    store.queue_run_request(
        attempt.attempt_id, "job-admission-race", run_dir, 30, 256, run_plan=plan
    )
    changed = replace(admitted, campaign_policy=CampaignPolicy(True, 2))
    monkeypatch.setattr(research_cli, "_admission_for_hypothesis", lambda *_: changed)
    monkeypatch.setattr(
        research_cli,
        "_host_execution_ready",
        lambda *_: pytest.fail("host readiness must follow admission"),
    )
    store.acquire_owner_lock()
    try:
        result = research_cli._dispatch_queued_job(store)
    finally:
        store.release_owner_lock()

    assert result == "ADMISSION_POLICY_RACE"
    assert store.get_attempt(attempt.attempt_id).state is AttemptState.REVIEW_PASSED
    job = store.job_for(attempt.attempt_id)
    assert job is not None and job["state"] == "ADMISSION_REFUSED"
    assert store.latest_admission_refusal(hypothesis.hypothesis_id) == (
        "ADMISSION_POLICY_RACE",
        "admission decision differs from stored payload",
    )
    assert research_cli._budget_execution_ready(store, attempt.attempt_id) == (
        "budget_campaign_policy_unset"
    )


def test_attempt_cap_pause_cancel_and_status_remain_safe(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    for number in (1, 2):
        attempt = store.open_attempt(hypothesis.hypothesis_id, source)
        record = implementation(attempt.attempt_id, "a" * 40)
        store.submit_implementation(
            attempt.attempt_id,
            record,
            run_plan=run_plan(store, attempt, record),
            containment_provenance=provenance_evidence(attempt.attempt_id, record.commit),
        )
        store.collect_review_evidence(
            attempt.attempt_id,
            review(attempt.attempt_id, "a" * 40, hypothesis.spec_sha256, verdict="FAIL"),
            json.dumps(
                {
                    "bound_commit": "a" * 40,
                    "bound_spec_sha256": hypothesis.spec_sha256,
                    "verdict": "FAIL",
                }
            ),
        )
        store.close_attempt(attempt.attempt_id, AttemptDecision.RETRY, f"retry {number}")
    final = store.open_attempt(hypothesis.hypothesis_id, source)
    final_record = implementation(final.attempt_id, "a" * 40)
    final_plan = run_plan(store, final, final_record)
    store.submit_implementation(
        final.attempt_id,
        final_record,
        run_plan=final_plan,
        containment_provenance=provenance_evidence(final.attempt_id, final_record.commit),
    )
    verified_review(store, review(final.attempt_id, "a" * 40, hypothesis.spec_sha256))
    run_dir = (
        store.root / "hypotheses" / hypothesis.hypothesis_id / "attempts" / final.attempt_id / "run"
    )
    store.queue_run_request(final.attempt_id, "job-cap", run_dir, 30, 256, run_plan=final_plan)

    paused = runner.invoke(
        app, ["research", "pause", "--root", str(store.root), "--reason", "cap review"]
    )
    cancelled = runner.invoke(
        app, ["research", "cancel", final.attempt_id, "--root", str(store.root)]
    )
    status = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])

    assert paused.exit_code == 0, paused.output
    assert cancelled.exit_code == 0, cancelled.output
    assert store.get_attempt(final.attempt_id).state is AttemptState.RUN_FAILED
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["campaign"][0] == "PAUSED"
    assert compose_wake(store) is None
    store.close_attempt(final.attempt_id, AttemptDecision.FINISH, "cap complete")
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.FINISHED, "cap complete")


def test_final_allocated_attempt_completes_review_run_collect_and_decision(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.research.review_evidence import collect_review

    del monkeypatch
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path)
    assert (
        collect_review(
            store, attempt_id, native.openclaw_database, native.codex_state_database
        ).state
        is AttemptState.REVIEW_PASSED
    )
    run_dir = store.root / "hypotheses" / "H0001" / "attempts" / attempt_id / "run"
    plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    queued = store.queue_run_request(attempt_id, "job-final", run_dir, 30, 256, run_plan=plan)
    assert queued.state is AttemptState.RUN_QUEUED
    store.claim_queued_job(attempt_id, "job-final")
    outcome = RunOutcome(
        attempt_id,
        "job-final",
        0,
        True,
        False,
        True,
        "accepted",
        "fixture",
        str(run_dir / "result.json"),
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
    )
    assert store.finish_run(attempt_id, outcome).state is AttemptState.RUN_SUCCEEDED
    assert (
        store.close_attempt(attempt_id, AttemptDecision.FINISH, "final complete").state
        is AttemptState.CLOSED
    )
    assert (
        store.decide_hypothesis("H0001", HypothesisDecision.FINISHED, "final complete").state
        is HypothesisState.DECIDED
    )


def test_admission_decision_is_insert_only_and_replay_bound(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, _hypothesis = campaign
    freeze_with_probe(store, "H0001")
    attempt = store.open_attempt("H0001", source)
    payload = _payload()
    decision = _admit(
        _document(payload),
        evaluation_spec_set_sha256=_stored_spec_set_digest(store, "H0001"),
    )

    store.insert_admission_decision(attempt.attempt_id, decision)
    store.insert_admission_decision(attempt.attempt_id, decision)

    assert json.loads(store.evidence(attempt.attempt_id, "admission_decision"))["admitted"]
    with pytest.raises(StoreConflict):
        store.insert_admission_decision(
            attempt.attempt_id,
            replace(decision, detail="different immutable decision"),
        )


def test_wired_policy_unset_pauses_status_and_suppresses_wake(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    payload = _payload()
    decision = _admit(_document(payload), policy=CampaignPolicy(False))
    assert decision.reason is AdmissionReason.CAMPAIGN_POLICY_UNSET
    _record_admission_refusal(store, hypothesis.hypothesis_id, decision)

    status = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])

    plan = compose_wake(store)

    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["campaign"][0] == "PAUSED"
    assert status_payload["policy_set"] is False
    assert status_payload["hypotheses"][0]["admission_refusal"]["reason"] == "CAMPAIGN_POLICY_UNSET"
    assert plan is None


def test_nonpausing_refusal_reason_is_visible_in_owner_wake(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    decision = _admit(_document(_payload()), capability=ExecutionCapability(frozenset({"reddit"})))
    assert decision.reason is AdmissionReason.INPUT_CAPABILITY_UNSUPPORTED
    store.record_admission_refusal(hypothesis.hypothesis_id, decision)

    plan = compose_wake(store)

    assert plan is not None
    assert "INPUT_CAPABILITY_UNSUPPORTED" in plan.message


def test_malformed_refusal_event_is_unavailable_in_read_only_status(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO events(at,hypothesis_id,attempt_id,kind,detail,actor) VALUES(?,?,?,?,?,?)",
            (
                "2026-09-10T00:00:00Z",
                hypothesis.hypothesis_id,
                None,
                "admission_refused",
                json.dumps({"reason": 123}),
                "driver",
            ),
        )
        conn.commit()

    result = runner.invoke(app, ["research", "status", "--root", str(store.root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "unavailable_reason" in payload["hypotheses"][0]["admission_refusal"]


def test_unset_policy_refusal_pauses_campaign_and_is_status_ready(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, _source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    decision = _admit(_document(_payload()), policy=CampaignPolicy(False))

    _record_admission_refusal(store, hypothesis.hypothesis_id, decision)

    assert store.campaign()[0] == "PAUSED"
    assert store.latest_admission_refusal(hypothesis.hypothesis_id) == (
        "CAMPAIGN_POLICY_UNSET",
        "campaign remains paused until an explicit operator policy is set",
    )
