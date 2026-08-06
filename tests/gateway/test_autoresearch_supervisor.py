from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from types import FrameType

import pytest
from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact,
    FinalDecisionArtifact,
    ImplementationResultArtifact,
    MemoryVerificationReceipt,
    PriceHydrationScopePreflight,
)
from gateway.autoresearch.configuration import (
    load_autoresearch_policy,
)
from gateway.autoresearch.constants import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    DEFAULT_QUANTIPY_ROOT,
)
from gateway.autoresearch.enums import (
    ConsensusStatus,
    FinalDecision,
    FinalReviewerVerdict,
    Phase,
    ResearchMode,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError,
)
from gateway.autoresearch.manifest_runtime import (
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
)
from gateway.autoresearch.state import (
    AutoresearchState,
)
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference,
)
from gateway.autoresearch_readiness import (
    PLATFORM_READINESS_SCHEMA_VERSION,
    EvidenceId,
    PlatformReadinessManifest,
    ReadinessIdentity,
    canonical_platform_capabilities,
)
from gateway.autoresearch_runs import (
    RunFailureClassification,
    RunState,
    command_sha256,
    complete_run,
    prepare_run,
    start_run,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
    DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS,
    DEFAULT_RENUDGE_ALERT_LIMIT,
    DEFAULT_RENUDGE_IDLE_SECONDS,
    MISSING_VERIFICATION_ARTIFACT_RECOVERY_MESSAGE,
    RECOVERY_MESSAGE,
    AutoresearchSupervisor,
    NativeGatewayRPC,
    OpenClawRPC,
    OpenClawUnavailableError,
    RecoveryStatus,
    ShutdownInterrupted,
    ShutdownRequested,
    SupervisorCheckpoint,
    SupervisorConfig,
    SupervisorError,
    SupervisorOutcome,
    SupervisorResult,
    WakeDeliveryProof,
    main,
    make_idempotency_key,
    memory_wake_acknowledgement_key,
    reset_recovery_checkpoint_for_manual_wake,
)
from gateway.openclaw_client import OpenClawError, OpenClawTransportError

from tests.gateway.autoresearch_fixtures import write_xnys_calendar_evidence

SignalHandler = Callable[[int, FrameType | None], None]
SignalDisposition = SignalHandler | signal.Handlers


def test_recovery_message_uses_bounded_current_attempt_reconciliation() -> None:
    for message in (RECOVERY_MESSAGE, MISSING_VERIFICATION_ARTIFACT_RECOVERY_MESSAGE):
        normalized = message.lower()
        assert "current" in normalized
        assert "task metadata" in normalized
        assert "do not enumerate historical sessions" in normalized
        assert "fetch old full transcripts" in normalized
        assert "exact current label" in normalized


