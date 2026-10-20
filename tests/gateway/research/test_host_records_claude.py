"""Claude-profile host records over fixtures built from the spike's real table schemas."""

# The copied CREATE TABLE statements keep their original long lines.
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from gateway.research.host_records import HostRecordError
from gateway.research.host_records_claude import (
    ClaudeRecordError,
    ClaudeRecordNotRecorded,
    ClaudeRun,
    ClaudeStores,
    child_runs,
    claude_stores,
    read_run,
    run_outcome,
)

# CREATE TABLE statements copied from the OpenClaw 2026.9.2 spike databases
# (`sqlite3 'file:...?mode=ro' .schema <table>`); indexes and triggers omitted.
TASK_RUNS_SCHEMA = """CREATE TABLE task_runs (
  task_id TEXT NOT NULL PRIMARY KEY,
  runtime TEXT NOT NULL,
  task_kind TEXT,
  source_id TEXT,
  requester_session_key TEXT,
  owner_key TEXT NOT NULL,
  scope_kind TEXT NOT NULL,
  child_session_key TEXT,
  parent_flow_id TEXT,
  parent_task_id TEXT,
  agent_id TEXT,
  requester_agent_id TEXT,
  run_id TEXT,
  label TEXT,
  task TEXT NOT NULL,
  status TEXT NOT NULL,
  delivery_status TEXT NOT NULL,
  notify_policy TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  ended_at INTEGER,
  last_event_at INTEGER,
  cleanup_after INTEGER,
  tool_use_count INTEGER,
  last_tool_name TEXT,
  error TEXT,
  progress_summary TEXT,
  terminal_summary TEXT,
  terminal_outcome TEXT,
  detail_json TEXT
) STRICT"""
SUBAGENT_RUNS_SCHEMA = """CREATE TABLE subagent_runs (
  run_id TEXT NOT NULL PRIMARY KEY,
  child_session_key TEXT NOT NULL,
  controller_session_key TEXT,
  requester_session_key TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
) STRICT"""
SESSION_NODES_SCHEMA = """CREATE TABLE session_nodes (
  session_key TEXT NOT NULL PRIMARY KEY,
  current_session_id TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  entry_valid INTEGER NOT NULL DEFAULT 0 CHECK (entry_valid IN (-1, 0, 1)),
  updated_at INTEGER NOT NULL,
  status TEXT CHECK (status IS NULL OR status IN ('running', 'done', 'failed', 'killed', 'timeout')),
  created_at INTEGER,
  created_via TEXT CHECK (created_via IS NULL OR created_via IN ('operator', 'spawn', 'channel', 'cron', 'talk', 'run', 'plugin', 'internal')),
  created_actor_type TEXT CHECK (created_actor_type IS NULL OR created_actor_type IN ('human', 'agent', 'system')),
  created_actor_id TEXT,
  owner_actor_type TEXT,
  owner_actor_id TEXT,
  owner_assigned_by_type TEXT,
  owner_assigned_by_id TEXT,
  owner_assigned_at INTEGER,
  project_id TEXT,
  parent_session_key TEXT,
  spawned_by TEXT,
  fork_source_session_key TEXT,
  fork_source_session_id TEXT,
  fork_source_entry_id TEXT,
  label TEXT,
  display_name TEXT,
  category TEXT,
  icon TEXT,
  pinned_at INTEGER,
  archived_at INTEGER,
  last_read_at INTEGER,
  last_interaction_at INTEGER,
  last_activity_at INTEGER
) STRICT"""

OWNER_KEY = "agent:research-orchestrator:autoresearch:quantipy-v2"
CHILD_KEY = "agent:research-implementer:subagent:3d34650f"
SESSION_ID = "1eff6d18-b671-4cb4-a90e-805b8a52fd1f"
T0 = 1_792_463_900_000


def make_stores(tmp_path: Path) -> ClaudeStores:
    stores = claude_stores(tmp_path)
    stores.openclaw_state_db.parent.mkdir(parents=True)
    with sqlite3.connect(stores.openclaw_state_db) as connection:
        connection.execute(TASK_RUNS_SCHEMA)
        connection.execute(SUBAGENT_RUNS_SCHEMA)
    return stores


