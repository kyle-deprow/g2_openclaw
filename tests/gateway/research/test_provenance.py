from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from gateway.research.containment import (
    PROVENANCE_RECORDER_SOURCE,
    RuntimePins,
    recorder_sha256,
)
from gateway.research.contracts import RunPlan
from gateway.research.provenance import (
    ProvenanceError,
    validate_provenance_evidence,
    verify_stage_provenance,
)
from gateway.research.store import ResearchStore

from tests.gateway.research.test_containment import _runtime


def _git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)


def _commit(path: Path) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def _record(
    stage: str,
    *,
    snapshot: Path,
    work: Path,
    include_quantipy: bool = True,
) -> dict[str, object]:
    modules: list[dict[str, object]] = [
        {
            "name": "strategy",
            "path": "/work/strategy.py",
            "origin": "work",
            "sha256": hashlib.sha256((work / "strategy.py").read_bytes()).hexdigest(),
        },
        {
            "name": "sitecustomize",
            "path": "/provenance/sitecustomize.py",
            "origin": "recorder",
            "sha256": recorder_sha256(),
        },
        {"name": "json", "path": "/usr/lib/python3.13/json/__init__.py", "origin": "runtime"},
    ]
    if include_quantipy:
        modules.append(
            {
                "name": "quantipy",
                "path": "/snapshot/src/quantipy/__init__.py",
                "origin": "snapshot",
                "sha256": hashlib.sha256(
                    (snapshot / "src" / "quantipy" / "__init__.py").read_bytes()
                ).hexdigest(),
            }
        )
    return {
        "contract": "research-containment-provenance-v1",
        "stage": stage,
        "pid": 101,
        "ppid": 1,
        "argv": ["/venv/bin/python", "-m", "strategy"],
        "executable": "/venv/bin/python",
        "cwd": "/work",
        "python_version": "3.13.0",
        "flags": {"safe_path": True, "no_user_site": True},
        "pythonpath": "/provenance:/snapshot/src:/work",
        "sys_path": ["/provenance", "/snapshot/src", "/work"],
        "recorder": {
            "path": "/provenance/sitecustomize.py",
            "sha256": recorder_sha256(),
        },
        "modules": modules,
        "written_at": "2026-09-22T00:00:00Z",
    }


def _fixture(tmp_path: Path) -> tuple[RuntimePins, Path, str, Path, dict[str, object]]:
    pins, snapshot, _evaluator, _universe, _venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    _git_repo(worktree)
    (worktree / "strategy.py").write_text("VALUE = 1\n")
    commit = _commit(worktree)
    records = tmp_path / "records"
    records.mkdir()
    payload = _record("evaluate-s000", snapshot=snapshot, work=worktree)
    (records / "evaluate.json").write_text(json.dumps(payload, sort_keys=True))
    return pins, worktree, commit, records, payload


def test_verify_stage_provenance_accepts_matching_snapshot_and_git_bytes(tmp_path: Path) -> None:
    pins, worktree, commit, records, _payload = _fixture(tmp_path)
    summary = verify_stage_provenance(
        records,
        stage="evaluate-s000",
        pins=pins,
        worktree=worktree,
        commit=commit,
        work_top_levels=frozenset({"strategy"}),
        require_quantipy=True,
        require_interpreter_flags=True,
    )
    assert summary["stage"] == "evaluate-s000"
    assert summary["record_count"] == 1
    assert summary["snapshot_modules"] == ["quantipy"]
    assert summary["work_modules"] == ["strategy"]
    assert summary["pids"] == [101]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("snapshot_hash", "snapshot module hash"),
        ("work_hash", "work module hash"),
        ("quantipy_runtime", "quantipy module did not"),
        ("recorder_hash", "recorder hash"),
        ("stage", "stage does not match"),
        ("safe_path", "safety flags"),
        ("missing_quantipy", "no quantipy"),
    ],
)
def test_verify_stage_provenance_rejects_mismatches(
    tmp_path: Path, mutation: str, message: str
) -> None:
    pins, worktree, commit, records, payload = _fixture(tmp_path)
    modules = payload["modules"]
    assert isinstance(modules, list)
    if mutation == "snapshot_hash":
        next(item for item in modules if item["origin"] == "snapshot")["sha256"] = "0" * 64
    elif mutation == "work_hash":
        next(item for item in modules if item["origin"] == "work")["sha256"] = "0" * 64
    elif mutation == "quantipy_runtime":
        next(item for item in modules if item["name"] == "quantipy")["origin"] = "runtime"
    elif mutation == "recorder_hash":
        payload["recorder"]["sha256"] = "0" * 64  # type: ignore[index]
    elif mutation == "stage":
        payload["stage"] = "targets-s000"
    elif mutation == "safe_path":
        payload["flags"]["safe_path"] = False  # type: ignore[index]
    elif mutation == "missing_quantipy":
        payload["modules"] = [item for item in modules if item["name"] != "quantipy"]
    (records / "evaluate.json").write_text(json.dumps(payload, sort_keys=True))
    with pytest.raises(ProvenanceError, match=message):
        verify_stage_provenance(
            records,
            stage="evaluate-s000",
            pins=pins,
            worktree=worktree,
            commit=commit,
            work_top_levels=frozenset({"strategy"}),
            require_quantipy=True,
            require_interpreter_flags=True,
        )


