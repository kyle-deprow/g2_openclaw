"""Managed Codex SQLite database repair helpers extracted from the push script."""

# ruff: noqa: E501, I001

import argparse
import sys
from collections.abc import Sequence


def repair_log_db() -> None:
    """Repair the managed Codex logs_2.sqlite database."""

    import os
    import json
    import re
    import stat
    import subprocess
    import sys
    from pathlib import Path

    log_db = Path(sys.argv[1])
    expected_schema = {
        (
            "index",
            "idx_logs_process_uuid_threadless_ts",
            "logs",
            "CREATE INDEX idx_logs_process_uuid_threadless_ts ON logs(process_uuid, ts DESC, ts_nanos DESC, id DESC)\nWHERE thread_id IS NULL",
        ),
        (
            "index",
            "idx_logs_thread_id",
            "logs",
            "CREATE INDEX idx_logs_thread_id ON logs(thread_id)",
        ),
        (
            "index",
            "idx_logs_thread_id_ts",
            "logs",
            "CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id, ts DESC, ts_nanos DESC, id DESC)",
        ),
        (
            "index",
            "idx_logs_ts",
            "logs",
            "CREATE INDEX idx_logs_ts ON logs(ts DESC, ts_nanos DESC, id DESC)",
        ),
        (
            "table",
            "logs",
            "logs",
            """CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ts_nanos INTEGER NOT NULL,
    level TEXT NOT NULL,
    target TEXT NOT NULL,
    feedback_log_body TEXT,
    module_path TEXT,
    file TEXT,
    line INTEGER,
    thread_id TEXT,
    process_uuid TEXT,
    estimated_bytes INTEGER NOT NULL DEFAULT 0
)""",
        ),
    }
    index_only_patterns = (
        re.compile(r"^row [0-9]+ missing from index idx_logs_thread_id$"),
        re.compile(r"^wrong # of entries in index idx_logs_thread_id$"),
    )

    def run_sql(sql: str) -> str:
        completed = subprocess.run(
            ["sqlite3", str(log_db), sql],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout.strip()

    def run_sql_json(sql: str) -> object:
        completed = subprocess.run(
            ["sqlite3", "-json", str(log_db), sql],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
        try:
            return json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid sqlite JSON output for {log_db}: {exc}") from exc

    def file_identity() -> tuple[int, int] | None:
        try:
            st = os.lstat(log_db)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            raise SystemExit(f"Scoped Codex log DB must not be a symlink: {log_db}")
        if not stat.S_ISREG(st.st_mode):
            raise SystemExit(f"Scoped Codex log DB must be a regular file: {log_db}")
        if st.st_nlink != 1:
            raise SystemExit(
                f"Scoped Codex log DB must not have hard links; st_nlink={st.st_nlink}: {log_db}"
            )
        if st.st_uid != os.geteuid():
            raise SystemExit(
                f"Scoped Codex log DB owner uid {st.st_uid} does not match current uid {os.geteuid()}: {log_db}"
            )
        return (st.st_dev, st.st_ino)

    def schema_rows() -> set[tuple[str, str, str, str]]:
        raw = run_sql_json(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name = 'logs' AND type IN ('table', 'index') "
            "ORDER BY type, name;"
        )
        if not isinstance(raw, list):
            raise SystemExit(f"unexpected logs_2.sqlite schema JSON for {log_db}")
        rows: set[tuple[str, str, str, str]] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise SystemExit(f"unexpected logs_2.sqlite schema row: {item!r}")
            values = (item.get("type"), item.get("name"), item.get("tbl_name"), item.get("sql"))
            if not all(isinstance(value, str) for value in values):
                raise SystemExit(f"unexpected logs_2.sqlite schema row: {item!r}")
            rows.add(values)  # type: ignore[arg-type]
        return rows

    def validate_schema() -> None:
        actual = schema_rows()
        if actual != expected_schema:
            missing = sorted(expected_schema - actual)
            extra = sorted(actual - expected_schema)
            raise SystemExit(
                f"Scoped Codex log DB schema does not match the pinned logs_2.sqlite schema at {log_db}; "
                f"missing={missing!r}; extra={extra!r}"
            )

    def parse_integrity(output: str) -> list[str]:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            raise SystemExit(f"empty PRAGMA integrity_check output for {log_db}")
        return lines

    def is_repairable_idx_logs_thread_id_only(lines: list[str]) -> bool:
        return all(
            any(pattern.fullmatch(line) for pattern in index_only_patterns) for line in lines
        )

    identity_before = file_identity()
    if identity_before is None:
        raise SystemExit(0)
    validate_schema()
    integrity_before = parse_integrity(run_sql("PRAGMA integrity_check;"))
    if integrity_before == ["ok"]:
        raise SystemExit(0)
    if not is_repairable_idx_logs_thread_id_only(integrity_before):
        raise SystemExit(
            f"Scoped Codex log DB {log_db} has non-repairable integrity errors: {integrity_before!r}"
        )

    print(f"Repairing scoped Codex log DB idx_logs_thread_id with REINDEX: {log_db}")
    integrity_after = parse_integrity(
        run_sql("REINDEX idx_logs_thread_id; PRAGMA integrity_check;")
    )
    if integrity_after != ["ok"]:
        if not is_repairable_idx_logs_thread_id_only(integrity_after):
            raise SystemExit(
                f"Scoped Codex log DB {log_db} has non-repairable integrity errors "
                f"after REINDEX idx_logs_thread_id: {integrity_after!r}"
            )
        print(f"Rebuilding scoped Codex log DB idx_logs_thread_id: {log_db}")
        integrity_after = parse_integrity(
            run_sql(
                "DROP INDEX idx_logs_thread_id; "
                "CREATE INDEX idx_logs_thread_id ON logs(thread_id); "
                "PRAGMA integrity_check;"
            )
        )
        if integrity_after != ["ok"]:
            raise SystemExit(
                f"Scoped Codex log DB {log_db} remains corrupt after rebuilding "
                f"idx_logs_thread_id: {integrity_after!r}"
            )
    if file_identity() != identity_before:
        raise SystemExit(f"Scoped Codex log DB identity changed during repair: {log_db}")
    validate_schema()
    print(f"Repaired scoped Codex log DB idx_logs_thread_id: {log_db}")


def repair_state_db() -> None:
    """Repair the managed Codex state_5.sqlite database."""

    import datetime as dt
    import os
    import sqlite3
    import stat
    import sys
    from pathlib import Path

    state_db = Path(sys.argv[1])
    codex_home = state_db.parent
    sessions_root = codex_home / "sessions"
    expected_schema = {
        (
            "index",
            "idx_agent_job_items_status",
            "agent_job_items",
            "CREATE INDEX idx_agent_job_items_status ON agent_job_items(job_id, status, row_index ASC)",
        ),
        (
            "index",
            "idx_agent_jobs_status",
            "agent_jobs",
            "CREATE INDEX idx_agent_jobs_status ON agent_jobs(status, updated_at DESC)",
        ),
        (
            "index",
            "idx_thread_dynamic_tools_thread",
            "thread_dynamic_tools",
            "CREATE INDEX idx_thread_dynamic_tools_thread ON thread_dynamic_tools(thread_id)",
        ),
        (
            "index",
            "idx_thread_spawn_edges_parent_status",
            "thread_spawn_edges",
            "CREATE INDEX idx_thread_spawn_edges_parent_status\n    ON thread_spawn_edges(parent_thread_id, status)",
        ),
        (
            "index",
            "idx_threads_archived",
            "threads",
            "CREATE INDEX idx_threads_archived ON threads(archived)",
        ),
        (
            "index",
            "idx_threads_archived_cwd_created_at_ms",
            "threads",
            "CREATE INDEX idx_threads_archived_cwd_created_at_ms ON threads(archived, cwd, created_at_ms DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_archived_cwd_recency_at_ms",
            "threads",
            "CREATE INDEX idx_threads_archived_cwd_recency_at_ms\n    ON threads(archived, cwd, recency_at_ms DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_archived_cwd_updated_at_ms",
            "threads",
            "CREATE INDEX idx_threads_archived_cwd_updated_at_ms ON threads(archived, cwd, updated_at_ms DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_created_at",
            "threads",
            "CREATE INDEX idx_threads_created_at ON threads(created_at DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_created_at_ms",
            "threads",
            "CREATE INDEX idx_threads_created_at_ms ON threads(created_at_ms DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_provider",
            "threads",
            "CREATE INDEX idx_threads_provider ON threads(model_provider)",
        ),
        (
            "index",
            "idx_threads_recency_at_ms",
            "threads",
            "CREATE INDEX idx_threads_recency_at_ms\n    ON threads(recency_at_ms DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_source",
            "threads",
            "CREATE INDEX idx_threads_source ON threads(source)",
        ),
        (
            "index",
            "idx_threads_updated_at",
            "threads",
            "CREATE INDEX idx_threads_updated_at ON threads(updated_at DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_updated_at_ms",
            "threads",
            "CREATE INDEX idx_threads_updated_at_ms ON threads(updated_at_ms DESC, id DESC)",
        ),
        (
            "index",
            "idx_threads_visible_created_at_ms",
            "threads",
            "CREATE INDEX idx_threads_visible_created_at_ms\n    ON threads(archived, created_at_ms DESC)\n    WHERE preview <> ''",
        ),
        (
            "index",
            "idx_threads_visible_recency_at_ms",
            "threads",
            "CREATE INDEX idx_threads_visible_recency_at_ms\n    ON threads(archived, recency_at_ms DESC, id DESC)\n    WHERE preview <> ''",
        ),
        (
            "index",
            "idx_threads_visible_updated_at_ms",
            "threads",
            "CREATE INDEX idx_threads_visible_updated_at_ms\n    ON threads(archived, updated_at_ms DESC)\n    WHERE preview <> ''",
        ),
        (
            "table",
            "_sqlx_migrations",
            "_sqlx_migrations",
            """CREATE TABLE _sqlx_migrations (
    version BIGINT PRIMARY KEY,
    description TEXT NOT NULL,
    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    success BOOLEAN NOT NULL,
    checksum BLOB NOT NULL,
    execution_time BIGINT NOT NULL
)""",
        ),
        (
            "table",
            "agent_job_items",
            "agent_job_items",
            """CREATE TABLE agent_job_items (
    job_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    source_id TEXT,
    row_json TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_thread_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    reported_at INTEGER,
    PRIMARY KEY (job_id, item_id),
    FOREIGN KEY(job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE
)""",
        ),
        (
            "table",
            "agent_jobs",
            "agent_jobs",
            """CREATE TABLE agent_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    instruction TEXT NOT NULL,
    output_schema_json TEXT,
    input_headers_json TEXT NOT NULL,
    input_csv_path TEXT NOT NULL,
    output_csv_path TEXT NOT NULL,
    auto_export INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    last_error TEXT
, max_runtime_seconds INTEGER)""",
        ),
        (
            "table",
            "backfill_state",
            "backfill_state",
            """CREATE TABLE backfill_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    last_watermark TEXT,
    last_success_at INTEGER,
    updated_at INTEGER NOT NULL
)""",
        ),
        (
            "table",
            "external_agent_config_imports",
            "external_agent_config_imports",
            """CREATE TABLE external_agent_config_imports (
    import_id TEXT PRIMARY KEY,
    completed_at_ms INTEGER NOT NULL,
    successes TEXT NOT NULL,
    failures TEXT NOT NULL
)""",
        ),
        (
            "table",
            "remote_control_enrollments",
            "remote_control_enrollments",
            """CREATE TABLE remote_control_enrollments (
    websocket_url TEXT NOT NULL,
    account_id TEXT NOT NULL,
    app_server_client_name TEXT NOT NULL,
    server_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    server_name TEXT NOT NULL,
    updated_at INTEGER NOT NULL, remote_control_enabled INTEGER,
    PRIMARY KEY (websocket_url, account_id, app_server_client_name)
)""",
        ),
        (
            "table",
            "thread_dynamic_tools",
            "thread_dynamic_tools",
            """CREATE TABLE thread_dynamic_tools (
    thread_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT 0, namespace TEXT,
    PRIMARY KEY(thread_id, position),
    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
)""",
        ),
        (
            "table",
            "thread_spawn_edges",
            "thread_spawn_edges",
            "CREATE TABLE thread_spawn_edges (\n    parent_thread_id TEXT NOT NULL,\n    child_thread_id TEXT NOT NULL PRIMARY KEY,\n    status TEXT NOT NULL\n)",
        ),
        (
            "table",
            "threads",
            "threads",
            """CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT
, cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '', agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled', model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT '', recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0, history_mode TEXT NOT NULL DEFAULT 'legacy')""",
        ),
        (
            "trigger",
            "threads_created_at_ms_after_insert",
            "threads",
            """CREATE TRIGGER threads_created_at_ms_after_insert
AFTER INSERT ON threads
WHEN NEW.created_at_ms IS NULL
BEGIN
    UPDATE threads
    SET created_at_ms = NEW.created_at * 1000
    WHERE id = NEW.id;
END""",
        ),
        (
            "trigger",
            "threads_created_at_ms_after_update",
            "threads",
            """CREATE TRIGGER threads_created_at_ms_after_update
AFTER UPDATE OF created_at ON threads
WHEN NEW.created_at != OLD.created_at
 AND NEW.created_at_ms IS OLD.created_at_ms
BEGIN
    UPDATE threads
    SET created_at_ms = NEW.created_at * 1000
    WHERE id = NEW.id;
END""",
        ),
        (
            "trigger",
            "threads_recency_at_after_insert",
            "threads",
            """CREATE TRIGGER threads_recency_at_after_insert
AFTER INSERT ON threads
WHEN NEW.recency_at_ms = 0
BEGIN
    UPDATE threads
    SET recency_at = NEW.updated_at,
        recency_at_ms = COALESCE(NEW.updated_at_ms, NEW.updated_at * 1000)
    WHERE id = NEW.id;
END""",
        ),
        (
            "trigger",
            "threads_updated_at_ms_after_insert",
            "threads",
            """CREATE TRIGGER threads_updated_at_ms_after_insert
AFTER INSERT ON threads
WHEN NEW.updated_at_ms IS NULL
BEGIN
    UPDATE threads
    SET updated_at_ms = NEW.updated_at * 1000
    WHERE id = NEW.id;
END""",
        ),
        (
            "trigger",
            "threads_updated_at_ms_after_update",
            "threads",
            """CREATE TRIGGER threads_updated_at_ms_after_update
AFTER UPDATE OF updated_at ON threads
WHEN NEW.updated_at != OLD.updated_at
 AND NEW.updated_at_ms IS OLD.updated_at_ms
BEGIN
    UPDATE threads
    SET updated_at_ms = NEW.updated_at * 1000
    WHERE id = NEW.id;
END""",
        ),
    }

    def file_identity() -> tuple[int, int] | None:
        try:
            st = os.lstat(state_db)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            raise SystemExit(f"Scoped Codex state DB must not be a symlink: {state_db}")
        if not stat.S_ISREG(st.st_mode):
            raise SystemExit(f"Scoped Codex state DB must be a regular file: {state_db}")
        if st.st_nlink != 1:
            raise SystemExit(
                f"Scoped Codex state DB must not have hard links; st_nlink={st.st_nlink}: {state_db}"
            )
        if st.st_uid != os.geteuid():
            raise SystemExit(
                f"Scoped Codex state DB owner uid {st.st_uid} does not match current uid {os.geteuid()}: {state_db}"
            )
        return (st.st_dev, st.st_ino)

    def parse_integrity(rows: list[tuple[object, ...]]) -> list[str]:
        lines = [str(row[0]).strip() for row in rows if row and str(row[0]).strip()]
        if not lines:
            raise SystemExit(f"empty PRAGMA integrity_check output for {state_db}")
        return lines

    def validate_integrity(connection: sqlite3.Connection, context: str) -> None:
        integrity = parse_integrity(connection.execute("PRAGMA integrity_check;").fetchall())
        if integrity != ["ok"]:
            raise SystemExit(
                f"Scoped Codex state DB {state_db} has non-repairable integrity errors {context}: {integrity!r}"
            )

    def validate_foreign_keys(connection: sqlite3.Connection, context: str) -> None:
        connection.execute("PRAGMA foreign_keys=ON;")
        status = connection.execute("PRAGMA foreign_keys;").fetchone()
        if status != (1,):
            raise SystemExit(
                f"Scoped Codex state DB could not enable foreign_keys {context}: {status!r}"
            )
        rows = connection.execute("PRAGMA foreign_key_check;").fetchall()
        violations: list[tuple[str, int | None, str, int]] = []
        for item in rows:
            if (
                len(item) != 4
                or not isinstance(item[0], str)
                or not (isinstance(item[1], int) or item[1] is None)
                or not isinstance(item[2], str)
                or not isinstance(item[3], int)
            ):
                raise SystemExit(
                    f"unexpected PRAGMA foreign_key_check row for scoped Codex state DB "
                    f"{context}: {item!r}"
                )
            violations.append(item)
        if violations:
            raise SystemExit(
                f"Scoped Codex state DB {state_db} has foreign-key violations "
                f"{context}: {violations!r}"
            )

    def schema_rows(connection: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
        raw = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND type IN ('table', 'index', 'trigger') "
            "ORDER BY type, name;"
        ).fetchall()
        rows: set[tuple[str, str, str, str]] = set()
        for item in raw:
            if len(item) != 4 or not all(isinstance(value, str) for value in item):
                raise SystemExit(f"unexpected state_5.sqlite schema row: {item!r}")
            rows.add(item)
        return rows

    def validate_schema(connection: sqlite3.Connection) -> None:
        actual = schema_rows(connection)
        if actual != expected_schema:
            missing = sorted(expected_schema - actual)
            extra = sorted(actual - expected_schema)
            raise SystemExit(
                f"Scoped Codex state DB schema does not match the pinned state_5.sqlite schema at {state_db}; "
                f"missing={missing!r}; extra={extra!r}"
            )

    def load_threads(connection: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = connection.execute("SELECT id, rollout_path FROM threads ORDER BY id;").fetchall()
        threads: list[tuple[str, str]] = []
        for item in rows:
            if (
                len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                or not item[0]
                or not item[1]
            ):
                raise SystemExit(f"unexpected state_5.sqlite thread row: {item!r}")
            if "\x00" in item[1]:
                raise SystemExit(
                    f"Scoped Codex state DB has NUL in rollout_path for thread {item[0]!r}"
                )
            rollout_path = Path(item[1])
            if not rollout_path.is_absolute():
                raise SystemExit(
                    f"Scoped Codex state DB rollout_path is not absolute for thread {item[0]!r}: {item[1]}"
                )
            threads.append((item[0], item[1]))
        return threads

    def require_normal_directory(path: Path, context: str) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise SystemExit(
                f"Scoped Codex state DB rollout_path parent topology is not a normal sessions tree; "
                f"missing {context}: {path}"
            ) from None
        if stat.S_ISLNK(st.st_mode):
            raise SystemExit(
                f"Scoped Codex state DB rollout_path parent topology is not a normal sessions tree; "
                f"symlinked {context}: {path}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise SystemExit(
                f"Scoped Codex state DB rollout_path parent topology is not a normal sessions tree; "
                f"non-directory {context}: {path}"
            )

    def validate_rollout_scope(thread_id: str, rollout_path: Path) -> None:
        try:
            relative = rollout_path.relative_to(sessions_root)
        except ValueError:
            raise SystemExit(
                f"Scoped Codex state DB rollout_path is outside expected Codex rollout tree "
                f"{sessions_root}/YYYY/MM/DD/rollout-*.jsonl for thread {thread_id!r}: {rollout_path}"
            ) from None

        parts = relative.parts
        if len(parts) != 4:
            raise SystemExit(
                f"Scoped Codex state DB rollout_path is not in expected Codex rollout tree "
                f"{sessions_root}/YYYY/MM/DD/rollout-*.jsonl for thread {thread_id!r}: {rollout_path}"
            )
        year, month, day, filename = parts
        if (
            len(year) != 4
            or len(month) != 2
            or len(day) != 2
            or not year.isdecimal()
            or not month.isdecimal()
            or not day.isdecimal()
        ):
            raise SystemExit(
                f"Scoped Codex state DB rollout_path has invalid YYYY/MM/DD path for "
                f"thread {thread_id!r}: {rollout_path}"
            )
        try:
            dt.date(int(year), int(month), int(day))
        except ValueError as exc:
            raise SystemExit(
                f"Scoped Codex state DB rollout_path has invalid calendar date for "
                f"thread {thread_id!r}: {rollout_path}"
            ) from exc
        if (
            not filename.startswith("rollout-")
            or not filename.endswith(".jsonl")
            or filename == "rollout-.jsonl"
        ):
            raise SystemExit(
                f"Scoped Codex state DB rollout_path filename is not rollout-*.jsonl for "
                f"thread {thread_id!r}: {rollout_path}"
            )

        require_normal_directory(codex_home, "Codex home")
        require_normal_directory(sessions_root, "sessions root")
        require_normal_directory(sessions_root / year, "year directory")
        require_normal_directory(sessions_root / year / month, "month directory")
        require_normal_directory(sessions_root / year / month / day, "day directory")

    def rollout_path_is_missing(thread_id: str, path: str) -> bool:
        rollout_path = Path(path)
        validate_rollout_scope(thread_id, rollout_path)
        try:
            st = os.lstat(rollout_path)
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(st.st_mode):
            raise SystemExit(
                f"Scoped Codex state DB existing rollout_path must not be a symlink for "
                f"thread {thread_id!r}: {rollout_path}"
            )
        if not stat.S_ISREG(st.st_mode):
            raise SystemExit(
                f"Scoped Codex state DB existing rollout_path must be a regular file for "
                f"thread {thread_id!r}: {rollout_path}"
            )
        return False

    def missing_rollout_threads(connection: sqlite3.Connection) -> list[tuple[str, str]]:
        return [
            (thread_id, path)
            for thread_id, path in load_threads(connection)
            if rollout_path_is_missing(thread_id, path)
        ]

    def fail_if_global_non_cascading_references_are_unsafe(
        connection: sqlite3.Connection,
        context: str,
    ) -> None:
        spawn_rows = connection.execute(
            """
        SELECT
            edges.parent_thread_id,
            edges.child_thread_id,
            parent_threads.id IS NULL AS parent_missing,
            child_threads.id IS NULL AS child_missing
        FROM thread_spawn_edges AS edges
        LEFT JOIN threads AS parent_threads
            ON parent_threads.id = edges.parent_thread_id
        LEFT JOIN threads AS child_threads
            ON child_threads.id = edges.child_thread_id
        WHERE parent_threads.id IS NULL
           OR child_threads.id IS NULL
        ORDER BY edges.parent_thread_id, edges.child_thread_id;
        """
        ).fetchall()
        if spawn_rows:
            raise SystemExit(
                "Scoped Codex state DB has orphaned non-cascading thread_spawn_edges "
                f"{context}; refusing validation/repair: {spawn_rows!r}"
            )

        job_item_rows = connection.execute(
            """
        SELECT items.job_id, items.item_id, items.assigned_thread_id
        FROM agent_job_items AS items
        LEFT JOIN threads
            ON threads.id = items.assigned_thread_id
        WHERE items.assigned_thread_id IS NOT NULL
          AND threads.id IS NULL
        ORDER BY items.job_id, items.item_id;
        """
        ).fetchall()
        if job_item_rows:
            raise SystemExit(
                "Scoped Codex state DB has orphaned non-cascading agent_job_items.assigned_thread_id "
                f"{context}; refusing validation/repair: {job_item_rows!r}"
            )

    def collect_stale_thread_spawn_edges_to_delete(
        connection: sqlite3.Connection,
        stale_threads: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        thread_ids = [thread_id for thread_id, _ in stale_threads]
        stale_thread_ids = set(thread_ids)
        placeholders = ",".join("?" for _ in thread_ids)
        spawn_rows = connection.execute(
            f"""
        SELECT parent_thread_id, child_thread_id
        FROM thread_spawn_edges
        WHERE parent_thread_id IN ({placeholders})
           OR child_thread_id IN ({placeholders})
        ORDER BY parent_thread_id, child_thread_id;
        """,
            (*thread_ids, *thread_ids),
        ).fetchall()
        mixed_rows: list[tuple[str, str]] = []
        deletable_rows: list[tuple[str, str]] = []
        for item in spawn_rows:
            if (
                len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                or not item[0]
                or not item[1]
            ):
                raise SystemExit(f"unexpected state_5.sqlite thread_spawn_edges row: {item!r}")
            parent_thread_id, child_thread_id = item
            parent_is_stale = parent_thread_id in stale_thread_ids
            child_is_stale = child_thread_id in stale_thread_ids
            if parent_is_stale and child_is_stale:
                deletable_rows.append((parent_thread_id, child_thread_id))
            else:
                mixed_rows.append((parent_thread_id, child_thread_id))
        if mixed_rows:
            raise SystemExit(
                "Scoped Codex state DB stale thread rows are referenced by mixed stale/non-stale "
                f"thread_spawn_edges state; refusing delete: {mixed_rows!r}"
            )
        return deletable_rows

    def fail_if_stale_threads_have_agent_job_items(
        connection: sqlite3.Connection,
        stale_threads: list[tuple[str, str]],
    ) -> None:
        thread_ids = [thread_id for thread_id, _ in stale_threads]
        placeholders = ",".join("?" for _ in thread_ids)
        job_item_rows = connection.execute(
            f"""
        SELECT job_id, item_id, assigned_thread_id
        FROM agent_job_items
        WHERE assigned_thread_id IN ({placeholders})
        ORDER BY job_id, item_id;
        """,
            thread_ids,
        ).fetchall()
        if job_item_rows:
            raise SystemExit(
                "Scoped Codex state DB stale thread rows are referenced by non-cascading "
                f"agent_job_items state; refusing delete: {job_item_rows!r}"
            )

    identity_before = file_identity()
    if identity_before is None:
        raise SystemExit(0)

    connection = sqlite3.connect(state_db)
    try:
        validate_schema(connection)
        validate_integrity(connection, "before stale rollout_path repair")
        validate_foreign_keys(connection, "before stale rollout_path repair")
        fail_if_global_non_cascading_references_are_unsafe(
            connection,
            "before stale rollout_path repair",
        )
        stale_threads = missing_rollout_threads(connection)
        if not stale_threads:
            raise SystemExit(0)

        for thread_id, rollout_path in stale_threads:
            if not rollout_path_is_missing(thread_id, rollout_path):
                raise SystemExit(
                    f"refusing to delete non-stale rollout_path that exists: {rollout_path}"
                )

        print(
            f"Repairing scoped Codex state DB stale thread rows: {state_db} ({len(stale_threads)} rows)"
        )
        try:
            connection.execute("BEGIN IMMEDIATE;")
            fail_if_global_non_cascading_references_are_unsafe(
                connection,
                "inside stale rollout_path repair transaction before delete",
            )
            stale_spawn_edges = collect_stale_thread_spawn_edges_to_delete(
                connection, stale_threads
            )
            fail_if_stale_threads_have_agent_job_items(connection, stale_threads)

            edge_deleted = 0
            for parent_thread_id, child_thread_id in stale_spawn_edges:
                cursor = connection.execute(
                    """
                DELETE FROM thread_spawn_edges
                WHERE parent_thread_id = ?
                  AND child_thread_id = ?;
                """,
                    (parent_thread_id, child_thread_id),
                )
                edge_deleted += cursor.rowcount
            if edge_deleted != len(stale_spawn_edges):
                raise RuntimeError(
                    f"deleted {edge_deleted} stale-to-stale thread_spawn_edges rows, "
                    f"expected {len(stale_spawn_edges)}"
                )

            deleted = 0
            for thread_id, rollout_path in stale_threads:
                if not rollout_path_is_missing(thread_id, rollout_path):
                    raise RuntimeError(
                        f"rollout_path appeared before delete for thread {thread_id}: {rollout_path}"
                    )
                cursor = connection.execute(
                    "DELETE FROM threads WHERE id = ? AND rollout_path = ?;",
                    (thread_id, rollout_path),
                )
                deleted += cursor.rowcount
            if deleted != len(stale_threads):
                raise RuntimeError(
                    f"deleted {deleted} stale thread rows, expected {len(stale_threads)}"
                )
            connection.commit()
        except BaseException as exc:
            try:
                connection.rollback()
            except Exception as rollback_exc:
                exc.add_note(
                    f"rollback failed after scoped Codex state DB transaction error: {rollback_exc!r}"
                )
            raise

        validate_integrity(connection, "after stale rollout_path repair")
        validate_foreign_keys(connection, "after stale rollout_path repair")
        validate_schema(connection)
        remaining_stale = missing_rollout_threads(connection)
        if remaining_stale:
            raise SystemExit(
                f"Scoped Codex state DB still has stale rollout_path rows after repair: {remaining_stale!r}"
            )
        fail_if_global_non_cascading_references_are_unsafe(
            connection,
            "after stale rollout_path repair",
        )
        if file_identity() != identity_before:
            raise SystemExit(f"Scoped Codex state DB identity changed during repair: {state_db}")
        if edge_deleted:
            print(
                f"Repaired scoped Codex state DB stale-to-stale thread_spawn_edges: "
                f"{state_db} ({edge_deleted} rows)"
            )
        print(f"Repaired scoped Codex state DB stale thread rows: {state_db} ({deleted} rows)")
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    """Build the Codex SQLite repair command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    log_parser = subparsers.add_parser("repair-log-db")
    log_parser.add_argument("log_db_path")

    state_parser = subparsers.add_parser("repair-state-db")
    state_parser.add_argument("state_db_path")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one managed Codex SQLite database repair."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "repair-log-db":
        sys.argv = [sys.argv[0], args.log_db_path]
        repair_log_db()
        return 0
    if args.command == "repair-state-db":
        sys.argv = [sys.argv[0], args.state_db_path]
        repair_state_db()
        return 0
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