@pytest.mark.parametrize(
    ("seam_module", "reexports"),
    [
        (
            "gateway.autoresearch_rpc",
            (
                "OpenClawRPC",
                "NativeGatewayRPC",
                "WakeDeliveryProof",
                "TaskGateway",
                "make_idempotency_key",
            ),
        ),
        (
            "gateway.autoresearch_reconciliation",
            (
                "TaskProvenance",
                "CanonicalTaskStatus",
                "ReconciledRunningTasks",
                "classify_autoresearch_task",
                "reconcile_relevant_running_tasks",
            ),
        ),
        (
            "gateway.autoresearch_checkpoint",
            (
                "RecoveryRecord",
                "MemoryWakeAcknowledgement",
                "SupervisorCheckpoint",
                "reset_recovery_checkpoint_for_manual_wake",
                "_optional_float",
            ),
        ),
    ],
)
def test_each_extracted_seam_imports_before_supervisor(
    seam_module: str, reexports: tuple[str, ...]
) -> None:
    script = """
import importlib
import sys

importlib.import_module(sys.argv[1])
supervisor = importlib.import_module("gateway.autoresearch_supervisor")
missing = [name for name in sys.argv[2:] if not hasattr(supervisor, name)]
if missing:
    raise SystemExit(f"missing supervisor re-exports: {missing}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, seam_module, *reexports],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _operator_precondition_state_json() -> str:
    return json.dumps(
        AutoresearchState(
            phase=Phase.IMPLEMENTATION,
            iteration=26,
            mode=ResearchMode.DATA_INFRA_G0,
            consensus_history=(
                ConsensusResultArtifact(
                    round_number=1,
                    status=ConsensusStatus.MAJORITY,
                    winner_theory_id="i26-operator-evidence-precondition",
                    winner_theory_family="no-code-operator-evidence-precondition",
                    majority_count=5,
                    majority_agent_ids=(
                        "debater_microstructure",
                        "debater_data",
                        "debater_skeptic",
                        "debater_theory",
                        "debater_implementation",
                    ),
                    dissenting_positions=(),
                    novelty_score=1.0,
                    theory_score=9.0,
                    implementation_risk_score=1.0,
                    data_adequacy_score=1.0,
                    overfit_risk_score=1.0,
                    expected_net_sharpe=0.0,
                    rejection_reasons=("missing operator evidence",),
                    implementation_brief=(
                        "Do not enter ENGINEER and do not modify Quantipy. "
                        "The operator must supply the manifest."
                    ),
                    dissent_summary="No semantic dissent.",
                ),
            ),
        ).to_dict()
    )


@dataclass(slots=True)
class SignalHarness:
    """Installs and invokes supervisor signal handlers without OS-level signals."""

    handlers: dict[int, SignalDisposition] = field(default_factory=dict)

    def install(self, signum: int, handler: SignalDisposition) -> SignalDisposition:
        previous = self.handlers.get(signum, signal.SIG_DFL)
        self.handlers[signum] = handler
        return previous

    def trigger(self, signum: int) -> None:
        handler = self.handlers[signum]
        assert callable(handler)
        handler(signum, None)


def _write_state(
    path: Path,
    *,
    phase: Phase = Phase.VERIFICATION,
    iteration: int = 4,
    implementation_result: ImplementationResultArtifact | None = None,
    platform_readiness: ReadinessIdentity | None = None,
) -> None:
    state = AutoresearchState(
        phase=phase,
        iteration=iteration,
        mode=ResearchMode.ALPHA_RESEARCH,
        implementation_result=implementation_result,
        platform_readiness=platform_readiness,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")


def _write_git_markers(repo_root: Path) -> list[Path]:
    git_dir = repo_root / ".git"
    paths = [git_dir / "HEAD", git_dir / "index", git_dir / "logs" / "HEAD"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("marker", encoding="utf-8")
    return paths


def _make_stale(paths: list[Path], *, now: float) -> None:
    for path in paths:
        os.utime(path, (now - 600.0, now - 600.0))


class FakeOpenClaw:
    def __init__(
        self,
        *,
        tasks: list[dict[str, object]] | None = None,
        shown_tasks: dict[str, dict[str, object]] | None = None,
        task_list_failures_before_success: int = 0,
    ) -> None:
        self.tasks = tasks or []
        self.shown_tasks = shown_tasks
        self.task_list_failures_before_success = task_list_failures_before_success
        self.task_list_calls = 0
        self.rpc_calls: list[tuple[str, Mapping[str, object]]] = []
        self.agent_payload: dict[str, object] = {
            "status": "accepted",
            "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
            "runId": "run-4",
        }

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        del shutdown_requested
        self.rpc_calls.append((method, params))
        if method == "tasks.list":
            if self.task_list_calls < self.task_list_failures_before_success:
                self.task_list_calls += 1
                raise OpenClawUnavailableError("poll failed")
            self.task_list_calls += 1
            return {"tasks": self.tasks}
        if method == "tasks.get":
            task_id = params["taskId"]
            assert isinstance(task_id, str)
            if self.shown_tasks is not None:
                return {"task": self.shown_tasks[task_id]}
            task = next(task for task in self.tasks if task.get("taskId") == task_id).copy()
            task.setdefault("status", "running")
            return {"task": task}
        if method == "agent":
            return self.agent_payload
        if method == "sessions.delete":
            return {"ok": True, "key": AUTORESEARCH_OWNER_SESSION_KEY, "deleted": True}
        if method == "sessions.abort":
            return {"ok": True, "status": "aborted", "abortedRunId": params["runId"]}
        if method == "tasks.cancel":
            task_id = params["taskId"]
            return {"found": True, "cancelled": True, "task": {"id": task_id, "taskId": task_id}}
        raise AssertionError(f"unexpected RPC method: {method}")


class FailingTaskListOpenClaw(FakeOpenClaw):
    def __init__(self, *, before_failure: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._before_failure = before_failure

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        if method == "tasks.list":
            if self._before_failure is not None:
                self._before_failure()
            raise OpenClawUnavailableError("poll failed")
        return super().request(method, params, shutdown_requested=shutdown_requested)


class FailingTaskShowOpenClaw(FakeOpenClaw):
    def __init__(
        self,
        *,
        tasks: list[dict[str, object]],
        before_failure: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(tasks=tasks)
        self._before_failure = before_failure

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        if method == "tasks.get":
            if self._before_failure is not None:
                self._before_failure()
            if shutdown_requested():
                raise ShutdownInterrupted("task RPC interrupted during shutdown")
            raise SupervisorError("task missing")
        return super().request(method, params, shutdown_requested=shutdown_requested)


class FailingWakeOpenClaw(FakeOpenClaw):
    def __init__(self, *, stderr: str) -> None:
        super().__init__()
        self._stderr = stderr

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        if method == "agent":
            raise SupervisorError(self._stderr)
        return super().request(method, params, shutdown_requested=shutdown_requested)


@dataclass(frozen=True, slots=True)
class SupervisorEnv:
    now: float
    state_path: Path
    repo_root: Path
    marker_paths: list[Path]
    sessions_path: Path
    proc_root: Path
    checkpoint_path: Path
    readiness_manifest_path: Path
    readiness_identity: ReadinessIdentity
    runs_root: Path
    launch_requests_path: Path
    stage_inbox_path: Path


@pytest.fixture()
def supervisor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SupervisorEnv:
    monkeypatch.delenv("OPENCLAW_BIN", raising=False)
    now = 1_000_000.0
    state_path = tmp_path / "autoresearch" / "quantipy-state.json"
    repo_root = tmp_path / "quantipy"
    repo_root.mkdir()
    marker_paths = _write_git_markers(repo_root)
    sessions_path = tmp_path / "owner-sessions.json"
    sessions_path.write_text("{}", encoding="utf-8")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    readiness_evidence = tmp_path / "readiness-evidence"
    readiness_evidence.mkdir()
    evidence: dict[str, dict[str, str | None]] = {}
    for evidence_id in EvidenceId:
        evidence_path = readiness_evidence / f"{evidence_id.value}.json"
        if evidence_id is EvidenceId.QUANTIPY_DATA_CONTRACT:
            evidence_path.write_text(
                json.dumps({"quantipy_commit": "a" * 40}),
                encoding="utf-8",
            )
        elif evidence_id is EvidenceId.XNYS_TRADING_CALENDAR:
            write_xnys_calendar_evidence(evidence_path)
        else:
            evidence_path.write_text(f"{evidence_id.value}\n", encoding="utf-8")
        evidence[evidence_id.value] = {
            "path": str(evidence_path),
            "sha256": sha256(evidence_path.read_bytes()).hexdigest(),
            "reason": None,
        }
    readiness = PlatformReadinessManifest.from_dict(
        {
            "schema_version": PLATFORM_READINESS_SCHEMA_VERSION,
            "status": "READY",
            "manifest_id": "supervisor-manifest-1",
            "snapshot_id": "supervisor-snapshot-1",
            "evidence": evidence,
            "capabilities": canonical_platform_capabilities().to_dict(),
            "reason": None,
        }
    )
    readiness_manifest_path = tmp_path / "platform-readiness.json"
    readiness_manifest_path.write_text(json.dumps(readiness.to_dict()), encoding="utf-8")
    return SupervisorEnv(
        now=now,
        state_path=state_path,
        repo_root=repo_root,
        marker_paths=marker_paths,
        sessions_path=sessions_path,
        proc_root=proc_root,
        checkpoint_path=tmp_path / "autoresearch" / "owner-recovery.json",
        readiness_manifest_path=readiness_manifest_path,
        readiness_identity=readiness.identity(),
        runs_root=tmp_path / "runs",
        launch_requests_path=tmp_path / "launch-requests",
        stage_inbox_path=tmp_path / "stage-inbox",
    )


def _supervisor(
    env: SupervisorEnv,
    fake: FakeOpenClaw,
    *,
    expected_stage_task_stale_seconds: float = 300.0,
    renudge_idle_seconds: float = DEFAULT_RENUDGE_IDLE_SECONDS,
    renudge_alert_limit: int = DEFAULT_RENUDGE_ALERT_LIMIT,
    grace_period_seconds: float = 120.0,
    now: Callable[[], float] | None = None,
) -> AutoresearchSupervisor:
    return AutoresearchSupervisor(
        SupervisorConfig(
            state_path=env.state_path,
            readiness_manifest_path=env.readiness_manifest_path,
            checkpoint_path=env.checkpoint_path,
            autoresearch_dir=env.state_path.parent,
            owner_sessions_path=env.sessions_path,
            target_repo=env.repo_root,
            proc_root=env.proc_root,
            runs_root=env.runs_root,
            grace_period_seconds=grace_period_seconds,
            renudge_idle_seconds=renudge_idle_seconds,
            renudge_alert_limit=renudge_alert_limit,
            expected_stage_task_stale_seconds=expected_stage_task_stale_seconds,
            launch_requests_path=env.launch_requests_path,
            stage_inbox_path=env.stage_inbox_path,
        ),
        now=(lambda: env.now) if now is None else now,
        sleep=lambda _: None,
        task_gateway=fake,
    )


def test_expected_stage_task_stale_default_allows_long_stage_turns() -> None:
    assert DEFAULT_EXPECTED_STAGE_TASK_STALE_SECONDS == 900.0
    assert DEFAULT_RENUDGE_IDLE_SECONDS == 600.0
    assert DEFAULT_RENUDGE_ALERT_LIMIT == 12


def test_native_gateway_rpc_reports_a_websocket_timeout_without_cli_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutClient:
        closed = False

        def __init__(self, host: str, port: int, token: str) -> None:
            del host, port, token

        async def request_once(
            self,
            method: str,
            params: Mapping[str, object],
            *,
            timeout_seconds: float,
            required_server_version: str,
        ) -> Mapping[str, object]:
            del method, params, timeout_seconds, required_server_version
            try:
                raise OpenClawTransportError("timed out waiting for tasks.list response")
            finally:
                TimedOutClient.closed = True

    monkeypatch.setattr("gateway.autoresearch_supervisor.OpenClawClient", TimedOutClient)

    with pytest.raises(OpenClawUnavailableError, match=r"gateway RPC failed.*timed out"):
        NativeGatewayRPC("127.0.0.1", 18789, "test-token").request(
            "tasks.list", {"status": "running"}, shutdown_requested=lambda: False
        )

    assert TimedOutClient.closed is True


def test_native_gateway_rpc_preserves_permanent_protocol_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectedClient:
        def __init__(self, host: str, port: int, token: str) -> None:
            del host, port, token

        async def request_once(
            self,
            method: str,
            params: Mapping[str, object],
            *,
            timeout_seconds: float,
            required_server_version: str,
        ) -> Mapping[str, object]:
            del method, params, timeout_seconds, required_server_version
            raise OpenClawError("auth rejected: bad token")

    monkeypatch.setattr("gateway.autoresearch_supervisor.OpenClawClient", RejectedClient)

    with pytest.raises(SupervisorError, match="auth rejected") as raised:
        NativeGatewayRPC("127.0.0.1", 18789, "test-token").request(
            "tasks.list", {"status": "running"}, shutdown_requested=lambda: False
        )

    assert not isinstance(raised.value, OpenClawUnavailableError)


def test_task_listing_does_not_retry_permanent_gateway_failures() -> None:
    class PermanentlyRejectedGateway(FakeOpenClaw):
        def request(
            self,
            method: str,
            params: Mapping[str, object],
            *,
            shutdown_requested: ShutdownRequested,
        ) -> Mapping[str, object]:
            del params, shutdown_requested
            self.task_list_calls += 1
            raise SupervisorError(f"{method} request rejected: unauthorized")

    gateway = PermanentlyRejectedGateway()

    with pytest.raises(SupervisorError, match="unauthorized") as raised:
        OpenClawRPC(gateway).list_running_tasks()

    assert not isinstance(raised.value, OpenClawUnavailableError)
    assert gateway.task_list_calls == 1


def test_native_gateway_rpc_cancels_an_inflight_websocket_request_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingClient:
        cancelled = False
        started = False

        def __init__(self, host: str, port: int, token: str) -> None:
            del host, port, token

        async def request_once(
            self,
            method: str,
            params: Mapping[str, object],
            *,
            timeout_seconds: float,
            required_server_version: str,
        ) -> Mapping[str, object]:
            del method, params, timeout_seconds, required_server_version
            BlockingClient.started = True
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                BlockingClient.cancelled = True
                raise
            raise AssertionError("blocking RPC unexpectedly completed")

    monkeypatch.setattr("gateway.autoresearch_supervisor.OpenClawClient", BlockingClient)

    def shutdown_requested() -> bool:
        return BlockingClient.started

    with pytest.raises(ShutdownInterrupted, match="gateway RPC interrupted"):
        NativeGatewayRPC("127.0.0.1", 18789, "test-token").request(
            "tasks.list", {"status": "running"}, shutdown_requested=shutdown_requested
        )

    assert BlockingClient.cancelled is True


async def test_native_gateway_rpc_rejects_calls_from_a_running_event_loop() -> None:
    rpc = NativeGatewayRPC("127.0.0.1", 18789, "test-token")

    with pytest.raises(SupervisorError, match="synchronous caller"):
        rpc.request("tasks.list", {"status": "running"}, shutdown_requested=lambda: False)


def _prepare_stale_state(env: SupervisorEnv, *, phase: Phase = Phase.VERIFICATION) -> None:
    _write_state(env.state_path, phase=phase, platform_readiness=env.readiness_identity)
    _make_stale([env.state_path, *env.marker_paths], now=env.now)


def _current_instruction_manifest_sha256(state: AutoresearchState, state_path: Path) -> str:
    return expected_instruction_manifest_sha256(
        state,
        load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH),
        build_receipt_catalog(DEFAULT_QUANTIPY_ROOT),
        state_path=state_path,
    )


def _write_launch_request(
    env: SupervisorEnv,
    *,
    name: str = "request.json",
    run_dir: Path | None = None,
    runs_root: Path | None = None,
    payload: dict[str, object] | None = None,
) -> Path:
    request_dir = env.launch_requests_path
    request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    request_dir.chmod(0o700)
    selected_runs_root = env.runs_root if runs_root is None else runs_root
    selected_run_dir = (
        env.runs_root / "iteration-4" / "verification" / "attempt-1" if run_dir is None else run_dir
    )
    if payload is None:
        selected_run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (selected_run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        payload = {
            "schema_version": 1,
            "run_dir": str(selected_run_dir),
            "runs_root": str(selected_runs_root),
        }
    request_path = request_dir / name
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    request_path.chmod(0o600)
    return request_path


def test_launch_request_inbox_accepts_one_request_and_invokes_prepared_launcher(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _write_launch_request(supervisor_env, name="attempt.json")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("gateway.autoresearch_supervisor.subprocess.run", fake_run)

    result = _supervisor(supervisor_env, FakeOpenClaw())._consume_launch_request_inbox()

    assert result == SupervisorResult(SupervisorOutcome.NUDGED, "launch_request_executed")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1:] == [
        str(Path(__file__).resolve().parents[2] / "scripts" / "run-long-task.sh"),
        "--launch-prepared",
        "--run-dir",
        str(supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"),
        "--runs-root",
        str(supervisor_env.runs_root),
    ]
    assert kwargs["cwd"] == Path(__file__).resolve().parents[2]
    assert kwargs["timeout"] == 30.0
    assert not request_path.exists()
    assert (supervisor_env.launch_requests_path / "accepted" / request_path.name).exists()


@pytest.mark.parametrize(
    "case",
    (
        "symlink",
        "hard_link",
        "oversized",
        "invalid_json",
        "wrong_schema",
        "relative_path",
        "outside_run_dir",
        "wrong_runs_root",
        "missing_run_dir",
        "missing_manifest",
        "status_exists",
    ),
)
def test_launch_request_inbox_rejects_invalid_requests(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    request_path = _write_launch_request(supervisor_env)
    if case == "symlink":
        target = supervisor_env.launch_requests_path / "target"
        target.write_bytes(request_path.read_bytes())
        target.chmod(0o600)
        request_path.unlink()
        request_path.symlink_to(target)
    elif case == "hard_link":
        target = supervisor_env.launch_requests_path / "hard-link-source"
        os.link(request_path, target)
    elif case == "oversized":
        request_path.write_text("x" * 4097, encoding="utf-8")
    elif case == "invalid_json":
        request_path.write_text("{", encoding="utf-8")
    elif case == "wrong_schema":
        request_path.write_text(
            json.dumps({"schema_version": 2, "run_dir": "", "runs_root": ""}),
            encoding="utf-8",
        )
    elif case == "relative_path":
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_dir": "relative/run",
                    "runs_root": str(supervisor_env.runs_root),
                }
            ),
            encoding="utf-8",
        )
    elif case == "outside_run_dir":
        outside = supervisor_env.state_path.parent / "outside-run"
        outside.mkdir(mode=0o700, parents=True)
        (outside / "manifest.json").write_text("{}", encoding="utf-8")
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_dir": str(outside),
                    "runs_root": str(supervisor_env.runs_root),
                }
            ),
            encoding="utf-8",
        )
    elif case == "wrong_runs_root":
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_dir": str(supervisor_env.runs_root / "run"),
                    "runs_root": str(supervisor_env.state_path.parent / "other-root"),
                }
            ),
            encoding="utf-8",
        )
    elif case == "missing_run_dir":
        missing = supervisor_env.runs_root / "missing-run"
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_dir": str(missing),
                    "runs_root": str(supervisor_env.runs_root),
                }
            ),
            encoding="utf-8",
        )
    elif case == "missing_manifest":
        run_dir = supervisor_env.runs_root / "missing-manifest"
        run_dir.mkdir(mode=0o700, parents=True)
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_dir": str(run_dir),
                    "runs_root": str(supervisor_env.runs_root),
                }
            ),
            encoding="utf-8",
        )
    elif case == "status_exists":
        run_dir = supervisor_env.runs_root / "started-run"
        run_dir.mkdir(mode=0o700, parents=True)
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        (run_dir / "status.json").write_text("{}", encoding="utf-8")
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_dir": str(run_dir),
                    "runs_root": str(supervisor_env.runs_root),
                }
            ),
            encoding="utf-8",
        )
    else:
        raise AssertionError(f"unhandled launch request test case: {case}")
    request_path.chmod(0o600)

    launcher_calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal launcher_calls
        launcher_calls += 1
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr("gateway.autoresearch_supervisor.subprocess.run", fake_run)

    result = _supervisor(supervisor_env, FakeOpenClaw())._consume_launch_request_inbox()

    assert result is None
    assert launcher_calls == 0
    assert not request_path.exists()
    assert any((supervisor_env.launch_requests_path / "rejected").iterdir())


def test_launch_request_inbox_executes_at_most_one_request_per_cycle(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _write_launch_request(supervisor_env, name="a.json")
    second = _write_launch_request(
        supervisor_env,
        name="b.json",
        run_dir=supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-2",
    )
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr("gateway.autoresearch_supervisor.subprocess.run", fake_run)

    result = _supervisor(supervisor_env, FakeOpenClaw())._consume_launch_request_inbox()

    assert result == SupervisorResult(SupervisorOutcome.NUDGED, "launch_request_executed")
    assert calls == 1
    assert not first.exists()
    assert second.exists()


def test_launch_request_inbox_launcher_failure_alerts_and_rejects_request(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _write_launch_request(supervisor_env)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="out", stderr="failed")

    monkeypatch.setattr("gateway.autoresearch_supervisor.subprocess.run", fake_run)

    result = _supervisor(supervisor_env, FakeOpenClaw())._consume_launch_request_inbox()

    assert result is not None
    assert result.outcome is SupervisorOutcome.ALERT
    assert result.recovery_key == f"launch-request:{request_path.name}"
    assert "launch_request_execution_failed" in result.reason
    assert not request_path.exists()
    assert (supervisor_env.launch_requests_path / "rejected" / request_path.name).exists()
    checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    assert checkpoint.recovery_records[result.recovery_key].alerted is True


def test_launch_request_inbox_rejection_failure_does_not_starve_other_entries(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor_env.launch_requests_path.mkdir(mode=0o700, parents=True)
    (supervisor_env.launch_requests_path / "a-invalid.json").mkdir(mode=0o700)
    (supervisor_env.launch_requests_path / "rejected").write_text("poisoned", encoding="utf-8")
    valid_request = _write_launch_request(supervisor_env, name="b-valid.json")
    launcher_calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal launcher_calls
        launcher_calls += 1
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr("gateway.autoresearch_supervisor.subprocess.run", fake_run)

    result = _supervisor(supervisor_env, FakeOpenClaw())._consume_launch_request_inbox()

    assert result == SupervisorResult(SupervisorOutcome.NUDGED, "launch_request_executed")
    assert launcher_calls == 1
    assert (supervisor_env.launch_requests_path / "a-invalid.json").is_dir()
    assert (supervisor_env.launch_requests_path / "rejected").is_file()
    assert not valid_request.exists()
    assert (supervisor_env.launch_requests_path / "accepted" / valid_request.name).exists()
    assert not supervisor_env.checkpoint_path.exists()


def test_launch_request_inbox_missing_directory_is_a_noop(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher_calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal launcher_calls
        launcher_calls += 1
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr("gateway.autoresearch_supervisor.subprocess.run", fake_run)

    result = _supervisor(supervisor_env, FakeOpenClaw())._consume_launch_request_inbox()

    assert result is None
    assert launcher_calls == 0


def test_stale_iteration_context_residue_does_not_defer_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    stale_context = supervisor_env.state_path.parent / "iteration-4-context.json"
    stale_context.write_text("{}", encoding="utf-8")
    os.utime(stale_context, (supervisor_env.now, supervisor_env.now))

    result = _supervisor(supervisor_env, FakeOpenClaw()).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED


def test_future_dated_git_marker_does_not_suppress_stale_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    future = supervisor_env.now + 86_400.0
    os.utime(supervisor_env.marker_paths[0], (future, future))

    result = _supervisor(supervisor_env, FakeOpenClaw()).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED


def test_current_git_marker_mtime_does_not_suppress_stale_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    os.utime(
        supervisor_env.marker_paths[0],
        (supervisor_env.now, supervisor_env.now),
    )

    result = _supervisor(supervisor_env, FakeOpenClaw()).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED


def test_symlinked_detached_run_record_alerts_without_waking_or_advancing_state(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )
    run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"
    command = ("verify", "--opaque")
    source_manifest = supervisor_env.state_path.parent / "detached-manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": 4,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verification",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": instruction_manifest_sha256,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=source_manifest,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )
    (run_dir / "status.json").symlink_to(source_manifest)

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason.startswith("invalid_detached_run_record:")
    assert not any(method == "agent" for method, _ in fake.rpc_calls)


def test_prepared_run_awaiting_queued_launch_is_not_a_malformed_record(
    supervisor_env: SupervisorEnv,
) -> None:
    # A valid manifest with no status.json is a run queued for the
    # supervisor-mediated launch, not a corrupted record.
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )
    run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"
    command = ("verify", "--opaque")
    source_manifest = supervisor_env.state_path.parent / "detached-manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": 4,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verification",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": instruction_manifest_sha256,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=source_manifest,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )

    result = supervisor.run_once()

    assert result.outcome is not SupervisorOutcome.ALERT
    assert not result.reason.startswith("invalid_detached_run_record:")


def test_newer_succeeded_detached_attempt_prevents_an_older_failure_from_advancing_state(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )

    for attempt, exit_code in ((1, 1), (2, 0)):
        run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / f"attempt-{attempt}"
        command = ("verify", f"attempt-{attempt}")
        source_manifest = supervisor_env.state_path.parent / f"detached-manifest-{attempt}.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "iteration": 4,
                    "phase": "verification",
                    "attempt": attempt,
                    "task_label": "verification",
                    "state_reference_sha256": state_reference_sha256,
                    "instruction_manifest_sha256": instruction_manifest_sha256,
                    "run_directory": str(run_dir),
                    "working_directory": str(supervisor_env.repo_root),
                    "command_sha256": command_sha256(command),
                    "expected_artifact_path": None,
                    "timeout_seconds": None,
                }
            ),
            encoding="utf-8",
        )
        prepare_run(
            manifest_path=source_manifest,
            run_dir=run_dir,
            runs_root=supervisor_env.runs_root,
            command=command,
        )
        start_run(run_dir=run_dir, pid=attempt, runs_root=supervisor_env.runs_root)
        complete_run(
            run_dir=run_dir,
            exit_code=exit_code,
            signal_number=None,
            peak_rss_bytes=None,
            runs_root=supervisor_env.runs_root,
        )

    result = supervisor._consume_terminal_verification_run(state)

    assert result is None


def test_operator_stopped_detached_verification_alerts_without_advancing_state(
    supervisor_env: SupervisorEnv,
) -> None:
    # Arrange
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state, state_path=supervisor_env.state_path
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state, supervisor_env.state_path
    )
    run_dir = supervisor_env.runs_root / "operator-stopped"
    manifest_path = supervisor_env.state_path.parent / "operator-stopped-manifest.json"
    command = ("verify", "--opaque")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": state.iteration,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verification",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": instruction_manifest_sha256,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )
    start_run(run_dir=run_dir, pid=999_999, runs_root=supervisor_env.runs_root)
    complete_run(
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        exit_code=143,
        signal_number=15,
        peak_rss_bytes=None,
        failure_classification=RunFailureClassification.OPERATOR_STOPPED,
    )

    # Act
    result = supervisor._consume_terminal_verification_run(state)

    # Assert
    assert result is not None
    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "interrupted_detached_verification_requires_operator_recovery"
    assert supervisor._load_state() == state


def test_detached_verification_instruction_digest_mismatch_is_ignored_as_stale_history(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"
    command = ("verify", "--opaque")
    source_manifest = supervisor_env.state_path.parent / "detached-manifest-mismatch.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": 4,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verification",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": "b" * 64,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=source_manifest,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )
    start_run(run_dir=run_dir, pid=123, runs_root=supervisor_env.runs_root)

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert result.reason == "recovery_message_sent"
    assert any(method == "agent" for method, _ in fake.rpc_calls)


def test_newest_matching_detached_run_ignores_older_well_formed_stale_retry_history(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )

    for attempt, instruction_digest in ((1, "b" * 64), (2, instruction_manifest_sha256)):
        run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / f"attempt-{attempt}"
        command = ("verify", f"attempt-{attempt}")
        source_manifest = supervisor_env.state_path.parent / f"detached-stale-{attempt}.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "iteration": 4,
                    "phase": "verification",
                    "attempt": attempt,
                    "task_label": "verification",
                    "state_reference_sha256": state_reference_sha256,
                    "instruction_manifest_sha256": instruction_digest,
                    "run_directory": str(run_dir),
                    "working_directory": str(supervisor_env.repo_root),
                    "command_sha256": command_sha256(command),
                    "expected_artifact_path": None,
                    "timeout_seconds": None,
                }
            ),
            encoding="utf-8",
        )
        prepare_run(
            manifest_path=source_manifest,
            run_dir=run_dir,
            runs_root=supervisor_env.runs_root,
            command=command,
        )
        start_run(run_dir=run_dir, pid=attempt, runs_root=supervisor_env.runs_root)
        complete_run(
            run_dir=run_dir,
            exit_code=0,
            signal_number=None,
            peak_rss_bytes=None,
            runs_root=supervisor_env.runs_root,
        )

    records = supervisor._matching_verification_runs(
        iteration=state.iteration,
        state_reference_sha256=state_reference_sha256,
        instruction_manifest_sha256=instruction_manifest_sha256,
    )

    assert [record.manifest.attempt for record in records] == [2]


def test_running_detached_run_with_systemd_oom_result_terminalizes_as_resource_exhausted(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )
    run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"
    command = ("verify", "--opaque")
    source_manifest = supervisor_env.state_path.parent / "detached-manifest-oom.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": 4,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verification",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": instruction_manifest_sha256,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=source_manifest,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )
    start_run(
        run_dir=run_dir,
        pid=123,
        systemd_unit="openclaw-long-task-test.service",
        runs_root=supervisor_env.runs_root,
    )

    def fake_systemctl_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del args, check, capture_output, text, timeout
        return subprocess.CompletedProcess(
            args=["systemctl"],
            returncode=0,
            stdout=(
                "Result=oom-kill\nExecMainStatus=9\nMemoryPeak=2048\n"  # pragma: allowlist secret
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_systemctl_run)

    records = supervisor._matching_verification_runs(
        iteration=state.iteration,
        state_reference_sha256=state_reference_sha256,
        instruction_manifest_sha256=instruction_manifest_sha256,
    )

    assert records[0].status.state is RunState.FAILED
    assert records[0].status.failure_classification is RunFailureClassification.RESOURCE_EXHAUSTED
    assert records[0].status.signal_number == 9
    assert records[0].status.resource_usage.peak_rss_bytes == 2048


def test_running_detached_run_with_collected_systemd_unit_terminalizes_as_process_error(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collected transient unit must not leave verification permanently running."""
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )
    run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"
    command = ("verify", "--opaque")
    source_manifest = supervisor_env.state_path.parent / "detached-manifest-collected.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": 4,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verify-collected",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": instruction_manifest_sha256,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=source_manifest,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )
    start_run(
        run_dir=run_dir,
        pid=123,
        systemd_unit="collected-openclaw-long-task.service",
        runs_root=supervisor_env.runs_root,
    )

    def fake_systemctl_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del args, check, capture_output, text, timeout
        return subprocess.CompletedProcess(
            args=["systemctl"],
            returncode=1,
            stdout="",
            stderr="Unit collected-openclaw-long-task.service not found.\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_systemctl_run)

    records = supervisor._matching_verification_runs(
        iteration=state.iteration,
        state_reference_sha256=state_reference_sha256,
        instruction_manifest_sha256=instruction_manifest_sha256,
    )

    assert records[0].status.state is RunState.FAILED
    assert records[0].status.failure_classification is RunFailureClassification.PROCESS_ERROR
    assert records[0].status.exit_code == 1
    assert records[0].status.signal_number is None


def test_running_detached_run_with_collected_systemd_unit_preserves_live_process(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing unit must not close a worker whose recorded PID is still live."""
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    state = supervisor._load_state()
    state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=supervisor_env.state_path,
    ).sha256()
    instruction_manifest_sha256 = _current_instruction_manifest_sha256(
        state,
        supervisor_env.state_path,
    )
    run_dir = supervisor_env.runs_root / "iteration-4" / "verification" / "attempt-1"
    command = ("verify", "--opaque")
    source_manifest = supervisor_env.state_path.parent / "detached-manifest-live.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iteration": 4,
                "phase": "verification",
                "attempt": 1,
                "task_label": "verify-live",
                "state_reference_sha256": state_reference_sha256,
                "instruction_manifest_sha256": instruction_manifest_sha256,
                "run_directory": str(run_dir),
                "working_directory": str(supervisor_env.repo_root),
                "command_sha256": command_sha256(command),
                "expected_artifact_path": None,
                "timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=source_manifest,
        run_dir=run_dir,
        runs_root=supervisor_env.runs_root,
        command=command,
    )
    start_run(
        run_dir=run_dir,
        pid=123,
        systemd_unit="collected-openclaw-long-task.service",
        runs_root=supervisor_env.runs_root,
    )
    process_dir = supervisor_env.proc_root / "123"
    process_dir.mkdir()
    (process_dir / "stat").write_text("123 (worker) S 1 2 3\n", encoding="utf-8")

    def fake_systemctl_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del args, check, capture_output, text, timeout
        return subprocess.CompletedProcess(
            args=["systemctl"],
            returncode=1,
            stdout="",
            stderr="Unit collected-openclaw-long-task.service not found.\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_systemctl_run)

    records = supervisor._matching_verification_runs(
        iteration=state.iteration,
        state_reference_sha256=state_reference_sha256,
        instruction_manifest_sha256=instruction_manifest_sha256,
    )

    assert records[0].status.state is RunState.RUNNING
    assert records[0].status.failure_classification is None


def _implementation_result(workspace_path: Path) -> ImplementationResultArtifact:
    return ImplementationResultArtifact(
        summary="implementation complete",
        workspace_path=str(workspace_path),
        commit_sha="deadbeef",
        module_path="src/quantipy/alpha/example.py",
        notebook_path="notebooks/example.ipynb",
        tests_added_or_updated=(),
        commands_run=(),
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


def test_supervisor_wakes_the_dedicated_owner_session_by_direct_rpc(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)

    result = supervisor.run_once()

    method, payload = fake.rpc_calls[-1]
    assert result.outcome is SupervisorOutcome.NUDGED
    assert method == "agent"
    assert payload["sessionKey"] == AUTORESEARCH_OWNER_SESSION_KEY
    assert payload["message"]
    idempotency_key = payload["idempotencyKey"]
    assert isinstance(idempotency_key, str)
    assert idempotency_key.startswith("autoresearch-")


def test_successful_recovery_renudges_after_idle_window_and_updates_timestamp(
    supervisor_env: SupervisorEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare_stale_state(supervisor_env)
    clock = [supervisor_env.now]
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake, now=lambda: clock[0])
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    first = supervisor.run_once()
    first_checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    first_record = first_checkpoint.recovery_records[first.recovery_key or ""]
    assert first_record.last_nudge_at == supervisor_env.now
    assert first_record.attempt_count == 1
    assert first_record.renudge_count == 0

    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS - 1.0
    before_idle_window = supervisor.run_once()
    before_checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    before_record = before_checkpoint.recovery_records[first.recovery_key or ""]

    clock[0] += 1.0
    after_idle_window = supervisor.run_once()
    after_checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    after_record = after_checkpoint.recovery_records[first.recovery_key or ""]

    assert first.outcome is SupervisorOutcome.NUDGED
    assert before_idle_window.outcome is SupervisorOutcome.NO_ACTION
    assert after_idle_window.outcome is SupervisorOutcome.RENUDGED
    assert before_record.last_nudge_at == supervisor_env.now
    assert after_record.last_nudge_at == clock[0]
    assert after_record.attempt_count == first_record.attempt_count
    assert after_record.renudge_count == 1
    assert after_checkpoint.last_cycle_outcome == SupervisorOutcome.RENUDGED.value
    agent_calls = [params for method, params in fake.rpc_calls if method == "agent"]
    assert len(agent_calls) == 2
    cycle_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "supervisor.cycle"
    ]
    assert len(cycle_events) == 3
    renudge_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "supervisor.renudged"
    ]
    assert len(renudge_events) == 1
    assert renudge_events[0]["idle_seconds"] == DEFAULT_RENUDGE_IDLE_SECONDS


def test_decayed_renudge_waits_for_active_tasks_and_running_owner(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    clock = [supervisor_env.now]
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake, now=lambda: clock[0])

    first = supervisor.run_once()
    assert first.outcome is SupervisorOutcome.NUDGED
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS
    active_task = {
        "taskId": "owner-turn-active",
        "id": "owner-turn-active",
        "status": "running",
        "runtime": "subagent",
        "taskKind": "codex-native",
        "runId": "codex-thread:owner-turn-active",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "updatedAt": int(clock[0] * 1000) - 1_000,
    }
    fake.tasks = [active_task]
    blocked_by_task = supervisor.run_once()

    fake.tasks = []
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "lastInteractionAt": int(clock[0] * 1000) - 1_000,
                    "startedAt": int(clock[0] * 1000) - 2_000,
                }
            }
        ),
        encoding="utf-8",
    )
    blocked_by_owner = supervisor.run_once()

    assert blocked_by_task.reason == "active_expected_stage_task"
    assert blocked_by_owner.reason == "active_owner_session"
    assert [method for method, _ in fake.rpc_calls if method == "agent"] == ["agent"]


def test_renudge_alert_limit_stops_decay_and_manual_reset_reenables_it(
    supervisor_env: SupervisorEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare_stale_state(supervisor_env)
    clock = [supervisor_env.now]
    fake = FakeOpenClaw()
    supervisor = _supervisor(
        supervisor_env,
        fake,
        renudge_alert_limit=2,
        now=lambda: clock[0],
    )
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    first = supervisor.run_once()
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS
    second = supervisor.run_once()
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS
    third = supervisor.run_once()
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS
    after_alert = supervisor.run_once()
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS
    after_alert_again = supervisor.run_once()

    checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    record = checkpoint.recovery_records[first.recovery_key or ""]
    limit_events = [
        json.loads(log_record.message)
        for log_record in caplog.records
        if json.loads(log_record.message).get("event") == "supervisor.renudge_limit_reached"
    ]

    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.RENUDGED
    assert third.outcome is SupervisorOutcome.RENUDGED
    assert after_alert.outcome is SupervisorOutcome.ALERT
    assert after_alert.reason.startswith("renudge_alert_limit_reached:")
    assert after_alert_again.outcome is SupervisorOutcome.NO_ACTION
    assert after_alert_again.reason == "alert_already_emitted"
    assert record.status is RecoveryStatus.EXHAUSTED
    assert record.alerted is True
    assert record.attempt_count == 1
    assert record.renudge_count == 2
    assert len([method for method, _ in fake.rpc_calls if method == "agent"]) == 3
    assert len(limit_events) == 1
    assert limit_events[0]["outcome"] == SupervisorOutcome.ALERT.value

    reset_recovery_checkpoint_for_manual_wake(
        supervisor_env.checkpoint_path,
        iteration=4,
        phase=Phase.VERIFICATION.value,
    )
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS
    reset_result = supervisor.run_once()

    assert reset_result.outcome is SupervisorOutcome.NUDGED
    reset_checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    reset_record = reset_checkpoint.recovery_records[reset_result.recovery_key or ""]
    assert reset_record.attempt_count == 1
    assert reset_record.renudge_count == 0


def test_exception_cycle_is_persisted_and_logged_before_reraise(
    supervisor_env: SupervisorEnv,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())

    def fail_reconciliation(*, shutdown_requested: ShutdownRequested) -> None:
        del shutdown_requested
        raise SupervisorError("synthetic cycle failure")

    monkeypatch.setattr(supervisor, "_reconciled_running_tasks", fail_reconciliation)
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    with pytest.raises(SupervisorError, match="synthetic cycle failure"):
        supervisor.run_once()

    checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    cycle_events = [
        json.loads(log_record.message)
        for log_record in caplog.records
        if json.loads(log_record.message).get("event") == "supervisor.cycle"
    ]
    assert checkpoint.last_cycle_outcome == SupervisorOutcome.ERROR.value
    assert checkpoint.last_cycle_detail == "SupervisorError: synthetic cycle failure"
    assert len(cycle_events) == 1
    assert cycle_events[0]["outcome"] == SupervisorOutcome.ERROR.value
    assert cycle_events[0]["detail"] == "SupervisorError: synthetic cycle failure"


def test_cycle_save_oserror_after_delivered_nudge_does_not_fail_run_once(
    supervisor_env: SupervisorEnv,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    original_replace = os.replace
    replace_calls = 0

    def fail_cycle_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 3:
            raise OSError("synthetic checkpoint disk failure")
        original_replace(source, target)

    monkeypatch.setattr("gateway.autoresearch_checkpoint.os.replace", fail_cycle_replace)
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert replace_calls == 3
    assert any(
        json.loads(log_record.message).get("event") == "supervisor.cycle_persist_failed"
        for log_record in caplog.records
    )
    assert any(
        json.loads(log_record.message).get("event") == "supervisor.cycle"
        for log_record in caplog.records
    )


def test_supervisor_cycle_is_logged_and_persisted(
    supervisor_env: SupervisorEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare_stale_state(supervisor_env)
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    result = _supervisor(supervisor_env, FakeOpenClaw()).run_once()

    cycle_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "supervisor.cycle"
    ]
    checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    assert cycle_events == [
        {
            "detail": result.reason,
            "event": "supervisor.cycle",
            "iteration": 4,
            "outcome": result.outcome.value,
            "phase": Phase.VERIFICATION.value,
        }
    ]
    assert checkpoint.last_cycle_outcome == result.outcome.value
    assert checkpoint.last_cycle_detail == result.reason
    assert checkpoint.last_cycle_at == supervisor_env.now


def test_supervisor_honors_non_default_renudge_and_stage_stale_thresholds(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    clock = [supervisor_env.now]
    fake = FakeOpenClaw()
    supervisor = _supervisor(
        supervisor_env,
        fake,
        expected_stage_task_stale_seconds=5.0,
        renudge_idle_seconds=30.0,
        grace_period_seconds=1.0,
        now=lambda: clock[0],
    )

    first = supervisor.run_once()
    clock[0] += 29.0
    before_renudge = supervisor.run_once()
    clock[0] += 1.0
    after_renudge = supervisor.run_once()

    assert first.outcome is SupervisorOutcome.NUDGED
    assert before_renudge.outcome is SupervisorOutcome.NO_ACTION
    assert after_renudge.outcome is SupervisorOutcome.RENUDGED

    stale_task = {
        "taskId": "review-stale-configured",
        "id": "review-stale-configured",
        "status": "running",
        "runtime": "subagent",
        "agentId": "reviewer",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:reviewer:configured-stale",
        "updatedAt": int(clock[0] * 1000) - 6_000,
    }
    fake.tasks = [stale_task]
    stale_result = supervisor.run_once()
    assert stale_result.reason == "stale_expected_stage_task"


def test_supervisor_finalizes_required_memory_and_immediately_wakes_owner(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=9,
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-9",
            decision=FinalDecision.KEEP,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.42,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Passes review and improves baseline.",
            log_summary="KEEP iteration-9.",
            continue_loop=True,
            memory_write_required=True,
        ),
        platform_readiness=supervisor_env.readiness_identity,
    )
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    fake = FakeOpenClaw()

    def fake_finalize(*args: object, **kwargs: object) -> AutoresearchState:
        del args, kwargs
        finalized = replace(
            state,
            memory_written=True,
            memory_verification_receipt=MemoryVerificationReceipt(
                experiment_id="iteration-9",
                kg_path=str(supervisor_env.state_path.parent / "knowledge_graph.sqlite3"),
                predicates=("decision",),
                verified_rows_digest="a" * 64,
            ),
        )
        supervisor_env.state_path.write_text(json.dumps(finalized.to_dict()), encoding="utf-8")
        return finalized

    monkeypatch.setattr("gateway.autoresearch_supervisor.can_write_memory", lambda _: True)
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.load_platform_readiness",
        lambda _: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.validate_state_readiness",
        lambda *_: supervisor_env.readiness_identity,
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.AutoresearchValidationContext.from_readiness",
        lambda _: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.finalize_repeat_memory_state_file",
        fake_finalize,
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.FINALIZED
    assert result.reason == "memory_finalized_owner_wake_sent"
    assert result.sent_wake is True
    method, payload = fake.rpc_calls[-1]
    assert method == "agent"
    assert payload["sessionKey"] == AUTORESEARCH_OWNER_SESSION_KEY
    message = payload["message"]
    assert isinstance(message, str)
    assert (
        "Required final MemPalace persistence was completed by the autoresearch supervisor"
        in message
    )
    finalized_state = AutoresearchState.from_dict(
        json.loads(supervisor_env.state_path.read_text(encoding="utf-8"))
    )
    idempotency_key = payload["idempotencyKey"]
    assert isinstance(idempotency_key, str)
    assert idempotency_key == make_idempotency_key(
        purpose="memory-finalized",
        material=memory_wake_acknowledgement_key(finalized_state),
    )


def test_supervisor_retries_unacknowledged_final_memory_wake_for_terminal_iteration(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=11,
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-11",
            decision=FinalDecision.KEEP,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.42,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Passes review and improves baseline.",
            log_summary="KEEP iteration-11.",
            continue_loop=False,
            memory_write_required=True,
        ),
        memory_written=True,
        memory_verification_receipt=MemoryVerificationReceipt(
            experiment_id="iteration-11",
            kg_path=str(supervisor_env.state_path.parent / "knowledge_graph.sqlite3"),
            predicates=("decision",),
            verified_rows_digest="a" * 64,
        ),
        platform_readiness=supervisor_env.readiness_identity,
    )
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    fake = FakeOpenClaw()
    fake.agent_payload = {"status": "rejected"}
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.load_platform_readiness", lambda _: object()
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.validate_state_readiness",
        lambda *_: supervisor_env.readiness_identity,
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.AutoresearchValidationContext.from_readiness",
        lambda _: object(),
    )

    # Act
    failed = _supervisor(supervisor_env, fake).run_once()
    fake.agent_payload = {
        "status": "accepted",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "runId": "retry-run-11",
    }
    retried = _supervisor(supervisor_env, fake).run_once()

    # Assert
    persisted = AutoresearchState.from_dict(
        json.loads(supervisor_env.state_path.read_text(encoding="utf-8"))
    )
    assert failed.reason == "memory_finalized_owner_wake_retryable"
    assert retried.reason == "memory_finalized_owner_wake_sent"
    assert "memory_owner_wake_sent" not in persisted.to_dict()
    checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    acknowledgement = checkpoint.memory_wake_acknowledgements[
        memory_wake_acknowledgement_key(persisted)
    ]
    assert acknowledgement.status == "accepted"
    assert acknowledgement.run_id == "retry-run-11"


def test_memory_wake_acknowledgement_preserves_a_racing_successor_state(
    supervisor_env: SupervisorEnv,
) -> None:
    # Arrange
    completed = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=12,
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-12",
            decision=FinalDecision.KEEP,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.42,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Passes review and improves baseline.",
            log_summary="KEEP iteration-12.",
            continue_loop=True,
            memory_write_required=True,
        ),
        memory_written=True,
        memory_verification_receipt=MemoryVerificationReceipt(
            experiment_id="iteration-12",
            kg_path=str(supervisor_env.state_path.parent / "knowledge_graph.sqlite3"),
            predicates=("decision",),
            verified_rows_digest="b" * 64,
        ),
    )
    successor = AutoresearchState(phase=Phase.SETUP_CONTEXT, iteration=13)
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(successor.to_dict()), encoding="utf-8")

    # Act
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())
    supervisor._acknowledge_memory_owner_wake(
        completed,
        WakeDeliveryProof(status="in_flight", run_id="run-12"),
    )

    # Assert
    assert (
        AutoresearchState.from_dict(
            json.loads(supervisor_env.state_path.read_text(encoding="utf-8"))
        )
        == successor
    )
    checkpoint = SupervisorCheckpoint.load(supervisor_env.checkpoint_path)
    acknowledgement = checkpoint.memory_wake_acknowledgements[
        memory_wake_acknowledgement_key(completed)
    ]
    assert acknowledgement.status == "in_flight"
    assert acknowledgement.run_id == "run-12"


def test_supervisor_persists_repeat_successor_before_wake(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=15,
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-15",
            decision=FinalDecision.NO_CONSENSUS,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.0,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="No consensus; continue with a fresh proposal.",
            log_summary="NO_CONSENSUS iteration-15.",
            continue_loop=True,
            memory_write_required=False,
        ),
        platform_readiness=supervisor_env.readiness_identity,
    )
    successor = replace(state, phase=Phase.SETUP_CONTEXT, iteration=16, final_decision=None)
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    fake = FakeOpenClaw()
    persisted: dict[str, object] = {}

    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.AutoresearchValidationContext.from_readiness",
        lambda _: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.build_receipt_catalog",
        lambda *_: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch.manifest_runtime.build_receipt_catalog",
        lambda *_: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.expected_instruction_manifest_sha256",
        lambda *_, **__: "d" * 64,
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.start_next_iteration",
        lambda *_args, **_kwargs: successor,
    )

    def fake_persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted["called"] = True
        supervisor_env.state_path.write_text(json.dumps(successor.to_dict()), encoding="utf-8")

    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.persist_next_iteration_state",
        fake_persist,
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert result.reason == "repeat_successor_started"
    assert result.sent_wake is True
    assert persisted == {"called": True}
    assert (
        AutoresearchState.from_dict(
            json.loads(supervisor_env.state_path.read_text(encoding="utf-8"))
        )
        == successor
    )
    method, payload = fake.rpc_calls[-1]
    assert method == "agent"
    assert payload["idempotencyKey"] == make_idempotency_key(
        purpose="repeat-successor",
        material=build_authoritative_state_reference(
            successor,
            state_path=supervisor_env.state_path,
        ).sha256(),
    )


def test_supervisor_memory_finalization_failure_does_not_wake_owner(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=10,
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-10",
            decision=FinalDecision.KEEP,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.42,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Passes review and improves baseline.",
            log_summary="KEEP iteration-10.",
            continue_loop=True,
            memory_write_required=True,
        ),
        platform_readiness=supervisor_env.readiness_identity,
    )
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    fake = FakeOpenClaw()

    def fail_finalize(*args: object, **kwargs: object) -> AutoresearchState:
        del args, kwargs
        raise AutoresearchValidationError("strict finalizer failure")

    monkeypatch.setattr("gateway.autoresearch_supervisor.can_write_memory", lambda _: True)
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.load_platform_readiness",
        lambda _: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.validate_state_readiness",
        lambda *_: supervisor_env.readiness_identity,
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.AutoresearchValidationContext.from_readiness",
        lambda _: object(),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.finalize_repeat_memory_state_file",
        fail_finalize,
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason == "memory_finalization_failed: strict finalizer failure"
    assert not any(method == "agent" for method, _ in fake.rpc_calls)


def test_supervisor_rejects_an_unpinned_state_before_any_openclaw_rpc(
    supervisor_env: SupervisorEnv,
) -> None:
    _write_state(supervisor_env.state_path)
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert "platform_readiness_blocked" in result.reason
    assert "no pinned platform readiness receipt" in result.reason
    assert fake.rpc_calls == []


def test_supervisor_classifies_missing_verification_artifact_and_wakes_owner(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = supervisor_env.repo_root.parent / "quantipy-worktree"
    workspace.mkdir()
    _write_state(
        supervisor_env.state_path,
        iteration=30,
        implementation_result=_implementation_result(workspace),
        platform_readiness=supervisor_env.readiness_identity,
    )
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    fake = FakeOpenClaw()
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.seal_canonical_verification_dispatch_state_file",
        lambda *_, **__: AutoresearchState.from_dict(
            json.loads(supervisor_env.state_path.read_text(encoding="utf-8"))
        ),
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.require_canonical_verification_dispatch_attestation",
        lambda *_, **__: None,
    )
    monkeypatch.setattr(
        "gateway.autoresearch_supervisor.provision_quantipy_experiment_runs_root",
        lambda: None,
    )

    result = _supervisor(supervisor_env, fake).run_once()

    method, payload = fake.rpc_calls[-1]
    assert result.outcome is SupervisorOutcome.NUDGED
    assert method == "agent"
    assert result.reason == "missing_verification_artifact"
    assert result.recovery_key is not None
    assert result.recovery_key.startswith("missing_verification_artifact:30:verification:")
    message = payload["message"]
    assert isinstance(message, str)
    assert "implementation_result but no verification_history" in message
    assert "Do not fabricate verification_result metrics" in message
    assert "strict production envelope" in message
    assert "Never pass a raw unwrapped verification_result" in message


def test_supervisor_preserves_operator_precondition_implementation_state(
    supervisor_env: SupervisorEnv,
) -> None:
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(_operator_precondition_state_json(), encoding="utf-8")

    state = _supervisor(supervisor_env, FakeOpenClaw())._load_state()

    assert state.phase is Phase.IMPLEMENTATION


def test_supervisor_does_not_wake_a_suspended_state(supervisor_env: SupervisorEnv) -> None:
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=36,
        mode=ResearchMode.DATA_INFRA_G0,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-36",
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
        suspended=True,
        suspension_reason="Operator must publish evidence.",
    )
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.NO_ACTION
    assert result.reason == "platform_readiness_suspended"
    assert not any(method == "agent" for method, _ in fake.rpc_calls)


def test_recovery_retries_use_distinct_idempotency_keys(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    clock = [supervisor_env.now]
    supervisor = AutoresearchSupervisor(
        SupervisorConfig(
            state_path=supervisor_env.state_path,
            readiness_manifest_path=supervisor_env.readiness_manifest_path,
            checkpoint_path=supervisor_env.checkpoint_path,
            autoresearch_dir=supervisor_env.state_path.parent,
            owner_sessions_path=supervisor_env.sessions_path,
            target_repo=supervisor_env.repo_root,
            proc_root=supervisor_env.proc_root,
            launch_requests_path=supervisor_env.launch_requests_path,
            stage_inbox_path=supervisor_env.stage_inbox_path,
        ),
        now=lambda: clock[0],
        sleep=lambda _: None,
        task_gateway=fake,
    )

    first = supervisor.run_once()
    clock[0] += DEFAULT_RENUDGE_IDLE_SECONDS + 1.0
    second = supervisor.run_once()

    agent_calls = [params for method, params in fake.rpc_calls if method == "agent"]
    idempotency_keys = [call["idempotencyKey"] for call in agent_calls]
    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.RENUDGED
    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] != idempotency_keys[1]


@pytest.mark.parametrize(
    "response",
    [
        {"status": "rejected", "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY, "runId": "run"},
        {"status": "accepted", "runId": "run"},
        {"status": "in_flight", "runId": "run"},
        {"status": "accepted", "sessionKey": "agent:other:session", "runId": "run"},
        {"status": "accepted", "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY, "runId": ""},
        {"status": "completed", "runId": "run"},
        {
            "runId": "cached-run",
            "status": "timeout",
            "summary": "completed",
            "result": {"payloads": [{"text": "owner wake completed"}]},
        },
        {
            "runId": "cached-run",
            "status": "error",
            "summary": "completed",
            "result": {"payloads": [{"text": "owner wake completed"}]},
        },
        {
            "runId": "cached-run",
            "status": "failed",
            "summary": "completed",
            "result": {"payloads": [{"text": "owner wake completed"}]},
        },
        {
            "runId": "cached-run",
            "status": "cancelled",
            "summary": "completed",
            "result": {"payloads": [{"text": "owner wake completed"}]},
        },
        {
            "runId": "cached-run",
            "status": "ok",
            "summary": "still running",
            "result": {"payloads": [{"text": "owner wake completed"}]},
        },
        {"runId": "cached-run", "status": "ok", "summary": "completed"},
        {
            "runId": "cached-run",
            "status": "ok",
            "summary": "completed",
            "result": None,
        },
        {
            "runId": "cached-run",
            "status": "ok",
            "summary": "completed",
            "result": {},
        },
        {
            "runId": "cached-run",
            "status": "ok",
            "summary": "completed",
            "result": {"payloads": [{"text": "owner wake completed"}]},
            "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        },
    ],
)
def test_supervisor_fails_closed_for_an_invalid_wake_response(response: dict[str, object]) -> None:
    fake = FakeOpenClaw()
    fake.agent_payload = response

    with pytest.raises(SupervisorError, match="OpenClaw wake response"):
        OpenClawRPC(fake).wake(message="continue", idempotency_key="idem")


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_run_id", "cached_terminal"),
    [
        (
            {
                "status": "accepted",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "runId": "run-accepted",
            },
            "accepted",
            "run-accepted",
            False,
        ),
        (
            {
                "status": "in_flight",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "runId": "run-in-flight",
            },
            "in_flight",
            "run-in-flight",
            False,
        ),
        (
            {
                "runId": "cached-run-1",
                "status": "ok",
                "summary": "completed",
                "result": {
                    "payloads": [{"text": "owner wake completed"}],
                    "meta": {"durationMs": 42},
                },
            },
            "ok",
            "cached-run-1",
            True,
        ),
    ],
)
def test_openclaw_wake_accepts_idempotent_delivery_proofs(
    response: dict[str, object],
    expected_status: str,
    expected_run_id: str | None,
    cached_terminal: bool,
) -> None:
    fake = FakeOpenClaw()
    fake.agent_payload = response

    proof = OpenClawRPC(fake).wake(message="continue", idempotency_key="idem")

    assert proof.status == expected_status
    assert proof.run_id == expected_run_id
    assert proof.cached_terminal is cached_terminal


def test_supervisor_corrupt_checkpoint_fails_closed_without_wake(
    supervisor_env: SupervisorEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = AutoresearchState(
        phase=Phase.REPEAT,
        iteration=14,
        mode=ResearchMode.ALPHA_RESEARCH,
        final_decision=FinalDecisionArtifact(
            experiment_id="iteration-14",
            decision=FinalDecision.KEEP,
            recommended_metric_name="OOS Sharpe net",
            recommended_metric_value=0.42,
            reviewer_verdict=FinalReviewerVerdict.PASS,
            rationale="Passes review and improves baseline.",
            log_summary="KEEP iteration-14.",
            continue_loop=False,
            memory_write_required=True,
        ),
        memory_written=True,
        memory_verification_receipt=MemoryVerificationReceipt(
            experiment_id="iteration-14",
            kg_path=str(supervisor_env.state_path.parent / "knowledge_graph.sqlite3"),
            predicates=("decision",),
            verified_rows_digest="c" * 64,
        ),
        platform_readiness=supervisor_env.readiness_identity,
    )
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    supervisor_env.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.checkpoint_path.write_text("{", encoding="utf-8")
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)

    with caplog.at_level(logging.ERROR):
        result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason.startswith("checkpoint_corrupt:")
    assert not fake.rpc_calls
    assert "supervisor.checkpoint_corrupt" in caplog.text


def test_supervisor_run_forever_stays_alive_after_closed_checkpoint_alert(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor_env.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.checkpoint_path.write_text("{", encoding="utf-8")
    fake = FakeOpenClaw()
    harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", harness.install)

    def stop_after_first_sleep(_seconds: float) -> None:
        harness.trigger(signal.SIGTERM)

    code = AutoresearchSupervisor(
        SupervisorConfig(
            state_path=supervisor_env.state_path,
            readiness_manifest_path=supervisor_env.readiness_manifest_path,
            checkpoint_path=supervisor_env.checkpoint_path,
            autoresearch_dir=supervisor_env.state_path.parent,
            owner_sessions_path=supervisor_env.sessions_path,
            target_repo=supervisor_env.repo_root,
            proc_root=supervisor_env.proc_root,
            launch_requests_path=supervisor_env.launch_requests_path,
            stage_inbox_path=supervisor_env.stage_inbox_path,
            poll_interval_seconds=60,
        ),
        now=lambda: supervisor_env.now,
        sleep=stop_after_first_sleep,
        task_gateway=fake,
    ).run_forever()

    assert code == 0
    assert not any(method == "agent" for method, _ in fake.rpc_calls)


@pytest.mark.parametrize("phase", [Phase.VERIFICATION, Phase.DECISION_LOG])
def test_fresh_owner_lifecycle_short_circuits_owner_stages_before_task_listing(
    supervisor_env: SupervisorEnv, phase: Phase
) -> None:
    _prepare_stale_state(supervisor_env, phase=phase)
    now = supervisor_env.now
    sessions_path = supervisor_env.sessions_path
    sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(now * 1000) - 1_000,
                    "lastInteractionAt": int(now * 1000) - 1_000,
                    "startedAt": int(now * 1000) - 2_000,
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_owner_session"
    assert fake.task_list_calls == 0
    assert fake.rpc_calls == []


@pytest.mark.parametrize("phase", [Phase.VERIFICATION, Phase.DECISION_LOG])
def test_statusless_owner_lifecycle_record_is_not_treated_as_invalid(
    supervisor_env: SupervisorEnv, phase: Phase
) -> None:
    # An aborted turn leaves the gateway session record without a top-level
    # status key; supervision must proceed instead of alert-looping.
    _prepare_stale_state(supervisor_env, phase=phase)
    now = supervisor_env.now
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "sessionId": "ff133dcc-0000-0000-0000-000000000000",
                    "updatedAt": int(now * 1000) - 1_000,
                    "lastInteractionAt": int(now * 1000) - 1_000,
                    "sessionStartedAt": int(now * 1000) - 2_000,
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason != "invalid_owner_session_lifecycle"


def test_fresh_owner_lifecycle_does_not_short_circuit_setup_context(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.SETUP_CONTEXT)
    now = supervisor_env.now
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(now * 1000) - 1_000,
                    "lastInteractionAt": int(now * 1000) - 1_000,
                    "sessionStartedAt": int(now * 1000) - 2_000,
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_owner_session"
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list"]


def test_fresh_updated_at_does_not_hide_stale_owner_interaction(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    now = supervisor_env.now
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(now * 1000) - 1_000,
                    "lastInteractionAt": int(now * 1000) - 301_000,
                    "startedAt": int(now * 1000) - 1_000,
                    "sessionStartedAt": int(now * 1000) - 1_000,
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "stale_running_owner_session")
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list"]


@pytest.mark.parametrize("contents", ["{", '{"owner":'])
def test_corrupt_owner_session_store_reconciles_before_failing_closed(
    supervisor_env: SupervisorEnv, contents: str
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor_env.sessions_path.write_text(contents, encoding="utf-8")
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "owner_session_store_unavailable")
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list"]


def test_unreadable_owner_session_store_reconciles_before_failing_closed(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor_env.sessions_path.unlink()
    supervisor_env.sessions_path.mkdir()
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "owner_session_store_unavailable")
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list"]


def test_run_forever_does_not_terminate_for_a_corrupt_owner_session_store(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor_env.sessions_path.write_text("{", encoding="utf-8")
    fake = FakeOpenClaw()
    supervisor = _supervisor(supervisor_env, fake)

    def request_stop(_seconds: float) -> None:
        signal_harness.trigger(signal.SIGTERM)

    monkeypatch.setattr(supervisor, "_sleep", request_stop)

    assert supervisor.run_forever() == 0
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list"]


def test_stale_running_owner_lifecycle_still_reconciles_and_fails_closed(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    now = supervisor_env.now
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(now * 1000) - 301_000,
                    "lastInteractionAt": int(now * 1000) - 301_000,
                    "startedAt": int(now * 1000) - 302_000,
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "stale_running_owner_session")
    assert fake.task_list_calls == 1
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list"]


def test_non_running_owner_lifecycle_still_reconciles_and_wakes(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor_env.sessions_path.write_text(
        json.dumps({AUTORESEARCH_OWNER_SESSION_KEY: {"status": "idle"}}),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED
    assert fake.task_list_calls == 2
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list", "tasks.list", "agent"]


def test_fresh_owner_lifecycle_still_reconciles_child_agent_stages(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.IMPLEMENTATION)
    now = supervisor_env.now
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(now * 1000) - 1_000,
                    "lastInteractionAt": int(now * 1000) - 1_000,
                    "startedAt": int(now * 1000) - 2_000,
                }
            }
        ),
        encoding="utf-8",
    )
    task: dict[str, object] = {
        "taskId": "implementer-terminal",
        "agentId": "implementer",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:implementer:child",
    }
    fake = FakeOpenClaw(
        tasks=[task],
        shown_tasks={"implementer-terminal": {**task, "status": "failed"}},
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_owner_session"
    assert fake.task_list_calls == 1
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list", "tasks.get"]
    assert not any(method == "agent" for method, _ in fake.rpc_calls)


def test_error_detection_reads_only_the_dedicated_owner_transcript(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    sessions_path = supervisor_env.sessions_path
    owner_transcript = sessions_path.parent / "owner.jsonl"
    owner_transcript.write_text("context overflow", encoding="utf-8")
    other_transcript = sessions_path.parent / "other.jsonl"
    other_transcript.write_text("maximum context length", encoding="utf-8")
    sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "idle",
                    "sessionFile": str(owner_transcript),
                },
                "agent:other:session": {"sessionFile": str(other_transcript)},
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "recovery_message_sent"


def test_missing_verification_reason_is_not_masked_by_owner_session_error(
    supervisor_env: SupervisorEnv,
) -> None:
    workspace = supervisor_env.repo_root.parent / "quantipy-worktree"
    workspace.mkdir()
    _write_state(
        supervisor_env.state_path,
        iteration=30,
        implementation_result=_implementation_result(workspace),
        platform_readiness=supervisor_env.readiness_identity,
    )
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    owner_transcript = supervisor_env.sessions_path.parent / "owner.jsonl"
    owner_transcript.write_text("context overflow", encoding="utf-8")
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "idle",
                    "sessionFile": str(owner_transcript),
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == (
        "controller_lifecycle_failed: implementation_result requires a majority consensus"
    )


def test_stage_task_uses_the_public_task_summary_requester_and_owner_mapping(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    now = supervisor_env.now
    fake = FakeOpenClaw(
        tasks=[
            {
                "id": "review-1",
                "taskId": "review-1",
                "status": "running",
                "runtime": "subagent",
                "agentId": "reviewer",
                # OpenClaw 2026.7.1-2 maps TaskRecord.requesterSessionKey here.
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                # The normal sessions_spawn path sets ownerKey to that same requester.
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "task": "review the Quantipy autoresearch result",
                "updatedAt": int(now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list", "tasks.get"]


def test_codex_native_subagent_task_under_pm_owner_is_active(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    now = supervisor_env.now
    task = {
        "id": "native-review-1",
        "taskId": "native-review-1",
        "status": "running",
        "runtime": "subagent",
        "taskKind": "codex-native",
        "runId": "codex-thread:review-1",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "task": "reviewer via native Codex spawn_agent",
        "updatedAt": int(now * 1000) - 1_000,
    }
    fake = FakeOpenClaw(tasks=[task])

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"
    assert [method for method, _ in fake.rpc_calls] == ["tasks.list", "tasks.get"]


def test_ordinary_stage_task_run_id_without_native_kind_stays_stage_child(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    now = supervisor_env.now
    fake = FakeOpenClaw(
        tasks=[
            {
                "id": "review-openclaw-run",
                "taskId": "review-openclaw-run",
                "status": "running",
                "runtime": "subagent",
                "runId": "openclaw-run-1",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "updatedAt": int(now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"


def test_stale_codex_native_subagent_task_under_pm_owner_alerts(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    stale_ms = int((supervisor_env.now - 1_000) * 1000)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "native-review-stale",
                "status": "running",
                "runtime": "subagent",
                "taskKind": "codex-native",
                "runId": "codex-thread:review-stale",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "updatedAt": stale_ms,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "stale_expected_stage_task")


def test_codex_native_subagent_task_requires_complete_native_markers(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "native-review-ambiguous",
                "status": "running",
                "runtime": "subagent",
                "taskKind": "codex-native",
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "updatedAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_supervisor_retries_a_transient_empty_task_list_failure(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gateway.autoresearch_supervisor.time.sleep", lambda _seconds: None)
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "id": "review-1",
                "taskId": "review-1",
                "status": "running",
                "runtime": "subagent",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "updatedAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ],
        task_list_failures_before_success=1,
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"
    assert fake.task_list_calls == 2


def test_gateway_task_summary_requires_its_public_session_key(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "review-raw-1",
                "status": "running",
                "runtime": "subagent",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "lastEventAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "active_expected_stage_task"


def test_task_with_disagreeing_raw_and_summary_requester_keys_is_ambiguous(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "review-conflict",
                "agentId": "reviewer",
                "requesterSessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "sessionKey": "agent:other:session",
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:reviewer:task-child",
                "lastEventAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_stage_task_with_ambiguous_child_agent_fails_closed(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.REVIEW)
    fake = FakeOpenClaw(
        tasks=[
            {
                "taskId": "review-1",
                "agentId": "reviewer",
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "childSessionKey": "agent:implementer:task-child",
                "updatedAt": int(supervisor_env.now * 1000) - 1_000,
            }
        ]
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_recovery_attempts_remain_bounded_after_repeated_wake_failures(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    fake.agent_payload = {
        "status": "rejected",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "runId": "run",
    }
    supervisor = _supervisor(supervisor_env, fake)

    with pytest.raises(SupervisorError):
        supervisor.run_once()
    with pytest.raises(SupervisorError):
        supervisor.run_once()

    result = supervisor.run_once()

    assert result.reason.startswith("recovery_attempts_exhausted:")


def test_provider_auth_wake_failures_alert_as_control_plane_blockers(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FailingWakeOpenClaw(
        stderr=(
            "CLI transcript compaction failed for openai/gpt-5.6-sol: "
            'No API key found for provider "openai"'
        )
    )
    supervisor = _supervisor(supervisor_env, fake)

    with pytest.raises(SupervisorError, match="No API key found"):
        supervisor.run_once()
    with pytest.raises(SupervisorError, match="No API key found"):
        supervisor.run_once()

    result = supervisor.run_once()

    assert result.outcome is SupervisorOutcome.ALERT
    assert result.reason.startswith("control_plane_provider_blocked:")


def test_repeated_stage_capacity_failures_alert_as_control_plane_blockers(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.DEBATE)
    clock = [supervisor_env.now]
    task: dict[str, object] = {
        "taskId": "debate-1",
        "status": "running",
        "agentId": "debater_data",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:debater_data:task-child",
        "updatedAt": int(supervisor_env.now * 1000) - 1_000,
    }
    fake = FakeOpenClaw(
        tasks=[task],
        shown_tasks={
            "debate-1": {
                **task,
                "status": "failed",
                "error": "Selected model is at capacity",
            }
        },
    )
    supervisor = AutoresearchSupervisor(
        SupervisorConfig(
            state_path=supervisor_env.state_path,
            readiness_manifest_path=supervisor_env.readiness_manifest_path,
            checkpoint_path=supervisor_env.checkpoint_path,
            autoresearch_dir=supervisor_env.state_path.parent,
            owner_sessions_path=supervisor_env.sessions_path,
            target_repo=supervisor_env.repo_root,
            proc_root=supervisor_env.proc_root,
            launch_requests_path=supervisor_env.launch_requests_path,
            stage_inbox_path=supervisor_env.stage_inbox_path,
        ),
        now=lambda: clock[0],
        sleep=lambda _: None,
        task_gateway=fake,
    )

    first = supervisor.run_once()
    clock[0] += 121.0
    second = supervisor.run_once()
    clock[0] += 121.0
    third = supervisor.run_once()

    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.NUDGED
    assert third.outcome is SupervisorOutcome.ALERT
    assert third.reason.startswith("control_plane_provider_blocked:")


def test_active_target_writer_process_suppresses_owner_wake(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    process_dir = supervisor_env.proc_root / "1234"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"uv\x00run\x00pytest\x00")
    (process_dir / "cwd").symlink_to(supervisor_env.repo_root, target_is_directory=True)
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "target_repo_writer_active"


def test_active_writer_in_the_verified_implementation_workspace_suppresses_owner_wake(
    supervisor_env: SupervisorEnv,
) -> None:
    workspace = supervisor_env.repo_root.parent / "quantipy-worktree"
    workspace.mkdir()
    _write_state(
        supervisor_env.state_path,
        implementation_result=_implementation_result(workspace),
        platform_readiness=supervisor_env.readiness_identity,
    )
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    process_dir = supervisor_env.proc_root / "1234"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"uv\x00run\x00pytest\x00")
    (process_dir / "cwd").symlink_to(workspace, target_is_directory=True)
    fake = FakeOpenClaw()

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "target_repo_writer_active"


def test_lost_task_projection_with_active_persisted_writer_suppresses_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    workspace = supervisor_env.repo_root.parent / "quantipy-worktree"
    workspace.mkdir()
    _write_state(
        supervisor_env.state_path,
        implementation_result=_implementation_result(workspace),
        platform_readiness=supervisor_env.readiness_identity,
    )
    _make_stale([supervisor_env.state_path, *supervisor_env.marker_paths], now=supervisor_env.now)
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(supervisor_env.now * 1000) - 600_000,
                    "lastInteractionAt": int(supervisor_env.now * 1000) - 600_000,
                    "startedAt": int(supervisor_env.now * 1000) - 700_000,
                }
            }
        ),
        encoding="utf-8",
    )
    process_dir = supervisor_env.proc_root / "1234"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"uv\x00run\x00pytest\x00")
    (process_dir / "cwd").symlink_to(workspace, target_is_directory=True)
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owner-turn": {**task, "status": "failed"}})

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.reason == "target_repo_writer_active"
    assert fake.rpc_calls == []


def test_lost_task_projection_permits_recovery_after_persisted_writer_exits(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    supervisor_env.sessions_path.write_text(
        json.dumps(
            {
                AUTORESEARCH_OWNER_SESSION_KEY: {
                    "status": "running",
                    "updatedAt": int(supervisor_env.now * 1000) - 600_000,
                    "lastInteractionAt": int(supervisor_env.now * 1000) - 600_000,
                    "startedAt": int(supervisor_env.now * 1000) - 700_000,
                }
            }
        ),
        encoding="utf-8",
    )
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FakeOpenClaw(tasks=[task], shown_tasks={"owner-turn": {**task, "status": "failed"}})

    result = _supervisor(supervisor_env, fake).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED


def test_mismatched_canonical_task_show_fails_closed_with_an_alert(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FakeOpenClaw(
        tasks=[task],
        shown_tasks={"owner-turn": {**task, "taskId": "different", "status": "running"}},
    )

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_task_show_failure_during_reconciliation_returns_a_controlled_alert(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FailingTaskShowOpenClaw(tasks=[task])

    result = _supervisor(supervisor_env, fake).run_once()

    assert result == SupervisorResult(SupervisorOutcome.ALERT, "task_reconciliation_failed")


def test_task_show_failure_preserves_shutdown_interruption(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    stop_requested = False

    def request_shutdown() -> None:
        nonlocal stop_requested
        stop_requested = True

    task: dict[str, object] = {
        "taskId": "owner-turn",
        "agentId": AUTORESEARCH_OWNER_AGENT_ID,
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
    }
    fake = FailingTaskShowOpenClaw(tasks=[task], before_failure=request_shutdown)

    with pytest.raises(ShutdownInterrupted):
        _supervisor(supervisor_env, fake).run_once(shutdown_requested=lambda: stop_requested)


def test_supervisor_rejects_non_object_task_list_entries(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    fake.tasks = [
        {
            "taskId": "owner-turn",
            "agentId": AUTORESEARCH_OWNER_AGENT_ID,
            "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
            "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        },
        "corrupted-task-entry",  # type: ignore[list-item]
    ]

    with pytest.raises(SupervisorError, match="non-object task"):
        _supervisor(supervisor_env, fake).run_once()


def test_supervisor_source_contains_no_g2_dev_surface() -> None:
    source = Path("gateway/autoresearch_supervisor.py").read_text(encoding="utf-8").lower()

    assert "/_dev" not in source
    assert "localhost:5173" not in source
    assert "agent:main:g2" not in source


def test_supervisor_source_does_not_manipulate_python_tracing() -> None:
    source = Path("gateway/autoresearch_supervisor.py").read_text(encoding="utf-8")

    assert "sys.settrace" not in source
    assert "sys.setprofile" not in source


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_treats_a_signal_before_command_failure_detection_as_clean_shutdown(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    signum: int,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(
        supervisor_env,
        FailingTaskListOpenClaw(before_failure=lambda: signal_harness.trigger(signum)),
    )
    caplog.set_level(logging.INFO, logger="gateway.autoresearch_supervisor")

    exit_code = supervisor.run_forever()

    shutdown_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "supervisor.shutdown_interrupted"
    ]
    assert exit_code == 0
    assert shutdown_events == [
        {
            "detail": "poll failed",
            "event": "supervisor.shutdown_interrupted",
        }
    ]


def test_run_forever_recovers_after_a_command_failure_when_shutdown_was_not_requested(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    now = [supervisor_env.now]
    poll_count = 0
    supervisor = AutoresearchSupervisor(
        SupervisorConfig(
            state_path=supervisor_env.state_path,
            readiness_manifest_path=supervisor_env.readiness_manifest_path,
            checkpoint_path=supervisor_env.checkpoint_path,
            autoresearch_dir=supervisor_env.state_path.parent,
            owner_sessions_path=supervisor_env.sessions_path,
            target_repo=supervisor_env.repo_root,
            proc_root=supervisor_env.proc_root,
            launch_requests_path=supervisor_env.launch_requests_path,
            stage_inbox_path=supervisor_env.stage_inbox_path,
            poll_interval_seconds=1.0,
        ),
        now=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        task_gateway=FakeOpenClaw(),
    )

    def fail_then_stop(*, shutdown_requested: Callable[[], bool]) -> None:
        nonlocal poll_count
        del shutdown_requested
        poll_count += 1
        if poll_count == 1:
            raise OpenClawUnavailableError("poll failed")
        signal_harness.trigger(signal.SIGTERM)

    monkeypatch.setattr(supervisor, "run_once", fail_then_stop)
    caplog.set_level(logging.ERROR, logger="gateway.autoresearch_supervisor")

    exit_code = supervisor.run_forever()

    failure_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "supervisor.poll_failed"
    ]
    assert exit_code == 0
    assert poll_count == 2
    assert failure_events == [
        {
            "detail": "poll failed",
            "event": "supervisor.poll_failed",
        }
    ]


def test_run_forever_reraises_a_non_recoverable_supervisor_failure(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())

    def fail_poll(*, shutdown_requested: Callable[[], bool]) -> None:
        del shutdown_requested
        raise SupervisorError("malformed authoritative state")

    monkeypatch.setattr(supervisor, "run_once", fail_poll)

    with pytest.raises(SupervisorError, match="malformed authoritative state"):
        supervisor.run_forever()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_treats_command_failure_followed_by_signal_as_clean_shutdown(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FailingTaskListOpenClaw())
    run_once = supervisor.run_once

    def fail_poll(*, shutdown_requested: Callable[[], bool]) -> None:
        try:
            run_once(shutdown_requested=shutdown_requested)
        finally:
            signal_harness.trigger(signum)

    monkeypatch.setattr(supervisor, "run_once", fail_poll)

    assert supervisor.run_forever() == 0


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_treats_task_list_failure_during_shutdown_as_clean_shutdown(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)
    supervisor = _supervisor(supervisor_env, FailingTaskListOpenClaw())

    def request_shutdown(_seconds: float) -> None:
        signal_harness.trigger(signum)

    monkeypatch.setattr("gateway.autoresearch_supervisor.time.sleep", request_shutdown)

    assert supervisor.run_forever() == 0


def test_run_forever_keeps_shutdown_classification_after_repeated_mixed_signals(
    supervisor_env: SupervisorEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_harness = SignalHarness()
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    _prepare_stale_state(supervisor_env)

    def request_shutdown() -> None:
        signal_harness.trigger(signal.SIGINT)

    supervisor = _supervisor(
        supervisor_env,
        FailingTaskListOpenClaw(before_failure=request_shutdown),
    )
    run_once = supervisor.run_once

    def preserve_classified_shutdown(*, shutdown_requested: Callable[[], bool]) -> None:
        try:
            run_once(shutdown_requested=shutdown_requested)
        except ShutdownInterrupted:
            signal_harness.trigger(signal.SIGTERM)
            signal_harness.trigger(signal.SIGINT)
            raise

    monkeypatch.setattr(supervisor, "run_once", preserve_classified_shutdown)

    exit_code = supervisor.run_forever()

    assert exit_code == 0


def test_main_returns_an_error_when_native_gateway_configuration_is_missing(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr("gateway.autoresearch_supervisor.load_dotenv", lambda _path: False)

    exit_code = main(["--state-path", str(supervisor_env.state_path)])

    assert exit_code == 1


def test_main_once_returns_an_error_when_native_gateway_configuration_is_missing(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr("gateway.autoresearch_supervisor.load_dotenv", lambda _path: False)

    exit_code = main(["--once", "--state-path", str(supervisor_env.state_path)])

    assert exit_code == 1


def test_run_forever_does_not_poll_again_after_shutdown_during_sleep(
    supervisor_env: SupervisorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_harness = SignalHarness()
    poll_count = 0
    monkeypatch.setattr(signal, "signal", signal_harness.install)
    supervisor = _supervisor(supervisor_env, FakeOpenClaw())

    def poll_once(*, shutdown_requested: Callable[[], bool]) -> None:
        nonlocal poll_count
        poll_count += 1

    def request_stop_while_sleeping(_: float) -> None:
        signal_harness.trigger(signal.SIGTERM)

    monkeypatch.setattr(supervisor, "run_once", poll_once)
    monkeypatch.setattr(supervisor, "_sleep", request_stop_while_sleeping)

    exit_code = supervisor.run_forever()

    assert exit_code == 0
    assert poll_count == 1
