"""Focused tests for detached autoresearch run records."""

from __future__ import annotations

import errno
import json
import os
import signal
import stat
import subprocess
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import gateway.autoresearch_runs as autoresearch_runs
import pytest
from gateway.autoresearch.constants import DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT
from gateway.autoresearch_runs import (
    EXPECTED_ARTIFACT_MAX_BYTES,
    OUTPUT_CAPTURE_MAX_BYTES,
    AutoresearchRunRecordError,
    RunFailureClassification,
    RunManifest,
    RunOutputStream,
    RunState,
    RunStatus,
    archive_timed_out_partial_run,
    capture_output_stream,
    complete_run,
    consume_command_handoff,
    consume_command_input_file,
    create_command_input_file_from_stdin,
    prepare_output_capture,
    prepare_run,
    read_run_record,
    start_run,
    supervise_command,
    validate_startup_marker,
    write_command_handoff,
)


def _manifest(
    run_dir: Path,
    *,
    expected_artifact_path: Path | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "iteration": 7,
        "phase": "verification",
        "attempt": 2,
        "task_label": "verification-tests",
        "state_reference_sha256": "a" * 64,
        "instruction_manifest_sha256": "b" * 64,
        "run_directory": str(run_dir),
        "working_directory": str(run_dir.parents[3]),
        "command_sha256": sha256(b"verify-command\x00--opaque-value").hexdigest(),
        "expected_artifact_path": (
            str(expected_artifact_path) if expected_artifact_path is not None else None
        ),
        "timeout_seconds": None,
    }


def test_prepared_run_persists_only_the_immutable_command_digest(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")

    prepared = prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )

    persisted = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert prepared.manifest == RunManifest.from_dict(persisted)
    assert "--opaque-value" not in (run_dir / "manifest.json").read_text(encoding="utf-8")


def _prepare_running_run_with_expected_artifact(
    tmp_path: Path,
    *,
    expected_artifact_path: Path | None,
    run_id: str = "autoresearch-i7-abcdef1-v5",
) -> tuple[Path, Path, Path]:
    artifact_root = tmp_path / "quantipy-runs"
    runs_root = tmp_path / "detached-runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=expected_artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=os.getpid(), runs_root=runs_root)
    return runs_root, run_dir, artifact_root / run_id


