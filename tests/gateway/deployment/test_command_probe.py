"""Black-box tests for the replacement read-only status command probe."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import gateway.deployment.command_probe as command_probe
import pytest
from gateway.deployment.command_probe import CommandProbeError


def _embedded_cli(tmp_path: Path) -> Path:
    cli = (
        tmp_path
        / "node_modules"
        / "@openclaw"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex"
        / "bin"
        / "codex.js"
    )
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.touch()
    return cli


def _unavailable_frame(*, available: bool = False) -> str:
    return json.dumps(
        {
            "type": "autoresearch_status",
            "hypothesisId": None,
            "hypothesisState": None,
            "attemptId": None,
            "attemptState": None,
            "stage": "idle",
            "lastAstraDecision": None,
            "campaignStatus": None,
            "boundaryFailure": None,
            "lastEventAt": None,
            "ownerState": "unknown",
            "updatedAt": None,
            "available": available,
            "unavailableReason": "research state database is missing" if not available else None,
        }
    )


def test_extract_commanded_invocations_uses_private_dynamic_root() -> None:
    root = Path("/private/probe-root")
    commands = command_probe.extract_commanded_invocations(root)

    assert commands == ("gateway-cli research-status --root /private/probe-root",)
    assert "/tmp/g2-research-status-probe" not in commands[0]


def test_probe_rejects_unapproved_command_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_probe,
        "extract_commanded_invocations",
        lambda _root: ("gateway-cli autoresearch-next /tmp/legacy-state.json",),
    )

    with pytest.raises(CommandProbeError, match="unsupported research status command contract"):
        command_probe.run_probe(tmp_path, _embedded_cli(tmp_path))


def test_probe_accepts_exact_unavailable_status_and_creates_no_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, _unavailable_frame(), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = command_probe.run_probe(tmp_path, _embedded_cli(tmp_path))

    assert result == 0
    args, kwargs = calls[0]
    assert "-P" in args
    assert args[args.index("-P") + 1] == command_probe.COMMAND_PROFILE
    assert "--" in args
    assert args[args.index("--") + 1 : args.index("--") + 5] == [
        "gateway-cli",
        "research-status",
        "--root",
        args[args.index("--") + 4],
    ]
    assert "timeout" in kwargs
    probe_root = Path(args[args.index("--") + 4])
    assert not probe_root.exists()
    assert "command-contract probe passed" in capsys.readouterr().out


def test_probe_provisions_only_isolated_command_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_like_home = tmp_path / "production-codex-home"
    production_like_home.mkdir()
    observed: dict[str, str] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        isolated_home = Path(str(environment["CODEX_HOME"]))
        observed["home"] = str(isolated_home)
        observed["config"] = (isolated_home / "config.toml").read_text()
        observed["cwd"] = str(kwargs["cwd"])
        return subprocess.CompletedProcess(args, 0, _unavailable_frame(), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert command_probe.run_probe(production_like_home, _embedded_cli(tmp_path)) == 0

    assert observed["home"] != str(production_like_home)
    assert observed["cwd"] == observed["home"]
    assert observed["config"] == command_probe._COMMAND_PROFILE_CONFIG
    assert not (production_like_home / "config.toml").exists()


def test_probe_fails_closed_on_healthy_or_malformed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outputs = [json.dumps({"available": True}), "not json"]

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, outputs.pop(0), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert command_probe.run_probe(tmp_path, _embedded_cli(tmp_path)) == 1
    assert command_probe.run_probe(tmp_path, _embedded_cli(tmp_path)) == 1
    captured = capsys.readouterr().err
    assert "unexpected status frame shape" in captured
    assert "did not emit JSON" in captured


def test_probe_fails_if_status_command_creates_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        probe_root = Path(args[args.index("--") + 4])
        probe_root.mkdir()
        return subprocess.CompletedProcess(args, 0, _unavailable_frame(), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert command_probe.run_probe(tmp_path, _embedded_cli(tmp_path)) == 1
    assert "created the private missing root" in capsys.readouterr().err


def test_probe_fails_closed_on_nonzero_status_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 2, "", "status command failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = command_probe.run_probe(tmp_path, _embedded_cli(tmp_path))

    assert result == 1
    assert "exit code 2" in capsys.readouterr().err


def test_probe_fails_closed_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert command_probe.run_probe(tmp_path, _embedded_cli(tmp_path)) == 1
    assert "timed out" in capsys.readouterr().err


def test_probe_fails_closed_on_execution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("sandbox executable unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert command_probe.run_probe(tmp_path, _embedded_cli(tmp_path)) == 1
    assert "could not execute" in capsys.readouterr().err
