from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from gateway.autoresearch_runner import (
    MEMBER_UNION_DIGEST_ALGORITHM,
    AuthoritativeSnapshotReceipt,
    AutoresearchState,
    AutoresearchValidationContext,
    AutoresearchValidationError,
    DynamicUniverseCoverageReceipt,
    GroupedSummaryReceipt,
    MemberUnionManifestReceipt,
    PriceHydrationReceipt,
    UniverseDateVerificationReceipt,
    UniverseHistoryBatchReceipt,
    UniversePlanArtifact,
    UniverseVerificationReceipt,
    canonical_member_union_digest,
    canonical_member_union_manifest,
    load_state_file,
    price_hydration_coverage_digest,
    price_hydration_request_digest,
    quantipy_member_union_digest,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_CANONICAL_TWO_MEMBER_DIGEST = (
    "9c3997284915605bfff6af270c021e91fa9cf3b806612096c6721831f4b67f5c"  # pragma: allowlist secret
)
_MIGRATED_RECEIPT_SHA256 = (
    "8e3c25d11956e932dde9fdfd4b1e15736923810e2d45183815c4b7254c3ba7ac"  # pragma: allowlist secret
)


def _plan(*, selection_dates: tuple[str, ...] | None = None) -> UniversePlanArtifact:
    return UniversePlanArtifact(
        profile_id="liquid-common-stocks-v1",
        profile_digest=_A,
        selection_dates=selection_dates or ("2021-01-04", "2021-07-01"),
        max_members_per_date=300,
        execution_policy="next-session-or-later",
    )


def _snapshot(date: str, digest: str) -> AuthoritativeSnapshotReceipt:
    return AuthoritativeSnapshotReceipt(
        as_of_date=date,
        source="massive",
        result_count=5000,
        identity_digest=digest,
        content_digest=digest,
        completed_at="2026-07-15T12:00:00+00:00",
    )


def _summary(date: str, digest: str) -> GroupedSummaryReceipt:
    return GroupedSummaryReceipt(
        summary_date=date,
        source="massive",
        result_count=17,
        identity_digest=digest,
        content_digest=digest,
        completed_at="2026-07-15T12:00:00+00:00",
        adjusted=False,
    )


def _manifest_receipt(
    path: str = "/tmp/member-union.txt", sha256: str = _A
) -> MemberUnionManifestReceipt:
    return MemberUnionManifestReceipt(path=path, sha256=sha256)


def _legacy_source(date: str, source: str, digest: str) -> dict[str, object]:
    return {
        "date": date,
        "source": source,
        "result_count": 17,
        "identity_digest": digest,
        "content_digest": digest,
        "completed_at": "2026-07-15T12:00:00+00:00",
        "adjusted": False,
    }


def _date_receipt(selection_date: str, execution_date: str) -> UniverseDateVerificationReceipt:
    return UniverseDateVerificationReceipt(
        selection_date=selection_date,
        earliest_execution_date=execution_date,
        calendar_identity="XNYS",
        calendar_digest=_D,
        selected_member_count=10,
        snapshot=_snapshot(selection_date, _A),
        summary=_summary(selection_date, _B),
    )


def _universe_receipt() -> UniverseVerificationReceipt:
    return UniverseVerificationReceipt(
        profile_id="liquid-common-stocks-v1",
        profile_digest=_A,
        execution_policy="next-session-or-later",
        max_members_per_date=300,
        batches=(
            UniverseHistoryBatchReceipt(
                contract_digest=_B,
                operation_count=1,
                dates=(
                    _date_receipt("2021-01-04", "2021-01-05"),
                    _date_receipt("2021-07-01", "2021-07-02"),
                ),
            ),
        ),
        member_union_digest_algorithm=MEMBER_UNION_DIGEST_ALGORITHM,
        member_union_count=17,
        member_union_digest=_C,
        member_union_manifest=_manifest_receipt(),
    )


def _hydration_receipt() -> PriceHydrationReceipt:
    request_digest = price_hydration_request_digest(
        member_union_count=17,
        member_union_digest=_C,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
    )
    completed_at = "2026-07-15T12:00:00+00:00"
    return PriceHydrationReceipt(
        member_union_count=17,
        member_union_digest=_C,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
        operation_count=1,
        request_digest=request_digest,
        coverage_receipt_digest=price_hydration_coverage_digest(
            request_digest=request_digest,
            operation_count=1,
            completed_at=completed_at,
        ),
        source_price_coverage_response_digest="d" * 64,
        completed_at=completed_at,
        folds_started_at="2026-07-15T12:01:00+00:00",
    )


def _coverage_receipt() -> DynamicUniverseCoverageReceipt:
    return DynamicUniverseCoverageReceipt(
        member_union_count=17,
        member_union_digest=_C,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        oos_start="2021-10-01",
        oos_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
        expected_symbol_sessions=2400,
        covered_symbol_sessions=2400,
        missing_symbol_count=0,
        missing_symbol_sessions=0,
        default_fold_count=24,
        fallback_fold_count=0,
    )


def test_universe_plan_is_canonical_and_supports_full_2021_2026_schedule() -> None:
    full_schedule = tuple(
        f"{year}-{month:02d}-01" for year in range(2021, 2027) for month in range(1, 13)
    )
    assert _plan(selection_dates=full_schedule).selection_dates == full_schedule

    raw = _plan().to_dict()
    raw["selection_dates"] = ["2021-07-01", "2021-01-04"]
    with pytest.raises(AutoresearchValidationError, match="sorted and unique"):
        UniversePlanArtifact.from_dict(raw)

    raw = _plan().to_dict()
    raw["profile_id"] = "Liquid_Common_Stocks"
    with pytest.raises(AutoresearchValidationError, match="kebab-case"):
        UniversePlanArtifact.from_dict(raw)

    raw = _plan().to_dict()
    raw["contract_digest"] = _B
    with pytest.raises(AutoresearchValidationError, match="exact keys"):
        UniversePlanArtifact.from_dict(raw)


def test_max_members_per_date_accepts_1000_and_rejects_1001() -> None:
    replace(_plan(), max_members_per_date=1000).validate()
    with pytest.raises(AutoresearchValidationError, match="between 1 and 1000"):
        replace(_plan(), max_members_per_date=1001).validate()
    with pytest.raises(AutoresearchValidationError, match="between 1 and 1000"):
        replace(_universe_receipt(), max_members_per_date=1001).validate()


def test_universe_receipt_binds_deterministic_batches_and_calendar() -> None:
    receipt = _universe_receipt()
    receipt.validate_against_plan(_plan())
    assert "members" not in receipt.to_dict()
    assert "tickers" not in receipt.to_dict()

    with pytest.raises(AutoresearchValidationError, match="exactly cover"):
        replace(
            receipt,
            batches=(replace(receipt.batches[0], dates=receipt.batches[0].dates[:1]),),
            member_union_count=10,
        ).validate_against_plan(_plan())

    same_session = replace(receipt.batches[0].dates[0], earliest_execution_date="2021-01-04")
    with pytest.raises(AutoresearchValidationError, match="after selection_date"):
        same_session.validate()


def test_full_snapshot_count_is_distinct_from_selected_member_limit() -> None:
    receipt = _universe_receipt()
    dates = tuple(replace(item, selected_member_count=300) for item in receipt.batches[0].dates)
    receipt = replace(
        receipt,
        batches=(replace(receipt.batches[0], dates=dates),),
        member_union_count=300,
    )
    receipt.validate_against_plan(_plan())
    assert receipt.batches[0].dates[0].snapshot.result_count == 5000
    assert receipt.batches[0].dates[0].selected_member_count == 300

    oversized = replace(receipt.batches[0].dates[0], selected_member_count=1001)
    plan = replace(_plan(), max_members_per_date=1000)
    with pytest.raises(AutoresearchValidationError, match="selected_member_count"):
        replace(
            receipt,
            max_members_per_date=1000,
            batches=(replace(receipt.batches[0], dates=(oversized, receipt.batches[0].dates[1])),),
        ).validate_against_plan(plan)


def test_selected_member_counts_cohere_with_compact_union_receipt() -> None:
    receipt = _universe_receipt()
    receipt.validate_against_plan(_plan())

    with pytest.raises(AutoresearchValidationError, match="at least the largest"):
        replace(receipt, member_union_count=9).validate()
    with pytest.raises(AutoresearchValidationError, match="cannot exceed the sum"):
        replace(receipt, member_union_count=21).validate()


def test_per_date_receipt_requires_selected_member_count() -> None:
    raw = _date_receipt("2024-01-02", "2024-01-03").to_dict()
    del raw["selected_member_count"]

    with pytest.raises(AutoresearchValidationError, match="selected_member_count"):
        UniverseDateVerificationReceipt.from_dict(raw)


def test_deterministic_batch_boundaries_follow_plan_limit() -> None:
    dates = tuple(f"2021-01-{day:02d}" for day in range(1, 21))
    plan = replace(_plan(selection_dates=dates), max_members_per_date=600)
    receipts = tuple(
        _date_receipt(date, f"2021-02-{index:02d}") for index, date in enumerate(dates, 1)
    )
    universe = replace(
        _universe_receipt(),
        max_members_per_date=600,
        batches=(
            UniverseHistoryBatchReceipt(_B, 1, receipts[:16]),
            UniverseHistoryBatchReceipt(_C, 1, receipts[16:]),
        ),
    )
    universe.validate_against_plan(plan)

    with pytest.raises(AutoresearchValidationError, match="deterministic contiguous"):
        replace(
            universe,
            batches=(
                UniverseHistoryBatchReceipt(_B, 1, receipts[:15]),
                UniverseHistoryBatchReceipt(_C, 1, receipts[15:]),
            ),
        ).validate_against_plan(plan)


def test_union_digest_algorithm_is_sorted_unique_and_shape_checked() -> None:
    count, digest = canonical_member_union_digest(("smci", "AMD", "AMD"))
    assert count == 2
    assert digest == canonical_member_union_digest(("AMD", "SMCI"))[1]
    assert digest == _CANONICAL_TWO_MEMBER_DIGEST
    assert canonical_member_union_manifest((" smci ", "amd", "AMD")) == b"AMD\nSMCI\n"

    with pytest.raises(AutoresearchValidationError, match="digest_algorithm"):
        replace(_universe_receipt(), member_union_digest_algorithm="unknown").validate()


def test_quantipy_member_union_digest_uses_compact_json_array_fixture() -> None:
    newline_count, newline_digest = canonical_member_union_digest(("smci", "AMD", "AMD"))
    quantipy_count, quantipy_digest = quantipy_member_union_digest(("smci", "AMD", "AMD"))

    assert newline_count == quantipy_count == 2
    assert newline_digest == _CANONICAL_TWO_MEMBER_DIGEST
    assert quantipy_digest == hashlib.sha256(b'["AMD","SMCI"]').hexdigest()
    assert quantipy_digest != newline_digest


def test_external_union_manifest_recomputes_compact_receipt(tmp_path: Path) -> None:
    content = canonical_member_union_manifest(("smci", "AMD", "AMD"))
    manifest = tmp_path / "member-union.txt"
    manifest.write_bytes(content)
    receipt = replace(
        _universe_receipt(),
        member_union_count=2,
        member_union_digest=canonical_member_union_digest(("AMD", "SMCI"))[1],
        member_union_manifest=_manifest_receipt(str(manifest), hashlib.sha256(content).hexdigest()),
    )
    context = AutoresearchValidationContext(
        None,
        _D,
        (date(2021, 1, 5), date(2021, 7, 2)),
    )

    context.validate_universe_receipt(receipt)

    manifest.write_bytes(b"SMCI\nAMD\n")
    with pytest.raises(AutoresearchValidationError, match="SHA-256 mismatch"):
        context.validate_universe_receipt(receipt)


def test_calendar_context_requires_exact_next_xnys_session(tmp_path: Path) -> None:
    content = canonical_member_union_manifest(("AMD", "SMCI"))
    manifest = tmp_path / "member-union.txt"
    manifest.write_bytes(content)
    receipt = replace(
        _universe_receipt(),
        member_union_count=2,
        member_union_digest=canonical_member_union_digest(("AMD", "SMCI"))[1],
        member_union_manifest=_manifest_receipt(str(manifest), hashlib.sha256(content).hexdigest()),
        batches=(
            UniverseHistoryBatchReceipt(
                _B,
                1,
                (_date_receipt("2021-07-02", "2021-07-06"),),
            ),
        ),
    )
    context = AutoresearchValidationContext(
        None,
        _D,
        (date(2021, 7, 2), date(2021, 7, 6), date(2021, 7, 7)),
    )
    context.validate_universe_receipt(receipt)

    weekend = replace(receipt.batches[0].dates[0], earliest_execution_date="2021-07-03")
    with pytest.raises(AutoresearchValidationError, match="first actual XNYS session"):
        context.validate_universe_receipt(
            replace(receipt, batches=(replace(receipt.batches[0], dates=(weekend,)),))
        )

    holiday = replace(receipt.batches[0].dates[0], earliest_execution_date="2021-07-05")
    with pytest.raises(AutoresearchValidationError, match="first actual XNYS session"):
        context.validate_universe_receipt(
            replace(receipt, batches=(replace(receipt.batches[0], dates=(holiday,)),))
        )


def test_snapshot_and_summary_receipts_match_exact_quantipy_shapes() -> None:
    snapshot = _snapshot("2024-01-02", _A).to_dict()
    summary = _summary("2024-01-02", _B).to_dict()
    assert "as_of_date" in snapshot and "adjusted" not in snapshot and "date" not in snapshot
    assert summary["summary_date"] == "2024-01-02" and summary["adjusted"] is False

    legacy = _legacy_source("2024-01-02", "massive", _A)
    with pytest.raises(AutoresearchValidationError, match="exact keys"):
        AuthoritativeSnapshotReceipt.from_dict(legacy)


def test_date_receipt_rejects_mixed_materialization_sources() -> None:
    receipt = _date_receipt("2024-01-02", "2024-01-03")
    with pytest.raises(AutoresearchValidationError, match="sources must match"):
        replace(receipt, summary=replace(receipt.summary, source="other-provider")).validate()


@pytest.mark.parametrize("late_materialization", ("snapshot", "summary"))
def test_hydration_and_dynamic_coverage_bind_union_range_and_time(
    late_materialization: str,
) -> None:
    universe = _universe_receipt()
    hydration = _hydration_receipt()
    coverage = _coverage_receipt()

    hydration.validate_against_universe(universe)
    coverage.validate_against_hydration(hydration, require_complete=True)

    with pytest.raises(AutoresearchValidationError, match="request_digest"):
        replace(hydration, member_union_digest=_D).validate_against_universe(universe)
    with pytest.raises(AutoresearchValidationError, match="missing symbols"):
        replace(coverage, missing_symbol_count=1).validate_against_hydration(
            hydration, require_complete=True
        )
    with pytest.raises(AutoresearchValidationError, match="before folds"):
        replace(hydration, folds_started_at=hydration.completed_at).validate()
    with pytest.raises(AutoresearchValidationError, match="request_digest"):
        replace(hydration, request_digest=_D).validate()

    first_date = universe.batches[0].dates[0]
    late_receipt = replace(
        getattr(first_date, late_materialization),
        completed_at="2026-07-15T12:00:01+00:00",
    )
    late_date = replace(first_date, **{late_materialization: late_receipt})
    reversed_universe = replace(
        universe,
        batches=(
            replace(
                universe.batches[0],
                dates=(late_date, universe.batches[0].dates[1]),
            ),
        ),
    )
    with pytest.raises(AutoresearchValidationError, match="before or at price hydration"):
        hydration.validate_against_universe(reversed_universe)


def _assert_rejects_membership_key(
    parser: Callable[[object], object], raw: dict[str, object]
) -> None:
    raw["tickers"] = ["AMD"]
    with pytest.raises(AutoresearchValidationError, match="exact keys"):
        parser(raw)


def test_compact_receipts_reject_unknown_or_membership_keys() -> None:
    _assert_rejects_membership_key(
        UniverseVerificationReceipt.from_dict, _universe_receipt().to_dict()
    )
    _assert_rejects_membership_key(PriceHydrationReceipt.from_dict, _hydration_receipt().to_dict())
    _assert_rejects_membership_key(
        DynamicUniverseCoverageReceipt.from_dict, _coverage_receipt().to_dict()
    )


def test_price_hydration_receipt_requires_source_price_coverage_response_digest() -> None:
    raw = _hydration_receipt().to_dict()
    raw.pop("source_price_coverage_response_digest")

    with pytest.raises(AutoresearchValidationError, match="source_price_coverage_response_digest"):
        PriceHydrationReceipt.from_dict(raw)


def test_schema_less_pristine_pinned_live_state_is_rejected(tmp_path: Path) -> None:
    raw: dict[str, object] = {
        "consensus_history": [],
        "consensus_retry_count": 0,
        "context_packet": None,
        "debate_rounds": [],
        "final_decision": None,
        "fix_history": [],
        "implementation_result": None,
        "iteration": 1,
        "memory_verification_receipt": None,
        "memory_written": False,
        "mode": None,
        "pending_fix_trigger": None,
        "phase": "setup_context",
        "platform_readiness": {
            "manifest_id": "quantipy-347b2ad558b11da7",
            "receipt_sha256": _MIGRATED_RECEIPT_SHA256,
            "snapshot_id": "snapshot-0c7d24bf18f4d6a0",
        },
        "review_history": [],
        "setup": None,
        "suspended": False,
        "suspension_reason": None,
        "verification_fix_attempts": 0,
        "verification_history": [],
    }
    source = tmp_path / "live-v1.json"
    source.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="missing schema_version"):
        load_state_file(source)


def test_schema_less_live_state_requires_init_before_next_load(tmp_path: Path) -> None:
    raw = AutoresearchState().to_dict()
    del raw["schema_version"]
    state_path = tmp_path / "live.json"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="autoresearch-init-state"):
        load_state_file(state_path)


def test_historical_schema_less_state_requires_new_campaign(
    tmp_path: Path,
) -> None:
    raw = AutoresearchState(iteration=2).to_dict()
    del raw["schema_version"]
    source = tmp_path / "historical.json"
    source.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="autoresearch-init-state"):
        load_state_file(source)