def test_verify_stage_provenance_rejects_untracked_work_and_extra_entries(tmp_path: Path) -> None:
    pins, worktree, commit, records, payload = _fixture(tmp_path)
    (worktree / "untracked.py").write_text("VALUE = 2\n")
    modules = payload["modules"]
    assert isinstance(modules, list)
    modules.append(
        {
            "name": "untracked",
            "path": "/work/untracked.py",
            "origin": "work",
            "sha256": hashlib.sha256((worktree / "untracked.py").read_bytes()).hexdigest(),
        }
    )
    (records / "evaluate.json").write_text(json.dumps(payload, sort_keys=True))
    with pytest.raises(ProvenanceError, match="not tracked"):
        verify_stage_provenance(
            records,
            stage="evaluate-s000",
            pins=pins,
            worktree=worktree,
            commit=commit,
            work_top_levels=frozenset({"strategy"}),
            require_quantipy=True,
            require_interpreter_flags=True,
        )
    (records / "unexpected.txt").write_text("nope")
    with pytest.raises(ProvenanceError, match="unexpected entry"):
        verify_stage_provenance(
            records,
            stage="evaluate-s000",
            pins=pins,
            worktree=worktree,
            commit=commit,
            work_top_levels=frozenset({"strategy"}),
            require_quantipy=True,
            require_interpreter_flags=True,
        )


def test_verify_stage_provenance_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    pins, worktree, commit, records, _payload = _fixture(tmp_path)
    (records / "evaluate.json").write_text('{"contract":"x","contract":"y"}')
    with pytest.raises(ProvenanceError, match="duplicate JSON key"):
        verify_stage_provenance(
            records,
            stage="evaluate-s000",
            pins=pins,
            worktree=worktree,
            commit=commit,
            work_top_levels=frozenset(),
            require_quantipy=False,
            require_interpreter_flags=True,
        )


def test_verify_stage_provenance_targets_report_disabled_interpreter_flag(
    tmp_path: Path,
) -> None:
    pins, worktree, commit, records, payload = _fixture(tmp_path)
    payload["stage"] = "targets-s000"
    flags = payload["flags"]
    assert isinstance(flags, dict)
    flags["safe_path"] = False
    (records / "evaluate.json").write_text(json.dumps(payload, sort_keys=True))

    summary = verify_stage_provenance(
        records,
        stage="targets-s000",
        pins=pins,
        worktree=worktree,
        commit=commit,
        work_top_levels=frozenset({"strategy"}),
        require_quantipy=False,
        require_interpreter_flags=False,
    )

    assert summary["interpreter_flags"] == {
        "safe_path": False,
        "no_user_site": True,
    }
    with pytest.raises(ProvenanceError, match="safety flags"):
        verify_stage_provenance(
            records,
            stage="targets-s000",
            pins=pins,
            worktree=worktree,
            commit=commit,
            work_top_levels=frozenset({"strategy"}),
            require_quantipy=False,
            require_interpreter_flags=True,
        )


def test_sitecustomize_records_runtime_modules_and_requires_environment(tmp_path: Path) -> None:
    records = tmp_path / "records"
    records.mkdir()
    fake_module = tmp_path / "fake_module.py"
    fake_module.write_text("VALUE = 1\n")
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": f"{PROVENANCE_RECORDER_SOURCE}:{tmp_path}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RESEARCH_PROVENANCE_DIR": str(records),
            "RESEARCH_PROVENANCE_STAGE": "analysis",
        }
    )
    subprocess.run(
        [sys.executable, "-c", "import fake_module"], env=env, check=True, capture_output=True
    )
    files = list(records.glob("*.json"))
    assert len(files) == 1
    value = json.loads(files[0].read_text())
    assert value["contract"] == "research-containment-provenance-v1"
    assert (
        next(item for item in value["modules"] if item["name"] == "fake_module")["origin"]
        == "runtime"
    )
    assert value["recorder"]["sha256"] == recorder_sha256()
    env.pop("RESEARCH_PROVENANCE_DIR")
    env.pop("RESEARCH_PROVENANCE_STAGE")
    subprocess.run([sys.executable, "-c", "import fake_module"], env=env, check=True)
    assert list(records.glob("*.json")) == files


class _FakeStore:
    def __init__(self, attempt: object, config: Mapping[str, object]) -> None:
        self._attempt = attempt
        self._config = config

    def get_attempt(self, _attempt_id: str) -> object:
        return self._attempt

    def config(self) -> Mapping[str, object]:
        return self._config


