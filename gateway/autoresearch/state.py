"""Autoresearch state value objects."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, TypeVar

from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact as ConsensusResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ContextPacketArtifact as ContextPacketArtifact,
)
from gateway.autoresearch.artifacts import (
    DebateResultArtifact as DebateResultArtifact,
)
from gateway.autoresearch.artifacts import (
    FinalDecisionArtifact as FinalDecisionArtifact,
)
from gateway.autoresearch.artifacts import (
    FixResultArtifact as FixResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ImplementationResultArtifact as ImplementationResultArtifact,
)
from gateway.autoresearch.artifacts import (
    MemoryVerificationReceipt as MemoryVerificationReceipt,
)
from gateway.autoresearch.artifacts import (
    ReviewResultArtifact as ReviewResultArtifact,
)
from gateway.autoresearch.artifacts import (
    SetupContextArtifact as SetupContextArtifact,
)
from gateway.autoresearch.artifacts import (
    VerificationResultArtifact as VerificationResultArtifact,
)
from gateway.autoresearch.constants import (
    AUTORESEARCH_STATE_SCHEMA_VERSION as AUTORESEARCH_STATE_SCHEMA_VERSION,
)
from gateway.autoresearch.constants import (
    XNYS_CALENDAR_IDENTITY as XNYS_CALENDAR_IDENTITY,
)
from gateway.autoresearch.enums import (
    FixTriggerPhase as FixTriggerPhase,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _require_bool as _require_bool,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.recovery_receipts import (
    CanonicalQuantipyRuntimeAttestation as CanonicalQuantipyRuntimeAttestation,
)
from gateway.autoresearch.recovery_receipts import (
    ExternalVerificationRetryReceipt as ExternalVerificationRetryReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    InterruptedVerificationAttemptReceipt as InterruptedVerificationAttemptReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    PlatformRuntimeRecoveryReceipt as PlatformRuntimeRecoveryReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    _verify_member_union_manifest as _verify_member_union_manifest,
)
from gateway.autoresearch_readiness import (
    ReadinessIdentity as ReadinessIdentity,
)
from gateway.autoresearch_readiness import (
    load_xnys_calendar_evidence as load_xnys_calendar_evidence,
)

if TYPE_CHECKING:
    from gateway.autoresearch.receipts import (
        UniverseVerificationReceipt as UniverseVerificationReceipt,
    )
    from gateway.autoresearch_readiness import (
        PlatformReadinessManifest as PlatformReadinessManifest,
    )

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class AutoresearchValidationContext:
    readiness_identity: ReadinessIdentity | None
    xnys_evidence_digest: str
    xnys_sessions: tuple[date, ...]
    xnys_range_start: date | None = None
    xnys_range_end: date | None = None
    quantipy_commit: str | None = None

    @classmethod
    def from_readiness(cls, readiness: PlatformReadinessManifest) -> AutoresearchValidationContext:
        try:
            digest, evidence = load_xnys_calendar_evidence(readiness)
            identity = readiness.require_ready()
        except ValueError as exc:
            raise AutoresearchValidationError(str(exc)) from exc
        quantipy_commit = identity.quantipy_commit
        if quantipy_commit is None:
            raise AutoresearchValidationError(
                "READY platform identity requires the pinned Quantipy commit"
            )
        sessions = evidence.sessions
        if not sessions:
            raise AutoresearchValidationError("XNYS calendar evidence contains no sessions")
        return cls(
            identity,
            digest,
            sessions,
            quantipy_commit=quantipy_commit,
            xnys_range_start=evidence.range_start,
            xnys_range_end=evidence.range_end,
        )

    def validate_for_state(self, state: AutoresearchState) -> None:
        state_identity = state.platform_readiness
        context_identity = self.readiness_identity
        if state_identity != context_identity:
            raise AutoresearchValidationError(
                "validation context readiness identity must match pinned state readiness"
            )

    def validate_universe_receipt(self, receipt: UniverseVerificationReceipt) -> None:
        _verify_member_union_manifest(receipt)
        for item in (date_receipt for batch in receipt.batches for date_receipt in batch.dates):
            if item.calendar_identity != XNYS_CALENDAR_IDENTITY:
                raise AutoresearchValidationError("calendar_identity must be XNYS")
            if item.calendar_digest != self.xnys_evidence_digest:
                raise AutoresearchValidationError(
                    "calendar_digest must exactly match pinned readiness XNYS evidence"
                )
            selection_date = date.fromisoformat(item.selection_date)
            index = bisect_right(self.xnys_sessions, selection_date)
            if index == len(self.xnys_sessions):
                raise AutoresearchValidationError(
                    "XNYS evidence does not cover a session after selection_date"
                )
            expected = self.xnys_sessions[index].isoformat()
            if item.earliest_execution_date != expected:
                raise AutoresearchValidationError(
                    "earliest_execution_date must equal the first actual XNYS session "
                    f"after selection_date ({expected})"
                )


@dataclass(frozen=True, slots=True)
class AutoresearchState:
    phase: Phase = Phase.SETUP_CONTEXT
    iteration: int = 1
    consensus_retry_count: int = 0
    verification_fix_attempts: int = 0
    setup: SetupContextArtifact | None = None
    context_packet: ContextPacketArtifact | None = None
    debate_rounds: tuple[DebateResultArtifact, ...] = field(default_factory=tuple)
    consensus_history: tuple[ConsensusResultArtifact, ...] = field(default_factory=tuple)
    implementation_result: ImplementationResultArtifact | None = None
    verification_history: tuple[VerificationResultArtifact, ...] = field(default_factory=tuple)
    review_history: tuple[ReviewResultArtifact, ...] = field(default_factory=tuple)
    fix_history: tuple[FixResultArtifact, ...] = field(default_factory=tuple)
    pending_fix_trigger: FixTriggerPhase | None = None
    final_decision: FinalDecisionArtifact | None = None
    memory_written: bool = False
    mode: ResearchMode | None = None
    memory_verification_receipt: MemoryVerificationReceipt | None = None
    platform_readiness: ReadinessIdentity | None = None
    external_verification_retry_receipt: ExternalVerificationRetryReceipt | None = None
    interrupted_verification_history: tuple[InterruptedVerificationAttemptReceipt, ...] = field(
        default_factory=tuple
    )
    platform_runtime_recovery_receipt: PlatformRuntimeRecoveryReceipt | None = None
    canonical_quantipy_runtime_attestation: CanonicalQuantipyRuntimeAttestation | None = None
    suspended: bool = False
    suspension_reason: str | None = None

    @property
    def latest_debate(self) -> DebateResultArtifact | None:
        return self.debate_rounds[-1] if self.debate_rounds else None

    @property
    def latest_consensus(self) -> ConsensusResultArtifact | None:
        return self.consensus_history[-1] if self.consensus_history else None

    @property
    def latest_verification(self) -> VerificationResultArtifact | None:
        return self.verification_history[-1] if self.verification_history else None

    @property
    def latest_review(self) -> ReviewResultArtifact | None:
        return self.review_history[-1] if self.review_history else None

    @property
    def latest_fix(self) -> FixResultArtifact | None:
        return self.fix_history[-1] if self.fix_history else None

    @classmethod
    def from_dict(cls, raw: object) -> AutoresearchState:
        data = _ensure_mapping(raw, label="autoresearch_state")
        if "schema_version" not in data:
            raise AutoresearchValidationError(
                "autoresearch state missing schema_version is unsupported; archive it and "
                "initialize a schema-v4 state with `gateway-cli autoresearch-init-state`; "
                f"expected schema_version={AUTORESEARCH_STATE_SCHEMA_VERSION}"
            )
        schema_version = _require_int(data, "schema_version")
        if schema_version != AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "autoresearch state cannot be migrated in place; stop the supervisor, archive "
                f"the live schema-v{schema_version} state, and initialize a fresh schema-v"
                f"{AUTORESEARCH_STATE_SCHEMA_VERSION} state with "
                "`gateway-cli autoresearch-init-state` before restart"
            )

        def _parse_tuple(
            field_name: str,
            parser: Callable[[object], _T],
        ) -> tuple[_T, ...]:
            value = data.get(field_name, [])
            if not isinstance(value, Sequence) or isinstance(value, str | bytes):
                raise AutoresearchValidationError(f"{field_name} must be a list")
            return tuple(parser(item) for item in value)

        setup_raw = data.get("setup")
        context_raw = data.get("context_packet")
        implementation_raw = data.get("implementation_result")
        decision_raw = data.get("final_decision")
        receipt_raw = data.get("memory_verification_receipt")
        platform_readiness_raw = data.get("platform_readiness")
        external_retry_raw = data.get("external_verification_retry_receipt")
        platform_runtime_recovery_raw = data.get("platform_runtime_recovery_receipt")
        canonical_runtime_raw = data.get("canonical_quantipy_runtime_attestation")
        pending_fix_trigger_raw = data.get("pending_fix_trigger")
        if pending_fix_trigger_raw is not None and not isinstance(pending_fix_trigger_raw, str):
            raise AutoresearchValidationError("pending_fix_trigger must be a string or null")
        if "mode" not in data:
            raise AutoresearchValidationError(
                "mode must be explicit in persisted autoresearch state"
            )
        expected_state_keys: tuple[str, ...] = (
            "schema_version",
            "phase",
            "iteration",
            "consensus_retry_count",
            "verification_fix_attempts",
            "setup",
            "context_packet",
            "debate_rounds",
            "consensus_history",
            "implementation_result",
            "verification_history",
            "review_history",
            "fix_history",
            "pending_fix_trigger",
            "final_decision",
            "memory_written",
            "mode",
            "memory_verification_receipt",
            "platform_readiness",
            "external_verification_retry_receipt",
            "interrupted_verification_history",
            "platform_runtime_recovery_receipt",
            "canonical_quantipy_runtime_attestation",
            "suspended",
            "suspension_reason",
        )
        if "interrupted_verification_history" not in data:
            expected_state_keys = tuple(
                key for key in expected_state_keys if key != "interrupted_verification_history"
            )
        if "platform_runtime_recovery_receipt" not in data:
            expected_state_keys = tuple(
                key for key in expected_state_keys if key != "platform_runtime_recovery_receipt"
            )
        if "canonical_quantipy_runtime_attestation" not in data:
            expected_state_keys = tuple(
                key
                for key in expected_state_keys
                if key != "canonical_quantipy_runtime_attestation"
            )
        _require_exact_keys(data, label="autoresearch_state", expected=expected_state_keys)
        mode_raw = data.get("mode")
        if mode_raw is not None and not isinstance(mode_raw, str):
            raise AutoresearchValidationError("mode must be a string or null")

        state = cls(
            phase=Phase(_require_str(data, "phase")) if "phase" in data else Phase.SETUP_CONTEXT,
            iteration=_require_int(data, "iteration") if "iteration" in data else 1,
            consensus_retry_count=_require_int(data, "consensus_retry_count")
            if "consensus_retry_count" in data
            else 0,
            verification_fix_attempts=_require_int(data, "verification_fix_attempts")
            if "verification_fix_attempts" in data
            else 0,
            setup=SetupContextArtifact.from_dict(setup_raw) if setup_raw is not None else None,
            context_packet=ContextPacketArtifact.from_dict(context_raw)
            if context_raw is not None
            else None,
            debate_rounds=_parse_tuple("debate_rounds", DebateResultArtifact.from_dict),
            consensus_history=_parse_tuple("consensus_history", ConsensusResultArtifact.from_dict),
            implementation_result=ImplementationResultArtifact.from_dict(implementation_raw)
            if implementation_raw is not None
            else None,
            verification_history=_parse_tuple(
                "verification_history",
                lambda item: VerificationResultArtifact.from_dict(
                    item,
                    mode=ResearchMode(mode_raw) if mode_raw is not None else None,
                ),
            ),
            review_history=_parse_tuple("review_history", ReviewResultArtifact.from_dict),
            fix_history=_parse_tuple("fix_history", FixResultArtifact.from_dict),
            pending_fix_trigger=FixTriggerPhase(pending_fix_trigger_raw)
            if pending_fix_trigger_raw is not None
            else None,
            final_decision=FinalDecisionArtifact.from_dict(decision_raw)
            if decision_raw is not None
            else None,
            memory_written=_require_bool(data, "memory_written")
            if "memory_written" in data
            else False,
            mode=ResearchMode(mode_raw) if mode_raw is not None else None,
            memory_verification_receipt=MemoryVerificationReceipt.from_dict(receipt_raw)
            if receipt_raw is not None
            else None,
            platform_readiness=ReadinessIdentity.from_dict(platform_readiness_raw)
            if platform_readiness_raw is not None
            else None,
            external_verification_retry_receipt=ExternalVerificationRetryReceipt.from_dict(
                external_retry_raw
            )
            if external_retry_raw is not None
            else None,
            interrupted_verification_history=_parse_tuple(
                "interrupted_verification_history",
                InterruptedVerificationAttemptReceipt.from_dict,
            ),
            platform_runtime_recovery_receipt=PlatformRuntimeRecoveryReceipt.from_dict(
                platform_runtime_recovery_raw
            )
            if platform_runtime_recovery_raw is not None
            else None,
            canonical_quantipy_runtime_attestation=(
                CanonicalQuantipyRuntimeAttestation.from_dict(canonical_runtime_raw)
                if canonical_runtime_raw is not None
                else None
            ),
            suspended=_require_bool(data, "suspended") if "suspended" in data else False,
            suspension_reason=(
                _require_str(data, "suspension_reason")
                if data.get("suspension_reason") is not None
                else None
            ),
        )
        return state

    def to_dict(self) -> dict[str, object]:
        verification_history = [artifact.to_dict() for artifact in self.verification_history]
        state: dict[str, object] = {
            "schema_version": AUTORESEARCH_STATE_SCHEMA_VERSION,
            "phase": self.phase.value,
            "iteration": self.iteration,
            "consensus_retry_count": self.consensus_retry_count,
            "verification_fix_attempts": self.verification_fix_attempts,
            "setup": self.setup.to_dict() if self.setup else None,
            "context_packet": self.context_packet.to_dict() if self.context_packet else None,
            "debate_rounds": [artifact.to_dict() for artifact in self.debate_rounds],
            "consensus_history": [artifact.to_dict() for artifact in self.consensus_history],
            "implementation_result": self.implementation_result.to_dict()
            if self.implementation_result
            else None,
            "verification_history": verification_history,
            "review_history": [artifact.to_dict() for artifact in self.review_history],
            "fix_history": [artifact.to_dict() for artifact in self.fix_history],
            "pending_fix_trigger": self.pending_fix_trigger.value
            if self.pending_fix_trigger is not None
            else None,
            "final_decision": self.final_decision.to_dict() if self.final_decision else None,
            "memory_written": self.memory_written,
            "mode": self.mode.value if self.mode is not None else None,
            "memory_verification_receipt": self.memory_verification_receipt.to_dict()
            if self.memory_verification_receipt is not None
            else None,
            "platform_readiness": self.platform_readiness.to_dict()
            if self.platform_readiness is not None
            else None,
            "external_verification_retry_receipt": (
                self.external_verification_retry_receipt.to_dict()
                if self.external_verification_retry_receipt is not None
                else None
            ),
            "suspended": self.suspended,
            "suspension_reason": self.suspension_reason,
        }
        if self.interrupted_verification_history:
            state["interrupted_verification_history"] = [
                receipt.to_dict() for receipt in self.interrupted_verification_history
            ]
        if self.platform_runtime_recovery_receipt is not None:
            state["platform_runtime_recovery_receipt"] = (
                self.platform_runtime_recovery_receipt.to_dict()
            )
        if self.canonical_quantipy_runtime_attestation is not None:
            state["canonical_quantipy_runtime_attestation"] = (
                self.canonical_quantipy_runtime_attestation.to_dict()
            )
        return state
