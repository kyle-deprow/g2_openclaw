from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import (
    replace,
)
from datetime import (
    date,
)
from hashlib import sha256
from pathlib import Path

import gateway.autoresearch_runner as autoresearch_runner
import pytest
from gateway.autoresearch_decision_receipts import (
    decision_receipt_content,
    decision_receipt_path,
    persist_decision_receipt,
)
from gateway.autoresearch_readiness import (
    EvidenceId,
    PlatformReadinessManifest,
    ReadinessIdentity,
    ReadinessStatus,
)
from gateway.autoresearch_runner import (
    AutoresearchPolicy,
    AutoresearchState,
    AutoresearchValidationContext,
    AutoresearchValidationError,
    MemoryVerificationReceipt,
    Phase,
    ReceiptCatalog,
    ResearchMode,
    build_final_memory_write_request,
    can_write_memory,
    expected_instruction_manifest_sha256,
    finalize_repeat_memory,
    finalize_repeat_memory_state_file,
    mark_memory_written,
    next_action,
    persist_next_iteration_state,
    resume_suspended_iteration,
    save_state_file,
    standardize_mempalace_kg_object,
    standardized_mempalace_kg_facts,
    start_next_iteration,
    suspend_for_infrastructure,
    verify_mempalace_final_decision,
)
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_SOURCE_FILE,
)

from tests.gateway.autoresearch.builders import (
    _final_decision,
    _ready_manifest,
    _state_to_decision,
    _StateDerivedMemoryWriter,
    _write_active_mempalace_facts,
    _write_committed_finalization_journal,
    advance_state,
)


