"""Tests for gateway.cli — init-env command."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from email.message import Message
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from unittest.mock import MagicMock, patch

import gateway.autoresearch.fields as autoresearch_fields
import gateway.autoresearch.persistence as autoresearch_persistence
import gateway.autoresearch.transitions as autoresearch_transitions
import gateway.cli as cli_module
import pytest
from dotenv import dotenv_values
from gateway.autoresearch import constants
from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact,
    ContextPacketArtifact,
    DebateResultArtifact,
    DebateSubmission,
    FinalDecisionArtifact,
    FixResultArtifact,
    ImplementationResultArtifact,
    PriceHydrationScopePreflight,
    SetupContextArtifact,
    UniversePlanArtifact,
    VerificationResultArtifact,
)
from gateway.autoresearch.compute import (
    ComputeFitArtifact,
)
from gateway.autoresearch.configuration import (
    load_autoresearch_policy,
)
from gateway.autoresearch.constants import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    MEMBER_UNION_DIGEST_ALGORITHM,
)
from gateway.autoresearch.enums import (
    ComputeTarget,
    ConsensusStatus,
    FinalDecision,
    FinalReviewerVerdict,
    FixTriggerPhase,
    MetricDirection,
    Phase,
    ResearchMode,
    VerificationStatus,
)
from gateway.autoresearch.fields import (
    price_hydration_coverage_digest,
    price_hydration_request_digest,
)
from gateway.autoresearch.manifest_runtime import (
    QUANTIPY_RECEIPT_PATHS,
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy,
)
from gateway.autoresearch.receipts import (
    AuthoritativeSnapshotReceipt,
    DynamicUniverseCoverageReceipt,
    GroupedSummaryReceipt,
    MemberUnionManifestReceipt,
    PriceHydrationReceipt,
    UniverseDateVerificationReceipt,
    UniverseHistoryBatchReceipt,
    UniverseVerificationReceipt,
)
from gateway.autoresearch.state import (
    AutoresearchState,
    AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    advance_state,
)
from gateway.autoresearch_checkpoint import (
    RecoveryRecord,
    SupervisorCheckpoint,
)
from gateway.autoresearch_control import (
    ControlStatus,
    TaskStatus,
)
from gateway.autoresearch_platform_validation import (
    DynamicPriceCoverageReceipt,
    PlatformCoverageScope,
    PlatformCoverageStatus,
    canonical_dynamic_price_coverage_digest,
)
from gateway.autoresearch_readiness import (
    PLATFORM_READINESS_SCHEMA_VERSION,
    EvidenceId,
    PlatformReadinessManifest,
    XNYSCalendarEvidence,
    canonical_platform_capabilities,
)
from gateway.autoresearch_shared import AUTORESEARCH_OWNER_SESSION_KEY, RecoveryStatus
from gateway.autoresearch_systemd import SystemdUnitStateError
from gateway.cli import (
    _active_target_writer_processes,
    _choose_whisper_model,
    _detect_gpu,
    _get_local_ip,
    _openclaw_daemon_env,
    _OperatorCommandError,
    _parse_gpu_output,
    _PartialArchiveError,
    _read_openclaw_config,
    _render_env,
    _require_simulator_backend,
    _require_simulator_still_running,
    _reset_owner_session,
    _ResolvedOpenClaw,
    _signal_process_group,
    _simulator_launch_command,
    _SimulatorLaunchError,
    _vite_health_check,
    _vite_launch_command,
    app,
)
from gateway.deployment.appserver_probe import AppServerProbeResult
from typer.testing import CliRunner

from tests.gateway.autoresearch_fixtures import (
    write_xnys_calendar_evidence,
    xnys_calendar_payload,
)


def _fixture_xnys_session_count(start: date, end: date) -> int:
    evidence = XNYSCalendarEvidence.from_dict(xnys_calendar_payload())
    return sum(start <= session <= end for session in evidence.sessions)


runner = CliRunner()
CAMPAIGN_XNYS_START = "2022-01-03"
CAMPAIGN_XNYS_END = "2025-12-31"
_MEMBER_UNION_PATH = Path("tests/fixtures/autoresearch-member-union.txt").resolve()
_MEMBER_UNION_SYMBOLS = _MEMBER_UNION_PATH.read_text(encoding="utf-8").splitlines()
_MEMBER_UNION_COUNT, _MEMBER_UNION_DIGEST = autoresearch_fields.canonical_member_union_digest(
    _MEMBER_UNION_SYMBOLS
)
_MEMBER_UNION_SHA256 = sha256(_MEMBER_UNION_PATH.read_bytes()).hexdigest()


def test_platform_runtime_recovery_requires_its_own_operator_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("G2_OPENCLAW_OPERATOR_PLATFORM_RUNTIME_RECOVERY", raising=False)

    # Act
    result = runner.invoke(
        app,
        [
            "autoresearch-recover-platform-runtime",
            str(state_path),
            "--reason",
            "Re-attest canonical runtime.",
        ],
    )

    # Assert
    assert result.exit_code == 1
    assert "G2_OPENCLAW_OPERATOR_PLATFORM_RUNTIME_RECOVERY=1" in result.output


@pytest.fixture(autouse=True)
def isolated_autoresearch_lock_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        constants,
        "AUTORESEARCH_LOCK_NAMESPACE",
        tmp_path / "autoresearch-locks",
    )


@pytest.fixture
def isolated_appserver_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep doctor tests independent of live writable-root paths and /proc."""

    monkeypatch.setattr(
        cli_module,
        "probe_appserver",
        lambda: AppServerProbeResult((), ()),
    )


def _health_response(*, content_type: str, body: bytes, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.headers = Message()
    response.headers["Content-Type"] = content_type
    response.read.return_value = body
    return response


def test_readme_simulator_cleanup_guidance_is_process_scoped() -> None:
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")

    assert "Kill all" not in readme
    assert "| `make sim` | Restart the project-owned" in readme
    assert "| `make stop` | Stop project-owned" in readme


def _universe_plan() -> UniversePlanArtifact:
    return UniversePlanArtifact(
        profile_id="liquid-common-stocks-v1",
        profile_digest="a" * 64,
        selection_dates=("2021-01-04",),
        max_members_per_date=300,
        execution_policy="next-session-or-later",
    )


def _universe_receipt() -> UniverseVerificationReceipt:
    return UniverseVerificationReceipt(
        profile_id="liquid-common-stocks-v1",
        profile_digest="a" * 64,
        execution_policy="next-session-or-later",
        max_members_per_date=300,
        batches=(
            UniverseHistoryBatchReceipt(
                contract_digest="b" * 64,
                operation_count=1,
                dates=(
                    UniverseDateVerificationReceipt(
                        selection_date="2021-01-04",
                        earliest_execution_date="2021-01-05",
                        calendar_identity="XNYS",
                        calendar_digest="f" * 64,
                        selected_member_count=_MEMBER_UNION_COUNT,
                        snapshot=AuthoritativeSnapshotReceipt(
                            as_of_date="2021-01-04",
                            source="massive",
                            result_count=17,
                            identity_digest="c" * 64,
                            content_digest="c" * 64,
                            completed_at="2026-07-15T12:00:00+00:00",
                        ),
                        summary=GroupedSummaryReceipt(
                            summary_date="2021-01-04",
                            source="massive",
                            result_count=17,
                            identity_digest="d" * 64,
                            content_digest="d" * 64,
                            completed_at="2026-07-15T12:00:00+00:00",
                            adjusted=False,
                        ),
                    ),
                ),
            ),
        ),
        member_union_digest_algorithm=MEMBER_UNION_DIGEST_ALGORITHM,
        member_union_count=_MEMBER_UNION_COUNT,
        member_union_digest=_MEMBER_UNION_DIGEST,
        member_union_manifest=MemberUnionManifestReceipt(
            path=str(_MEMBER_UNION_PATH), sha256=_MEMBER_UNION_SHA256
        ),
    )


def _hydration_receipt() -> PriceHydrationReceipt:
    request_digest = price_hydration_request_digest(
        member_union_count=_MEMBER_UNION_COUNT,
        member_union_digest=_MEMBER_UNION_DIGEST,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
    )
    completed_at = "2026-07-15T12:00:00+00:00"
    return PriceHydrationReceipt(
        member_union_count=_MEMBER_UNION_COUNT,
        member_union_digest=_MEMBER_UNION_DIGEST,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
        operation_count=1,
        request_digest=request_digest,
        coverage_receipt_digest=price_hydration_coverage_digest(
            request_digest=request_digest, operation_count=1, completed_at=completed_at
        ),
        source_price_coverage_response_digest="d" * 64,
        completed_at=completed_at,
        folds_started_at="2026-07-15T12:01:00+00:00",
    )


def _dynamic_coverage() -> DynamicUniverseCoverageReceipt:
    return DynamicUniverseCoverageReceipt(
        member_union_count=_MEMBER_UNION_COUNT,
        member_union_digest=_MEMBER_UNION_DIGEST,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        oos_start="2021-10-01",
        oos_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
        expected_symbol_sessions=252,
        covered_symbol_sessions=252,
        missing_symbol_count=0,
        missing_symbol_sessions=0,
        default_fold_count=24,
        fallback_fold_count=0,
    )


def _platform_coverage_receipt(
    *,
    context: AutoresearchValidationContext,
    preflight: PriceHydrationScopePreflight,
) -> DynamicPriceCoverageReceipt:
    requested_sessions = tuple(
        session
        for session in context.xnys_sessions
        if date.fromisoformat(preflight.experiment_start)
        <= session
        <= date.fromisoformat(preflight.experiment_end)
    )
    raw: dict[str, object] = {
        "contract_version": "dynamic-price-coverage-v1",
        "source_contract_version": "price-coverage-v1",
        "scope": PlatformCoverageScope.FULL_UNION_HYDRATION.value,
        "status": PlatformCoverageStatus.COMPLETE.value,
        "requested_start_date": preflight.experiment_start,
        "requested_end_date": preflight.experiment_end,
        "timeframe": preflight.timeframe,
        "market_hours": preflight.market_hours,
        "source_requested_start_date": preflight.experiment_start,
        "source_requested_end_date": preflight.experiment_end,
        "source_timeframe": preflight.timeframe,
        "source_market_hours": preflight.market_hours,
        "source_provider": "massive",
        "member_union_digest": autoresearch_fields.quantipy_member_union_digest(
            _MEMBER_UNION_SYMBOLS
        )[1],
        "requested_sessions_digest": autoresearch_fields.platform_requested_sessions_digest(
            requested_sessions
        ),
        "pit_active_roster_digest": "c" * 64,
        "source_price_coverage_response_digest": (
            _hydration_receipt().source_price_coverage_response_digest
        ),
        "member_union_count": preflight.member_union_count,
        "requested_session_count": preflight.session_count,
        "hydrated_symbol_sessions": preflight.planned_symbol_sessions,
        "observed_hydrated_symbol_sessions": preflight.planned_symbol_sessions,
        "provider_empty_hydrated_symbol_sessions": 0,
        "missing_hydrated_symbol_sessions": 0,
        "active_symbol_sessions": preflight.planned_symbol_sessions,
        "observed_active_symbol_sessions": preflight.planned_symbol_sessions,
        "provider_empty_active_symbol_sessions": 0,
        "missing_active_symbol_sessions": 0,
        "inactive_union_symbol_sessions": 0,
        "unexpected_ticker_count": 0,
        "unexpected_session_count": 0,
        "violation_codes": [],
    }
    raw["receipt_digest"] = canonical_dynamic_price_coverage_digest(raw)
    return DynamicPriceCoverageReceipt.from_dict(raw)


def _bind_universe_receipt_to_validation_context(
    receipt: UniverseVerificationReceipt,
    context: AutoresearchValidationContext,
) -> UniverseVerificationReceipt:
    return replace(
        receipt,
        batches=tuple(
            replace(
                batch,
                dates=tuple(
                    replace(date_receipt, calendar_digest=context.xnys_evidence_digest)
                    for date_receipt in batch.dates
                ),
            )
            for batch in receipt.batches
        ),
    )


def test_autoresearch_init_state_pins_readiness(tmp_path: Path) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    output = tmp_path / "pristine-v6.json"
    runs_root = tmp_path / "openclaw" / "autoresearch" / "quantipy-experiment-runs"
    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(output),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "state v6" in result.output
    state = AutoresearchState.from_dict(json.loads(output.read_text(encoding="utf-8")))
    assert state.to_dict()["schema_version"] == 6
    assert state.platform_readiness == readiness.identity()
    assert runs_root.is_dir()
    assert runs_root.stat().st_mode & 0o777 == 0o700


def test_autoresearch_init_state_normalizes_user_owned_control_plane_ancestors(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    openclaw_root = tmp_path / "openclaw"
    openclaw_root.mkdir(mode=0o755)
    openclaw_root.chmod(0o755)
    runs_parent = openclaw_root / "autoresearch"
    runs_parent.mkdir(mode=0o775)
    runs_parent.chmod(0o775)
    runs_root = runs_parent / "quantipy-experiment-runs"

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert runs_parent.stat().st_mode & 0o777 == 0o700
    assert openclaw_root.stat().st_mode & 0o777 == 0o700


def test_autoresearch_init_state_securely_creates_control_plane_ancestors_with_umask_zero(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    openclaw_root = tmp_path / "openclaw"
    runs_parent = openclaw_root / "autoresearch"
    runs_root = runs_parent / "quantipy-experiment-runs"

    original_umask = os.umask(0)
    try:
        with (
            patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
        ):
            result = runner.invoke(
                app,
                [
                    "autoresearch-init-state",
                    "--output",
                    str(tmp_path / "state.json"),
                    "--readiness-manifest",
                    str(readiness_path),
                ],
            )
    finally:
        os.umask(original_umask)

    assert result.exit_code == 0, result.output
    assert openclaw_root.stat().st_mode & 0o777 == 0o700
    assert runs_parent.stat().st_mode & 0o777 == 0o700


def test_autoresearch_init_state_rejects_untrusted_control_plane_root_before_creating_leaf(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    runs_parent = tmp_path / "openclaw" / "autoresearch"
    runs_parent.mkdir(parents=True, mode=0o700)
    runs_parent.chmod(0o700)
    runs_root = runs_parent / "quantipy-experiment-runs"

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
        patch(
            "gateway.autoresearch.persistence.os.getuid",
            return_value=os.getuid() + 1,
        ),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 1
    assert "control-plane root" in result.output
    assert not runs_root.exists()


def test_autoresearch_init_state_help_names_schema_v6() -> None:
    result = runner.invoke(app, ["autoresearch-init-state", "--help"])

    assert result.exit_code == 0, result.output
    assert "schema-v6" in result.output
    assert "schema-v3" not in result.output


def test_autoresearch_init_state_rejects_existing_nonprivate_quantipy_runs_root(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    runs_root = tmp_path / "openclaw" / "autoresearch" / "quantipy-experiment-runs"
    runs_root.mkdir(parents=True, mode=0o755)
    runs_root.chmod(0o755)

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 1
    assert "mode-0700" in result.output


def test_autoresearch_init_state_rejects_symlink_quantipy_runs_root(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    private_target = tmp_path / "private-target"
    private_target.mkdir(mode=0o700)
    runs_root = tmp_path / "quantipy-experiment-runs"
    runs_root.symlink_to(private_target, target_is_directory=True)

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 1
    assert "non-symlink directory" in result.output


def test_autoresearch_init_state_rejects_symlinked_quantipy_runs_parent(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    private_target = tmp_path / "private-target"
    private_target.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(private_target, target_is_directory=True)
    runs_root = linked_parent / "quantipy-experiment-runs"

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 1
    assert "non-symlink directory" in result.output
    assert not (private_target / "quantipy-experiment-runs").exists()


def test_autoresearch_init_state_rejects_wrong_owner_quantipy_runs_root(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    runs_root = tmp_path / "quantipy-experiment-runs"
    runs_root.mkdir(mode=0o700)

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root),
        patch(
            "gateway.autoresearch.persistence.os.getuid",
            return_value=os.getuid() + 1,
        ),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 1
    assert "owned" in result.output
    assert "non-symlink directory" in result.output


def _doctor_control_status(*, last_cycle_at: float | None) -> ControlStatus:
    return ControlStatus(
        owner_agent_id="autoresearch-pm",
        owner_session_key=AUTORESEARCH_OWNER_SESSION_KEY,
        phase="setup_context",
        iteration=1,
        owner_lifecycle_status="idle",
        supervisor_active=True,
        tasks=(TaskStatus("task-1", "autoresearch-pm", AUTORESEARCH_OWNER_SESSION_KEY),),
        supervisor_last_outcome="no_action",
        supervisor_last_detail="healthy",
        supervisor_last_cycle_at=last_cycle_at,
    )


def test_autoresearch_doctor_reports_healthy_status_and_d1_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appserver_probe: None,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    sessions_path = tmp_path / "sessions.json"
    state_path.write_text(json.dumps(AutoresearchState().to_dict()), encoding="utf-8")
    now = time.time()
    SupervisorCheckpoint(
        recovery_records={"recovery-key": RecoveryRecord(last_nudge_at=now)},
        last_cycle_at=now,
    ).save(checkpoint_path)
    sessions_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: True)
    status = _doctor_control_status(last_cycle_at=now)

    with patch("gateway.autoresearch_control.AutoresearchControl.status", return_value=status):
        result = runner.invoke(app, ["autoresearch-doctor"])

    assert result.exit_code == 0, result.output
    assert "health=HEALTHY" in result.output
    assert "owner_lifecycle=idle" in result.output
    assert "task_count=1" in result.output
    assert "supervisor_last_outcome=no_action" in result.output
    assert "recovery_records=1" in result.output
    assert "alerted_keys=none" in result.output


def test_autoresearch_doctor_reports_degraded_checks_and_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appserver_probe: None,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    state = replace(
        AutoresearchState(),
        suspended=True,
        suspension_reason="operator repair",
        campaign_review_required=True,
        campaign_review_reason="review me",
    )
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    SupervisorCheckpoint(
        recovery_records={"alerted-key": RecoveryRecord(alerted=True)},
    ).save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", tmp_path / "sessions.json"
    )
    monkeypatch.setattr(
        cli_module,
        "_is_systemd_unit_active",
        lambda unit: unit == cli_module.DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE,
    )
    status = _doctor_control_status(last_cycle_at=0.0)

    with patch("gateway.autoresearch_control.AutoresearchControl.status", return_value=status):
        result = runner.invoke(app, ["autoresearch-doctor"])

    assert result.exit_code == 1, result.output
    assert "INACTIVE" in result.output
    assert "suspended=True" in result.output
    assert "campaign_review=True" in result.output
    assert "alerted_keys=alerted-key" in result.output
    assert "health=DEGRADED" in result.output


def test_autoresearch_doctor_corrupt_state_is_clear_and_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appserver_probe: None,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    state_path.write_text("{not-json", encoding="utf-8")
    SupervisorCheckpoint(last_cycle_at=time.time()).save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", tmp_path / "sessions.json"
    )
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: True)

    with patch(
        "gateway.autoresearch_control.AutoresearchControl.status",
        return_value=_doctor_control_status(last_cycle_at=time.time()),
    ):
        result = runner.invoke(app, ["autoresearch-doctor"])

    assert result.exit_code == 1, result.output
    assert "ERROR:" in result.output
    assert "invalid state JSON" in result.output
    assert "Traceback" not in result.output


def test_autoresearch_doctor_marks_stale_cycle_degraded_when_services_are_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appserver_probe: None,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    state_path.write_text(json.dumps(AutoresearchState().to_dict()), encoding="utf-8")
    SupervisorCheckpoint(last_cycle_at=0.0).save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: True)

    with patch(
        "gateway.autoresearch_control.AutoresearchControl.status",
        return_value=_doctor_control_status(last_cycle_at=0.0),
    ):
        result = runner.invoke(app, ["autoresearch-doctor"])

    assert result.exit_code == 1, result.output
    assert "older than 10 minutes" in result.output


def test_autoresearch_doctor_reports_systemd_probe_error_distinctly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appserver_probe: None,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    state_path.write_text(json.dumps(AutoresearchState().to_dict()), encoding="utf-8")
    SupervisorCheckpoint(last_cycle_at=time.time()).save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)

    def probe(unit: str) -> bool:
        if unit == cli_module.DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE:
            raise SystemdUnitStateError("inconclusive systemd evidence [probe]")
        return True

    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", probe)
    with patch(
        "gateway.autoresearch_control.AutoresearchControl.status",
        return_value=_doctor_control_status(last_cycle_at=time.time()),
    ):
        result = runner.invoke(app, ["autoresearch-doctor"])

    assert result.exit_code == 1, result.output
    assert "probe-error" in result.output
    assert "[probe]" in result.output
    assert "supervisor_active=False" not in result.output


def test_autoresearch_doctor_reports_appserver_probe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    state_path.write_text(json.dumps(AutoresearchState().to_dict()), encoding="utf-8")
    SupervisorCheckpoint(last_cycle_at=time.time()).save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: True)

    def fail_probe() -> AppServerProbeResult:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(cli_module, "probe_appserver", fail_probe)

    with patch(
        "gateway.autoresearch_control.AutoresearchControl.status",
        return_value=_doctor_control_status(last_cycle_at=time.time()),
    ):
        result = runner.invoke(app, ["autoresearch-doctor"])

    assert result.exit_code == 1, result.output
    assert "app-server probe-error" in result.output
    assert "probe failed" in result.output


