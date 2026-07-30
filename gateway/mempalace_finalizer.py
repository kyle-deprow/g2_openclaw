"""Narrow, platform-owned subprocess boundary for final MemPalace writes."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

FINAL_MEMORY_SOURCE_FILE = "g2-openclaw-autoresearch-finalizer"
FINAL_MEMORY_ADDED_BY = "g2-openclaw-finalizer"
FINALIZATION_JOURNAL_DIRECTORY = ".g2-openclaw-finalizations"


class MempalaceFinalizationError(RuntimeError):
    """Raised when the isolated MemPalace finalizer cannot prove a write."""


@dataclass(frozen=True)
class FinalMemoryWriteRequest:
    """The complete, state-derived final-memory record passed to MemPalace."""

    experiment_id: str
    drawer_content: str
    facts: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "drawer_content": self.drawer_content,
            "experiment_id": self.experiment_id,
            "facts": self.facts,
        }


class FinalMemoryWriter(Protocol):
    """Writes exactly one canonical final record for a validated decision."""

    def write(self, request: FinalMemoryWriteRequest) -> Path: ...


def finalization_journal_path(palace_path: Path, experiment_id: str) -> Path:
    """Return the durable platform commit record for one final decision."""
    if not experiment_id or Path(experiment_id).name != experiment_id:
        raise MempalaceFinalizationError("final-memory experiment_id is unsafe for a journal path")
    return palace_path / FINALIZATION_JOURNAL_DIRECTORY / f"{experiment_id}.json"


@dataclass(frozen=True)
class SubprocessFinalMemoryWriter:
    """Run the finalizer inside the separately installed MemPalace venv."""

    python_executable: Path
    script_path: Path
    palace_path: Path
    timeout_seconds: int = 120

    @classmethod
    def from_environment(cls, *, repository_root: Path) -> SubprocessFinalMemoryWriter:
        palace_path = Path(
            os.environ.get("MEMPALACE_PALACE", str(Path.home() / ".mempalace/palace"))
        ).expanduser()
        return cls(
            python_executable=Path.home() / ".local/share/mempalace/venv/bin/python",
            script_path=repository_root / "gateway/mempalace_finalizer_script.py",
            palace_path=palace_path,
        )

    def write(self, request: FinalMemoryWriteRequest) -> Path:
        if not self.python_executable.is_file():
            raise MempalaceFinalizationError(
                f"MemPalace Python is unavailable: {self.python_executable}"
            )
        if not self.script_path.is_file():
            raise MempalaceFinalizationError(
                f"MemPalace finalizer script is unavailable: {self.script_path}"
            )
        try:
            completed = subprocess.run(
                [
                    str(self.python_executable),
                    str(self.script_path),
                    "--palace",
                    str(self.palace_path),
                ],
                input=json.dumps(request.to_dict(), separators=(",", ":"), sort_keys=True),
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except OSError as exc:
            raise MempalaceFinalizationError(f"cannot start MemPalace finalizer: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MempalaceFinalizationError("MemPalace finalizer timed out") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no error output"
            raise MempalaceFinalizationError(f"MemPalace finalizer failed: {detail}")
        try:
            raw: object = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise MempalaceFinalizationError("MemPalace finalizer returned invalid JSON") from exc
        if not isinstance(raw, dict):
            raise MempalaceFinalizationError("MemPalace finalizer returned a non-object result")
        if set(raw) != {"kg_path"}:
            raise MempalaceFinalizationError("MemPalace finalizer returned unexpected fields")
        kg_path = raw.get("kg_path")
        if not isinstance(kg_path, str) or not kg_path:
            raise MempalaceFinalizationError("MemPalace finalizer did not return kg_path")
        returned_path = Path(kg_path).expanduser()
        expected_path = self.palace_path.expanduser().resolve() / "knowledge_graph.sqlite3"
        try:
            resolved_returned = returned_path.resolve(strict=False)
        except RuntimeError as exc:
            raise MempalaceFinalizationError("MemPalace finalizer returned unsafe kg_path") from exc
        if resolved_returned != expected_path:
            raise MempalaceFinalizationError("MemPalace finalizer returned unexpected kg_path")
        return expected_path