def add_task(
    stores: ClaudeStores,
    run_id: str,
    *,
    status: str = "succeeded",
    runtime: str = "subagent",
    agent_id: str = "research-implementer",
    requester: str | None = OWNER_KEY,
    child: str | None = CHILD_KEY,
    created: int = T0,
    started: int | None = T0 + 50,
    ended: int | None = T0 + 5000,
    task_id: str | None = None,
) -> None:
    with sqlite3.connect(stores.openclaw_state_db) as connection:
        connection.execute(
            "INSERT INTO task_runs (task_id, runtime, requester_session_key, owner_key, "
            "scope_kind, child_session_key, agent_id, run_id, task, status, delivery_status, "
            "notify_policy, created_at, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, 'session', ?, ?, ?, 'task', ?, 'pending', 'done_only', ?, ?, ?)",
            (
                task_id or f"task-{run_id}",
                runtime,
                requester,
                requester or "owner",
                child,
                agent_id,
                run_id,
                status,
                created,
                started,
                ended,
            ),
        )


def add_subagent(
    stores: ClaudeStores, run_id: str, payload: dict[str, object], child: str = CHILD_KEY
) -> None:
    with sqlite3.connect(stores.openclaw_state_db) as connection:
        connection.execute(
            "INSERT INTO subagent_runs (run_id, child_session_key, requester_session_key, "
            "created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, child, OWNER_KEY, T0, json.dumps(payload)),
        )


def add_node(
    stores: ClaudeStores,
    agent_id: str,
    session_key: str,
    entry: dict[str, object] | str,
) -> None:
    database = stores.agents_dir / agent_id / "agent" / "openclaw-agent.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS session_nodes_marker (x INTEGER)")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_nodes'"
        ).fetchone():
            connection.execute(SESSION_NODES_SCHEMA)
        connection.execute(
            "INSERT INTO session_nodes (session_key, current_session_id, entry_json, updated_at) "
            "VALUES (?, 'sid', ?, ?)",
            (session_key, entry if isinstance(entry, str) else json.dumps(entry), T0),
        )


def write_transcript(
    stores: ClaudeStores,
    session_id: str,
    lines: list[str],
    slug: str = "-work-tree-attempt",
) -> Path:
    directory = stores.claude_config_dir / "projects" / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def api_request(model: str, effort: str | None, session_id: str = SESSION_ID) -> str:
    params: dict[str, object] = {"model": model, "max_tokens": 1}
    if effort is not None:
        params["output_config"] = {"effort": effort}
    return json.dumps(
        {"type": "api-request", "sessionId": session_id, "id": "r", "params": params},
        separators=(",", ":"),
    )


def user_line(session_id: str = SESSION_ID, cwd: str = "/work/tree/attempt") -> str:
    return json.dumps(
        {"type": "user", "sessionId": session_id, "cwd": cwd, "message": "hi"},
        separators=(",", ":"),
    )


def child_fixture(tmp_path: Path, efforts: tuple[str, ...] = ("max",)) -> ClaudeStores:
    stores = make_stores(tmp_path)
    add_task(stores, "child-1")
    add_subagent(
        stores,
        "child-1",
        {
            "task": "implement it",
            "model": "anthropic/claude-sonnet-5-5",
            "completion": {"resultText": "DONE", "terminalReply": {"text": "other"}},
        },
    )
    add_node(
        stores,
        "research-implementer",
        CHILD_KEY,
        {
            "model": "claude-sonnet-5-5",
            "thinkingLevel": "max",
            "claudeCliSessionId": SESSION_ID,
            "spawnedWorkspaceDir": "/work/tree/attempt",
            "lastRunId": "child-1",
        },
    )
    write_transcript(
        stores,
        SESSION_ID,
        [
            user_line(),
            '{"type":"queue-operation","operation":"enqueue"}',
            '{"type":"future-unknown-record","x":1}',
            *[api_request("claude-sonnet-5-5", effort) for effort in efforts],
        ],
    )
    return stores