def test_autoresearch_reset_owner_session_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps({AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)

    result = runner.invoke(app, ["autoresearch-reset-owner-session"])

    assert result.exit_code == 1, result.output
    assert "rerun with --confirm" in result.output
    assert json.loads(sessions_path.read_text(encoding="utf-8")) == {
        AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner"}
    }


def test_autoresearch_reset_owner_session_confirmed_reports_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps({AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    result = runner.invoke(app, ["autoresearch-reset-owner-session", "--confirm"])

    assert result.exit_code == 0, result.output
    assert "sessionId=stale-owner" in result.output
    assert "backup:" in result.output
    assert AUTORESEARCH_OWNER_SESSION_KEY not in json.loads(
        sessions_path.read_text(encoding="utf-8")
    )
    assert len(list(tmp_path.glob("sessions.json.pre-reset-*"))) == 1
    assert not sessions_path.with_name("sessions.json.tmp").exists()


def _assert_autoresearch_reset_owner_session_refuses_active_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_unit: str,
) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps({AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(
        cli_module,
        "_is_systemd_unit_active",
        lambda unit: unit == active_unit,
    )

    result = runner.invoke(app, ["autoresearch-reset-owner-session", "--confirm"])

    assert result.exit_code == 1, result.output
    assert "stop it first" in result.output
    assert json.loads(sessions_path.read_text(encoding="utf-8"))
    assert not list(tmp_path.glob("sessions.json.pre-reset-*"))


@pytest.mark.parametrize(
    "active_unit",
    [
        cli_module.DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE,
        cli_module.DEFAULT_OPENCLAW_GATEWAY_SERVICE,
    ],
)
def test_autoresearch_reset_owner_session_refuses_active_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_unit: str,
) -> None:
    _assert_autoresearch_reset_owner_session_refuses_active_service(
        tmp_path,
        monkeypatch,
        active_unit,
    )


def test_autoresearch_reset_owner_session_fails_closed_on_systemd_probe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps({AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(
        cli_module,
        "_is_systemd_unit_active",
        lambda _unit: (_ for _ in ()).throw(SystemdUnitStateError("inconclusive")),
    )

    result = runner.invoke(app, ["autoresearch-reset-owner-session", "--confirm"])

    assert result.exit_code == 1, result.output
    assert "cannot prove services are inactive" in result.output
    assert not list(tmp_path.glob("sessions.json.pre-reset-*"))


def test_reset_owner_session_removes_key_and_writes_backup_atomically(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    original = {
        AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner", "updatedAt": 1},
        "other:key": {"sessionId": "keep-me"},
    }
    sessions_path.write_text(json.dumps(original), encoding="utf-8")
    sessions_path.chmod(0o640)

    result = _reset_owner_session(
        sessions_path,
        AUTORESEARCH_OWNER_SESSION_KEY,
        confirm=True,
    )

    assert result.changed is True
    assert result.session_id == "stale-owner"
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("sessions.json.pre-reset-")
    assert json.loads(result.backup_path.read_text(encoding="utf-8")) == original
    assert json.loads(sessions_path.read_text(encoding="utf-8")) == {
        "other:key": {"sessionId": "keep-me"}
    }
    assert sessions_path.stat().st_mode & 0o777 == 0o640
    assert not sessions_path.with_name("sessions.json.tmp").exists()


def test_reset_owner_session_without_key_is_successful_noop(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(json.dumps({"other:key": {"sessionId": "keep-me"}}), encoding="utf-8")

    result = _reset_owner_session(
        sessions_path,
        AUTORESEARCH_OWNER_SESSION_KEY,
        confirm=False,
    )

    assert result.key_present is False
    assert result.changed is False
    assert result.backup_path is None
    assert not list(tmp_path.glob("sessions.json.pre-reset-*"))


def test_reset_owner_session_without_confirmation_does_not_mutate(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    original = {AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "stale-owner"}}
    sessions_path.write_text(json.dumps(original), encoding="utf-8")

    result = _reset_owner_session(
        sessions_path,
        AUTORESEARCH_OWNER_SESSION_KEY,
        confirm=False,
    )

    assert result.key_present is True
    assert result.changed is False
    assert json.loads(sessions_path.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob("sessions.json.pre-reset-*"))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "not a JSON object"),
        ({AUTORESEARCH_OWNER_SESSION_KEY: []}, "not an object"),
        ({AUTORESEARCH_OWNER_SESSION_KEY: {}}, "no usable sessionId"),
    ],
)
def test_reset_owner_session_rejects_malformed_store_entries(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(_OperatorCommandError, match=message):
        _reset_owner_session(
            sessions_path,
            AUTORESEARCH_OWNER_SESSION_KEY,
            confirm=True,
        )


def test_reset_owner_session_reports_missing_store(tmp_path: Path) -> None:
    with pytest.raises(_OperatorCommandError, match="owner sessions store is missing"):
        _reset_owner_session(
            tmp_path / "sessions.json",
            AUTORESEARCH_OWNER_SESSION_KEY,
            confirm=True,
        )


def test_autoresearch_reset_owner_session_missing_store_is_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_path = tmp_path / "sessions.json"
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)

    result = runner.invoke(app, ["autoresearch-reset-owner-session", "--confirm"])

    assert result.exit_code == 1, result.output
    assert "owner sessions store is missing" in result.output


def test_autoresearch_clear_exhausted_recovery_removes_only_exhausted_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    checkpoint_path = tmp_path / "owner-recovery.json"
    SupervisorCheckpoint(
        recovery_records={
            "exhausted-key": RecoveryRecord(status=RecoveryStatus.EXHAUSTED),
            "failed-key": RecoveryRecord(status=RecoveryStatus.FAILED),
            "ready-key": RecoveryRecord(status=RecoveryStatus.READY),
        }
    ).save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    # Act
    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery", "--confirm"])

    # Assert
    assert result.exit_code == 0, result.output
    assert set(SupervisorCheckpoint.load(checkpoint_path).recovery_records) == {
        "failed-key",
        "ready-key",
    }
    assert "exhausted-key" in result.output


def test_autoresearch_clear_exhausted_recovery_writes_timestamped_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    checkpoint_path = tmp_path / "owner-recovery.json"
    original = SupervisorCheckpoint(
        recovery_records={"exhausted-key": RecoveryRecord(status=RecoveryStatus.EXHAUSTED)}
    )
    original.save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    # Act
    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery", "--confirm"])

    # Assert
    assert result.exit_code == 0, result.output
    backup_paths = list(tmp_path.glob("owner-recovery.json.pre-clear-exhausted-*"))
    assert len(backup_paths) == 1
    assert SupervisorCheckpoint.load(backup_paths[0]) == original


def test_autoresearch_clear_exhausted_recovery_without_exhausted_records_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    checkpoint_path = tmp_path / "owner-recovery.json"
    original = SupervisorCheckpoint(
        recovery_records={"ready-key": RecoveryRecord(status=RecoveryStatus.READY)}
    )
    original.save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)

    # Act
    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery", "--confirm"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "no exhausted recovery records; no action" in result.output
    assert SupervisorCheckpoint.load(checkpoint_path) == original
    assert not list(tmp_path.glob("owner-recovery.json.pre-clear-exhausted-*"))


def test_autoresearch_clear_exhausted_recovery_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    checkpoint_path = tmp_path / "owner-recovery.json"
    original = SupervisorCheckpoint(
        recovery_records={"exhausted-key": RecoveryRecord(status=RecoveryStatus.EXHAUSTED)}
    )
    original.save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)

    # Act
    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery"])

    # Assert
    assert result.exit_code == 1, result.output
    assert "rerun with --confirm" in result.output
    assert SupervisorCheckpoint.load(checkpoint_path) == original
    assert not list(tmp_path.glob("owner-recovery.json.pre-clear-exhausted-*"))


def _assert_clear_exhausted_recovery_refuses_active_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_unit: str,
) -> None:
    checkpoint_path = tmp_path / "owner-recovery.json"
    original = SupervisorCheckpoint(
        recovery_records={"exhausted-key": RecoveryRecord(status=RecoveryStatus.EXHAUSTED)}
    )
    original.save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(
        cli_module,
        "_is_systemd_unit_active",
        lambda unit: unit == active_unit,
    )

    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery", "--confirm"])
    output = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert "refusing autoresearch-clear-exhausted-recovery while" in output
    assert "refusing owner-session reset while" not in output
    assert SupervisorCheckpoint.load(checkpoint_path) == original
    assert not list(tmp_path.glob("owner-recovery.json.pre-clear-exhausted-*"))


@pytest.mark.parametrize(
    "active_unit",
    [
        cli_module.DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE,
        cli_module.DEFAULT_OPENCLAW_GATEWAY_SERVICE,
    ],
)
def test_autoresearch_clear_exhausted_recovery_refuses_active_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_unit: str,
) -> None:
    _assert_clear_exhausted_recovery_refuses_active_service(tmp_path, monkeypatch, active_unit)


