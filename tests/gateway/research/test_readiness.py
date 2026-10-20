from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.openclaw_client import OpenClawClient
from gateway.research import readiness
from gateway.research.control import (
    OpenClawReviewCanceller,
    OwnerControl,
    _parse_owner_environment,
    production_owner_control,
)
from gateway.research.readiness import (
    NATIVE_CAPABILITY_MODEL,
    NATIVE_OBSERVED_SERVICE_TIER,
    budget_execution_ready,
    host_execution_ready,
    native_execution_ready,
    read_native_service_tier_observation,
    register_native_runtime_record,
    validate_native_runtime_record_registration,
)
from gateway.research.status import ResearchStatus
from gateway.research.store import ResearchStore, StoreConflict
from gateway.research.wake import OpenClawWakeSender, compose_wake, deliver, poll_owner_turn

from tests.gateway.research.conftest import freeze_with_probe
from tests.gateway.research.native_review_fixtures import OWNER_SESSION, prepare_review
from tests.gateway.research.test_admission import _admit, _document, _payload


def _native_database(path: Path) -> Path:
    managed_root = path.parent / f"{path.stem}-managed"
    core = managed_root / "state" / "openclaw.sqlite"
    agent = managed_root / "agents" / "research-orchestrator" / "agent"
    core.parent.mkdir(parents=True, exist_ok=True)
    agent.joinpath("codex-home").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(core) as connection:
        connection.executescript(
            """
            CREATE TABLE task_runs (task_id TEXT);
            """
        )
    with sqlite3.connect(agent / "openclaw-agent.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT,
              parent_session_key TEXT, spawned_by TEXT, label TEXT, created_at INTEGER,
              entry_valid INTEGER);
            CREATE TABLE trajectory_runtime_events (session_id TEXT, seq INTEGER,
              run_id TEXT, event_json TEXT, created_at INTEGER);
            """
        )
    with sqlite3.connect(agent / "codex-home" / "state_5.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (id TEXT, rollout_path TEXT, source TEXT,
              model_provider TEXT, thread_source TEXT, agent_role TEXT, model TEXT,
              reasoning_effort TEXT, agent_path TEXT, name TEXT);
            CREATE TABLE thread_spawn_edges (parent_thread_id TEXT,
              child_thread_id TEXT, status TEXT);
            """
        )
    core.chmod(0o600)
    return core


def configure_real_readiness(
    store: ResearchStore,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: bool = True,
) -> None:
    """Install only synthetic evidence needed by dispatch-focused fixtures."""
    database = _native_database(base / "native-core.sqlite")
    record, digest = _native_runtime_record(base / "native-rollout.jsonl")
    register_native_runtime_record(store.root, record, digest)
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(database))
    if policy:
        store.set_campaign_policy(100, None, "synthetic-dispatch-fixture")


