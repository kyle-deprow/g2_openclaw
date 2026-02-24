"""Tests for gateway.cli — init-env command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import dotenv_values
from gateway.cli import (
    _choose_whisper_model,
    _detect_gpu,
    _get_local_ip,
    _parse_gpu_output,
    _read_openclaw_config,
    _render_env,
    app,
)
from typer.testing import CliRunner

runner = CliRunner()


# ---------------------------------------------------------------------------
# GPU detection / parsing
# ---------------------------------------------------------------------------


class TestParseGpuOutput:
    """_parse_gpu_output handles nvidia-smi CSV lines."""

    def test_typical_gpu(self) -> None:
        name, vram = _parse_gpu_output("NVIDIA GeForce RTX 3060, 12288 MiB\n")
        assert name == "NVIDIA GeForce RTX 3060"
        assert vram == pytest.approx(12.0, abs=0.1)

    def test_small_gpu(self) -> None:
        name, vram = _parse_gpu_output("NVIDIA GeForce GTX 1050, 2048 MiB\n")
        assert name == "NVIDIA GeForce GTX 1050"
        assert vram == pytest.approx(2.0, abs=0.1)

    def test_empty_output(self) -> None:
        name, vram = _parse_gpu_output("")
        assert name is None
        assert vram == 0.0

    def test_malformed_single_field(self) -> None:
        name, vram = _parse_gpu_output("garbage")
        assert name is None
        assert vram == 0.0

    def test_non_numeric_vram(self) -> None:
        name, vram = _parse_gpu_output("GPU Name, not_a_number MiB\n")
        assert name == "GPU Name"
        assert vram == 0.0


class TestDetectGpu:
    """_detect_gpu calls nvidia-smi and interprets the result."""

    def test_gpu_found(self) -> None:
        fake = MagicMock(
            returncode=0,
            stdout="NVIDIA RTX 4090, 24564 MiB\n",
        )
        with patch("gateway.cli.subprocess.run", return_value=fake) as mock_run:
            name, vram = _detect_gpu()
            mock_run.assert_called_once()
        assert name == "NVIDIA RTX 4090"
        assert vram == pytest.approx(23.99, abs=0.1)

    def test_nvidia_smi_not_found(self) -> None:
        with patch("gateway.cli.subprocess.run", side_effect=FileNotFoundError):
            name, vram = _detect_gpu()
        assert name is None
        assert vram == 0.0

    def test_nvidia_smi_nonzero_exit(self) -> None:
        fake = MagicMock(returncode=1, stdout="")
        with patch("gateway.cli.subprocess.run", return_value=fake):
            name, vram = _detect_gpu()
        assert name is None
        assert vram == 0.0

    def test_nvidia_smi_timeout(self) -> None:
        with patch(
            "gateway.cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10),
        ):
            name, vram = _detect_gpu()
        assert name is None
        assert vram == 0.0


# ---------------------------------------------------------------------------
# Whisper model selection
# ---------------------------------------------------------------------------


class TestChooseWhisperModel:
    """_choose_whisper_model picks appropriate model for VRAM."""

    def test_no_gpu(self) -> None:
        assert _choose_whisper_model(0.0, has_gpu=False) == "tiny.en"

    def test_low_vram(self) -> None:
        assert _choose_whisper_model(2.0, has_gpu=True) == "base.en"

    def test_medium_vram(self) -> None:
        assert _choose_whisper_model(6.0, has_gpu=True) == "small.en"

    def test_high_vram(self) -> None:
        assert _choose_whisper_model(12.0, has_gpu=True) == "medium.en"

    def test_boundary_4gb(self) -> None:
        assert _choose_whisper_model(4.0, has_gpu=True) == "small.en"

    def test_boundary_8gb(self) -> None:
        assert _choose_whisper_model(8.0, has_gpu=True) == "medium.en"

    def test_boundary_just_under_4gb(self) -> None:
        assert _choose_whisper_model(3.99, has_gpu=True) == "base.en"


# ---------------------------------------------------------------------------
# OpenClaw config reading
# ---------------------------------------------------------------------------


class TestReadOpenClawConfig:
    """_read_openclaw_config reads token/port from JSON."""

    def test_full_config(self, tmp_path: Path) -> None:
        cfg = {"gateway": {"auth": {"token": "oc-tok-123"}, "port": 19000}}
        p = tmp_path / "openclaw.json"
        p.write_text(json.dumps(cfg))
        token, port = _read_openclaw_config(p)
        assert token == "oc-tok-123"
        assert port == 19000

    def test_missing_token(self, tmp_path: Path) -> None:
        cfg = {"gateway": {"port": 19000}}
        p = tmp_path / "openclaw.json"
        p.write_text(json.dumps(cfg))
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 19000

    def test_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.json"
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 18789

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "openclaw.json"
        p.write_text("NOT JSON")
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 18789

    def test_empty_gateway_section(self, tmp_path: Path) -> None:
        p = tmp_path / "openclaw.json"
        p.write_text(json.dumps({"gateway": {}}))
        token, port = _read_openclaw_config(p)
        assert token is None
        assert port == 18789


# ---------------------------------------------------------------------------
# .env rendering
# ---------------------------------------------------------------------------


class TestRenderEnv:
    """_render_env produces correct .env content."""

    def test_contains_all_keys(self) -> None:
        content = _render_env(
            local_ip="10.0.0.5",
            gateway_token="abc123",
            whisper_model="small.en",
            whisper_device="cuda",
            whisper_compute_type="float16",
            gpu_label="NVIDIA RTX 3060 (12.0 GB)",
            openclaw_port=18789,
            openclaw_token="oc-tok",
        )
        for key in (
            "GATEWAY_HOST",
            "GATEWAY_PORT",
            "GATEWAY_TOKEN",
            "WHISPER_MODEL",
            "WHISPER_DEVICE",
            "WHISPER_COMPUTE_TYPE",
            "OPENCLAW_HOST",
            "OPENCLAW_PORT",
            "OPENCLAW_GATEWAY_TOKEN",
            "AGENT_TIMEOUT",
        ):
            assert key in content

    def test_no_openclaw_token_comment(self) -> None:
        content = _render_env(
            local_ip="10.0.0.5",
            gateway_token="abc",
            whisper_model="tiny.en",
            whisper_device="cpu",
            whisper_compute_type="int8",
            gpu_label="No NVIDIA GPU detected (CPU mode)",
            openclaw_port=18789,
            openclaw_token=None,
        )
        assert "Not found in ~/.openclaw/openclaw.json" in content
        assert "OPENCLAW_GATEWAY_TOKEN=\n" in content

    def test_with_openclaw_token_comment(self) -> None:
        content = _render_env(
            local_ip="10.0.0.5",
            gateway_token="abc",
            whisper_model="small.en",
            whisper_device="cuda",
            whisper_compute_type="float16",
            gpu_label="NVIDIA RTX 3060 (12.0 GB)",
            openclaw_port=19000,
            openclaw_token="secret-oc",
        )
        assert "Read from ~/.openclaw/openclaw.json" in content
        assert "OPENCLAW_GATEWAY_TOKEN=secret-oc" in content


# ---------------------------------------------------------------------------
# .env parseable by python-dotenv
# ---------------------------------------------------------------------------


class TestEnvParseable:
    """Generated .env must be parseable by python-dotenv."""

    def test_dotenv_loads_all_keys(self, tmp_path: Path) -> None:
        content = _render_env(
            local_ip="192.168.1.42",
            gateway_token="tok123",
            whisper_model="medium.en",
            whisper_device="cuda",
            whisper_compute_type="float16",
            gpu_label="NVIDIA RTX 4090 (24.0 GB)",
            openclaw_port=18789,
            openclaw_token="oc-abc",
        )
        env_file = tmp_path / ".env"
        env_file.write_text(content)
        values = dotenv_values(env_file)
        assert values["GATEWAY_HOST"] == "0.0.0.0"
        assert values["GATEWAY_PORT"] == "8765"
        assert values["GATEWAY_TOKEN"] == "tok123"
        assert values["WHISPER_MODEL"] == "medium.en"
        assert values["WHISPER_DEVICE"] == "cuda"
        assert values["WHISPER_COMPUTE_TYPE"] == "float16"
        assert values["OPENCLAW_HOST"] == "127.0.0.1"
        assert values["OPENCLAW_PORT"] == "18789"
        assert values["OPENCLAW_GATEWAY_TOKEN"] == "oc-abc"
        assert values["AGENT_TIMEOUT"] == "120"


# ---------------------------------------------------------------------------
# CLI integration — init-env command
# ---------------------------------------------------------------------------


class TestInitEnvCommand:
    """Full CLI integration via typer.testing.CliRunner."""

    @staticmethod
    def _mock_detect_no_gpu() -> tuple[None, float]:
        return None, 0.0

    @staticmethod
    def _mock_detect_gpu() -> tuple[str, float]:
        return "NVIDIA RTX 3060", 12.0

    def test_creates_env_file(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.99"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "WHISPER_DEVICE=cpu" in content
        assert "WHISPER_MODEL=tiny.en" in content

    def test_existing_env_without_force(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OLD=value\n")
        result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 1
        assert "already" in result.output and "exists" in result.output
        # Original file untouched
        assert (tmp_path / ".env").read_text() == "OLD=value\n"

    def test_existing_env_with_force(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OLD=value\n")
        with (
            patch("gateway.cli._detect_gpu", return_value=("NVIDIA RTX 3060", 12.0)),
            patch("gateway.cli._read_openclaw_config", return_value=("oc-tok", 19000)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.1"),
        ):
            result = runner.invoke(app, ["--force", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        content = (tmp_path / ".env").read_text()
        assert "WHISPER_DEVICE=cuda" in content
        assert "OPENCLAW_PORT=19000" in content
        assert "OLD=value" not in content

    def test_gpu_detected_sets_cuda(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=("NVIDIA RTX 4090", 24.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.1"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        content = (tmp_path / ".env").read_text()
        assert "WHISPER_DEVICE=cuda" in content
        assert "WHISPER_COMPUTE_TYPE=float16" in content
        assert "WHISPER_MODEL=medium.en" in content

    def test_summary_panel_printed(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.10"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "init-env summary" in result.output

    def test_generated_file_parseable(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=("RTX A5000", 8.0)),
            patch("gateway.cli._read_openclaw_config", return_value=("tok-x", 18789)),
            patch("gateway.cli._get_local_ip", return_value="172.16.0.5"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        values = dotenv_values(tmp_path / ".env")
        assert values["GATEWAY_HOST"] == "0.0.0.0"
        assert values["WHISPER_DEVICE"] == "cuda"
        assert values["OPENCLAW_GATEWAY_TOKEN"] == "tok-x"


# ---------------------------------------------------------------------------
# Local IP helper
# ---------------------------------------------------------------------------


class TestInitEnvG2App:
    """init-env generates g2_app/.env.local when g2_app/ exists."""

    def test_creates_env_local_when_g2_app_exists(self, tmp_path: Path) -> None:
        (tmp_path / "g2_app").mkdir()
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.50"),
            patch("gateway.cli.secrets.token_hex", return_value="aabbccdd" * 6),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        env_local = tmp_path / "g2_app" / ".env.local"
        assert env_local.exists()
        content = env_local.read_text()
        assert "VITE_GATEWAY_URL=ws://192.168.1.50:8765?token=" in content
        assert "aabbccdd" * 6 in content
        assert content.startswith("# Auto-generated by: python -m gateway init-env")

    def test_skips_when_g2_app_dir_missing(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="192.168.1.50"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert not (tmp_path / "g2_app" / ".env.local").exists()

    def test_force_overwrites_existing_env_local(self, tmp_path: Path) -> None:
        g2_dir = tmp_path / "g2_app"
        g2_dir.mkdir()
        (g2_dir / ".env.local").write_text("OLD_CONTENT=1\n")
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.5"),
        ):
            result = runner.invoke(app, ["--force", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        content = (g2_dir / ".env.local").read_text()
        assert "VITE_GATEWAY_URL=ws://10.0.0.5:8765?token=" in content
        assert "OLD_CONTENT" not in content

    def test_existing_env_local_without_force_warns(self, tmp_path: Path) -> None:
        g2_dir = tmp_path / "g2_app"
        g2_dir.mkdir()
        (g2_dir / ".env.local").write_text("KEEP=1\n")
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.5"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        # Original file untouched
        assert (g2_dir / ".env.local").read_text() == "KEEP=1\n"
        assert "already exists" in result.output

    def test_url_format_correct(self, tmp_path: Path) -> None:
        (tmp_path / "g2_app").mkdir()
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="172.16.0.1"),
            patch("gateway.cli.secrets.token_hex", return_value="deadbeef" * 6),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        values = dotenv_values(tmp_path / "g2_app" / ".env.local")
        expected_url = "ws://172.16.0.1:8765?token=" + "deadbeef" * 6
        assert values["VITE_GATEWAY_URL"] == expected_url

    def test_summary_includes_g2_env(self, tmp_path: Path) -> None:
        (tmp_path / "g2_app").mkdir()
        with (
            patch("gateway.cli._detect_gpu", return_value=(None, 0.0)),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._get_local_ip", return_value="10.0.0.1"),
        ):
            result = runner.invoke(app, ["--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "G2 app env" in result.output


class TestGetLocalIp:
    """_get_local_ip falls back gracefully."""

    def test_returns_string(self) -> None:
        ip = _get_local_ip()
        assert isinstance(ip, str)
        parts = ip.split(".")
        assert len(parts) == 4
