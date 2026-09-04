"""Static contract tests for the OpenClaw gateway healthcheck script."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTHCHECK_SCRIPT = REPO_ROOT / "scripts" / "openclaw-gateway-healthcheck.sh"
STOP_SUPERVISOR = "systemctl --user stop quantipy-autoresearch-supervisor.service"
STOP_GATEWAY = "systemctl --user stop openclaw-gateway.service"
START_GATEWAY = "systemctl --user start openclaw-gateway.service"
RESTART_GATEWAY = "systemctl --user restart openclaw-gateway.service"
START_SUPERVISOR = "systemctl --user start quantipy-autoresearch-supervisor.service"


def _script() -> str:
    return HEALTHCHECK_SCRIPT.read_text(encoding="utf-8")


def test_healthcheck_script_is_valid_bash_and_sets_runtime_directory() -> None:
    result = subprocess.run(
        ["bash", "-n", str(HEALTHCHECK_SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    script = _script()
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script
    assert "export XDG_RUNTIME_DIR=/run/user/1000" in script


def test_healthcheck_has_three_spaced_health_probes() -> None:
    script = _script()
    probe_loop = re.search(
        r"for attempt in 1 2 3; do(?P<body>.*?)\ndone",
        script,
        re.DOTALL,
    )

    assert 'readonly HEALTH_URL="http://127.0.0.1:18789/health"' in script
    assert probe_loop is not None
    probe_body = probe_loop.group("body")
    assert "timeout 8 curl -sf \"$HEALTH_URL\"" in probe_body
    assert "failure_count=$((failure_count + 1))" in probe_body
    assert "if (( attempt < 3 )); then" in probe_body
    assert "sleep 5 || true" in probe_body


def test_healthcheck_pins_restart_logging_and_rotation_contract() -> None:
    script = _script()

    assert "logger -t openclaw-healthcheck" in script
    assert "readonly MAX_LOG_DB_BYTES=$((256 * 1024 * 1024))" in script
    assert 'readonly LOG_DB="${CODEX_HOME}/logs_2.sqlite"' in script
    assert 'readonly LOG_DB_SHM="${CODEX_HOME}/logs_2.sqlite-shm"' in script
    assert 'readonly LOG_DB_WAL="${CODEX_HOME}/logs_2.sqlite-wal"' in script
    assert (
        'readonly ARCHIVE_DIR="/home/dev/.openclaw/agents/autoresearch-pm/agent/'
        'codex-home/.archived-logs"'
    ) in script
    assert "date -u +%Y%m%dT%H%M%S%NZ" in script
    assert 'mv -- "$source" "${ARCHIVE_DIR}/${source_name}.${archive_timestamp}"' in script
    assert 'readonly LOCK_FILE="${XDG_RUNTIME_DIR}/openclaw-gateway-healthcheck.lock"' in script
    assert "flock -n \"$lock_fd\"" in script
    assert "systemctl --user restart openclaw-gateway.service" in script
    assert "systemctl --user start quantipy-autoresearch-supervisor.service" in script


def test_rotation_call_sites_are_limited_to_explicit_or_all_failed_paths() -> None:
    script = _script()
    helper = re.search(
        r"(?ms)^rotate_logs_while_gateway_stopped\(\) \{.*?^\}",
        script,
    )
    assert helper is not None

    executable_text = script[: helper.start()] + script[helper.end() :]
    call_sites = list(
        re.finditer(r"(?m)^\s{4}rotate_logs_while_gateway_stopped$", executable_text)
    )
    assert len(call_sites) == 2

    explicit_start = executable_text.index("if (( rotate_logs_requested )); then")
    explicit_end = executable_text.index("failure_count=0")
    failed_start = executable_text.index("if (( failure_count == 3 )); then")
    assert explicit_start < call_sites[0].start() < explicit_end
    assert failed_start < call_sites[1].start()

    normal_probe_path = executable_text[explicit_end:failed_start]
    assert "rotate_logs_while_gateway_stopped" not in normal_probe_path


def test_maintenance_lock_and_service_order_surround_both_rotation_paths() -> None:
    script = _script()
    explicit_start = script.index("if (( rotate_logs_requested )); then")
    explicit_end = script.index("failure_count=0")
    failed_start = script.index("if (( failure_count == 3 )); then")

    explicit_path = script[explicit_start:explicit_end]
    failed_path = script[failed_start:]

    assert script.count("if ! acquire_maintenance_lock; then") == 2
    assert script.count("flock -n") == 1

    explicit_acquire = explicit_path.index("if ! acquire_maintenance_lock; then")
    explicit_stop_supervisor = explicit_path.index(STOP_SUPERVISOR)
    explicit_stop_gateway = explicit_path.index(STOP_GATEWAY)
    explicit_rotation = explicit_path.index("rotate_logs_while_gateway_stopped")
    explicit_start_gateway = explicit_path.index(START_GATEWAY)
    explicit_start_supervisor = explicit_path.index(START_SUPERVISOR)
    assert explicit_acquire < explicit_stop_supervisor < explicit_rotation
    assert explicit_acquire < explicit_stop_gateway < explicit_rotation
    assert explicit_start_gateway > explicit_rotation
    assert explicit_start_supervisor > explicit_rotation
    assert explicit_path.index("release_maintenance_lock") > explicit_start_supervisor

    failed_acquire = failed_path.index("if ! acquire_maintenance_lock; then")
    failed_stop_supervisor = failed_path.index(STOP_SUPERVISOR)
    failed_stop_gateway = failed_path.index(STOP_GATEWAY)
    failed_rotation = failed_path.index("rotate_logs_while_gateway_stopped")
    failed_restart_gateway = failed_path.index(RESTART_GATEWAY)
    failed_start_supervisor = failed_path.index(START_SUPERVISOR)
    assert failed_acquire < failed_stop_supervisor < failed_rotation
    assert failed_acquire < failed_stop_gateway < failed_rotation
    assert failed_restart_gateway > failed_rotation
    assert failed_start_supervisor > failed_restart_gateway
    assert failed_path.index("release_maintenance_lock") > failed_start_supervisor
