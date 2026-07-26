"""Focused tests for detached autoresearch run records."""

from __future__ import annotations

import json
import stat
from hashlib import sha256
from pathlib import Path

import pytest
from gateway.autoresearch_runs import (
    AutoresearchRunRecordError,
    RunFailureClassification,
    RunManifest,
    RunState,
    complete_run,
    consume_command_handoff,
    consume_command_input_file,
    create_command_input_file_from_stdin,
    prepare_run,
    read_run_record,
    start_run,
    validate_startup_marker,
    write_command_handoff,
)


def _manifest(run_dir: Path) -> dict[str, object]:
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
        "expected_artifact_path": None,
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

    with pytest.raises(AutoresearchRunRecordError, match="live run identity"):
        validate_startup_marker(run_dir=run_dir, marker_path=marker_path, runs_root=runs_root)
