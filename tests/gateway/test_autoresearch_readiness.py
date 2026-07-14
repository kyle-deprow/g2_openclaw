from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from gateway.autoresearch_readiness import (
    EvidenceId,
    PlatformReadinessManifest,
    ReadinessBlockedError,
    ReadinessManifestError,
    ReadinessStatus,
    validate_state_readiness,
)
from gateway.autoresearch_runner import (
    AutoresearchState,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    MetricDirection,
    Phase,
    ResearchMode,
    SetupContextArtifact,
    resume_suspended_iteration,
)


def _manifest_payload(
    tmp_path: Path,
    *,
    manifest_id: str = "manifest-1",
    snapshot_id: str = "snapshot-1",
    status: str = "READY",
    reason: str | None = None,
) -> dict[str, object]:
    evidence: dict[str, dict[str, str | None]] = {}
    for evidence_id in EvidenceId:
        path = tmp_path / f"{evidence_id.value}.json"
        path.write_text(f"{evidence_id.value}\n", encoding="utf-8")
        evidence[evidence_id.value] = {
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "reason": None,
        }
    if status == ReadinessStatus.BLOCKED:
        evidence = {
            evidence_id.value: {"path": None, "sha256": None, "reason": "operator input required"}
            for evidence_id in EvidenceId
        }
    return {
        "schema_version": 1,
        "status": status,
        "manifest_id": manifest_id,
        "snapshot_id": snapshot_id,
        "evidence": evidence,
        "reason": reason,
    }


def _ready_manifest(
    tmp_path: Path, *, manifest_id: str = "manifest-1"
) -> PlatformReadinessManifest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return PlatformReadinessManifest.from_dict(_manifest_payload(tmp_path, manifest_id=manifest_id))


def test_ready_manifest_validates_files_and_pins_identity(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)

    assert manifest.status is ReadinessStatus.READY
    identity = manifest.require_ready()
    assert identity.manifest_id == "manifest-1"
    assert identity.snapshot_id == "snapshot-1"
    assert len(identity.receipt_sha256) == 64


def test_direct_manifest_construction_rejects_boolean_schema_version(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)

    with pytest.raises(ReadinessManifestError, match="schema_version"):
        PlatformReadinessManifest(
            schema_version=True,
            status=manifest.status,
            manifest_id=manifest.manifest_id,
            snapshot_id=manifest.snapshot_id,
            evidence=manifest.evidence,
            reason=manifest.reason,
        )


def test_direct_manifest_construction_rejects_non_string_reason(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)

    with pytest.raises(ReadinessManifestError, match="reason"):
        PlatformReadinessManifest(
            schema_version=1,
            status=manifest.status,
            manifest_id=manifest.manifest_id,
            snapshot_id=manifest.snapshot_id,
            evidence=manifest.evidence,
            reason=cast(str | None, object()),
        )


@pytest.mark.parametrize("mutation", ["missing", "mismatch"])
def test_ready_manifest_fails_closed_for_missing_or_mismatched_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _manifest_payload(tmp_path)
    evidence = payload["evidence"]
    assert isinstance(evidence, dict)
    entry = evidence[EvidenceId.SEC_COMMON_STOCK_PROVENANCE.value]
    assert isinstance(entry, dict)
    if mutation == "missing":
        entry["path"] = str(tmp_path / "missing.json")
    else:
        entry["sha256"] = "0" * 64

    with pytest.raises(ReadinessManifestError, match=r"(regular file|mismatch)"):
        PlatformReadinessManifest.from_dict(payload)


def test_blocked_manifest_requires_reason_and_never_becomes_ready(tmp_path: Path) -> None:
    manifest = PlatformReadinessManifest.from_dict(
        _manifest_payload(
            tmp_path,
            status=ReadinessStatus.BLOCKED.value,
            reason="Operator must publish the SEC and XNYS manifests.",
        )
    )

    assert manifest.status is ReadinessStatus.BLOCKED
    with pytest.raises(ReadinessBlockedError, match="Operator must publish"):
        manifest.require_ready()


def test_stale_pinned_snapshot_is_rejected(tmp_path: Path) -> None:
    old_manifest = _ready_manifest(tmp_path / "old")
    new_manifest = _ready_manifest(tmp_path / "new", manifest_id="manifest-2")

    with pytest.raises(ReadinessManifestError, match="stale"):
        validate_state_readiness(old_manifest.identity(), new_manifest)


def test_resume_rechecks_changed_readiness_and_increments_explicitly(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path / "new", manifest_id="manifest-2")
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=7,
        setup=SetupContextArtifact(
            goal="test",
            metric_name="OOS Sharpe net",
            metric_direction=MetricDirection.MAXIMIZE,
            target_repo="/home/dev/repos/quantipy",
            writable_scope="src/quantipy/alpha",
            baseline_summary="0.0",
            hard_constraints=("test",),
            data_sources=("fixture",),
        ),
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-7",
            decision=FinalDecision.INFRA_BLOCKED,
            recommended_metric_name="operator_precondition",
            recommended_metric_value=None,
            reviewer_verdict=FinalReviewerVerdict.NOT_RUN,
            rationale="Evidence is unavailable.",
            log_summary="Suspended.",
            continue_loop=True,
            memory_write_required=False,
            infra_rationale="Operator must publish evidence.",
        ),
        mode=ResearchMode.DATA_INFRA_G0,
        suspended=True,
        suspension_reason="Operator must publish evidence.",
    )

    resumed = resume_suspended_iteration(state, manifest)

    assert resumed.phase is Phase.SETUP_CONTEXT
    assert resumed.iteration == 8
    assert resumed.platform_readiness == manifest.identity()
    assert resumed.suspended is False
    assert resumed.final_decision is None
