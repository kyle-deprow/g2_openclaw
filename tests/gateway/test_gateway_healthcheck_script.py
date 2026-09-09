"""Static contract tests for the OpenClaw gateway healthcheck script."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTHCHECK_SCRIPT = REPO_ROOT / "scripts" / "openclaw-gateway-healthcheck.sh"
STOP_GATEWAY = "systemctl --user stop openclaw-gateway.service"
START_GATEWAY = "systemctl --user start openclaw-gateway.service"
RESTART_GATEWAY = "systemctl --user restart openclaw-gateway.service"


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
    assert 'timeout 8 curl -sf "$HEALTH_URL"' in probe_body
    assert "failure_count=$((failure_count + 1))" in probe_body
    assert "if (( attempt < 3 )); then" in probe_body
    assert "sleep 5 || true" in probe_body


def test_healthcheck_pins_gateway_restart_and_retires_research_maintenance() -> None:
    script = _script()

    assert "logger -t openclaw-healthcheck" in script
    assert 'readonly LOCK_FILE="${XDG_RUNTIME_DIR}/openclaw-gateway-healthcheck.lock"' in script
    assert 'flock -n "$lock_fd"' in script
    assert "systemctl --user restart openclaw-gateway.service" in script
    assert "quantipy-autoresearch-supervisor.service" not in script

    # Research-owner PM logs are not a gateway healthcheck concern.  Keeping
    # these strings out also prevents a future size threshold from coupling
    # gateway restarts to unrelated autoresearch state.
    for retired in (
        "autoresearch-pm",
        "MAX_LOG_DB_BYTES",
        "LOG_DB",
        "rotate_logs",
        "rotate-logs",
        "archived-logs",
    ):
        assert retired not in script


def test_restart_path_uses_only_the_gateway_healthcheck_lock() -> None:
    script = _script()
    failed_start = script.index("if (( failure_count == 3 )); then")
    failed_path = script[failed_start:]

    assert script.count("if ! acquire_healthcheck_lock; then") == 1
    assert script.count("flock -n") == 1
    failed_acquire = failed_path.index("if ! acquire_healthcheck_lock; then")
    failed_restart_gateway = failed_path.index(RESTART_GATEWAY)
    assert failed_restart_gateway > failed_acquire
    assert STOP_GATEWAY not in failed_path
    assert START_GATEWAY not in failed_path
    assert failed_path.index("release_healthcheck_lock") > failed_restart_gateway
