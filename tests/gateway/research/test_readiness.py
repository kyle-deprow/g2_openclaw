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
    production_owner_control,
)
from gateway.research.readiness import (
    NATIVE_CAPABILITY_MODEL,
    budget_execution_ready,
    host_execution_ready,
    native_execution_ready,
    register_native_capability_receipt,
    validate_native_capability_registration,
)
from gateway.research.status import ResearchStatus
from gateway.research.store import ResearchStore
from gateway.research.wake import OpenClawWakeSender, compose_wake, deliver, poll_owner_turn

from tests.gateway.research.test_review_evidence import _prepare_review


def _native_database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE task_runs (task_id TEXT);
            CREATE TABLE acp_sessions (session_key TEXT);
            """
        )
    path.chmod(0o600)
    return path


def configure_real_readiness(
    store: ResearchStore,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: bool = True,
) -> None:
    """Install only synthetic evidence needed by dispatch-focused fixtures."""
    database = _native_database(base / "native-core.sqlite")
    receipt, digest = _native_receipt(base / "native-receipt.json")
    register_native_capability_receipt(store.root, receipt, digest)
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(database))
    if policy:
        store.set_campaign_policy(100, None, "synthetic-dispatch-fixture")


def _native_receipt(path: Path, *, identity: str = "implementer") -> tuple[Path, str]:
    payload = {
        "effort": "xhigh",
        "identity": identity,
        "model": NATIVE_CAPABILITY_MODEL,
        "schema": "native-capability-v1",
        "service_tier": "fast",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
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


def test_native_guard_accepts_only_registered_synthetic_receipt(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    database = _native_database(tmp_path / "core.sqlite")
    receipt, digest = _native_receipt(tmp_path / "native-receipt.json")
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(database))

    register_native_capability_receipt(store.root, receipt, digest)

    validate_native_capability_registration(store.root)
    assert native_execution_ready(store, None) is None


def test_native_guard_rejects_unset_database_and_unapproved_receipt(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _hypothesis = campaign
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    assert native_execution_ready(store, None) == "native_core_database_unset"

    receipt, digest = _native_receipt(tmp_path / "native-receipt.json", identity="reviewer")
    with pytest.raises(ValueError, match="approved native role"):
        register_native_capability_receipt(store.root, receipt, digest)


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
    assert native_execution_ready(store, None) == "native_core_database_schema"

    database.unlink()
    _native_database(database)
    receipt, digest = _native_receipt(tmp_path / "digest-receipt.json")
    register_native_capability_receipt(store.root, receipt, digest)
    receipt.write_bytes(b"mutated")
    assert native_execution_ready(store, None) == "native_valueerror"


def test_budget_guard_reports_attempt_and_wall_clock_caps(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path
) -> None:
    store, source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    store.open_attempt(hypothesis.hypothesis_id, source)
    store.set_campaign_policy(1, None, "synthetic-cap-fixture")
    assert budget_execution_ready(store, None) == "budget_attempt_cap_exceeded"

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

    assert isinstance(control._review_canceller, OpenClawReviewCanceller)
    assert control._readiness_gate is not None


def test_production_factory_gate_uses_file_database_without_process_mutation(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, _hypothesis = campaign
    database = _native_database(tmp_path / "core.sqlite")
    receipt, digest = _native_receipt(store.root / "native-receipt.json")
    register_native_capability_receipt(store.root, receipt, digest)
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


@pytest.mark.parametrize(
    ("content", "mode", "expected"),
    (
        ("OPENCLAW_HOST=127.0.0.1\nUNKNOWN=value\n", 0o600, "owner_environment_unknown_key"),
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
    def __init__(self, core_database: Path) -> None:
        self.core_database = core_database
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
        if method == "tasks.cancel":
            task_id = str(params["taskId"])
            with sqlite3.connect(self.core_database) as connection:
                connection.execute(
                    "UPDATE task_runs SET status='cancelled' WHERE task_id=?",
                    (task_id,),
                )
                connection.commit()
            return {"taskId": task_id, "status": "cancelled"}
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
        if method == "tasks.cancel":
            del timeout_seconds, required_server_version
            self.calls.append((method, dict(params)))
            return {"taskId": "wrong-task", "status": "cancelled"}
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
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )
    gateway = _FakeGateway(core)
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
        review_canceller=OpenClawReviewCanceller(core, gateway.request_once),
    ).stop()

    assert result.completed is True
    assert result.review_cancellation == "cancelled"
    assert [method for method, _params in gateway.calls] == [
        "agent",
        "agent.wait",
        "tasks.cancel",
    ]
    assert gateway.calls[-1][1]["taskId"] == "task-1"
    assert store.get_attempt(attempt_id).state.value == "IMPLEMENTED"


def test_mismatched_review_cancel_stays_pending_and_incomplete(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
) -> None:
    store, _attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )
    gateway = _MismatchedCancelGateway(core)

    result = OwnerControl(
        store.root,
        unit=_ActiveUnit(),
        review_canceller=OpenClawReviewCanceller(core, gateway.request_once),
    ).stop()

    assert result.review_cancellation == "incomplete_pending"
    assert result.completed is False
    assert gateway.calls == [("tasks.cancel", {"taskId": "task-1", "reason": "operator stop"})]
