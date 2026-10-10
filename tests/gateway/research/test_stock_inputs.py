"""Optional ``earnings`` / ``membership`` bound inputs and daily receipts (stock campaigns)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from gateway.research import cli as research_cli
from gateway.research import readiness
from gateway.research import store as research_store
from gateway.research.admission import (
    COMMON_STOCK_REFUSAL,
    AdmissionReason,
    EarningsCoverage,
    EarningsCoverageStatus,
    ValidationReceipt,
    admit_hypothesis,
)
from gateway.research.cli import _admission_for_hypothesis, _AdmissionInputError
from gateway.research.contracts import (
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisSpec,
    InputBinding,
)
from gateway.research.receipt_sessions import (
    daily_receipt_view,
    decode_daily_sessions,
    membership_universe,
    panel_sessions_from_receipt_bytes,
)
from gateway.research.status import read_status
from gateway.research.store import ResearchStore
from typer.testing import CliRunner, Result

from tests.gateway.research.conftest import freeze_with_probe
from tests.gateway.research.stock_fixtures import (
    EVAL_END,
    EVAL_START,
    PANEL_END,
    SESSIONS,
    _sessions,
    daily_receipt_text,
    earnings_bytes,
    membership_bytes,
    sha256,
    stock_eval_spec_text,
    stock_payload,
)
from tests.gateway.research.test_status_control import _configured_store

LEDGER_FIXTURE = Path(__file__).parent / "fixtures" / "exposure-ledger.json"
MINUTE_RECEIPT = Path(__file__).parent / "fixtures" / "receipt.json"
BASE_COMMIT = "b" * 40


@dataclass
class Inputs:
    root: Path
    spec: Path
    panel: Path
    receipt: Path
    eval_spec: Path
    dividends: Path
    spec_set: Path
    earnings: Path
    membership: Path

    def create(
        self,
        store: ResearchStore,
        *,
        earnings: bool = True,
        membership: bool = True,
    ) -> HypothesisSpec:
        return store.create_hypothesis(
            "stock",
            self.spec,
            self.panel,
            self.receipt,
            self.eval_spec,
            BASE_COMMIT,
            dividends=self.dividends,
            evaluation_spec_set=self.spec_set,
            earnings=self.earnings if earnings else None,
            membership=self.membership if membership else None,
        )


def make_inputs(
    root: Path,
    *,
    eval_text: str | None = None,
    receipt_text: str | None = None,
    payload: dict[str, object] | None = None,
    membership_data: bytes | None = None,
) -> Inputs:
    root.mkdir(parents=True, exist_ok=True)
    panel = root / "panel.parquet"
    panel.write_bytes(b"PAR1stock-panelPAR1")
    membership = root / "membership.json"
    membership.write_bytes(membership_data or membership_bytes())
    earnings = root / "earnings.json"
    earnings.write_bytes(earnings_bytes())
    receipt = root / "receipt.json"
    receipt.write_text(
        receipt_text
        or daily_receipt_text(sha256(panel.read_bytes()), sha256(membership.read_bytes())),
        encoding="utf-8",
    )
    eval_spec = root / "eval-c000.json"
    eval_spec.write_text(eval_text or stock_eval_spec_text(1), encoding="utf-8")
    dividends = root / "dividends.json"
    dividends.write_text('{"contract":"trusted-dividends-v2"}', encoding="utf-8")
    spec = root / "spec.json"
    spec.write_text(json.dumps(payload or stock_payload()), encoding="utf-8")
    spec_set = root / "evaluation-spec-set.json"
    spec_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0001",
            "c000",
            (EvaluationSpecEntry("c000", str(eval_spec), sha256(eval_spec.read_bytes())),),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    return Inputs(root, spec, panel, receipt, eval_spec, dividends, spec_set, earnings, membership)


@pytest.fixture
def stock(tmp_path: Path) -> tuple[ResearchStore, Inputs]:
    return _configured_store(tmp_path / "driver"), make_inputs(tmp_path / "inputs")


def minute_payload() -> dict[str, object]:
    """A hypothesis whose ranges fit the committed one-minute receipt fixture (2025-09)."""
    payload = stock_payload()
    payload["analysis"] = {"start": "2025-09-15", "end": "2025-09-26"}
    payload["evaluation"] = {"start": "2025-09-15", "end": "2025-09-26"}
    return payload


def _refusal_events(store: ResearchStore) -> list[str]:
    return [
        str(json.loads(event.detail)["reason"])
        for event in store.events()
        if event.kind == "hypothesis_create_refused"
    ]


# -- create ------------------------------------------------------------------------------


def test_create_persists_both_bindings_as_insert_only_evidence_without_widening_the_row(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock

    hypothesis = inputs.create(store)

    bindings = store.input_bindings(hypothesis.hypothesis_id)
    assert bindings == {
        "earnings": InputBinding(
            str(inputs.earnings.resolve()), sha256(inputs.earnings.read_bytes())
        ),
        "membership": InputBinding(
            str(inputs.membership.resolve()), sha256(inputs.membership.read_bytes())
        ),
    }
    # The key-exact hypothesis payload and the table columns are untouched.
    assert set(json.loads(hypothesis.to_json())) == {
        field for field in HypothesisSpec.__dataclass_fields__
    }
    with store._connect() as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(hypotheses)")}
        kinds = {
            str(row[0])
            for row in conn.execute(
                "SELECT kind FROM hypothesis_evidence WHERE hypothesis_id='H0001'"
            )
        }
        assert not any("earnings" in column or "membership" in column for column in columns)
        assert kinds == {"evaluation_spec_set", "earnings_binding", "membership_binding"}
        with pytest.raises(sqlite3.IntegrityError, match="insert-only"):
            conn.execute(
                "UPDATE hypothesis_evidence SET payload_json='{}' WHERE kind LIKE '%_binding'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="insert-only"):
            conn.execute("DELETE FROM hypothesis_evidence WHERE kind LIKE '%_binding'")
    projection = store.root / "hypotheses" / "H0001" / "earnings_binding.json"
    assert InputBinding.from_json(projection.read_text(encoding="utf-8")) == bindings["earnings"]


def test_bindings_feed_probe_bindings_and_every_frozen_input_map(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock
    hypothesis = inputs.create(store)

    _pins, probe_inputs = store.probe_bindings("H0001")
    paths, digests = store.frozen_inputs(hypothesis)

    assert probe_inputs["earnings_sha256"] == sha256(inputs.earnings.read_bytes())
    assert probe_inputs["membership_sha256"] == sha256(inputs.membership.read_bytes())
    assert (
        set(paths)
        == set(digests)
        == {
            "spec",
            "panel",
            "receipt",
            "evaluation_spec",
            "dividends",
            "earnings",
            "membership",
        }
    )
    assert paths["earnings"] == inputs.earnings.resolve()
    assert digests["membership"] == probe_inputs["membership_sha256"]
    # A recorded probe binds the optional digests, so a freeze needs the same inputs.
    freeze_with_probe(store, "H0001")
    probe = store.compute_probe("H0001")
    assert probe is not None and probe.inputs == probe_inputs


def test_a_probe_recorded_without_the_digests_is_refused_for_a_stock_hypothesis(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    from gateway.research.contracts import COMPUTE_PROBE_CONTRACT, ComputeProbe, ProbeStage
    from gateway.research.store import StoreConflict

    store, inputs = stock
    inputs.create(store)
    pins, probe_inputs = store.probe_bindings("H0001")
    stripped = {k: v for k, v in probe_inputs.items() if k != "earnings_sha256"}
    probe = ComputeProbe(
        COMPUTE_PROBE_CONTRACT,
        "P000000000001",
        "H0001",
        pins,
        stripped,
        (
            ProbeStage("validate-c000", "c000", 0, 0.5, 1),
            ProbeStage("evaluate-s000", "c000", 0, 0.5, 1),
        ),
        "2026-01-01T00:00:00Z",
    )

    with pytest.raises(StoreConflict, match="inputs do not match"):
        store.record_compute_probe(probe)


def test_stock_spec_without_earnings_is_refused_with_the_evaluator_text(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock

    with pytest.raises(ValueError) as refused:
        inputs.create(store, earnings=False)

    assert COMMON_STOCK_REFUSAL in str(refused.value)
    assert "--earnings" in str(refused.value)
    assert store.hypotheses() == []
    assert len(_refusal_events(store)) == 1
    assert not (store.root / "hypotheses" / "H0001").exists()


def test_etf_only_spec_needs_no_earnings_and_may_bind_one(tmp_path: Path) -> None:
    etf = json.dumps(
        {
            "instruments": [{"ticker": "SPY", "instrument_class": "etf"}],
            "start_session": EVAL_START,
            "end_session": EVAL_END,
            "holding": {"max_sessions": 5},
        }
    )
    store = _configured_store(tmp_path / "driver")
    inputs = make_inputs(tmp_path / "inputs", eval_text=etf)

    plain = inputs.create(store, earnings=False)

    assert set(store.input_bindings(plain.hypothesis_id)) == {"membership"}


@pytest.mark.parametrize("case", ["missing", "wrong-digest", "receipt-binds-null"])
def test_daily_receipt_requires_the_membership_file_it_binds(tmp_path: Path, case: str) -> None:
    store = _configured_store(tmp_path / "driver")
    inputs = make_inputs(tmp_path / "inputs")
    if case == "wrong-digest":
        inputs.membership.write_bytes(membership_bytes(("AAA", "CCC")))
    if case == "receipt-binds-null":
        inputs.receipt.write_text(
            daily_receipt_text(sha256(inputs.panel.read_bytes()), None), encoding="utf-8"
        )

    with pytest.raises(ValueError) as refused:
        inputs.create(store, membership=case != "missing")

    message = str(refused.value)
    assert {
        "missing": "requires --membership",
        "wrong-digest": "does not match the receipt membership_sha256",
        "receipt-binds-null": "does not bind a membership_sha256",
    }[case] in message
    assert store.hypotheses() == [] and len(_refusal_events(store)) == 1


def test_minute_receipt_refuses_a_membership_file(tmp_path: Path) -> None:
    store = _configured_store(tmp_path / "driver")
    inputs = make_inputs(
        tmp_path / "inputs",
        receipt_text=MINUTE_RECEIPT.read_text(),
        payload=minute_payload(),
        eval_text=stock_eval_spec_text(1)
        .replace(EVAL_START, "2025-09-15")
        .replace(EVAL_END, "2025-09-26"),
    )

    with pytest.raises(ValueError, match="requires a daily panel receipt"):
        inputs.create(store)


@pytest.mark.parametrize("name", ["earnings", "membership"])
def test_optional_inputs_must_be_regular_non_symlink_files(
    stock: tuple[ResearchStore, Inputs], name: str
) -> None:
    store, inputs = stock
    target = getattr(inputs, name)
    link = target.with_name(f"{name}-link.json")
    link.symlink_to(target)
    setattr(inputs, name, link)

    with pytest.raises(ValueError, match=f"{name} must be a regular non-symlink file"):
        inputs.create(store)


def test_a_hypothesis_without_the_new_inputs_loads_unchanged(tmp_path: Path) -> None:
    """An old root (no binding rows): same row, same frozen maps, same probe bindings."""
    store = _configured_store(tmp_path / "driver")
    etf = json.dumps(
        {
            "instruments": [{"ticker": "SPY", "instrument_class": "etf"}],
            "start_session": "2025-09-15",
            "end_session": "2025-09-26",
            "holding": {"max_sessions": 5},
        }
    )
    inputs = make_inputs(
        tmp_path / "inputs",
        eval_text=etf,
        receipt_text=MINUTE_RECEIPT.read_text(),
        payload=minute_payload(),
    )
    hypothesis = inputs.create(store, earnings=False, membership=False)

    assert store.input_bindings("H0001") == {}
    paths, digests = store.frozen_inputs(hypothesis)
    assert (
        set(paths) == set(digests) == {"spec", "panel", "receipt", "evaluation_spec", "dividends"}
    )
    _pins, probe_inputs = store.probe_bindings("H0001")
    assert set(probe_inputs) == {
        "dividends_sha256",
        "evaluation_spec_set_sha256",
        "panel_sha256",
        "receipt_sha256",
    }
    assert store.hypotheses() == [hypothesis]
    assert read_status(store.root, unit_state=None).hypothesis_id == "H0001"


def test_repair_projections_rewrites_a_missing_binding_projection(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock
    inputs.create(store)
    projection = store.root / "hypotheses" / "H0001" / "membership_binding.json"
    projection.unlink()

    assert store.repair_projections() == 1
    assert InputBinding.from_json(projection.read_text()).sha256 == sha256(
        inputs.membership.read_bytes()
    )


def _cli_create(store: ResearchStore, inputs: Inputs, *extra: str) -> Result:
    from gateway.cli import app

    return CliRunner().invoke(
        app,
        [
            "research",
            "hypothesis-create",
            "--root",
            str(store.root),
            "--title",
            "stock",
            "--spec-file",
            str(inputs.spec),
            "--panel",
            str(inputs.panel),
            "--receipt",
            str(inputs.receipt),
            "--eval-spec",
            str(inputs.eval_spec),
            "--dividends",
            str(inputs.dividends),
            "--base-commit",
            BASE_COMMIT,
            "--evaluation-spec-set",
            str(inputs.spec_set),
            *extra,
        ],
    )


def test_cli_hypothesis_create_takes_the_two_optional_inputs(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock

    refused = _cli_create(store, inputs, "--membership", str(inputs.membership))
    created = _cli_create(
        store,
        inputs,
        "--earnings",
        str(inputs.earnings),
        "--membership",
        str(inputs.membership),
    )

    assert refused.exit_code == 1
    assert COMMON_STOCK_REFUSAL in refused.output
    assert created.exit_code == 0, created.output
    assert created.output.strip() == "H0001"
    assert set(store.input_bindings("H0001")) == {"earnings", "membership"}


def test_status_reads_a_stock_hypothesis_with_binding_rows(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock
    inputs.create(store)

    status = read_status(store.root, unit_state=None)

    assert status.hypothesis_id == "H0001"


# -- daily receipts ----------------------------------------------------------------------


def _wire(**changes: object) -> dict[str, object]:
    wire: dict[str, object] = json.loads(daily_receipt_text("a" * 64, "b" * 64))
    wire.update(changes)
    return wire


def test_daily_receipt_sessions_come_from_the_per_session_counts() -> None:
    wire = _wire(session_ticker_counts={"2024-01-03": 2, "2024-01-02": 2, "2024-01-04": 0})

    assert decode_daily_sessions(wire) == ("2024-01-02", "2024-01-03")
    assert panel_sessions_from_receipt_bytes(
        daily_receipt_text("a" * 64, "b" * 64).encode()
    ) == tuple(SESSIONS)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"session_ticker_counts": {}}, "lists no sessions"),
        ({"session_ticker_counts": {"2024-1-2": 2}}, "ISO date"),
        ({"session_ticker_counts": {"20240102": 2}}, "canonical"),
        ({"session_ticker_counts": {"2024-01-02": -1}}, "non-negative"),
        ({"session_ticker_counts": {"2023-12-29": 2}}, "outside its request range"),
        ({"session_ticker_counts": []}, "must be an object"),
        ({"request": {"start": "2024-01-02", "end": "2024-01-01", "adjusted": True}}, "reversed"),
        ({"request": {"start": "2024-01-02", "end": PANEL_END, "adjusted": False}}, "adjusted"),
        (
            {
                "request": {
                    "start": "2024-01-02",
                    "end": PANEL_END,
                    "adjusted": True,
                    "membership_sha256": "zz",
                }
            },
            "membership_sha256",
        ),
    ],
)
def test_malformed_daily_receipts_are_refused(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        decode_daily_sessions(_wire(**changes))


def test_membership_universe_is_the_member_union_plus_reference_tickers() -> None:
    assert membership_universe(membership_bytes(("BBB", "AAA"), ("SPY", "QQQ"))) == (
        "AAA",
        "BBB",
        "QQQ",
        "SPY",
    )
    with pytest.raises(ValueError, match="contract"):
        membership_universe(b'{"contract":"other","members":{}}')
    with pytest.raises(ValueError, match="not JSON"):
        membership_universe(b"nope")


def test_daily_receipt_view_binds_the_membership_and_lists_the_universe() -> None:
    membership = membership_bytes()
    wire = _wire(request={**_wire()["request"], "membership_sha256": sha256(membership)})  # type: ignore[dict-item]

    view = daily_receipt_view(wire, receipt_sha256="c" * 64, membership_bytes=membership)

    assert view.contract_version == "research-price-panel-daily-v1"
    assert view.instruments == ("AAA", "BBB", "SPY")
    assert view.panel_sha256 == "a" * 64 and view.receipt_sha256 == "c" * 64
    with pytest.raises(ValueError, match="does not match the receipt membership_sha256"):
        daily_receipt_view(wire, receipt_sha256="c" * 64, membership_bytes=membership + b" ")


# -- admission ---------------------------------------------------------------------------


def _admission_ready(store: ResearchStore) -> None:
    store.set_campaign_policy(3, None, "operator-test")
    store.register_exposure_ledger(
        LEDGER_FIXTURE, hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    )


def test_admission_binds_the_snapshot_and_the_daily_universe(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock
    inputs.create(store)
    _admission_ready(store)

    decision = _admission_for_hypothesis(store, "H0001")

    assert decision.admitted, decision.detail
    receipt = json.loads(decision.to_json())["receipt"]
    assert receipt["contract_version"] == "research-price-panel-daily-v1"
    assert receipt["instruments"] == ["AAA", "BBB", "SPY"]  # superset of the spec's AAA + SPY


def test_admission_without_a_bound_earnings_snapshot_refuses_the_stock(
    stock: tuple[ResearchStore, Inputs], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, inputs = stock
    inputs.create(store)
    _admission_ready(store)
    real = store.input_bindings
    monkeypatch.setattr(
        store,
        "input_bindings",
        lambda hid: {k: v for k, v in real(hid).items() if k != "earnings"},
    )

    decision = _admission_for_hypothesis(store, "H0001")

    assert not decision.admitted
    assert decision.reason is AdmissionReason.STOCK_EARNINGS_UNAVAILABLE


def _early_receipt(inputs: Inputs, first: str) -> tuple[str, ...]:
    """Rewrite the receipt so its sessions start at ``first``, before the spec start."""
    sessions = _sessions(date.fromisoformat(first), date.fromisoformat(PANEL_END))
    inputs.receipt.write_text(
        daily_receipt_text(
            sha256(inputs.panel.read_bytes()),
            sha256(inputs.membership.read_bytes()),
            sessions=sessions,
            start=first,
        ),
        encoding="utf-8",
    )
    return sessions


def test_admission_keeps_pre_spec_sessions_and_clips_the_daily_tail(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock
    sessions = _early_receipt(inputs, "2023-11-01")
    inputs.create(store)
    kept = decode_daily_sessions(
        json.loads(inputs.receipt.read_text()), start=EVAL_START, end=EVAL_END
    )
    bounds = research_cli._parse_evaluator_bounds(
        inputs.eval_spec, sha256(inputs.eval_spec.read_bytes()), kept, panel_start=kept[0]
    )

    assert kept[0] == sessions[0] == "2023-11-01" < EVAL_START
    assert bounds.panel_sessions[-1] == EVAL_END and bounds.panel_start == "2023-11-01"
    assert all(item <= EVAL_END for item in bounds.panel_sessions)
    assert sessions[-1] > EVAL_END  # the receipt really carries tail sessions
    assert kept == tuple(item for item in sessions if item <= EVAL_END)
    with pytest.raises(_AdmissionInputError):  # without the override the spec start must match
        research_cli._parse_evaluator_bounds(
            inputs.eval_spec, sha256(inputs.eval_spec.read_bytes()), kept
        )
    with pytest.raises(_AdmissionInputError):  # unclipped, the tail breaks the span check
        research_cli._parse_evaluator_bounds(
            inputs.eval_spec,
            sha256(inputs.eval_spec.read_bytes()),
            tuple(sessions),
            panel_start=sessions[0],
        )


def _early_payload(first: str, training_end: str, forward: int) -> dict[str, object]:
    payload = stock_payload()
    payload["analysis"] = {"start": first, "end": EVAL_END}
    payload["training"] = {"start": first, "end": training_end}
    payload["forward_label_sessions"] = forward
    return payload


def test_create_counts_pre_spec_sessions_for_the_purge_gap(tmp_path: Path) -> None:
    training_end = "2023-12-27"
    sessions = _sessions(date.fromisoformat("2023-12-01"), date.fromisoformat(PANEL_END))
    gap = sum(1 for item in sessions if training_end < item < EVAL_START)
    assert gap >= 2

    def build(name: str, forward: int) -> tuple[ResearchStore, Inputs]:
        store = _configured_store(tmp_path / name / "driver")
        inputs = make_inputs(
            tmp_path / name / "inputs", payload=_early_payload("2023-12-01", training_end, forward)
        )
        _early_receipt(inputs, "2023-12-01")
        return store, inputs

    store, inputs = build("exact", gap)
    assert inputs.create(store).hypothesis_id == "H0001"
    store, inputs = build("exact-plus", gap - 1)
    assert inputs.create(store).hypothesis_id == "H0001"
    store, inputs = build("short", gap + 1)
    with pytest.raises(ValueError, match=f"PURGE_GAP_INSUFFICIENT: only {gap} panel sessions"):
        inputs.create(store)


def test_admission_admits_an_analysis_start_before_the_spec_start(
    stock: tuple[ResearchStore, Inputs], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, inputs = stock
    first = "2023-12-01"
    inputs.spec.write_text(json.dumps(_early_payload(first, "2023-12-27", 1)), encoding="utf-8")
    _early_receipt(inputs, first)
    inputs.create(store)
    _admission_ready(store)
    seen: list[Any] = []
    real = admit_hypothesis  # the same object cli.py imported

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[4])
        return real(*args, **kwargs)

    monkeypatch.setattr(research_cli, "admit_hypothesis", spy)

    decision = _admission_for_hypothesis(store, "H0001")

    assert decision.reason not in (
        AdmissionReason.ANALYSIS_OUTSIDE_PANEL,
        AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL,
    ), decision.detail
    assert decision.admitted, decision.detail
    bounds = seen[0]
    assert bounds.panel_sessions[0] == first and bounds.panel_start == first
    assert bounds.panel_sessions[-1] == EVAL_END == bounds.panel_end
    assert all(item <= EVAL_END for item in bounds.panel_sessions)


def test_admission_lookback_counts_the_pre_spec_sessions(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    first = "2023-12-01"
    sessions = _early_receipt(stock[1], first)
    k = 3
    store, inputs = stock
    results: dict[int, Any] = {}
    for lookback in (k, k + 1):
        store = _configured_store(inputs.root.parent / f"driver-{lookback}")
        payload = _early_payload(sessions[k], "2023-12-27", 1)
        payload["features"][0]["lookback_sessions"] = lookback  # type: ignore[index]
        inputs.spec.write_text(json.dumps(payload), encoding="utf-8")
        inputs.create(store)
        _admission_ready(store)
        results[lookback] = _admission_for_hypothesis(store, "H0001")

    assert results[k].admitted, results[k].detail
    assert results[k + 1].reason is AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL


@pytest.mark.parametrize(
    "start",
    [None, "2024-04-02", "2023-10-02", "2024-01-06", "2024-01-15"],
    ids=["null", "reversed", "before-receipt", "weekend", "holiday"],
)
def test_daily_admission_refuses_an_untrusted_spec_start(
    stock: tuple[ResearchStore, Inputs], monkeypatch: pytest.MonkeyPatch, start: str | None
) -> None:
    store, inputs = stock
    inputs.create(store)
    _admission_ready(store)
    real = research_cli._spec_session_range
    monkeypatch.setattr(
        research_cli,
        "_spec_session_range",
        lambda path: (start, EVAL_END) if path.name == "c000.json" else real(path),
    )

    with pytest.raises(_AdmissionInputError) as refused:
        _admission_for_hypothesis(store, "H0001")

    assert refused.value.reason == "EVALUATION_SPEC_DIGEST_MISMATCH"


def test_daily_spec_set_entry_with_a_later_start_changes_a_non_cost_bound(
    stock: tuple[ResearchStore, Inputs],
) -> None:
    store, inputs = stock
    later = json.loads(inputs.eval_spec.read_text())
    later["start_session"] = SESSIONS[SESSIONS.index(EVAL_START) + 1]
    other = inputs.root / "eval-c001.json"
    other.write_text(json.dumps(later, separators=(",", ":")), encoding="utf-8")
    inputs.spec_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0001",
            "c000",
            (
                EvaluationSpecEntry(
                    "c000", str(inputs.eval_spec), sha256(inputs.eval_spec.read_bytes())
                ),
                EvaluationSpecEntry("c001", str(other), sha256(other.read_bytes())),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    inputs.create(store)
    _admission_ready(store)

    with pytest.raises(_AdmissionInputError) as refused:
        _admission_for_hypothesis(store, "H0001")

    assert "changes a non-cost bound" in refused.value.detail


def test_daily_evaluation_window_must_lie_inside_the_spec_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, training_end = "2023-12-01", "2023-12-27"
    payload = _early_payload(first, training_end, 1)
    payload["evaluation"] = {"start": "2023-12-28", "end": EVAL_END}
    store = _configured_store(tmp_path / "driver")
    inputs = make_inputs(tmp_path / "inputs", payload=payload)
    _early_receipt(inputs, first)

    with pytest.raises(ValueError, match="ANALYSIS_OUTSIDE_PANEL: the evaluation window"):
        inputs.create(store)  # create refuses what admission would refuse forever

    monkeypatch.setattr(research_store, "_require_panel_supports_design", lambda *a: None)
    inputs.create(store)
    _admission_ready(store)
    with pytest.raises(_AdmissionInputError) as refused:
        _admission_for_hypothesis(store, "H0001")
    assert refused.value.reason == AdmissionReason.ANALYSIS_OUTSIDE_PANEL.value
    assert "inside the evaluator spec range" in refused.value.detail


@pytest.mark.parametrize(
    ("name", "reason"),
    [("earnings", "EARNINGS_DIGEST_MISMATCH"), ("membership", "RECEIPT_REJECTED")],
)
def test_admission_fails_closed_when_a_bound_input_changed_or_vanished(
    stock: tuple[ResearchStore, Inputs], name: str, reason: str
) -> None:
    store, inputs = stock
    inputs.create(store)
    _admission_ready(store)
    bound = Path(store.input_bindings("H0001")[name].path)

    bound.write_bytes(b"{}")
    with pytest.raises(_AdmissionInputError) as changed:
        _admission_for_hypothesis(store, "H0001")
    assert changed.value.reason == reason and "digest differs" in changed.value.detail

    bound.unlink()
    with pytest.raises(_AdmissionInputError) as missing:
        _admission_for_hypothesis(store, "H0001")
    assert missing.value.reason == reason


def test_snapshot_bound_is_distinct_from_trusted_coverage() -> None:
    assert EarningsCoverageStatus.SNAPSHOT_BOUND.value == "SNAPSHOT_BOUND"
    assert not EarningsCoverage(EarningsCoverageStatus.SNAPSHOT_BOUND).trusted
    assert EarningsCoverage(EarningsCoverageStatus.SNAPSHOT_BOUND).admits_stock
    assert not EarningsCoverage(EarningsCoverageStatus.UNAVAILABLE).admits_stock


# -- readiness ---------------------------------------------------------------------------


def test_host_readiness_smoke_mounts_the_bound_inputs(
    stock: tuple[ResearchStore, Inputs], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, inputs = stock
    inputs.create(store)
    freeze_with_probe(store, "H0001")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    attempt = store.open_attempt("H0001", worktree)
    seen: dict[str, object] = {}

    def spy(*_args: object, **kwargs: object) -> tuple[str, ...]:
        seen.update(kwargs)
        return ()

    monkeypatch.setattr(readiness, "which_tools", lambda: ("/bin/true", "/bin/true"))
    monkeypatch.setattr(readiness, "bwrap_argv", spy)

    assert readiness.host_execution_ready(store, attempt.attempt_id) is None
    assert seen["earnings"] == inputs.earnings.resolve()
    assert seen["membership"] == inputs.membership.resolve()


# -- daily session list must match the evaluator ------------------------------------------


def _early_wire(**changes: object) -> dict[str, object]:
    wire = _wire(**changes)
    wire["request"] = {**wire["request"], "start": "2023-12-01"}  # type: ignore[dict-item]
    return wire


def test_daily_sessions_inside_the_spec_range_must_all_have_rows_and_none_be_missing() -> None:
    counts = {session: 2 for session in SESSIONS}
    inside = "2024-02-01"
    before = "2023-12-15"
    outside = SESSIONS[-1]  # a tail session past the evaluation end
    assert before < EVAL_START and outside > EVAL_END
    counts = {**counts, "2023-12-14": 2, before: 2}

    zero_inside = _early_wire(session_ticker_counts={**counts, inside: 0})
    with pytest.raises(ValueError, match="2024-02-01 with no rows inside the evaluation range"):
        decode_daily_sessions(zero_inside, start=EVAL_START, end=EVAL_END)
    zero_outside = _early_wire(session_ticker_counts={**counts, outside: 0})
    kept_tail = decode_daily_sessions(zero_outside, start=EVAL_START, end=EVAL_END)
    assert kept_tail[0] == "2023-12-14" and kept_tail[-1] == EVAL_END
    assert inside in kept_tail and outside not in kept_tail
    zero_before = _early_wire(session_ticker_counts={**counts, before: 0})
    tolerated = decode_daily_sessions(zero_before, start=EVAL_START, end=EVAL_END)
    assert before not in tolerated and tolerated[0] == "2023-12-14"

    with pytest.raises(ValueError, match="2024-02-01 missing inside the evaluation range"):
        decode_daily_sessions(
            _early_wire(session_ticker_counts=counts, missing_sessions=[inside]),
            start=EVAL_START,
            end=EVAL_END,
        )
    for tolerated_missing in (outside, before):
        kept = decode_daily_sessions(
            _early_wire(session_ticker_counts=counts, missing_sessions=[tolerated_missing]),
            start=EVAL_START,
            end=EVAL_END,
        )
        assert kept[-1] == EVAL_END and kept[0] == "2023-12-14"
    assert decode_daily_sessions(_early_wire(session_ticker_counts=counts))[-1] == outside


@pytest.mark.parametrize("field", ["session_ticker_counts", "missing_sessions"])
def test_daily_receipt_refuses_weekend_dates(field: str) -> None:
    wire = _wire()
    if field == "session_ticker_counts":
        wire["session_ticker_counts"] = {**wire["session_ticker_counts"], "2024-02-03": 2}  # type: ignore[dict-item]
    else:
        wire["missing_sessions"] = ["2024-02-04"]

    with pytest.raises(ValueError, match="weekend"):
        decode_daily_sessions(wire)


def test_daily_session_list_problems_are_refused_at_create(tmp_path: Path) -> None:
    cases: tuple[tuple[str, dict[str, Any], str], ...] = (
        ("zero", {"counts": {**{s: 2 for s in SESSIONS}, "2024-02-01": 0}}, "no rows inside"),
        ("missing", {"missing_sessions": ("2024-02-01",)}, "missing inside"),
    )
    for name, kwargs, message in cases:
        store = _configured_store(tmp_path / name / "driver")
        inputs = make_inputs(tmp_path / name / "inputs")
        inputs.receipt.write_text(
            daily_receipt_text(
                sha256(inputs.panel.read_bytes()), sha256(inputs.membership.read_bytes()), **kwargs
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=f"invalid daily panel receipt: .*{message}"):
            inputs.create(store)
        assert store.hypotheses() == []


def test_admission_refuses_a_daily_receipt_whose_session_list_disagrees_with_the_spec_range(
    stock: tuple[ResearchStore, Inputs], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, inputs = stock
    hypothesis = inputs.create(store)
    _admission_ready(store)
    receipt = inputs.root / "receipt-missing.json"
    receipt.write_text(
        daily_receipt_text(
            sha256(inputs.panel.read_bytes()),
            sha256(inputs.membership.read_bytes()),
            missing_sessions=("2024-02-01",),
        ),
        encoding="utf-8",
    )
    swapped = replace(
        hypothesis, receipt_path=str(receipt), receipt_sha256=sha256(receipt.read_bytes())
    )
    monkeypatch.setattr(store, "get_hypothesis", lambda _hypothesis_id: swapped)

    with pytest.raises(_AdmissionInputError) as refused:
        _admission_for_hypothesis(store, "H0001")

    assert refused.value.reason == "RECEIPT_REJECTED"
    assert "missing inside the evaluation range" in refused.value.detail


def test_a_minute_receipt_cannot_claim_the_daily_contract() -> None:
    wire = json.loads(MINUTE_RECEIPT.read_text(encoding="utf-8"))
    wire["contract_version"] = "research-price-panel-daily-v1"

    with pytest.raises(
        ValueError, match="contract_version must be research-price-panel-receipt-v2"
    ):
        ValidationReceipt.from_wire(wire, acceptance_class="wire", receipt_sha256="a" * 64)


def test_membership_universe_rejects_non_string_reference_tickers() -> None:
    document = json.loads(membership_bytes())
    document["reference_tickers"] = ["SPY", 7]

    with pytest.raises(ValueError, match="reference_tickers must be strings"):
        membership_universe(json.dumps(document).encode())
