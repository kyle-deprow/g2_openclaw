"""Focused tests for the generic and replacement gateway CLI surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from gateway.cli import (
    _choose_whisper_model,
    _parse_gpu_output,
    app,
)
from typer.testing import CliRunner

runner = CliRunner()


def test_parse_gpu_output() -> None:
    assert _parse_gpu_output("NVIDIA RTX 4090, 24576 MiB\n") == ("NVIDIA RTX 4090", 24.0)
    assert _parse_gpu_output("") == (None, 0.0)


def test_choose_whisper_model() -> None:
    assert _choose_whisper_model(0.0, has_gpu=False) == "tiny.en"
    assert _choose_whisper_model(3.9, has_gpu=True) == "base.en"
    assert _choose_whisper_model(4.0, has_gpu=True) == "small.en"
    assert _choose_whisper_model(8.0, has_gpu=True) == "medium.en"


def test_help_exposes_research_and_generic_commands_without_retired_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "research" in result.output
    assert "research-status" in result.output
    assert "autoresearch-next" not in result.output


def test_read_only_research_status_does_not_create_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "missing"

    result = runner.invoke(app, ["research-status", "--root", str(root)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["available"] is False
    assert not root.exists()


def test_read_only_research_status_serializes_projection(tmp_path: Path) -> None:
    with patch("gateway.cli.read_status") as read_status:
        from gateway.research.status import ResearchStatus

        read_status.return_value = ResearchStatus(
            available=True,
            hypothesis_id=None,
            hypothesis_state=None,
            attempt_id=None,
            attempt_state=None,
            stage="idle",
            last_astra_decision=None,
            campaign_status="ACTIVE",
            boundary_failure=None,
            last_event_at=None,
            owner_state="inactive",
            updated_at=None,
            unavailable_reason=None,
        )
        result = runner.invoke(app, ["research-status", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["stage"] == "idle"


def test_read_only_research_status_uses_shared_env_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "research#status?root"
    monkeypatch.setenv("RESEARCH_V2_ROOT", str(root))
    with patch("gateway.cli.read_status") as read_status:
        from gateway.research.status import unavailable_status

        read_status.return_value = unavailable_status("fixture")
        result = runner.invoke(app, ["research-status"])

    assert result.exit_code == 0
    read_status.assert_called_once_with(root)
