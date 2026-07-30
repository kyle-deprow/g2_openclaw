from __future__ import annotations

import subprocess

import pytest
from gateway.autoresearch_systemd import (
    SystemdUnitStateError,
    systemd_unit_is_active,
)


@pytest.mark.parametrize(
    ("returncode", "show_output"),
    (
        (3, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"),
        (4, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"),
    ),
)
def test_systemd_unit_is_active_accepts_only_the_exact_inactive_tuple(
    returncode: int,
    show_output: str,
) -> None:
    # Arrange
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[2] == "is-active":
            return subprocess.CompletedProcess(command, returncode, "", "")
        return subprocess.CompletedProcess(command, 0, show_output, "")

    # Act
    active = systemd_unit_is_active("openclaw-long-task-1-1.service", run_command=run_command)

    # Assert
    assert active is False
    assert calls[1] == (
        "systemctl",
        "--user",
        "show",
        "openclaw-long-task-1-1.service",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
    )


@pytest.mark.parametrize(
    ("returncode", "show_returncode", "show_output"),
    (
        (3, 0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"),
        (4, 0, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"),
        (3, 0, "LoadState=loaded\nActiveState=active\nSubState=dead\n"),
        (4, 0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\nExtra=value\n"),
        (3, 1, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"),
        (1, 0, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"),
    ),
)
def test_systemd_unit_is_active_rejects_non_exact_inactive_evidence(
    returncode: int,
    show_returncode: int,
    show_output: str,
) -> None:
    # Arrange
    def run_command(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if command[2] == "is-active":
            return subprocess.CompletedProcess(command, returncode, "", "")
        return subprocess.CompletedProcess(command, show_returncode, show_output, "")

    # Act / Assert
    with pytest.raises(SystemdUnitStateError):
        systemd_unit_is_active("openclaw-long-task-1-1.service", run_command=run_command)
