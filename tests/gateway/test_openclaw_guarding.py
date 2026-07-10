"""Focused tests for OpenClaw binary resolution and guarding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from gateway.cli import (
    _OpenClawResolutionError,
    _OpenClawVersionError,
    _require_openclaw_binary,
    _resolve_openclaw_executable,
    _ResolvedOpenClaw,
    app,
)
from typer.testing import CliRunner

runner = CliRunner()


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_resolve_openclaw_prefers_user_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_bin = tmp_path / ".local/share/pnpm/openclaw"
    path_bin = tmp_path / "bin/openclaw"
    _make_executable(user_bin)
    _make_executable(path_bin)
    monkeypatch.delenv("OPENCLAW_BIN", raising=False)
    monkeypatch.setenv("PATH", str(path_bin.parent))

    with patch("gateway.cli.Path.home", return_value=tmp_path):
        assert _resolve_openclaw_executable() == user_bin


def test_resolve_openclaw_honors_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom/openclaw"
    preferred = tmp_path / ".local/share/pnpm/openclaw"
    _make_executable(override)
    _make_executable(preferred)
    monkeypatch.setenv("OPENCLAW_BIN", str(override))
    monkeypatch.setenv("PATH", "")

    with patch("gateway.cli.Path.home", return_value=tmp_path):
        assert _resolve_openclaw_executable() == override


def test_resolve_openclaw_fails_closed_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENCLAW_BIN", raising=False)
    monkeypatch.setenv("PATH", "")

    with (
        patch("gateway.cli.Path.home", return_value=tmp_path),
        pytest.raises(_OpenClawResolutionError, match="OpenClaw executable not found"),
    ):
        _resolve_openclaw_executable()


def test_require_openclaw_rejects_old_version(tmp_path: Path) -> None:
    executable = tmp_path / "openclaw"
    _make_executable(executable)

    with (
        patch("gateway.cli._resolve_openclaw_executable", return_value=executable),
        patch(
            "gateway.cli.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="openclaw 2026.6.10\n", stderr=""),
        ),
        pytest.raises(_OpenClawVersionError, match="too old"),
    ):
        _require_openclaw_binary()


def test_require_openclaw_rejects_newer_version(tmp_path: Path) -> None:
    executable = tmp_path / "openclaw"
    _make_executable(executable)

    with (
        patch("gateway.cli._resolve_openclaw_executable", return_value=executable),
        patch(
            "gateway.cli.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="openclaw 2026.6.12\n", stderr=""),
        ),
        pytest.raises(_OpenClawVersionError, match=r"too new; need exactly 2026\.6\.11"),
    ):
        _require_openclaw_binary()


@pytest.mark.parametrize(
    "version_token",
    ["2026.6.11-beta.1", "2026.6.11+build", "2026.6.11.1"],
)
def test_require_openclaw_rejects_unstable_exact_prefix_version(
    tmp_path: Path, version_token: str
) -> None:
    executable = tmp_path / "openclaw"
    _make_executable(executable)

    with (
        patch("gateway.cli._resolve_openclaw_executable", return_value=executable),
        patch(
            "gateway.cli.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=f"openclaw {version_token}\n", stderr=""),
        ),
        pytest.raises(_OpenClawVersionError, match="need exactly"),
    ):
        _require_openclaw_binary()


def test_launch_uses_resolved_openclaw_binary_for_start(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        calls.append(cmd)
        return MagicMock(returncode=0)

    with (
        patch("gateway.cli._PROJECT_ROOT", tmp_path),
        patch("gateway.cli._check_mempalace_health", return_value=True),
        patch("gateway.otel_setup.init_otel", return_value=lambda: None),
        patch(
            "gateway.cli._require_openclaw_binary",
            return_value=_ResolvedOpenClaw(
                Path("/resolved/openclaw"),
                "2026.6.11",
                (2026, 6, 11),
            ),
        ),
        patch("gateway.cli._read_gateway_port", return_value=8765),
        patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
        patch("gateway.cli._is_port_open", side_effect=[False, True, True]),
        patch("gateway.cli._wait_for_port", return_value=True),
        patch("gateway.cli._vite_health_check", return_value=True),
        patch("gateway.cli.Path.is_file", return_value=False),
        patch("gateway.cli.subprocess.run", side_effect=_fake_run),
    ):
        result = runner.invoke(app, ["launch", "--no-simulator"])

    assert result.exit_code == 0
    assert ["/resolved/openclaw", "daemon", "start"] in calls
