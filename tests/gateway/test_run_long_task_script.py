"""Tests for the detached long-task launcher."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LONG_TASK = REPO_ROOT / "scripts" / "run-long-task.sh"
RUN_LONG_TASK_WORKER = REPO_ROOT / "scripts" / "run-long-task-worker.sh"


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


def _wait_for_file(path: Path, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path}")


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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


def test_run_long_task_survives_caller_exit_and_publishes_terminal_metadata(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "caller-exit-task"
    marker = tmp_path / "continued-after-caller-exit"
    caller = subprocess.Popen(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--",
            "bash",
            "-lc",
            f"sleep 1; printf 'continued\\n' > {marker}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert caller.wait(timeout=3) == 0, caller.stderr.read() if caller.stderr else ""
    running = _wait_for_running_metadata(run_dir)
    assert cast(dict[str, object], running["status"])["exit_code"] is None
    assert not marker.exists()

    terminal_status = _wait_for_terminal_status(run_dir)
    assert terminal_status["status"] == "succeeded"
    assert terminal_status["exit_code"] == 0
    assert marker.read_text(encoding="utf-8") == "continued\n"
    assert terminal_status["pid"] == running["pid"]
    assert terminal_status["started_at"] == running["started_at"]


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


def test_worker_waits_for_delayed_sigterm_child_before_terminal_metadata(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "delayed-term-task"
    run_dir.mkdir()
    startup_marker = run_dir / ".startup-published.json"
    term_started = tmp_path / "term-started"
    release = tmp_path / "release-child"
    child_script = tmp_path / "delayed-term-child.py"
    child_script.write_text(
        """from pathlib import Path
import signal
import sys
import time

term_started = Path(sys.argv[1])
release = Path(sys.argv[2])
handling_term = False


def handle_term(_signum: int, _frame: object) -> None:
    global handling_term
    if handling_term:
        return
    handling_term = True
    term_started.write_text("handling\\n", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
    raise SystemExit(7)


signal.signal(signal.SIGTERM, handle_term)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )

    worker = subprocess.Popen(
        [
            "bash",
            str(RUN_LONG_TASK_WORKER),
            str(run_dir),
            str(startup_marker),
            sys.executable,
            str(child_script),
            str(term_started),
            str(release),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        running = _wait_for_running_metadata(run_dir)
        child_pid = cast(int, running["pid"])
        os.kill(worker.pid, signal.SIGTERM)
        _wait_for_file(term_started)

        deadline = time.monotonic() + 0.5
        observed_status: dict[str, object] | None = None
        while time.monotonic() < deadline:
            observed_status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            assert observed_status is not None
            if observed_status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)

        assert observed_status == {
            "status": "running",
            "pid": child_pid,
            "started_at": running["started_at"],
            "exit_code": None,
        }
        assert not (run_dir / "exit_code").exists()
        assert _process_is_alive(child_pid)

        release.touch()
        assert worker.wait(timeout=3) == 0, worker.stderr.read() if worker.stderr else ""
        terminal_status = _wait_for_terminal_status(run_dir)
        assert terminal_status == {
            "status": "failed",
            "pid": child_pid,
            "started_at": running["started_at"],
            "exit_code": 7,
        }
        assert (run_dir / "exit_code").read_text(encoding="utf-8").strip() == "7"
        assert not _process_is_alive(child_pid)
    finally:
        release.touch()
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=3)


