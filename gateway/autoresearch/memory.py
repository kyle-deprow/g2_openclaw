"""Memory-write derivation and finalization verification for autoresearch."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch.artifacts import (
    MemoryVerificationReceipt as MemoryVerificationReceipt,
)
from gateway.autoresearch.constants import (
    MEMPALACE_KG_OBJECT_MAX_LENGTH as MEMPALACE_KG_OBJECT_MAX_LENGTH,
)
from gateway.autoresearch.constants import (
    MEMPALACE_KG_OBJECT_SHA256_LENGTH as MEMPALACE_KG_OBJECT_SHA256_LENGTH,
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
    _normalise_predicate as _normalise_predicate,
)
from gateway.autoresearch.fields import (
    _sha256_text as _sha256_text,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy as AutoresearchPolicy,
)
from gateway.autoresearch.receipts import (
    AggregateCoverageReceipt as AggregateCoverageReceipt,
)
from gateway.autoresearch.receipts import (
    DynamicUniverseCoverageReceipt as DynamicUniverseCoverageReceipt,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    KEEP_DECISIONS as KEEP_DECISIONS,
)
from gateway.autoresearch.transitions import (
    _compact_json_block as _compact_json_block,
)
from gateway.autoresearch.transitions import (
    _final_decision_requires_memory_write as _final_decision_requires_memory_write,
)
from gateway.autoresearch.transitions import (
    _is_authorized_no_memory_final_decision as _is_authorized_no_memory_final_decision,
)
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_SOURCE_FILE as FINAL_MEMORY_SOURCE_FILE,
)
from gateway.mempalace_finalizer import (
    FinalMemoryWriter as FinalMemoryWriter,
)
from gateway.mempalace_finalizer import (
    FinalMemoryWriteRequest as FinalMemoryWriteRequest,
)
from gateway.mempalace_finalizer import (
    MempalaceFinalizationError as MempalaceFinalizationError,
)
from gateway.mempalace_finalizer import (
    SubprocessFinalMemoryWriter as SubprocessFinalMemoryWriter,
)
from gateway.mempalace_finalizer import (
    finalization_journal_path as finalization_journal_path,
)

if TYPE_CHECKING:
    from gateway.autoresearch.artifacts import (
        FinalDecisionArtifact as FinalDecisionArtifact,
    )


G2_OPENCLAW_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def standardize_mempalace_kg_object(value: str) -> str:
    """Normalize a KG object and compact it to MemPalace's 128-character limit."""
    normalized = _normalise_predicate(value)
    if len(normalized) <= MEMPALACE_KG_OBJECT_MAX_LENGTH:
        return normalized
    prefix_length = MEMPALACE_KG_OBJECT_MAX_LENGTH - MEMPALACE_KG_OBJECT_SHA256_LENGTH - 1
    return f"{normalized[:prefix_length]}_{_sha256_text(normalized)}"


def can_write_memory(state: AutoresearchState) -> bool:
    return (
        state.phase is Phase.REPEAT
        and state.final_decision is not None
        and state.final_decision.memory_write_required
        and _final_decision_requires_memory_write(state, state.final_decision)
    )


def _is_explicit_no_memory_transition(state: AutoresearchState) -> bool:
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and decision is not None
        and _is_authorized_no_memory_final_decision(state)
        and not state.memory_written
        and state.memory_verification_receipt is None
    )


def _default_mempalace_kg_path() -> Path:
    palace_root = Path(
        os.environ.get("MEMPALACE_PALACE", str(Path.home() / ".mempalace/palace"))
    ).expanduser()
    return palace_root / "knowledge_graph.sqlite3"


def _standard_metric_object(decision: FinalDecisionArtifact) -> str:
    if decision.recommended_metric_value is None:
        return standardize_mempalace_kg_object(decision.recommended_metric_name)
    value = decision.recommended_metric_value
    sign = "negative_" if value < 0 else ""
    metric = f"{decision.recommended_metric_name}_{sign}{abs(value):g}"
    return standardize_mempalace_kg_object(metric)


def _standard_data_window_object(
    coverage: DynamicUniverseCoverageReceipt | AggregateCoverageReceipt,
) -> str:
    """Return the normalized common data/OOS window token required in MemPalace."""
    if isinstance(coverage, DynamicUniverseCoverageReceipt):
        return standardize_mempalace_kg_object(
            f"{coverage.experiment_start}_to_{coverage.experiment_end}_oos_"
            f"{coverage.oos_start}_to_{coverage.oos_end}"
        )
    return standardize_mempalace_kg_object(
        f"{coverage.actual_common_start}_to_{coverage.actual_common_end}_oos_"
        f"{coverage.oos_start}_to_{coverage.oos_end}"
    )


