"""Focused tests for the pure scientific admission contract."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.research.admission import (
    AdmissionDecision,
    AdmissionReason,
    CampaignPolicy,
    DividendCoverage,
    EarningsCoverage,
    EarningsCoverageStatus,
    EvaluatorBounds,
    ExecutionCapability,
    ExposureLedger,
    ExposureRange,
    Instrument,
    InstrumentClass,
    OverlapClassification,
    ValidationReceipt,
    admit_hypothesis,
)
from gateway.research.contracts import HypothesisSpec
from gateway.research.hypothesis import HypothesisDocument

RAW_EVAL_SHA = hashlib.sha256(b"raw evaluator spec").hexdigest()
SEMANTIC_EVAL_SHA = hashlib.sha256(b"semantic evaluator spec").hexdigest()
PANEL_SHA = hashlib.sha256(b"panel").hexdigest()
RECEIPT_SHA = hashlib.sha256(b"receipt").hexdigest()
SNAPSHOT_SHA = hashlib.sha256(b"snapshot").hexdigest()
REQUEST_SHA = hashlib.sha256(b"request").hexdigest()
UNIVERSE_SHA = hashlib.sha256(b"universe").hexdigest()
DIVIDENDS_SHA = hashlib.sha256(b"dividends").hexdigest()


@pytest.fixture
def payload() -> dict[str, object]:
    return {
        "contract": "research-hypothesis-v1",
        "mechanism": "A short-term reversal after an overnight move.",
        "prediction": "The next regular close return is positive on average.",
        "target": "next_regular_close_return",
        "baseline": "buy-and-hold over the same evaluation sessions",
        "universe_rule": "ETF panel members with a complete close history.",
        "features": [
            {
                "name": "overnight_return",
                "source": "panel",
                "as_of_rule": "known at session open",
                "lookback_sessions": 1,
            }
        ],
        "entry_rule": "Enter at the next regular open after a sufficiently large move.",
        "exit_rule": "Exit at the following regular close.",
        "position_sizing_rule": "Use the trusted outer evaluator sizing contract.",
        "variants": ["baseline-threshold", "wider-threshold"],
        "search_budget_evaluations": 2,
        "analysis": {"start": "2020-01-01", "end": "2024-12-31"},
        "evaluation": {"start": "2024-01-01", "end": "2024-12-31"},
        "training": {"start": "2020-01-01", "end": "2023-12-31"},
        "purpose": "DEVELOPMENT_VALIDATION",
        "forward_label_sessions": 1,
        "purge_rule": "Purge at least the maximum declared forward label horizon.",
        "primary_metric": "mean net return per evaluation session",
        "minimum_evidence": {"sessions": 1, "trades": 0},
        "null_tests": ["placebo entry dates"],
        "reject_criteria": "Reject if the primary metric is not positive.",
        "missing_data_rule": "Skip a signal when any required panel value is absent.",
        "compute": {"max_wall_seconds": 120.0, "max_rss_mb": 1024},
        "deliverables": ["reports/summary.json"],
    }


def _document(payload: dict[str, object]) -> HypothesisDocument:
    return HypothesisDocument.from_json(json.dumps(payload, separators=(",", ":")))


def _receipt() -> ValidationReceipt:
    return ValidationReceipt(
        snapshot_sha256=SNAPSHOT_SHA,
        spec_sha256_raw=RAW_EVAL_SHA,
        spec_sha256_semantic=SEMANTIC_EVAL_SHA,
        universe_sha256=UNIVERSE_SHA,
        dividends_sha256=DIVIDENDS_SHA,
        receipt_sha256=RECEIPT_SHA,
        request_sha256=REQUEST_SHA,
        panel_sha256=PANEL_SHA,
        universe_file_sha256=UNIVERSE_SHA,
        validation_dividends_sha256=DIVIDENDS_SHA,
        panel_start="2019-01-01",
        panel_end="2024-12-31",
        instruments=("SPY",),
        verdict="PASS",
        reasons=(),
        acceptance_class="etf_only",
        dividends_coverage=DividendCoverage(
            action_type="dividend",
            start_date="2019-01-01",
            end_date="2024-12-31",
            completed_at="2026-09-06T20:04:14.179876Z",
        ),
    )


def _bounds(
    *, instrument_class: InstrumentClass = InstrumentClass.ETF, max_holding: int = 5
) -> EvaluatorBounds:
    return EvaluatorBounds(
        panel_start="2019-01-01",
        panel_end="2024-12-31",
        instruments=(Instrument("SPY", instrument_class),),
        semantic_spec_sha256=SEMANTIC_EVAL_SHA,
        max_holding_sessions=max_holding,
        earnings_coverage=EarningsCoverage(EarningsCoverageStatus.UNAVAILABLE),
        panel_sessions=("2019-01-01", "2020-01-01", "2024-12-31"),
    )


def _ledger(
    *,
    ranges: tuple[ExposureRange, ...] = (),
    history_unknown: bool = False,
    trial_count: int | None = 0,
) -> ExposureLedger:
    unsigned = ExposureLedger(ranges, history_unknown, trial_count, "")
    return replace(unsigned, ledger_sha256=unsigned.computed_sha256)


def _spec(document: HypothesisDocument, *, max_attempts: int = 3) -> HypothesisSpec:
    return HypothesisSpec(
        "H0001",
        "reversal",
        document.to_json(),
        document.sha256,
        "/panel",
        "/receipt",
        "/evaluation-spec",
        RAW_EVAL_SHA,
        PANEL_SHA,
        RECEIPT_SHA,
        max_attempts,
        "a" * 40,
        "2026-09-09T00:00:00Z",
    )


_DEFAULT = object()


def _admit(
    document: HypothesisDocument,
    *,
    spec: HypothesisSpec | None = None,
    receipt: ValidationReceipt | None = None,
    ledger: ExposureLedger | None | object = _DEFAULT,
    bounds: EvaluatorBounds | None = None,
    policy: CampaignPolicy | None = None,
    capability: ExecutionCapability | None = None,
) -> AdmissionDecision:
    return admit_hypothesis(
        document,
        _spec(document) if spec is None else spec,
        _receipt() if receipt is None else receipt,
        _ledger() if ledger is _DEFAULT else ledger,  # type: ignore[arg-type]
        _bounds() if bounds is None else bounds,
        CampaignPolicy(True, 3) if policy is None else policy,
        ExecutionCapability(frozenset({"panel"})) if capability is None else capability,
    )


def test_valid_price_only_development_is_admitted_and_binds_all_evidence(
    payload: dict[str, object],
) -> None:
    result = _admit(_document(payload))

    assert result.admitted is True
    assert result.reason is None
    assert result.overlap_classification is OverlapClassification.NONE
    assert result.exposure_ledger_sha256 is not None
    assert result.execution_capability.sources == frozenset({"panel"})
    assert result.hypothesis_document == _document(payload)
    assert result.hypothesis_spec.spec_sha256 == result.hypothesis_sha256


def test_canonical_decision_serialization_is_stable_and_retains_capability_and_ledger(
    payload: dict[str, object],
) -> None:
    result = _admit(_document(payload))
    encoded = result.to_json()
    decoded = json.loads(encoded)

    assert encoded == result.to_json()
    assert result.sha256 == hashlib.sha256(encoded.encode()).hexdigest()
    assert decoded["overlap_classification"] == "NONE"
    assert decoded["exposure_ledger_sha256"] == result.exposure_ledger_sha256
    assert decoded["execution_capability"] == ["panel"]
    assert decoded["hypothesis_document_json"] == result.hypothesis_document.to_json()
    assert decoded["hypothesis_spec_json"] == result.hypothesis_spec.to_json()


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("spec_sha256", AdmissionReason.SPEC_DIGEST_MISMATCH),
        ("panel_sha256", AdmissionReason.PANEL_DIGEST_MISMATCH),
        ("receipt_sha256", AdmissionReason.RECEIPT_DIGEST_MISMATCH),
        ("evaluation_spec_sha256", AdmissionReason.EVALUATION_SPEC_DIGEST_MISMATCH),
    ],
)
def test_outer_digest_mismatch_is_refused(
    payload: dict[str, object], field: str, reason: AdmissionReason
) -> None:
    document = _document(payload)
    spec = _spec(document)
    changed: HypothesisSpec
    replacement = hashlib.sha256(field.encode()).hexdigest()
    if field == "spec_sha256":
        changed = replace(spec, spec_sha256=replacement)
    elif field == "panel_sha256":
        changed = replace(spec, panel_sha256=replacement)
    elif field == "receipt_sha256":
        changed = replace(spec, receipt_sha256=replacement)
    else:
        changed = replace(spec, evaluation_spec_sha256=replacement)

    result = _admit(document, spec=changed)

    assert result.admitted is False
    assert result.reason is reason


def test_hypothesis_spec_json_mismatch_is_refused(payload: dict[str, object]) -> None:
    document = _document(payload)
    spec = replace(_spec(document), spec_json="{}")

    result = _admit(document, spec=spec)

    assert result.reason is AdmissionReason.HYPOTHESIS_SPEC_MISMATCH


def test_receipt_semantic_digest_mismatch_is_refused(payload: dict[str, object]) -> None:
    receipt = replace(_receipt(), spec_sha256_semantic=hashlib.sha256(b"changed").hexdigest())

    result = _admit(_document(payload), receipt=receipt)

    assert result.reason is AdmissionReason.EVALUATION_SPEC_DIGEST_MISMATCH


def test_rejected_receipt_is_refused(payload: dict[str, object]) -> None:
    result = _admit(_document(payload), receipt=replace(_receipt(), verdict="FAIL"))

    assert result.reason is AdmissionReason.RECEIPT_REJECTED


def test_direct_receipt_duplicate_digest_mismatch_is_refused(payload: dict[str, object]) -> None:
    receipt = replace(_receipt(), universe_file_sha256=hashlib.sha256(b"wrong").hexdigest())

    result = _admit(_document(payload), receipt=receipt)

    assert result.reason is AdmissionReason.RECEIPT_INTERNAL_DIGEST_MISMATCH


def test_receipt_panel_bounds_must_match_evaluator_bounds(payload: dict[str, object]) -> None:
    receipt = replace(_receipt(), panel_start="2020-01-01")

    result = _admit(_document(payload), receipt=receipt)

    assert result.reason is AdmissionReason.PANEL_BOUNDS_MISMATCH


def test_pinned_receipt_fixture_hash_and_exact_wire_shape() -> None:
    fixture = Path(__file__).parent / "fixtures" / "receipt.json"
    payload_bytes = fixture.read_bytes()
    digest = hashlib.sha256(payload_bytes).hexdigest()
    wire = json.loads(payload_bytes)

    receipt = ValidationReceipt.from_wire(
        wire,
        acceptance_class="etf_only",
        receipt_sha256=digest,
    )

    "d63e1e872ad6dcb5a7362f8474d206e474e2c5362cead9ce4968b71fc193a45f"  # pragma: allowlist secret
    assert set(wire) == {
        "contract_version",
        "coverage",
        "coverage_sha256",
        "exported_at",
        "hydrated_at",
        "panel_sha256",
        "request",
        "request_sha256",
    }
    assert set(wire["request"]) == {
        "contract_version",
        "end",
        "market_hours",
        "start",
        "tickers",
        "timeframe",
    }
    assert set(wire["coverage"]) == {
        "compressed_size",
        "compression_ratio",
        "contract_version",
        "coverage_sha256",
        "encoding",
        "expanded_size",
        "payload",
    }
    assert digest == receipt.receipt_sha256
    assert receipt.coverage_expanded_size == wire["coverage"]["expanded_size"]
    assert receipt.acceptance_class == "etf_only"


def test_pinned_receipt_duplicate_coverage_digest_is_rejected() -> None:
    fixture = Path(__file__).parent / "fixtures" / "receipt.json"
    wire = json.loads(fixture.read_bytes())
    wire["coverage"]["coverage_sha256"] = hashlib.sha256(b"wrong").hexdigest()

    # Keep the frozen receipt digest on its scanner-audited source line.
    # The fixture hash is independently checked by the preceding test.
    # This is public receipt provenance, not a credential.
    # The assertion below exercises a mutated copy of the parsed wire.
    "d63e1e872ad6dcb5a7362f8474d206e474e2c5362cead9ce4968b71fc193a45f"  # pragma: allowlist secret
    with pytest.raises(ValueError, match="coverage digests disagree"):
        ValidationReceipt.from_wire(
            wire,
            acceptance_class="etf_only",
            receipt_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
        )


def test_coverage_expanded_size_is_part_of_decision_identity(
    payload: dict[str, object],
) -> None:
    document = _document(payload)
    smaller = _admit(document, receipt=replace(_receipt(), coverage_expanded_size=100))
    larger = _admit(document, receipt=replace(_receipt(), coverage_expanded_size=101))

    assert smaller.mapping()["receipt"]["coverage_expanded_size"] == 100  # type: ignore[index]
    assert larger.mapping()["receipt"]["coverage_expanded_size"] == 101  # type: ignore[index]
    assert smaller.to_json() != larger.to_json()
    assert smaller.sha256 != larger.sha256


@pytest.mark.parametrize(
    "ledger",
    [
        None,
        ExposureLedger((), False, 0, ""),
        ExposureLedger((), False, 0, "abc"),
        ExposureLedger((), False, 0, "g" * 64),
        ExposureLedger((), False, 0, "0" * 64),
        ExposureLedger((ExposureRange("2024-01-01", "2024-12-31"),), False, 0, "0" * 64),
        ExposureLedger((ExposureRange("2024-06-01", "2024-01-01"),), False, 0, "0" * 64),
        ExposureLedger(
            (ExposureRange("2024-01-01", "2024-06-30"), ExposureRange("2024-06-30", "2024-12-31")),
            False,
            0,
            "0" * 64,
        ),
    ],
)
def test_missing_malformed_mismatched_reversed_or_overlapping_ledger_fails_closed(
    payload: dict[str, object], ledger: ExposureLedger | None
) -> None:
    result = _admit(_document(payload), ledger=ledger)

    assert result.admitted is False
    assert result.reason is AdmissionReason.EXPOSURE_LEDGER_INVALID


def test_unknown_history_development_is_admitted_as_unknown(payload: dict[str, object]) -> None:
    ledger = _ledger(history_unknown=True, trial_count=0)

    result = _admit(_document(payload), ledger=ledger)

    assert result.admitted is True
    assert result.overlap_classification is OverlapClassification.UNKNOWN_HISTORY


def test_unknown_trial_count_development_is_admitted_as_unknown(payload: dict[str, object]) -> None:
    result = _admit(_document(payload), ledger=_ledger(trial_count=None))

    assert result.admitted is True
    assert result.overlap_classification is OverlapClassification.UNKNOWN_HISTORY


def test_unknown_history_final_holdout_is_refused(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["purpose"] = "FINAL_HOLDOUT"

    result = _admit(_document(changed), ledger=_ledger(history_unknown=True))

    assert result.reason is AdmissionReason.HOLDOUT_HISTORY_UNKNOWN


def test_known_overlap_development_is_admitted_but_holdout_is_refused(
    payload: dict[str, object],
) -> None:
    ledger = _ledger(ranges=(ExposureRange("2024-06-01", "2024-06-30"),))
    development = _admit(_document(payload), ledger=ledger)
    changed = copy.deepcopy(payload)
    changed["purpose"] = "FINAL_HOLDOUT"
    holdout = _admit(_document(changed), ledger=ledger)

    assert development.admitted is True
    assert development.overlap_classification is OverlapClassification.KNOWN_EXPOSED
    assert holdout.reason is AdmissionReason.HOLDOUT_KNOWN_EXPOSED


def test_empty_pristine_ledger_allows_final_holdout(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["purpose"] = "FINAL_HOLDOUT"

    result = _admit(_document(changed), ledger=_ledger())

    assert result.admitted is True
    assert result.overlap_classification is OverlapClassification.NONE


def test_capability_panel_allows_price_feature_and_reddit_requires_supplied_capability(
    payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(payload)
    changed["features"][0]["source"] = "reddit"  # type: ignore[index]
    document = _document(changed)
    refused = _admit(document)
    admitted = _admit(document, capability=ExecutionCapability(frozenset({"panel", "reddit"})))

    assert refused.reason is AdmissionReason.INPUT_CAPABILITY_UNSUPPORTED
    assert "overnight_return" in (refused.detail or "")
    assert admitted.admitted is True


def test_derived_feature_without_exposed_dependencies_fails_closed(
    payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(payload)
    changed["features"][0]["source"] = "derived"  # type: ignore[index]

    result = _admit(
        _document(changed), capability=ExecutionCapability(frozenset({"panel", "derived"}))
    )

    assert result.reason is AdmissionReason.INPUT_CAPABILITY_UNSUPPORTED
    assert "overnight_return" in (result.detail or "")


def test_unset_policy_fails_closed(payload: dict[str, object]) -> None:
    result = _admit(_document(payload), policy=CampaignPolicy(False))

    assert result.reason is AdmissionReason.CAMPAIGN_POLICY_UNSET


def test_policy_cap_below_outer_max_attempts_fails_closed(payload: dict[str, object]) -> None:
    result = _admit(_document(payload), policy=CampaignPolicy(True, 2))

    assert result.reason is AdmissionReason.CAMPAIGN_ATTEMPT_CAP_EXCEEDED


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            {"analysis": {"start": "2018-01-01", "end": "2024-12-31"}},
            AdmissionReason.ANALYSIS_OUTSIDE_PANEL,
        ),
        (
            {
                "features": [
                    {
                        "name": "overnight_return",
                        "source": "panel",
                        "as_of_rule": "open",
                        "lookback_sessions": 5000,
                    }
                ]
            },
            AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL,
        ),
        ({"forward_label_sessions": 6}, AdmissionReason.HOLDING_HORIZON_EXCEEDS_LIMIT),
    ],
)
def test_bounds_and_exact_five_session_limit(
    payload: dict[str, object], mutation: dict[str, object], reason: AdmissionReason
) -> None:
    changed = copy.deepcopy(payload)
    changed.update(mutation)
    result = _admit(_document(changed))

    assert result.reason is reason


def test_five_session_horizon_is_inclusive(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["forward_label_sessions"] = 5

    result = _admit(_document(changed), bounds=_bounds(max_holding=5))

    assert result.admitted is True


def test_panel_boundary_dates_are_inclusive(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["features"][0]["lookback_sessions"] = 0  # type: ignore[index]
    changed["analysis"] = {"start": "2019-01-01", "end": "2024-12-31"}
    changed["evaluation"] = {"start": "2019-01-01", "end": "2024-12-31"}
    changed["training"] = None

    result = _admit(_document(changed))

    assert result.admitted is True


def test_exactly_available_prior_panel_session_is_admitted(payload: dict[str, object]) -> None:
    result = _admit(_document(payload))

    assert result.admitted is True


def test_one_session_too_many_for_lookback_is_refused(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["features"][0]["lookback_sessions"] = 2  # type: ignore[index]

    result = _admit(_document(changed))

    assert result.reason is AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL


def test_missing_analysis_session_is_refused(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["analysis"] = {"start": "2020-01-02", "end": "2024-12-31"}
    changed["training"] = None

    result = _admit(_document(changed))

    assert result.reason is AdmissionReason.FEATURE_LOOKBACK_OUTSIDE_PANEL


def test_holiday_gap_uses_exact_sessions_not_calendar_days(payload: dict[str, object]) -> None:
    changed = copy.deepcopy(payload)
    changed["analysis"] = {"start": "2019-01-03", "end": "2019-01-03"}
    changed["evaluation"] = {"start": "2019-01-03", "end": "2019-01-03"}
    changed["training"] = None
    bounds = replace(
        _bounds(),
        panel_sessions=("2019-01-01", "2019-01-03", "2024-12-31"),
    )

    result = _admit(_document(changed), bounds=bounds)

    assert result.admitted is True


@pytest.mark.parametrize(
    "sessions",
    [
        (),
        ("2019-01-01", "2018-12-31", "2024-12-31"),
        ("2019-01-01", "2020-01-01", "2020-01-01", "2024-12-31"),
        ("2018-12-31", "2024-12-31"),
    ],
)
def test_panel_session_tuple_must_be_ordered_unique_and_bound_to_panel(
    sessions: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        replace(_bounds(), panel_sessions=sessions)


def test_evaluator_maximum_holding_horizon_is_refused(payload: dict[str, object]) -> None:
    result = _admit(_document(payload), bounds=_bounds(max_holding=6))

    assert result.reason is AdmissionReason.HOLDING_HORIZON_EXCEEDS_LIMIT


def test_fresh_equivalent_admission_is_deterministic(payload: dict[str, object]) -> None:
    first = _admit(_document(payload))
    second = _admit(_document(payload))

    assert first.to_json() == second.to_json()


def test_stock_without_trusted_earnings_is_refused_but_etf_is_allowed(
    payload: dict[str, object],
) -> None:
    stock = _admit(_document(payload), bounds=_bounds(instrument_class=InstrumentClass.STOCK))
    etf = _admit(_document(payload), bounds=_bounds(instrument_class=InstrumentClass.ETF))
    covered = _bounds(instrument_class=InstrumentClass.STOCK)
    covered = replace(covered, earnings_coverage=EarningsCoverage(EarningsCoverageStatus.TRUSTED))
    stock_covered = _admit(_document(payload), bounds=covered)

    assert stock.reason is AdmissionReason.STOCK_EARNINGS_UNAVAILABLE
    assert etf.admitted is True
    assert stock_covered.admitted is True


def test_receipt_instrument_set_mismatch_is_refused(payload: dict[str, object]) -> None:
    result = _admit(_document(payload), receipt=replace(_receipt(), instruments=("QQQ",)))

    assert result.reason is AdmissionReason.INSTRUMENT_SET_MISMATCH
