"""Strict systemd unit-state inspection for detached autoresearch work."""

from __future__ import annotations

import subprocess
from collections.abc import Callable


class SystemdUnitStateError(Exception):
    """Systemd did not provide conclusive unit-state evidence."""


SystemdCommandRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]

_INACTIVE_SHOW_OUTPUT_BY_IS_ACTIVE_RETURN_CODE = {
    3: (
        "LoadState=loaded",
        "ActiveState=inactive",
        "SubState=dead",
    ),
    4: (
        "LoadState=not-found",
        "ActiveState=inactive",
        "SubState=dead",
    ),
}


def systemd_unit_is_active(unit: str, *, run_command: SystemdCommandRunner) -> bool:
    """Return activity only when systemd provides conclusive unit-state evidence."""
    active = run_command(("systemctl", "--user", "is-active", "--quiet", unit))
    if active.returncode == 0:
        return True
    expected_show_output = _INACTIVE_SHOW_OUTPUT_BY_IS_ACTIVE_RETURN_CODE.get(active.returncode)
    if expected_show_output is None:
        raise SystemdUnitStateError("is-active did not prove the unit state")

    shown = run_command(
        (
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
        )
    )
    if shown.returncode != 0 or tuple(shown.stdout.splitlines()) != expected_show_output:
        raise SystemdUnitStateError("show did not prove the unit is inactive")
    return False
