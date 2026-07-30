from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_ADDED_BY,
    FINAL_MEMORY_SOURCE_FILE,
    FinalMemoryWriteRequest,
    MempalaceFinalizationError,
    SubprocessFinalMemoryWriter,
)
from gateway.mempalace_finalizer_script import complete_finalization

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMPALACE_PYTHON = Path.home() / ".local/share/mempalace/venv/bin/python"
FINALIZER_SCRIPT = REPO_ROOT / "gateway/mempalace_finalizer_script.py"


class _FinalizationRequest(TypedDict):
    experiment_id: str
    drawer_content: str
    facts: dict[str, str]


class _FakeMempalaceServer:
    def __init__(self, *, fail_predicate: str | None = None) -> None:
        self.drawers: dict[str, dict[str, object]] = {}
        self.facts: list[dict[str, object]] = []
        self.fail_predicate = fail_predicate

    def tool_list_drawers(self, *, wing: str, room: str, limit: int, offset: int) -> object:
        del wing, limit, offset
        return {
            "drawers": [
                {"drawer_id": drawer_id}
                for drawer_id, drawer in self.drawers.items()
                if drawer["room"] == room
            ]
        }

    def tool_get_drawer(self, drawer_id: str) -> object:
        return self.drawers[drawer_id]

    def tool_add_drawer(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source_file: str,
        added_by: str,
    ) -> object:
        drawer_id = f"drawer-{room}"
        self.drawers[drawer_id] = {
            "drawer_id": drawer_id,
            "content": content,
            "wing": wing,
            "room": room,
            "metadata": {"source_file": source_file, "added_by": added_by},
        }
        return {"success": True, "drawer_id": drawer_id}

    def tool_kg_add(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        source_file: str,
        source_drawer_id: str,
    ) -> object:
        if predicate == self.fail_predicate:
            return {"success": False, "error": "injected KG failure"}
        self.facts.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": object,
                "source_file": source_file,
                "source_drawer_id": source_drawer_id,
                "current": True,
                "valid_to": None,
            }
        )
        return {"success": True}


class _FakeKnowledgeGraph:
    def __init__(self, server: _FakeMempalaceServer) -> None:
        self._server = server

    def query_entity(self, entity: str, *, direction: str) -> object:
        del direction
        return [fact for fact in self._server.facts if fact["subject"] == entity]


def test_finalizer_rejects_identical_model_fact_without_finalizer_provenance(
    tmp_path: Path,
) -> None:
    # Arrange
    server = _FakeMempalaceServer()
    server.facts.append(
        {
            "subject": "iteration-94",
            "predicate": "decision",
            "object": "discard",
            "source_file": "model-turn.json",
            "source_drawer_id": "drawer-model",
            "current": True,
            "valid_to": None,
        }
    )

    # Act / Assert
    with pytest.raises(RuntimeError, match="finalizer provenance"):
        complete_finalization(
            server,
            _FakeKnowledgeGraph(server),
            palace_path=tmp_path,
            experiment_id="iteration-94",
            drawer_content='{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
            facts={"decision": "discard"},
        )


def test_finalizer_retries_pending_cross_store_write_without_committing_partial_state(
    tmp_path: Path,
) -> None:
    # Arrange
    server = _FakeMempalaceServer(fail_predicate="research_mode")
    kg = _FakeKnowledgeGraph(server)
    request: _FinalizationRequest = {
        "experiment_id": "iteration-95",
        "drawer_content": '{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
        "facts": {"decision": "discard", "research_mode": "alpha_research"},
    }

    # Act
    with pytest.raises(RuntimeError, match="research_mode"):
        complete_finalization(server, kg, palace_path=tmp_path, **request)
    server.fail_predicate = None
    complete_finalization(server, kg, palace_path=tmp_path, **request)

    # Assert
    journal_path = tmp_path / ".g2-openclaw-finalizations" / "iteration-95.json"
    journal = json.loads(journal_path.read_text())
    assert journal["status"] == "committed"
    written_facts = {
        (fact["predicate"], fact["source_file"], fact["source_drawer_id"]) for fact in server.facts
    }
    assert written_facts == {
        ("decision", FINAL_MEMORY_SOURCE_FILE, "drawer-room_iteration-95"),
        ("research_mode", FINAL_MEMORY_SOURCE_FILE, "drawer-room_iteration-95"),
    }


def test_finalizer_rejects_existing_drawer_without_exact_finalizer_provenance(
    tmp_path: Path,
) -> None:
    # Arrange
    server = _FakeMempalaceServer()
    server.drawers["drawer-room_iteration-96"] = {
        "drawer_id": "drawer-room_iteration-96",
        "content": '{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
        "wing": "wing_quantipy",
        "room": "room_iteration-96",
        "metadata": {"source_file": "model-turn.json", "added_by": FINAL_MEMORY_ADDED_BY},
    }

    # Act / Assert
    with pytest.raises(RuntimeError, match="finalizer provenance"):
        complete_finalization(
            server,
            _FakeKnowledgeGraph(server),
            palace_path=tmp_path,
            experiment_id="iteration-96",
            drawer_content='{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
            facts={"decision": "discard"},
        )


def test_subprocess_finalizer_rejects_extra_output_fields(tmp_path: Path) -> None:
    script = tmp_path / "finalizer.py"
    script.write_text(
        "import json\nprint(json.dumps({'kg_path':'%s','extra':True}))\n"
        % (tmp_path / "knowledge_graph.sqlite3"),
        encoding="utf-8",
    )
    writer = SubprocessFinalMemoryWriter(
        python_executable=Path(sys.executable),
        script_path=script,
        palace_path=tmp_path,
    )

    with pytest.raises(MempalaceFinalizationError, match="unexpected fields"):
        writer.write(
            FinalMemoryWriteRequest(
                experiment_id="iteration-1",
                drawer_content="content",
                facts={"decision": "discard"},
            )
        )


