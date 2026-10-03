"""Focused native-review correlation tests.

These fixtures model the record shapes the installed OpenClaw/Codex host really
writes (see ``native_review_fixtures``).  They intentionally avoid the installed
campaign database and never exercise a live gateway RPC.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research import host_records, review_evidence
from gateway.research.host_records import (
    HostRecordError,
    HostRecordNotRecorded,
    HostRecordPending,
    NativeChildHostRecord,
    read_exact_native_child,
    read_exact_native_owner,
)
from gateway.research.review_evidence import _ack_from_payload, request_cancel
from gateway.research.store import StoreConflict
from typer.testing import CliRunner

from tests.gateway.research.native_review_fixtures import (
    OLD_OWNER_RUN_ID,
    OWNER_RUN_ID,
    OWNER_SESSION,
    OWNER_SESSION_ID,
    OWNER_THREAD_ID,
    ROTATED_OWNER_THREAD_ID,
    NativeReviewStores,
    add_announce,
    build_native_host,
    create_owner_database,
    insert_owner_event,
    set_owner_node,
    write_child_rollout,
    write_owner_rollout,
)

TASK_NAME = "review_task"
RUNTIME_TASK_NAME = f"/root/{TASK_NAME}"
RESERVED_AT = 1_800_000_000_000
OWNER_STARTED_AT = RESERVED_AT - 5_000
VERDICT = '{"verdict":"PASS"}'


def _stores(
    tmp_path: Path,
    *,
    status: str = "succeeded",
    announce: str | None = "auto",
    model: str = "gpt-5.6-sol",
    effort: str = "xhigh",
    child_name: str | None = None,
    owner_ended_event: bool = True,
    owner_ended_at_ms: int | None = None,
    owner_flushed: bool = True,
) -> NativeReviewStores:
    openclaw = create_owner_database(
        tmp_path / "owner.sqlite",
        started_at_ms=OWNER_STARTED_AT,
        ended_at_ms=owner_ended_at_ms,
        owner_ended_event=owner_ended_event,
        owner_flushed=owner_flushed,
    )
    return build_native_host(
        tmp_path,
        task_name=TASK_NAME,
        reserved_at_ms=RESERVED_AT,
        last_agent_message=VERDICT,
        status=status,
        announce=announce,
        model=model,
        effort=effort,
        child_name=child_name,
        openclaw_database=openclaw,
    )


def _read(
    stores: NativeReviewStores,
    reserved_at: int = RESERVED_AT,
    *,
    callback: bool = True,
) -> NativeChildHostRecord:
    return read_exact_native_child(
        stores.openclaw_database,
        stores.codex_state_database,
        OWNER_SESSION,
        OWNER_RUN_ID,
        OWNER_THREAD_ID,
        TASK_NAME,
        reserved_at,
        corroborate_completion_callback=callback,
    )


def _mutate_event(
    openclaw: Path, run_id: str, event_type: str, mutate: Callable[[dict[str, Any]], object]
) -> None:
    with sqlite3.connect(openclaw) as connection:
        rows = connection.execute(
            "SELECT session_id, seq, event_json FROM trajectory_runtime_events WHERE run_id=?",
            (run_id,),
        ).fetchall()
        changed = 0
        for session_id, seq, raw in rows:
            event = json.loads(str(raw))
            if event["type"] != event_type:
                continue
            mutate(event)
            connection.execute(
                "UPDATE trajectory_runtime_events SET event_json=? WHERE session_id=? AND seq=?",
                (json.dumps(event), session_id, seq),
            )
            changed += 1
        assert changed == 1


def _update_source(
    codex: Path, child_thread_id: str, mutate: Callable[[dict[str, Any]], object]
) -> None:
    with sqlite3.connect(codex) as connection:
        row = connection.execute(
            "SELECT source FROM threads WHERE id=?", (child_thread_id,)
        ).fetchone()
        assert row is not None
        source = json.loads(str(row[0]))
        mutate(source)
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), child_thread_id)
        )


def test_native_correlation_uses_official_child_identity(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    child = _read(stores)
    assert child.child_thread_id == stores.child_thread_id
    assert child.task_name == RUNTIME_TASK_NAME
    assert child.terminal_state == "succeeded"
    assert child.last_agent_message == VERDICT
    assert child.announce_status == "succeeded"
    assert child.announce_run_id == stores.announce_run_id
    assert child.spawn_call_id == "call-reviewer-0"


def test_native_child_has_no_openclaw_session_or_event_rows(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.openclaw_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM session_nodes").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM trajectory_runtime_events WHERE event_json LIKE ?",
            (f'%{stores.child_thread_id}"%',),
        ).fetchone() == (0,)
    assert _read(stores).child_thread_id == stores.child_thread_id


def test_child_rollout_limit_is_raised_and_still_rejects_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert host_records.MAX_CHILD_ROLLOUT_BYTES == 256 * 1024 * 1024
    assert host_records.MAX_PARENT_ROLLOUT_BYTES == 512 * 1024 * 1024
    stores = _stores(tmp_path)
    size = stores.child_rollout.stat().st_size
    monkeypatch.setattr(host_records, "MAX_CHILD_ROLLOUT_BYTES", size)
    assert _read(stores).child_thread_id == stores.child_thread_id
    monkeypatch.setattr(host_records, "MAX_CHILD_ROLLOUT_BYTES", size - 1)
    with pytest.raises(HostRecordError, match="exceeds the bounded size limit"):
        _read(stores)


def test_implementer_and_later_unrelated_spawns_do_not_block_correlation(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    text = stores.owner_rollout.read_text()
    assert text.count('"spawn_agent"') == 3  # implementer, reviewer, later implementer
    assert '"agent_type\\":\\"implementer' in text
    assert _read(stores).child_thread_id == stores.child_thread_id


def test_unrelated_malformed_spawn_arguments_are_skipped(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    broken = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "namespace": "collaboration",
            "arguments": "{not json",
            "call_id": "call-broken",
        },
    }
    with stores.owner_rollout.open("a") as stream:
        stream.write(json.dumps(broken) + "\n")
    assert _read(stores).child_thread_id == stores.child_thread_id


def test_matching_spawn_call_with_malformed_arguments_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    broken = {
        "timestamp": "2027-01-15T08:00:00.500Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "namespace": "collaboration",
            "arguments": f"{{task_name {TASK_NAME}",
            "call_id": "call-broken",
        },
    }
    with stores.owner_rollout.open("a") as stream:
        stream.write(json.dumps(broken) + "\n")
    with pytest.raises(HostRecordError, match="arguments are malformed"):
        _read(stores)


def test_two_reviewer_spawns_with_the_reserved_task_name_reject(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    write_owner_rollout(
        stores.owner_rollout,
        thread_id=OWNER_THREAD_ID,
        task_name=TASK_NAME,
        spawn_ms=RESERVED_AT + 1,
        reviewer_spawns=2,
    )
    with pytest.raises(HostRecordError, match="multiple calls for the reserved task name"):
        _read(stores)


def test_reserved_spawn_with_wrong_role_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    write_owner_rollout(
        stores.owner_rollout,
        thread_id=OWNER_THREAD_ID,
        task_name=TASK_NAME,
        spawn_ms=RESERVED_AT + 1,
        reviewer_agent_type="implementer",
    )
    with pytest.raises(HostRecordError, match="role or fork_turns"):
        _read(stores)


@pytest.mark.parametrize("model,effort", [("gpt-5.4", "xhigh"), ("gpt-5.6-sol", "high")])
def test_native_correlation_rejects_wrong_model_or_effort(
    tmp_path: Path, model: str, effort: str
) -> None:
    stores = _stores(tmp_path, model=model, effort=effort)
    with pytest.raises(HostRecordError, match="model or effort"):
        _read(stores)


def test_native_correlation_rejects_nonterminal_child_as_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="running")
    assert _read(stores).terminal_state == "pending"
    assert _read(stores, callback=False).terminal_state == "pending"


def test_null_name_is_accepted_and_matching_name_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "null").mkdir()
    (tmp_path / "named").mkdir()
    null_name = _stores(tmp_path / "null")
    with sqlite3.connect(null_name.codex_state_database) as connection:
        assert connection.execute(
            "SELECT name FROM threads WHERE id=?", (null_name.child_thread_id,)
        ).fetchone() == (None,)
    assert _read(null_name).child_thread_id == null_name.child_thread_id
    named = _stores(tmp_path / "named", child_name=RUNTIME_TASK_NAME)
    assert _read(named).child_thread_id == named.child_thread_id


def test_non_null_wrong_name_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path, child_name="/root/other_review")
    with pytest.raises(HostRecordError, match="child thread correlation"):
        _read(stores)


def test_native_correlation_rejects_thread_agent_path_mismatch(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    _update_source(
        stores.codex_state_database,
        stores.child_thread_id,
        lambda source: source["subagent"]["thread_spawn"].__setitem__(
            "agent_path", "/root/other-review"
        ),
    )
    with pytest.raises(HostRecordError, match="child thread correlation"):
        _read(stores)


def test_native_correlation_rejects_thread_row_agent_path_mismatch(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.codex_state_database) as connection:
        connection.execute(
            "UPDATE threads SET agent_path=? WHERE id=?",
            ("/root/other-review", stores.child_thread_id),
        )
    with pytest.raises(HostRecordError, match="child thread correlation"):
        _read(stores)


def test_native_correlation_rejects_thread_spawn_role_mismatch(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    _update_source(
        stores.codex_state_database,
        stores.child_thread_id,
        lambda source: source["subagent"]["thread_spawn"].__setitem__("agent_role", "implementer"),
    )
    with pytest.raises(HostRecordError, match="child thread correlation"):
        _read(stores)


def test_native_correlation_rejects_child_created_before_reservation(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.codex_state_database) as connection:
        connection.execute(
            "UPDATE threads SET created_at_ms=? WHERE id=?",
            (RESERVED_AT - 1, stores.child_thread_id),
        )
    with pytest.raises(HostRecordError, match="created before reservation"):
        _read(stores)


def test_rollout_paths_must_be_official_paths_under_codex_sessions(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    outside = tmp_path / "child-copy.jsonl"
    outside.write_bytes(stores.child_rollout.read_bytes())
    with sqlite3.connect(stores.codex_state_database) as connection:
        connection.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", (str(outside), stores.child_thread_id)
        )
    with pytest.raises(HostRecordError, match="outside the managed Codex sessions"):
        _read(stores)


def test_rollout_path_symlink_component_is_rejected(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    real = stores.child_rollout.with_name("real.jsonl")
    stores.child_rollout.rename(real)
    stores.child_rollout.symlink_to(real)
    with pytest.raises(HostRecordError, match="regular non-symlink"):
        _read(stores)


def test_native_boundary_open_owner_interval_is_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path, owner_ended_event=False)
    with pytest.raises(HostRecordPending, match="interval is still open"):
        _read(stores)
    owner = read_exact_native_owner(
        stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
    )
    assert owner.ended_at_ms is None


def test_open_owner_interval_does_not_fall_back_to_next_run_start(tmp_path: Path) -> None:
    stores = _stores(tmp_path, owner_ended_event=False)
    insert_owner_event(
        stores.openclaw_database,
        run_id="next-owner-run",
        event={
            "type": "session.started",
            "provider": "openai",
            "modelId": "gpt-6-astra",
            "data": {"threadId": OWNER_THREAD_ID},
        },
        created_at_ms=RESERVED_AT + 1_000,
    )
    owner = read_exact_native_owner(
        stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
    )
    assert owner.ended_at_ms is None


def test_unflushed_owner_run_is_typed_not_recorded_and_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path, owner_flushed=False)
    with pytest.raises(HostRecordNotRecorded) as excinfo:
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
        )
    assert isinstance(excinfo.value, HostRecordPending)
    with pytest.raises(HostRecordNotRecorded):
        _read(stores)
    # An unrelated run id is also simply not recorded.
    with pytest.raises(HostRecordNotRecorded):
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id="never-ran"
        )


def test_owner_run_with_an_end_but_no_start_is_malformed_not_unrecorded(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.openclaw_database) as connection:
        connection.execute(
            "DELETE FROM trajectory_runtime_events WHERE run_id=? "
            "AND json_extract(event_json,'$.type')='session.started'",
            (OWNER_RUN_ID,),
        )
    with pytest.raises(HostRecordError, match="correlation is unresolved") as excinfo:
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
        )
    assert not isinstance(excinfo.value, HostRecordPending)


def test_owner_run_recorded_only_under_another_session_is_not_unrecorded(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path, owner_flushed=False)
    insert_owner_event(
        stores.openclaw_database,
        run_id=OWNER_RUN_ID,
        event={
            "type": "session.started",
            "provider": "openai",
            "modelId": "gpt-6-astra",
            "data": {"threadId": OWNER_THREAD_ID},
        },
        created_at_ms=OWNER_STARTED_AT,
        session_id="foreign-session",
        session_key="agent:foreign:session",
    )
    with pytest.raises(HostRecordError, match="correlation is unresolved") as excinfo:
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
        )
    assert not isinstance(excinfo.value, HostRecordPending)


def test_spawn_at_or_after_owner_end_is_rejected(tmp_path: Path) -> None:
    # The owner ended at RESERVED_AT, before the spawn call at RESERVED_AT + 1:
    # a call recorded after the bound run ended belongs to a later turn.
    stores = _stores(tmp_path, owner_ended_at_ms=RESERVED_AT)
    with pytest.raises(HostRecordError, match="spawn call correlation"):
        _read(stores)


def test_native_correlation_rejects_reservation_before_owner_start(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with pytest.raises(HostRecordError, match="spawn call correlation"):
        _read(stores, OWNER_STARTED_AT - 1)


def test_native_correlation_rejects_spawn_before_reservation(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    # The spawn call is recorded at RESERVED_AT + 1 ms.
    with pytest.raises(HostRecordError, match="spawn call correlation"):
        _read(stores, RESERVED_AT + 2)


def test_callback_on_rotated_owner_thread_correlates(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.openclaw_database) as connection:
        rows = connection.execute(
            "SELECT json_extract(event_json,'$.data.threadId') FROM trajectory_runtime_events "
            "WHERE run_id LIKE 'announce:%'"
        ).fetchall()
    assert {row[0] for row in rows} == {ROTATED_OWNER_THREAD_ID}
    assert ROTATED_OWNER_THREAD_ID != OWNER_THREAD_ID
    assert _read(stores).announce_status == "succeeded"


def test_missing_callback_is_acceptable_and_the_rollout_is_authoritative(
    tmp_path: Path,
) -> None:
    # In-turn collection: the callback run cannot see its own events yet.
    stores = _stores(tmp_path, announce=None)
    child = _read(stores)
    assert child.terminal_state == "succeeded"
    assert child.announce_run_id is None
    assert child.announce_status is None
    assert child.last_agent_message == VERDICT
    assert _read(stores, callback=False).terminal_state == "succeeded"


def test_missing_callback_with_a_running_child_is_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="running", announce=None)
    assert _read(stores).terminal_state == "pending"


def test_missing_callback_with_a_failed_rollout_is_failed(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="failed", announce=None)
    child = _read(stores)
    assert child.terminal_state == "failed"
    assert child.last_agent_message is None


def test_failed_callback_maps_to_failed_child_without_verdict(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="failed")
    child = _read(stores)
    assert child.terminal_state == "failed"
    assert child.announce_status == "failed"
    assert child.last_agent_message is None


def test_succeeded_callback_with_pending_rollout_stays_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="running", announce="succeeded")
    assert _read(stores).terminal_state == "pending"


@pytest.mark.parametrize(
    "status,announce",
    [
        ("failed", "succeeded"),  # a success callback never overrides an errored rollout
        ("succeeded", "failed"),  # a failure callback never contradicts a success
    ],
)
def test_inconsistent_callback_status_rejects(tmp_path: Path, status: str, announce: str) -> None:
    stores = _stores(tmp_path, status=status, announce=announce)
    with pytest.raises(HostRecordError, match="contradicts the child rollout"):
        _read(stores)
    # Without corroboration the rollout alone decides.
    assert _read(stores, callback=False).terminal_state == status


def test_succeeded_callback_cannot_override_an_aborted_rollout(tmp_path: Path) -> None:
    stores = _stores(tmp_path, announce="succeeded")
    write_child_rollout(
        stores.child_rollout,
        thread_id=stores.child_thread_id,
        parent_thread_id=OWNER_THREAD_ID,
        task_name=TASK_NAME,
        terminal="turn_aborted",
    )
    with pytest.raises(HostRecordError, match="contradicts the child rollout"):
        _read(stores)


def test_failed_callback_without_a_terminal_marker_is_terminal_failure(tmp_path: Path) -> None:
    # The child died without writing a marker; complete lines only.
    stores = _stores(tmp_path, status="running", announce="failed")
    child = _read(stores)
    assert child.terminal_state == "failed"
    assert child.announce_status == "failed"
    assert child.last_agent_message is None
    # Without corroboration the rollout alone stays pending.
    assert _read(stores, callback=False).terminal_state == "pending"


def test_failed_callback_with_an_unterminated_final_line_stays_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="running", announce="failed")
    with stores.child_rollout.open("a") as stream:
        stream.write('{"type":"event_msg","payload":{"type":"token_cou')
    assert _read(stores).terminal_state == "pending"


def test_failed_callback_without_a_marker_still_needs_exactly_one_callback(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path, status="running", announce="failed")
    add_announce(
        stores.openclaw_database,
        owner_thread_id=OWNER_THREAD_ID,
        child_thread_id=stores.child_thread_id,
        status="succeeded",
        at_ms=RESERVED_AT + 120_000,
    )
    with pytest.raises(HostRecordError, match="callback is ambiguous"):
        _read(stores)


def test_succeeded_callback_never_completes_a_rollout_without_a_marker(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="running", announce="succeeded")
    child = _read(stores)
    assert child.terminal_state == "pending"
    assert child.last_agent_message is None


def _writer(stores: NativeReviewStores) -> host_records.NativeOwnerWriter:
    return host_records.read_native_owner_writer(stores.openclaw_database, OWNER_SESSION)


def test_owner_writer_reads_running_status_and_active_writer(tmp_path: Path) -> None:
    stores = _stores(tmp_path, owner_flushed=False)
    writer = _writer(stores)
    assert writer.status == "running"
    assert writer.active_writer_run_id == OWNER_RUN_ID
    assert writer.session_id == OWNER_SESSION_ID
    # After the run ends the host keeps the writer id and flips the status.
    set_owner_node(stores.openclaw_database, status="done")
    done = _writer(stores)
    assert done.status == "done" and done.active_writer_run_id == OWNER_RUN_ID


def test_owner_writer_allows_a_null_active_writer(tmp_path: Path) -> None:
    stores = _stores(tmp_path, owner_flushed=False)
    set_owner_node(stores.openclaw_database, active_writer_run_id=None)
    assert _writer(stores).active_writer_run_id is None


@pytest.mark.parametrize(
    "entry_json",
    [
        None,
        "not json",
        "[]",
        '{"activeWriterRunId":"owner-run-1"}',
        '{"status":"","activeWriterRunId":"owner-run-1"}',
        '{"status":7,"activeWriterRunId":"owner-run-1"}',
        '{"status":"running","activeWriterRunId":7}',
        '{"status":"running","activeWriterRunId":""}',
        '{"status":"running","status":"done"}',
        '{"status":"running","sessionId":"another-session"}',
    ],
)
def test_owner_writer_rejects_missing_or_malformed_entry(
    tmp_path: Path, entry_json: str | None
) -> None:
    stores = _stores(tmp_path, owner_flushed=False)
    with sqlite3.connect(stores.openclaw_database) as connection:
        connection.execute("UPDATE session_nodes SET entry_json=?", (entry_json,))
    with pytest.raises(HostRecordError):
        _writer(stores)


def test_owner_writer_rejects_missing_invalid_node_and_schema(tmp_path: Path) -> None:
    stores = _stores(tmp_path, owner_flushed=False)
    with pytest.raises(HostRecordError, match="missing or ambiguous"):
        host_records.read_native_owner_writer(stores.openclaw_database, "agent:other:session")
    with sqlite3.connect(stores.openclaw_database) as connection:
        connection.execute("UPDATE session_nodes SET entry_valid=0")
    with pytest.raises(HostRecordError, match="not valid"):
        _writer(stores)
    bare = tmp_path / "bare.sqlite"
    with sqlite3.connect(bare) as connection:
        connection.execute("CREATE TABLE other (k TEXT)")
    with pytest.raises(HostRecordError, match="no session node schema"):
        host_records.read_native_owner_writer(bare, OWNER_SESSION)


def test_callback_for_another_parent_thread_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path, announce=None)
    add_announce(
        stores.openclaw_database,
        owner_thread_id="some-other-owner-thread",
        child_thread_id=stores.child_thread_id,
        status="succeeded",
        at_ms=RESERVED_AT + 120_000,
    )
    with pytest.raises(HostRecordError, match="parent thread does not match owner"):
        _read(stores)


def test_callback_for_another_child_is_ignored(tmp_path: Path) -> None:
    stores = _stores(tmp_path, announce=None)
    add_announce(
        stores.openclaw_database,
        owner_thread_id=OWNER_THREAD_ID,
        child_thread_id="some-other-child",
        status="failed",
        at_ms=RESERVED_AT + 120_000,
    )
    child = _read(stores)
    assert child.terminal_state == "succeeded"
    assert child.announce_run_id is None


def test_callback_under_another_parent_rejects_even_beside_a_good_callback(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    add_announce(
        stores.openclaw_database,
        owner_thread_id="some-other-owner-thread",
        child_thread_id=stores.child_thread_id,
        status="succeeded",
        at_ms=RESERVED_AT + 120_000,
    )
    with pytest.raises(HostRecordError, match="parent thread does not match owner"):
        _read(stores)


def test_callback_in_another_session_is_ignored(tmp_path: Path) -> None:
    stores = _stores(tmp_path, announce=None)
    add_announce(
        stores.openclaw_database,
        owner_thread_id=OWNER_THREAD_ID,
        child_thread_id=stores.child_thread_id,
        status="failed",
        at_ms=RESERVED_AT + 120_000,
        owner_session_key="agent:other:session",
    )
    assert _read(stores).terminal_state == "succeeded"


def test_duplicate_callbacks_for_one_child_reject(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    add_announce(
        stores.openclaw_database,
        owner_thread_id=OWNER_THREAD_ID,
        child_thread_id=stores.child_thread_id,
        status="failed",
        at_ms=RESERVED_AT + 120_000,
    )
    with pytest.raises(HostRecordError, match="callback is ambiguous"):
        _read(stores)


def test_duplicate_callback_start_event_in_one_run_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    assert stores.announce_run_id is not None
    insert_owner_event(
        stores.openclaw_database,
        run_id=stores.announce_run_id,
        event={
            "type": "session.started",
            "provider": "openai",
            "modelId": "gpt-6-astra",
            "data": {"threadId": ROTATED_OWNER_THREAD_ID},
        },
        created_at_ms=RESERVED_AT + 111_000,
    )
    with pytest.raises(HostRecordError, match="callback is ambiguous"):
        _read(stores)


def test_unrecognized_callback_status_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path, announce="killed")
    with pytest.raises(HostRecordError, match="status is not recognized"):
        _read(stores)


def test_unrelated_requester_announce_runs_are_ignored(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    insert_owner_event(
        stores.openclaw_database,
        run_id="announce:requester-set-1:yield-1",
        event={
            "type": "session.started",
            "provider": "openai",
            "modelId": "gpt-6-astra",
            "data": {"threadId": OWNER_THREAD_ID},
        },
        created_at_ms=RESERVED_AT + 130_000,
    )
    assert _read(stores).announce_status == "succeeded"


def test_bound_run_end_with_different_thread_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    _mutate_event(
        stores.openclaw_database,
        OWNER_RUN_ID,
        "session.ended",
        lambda event: event["data"].__setitem__("threadId", "event-thread-mismatch"),
    )
    with pytest.raises(HostRecordError, match="owner end event thread does not match owner"):
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
        )


def test_end_row_with_a_different_run_id_is_not_the_bound_runs_end(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.openclaw_database) as connection:
        connection.execute(
            "UPDATE trajectory_runtime_events SET run_id=? "
            "WHERE json_extract(event_json,'$.type')='session.ended' AND run_id=?",
            ("row-run-mismatch", OWNER_RUN_ID),
        )
    # The mismatching row no longer belongs to the bound run's own events, so
    # the interval is open rather than silently ended.
    owner = read_exact_native_owner(
        stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
    )
    assert owner.ended_at_ms is None


def test_native_boundary_rejects_event_session_mismatch(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    _mutate_event(
        stores.openclaw_database,
        OWNER_RUN_ID,
        "session.ended",
        lambda event: event.__setitem__("sessionId", "event-session-mismatch"),
    )
    with pytest.raises(HostRecordError, match="boundary event identity does not match its row"):
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
        )


def test_native_boundary_rejects_duplicate_end_events_for_the_bound_run(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    insert_owner_event(
        stores.openclaw_database,
        run_id=OWNER_RUN_ID,
        event={
            "type": "session.ended",
            "data": {"threadId": OWNER_THREAD_ID, "status": "success"},
        },
        created_at_ms=RESERVED_AT + 2_000,
    )
    with pytest.raises(HostRecordError, match="ambiguous"):
        read_exact_native_owner(
            stores.openclaw_database, OWNER_SESSION, RESERVED_AT, expected_run_id=OWNER_RUN_ID
        )


def test_old_owner_run_on_another_model_does_not_block_the_bound_run(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores.openclaw_database) as connection:
        assert connection.execute(
            "SELECT json_extract(event_json,'$.modelId') FROM trajectory_runtime_events "
            "WHERE run_id=? AND json_extract(event_json,'$.type')='session.started'",
            (OLD_OWNER_RUN_ID,),
        ).fetchone() == ("gpt-5.4",)
    owner = read_exact_native_owner(
        stores.openclaw_database,
        OWNER_SESSION,
        RESERVED_AT,
        expected_run_id=OWNER_RUN_ID,
        expected_thread_id=OWNER_THREAD_ID,
    )
    assert owner.model == "gpt-6-astra"
    assert owner.session_id == OWNER_SESSION_ID
    assert _read(stores).child_thread_id == stores.child_thread_id


def test_bound_owner_run_on_wrong_model_rejects(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    _mutate_event(
        stores.openclaw_database,
        OWNER_RUN_ID,
        "session.started",
        lambda event: event.__setitem__("modelId", "gpt-5.4"),
    )
    with pytest.raises(HostRecordError, match="not the observed OpenAI Astra route"):
        _read(stores)


def test_owner_rollout_above_eight_mebibytes_correlates(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    write_owner_rollout(
        stores.owner_rollout,
        thread_id=OWNER_THREAD_ID,
        task_name=TASK_NAME,
        spawn_ms=RESERVED_AT + 1,
        padding_bytes=9 * 1024 * 1024,
    )
    assert stores.owner_rollout.stat().st_size > 8 * 1024 * 1024
    assert _read(stores).child_thread_id == stores.child_thread_id


def test_unterminated_last_parent_line_is_ignored(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    # A second reserved spawn that is still being appended must not count.
    partial = json.dumps(
        {
            "timestamp": "2027-01-15T08:00:00.600Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "namespace": "collaboration",
                "arguments": json.dumps({"task_name": TASK_NAME}),
                "call_id": "call-partial",
            },
        }
    )
    with stores.owner_rollout.open("a") as stream:
        stream.write(partial)
    assert not stores.owner_rollout.read_bytes().endswith(b"\n")
    assert _read(stores).child_thread_id == stores.child_thread_id


def test_unterminated_last_child_line_is_pending(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with stores.child_rollout.open("a") as stream:
        stream.write('{"type":"event_msg","payload":{"type":"token_cou')
    child = _read(stores)
    assert child.terminal_state == "pending"
    assert child.last_agent_message is None


def test_child_rollout_digest_stops_at_the_terminal_marker(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    before = _read(stores).rollout_sha256
    with stores.child_rollout.open("a") as stream:
        stream.write('{"type":"event_msg","payload":{"type":"task_started"}}\n')
    assert _read(stores).rollout_sha256 == before


def test_task_complete_with_error_is_failed(tmp_path: Path) -> None:
    stores = _stores(tmp_path, status="failed", announce="failed")
    assert _read(stores, callback=False).terminal_state == "failed"


def test_turn_aborted_is_cancelled(tmp_path: Path) -> None:
    stores = _stores(tmp_path, announce="failed")
    write_child_rollout(
        stores.child_rollout,
        thread_id=stores.child_thread_id,
        parent_thread_id=OWNER_THREAD_ID,
        task_name=TASK_NAME,
        terminal="turn_aborted",
    )
    assert _read(stores, callback=False).terminal_state == "cancelled"
    # With a failed callback the aborted turn stays distinguishable from a failure.
    assert _read(stores).terminal_state == "cancelled"


def test_two_terminal_markers_reject(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    write_child_rollout(
        stores.child_rollout,
        thread_id=stores.child_thread_id,
        parent_thread_id=OWNER_THREAD_ID,
        task_name=TASK_NAME,
        last_agent_message=VERDICT,
        extra_terminals=1,
    )
    with pytest.raises(HostRecordError, match="multiple terminal markers"):
        _read(stores)


def test_native_cancel_uses_exact_owner_chat_abort_shape() -> None:
    seen: dict[str, object] = {}

    async def request_once(
        method: str, params: dict[str, str], *, timeout_seconds: int
    ) -> dict[str, object]:
        seen.update(method=method, params=params, timeout_seconds=timeout_seconds)
        return {"ok": True, "aborted": True, "runIds": [OWNER_RUN_ID]}

    response = asyncio.run(
        request_cancel(
            request_once,
            OWNER_SESSION,
            "research-orchestrator",
            OWNER_RUN_ID,
        )
    )
    assert response["runIds"] == [OWNER_RUN_ID]
    assert seen == {
        "method": "chat.abort",
        "params": {
            "sessionKey": OWNER_SESSION,
            "agentId": "research-orchestrator",
            "runId": OWNER_RUN_ID,
        },
        "timeout_seconds": 30,
    }


@pytest.mark.parametrize(
    "response,expected",
    [
        (
            {"ok": True, "aborted": False, "runIds": []},
            "owner_run_inactive_no_supported_child_cancel",
        ),
        ({"ok": True, "aborted": True, "runIds": [OWNER_RUN_ID]}, "owner_run_aborted"),
        ({"ok": True, "aborted": True, "runIds": ["other-run"]}, "MISMATCHED_RESPONSE"),
        ({"ok": True, "aborted": True, "runIds": []}, "MALFORMED_RESPONSE"),
        ({"ok": True, "aborted": False, "runIds": [OWNER_RUN_ID]}, "MALFORMED_RESPONSE"),
        ({"ok": False, "aborted": False, "runIds": []}, "MALFORMED_RESPONSE"),
        ({"ok": True, "aborted": "no", "runIds": []}, "MALFORMED_RESPONSE"),
        ({"ok": True, "aborted": False, "runIds": [1]}, "MALFORMED_RESPONSE"),
        ({"ok": True, "aborted": False}, "MALFORMED_RESPONSE"),
        ({"ok": True, "aborted": False, "runIds": [], "extra": 1}, "MALFORMED_RESPONSE"),
        ({"status": "accepted", "runId": OWNER_RUN_ID}, "MALFORMED_RESPONSE"),
    ],
)
def test_chat_abort_response_shapes(response: dict[str, object], expected: str) -> None:
    assert review_evidence._safe_rpc_result(response, OWNER_RUN_ID) == expected


def test_native_ack_rejects_unknown_identity_field() -> None:
    with pytest.raises(StoreConflict, match="review ACK payload is malformed"):
        _ack_from_payload(
            {
                "mode": "run",
                "run_timeout_seconds": None,
                "acked_at": "2026-10-02T00:00:00Z",
                "owner_session_key": OWNER_SESSION,
                "owner_run_id": OWNER_RUN_ID,
                "owner_thread_id": OWNER_THREAD_ID,
                "child_thread_id": "child",
                "forged": "unknown",
            }
        )


def test_historical_acp_ack_payload_remains_readable() -> None:
    ack = _ack_from_payload(
        {
            "child_session_key": "agent:research-orchestrator:reviewer:old",
            "run_id": "old-run",
            "mode": "run",
            "run_timeout_seconds": None,
            "acked_at": "2026-10-02T00:00:00Z",
        }
    )
    assert ack.child_session_key == "agent:research-orchestrator:reviewer:old"
    assert ack.run_id == "old-run"
    assert ack.child_thread_id is None


def test_native_ack_has_no_invented_session_or_run_identity() -> None:
    payload = {
        "mode": "run",
        "run_timeout_seconds": None,
        "acked_at": "2026-10-02T00:00:00Z",
        "owner_session_key": OWNER_SESSION,
        "owner_run_id": OWNER_RUN_ID,
        "owner_thread_id": OWNER_THREAD_ID,
        "child_thread_id": "child",
    }
    ack = _ack_from_payload(payload)
    assert ack.child_session_key is None and ack.run_id is None
    assert json.loads(ack.to_json()) == payload


def _managed_layout(root: Path) -> tuple[Path, Path, Path]:
    """Create the minimal managed-root layout ``managed_native_databases`` validates."""

    core = root / "state" / "openclaw.sqlite"
    agent = root / "agents" / "research-orchestrator" / "agent"
    openclaw = agent / "openclaw-agent.sqlite"
    codex = agent / "codex-home" / "state_5.sqlite"
    schemas = {
        core: "CREATE TABLE task_runs (id TEXT)",
        openclaw: (
            "CREATE TABLE session_nodes (k TEXT); CREATE TABLE trajectory_runtime_events (k TEXT)"
        ),
        codex: "CREATE TABLE threads (id TEXT); CREATE TABLE thread_spawn_edges (id TEXT)",
    }
    for path, schema in schemas.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript(schema)
    return core, openclaw, codex


def test_cli_native_paths_derive_from_the_root_with_no_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    _core, openclaw, codex = _managed_layout(managed)
    root = managed / "research-v2"
    root.mkdir()
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    monkeypatch.delenv("G2_OWNER_ENV_FILE", raising=False)
    research_cli._require_managed_native_paths(root, openclaw, codex)
    research_cli._require_managed_native_paths(root, openclaw)


def test_cli_native_paths_reject_a_mismatched_supplied_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    _core, openclaw, codex = _managed_layout(managed)
    root = managed / "research-v2"
    root.mkdir()
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    other = tmp_path / "other.sqlite"
    other.write_bytes(openclaw.read_bytes())
    with pytest.raises(ValueError, match="--openclaw-database must equal the managed"):
        research_cli._require_managed_native_paths(root, other, codex)
    with pytest.raises(ValueError, match="--codex-state-database must equal the managed"):
        research_cli._require_managed_native_paths(root, openclaw, other)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(openclaw)
    with pytest.raises(ValueError, match="--openclaw-database must equal the managed"):
        research_cli._require_managed_native_paths(root, alias, codex)


def test_cli_native_paths_reject_an_environment_core_database_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    core, openclaw, codex = _managed_layout(managed)
    root = managed / "research-v2"
    root.mkdir()
    # Consistency cross-check only: a matching value passes, another core rejects.
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(core))
    research_cli._require_managed_native_paths(root, openclaw, codex)
    other_core, _other_openclaw, _other_codex = _managed_layout(tmp_path / "elsewhere")
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(other_core))
    with pytest.raises(ValueError, match="RESEARCH_CORE_DATABASE does not match"):
        research_cli._require_managed_native_paths(root, openclaw, codex)


def test_cli_native_paths_reject_a_root_without_a_managed_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _core, openclaw, codex = _managed_layout(tmp_path / "managed")
    lonely = tmp_path / "lonely" / "research-v2"
    lonely.mkdir(parents=True)
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    with pytest.raises(ValueError, match="managed native databases are unavailable"):
        research_cli._require_managed_native_paths(lonely, openclaw, codex)


@pytest.mark.parametrize(
    "command,extra",
    [
        ("review-reserve", ["--bundle-dir", "/nonexistent/bundle", "--owner-key", "k"]),
        ("review-reconcile", []),
        ("review-collect", []),
        ("review-cancel", ["--reason", "stop"]),
    ],
)
def test_cli_review_commands_reject_a_mismatched_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, extra: list[str]
) -> None:
    managed = tmp_path / "managed"
    _core, openclaw, codex = _managed_layout(managed)
    root = managed / "research-v2"
    root.mkdir()
    monkeypatch.delenv("RESEARCH_CORE_DATABASE", raising=False)
    other = tmp_path / "other.sqlite"
    other.write_bytes(openclaw.read_bytes())
    args = ["research", command, "H0001-A001", "--root", str(root), *extra]
    args += ["--openclaw-database", str(other)]
    if command != "review-reserve":
        args += ["--codex-state-database", str(codex)]
    else:
        args += ["--wake-key", "wake"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "must equal the managed native database" in result.output
