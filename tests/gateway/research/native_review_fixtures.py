"""Read-only official-store fixtures for native review evidence tests.

The default fixture models the record shapes the installed OpenClaw 2026.9.2 /
Codex 0.153.4 host really writes (confirmed read-only against the live stores):

* the owner rollout holds an earlier implementer spawn, the reviewer spawn and a
  later unrelated spawn; every spawn ``message`` is ciphertext;
* the child ``threads`` row has ``name`` NULL and no OpenClaw session row or
  session event exists for the child;
* the owner thread rotates: the completion callback is an owner-session run
  ``announce:codex-native:<spawning thread>:<child>:<status>`` that runs on a
  different thread, and the owner run itself ends with ``yieldDetected``;
* an earlier owner run on another model exists in the same session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gateway.research.review_evidence import ReviewReservation, reconcile_review, reserve_review
from gateway.research.store import ResearchStore, canonical_wake_key

from tests.gateway.research.conftest import provenance_evidence, run_plan

OWNER_SESSION = "agent:research-orchestrator:autoresearch:test"
OWNER_RUN_ID = "owner-run-1"
OWNER_THREAD_ID = "owner-thread-1"
OWNER_SESSION_ID = "owner-session-1"
ROTATED_OWNER_THREAD_ID = "owner-thread-rotated"
OLD_OWNER_RUN_ID = "owner-run-0"
OLD_OWNER_THREAD_ID = "owner-thread-0"
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_EFFORT = "xhigh"
SESSIONS_RELATIVE = Path("sessions") / "2026" / "10" / "02"


@dataclass(frozen=True)
class NativeReviewStores:
    openclaw_database: Path
    codex_state_database: Path
    child_thread_id: str
    owner_rollout: Path
    child_rollout: Path
    announce_run_id: str | None

    @property
    def codex_home(self) -> Path:
        return self.codex_state_database.parent


def _line(event: dict[str, object]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _epoch_ms(timestamp: str) -> int:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def ciphertext(seed: str) -> str:
    """An encrypted-looking spawn message like the installed host writes."""

    digest = hashlib.sha256(seed.encode()).digest() * 3
    return "gAAAAAB" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def insert_owner_event(
    path: Path,
    *,
    run_id: str,
    event: dict[str, object],
    created_at_ms: int,
    session_id: str = OWNER_SESSION_ID,
    session_key: str = OWNER_SESSION,
) -> None:
    """Append one runtime event to the owner session's trajectory."""

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM trajectory_runtime_events WHERE session_id=?",
            (session_id,),
        ).fetchone()
        body = {"sessionKey": session_key, "sessionId": session_id, "runId": run_id, **event}
        connection.execute(
            "INSERT INTO trajectory_runtime_events VALUES(?,?,?,?,?)",
            (session_id, int(row[0]), run_id, _line(body), created_at_ms),
        )


def owner_started(thread_id: str, *, model: str = "gpt-6-astra") -> dict[str, object]:
    return {
        "type": "session.started",
        "provider": "openai",
        "modelId": model,
        "data": {"threadId": thread_id, "toolCount": 24},
    }


def owner_ended(thread_id: str) -> dict[str, object]:
    return {
        "type": "session.ended",
        "provider": "openai",
        "modelId": "gpt-6-astra",
        "data": {
            "status": "success",
            "threadId": thread_id,
            "turnId": "turn-1",
            "timedOut": False,
            "yieldDetected": True,
            "promptError": None,
        },
    }


