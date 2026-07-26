"""Integration tests for the detached long-task manifest boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest
from gateway.autoresearch_runs import (
    AutoresearchRunRecordError,
    RunFailureClassification,
    RunState,
    command_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LONG_TASK = REPO_ROOT / "scripts" / "run-long-task.sh"


def _manifest(run_dir: Path, command: tuple[str, ...]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "iteration": 3,
        "phase": "verification",
        "attempt": 1,
        "task_label": "verification",
        "state_reference_sha256": "a" * 64,
        "instruction_manifest_sha256": "b" * 64,
        "run_directory": str(run_dir),
        "working_directory": str(REPO_ROOT),
        "command_sha256": command_sha256(command),
        "expected_artifact_path": None,
        "timeout_seconds": None,
    }


def _write_command_file(path: Path, command: tuple[str, ...]) -> None:
    path.write_text(json.dumps({"command": list(command)}), encoding="utf-8")
    path.chmod(0o600)


def _wait_for_terminal_status(run_dir: Path) -> dict[str, object]:
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        status_path = run_dir / "status.json"
        if status_path.exists():
            status = cast(dict[str, object], json.loads(status_path.read_text(encoding="utf-8")))
            if status["state"] != RunState.RUNNING.value:
                return status
        time.sleep(0.05)
    raise AssertionError("timed out waiting for detached long task")


def test_run_long_task_writes_a_secret_free_terminal_record(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("bash", "-lc", "exit 0")
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    _write_command_file(command_file, command)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    status = _wait_for_terminal_status(run_dir)
    assert status["state"] == RunState.SUCCEEDED.value
    assert status["failure_classification"] is None
    assert "-lc" not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert not command_file.exists()


def test_run_long_task_preserves_an_unattributed_kill_as_a_process_error(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = (sys.executable, "-c", "import os; os.kill(os.getpid(), 9)")
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    _write_command_file(command_file, command)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    status = _wait_for_terminal_status(run_dir)
    assert status["failure_classification"] == RunFailureClassification.PROCESS_ERROR.value


def test_run_long_task_rejects_positional_command_payloads(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, ("true",))), encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--",
            "false",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "positional command payloads are not supported" in result.stderr


def test_run_long_task_rejects_a_command_file_not_bound_to_manifest(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, ("true",))), encoding="utf-8")
    _write_command_file(command_file, ("false",))

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "command_sha256" in result.stderr


def test_run_long_task_rejects_secret_bearing_argv(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("bash", "-lc", "exit 0")
    secret_command = ("bash", "--api-key", "sk-testsecret000000000000")
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    _write_command_file(command_file, secret_command)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "credential files or inherited authentication" in result.stderr


def test_run_long_task_operator_stop_kills_a_term_resistant_command_once(
    tmp_path: Path,
) -> None:
    from gateway.autoresearch_runs import prepare_run, start_run, write_command_handoff

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    startup_marker = run_dir / ".startup-published.json"
    worker = REPO_ROOT / "scripts" / "run-long-task-worker.sh"
    command = ("bash", "-lc", "trap '' TERM; while true; do sleep 1; done")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=command,
    )
    write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)

    proc = subprocess.Popen(
        [
            "bash",
            str(worker),
            str(run_dir),
            str(runs_root),
            str(startup_marker),
            "openclaw-long-task-test.service",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS": "0.2"},
    )
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not startup_marker.exists():
            time.sleep(0.05)
        assert startup_marker.exists()
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=8.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)

    assert proc.returncode == 0, stderr or stdout
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == RunState.FAILED.value
    assert status["failure_classification"] == RunFailureClassification.OPERATOR_STOPPED.value
    assert status["signal_number"] == 9
    with pytest.raises(AutoresearchRunRecordError, match="startup status already exists"):
        start_run(run_dir=run_dir, pid=999, runs_root=runs_root)


def test_run_long_task_does_not_put_command_payload_in_systemd_argv(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("bash", "-lc", "printf unique-launch-payload")
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    captured_argv = tmp_path / "systemd-argv.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    _write_command_file(command_file, command)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "systemd-run").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "open(os.environ['CAPTURED_ARGV'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    (shim_dir / "systemd-run").chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "CAPTURED_ARGV": str(captured_argv),
        },
    )

    assert result.returncode != 0
    argv_text = captured_argv.read_text(encoding="utf-8")
    assert "unique-launch-payload" not in argv_text
    assert str(command_file) not in argv_text
    assert str(run_dir) in argv_text


def test_run_long_task_does_not_misclassify_a_child_exit_124_as_timeout(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("bash", "-lc", "exit 124")
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    _write_command_file(command_file, command)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    status = _wait_for_terminal_status(run_dir)
    assert status["failure_classification"] == RunFailureClassification.PROCESS_ERROR.value


def test_run_long_task_timeout_kills_a_term_resistant_command(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("bash", "-lc", "trap '' TERM; while true; do sleep 1; done")
    manifest = _manifest(run_dir, command)
    manifest["timeout_seconds"] = 0.2
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_command_file(command_file, command)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS": "0.2"},
    )

    assert result.returncode == 0, result.stderr
    status = _wait_for_terminal_status(run_dir)
    assert status["failure_classification"] == RunFailureClassification.TIMEOUT.value
    assert status["signal_number"] == 9
