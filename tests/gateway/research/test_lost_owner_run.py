"""An owner run lost to a gateway restart is recorded and re-woken, bounded per resume."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gateway.openclaw_client import OpenClawError
from gateway.research.contracts import HypothesisSpec
from gateway.research.lost_runs import (
    GATEWAY_RESTART_STATUS,
    MAX_LOST_OWNER_RUNS,
    UNKNOWN_RUN_STATUS,
    lost_wake_key,
)
from gateway.research.status import build_status_frame, read_status
from gateway.research.store import ResearchStore, canonical_wake_key
from gateway.research.wake import (
    _LOST_RUN_SUFFIX,
    GatewayInstance,
    OpenClawWakeSender,
    WakePlan,
    _compose_base_wake,
    _parse_systemd_utc,
    compose_wake,
    deliver,
    poll_owner_turn,
)

from tests.gateway.research.native_review_fixtures import (
    OWNER_SESSION,
    OWNER_THREAD_ID,
    create_owner_database,
    insert_owner_event,
    owner_ended,
)

Campaign = tuple[ResearchStore, Path, HypothesisSpec]
FUTURE = datetime.now(UTC) + timedelta(days=1)
PAST = datetime.now(UTC) - timedelta(days=1)


class Sender:
    """Counts sends; ``wait`` is the agent.wait payload, ``started`` the gateway start time."""

    def __init__(
        self, wait: object, started: datetime | None = None, invocation: str | None = None
    ) -> None:
        self.wait = wait
        self.started = started
        self.invocation = invocation
        self.sent: list[tuple[str, str]] = []
        self.polls = 0

    def send(self, message: str, session_key: str, idempotency_key: str) -> str:
        self.sent.append((message, idempotency_key))
        return f"run-{len(self.sent)}"

    def owner_task_status(self, _run_id: str) -> dict[str, object]:
        self.polls += 1
        if isinstance(self.wait, Exception):
            raise self.wait
        assert isinstance(self.wait, dict)
        return self.wait

    def gateway_instance(self) -> GatewayInstance | None:
        if self.started is None and self.invocation is None:
            return None
        return GatewayInstance(self.invocation, self.started)


def _plan(store: ResearchStore) -> WakePlan:
    plan = compose_wake(store)
    assert plan is not None
    return plan


def _lose(store: ResearchStore, sender: Sender) -> None:
    plan = _plan(store)
    assert deliver(store, sender, plan, "owner") is not None
    poll_owner_turn(store, sender)


def _events(store: ResearchStore, kind: str) -> list[dict[str, object]]:
    return [json.loads(event.detail) for event in store.events() if event.kind == kind]


def test_unknown_run_response_is_lost_without_pausing(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "run-1", "status": "unknown"})
    _lose(store, sender)
    row = store.wake_rows()[-1]
    assert row["turn_status"] == "lost"
    statuses = _events(store, "owner_turn_status")
    assert statuses[-1]["status"] == "lost" and statuses[-1]["reason"]
    failed = _events(store, "owner_turn_failed")
    assert len(failed) == 1 and failed[0]["status"] == UNKNOWN_RUN_STATUS
    assert store.campaign()[0] == "ACTIVE"
    assert store.lost_owner_runs()[0] == 1


def test_rejected_unknown_run_error_is_lost_but_other_errors_propagate(campaign: Campaign) -> None:
    store = campaign[0]
    other = Sender(OpenClawError("agent.wait request rejected: bad token"))
    plan = _plan(store)
    deliver(store, other, plan, "owner")
    with pytest.raises(OpenClawError):
        poll_owner_turn(store, other)
    assert store.lost_owner_runs()[0] == 0
    unknown = Sender(OpenClawError("agent.wait request rejected: unknown run run-1"))
    poll_owner_turn(store, unknown)
    assert store.wake_rows()[-1]["turn_status"] == "lost"


def test_gateway_restart_after_send_is_lost(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "run-1", "status": "timeout"}, started=FUTURE)
    _lose(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "lost"
    failed = _events(store, "owner_turn_failed")
    assert failed[0]["status"] == GATEWAY_RESTART_STATUS
    assert store.campaign()[0] == "ACTIVE"


def test_gateway_started_before_send_is_not_lost(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "run-1", "status": "timeout"}, started=PAST)
    _lose(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "timeout"
    assert store.lost_owner_runs()[0] == 0
    unreadable = Sender({"runId": "run-1", "status": "timeout"}, started=None)
    poll_owner_turn(store, unreadable)
    assert store.wake_rows()[-1]["turn_status"] == "timeout"


def test_terminal_status_wins_over_restart_time(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "run-1", "status": "ok"}, started=FUTURE)
    _lose(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "ok"
    assert store.lost_owner_runs()[0] == 0


def test_lost_run_rewakes_once_with_interruption_note(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "run-1", "status": "timeout"}, started=FUTURE)
    first = _plan(store)
    assert _LOST_RUN_SUFFIX not in first.message
    _lose(store, sender)
    second = _plan(store)
    assert second.pending_key != first.pending_key
    assert second.pending_key == lost_wake_key(first.pending_key, 1)
    assert second.message.startswith(first.message)
    assert second.message.endswith(_LOST_RUN_SUFFIX)
    assert "interrupted by a gateway restart" in second.message
    assert "durable notes" in second.message
    sender.started = None
    assert deliver(store, sender, second, "owner") is not None
    assert deliver(store, sender, _plan(store), "owner") is None
    assert len(sender.sent) == 2


def test_third_lost_run_pauses_and_resume_resets(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "x", "status": "timeout"}, started=FUTURE)
    keys: set[str] = set()
    for expected in range(1, MAX_LOST_OWNER_RUNS):
        keys.add(_plan(store).pending_key)
        _lose(store, sender)
        assert store.lost_owner_runs()[0] == expected
        assert store.campaign()[0] == "ACTIVE"
    keys.add(_plan(store).pending_key)
    _lose(store, sender)
    assert len(keys) == MAX_LOST_OWNER_RUNS
    assert store.lost_owner_runs()[0] == MAX_LOST_OWNER_RUNS
    assert store.campaign()[0] == "PAUSED"
    assert compose_wake(store) is None
    paused = _events(store, "campaign_paused")
    assert "owner runs were lost" in str(paused[-1]["reason"])
    store.resume("operator reviewed")
    assert store.lost_owner_runs()[0] == 0
    resumed = _plan(store)
    assert resumed.message == _compose_base_wake(store).message  # type: ignore[union-attr]


def test_zero_lost_keys_and_texts_are_unchanged(campaign: Campaign) -> None:
    store, _source, hypothesis = campaign
    plan = _plan(store)
    base = _compose_base_wake(store)
    assert base == plan
    _status, resume_seq = store.campaign()
    legacy = hashlib.sha256(f"{plan.hypothesis_id}||{plan.state}|{resume_seq}".encode()).hexdigest()
    assert plan.pending_key == legacy
    assert _LOST_RUN_SUFFIX not in plan.message
    assert hypothesis.hypothesis_id in plan.message


def test_status_frame_reports_lost_count_additively(campaign: Campaign) -> None:
    store = campaign[0]
    root = store.root
    assert "lostOwnerRuns" not in build_status_frame(
        read_status(root, unit_state=lambda _: "active")
    )
    _lose(store, Sender({"runId": "x", "status": "timeout"}, started=FUTURE))
    status = read_status(root, unit_state=lambda _: "active")
    assert status.lost_owner_runs == 1
    assert build_status_frame(status)["lostOwnerRuns"] == 1


def _systemctl(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    import subprocess

    import gateway.research.wake as wake

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(wake, "_run", fake_run)
    return calls


def test_gateway_instance_reads_systemd_wall_clock_and_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _systemctl(
        monkeypatch,
        "ActiveState=active\nActiveEnterTimestamp=Fri 2026-10-09 21:56:26.935517 UTC\n"
        "InvocationID=38925a5c35fb4b18be33b3314400b603\n",  # pragma: allowlist secret
    )
    instance = OpenClawWakeSender("127.0.0.1", 1, "t").gateway_instance()
    assert instance == GatewayInstance(
        "38925a5c35fb4b18be33b3314400b603",  # pragma: allowlist secret
        datetime(2026, 10, 9, 21, 56, 26, 935517, tzinfo=UTC),
    )
    assert "--timestamp=us+utc" in calls[0] and "ActiveEnterTimestamp" in calls[0]
    assert _parse_systemd_utc("Fri 2026-10-09 21:56:26 UTC") == datetime(
        2026, 10, 9, 21, 56, 26, tzinfo=UTC
    )
    assert _parse_systemd_utc("n/a") is None


def test_gateway_instance_is_none_when_inactive_or_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = OpenClawWakeSender("127.0.0.1", 1, "t")
    _systemctl(monkeypatch, "ActiveState=inactive\nActiveEnterTimestamp=\nInvocationID=\n")
    assert sender.gateway_instance() is None
    _systemctl(monkeypatch, "")
    assert sender.gateway_instance() is None


def test_delivery_records_invocation_and_changed_invocation_is_a_restart(
    campaign: Campaign,
) -> None:
    store = campaign[0]
    sender = Sender({"runId": "x", "status": "timeout"}, started=PAST, invocation="inv-1")
    plan = _plan(store)
    deliver(store, sender, plan, "owner")
    assert store.wake_rows()[-1]["gateway_invocation_id"] == "inv-1"
    poll_owner_turn(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "timeout"  # same invocation: still running
    # A different invocation is a restart even though the reported start time looks old.
    sender.invocation = "inv-2"
    poll_owner_turn(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "lost"
    reason = _events(store, "owner_turn_failed")[-1]["reason"]
    assert "inv-1" in str(reason) and "inv-2" in str(reason)


def test_same_invocation_wins_over_a_newer_looking_timestamp(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "x", "status": "timeout"}, started=FUTURE, invocation="inv-1")
    deliver(store, sender, _plan(store), "owner")
    poll_owner_turn(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "timeout"
    assert store.lost_owner_runs()[0] == 0


def test_only_the_newest_delivered_row_can_be_lost_older_rows_are_superseded(
    campaign: Campaign,
) -> None:
    store = campaign[0]
    sender = Sender({"runId": "x", "status": "timeout"})
    first = _plan(store)
    deliver(store, sender, first, "owner")
    poll_owner_turn(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "timeout"
    resume_seq = store.campaign()[1]
    assert store.reserve_wake("newer", None, "FROZEN", resume_seq)
    store.complete_wake("newer", "run-2")
    store.update_wake_status("newer", "ok")
    # The gateway started after both rows were sent, yet only the newest row is considered.
    poll_owner_turn(store, Sender({"runId": "x", "status": "timeout"}, started=FUTURE))
    statuses = {str(r["pending_key"]): r["turn_status"] for r in store.wake_rows()}
    assert statuses[first.pending_key] == "superseded"
    assert statuses["newer"] == "ok"
    assert store.lost_owner_runs()[0] == 0
    assert not _events(store, "owner_turn_failed")
    assert _plan(store) == first  # no salted key, so no re-wake
    assert deliver(store, sender, first, "owner") is None
    assert len(sender.sent) == 1


def test_row_from_before_the_last_resume_is_superseded_not_lost(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "x", "status": "timeout"}, started=FUTURE)
    deliver(store, sender, _plan(store), "owner")
    store.resume("operator pause/resume")
    poll_owner_turn(store, sender)
    assert store.wake_rows()[-1]["turn_status"] == "superseded"
    assert store.lost_owner_runs()[0] == 0
    assert not _events(store, "owner_turn_failed")


def _real_host_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    end_data: dict[str, object] | None,
) -> None:
    """Point the reader at a real native-schema database whose run-1 ends with ``end_data``."""
    import gateway.research.wake as wake

    database = create_owner_database(
        tmp_path / "native.sqlite", owner_run_id="run-1", owner_ended_event=False
    )
    if end_data is not None:
        ended = owner_ended(OWNER_THREAD_ID)
        ended["data"] = {"threadId": OWNER_THREAD_ID, **end_data}
        insert_owner_event(database, run_id="run-1", event=ended, created_at_ms=1_700_000_100_000)

    class Stores:
        openclaw_database = database

    monkeypatch.setenv("RESEARCH_CORE_DATABASE", "/managed/state/openclaw.sqlite")
    monkeypatch.setattr(wake, "managed_native_databases", lambda _core: Stores())
    # The fixture session key is OWNER_SESSION; the wake row's sent_at is irrelevant to a
    # run-id-bound lookup.


def _poll_after_restart(store: ResearchStore) -> Sender:
    sender = Sender({"runId": "x", "status": "timeout"}, started=FUTURE)
    deliver(store, sender, _plan(store), OWNER_SESSION)
    poll_owner_turn(store, sender, session_key=OWNER_SESSION)
    return sender


def test_interrupted_end_event_is_lost_and_salts_the_next_wake(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = campaign[0]
    first = _plan(store)
    _real_host_record(monkeypatch, tmp_path, {"status": "interrupted", "aborted": True})
    _poll_after_restart(store)
    assert store.wake_rows()[-1]["turn_status"] == "lost"
    assert _events(store, "owner_turn_failed")[-1]["lost"] is True
    assert store.lost_owner_runs()[0] == 1
    assert _plan(store).pending_key == lost_wake_key(first.pending_key, 1)


@pytest.mark.parametrize(
    "end_data",
    [
        {"status": "interrupted"},
        {"status": "success", "aborted": True},
        {"status": "success", "externalAbort": True},
        {"status": "unexpected"},
        {},
    ],
)
def test_aborted_or_unrecognised_end_events_stay_lost(
    campaign: Campaign,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    end_data: dict[str, object],
) -> None:
    store = campaign[0]
    _real_host_record(monkeypatch, tmp_path, end_data)
    _poll_after_restart(store)
    assert store.wake_rows()[-1]["turn_status"] == "lost"


def test_error_end_event_records_error_without_a_lost_count(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = campaign[0]
    _real_host_record(monkeypatch, tmp_path, {"status": "error", "aborted": False})
    _poll_after_restart(store)
    assert store.wake_rows()[-1]["turn_status"] == "error"
    assert store.lost_owner_runs()[0] == 0
    assert _events(store, "owner_turn_failed")[-1]["status"] == "error"


def test_success_end_event_records_ok_without_a_lost_count(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = campaign[0]
    _real_host_record(monkeypatch, tmp_path, {"status": "success", "aborted": False})
    _poll_after_restart(store)
    assert store.wake_rows()[-1]["turn_status"] == "ok"
    assert store.lost_owner_runs()[0] == 0
    assert not _events(store, "owner_turn_failed")


def test_run_without_an_end_event_or_record_is_still_lost(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = campaign[0]
    _real_host_record(monkeypatch, tmp_path, None)  # started, never ended
    _poll_after_restart(store)
    assert store.wake_rows()[-1]["turn_status"] == "lost"


def test_unrecorded_run_is_still_lost(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import gateway.research.wake as wake
    from gateway.research.host_records import HostRecordNotRecorded

    def not_recorded(*_a: object, **_k: object) -> object:
        raise HostRecordNotRecorded("not flushed")

    class Stores:
        openclaw_database = tmp_path / "unused.sqlite"

    monkeypatch.setenv("RESEARCH_CORE_DATABASE", "/managed/state/openclaw.sqlite")
    monkeypatch.setattr(wake, "managed_native_databases", lambda _core: Stores())
    monkeypatch.setattr(wake, "read_exact_native_owner", not_recorded)
    _poll_after_restart(campaign[0])
    assert campaign[0].wake_rows()[-1]["turn_status"] == "lost"


def test_unknown_run_error_match_is_narrow() -> None:
    from gateway.research.wake import _UNKNOWN_RUN_ERROR

    assert _UNKNOWN_RUN_ERROR.search("agent.wait request rejected: agent run was not found")
    assert _UNKNOWN_RUN_ERROR.search("Unknown run abc")
    assert _UNKNOWN_RUN_ERROR.search("method not found") is None
    assert _UNKNOWN_RUN_ERROR.search("not_found") is None


def test_wake_sent_at_keeps_microseconds_so_text_order_is_chronological(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.research.store as store_module

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FrozenDatetime:
            return cls(2026, 10, 9, 12, 0, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(store_module, "datetime", FrozenDatetime)
    assert store_module._wake_sent_at() == "2026-10-09T12:00:00.000000Z"


def test_no_second_pause_event_when_already_paused(campaign: Campaign) -> None:
    store = campaign[0]
    sender = Sender({"runId": "x", "status": "timeout"}, started=FUTURE)
    for _ in range(MAX_LOST_OWNER_RUNS):
        _lose(store, sender)
    assert len(_events(store, "campaign_paused")) == 1
    # A further lost run on an already paused campaign (operator paused first) adds no event.
    plan_key = store.wake_rows()[-1]["pending_key"]
    assert store.reserve_wake("manual", None, "X", store.campaign()[1])
    store.complete_wake("manual", "run-x")
    store.mark_wake_lost("manual", GATEWAY_RESTART_STATUS, "again")
    assert plan_key != "manual"
    assert len(_events(store, "campaign_paused")) == 1


def test_recovered_loss_clears_boundary_failure_on_the_next_ok(campaign: Campaign) -> None:
    store = campaign[0]
    _lose(store, Sender({"runId": "x", "status": "timeout"}, started=FUTURE))
    root = store.root
    assert read_status(root, unit_state=lambda _: "active").boundary_failure == (
        GATEWAY_RESTART_STATUS
    )
    sender = Sender({"runId": "x", "status": "ok"})
    deliver(store, sender, _plan(store), "owner")
    poll_owner_turn(store, sender)
    assert read_status(root, unit_state=lambda _: "active").boundary_failure is None


def test_no_hypothesis_wake_salts_refusals_and_lost_runs_together(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "empty-driver")
    base = _plan(store)
    store._record_create_refusal(ValueError("underpowered"), tmp_path / "missing-spec.json")
    refused = _plan(store)
    refusal_key = canonical_wake_key("H0001", "create_refusals:1", "NO_HYPOTHESIS", 0)
    assert refused.pending_key == refusal_key != base.pending_key
    assert "1 hypothesis-create refusal(s)" in refused.message
    sender = Sender({"runId": "x", "status": "timeout"}, started=FUTURE)
    deliver(store, sender, refused, "owner")
    poll_owner_turn(store, sender)
    after_loss = _plan(store)
    assert after_loss.pending_key == lost_wake_key(refusal_key, 1) != refusal_key
    assert after_loss.message.startswith(refused.message)
    assert after_loss.message.endswith(_LOST_RUN_SUFFIX)


def test_wake_events_use_wake_rows_table(campaign: Campaign) -> None:
    store = campaign[0]
    _lose(store, Sender({"runId": "x", "status": "timeout"}, started=FUTURE))
    with sqlite3.connect(store.root / "state.sqlite3") as conn:
        assert conn.execute("SELECT turn_status FROM wake_deliveries").fetchone()[0] == "lost"


def test_canonical_wake_key_salts_only_when_runs_were_lost() -> None:
    base = canonical_wake_key("H0001", "A0001", "IMPLEMENTED", 2)
    assert canonical_wake_key("H0001", "A0001", "IMPLEMENTED", 2, 0) == base
    assert canonical_wake_key("H0001", "A0001", "IMPLEMENTED", 2, 1) == lost_wake_key(base, 1)
    assert lost_wake_key(base, 1) != lost_wake_key(base, 2) != base


def test_old_roots_without_the_invocation_column_migrate_additively(tmp_path: Path) -> None:
    root = tmp_path / "old-root"
    ResearchStore(root)
    with sqlite3.connect(root / "state.sqlite3") as conn:
        conn.execute("ALTER TABLE wake_deliveries DROP COLUMN gateway_invocation_id")
    store = ResearchStore(root)
    assert store.reserve_wake("k", None, "NO_HYPOTHESIS", 0)
    store.complete_wake("k", "run-1")
    row = store.wake_row("k")
    assert row is not None and row["gateway_invocation_id"] is None
