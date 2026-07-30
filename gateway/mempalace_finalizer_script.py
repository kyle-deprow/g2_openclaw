"""Write one canonical autoresearch decision record inside the MemPalace venv."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import NoReturn, Protocol, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from gateway.mempalace_finalizer import (  # noqa: E402
    FINAL_MEMORY_ADDED_BY,
    FINAL_MEMORY_SOURCE_FILE,
    finalization_journal_path,
)

FINAL_MEMORY_WING = "wing_quantipy"


class MempalaceServer(Protocol):
    def tool_list_drawers(self, *, wing: str, room: str, limit: int, offset: int) -> object: ...

    def tool_get_drawer(self, drawer_id: str) -> object: ...

    def tool_add_drawer(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source_file: str,
        added_by: str,
    ) -> object: ...

    def tool_kg_add(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        source_file: str,
        source_drawer_id: str,
    ) -> object: ...


class KnowledgeGraphReader(Protocol):
    def query_entity(self, entity: str, *, direction: str) -> object: ...


class SqliteKnowledgeGraphReader:
    """Read exact KG provenance that MemPalace's public query omits."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def query_entity(self, entity: str, *, direction: str) -> object:
        if direction != "outgoing":
            _fail("finalizer requires an outgoing MemPalace KG query")
        if not self._path.is_file():
            return []
        try:
            with sqlite3.connect(f"file:{self._path}?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    """
                    SELECT predicate, object, valid_to, source_file, source_drawer_id
                    FROM triples WHERE subject = ?
                    """,
                    (entity,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot read MemPalace KG provenance: {exc}") from exc
        return [
            {
                "predicate": str(row[0]),
                "object": str(row[1]),
                "valid_to": None if row[2] is None else str(row[2]),
                "current": row[2] is None,
                "source_file": None if row[3] is None else str(row[3]),
                "source_drawer_id": None if row[4] is None else str(row[4]),
            }
            for row in rows
        ]


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _load_request() -> tuple[str, str, dict[str, str]]:
    try:
        raw: object = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise RuntimeError("final-memory request must be JSON") from exc
    if not isinstance(raw, dict):
        _fail("final-memory request must be an object")
    experiment_id = raw.get("experiment_id")
    drawer_content = raw.get("drawer_content")
    facts = raw.get("facts")
    if not isinstance(experiment_id, str) or not experiment_id:
        _fail("final-memory request experiment_id must be a non-empty string")
    if not isinstance(drawer_content, str) or not drawer_content:
        _fail("final-memory request drawer_content must be a non-empty string")
    if not isinstance(facts, dict) or not facts:
        _fail("final-memory request facts must be a non-empty object")
    typed_facts: dict[str, str] = {}
    for predicate, object_value in facts.items():
        if not isinstance(predicate, str) or not isinstance(object_value, str):
            _fail("final-memory facts must map strings to strings")
        typed_facts[predicate] = object_value
    return experiment_id, drawer_content, typed_facts


def _assert_canonical_drawer(
    mcp_server: MempalaceServer,
    *,
    room: str,
    content: str,
) -> str:
    listed = mcp_server.tool_list_drawers(wing=FINAL_MEMORY_WING, room=room, limit=100, offset=0)
    if not isinstance(listed, dict):
        _fail("MemPalace drawer listing returned an invalid result")
    drawers = listed.get("drawers", [])
    if not isinstance(drawers, list):
        _fail("MemPalace drawer listing returned invalid drawers")
    if drawers:
        if len(drawers) != 1:
            _fail("conflicting existing final drawers")
        candidate = drawers[0]
        if not isinstance(candidate, dict):
            _fail("MemPalace drawer listing returned an invalid drawer")
        drawer_id = candidate.get("drawer_id")
        if not isinstance(drawer_id, str) or not drawer_id:
            _fail("MemPalace drawer listing omitted drawer_id")
        existing = mcp_server.tool_get_drawer(drawer_id)
        if not isinstance(existing, dict) or existing.get("content") != content:
            _fail("conflicting existing canonical final drawer")
        _assert_finalizer_drawer_provenance(existing, room=room)
        return drawer_id
    created = mcp_server.tool_add_drawer(
        wing=FINAL_MEMORY_WING,
        room=room,
        content=content,
        source_file=FINAL_MEMORY_SOURCE_FILE,
        added_by=FINAL_MEMORY_ADDED_BY,
    )
    if not isinstance(created, dict) or created.get("success") is not True:
        _fail(f"MemPalace final drawer write failed: {created!r}")
    drawer_id = created.get("drawer_id")
    if not isinstance(drawer_id, str) or not drawer_id:
        _fail("MemPalace final drawer write omitted drawer_id")
    existing = mcp_server.tool_get_drawer(drawer_id)
    if not isinstance(existing, dict) or existing.get("content") != content:
        _fail("MemPalace final drawer is not readable after write")
    _assert_finalizer_drawer_provenance(existing, room=room)
    return drawer_id


def _assert_finalizer_drawer_provenance(drawer: dict[object, object], *, room: str) -> None:
    metadata = drawer.get("metadata")
    if (
        drawer.get("wing") != FINAL_MEMORY_WING
        or drawer.get("room") != room
        or not isinstance(metadata, dict)
        or metadata.get("source_file") != FINAL_MEMORY_SOURCE_FILE
        or metadata.get("added_by") != FINAL_MEMORY_ADDED_BY
    ):
        _fail("existing canonical final drawer lacks exact finalizer provenance")


def _active_facts(
    kg: KnowledgeGraphReader,
    experiment_id: str,
    facts: dict[str, str],
    *,
    drawer_id: str,
) -> set[tuple[str, str]]:
    existing = kg.query_entity(experiment_id, direction="outgoing")
    if not isinstance(existing, list):
        _fail("MemPalace KG query returned an invalid result")
    active: set[tuple[str, str]] = set()
    for fact in existing:
        if not isinstance(fact, dict):
            _fail("MemPalace KG query returned an invalid fact")
        predicate = fact.get("predicate")
        object_value = fact.get("object")
        if fact.get("current") is False or fact.get("valid_to") is not None:
            continue
        if isinstance(predicate, str) and predicate in facts and object_value != facts[predicate]:
            _fail(f"conflicting active MemPalace fact: {predicate}")
        if (
            isinstance(predicate, str)
            and predicate in facts
            and (
                fact.get("source_file") != FINAL_MEMORY_SOURCE_FILE
                or fact.get("source_drawer_id") != drawer_id
            )
        ):
            _fail(f"active MemPalace fact lacks canonical finalizer provenance: {predicate}")
        if isinstance(predicate, str) and isinstance(object_value, str):
            active.add((predicate, object_value))
    return active


def _request_digest(*, experiment_id: str, drawer_content: str, facts: dict[str, str]) -> str:
    canonical = json.dumps(
        {"drawer_content": drawer_content, "experiment_id": experiment_id, "facts": facts},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_journal(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8") as journal:
            json.dump(payload, journal, separators=(",", ":"), sort_keys=True)
            journal.flush()
            os.fsync(journal.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _start_or_resume_journal(
    *,
    palace_path: Path,
    experiment_id: str,
    drawer_content: str,
    facts: dict[str, str],
) -> Path:
    journal_path = finalization_journal_path(palace_path, experiment_id)
    request_digest = _request_digest(
        experiment_id=experiment_id,
        drawer_content=drawer_content,
        facts=facts,
    )
    if journal_path.exists():
        try:
            existing: object = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("final-memory journal is unreadable") from exc
        if not isinstance(existing, dict):
            _fail("final-memory journal is invalid")
        if existing.get("request_sha256") != request_digest:
            _fail("final-memory journal conflicts with the requested canonical decision")
        if existing.get("status") not in {"pending", "committed"}:
            _fail("final-memory journal has an invalid status")
        return journal_path
    _write_journal(
        journal_path,
        {"request_sha256": request_digest, "status": "pending"},
    )
    return journal_path


def _commit_journal(
    path: Path,
    *,
    experiment_id: str,
    drawer_content: str,
    facts: dict[str, str],
    drawer_id: str,
) -> None:
    _write_journal(
        path,
        {
            "request_sha256": _request_digest(
                experiment_id=experiment_id,
                drawer_content=drawer_content,
                facts=facts,
            ),
            "status": "committed",
            "drawer_id": drawer_id,
        },
    )


def complete_finalization(
    mcp_server: MempalaceServer,
    kg: KnowledgeGraphReader,
    *,
    palace_path: Path,
    experiment_id: str,
    drawer_content: str,
    facts: dict[str, str],
) -> None:
    """Complete a retryable cross-store finalization under the MemPalace lease."""
    journal_path = _start_or_resume_journal(
        palace_path=palace_path,
        experiment_id=experiment_id,
        drawer_content=drawer_content,
        facts=facts,
    )
    room = f"room_{experiment_id}"
    drawer_id = _assert_canonical_drawer(mcp_server, room=room, content=drawer_content)
    active_facts = _active_facts(kg, experiment_id, facts, drawer_id=drawer_id)
    for predicate, object_value in sorted(facts.items()):
        if (predicate, object_value) in active_facts:
            continue
        result = mcp_server.tool_kg_add(
            subject=experiment_id,
            predicate=predicate,
            object=object_value,
            source_file=FINAL_MEMORY_SOURCE_FILE,
            source_drawer_id=drawer_id,
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            _fail(f"MemPalace final fact write failed for {predicate}: {result!r}")
    committed_facts = _active_facts(kg, experiment_id, facts, drawer_id=drawer_id)
    missing = sorted(
        predicate
        for predicate, object_value in facts.items()
        if (predicate, object_value) not in committed_facts
    )
    if missing:
        _fail("MemPalace final facts are not readable after write: " + ", ".join(missing))
    _commit_journal(
        journal_path,
        experiment_id=experiment_id,
        drawer_content=drawer_content,
        facts=facts,
        drawer_id=drawer_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", required=True)
    args = parser.parse_args()
    experiment_id, drawer_content, facts = _load_request()

    # The imported MCP module owns the supported drawer storage semantics.
    sys.argv = [sys.argv[0], "--palace", args.palace]
    from mempalace import mcp_server as raw_mcp_server  # type: ignore[import-not-found]
    from mempalace.palace import (  # type: ignore[import-not-found]
        MineAlreadyRunning,
        mine_palace_lock,
    )

    mcp_server = cast(MempalaceServer, raw_mcp_server)

    restore_stdout = getattr(mcp_server, "_restore_stdout", None)
    if not callable(restore_stdout):
        _fail("MemPalace MCP stdout restoration hook is unavailable")
    restore_stdout()

    palace_path = Path(args.palace).expanduser().resolve()
    kg_path = palace_path / "knowledge_graph.sqlite3"
    try:
        with mine_palace_lock(str(palace_path)):
            complete_finalization(
                mcp_server,
                SqliteKnowledgeGraphReader(kg_path),
                palace_path=palace_path,
                experiment_id=experiment_id,
                drawer_content=drawer_content,
                facts=facts,
            )
    except MineAlreadyRunning as exc:
        raise RuntimeError(
            f"MemPalace finalizer could not acquire the palace writer lease: {exc}"
        ) from exc
    print(json.dumps({"kg_path": str(kg_path)}, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
