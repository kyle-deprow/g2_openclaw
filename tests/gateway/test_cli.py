"""Tests for gateway.cli — init-env command."""

from __future__ import annotations

import json
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import dotenv_values
from gateway.autoresearch_runner import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    QUANTIPY_RECEIPT_PATHS,
    AutoresearchState,
    FinalDecision,
    FinalDecisionArtifact,
    MetricDirection,
    Phase,
    ReviewVerdict,
    SetupContextArtifact,
)
from gateway.cli import (
    _active_target_writer_processes,
    _choose_whisper_model,
    _detect_gpu,
    _get_local_ip,
    _parse_gpu_output,
    _read_openclaw_config,
    _render_env,
    _signal_process_group,
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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "WHISPER_DEVICE=cpu" in content
        assert "WHISPER_MODEL=tiny.en" in content

    def test_existing_env_without_force(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OLD=value\n")
        result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--force", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "init-env summary" in result.output

    def test_generated_file_parseable(self, tmp_path: Path) -> None:
        with (
            patch("gateway.cli._detect_gpu", return_value=("RTX A5000", 8.0)),
            patch("gateway.cli._read_openclaw_config", return_value=("tok-x", 18789)),
            patch("gateway.cli._get_local_ip", return_value="172.16.0.5"),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        values = dotenv_values(tmp_path / ".env")
        assert values["GATEWAY_HOST"] == "0.0.0.0"
        assert values["WHISPER_DEVICE"] == "cuda"
        assert values["OPENCLAW_GATEWAY_TOKEN"] == "tok-x"


class TestAutoresearchCliCommands:
    @staticmethod
    def _write_quantipy_receipts(root: Path) -> None:
        for relative_path in QUANTIPY_RECEIPT_PATHS.values():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"fixture for {relative_path}\n", encoding="utf-8")

    def test_autoresearch_advance_persists_state(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        artifact_path = tmp_path / "artifact.json"
        output_path = tmp_path / "state-out.json"
        state_path.write_text(json.dumps(AutoresearchState().to_dict()), encoding="utf-8")
        artifact_path.write_text(
            json.dumps(
                SetupContextArtifact(
                    goal="Find a profitable intraday alpha",
                    metric_name="OOS Sharpe net",
                    metric_direction=MetricDirection.MAXIMIZE,
                    target_repo="/home/dev/repos/quantipy",
                    writable_scope="src/quantipy/alpha",
                    baseline_summary="Baseline OOS Sharpe net is 0.18.",
                    hard_constraints=("No overnight holds",),
                    data_sources=("qp.prices()",),
                ).to_dict()
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "autoresearch-advance",
                str(state_path),
                str(artifact_path),
                "--output",
                str(output_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
            ],
        )

        assert result.exit_code == 0
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        assert saved["phase"] == "setup_context"
        assert saved["setup"]["metric_name"] == "OOS Sharpe net"

    def test_autoresearch_mark_memory_and_start_next_persist_state(
        self,
        tmp_path: Path,
    ) -> None:
        repeat_state = AutoresearchState(
            phase=Phase.REPEAT,
            iteration=3,
            setup=SetupContextArtifact(
                goal="Find a profitable intraday alpha",
                metric_name="OOS Sharpe net",
                metric_direction=MetricDirection.MAXIMIZE,
                target_repo="/home/dev/repos/quantipy",
                writable_scope="src/quantipy/alpha",
                baseline_summary="Baseline OOS Sharpe net is 0.18.",
                hard_constraints=("No overnight holds",),
                data_sources=("qp.prices()",),
            ),
            final_decision=FinalDecisionArtifact(
                decision=FinalDecision.KEEP,
                recommended_metric_name="OOS Sharpe net",
                recommended_metric_value=0.38,
                reviewer_verdict=ReviewVerdict.PASS.value,
                rationale="Improves baseline without review blockers.",
                log_summary="KEEP vwap_obv_intraday with updated baseline review.",
                continue_loop=True,
                memory_write_required=True,
            ),
        )
        state_path = tmp_path / "repeat-state.json"
        memory_state_path = tmp_path / "memory-state.json"
        next_state_path = tmp_path / "next-state.json"
        state_path.write_text(json.dumps(repeat_state.to_dict()), encoding="utf-8")

        mark_result = runner.invoke(
            app,
            [
                "autoresearch-mark-memory",
                str(state_path),
                "--output",
                str(memory_state_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
            ],
        )
        assert mark_result.exit_code == 0
        marked = json.loads(memory_state_path.read_text(encoding="utf-8"))
        assert marked["memory_written"] is True

        next_result = runner.invoke(
            app,
            [
                "autoresearch-start-next",
                str(memory_state_path),
                "--output",
                str(next_state_path),
                "--openclaw-config",
                str(DEFAULT_OPENCLAW_CONFIG_PATH),
            ],
        )
        assert next_result.exit_code == 0
        next_state = json.loads(next_state_path.read_text(encoding="utf-8"))
        assert next_state["phase"] == "setup_context"
        assert next_state["iteration"] == 4
        assert next_state["setup"]["metric_name"] == "OOS Sharpe net"

    def test_autoresearch_next_rejects_active_target_writer(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        quantipy_root = tmp_path / "quantipy"
        self._write_quantipy_receipts(quantipy_root)
        state_path.write_text(json.dumps(AutoresearchState().to_dict()), encoding="utf-8")

        with (
            patch("gateway.cli._git_status_short", return_value=()),
            patch(
                "gateway.cli._active_target_writer_processes",
                return_value=("123 uv run python notebooks/experiments/t999.py",),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "autoresearch-next",
                    str(state_path),
                    "--quantipy-root",
                    str(quantipy_root),
                    "--openclaw-config",
                    str(DEFAULT_OPENCLAW_CONFIG_PATH),
                ],
            )

        assert result.exit_code == 1
        assert "active experiment/test writer" in result.output
        assert "processes" in result.output

    @pytest.mark.parametrize(
        ("cmdline", "touches_root", "expected_count"),
        [
            ("uv run python -m quantipy.api --port 8000", True, 0),
            ("uv run pytest tests/test_alpha.py", True, 1),
            ("jupyter nbconvert --execute notebooks/experiments/t999.ipynb", True, 1),
            ("uv run python scripts/experiments/generate_t999.py", True, 1),
            ("uv run pytest tests/test_alpha.py", False, 0),
        ],
    )
    def test_target_writer_detection_scopes_writers(
        self,
        tmp_path: Path,
        cmdline: str,
        touches_root: bool,
        expected_count: int,
    ) -> None:
        proc_dir = tmp_path / "101"
        proc_dir.mkdir()

        def _fake_read_bytes(path: Path) -> bytes:
            if path == proc_dir / "cmdline":
                return cmdline.replace(" ", "\x00").encode()
            raise FileNotFoundError

        with (
            patch("gateway.cli.Path.glob", return_value=[proc_dir]),
            patch("gateway.cli.Path.read_bytes", autospec=True, side_effect=_fake_read_bytes),
            patch("gateway.cli.os.getpid", return_value=999),
            patch("gateway.cli.os.getppid", return_value=998),
            patch("gateway.cli._process_touches_path", return_value=touches_root),
        ):
            offenders = _active_target_writer_processes(Path("/home/dev/repos/quantipy"))

        assert len(offenders) == expected_count


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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--force", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
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
            patch("gateway.cli._get_tailscale_ip", return_value=None),
            patch("gateway.cli.secrets.token_hex", return_value="deadbeef" * 6),
        ):
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
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
            result = runner.invoke(app, ["init-env", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "G2 app env" in result.output


class TestGetLocalIp:
    """_get_local_ip falls back gracefully."""

    def test_returns_string(self) -> None:
        ip = _get_local_ip()
        assert isinstance(ip, str)
        parts = ip.split(".")
        assert len(parts) == 4


# ---------------------------------------------------------------------------
# push-config command
# ---------------------------------------------------------------------------


class TestPushConfig:
    """Tests for the push-config command."""

    def test_push_script_not_found(self, tmp_path: Path) -> None:
        """Error when the push script does not exist."""
        fake_root = tmp_path / "repo"
        fake_root.mkdir()
        with patch("gateway.cli._PROJECT_ROOT", fake_root):
            result = runner.invoke(app, ["push-config"])
        assert result.exit_code == 1
        assert "Push script not found" in result.output

    def test_push_script_fails(self) -> None:
        """Error when the push script exits non-zero."""
        fake_result = MagicMock(returncode=2)
        with (
            patch("gateway.cli._PROJECT_ROOT", Path("/fake")),
            patch(
                "gateway.cli.Path.is_file",
                side_effect=lambda self=None: True,
            ),
            patch("gateway.cli.subprocess.run", return_value=fake_result),
        ):
            result = runner.invoke(app, ["push-config"])
        assert result.exit_code == 1
        assert "Push script failed" in result.output

    def test_push_only_no_restart(self, tmp_path: Path) -> None:
        """--no-restart pushes config without restarting the daemon."""
        # Create a fake push script
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        push_script = scripts_dir / "push-openclaw-config.sh"
        push_script.write_text("#!/bin/bash\nexit 0\n")
        push_script.chmod(0o755)

        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(cmd)
            return MagicMock(returncode=0)

        with (
            patch("gateway.cli._PROJECT_ROOT", tmp_path),
            patch("gateway.cli.subprocess.run", side_effect=_fake_run),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._is_port_open", return_value=False),
        ):
            result = runner.invoke(app, ["push-config", "--no-restart"])

        assert result.exit_code == 0
        assert "Skipped (--no-restart)" in result.output
        # Should NOT have called openclaw daemon restart
        for call in calls:
            assert "restart" not in call, f"Unexpected restart call: {call}"

    def test_push_and_restart(self, tmp_path: Path) -> None:
        """Push config then restart the daemon."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        push_script = scripts_dir / "push-openclaw-config.sh"
        push_script.write_text("#!/bin/bash\nexit 0\n")
        push_script.chmod(0o755)

        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(cmd)
            return MagicMock(returncode=0)

        with (
            patch("gateway.cli._PROJECT_ROOT", tmp_path),
            patch("gateway.cli.subprocess.run", side_effect=_fake_run),
            patch("gateway.cli._read_openclaw_config", return_value=(None, 18789)),
            patch("gateway.cli._wait_for_port", return_value=True),
            patch("gateway.cli._is_port_open", return_value=True),
        ):
            result = runner.invoke(app, ["push-config"])

        assert result.exit_code == 0
        # Should have called openclaw daemon restart
        restart_calls = [c for c in calls if "restart" in c]
        assert len(restart_calls) == 1
        assert restart_calls[0] == ["openclaw", "daemon", "restart"]


# ---------------------------------------------------------------------------
# stop command
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for the stop command process cleanup."""

    @staticmethod
    def _pgrep_side_effect(
        matches: dict[str, str],
    ) -> Callable[..., MagicMock]:
        """Return a side_effect for subprocess.run that simulates pgrep.

        *matches* maps a pgrep pattern substring to the stdout to return.
        """

        def _side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0] == "pgrep":
                pattern = cmd[-1]
                for key, stdout in matches.items():
                    if key in pattern:
                        return MagicMock(returncode=0, stdout=stdout)
            return MagicMock(returncode=1, stdout="")

        return _side_effect

    def test_stop_kills_openclaw_agent_processes(self) -> None:
        """When pgrep matches openclaw-agent, SIGTERM is sent to returned PIDs."""
        killed_signals: dict[int, list[int]] = {}

        def _fake_kill(pid: int, sig: int) -> None:
            killed_signals.setdefault(pid, []).append(sig)
            if sig == 0:
                raise ProcessLookupError

        side_effect = self._pgrep_side_effect({"openclaw-agent": "1001\n1002\n"})

        with (
            patch("gateway.cli.subprocess.run", side_effect=side_effect),
            patch("gateway.cli.os.kill", side_effect=_fake_kill),
            patch("gateway.cli.os.getpid", return_value=99999),
            patch("gateway.cli.os.getppid", return_value=99998),
            patch("gateway.cli.time.sleep"),
            patch("gateway.cli.time.monotonic", side_effect=[0, 0, 10, 10, 10, 10, 10, 10] * 10),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert signal.SIGTERM in killed_signals.get(1001, [])
        assert signal.SIGTERM in killed_signals.get(1002, [])

    def test_stop_kills_mempalace_mcp_processes(self) -> None:
        """When pgrep matches MemPalace MCP server, SIGTERM is sent to returned PIDs."""
        killed_signals: dict[int, list[int]] = {}

        def _fake_kill(pid: int, sig: int) -> None:
            killed_signals.setdefault(pid, []).append(sig)
            if sig == 0:
                raise ProcessLookupError

        side_effect = self._pgrep_side_effect({"mempalace": "2001\n"})

        with (
            patch("gateway.cli.subprocess.run", side_effect=side_effect),
            patch("gateway.cli.os.kill", side_effect=_fake_kill),
            patch("gateway.cli.os.getpid", return_value=99999),
            patch("gateway.cli.os.getppid", return_value=99998),
            patch("gateway.cli.time.sleep"),
            patch("gateway.cli.time.monotonic", side_effect=[0, 0, 10, 10, 10, 10, 10, 10] * 10),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert signal.SIGTERM in killed_signals.get(2001, [])

    def test_stop_excludes_own_pid(self) -> None:
        """When pgrep returns the current process PID, it is excluded from kill targets."""
        own_pid = 5000
        killed_pids: set[int] = set()

        def _fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGTERM:
                killed_pids.add(pid)
            if sig == 0:
                raise ProcessLookupError

        side_effect = self._pgrep_side_effect({"openclaw-agent": f"{own_pid}\n3001\n"})

        with (
            patch("gateway.cli.subprocess.run", side_effect=side_effect),
            patch("gateway.cli.os.kill", side_effect=_fake_kill),
            patch("gateway.cli.os.getpid", return_value=own_pid),
            patch("gateway.cli.os.getppid", return_value=99998),
            patch("gateway.cli.time.sleep"),
            patch("gateway.cli.time.monotonic", side_effect=[0, 0, 10, 10, 10, 10, 10, 10] * 10),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert own_pid not in killed_pids
        assert 3001 in killed_pids

    def test_signal_process_group_does_not_signal_callers_group(self) -> None:
        killed_pids: list[tuple[int, int]] = []
        killed_groups: list[tuple[int, int]] = []

        with (
            patch("gateway.cli.os.getpgid", return_value=777),
            patch("gateway.cli.os.getpgrp", return_value=777),
            patch(
                "gateway.cli.os.kill",
                side_effect=lambda pid, sig: killed_pids.append((pid, sig)),
            ),
            patch(
                "gateway.cli.os.killpg",
                side_effect=lambda pgid, sig: killed_groups.append((pgid, sig)),
            ),
        ):
            _signal_process_group(3001, signal.SIGTERM)

        assert killed_pids == [(3001, signal.SIGTERM)]
        assert killed_groups == []