def test_autoresearch_resume_rejects_an_unchanged_readiness_identity(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = suspend_for_infrastructure(
        _state_to_decision(policy, platform_readiness),
        "Operator is repairing infrastructure.",
    )

    with pytest.raises(AutoresearchValidationError, match="changed READY"):
        resume_suspended_iteration(state, platform_readiness)


def test_standardize_mempalace_kg_object_preserves_short_normalized_objects() -> None:
    serialized = standardize_mempalace_kg_object("  Provenance: Verified!  ")

    assert serialized == "provenance_verified"


def test_standardize_mempalace_kg_object_compacts_long_objects_to_a_stable_exact_bound() -> None:
    long_object = "auditable provenance " * 20
    normalized = "auditable_provenance_" * 19 + "auditable_provenance"

    serialized = standardize_mempalace_kg_object(long_object)

    assert len(serialized) == 128
    assert serialized == f"{normalized[:63]}_{sha256(normalized.encode()).hexdigest()}"


def test_standardize_mempalace_kg_object_distinguishes_long_objects_with_same_prefix() -> None:
    first = standardize_mempalace_kg_object("a" * 200 + "first")
    second = standardize_mempalace_kg_object("a" * 200 + "second")

    assert first != second


def test_verify_mempalace_final_decision_accepts_compacted_alpha_discard_rationale(
    alpha_memory_state: AutoresearchState,
    mempalace_kg_path: Path,
) -> None:
    facts = standardized_mempalace_kg_facts(alpha_memory_state)
    _write_active_mempalace_facts(
        mempalace_kg_path,
        subject="alpha-discard-1",
        facts=facts,
    )
    _write_committed_finalization_journal(
        mempalace_kg_path, build_final_memory_write_request(alpha_memory_state)
    )
    connection = sqlite3.connect(mempalace_kg_path)
    connection.execute(
        "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, ?, ?, NULL)",
        (
            "inactive-rationale",
            "alpha-discard-1",
            "failed_due_to",
            "shortened_retry_rationale",
            "2026-07-10T00:00:00Z",
            "result.json",
        ),
    )
    connection.commit()
    connection.close()

    receipt = verify_mempalace_final_decision(alpha_memory_state, mempalace_kg_path)

    assert receipt.predicates == tuple(sorted(facts))


def test_verify_mempalace_final_decision_rejects_conflicting_active_alpha_fact(
    alpha_memory_state: AutoresearchState,
    mempalace_kg_path: Path,
) -> None:
    facts = standardized_mempalace_kg_facts(alpha_memory_state)
    _write_active_mempalace_facts(
        mempalace_kg_path,
        subject="alpha-discard-1",
        facts=facts,
    )
    _write_committed_finalization_journal(
        mempalace_kg_path, build_final_memory_write_request(alpha_memory_state)
    )
    connection = sqlite3.connect(mempalace_kg_path)
    connection.execute(
        "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
        (
            "conflicting-rationale",
            "alpha-discard-1",
            "failed_due_to",
            "shortened_retry_rationale",
            FINAL_MEMORY_SOURCE_FILE,
            "drawer-finalizer",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        AutoresearchValidationError,
        match="MemPalace failed_due_to fact does not match final decision artifact",
    ):
        verify_mempalace_final_decision(alpha_memory_state, mempalace_kg_path)


def test_verify_mempalace_final_decision_rejects_normalized_but_not_exact_active_object(
    alpha_memory_state: AutoresearchState,
    mempalace_kg_path: Path,
) -> None:
    facts = standardized_mempalace_kg_facts(alpha_memory_state)
    _write_active_mempalace_facts(
        mempalace_kg_path,
        subject="alpha-discard-1",
        facts=facts,
        object_overrides={"decision": " DISC---ARD "},
    )
    _write_committed_finalization_journal(
        mempalace_kg_path, build_final_memory_write_request(alpha_memory_state)
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="MemPalace decision fact does not match final decision artifact",
    ):
        verify_mempalace_final_decision(alpha_memory_state, mempalace_kg_path)


def test_mempalace_incident_replay_accepts_emitted_compacted_alpha_discard_rationale(
    alpha_memory_state: AutoresearchState,
    mempalace_kg_path: Path,
) -> None:
    final_decision = alpha_memory_state.final_decision
    assert final_decision is not None
    incident_state = replace(
        alpha_memory_state,
        final_decision=replace(
            final_decision,
            rationale="r" * 268,
        ),
    )
    facts = standardized_mempalace_kg_facts(incident_state)
    emitted_rationale = facts["failed_due_to"]
    partial_facts = {
        predicate: object_value
        for predicate, object_value in facts.items()
        if predicate != "failed_due_to"
    }
    _write_active_mempalace_facts(
        mempalace_kg_path,
        subject="alpha-discard-1",
        facts=partial_facts,
    )
    _write_committed_finalization_journal(
        mempalace_kg_path, build_final_memory_write_request(incident_state)
    )
    connection = sqlite3.connect(mempalace_kg_path)
    connection.execute(
        "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
        (
            "emitted-infra-rationale",
            "alpha-discard-1",
            "failed_due_to",
            emitted_rationale,
            FINAL_MEMORY_SOURCE_FILE,
            "drawer-finalizer",
        ),
    )
    connection.commit()
    connection.close()

    receipt = verify_mempalace_final_decision(incident_state, mempalace_kg_path)

    assert len(emitted_rationale) == 128
    assert receipt.predicates == tuple(sorted(facts))


def test_memory_write_gated_until_final_decision(
    policy: AutoresearchPolicy,
    tmp_path: Path,
) -> None:
    state = _state_to_decision(policy)

    assert can_write_memory(state) is False
    with pytest.raises(AutoresearchValidationError, match="only after final decision"):
        mark_memory_written(
            state,
            MemoryVerificationReceipt(
                experiment_id="iteration-1",
                kg_path="/tmp/knowledge_graph.sqlite3",
                predicates=("decision",),
                verified_rows_digest="0" * 64,
            ),
        )

    state = advance_state(state, _final_decision(), policy)
    assert can_write_memory(state) is True

    state = mark_memory_written(
        state,
        MemoryVerificationReceipt(
            experiment_id="iteration-1",
            kg_path="/tmp/knowledge_graph.sqlite3",
            predicates=("decision",),
            verified_rows_digest="0" * 64,
        ),
    )
    assert state.memory_written is True

    readiness = _ready_manifest(tmp_path)
    state = replace(state, platform_readiness=readiness.identity())
    next_iteration = start_next_iteration(state, readiness=readiness)
    assert next_iteration.phase is Phase.SETUP_CONTEXT
    assert next_iteration.iteration == 2


def test_final_memory_write_request_is_derived_from_authoritative_repeat_state(
    alpha_memory_state: AutoresearchState,
) -> None:
    request = build_final_memory_write_request(alpha_memory_state)
    final_decision = alpha_memory_state.final_decision
    verification_result = alpha_memory_state.latest_verification
    assert final_decision is not None
    assert verification_result is not None

    assert request.experiment_id == "alpha-discard-1"
    assert request.facts == standardized_mempalace_kg_facts(alpha_memory_state)
    assert request.drawer_content == json.dumps(
        {
            "experiment_id": "alpha-discard-1",
            "final_decision": final_decision.to_dict(),
            "research_mode": ResearchMode.ALPHA_RESEARCH.value,
            "schema": "g2-openclaw.autoresearch.final-memory.v1",
            "verification_result": verification_result.to_dict(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def test_finalize_repeat_memory_marks_only_the_state_derived_final_decision(
    alpha_memory_state: AutoresearchState,
    mempalace_kg_path: Path,
) -> None:
    writer = _StateDerivedMemoryWriter(mempalace_kg_path)

    finalized = finalize_repeat_memory(alpha_memory_state, writer=writer)

    assert finalized.memory_written is True
    assert finalized.memory_verification_receipt is not None
    assert finalized.memory_verification_receipt.experiment_id == "alpha-discard-1"
    assert writer.request_experiment_id == "alpha-discard-1"


def test_finalize_repeat_memory_state_file_atomically_marks_the_current_repeat_state(
    alpha_memory_state: AutoresearchState,
    mempalace_kg_path: Path,
    policy: AutoresearchPolicy,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    save_state_file(state_path, alpha_memory_state)
    writer = _StateDerivedMemoryWriter(mempalace_kg_path)

    finalized = finalize_repeat_memory_state_file(
        state_path,
        policy=policy,
        validation_context=None,
        writer=writer,
    )

    assert finalized.memory_written is True
    assert autoresearch_runner.load_state_file(state_path) == finalized


def test_persist_next_iteration_state_writes_canonical_decision_receipt(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(
        json.dumps(completed_memory_written_state.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )
    next_state = start_next_iteration(completed_memory_written_state, readiness=platform_readiness)

    persist_next_iteration_state(
        state_path,
        state_path,
        completed_memory_written_state,
        next_state,
        instruction_manifest_sha256=instruction_digest,
        policy=policy,
        receipt_catalog_factory=lambda: receipts,
    )

    receipt_path = decision_receipt_path(state_path, completed_memory_written_state.iteration)
    receipt_content = receipt_path.read_bytes()
    receipt_payload = json.loads(receipt_content)
    persisted = AutoresearchState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))

    assert persisted.phase is Phase.SETUP_CONTEXT
    assert persisted.iteration == completed_memory_written_state.iteration + 1
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert receipt_content == json.dumps(
        receipt_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert receipt_payload["instruction_manifest_sha256"] == instruction_digest
    assert receipt_payload["state_reference"]["phase"] == Phase.REPEAT.value
    assert completed_memory_written_state.final_decision is not None
    assert completed_memory_written_state.memory_verification_receipt is not None
    assert (
        receipt_payload["final_decision"] == completed_memory_written_state.final_decision.to_dict()
    )
    assert (
        receipt_payload["memory_verification_receipt"]
        == completed_memory_written_state.memory_verification_receipt.to_dict()
    )


def test_decision_receipt_is_idempotent_for_same_content(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )

    first = persist_decision_receipt(
        completed_memory_written_state,
        state_path=state_path,
        instruction_manifest_sha256=instruction_digest,
    )
    second = persist_decision_receipt(
        completed_memory_written_state,
        state_path=state_path,
        instruction_manifest_sha256=instruction_digest,
    )

    assert second.path == first.path
    assert second.sha256 == first.sha256
    assert second.content == first.content


def test_decision_receipt_conflict_fails_without_replacing_state(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(completed_memory_written_state.to_dict()), encoding="utf-8")
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )
    receipt_path = decision_receipt_path(state_path, completed_memory_written_state.iteration)
    receipt_path.write_bytes(b"{}")
    receipt_path.chmod(0o600)
    next_state = start_next_iteration(completed_memory_written_state, readiness=platform_readiness)

    with pytest.raises(AutoresearchValidationError, match="decision receipt conflict"):
        persist_next_iteration_state(
            state_path,
            state_path,
            completed_memory_written_state,
            next_state,
            instruction_manifest_sha256=instruction_digest,
            policy=policy,
            receipt_catalog_factory=lambda: receipts,
        )

    persisted = AutoresearchState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
    assert persisted == completed_memory_written_state


def test_decision_receipt_rejects_symlink_path(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )
    receipt_path = decision_receipt_path(state_path, completed_memory_written_state.iteration)
    receipt_path.symlink_to(tmp_path / "outside.json")

    with pytest.raises(AutoresearchValidationError, match="must not be a symlink"):
        persist_decision_receipt(
            completed_memory_written_state,
            state_path=state_path,
            instruction_manifest_sha256=instruction_digest,
        )


def test_decision_receipt_rejects_symlinked_state_parent(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
) -> None:
    real_parent = tmp_path / "real-state"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-state"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    state_path = linked_parent / "quantipy-state.json"
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )

    with pytest.raises(AutoresearchValidationError, match=r"symlinks|canonical no-symlink"):
        persist_decision_receipt(
            completed_memory_written_state,
            state_path=state_path,
            instruction_manifest_sha256=instruction_digest,
        )

    assert not (real_parent / "decision-receipts").exists()


def test_decision_receipt_full_write_loop_handles_partial_writes(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )
    real_write: Callable[[int, bytes], int] = os.write
    partial_writes = 0

    def write_partial(fd: int, data: bytes) -> int:
        nonlocal partial_writes
        if len(data) > 1:
            partial_writes += 1
            return real_write(fd, data[: max(1, len(data) // 3)])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write_partial)

    persisted = persist_decision_receipt(
        completed_memory_written_state,
        state_path=state_path,
        instruction_manifest_sha256=instruction_digest,
    )

    assert partial_writes > 0
    assert persisted.path.read_bytes() == persisted.content


def test_decision_receipt_existing_file_revalidates_internal_digest_bindings(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    completed_memory_written_state: AutoresearchState,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )
    receipt_path = decision_receipt_path(state_path, completed_memory_written_state.iteration)
    payload = json.loads(
        decision_receipt_content(
            completed_memory_written_state,
            state_path=state_path,
            instruction_manifest_sha256=instruction_digest,
        ).decode("utf-8")
    )
    payload["final_decision_sha256"] = "0" * 64
    receipt_path.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    receipt_path.chmod(0o600)

    with pytest.raises(AutoresearchValidationError, match="decision receipt conflict"):
        persist_decision_receipt(
            completed_memory_written_state,
            state_path=state_path,
            instruction_manifest_sha256=instruction_digest,
        )


def test_start_next_adopts_changed_ready_identity_at_completed_boundary(
    completed_memory_written_state: AutoresearchState,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    changed_readiness = replace(
        platform_readiness,
        manifest_id="manifest-test-2",
        snapshot_id="snapshot-test-2",
    )

    next_iteration = start_next_iteration(
        completed_memory_written_state,
        readiness=changed_readiness,
    )

    assert next_iteration.platform_readiness == changed_readiness.identity()


def test_start_next_rejects_changed_ready_identity_before_completed_boundary(
    policy: AutoresearchPolicy,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    incomplete_state = replace(
        _state_to_decision(policy, platform_readiness),
        final_decision=_final_decision(),
        memory_written=True,
    )
    changed_readiness = replace(
        platform_readiness,
        manifest_id="manifest-test-2",
        snapshot_id="snapshot-test-2",
    )

    with pytest.raises(AutoresearchValidationError, match="completed repeat phase"):
        start_next_iteration(incomplete_state, readiness=changed_readiness)


def test_start_next_rejects_blocked_readiness_at_completed_boundary(
    completed_memory_written_state: AutoresearchState,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    blocked_readiness = replace(
        platform_readiness,
        status=ReadinessStatus.BLOCKED,
        capabilities=None,
        reason="Historical market-data repair is incomplete.",
    )

    with pytest.raises(
        AutoresearchValidationError,
        match="Historical market-data repair is incomplete",
    ):
        start_next_iteration(completed_memory_written_state, readiness=blocked_readiness)


def test_start_next_rejects_invalid_readiness_at_completed_boundary(
    completed_memory_written_state: AutoresearchState,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    evidence_path = Path(platform_readiness.evidence[EvidenceId.QUANTIPY_DATA_CONTRACT].path or "")
    evidence_path.write_text("changed evidence\n", encoding="utf-8")

    with pytest.raises(AutoresearchValidationError, match="SHA-256 mismatch"):
        start_next_iteration(completed_memory_written_state, readiness=platform_readiness)


def test_readiness_identity_without_quantipy_commit_preserves_predecessor_digest() -> None:
    # Arrange
    predecessor = {
        "manifest_id": "manifest-test",
        "receipt_sha256": "a" * 64,
        "snapshot_id": "snapshot-test",
    }
    identity = ReadinessIdentity.from_dict(predecessor)

    # Act
    digest = autoresearch_runner._canonical_json_digest(identity.to_dict())

    # Assert
    assert identity.to_dict() == predecessor
    assert digest == "".join(
        (
            "2011a44bd13263d9",  # pragma: allowlist secret
            "6c6e9fb379885663",  # pragma: allowlist secret
            "02b79ca526d896dd",  # pragma: allowlist secret
            "c0c7290004d46701",  # pragma: allowlist secret
        )
    )


def test_new_readiness_identity_includes_the_pinned_quantipy_commit(
    platform_readiness: PlatformReadinessManifest,
) -> None:
    # Act
    identity = platform_readiness.require_ready()

    # Assert
    assert identity.quantipy_commit == "a" * 40


def test_validation_context_rejects_predecessor_readiness_identity_without_commit() -> None:
    # Arrange
    predecessor = ReadinessIdentity(
        manifest_id="manifest-test",
        snapshot_id="snapshot-test",
        receipt_sha256="a" * 64,
    )
    current = replace(predecessor, quantipy_commit="b" * 40)
    context = AutoresearchValidationContext(
        current,
        "c" * 64,
        (date(2021, 1, 5),),
        quantipy_commit="b" * 40,
    )
    state = AutoresearchState(platform_readiness=predecessor)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must match pinned state"):
        context.validate_for_state(state)


def test_next_action_rejects_predecessor_readiness_identity_without_commit(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    # Arrange
    identity = platform_readiness.identity()
    predecessor = ReadinessIdentity(
        manifest_id=identity.manifest_id,
        snapshot_id=identity.snapshot_id,
        receipt_sha256=identity.receipt_sha256,
    )
    state = AutoresearchState(platform_readiness=predecessor)

    # Act / Assert
    with pytest.raises(AutoresearchValidationError, match="must match pinned state"):
        next_action(state, policy, receipts, platform_readiness)
