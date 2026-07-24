"""Tests for gateway.cli — init-env command."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from email.message import Message
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from unittest.mock import MagicMock, patch

import gateway.autoresearch_runner as autoresearch_runner
import pytest
from dotenv import dotenv_values
from gateway.autoresearch_readiness import (
    PLATFORM_READINESS_SCHEMA_VERSION,
    EvidenceId,
    PlatformReadinessManifest,
    canonical_platform_capabilities,
)
from gateway.autoresearch_runner import (
    DEFAULT_AUTORESEARCH_WORKTREE_ROOT,
    DEFAULT_OPENCLAW_CONFIG_PATH,
    MEMBER_UNION_DIGEST_ALGORITHM,
    QUANTIPY_RECEIPT_PATHS,
    AuthoritativeSnapshotReceipt,
    AutoresearchState,
    AutoresearchValidationContext,
    ComputeFitArtifact,
    ComputeTarget,
    ConsensusResultArtifact,
    ConsensusStatus,
    ContextPacketArtifact,
    DebateResultArtifact,
    DebateSubmission,
    DynamicUniverseCoverageReceipt,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    FixResultArtifact,
    FixTriggerPhase,
    GroupedSummaryReceipt,
    ImplementationResultArtifact,
    MemberUnionManifestReceipt,
    MetricDirection,
    Phase,
    PriceHydrationReceipt,
    PriceHydrationScopePreflight,
    ResearchMode,
    ReviewResultArtifact,
    ReviewVerdict,
    SetupContextArtifact,
    UniverseDateVerificationReceipt,
    UniverseHistoryBatchReceipt,
    UniversePlanArtifact,
    UniverseVerificationReceipt,
    VerificationResultArtifact,
    VerificationStatus,
    advance_state,
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
    load_autoresearch_policy,
    price_hydration_coverage_digest,
    price_hydration_request_digest,
)
from gateway.cli import (
    _active_target_writer_processes,
    _choose_whisper_model,
    _detect_gpu,
    _get_local_ip,
    _openclaw_daemon_env,
    _parse_gpu_output,
    _read_openclaw_config,
    _render_env,
    _require_simulator_backend,
    _require_simulator_still_running,
    _ResolvedOpenClaw,
    _signal_process_group,
    _simulator_launch_command,
    _SimulatorLaunchError,
    _vite_health_check,
    _vite_launch_command,
    app,
)
from typer.testing import CliRunner

from tests.gateway.autoresearch_fixtures import write_xnys_calendar_evidence

runner = CliRunner()
CAMPAIGN_XNYS_START = "2022-01-03"
CAMPAIGN_XNYS_END = "2025-12-31"


@pytest.fixture(autouse=True)
def isolated_autoresearch_lock_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autoresearch_runner,
        "AUTORESEARCH_LOCK_NAMESPACE",
        tmp_path / "autoresearch-locks",
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
                        selected_member_count=17,
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
        member_union_count=17,
        member_union_digest="e" * 64,
        member_union_manifest=MemberUnionManifestReceipt(
            path="/tmp/quantipy-member-union.txt", sha256="e" * 64
        ),
    )


def _hydration_receipt() -> PriceHydrationReceipt:
    request_digest = price_hydration_request_digest(
        member_union_count=17,
        member_union_digest="e" * 64,
        experiment_start="2021-01-04",
        experiment_end="2021-12-31",
        timeframe="1min",
        market_hours="regular",
    )
    completed_at = "2026-07-15T12:00:00+00:00"
    return PriceHydrationReceipt(
        member_union_count=17,
        member_union_digest="e" * 64,
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
        member_union_count=17,
        member_union_digest="e" * 64,
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


@pytest.mark.parametrize("in_place", [False, True], ids=("distinct-output", "in-place"))
def test_autoresearch_migrate_state_smoke(tmp_path: Path, *, in_place: bool) -> None:
    readiness = _ready_manifest(tmp_path / "migration-readiness")
    raw = AutoresearchState(platform_readiness=readiness.identity()).to_dict()
    del raw["schema_version"]
    source = tmp_path / "live-schema-less.json"
    output = source if in_place else tmp_path / "live-v2.json"
    source.write_text(json.dumps(raw), encoding="utf-8")

    result = runner.invoke(
        app,
        ["autoresearch-migrate-state", str(source), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 2


def test_autoresearch_init_state_pins_readiness(tmp_path: Path) -> None:
    readiness = _ready_manifest(tmp_path / "init-readiness")
    readiness_path = tmp_path / "platform-readiness.json"
    _write_readiness_manifest(readiness_path, readiness)
    output = tmp_path / "pristine-v2.json"

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
    state = AutoresearchState.from_dict(json.loads(output.read_text(encoding="utf-8")))
    assert state.platform_readiness == readiness.identity()


def _ready_manifest(tmp_path: Path) -> PlatformReadinessManifest:
    evidence: dict[str, dict[str, str | None]] = {}
    tmp_path.mkdir(parents=True, exist_ok=True)
    for evidence_id in EvidenceId:
        path = tmp_path / f"{evidence_id.value}.json"
        if evidence_id is EvidenceId.XNYS_TRADING_CALENDAR:
            write_xnys_calendar_evidence(path)
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
    worktree_root.mkdir(parents=True)
    monkeypatch.setattr(
        autoresearch_runner,
        "DEFAULT_AUTORESEARCH_WORKTREE_ROOT",
        worktree_root,
    )
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
    _git(target_checkout, "worktree", "add", "-b", "autoresearch", str(workspace))
    (workspace / "experiment.txt").write_text("implementation\n", encoding="utf-8")
    _git(workspace, "add", "experiment.txt")
    _git(workspace, "commit", "-m", "implementation")
    implementation_commit = _git(workspace, "rev-parse", "HEAD")
    (workspace / "experiment.txt").write_text("fixed\n", encoding="utf-8")
    _git(workspace, "add", "experiment.txt")
    _git(workspace, "commit", "-m", "fix")
    final_commit = _git(workspace, "rev-parse", "HEAD")
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
                session_count=252,
                planned_symbol_sessions=252,
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
                "state_reference_sha256": autoresearch_runner.build_authoritative_state_reference(
                    state,
                    state_path=state_path,
                ).sha256(),
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
        state_reference_sha256 = autoresearch_runner.build_authoritative_state_reference(
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
        original_persist = autoresearch_runner.persist_derived_state

        def mutate_source_then_persist(
            source_path: Path,
            output_path: Path,
            source_state: AutoresearchState,
            derived_state: AutoresearchState,
        ) -> None:
            changed_source_state = replace(source_state, iteration=source_state.iteration + 1)
            source_path.write_text(
                json.dumps(changed_source_state.to_dict()),
                encoding="utf-8",
            )
            original_persist(source_path, output_path, source_state, derived_state)

        with patch.object(
            autoresearch_runner,
            "persist_derived_state",
            new=mutate_source_then_persist,
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
        state_reference_sha256 = autoresearch_runner.build_authoritative_state_reference(
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

        updated_preflight = PriceHydrationScopePreflight(
            member_union_count=2,
            experiment_start="2021-01-04",
            experiment_end="2021-12-31",
            timeframe="1min",
            market_hours="regular",
            session_count=252,
            planned_symbol_sessions=504,
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
        state_reference_sha256 = autoresearch_runner.build_authoritative_state_reference(
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

    def test_autoresearch_mark_memory_and_start_next_persist_state(
        self,
        tmp_path: Path,
    ) -> None:
        debate_agent_ids = (
            "debater-microstructure",
            "debater-data",
            "debater-skeptic",
            "debater-theory",
            "debater-implementation",
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
                )
                for index, agent_id in enumerate(debate_agent_ids, start=1)
            ),
        )
        consensus = ConsensusResultArtifact(
            round_number=1,
            status=ConsensusStatus.MAJORITY,
            winner_theory_id="theory-1",
            winner_theory_family="vwap-obv",
            majority_count=5,
            majority_agent_ids=debate_agent_ids,
            dissenting_positions=(),
            novelty_score=0.6,
            theory_score=0.7,
            implementation_risk_score=0.3,
            data_adequacy_score=0.9,
            overfit_risk_score=0.2,
            expected_net_sharpe=0.5,
            rejection_reasons=(),
            implementation_brief="Implement the narrow VWAP and OBV experiment.",
            dissent_summary="The panel reached consensus.",
            universe_plan=_universe_plan(),
        )
        verification = VerificationResultArtifact(
            status=VerificationStatus.PASS,
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
            tests_passed=True,
            commands_run=("uv run pytest",),
            data_coverage=_dynamic_coverage(),
            universe_verification_receipt=_universe_receipt(),
            price_hydration_receipt=_hydration_receipt(),
        )
        readiness = _ready_manifest(tmp_path / "readiness")
        readiness_path = tmp_path / "platform-readiness.json"
        _write_readiness_manifest(readiness_path, readiness)
        repeat_state = AutoresearchState(
            phase=Phase.REPEAT,
            iteration=3,
            setup=SetupContextArtifact(
                goal="Find a profitable intraday alpha",
                metric_name="OOS Sharpe net",
                metric_direction=MetricDirection.MAXIMIZE,
                target_repo="/home/dev/repos/quantipy",
                writable_scope="src/quantipy/alpha",
                baseline_summary="Baseline OOS Sharpe net is 0.18.",
                hard_constraints=("No overnight holds",),
                data_sources=("qp.prices()",),
            ),
            context_packet=ContextPacketArtifact(
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
            debate_rounds=(debate,),
            consensus_history=(consensus,),
            implementation_result=ImplementationResultArtifact(
                summary="Implemented the narrow VWAP and OBV experiment.",
                workspace_path=str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT / "iteration-3"),
                commit_sha="abc1234",
                module_path="src/quantipy/alpha/vwap_obv/",
                notebook_path="notebooks/experiments/vwap_obv.ipynb",
                tests_added_or_updated=("tests/test_vwap_obv.py",),
                commands_run=("uv run pytest tests/test_vwap_obv.py",),
                price_hydration_scope_preflight=PriceHydrationScopePreflight(
                    member_union_count=1,
                    experiment_start="2021-01-04",
                    experiment_end="2021-12-31",
                    timeframe="1min",
                    market_hours="regular",
                    session_count=252,
                    planned_symbol_sessions=252,
                    within_budget=True,
                ),
            ),
            verification_history=(verification,),
            review_history=(
                ReviewResultArtifact(
                    reviewer_agent_id="reviewer",
                    verdict=ReviewVerdict.PASS,
                    recommended_metric_name="OOS Sharpe net",
                    recommended_metric_value=0.38,
                    critical_issues=(),
                    noncritical_issues=(),
                    fix_requests=(),
                    summary="Methodology review passed.",
                ),
            ),
            final_decision=FinalDecisionArtifact(
                experiment_id="iteration-3",
                decision=FinalDecision.KEEP,
                recommended_metric_name="OOS Sharpe net",
                recommended_metric_value=0.38,
                reviewer_verdict=FinalReviewerVerdict.PASS,
                rationale="Improves baseline without review blockers.",
                log_summary="KEEP vwap_obv_intraday with updated baseline review.",
                continue_loop=True,
                memory_write_required=True,
            ),
            mode=ResearchMode.ALPHA_RESEARCH,
            platform_readiness=readiness.identity(),
        )
        state_path = tmp_path / "repeat-state.json"
        memory_state_path = tmp_path / "memory-state.json"
        next_state_path = tmp_path / "next-state.json"
        kg_path = tmp_path / "knowledge_graph.sqlite3"
        connection = sqlite3.connect(kg_path)
        connection.executescript(
            """
            CREATE TABLE triples (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
                object TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
                source_file TEXT, source_drawer_id TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO triples VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)",
            [
                ("1", "iteration-3", "decision", "keep", "result.json"),
                ("2", "iteration-3", "research_mode", "alpha_research", "result.json"),
                ("3", "iteration-3", "alpha_decision_metric", "oos_sharpe_net_0_38", "result.json"),
                (
                    "4",
                    "iteration-3",
                    "data_window",
                    "2021_01_04_to_2021_12_31_oos_2021_10_01_to_2021_12_31",
                    "result.json",
                ),
                ("5", "iteration-3", "reviewer_verdict", "pass", "result.json"),
                (
                    "6",
                    "iteration-3",
                    "keeper_rationale",
                    "improves_baseline_without_review_blockers",
                    "result.json",
                ),
            ],
        )
        connection.commit()
        connection.close()
        state_path.write_text(json.dumps(repeat_state.to_dict()), encoding="utf-8")

        mark_result = runner.invoke(
            app,
            [
                "autoresearch-mark-memory",
                str(state_path),
                "--output",
                str(memory_state_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--mempalace-kg-path",
                str(kg_path),
            ],
        )
        assert mark_result.exit_code == 0, mark_result.output
        marked = json.loads(memory_state_path.read_text(encoding="utf-8"))
        assert marked["memory_written"] is True
        assert marked["memory_verification_receipt"]["experiment_id"] == "iteration-3"

        next_result = runner.invoke(
            app,
            [
                "autoresearch-start-next",
                str(memory_state_path),
                "--output",
                str(next_state_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
                "--readiness-manifest",
                str(readiness_path),
            ],
        )
        assert next_result.exit_code == 0
        next_state = json.loads(next_state_path.read_text(encoding="utf-8"))
        assert next_state["phase"] == "setup_context"
        assert next_state["iteration"] == 4
        assert next_state["setup"]["metric_name"] == "OOS Sharpe net"

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

        side_effect = self._pgrep_side_effect({"openclaw-agent": "1001\n1002\n"})

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
        assert signal.SIGTERM in killed_signals.get(1002, [])

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
