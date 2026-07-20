from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import FrameType

import pytest
from gateway.autoresearch_readiness import (
    PLATFORM_READINESS_SCHEMA_VERSION,
    EvidenceId,
    PlatformReadinessManifest,
    ReadinessIdentity,
    canonical_platform_capabilities,
)
from gateway.autoresearch_runner import (
    AutoresearchState,
    ConsensusResultArtifact,
    ConsensusStatus,
    FinalDecision,
    FinalDecisionArtifact,
    FinalReviewerVerdict,
    ImplementationResultArtifact,
    Phase,
    PriceHydrationScopePreflight,
    ResearchMode,
)
from gateway.autoresearch_supervisor import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
    AutoresearchSupervisor,
    NativeGatewayRPC,
    OpenClawRPC,
    OpenClawUnavailableError,
    ShutdownInterrupted,
    ShutdownRequested,
    SupervisorConfig,
    SupervisorError,
    SupervisorOutcome,
    SupervisorResult,
    main,
)
from gateway.openclaw_client import OpenClawError, OpenClawTransportError

SignalHandler = Callable[[int, FrameType | None], None]
SignalDisposition = SignalHandler | signal.Handlers


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
                        "debater-microstructure",
                        "debater-data",
                        "debater-skeptic",
                        "debater-theory",
                        "debater-implementation",
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
    )


def _supervisor(env: SupervisorEnv, fake: FakeOpenClaw) -> AutoresearchSupervisor:
    return AutoresearchSupervisor(
        SupervisorConfig(
            state_path=env.state_path,
            readiness_manifest_path=env.readiness_manifest_path,
            checkpoint_path=env.checkpoint_path,
            autoresearch_dir=env.state_path.parent,
            owner_sessions_path=env.sessions_path,
            target_repo=env.repo_root,
            proc_root=env.proc_root,
        ),
        now=lambda: env.now,
        sleep=lambda _: None,
        task_gateway=fake,
    )


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


def test_stale_iteration_context_residue_does_not_defer_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
    stale_context = supervisor_env.state_path.parent / "iteration-4-context.json"
    stale_context.write_text("{}", encoding="utf-8")
    os.utime(stale_context, (supervisor_env.now, supervisor_env.now))

    result = _supervisor(supervisor_env, FakeOpenClaw()).run_once()

    assert result.outcome is SupervisorOutcome.NUDGED


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


def test_supervisor_normalizes_operator_precondition_implementation_state(
    supervisor_env: SupervisorEnv,
) -> None:
    supervisor_env.state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_env.state_path.write_text(_operator_precondition_state_json(), encoding="utf-8")

    state = _supervisor(supervisor_env, FakeOpenClaw())._load_state()

    assert state.phase is Phase.DECISION_LOG


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
        ),
        now=lambda: clock[0],
        sleep=lambda _: None,
        task_gateway=fake,
    )

    first = supervisor.run_once()
    clock[0] += 121.0
    second = supervisor.run_once()

    agent_calls = [params for method, params in fake.rpc_calls if method == "agent"]
    idempotency_keys = [call["idempotencyKey"] for call in agent_calls]
    assert first.outcome is SupervisorOutcome.NUDGED
    assert second.outcome is SupervisorOutcome.NUDGED
    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] != idempotency_keys[1]


@pytest.mark.parametrize(
    "response",
    [
        {"status": "rejected", "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY, "runId": "run"},
        {"status": "accepted", "sessionKey": "agent:other:session", "runId": "run"},
        {"status": "accepted", "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY, "runId": ""},
    ],
)
def test_supervisor_fails_closed_for_an_invalid_wake_response(
    supervisor_env: SupervisorEnv, response: dict[str, object]
) -> None:
    _prepare_stale_state(supervisor_env)
    fake = FakeOpenClaw()
    fake.agent_payload = response
    supervisor = _supervisor(supervisor_env, fake)

    with pytest.raises(SupervisorError, match="wake response"):
        supervisor.run_once()


def test_active_owner_lifecycle_for_the_exact_session_suppresses_recovery(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env)
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


def test_terminal_task_with_fresh_owner_lifecycle_suppresses_duplicate_recovery(
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

    assert result.reason == "missing_verification_artifact"


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

    assert result.reason == "recovery_attempts_exhausted"


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
    assert result.reason == "control_plane_provider_blocked"


def test_repeated_stage_capacity_failures_alert_as_control_plane_blockers(
    supervisor_env: SupervisorEnv,
) -> None:
    _prepare_stale_state(supervisor_env, phase=Phase.DEBATE)
    clock = [supervisor_env.now]
    task: dict[str, object] = {
        "taskId": "debate-1",
        "status": "running",
        "agentId": "debater-data",
        "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "ownerKey": AUTORESEARCH_OWNER_SESSION_KEY,
        "childSessionKey": "agent:debater-data:task-child",
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
    assert third.reason == "control_plane_provider_blocked"


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
    assert ("tasks.get", {"taskId": "owner-turn"}) in fake.rpc_calls


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