def test_timeout_archival_moves_exact_partial_run_into_private_sibling_archive(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    artifact_path = source / "run.json"
    source.mkdir(mode=0o700)
    artifact_path.write_text('{"partial":true}\n', encoding="utf-8")
    artifact_path.chmod(0o600)
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=artifact_path,
        run_id=run_id,
    )

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert archived is not None
    assert not source.exists()
    assert archived.parent == artifact_root.parent / ".archive-partial-runs"
    assert archived.name.startswith(f"{run_id}.timeout.")
    assert (archived / "run.json").read_text(encoding="utf-8") == '{"partial":true}\n'
    assert stat.S_IMODE(archived.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(archived.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("expected_artifact", "create_source"),
    (
        (None, False),
        ("outside", True),
        ("missing", False),
    ),
)
def test_timeout_archival_ignores_ineligible_or_missing_artifacts(
    tmp_path: Path,
    expected_artifact: str | None,
    create_source: bool,
) -> None:
    run_id = "autoresearch-i7-abcdef1-v5"
    if expected_artifact is None:
        test_root = tmp_path / "no-artifact"
        test_root.mkdir()
        artifact_root = test_root / "quantipy-runs"
        artifact_root.mkdir(mode=0o700)
        artifact_path = None
    else:
        artifact_root = tmp_path / "quantipy-runs"
        artifact_root.mkdir(mode=0o700)
        if expected_artifact == "outside":
            artifact_path = tmp_path / "outside" / run_id / "run.json"
        else:
            artifact_path = artifact_root / run_id / "run.json"
    runs_root, run_dir, source = _prepare_running_run_with_expected_artifact(
        test_root if expected_artifact is None else tmp_path,
        expected_artifact_path=artifact_path,
        run_id=run_id,
    )
    if create_source:
        source.mkdir(mode=0o700, parents=True)
        (source / "run.json").write_text("outside", encoding="utf-8")

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert archived is None
    assert not (artifact_root.parent / ".archive-partial-runs").exists()
    if expected_artifact == "outside":
        assert source.exists()


def test_timeout_archival_rejects_a_malformed_path_inside_the_artifact_root(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    source = artifact_root / "autoresearch-i7-abcdef1-v5"
    source.mkdir(mode=0o700)
    malformed_path = source / "result.json"
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=malformed_path,
    )

    with pytest.raises(AutoresearchRunRecordError, match="expected artifact path"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )


def test_timeout_archival_rejects_a_symlink_source_without_moving_its_target(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    target = tmp_path / "target-run"
    target.mkdir(mode=0o700)
    source = artifact_root / run_id
    source.symlink_to(target, target_is_directory=True)
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )

    with pytest.raises(AutoresearchRunRecordError, match="non-symlink directory"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.is_symlink()
    assert target.exists()
    assert not (artifact_root.parent / ".archive-partial-runs").exists()


def test_timeout_archival_rejects_a_group_or_world_writable_source(tmp_path: Path) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o720)
    (source / "run.json").write_text("partial", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )

    with pytest.raises(AutoresearchRunRecordError, match="group/world writable"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.exists()


def test_timeout_archival_rejects_a_foreign_source_owner_without_chown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("partial", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    real_fstat = os.fstat

    def foreign_source_owner(fd: int) -> os.stat_result:
        metadata = real_fstat(fd)
        try:
            descriptor_target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return metadata
        if descriptor_target != str(source):
            return metadata
        fields = list(metadata)
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign_source_owner)

    with pytest.raises(AutoresearchRunRecordError, match="owned by the current user"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.exists()


def test_timeout_archival_rejects_an_unsafe_artifact_root(tmp_path: Path) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o770)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("partial", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )

    with pytest.raises(AutoresearchRunRecordError, match="artifact root"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.exists()


def test_timeout_archival_rejects_an_archive_root_symlink_without_moving_source(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("partial", encoding="utf-8")
    archive_target = tmp_path / "archive-target"
    archive_target.mkdir(mode=0o700)
    (artifact_root.parent / ".archive-partial-runs").symlink_to(
        archive_target,
        target_is_directory=True,
    )
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )

    with pytest.raises(AutoresearchRunRecordError, match="non-symlink directory"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.exists()
    assert list(archive_target.iterdir()) == []


def test_timeout_archival_rejects_injected_rename_failure_with_source_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("partial", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )

    def fail_rename(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "injected rename failure")

    monkeypatch.setattr(autoresearch_runs, "_rename_directory_no_replace", fail_rename)

    with pytest.raises(AutoresearchRunRecordError, match="injected rename failure"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.exists()


def test_timeout_archival_rejects_a_source_swap_at_rename_and_restores_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("original", encoding="utf-8")
    original = artifact_root / f"{run_id}.original"
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    real_rename = autoresearch_runs._rename_directory_no_replace
    swapped = False

    def swap_before_rename(
        source_name: str,
        *,
        source_directory: int,
        destination_name: str,
        destination_directory: int,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(original)
            source.mkdir(mode=0o700)
            (source / "run.json").write_text("replacement", encoding="utf-8")
        real_rename(
            source_name,
            source_directory=source_directory,
            destination_name=destination_name,
            destination_directory=destination_directory,
        )

    monkeypatch.setattr(
        autoresearch_runs,
        "_rename_directory_no_replace",
        swap_before_rename,
    )

    with pytest.raises(AutoresearchRunRecordError, match="identity mismatch"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.joinpath("run.json").read_text(encoding="utf-8") == "replacement"
    assert original.joinpath("run.json").read_text(encoding="utf-8") == "original"
    archive_root = artifact_root.parent / ".archive-partial-runs"
    assert not [path for path in archive_root.iterdir() if path.name != ".keep"]


def test_timeout_archival_keeps_pending_quarantine_when_rollback_source_is_reoccupied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("original", encoding="utf-8")
    original = artifact_root / f"{run_id}.original"
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    real_rename = autoresearch_runs._rename_directory_no_replace
    stage_swapped = False
    rollback_blocked = False

    def swap_and_reoccupy_source(
        source_name: str,
        *,
        source_directory: int,
        destination_name: str,
        destination_directory: int,
    ) -> None:
        nonlocal rollback_blocked, stage_swapped
        if source_name == run_id and not stage_swapped:
            stage_swapped = True
            source.rename(original)
            source.mkdir(mode=0o700)
            (source / "run.json").write_text("replacement", encoding="utf-8")
        elif source_name.startswith(".pending.") and not rollback_blocked:
            rollback_blocked = True
            source.mkdir(mode=0o700)
            (source / "run.json").write_text("blocker", encoding="utf-8")
        real_rename(
            source_name,
            source_directory=source_directory,
            destination_name=destination_name,
            destination_directory=destination_directory,
        )

    monkeypatch.setattr(
        autoresearch_runs,
        "_rename_directory_no_replace",
        swap_and_reoccupy_source,
    )

    with pytest.raises(AutoresearchRunRecordError, match="pending quarantine retained"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    archive_root = artifact_root.parent / ".archive-partial-runs"
    pending_entries = [path for path in archive_root.iterdir() if path.name.startswith(".pending.")]
    assert stage_swapped
    assert rollback_blocked
    assert source.joinpath("run.json").read_text(encoding="utf-8") == "blocker"
    assert original.joinpath("run.json").read_text(encoding="utf-8") == "original"
    assert len(pending_entries) == 1
    assert pending_entries[0].joinpath("run.json").read_text(encoding="utf-8") == "replacement"
    assert not [
        path for path in archive_root.iterdir() if path.name.startswith(f"{run_id}.timeout.")
    ]


def test_timeout_archival_quarantines_a_replacement_after_final_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("original", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    fixed_timestamp = "2026-08-20T12:34:56.123456Z"
    monkeypatch.setattr(autoresearch_runs, "_utc_now", lambda: fixed_timestamp)
    archive_root = artifact_root.parent / ".archive-partial-runs"
    real_rename = autoresearch_runs._rename_directory_no_replace
    promotion_swapped = False
    original_archived = archive_root / f"{run_id}.timeout.{fixed_timestamp}.original"
    old_quarantine_base = f".pending.{run_id}.timeout.{fixed_timestamp}.promotion-mismatch"
    old_quarantine_names = {
        old_quarantine_base if attempt == 0 else f"{old_quarantine_base}.{attempt}"
        for attempt in range(128)
    }
    preferred_pending_name = ""
    fsynced_paths: list[str] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsynced_paths.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    def swap_after_promotion(
        source_name: str,
        *,
        source_directory: int,
        destination_name: str,
        destination_directory: int,
    ) -> None:
        nonlocal preferred_pending_name, promotion_swapped
        real_rename(
            source_name,
            source_directory=source_directory,
            destination_name=destination_name,
            destination_directory=destination_directory,
        )
        if source_name.startswith(".pending.") and not promotion_swapped:
            promotion_swapped = True
            preferred_pending_name = source_name
            (archive_root / preferred_pending_name).mkdir(mode=0o700)
            for quarantine_name in old_quarantine_names:
                (archive_root / quarantine_name).mkdir(mode=0o700)
            final_path = archive_root / destination_name
            final_path.rename(original_archived)
            final_path.mkdir(mode=0o700)
            (final_path / "run.json").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(
        autoresearch_runs,
        "_rename_directory_no_replace",
        swap_after_promotion,
    )

    with pytest.raises(AutoresearchRunRecordError, match="unvalidated entry quarantined"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    final_path = archive_root / f"{run_id}.timeout.{fixed_timestamp}"
    pending_entries = [path for path in archive_root.iterdir() if path.name.startswith(".pending.")]
    randomized_entries = [
        path
        for path in pending_entries
        if path.name not in old_quarantine_names | {preferred_pending_name}
    ]
    assert promotion_swapped
    assert preferred_pending_name
    assert not final_path.exists()
    assert original_archived.joinpath("run.json").read_text(encoding="utf-8") == "original"
    assert len(randomized_entries) == 1
    assert randomized_entries[0].joinpath("run.json").read_text(encoding="utf-8") == "replacement"
    assert old_quarantine_names <= {path.name for path in pending_entries}
    assert str(artifact_root) in fsynced_paths
    assert str(archive_root) in fsynced_paths
    assert str(artifact_root.parent) in fsynced_paths


def test_timeout_archival_returns_descriptor_path_after_post_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("original", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    real_descriptor_path = autoresearch_runs._directory_path_from_descriptor
    swapped = False
    original_path: Path | None = None
    replacement_path: Path | None = None

    def replace_after_final_identity(
        descriptor: int,
        *,
        expected_metadata: os.stat_result,
        label: str,
    ) -> Path:
        nonlocal original_path, replacement_path, swapped
        if not swapped:
            swapped = True
            final_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            original_path = final_path.with_name(f"{final_path.name}.original")
            replacement_path = final_path
            final_path.rename(original_path)
            final_path.mkdir(mode=0o700)
            (final_path / "run.json").write_text("replacement", encoding="utf-8")
        return real_descriptor_path(
            descriptor,
            expected_metadata=expected_metadata,
            label=label,
        )

    monkeypatch.setattr(
        autoresearch_runs,
        "_directory_path_from_descriptor",
        replace_after_final_identity,
    )

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert swapped
    assert archived == original_path
    assert archived is not None
    assert archived.exists()
    assert (archived / "run.json").read_text(encoding="utf-8") == "original"
    assert replacement_path is not None
    assert (replacement_path / "run.json").read_text(encoding="utf-8") == "replacement"


def test_timeout_archival_fsyncs_all_successful_move_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("partial", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    archive_root = artifact_root.parent / ".archive-partial-runs"
    fsynced_paths: list[str] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsynced_paths.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert archived is not None
    assert str(artifact_root.parent) in fsynced_paths
    assert str(artifact_root) in fsynced_paths
    assert str(archive_root) in fsynced_paths


def test_timeout_archival_pins_parent_before_root_name_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "artifact-parent"
    parent.mkdir(mode=0o700)
    artifact_root = parent / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("original", encoding="utf-8")
    attacker_parent = tmp_path / "attacker-parent"
    attacker_parent.mkdir(mode=0o700)
    (attacker_parent / "quantipy-runs").mkdir(mode=0o700)
    original_parent = tmp_path / "artifact-parent-original"
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    real_open = autoresearch_runs._open_absolute_directory_no_follow
    swapped = False

    def swap_after_parent_open(path: Path, *, label: str, missing_ok: bool = False) -> int | None:
        nonlocal swapped
        descriptor = real_open(path, label=label, missing_ok=missing_ok)
        if label in {"artifact root", "artifact root parent"} and not swapped:
            swapped = True
            parent.rename(original_parent)
            attacker_parent.rename(parent)
        return descriptor

    monkeypatch.setattr(
        autoresearch_runs,
        "_open_absolute_directory_no_follow",
        swap_after_parent_open,
    )

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert archived is not None
    pinned_archive_root = original_parent / ".archive-partial-runs"
    assert swapped
    assert archived.exists()
    assert archived.parent == pinned_archive_root
    assert (archived / "run.json").read_text(encoding="utf-8") == "original"
    assert len(list(pinned_archive_root.iterdir())) == 1
    assert not (parent / ".archive-partial-runs").exists()


def test_timeout_archival_rejects_a_cross_device_archive_simulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    source.mkdir(mode=0o700)
    (source / "run.json").write_text("partial", encoding="utf-8")
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=source / "run.json",
        run_id=run_id,
    )
    archive_root = artifact_root.parent / ".archive-partial-runs"
    real_fstat = os.fstat

    def foreign_device_for_archive(fd: int) -> os.stat_result:
        metadata = real_fstat(fd)
        try:
            descriptor_target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return metadata
        if descriptor_target != str(archive_root):
            return metadata
        fields = list(metadata)
        fields[2] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign_device_for_archive)

    with pytest.raises(AutoresearchRunRecordError, match="different filesystems"):
        archive_timed_out_partial_run(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_root=artifact_root,
        )

    assert source.exists()


def test_archive_timeout_cli_dispatches_the_fixed_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, Path, Path]] = []

    def record_dispatch(*, run_dir: Path, runs_root: Path, artifact_root: Path) -> Path | None:
        calls.append((run_dir, runs_root, artifact_root))
        return None

    run_dir = tmp_path / "detached-run"
    runs_root = tmp_path / "detached-runs"
    monkeypatch.setattr(autoresearch_runs, "archive_timed_out_partial_run", record_dispatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autoresearch_runs",
            "archive-timeout-partial-run",
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
        ],
    )

    assert autoresearch_runs._main() == 0
    assert calls == [
        (
            run_dir,
            runs_root,
            DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        )
    ]


def test_archive_timeout_cli_rejects_an_artifact_root_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autoresearch_runs",
            "archive-timeout-partial-run",
            "--run-dir",
            str(tmp_path / "detached-run"),
            "--artifact-root",
            str(tmp_path / "artifact-root"),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        autoresearch_runs._main()

    assert raised.value.code == 2


def test_timeout_archival_does_not_touch_a_terminal_detached_record(tmp_path: Path) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    artifact_path = source / "run.json"
    source.mkdir(mode=0o700)
    artifact_path.write_text("terminal", encoding="utf-8")
    artifact_path.chmod(0o600)
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=artifact_path,
        run_id=run_id,
    )
    complete_run(
        run_dir=run_dir,
        runs_root=runs_root,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
    )

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert archived is None
    assert source.exists()


def test_timeout_archival_chooses_a_new_name_without_overwriting_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "quantipy-runs"
    artifact_root.mkdir(mode=0o700)
    run_id = "autoresearch-i7-abcdef1-v5"
    source = artifact_root / run_id
    artifact_path = source / "run.json"
    source.mkdir(mode=0o700)
    artifact_path.write_text("source", encoding="utf-8")
    artifact_path.chmod(0o600)
    runs_root, run_dir, _ = _prepare_running_run_with_expected_artifact(
        tmp_path,
        expected_artifact_path=artifact_path,
        run_id=run_id,
    )
    fixed_timestamp = "2026-08-20T12:34:56.123456Z"
    monkeypatch.setattr(autoresearch_runs, "_utc_now", lambda: fixed_timestamp)
    archive_root = artifact_root.parent / ".archive-partial-runs"
    archive_root.mkdir(mode=0o700)
    collision = archive_root / f"{run_id}.timeout.{fixed_timestamp}"
    collision.mkdir(mode=0o700)
    (collision / "run.json").write_text("existing", encoding="utf-8")

    archived = archive_timed_out_partial_run(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_root=artifact_root,
    )

    assert archived is not None
    assert archived != collision
    assert (collision / "run.json").read_text(encoding="utf-8") == "existing"
    assert (archived / "run.json").read_text(encoding="utf-8") == "source"


def test_prepare_accepts_untrustworthy_looking_ancestors_and_supervise_rejects_them(
    tmp_path: Path,
) -> None:
    # The prepare step runs inside the stage-agent sandbox, where uid mapping
    # makes ancestor ownership unknowable; the trust check belongs to the
    # unsandboxed supervise step.
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_parent = tmp_path / "artifact-parent"
    artifact_parent.mkdir(mode=0o775)
    artifact_parent.chmod(0o775)
    artifact_path = artifact_parent / "run.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )

    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )

    stdout_descriptor = os.open(os.devnull, os.O_WRONLY)
    stderr_descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(AutoresearchRunRecordError, match="group/world writable"):
            supervise_command(
                run_dir=run_dir,
                runs_root=runs_root,
                systemd_unit="openclaw-long-task-test.service",
                grace_seconds=0.1,
                stdout_descriptor=stdout_descriptor,
                stderr_descriptor=stderr_descriptor,
            )
    finally:
        os.close(stdout_descriptor)
        os.close(stderr_descriptor)


def test_complete_killed_run_reports_resource_exhaustion_with_exact_evidence(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=123, runs_root=runs_root)

    complete_run(
        run_dir=run_dir,
        exit_code=137,
        signal_number=9,
        peak_rss_bytes=1024,
        failure_classification=RunFailureClassification.RESOURCE_EXHAUSTED,
        runs_root=runs_root,
    )

    record = read_run_record(run_dir=run_dir, runs_root=runs_root)
    assert record.status.state is RunState.FAILED
    assert record.status.failure_classification is RunFailureClassification.RESOURCE_EXHAUSTED
    assert record.status.exit_code == 137
    assert record.status.signal_number == 9
    assert record.status.resource_usage.peak_rss_bytes == 1024


def test_output_capture_keeps_streams_private_and_out_of_status_content(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    prepare_output_capture(run_dir=run_dir, runs_root=runs_root)
    start_run(run_dir=run_dir, pid=123, runs_root=runs_root)

    capture_output_stream(
        run_dir=run_dir,
        runs_root=runs_root,
        stream=RunOutputStream.STDOUT,
        source=BytesIO(b"stdout diagnostic\n"),
    )
    capture_output_stream(
        run_dir=run_dir,
        runs_root=runs_root,
        stream=RunOutputStream.STDERR,
        source=BytesIO(b"stderr diagnostic\n"),
    )
    complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )

    record = read_run_record(run_dir=run_dir, runs_root=runs_root)

    assert record.status.output_capture is not None
    assert record.status.output_capture.stdout.relative_path == "stdout.log"
    assert record.status.output_capture.stderr.relative_path == "stderr.log"
    assert record.status.output_capture.stdout.eof_observed is True
    assert record.status.output_capture.stderr.eof_observed is True
    assert (run_dir / "stdout.log").read_bytes() == b"stdout diagnostic\n"
    assert (run_dir / "stderr.log").read_bytes() == b"stderr diagnostic\n"
    assert stat.S_IMODE((run_dir / "stdout.log").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "stderr.log").stat().st_mode) == 0o600
    assert b"stdout diagnostic" not in (run_dir / "status.json").read_bytes()
    assert b"stderr diagnostic" not in (run_dir / "status.json").read_bytes()


def test_empty_capture_receipt_reads_as_not_yet_written(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    prepare_output_capture(run_dir=run_dir, runs_root=runs_root)

    receipt_path = run_dir / ".stdout.capture.json"
    receipt_path.touch(mode=0o600)

    receipt = autoresearch_runs._read_capture_receipt(
        run_dir, RunOutputStream.STDOUT, required=False
    )
    assert receipt is None

    with pytest.raises(
        autoresearch_runs.AutoresearchRunRecordError,
        match="missing stdout capture receipt",
    ):
        autoresearch_runs._read_capture_receipt(run_dir, RunOutputStream.STDOUT, required=True)


def test_output_capture_stores_a_bounded_tail_with_a_digest(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    prepare_output_capture(run_dir=run_dir, runs_root=runs_root)
    start_run(run_dir=run_dir, pid=123, runs_root=runs_root)
    observed = b"discarded-prefix" + (b"x" * OUTPUT_CAPTURE_MAX_BYTES)

    capture_output_stream(
        run_dir=run_dir,
        runs_root=runs_root,
        stream=RunOutputStream.STDOUT,
        source=BytesIO(observed),
    )
    capture_output_stream(
        run_dir=run_dir,
        runs_root=runs_root,
        stream=RunOutputStream.STDERR,
        source=BytesIO(b""),
    )
    complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )

    capture = read_run_record(run_dir=run_dir, runs_root=runs_root).status.output_capture

    assert capture is not None
    assert capture.stdout.bytes_observed == len(observed)
    assert capture.stdout.bytes_stored == OUTPUT_CAPTURE_MAX_BYTES
    assert capture.stdout.truncated is True
    assert capture.stdout.eof_observed is True
    assert capture.stdout.sha256 == sha256((run_dir / "stdout.log").read_bytes()).hexdigest()
    assert (run_dir / "stdout.log").read_bytes() == b"x" * OUTPUT_CAPTURE_MAX_BYTES


def test_output_capture_refuses_a_preexisting_symlink(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    target = tmp_path / "outside.log"
    (run_dir / "stdout.log").symlink_to(target)

    with pytest.raises(AutoresearchRunRecordError, match="capture output must not be a symlink"):
        prepare_output_capture(run_dir=run_dir, runs_root=runs_root)


def test_status_reader_explicitly_migrates_a_schema_v1_status_without_capture() -> None:
    raw = {
        "schema_version": 1,
        "manifest_sha256": "a" * 64,
        "state": "running",
        "pid": 123,
        "systemd_unit": None,
        "updated_at": "2026-07-26T12:00:00.000000Z",
        "started_at": "2026-07-26T12:00:00.000000Z",
        "finished_at": None,
        "exit_code": None,
        "signal_number": None,
        "failure_classification": None,
        "resource_usage": {"elapsed_seconds": 0.0, "peak_rss_bytes": None},
    }

    status = RunStatus.from_dict(raw)

    assert status.schema_version == 5
    assert status.output_capture is None


def test_status_reader_explicitly_migrates_schema_v2_capture_as_incomplete() -> None:
    empty_stream = {
        "relative_path": "stdout.log",
        "bytes_observed": 0,
        "bytes_stored": 0,
        "sha256": sha256(b"").hexdigest(),
        "truncated": False,
    }
    raw = {
        "schema_version": 2,
        "manifest_sha256": "a" * 64,
        "state": "running",
        "pid": 123,
        "systemd_unit": None,
        "updated_at": "2026-07-26T12:00:00.000000Z",
        "started_at": "2026-07-26T12:00:00.000000Z",
        "finished_at": None,
        "exit_code": None,
        "signal_number": None,
        "failure_classification": None,
        "resource_usage": {"elapsed_seconds": 0.0, "peak_rss_bytes": None},
        "output_capture": {
            "stdout": empty_stream,
            "stderr": {**empty_stream, "relative_path": "stderr.log"},
        },
    }

    status = RunStatus.from_dict(raw)

    assert status.schema_version == 5
    assert status.output_capture is not None
    assert status.output_capture.stdout.eof_observed is False
    assert status.output_capture.stderr.eof_observed is False


def test_status_reader_rejects_a_boolean_historical_schema_version() -> None:
    raw = {
        "schema_version": True,
        "manifest_sha256": "a" * 64,
        "state": "running",
        "pid": 123,
        "systemd_unit": None,
        "updated_at": "2026-07-26T12:00:00.000000Z",
        "started_at": "2026-07-26T12:00:00.000000Z",
        "finished_at": None,
        "exit_code": None,
        "signal_number": None,
        "failure_classification": None,
        "resource_usage": {"elapsed_seconds": 0.0, "peak_rss_bytes": None},
    }

    with pytest.raises(AutoresearchRunRecordError, match="schema_version"):
        RunStatus.from_dict(raw)


def test_reader_rejects_a_symlinked_record_before_it_can_be_consumed(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    status_path = run_dir / "status.json"
    status_path.symlink_to(manifest_path)

    with pytest.raises(AutoresearchRunRecordError, match="symlink"):
        read_run_record(run_dir=run_dir, runs_root=runs_root)


def test_manifest_rejects_an_uppercase_digest(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "iteration-7" / "verification" / "attempt-2"
    raw = _manifest(run_dir)
    raw["command_sha256"] = "A" * 64

    with pytest.raises(AutoresearchRunRecordError, match="canonical"):
        RunManifest.from_dict(raw)


def test_status_rejects_noncanonical_or_inverted_timestamps(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=tmp_path / "runs",
        command=("verify-command", "--opaque-value"),
    )
    status_path = run_dir / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_sha256": sha256((run_dir / "manifest.json").read_bytes()).hexdigest(),
                "state": "failed",
                "pid": 123,
                "systemd_unit": None,
                "updated_at": "2026-07-26T12:00:00+00:00",
                "started_at": "2026-07-26T12:00:01Z",
                "finished_at": "2026-07-26T12:00:00Z",
                "exit_code": 1,
                "signal_number": None,
                "failure_classification": "process_error",
                "resource_usage": {"elapsed_seconds": 0.0, "peak_rss_bytes": None},
            }
        ),
        encoding="utf-8",
    )
    status_path.chmod(0o600)

    with pytest.raises(AutoresearchRunRecordError, match="timestamp"):
        read_run_record(run_dir=run_dir, runs_root=tmp_path / "runs")


def test_complete_requires_a_coherent_started_record(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )

    with pytest.raises(AutoresearchRunRecordError, match="missing status"):
        complete_run(
            run_dir=run_dir,
            exit_code=1,
            signal_number=None,
            peak_rss_bytes=None,
            runs_root=runs_root,
        )


def test_start_run_uses_fixed_microsecond_utc_timestamps(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )

    status = start_run(run_dir=run_dir, pid=124, runs_root=runs_root)

    assert status.started_at.endswith("Z")
    assert len(status.started_at.rsplit(".", maxsplit=1)[1]) == len("000000Z")


def test_exit_code_124_without_timeout_evidence_is_a_process_error(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)

    complete_run(
        run_dir=run_dir,
        exit_code=124,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )

    record = read_run_record(run_dir=run_dir, runs_root=runs_root)
    assert record.status.failure_classification is RunFailureClassification.PROCESS_ERROR


def test_command_input_file_is_private_no_follow_and_one_time(tmp_path: Path) -> None:
    command_file = tmp_path / "command.json"
    command = ("verify-command", "--credential-file", str(tmp_path / "creds.json"))
    command_file.write_text(json.dumps({"command": list(command)}), encoding="utf-8")
    command_file.chmod(0o600)

    assert consume_command_input_file(command_file) == command
    assert not command_file.exists()

    symlink = tmp_path / "command-link.json"
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"command": ["true"]}), encoding="utf-8")
    target.chmod(0o600)
    symlink.symlink_to(target)
    with pytest.raises(AutoresearchRunRecordError, match="cannot open command input"):
        consume_command_input_file(symlink)


def test_command_input_helper_creates_private_file_with_exclusive_no_follow(
    tmp_path: Path,
) -> None:
    command_file = tmp_path / "command.json"
    payload = json.dumps(
        {"schema_version": 1, "command": ["verify-command", "--opaque-value"]}
    ).encode()

    create_command_input_file_from_stdin(output_path=command_file, payload=payload)

    assert stat.S_IMODE(command_file.stat().st_mode) == 0o600
    assert json.loads(command_file.read_text(encoding="utf-8")) == {
        "command": ["verify-command", "--opaque-value"]
    }
    with pytest.raises(AutoresearchRunRecordError, match="already exists"):
        create_command_input_file_from_stdin(output_path=command_file, payload=payload)


def test_command_handoff_is_private_and_removed_after_consumption(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    credential_file = tmp_path / "credential.txt"
    credential_file.write_text("secret outside argv\n", encoding="utf-8")
    command = ("verify-command", "--credential-file", str(credential_file))
    manifest_path = tmp_path / "manifest.json"
    raw = _manifest(run_dir)
    raw["command_sha256"] = sha256("\0".join(command).encode("utf-8")).hexdigest()
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=command,
    )

    write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)

    handoff_path = run_dir / ".command-handoff.json"
    assert stat.S_IMODE(handoff_path.stat().st_mode) == 0o600
    assert consume_command_handoff(run_dir=run_dir, runs_root=runs_root) == command
    assert not handoff_path.exists()


def test_supervisor_does_not_signal_reused_group_after_result_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    command = ("verify-command", "--opaque-value")
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=command,
    )
    write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)
    result_path = run_dir / ".command-result.json"
    result_path.write_text("{}", encoding="utf-8")
    result_path.chmod(0o600)
    sentinel = subprocess.Popen(
        ["sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    class FakeProcess:
        pid = 987_654
        reaped = False

        def wait(self) -> int:
            self.reaped = True
            return 0

    process = FakeProcess()

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    def signal_reused_group(_leader_pid: int, signal_number: int) -> None:
        if process.reaped:
            os.killpg(sentinel.pid, signal_number)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        autoresearch_runs,
        "_wait_for_leader_exit_without_reaping",
        lambda *_args, **_kwargs: os.waitid_result(
            (process.pid, os.getuid(), signal.SIGCHLD, 0, os.CLD_EXITED)
        ),
    )
    monkeypatch.setattr(
        autoresearch_runs,
        "_terminate_group_members_while_leader_waitable",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        autoresearch_runs,
        "_signal_anchored_process_group",
        signal_reused_group,
    )
    stdout_descriptor = os.open(os.devnull, os.O_WRONLY)
    stderr_descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(
            AutoresearchRunRecordError,
            match="supervised command result already exists",
        ):
            supervise_command(
                run_dir=run_dir,
                runs_root=runs_root,
                systemd_unit="openclaw-long-task-test.service",
                grace_seconds=0.1,
                stdout_descriptor=stdout_descriptor,
                stderr_descriptor=stderr_descriptor,
            )
        with pytest.raises(subprocess.TimeoutExpired):
            sentinel.wait(timeout=0.2)
    finally:
        if sentinel.poll() is None:
            os.killpg(sentinel.pid, signal.SIGKILL)
        sentinel.wait(timeout=2.0)


def test_secret_detection_allows_hyphenated_words_containing_key_prefixes() -> None:
    # A theory family named "...risk-rotation-microstructure" contains the
    # substring "sk-rotation-microstructure", which an unanchored OpenAI-key
    # pattern reads as a secret and which blocked a live campaign iteration.
    benign = (
        "/opt/py",
        "-m",
        "quantipy.experiments",
        "--experiment",
        "i9-equity-duration-risk-rotation-microstructure",
        "--tag",
        "brisk-rebalance-experiment-alpha",
    )
    assert autoresearch_runs.command_sha256(benign)

    # Built at runtime so the literals never appear in the repository.
    openai_like = "sk-" + "a" * 20
    github_like = "gh" + "p_" + "b" * 20
    aws_like = "AK" + "IA" + "C" * 16
    for secret in (openai_like, f"--key={openai_like}", github_like, aws_like):
        with pytest.raises(AutoresearchRunRecordError, match="secret"):
            autoresearch_runs.command_sha256(("/opt/py", secret))


def test_secret_bearing_command_arguments_are_rejected(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    raw = _manifest(run_dir)
    raw["command_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AutoresearchRunRecordError, match="credential files"):
        prepare_run(
            manifest_path=manifest_path,
            run_dir=run_dir,
            runs_root=runs_root,
            command=("verify-command", "--api-key", "sk-testsecret000000000000"),
        )


def test_startup_marker_must_bind_to_the_live_manifest_and_pid(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=555, runs_root=runs_root)
    marker_path = run_dir / ".startup-published.json"
    marker = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    marker["pid"] = 556
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    marker_path.chmod(0o600)

    with pytest.raises(AutoresearchRunRecordError, match="live run identity"):
        validate_startup_marker(run_dir=run_dir, marker_path=marker_path, runs_root=runs_root)


def test_complete_run_attests_expected_artifact_before_terminal_publication(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "run.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)
    artifact_path.parent.mkdir()
    artifact_path.parent.chmod(0o700)
    artifact_path.parent.chmod(0o700)
    artifact_bytes = b'{"success":true}\n'
    artifact_path.write_bytes(artifact_bytes)
    artifact_path.chmod(0o600)

    status = complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )

    assert status.state is RunState.SUCCEEDED
    attestation = status.expected_artifact_attestation
    assert attestation is not None
    assert attestation.path == str(artifact_path)
    assert attestation.size_bytes == len(artifact_bytes)
    assert attestation.sha256 == sha256(artifact_bytes).hexdigest()
    assert attestation.inode == artifact_path.stat().st_ino
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o400
    assert stat.S_IMODE((run_dir / "status.json").stat().st_mode) == 0o400
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o500
    assert artifact_bytes.decode().strip() not in (run_dir / "status.json").read_text(
        encoding="utf-8"
    )
    assert read_run_record(run_dir=run_dir, runs_root=runs_root).status == status


@pytest.mark.parametrize("writable_target", ("status", "artifact", "run_directory"))
def test_terminal_reader_rejects_writable_sealed_evidence(
    tmp_path: Path,
    writable_target: str,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "run.json"
    artifact_path.parent.mkdir(mode=0o700)
    artifact_path.write_bytes(b"receipt")
    artifact_path.chmod(0o600)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)
    complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )
    if writable_target == "status":
        (run_dir / "status.json").chmod(0o600)
    elif writable_target == "artifact":
        artifact_path.chmod(0o600)
    else:
        run_dir.chmod(0o700)

    with pytest.raises(AutoresearchRunRecordError, match=r"mode|directory"):
        read_run_record(run_dir=run_dir, runs_root=runs_root)


def test_terminal_status_rejects_non_strict_expected_artifact_attestation(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "run.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)
    artifact_path.parent.mkdir()
    artifact_path.parent.chmod(0o700)
    artifact_path.write_bytes(b"receipt")
    artifact_path.chmod(0o600)
    complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )
    status_path = run_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    run_dir.chmod(0o700)
    status_path.chmod(0o600)
    status["expected_artifact_attestation"]["worker_claim"] = True
    status_path.write_text(json.dumps(status), encoding="utf-8")
    status_path.chmod(0o400)
    run_dir.chmod(0o500)

    with pytest.raises(
        AutoresearchRunRecordError,
        match="expected_artifact_attestation must contain exact keys",
    ):
        read_run_record(run_dir=run_dir, runs_root=runs_root)


def test_terminal_record_rejects_substituted_expected_artifact(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "run.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)
    artifact_path.parent.mkdir()
    artifact_path.parent.chmod(0o700)
    artifact_path.write_bytes(b"original")
    artifact_path.chmod(0o600)
    complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )
    replacement = artifact_path.with_suffix(".replacement")
    replacement.write_bytes(b"substitute")
    replacement.chmod(0o400)
    replacement.replace(artifact_path)

    with pytest.raises(
        AutoresearchRunRecordError,
        match="does not match terminal worker attestation",
    ):
        read_run_record(run_dir=run_dir, runs_root=runs_root)


@pytest.mark.parametrize(
    ("invalid_artifact", "expected_error"),
    (
        ("missing", "missing"),
        ("symlink", "symlink"),
        ("hard_link", "hard_link"),
        ("oversized", "oversized"),
        ("mode", "wrong_mode"),
    ),
)
def test_successful_process_with_unattestable_expected_artifact_is_artifact_missing(
    tmp_path: Path,
    invalid_artifact: str,
    expected_error: str,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "run.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)
    artifact_path.parent.mkdir()
    artifact_path.parent.chmod(0o700)
    if invalid_artifact == "symlink":
        target = tmp_path / "outside.json"
        target.write_bytes(b"outside")
        target.chmod(0o600)
        artifact_path.symlink_to(target)
    elif invalid_artifact == "hard_link":
        target = tmp_path / "outside.json"
        target.write_bytes(b"outside")
        target.chmod(0o600)
        os.link(target, artifact_path)
    elif invalid_artifact == "oversized":
        artifact_path.write_bytes(b"x" * (EXPECTED_ARTIFACT_MAX_BYTES + 1))
        artifact_path.chmod(0o600)
    elif invalid_artifact == "mode":
        artifact_path.write_bytes(b"public")
        artifact_path.chmod(0o644)

    status = complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )

    assert status.state is RunState.FAILED
    assert status.failure_classification is RunFailureClassification.ARTIFACT_MISSING
    assert status.expected_artifact_attestation is None
    assert status.expected_artifact_attestation_status.value == "failed"
    assert status.expected_artifact_attestation_error is not None
    assert status.expected_artifact_attestation_error.value == expected_error


@pytest.mark.parametrize(
    ("exit_code", "signal_number", "timed_out", "failure_classification"),
    (
        (124, None, True, RunFailureClassification.TIMEOUT),
        (143, 15, False, RunFailureClassification.OPERATOR_STOPPED),
        (137, 9, False, RunFailureClassification.RESOURCE_EXHAUSTED),
        (7, None, False, RunFailureClassification.PROCESS_ERROR),
    ),
)
def test_artifact_attestation_failure_preserves_primary_process_failure(
    tmp_path: Path,
    exit_code: int,
    signal_number: int | None,
    timed_out: bool,
    failure_classification: RunFailureClassification,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "missing-run.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)

    status = complete_run(
        run_dir=run_dir,
        exit_code=exit_code,
        signal_number=signal_number,
        peak_rss_bytes=None,
        timed_out=timed_out,
        failure_classification=failure_classification,
        runs_root=runs_root,
    )

    assert status.state is RunState.FAILED
    assert status.failure_classification is failure_classification
    assert status.expected_artifact_attestation is None
    assert status.expected_artifact_attestation_status.value == "failed"
    assert status.expected_artifact_attestation_error is not None
    assert status.expected_artifact_attestation_error.value == "missing"


def test_expected_artifact_open_is_pinned_across_ancestor_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_parent = tmp_path / "receipts"
    artifact_path = artifact_parent / "run.json"
    moved_parent = tmp_path / "receipts-pinned"
    attacker_parent = tmp_path / "attacker"
    attacker_parent.mkdir()
    attacker_parent.chmod(0o700)
    attacker_artifact = attacker_parent / artifact_path.name
    attacker_artifact.write_bytes(b"attacker")
    attacker_artifact.chmod(0o600)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    start_run(run_dir=run_dir, pid=124, runs_root=runs_root)
    artifact_parent.mkdir()
    artifact_parent.chmod(0o700)
    artifact_path.write_bytes(b"trusted")
    artifact_path.chmod(0o600)
    real_open = os.open
    swapped = False

    def swap_parent_before_final_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is not None and os.fspath(path) == artifact_path.name:
            artifact_parent.rename(moved_parent)
            artifact_parent.symlink_to(attacker_parent, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_parent_before_final_open)
    status = complete_run(
        run_dir=run_dir,
        exit_code=0,
        signal_number=None,
        peak_rss_bytes=None,
        runs_root=runs_root,
    )
    artifact_parent.unlink()
    moved_parent.rename(artifact_parent)

    assert swapped
    assert status.state is RunState.SUCCEEDED
    assert status.expected_artifact_attestation is not None
    assert status.expected_artifact_attestation.sha256 == sha256(b"trusted").hexdigest()
    assert read_run_record(run_dir=run_dir, runs_root=runs_root).status == status


def test_historical_terminal_success_cannot_prove_expected_artifact_bytes(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-7" / "verification" / "attempt-2"
    artifact_path = tmp_path / "receipts" / "run.json"
    artifact_path.parent.mkdir()
    artifact_path.parent.chmod(0o700)
    artifact_path.write_bytes(b"historic")
    artifact_path.chmod(0o600)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, expected_artifact_path=artifact_path)),
        encoding="utf-8",
    )
    prepared = prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=("verify-command", "--opaque-value"),
    )
    timestamp = "2026-07-26T12:00:00.000000Z"
    status_path = run_dir / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "manifest_sha256": prepared.manifest_sha256,
                "state": "succeeded",
                "pid": 123,
                "systemd_unit": None,
                "updated_at": timestamp,
                "started_at": timestamp,
                "finished_at": timestamp,
                "exit_code": 0,
                "signal_number": None,
                "failure_classification": None,
                "resource_usage": {"elapsed_seconds": 0.0, "peak_rss_bytes": None},
                "output_capture": None,
            }
        ),
        encoding="utf-8",
    )
    status_path.chmod(0o400)
    lock_path = run_dir / ".status.lock"
    lock_path.touch(mode=0o600)
    run_dir.chmod(0o500)

    with pytest.raises(
        AutoresearchRunRecordError,
        match="historical records cannot prove artifact attestation status",
    ):
        read_run_record(run_dir=run_dir, runs_root=runs_root)