def create_owner_database(
    path: Path,
    *,
    owner_session_key: str = OWNER_SESSION,
    owner_run_id: str = OWNER_RUN_ID,
    owner_thread_id: str = OWNER_THREAD_ID,
    started_at_ms: int = 1_700_000_000_000,
    ended_at_ms: int | None = None,
    owner_ended_event: bool = True,
) -> Path:
    """Create only the official owner session/runtime-event schema.

    The session also holds an older owner run on another model and thread, as
    the installed owner session does.
    """

    if path.exists():
        return path
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session_nodes (
              session_key TEXT PRIMARY KEY, current_session_id TEXT NOT NULL,
              parent_session_key TEXT, spawned_by TEXT, label TEXT, created_at INTEGER,
              entry_valid INTEGER NOT NULL
            );
            CREATE TABLE trajectory_runtime_events (
              session_id TEXT NOT NULL, seq INTEGER NOT NULL, run_id TEXT,
              event_json TEXT NOT NULL, created_at INTEGER NOT NULL,
              PRIMARY KEY(session_id, seq)
            );
            """
        )
        connection.execute(
            "INSERT INTO session_nodes VALUES(?,?,?,?,?,?,?)",
            (owner_session_key, OWNER_SESSION_ID, None, None, None, started_at_ms, 1),
        )
    insert_owner_event(
        path,
        run_id=OLD_OWNER_RUN_ID,
        event=owner_started(OLD_OWNER_THREAD_ID, model="gpt-5.4"),
        created_at_ms=started_at_ms - 500_000,
        session_key=owner_session_key,
    )
    insert_owner_event(
        path,
        run_id=OLD_OWNER_RUN_ID,
        event=owner_ended(OLD_OWNER_THREAD_ID),
        created_at_ms=started_at_ms - 400_000,
        session_key=owner_session_key,
    )
    insert_owner_event(
        path,
        run_id=owner_run_id,
        event=owner_started(owner_thread_id),
        created_at_ms=started_at_ms,
        session_key=owner_session_key,
    )
    if owner_ended_event:
        insert_owner_event(
            path,
            run_id=owner_run_id,
            event=owner_ended(owner_thread_id),
            created_at_ms=ended_at_ms if ended_at_ms is not None else started_at_ms + 100_000,
            session_key=owner_session_key,
        )
    return path


def _setup(
    campaign: tuple[ResearchStore, Path, Any], tmp_path: Path
) -> tuple[ResearchStore, Path, Any, str, Path]:
    """Create one committed implementation and its immutable review bundle target."""

    store, source, hypothesis = campaign
    evidence = tmp_path / "tests.json"
    evidence.write_text('{"pytest":"pass"}\n', encoding="utf-8")
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    from gateway.research.contracts import ImplementationRecord

    record = ImplementationRecord(
        attempt.attempt_id,
        commit,
        ("python", "-m", "target"),
        str(evidence),
        "reported-coder",
        "xhigh",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    store.submit_implementation(
        attempt.attempt_id,
        record,
        run_plan=run_plan(store, attempt, record),
        containment_provenance=provenance_evidence(attempt.attempt_id, record.commit),
    )
    return store, source, hypothesis, attempt.attempt_id, tmp_path / "review-bundle"


def _function_call(
    call_id: str, task_name: str, agent_type: str, timestamp_ms: int
) -> list[dict[str, object]]:
    arguments = json.dumps(
        {
            "task_name": task_name,
            "agent_type": agent_type,
            "fork_turns": "none",
            "message": ciphertext(task_name),
        },
        separators=(",", ":"),
    )
    return [
        {
            "timestamp": _iso(timestamp_ms),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "namespace": "collaboration",
                "arguments": arguments,
                "call_id": call_id,
            },
        },
        {
            "timestamp": _iso(timestamp_ms + 1),
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"task_name": f"/root/{task_name}"}),
            },
        },
    ]


def write_owner_rollout(
    path: Path,
    *,
    thread_id: str,
    task_name: str,
    spawn_ms: int,
    reviewer_spawns: int = 1,
    padding_bytes: int = 0,
    unterminated_tail: str = "",
    reviewer_agent_type: str = "reviewer",
) -> None:
    """Write an owner rollout: implementer spawn, reviewer spawn, unrelated spawn."""

    events: list[dict[str, object]] = [
        {
            "type": "session_meta",
            "payload": {"id": thread_id, "source": "vscode", "originator": "openclaw"},
        },
        {
            "type": "turn_context",
            "payload": {"model": "gpt-6-astra", "effort": "high"},
        },
        *_function_call("call-implementer", "h0006_earlier_implementer", "implementer", 1000),
    ]
    for index in range(reviewer_spawns):
        events.extend(
            _function_call(
                f"call-reviewer-{index}", task_name, reviewer_agent_type, spawn_ms + index * 10
            )
        )
    events.extend(
        _function_call("call-later", "h0007_later_implementer", "implementer", spawn_ms + 50_000)
    )
    if padding_bytes:
        events.append(
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "x" * padding_bytes},
            }
        )
    text = "\n".join(_line(event) for event in events) + "\n" + unterminated_tail
    path.write_text(text, encoding="utf-8")


def write_child_rollout(
    path: Path,
    *,
    thread_id: str,
    parent_thread_id: str,
    task_name: str,
    model: str = REVIEW_MODEL,
    effort: str = REVIEW_EFFORT,
    terminal: str = "task_complete",
    last_agent_message: str | None = None,
    error: dict[str, object] | None = None,
    extra_terminals: int = 0,
    unterminated_tail: str = "",
) -> None:
    """Write a child rollout shaped like an installed native child."""

    spawn: dict[str, object] = {
        "parent_thread_id": parent_thread_id,
        "depth": 1,
        "agent_path": f"/root/{task_name}",
        "agent_nickname": "Reviewer",
        "agent_role": "reviewer",
    }
    events: list[dict[str, object]] = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": parent_thread_id,
                "thread_source": "subagent",
                "agent_role": "reviewer",
                "agent_path": f"/root/{task_name}",
                "source": {"subagent": {"thread_spawn": spawn}},
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "child-turn"}},
        {"type": "turn_context", "payload": {"model": model, "effort": effort}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {"total_tokens": 10},
                    "model_context_window": 258400,
                },
            },
        },
    ]
    if terminal != "none":
        marker: dict[str, object] = {"type": terminal, "turn_id": "child-turn"}
        if terminal == "task_complete":
            marker["last_agent_message"] = last_agent_message
            if error is not None:
                marker["error"] = error
        elif terminal == "turn_aborted":
            marker["reason"] = "interrupted"
        for _ in range(1 + extra_terminals):
            events.append({"type": "event_msg", "payload": dict(marker)})
    path.write_text("\n".join(_line(event) for event in events) + "\n" + unterminated_tail)


def create_codex_database(
    path: Path,
    *,
    owner_thread_id: str,
    owner_rollout: Path,
    child_thread_id: str,
    child_rollout: Path,
    task_name: str,
    created_at_ms: int,
    model: str = REVIEW_MODEL,
    effort: str = REVIEW_EFFORT,
    name: str | None = None,
) -> None:
    """Create the Codex state store: NULL child ``name`` and an ``open`` edge."""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
              id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL,
              model_provider TEXT NOT NULL, thread_source TEXT, agent_role TEXT,
              model TEXT, reasoning_effort TEXT, agent_path TEXT, name TEXT,
              created_at_ms INTEGER
            );
            CREATE TABLE thread_spawn_edges (
              parent_thread_id TEXT NOT NULL, child_thread_id TEXT NOT NULL PRIMARY KEY,
              status TEXT NOT NULL
            );
            """
        )
        source: dict[str, object] = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": owner_thread_id,
                    "depth": 1,
                    "agent_path": f"/root/{task_name}",
                    "agent_nickname": "Reviewer",
                    "agent_role": "reviewer",
                }
            }
        }
        connection.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                owner_thread_id,
                str(owner_rollout),
                "vscode",
                "openai",
                None,
                None,
                "gpt-6-astra",
                "high",
                None,
                None,
                created_at_ms - 100_000,
            ),
        )
        connection.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                child_thread_id,
                str(child_rollout),
                _line(source),
                "openai",
                "subagent",
                "reviewer",
                model,
                effort,
                f"/root/{task_name}",
                name,
                created_at_ms,
            ),
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges VALUES(?,?,?)",
            (owner_thread_id, child_thread_id, "open"),
        )