def test_autoresearch_clear_exhausted_recovery_fails_closed_on_systemd_probe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_path = tmp_path / "owner-recovery.json"
    original = SupervisorCheckpoint(
        recovery_records={"exhausted-key": RecoveryRecord(status=RecoveryStatus.EXHAUSTED)}
    )
    original.save(checkpoint_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(
        cli_module,
        "_is_systemd_unit_active",
        lambda _unit: (_ for _ in ()).throw(SystemdUnitStateError("inconclusive")),
    )

    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery", "--confirm"])
    output = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert "cannot prove services are inactive" in output
    assert SupervisorCheckpoint.load(checkpoint_path) == original
    assert not list(tmp_path.glob("owner-recovery.json.pre-clear-exhausted-*"))


def test_autoresearch_clear_exhausted_recovery_refuses_symlinked_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "real-owner-recovery.json"
    SupervisorCheckpoint(
        recovery_records={"exhausted-key": RecoveryRecord(status=RecoveryStatus.EXHAUSTED)}
    ).save(target)
    checkpoint_path = tmp_path / "owner-recovery.json"
    checkpoint_path.symlink_to(target)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint_path)

    result = runner.invoke(app, ["autoresearch-clear-exhausted-recovery", "--confirm"])
    output = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert "refusing symlinked recovery checkpoint" in output
    assert SupervisorCheckpoint.load(target).recovery_records
    assert not list(tmp_path.glob("owner-recovery.json.pre-clear-exhausted-*"))


def test_autoresearch_init_state_fresh_campaign_archives_residue_and_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    artifacts = autoresearch_dir / "artifacts"
    stage_inbox = autoresearch_dir / "stage-inbox"
    checkpoint = autoresearch_dir / "owner-recovery.json"
    state_path = autoresearch_dir / "quantipy-state.json"
    sessions_path = tmp_path / "agent" / "sessions" / "sessions.json"
    session_file = sessions_path.parent / "ses-owner.jsonl"
    artifacts.mkdir(parents=True)
    stage_inbox.mkdir(parents=True)
    (artifacts / "old.json").write_text("{}", encoding="utf-8")
    (stage_inbox / "submission.json").write_text("{}", encoding="utf-8")
    SupervisorCheckpoint().save(checkpoint)
    state_path.write_text(json.dumps({"prior_campaign": True}), encoding="utf-8")
    sessions_path.parent.mkdir(parents=True)
    sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "ses-owner"},
                "other:key": {"sessionId": "ses-other"},
            }
        ),
        encoding="utf-8",
    )
    session_file.write_text("owner transcript\n", encoding="utf-8")
    output = tmp_path / "pristine-v5.json"
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_ARTIFACTS_PATH", artifacts)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH", stage_inbox)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    with patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", runs_root):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(output),
                "--readiness-manifest",
                str(readiness_path),
                "--fresh-campaign",
            ],
        )

    assert result.exit_code == 0, result.output
    archive_paths = sorted((autoresearch_dir / "campaign-archives").iterdir())
    assert len(archive_paths) == 1
    archive = archive_paths[0]
    assert (archive / "artifacts/old.json").is_file()
    assert (archive / "stage-inbox/submission.json").is_file()
    assert (archive / "owner-recovery.json").is_file()
    assert json.loads((archive / "quantipy-state.json").read_text(encoding="utf-8")) == {
        "prior_campaign": True
    }
    assert (archive / "sessions/ses-owner.jsonl").is_file()
    assert json.loads((archive / "sessions.json").read_text(encoding="utf-8")) == {
        AUTORESEARCH_OWNER_SESSION_KEY: {"sessionId": "ses-owner"}
    }
    assert not artifacts.exists()
    assert not stage_inbox.exists()
    assert not checkpoint.exists()
    assert not state_path.exists()
    assert not session_file.exists()
    assert AUTORESEARCH_OWNER_SESSION_KEY not in json.loads(
        sessions_path.read_text(encoding="utf-8")
    )
    assert "archived campaign residue" in result.output


def test_autoresearch_init_state_save_failure_restores_archived_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    artifacts = autoresearch_dir / "artifacts"
    stage_inbox = autoresearch_dir / "stage-inbox"
    checkpoint = autoresearch_dir / "owner-recovery.json"
    state_path = autoresearch_dir / "quantipy-state.json"
    sessions_path = tmp_path / "sessions.json"
    artifacts.mkdir(parents=True)
    stage_inbox.mkdir(parents=True)
    (artifacts / "old.json").write_text("{}", encoding="utf-8")
    (stage_inbox / "old.json").write_text("{}", encoding="utf-8")
    SupervisorCheckpoint().save(checkpoint)
    state_path.write_text(json.dumps({"prior_campaign": True}), encoding="utf-8")
    output = tmp_path / "new-state.json"
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_ARTIFACTS_PATH", artifacts)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH", stage_inbox)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", tmp_path / "runs"),
        patch(
            "gateway.autoresearch.persistence.save_state_file",
            side_effect=OSError("injected state-save failure"),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(output),
                "--readiness-manifest",
                str(readiness_path),
                "--fresh-campaign",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "fresh campaign state save failed" in result.output
    assert "PARTIAL ARCHIVE" not in result.output
    assert artifacts.is_dir()
    assert stage_inbox.is_dir()
    assert checkpoint.is_file()
    assert state_path.is_file()
    assert not list((autoresearch_dir / "campaign-archives").glob("campaign-*"))


def test_autoresearch_init_state_preparation_failure_occurs_before_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    artifacts = autoresearch_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "old.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_ARTIFACTS_PATH", artifacts)
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH", autoresearch_dir / "stage-inbox"
    )
    monkeypatch.setattr(
        cli_module,
        "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH",
        autoresearch_dir / "owner-recovery.json",
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", autoresearch_dir / "quantipy-state.json"
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", tmp_path / "sessions.json"
    )
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    with patch(
        "gateway.autoresearch.persistence.provision_quantipy_experiment_runs_root",
        side_effect=OSError("injected preparation failure"),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
                "--fresh-campaign",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "injected preparation failure" in result.output
    assert artifacts.is_dir()
    assert not (autoresearch_dir / "campaign-archives").exists()


def test_autoresearch_init_state_reports_partial_archive_with_stranded_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    artifacts = autoresearch_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "old.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_ARTIFACTS_PATH", artifacts)
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH", autoresearch_dir / "stage-inbox"
    )
    monkeypatch.setattr(
        cli_module,
        "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH",
        autoresearch_dir / "owner-recovery.json",
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", autoresearch_dir / "quantipy-state.json"
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", tmp_path / "sessions.json"
    )
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)
    archive_failure = _PartialArchiveError(
        [
            f"{autoresearch_dir}/campaign-archives/campaign-1/artifacts -> {artifacts}",
            f"{autoresearch_dir}/campaign-archives/campaign-1/quantipy-state.json -> "
            f"{autoresearch_dir}/quantipy-state.json",
        ]
    )

    with (
        patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", tmp_path / "runs"),
        patch(
            "gateway.autoresearch.persistence.save_state_file",
            side_effect=OSError("injected state-save failure"),
        ),
        patch.object(cli_module, "_restore_campaign_archive", side_effect=archive_failure),
    ):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
                "--fresh-campaign",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "PARTIAL ARCHIVE" in result.output
    assert "campaign-1/artifacts" in result.output
    assert "campaign-1/quantipy-state.json" in result.output
    assert "fresh campaign state save failed" not in result.output


def test_autoresearch_init_state_fresh_campaign_notes_missing_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    sessions_path = tmp_path / "sessions.json"
    output = tmp_path / "state.json"
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_ARTIFACTS_PATH", autoresearch_dir / "artifacts"
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH", autoresearch_dir / "stage-inbox"
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", autoresearch_dir / "owner-recovery.json"
    )
    monkeypatch.setattr(
        cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", autoresearch_dir / "quantipy-state.json"
    )
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    with patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", tmp_path / "runs"):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(output),
                "--readiness-manifest",
                str(readiness_path),
                "--fresh-campaign",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "note: missing" in result.output
    assert output.is_file()


def test_autoresearch_init_state_fresh_campaign_refuses_active_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    artifacts = autoresearch_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "old.json").write_text("{}", encoding="utf-8")
    stage_inbox = autoresearch_dir / "stage-inbox"
    checkpoint = autoresearch_dir / "owner-recovery.json"
    state_path = autoresearch_dir / "quantipy-state.json"
    sessions_path = tmp_path / "agent" / "sessions" / "sessions.json"
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_ARTIFACTS_PATH", artifacts)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH", stage_inbox)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_CHECKPOINT_PATH", checkpoint)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: True)
    output = tmp_path / "state.json"

    result = runner.invoke(
        app,
        [
            "autoresearch-init-state",
            "--output",
            str(output),
            "--readiness-manifest",
            str(readiness_path),
            "--fresh-campaign",
        ],
    )

    assert result.exit_code == 1
    assert "is active; stop it first" in result.output
    assert artifacts.is_dir()
    assert not output.exists()
    assert not (autoresearch_dir / "campaign-archives").exists()


