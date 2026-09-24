"""Host-side verification of in-sandbox import provenance."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .codec import to_json
from .containment import (
    PROVENANCE_CONTRACT,
    ContainmentError,
    RuntimePins,
    _require_stage,
    recorder_sha256,
    runtime_pins_from_record,
    verify_runtime_pins,
)

if TYPE_CHECKING:
    from .contracts import RunPlan
    from .store import ResearchStore


class ProvenanceError(RuntimeError):
    """The recorded import provenance is absent, malformed, or mismatched."""


MAX_RECORD_BYTES = 8 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 1 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_STAGE = re.compile(r"^(?:targets-s\d{3}|evaluate-s\d{3}|analysis)$")


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ProvenanceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_json(path: Path, limit: int, label: str) -> dict[str, object]:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ProvenanceError(f"{label} is missing or unreadable: {path}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_size > limit:
        raise ProvenanceError(f"{label} must be a bounded regular non-symlink file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProvenanceError(f"{label} could not be read: {path}") from exc
    if len(raw) > limit:
        raise ProvenanceError(f"{label} exceeds its size limit: {path}")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object)
    except ProvenanceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(parsed, dict):
        raise ProvenanceError(f"{label} must contain a JSON object: {path}")
    return cast(dict[str, object], parsed)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProvenanceError(f"{label} must be a non-empty string")
    return value


def _digest(value: object, label: str) -> str:
    result = _text(value, label)
    if _SHA256.fullmatch(result) is None:
        raise ProvenanceError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _relative_virtual_path(value: str, prefix: str, label: str) -> str:
    if not value.startswith(prefix):
        raise ProvenanceError(f"{label} is outside {prefix}: {value}")
    relative = value.removeprefix(prefix)
    path = Path(relative)
    if not relative or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProvenanceError(f"{label} is not a safe relative path: {value}")
    return path.as_posix()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, label: str) -> str:
    try:
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise ProvenanceError(f"{label} must be a regular non-symlink file: {path}")
        return _sha256_bytes(path.read_bytes())
    except ProvenanceError:
        raise
    except OSError as exc:
        raise ProvenanceError(f"{label} could not be read: {path}") from exc


def _git_blob(worktree: Path, commit: str, relative: str, label: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "blob", f"{commit}:{relative}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ProvenanceError(f"{label} is not tracked at commit: {relative}")
    return result.stdout


def _record_files(records_dir: Path) -> list[Path]:
    try:
        directory = records_dir.lstat()
        entries = sorted(records_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ProvenanceError(
            f"provenance records directory is unavailable: {records_dir}"
        ) from exc
    if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
        raise ProvenanceError(f"provenance records directory must be real: {records_dir}")
    if not entries:
        raise ProvenanceError("provenance records directory contains no records")
    files: list[Path] = []
    for path in entries:
        try:
            value = path.lstat()
        except OSError as exc:
            raise ProvenanceError(f"provenance record entry is unreadable: {path}") from exc
        if path.suffix != ".json" or stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise ProvenanceError(
                f"provenance records directory contains an unexpected entry: {path}"
            )
        if value.st_size > MAX_RECORD_BYTES:
            raise ProvenanceError(f"provenance record exceeds its size limit: {path}")
        files.append(path)
    return files


def _verify_stage_provenance(
    records_dir: Path,
    *,
    stage: str,
    pins: RuntimePins,
    worktree: Path,
    commit: str,
    work_top_levels: frozenset[str],
    require_quantipy: bool,
    require_interpreter_flags: bool,
) -> dict[str, object]:
    try:
        _require_stage(stage)
    except ContainmentError as exc:
        raise ProvenanceError(str(exc)) from exc
    files = _record_files(records_dir)
    try:
        expected_recorder = recorder_sha256()
    except ContainmentError as exc:
        raise ProvenanceError(str(exc)) from exc
    records = [_read_json(path, MAX_RECORD_BYTES, "provenance record") for path in files]
    module_names: dict[str, set[str]] = {
        "snapshot": set(),
        "work": set(),
        "runtime": set(),
    }
    recorder_names: set[str] = set()
    pids: list[int] = []
    argv_values: list[list[str]] = []
    interpreter_flags: tuple[bool, bool] | None = None
    quantipy_seen = False
    for record in records:
        if record.get("contract") != PROVENANCE_CONTRACT:
            raise ProvenanceError("provenance record contract is invalid")
        if record.get("stage") != stage:
            raise ProvenanceError("provenance record stage does not match requested stage")
        pid = record.get("pid")
        if type(pid) is not int:
            raise ProvenanceError("provenance record pid must be an integer")
        pids.append(pid)
        argv = record.get("argv")
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise ProvenanceError("provenance record argv must be an array of strings")
        argv_values.append(list(argv))
        flags = _object(record.get("flags"), "provenance record flags")
        safe_path = flags.get("safe_path")
        no_user_site = flags.get("no_user_site")
        if type(safe_path) is not bool or type(no_user_site) is not bool:
            raise ProvenanceError("provenance record Python safety flags must be booleans")
        observed_flags = (safe_path, no_user_site)
        if interpreter_flags is None:
            interpreter_flags = observed_flags
        elif interpreter_flags != observed_flags:
            raise ProvenanceError("provenance record Python safety flags disagree")
        if require_interpreter_flags and observed_flags != (True, True):
            raise ProvenanceError("provenance record Python safety flags are not enabled")
        recorder = _object(record.get("recorder"), "provenance record recorder")
        if recorder.get("path") != "/provenance/sitecustomize.py":
            raise ProvenanceError("provenance recorder path is invalid")
        if recorder.get("sha256") != expected_recorder:
            raise ProvenanceError("provenance recorder hash does not match")
        pythonpath = record.get("pythonpath")
        if not isinstance(pythonpath, str) or not pythonpath.startswith(
            "/provenance:/snapshot/src"
        ):
            raise ProvenanceError("provenance PYTHONPATH does not contain the fixed prefix")
        modules = record.get("modules")
        if not isinstance(modules, list):
            raise ProvenanceError("provenance record modules must be an array")
        for raw_module in modules:
            module = _object(raw_module, "provenance module")
            name = _text(module.get("name"), "provenance module name")
            path = _text(module.get("path"), f"provenance module path for {name}")
            origin = _text(module.get("origin"), f"provenance module origin for {name}")
            top_level = name.partition(".")[0]
            if top_level == "quantipy":
                quantipy_seen = True
                if origin != "snapshot":
                    raise ProvenanceError("quantipy module did not resolve from the snapshot")
            if top_level in work_top_levels and origin != "work":
                raise ProvenanceError(f"work module {name} did not resolve from /work")
            if origin == "snapshot":
                relative = _relative_virtual_path(path, "/snapshot/src/", "snapshot module path")
                host_path = pins.snapshot_dir / "src" / relative
                if _sha256_file(host_path, "snapshot module") != _digest(
                    module.get("sha256"), f"snapshot module hash for {name}"
                ):
                    raise ProvenanceError(f"snapshot module hash does not match: {relative}")
            elif origin == "work":
                relative = _relative_virtual_path(path, "/work/", "work module path")
                if top_level == "quantipy":
                    raise ProvenanceError("quantipy module may not resolve from /work")
                recorded = _digest(module.get("sha256"), f"work module hash for {name}")
                if _sha256_bytes(_git_blob(worktree, commit, relative, "work module")) != recorded:
                    raise ProvenanceError(f"work module hash does not match: {relative}")
            elif origin == "recorder":
                recorder_names.add(name)
                if name != "sitecustomize" or path != "/provenance/sitecustomize.py":
                    raise ProvenanceError("unexpected recorder module")
                if module.get("sha256") != expected_recorder:
                    raise ProvenanceError("recorder module hash does not match")
            elif origin == "runtime":
                if "sha256" in module:
                    raise ProvenanceError(f"runtime module must not carry a hash: {name}")
            else:
                raise ProvenanceError(f"unknown provenance module origin: {origin}")
            if origin in module_names:
                module_names[origin].add(name)
    if recorder_names != {"sitecustomize"}:
        raise ProvenanceError("provenance records must contain exactly sitecustomize")
    if require_quantipy and not quantipy_seen:
        raise ProvenanceError("provenance records contain no quantipy module")
    if interpreter_flags is None:
        raise ProvenanceError("provenance records contain no interpreter flags")
    return {
        "stage": stage,
        "record_count": len(records),
        "recorder_sha256": expected_recorder,
        "snapshot_modules": sorted(module_names["snapshot"]),
        "work_modules": sorted(module_names["work"]),
        "runtime_modules": sorted(module_names["runtime"]),
        "work_top_levels": sorted(work_top_levels),
        "argv": argv_values,
        "pids": pids,
        "interpreter_flags": {
            "safe_path": interpreter_flags[0],
            "no_user_site": interpreter_flags[1],
        },
    }


def verify_stage_provenance(
    records_dir: Path,
    *,
    stage: str,
    pins: RuntimePins,
    worktree: Path,
    commit: str,
    work_top_levels: frozenset[str],
    require_quantipy: bool,
    require_interpreter_flags: bool,
) -> dict[str, object]:
    """Verify every recorded import against the pinned snapshot or commit."""
    try:
        return _verify_stage_provenance(
            records_dir,
            stage=stage,
            pins=pins,
            worktree=worktree,
            commit=commit,
            work_top_levels=work_top_levels,
            require_quantipy=require_quantipy,
            require_interpreter_flags=require_interpreter_flags,
        )
    except ProvenanceError:
        raise
    except Exception as exc:
        raise ProvenanceError(f"provenance verification failed: {exc}") from exc


def _inside_worktree(path: Path, worktree: Path) -> Path:
    try:
        relative = path.resolve(strict=False).relative_to(worktree.resolve())
    except ValueError as exc:
        raise ProvenanceError(
            f"provenance records directory is outside the worktree: {path}"
        ) from exc
    if not relative.parts:
        raise ProvenanceError("provenance records directory must be inside the worktree")
    return relative


def _tracked_record_files(records_dir: Path, worktree: Path, commit: str) -> None:
    try:
        files = sorted(records_dir.rglob("*.json"))
    except OSError as exc:
        raise ProvenanceError("provenance records could not be listed") from exc
    if not files:
        raise ProvenanceError("provenance evidence has no JSON records")
    for path in files:
        relative = _inside_worktree(path, worktree).as_posix()
        actual = _sha256_file(path, "provenance record")
        committed = _sha256_bytes(_git_blob(worktree, commit, relative, "provenance record"))
        if actual != committed:
            raise ProvenanceError(
                f"provenance record bytes differ from the tested commit: {relative}"
            )


def _work_top_levels(run_plan: RunPlan) -> frozenset[str]:
    values = {run_plan.analysis.module.partition(".")[0]}
    for scenario in run_plan.scenarios:
        argv = scenario.targets_argv
        if len(argv) > 2 and argv[1] == "-m":
            values.add(argv[2].partition(".")[0])
    return frozenset(values)


def validate_provenance_evidence(
    store: ResearchStore,
    attempt_id: str,
    run_plan: RunPlan,
    path: Path,
) -> str:
    """Validate and canonicalize the committed provenance evidence index."""
    try:
        evidence = _read_json(path, _MAX_EVIDENCE_BYTES, "provenance evidence")
        if set(evidence) != {"contract", "attempt_id", "commit", "stages"}:
            raise ProvenanceError("provenance evidence keys are not exact")
        if evidence.get("contract") != "research-provenance-evidence-v1":
            raise ProvenanceError("provenance evidence contract is invalid")
        if evidence.get("attempt_id") != attempt_id or run_plan.attempt_id != attempt_id:
            raise ProvenanceError("provenance evidence attempt does not match")
        commit = _text(evidence.get("commit"), "provenance evidence commit")
        if commit != run_plan.commit:
            raise ProvenanceError("provenance evidence commit does not match run plan")
        attempt = store.get_attempt(attempt_id)
        if attempt.attempt_id != attempt_id:
            raise ProvenanceError("provenance evidence attempt does not match stored attempt")
        worktree = Path(_text(attempt.worktree_path, "attempt worktree path"))
        if not worktree.is_absolute():
            raise ProvenanceError("attempt worktree path must be absolute")
        raw_stages = evidence.get("stages")
        if not isinstance(raw_stages, list) or not raw_stages:
            raise ProvenanceError("provenance evidence stages must be a non-empty array")
        stages: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for raw_stage in raw_stages:
            stage_record = _object(raw_stage, "provenance evidence stage")
            if set(stage_record) != {"stage", "records_dir"}:
                raise ProvenanceError("provenance evidence stage keys are not exact")
            stage = _text(stage_record.get("stage"), "provenance evidence stage name")
            if _EVIDENCE_STAGE.fullmatch(stage) is None:
                raise ProvenanceError(f"invalid provenance evidence stage: {stage}")
            if stage in seen:
                raise ProvenanceError(f"duplicate provenance evidence stage: {stage}")
            seen.add(stage)
            records_value = _text(stage_record.get("records_dir"), "provenance records directory")
            records_dir = Path(records_value)
            if not records_dir.is_absolute():
                raise ProvenanceError("provenance records directory must be absolute")
            _inside_worktree(records_dir, worktree)
            _tracked_record_files(records_dir, worktree, commit)
            stages.append((stage, records_dir))
        if not any(stage.startswith("targets-") for stage, _path in stages):
            raise ProvenanceError("provenance evidence is missing a targets stage")
        if not any(stage.startswith("evaluate-") for stage, _path in stages):
            raise ProvenanceError("provenance evidence is missing an evaluate stage")
        if sum(stage == "analysis" for stage, _path in stages) != 1:
            raise ProvenanceError("provenance evidence requires exactly one analysis stage")
        try:
            pins = runtime_pins_from_record(dict(store.config()))
            verify_runtime_pins(pins)
        except Exception as exc:
            raise ProvenanceError(f"configured runtime pins are invalid: {exc}") from exc
        top_levels = _work_top_levels(run_plan)
        summaries: list[dict[str, object]] = []
        for stage, records_dir in stages:
            summaries.append(
                verify_stage_provenance(
                    records_dir,
                    stage=stage,
                    pins=pins,
                    worktree=worktree,
                    commit=commit,
                    work_top_levels=top_levels,
                    require_quantipy=stage == "analysis" or stage.startswith("evaluate-"),
                    require_interpreter_flags=stage == "analysis" or stage.startswith("evaluate-"),
                )
            )
        return to_json(
            {
                "contract": "research-provenance-evidence-v1",
                "attempt_id": attempt_id,
                "commit": commit,
                "recorder_sha256": recorder_sha256(),
                "stages": summaries,
            }
        )
    except ProvenanceError:
        raise
    except Exception as exc:
        raise ProvenanceError(f"provenance evidence validation failed: {exc}") from exc
