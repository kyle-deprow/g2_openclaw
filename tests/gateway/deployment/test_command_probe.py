"""Black-box tests for the publication-time Codex command-contract probe."""

from __future__ import annotations

import subprocess
from pathlib import Path

import gateway.autoresearch_supervisor as supervisor
import gateway.deployment.command_probe as command_probe
import pytest
from gateway.deployment.command_probe import (
    CommandProbeError,
    extract_commanded_invocations,
)


def test_extract_commanded_invocations_preserves_all_four_wake_commands() -> None:
    assert extract_commanded_invocations() == (
        "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
        "/home/dev/.openclaw/autoresearch/quantipy-state.json",
        "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
        "/home/dev/.openclaw/autoresearch/quantipy-state.json",
        "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
        "/home/dev/.openclaw/autoresearch/quantipy-state.json",
        "/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next "
        "/home/dev/.openclaw/autoresearch/quantipy-state.json",
    )


def test_extract_commanded_invocations_fails_closed_when_a_message_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor, "WAKE_MESSAGE", "missing command contract")

    with pytest.raises(CommandProbeError, match="WAKE_MESSAGE"):
        extract_commanded_invocations()


def test_probe_accepts_state_missing_signature_when_parent_is_traversable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "quantipy-state.json"
    command = f"/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next {state_path}"
    stderr = (
        "Usage: gateway-cli autoresearch-next [OPTIONS] STATE_PATH\n"
        "Invalid value for 'STATE_PATH': File\n"
        f"'{state_path}' does not exist.\n"
    )

    monkeypatch.setattr(command_probe, "extract_commanded_invocations", lambda: (command,))
    monkeypatch.setattr(
        command_probe,
        "resolve_embedded_codex_binary",
        lambda _: ("node", "/tmp/codex.js"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, "", stderr),
    )

    result = command_probe.run_probe(tmp_path / "codex-home", tmp_path / "codex.js")

    assert result == 0
    assert "accepted state-file-missing failure" in capsys.readouterr().out


def test_probe_rejects_state_missing_signature_when_parent_is_untraversable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.mkdir()
    state_path = blocked_parent / "quantipy-state.json"
    command = f"/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next {state_path}"
    stderr = (
        "Usage: gateway-cli autoresearch-next [OPTIONS] STATE_PATH\n"
        "Invalid value for 'STATE_PATH': File\n"
        f"'{state_path}' does not exist.\n"
    )

    monkeypatch.setattr(command_probe, "extract_commanded_invocations", lambda: (command,))
    monkeypatch.setattr(
        command_probe,
        "resolve_embedded_codex_binary",
        lambda _: ("node", "/tmp/codex.js"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, "", stderr),
    )
    blocked_parent.chmod(0)
    try:
        result = command_probe.run_probe(tmp_path / "codex-home", tmp_path / "codex.js")
    finally:
        blocked_parent.chmod(0o755)

    assert result == 1
    assert "state-file-missing signature rejected" in capsys.readouterr().err