def add_announce(
    openclaw_database: Path,
    *,
    owner_thread_id: str,
    child_thread_id: str,
    status: str,
    at_ms: int,
    run_thread_id: str = ROTATED_OWNER_THREAD_ID,
    owner_session_key: str = OWNER_SESSION,
) -> str:
    """Record a completion-callback run on a (rotated) owner thread."""

    run_id = f"announce:codex-native:{owner_thread_id}:{child_thread_id}:{status}"
    insert_owner_event(
        openclaw_database,
        run_id=run_id,
        event=owner_started(run_thread_id),
        created_at_ms=at_ms,
        session_key=owner_session_key,
    )
    insert_owner_event(
        openclaw_database,
        run_id=run_id,
        event=owner_ended(run_thread_id),
        created_at_ms=at_ms + 2_000,
        session_key=owner_session_key,
    )
    return run_id


def build_native_host(
    tmp_path: Path,
    *,
    task_name: str,
    reserved_at_ms: int,
    last_agent_message: str | None,
    owner_thread_id: str = OWNER_THREAD_ID,
    status: str = "succeeded",
    announce: str | None = "auto",
    model: str = REVIEW_MODEL,
    effort: str = REVIEW_EFFORT,
    child_name: str | None = None,
    openclaw_database: Path | None = None,
    spawn_offset_ms: int = 1,
) -> NativeReviewStores:
    """Create the owner rollout, child rollout and both stores for one child.

    ``status`` is ``succeeded`` (task_complete), ``running`` (no terminal marker
    and no callback) or ``failed`` (task_complete with a usage-limit error).
    ``announce`` ``auto`` follows ``status``; ``None`` writes no callback.
    """

    sessions = tmp_path / SESSIONS_RELATIVE
    sessions.mkdir(parents=True, exist_ok=True)
    child_thread_id = f"child-thread-{task_name}"
    owner_rollout = sessions / "rollout-owner.jsonl"
    child_rollout = sessions / f"rollout-{child_thread_id}.jsonl"
    write_owner_rollout(
        owner_rollout,
        thread_id=owner_thread_id,
        task_name=task_name,
        spawn_ms=reserved_at_ms + spawn_offset_ms,
    )
    if status == "running":
        write_child_rollout(
            child_rollout,
            thread_id=child_thread_id,
            parent_thread_id=owner_thread_id,
            task_name=task_name,
            model=model,
            effort=effort,
            terminal="none",
        )
    else:
        write_child_rollout(
            child_rollout,
            thread_id=child_thread_id,
            parent_thread_id=owner_thread_id,
            task_name=task_name,
            model=model,
            effort=effort,
            last_agent_message=None if status == "failed" else last_agent_message,
            error={"message": "usage limit", "codex_error_info": "usage_limit_exceeded"}
            if status == "failed"
            else None,
        )
    codex = tmp_path / "state_5.sqlite"
    openclaw = openclaw_database or (tmp_path / "owner.sqlite")
    create_codex_database(
        codex,
        owner_thread_id=owner_thread_id,
        owner_rollout=owner_rollout,
        child_thread_id=child_thread_id,
        child_rollout=child_rollout,
        task_name=task_name,
        created_at_ms=reserved_at_ms + spawn_offset_ms + 20,
        model=model,
        effort=effort,
        name=child_name,
    )
    announce_status = (
        ({"succeeded": "succeeded", "failed": "failed"}.get(status))
        if (announce == "auto")
        else announce
    )
    announce_run_id = None
    if announce_status is not None:
        announce_run_id = add_announce(
            openclaw,
            owner_thread_id=owner_thread_id,
            child_thread_id=child_thread_id,
            status=announce_status,
            at_ms=reserved_at_ms + 110_000,
        )
    return NativeReviewStores(
        openclaw, codex, child_thread_id, owner_rollout, child_rollout, announce_run_id
    )


