"""Integration tests for the detached long-task manifest boundary."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_LAUNCH_REQUESTS,
    DEFAULT_AUTORESEARCH_LONG_RUNS_ROOT,
)
from gateway.autoresearch_runs import (
    OUTPUT_CAPTURE_MAX_BYTES,
    AutoresearchRunRecordError,
    RunFailureClassification,
    RunState,
    command_sha256,
    prepare_run,
    read_run_record,
    write_command_handoff,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LONG_TASK = REPO_ROOT / "scripts" / "run-long-task.sh"


@pytest.fixture(autouse=True)
def launch_request_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTORESEARCH_LAUNCH_REQUESTS_DIR", str(tmp_path / "launch-requests"))


def _manifest(
    run_dir: Path,
    command: tuple[str, ...],
    *,
    working_directory: Path = REPO_ROOT,
    expected_artifact_path: Path | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "iteration": 3,
        "phase": "verification",
        "attempt": 1,
        "task_label": "verification",
        "state_reference_sha256": "a" * 64,
        "instruction_manifest_sha256": "b" * 64,
        "run_directory": str(run_dir),
        "working_directory": str(working_directory),
        "command_sha256": command_sha256(command),
        "expected_artifact_path": (
            str(expected_artifact_path) if expected_artifact_path is not None else None
        ),
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


def test_run_long_task_queues_when_systemd_user_bus_is_unreachable(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("true",)
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    launch_requests = tmp_path / "launch-requests"
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
        env={
            **os.environ,
            "AUTORESEARCH_FORCE_LAUNCH_QUEUE": "1",
            "AUTORESEARCH_LAUNCH_REQUESTS_DIR": str(launch_requests),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"LAUNCH_QUEUED: {run_dir}"
    request_paths = sorted(launch_requests.glob("*.json"))
    assert len(request_paths) == 1
    request_path = request_paths[0]
    assert request_path.name.startswith("attempt-1-")
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(launch_requests.stat().st_mode) == 0o700
    assert json.loads(request_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "run_dir": str(run_dir),
        "runs_root": str(runs_root),
    }
    assert not list(launch_requests.glob(".*.tmp"))
    assert not (run_dir / "status.json").exists()
    assert (run_dir / ".command-handoff.json").exists()


def test_run_long_task_queues_when_systemd_probe_fails(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("true",)
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    launch_requests = tmp_path / "launch-requests"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    _write_command_file(command_file, command)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "systemd-run").write_text(
        "#!/usr/bin/env bash\nexit 1\n",
        encoding="utf-8",
    )
    (shim_dir / "systemd-run").chmod(0o755)
    environment = dict(os.environ)
    environment.pop("AUTORESEARCH_FORCE_LAUNCH_QUEUE", None)
    environment.update(
        {
            "PATH": f"{shim_dir}:{environment['PATH']}",
            "AUTORESEARCH_LAUNCH_REQUESTS_DIR": str(launch_requests),
        }
    )

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
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"LAUNCH_QUEUED: {run_dir}"
    assert len(list(launch_requests.glob("*.json"))) == 1
    assert (run_dir / ".command-handoff.json").exists()


def test_run_long_task_script_defaults_match_python_constants() -> None:
    script = RUN_LONG_TASK.read_text(encoding="utf-8")
    runs_match = re.search(
        r'runs_root="\$\{AUTORESEARCH_RUNS_ROOT:-([^}]+)\}"',
        script,
    )
    launch_requests_match = re.search(
        r'launch_requests_dir="\$\{AUTORESEARCH_LAUNCH_REQUESTS_DIR:-([^}]+)\}"',
        script,
    )

    assert runs_match is not None
    assert launch_requests_match is not None
    assert Path(runs_match.group(1)) == DEFAULT_AUTORESEARCH_LONG_RUNS_ROOT
    assert Path(launch_requests_match.group(1)) == DEFAULT_AUTORESEARCH_LAUNCH_REQUESTS


def test_run_long_task_launch_prepared_uses_only_prepared_run_state(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("true",)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(run_dir, command)), encoding="utf-8")
    prepare_run(
        manifest_path=manifest_path,
        run_dir=run_dir,
        runs_root=runs_root,
        command=command,
    )
    write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "systemd-run").write_text(
        "#!/usr/bin/env bash\nexit 1\n",
        encoding="utf-8",
    )
    (shim_dir / "systemd-run").chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--launch-prepared",
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{shim_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert "detached systemd unit could not be enqueued" in result.stderr
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / ".command-handoff.json").exists()
    assert not (run_dir / "status.json").exists()


@pytest.mark.parametrize("die_case", ("missing_manifest", "already_started"))
def test_run_long_task_launch_prepared_rejects_unprepared_or_started_runs(
    tmp_path: Path,
    die_case: str,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    run_dir.mkdir(parents=True)
    if die_case == "already_started":
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        (run_dir / ".command-handoff.json").write_text("{}", encoding="utf-8")
        (run_dir / "status.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(RUN_LONG_TASK),
            "--launch-prepared",
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    if die_case == "missing_manifest":
        assert "prepared run manifest is missing" in result.stderr
    else:
        assert "status.json exists" in result.stderr


def test_run_long_task_writes_separate_private_secret_free_terminal_capture(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = ("bash", "-lc", "printf stdout-diagnostic; printf stderr-diagnostic >&2")
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
    capture = cast(dict[str, object], status["output_capture"])
    stdout = cast(dict[str, object], capture["stdout"])
    stderr = cast(dict[str, object], capture["stderr"])
    assert stdout["relative_path"] == "stdout.log"
    assert stderr["relative_path"] == "stderr.log"
    assert stdout["eof_observed"] is True
    assert stderr["eof_observed"] is True
    assert (run_dir / "stdout.log").read_bytes() == b"stdout-diagnostic"
    assert (run_dir / "stderr.log").read_bytes() == b"stderr-diagnostic"
    assert stat.S_IMODE((run_dir / "stdout.log").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "stderr.log").stat().st_mode) == 0o600
    assert b"stdout-diagnostic" not in (run_dir / "status.json").read_bytes()
    assert b"stderr-diagnostic" not in (run_dir / "status.json").read_bytes()
    assert "-lc" not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert not command_file.exists()


def test_run_long_task_attests_expected_artifact_before_success(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    artifact_path = tmp_path / "quantipy-runs" / "known-run" / "run.json"
    command = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(artifact_path)!r}); p.parent.mkdir(parents=True); "
            "p.write_bytes(b'{\"success\":true}'); p.chmod(0o600)"
        ),
    )
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(
        json.dumps(
            _manifest(
                run_dir,
                command,
                expected_artifact_path=artifact_path,
            )
        ),
        encoding="utf-8",
    )
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
    attestation = cast(dict[str, object], status["expected_artifact_attestation"])
    artifact_bytes = artifact_path.read_bytes()
    assert attestation["path"] == str(artifact_path)
    assert attestation["size_bytes"] == len(artifact_bytes)
    assert attestation["sha256"] == sha256(artifact_bytes).hexdigest()


def test_run_long_task_missing_expected_artifact_is_not_success(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    artifact_path = tmp_path / "missing" / "run.json"
    command = ("true",)
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(
        json.dumps(
            _manifest(
                run_dir,
                command,
                expected_artifact_path=artifact_path,
            )
        ),
        encoding="utf-8",
    )
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
    assert status["state"] == RunState.FAILED.value
    assert status["failure_classification"] == RunFailureClassification.ARTIFACT_MISSING.value
    assert status["expected_artifact_attestation"] is None


def test_run_long_task_binds_control_plane_to_g2_project_from_target_worktree(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    target_worktree = tmp_path / "quantipy-worktree"
    target_worktree.mkdir()
    command = (
        sys.executable,
        "-c",
        f"import os; assert os.getcwd() == {str(target_worktree)!r}",
    )
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text(
        json.dumps(_manifest(run_dir, command, working_directory=target_worktree)),
        encoding="utf-8",
    )
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
        cwd=target_worktree,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    status = _wait_for_terminal_status(run_dir)
    assert status["state"] == RunState.SUCCEEDED.value


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


def test_operator_stop_preserves_primary_outcome_with_escaped_capture_sentinel(
    tmp_path: Path,
) -> None:
    from gateway.autoresearch_runs import prepare_run, start_run, write_command_handoff

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    startup_marker = run_dir / ".startup-published.json"
    escaped_pid_path = tmp_path / "operator-escaped.pid"
    worker = REPO_ROOT / "scripts" / "run-long-task-worker.sh"
    command = (
        "bash",
        "-lc",
        (
            "printf operator-flush; printf operator-stderr >&2; "
            "trap '' TERM; "
            "setsid bash -c 'trap \"\" TERM; while true; do sleep 1; done' & "
            f"echo $! > {escaped_pid_path}; "
            "while true; do sleep 1; done"
        ),
    )
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
        start_new_session=True,
        env={**os.environ, "AUTORESEARCH_TIMEOUT_TERM_GRACE_SECONDS": "0.2"},
    )
    sentinel_survived = False
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and (
            not startup_marker.exists() or not escaped_pid_path.exists()
        ):
            time.sleep(0.05)
        assert startup_marker.exists()
        assert escaped_pid_path.exists()
        os.killpg(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=8.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
        if escaped_pid_path.exists():
            escaped_pid = int(escaped_pid_path.read_text(encoding="utf-8"))
            with suppress(ProcessLookupError):
                os.kill(escaped_pid, 0)
                sentinel_survived = True
                os.kill(escaped_pid, signal.SIGKILL)

    assert proc.returncode == 0, stderr or stdout
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == RunState.FAILED.value
    assert status["failure_classification"] == RunFailureClassification.OPERATOR_STOPPED.value
    assert status["signal_number"] == 9
    capture = cast(dict[str, object], status["output_capture"])
    stdout_capture = cast(dict[str, object], capture["stdout"])
    stderr_capture = cast(dict[str, object], capture["stderr"])
    assert stdout_capture["eof_observed"] is False
    assert stderr_capture["eof_observed"] is False
    assert sentinel_survived is True
    assert (run_dir / "stdout.log").read_bytes() == b"operator-flush"
    assert (run_dir / "stderr.log").read_bytes() == b"operator-stderr"
    with pytest.raises(AutoresearchRunRecordError, match="startup status already exists"):
        start_run(run_dir=run_dir, pid=999, runs_root=runs_root)


def test_worker_terminalizes_after_direct_child_exits_with_a_live_descendant(
    tmp_path: Path,
) -> None:
    from gateway.autoresearch_runs import prepare_run, write_command_handoff

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    startup_marker = run_dir / ".startup-published.json"
    descendant_pid_path = tmp_path / "descendant.pid"
    worker = REPO_ROOT / "scripts" / "run-long-task-worker.sh"
    command = (
        "bash",
        "-lc",
        f"printf parent-finished; sleep 30 & echo $! > {descendant_pid_path}; exit 0",
    )
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
        stdout, stderr = proc.communicate(timeout=3.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
        if descendant_pid_path.exists():
            descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)

    assert proc.returncode == 0, stderr or stdout
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == RunState.SUCCEEDED.value
    capture = cast(dict[str, object], status["output_capture"])
    assert cast(dict[str, object], capture["stdout"])["eof_observed"] is True
    assert cast(dict[str, object], capture["stderr"])["eof_observed"] is True
    assert (run_dir / "stdout.log").read_bytes() == b"parent-finished"


def test_worker_keeps_escaped_sentinel_alive_and_reports_incomplete_capture(
    tmp_path: Path,
) -> None:
    from gateway.autoresearch_runs import prepare_run, write_command_handoff

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    startup_marker = run_dir / ".startup-published.json"
    descendant_pid_path = tmp_path / "escaped-descendant.pid"
    worker = REPO_ROOT / "scripts" / "run-long-task-worker.sh"
    command = (
        "bash",
        "-lc",
        f"printf escaped-parent; setsid sleep 30 & echo $! > {descendant_pid_path}; exit 0",
    )
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
    sentinel_survived = False
    try:
        stdout, stderr = proc.communicate(timeout=3.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
        if descendant_pid_path.exists():
            descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, 0)
                sentinel_survived = True
                os.kill(descendant_pid, signal.SIGKILL)

    assert proc.returncode == 0, stderr or stdout
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == RunState.FAILED.value
    assert status["failure_classification"] == RunFailureClassification.OUTPUT_CAPTURE_ERROR.value
    capture = cast(dict[str, object], status["output_capture"])
    assert cast(dict[str, object], capture["stdout"])["eof_observed"] is False
    assert cast(dict[str, object], capture["stderr"])["eof_observed"] is False
    assert sentinel_survived is True
    assert (run_dir / "stdout.log").read_bytes() == b"escaped-parent"


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
        "if '--no-block' not in sys.argv:\n"
        "    raise SystemExit(0)\n"
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
            "AUTORESEARCH_LAUNCH_REQUESTS_DIR": str(tmp_path / "launch-requests"),
        },
    )

    assert result.returncode != 0
    argv_text = captured_argv.read_text(encoding="utf-8")
    assert "unique-launch-payload" not in argv_text
    assert str(command_file) not in argv_text
    assert str(run_dir) in argv_text
    assert "--property=MemoryHigh=8G" in argv_text
    assert "--property=MemoryMax=12G" in argv_text
    assert "--property=KillMode=control-group" in argv_text


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
    command = (
        "bash",
        "-lc",
        (
            "printf timeout-flush; printf timeout-stderr >&2; "
            "trap '' TERM; while true; do sleep 1; done"
        ),
    )
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
    assert (run_dir / "stdout.log").read_bytes() == b"timeout-flush"
    assert (run_dir / "stderr.log").read_bytes() == b"timeout-stderr"


def test_run_long_task_drains_large_output_before_recording_success(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    command = (
        sys.executable,
        "-c",
        f"import sys; sys.stdout.buffer.write(b'x' * {OUTPUT_CAPTURE_MAX_BYTES + 1})",
    )
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
    capture = cast(dict[str, object], status["output_capture"])
    stdout = cast(dict[str, object], capture["stdout"])
    assert stdout["bytes_observed"] == OUTPUT_CAPTURE_MAX_BYTES + 1
    assert stdout["bytes_stored"] == OUTPUT_CAPTURE_MAX_BYTES
    assert stdout["truncated"] is True
    assert stdout["eof_observed"] is True
    assert read_run_record(run_dir=run_dir, runs_root=runs_root).status.state is RunState.SUCCEEDED
