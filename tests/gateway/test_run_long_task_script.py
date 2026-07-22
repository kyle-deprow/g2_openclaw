"""Tests for the detached long-task launcher."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LONG_TASK = REPO_ROOT / "scripts" / "run-long-task.sh"


def _wait_for_terminal_status(run_dir: Path, timeout_seconds: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    status_path = run_dir / "status.json"

    while time.monotonic() < deadline:
        if status_path.exists():
            status = cast(dict[str, object], json.loads(status_path.read_text(encoding="utf-8")))
            if status["status"] in {"succeeded", "failed"}:
                return status
        time.sleep(0.05)

    raise AssertionError(f"Timed out waiting for terminal status in {run_dir}")


def _wait_for_running_metadata(run_dir: Path, timeout_seconds: float = 2.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    status_path = run_dir / "status.json"
    pid_path = run_dir / "pid"
    started_at_path = run_dir / "started_at"

    while time.monotonic() < deadline:
        if status_path.exists() and pid_path.exists() and started_at_path.exists():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status["status"] == "running":
                pid = int(pid_path.read_text(encoding="utf-8").strip())
                started_at = started_at_path.read_text(encoding="utf-8").strip()
                return {
                    "status": status,
                    "pid": pid,
                    "started_at": started_at,
                }
        time.sleep(0.01)

    raise AssertionError(f"Timed out waiting for running metadata in {run_dir}")


def test_run_long_task_detaches_and_records_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "long-task"
    command = [
        "bash",
        str(RUN_LONG_TASK),
        "--run-dir",
        str(run_dir),
        "--",
        "bash",
        "-lc",
        "printf 'hello\\n'; printf 'warn\\n' >&2; sleep 1",
    ]

    started = time.monotonic()
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 2.0

    running = _wait_for_running_metadata(run_dir)
    assert running["status"] == {
        "status": "running",
        "pid": running["pid"],
        "started_at": running["started_at"],
        "exit_code": None,
    }
    started_at = running["started_at"]
    assert isinstance(started_at, str)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", started_at)

    pid = int((run_dir / "pid").read_text(encoding="utf-8").strip())
    assert pid > 0
    assert pid == running["pid"]
    os.kill(pid, 0)
    cgroup = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    assert "openclaw-gateway.service" not in cgroup
    assert "openclaw-long-task-" in cgroup

    terminal_status = _wait_for_terminal_status(run_dir)
    assert terminal_status == {
        "status": "succeeded",
        "pid": pid,
        "started_at": terminal_status["started_at"],
        "exit_code": 0,
    }
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        (run_dir / "started_at").read_text(encoding="utf-8").strip(),
    )
    assert (run_dir / "exit_code").read_text(encoding="utf-8").strip() == "0"
    assert (run_dir / "stdout.log").read_text(encoding="utf-8") == "hello\n"
    assert (run_dir / "stderr.log").read_text(encoding="utf-8") == "warn\n"
    assert not (run_dir / ".startup-published.json").exists()


def test_run_long_task_preserves_nonzero_exit_code_in_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "failing-task"
    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--",
            "bash",
            "-lc",
            "printf 'before-fail\\n'; printf 'bad\\n' >&2; exit 7",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    status = _wait_for_terminal_status(run_dir)
    assert status["status"] == "failed"
    assert status["exit_code"] == 7
    assert (run_dir / "exit_code").read_text(encoding="utf-8").strip() == "7"
    assert (run_dir / "stdout.log").read_text(encoding="utf-8") == "before-fail\n"
    assert (run_dir / "stderr.log").read_text(encoding="utf-8") == "bad\n"


def test_run_long_task_rejects_malformed_args(tmp_path: Path) -> None:
    relative_result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            "relative-path",
            "--",
            "true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert relative_result.returncode != 0
    assert "--run-dir must be an absolute path" in relative_result.stderr

    missing_command_result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(tmp_path / "missing-command"),
            "--",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_command_result.returncode != 0
    assert "command is required after --" in missing_command_result.stderr

    stale_metadata_dir = tmp_path / "stale-metadata"
    stale_metadata_dir.mkdir()
    (stale_metadata_dir / "status.json").write_text("{}", encoding="utf-8")
    stale_result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(stale_metadata_dir),
            "--",
            "true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stale_result.returncode != 0
    assert "run directory already contains status.json" in stale_result.stderr


def test_run_long_task_fails_closed_without_setsid(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()

    for command_path in (
        "/bin/bash",
        "/usr/bin/basename",
        "/usr/bin/date",
        "/usr/bin/mkdir",
        "/usr/bin/mktemp",
        "/usr/bin/mv",
    ):
        source = Path(command_path)
        if source.exists():
            (shim_dir / source.name).symlink_to(source)

    run_dir = tmp_path / "missing-setsid"
    result = subprocess.run(
        [
            "/bin/bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--",
            "true",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(shim_dir)},
    )

    assert result.returncode != 0
    assert "setsid is required for detached launch" in result.stderr
    assert not (run_dir / "status.json").exists()


def test_run_long_task_fails_closed_without_systemd_run(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()

    for command_path in (
        "/bin/bash",
        "/usr/bin/basename",
        "/usr/bin/date",
        "/usr/bin/mkdir",
        "/usr/bin/mktemp",
        "/usr/bin/mv",
        "/usr/bin/python3",
        "/usr/bin/setsid",
    ):
        source = Path(command_path)
        if source.exists():
            (shim_dir / source.name).symlink_to(source)

    run_dir = tmp_path / "missing-systemd-run"
    result = subprocess.run(
        [
            "/bin/bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--",
            "true",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(shim_dir)},
    )

    assert result.returncode != 0
    assert "systemd-run is required for isolated detached launch" in result.stderr
    assert not (run_dir / "status.json").exists()