def create_native_stores(
    store: ResearchStore,
    attempt_id: str,
    bundle: Path,
    tmp_path: Path,
    *,
    status: str = "succeeded",
    verdict: str = "PASS",
    findings: tuple[str, ...] = (),
    model: str = REVIEW_MODEL,
    effort: str = REVIEW_EFFORT,
    owner_thread_id: str = OWNER_THREAD_ID,
    announce: str | None = "auto",
) -> NativeReviewStores:
    """Create exact OpenClaw/Codex rows for one reserved native child."""

    del bundle
    reservation = json.loads(store.evidence(attempt_id, "review_reservation"))
    reservation_object = ReviewReservation(
        **{
            "attempt_id": reservation["attempt_id"],
            "commit": reservation["commit"],
            "hypothesis_spec_sha256": reservation["hypothesis_spec_sha256"],
            "bundle_dir": reservation["bundle_dir"],
            "bundle_sha256": reservation["bundle_sha256"],
            "reserved_at": reservation["reserved_at"],
            "owner_session_key": reservation["owner_session_key"],
            "label": reservation["label"],
            "reservation_nonce": reservation["reservation_nonce"],
            "owner_run_id": reservation.get("owner_run_id"),
            "owner_thread_id": reservation.get("owner_thread_id"),
            "task_name": reservation.get("task_name"),
            "prompt_sha256": reservation.get("prompt_sha256"),
            "spawn_arguments_json": json.dumps(
                reservation["spawn_arguments"], sort_keys=True, separators=(",", ":")
            ),
        }
    )
    task_name = reservation_object.task_name or reservation_object.label
    payload = {
        "verdict": verdict,
        "attempt_id": attempt_id,
        "commit": reservation_object.commit,
        "spec_sha256": reservation_object.hypothesis_spec_sha256,
        "findings": list(findings),
    }
    return build_native_host(
        tmp_path,
        task_name=task_name,
        reserved_at_ms=_epoch_ms(reservation_object.reserved_at),
        last_agent_message=json.dumps(payload),
        owner_thread_id=owner_thread_id,
        status=status,
        announce=announce,
        model=model,
        effort=effort,
    )