def test_autoresearch_init_state_without_fresh_flag_does_not_probe_or_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    autoresearch_dir = tmp_path / "autoresearch"
    monkeypatch.setattr(cli_module, "DEFAULT_AUTORESEARCH_DIR", autoresearch_dir)
    monkeypatch.setattr(
        cli_module,
        "_is_systemd_unit_active",
        lambda _unit: (_ for _ in ()).throw(AssertionError("plain init must not probe systemd")),
    )

    with patch.object(constants, "DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT", tmp_path / "runs"):
        result = runner.invoke(
            app,
            [
                "autoresearch-init-state",
                "--output",
                str(tmp_path / "state.json"),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "state v6" in result.output
    assert not (autoresearch_dir / "campaign-archives").exists()


def _write_config_health_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE config_health_entries (config_path TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO config_health_entries(config_path) VALUES (?)",
            [
                ("/home/dev/.openclaw/openclaw.json",),
                ("/home/dev/.openclaw/other.json",),
            ],
        )


def _write_wal_config_health_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=1000000")
    connection.execute("CREATE TABLE config_health_entries (config_path TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO config_health_entries(config_path) VALUES (?)",
        ("/home/dev/.openclaw/openclaw.json",),
    )
    connection.commit()
    return connection


def test_deployment_rebaseline_config_health_backs_up_and_deletes_exact_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "state" / "openclaw.sqlite"
    _write_config_health_db(database_path)
    monkeypatch.setattr(cli_module, "DEFAULT_OPENCLAW_STATE_DB_PATH", database_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    result = runner.invoke(app, ["deployment-rebaseline-config-health"])

    assert result.exit_code == 0, result.output
    backups = list(database_path.parent.glob("openclaw.sqlite.rebaseline-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT config_path FROM config_health_entries ORDER BY config_path"
        ).fetchall()
    assert rows == [("/home/dev/.openclaw/other.json",)]
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM config_health_entries").fetchone() == (2,)
    assert "rows_deleted=1" in result.output
    assert "openclaw config validate" in result.output


def test_deployment_rebaseline_config_health_backup_contains_uncheckpointed_wal_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "state" / "openclaw.sqlite"
    writer = _write_wal_config_health_db(database_path)
    monkeypatch.setattr(cli_module, "DEFAULT_OPENCLAW_STATE_DB_PATH", database_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    try:
        result = runner.invoke(app, ["deployment-rebaseline-config-health"])
    finally:
        writer.close()

    assert result.exit_code == 0, result.output
    backups = list(database_path.parent.glob("openclaw.sqlite.rebaseline-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT config_path FROM config_health_entries").fetchall() == [
            ("/home/dev/.openclaw/openclaw.json",)
        ]


def test_deployment_rebaseline_config_health_rejects_external_schema_without_config_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "openclaw.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE config_health_entries (path TEXT NOT NULL)")
    monkeypatch.setattr(cli_module, "DEFAULT_OPENCLAW_STATE_DB_PATH", database_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    result = runner.invoke(app, ["deployment-rebaseline-config-health"])

    assert result.exit_code == 1, result.output
    assert "expected config_path column" in result.output
    assert "Follow-up" not in result.output


def test_deployment_rebaseline_config_health_rejects_zero_matching_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "openclaw.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE config_health_entries (config_path TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO config_health_entries(config_path) VALUES (?)",
            ("/home/dev/.openclaw/other.json",),
        )
    monkeypatch.setattr(cli_module, "DEFAULT_OPENCLAW_STATE_DB_PATH", database_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    result = runner.invoke(app, ["deployment-rebaseline-config-health"])

    assert result.exit_code == 1, result.output
    assert "no matching config-health row for" in result.output
    assert "nothing rebaselined" in result.output
    assert "Follow-up" not in result.output
    assert "rows_deleted=" not in result.output


def test_deployment_rebaseline_config_health_refuses_active_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "openclaw.sqlite"
    _write_config_health_db(database_path)
    monkeypatch.setattr(cli_module, "DEFAULT_OPENCLAW_STATE_DB_PATH", database_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: True)

    result = runner.invoke(app, ["deployment-rebaseline-config-health"])

    assert result.exit_code == 1
    assert "is active; stop it first" in result.output
    assert not list(tmp_path.glob("openclaw.sqlite.rebaseline-*.bak"))


def test_deployment_rebaseline_config_health_reports_missing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "missing.sqlite"
    monkeypatch.setattr(cli_module, "DEFAULT_OPENCLAW_STATE_DB_PATH", database_path)
    monkeypatch.setattr(cli_module, "_is_systemd_unit_active", lambda _unit: False)

    result = runner.invoke(app, ["deployment-rebaseline-config-health"])

    assert result.exit_code == 1
    assert "missing database" in result.output


def _ready_manifest(tmp_path: Path) -> PlatformReadinessManifest:
    evidence: dict[str, dict[str, str | None]] = {}
    tmp_path.mkdir(parents=True, exist_ok=True)
    for evidence_id in EvidenceId:
        path = tmp_path / f"{evidence_id.value}.json"
        if evidence_id is EvidenceId.XNYS_TRADING_CALENDAR:
            write_xnys_calendar_evidence(path)
        elif evidence_id is EvidenceId.QUANTIPY_DATA_CONTRACT:
            path.write_text(json.dumps({"quantipy_commit": "a" * 40}), encoding="utf-8")
        else:
            path.write_text(f"{evidence_id.value}\n", encoding="utf-8")
        evidence[evidence_id.value] = {
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "reason": None,
        }
    return PlatformReadinessManifest.from_dict(
        {
            "schema_version": PLATFORM_READINESS_SCHEMA_VERSION,
            "status": "READY",
            "manifest_id": "manifest-cli-test-1",
            "snapshot_id": "snapshot-cli-test-1",
            "evidence": evidence,
            "capabilities": canonical_platform_capabilities().to_dict(),
            "reason": None,
        }
    )


def _write_readiness_manifest(path: Path, manifest: PlatformReadinessManifest) -> None:
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")


def test_autoresearch_build_readiness_command_fails_closed_on_invalid_commit(
    tmp_path: Path,
) -> None:
    quantipy_root = tmp_path / "quantipy"
    quantipy_root.mkdir()
    xnys = tmp_path / "xnys.json"
    write_xnys_calendar_evidence(xnys)

    result = runner.invoke(
        app,
        [
            "autoresearch-build-readiness",
            str(tmp_path / "manifest.json"),
            "--quantipy-root",
            str(quantipy_root),
            "--expected-quantipy-commit",
            "not-a-commit",
            "--xnys-calendar",
            str(xnys),
            "--campaign-xnys-start",
            CAMPAIGN_XNYS_START,
            "--campaign-xnys-end",
            CAMPAIGN_XNYS_END,
        ],
    )

    assert result.exit_code == 1
    assert "full" in result.output
    assert "lowercase Git hash" in result.output
    assert not (tmp_path / "manifest.json").exists()


def test_autoresearch_build_readiness_command_requires_explicit_campaign_xnys_bounds(
    tmp_path: Path,
) -> None:
    quantipy_root = tmp_path / "quantipy"
    quantipy_root.mkdir()
    xnys = tmp_path / "xnys.json"
    write_xnys_calendar_evidence(xnys)

    missing_start = runner.invoke(
        app,
        [
            "autoresearch-build-readiness",
            str(tmp_path / "manifest.json"),
            "--quantipy-root",
            str(quantipy_root),
            "--expected-quantipy-commit",
            "0" * 40,
            "--xnys-calendar",
            str(xnys),
        ],
    )

    assert missing_start.exit_code == 2
    assert "--campaign-xnys-start" in missing_start.output

    missing_end = runner.invoke(
        app,
        [
            "autoresearch-build-readiness",
            str(tmp_path / "manifest.json"),
            "--quantipy-root",
            str(quantipy_root),
            "--expected-quantipy-commit",
            "0" * 40,
            "--xnys-calendar",
            str(xnys),
            "--campaign-xnys-start",
            CAMPAIGN_XNYS_START,
        ],
    )

    assert missing_end.exit_code == 2
    assert "--campaign-xnys-end" in missing_end.output


def test_autoresearch_build_readiness_command_rejects_noncanonical_campaign_xnys_bounds(
    tmp_path: Path,
) -> None:
    quantipy_root = tmp_path / "quantipy"
    quantipy_root.mkdir()
    xnys = tmp_path / "xnys.json"
    write_xnys_calendar_evidence(xnys)

    result = runner.invoke(
        app,
        [
            "autoresearch-build-readiness",
            str(tmp_path / "manifest.json"),
            "--quantipy-root",
            str(quantipy_root),
            "--expected-quantipy-commit",
            "0" * 40,
            "--xnys-calendar",
            str(xnys),
            "--campaign-xnys-start",
            "2022-01-04",
            "--campaign-xnys-end",
            CAMPAIGN_XNYS_END,
        ],
    )

    assert result.exit_code == 1
    assert " ".join(result.output.split()).endswith("must be pinned to 2022-01-03..2025-12-31")
    assert not (tmp_path / "manifest.json").exists()


def test_autoresearch_pin_readiness_repins_same_ids_without_resetting_state(
    tmp_path: Path,
) -> None:
    readiness = _ready_manifest(tmp_path / "evidence")
    readiness_path = tmp_path / "readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    old_identity = replace(readiness.identity(), receipt_sha256="0" * 64)
    state = AutoresearchState(iteration=9, platform_readiness=old_identity)
    state_path = tmp_path / "state.json"
    output_path = tmp_path / "repinned.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "autoresearch-pin-readiness",
            str(state_path),
            "--output",
            str(output_path),
            "--readiness-manifest",
            str(readiness_path),
        ],
    )

    assert result.exit_code == 0, result.output
    repinned = AutoresearchState.from_dict(json.loads(output_path.read_text(encoding="utf-8")))
    assert repinned.platform_readiness == readiness.identity()
    assert replace(repinned, platform_readiness=old_identity) == state


class CliInvocationResult(Protocol):
    @property
    def exit_code(self) -> int: ...

    @property
    def output(self) -> str: ...


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        cwd=cwd,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class GitWorktree:
    target_checkout: Path
    workspace: Path
    implementation_commit: str
    final_commit: str


@pytest.fixture()
def autoresearch_worktree_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    worktree_root = tmp_path / "operator-controlled" / "worktrees"
    worktree_root.mkdir(mode=0o700, parents=True)
    worktree_root.chmod(0o700)
    monkeypatch.setattr(constants, "DEFAULT_AUTORESEARCH_WORKTREE_ROOT", worktree_root)
    return worktree_root


@pytest.fixture()
def git_worktree(tmp_path: Path, autoresearch_worktree_root: Path) -> GitWorktree:
    target_checkout = tmp_path / "target"
    workspace = autoresearch_worktree_root / "workspace"
    _git(tmp_path, "init", "--initial-branch=main", str(target_checkout))
    _git(target_checkout, "config", "user.email", "autoresearch@example.test")
    _git(target_checkout, "config", "user.name", "Autoresearch Test")
    (target_checkout / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(target_checkout, "add", "README.md")
    _git(target_checkout, "commit", "-m", "baseline")
    _git(tmp_path, "clone", str(target_checkout), str(workspace))
    _git(workspace, "config", "user.email", "autoresearch@example.test")
    _git(workspace, "config", "user.name", "Autoresearch Test")
    workspace.chmod(0o700)
    (workspace / ".git").chmod(0o700)
    manifest = {
        "schema_version": "quantipy-experiment-v2",
        "experiment_id": "cli-runtime-audit",
        "package_path": "experiment",
        "stage_files": [
            {
                "name": stage,
                "file_path": f"{stage}.py",
                "entrypoint": f"experiment.{stage}:run",
            }
            for stage in ("prepare", "smoke", "feasibility", "model")
        ],
    }
    manifest_path = workspace / "experiment-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    manifest_path.chmod(0o644)
    experiment_package = workspace / "experiment"
    experiment_package.mkdir()
    experiment_package.chmod(0o755)
    (experiment_package / "__init__.py").write_text("", encoding="utf-8")
    (experiment_package / "__init__.py").chmod(0o644)
    for stage in ("prepare", "smoke", "feasibility", "model"):
        stage_path = experiment_package / f"{stage}.py"
        stage_path.write_text(
            "def run(context):\n    return context.accept('accepted')\n",
            encoding="utf-8",
        )
        stage_path.chmod(0o644)
    (workspace / "experiment.txt").write_text("implementation\n", encoding="utf-8")
    _git(workspace, "add", "experiment", "experiment.txt", "experiment-manifest.json")
    _git(workspace, "commit", "-m", "implementation")
    implementation_commit = _git(workspace, "rev-parse", "HEAD")
    (workspace / "experiment.txt").write_text("fixed\n", encoding="utf-8")
    _git(workspace, "add", "experiment.txt")
    _git(workspace, "commit", "-m", "fix")
    final_commit = _git(workspace, "rev-parse", "HEAD")
    _git(
        target_checkout,
        "fetch",
        str(workspace),
        f"{implementation_commit}:refs/autoresearch-fixtures/{implementation_commit}",
    )
    _git(
        target_checkout,
        "fetch",
        str(workspace),
        f"{final_commit}:refs/autoresearch-fixtures/{final_commit}",
    )
    return GitWorktree(target_checkout, workspace, implementation_commit, final_commit)


# ---------------------------------------------------------------------------
# GPU detection / parsing
# ---------------------------------------------------------------------------


class TestParseGpuOutput:
    """_parse_gpu_output handles nvidia-smi CSV lines."""

    def test_typical_gpu(self) -> None:
        name, vram = _parse_gpu_output("NVIDIA GeForce RTX 3060, 12288 MiB\n")
        assert name == "NVIDIA GeForce RTX 3060"
        assert vram == pytest.approx(12.0, abs=0.1)

    def test_small_gpu(self) -> None:
        name, vram = _parse_gpu_output("NVIDIA GeForce GTX 1050, 2048 MiB\n")
        assert name == "NVIDIA GeForce GTX 1050"
        assert vram == pytest.approx(2.0, abs=0.1)

    def test_empty_output(self) -> None:
        name, vram = _parse_gpu_output("")
        assert name is None
        assert vram == 0.0

    def test_malformed_single_field(self) -> None:
        name, vram = _parse_gpu_output("garbage")
        assert name is None
        assert vram == 0.0

    def test_non_numeric_vram(self) -> None:
        name, vram = _parse_gpu_output("GPU Name, not_a_number MiB\n")
        assert name == "GPU Name"
        assert vram == 0.0


class TestDetectGpu:
    """_detect_gpu calls nvidia-smi and interprets the result."""

    def test_gpu_found(self) -> None:
        fake = MagicMock(
            returncode=0,
            stdout="NVIDIA RTX 4090, 24564 MiB\n",
        )
        with patch("gateway.cli.subprocess.run", return_value=fake) as mock_run:
            name, vram = _detect_gpu()
            mock_run.assert_called_once()
        assert name == "NVIDIA RTX 4090"
        assert vram == pytest.approx(23.99, abs=0.1)

    def test_nvidia_smi_not_found(self) -> None:
        with patch("gateway.cli.subprocess.run", side_effect=FileNotFoundError):
            name, vram = _detect_gpu()
        assert name is None
        assert vram == 0.0

    def test_nvidia_smi_nonzero_exit(self) -> None:
        fake = MagicMock(returncode=1, stdout="")
        with patch("gateway.cli.subprocess.run", return_value=fake):
            name, vram = _detect_gpu()
        assert name is None
        assert vram == 0.0

    def test_nvidia_smi_timeout(self) -> None:
        with patch(
            "gateway.cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10),
        ):
            name, vram = _detect_gpu()
        assert name is None
        assert vram == 0.0


# ---------------------------------------------------------------------------
# Whisper model selection
# ---------------------------------------------------------------------------


class TestChooseWhisperModel:
    """_choose_whisper_model picks appropriate model for VRAM."""

    def test_no_gpu(self) -> None:
        assert _choose_whisper_model(0.0, has_gpu=False) == "tiny.en"

    def test_low_vram(self) -> None:
        assert _choose_whisper_model(2.0, has_gpu=True) == "base.en"

    def test_medium_vram(self) -> None:
        assert _choose_whisper_model(6.0, has_gpu=True) == "small.en"

    def test_high_vram(self) -> None:
        assert _choose_whisper_model(12.0, has_gpu=True) == "medium.en"

    def test_boundary_4gb(self) -> None:
        assert _choose_whisper_model(4.0, has_gpu=True) == "small.en"

    def test_boundary_8gb(self) -> None:
        assert _choose_whisper_model(8.0, has_gpu=True) == "medium.en"

    def test_boundary_just_under_4gb(self) -> None:
        assert _choose_whisper_model(3.99, has_gpu=True) == "base.en"


# ---------------------------------------------------------------------------
# OpenClaw config reading
# ---------------------------------------------------------------------------


class TestReadOpenClawConfig:
    """_read_openclaw_config reads token/port from JSON."""

    def test_full_config(self, tmp_path: Path) -> None:
        cfg = {"gateway": {"auth": {"token": "oc-tok-123"}, "port": 19000}}
        p = tmp_path / "openclaw.json"
        p.write_text(json.dumps(cfg))
        token, port = _read_openclaw_config(p)
        assert token == "oc-tok-123"
        assert port == 19000

    def test_missing_token(self, tmp_path: Path) -> None:
        cfg = {"gateway": {"port": 19000}}
        p = tmp_path / "openclaw.json"
        p.write_text(json.dumps(cfg))
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 19000

    def test_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.json"
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 18789

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "openclaw.json"
        p.write_text("NOT JSON")
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 18789

    def test_empty_gateway_section(self, tmp_path: Path) -> None:
        p = tmp_path / "openclaw.json"
        p.write_text(json.dumps({"gateway": {}}))
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 18789


# ---------------------------------------------------------------------------
# .env rendering
# ---------------------------------------------------------------------------


class TestRenderEnv:
    """_render_env produces correct .env content."""

    def test_contains_all_keys(self) -> None:
        content = _render_env(
            local_ip="10.0.0.5",
            gateway_token="abc123",
            whisper_model="small.en",
            whisper_device="cuda",
            whisper_compute_type="float16",
            gpu_label="NVIDIA RTX 3060 (12.0 GB)",
            openclaw_port=18789,
            openclaw_token="oc-tok",
        )
        for key in (
            "GATEWAY_HOST",
            "GATEWAY_PORT",
            "GATEWAY_TOKEN",
            "WHISPER_MODEL",
            "WHISPER_DEVICE",
            "WHISPER_COMPUTE_TYPE",
            "OPENCLAW_HOST",
            "OPENCLAW_PORT",
            "OPENCLAW_GATEWAY_TOKEN",
            "AGENT_TIMEOUT",
        ):
            assert key in content

    def test_no_openclaw_token_comment(self) -> None:
        content = _render_env(
            local_ip="10.0.0.5",
            gateway_token="abc",
            whisper_model="tiny.en",
            whisper_device="cpu",
            whisper_compute_type="int8",
            gpu_label="No NVIDIA GPU detected (CPU mode)",
            openclaw_port=18789,
            openclaw_token=None,
        )
        assert "Not found in ~/.openclaw/openclaw.json" in content
        assert "OPENCLAW_GATEWAY_TOKEN=\n" in content

    def test_with_openclaw_token_comment(self) -> None:
        content = _render_env(
            local_ip="10.0.0.5",
            gateway_token="abc",
            whisper_model="small.en",
            whisper_device="cuda",
            whisper_compute_type="float16",
            gpu_label="NVIDIA RTX 3060 (12.0 GB)",
            openclaw_port=19000,
            openclaw_token="secret-oc",
        )
        assert "Read from ~/.openclaw/openclaw.json" in content
        assert "OPENCLAW_GATEWAY_TOKEN=secret-oc" in content


# ---------------------------------------------------------------------------
# .env parseable by python-dotenv
# ---------------------------------------------------------------------------


class TestEnvParseable:
    """Generated .env must be parseable by python-dotenv."""

    def test_dotenv_loads_all_keys(self, tmp_path: Path) -> None:
        content = _render_env(
            local_ip="192.168.1.42",
            gateway_token="tok123",
            whisper_model="medium.en",
            whisper_device="cuda",
            whisper_compute_type="float16",
            gpu_label="NVIDIA RTX 4090 (24.0 GB)",
            openclaw_port=18789,
            openclaw_token="oc-abc",
        )
        env_file = tmp_path / ".env"
        env_file.write_text(content)
        values = dotenv_values(env_file)
        assert values["GATEWAY_HOST"] == "0.0.0.0"
        assert values["GATEWAY_PORT"] == "8765"
        assert values["GATEWAY_TOKEN"] == "tok123"
        assert values["WHISPER_MODEL"] == "medium.en"
        assert values["WHISPER_DEVICE"] == "cuda"
        assert values["WHISPER_COMPUTE_TYPE"] == "float16"
        assert values["OPENCLAW_HOST"] == "127.0.0.1"
        assert values["OPENCLAW_PORT"] == "18789"
        assert values["OPENCLAW_GATEWAY_TOKEN"] == "oc-abc"
        assert values["AGENT_TIMEOUT"] == "120"


# ---------------------------------------------------------------------------
# CLI integration — init-env command
# ---------------------------------------------------------------------------


class TestInitEnvCommand:
    """Full CLI integration via typer.testing.CliRunner."""

    @staticmethod
    def _mock_detect_no_gpu() -> tuple[None, float]:
        return None, 0.0

    @staticmethod
    def _mock_detect_gpu() -> tuple[str, float]:
        return "NVIDIA RTX 3060", 12.0

    def test_creates_env_file(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.99"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "WHISPER_DEVICE=cpu" in content
        assert "WHISPER_MODEL=tiny.en" in content

    def test_existing_env_without_force(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OLD=value\n")
        result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 1
        assert "already" in result.output and "exists" in result.output
        # Original file untouched
        assert (tmp_path / ".env").read_text() == "OLD=value\n"

    def test_existing_env_with_force(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OLD=value\n")
        with (
            patch("gateway.cli._detect_gpu", return_value=("NVIDIA RTX 3060", 12.0)),
            patch("gateway.cli._read_openclaw_config", return_value=("oc-tok", 19000)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.1"),
        ):
            result = runner.invoke(app, ["init-env", "--force", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        content = (tmp_path / ".env").read_text()
        assert "WHISPER_DEVICE=cuda" in content
        assert "OPENCLAW_PORT=19000" in content
        assert "OLD=value" not in content

    def test_gpu_detected_sets_cuda(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=("NVIDIA RTX 4090", 24.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.1"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        content = (tmp_path / ".env").read_text()
        assert "WHISPER_DEVICE=cuda" in content
        assert "WHISPER_COMPUTE_TYPE=float16" in content
        assert "WHISPER_MODEL=medium.en" in content

    def test_summary_panel_printed(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.10"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "init-env summary" in result.output

    def test_generated_file_parseable(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=("RTX A5000", 8.0)),
            patch("gateway.cli._read_openclaw_config", return_value=("tok-x", 18789)),
            patch("gateway.cli._get_local_ip", return_value="172.16.0.5"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        values = dotenv_values(tmp_path / ".env")
        assert values["GATEWAY_HOST"] == "0.0.0.0"
        assert values["WHISPER_DEVICE"] == "cuda"
        assert values["OPENCLAW_GATEWAY_TOKEN"] == "tok-x"


class TestAutoresearchCliCommands:
    @staticmethod
    def _write_quantipy_receipts(root: Path) -> None:
        for relative_path in QUANTIPY_RECEIPT_PATHS.values():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"fixture for {relative_path}\n", encoding="utf-8")

    @staticmethod
    def _state_for_implementation(target_checkout: Path) -> AutoresearchState:
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        state = advance_state(
            AutoresearchState(),
            SetupContextArtifact(
                goal="Find a profitable intraday alpha",
                metric_name="OOS Sharpe net",
                metric_direction=MetricDirection.MAXIMIZE,
                target_repo=str(target_checkout),
                writable_scope="src/quantipy/alpha",
                baseline_summary="Baseline OOS Sharpe net is 0.18.",
                hard_constraints=("No overnight holds",),
                data_sources=("qp.prices()",),
            ),
            policy,
        )
        state = advance_state(
            state,
            ContextPacketArtifact(
                baseline_metric="0.18 OOS Sharpe net",
                current_best_metric="0.22 OOS Sharpe net",
                recent_experiment_outcomes=(),
                prior_findings=(),
                open_proposals=(),
                hard_constraints=("No overnight holds",),
                available_data_sources=("qp.prices()",),
                loaded_quantipy_sources=("AGENTS.md",),
                research_mode=ResearchMode.ALPHA_RESEARCH,
                mode_rationale="Coverage supports an alpha experiment.",
                burned_theory_families=(),
            ),
            policy,
        )
        debate = DebateResultArtifact(
            round_number=1,
            submissions=tuple(
                DebateSubmission(
                    agent_id=agent_id,
                    theory_id=f"theory-{index}",
                    theory_family="vwap-obv",
                    vote_family="vwap-obv",
                    hypothesis="VWAP and OBV capture intraday accumulation.",
                    universe="Small-cap semiconductors",
                    example_tickers=("AMD", "SMCI"),
                    feature_pipeline="OHLCV to VWAP and OBV features",
                    model_plan="Time-series classifier",
                    walk_forward_plan="Expanding windows",
                    transaction_cost_model="0.7 bps",
                    data_coverage_plan="Use the common 2021-2026 calendar.",
                    rejection_criteria="Discard below baseline.",
                    objections=(),
                    compute_fit=ComputeFitArtifact(
                        target=ComputeTarget.CPU,
                        rationale=(
                            "The tabular feature set is small and the existing CPU stack "
                            "is reproducible."
                        ),
                        required_dependencies=(),
                        benchmark_plan="Record wall time and peak memory for verification.",
                    ),
                )
                for index, agent_id in enumerate(policy.debate_agent_ids, start=1)
            ),
        )
        state = advance_state(state, debate, policy)
        return advance_state(
            state,
            ConsensusResultArtifact(
                round_number=1,
                status=ConsensusStatus.MAJORITY,
                winner_theory_id="theory-1",
                winner_theory_family="vwap-obv",
                majority_count=5,
                majority_agent_ids=policy.debate_agent_ids,
                dissenting_positions=(),
                novelty_score=0.6,
                theory_score=0.7,
                implementation_risk_score=0.3,
                data_adequacy_score=0.9,
                overfit_risk_score=0.2,
                expected_net_sharpe=0.5,
                data_requirements=("price_panel",),
                rejection_reasons=(),
                implementation_brief="Implement the narrow VWAP and OBV experiment.",
                dissent_summary="The panel reached consensus.",
                universe_plan=_universe_plan(),
            ),
            policy,
        )

    @staticmethod
    def _implementation_artifact(
        worktree: GitWorktree,
        *,
        commit_sha: str,
        workspace_path: str | None = None,
    ) -> ImplementationResultArtifact:
        resolved_workspace = (
            Path(workspace_path) if workspace_path is not None else worktree.workspace
        )
        manifest_path = resolved_workspace / "experiment-manifest.json"
        session_count = _fixture_xnys_session_count(date(2021, 1, 4), date(2021, 12, 31))
        return ImplementationResultArtifact(
            summary="Implemented the narrow VWAP and OBV experiment.",
            workspace_path=workspace_path
            if workspace_path is not None
            else str(worktree.workspace),
            commit_sha=commit_sha,
            module_path="src/quantipy/alpha/vwap_obv/",
            notebook_path="notebooks/experiments/vwap_obv.ipynb",
            tests_added_or_updated=("tests/test_vwap_obv.py",),
            commands_run=("uv run pytest tests/test_vwap_obv.py",),
            experiment_manifest_path=str(manifest_path),
            experiment_manifest_sha256=sha256(
                json.dumps(
                    {
                        "schema_version": "quantipy-experiment-v2",
                        "experiment_id": "cli-runtime-audit",
                        "package_path": "experiment",
                        "stage_files": [
                            {
                                "name": stage,
                                "file_path": f"{stage}.py",
                                "entrypoint": f"experiment.{stage}:run",
                            }
                            for stage in ("prepare", "smoke", "feasibility", "model")
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            compute_fit=ComputeFitArtifact(
                target=ComputeTarget.CPU,
                rationale=(
                    "The tabular feature set is small and the existing CPU stack is reproducible."
                ),
                required_dependencies=(),
                benchmark_plan="Record wall time and peak memory for verification.",
            ),
            price_hydration_scope_preflight=PriceHydrationScopePreflight(
                member_union_count=1,
                experiment_start="2021-01-04",
                experiment_end="2021-12-31",
                timeframe="1min",
                market_hours="regular",
                session_count=session_count,
                planned_symbol_sessions=session_count,
                within_budget=True,
            ),
        )

    @staticmethod
    def _verification_failure() -> VerificationResultArtifact:
        return VerificationResultArtifact(
            status=VerificationStatus.TEST_FAILURE,
            is_walk_forward_sharpe_net=0.41,
            oos_sharpe_net=0.38,
            max_drawdown_pct=12.4,
            win_rate=0.54,
            trade_count=211,
            trades_per_day=1.9,
            oos_trading_days=128,
            feature_importances_summary="VWAP distance and OBV slope dominate.",
            null_test_summary="Null shuffle drops Sharpe near zero.",
            bug_signals=(),
            tests_passed=False,
            commands_run=("uv run pytest",),
            data_coverage=None,
        )

    @staticmethod
    def _fix_artifact(
        worktree: GitWorktree,
        *,
        workspace_path: str | None = None,
        price_hydration_scope_preflight: PriceHydrationScopePreflight | None = None,
    ) -> FixResultArtifact:
        return FixResultArtifact(
            trigger_phase=FixTriggerPhase.VERIFICATION,
            summary="Applied the requested narrow fix.",
            workspace_path=workspace_path
            if workspace_path is not None
            else str(worktree.workspace),
            commit_sha=worktree.final_commit,
            fixes_applied=("Expanded ticker coverage to 5 names",),
            tests_rerun=("uv run pytest",),
            remaining_issues=(),
            price_hydration_scope_preflight=price_hydration_scope_preflight,
        )

    @staticmethod
    def _invoke_autoresearch_advance(
        tmp_path: Path,
        state: AutoresearchState,
        artifact: ImplementationResultArtifact | FixResultArtifact,
        *,
        legacy_unwrapped: bool = False,
    ) -> tuple[CliInvocationResult, Path]:
        readiness = _ready_manifest(tmp_path / "advance-readiness")
        readiness_path = tmp_path / "advance-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state = replace(state, platform_readiness=readiness.identity())
        quantipy_root = tmp_path / "quantipy"
        TestAutoresearchCliCommands._write_quantipy_receipts(quantipy_root)
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        state_path = tmp_path / "state.json"
        artifact_path = tmp_path / "artifact.json"
        output_path = tmp_path / "state-out.json"
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
        digest = expected_instruction_manifest_sha256(
            state,
            policy,
            build_receipt_catalog(quantipy_root),
            state_path=state_path,
        )
        artifact_payload: object = artifact.to_dict()
        if not legacy_unwrapped:
            artifact_payload = {
                "instruction_manifest_sha256": digest,
                "state_reference_sha256": (
                    autoresearch_transitions.build_authoritative_state_reference(
                        state,
                        state_path=state_path,
                    ).sha256()
                ),
                "artifact": artifact_payload,
            }
        artifact_path.write_text(json.dumps(artifact_payload), encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "autoresearch-advance",
                str(state_path),
                str(artifact_path),
                "--output",
                str(output_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--quantipy-root",
                str(quantipy_root),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )
        return result, output_path

    def test_autoresearch_suspend_infra_persists_a_validated_operator_transition(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        readiness = _ready_manifest(tmp_path / "suspend-readiness")
        state = replace(
            self._state_for_implementation(git_worktree.target_checkout),
            platform_readiness=readiness.identity(),
        )
        state_path = tmp_path / "active-state.json"
        output_path = tmp_path / "suspended-state.json"
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "autoresearch-suspend-infra",
                str(state_path),
                "--reason",
                "Operator is repairing historical market-data infrastructure.",
                "--output",
                str(output_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
            ],
        )

        assert result.exit_code == 0, result.output
        suspended = AutoresearchState.from_dict(json.loads(output_path.read_text(encoding="utf-8")))
        assert suspended.phase is Phase.REPEAT
        assert suspended.suspended is True
        assert suspended.suspension_reason == (
            "Operator is repairing historical market-data infrastructure."
        )
        assert suspended.final_decision is not None
        assert suspended.final_decision.decision is FinalDecision.INFRA_BLOCKED
        assert suspended.final_decision.memory_write_required is False
        assert json.loads(state_path.read_text(encoding="utf-8")) == state.to_dict()

    def test_autoresearch_advance_validates_and_accepts_implementation_worktree(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        state = self._state_for_implementation(git_worktree.target_checkout)
        artifact = self._implementation_artifact(
            git_worktree,
            commit_sha=git_worktree.final_commit,
        )

        result, output_path = self._invoke_autoresearch_advance(tmp_path, state, artifact)

        assert result.exit_code == 0
        assert json.loads(output_path.read_text(encoding="utf-8"))["phase"] == "verification"

    def test_autoresearch_advance_uses_dispatch_manifest_after_source_drift(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        readiness = _ready_manifest(tmp_path / "advance-readiness")
        readiness_path = tmp_path / "advance-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state = replace(
            self._state_for_implementation(git_worktree.target_checkout),
            platform_readiness=readiness.identity(),
        )
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        state_path = tmp_path / "state.json"
        artifact_path = tmp_path / "artifact.json"
        output_path = tmp_path / "state-out.json"
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
        source_manifest_sha256 = expected_instruction_manifest_sha256(
            state,
            policy,
            build_receipt_catalog(quantipy_root),
            state_path=state_path,
        )
        state_reference_sha256 = autoresearch_transitions.build_authoritative_state_reference(
            state,
            state_path=state_path,
        ).sha256()
        artifact_path.write_text(
            json.dumps(
                {
                    "instruction_manifest_sha256": source_manifest_sha256,
                    "state_reference_sha256": state_reference_sha256,
                    "artifact": self._implementation_artifact(
                        git_worktree,
                        commit_sha=git_worktree.final_commit,
                    ).to_dict(),
                }
            ),
            encoding="utf-8",
        )
        drifted_source = quantipy_root / QUANTIPY_RECEIPT_PATHS["quantipy.agents"]
        drifted_source.write_text("updated methodology after dispatch\n", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "autoresearch-advance",
                str(state_path),
                str(artifact_path),
                "--output",
                str(output_path),
                "--instruction-manifest-sha256",
                source_manifest_sha256,
                "--state-reference-sha256",
                state_reference_sha256,
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--quantipy-root",
                str(quantipy_root),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(output_path.read_text(encoding="utf-8"))["phase"] == "verification"

    def test_autoresearch_advance_does_not_publish_output_after_source_changes_before_persistence(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        state = self._state_for_implementation(git_worktree.target_checkout)
        artifact = self._implementation_artifact(
            git_worktree,
            commit_sha=git_worktree.final_commit,
        )
        original_advance = autoresearch_persistence.advance_artifact_state_file

        def mutate_source_then_advance(
            *,
            state_path: Path,
            output_path: Path,
            artifact_path: Path,
            instruction_manifest_sha256: str,
            policy: AutoresearchPolicy,
            validation_context: AutoresearchValidationContext | None,
            state_reference_sha256: str | None = None,
        ) -> AutoresearchState:
            source_path = state_path
            source_state = AutoresearchState.from_dict(
                json.loads(source_path.read_text(encoding="utf-8"))
            )
            source_path.write_text(
                json.dumps(replace(source_state, iteration=source_state.iteration + 1).to_dict()),
                encoding="utf-8",
            )
            return original_advance(
                state_path=state_path,
                output_path=output_path,
                artifact_path=artifact_path,
                instruction_manifest_sha256=instruction_manifest_sha256,
                policy=policy,
                validation_context=validation_context,
                state_reference_sha256=state_reference_sha256,
            )

        with patch.object(
            autoresearch_persistence,
            "advance_artifact_state_file",
            new=mutate_source_then_advance,
        ):
            result, output_path = self._invoke_autoresearch_advance(tmp_path, state, artifact)

        assert result.exit_code == 1
        assert not output_path.exists()

    def test_autoresearch_advance_rejects_noncanonical_implementation_worktree(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        state = self._state_for_implementation(git_worktree.target_checkout)
        artifact = self._implementation_artifact(
            git_worktree,
            commit_sha=git_worktree.final_commit,
            workspace_path=str(git_worktree.workspace / ".." / git_worktree.workspace.name),
        )

        result, _ = self._invoke_autoresearch_advance(tmp_path, state, artifact)

        assert result.exit_code == 1
        assert "canonical resolved path" in result.output

    def test_autoresearch_advance_rejects_legacy_unwrapped_artifact(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        state = self._state_for_implementation(git_worktree.target_checkout)
        artifact = self._implementation_artifact(
            git_worktree,
            commit_sha=git_worktree.final_commit,
        )

        result, _ = self._invoke_autoresearch_advance(
            tmp_path,
            state,
            artifact,
            legacy_unwrapped=True,
        )

        assert result.exit_code == 1
        assert "artifact_file must contain exact keys" in result.output

    def test_autoresearch_advance_rejects_oversized_artifact_envelope(
        self,
        tmp_path: Path,
    ) -> None:
        readiness = _ready_manifest(tmp_path / "oversized-readiness")
        readiness_path = tmp_path / "oversized-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state = AutoresearchState(platform_readiness=readiness.identity())
        state_path = tmp_path / "state.json"
        output_path = tmp_path / "state-out.json"
        artifact_path = tmp_path / "oversized-artifact.json"
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
        digest = expected_instruction_manifest_sha256(
            state,
            policy,
            build_receipt_catalog(quantipy_root),
            state_path=state_path,
        )
        state_reference_sha256 = autoresearch_transitions.build_authoritative_state_reference(
            state,
            state_path=state_path,
        ).sha256()
        artifact_path.write_text(
            json.dumps(
                {
                    "instruction_manifest_sha256": digest,
                    "state_reference_sha256": state_reference_sha256,
                    "artifact": {
                        **SetupContextArtifact(
                            goal="Find a profitable intraday alpha",
                            metric_name="OOS Sharpe net",
                            metric_direction=MetricDirection.MAXIMIZE,
                            target_repo="/home/dev/repos/quantipy",
                            writable_scope="src/quantipy/alpha",
                            baseline_summary="reviewer baseline " + ("x" * 80_000),
                            hard_constraints=("No overnight holds",),
                            data_sources=("qp.prices()",),
                        ).to_dict(),
                    },
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "autoresearch-advance",
                str(state_path),
                str(artifact_path),
                "--output",
                str(output_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--quantipy-root",
                str(quantipy_root),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

        assert result.exit_code == 1
        assert "artifact file exceeds hard byte budget" in result.output
        assert not output_path.exists()

    def test_autoresearch_advance_validates_and_accepts_fix_worktree(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        implementation = self._implementation_artifact(
            git_worktree,
            commit_sha=git_worktree.implementation_commit,
        )
        state = advance_state(
            self._state_for_implementation(git_worktree.target_checkout),
            implementation,
            policy,
        )
        state = advance_state(
            state,
            self._verification_failure(),
            policy,
            validation_context=AutoresearchValidationContext(
                state.platform_readiness, "f" * 64, (date(2021, 1, 5),)
            ),
        )

        session_count = _fixture_xnys_session_count(date(2021, 1, 4), date(2021, 12, 31))
        updated_preflight = PriceHydrationScopePreflight(
            member_union_count=2,
            experiment_start="2021-01-04",
            experiment_end="2021-12-31",
            timeframe="1min",
            market_hours="regular",
            session_count=session_count,
            planned_symbol_sessions=2 * session_count,
            within_budget=True,
        )

        result, output_path = self._invoke_autoresearch_advance(
            tmp_path,
            state,
            self._fix_artifact(
                git_worktree,
                price_hydration_scope_preflight=updated_preflight,
            ),
        )

        assert result.exit_code == 0
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        assert saved["phase"] == "verification"
        assert (
            saved["implementation_result"]["price_hydration_scope_preflight"]
            == updated_preflight.to_dict()
        )

    def test_autoresearch_advance_rejects_noncanonical_fix_worktree(
        self,
        tmp_path: Path,
        git_worktree: GitWorktree,
    ) -> None:
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        implementation = self._implementation_artifact(
            git_worktree,
            commit_sha=git_worktree.implementation_commit,
        )
        state = advance_state(
            self._state_for_implementation(git_worktree.target_checkout),
            implementation,
            policy,
        )
        state = advance_state(
            state,
            self._verification_failure(),
            policy,
            validation_context=AutoresearchValidationContext(
                state.platform_readiness, "f" * 64, (date(2021, 1, 5),)
            ),
        )
        alias = str(git_worktree.workspace / ".." / git_worktree.workspace.name)

        result, _ = self._invoke_autoresearch_advance(
            tmp_path,
            state,
            self._fix_artifact(git_worktree, workspace_path=alias),
        )

        assert result.exit_code == 1
        assert "canonical resolved path" in result.output

    def test_autoresearch_advance_persists_state(self, tmp_path: Path) -> None:
        readiness = _ready_manifest(tmp_path / "setup-readiness")
        readiness_path = tmp_path / "setup-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state_path = tmp_path / "state.json"
        artifact_path = tmp_path / "artifact.json"
        output_path = tmp_path / "state-out.json"
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        state = AutoresearchState(platform_readiness=readiness.identity())
        policy = load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
        digest = expected_instruction_manifest_sha256(
            state,
            policy,
            build_receipt_catalog(quantipy_root),
            state_path=state_path,
        )
        setup_artifact = SetupContextArtifact(
            goal="Find a profitable intraday alpha",
            metric_name="OOS Sharpe net",
            metric_direction=MetricDirection.MAXIMIZE,
            target_repo="/home/dev/repos/quantipy",
            writable_scope="src/quantipy/alpha",
            baseline_summary="Baseline OOS Sharpe net is 0.18.",
            hard_constraints=("No overnight holds",),
            data_sources=("qp.prices()",),
        )
        state_reference_sha256 = autoresearch_transitions.build_authoritative_state_reference(
            state,
            state_path=state_path,
        ).sha256()
        artifact_path.write_text(
            json.dumps(
                {
                    "instruction_manifest_sha256": digest,
                    "state_reference_sha256": state_reference_sha256,
                    "artifact": setup_artifact.to_dict(),
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "autoresearch-advance",
                str(state_path),
                str(artifact_path),
                "--output",
                str(output_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--quantipy-root",
                str(quantipy_root),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

        assert result.exit_code == 0
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        assert saved["phase"] == "setup_context"
        assert saved["setup"]["metric_name"] == "OOS Sharpe net"

    def test_process_touches_path_ignores_sibling_checkouts(self, tmp_path: Path) -> None:
        from gateway.cli import _process_touches_path

        root = tmp_path / "quantipy"
        root.mkdir()
        sibling = f"{root}-worktrees/luna-histfix"
        proc_dir = tmp_path / "proc"
        proc_dir.mkdir()

        assert not _process_touches_path(
            proc_dir, root, f"codex-linux-sandbox --sandbox-policy-cwd {sibling} pytest"
        )
        assert _process_touches_path(proc_dir, root, f"uv run pytest {root}/tests/unit")
        assert _process_touches_path(proc_dir, root, f"python {root}/notebooks/experiments/t1.py")
        assert _process_touches_path(proc_dir, root, f'sh -c "cd {root} && pytest"')

    def test_autoresearch_next_rejects_active_target_writer(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        readiness = _ready_manifest(tmp_path / "readiness")
        readiness_path = tmp_path / "platform-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state_path.write_text(
            json.dumps(AutoresearchState(platform_readiness=readiness.identity()).to_dict()),
            encoding="utf-8",
        )

        with (
            patch("gateway.cli._git_status_short", return_value=()),
            patch(
                "gateway.cli._active_target_writer_processes",
                return_value=("123 uv run python notebooks/experiments/t999.py",),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "autoresearch-next",
                    str(state_path),
                    "--quantipy-root",
                    str(quantipy_root),
                    "--openclaw-config",
                    str(DEFAULT_OPENCLAW_CONFIG_PATH),
                    "--readiness-manifest",
                    str(readiness_path),
                ],
            )

        assert result.exit_code == 1
        assert "active experiment/test writer" in result.output
        assert "processes" in result.output

    def test_autoresearch_next_surfaces_pending_campaign_review(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        readiness = _ready_manifest(tmp_path / "readiness")
        readiness_path = tmp_path / "platform-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state = AutoresearchState(
            campaign_review_required=True,
            campaign_review_reason="campaign stalled: review me",
            platform_readiness=readiness.identity(),
        )
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

        class FakeAction:
            source_manifest_sha256 = "0" * 64
            state_reference_sha256 = "1" * 64

            def to_dict(self) -> dict[str, object]:
                return {"phase": "repeat", "next_agent_ids": []}

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                "gateway.autoresearch.engine.next_action",
                lambda *_, **__: FakeAction(),
            )
            monkeypatch.setattr(cli_module, "_git_status_short", lambda _: ())
            monkeypatch.setattr(cli_module, "_active_target_writer_processes", lambda _: ())
            result = runner.invoke(
                app,
                [
                    "autoresearch-next",
                    str(state_path),
                    "--quantipy-root",
                    str(quantipy_root),
                    "--openclaw-config",
                    str(DEFAULT_OPENCLAW_CONFIG_PATH),
                    "--readiness-manifest",
                    str(readiness_path),
                ],
            )
        finally:
            monkeypatch.undo()

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["campaign_review"] == {
            "counters": {
                "consecutive_non_keep": 0,
                "consecutive_no_consensus": 0,
                "iterations_since_last_keep": 0,
            },
            "reason": "campaign stalled: review me",
            "required": True,
        }

    def test_autoresearch_next_still_refuses_suspended_state(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        readiness = _ready_manifest(tmp_path / "readiness")
        readiness_path = tmp_path / "platform-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state = AutoresearchState(suspended=True, platform_readiness=readiness.identity())
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                "gateway.autoresearch.engine.next_action",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ValueError("autoresearch is suspended")
                ),
            )
            monkeypatch.setattr(cli_module, "_git_status_short", lambda _: ())
            monkeypatch.setattr(cli_module, "_active_target_writer_processes", lambda _: ())
            result = runner.invoke(
                app,
                [
                    "autoresearch-next",
                    str(state_path),
                    "--quantipy-root",
                    str(quantipy_root),
                    "--openclaw-config",
                    str(DEFAULT_OPENCLAW_CONFIG_PATH),
                    "--readiness-manifest",
                    str(readiness_path),
                ],
            )
        finally:
            monkeypatch.undo()

        assert result.exit_code == 1
        assert "autoresearch is suspended" in result.output

    def test_autoresearch_next_does_not_finalize_memory_from_cli(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "repeat-state.json"
        quantipy_root = tmp_path / "quantipy"
        quantipy_root.mkdir()
        readiness = _ready_manifest(tmp_path / "readiness")
        readiness_path = tmp_path / "platform-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        state = AutoresearchState(
            phase=Phase.REPEAT,
            iteration=7,
            mode=ResearchMode.ALPHA_RESEARCH,
            final_decision=FinalDecisionArtifact(
                experiment_id="iteration-7",
                decision=FinalDecision.KEEP,
                recommended_metric_name="OOS Sharpe net",
                recommended_metric_value=0.42,
                reviewer_verdict=FinalReviewerVerdict.PASS,
                rationale="Passes review and improves baseline.",
                log_summary="KEEP iteration-7.",
                continue_loop=True,
                memory_write_required=True,
            ),
            platform_readiness=readiness.identity(),
        )
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

        class FakeAction:
            source_manifest_sha256 = "0" * 64
            state_reference_sha256 = "1" * 64

            def to_dict(self) -> dict[str, object]:
                return {"phase": "repeat", "next_agent_ids": []}

        def forbidden_finalize(*args: object, **kwargs: object) -> AutoresearchState:
            del args, kwargs
            raise AssertionError("autoresearch-next must not finalize memory")

        monkeypatch.setattr(
            "gateway.autoresearch.memory.finalize_repeat_memory_state_file",
            forbidden_finalize,
        )
        monkeypatch.setattr(
            "gateway.autoresearch.manifest_runtime.build_receipt_catalog",
            lambda _: object(),
        )
        monkeypatch.setattr(
            "gateway.autoresearch.engine.next_action", lambda *_, **__: FakeAction()
        )
        monkeypatch.setattr("gateway.cli._active_target_writer_processes", lambda _: ())

        result = runner.invoke(
            app,
            [
                "autoresearch-next",
                str(state_path),
                "--quantipy-root",
                str(quantipy_root),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"next_agent_ids": [], "phase": "repeat"}
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["memory_written"] is False

    def test_autoresearch_next_provisions_fixed_runs_root_before_verification_dispatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = MagicMock()
        state.phase = Phase.VERIFICATION
        action = MagicMock()
        action.state_reference_sha256 = "a" * 64
        action.to_dict.return_value = {"phase": "verification"}
        state_path = tmp_path / "state.json"
        config_path = tmp_path / "openclaw.json"
        readiness_path = tmp_path / "readiness.json"
        quantipy_root = tmp_path / "quantipy"
        for path in (state_path, config_path, readiness_path):
            path.write_text("{}\n", encoding="utf-8")
        quantipy_root.mkdir()
        foreign_cwd = tmp_path / "foreign-cwd"
        foreign_cwd.mkdir()
        monkeypatch.chdir(foreign_cwd)

        with (
            patch("gateway.autoresearch.persistence.load_state_file", return_value=state),
            patch("gateway.autoresearch.configuration.load_autoresearch_policy") as load_policy,
            patch("gateway.cli.load_platform_readiness"),
            patch("gateway.autoresearch.manifest_runtime.build_receipt_catalog"),
            patch("gateway.autoresearch.engine.next_action", return_value=action),
            patch("gateway.autoresearch.state.AutoresearchValidationContext.from_readiness"),
            patch(
                "gateway.autoresearch.attestation.seal_canonical_verification_dispatch_state_file",
                return_value=state,
            ) as seal_runtime,
            patch(
                "gateway.autoresearch.attestation.require_canonical_verification_dispatch_attestation",
                return_value=state,
            ) as require_runtime,
            patch(
                "gateway.autoresearch.persistence.provision_quantipy_experiment_runs_root"
            ) as provision,
            patch("gateway.cli._git_status_short", return_value=None),
            patch("gateway.cli._active_target_writer_processes", return_value=()),
        ):
            result = runner.invoke(
                app,
                [
                    "autoresearch-next",
                    str(state_path),
                    "--quantipy-root",
                    str(quantipy_root),
                    "--readiness-manifest",
                    str(readiness_path),
                ],
            )

        assert result.exit_code == 0, result.output
        assert load_policy.call_args.args[0] == (
            cli_module._PROJECT_ROOT / "gateway/openclaw_config/openclaw.json"
        )
        seal_runtime.assert_not_called()
        require_runtime.assert_not_called()
        provision.assert_not_called()

    def test_autoresearch_create_command_file_reads_secure_stdin_protocol(
        self,
        tmp_path: Path,
    ) -> None:
        command_file = tmp_path / "command.json"

        result = runner.invoke(
            app,
            [
                "autoresearch-create-command-file",
                "--output",
                str(command_file),
            ],
            input=json.dumps(
                {"schema_version": 1, "command": ["verify-command", "--opaque-value"]}
            ),
        )

        assert result.exit_code == 0, result.output
        assert json.loads(command_file.read_text(encoding="utf-8")) == {
            "command": ["verify-command", "--opaque-value"]
        }
        assert "verify-command" not in result.output

    @pytest.mark.parametrize(
        ("cmdline", "touches_root", "expected_count"),
        [
            ("uv run python -m quantipy.api --port 8000", True, 0),
            ("uv run pytest tests/test_alpha.py", True, 1),
            ("jupyter nbconvert --execute notebooks/experiments/t999.ipynb", True, 1),
            ("uv run python scripts/experiments/generate_t999.py", True, 1),
            ("uv run pytest tests/test_alpha.py", False, 0),
        ],
    )
    def test_target_writer_detection_scopes_writers(
        self,
        tmp_path: Path,
        cmdline: str,
        touches_root: bool,
        expected_count: int,
    ) -> None:
        proc_dir = tmp_path / "101"
        proc_dir.mkdir()

        def _fake_read_bytes(path: Path) -> bytes:
            if path == proc_dir / "cmdline":
                return cmdline.replace(" ", "\x00").encode()
            raise FileNotFoundError

        with (
            patch("gateway.cli.Path.glob", return_value=[proc_dir]),
            patch("gateway.cli.Path.read_bytes", autospec=True, side_effect=_fake_read_bytes),
            patch("gateway.cli.os.getpid", return_value=999),
            patch("gateway.cli.os.getppid", return_value=998),
            patch("gateway.cli._process_touches_path", return_value=touches_root),
        ):
            offenders = _active_target_writer_processes(Path("/home/dev/repos/quantipy"))

        assert len(offenders) == expected_count


# ---------------------------------------------------------------------------
# Local IP helper
# ---------------------------------------------------------------------------


class TestInitEnvG2App:
    """init-env generates g2_app/.env.local when g2_app/ exists."""

    def test_creates_env_local_when_g2_app_exists(self, tmp_path: Path) -> None:
        (tmp_path / "g2_app").mkdir()
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.50"),
            patch("gateway.cli.secrets.token_hex", return_value="aabbccdd" * 6),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        env_local = tmp_path / "g2_app" / ".env.local"
        assert env_local.exists()
        content = env_local.read_text()
        assert "VITE_GATEWAY_URL=ws://192.168.1.50:8765?token=" in content
        assert "aabbccdd" * 6 in content
        assert content.startswith("# Auto-generated by: python -m gateway init-env")

    def test_skips_when_g2_app_dir_missing(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.50"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert not (tmp_path / "g2_app" / ".env.local").exists()

    def test_force_overwrites_existing_env_local(self, tmp_path: Path) -> None:
        g2_dir = tmp_path / "g2_app"
        g2_dir.mkdir()
        (g2_dir / ".env.local").write_text("OLD_CONTENT=1\n")
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.5"),
        ):
            result = runner.invoke(app, ["init-env", "--force", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        content = (g2_dir / ".env.local").read_text()
        assert "VITE_GATEWAY_URL=ws://10.0.0.5:8765?token=" in content
        assert "OLD_CONTENT" not in content

    def test_existing_env_local_without_force_warns(self, tmp_path: Path) -> None:
        g2_dir = tmp_path / "g2_app"
        g2_dir.mkdir()
        (g2_dir / ".env.local").write_text("KEEP=1\n")
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.5"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        # Original file untouched
        assert (g2_dir / ".env.local").read_text() == "KEEP=1\n"
        assert "already exists" in result.output

    def test_url_format_correct(self, tmp_path: Path) -> None:
        (tmp_path / "g2_app").mkdir()
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="172.16.0.1"),
            patch("gateway.cli._get_tailscale_ip", return_value=None),
            patch("gateway.cli.secrets.token_hex", return_value="deadbeef" * 6),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        values = dotenv_values(tmp_path / "g2_app" / ".env.local")
        expected_url = "ws://172.16.0.1:8765?token=" + "deadbeef" * 6
        assert values["VITE_GATEWAY_URL"] == expected_url

    def test_summary_includes_g2_env(self, tmp_path: Path) -> None:
        (tmp_path / "g2_app").mkdir()
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.1"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "G2 app env" in result.output


class TestGetLocalIp:
    """_get_local_ip falls back gracefully."""

    def test_returns_string(self) -> None:
        ip = _get_local_ip()
        assert isinstance(ip, str)
        parts = ip.split(".")
        assert len(parts) == 4


# ---------------------------------------------------------------------------
# Vite simulator health helper
# ---------------------------------------------------------------------------


class TestViteHealthCheck:
    def test_rejects_vite_spa_fallback_html_with_status_200(self) -> None:
        response = _health_response(
            content_type="text/html; charset=utf-8",
            body=b"<!doctype html><html><body>G2 app</body></html>",
        )
        with patch("gateway.cli.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response

            assert not _vite_health_check(5173)

    def test_accepts_only_the_exact_simulator_health_payload(self) -> None:
        response = _health_response(
            content_type="application/json; charset=utf-8",
            body=b'{"ok":true}',
        )
        with patch("gateway.cli.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response

            assert _vite_health_check(5173)


# ---------------------------------------------------------------------------
# Simulator launch helper
# ---------------------------------------------------------------------------


class TestSimulatorLaunchCommand:
    """Simulator launch command handles headless shells explicitly."""

    def test_uses_direct_command_when_display_is_available(self) -> None:
        command = ["evenhub-simulator", "http://localhost:5173"]

        assert _simulator_launch_command(command, env={"DISPLAY": ":0"}) == command

    def test_wraps_with_xvfb_when_headless(self) -> None:
        command = ["evenhub-simulator", "http://localhost:5173"]

        with patch("gateway.cli.shutil.which", return_value="/usr/bin/xvfb-run"):
            wrapped = _simulator_launch_command(command, env={})

        assert wrapped == ["/usr/bin/xvfb-run", "-a", *command]

    def test_fails_when_headless_and_xvfb_is_missing(self) -> None:
        command = ["evenhub-simulator", "http://localhost:5173"]

        with (
            patch("gateway.cli.shutil.which", return_value=None),
            pytest.raises(_SimulatorLaunchError, match="DISPLAY/WAYLAND_DISPLAY"),
        ):
            _simulator_launch_command(command, env={})

    def test_preflight_fails_before_launch_when_headless_and_xvfb_is_missing(self) -> None:
        with (
            patch("gateway.cli.shutil.which", return_value=None),
            pytest.raises(_SimulatorLaunchError, match="DISPLAY/WAYLAND_DISPLAY"),
        ):
            _require_simulator_backend(env={})

    def test_preflight_accepts_headless_when_xvfb_is_available(self) -> None:
        with patch("gateway.cli.shutil.which", return_value="/usr/bin/xvfb-run"):
            _require_simulator_backend(env={})

    def test_immediate_simulator_exit_reports_log_tail(self, tmp_path: Path) -> None:
        log_path = tmp_path / "simulator.log"
        log_path.write_text("Failed to initialize GTK\n", encoding="utf-8")
        proc = MagicMock()
        proc.poll.return_value = 70

        with pytest.raises(_SimulatorLaunchError, match="Failed to initialize GTK"):
            _require_simulator_still_running(proc, log_path=log_path, timeout=0.1)

    def test_running_simulator_survives_startup_check(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.poll.return_value = None

        _require_simulator_still_running(proc, log_path=tmp_path / "simulator.log", timeout=0.1)


def test_vite_launch_uses_the_loopback_simulator_mode() -> None:
    assert _vite_launch_command() == ["npm", "run", "dev:sim"]


def test_openclaw_daemon_env_strips_azure_preload_from_default_codex_route() -> None:
    env = _openclaw_daemon_env(
        {
            "NODE_OPTIONS": "--require /home/dev/.openclaw/azure-api-version-preload.cjs",
        }
    )

    assert "NODE_OPTIONS" not in env
    assert env["MEMPALACE_EMBEDDING_MODEL"]


def test_openclaw_daemon_env_preserves_azure_preload_for_explicit_azure_route() -> None:
    env = _openclaw_daemon_env(
        {
            "OPENCLAW_PROVIDER": "azure",
            "NODE_OPTIONS": "--require /home/dev/.openclaw/azure-api-version-preload.cjs",
        }
    )

    assert env["NODE_OPTIONS"] == "--require /home/dev/.openclaw/azure-api-version-preload.cjs"


def test_openclaw_daemon_env_preserves_unrelated_node_options() -> None:
    env = _openclaw_daemon_env({"NODE_OPTIONS": "--max-old-space-size=4096"})

    assert env["NODE_OPTIONS"] == "--max-old-space-size=4096"


# ---------------------------------------------------------------------------
# push-config command
# ---------------------------------------------------------------------------


class TestPushConfig:
    """Tests for the push-config command."""

    @staticmethod
    def _resolved_openclaw(path: str = "/resolved/openclaw") -> _ResolvedOpenClaw:
        return _ResolvedOpenClaw(Path(path), "2026.7.1-2", (2026, 7, 1))

    def test_push_script_not_found(self, tmp_path: Path) -> None:
        """Error when the push script does not exist."""
        fake_root = tmp_path / "repo"
        fake_root.mkdir()
        with patch("gateway.cli._PROJECT_ROOT", fake_root):
            result = runner.invoke(app, ["push-config"])
        assert result.exit_code == 1
        assert "Push script not found" in result.output

    def test_push_script_fails(self) -> None:
        """Error when the push script exits non-zero."""
        fake_result = MagicMock(returncode=2)
        with (
            patch("gateway.cli._PROJECT_ROOT", Path("/fake")),
            patch(
                "gateway.cli.Path.is_file",
                side_effect=lambda self=None: True,
            ),
            patch(
                "gateway.cli._require_openclaw_binary",
                return_value=self._resolved_openclaw(),
            ),
            patch("gateway.cli.subprocess.run", return_value=fake_result),
        ):
            result = runner.invoke(app, ["push-config"])
        assert result.exit_code == 1
        assert "Push script failed" in result.output

    def test_push_only_no_restart(self, tmp_path: Path) -> None:
        """--no-restart pushes config without restarting the daemon."""
        # Create a fake push script
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        push_script = scripts_dir / "push-openclaw-config.sh"
        push_script.write_text("#!/bin/bash\nexit 0\n")
        push_script.chmod(0o755)

        calls: list[tuple[list[str], dict[str, object]]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append((cmd, kwargs))
            return MagicMock(returncode=0)

        with (
            patch("gateway.cli._PROJECT_ROOT", tmp_path),
            patch(
                "gateway.cli._require_openclaw_binary",
                return_value=self._resolved_openclaw(),
            ),
            patch("gateway.cli.subprocess.run", side_effect=_fake_run),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._is_port_open", return_value=False),
        ):
            result = runner.invoke(app, ["push-config", "--no-restart"])

        assert result.exit_code == 0
        assert "Skipped (--no-restart)" in result.output
        assert calls[0][0] == ["bash", str(push_script)]
        env = calls[0][1].get("env")
        assert isinstance(env, dict)
        assert env["OPENCLAW_BIN"] == "/resolved/openclaw"
        # Should NOT have called openclaw daemon restart
        for call, _kwargs in calls:
            assert "restart" not in call, f"Unexpected restart call: {call}"

    def test_push_and_restart(self, tmp_path: Path) -> None:
        """Push config then restart the daemon."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        push_script = scripts_dir / "push-openclaw-config.sh"
        push_script.write_text("#!/bin/bash\nexit 0\n")
        push_script.chmod(0o755)

        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(cmd)
            return MagicMock(returncode=0)

        with (
            patch("gateway.cli._PROJECT_ROOT", tmp_path),
            patch(
                "gateway.cli._require_openclaw_binary",
                return_value=self._resolved_openclaw(),
            ),
            patch("gateway.cli.subprocess.run", side_effect=_fake_run),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._wait_for_port", return_value=True),
            patch("gateway.cli._is_port_open", return_value=True),
        ):
            result = runner.invoke(app, ["push-config"])

        assert result.exit_code == 0
        # Should have called openclaw daemon restart
        restart_calls = [c for c in calls if "restart" in c]
        assert len(restart_calls) == 1
        assert restart_calls[0] == ["/resolved/openclaw", "daemon", "restart"]

    def test_restart_failure_does_not_accept_existing_listener(self, tmp_path: Path) -> None:
        """A failed restart is fatal even when the old daemon still owns the port."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        push_script = scripts_dir / "push-openclaw-config.sh"
        push_script.write_text("#!/bin/bash\nexit 0\n")
        push_script.chmod(0o755)

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            return MagicMock(returncode=3 if "restart" in cmd else 0, stderr="rejected")

        with (
            patch("gateway.cli._PROJECT_ROOT", tmp_path),
            patch(
                "gateway.cli._require_openclaw_binary",
                return_value=self._resolved_openclaw(),
            ),
            patch("gateway.cli.subprocess.run", side_effect=_fake_run),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._wait_for_port") as wait_for_port,
        ):
            result = runner.invoke(app, ["push-config"])

        assert result.exit_code == 1
        assert "Daemon restart failed (exit code 3)" in result.output
        wait_for_port.assert_not_called()


# ---------------------------------------------------------------------------
# stop command
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for the stop command process cleanup."""

    @staticmethod
    def _pgrep_side_effect(
        matches: dict[str, str],
    ) -> Callable[..., MagicMock]:
        """Return a side_effect for subprocess.run that simulates pgrep.

        *matches* maps a pgrep pattern substring to the stdout to return.
        """

        def _side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0] == "pgrep":
                pattern = cmd[-1]
                for key, stdout in matches.items():
                    if key in pattern:
                        return MagicMock(returncode=0, stdout=stdout)
            return MagicMock(returncode=1, stdout="")

        return _side_effect

    def test_stop_kills_openclaw_agent_processes(self) -> None:
        """When pgrep matches openclaw-agent, SIGTERM is sent to returned PIDs."""
        killed_signals: dict[int, list[int]] = {}

        def _fake_kill(pid: int, sig: int) -> None:
            killed_signals.setdefault(pid, []).append(sig)
            if sig == 0:
                raise ProcessLookupError

        side_effect = self._pgrep_side_effect({"openclaw-agent": "1001\n4242\n"})

        with (
            patch("gateway.cli.subprocess.run", side_effect=side_effect),
            patch("gateway.cli.os.kill", side_effect=_fake_kill),
            patch("gateway.cli.os.getpid", return_value=99999),
            patch("gateway.cli.os.getppid", return_value=99998),
            patch("gateway.cli.time.sleep"),
            patch("gateway.cli.time.monotonic", side_effect=[0, 0, 10, 10, 10, 10, 10, 10] * 10),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert signal.SIGTERM in killed_signals.get(1001, [])
        assert signal.SIGTERM in killed_signals.get(4242, [])

    def test_stop_kills_mempalace_mcp_processes(self) -> None:
        """When pgrep matches MemPalace MCP server, SIGTERM is sent to returned PIDs."""
        killed_signals: dict[int, list[int]] = {}

        def _fake_kill(pid: int, sig: int) -> None:
            killed_signals.setdefault(pid, []).append(sig)
            if sig == 0:
                raise ProcessLookupError

        side_effect = self._pgrep_side_effect({"mempalace": "2001\n"})

        with (
            patch("gateway.cli.subprocess.run", side_effect=side_effect),
            patch("gateway.cli.os.kill", side_effect=_fake_kill),
            patch("gateway.cli.os.getpid", return_value=99999),
            patch("gateway.cli.os.getppid", return_value=99998),
            patch("gateway.cli.time.sleep"),
            patch("gateway.cli.time.monotonic", side_effect=[0, 0, 10, 10, 10, 10, 10, 10] * 10),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert signal.SIGTERM in killed_signals.get(2001, [])

    def test_stop_excludes_own_pid(self) -> None:
        """When pgrep returns the current process PID, it is excluded from kill targets."""
        own_pid = 5000
        killed_pids: set[int] = set()

        def _fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGTERM:
                killed_pids.add(pid)
            if sig == 0:
                raise ProcessLookupError

        side_effect = self._pgrep_side_effect({"openclaw-agent": f"{own_pid}\n3001\n"})

        with (
            patch("gateway.cli.subprocess.run", side_effect=side_effect),
            patch("gateway.cli.os.kill", side_effect=_fake_kill),
            patch("gateway.cli.os.getpid", return_value=own_pid),
            patch("gateway.cli.os.getppid", return_value=99998),
            patch("gateway.cli.time.sleep"),
            patch("gateway.cli.time.monotonic", side_effect=[0, 0, 10, 10, 10, 10, 10, 10] * 10),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert own_pid not in killed_pids
        assert 3001 in killed_pids

    def test_signal_process_group_does_not_signal_callers_group(self) -> None:
        killed_pids: list[tuple[int, int]] = []
        killed_groups: list[tuple[int, int]] = []

        with (
            patch("gateway.cli.os.getpgid", return_value=777),
            patch("gateway.cli.os.getpgrp", return_value=777),
            patch(
                "gateway.cli.os.kill",
                side_effect=lambda pid, sig: killed_pids.append((pid, sig)),
            ),
            patch(
                "gateway.cli.os.killpg",
                side_effect=lambda pgid, sig: killed_groups.append((pgid, sig)),
            ),
        ):
            _signal_process_group(3001, signal.SIGTERM)

        assert killed_pids == [(3001, signal.SIGTERM)]
        assert killed_groups == []