def test_run_long_task_unit_stop_records_terminal_signal_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "stopped-task"
    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--run-dir",
            str(run_dir),
            "--",
            "bash",
            "-lc",
            "sleep 30",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    running = _wait_for_running_metadata(run_dir)
    unit_result = subprocess.run(
        ["systemctl", "--user", "whoami", str(running["pid"])],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unit_result.returncode == 0, unit_result.stderr
    unit_name = unit_result.stdout.strip()
    assert unit_name.startswith("openclaw-long-task-")

    stop_result = subprocess.run(
        ["systemctl", "--user", "stop", unit_name],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stop_result.returncode == 0, stop_result.stderr

    terminal_status = _wait_for_terminal_status(run_dir)
    assert terminal_status == {
        "status": "failed",
        "pid": running["pid"],
        "started_at": running["started_at"],
        "exit_code": 143,
    }
    assert (run_dir / "exit_code").read_text(encoding="utf-8").strip() == "143"


def test_run_long_task_propagates_caller_path_and_uv_directory(tmp_path: Path) -> None:
    shim_dir = tmp_path / "caller-bin"
    shim_dir.mkdir()

    uv_path = shim_dir / "uv"
    uv_path.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    uv_path.chmod(0o755)

    captured_path = tmp_path / "transient-path"
    systemd_run_path = shim_dir / "systemd-run"
    systemd_run_path.write_text(
        """#!/bin/bash
set -euo pipefail

transient_path=""
saw_no_block=0
saw_wait=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --setenv=PATH=*)
      transient_path="${1#--setenv=PATH=}"
      ;;
    --no-block)
      saw_no_block=1
      ;;
    --wait)
      saw_wait=1
      ;;
    --)
      shift
      break
      ;;
  esac
  shift
done

[[ -n "$transient_path" ]]
printf '%s\\n' "$transient_path" > "$CAPTURE_PATH"
printf '%s %s\\n' "$saw_no_block" "$saw_wait" > "$CAPTURE_FLAGS_PATH"
PATH="$transient_path" exec "$@"
""",
        encoding="utf-8",
    )
    systemd_run_path.chmod(0o755)
    systemctl_path = shim_dir / "systemctl"
    systemctl_path.write_text(
        """#!/bin/bash
printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nResult=success\\n'
""",
        encoding="utf-8",
    )
    systemctl_path.chmod(0o755)

    caller_path = f"{shim_dir}:/usr/bin:/bin"
    run_dir = tmp_path / "path-task"
    flags_path = tmp_path / "systemd-run-flags"
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
        env={
            **os.environ,
            "PATH": caller_path,
            "CAPTURE_PATH": str(captured_path),
            "CAPTURE_FLAGS_PATH": str(flags_path),
        },
    )

    assert result.returncode == 0, result.stderr
    assert captured_path.read_text(encoding="utf-8").strip() == f"{caller_path}:{shim_dir}"
    assert flags_path.read_text(encoding="utf-8").strip() == "1 0"
    assert _wait_for_terminal_status(run_dir)["status"] == "succeeded"


def test_run_long_task_fails_closed_when_transient_unit_fails_to_start(tmp_path: Path) -> None:
    shim_dir = tmp_path / "failed-unit-bin"
    shim_dir.mkdir()

    for command_path in (
        "/bin/bash",
        "/usr/bin/basename",
        "/usr/bin/date",
        "/usr/bin/dirname",
        "/usr/bin/mkdir",
        "/usr/bin/mktemp",
        "/usr/bin/mv",
        "/usr/bin/python3",
        "/usr/bin/setsid",
        "/usr/bin/seq",
        "/usr/bin/rm",
    ):
        source = Path(command_path)
        if source.exists():
            (shim_dir / source.name).symlink_to(source)

    (shim_dir / "uv").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (shim_dir / "uv").chmod(0o755)
    (shim_dir / "systemd-run").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (shim_dir / "systemd-run").chmod(0o755)
    (shim_dir / "systemctl").write_text(
        """#!/bin/bash
if [[ "$*" == *" stop "* ]]; then
  exit 0
fi
printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nResult=exit-code\\n'
""",
        encoding="utf-8",
    )
    (shim_dir / "systemctl").chmod(0o755)

    run_dir = tmp_path / "failed-unit"
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
    assert "failed before publishing startup metadata" in result.stderr
    assert not (run_dir / "status.json").exists()


def test_run_long_task_fails_closed_without_uv(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()

    for command_path in (
        "/bin/bash",
        "/usr/bin/python3",
        "/usr/bin/setsid",
        "/usr/bin/systemctl",
        "/usr/bin/systemd-run",
    ):
        source = Path(command_path)
        if source.exists():
            (shim_dir / source.name).symlink_to(source)

    run_dir = tmp_path / "missing-uv"
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
    assert "uv is required for detached launch" in result.stderr
    assert not (run_dir / "status.json").exists()


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
        "/usr/bin/dirname",
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
        "/usr/bin/systemctl",
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