def _native_runtime_record(
    path: Path,
    *,
    identity: str = "implementer",
    model: str = NATIVE_CAPABILITY_MODEL,
    effort: str = "xhigh",
) -> tuple[Path, str]:
    payload = {
        "type": "session_meta",
        "payload": {
            "id": "native-rollout-001",
            "model": None,
            "reasoning_effort": None,
            "source": {"subagent": {"thread_spawn": {"agent_role": identity}}},
        },
    }
    turn_context = {
        "type": "turn_context",
        "payload": {"model": model, "effort": effort},
    }
    raw = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + json.dumps(turn_context, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _owner_env_file(
    path: Path, database: Path, root: Path, *, token: str = "synthetic-token"
) -> Path:
    path.write_text(
        "\n".join(
            (
                "OPENCLAW_HOST=127.0.0.1",
                "OPENCLAW_PORT=18789",
                f"OPENCLAW_GATEWAY_TOKEN={token}",
                f"RESEARCH_CORE_DATABASE={database}",
                f"RESEARCH_V2_ROOT={root}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_native_guard_accepts_only_registered_official_rollout_record(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    database = _native_database(tmp_path / "core.sqlite")
    record, digest = _native_runtime_record(tmp_path / "native-rollout.jsonl")
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(database))

    register_native_runtime_record(store.root, record, digest)

    validate_native_runtime_record_registration(store.root)
    assert native_execution_ready(store, None) is None


def test_native_guard_rejects_unset_database_and_unapproved_rollout_role(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    assert native_execution_ready(store, None) == "native_core_database_unset"

    record, digest = _native_runtime_record(tmp_path / "native-rollout.jsonl", identity="reviewer")
    with pytest.raises(ValueError, match="approved native role"):
        register_native_runtime_record(store.root, record, digest)


@pytest.mark.parametrize(
    ("model", "effort", "message"),
    [
        ("gpt-5.5", "xhigh", "model"),
        (NATIVE_CAPABILITY_MODEL, "high", "effort"),
    ],
)
def test_native_guard_requires_verified_rollout_model_and_effort(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    model: str,
    effort: str,
    message: str,
) -> None:
    store, _source, _hypothesis = campaign
    record, digest = _native_runtime_record(
        tmp_path / f"wrong-{message}.jsonl", model=model, effort=effort
    )

    with pytest.raises(ValueError, match=message):
        register_native_runtime_record(store.root, record, digest)


def test_old_v1_native_receipt_is_not_a_runtime_record(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path
) -> None:
    store, _source, _hypothesis = campaign
    path = tmp_path / "native-capability-v1.json"
    payload = {
        "effort": "xhigh",
        "identity": "implementer",
        "model": NATIVE_CAPABILITY_MODEL,
        "schema": "native-capability-v1",
        "service_tier": "fast",
    }
    path.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())

    with pytest.raises(ValueError, match="rollout is missing session_meta"):
        register_native_runtime_record(
            store.root, path, hashlib.sha256(path.read_bytes()).hexdigest()
        )


def test_native_tier_reads_pinned_configuration_and_keeps_observation_unknown(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path
) -> None:
    store, _source, _hypothesis = campaign
    record, digest = _native_runtime_record(tmp_path / "native-rollout.jsonl")
    register_native_runtime_record(store.root, record, digest)

    assert read_native_service_tier_observation(store.root) == (
        "fast",
        NATIVE_OBSERVED_SERVICE_TIER,
    )

    alternate_config = tmp_path / "roles"
    alternate_config.mkdir()
    (alternate_config / "implementer.toml").write_text(
        'service_tier = "standard"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must request service tier fast"):
        read_native_service_tier_observation(store.root, role_config_root=alternate_config)


def test_budget_guard_remains_fail_closed_until_explicit_policy(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    assert budget_execution_ready(store, None) == "budget_campaign_policy_unset"
    store.set_campaign_policy(2, None, "synthetic-readiness-fixture")
    assert budget_execution_ready(store, None) is None


def test_host_guard_returns_evidence_result_for_configured_fixture(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, _source, _hypothesis = campaign

    result = host_execution_ready(store, None)

    assert result is None


def test_host_guard_reports_tool_and_bus_refusals(
    campaign: tuple[ResearchStore, Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    monkeypatch.setattr(readiness, "which_tools", lambda: ())
    assert host_execution_ready(store, None) == "host_tools_unavailable"

    monkeypatch.setattr(readiness, "which_tools", lambda: ("/usr/bin/python3", "/usr/bin/python3"))
    monkeypatch.setattr(readiness, "host_bus_environment", lambda: {})
    assert host_execution_ready(store, None) == "host_bus_environment_unavailable"


def test_native_guard_reports_database_schema_permissions_and_digest_refusals(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, _hypothesis = campaign
    database = tmp_path / "native-core.sqlite"
    database.write_bytes(b"not sqlite")
    database.chmod(0o600)
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(database))
    assert native_execution_ready(store, None) == "native_hostrecorderror"

    database.unlink()
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE task_runs (task_id TEXT);")
    database.chmod(0o666)
    assert native_execution_ready(store, None) == "native_core_database_permissions"

    database.chmod(0o600)
    assert native_execution_ready(store, None) == "native_valueerror"

    database.unlink()
    _native_database(database)
    monkeypatch.setenv(
        "RESEARCH_CORE_DATABASE",
        str(tmp_path / "native-core-managed" / "state" / "openclaw.sqlite"),
    )
    record, digest = _native_runtime_record(tmp_path / "digest-rollout.jsonl")
    register_native_runtime_record(store.root, record, digest)
    record.write_bytes(b"mutated")
    assert native_execution_ready(store, None) == "native_valueerror"


def test_budget_guard_allows_admitted_attempt_at_cap_one(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    spec_set_digest = hashlib.sha256(
        store.evaluation_spec_set(hypothesis.hypothesis_id).to_json().encode()
    ).hexdigest()
    admitted = _admit(_document(_payload()), evaluation_spec_set_sha256=spec_set_digest)
    first = store.open_attempt(hypothesis.hypothesis_id, source, admission=admitted)
    store.set_campaign_policy(1, None, "synthetic-cap-one-fixture")

    assert budget_execution_ready(store, first.attempt_id) is None
    assert budget_execution_ready(store, None) == "budget_attempt_cap_exceeded"
    with pytest.raises(StoreConflict, match="campaign attempt cap exceeded"):
        store.open_attempt(hypothesis.hypothesis_id, source, admission=admitted)


def test_budget_guard_reports_wall_clock_cap(
    campaign: tuple[ResearchStore, Path, Any],
) -> None:
    store, _source, _hypothesis = campaign
    store.set_campaign_policy(3, None, "synthetic-cap-fixture")
    with store._connect() as connection:
        connection.execute(
            "UPDATE campaign SET attempt_cap=10, wall_clock_cap_seconds=1, "
            "policy_set_at='2000-01-01T00:00:00Z' WHERE singleton=1"
        )
        connection.commit()
    assert budget_execution_ready(store, None) == "budget_wall_clock_cap_exceeded"


def test_production_factory_consumes_explicit_deployment_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _native_database(tmp_path / "core.sqlite")
    for name in (
        "OPENCLAW_HOST",
        "OPENCLAW_PORT",
        "OPENCLAW_GATEWAY_TOKEN",
        "RESEARCH_CORE_DATABASE",
        "RESEARCH_V2_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = _owner_env_file(tmp_path / "owner.env", database, tmp_path)
    monkeypatch.setenv("G2_OWNER_ENV_FILE", str(env_file))

    control = production_owner_control(tmp_path)
    parsed = _parse_owner_environment(env_file.read_text(encoding="utf-8"))

    assert isinstance(control._review_canceller, OpenClawReviewCanceller)
    assert control._readiness_gate is not None
    assert parsed.refusal is None
    assert set(parsed.values) == {
        "OPENCLAW_HOST",
        "OPENCLAW_PORT",
        "OPENCLAW_GATEWAY_TOKEN",
        "RESEARCH_CORE_DATABASE",
        "RESEARCH_V2_ROOT",
    }


def test_production_factory_gate_uses_file_database_without_process_mutation(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, _hypothesis = campaign
    database = _native_database(tmp_path / "core.sqlite")
    record, digest = _native_runtime_record(store.root / "native-rollout.jsonl")
    register_native_runtime_record(store.root, record, digest)
    store.set_campaign_policy(2, None, "synthetic-protected-env-fixture")
    for name in (
        "OPENCLAW_HOST",
        "OPENCLAW_PORT",
        "OPENCLAW_GATEWAY_TOKEN",
        "RESEARCH_CORE_DATABASE",
        "RESEARCH_V2_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = _owner_env_file(tmp_path / "owner.env", database, store.root)
    monkeypatch.setenv("G2_OWNER_ENV_FILE", str(env_file))

    control = production_owner_control(store.root)

    assert control._readiness_gate is not None
    assert control._readiness_gate(cast(ResearchStatus, None)) is None
    assert "RESEARCH_CORE_DATABASE" not in os.environ


def test_production_factory_selects_contract_from_full_deployment_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _native_database(tmp_path / "core.sqlite")
    for name in (
        "OPENCLAW_HOST",
        "OPENCLAW_PORT",
        "OPENCLAW_GATEWAY_TOKEN",
        "RESEARCH_CORE_DATABASE",
        "RESEARCH_V2_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = _owner_env_file(tmp_path / "owner.env", database, tmp_path)
    extra_deployment_settings = (
        "OPENCLAW_PROVIDER=codex",
        "STT_PROVIDER=fixture",
        "OPENAI_ORG_ID=synthetic",
    )
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "\n" + "\n".join(extra_deployment_settings) + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("G2_OWNER_ENV_FILE", str(env_file))

    control = production_owner_control(tmp_path)

    assert isinstance(control._review_canceller, OpenClawReviewCanceller)
    assert control._readiness_gate is not None


@pytest.mark.parametrize(
    ("content", "mode", "expected"),
    (
        ("OPENCLAW_HOST=127.0.0.1\n", 0o600, "owner_environment_missing_or_empty"),
        (
            "\n".join(
                (
                    "OPENCLAW_HOST=127.0.0.1",
                    "OPENCLAW_PORT=18789",
                    "OPENCLAW_GATEWAY_TOKEN=${TOKEN}",
                    "RESEARCH_CORE_DATABASE=/tmp/core.sqlite",
                    "RESEARCH_V2_ROOT=/tmp/research",
                )
            )
            + "\n",
            0o600,
            "owner_environment_unresolved_placeholder",
        ),
        ("OPENCLAW_HOST 127.0.0.1\n", 0o600, "owner_environment_malformed"),
        ("OPENCLAW_HOST=127.0.0.1\n", 0o640, "owner_environment_file_permissions"),
    ),
)
def test_production_factory_refuses_invalid_protected_owner_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    mode: int,
    expected: str,
) -> None:
    env_file = tmp_path / "owner.env"
    env_file.write_text(content, encoding="utf-8")
    env_file.chmod(mode)
    monkeypatch.setenv("G2_OWNER_ENV_FILE", str(env_file))

    control = production_owner_control(tmp_path)

    assert control._readiness_gate is not None
    assert control._readiness_gate(cast(ResearchStatus, None)) == expected
    assert control._review_canceller is None


def test_production_factory_keeps_status_available_without_auth_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("G2_OWNER_ENV_FILE", raising=False)

    control = production_owner_control(tmp_path)

    status = control.status()
    assert status.available is False
    assert status.unavailable_reason == "research state database is missing"


def test_production_factory_rejects_process_contract_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _native_database(tmp_path / "core.sqlite")
    env_file = _owner_env_file(tmp_path / "owner.env", database, tmp_path)
    monkeypatch.setenv("G2_OWNER_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPENCLAW_PORT", "18790")

    control = production_owner_control(tmp_path)

    assert control._readiness_gate is not None
    assert (
        control._readiness_gate(cast(ResearchStatus, None)) == "owner_environment_contract_mismatch"
    )
    assert control._review_canceller is None


class _FakeGateway:
    def __init__(self, codex_state_database: Path) -> None:
        self.codex_state_database = codex_state_database
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request_once(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float,
        required_server_version: str | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, required_server_version
        self.calls.append((method, dict(params)))
        if method == "agent":
            return {
                "status": "accepted",
                "sessionKey": params["sessionKey"],
                "runId": "owner-run-1",
            }
        if method == "agent.wait":
            return {"runId": params["runId"], "status": "ok"}
        if method == "chat.abort":
            return {"runId": params["runId"], "status": "accepted"}
        raise AssertionError(f"unexpected gateway method: {method}")


class _MismatchedCancelGateway(_FakeGateway):
    async def request_once(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float,
        required_server_version: str | None = None,
    ) -> dict[str, object]:
        if method == "chat.abort":
            del timeout_seconds, required_server_version
            self.calls.append((method, dict(params)))
            return {"runId": "wrong-run", "status": "accepted"}
        return await super().request_once(
            method,
            params,
            timeout_seconds=timeout_seconds,
            required_server_version=required_server_version,
        )


class _ActiveUnit:
    def __init__(self) -> None:
        self.active = True

    def state(self, unit: str) -> str:
        del unit
        return "active" if self.active else "inactive"

    def start(self, unit: str) -> None:
        del unit

    def stop(self, unit: str) -> None:
        del unit
        self.active = False


def test_fake_gateway_proves_wake_wait_and_exact_review_cancel(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")
    gateway = _FakeGateway(native.codex_state_database)
    monkeypatch.setattr(OpenClawClient, "request_once", gateway.request_once)
    sender = OpenClawWakeSender("127.0.0.1", 18789, "synthetic-token")
    session_key = "agent:research-orchestrator:autoresearch:quantipy-v2"

    plan = compose_wake(store)
    assert plan is not None
    run_id = deliver(store, sender, plan, session_key)
    assert run_id == "owner-run-1"
    poll_owner_turn(store, sender)
    wake_row = store.wake_rows()[-1]
    assert wake_row["run_id"] == "owner-run-1"
    assert wake_row["turn_status"] == "ok"

    result = OwnerControl(
        store.root,
        unit=_ActiveUnit(),
        review_canceller=OpenClawReviewCanceller(
            native.openclaw_database, gateway.request_once, native.codex_state_database
        ),
    ).stop()

    assert result.completed is False
    assert result.review_cancellation == "incomplete_pending"
    assert [method for method, _params in gateway.calls] == ["agent", "agent.wait", "chat.abort"]
    assert gateway.calls[-1][1] == {
        "sessionKey": OWNER_SESSION,
        "agentId": "research-orchestrator",
        "runId": "owner-run-1",
    }
    assert store.get_attempt(attempt_id).state.value == "IMPLEMENTED"


def test_mismatched_review_cancel_stays_pending_and_incomplete(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
) -> None:
    store, _attempt_id, _bundle, native = prepare_review(campaign, tmp_path, status="running")
    gateway = _MismatchedCancelGateway(native.codex_state_database)

    result = OwnerControl(
        store.root,
        unit=_ActiveUnit(),
        review_canceller=OpenClawReviewCanceller(
            native.openclaw_database, gateway.request_once, native.codex_state_database
        ),
    ).stop()

    assert result.review_cancellation == "incomplete_pending"
    assert result.completed is False
    assert gateway.calls == [
        (
            "chat.abort",
            {
                "sessionKey": OWNER_SESSION,
                "agentId": "research-orchestrator",
                "runId": "owner-run-1",
            },
        )
    ]


class _ClaudeAttemptStore:
    """Just enough store for the claude implementer check: attempt open time and refusals."""

    def __init__(self, opened_at: str = "2026-10-19T00:00:00Z") -> None:
        self.opened_at = opened_at
        self.refusals: list[str] = []

    def get_attempt(self, _attempt_id: str) -> Any:
        return type("Attempt", (), {"opened_at": self.opened_at})()

    def record_review_event(self, _attempt_id: str, _kind: str, detail: dict[str, str]) -> None:
        self.refusals.append(detail["reason"])


def _claude_ready(
    monkeypatch: pytest.MonkeyPatch,
    stores: Any,
    store: _ClaudeAttemptStore,
    attempt: str | None = "A0001",
    *,
    session: str | None = "default",
) -> str | None:
    from gateway.agent_profiles import load_profile

    from tests.gateway.research.test_host_records_claude import OWNER_KEY

    monkeypatch.setattr(readiness, "active_runtime_profile", lambda: load_profile("claude"))
    return native_execution_ready(
        cast(Any, store),
        attempt,
        claude_state=stores,
        owner_session_key=OWNER_KEY if session == "default" else session,
    )


def _claude_fixture(tmp_path: Path, effort: str = "xhigh", model: str = "claude-sonnet-5-5") -> Any:
    from tests.gateway.research.test_host_records_claude import (
        CHILD_KEY,
        SESSION_ID,
        T0,
        add_node,
        add_subagent,
        add_task,
        api_request,
        make_stores,
        write_transcript,
    )

    stores = make_stores(tmp_path)
    add_task(stores, "impl-1", created=T0 + 1000)
    add_subagent(stores, "impl-1", {"task": "t", "model": f"anthropic/{model}"})
    add_node(
        stores,
        "research-implementer",
        CHILD_KEY,
        {
            "model": model,
            "thinkingLevel": effort,
            "claudeCliSessionId": SESSION_ID,
            "lastRunId": "impl-1",
        },
    )
    write_transcript(stores, SESSION_ID, [api_request(model, effort)])
    return stores


# 2026-10-19T00:00:00Z is before T0 (1792463900000 ms == 2026-10-20), so use an earlier open.
def test_claude_readiness_accepts_exact_implementer_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _ClaudeAttemptStore()
    assert _claude_ready(monkeypatch, _claude_fixture(tmp_path), store) is None
    assert store.refusals == []


def test_claude_readiness_start_gate_needs_only_readable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _ClaudeAttemptStore()
    assert _claude_ready(monkeypatch, _claude_fixture(tmp_path), store, None) is None
    from gateway.research.host_records_claude import claude_stores

    missing = _claude_ready(monkeypatch, claude_stores(tmp_path / "none"), store, None)
    assert missing is not None and missing.startswith("native_")


@pytest.mark.parametrize(
    ("effort", "model", "reason"),
    [
        ("high", "claude-sonnet-5-5", "native_claude_implementer_effort"),
        ("xhigh", "claude-opus-5-5", "native_claude_implementer_model"),
    ],
)
def test_claude_readiness_rejects_wrong_model_or_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str, model: str, reason: str
) -> None:
    store = _ClaudeAttemptStore()
    stores = _claude_fixture(tmp_path, effort=effort, model=model)
    assert _claude_ready(monkeypatch, stores, store) == reason


def test_claude_readiness_fails_closed_without_transcript_or_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _claude_fixture(tmp_path)
    for transcript in stores.claude_config_dir.rglob("*.jsonl"):
        transcript.unlink()
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_model"
    )
    later = _ClaudeAttemptStore(opened_at="2027-01-01T00:00:00Z")
    assert _claude_ready(monkeypatch, stores, later) == "native_claude_implementer_missing"
    assert _claude_ready(monkeypatch, stores, later, session="agent:other") == (
        "native_claude_implementer_missing"
    )
    for unset in ("", None):
        assert _claude_ready(monkeypatch, stores, _ClaudeAttemptStore(), session=unset) == (
            "native_claude_owner_session_unset"
        )


def test_claude_readiness_requires_ok_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _claude_fixture(tmp_path)
    with sqlite3.connect(stores.openclaw_state_db) as connection:
        connection.execute("UPDATE task_runs SET status = 'running'")
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_not_ok"
    )


def test_codex_profile_keeps_the_codex_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.agent_profiles import load_profile

    monkeypatch.setattr(readiness, "active_runtime_profile", lambda: load_profile("codex"))
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    store = _ClaudeAttemptStore()
    assert native_execution_ready(cast(Any, store), None) == "native_core_database_unset"


def test_claude_readiness_checks_every_implementer_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway.research.test_host_records_claude import (
        T0,
        add_node,
        add_subagent,
        add_task,
        api_request,
        write_transcript,
    )

    stores = _claude_fixture(tmp_path)
    older_key = "agent:research-implementer:subagent:older"
    add_task(stores, "impl-0", created=T0 + 10, child=older_key)
    add_subagent(
        stores, "impl-0", {"task": "t", "model": "anthropic/claude-sonnet-5-5"}, child=older_key
    )
    add_node(
        stores,
        "research-implementer",
        older_key,
        {
            "model": "claude-sonnet-5-5",
            "thinkingLevel": "xhigh",
            "claudeCliSessionId": "older-session",
            "lastRunId": "impl-0",
        },
    )
    write_transcript(
        stores, "older-session", [api_request("claude-sonnet-5-5", "low", "older-session")]
    )
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_effort"
    )


def test_claude_readiness_counts_builtin_subagent_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway.research.test_host_records_claude import SESSION_ID, api_request

    stores = _claude_fixture(tmp_path)
    transcript = next(stores.claude_config_dir.rglob(f"{SESSION_ID}.jsonl"))
    directory = transcript.parent / SESSION_ID / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-x.jsonl").write_text(
        api_request("claude-haiku-5", "xhigh") + "\n", encoding="utf-8"
    )
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_model"
    )
    (directory / "agent-x.jsonl").write_text(
        api_request("claude-sonnet-5-5", "low") + "\n", encoding="utf-8"
    )
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_effort"
    )


def test_claude_readiness_requires_explicit_thinking_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _claude_fixture(tmp_path)
    database = stores.agents_dir / "research-implementer" / "agent" / "openclaw-agent.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session_nodes SET entry_json = json_set(entry_json, '$.thinkingLevel', 'high')"
        )
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_effort"
    )


def test_claude_readiness_ignores_unrelated_child_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway.research.test_host_records_claude import (
        T0,
        add_node,
        add_task,
        write_transcript,
    )

    stores = _claude_fixture(tmp_path)
    key = "agent:research-reviewer:subagent:r"
    add_task(stores, "rev-1", agent_id="research-reviewer", child=key, created=T0 + 5)
    add_node(stores, "research-reviewer", key, {"claudeCliSessionId": "rs", "lastRunId": "rev-1"})
    write_transcript(stores, "rs", ['{"type":"api-request","params":'], slug="-rev")
    assert _claude_ready(monkeypatch, stores, _ClaudeAttemptStore()) is None


def test_claude_readiness_counts_nested_workflow_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway.research.test_host_records_claude import SESSION_ID, api_request

    stores = _claude_fixture(tmp_path)
    transcript = next(stores.claude_config_dir.rglob(f"{SESSION_ID}.jsonl"))
    journal = transcript.parent / SESSION_ID / "subagents" / "workflows" / "wf_1"
    journal.mkdir(parents=True)
    (journal / "journal.jsonl").write_text(
        api_request("claude-opus-5-5", "xhigh") + "\n", encoding="utf-8"
    )
    assert (
        _claude_ready(monkeypatch, stores, _ClaudeAttemptStore())
        == "native_claude_implementer_model"
    )
