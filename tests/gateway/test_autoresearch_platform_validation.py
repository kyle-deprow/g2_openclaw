from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from gateway.autoresearch_platform_validation import (
    DynamicPriceCoverageReceipt,
    PlatformCoverageStatus,
    PlatformCoverageValidationError,
    PlatformCoverageViolationCode,
    canonical_dynamic_price_coverage_digest,
    canonical_requested_sessions_digest,
)


def _complete_receipt() -> dict[str, object]:
    receipt: dict[str, object] = {
        "contract_version": "dynamic-price-coverage-v1",
        "source_contract_version": "price-coverage-v1",
        "scope": "full_union_hydration",
        "status": "COMPLETE",
        "requested_start_date": "2024-01-02",
        "requested_end_date": "2024-01-03",
        "timeframe": "1min",
        "market_hours": "regular",
        "source_requested_start_date": "2024-01-02",
        "source_requested_end_date": "2024-01-03",
        "source_timeframe": "1min",
        "source_market_hours": "regular",
        "source_provider": "massive",
        "member_union_digest": "a" * 64,
        "requested_sessions_digest": canonical_requested_sessions_digest(
            (date(2024, 1, 2), date(2024, 1, 3))
        ),
        "pit_active_roster_digest": "c" * 64,
        "source_price_coverage_response_digest": "d" * 64,
        "member_union_count": 3,
        "requested_session_count": 2,
        "hydrated_symbol_sessions": 6,
        "observed_hydrated_symbol_sessions": 6,
        "provider_empty_hydrated_symbol_sessions": 0,
        "missing_hydrated_symbol_sessions": 0,
        "active_symbol_sessions": 4,
        "observed_active_symbol_sessions": 4,
        "provider_empty_active_symbol_sessions": 0,
        "missing_active_symbol_sessions": 0,
        "inactive_union_symbol_sessions": 2,
        "unexpected_ticker_count": 0,
        "unexpected_session_count": 0,
        "violation_codes": [],
    }
    receipt["receipt_digest"] = canonical_dynamic_price_coverage_digest(receipt)
    return receipt


def test_receipt_parses_canonical_full_union_coverage() -> None:
    raw = _complete_receipt()

    receipt = DynamicPriceCoverageReceipt.from_dict(raw)

    assert receipt.status is PlatformCoverageStatus.COMPLETE
    assert receipt.member_union_count == 3


def test_violation_code_set_matches_the_flattened_quantipy_contract() -> None:
    assert tuple(code.value for code in PlatformCoverageViolationCode) == (
        "unexpected_ticker",
        "unexpected_symbol_session",
        "missing_hydrated_symbol_session",
        "provider_empty_active_symbol_session",
        "missing_active_symbol_session",
    )


def test_receipt_rejects_unknown_fields() -> None:
    raw = _complete_receipt()
    raw["untrusted"] = True

    with pytest.raises(PlatformCoverageValidationError, match="unknown"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_rejects_obsolete_digest_field_names() -> None:
    raw = _complete_receipt()
    raw["active_roster_digest"] = raw.pop("pit_active_roster_digest")

    with pytest.raises(PlatformCoverageValidationError, match=r"missing|unknown"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_rejects_boolean_counts() -> None:
    raw = _complete_receipt()
    raw["member_union_count"] = True
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="member_union_count"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_rejects_unknown_market_hours() -> None:
    raw = _complete_receipt()
    raw["market_hours"] = "overnight"
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="market_hours"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_rejects_non_regular_platform_contract_hours() -> None:
    raw = _complete_receipt()
    raw["market_hours"] = "extended"
    raw["source_market_hours"] = "extended"
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="regular-hours only"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_rejects_source_identity_mismatch_instead_of_violation_claim() -> None:
    raw = _complete_receipt()
    raw["source_requested_end_date"] = "2024-01-04"
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="source request identity"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_rejects_zero_identity_counts() -> None:
    raw = _complete_receipt()
    raw["member_union_count"] = 0
    raw["hydrated_symbol_sessions"] = 0
    raw["observed_hydrated_symbol_sessions"] = 0
    raw["active_symbol_sessions"] = 0
    raw["observed_active_symbol_sessions"] = 0
    raw["inactive_union_symbol_sessions"] = 0
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="member_union_count"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_receipt_binds_count_and_digest_to_authoritative_xnys_sessions() -> None:
    receipt = DynamicPriceCoverageReceipt.from_dict(_complete_receipt())

    receipt.validate_requested_sessions((date(2024, 1, 2), date(2024, 1, 3)))

    with pytest.raises(PlatformCoverageValidationError, match="real XNYS session count"):
        receipt.validate_requested_sessions((date(2024, 1, 2),))


def test_receipt_requires_full_union_hydration_count_to_cover_every_requested_session() -> None:
    raw = _complete_receipt()
    raw["hydrated_symbol_sessions"] = 5
    raw["observed_hydrated_symbol_sessions"] = 5
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="hydrated_symbol_sessions"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_pit_active_scope_reports_the_same_full_union_and_active_geometries() -> None:
    raw = _complete_receipt()
    raw["scope"] = "pit_active_roster"
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    receipt = DynamicPriceCoverageReceipt.from_dict(raw)

    assert receipt.scope.value == "pit_active_roster"


def test_remediation_requires_codes_that_match_observed_violations() -> None:
    raw = _complete_receipt()
    raw["status"] = "REMEDIATION_REQUIRED"
    raw["observed_hydrated_symbol_sessions"] = 5
    raw["missing_hydrated_symbol_sessions"] = 1
    raw["active_symbol_sessions"] = 5
    raw["observed_active_symbol_sessions"] = 5
    raw["inactive_union_symbol_sessions"] = 1
    raw["violation_codes"] = [PlatformCoverageViolationCode.MISSING_HYDRATED_SYMBOL_SESSION.value]
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    receipt = DynamicPriceCoverageReceipt.from_dict(raw)

    assert receipt.violation_codes == (
        PlatformCoverageViolationCode.MISSING_HYDRATED_SYMBOL_SESSION,
    )


def test_complete_rejects_real_violation_counts() -> None:
    raw = _complete_receipt()
    raw["observed_hydrated_symbol_sessions"] = 5
    raw["missing_hydrated_symbol_sessions"] = 1
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    with pytest.raises(PlatformCoverageValidationError, match="COMPLETE"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_complete_allows_provider_empty_inactive_union_sessions() -> None:
    raw = _complete_receipt()
    raw["observed_hydrated_symbol_sessions"] = 4
    raw["provider_empty_hydrated_symbol_sessions"] = 2
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)

    receipt = DynamicPriceCoverageReceipt.from_dict(raw)

    assert receipt.status is PlatformCoverageStatus.COMPLETE


def test_receipt_rejects_a_noncanonical_digest() -> None:
    raw = deepcopy(_complete_receipt())
    raw["receipt_digest"] = "0" * 64

    with pytest.raises(PlatformCoverageValidationError, match="receipt_digest"):
        DynamicPriceCoverageReceipt.from_dict(raw)


def test_autoresearch_docs_require_the_shared_full_union_validator() -> None:
    plan = Path("docs/reference/quantipy-autonomous-research-plan.md").read_text(encoding="utf-8")
    skill = Path("gateway/agent_config/skills/autoresearch/SKILL.md").read_text(encoding="utf-8")

    assert "qp.validate_dynamic_price_coverage" in plan
    assert "full_union_hydration" in skill
    assert "platform_coverage_contract_mismatch" in plan