def _evidence_fixture(
    tmp_path: Path, *, evaluate_safe_path: bool = True
) -> tuple[_FakeStore, object, object, Path, str]:
    pins, snapshot, evaluator, universe, venv = _runtime(tmp_path)
    worktree = tmp_path / "worktree"
    _git_repo(worktree)
    (worktree / "strategy.py").write_text("VALUE = 1\n")
    (worktree / "analysis.py").write_text("VALUE = 1\n")
    records: dict[str, Path] = {}
    for stage in ("targets-s000", "evaluate-s000", "analysis"):
        directory = worktree / "evidence" / stage
        directory.mkdir(parents=True)
        record = _record(stage, snapshot=snapshot, work=worktree)
        if stage == "evaluate-s000" and not evaluate_safe_path:
            flags = record["flags"]
            assert isinstance(flags, dict)
            flags["safe_path"] = False
        if stage == "analysis":
            modules = record["modules"]
            assert isinstance(modules, list)
            modules.append(
                {
                    "name": "analysis",
                    "path": "/work/analysis.py",
                    "origin": "work",
                    "sha256": hashlib.sha256((worktree / "analysis.py").read_bytes()).hexdigest(),
                }
            )
        (directory / "record.json").write_text(json.dumps(record, sort_keys=True))
        records[stage] = directory
    commit = _commit(worktree)
    attempt_id = "H0001-A001"
    attempt = SimpleNamespace(attempt_id=attempt_id, worktree_path=str(worktree))
    config = {
        "snapshot_dir": str(pins.snapshot_dir),
        "snapshot_sha256": pins.snapshot_sha256,
        "shared_python": str(pins.shared_python),
        "shared_python_resolved": str(pins.resolved_python),
        "shared_python_sha256": pins.shared_python_sha256,
        "pyvenv_cfg": str(pins.pyvenv_cfg),
        "pyvenv_sha256": pins.pyvenv_sha256,
        "distribution_dir": str(pins.distribution_dir),
        "evaluator": str(evaluator),
        "evaluator_sha256": pins.evaluator_sha256,
        "universe": str(universe),
        "universe_sha256": pins.universe_sha256,
    }
    plan = SimpleNamespace(
        attempt_id=attempt_id,
        commit=commit,
        analysis=SimpleNamespace(module="analysis"),
        scenarios=(SimpleNamespace(targets_argv=(str(venv / "bin" / "python"), "-m", "strategy")),),
    )
    index = tmp_path / "provenance.json"
    index.write_text(
        json.dumps(
            {
                "contract": "research-provenance-evidence-v1",
                "attempt_id": attempt_id,
                "commit": commit,
                "stages": [
                    {"stage": stage, "records_dir": str(records[stage])}
                    for stage in ("targets-s000", "evaluate-s000", "analysis")
                ],
            },
            sort_keys=True,
        )
    )
    return _FakeStore(attempt, config), plan, attempt, index, commit


def test_validate_provenance_evidence_returns_canonical_verified_summary(tmp_path: Path) -> None:
    store, plan, _attempt, index, commit = _evidence_fixture(tmp_path)
    value = validate_provenance_evidence(
        cast(ResearchStore, store), "H0001-A001", cast(RunPlan, plan), index
    )
    parsed = json.loads(value)
    assert parsed["contract"] == "research-provenance-evidence-v1"
    assert parsed["commit"] == commit
    assert [item["stage"] for item in parsed["stages"]] == [
        "targets-s000",
        "evaluate-s000",
        "analysis",
    ]


def test_validate_provenance_evidence_rejects_disabled_evaluate_interpreter_flag(
    tmp_path: Path,
) -> None:
    store, plan, _attempt, index, _commit_value = _evidence_fixture(
        tmp_path, evaluate_safe_path=False
    )

    with pytest.raises(ProvenanceError, match="safety flags"):
        validate_provenance_evidence(
            cast(ResearchStore, store), "H0001-A001", cast(RunPlan, plan), index
        )


@pytest.mark.parametrize("failure", ["missing_analysis", "outside", "untracked", "commit"])
def test_validate_provenance_evidence_rejects_index_bindings(tmp_path: Path, failure: str) -> None:
    store, plan, _attempt, index, _commit_value = _evidence_fixture(tmp_path)
    value = json.loads(index.read_text())
    if failure == "missing_analysis":
        value["stages"] = value["stages"][:2]
    elif failure == "outside":
        value["stages"][0]["records_dir"] = str(tmp_path / "outside")
    elif failure == "untracked":
        records_dir = Path(value["stages"][0]["records_dir"])
        (records_dir / "new.json").write_text((records_dir / "record.json").read_text())
    else:
        value["commit"] = "0" * 40
    index.write_text(json.dumps(value, sort_keys=True))
    with pytest.raises(ProvenanceError):
        validate_provenance_evidence(
            cast(ResearchStore, store), "H0001-A001", cast(RunPlan, plan), index
        )
