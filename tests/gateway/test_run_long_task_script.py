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
    DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
)
from gateway.autoresearch_runs import (
    OUTPUT_CAPTURE_MAX_BYTES,
    AutoresearchRunRecordError,
    RunFailureClassification,
    RunState,
    capture_prepared_run_identity,
    command_sha256,
    prepare_run,
    read_run_record,
    start_run,
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


def _identity_json(run_dir: Path, runs_root: Path) -> str:
    return json.dumps(
        capture_prepared_run_identity(run_dir=run_dir, runs_root=runs_root).to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )


def _run_launcher_with_fake_identity_output(
    tmp_path: Path, identity_output: str
) -> subprocess.CompletedProcess[str]:
    fake_repo = tmp_path / "launcher-repo"
    (fake_repo / "scripts").mkdir(parents=True)
    (fake_repo / ".venv" / "bin").mkdir(parents=True)
    launcher = fake_repo / "scripts" / "run-long-task.sh"
    launcher.write_text(RUN_LONG_TASK.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(0o755)
    runtime = fake_repo / ".venv" / "bin" / "python"
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if sys.argv[3] == 'prepared-identity':\n"
        "    sys.stdout.write(os.environ['FAKE_IDENTITY_OUTPUT'])\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    for command in ("setsid", "systemd-run", "systemctl", "timeout", "uv"):
        shim = shim_dir / command
        shim.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    manifest_path = tmp_path / "manifest.json"
    command_file = tmp_path / "command.json"
    manifest_path.write_text("{}", encoding="utf-8")
    _write_command_file(command_file, ("true",))
    return subprocess.run(
        [
            "bash",
            str(launcher),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--manifest",
            str(manifest_path),
            "--command-file",
            str(command_file),
        ],
        cwd=fake_repo,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "AUTORESEARCH_FORCE_LAUNCH_QUEUE": "1",
            "AUTORESEARCH_LAUNCH_REQUESTS_DIR": str(tmp_path / "launch-requests"),
            "AUTORESEARCH_LONG_TASK_TMPDIR": str(tmp_path / "long-task-tmp"),
            "FAKE_IDENTITY_OUTPUT": identity_output,
        },
    )


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


def _run_worker_with_fake_runtime(
    tmp_path: Path,
    *,
    timeout_seconds: float | None,
    archival_exit_code: int = 0,
    outcome: str = "success",
    real_archival: bool = False,
    identity_json_override: str | None = None,
    identity_mutation: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]], dict[str, object]]:
    fake_repo = tmp_path / "worker-repo"
    (fake_repo / "scripts").mkdir(parents=True)
    (fake_repo / ".venv" / "bin").mkdir(parents=True)
    worker = fake_repo / "scripts" / "run-long-task-worker.sh"
    worker.write_text(
        (REPO_ROOT / "scripts" / "run-long-task-worker.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    worker.chmod(0o755)
    runtime = fake_repo / ".venv" / "bin" / "python"
    runtime.write_text(
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

operation = sys.argv[3]
arguments = sys.argv[4:]
run_dir = Path(arguments[arguments.index('--run-dir') + 1])
log_path = Path(os.environ['FAKE_RUNTIME_LOG'])
with log_path.open('a', encoding='utf-8') as log:
    log.write(json.dumps({'operation': operation, 'arguments': arguments}) + '\\n')

def argument(name):
    return arguments[arguments.index(name) + 1]

if operation == 'prepare-output-capture':
    run_dir.mkdir(parents=True, exist_ok=True)
    for stream in ('stdout', 'stderr'):
        (run_dir / f'{stream}.log').write_bytes(b'')
elif operation == 'validate-prepared-identity':
    if os.environ.get('FAKE_REAL_ARCHIVAL') != '1':
        subprocess.run(
            [
                sys.executable,
                '-m',
                'gateway.autoresearch_runs',
                operation,
                *arguments,
            ],
            check=True,
        )
elif operation == 'capture-output-stream':
    stream = argument('--stream')
    (run_dir / f'{stream}.log').write_bytes(sys.stdin.buffer.read())
elif operation == 'supervise-command':
    (run_dir / '.startup-published.json').write_text('startup', encoding='utf-8')
    os.write(3, b'fake-stdout')
    os.write(4, b'fake-stderr')
    timeout = json.loads((run_dir / 'manifest.json').read_text())['timeout_seconds']
    time.sleep(0.25 if timeout is not None else 0.02)
elif operation == 'consume-supervised-command-result':
    timed_out = json.loads((run_dir / 'manifest.json').read_text())['timeout_seconds'] is not None
    if timed_out:
        sys.stdout.write('137\\n9\\n')
    elif os.environ['FAKE_WORKER_OUTCOME'] == 'process_error':
        sys.stdout.write('1\\n\\n')
    else:
        sys.stdout.write('0\\n\\n')
elif operation == 'archive-timeout-partial-run':
    if os.environ.get('FAKE_REAL_ARCHIVAL') == '1':
        os.execv(
            sys.executable,
            [sys.executable, '-m', 'gateway.autoresearch_runs', operation, *arguments],
        )
    else:
        exit_code = int(os.environ['FAKE_ARCHIVE_EXIT_CODE'])
        if exit_code:
            print(os.environ['FAKE_ARCHIVE_DIAGNOSTIC'], file=sys.stderr)
            raise SystemExit(exit_code)
elif operation == 'complete':
    timed_out = '--timed-out' in arguments
    if timed_out:
        failure_classification = 'timeout'
    elif '--operator-stopped' in arguments:
        failure_classification = 'operator_stopped'
    elif '--resource-exhausted' in arguments:
        failure_classification = 'resource_exhausted'
    elif '--exit-code' in arguments and arguments[arguments.index('--exit-code') + 1] != '0':
        failure_classification = 'process_error'
    else:
        failure_classification = None
    status = {
        'state': 'failed' if failure_classification is not None else 'succeeded',
        'failure_classification': failure_classification,
    }
    (run_dir / 'status.json').write_text(json.dumps(status), encoding='utf-8')
""",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    systemctl = fake_repo / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *property=Result* ]]; then\n'
        "  printf '%s\\n' \"${FAKE_SYSTEMD_RESULT:-}\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "iteration-3" / "verification" / "attempt-1"
    if real_archival:
        run_dir.parent.mkdir(parents=True)
        command = ("fake-command",)
        malformed_artifact_path = (
            DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / "autoresearch-i7-abcdef1-v5" / "result.json"
        )
        manifest_path = tmp_path / "source-manifest.json"
        manifest = _manifest(
            run_dir,
            command,
            expected_artifact_path=malformed_artifact_path,
        )
        manifest["timeout_seconds"] = timeout_seconds
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        prepare_run(
            manifest_path=manifest_path,
            run_dir=run_dir,
            runs_root=runs_root,
            command=command,
        )
        write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)
    else:
        command = ("fake-command",)
        manifest_path = tmp_path / "source-manifest.json"
        manifest = _manifest(run_dir, command)
        manifest["timeout_seconds"] = timeout_seconds
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        prepare_run(
            manifest_path=manifest_path,
            run_dir=run_dir,
            runs_root=runs_root,
            command=command,
        )
        write_command_handoff(run_dir=run_dir, runs_root=runs_root, command=command)
    identity_json = _identity_json(run_dir, runs_root)
    if identity_mutation == "run-inode":
        identity = cast(dict[str, object], json.loads(identity_json))
        identity["run_inode"] = int(cast(int, identity["run_inode"])) + 1
        identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    elif identity_mutation == "handoff-swap":
        handoff_path = run_dir / ".command-handoff.json"
        replacement = tmp_path / "replacement-handoff.json"
        replacement.write_bytes(handoff_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, handoff_path)
    elif identity_mutation == "manifest-swap":
        manifest_path = run_dir / "manifest.json"
        manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
        manifest["task_label"] = "identity-swap"
        manifest_path.chmod(0o600)
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o400)
    if identity_json_override is not None:
        identity_json = identity_json_override
    if real_archival:
        start_run(run_dir=run_dir, pid=os.getpid(), runs_root=runs_root)
    log_path = tmp_path / "runtime.log"
    diagnostic = "ARCHIVAL_FAILURE_SENTINEL"
    worker_arguments = [
        "bash",
        str(worker),
        str(run_dir),
        str(runs_root),
        str(run_dir / ".startup-published.json"),
        "openclaw-long-task-test.service",
        identity_json,
    ]
    worker_environment = {
        **os.environ,
        "PATH": f"{fake_repo}:{os.environ['PATH']}",
        "FAKE_RUNTIME_LOG": str(log_path),
        "FAKE_ARCHIVE_EXIT_CODE": str(archival_exit_code),
        "FAKE_ARCHIVE_DIAGNOSTIC": diagnostic,
        "FAKE_WORKER_OUTCOME": outcome,
        "FAKE_SYSTEMD_RESULT": "oom-kill" if outcome == "oom" else "",
        "FAKE_REAL_ARCHIVAL": "1" if real_archival else "0",
        "PYTHONPATH": os.pathsep.join(
            value for value in (str(REPO_ROOT), os.environ.get("PYTHONPATH")) if value
        ),
    }
    if outcome == "operator_stopped":
        process = subprocess.Popen(
            worker_arguments,
            cwd=fake_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=worker_environment,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not (run_dir / ".startup-published.json").exists():
            time.sleep(0.01)
        assert (run_dir / ".startup-published.json").exists()
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5.0)
        result = subprocess.CompletedProcess(worker_arguments, process.returncode, stdout, stderr)
    else:
        result = subprocess.run(
            worker_arguments,
            cwd=fake_repo,
            check=False,
            capture_output=True,
            text=True,
            env=worker_environment,
        )
    operations = [
        cast(dict[str, object], json.loads(line))
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    status = (
        cast(dict[str, object], json.loads((run_dir / "status.json").read_text()))
        if (run_dir / "status.json").exists()
        else {}
    )
    return result, operations, status


@pytest.mark.parametrize("failure", ("run-inode", "handoff-swap", "manifest-swap"))
def test_worker_rejects_identity_mismatch_before_runtime_mutation(
    tmp_path: Path, failure: str
) -> None:
    result, operations, status = _run_worker_with_fake_runtime(
        tmp_path,
        timeout_seconds=None,
        identity_mutation=failure,
    )

    assert result.returncode != 0
    assert [operation["operation"] for operation in operations] == ["validate-prepared-identity"]
    assert status == {}


@pytest.mark.parametrize("identity_json", ("not-json", "x" * 4097))
def test_worker_rejects_malformed_or_oversized_identity_before_runtime_mutation(
    tmp_path: Path, identity_json: str
) -> None:
    result, operations, status = _run_worker_with_fake_runtime(
        tmp_path,
        timeout_seconds=None,
        identity_json_override=identity_json,
    )

    assert result.returncode != 0
    assert [operation["operation"] for operation in operations] == ["validate-prepared-identity"]
    assert status == {}


def test_worker_timeout_archival_failure_terminalizes_timeout_and_returns_nonzero(
    tmp_path: Path,
) -> None:
    result, operations, status = _run_worker_with_fake_runtime(
        tmp_path,
        timeout_seconds=0.05,
        archival_exit_code=23,
    )

    operation_names = [str(operation["operation"]) for operation in operations]
    assert result.returncode == 1
    assert status == {"state": "failed", "failure_classification": "timeout"}
    assert "archive-timeout-partial-run" in operation_names
    assert "complete" in operation_names
    assert operation_names.index("archive-timeout-partial-run") < operation_names.index("complete")
    complete = next(operation for operation in operations if operation["operation"] == "complete")
    assert "--timed-out" in cast(list[str], complete["arguments"])
    assert "ERROR: timed-out partial-run archival failed" in result.stderr
    assert "ARCHIVAL_FAILURE_SENTINEL" in result.stderr


def test_worker_real_archival_failure_from_malformed_expected_path_terminalizes_timeout(
    tmp_path: Path,
) -> None:
    result, operations, status = _run_worker_with_fake_runtime(
        tmp_path,
        timeout_seconds=0.05,
        real_archival=True,
    )

    operation_names = [str(operation["operation"]) for operation in operations]
    assert result.returncode == 1
    assert status == {"state": "failed", "failure_classification": "timeout"}
    assert operation_names.index("archive-timeout-partial-run") < operation_names.index("complete")
    assert "ERROR: timed-out partial-run archival failed" in result.stderr
    assert (
        "expected artifact path must be exactly <artifact_root>/<safe-run-id>/run.json"
        in result.stderr
    )


def test_worker_non_timeout_flow_does_not_invoke_archival(tmp_path: Path) -> None:
    result, operations, status = _run_worker_with_fake_runtime(
        tmp_path,
        timeout_seconds=None,
    )

    operation_names = [str(operation["operation"]) for operation in operations]
    assert result.returncode == 0
    assert status == {"state": "succeeded", "failure_classification": None}
    assert "archive-timeout-partial-run" not in operation_names
    assert operation_names[-1] == "complete"


@pytest.mark.parametrize("outcome", ("process_error", "operator_stopped", "oom"))
def test_worker_non_timeout_failure_flows_do_not_invoke_archival(
    tmp_path: Path,
    outcome: str,
) -> None:
    result, operations, status = _run_worker_with_fake_runtime(
        tmp_path,
        timeout_seconds=None,
        outcome=outcome,
    )

    expected_classification = {
        "process_error": "process_error",
        "operator_stopped": "operator_stopped",
        "oom": "resource_exhausted",
    }[outcome]
    operation_names = [str(operation["operation"]) for operation in operations]
    assert result.returncode == 0
    assert status == {
        "state": "failed",
        "failure_classification": expected_classification,
    }
    assert "archive-timeout-partial-run" not in operation_names


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


@pytest.mark.parametrize(
    ("identity_output", "error"),
    (
        ("", "output is empty"),
        ("{}\n{}", "output is multiline"),
        ("x" * 4097, "output exceeds 4096 bytes"),
    ),
)
def test_run_long_task_rejects_invalid_launcher_identity_output(
    tmp_path: Path, identity_output: str, error: str
) -> None:
    result = _run_launcher_with_fake_identity_output(tmp_path, identity_output)

    assert result.returncode != 0
    assert error in result.stderr


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
    from gateway.autoresearch_runs import prepare_run, write_command_handoff

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
            _identity_json(run_dir, runs_root),
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
            _identity_json(run_dir, runs_root),
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
            _identity_json(run_dir, runs_root),
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
    argv = cast(list[str], json.loads(argv_text))
    worker_script = str(RUN_LONG_TASK.parent / "run-long-task-worker.sh")
    worker_index = argv.index(worker_script)
    assert argv[worker_index + 1 : worker_index + 6] == [
        str(run_dir),
        str(runs_root),
        str(run_dir / ".startup-published.json"),
        argv[worker_index + 4],
        _identity_json(run_dir, runs_root),
    ]
    assert "unique-launch-payload" not in argv_text
    assert str(command_file) not in argv_text
    assert str(run_dir) in argv_text
    assert "--property=MemoryHigh=10G" in argv_text
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