def standardized_mempalace_kg_facts(state: AutoresearchState) -> dict[str, str]:
    """Return the exact standardized KG facts required for a final decision."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError(
            "standardized MemPalace facts require final_decision and mode"
        )
    if not _final_decision_requires_memory_write(state, state.final_decision):
        raise AutoresearchValidationError(
            "standardized MemPalace facts are allowed only for retention-eligible final decisions"
        )
    verification = state.latest_verification
    if verification is None:
        raise AutoresearchValidationError(
            "standardized MemPalace facts require the final decision's verification_result"
        )
    decision = state.final_decision
    data_window = (
        standardize_mempalace_kg_object("unavailable")
        if verification.data_coverage is None
        else _standard_data_window_object(verification.data_coverage)
    )
    facts = {
        "decision": standardize_mempalace_kg_object(decision.decision.value),
        "research_mode": standardize_mempalace_kg_object(state.mode.value),
        "data_window": data_window,
        "reviewer_verdict": standardize_mempalace_kg_object(decision.reviewer_verdict.value),
    }
    if state.mode is ResearchMode.ALPHA_RESEARCH:
        facts["alpha_decision_metric"] = _standard_metric_object(decision)
        facts["keeper_rationale" if decision.decision in KEEP_DECISIONS else "failed_due_to"] = (
            standardize_mempalace_kg_object(decision.rationale)
        )
        return facts
    if verification.infra_gate_outcome is None or decision.infra_rationale is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 standardized MemPalace facts require gate outcome and infra_rationale"
        )
    facts["infra_gate_outcome"] = standardize_mempalace_kg_object(
        verification.infra_gate_outcome.value
    )
    facts["infra_rationale"] = standardize_mempalace_kg_object(decision.infra_rationale)
    return facts


def build_final_memory_write_request(state: AutoresearchState) -> FinalMemoryWriteRequest:
    """Derive the only MemPalace write payload from a validated final state."""
    if not can_write_memory(state) or state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError(
            "final MemPalace persistence requires a retention-eligible repeat decision"
        )
    verification = state.latest_verification
    if verification is None:
        raise AutoresearchValidationError(
            "final MemPalace persistence requires the final verification result"
        )
    drawer_content = _compact_json_block(
        {
            "experiment_id": state.final_decision.experiment_id,
            "final_decision": state.final_decision.to_dict(),
            "research_mode": state.mode.value,
            "schema": "g2-openclaw.autoresearch.final-memory.v1",
            "verification_result": verification.to_dict(),
        }
    )
    return FinalMemoryWriteRequest(
        experiment_id=state.final_decision.experiment_id,
        drawer_content=drawer_content,
        facts=standardized_mempalace_kg_facts(state),
    )


def finalize_repeat_memory(
    state: AutoresearchState,
    *,
    writer: FinalMemoryWriter | None = None,
) -> AutoresearchState:
    """Write, verify, and mark the state-owned final decision exactly once."""
    request = build_final_memory_write_request(state)
    finalizer = writer or SubprocessFinalMemoryWriter.from_environment(
        repository_root=G2_OPENCLAW_REPO_ROOT
    )
    try:
        kg_path = finalizer.write(request)
    except MempalaceFinalizationError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    receipt = verify_mempalace_final_decision(state, kg_path)
    return mark_memory_written(state, receipt)


def _committed_finalization_journal_drawer_id(
    journal: object,
    *,
    expected_request_sha256: str,
) -> str:
    _validate_sha256(expected_request_sha256, label="expected_request_sha256")
    if not isinstance(journal, Mapping):
        raise AutoresearchValidationError("MemPalace finalization journal must be an object")
    if set(journal) != {"status", "request_sha256", "drawer_id"}:
        raise AutoresearchValidationError(
            "MemPalace committed finalization journal schema is invalid"
        )
    if journal.get("status") != "committed":
        raise AutoresearchValidationError("MemPalace finalization journal is not committed")
    request_sha256 = journal.get("request_sha256")
    if not isinstance(request_sha256, str):
        raise AutoresearchValidationError(
            "MemPalace finalization journal request_sha256 is invalid"
        )
    try:
        _validate_sha256(request_sha256, label="request_sha256")
    except AutoresearchValidationError as exc:
        raise AutoresearchValidationError(
            "MemPalace finalization journal request_sha256 is invalid"
        ) from exc
    if request_sha256 != expected_request_sha256:
        raise AutoresearchValidationError("MemPalace finalization journal does not match state")
    drawer_id = journal.get("drawer_id")
    if not isinstance(drawer_id, str) or not drawer_id.strip():
        raise AutoresearchValidationError(
            "MemPalace finalization journal lacks canonical drawer ID"
        )
    return drawer_id


def verify_mempalace_final_decision(
    state: AutoresearchState,
    kg_path: Path | None = None,
) -> MemoryVerificationReceipt:
    """Read and attest KG facts; this function never mutates MemPalace."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError("MemPalace verification requires final_decision and mode")
    if not _final_decision_requires_memory_write(state, state.final_decision):
        raise AutoresearchValidationError(
            "MemPalace verification is prohibited for a non-retention final decision"
        )
    if not state.final_decision.memory_write_required:
        raise AutoresearchValidationError(
            "MemPalace verification is not required for this final decision"
        )
    path = (kg_path if kg_path is not None else _default_mempalace_kg_path()).expanduser()
    if not path.is_file():
        raise AutoresearchValidationError(f"MemPalace KG does not exist: {path}")
    decision = state.final_decision
    expected_objects = standardized_mempalace_kg_facts(state)
    request = build_final_memory_write_request(state)
    journal_path = finalization_journal_path(path.parent, decision.experiment_id)
    try:
        journal: object = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoresearchValidationError("MemPalace finalization journal is unavailable") from exc
    expected_journal_digest = _sha256_text(
        json.dumps(request.to_dict(), separators=(",", ":"), sort_keys=True)
    )
    expected_drawer_id = _committed_finalization_journal_drawer_id(
        journal,
        expected_request_sha256=expected_journal_digest,
    )
    required = set(expected_objects)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(triples)").fetchall()}
        required_columns = {
            "id",
            "subject",
            "predicate",
            "object",
            "valid_from",
            "valid_to",
            "source_file",
            "source_drawer_id",
        }
        if not required_columns <= columns:
            raise AutoresearchValidationError("MemPalace KG triples schema is incomplete")
        rows = connection.execute(
            """
            SELECT id, subject, predicate, object, valid_from, valid_to,
                   source_file, source_drawer_id
            FROM triples WHERE subject = ? AND valid_to IS NULL
            """,
            (decision.experiment_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AutoresearchValidationError(f"cannot read MemPalace KG: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    facts: dict[str, list[tuple[str, str, str, str, str, str, str, str]]] = {}
    for row in rows:
        normalized_predicate = _normalise_predicate(str(row[2]))
        if normalized_predicate not in required:
            continue
        source_file = str(row[6] or "").strip()
        source_drawer_id = str(row[7] or "").strip()
        if source_file != FINAL_MEMORY_SOURCE_FILE or source_drawer_id != expected_drawer_id:
            raise AutoresearchValidationError(
                "MemPalace standardized facts require exact canonical finalizer provenance"
            )
        if not str(row[3]).strip():
            raise AutoresearchValidationError(
                "MemPalace standardized facts require non-empty objects"
            )
        normalized_row = (
            "" if row[0] is None else str(row[0]),
            "" if row[1] is None else str(row[1]),
            "" if row[2] is None else str(row[2]),
            "" if row[3] is None else str(row[3]),
            "" if row[4] is None else str(row[4]),
            "" if row[5] is None else str(row[5]),
            "" if row[6] is None else str(row[6]),
            "" if row[7] is None else str(row[7]),
        )
        facts.setdefault(normalized_predicate, []).append(normalized_row)
    missing = sorted(required - facts.keys())
    if missing:
        raise AutoresearchValidationError(
            "MemPalace required standardized facts are missing: " + ", ".join(missing)
        )

    for predicate, expected_object in expected_objects.items():
        if any(row[3] != expected_object for row in facts[predicate]):
            raise AutoresearchValidationError(
                f"MemPalace {predicate} fact does not match final decision artifact"
            )
    stable_rows = sorted(row for predicate_rows in facts.values() for row in predicate_rows)
    digest = _sha256_text(json.dumps(stable_rows, separators=(",", ":"), ensure_ascii=True))
    return MemoryVerificationReceipt(
        experiment_id=decision.experiment_id,
        kg_path=str(path),
        predicates=tuple(sorted(facts)),
        verified_rows_digest=digest,
    )


def mark_memory_written(
    state: AutoresearchState,
    receipt: MemoryVerificationReceipt,
) -> AutoresearchState:
    if not can_write_memory(state):
        raise AutoresearchValidationError(
            "memory write is allowed only after final decision that requires memory"
        )
    if state.final_decision is None or receipt.experiment_id != state.final_decision.experiment_id:
        raise AutoresearchValidationError("memory receipt must match final decision experiment_id")
    return replace(state, memory_written=True, memory_verification_receipt=receipt)


def finalize_repeat_memory_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    writer: FinalMemoryWriter | None = None,
) -> AutoresearchState:
    """Finalize and atomically mark the current repeat state under its state lock."""
    from gateway.autoresearch import memory as memory_module
    from gateway.autoresearch import persistence as persistence_module
    from gateway.autoresearch import transitions as transitions_module

    resolved_state_path = state_path.expanduser().resolve(strict=False)
    with persistence_module._exclusive_state_lock(resolved_state_path):
        state = persistence_module.load_state_file(resolved_state_path)
        transitions_module._validate_state(state, policy, validation_context)
        finalized = memory_module.finalize_repeat_memory(state, writer=writer)
        transitions_module._validate_state(finalized, policy, validation_context)
        persistence_module._atomic_save_state_file(resolved_state_path, finalized)
        return finalized
