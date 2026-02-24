"""CLI commands for the G2 OpenClaw gateway.

Provides ``gateway-cli init-env`` to auto-generate a ``.env`` file
by detecting system capabilities and reading OpenClaw configuration.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(help="G2 OpenClaw Gateway CLI utilities.")
console = Console()

# ---------------------------------------------------------------------------
# Root of the repository (parent of the ``gateway/`` package)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------


def _detect_gpu() -> tuple[str | None, float]:
    """Detect NVIDIA GPU via ``nvidia-smi``.

    Returns:
        A tuple of ``(gpu_name, vram_gb)``.  ``gpu_name`` is *None* when no
        GPU is found.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None, 0.0
        return _parse_gpu_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, 0.0


def _parse_gpu_output(output: str) -> tuple[str | None, float]:
    """Parse the CSV output of ``nvidia-smi``.

    Expected format: ``NVIDIA GeForce RTX 3060, 12288 MiB``
    """
    line = output.strip().split("\n")[0].strip()
    if not line:
        return None, 0.0
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None, 0.0
    gpu_name = parts[0]
    vram_str = parts[1].lower().replace("mib", "").strip()
    try:
        vram_mb = float(vram_str)
    except ValueError:
        return gpu_name, 0.0
    return gpu_name, vram_mb / 1024.0


# ---------------------------------------------------------------------------
# Whisper model selection
# ---------------------------------------------------------------------------


def _choose_whisper_model(vram_gb: float, *, has_gpu: bool) -> str:
    """Pick a Whisper model based on available VRAM.

    Rules:
        - No GPU: ``tiny.en``
        - < 4 GB: ``base.en``
        - 4-8 GB: ``small.en``
        - >= 8 GB: ``medium.en``
    """
    if not has_gpu:
        return "tiny.en"
    if vram_gb < 4:
        return "base.en"
    if vram_gb < 8:
        return "small.en"
    return "medium.en"


# ---------------------------------------------------------------------------
# OpenClaw config reading
# ---------------------------------------------------------------------------


def _read_openclaw_config(
    config_path: Path | None = None,
) -> tuple[str | None, int]:
    """Read ``~/.openclaw/openclaw.json`` and extract gateway settings.

    Returns:
        ``(token, port)`` — token may be *None* if absent.
    """
    if config_path is None:
        config_path = Path.home() / ".openclaw" / "openclaw.json"
    if not config_path.is_file():
        return None, 18789
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        gw = data.get("gateway", {})
        token = gw.get("auth", {}).get("token")
        port = gw.get("port", 18789)
        return token, int(port)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, 18789


# ---------------------------------------------------------------------------
# Local IP
# ---------------------------------------------------------------------------


def _get_local_ip() -> str:
    """Return the local network IP address (best-effort)."""
    try:
        # Connect to a public address (no actual traffic) to find the local IP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addr: str = s.getsockname()[0]
            return addr
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            return "127.0.0.1"


# ---------------------------------------------------------------------------
# .env rendering
# ---------------------------------------------------------------------------


def _render_env(
    *,
    local_ip: str,
    gateway_token: str,
    whisper_model: str,
    whisper_device: str,
    whisper_compute_type: str,
    gpu_label: str,
    openclaw_port: int,
    openclaw_token: str | None,
) -> str:
    """Render the ``.env`` file contents."""
    oc_token_line = openclaw_token or ""
    oc_comment = (
        "# Read from ~/.openclaw/openclaw.json → gateway.auth.token"
        if openclaw_token
        else "# Not found in ~/.openclaw/openclaw.json — set manually if needed"
    )

    return f"""\
# G2 OpenClaw Gateway — Environment Configuration
# Generated by: python -m gateway init-env
# See gateway/config.py for full documentation of each variable.

# --- Gateway Server ---
# Bind to all interfaces so the G2 app (on iPhone) can reach this server.
# Your local IP: {local_ip} — use this in the G2 app's VITE_GATEWAY_URL.
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8765
# Auth token — the G2 app must send this as ?token= query param when connecting.
GATEWAY_TOKEN={gateway_token}

# --- Whisper (Speech-to-Text) ---
# Detected: {gpu_label} → using {whisper_model} on {whisper_device}
# Options: tiny.en, base.en, small.en, medium.en, large-v3
WHISPER_MODEL={whisper_model}
WHISPER_DEVICE={whisper_device}
WHISPER_COMPUTE_TYPE={whisper_compute_type}

# --- OpenClaw Connection ---
OPENCLAW_HOST=127.0.0.1
OPENCLAW_PORT={openclaw_port}
# This must match the token in ~/.openclaw/openclaw.json → gateway.auth.token
{oc_comment}
OPENCLAW_GATEWAY_TOKEN={oc_token_line}

# --- Timeouts ---
AGENT_TIMEOUT=120
"""


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

