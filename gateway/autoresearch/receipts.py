"""Autoresearch universe, price, and coverage receipt value objects."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch.constants import (
    MAX_FIXED_SLEEVE_SYMBOLS as MAX_FIXED_SLEEVE_SYMBOLS,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_BATCH_DATES as MAX_UNIVERSE_BATCH_DATES,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_BATCH_RESULTS as MAX_UNIVERSE_BATCH_RESULTS,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_MEMBERS_PER_DATE as MAX_UNIVERSE_MEMBERS_PER_DATE,
)
from gateway.autoresearch.constants import (
    MEMBER_UNION_DIGEST_ALGORITHM as MEMBER_UNION_DIGEST_ALGORITHM,
)
from gateway.autoresearch.constants import (
    NEXT_SESSION_EXECUTION_POLICY as NEXT_SESSION_EXECUTION_POLICY,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _parse_timestamp as _parse_timestamp,
)
from gateway.autoresearch.fields import (
    _require_bool as _require_bool,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_float as _require_float,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_iso_date as _require_iso_date,
)
from gateway.autoresearch.fields import (
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _validate_iso_date_value as _validate_iso_date_value,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.fields import (
    price_hydration_coverage_digest as price_hydration_coverage_digest,
)
from gateway.autoresearch.fields import (
    price_hydration_request_digest as price_hydration_request_digest,
)

if TYPE_CHECKING:
    from gateway.autoresearch.artifacts import UniversePlanArtifact as UniversePlanArtifact


def _validate_coverage_values(receipt: CoverageReceipt, *, label: str) -> None:
    if not (
        receipt.declared_intended_start
        <= receipt.actual_common_start
        <= receipt.actual_common_end
        <= receipt.declared_intended_end
    ):
        raise AutoresearchValidationError(f"{label} actual common range must fit intended range")
    if not (
        receipt.actual_common_start
        <= receipt.oos_start
        <= receipt.oos_end
        <= receipt.actual_common_end
    ):
        raise AutoresearchValidationError(f"{label} OOS range must fit actual common range")
    if (
        receipt.expected_trading_days <= 0
        or not 0 <= receipt.actual_trading_days <= receipt.expected_trading_days
    ):
        raise AutoresearchValidationError(f"{label} trading day counts are invalid")
    if not 0.0 <= receipt.coverage_percent <= 100.0:
        raise AutoresearchValidationError(f"{label} coverage_percent must be between 0 and 100")
    expected_percent = receipt.actual_trading_days / receipt.expected_trading_days * 100.0
    if abs(receipt.coverage_percent - expected_percent) > 0.01:
        raise AutoresearchValidationError(f"{label} coverage_percent must match trading day counts")
    if receipt.actual_trading_days < receipt.expected_trading_days and not receipt.missing_reason:
        raise AutoresearchValidationError(
            f"{label} missing_reason is required for missing trading days"
        )
    if receipt.actual_trading_days == receipt.expected_trading_days and receipt.missing_reason:
        raise AutoresearchValidationError(
            f"{label} missing_reason is only valid for missing trading days"
        )
    if receipt.default_fold_count < 0 or receipt.fallback_fold_count < 0:
        raise AutoresearchValidationError(f"{label} fold counts must be non-negative")
    if receipt.fixed_sleeve_local_data and receipt.cap_provenance_available:
        raise AutoresearchValidationError(
            f"{label} fixed_sleeve_local_data cannot claim cap_provenance_available"
        )


@dataclass(frozen=True, slots=True)
class MemberUnionManifestReceipt:
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, raw: object) -> MemberUnionManifestReceipt:
        data = _ensure_mapping(raw, label="member_union_manifest_receipt")
        _require_exact_keys(
            data,
            label="member_union_manifest_receipt",
            expected=("path", "sha256"),
        )
        receipt = cls(path=_require_str(data, "path"), sha256=_require_sha256(data, "sha256"))
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if not Path(self.path).is_absolute():
            raise AutoresearchValidationError("member union manifest path must be absolute")
        _validate_sha256(self.sha256, label="member union manifest sha256")

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class AuthoritativeSnapshotReceipt:
    as_of_date: str
    source: str
    result_count: int
    identity_digest: str
    content_digest: str
    completed_at: str

    @classmethod
    def from_dict(cls, raw: object) -> AuthoritativeSnapshotReceipt:
        data = _ensure_mapping(raw, label="authoritative_snapshot_receipt")
        _require_exact_keys(
            data,
            label="authoritative_snapshot_receipt",
            expected=(
                "as_of_date",
                "source",
                "result_count",
                "identity_digest",
                "content_digest",
                "completed_at",
            ),
        )
        receipt = cls(
            as_of_date=_require_iso_date(data, "as_of_date"),
            source=_require_str(data, "source"),
            result_count=_require_int(data, "result_count"),
            identity_digest=_require_sha256(data, "identity_digest"),
            content_digest=_require_sha256(data, "content_digest"),
            completed_at=_require_str(data, "completed_at"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.as_of_date, label="as_of_date")
        _validate_sha256(self.identity_digest, label="identity_digest")
        _validate_sha256(self.content_digest, label="content_digest")
        _parse_timestamp(self.completed_at, label="completed_at")
        if self.result_count < 0:
            raise AutoresearchValidationError("snapshot result_count must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date,
            "source": self.source,
            "result_count": self.result_count,
            "identity_digest": self.identity_digest,
            "content_digest": self.content_digest,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class GroupedSummaryReceipt:
    summary_date: str
    source: str
    result_count: int
    identity_digest: str
    content_digest: str
    completed_at: str
    adjusted: bool

    @classmethod
    def from_dict(cls, raw: object) -> GroupedSummaryReceipt:
        data = _ensure_mapping(raw, label="grouped_summary_receipt")
        _require_exact_keys(
            data,
            label="grouped_summary_receipt",
            expected=(
                "summary_date",
                "source",
                "result_count",
                "identity_digest",
                "content_digest",
                "completed_at",
                "adjusted",
            ),
        )
        receipt = cls(
            summary_date=_require_iso_date(data, "summary_date"),
            source=_require_str(data, "source"),
            result_count=_require_int(data, "result_count"),
            identity_digest=_require_sha256(data, "identity_digest"),
            content_digest=_require_sha256(data, "content_digest"),
            completed_at=_require_str(data, "completed_at"),
            adjusted=_require_bool(data, "adjusted"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.summary_date, label="summary_date")
        _validate_sha256(self.identity_digest, label="identity_digest")
        _validate_sha256(self.content_digest, label="content_digest")
        _parse_timestamp(self.completed_at, label="completed_at")
        if self.result_count < 0:
            raise AutoresearchValidationError("summary result_count must be non-negative")
        if self.adjusted:
            raise AutoresearchValidationError("grouped summary receipt requires adjusted=false")

    def to_dict(self) -> dict[str, object]:
        return {
            "summary_date": self.summary_date,
            "source": self.source,
            "result_count": self.result_count,
            "identity_digest": self.identity_digest,
            "content_digest": self.content_digest,
            "completed_at": self.completed_at,
            "adjusted": self.adjusted,
        }


@dataclass(frozen=True, slots=True)
class UniverseDateVerificationReceipt:
    selection_date: str
    earliest_execution_date: str
    calendar_identity: str
    calendar_digest: str
    selected_member_count: int
    snapshot: AuthoritativeSnapshotReceipt
    summary: GroupedSummaryReceipt

    @classmethod
    def from_dict(cls, raw: object) -> UniverseDateVerificationReceipt:
        data = _ensure_mapping(raw, label="universe_date_verification_receipt")
        _require_exact_keys(
            data,
            label="universe_date_verification_receipt",
            expected=(
                "selection_date",
                "earliest_execution_date",
                "calendar_identity",
                "calendar_digest",
                "selected_member_count",
                "snapshot",
                "summary",
            ),
        )
        receipt = cls(
            selection_date=_require_iso_date(data, "selection_date"),
            earliest_execution_date=_require_iso_date(data, "earliest_execution_date"),
            calendar_identity=_require_str(data, "calendar_identity"),
            calendar_digest=_require_sha256(data, "calendar_digest"),
            selected_member_count=_require_int(data, "selected_member_count"),
            snapshot=AuthoritativeSnapshotReceipt.from_dict(data["snapshot"]),
            summary=GroupedSummaryReceipt.from_dict(data["summary"]),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.selection_date, label="selection_date")
        _validate_iso_date_value(self.earliest_execution_date, label="earliest_execution_date")
        _validate_sha256(self.calendar_digest, label="calendar_digest")
        if self.selected_member_count < 0:
            raise AutoresearchValidationError("selected_member_count must be non-negative")
        if self.earliest_execution_date <= self.selection_date:
            raise AutoresearchValidationError(
                "earliest_execution_date must be after selection_date"
            )
        self.snapshot.validate()
        self.summary.validate()
        if self.snapshot.as_of_date != self.selection_date:
            raise AutoresearchValidationError(
                "snapshot receipt as_of_date must match selection_date"
            )
        if self.summary.summary_date != self.selection_date:
            raise AutoresearchValidationError(
                "summary receipt summary_date must match selection_date"
            )
        if self.snapshot.source != self.summary.source:
            raise AutoresearchValidationError(
                "snapshot and summary sources must match for each selection date"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "selection_date": self.selection_date,
            "earliest_execution_date": self.earliest_execution_date,
            "calendar_identity": self.calendar_identity,
            "calendar_digest": self.calendar_digest,
            "selected_member_count": self.selected_member_count,
            "snapshot": self.snapshot.to_dict(),
            "summary": self.summary.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UniverseHistoryBatchReceipt:
    contract_digest: str
    operation_count: int
    dates: tuple[UniverseDateVerificationReceipt, ...]

    @classmethod
    def from_dict(cls, raw: object) -> UniverseHistoryBatchReceipt:
        data = _ensure_mapping(raw, label="universe_history_batch_receipt")
        _require_exact_keys(
            data,
            label="universe_history_batch_receipt",
            expected=("contract_digest", "operation_count", "dates"),
        )
        dates_raw = data["dates"]
        if not isinstance(dates_raw, Sequence) or isinstance(dates_raw, str | bytes):
            raise AutoresearchValidationError("batch dates must be a list")
        receipt = cls(
            contract_digest=_require_sha256(data, "contract_digest"),
            operation_count=_require_int(data, "operation_count"),
            dates=tuple(UniverseDateVerificationReceipt.from_dict(item) for item in dates_raw),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.contract_digest, label="contract_digest")
        if self.operation_count != 1:
            raise AutoresearchValidationError(
                "each universe history batch requires operation_count=1"
            )
        if not 1 <= len(self.dates) <= MAX_UNIVERSE_BATCH_DATES:
            raise AutoresearchValidationError("universe history batch requires 1 to 32 dates")
        for receipt in self.dates:
            receipt.validate()
        dates = tuple(receipt.selection_date for receipt in self.dates)
        if tuple(sorted(set(dates))) != dates:
            raise AutoresearchValidationError(
                "universe history batch dates must be sorted and unique"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_digest": self.contract_digest,
            "operation_count": self.operation_count,
            "dates": [receipt.to_dict() for receipt in self.dates],
        }


@dataclass(frozen=True, slots=True)
class UniverseVerificationReceipt:
    profile_id: str
    profile_digest: str
    execution_policy: str
    max_members_per_date: int
    batches: tuple[UniverseHistoryBatchReceipt, ...]
    member_union_digest_algorithm: str
    member_union_count: int
    member_union_digest: str
    member_union_manifest: MemberUnionManifestReceipt

    @classmethod
    def from_dict(cls, raw: object) -> UniverseVerificationReceipt:
        data = _ensure_mapping(raw, label="universe_verification_receipt")
        _require_exact_keys(
            data,
            label="universe_verification_receipt",
            expected=(
                "profile_id",
                "profile_digest",
                "execution_policy",
                "max_members_per_date",
                "batches",
                "member_union_digest_algorithm",
                "member_union_count",
                "member_union_digest",
                "member_union_manifest",
            ),
        )
        batches_raw = data["batches"]
        if not isinstance(batches_raw, Sequence) or isinstance(batches_raw, str | bytes):
            raise AutoresearchValidationError("batches must be a list")
        receipt = cls(
            profile_id=_require_str(data, "profile_id"),
            profile_digest=_require_sha256(data, "profile_digest"),
            execution_policy=_require_str(data, "execution_policy"),
            max_members_per_date=_require_int(data, "max_members_per_date"),
            batches=tuple(UniverseHistoryBatchReceipt.from_dict(item) for item in batches_raw),
            member_union_digest_algorithm=_require_str(data, "member_union_digest_algorithm"),
            member_union_count=_require_int(data, "member_union_count"),
            member_union_digest=_require_sha256(data, "member_union_digest"),
            member_union_manifest=MemberUnionManifestReceipt.from_dict(
                data["member_union_manifest"]
            ),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.profile_digest, label="profile_digest")
        _validate_sha256(self.member_union_digest, label="member_union_digest")
        self.member_union_manifest.validate()
        if not self.batches:
            raise AutoresearchValidationError("universe verification requires history batches")
        for batch in self.batches:
            batch.validate()
        if self.execution_policy != NEXT_SESSION_EXECUTION_POLICY:
            raise AutoresearchValidationError(
                f"universe execution_policy must be {NEXT_SESSION_EXECUTION_POLICY}"
            )
        if self.member_union_count <= 0:
            raise AutoresearchValidationError("member_union_count must be positive")
        if self.member_union_digest_algorithm != MEMBER_UNION_DIGEST_ALGORITHM:
            raise AutoresearchValidationError(
                f"member_union_digest_algorithm must be {MEMBER_UNION_DIGEST_ALGORITHM}"
            )
        if not 1 <= self.max_members_per_date <= MAX_UNIVERSE_MEMBERS_PER_DATE:
            raise AutoresearchValidationError(
                "universe verification max_members_per_date must be between 1 and 1000"
            )
        selected_member_counts = tuple(
            item.selected_member_count for batch in self.batches for item in batch.dates
        )
        if self.member_union_count < max(selected_member_counts):
            raise AutoresearchValidationError(
                "member_union_count must be at least the largest selected_member_count"
            )
        if self.member_union_count > sum(selected_member_counts):
            raise AutoresearchValidationError(
                "member_union_count cannot exceed the sum of selected_member_count values"
            )

    def validate_against_plan(self, plan: UniversePlanArtifact) -> None:
        self.validate()
        plan.validate()
        for field_name in (
            "profile_id",
            "profile_digest",
            "execution_policy",
            "max_members_per_date",
        ):
            if getattr(self, field_name) != getattr(plan, field_name):
                raise AutoresearchValidationError(
                    f"universe verification {field_name} must match universe plan"
                )
        flattened = tuple(item.selection_date for batch in self.batches for item in batch.dates)
        if flattened != plan.selection_dates:
            raise AutoresearchValidationError(
                "universe verification batches must exactly cover plan dates "
                "without gaps or overlap"
            )
        max_batch_dates = min(
            MAX_UNIVERSE_BATCH_DATES,
            MAX_UNIVERSE_BATCH_RESULTS // plan.max_members_per_date,
        )
        if max_batch_dates < 1:
            raise AutoresearchValidationError("max_members_per_date cannot fit one history batch")
        expected_batches = tuple(
            plan.selection_dates[index : index + max_batch_dates]
            for index in range(0, len(plan.selection_dates), max_batch_dates)
        )
        actual_batches = tuple(
            tuple(item.selection_date for item in batch.dates) for batch in self.batches
        )
        if actual_batches != expected_batches:
            raise AutoresearchValidationError(
                "universe history batches must use deterministic contiguous canonical chunks"
            )
        if any(
            len(batch.dates) * plan.max_members_per_date > MAX_UNIVERSE_BATCH_RESULTS
            for batch in self.batches
        ):
            raise AutoresearchValidationError("universe history batch exceeds 10000 results")
        date_receipts = tuple(item for batch in self.batches for item in batch.dates)
        calendar_bindings = {
            (item.calendar_identity, item.calendar_digest) for item in date_receipts
        }
        if len(calendar_bindings) != 1:
            raise AutoresearchValidationError(
                "all execution dates must bind the same XNYS calendar receipt"
            )
        for item in date_receipts:
            if item.selected_member_count > plan.max_members_per_date:
                raise AutoresearchValidationError(
                    "selected_member_count exceeds plan max_members_per_date"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "execution_policy": self.execution_policy,
            "max_members_per_date": self.max_members_per_date,
            "batches": [receipt.to_dict() for receipt in self.batches],
            "member_union_digest_algorithm": self.member_union_digest_algorithm,
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "member_union_manifest": self.member_union_manifest.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PriceHydrationReceipt:
    member_union_count: int
    member_union_digest: str
    experiment_start: str
    experiment_end: str
    timeframe: str
    market_hours: str
    operation_count: int
    request_digest: str
    coverage_receipt_digest: str
    source_price_coverage_response_digest: str
    completed_at: str
    folds_started_at: str

    def request_identity(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
        }

    def coverage_identity(self) -> dict[str, object]:
        return {
            "request_digest": self.request_digest,
            "operation_count": self.operation_count,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> PriceHydrationReceipt:
        data = _ensure_mapping(raw, label="price_hydration_receipt")
        _require_exact_keys(
            data,
            label="price_hydration_receipt",
            expected=(
                "member_union_count",
                "member_union_digest",
                "experiment_start",
                "experiment_end",
                "timeframe",
                "market_hours",
                "operation_count",
                "request_digest",
                "coverage_receipt_digest",
                "source_price_coverage_response_digest",
                "completed_at",
                "folds_started_at",
            ),
        )
        receipt = cls(
            member_union_count=_require_int(data, "member_union_count"),
            member_union_digest=_require_sha256(data, "member_union_digest"),
            experiment_start=_require_iso_date(data, "experiment_start"),
            experiment_end=_require_iso_date(data, "experiment_end"),
            timeframe=_require_str(data, "timeframe"),
            market_hours=_require_str(data, "market_hours"),
            operation_count=_require_int(data, "operation_count"),
            request_digest=_require_sha256(data, "request_digest"),
            coverage_receipt_digest=_require_sha256(data, "coverage_receipt_digest"),
            source_price_coverage_response_digest=_require_sha256(
                data, "source_price_coverage_response_digest"
            ),
            completed_at=_require_str(data, "completed_at"),
            folds_started_at=_require_str(data, "folds_started_at"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.member_union_digest, label="member_union_digest")
        _validate_iso_date_value(self.experiment_start, label="experiment_start")
        _validate_iso_date_value(self.experiment_end, label="experiment_end")
        if self.member_union_count <= 0:
            raise AutoresearchValidationError("member_union_count must be positive")
        if self.experiment_start > self.experiment_end:
            raise AutoresearchValidationError("price hydration experiment range is invalid")
        if self.operation_count != 1:
            raise AutoresearchValidationError("price hydration requires operation_count=1")
        if self.request_digest != price_hydration_request_digest(
            member_union_count=self.member_union_count,
            member_union_digest=self.member_union_digest,
            experiment_start=self.experiment_start,
            experiment_end=self.experiment_end,
            timeframe=self.timeframe,
            market_hours=self.market_hours,
        ):
            raise AutoresearchValidationError("price hydration request_digest is not canonical")
        if self.coverage_receipt_digest != price_hydration_coverage_digest(
            request_digest=self.request_digest,
            operation_count=self.operation_count,
            completed_at=self.completed_at,
        ):
            raise AutoresearchValidationError(
                "price hydration coverage_receipt_digest is not canonical"
            )
        _validate_sha256(
            self.source_price_coverage_response_digest,
            label="source_price_coverage_response_digest",
        )
        completed_at = _parse_timestamp(self.completed_at, label="completed_at")
        folds_started_at = _parse_timestamp(self.folds_started_at, label="folds_started_at")
        if completed_at >= folds_started_at:
            raise AutoresearchValidationError("price hydration must complete before folds start")

    def validate_against_universe(self, universe: UniverseVerificationReceipt) -> None:
        self.validate()
        universe.validate()
        if (
            self.member_union_count != universe.member_union_count
            or self.member_union_digest != universe.member_union_digest
        ):
            raise AutoresearchValidationError(
                "price hydration member union must match universe verification"
            )
        hydration_completed_at = _parse_timestamp(self.completed_at, label="completed_at")
        for date_receipt in (item for batch in universe.batches for item in batch.dates):
            for label, completed_at in (
                ("snapshot", date_receipt.snapshot.completed_at),
                ("summary", date_receipt.summary.completed_at),
            ):
                materialization_completed_at = _parse_timestamp(
                    completed_at,
                    label=f"{label} completed_at",
                )
                if materialization_completed_at > hydration_completed_at:
                    raise AutoresearchValidationError(
                        f"{label} materialization must complete before or at price hydration"
                    )

    def to_dict(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
            "operation_count": self.operation_count,
            "request_digest": self.request_digest,
            "coverage_receipt_digest": self.coverage_receipt_digest,
            "source_price_coverage_response_digest": self.source_price_coverage_response_digest,
            "completed_at": self.completed_at,
            "folds_started_at": self.folds_started_at,
        }


@dataclass(frozen=True, slots=True)
class DynamicUniverseCoverageReceipt:
    member_union_count: int
    member_union_digest: str
    experiment_start: str
    experiment_end: str
    oos_start: str
    oos_end: str
    timeframe: str
    market_hours: str
    expected_symbol_sessions: int
    covered_symbol_sessions: int
    missing_symbol_count: int
    missing_symbol_sessions: int
    default_fold_count: int
    fallback_fold_count: int

    @classmethod
    def from_dict(cls, raw: object) -> DynamicUniverseCoverageReceipt:
        data = _ensure_mapping(raw, label="dynamic_universe_coverage_receipt")
        _require_exact_keys(
            data,
            label="dynamic_universe_coverage_receipt",
            expected=(
                "member_union_count",
                "member_union_digest",
                "experiment_start",
                "experiment_end",
                "oos_start",
                "oos_end",
                "timeframe",
                "market_hours",
                "expected_symbol_sessions",
                "covered_symbol_sessions",
                "missing_symbol_count",
                "missing_symbol_sessions",
                "default_fold_count",
                "fallback_fold_count",
            ),
        )
        receipt = cls(
            member_union_count=_require_int(data, "member_union_count"),
            member_union_digest=_require_sha256(data, "member_union_digest"),
            experiment_start=_require_iso_date(data, "experiment_start"),
            experiment_end=_require_iso_date(data, "experiment_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            timeframe=_require_str(data, "timeframe"),
            market_hours=_require_str(data, "market_hours"),
            expected_symbol_sessions=_require_int(data, "expected_symbol_sessions"),
            covered_symbol_sessions=_require_int(data, "covered_symbol_sessions"),
            missing_symbol_count=_require_int(data, "missing_symbol_count"),
            missing_symbol_sessions=_require_int(data, "missing_symbol_sessions"),
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.member_union_digest, label="member_union_digest")
        _validate_iso_date_value(self.experiment_start, label="experiment_start")
        _validate_iso_date_value(self.experiment_end, label="experiment_end")
        if self.member_union_count <= 0 or self.experiment_start > self.experiment_end:
            raise AutoresearchValidationError("dynamic universe coverage identity is invalid")
        if not self.experiment_start <= self.oos_start <= self.oos_end <= self.experiment_end:
            raise AutoresearchValidationError(
                "dynamic universe OOS range must fit experiment range"
            )
        if not 0 <= self.covered_symbol_sessions <= self.expected_symbol_sessions:
            raise AutoresearchValidationError("dynamic universe symbol-session counts are invalid")
        if self.expected_symbol_sessions <= 0:
            raise AutoresearchValidationError("expected_symbol_sessions must be positive")
        if (
            min(
                self.missing_symbol_count,
                self.missing_symbol_sessions,
                self.default_fold_count,
                self.fallback_fold_count,
            )
            < 0
        ):
            raise AutoresearchValidationError("dynamic universe counts must be non-negative")

    def validate_against_hydration(
        self, hydration: PriceHydrationReceipt, *, require_complete: bool
    ) -> None:
        self.validate()
        hydration.validate()
        for field_name in (
            "member_union_count",
            "member_union_digest",
            "experiment_start",
            "experiment_end",
            "timeframe",
            "market_hours",
        ):
            if getattr(self, field_name) != getattr(hydration, field_name):
                raise AutoresearchValidationError(
                    f"dynamic coverage {field_name} must match price hydration"
                )
        if require_complete and (
            self.missing_symbol_count != 0
            or self.missing_symbol_sessions != 0
            or self.covered_symbol_sessions != self.expected_symbol_sessions
        ):
            raise AutoresearchValidationError(
                "PASS dynamic coverage requires zero missing symbols and symbol sessions"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
            "expected_symbol_sessions": self.expected_symbol_sessions,
            "covered_symbol_sessions": self.covered_symbol_sessions,
            "missing_symbol_count": self.missing_symbol_count,
            "missing_symbol_sessions": self.missing_symbol_sessions,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
        }


@dataclass(frozen=True, slots=True)
class CoverageReceipt:
    symbol: str
    declared_intended_start: str
    declared_intended_end: str
    actual_common_start: str
    actual_common_end: str
    oos_start: str
    oos_end: str
    expected_trading_days: int
    actual_trading_days: int
    coverage_percent: float
    missing_reason: str | None
    default_fold_count: int
    fallback_fold_count: int
    cap_provenance_available: bool
    fixed_sleeve_local_data: bool

    @classmethod
    def from_dict(cls, raw: object) -> CoverageReceipt:
        data = _ensure_mapping(raw, label="coverage_receipt")
        _require_exact_keys(
            data,
            label="coverage_receipt",
            expected=(
                "symbol",
                "declared_intended_start",
                "declared_intended_end",
                "actual_common_start",
                "actual_common_end",
                "oos_start",
                "oos_end",
                "expected_trading_days",
                "actual_trading_days",
                "coverage_percent",
                "missing_reason",
                "default_fold_count",
                "fallback_fold_count",
                "cap_provenance_available",
                "fixed_sleeve_local_data",
            ),
        )
        missing_reason = data.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            raise AutoresearchValidationError("missing_reason must be a string or null")
        receipt = cls(
            symbol=_require_str(data, "symbol"),
            declared_intended_start=_require_iso_date(data, "declared_intended_start"),
            declared_intended_end=_require_iso_date(data, "declared_intended_end"),
            actual_common_start=_require_iso_date(data, "actual_common_start"),
            actual_common_end=_require_iso_date(data, "actual_common_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            expected_trading_days=_require_int(data, "expected_trading_days"),
            actual_trading_days=_require_int(data, "actual_trading_days"),
            coverage_percent=_require_float(data, "coverage_percent"),
            missing_reason=missing_reason.strip() if isinstance(missing_reason, str) else None,
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
            cap_provenance_available=_require_bool(data, "cap_provenance_available"),
            fixed_sleeve_local_data=_require_bool(data, "fixed_sleeve_local_data"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_coverage_values(self, label=f"coverage receipt for {self.symbol}")

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "declared_intended_start": self.declared_intended_start,
            "declared_intended_end": self.declared_intended_end,
            "actual_common_start": self.actual_common_start,
            "actual_common_end": self.actual_common_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "expected_trading_days": self.expected_trading_days,
            "actual_trading_days": self.actual_trading_days,
            "coverage_percent": self.coverage_percent,
            "missing_reason": self.missing_reason,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
            "cap_provenance_available": self.cap_provenance_available,
            "fixed_sleeve_local_data": self.fixed_sleeve_local_data,
        }


@dataclass(frozen=True, slots=True)
class AggregateCoverageReceipt:
    declared_intended_start: str
    declared_intended_end: str
    actual_common_start: str
    actual_common_end: str
    oos_start: str
    oos_end: str
    expected_trading_days: int
    actual_trading_days: int
    coverage_percent: float
    missing_reason: str | None
    default_fold_count: int
    fallback_fold_count: int
    cap_provenance_available: bool
    fixed_sleeve_local_data: bool
    per_symbol: tuple[CoverageReceipt, ...]

    @classmethod
    def from_dict(cls, raw: object) -> AggregateCoverageReceipt:
        data = _ensure_mapping(raw, label="aggregate_coverage_receipt")
        _require_exact_keys(
            data,
            label="aggregate_coverage_receipt",
            expected=(
                "declared_intended_start",
                "declared_intended_end",
                "actual_common_start",
                "actual_common_end",
                "oos_start",
                "oos_end",
                "expected_trading_days",
                "actual_trading_days",
                "coverage_percent",
                "missing_reason",
                "default_fold_count",
                "fallback_fold_count",
                "cap_provenance_available",
                "fixed_sleeve_local_data",
                "per_symbol",
            ),
        )
        symbols_raw = data.get("per_symbol")
        if not isinstance(symbols_raw, Sequence) or isinstance(symbols_raw, str | bytes):
            raise AutoresearchValidationError("per_symbol must be a list")
        missing_reason = data.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            raise AutoresearchValidationError("missing_reason must be a string or null")
        receipt = cls(
            declared_intended_start=_require_iso_date(data, "declared_intended_start"),
            declared_intended_end=_require_iso_date(data, "declared_intended_end"),
            actual_common_start=_require_iso_date(data, "actual_common_start"),
            actual_common_end=_require_iso_date(data, "actual_common_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            expected_trading_days=_require_int(data, "expected_trading_days"),
            actual_trading_days=_require_int(data, "actual_trading_days"),
            coverage_percent=_require_float(data, "coverage_percent"),
            missing_reason=missing_reason.strip() if isinstance(missing_reason, str) else None,
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
            cap_provenance_available=_require_bool(data, "cap_provenance_available"),
            fixed_sleeve_local_data=_require_bool(data, "fixed_sleeve_local_data"),
            per_symbol=tuple(CoverageReceipt.from_dict(item) for item in symbols_raw),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if not self.per_symbol:
            raise AutoresearchValidationError(
                "aggregate coverage requires at least one per-symbol receipt"
            )
        if len(self.per_symbol) > MAX_FIXED_SLEEVE_SYMBOLS:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 coverage allows at most 32 per-symbol receipts"
            )
        synthetic = CoverageReceipt(
            symbol="aggregate",
            declared_intended_start=self.declared_intended_start,
            declared_intended_end=self.declared_intended_end,
            actual_common_start=self.actual_common_start,
            actual_common_end=self.actual_common_end,
            oos_start=self.oos_start,
            oos_end=self.oos_end,
            expected_trading_days=self.expected_trading_days,
            actual_trading_days=self.actual_trading_days,
            coverage_percent=self.coverage_percent,
            missing_reason=self.missing_reason,
            default_fold_count=self.default_fold_count,
            fallback_fold_count=self.fallback_fold_count,
            cap_provenance_available=self.cap_provenance_available,
            fixed_sleeve_local_data=self.fixed_sleeve_local_data,
        )
        _validate_coverage_values(synthetic, label="aggregate coverage")

        if len({receipt.symbol for receipt in self.per_symbol}) != len(self.per_symbol):
            raise AutoresearchValidationError("aggregate coverage cannot contain duplicate symbols")
        for receipt in self.per_symbol:
            receipt.validate()
            if (
                receipt.declared_intended_start != self.declared_intended_start
                or receipt.declared_intended_end != self.declared_intended_end
            ):
                raise AutoresearchValidationError(
                    "aggregate declared intended range must match every per-symbol receipt"
                )
            if receipt.fixed_sleeve_local_data != self.fixed_sleeve_local_data:
                raise AutoresearchValidationError(
                    "per-symbol fixed_sleeve_local_data must match aggregate"
                )
            if receipt.cap_provenance_available != self.cap_provenance_available:
                raise AutoresearchValidationError("per-symbol cap provenance must match aggregate")
        expected_common_start = max(receipt.actual_common_start for receipt in self.per_symbol)
        expected_common_end = min(receipt.actual_common_end for receipt in self.per_symbol)
        if self.actual_common_start != expected_common_start:
            raise AutoresearchValidationError(
                "aggregate actual_common_start must equal the latest per-symbol actual start"
            )
        if self.actual_common_end != expected_common_end:
            raise AutoresearchValidationError(
                "aggregate actual_common_end must equal the earliest per-symbol actual end"
            )
        expected_oos_start = max(receipt.oos_start for receipt in self.per_symbol)
        expected_oos_end = min(receipt.oos_end for receipt in self.per_symbol)
        if self.oos_start != expected_oos_start or self.oos_end != expected_oos_end:
            raise AutoresearchValidationError(
                "aggregate OOS range must equal the common per-symbol OOS intersection"
            )
        if any(
            receipt.expected_trading_days != self.expected_trading_days
            for receipt in self.per_symbol
        ):
            raise AutoresearchValidationError(
                "aggregate expected_trading_days must match every per-symbol common calendar"
            )
        if any(
            receipt.actual_trading_days != self.actual_trading_days for receipt in self.per_symbol
        ):
            raise AutoresearchValidationError(
                "aggregate actual_trading_days must match every per-symbol common calendar"
            )
        if any(receipt.coverage_percent != self.coverage_percent for receipt in self.per_symbol):
            raise AutoresearchValidationError(
                "aggregate coverage_percent must match every per-symbol common calendar"
            )
        if any(receipt.missing_reason != self.missing_reason for receipt in self.per_symbol):
            raise AutoresearchValidationError(
                "aggregate missing_reason must match every per-symbol common calendar"
            )
        expected_default_folds = min(receipt.default_fold_count for receipt in self.per_symbol)
        expected_fallback_folds = min(receipt.fallback_fold_count for receipt in self.per_symbol)
        if self.default_fold_count != expected_default_folds:
            raise AutoresearchValidationError(
                "aggregate default_fold_count must equal the fewest per-symbol default folds"
            )
        if self.fallback_fold_count != expected_fallback_folds:
            raise AutoresearchValidationError(
                "aggregate fallback_fold_count must equal the fewest per-symbol fallback folds"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "declared_intended_start": self.declared_intended_start,
            "declared_intended_end": self.declared_intended_end,
            "actual_common_start": self.actual_common_start,
            "actual_common_end": self.actual_common_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "expected_trading_days": self.expected_trading_days,
            "actual_trading_days": self.actual_trading_days,
            "coverage_percent": self.coverage_percent,
            "missing_reason": self.missing_reason,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
            "cap_provenance_available": self.cap_provenance_available,
            "fixed_sleeve_local_data": self.fixed_sleeve_local_data,
            "per_symbol": [receipt.to_dict() for receipt in self.per_symbol],
        }