def prepare_review(
    campaign: tuple[ResearchStore, Path, Any],
    tmp_path: Path,
    *,
    status: str = "succeeded",
    verdict: str = "PASS",
    findings: tuple[str, ...] = (),
) -> tuple[ResearchStore, str, Path, NativeReviewStores]:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    owner_database = tmp_path / "owner.sqlite"
    owner_started_at_ms = int(datetime.now(tz=UTC).timestamp() * 1000) - 1_000
    create_owner_database(owner_database, started_at_ms=owner_started_at_ms)
    _status, resume_seq = store.campaign()
    wake_key = canonical_wake_key("H0001", attempt_id, "IMPLEMENTED", resume_seq)
    if not store.reserve_wake(wake_key, attempt_id, "IMPLEMENTED", resume_seq):
        raise AssertionError("native fixture wake reservation failed")
    store.complete_wake(wake_key, OWNER_RUN_ID)
    reserve_review(
        store,
        attempt_id,
        bundle,
        OWNER_SESSION,
        wake_pending_key=wake_key,
        openclaw_database=owner_database,
    )
    native = create_native_stores(
        store,
        attempt_id,
        bundle,
        tmp_path,
        status=status,
        verdict=verdict,
        findings=findings,
    )
    reconcile_review(store, attempt_id, native.openclaw_database, native.codex_state_database)
    # The fixture has completed the canonical reservation turn; advance the
    # campaign sequence before tests compose a subsequent owner wake.
    store.resume("native-fixture-next-owner-turn")
    return store, attempt_id, bundle, native