_force_option = typer.Option(False, "--force", help="Overwrite an existing .env file.")
_project_root_option = typer.Option(
    _PROJECT_ROOT,
    "--project-root",
    hidden=True,
    help="Override project root (for testing).",
)


@app.command()
def init_env(
    force: bool = _force_option,
    project_root: Path = _project_root_option,
) -> None:
    """Auto-generate the .env file by detecting system capabilities."""
    env_path = project_root / ".env"

    # --- Guard against overwriting -----------------------------------------------
    if env_path.exists() and not force:
        console.print(
            f"[bold yellow]⚠  {env_path} already exists.[/bold yellow]\n"
            "  Run again with [bold]--force[/bold] to overwrite.",
        )
        raise typer.Exit(code=1)

    # --- GPU detection -----------------------------------------------------------
    gpu_name, vram_gb = _detect_gpu()
    has_gpu = gpu_name is not None
    whisper_device = "cuda" if has_gpu else "cpu"
    whisper_compute_type = "float16" if has_gpu else "int8"
    whisper_model = _choose_whisper_model(vram_gb, has_gpu=has_gpu)

    if gpu_name:
        gpu_label = f"{gpu_name} ({vram_gb:.1f} GB)"
    else:
        gpu_label = "No NVIDIA GPU detected (CPU mode)"

    # --- Gateway token -----------------------------------------------------------
    gateway_token = secrets.token_hex(24)

    # --- OpenClaw config ---------------------------------------------------------
    openclaw_token, openclaw_port = _read_openclaw_config()

    # --- Local IP ----------------------------------------------------------------
    local_ip = _get_local_ip()

    # --- Render & write ----------------------------------------------------------
    content = _render_env(
        local_ip=local_ip,
        gateway_token=gateway_token,
        whisper_model=whisper_model,
        whisper_device=whisper_device,
        whisper_compute_type=whisper_compute_type,
        gpu_label=gpu_label,
        openclaw_port=openclaw_port,
        openclaw_token=openclaw_token,
    )

    env_path.write_text(content, encoding="utf-8")

    # --- G2 app .env.local -------------------------------------------------------
    g2_env_path: Path | None = None
    g2_app_dir = project_root / "g2_app"
    if g2_app_dir.is_dir():
        g2_env_path = g2_app_dir / ".env.local"
        if g2_env_path.exists() and not force:
            console.print(
                f"[bold yellow]⚠  {g2_env_path} already exists — skipping.[/bold yellow]\n"
                "  Run again with [bold]--force[/bold] to overwrite.",
            )
            g2_env_path = None  # signal: not written
        else:
            vite_url = f"ws://{local_ip}:8765?token={gateway_token}"
            g2_env_content = (
                "# Auto-generated by: python -m gateway init-env\n"
                "# The G2 app reads this at build time "
                "(Vite injects import.meta.env.VITE_GATEWAY_URL).\n"
                f"VITE_GATEWAY_URL={vite_url}\n"
            )
            g2_env_path.write_text(g2_env_content, encoding="utf-8")

    # --- Summary -----------------------------------------------------------------
    rows = [
        f"[bold]File written:[/bold]   {env_path}",
        f"[bold]Local IP:[/bold]       {local_ip}",
        f"[bold]GPU:[/bold]            {gpu_label}",
        f"[bold]Whisper:[/bold]        {whisper_model} on {whisper_device}"
        f" ({whisper_compute_type})",
        f"[bold]Gateway token:[/bold]  {gateway_token[:8]}…",
        f"[bold]OpenClaw port:[/bold]  {openclaw_port}",
        f"[bold]OpenClaw token:[/bold] {'(set)' if openclaw_token else '(not set)'}",
    ]
    if g2_env_path is not None:
        rows.append(f"[bold]G2 app env:[/bold]    {g2_env_path}")
    console.print(Panel("\n".join(rows), title="init-env summary", border_style="green"))