def test_subprocess_finalizer_rejects_unexpected_kg_path(tmp_path: Path) -> None:
    script = tmp_path / "finalizer.py"
    script.write_text(
        "import json\nprint(json.dumps({'kg_path':'/tmp/knowledge_graph.sqlite3'}))\n",
        encoding="utf-8",
    )
    writer = SubprocessFinalMemoryWriter(
        python_executable=Path(sys.executable),
        script_path=script,
        palace_path=tmp_path,
    )

    with pytest.raises(MempalaceFinalizationError, match="unexpected kg_path"):
        writer.write(
            FinalMemoryWriteRequest(
                experiment_id="iteration-1",
                drawer_content="content",
                facts={"decision": "discard"},
            )
        )


def _seed_kg_fact(
    palace_path: Path,
    *,
    subject: str,
    predicate: str,
    object_value: str,
    valid_to: str | None = None,
) -> None:
    script = """
import sys
from pathlib import Path
from mempalace.knowledge_graph import KnowledgeGraph

palace = Path(sys.argv[1])
subject = sys.argv[2]
predicate = sys.argv[3]
object_value = sys.argv[4]
valid_to = sys.argv[5] or None
with KnowledgeGraph(str(palace / "knowledge_graph.sqlite3")) as kg:
    kg.add_triple(subject, predicate, object_value, valid_to=valid_to, source_file="seed.json")
"""
    subprocess.run(
        [
            str(MEMPALACE_PYTHON),
            "-c",
            script,
            str(palace_path),
            subject,
            predicate,
            object_value,
            valid_to or "",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_finalizer_subprocess_writes_once_and_returns_a_parseable_receipt(tmp_path: Path) -> None:
    request = {
        "drawer_content": '{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
        "experiment_id": "iteration-91",
        "facts": {"decision": "discard", "research_mode": "alpha_research"},
    }
    command = [str(MEMPALACE_PYTHON), str(FINALIZER_SCRIPT), "--palace", str(tmp_path)]

    first = subprocess.run(
        command,
        input=json.dumps(request),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    second = subprocess.run(
        command,
        input=json.dumps(request),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"kg_path": str(tmp_path / "knowledge_graph.sqlite3")}
    assert second.returncode == 0, second.stderr
    connection = sqlite3.connect(tmp_path / "knowledge_graph.sqlite3")
    rows = connection.execute(
        "SELECT predicate, object FROM triples WHERE subject = ? AND valid_to IS NULL",
        ("iteration-91",),
    ).fetchall()
    connection.close()
    assert sorted(rows) == [("decision", "discard"), ("research_mode", "alpha_research")]
    assert not (tmp_path / "g2-openclaw-autoresearch-finalizer.lock").exists()
    assert (
        json.loads((tmp_path / ".g2-openclaw-finalizations" / "iteration-91.json").read_text())[
            "status"
        ]
        == "committed"
    )


def test_finalizer_ignores_historical_fact_conflicts(tmp_path: Path) -> None:
    _seed_kg_fact(
        tmp_path,
        subject="iteration-92",
        predicate="decision",
        object_value="keep",
        valid_to="2026-01-01",
    )
    request = {
        "drawer_content": '{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
        "experiment_id": "iteration-92",
        "facts": {"decision": "discard"},
    }

    result = subprocess.run(
        [str(MEMPALACE_PYTHON), str(FINALIZER_SCRIPT), "--palace", str(tmp_path)],
        input=json.dumps(request),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(tmp_path / "knowledge_graph.sqlite3")
    rows = connection.execute(
        "SELECT predicate, object FROM triples WHERE subject = ? AND valid_to IS NULL",
        ("iteration-92",),
    ).fetchall()
    connection.close()
    assert rows == [("decision", "discard")]


def test_finalizer_rejects_active_fact_conflicts_without_stdout_noise(tmp_path: Path) -> None:
    _seed_kg_fact(
        tmp_path,
        subject="iteration-93",
        predicate="decision",
        object_value="keep",
    )
    request = {
        "drawer_content": '{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
        "experiment_id": "iteration-93",
        "facts": {"decision": "discard"},
    }

    result = subprocess.run(
        [str(MEMPALACE_PYTHON), str(FINALIZER_SCRIPT), "--palace", str(tmp_path)],
        input=json.dumps(request),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "conflicting active MemPalace fact: decision" in result.stderr


def test_finalizer_refuses_to_write_while_the_mempalace_palace_lease_is_held(
    tmp_path: Path,
) -> None:
    locker = subprocess.Popen(
        [
            str(MEMPALACE_PYTHON),
            "-c",
            (
                "import sys\n"
                "from mempalace.palace import mine_palace_lock\n"
                "with mine_palace_lock(sys.argv[1]):\n"
                "    print('locked', flush=True)\n"
                "    sys.stdin.read()\n"
            ),
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert locker.stdout is not None
    assert locker.stdin is not None
    assert locker.stdout.readline().strip() == "locked"

    try:
        result = subprocess.run(
            [str(MEMPALACE_PYTHON), str(FINALIZER_SCRIPT), "--palace", str(tmp_path)],
            input=json.dumps(
                {
                    "drawer_content": '{"schema":"g2-openclaw.autoresearch.final-memory.v1"}',
                    "experiment_id": "iteration-97",
                    "facts": {"decision": "discard"},
                }
            ),
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
    finally:
        locker.stdin.close()
        locker.wait(timeout=30)

    assert result.returncode == 1
    assert "could not acquire the palace writer lease" in result.stderr