def test_stores_derive_from_state_dir(tmp_path: Path) -> None:
    stores = claude_stores(tmp_path)
    assert stores.openclaw_state_db == tmp_path / "state" / "openclaw.sqlite"
    assert stores.agents_dir == tmp_path / "agents"
    assert stores.claude_config_dir == tmp_path / "claude-code"
    with pytest.raises(ClaudeRecordError):
        claude_stores(Path("relative"))


def test_read_run_joins_rows_and_transcript(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    run = read_run(stores, "child-1")
    assert run == ClaudeRun(
        run_id="child-1",
        agent_id="research-implementer",
        session_key=CHILD_KEY,
        requester_session_key=OWNER_KEY,
        runtime="subagent",
        status="succeeded",
        started_at_ms=T0 + 50,
        ended_at_ms=T0 + 5000,
        model="claude-sonnet-5-5",
        thinking_level="max",
        claude_session_id=SESSION_ID,
        transcript_models=frozenset({"claude-sonnet-5-5"}),
        transcript_efforts=frozenset({"max"}),
        task_text="implement it",
        cwd="/work/tree/attempt",
        result_text="DONE",
    )


def test_read_run_not_recorded_and_ambiguous(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    with pytest.raises(ClaudeRecordNotRecorded):
        read_run(stores, "nope")
    assert issubclass(ClaudeRecordNotRecorded, HostRecordError)
    add_task(stores, "child-1", task_id="second")
    with pytest.raises(ClaudeRecordError, match="ambiguous"):
        read_run(stores, "child-1")
    with pytest.raises(ClaudeRecordError, match="ambiguous"):
        run_outcome(stores, "child-1")


def test_missing_database_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ClaudeRecordError):
        read_run(claude_stores(tmp_path), "x")


def test_transcript_with_mixed_efforts_reports_both(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path, efforts=("max", "high", "max"))
    run = read_run(stores, "child-1")
    assert run.transcript_efforts == frozenset({"max", "high"})
    assert run.transcript_models == frozenset({"claude-sonnet-5-5"})


def test_transcript_with_mixed_models_reports_both(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    path = write_transcript(
        stores,
        SESSION_ID,
        [api_request("claude-sonnet-5-5", "max"), api_request("claude-opus-5-5", "max")],
    )
    assert path.is_file()
    assert read_run(stores, "child-1").transcript_models == frozenset(
        {"claude-sonnet-5-5", "claude-opus-5-5"}
    )


@pytest.mark.parametrize(
    "bad",
    [
        '{"type":"api-request","params":',
        f'{{"type":"api-request","sessionId":"{SESSION_ID}"}}',
        '{"type":"api-request","params":{"model":3}}',
        '{"type":"api-request","params":{"model":"m","output_config":{"effort":5}}}',
        '{"type":"api-request","params":{"model":"m","output_config":"high"}}',
        api_request("claude-sonnet-5-5", "max", session_id="another-session"),
    ],
)
def test_malformed_api_request_raises(tmp_path: Path, bad: str) -> None:
    stores = child_fixture(tmp_path)
    write_transcript(
        stores, SESSION_ID, [api_request("claude-sonnet-5-5", "max"), bad, user_line()]
    )
    with pytest.raises(ClaudeRecordError):
        read_run(stores, "child-1")


def test_unterminated_trailing_partial_record_is_ignored(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    path = write_transcript(stores, SESSION_ID, [api_request("claude-sonnet-5-5", "max")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"api-request","params":{"mod')
    assert read_run(stores, "child-1").transcript_efforts == frozenset({"max"})


def test_transcript_size_bound_and_symlink_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gateway.research.host_records_claude as module

    stores = child_fixture(tmp_path)
    monkeypatch.setattr(module, "MAX_CLAUDE_TRANSCRIPT_BYTES", 10)
    with pytest.raises(ClaudeRecordError, match="size limit"):
        read_run(stores, "child-1")
    monkeypatch.undo()
    path = stores.claude_config_dir / "projects" / "-work-tree-attempt" / f"{SESSION_ID}.jsonl"
    real = tmp_path / "real.jsonl"
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(ClaudeRecordError):
        read_run(stores, "child-1")


def test_unsafe_session_id_and_ambiguous_transcript_fail_closed(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    write_transcript(stores, SESSION_ID, [api_request("claude-sonnet-5-5", "max")], slug="-other")
    with pytest.raises(ClaudeRecordError, match="ambiguous"):
        read_run(stores, "child-1")
    other = make_stores(tmp_path / "second")
    add_task(other, "c")
    add_node(
        other,
        "research-implementer",
        CHILD_KEY,
        {"claudeCliSessionId": "../escape", "lastRunId": "c"},
    )
    with pytest.raises(ClaudeRecordError, match="safe session id"):
        read_run(other, "c")


def test_missing_transcript_leaves_empty_sets(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    (stores.claude_config_dir / "projects" / "-work-tree-attempt" / f"{SESSION_ID}.jsonl").unlink()
    run = read_run(stores, "child-1")
    assert run.transcript_models == frozenset() and run.transcript_efforts == frozenset()
    assert run.model == "claude-sonnet-5-5" and run.cwd == "/work/tree/attempt"


def test_missing_agent_database_or_node_gives_none_fields(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "child-1")
    run = read_run(stores, "child-1")
    assert run.model is None and run.claude_session_id is None and run.thinking_level is None
    add_node(stores, "research-implementer", "agent:other:key", {"model": "claude-sonnet-5-5"})
    assert read_run(stores, "child-1").model is None


def test_model_falls_back_to_subagent_payload_and_strips_provider(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "child-1")
    add_subagent(
        stores,
        "child-1",
        {
            "task": "t",
            "model": "anthropic/claude-sonnet-5-5",
            "completion": {"terminalReply": {"text": "R"}},
        },
    )
    run = read_run(stores, "child-1")
    assert run.model == "claude-sonnet-5-5" and run.result_text == "R"


def test_session_reused_by_later_run_is_not_attributed(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    with sqlite3.connect(
        stores.agents_dir / "research-implementer" / "agent" / "openclaw-agent.sqlite"
    ) as connection:
        connection.execute(
            "UPDATE session_nodes SET entry_json = json_set(entry_json, '$.lastRunId', 'later')"
        )
    run = read_run(stores, "child-1")
    assert run.claude_session_id is None and run.transcript_efforts == frozenset()


def test_malformed_session_entry_and_payload_fail_closed(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "child-1")
    add_node(stores, "research-implementer", CHILD_KEY, "{not json")
    with pytest.raises(ClaudeRecordError):
        read_run(stores, "child-1")
    other = make_stores(tmp_path / "b")
    add_task(other, "child-1")
    add_subagent(other, "child-1", {})
    with sqlite3.connect(other.openclaw_state_db) as connection:
        connection.execute("UPDATE subagent_runs SET payload_json = '[1]'")
    with pytest.raises(ClaudeRecordError):
        read_run(other, "child-1")


def test_unsafe_agent_id_refused(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "child-1", agent_id="../evil")
    with pytest.raises(ClaudeRecordError, match="safe agent id"):
        read_run(stores, "child-1")


def test_owner_cli_run_uses_requester_session(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "owner-1", runtime="cli", agent_id="research-orchestrator", child=None)
    run = read_run(stores, "owner-1")
    assert run.session_key == OWNER_KEY and run.task_text is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("succeeded", "ok"),
        ("failed", "error"),
        ("error", "error"),
        ("cancelled", "error"),
        ("timed_out", "error"),
        ("lost", "lost"),
        ("running", "pending"),
        ("queued", "pending"),
    ],
)
def test_run_outcome_maps_status(tmp_path: Path, status: str, expected: str) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "r", status=status)
    assert run_outcome(stores, "r") == expected
    assert read_run(stores, "r").status == status


def test_unknown_status_and_missing_row_fail_closed(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "r", status="bogus_status")
    with pytest.raises(ClaudeRecordError, match="unknown task_runs status"):
        run_outcome(stores, "r")
    with pytest.raises(ClaudeRecordNotRecorded):
        run_outcome(stores, "missing")


def test_child_runs_filters_requester_runtime_and_since(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    add_task(stores, "old", created=T0 - 10, child="agent:research-implementer:subagent:old")
    add_task(stores, "new-b", created=T0 + 20, child="agent:research-implementer:subagent:b")
    add_task(stores, "new-a", created=T0 + 10, child="agent:research-implementer:subagent:a")
    add_task(
        stores,
        "foreign",
        requester="agent:other:session",
        created=T0 + 30,
        child="agent:research-implementer:subagent:f",
    )
    add_task(stores, "owner-cli", runtime="cli", created=T0 + 40, child=None)
    runs = child_runs(stores, OWNER_KEY, since_ms=T0)
    assert [run.run_id for run in runs] == ["new-a", "new-b"]
    assert [run.run_id for run in child_runs(stores, OWNER_KEY, since_ms=T0 - 100)] == [
        "old",
        "new-a",
        "new-b",
    ]
    assert child_runs(stores, "agent:nobody", since_ms=0) == ()
    with pytest.raises(ClaudeRecordError):
        child_runs(stores, OWNER_KEY, since_ms=-1)
    with pytest.raises(ClaudeRecordError):
        child_runs(stores, "", since_ms=0)


def test_reads_do_not_modify_databases(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    before = stores.openclaw_state_db.read_bytes()
    read_run(stores, "child-1")
    assert stores.openclaw_state_db.read_bytes() == before


def _subagent_dir(stores: ClaudeStores) -> Path:
    directory = (
        stores.claude_config_dir / "projects" / "-work-tree-attempt" / SESSION_ID / "subagents"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_builtin_subagent_transcripts_are_unioned(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    directory = _subagent_dir(stores)
    (directory / "agent-a1.jsonl").write_text(
        api_request("claude-haiku-5", "low") + "\n" + user_line() + "\n", encoding="utf-8"
    )
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    run = read_run(stores, "child-1")
    assert run.transcript_models == frozenset({"claude-sonnet-5-5", "claude-haiku-5"})
    assert run.transcript_efforts == frozenset({"max", "low"})


def test_builtin_subagent_transcript_faults_fail_closed(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    directory = _subagent_dir(stores)
    bad = directory / "agent-bad.jsonl"
    bad.write_text('{"type":"api-request","params":\n', encoding="utf-8")
    with pytest.raises(ClaudeRecordError):
        read_run(stores, "child-1")
    bad.unlink()
    real = tmp_path / "elsewhere.jsonl"
    real.write_text(api_request("claude-haiku-5", "low") + "\n", encoding="utf-8")
    (directory / "agent-link.jsonl").symlink_to(real)
    with pytest.raises(ClaudeRecordError):
        read_run(stores, "child-1")


def test_subagent_directory_symlink_refused(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = stores.claude_config_dir / "projects" / "-work-tree-attempt" / SESSION_ID / "subagents"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(ClaudeRecordError):
        read_run(stores, "child-1")


def test_api_request_without_session_id_raises(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    record = json.dumps({"type": "api-request", "params": {"model": "m"}})
    write_transcript(stores, SESSION_ID, [record])
    with pytest.raises(ClaudeRecordError, match="sessionId"):
        read_run(stores, "child-1")


def test_finished_run_needs_last_run_id_to_attribute_node(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    database = stores.agents_dir / "research-implementer" / "agent" / "openclaw-agent.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session_nodes SET entry_json = json_remove(entry_json, '$.lastRunId')"
        )
    assert read_run(stores, "child-1").claude_session_id is None
    with sqlite3.connect(stores.openclaw_state_db) as connection:
        connection.execute("UPDATE task_runs SET status = 'running'")
    assert read_run(stores, "child-1").claude_session_id == SESSION_ID


def test_model_and_cwd_disagreements_fail_closed(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    with sqlite3.connect(stores.openclaw_state_db) as connection:
        connection.execute(
            "UPDATE subagent_runs SET payload_json = json_set(payload_json, '$.model', "
            "'anthropic/claude-opus-5-5')"
        )
    with pytest.raises(ClaudeRecordError, match="disagrees"):
        read_run(stores, "child-1")
    other = child_fixture(tmp_path / "b")
    write_transcript(other, SESSION_ID, [user_line(cwd="/elsewhere")])
    with pytest.raises(ClaudeRecordError, match="cwd"):
        read_run(other, "child-1")


def test_child_runs_agent_filter_precedes_transcript_reads(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    add_task(
        stores,
        "other-1",
        agent_id="research-reviewer",
        child="agent:research-reviewer:subagent:x",
        created=T0 + 5,
    )
    add_node(
        stores,
        "research-reviewer",
        "agent:research-reviewer:subagent:x",
        {"claudeCliSessionId": "rev-session", "lastRunId": "other-1"},
    )
    write_transcript(stores, "rev-session", ['{"type":"api-request","params":'], slug="-rev")
    with pytest.raises(ClaudeRecordError):
        child_runs(stores, OWNER_KEY, since_ms=0)
    runs = child_runs(stores, OWNER_KEY, since_ms=0, agent_id="research-implementer")
    assert [run.run_id for run in runs] == ["child-1"]


def _set_node(stores: ClaudeStores, **fields: str) -> None:
    database = stores.agents_dir / "research-implementer" / "agent" / "openclaw-agent.sqlite"
    with sqlite3.connect(database) as connection:
        for key, value in fields.items():
            connection.execute(
                "UPDATE session_nodes SET entry_json = json_set(entry_json, ?, ?)",
                (f"$.{key}", value),
            )


def test_cwd_prefers_spawned_cwd_over_agent_workspace(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    _set_node(stores, spawnedCwd="/work/tree/attempt", spawnedWorkspaceDir="/state/workspace-impl")
    run = read_run(stores, "child-1")
    assert run.cwd == "/work/tree/attempt"
    # Transcript cwd equal only to the agent workspace is a mismatch.
    write_transcript(stores, SESSION_ID, [user_line(cwd="/state/workspace-impl")])
    with pytest.raises(ClaudeRecordError, match="cwd"):
        read_run(stores, "child-1")


def test_cwd_comparison_normalises_paths(tmp_path: Path) -> None:
    real = tmp_path / "checkout"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    stores = child_fixture(tmp_path / "s")
    _set_node(stores, spawnedCwd=f"{link}/")
    write_transcript(stores, SESSION_ID, [user_line(cwd=str(real))])
    assert read_run(stores, "child-1").cwd == f"{link}/"


def test_nested_workflow_journals_are_unioned_and_symlinks_refused(tmp_path: Path) -> None:
    stores = child_fixture(tmp_path)
    journal = _subagent_dir(stores) / "workflows" / "wf_1"
    journal.mkdir(parents=True)
    (journal / "journal.jsonl").write_text(
        api_request("claude-haiku-5", "low") + "\n" + user_line() + "\n", encoding="utf-8"
    )
    run = read_run(stores, "child-1")
    assert "claude-haiku-5" in run.transcript_models and "low" in run.transcript_efforts
    (journal / "tool-results").symlink_to(tmp_path)
    with pytest.raises(ClaudeRecordError, match="symlink"):
        read_run(stores, "child-1")


def test_oserror_during_transcript_walk_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gateway.research.host_records_claude as module

    stores = child_fixture(tmp_path)
    for target in ("_find_transcript", "_read_transcript"):
        monkeypatch.setattr(
            module,
            target,
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(13, "denied")),
        )
        with pytest.raises(ClaudeRecordError, match="walk failed"):
            read_run(stores, "child-1")
        with pytest.raises(ClaudeRecordError):
            child_runs(stores, OWNER_KEY, since_ms=0)
        monkeypatch.undo()
    # A real unreadable session directory (no mocks) is also typed.
    directory = stores.claude_config_dir / "projects" / "-work-tree-attempt" / SESSION_ID
    directory.mkdir()
    directory.chmod(0)
    try:
        if directory.stat().st_mode & 0o777 == 0 and not _is_root():
            with pytest.raises(ClaudeRecordError):
                read_run(stores, "child-1")
    finally:
        directory.chmod(0o700)


def _is_root() -> bool:
    import os

    return os.geteuid() == 0
