"""CLI commands for the G2 OpenClaw gateway.

Provides ``gateway-cli init-env`` to auto-generate a ``.env`` file
by detecting system capabilities and reading OpenClaw configuration.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Any

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(help="G2 OpenClaw Gateway CLI utilities.")
console = Console()

# ---------------------------------------------------------------------------
# Root of the repository (parent of the ``gateway/`` package)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PID_FILE = _PROJECT_ROOT / "logs" / ".sim.pid"
_MEMPALACE_PYTHON = Path.home() / ".local/share/mempalace/venv/bin/python"
_MEMPALACE_HEALTH_SCRIPT = _PROJECT_ROOT / "scripts" / "check-mempalace-health.py"
_MEMPALACE_CACHE_PATH = Path.home() / ".cache/fastembed"
_MEMPALACE_EMBEDDING_MODEL = "bge-base"
_REQUIRED_OPENCLAW_VERSION = (2026, 6, 11)
_REQUIRED_OPENCLAW_VERSION_TEXT = ".".join(str(part) for part in _REQUIRED_OPENCLAW_VERSION)
_mempalace_kg_path_option = typer.Option(
    None,
    "--mempalace-kg-path",
    hidden=True,
    help="Override the MemPalace KG path for deterministic tests.",
)
_TARGET_WRITER_COMMAND_RE = re.compile(
    r"(\bpytest\b|\bpy\.test\b|\bjupyter\b|\bpapermill\b|\bipython\b|"
    r"\bnbconvert\b|\bgenerate_[\w.-]*|notebooks/experiments|"
    r"src/quantipy/alpha|scripts/experiments|tools/experiments)"
)
_OPENCLAW_VERSION_TOKEN_RE = re.compile(r"(?<!\S)(\d+\.\d+\.\d+\S*)(?!\S)")
_OPENCLAW_STABLE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class _OpenClawResolutionError(RuntimeError):
    """Raised when the OpenClaw executable cannot be resolved safely."""


class _OpenClawVersionError(RuntimeError):
    """Raised when the resolved OpenClaw executable does not meet requirements."""


class _ResolvedOpenClaw:
    """Resolved OpenClaw executable path and parsed version."""

    def __init__(self, path: Path, version_text: str, version: tuple[int, int, int]) -> None:
        self.path = path
        self.version_text = version_text
        self.version = version


def _iter_openclaw_candidates() -> tuple[Path, ...]:
    """Return ordered OpenClaw executable candidates without following wrappers."""
    override = os.environ.get("OPENCLAW_BIN")
    if override:
        return (Path(override).expanduser(),)

    candidates: list[Path] = [
        Path.home() / ".local/share/pnpm/openclaw",
        Path.home() / ".local/bin/openclaw",
    ]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(Path(entry).expanduser() / "openclaw")

    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return tuple(ordered)


def _resolve_openclaw_executable() -> Path:
    """Resolve OpenClaw deterministically, preferring the user-level install."""
    for candidate in _iter_openclaw_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    override = os.environ.get("OPENCLAW_BIN")
    if override:
        override_path = Path(override).expanduser()
        raise _OpenClawResolutionError(
            f"OPENCLAW_BIN points to a missing or non-executable path: {override_path}"
        )

    preferred = ", ".join(str(path) for path in _iter_openclaw_candidates()[:2])
    raise _OpenClawResolutionError(
        "OpenClaw executable not found. Checked preferred locations "
        f"({preferred}) and PATH entries."
    )


def _parse_openclaw_version_token(output: str) -> str | None:
    """Extract the complete version token from OpenClaw version output."""
    match = _OPENCLAW_VERSION_TOKEN_RE.search(output)
    if match is None:
        return None
    return match.group(1)


def _require_openclaw_binary() -> _ResolvedOpenClaw:
    """Resolve OpenClaw and require the exact repo-supported version."""
    executable = _resolve_openclaw_executable()
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise _OpenClawResolutionError(
            f"Failed to execute OpenClaw at {executable}: {exc}"
        ) from exc

    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise _OpenClawVersionError(
            f"OpenClaw version check failed for {executable}: {output or 'no output'}"
        )

    version_token = _parse_openclaw_version_token(output)
    if version_token is None:
        raise _OpenClawVersionError(
            f"Could not parse OpenClaw version from {executable}: {output or 'no output'}"
        )
    if version_token != _REQUIRED_OPENCLAW_VERSION_TEXT:
        relation = "unsupported"
        stable_match = _OPENCLAW_STABLE_VERSION_RE.fullmatch(version_token)
        if stable_match is not None:
            version = tuple(int(part) for part in stable_match.groups())
            relation = "too old" if version < _REQUIRED_OPENCLAW_VERSION else "too new"
        raise _OpenClawVersionError(
            f"OpenClaw {version_token} at {executable} is {relation}; "
            f"need exactly {_REQUIRED_OPENCLAW_VERSION_TEXT}."
        )

    return _ResolvedOpenClaw(
        path=executable,
        version_text=version_token,
        version=_REQUIRED_OPENCLAW_VERSION,
    )


def _write_pid_file(pids: dict[str, int]) -> None:
    """Write spawned process PIDs to the PID file."""
    _PID_FILE.parent.mkdir(exist_ok=True)
    _PID_FILE.write_text(json.dumps(pids), encoding="utf-8")


def _read_pid_file() -> dict[str, int]:
    """Read PIDs from the PID file, returning empty dict if missing/invalid."""
    try:
        data: dict[str, int] = json.loads(_PID_FILE.read_text(encoding="utf-8"))
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _remove_pid_file() -> None:
    """Remove the PID file if it exists."""
    with contextlib.suppress(FileNotFoundError):
        _PID_FILE.unlink()


def _mempalace_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return environment variables required for strict MemPalace startup."""
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("FASTEMBED_CACHE_PATH", str(_MEMPALACE_CACHE_PATH))
    env.setdefault("MEMPALACE_EMBEDDING_MODEL", _MEMPALACE_EMBEDDING_MODEL)
    env.setdefault("MEMPALACE_EXPECTED_EMBEDDING_MODEL", _MEMPALACE_EMBEDDING_MODEL)
    env.setdefault("MEMPALACE_EXPECTED_EMBEDDING_DIMENSION", "768")
    env.setdefault("HF_HUB_OFFLINE", "1")
    return env


def _check_mempalace_health() -> bool:
    """Run the strict MemPalace startup healthcheck."""
    if not _MEMPALACE_PYTHON.is_file():
        console.print(f"  [red]✗[/red] MemPalace Python not found: {_MEMPALACE_PYTHON}")
        return False
    if not _MEMPALACE_HEALTH_SCRIPT.is_file():
        console.print(
            f"  [red]✗[/red] MemPalace health script not found: {_MEMPALACE_HEALTH_SCRIPT}"
        )
        return False
    _MEMPALACE_CACHE_PATH.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(_MEMPALACE_PYTHON), str(_MEMPALACE_HEALTH_SCRIPT)],
        cwd=str(_PROJECT_ROOT),
        env=_mempalace_env(),
        text=True,
        capture_output=True,
    )
    if result.stdout.strip():
        console.print(f"  [dim]{result.stdout.strip()}[/dim]")
    if result.returncode == 0:
        console.print("  [green]✓[/green] MemPalace healthcheck passed")
        return True
    if result.stderr.strip():
        console.print(f"  [red]✗[/red] {result.stderr.strip()}")
    else:
        console.print("  [red]✗[/red] MemPalace healthcheck failed")
    return False


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
# CUDA library validation
# ---------------------------------------------------------------------------


_CUDA_TARGETS = ["libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9"]


def _validate_cuda_libraries() -> dict[str, Path | None]:
    """Check whether the CUDA shared libraries needed for GPU transcription are findable.

    Returns a dict mapping library name to the path where it was found, or
    *None* if the library could not be located.
    """
    search_roots: list[Path] = [
        *sorted(Path(sys.prefix).glob("lib/python*/site-packages/nvidia")),
        Path.home() / ".cache" / "uv",
        *sorted(Path("/usr/local").glob("cuda*/lib64")),
        Path("/usr/lib/x86_64-linux-gnu"),
    ]

    results: dict[str, Path | None] = {}
    for lib_name in _CUDA_TARGETS:
        # Already loadable by the dynamic linker?
        try:
            ctypes.CDLL(lib_name)
            results[lib_name] = Path("(system)")
            continue
        except OSError:
            pass

        # Walk search roots
        found_path: Path | None = None
        for root in search_roots:
            if not root.exists() or root.is_file():
                continue
            matches = list(root.rglob(lib_name))
            if matches:
                found_path = matches[0]
                break
        results[lib_name] = found_path

    return results


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


def _get_tailscale_ip() -> str | None:
    """Return the Tailscale IPv4 address if available, else *None*."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


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
    tailscale_ip: str | None = None,
) -> str:
    """Render the ``.env`` file contents."""
    oc_token_line = openclaw_token or ""
    oc_comment = (
        "# Read from ~/.openclaw/openclaw.json → gateway.auth.token"
        if openclaw_token
        else "# Not found in ~/.openclaw/openclaw.json — set manually if needed"
    )

    tailscale_section = ""
    if tailscale_ip:
        tailscale_section = (
            "\n# --- Tailscale (Remote Access) ---\n"
            f"# Tailscale IP: {tailscale_ip} (used as default G2 app URL)\n"
            "# Traffic is encrypted inside the Tailscale tunnel — ws:// is safe.\n"
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
# Auth token — the G2 app reads this from VITE_GATEWAY_URL, strips it from the
# WebSocket URL, and sends it in the first auth frame.
GATEWAY_TOKEN={gateway_token}
{tailscale_section}
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
_openclaw_config_option = typer.Option(
    Path("gateway/openclaw_config/openclaw.json"),
    "--openclaw-config",
    hidden=True,
    help="Override autoresearch config path (for testing).",
)
_quantipy_root_option = typer.Option(
    Path("/home/dev/repos/quantipy"),
    "--quantipy-root",
    hidden=True,
    help="Override Quantipy root for autoresearch receipts (for testing).",
)
_state_path_argument = typer.Argument(..., exists=True, dir_okay=False, readable=True)
_artifact_path_argument = typer.Argument(..., exists=True, dir_okay=False, readable=True)
_output_path_option = typer.Option(
    ...,
    "--output",
    dir_okay=False,
    help="Path to write the updated autoresearch state JSON.",
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

    # --- CUDA library validation -------------------------------------------------
    cuda_label = "N/A (CPU mode)"
    if has_gpu:
        cuda_results = _validate_cuda_libraries()
        found_libs = [n for n, p in cuda_results.items() if p is not None]
        missing_libs = [n for n, p in cuda_results.items() if p is None]
        if missing_libs:
            names = ", ".join(missing_libs)
            console.print(
                f"[bold yellow]\u26a0  Missing CUDA libraries: {names}[/bold yellow]\n"
                "  GPU transcription may fail. Run [bold]uv sync[/bold] to install CUDA packages.",
            )
            cuda_label = f"MISSING: {names}"
        else:
            short = ", ".join(n.split(".so")[0].removeprefix("lib") for n in found_libs)
            console.print(f"[green]\u2713[/green] CUDA libraries: {short} loaded successfully")
            cuda_label = f"{short} \u2713"

    # --- Gateway token -----------------------------------------------------------
    gateway_token = secrets.token_hex(24)

    # --- OpenClaw config ---------------------------------------------------------
    openclaw_token, openclaw_port = _read_openclaw_config()

    # --- Local IP ----------------------------------------------------------------
    local_ip = _get_local_ip()

    # --- Tailscale detection -----------------------------------------------------
    tailscale_ip = _get_tailscale_ip()
    if tailscale_ip:
        console.print(f"[green]✓[/green] Tailscale detected: {tailscale_ip}")
    else:
        console.print(
            "[dim]i  Tailscale not detected \u2014 gateway will be LAN-only.[/dim]\n"
            "[dim]   Install Tailscale for remote access: https://tailscale.com/download[/dim]"
        )

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
        tailscale_ip=tailscale_ip,
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
            if tailscale_ip:
                vite_url = f"ws://{tailscale_ip}:8765?token={gateway_token}"
            else:
                vite_url = f"ws://{local_ip}:8765?token={gateway_token}"
            lines = [
                "# Auto-generated by: python -m gateway init-env",
                "# The G2 app reads this at build time "
                "(Vite injects import.meta.env.VITE_GATEWAY_URL).",
                f"VITE_GATEWAY_URL={vite_url}",
            ]
            if tailscale_ip:
                lines.append("")
                lines.append("# Alternate LAN URL (home network only):")
                lines.append(f"# VITE_GATEWAY_URL=ws://{local_ip}:8765?token={gateway_token}")
            g2_env_content = "\n".join(lines) + "\n"
            g2_env_path.write_text(g2_env_content, encoding="utf-8")

    # --- Summary -----------------------------------------------------------------
    rows = [
        f"[bold]File written:[/bold]   {env_path}",
        f"[bold]Local IP:[/bold]       {local_ip}",
        f"[bold]GPU:[/bold]            {gpu_label}",
        f"[bold]Whisper:[/bold]        {whisper_model} on {whisper_device}"
        f" ({whisper_compute_type})",
        f"[bold]CUDA libs:[/bold]      {cuda_label}",
        f"[bold]Gateway token:[/bold]  {gateway_token[:8]}…",
        f"[bold]OpenClaw port:[/bold]  {openclaw_port}",
        f"[bold]OpenClaw token:[/bold] {'(set)' if openclaw_token else '(not set)'}",
        f"[bold]Tailscale IP:[/bold]   {tailscale_ip or '(not detected)'}",
    ]
    if g2_env_path is not None:
        rows.append(f"[bold]G2 app env:[/bold]    {g2_env_path}")
    console.print(Panel("\n".join(rows), title="init-env summary", border_style="green"))


@app.command("autoresearch-next")
def autoresearch_next(
    state_path: Path = _state_path_argument,
    openclaw_config: Path = _openclaw_config_option,
    quantipy_root: Path = _quantipy_root_option,
) -> None:
    """Validate autoresearch state/config and print the deterministic next action."""
    from gateway.autoresearch_runner import (
        build_receipt_catalog,
        load_autoresearch_policy,
        load_state_file,
        next_action,
        validate_target_worktree_clean,
    )

    try:
        state = load_state_file(state_path)
        policy = load_autoresearch_policy(openclaw_config)
        receipts = build_receipt_catalog(quantipy_root)
        status_lines = _git_status_short(quantipy_root)
        if status_lines is not None:
            validate_target_worktree_clean(status_lines)
        active_writers = _active_target_writer_processes(quantipy_root)
        if active_writers:
            details = "\n".join(f"- {line}" for line in active_writers)
            raise ValueError(
                "target repo has active experiment/test writer processes:\n"
                f"{details}\n"
                "Stop them before launching the next autoresearch stage."
            )
        action = next_action(state, policy, receipts)
    except ValueError as exc:
        console.print(f"[red]autoresearch-next failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print_json(json.dumps(action.to_dict(), indent=2, sort_keys=True))


def _git_status_short(repo_root: Path) -> tuple[str, ...] | None:
    """Return porcelain status for a Git repo, or None when not a worktree."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        return None
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--short"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if status.returncode != 0:
        raise ValueError(f"could not read git status for target repo: {repo_root}")
    return tuple(line for line in status.stdout.splitlines() if line.strip())


def _active_target_writer_processes(repo_root: Path) -> tuple[str, ...]:
    """Return active experiment/test processes tied to the target repo."""
    root = repo_root.expanduser().resolve()
    exclude_pids = {os.getpid(), os.getppid()}
    offenders: list[str] = []
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc_dir.name)
        except ValueError:
            continue
        if pid in exclude_pids:
            continue
        try:
            raw = (proc_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        if not cmdline or _TARGET_WRITER_COMMAND_RE.search(cmdline) is None:
            continue
        if _process_touches_path(proc_dir, root, cmdline):
            offenders.append(f"{pid} {cmdline}")
    return tuple(offenders)


def _process_touches_path(proc_dir: Path, root: Path, cmdline: str) -> bool:
    if str(root) in cmdline:
        return True
    try:
        cwd = (proc_dir / "cwd").resolve()
    except OSError:
        return False
    return cwd == root or root in cwd.parents


@app.command("autoresearch-advance")
def autoresearch_advance(
    state_path: Path = _state_path_argument,
    artifact_path: Path = _artifact_path_argument,
    output_path: Path = _output_path_option,
    openclaw_config: Path = _openclaw_config_option,
) -> None:
    """Advance autoresearch state with a validated artifact and persist the result."""
    from gateway.autoresearch_runner import (
        FixResultArtifact,
        ImplementationResultArtifact,
        advance_state,
        load_artifact_file,
        load_autoresearch_policy,
        load_state_file,
        save_state_file,
        validate_artifact_workspace,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        artifact = load_artifact_file(artifact_path, state, policy)
        if isinstance(artifact, ImplementationResultArtifact | FixResultArtifact):
            validate_artifact_workspace(state, artifact)
        next_state = advance_state(state, artifact, policy)
        save_state_file(output_path, next_state)
    except ValueError as exc:
        console.print(f"[red]autoresearch-advance failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


@app.command("autoresearch-mark-memory")
def autoresearch_mark_memory(
    state_path: Path = _state_path_argument,
    output_path: Path = _output_path_option,
    openclaw_config: Path = _openclaw_config_option,
    mempalace_kg_path: Path | None = _mempalace_kg_path_option,
) -> None:
    """Verify MemPalace facts and persist its receipt for a finished iteration."""
    from gateway.autoresearch_runner import (
        load_autoresearch_policy,
        load_state_file,
        mark_memory_written,
        save_state_file,
        validate_state,
        verify_mempalace_final_decision,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        validate_state(state, policy)
        receipt = verify_mempalace_final_decision(state, mempalace_kg_path)
        next_state = mark_memory_written(state, receipt)
        save_state_file(output_path, next_state)
    except ValueError as exc:
        console.print(f"[red]autoresearch-mark-memory failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


@app.command("autoresearch-start-next")
def autoresearch_start_next(
    state_path: Path = _state_path_argument,
    output_path: Path = _output_path_option,
    openclaw_config: Path = _openclaw_config_option,
) -> None:
    """Start the next persisted autoresearch iteration after memory is written."""
    from gateway.autoresearch_runner import (
        load_autoresearch_policy,
        load_state_file,
        save_state_file,
        start_next_iteration,
        validate_state,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        validate_state(state, policy)
        next_state = start_next_iteration(state)
        save_state_file(output_path, next_state)
    except ValueError as exc:
        console.print(f"[red]autoresearch-start-next failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


# ---------------------------------------------------------------------------
# Launch helpers
# ---------------------------------------------------------------------------


def _read_gateway_port() -> int:
    """Read GATEWAY_PORT from ``.env`` file, default 8765."""
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return 8765
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "GATEWAY_PORT":
                return int(value.strip())
    except (ValueError, OSError):
        pass
    return 8765


def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Return *True* if *port* is accepting connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect((host, port))
        return True
    except OSError:
        return False


def _find_pid_on_port(port: int) -> int | None:
    """Return the PID of the process listening on *port*, or *None*.

    Uses ``ss -tlnp`` (available on modern Linux) to find the listener.
    Falls back to ``lsof`` if ``ss`` is unavailable.
    """
    # Try ss first (faster, no root needed for own processes)
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Look for pid=<N> in the output
            for line in result.stdout.splitlines():
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return int(m.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: lsof
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().splitlines()[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    return None


def _proc_has_env(pid: int, var_name: str) -> bool:
    """Check whether process *pid* has *var_name* in its environment.

    Reads ``/proc/<pid>/environ``.  Returns *False* on any error
    (permission denied, non-Linux, etc.).
    """
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
        # environ entries are NUL-separated: KEY=VALUE\x00KEY=VALUE\x00…
        for entry in data.split(b"\x00"):
            if entry.startswith(var_name.encode() + b"="):
                return True
    except OSError:
        pass
    return False


def _wait_for_port(
    port: int, label: str, host: str = "127.0.0.1", *, report_interval: float = 10.0
) -> None:
    """Poll until *port* is open, printing status every *report_interval* seconds.

    Blocks indefinitely — the user can press Ctrl+C to abort.
    """
    start = time.monotonic()
    next_report = start + report_interval
    while True:
        if _is_port_open(port, host):
            elapsed = time.monotonic() - start
            console.print(
                f"  [green]✓[/green] {label} ready on port {port}  [dim]({elapsed:.0f}s)[/dim]"
            )
            return
        now = time.monotonic()
        if now >= next_report:
            elapsed = now - start
            console.print(
                f"  [dim]…[/dim] waiting for {label} on port {port}"
                f"  [dim]({elapsed:.0f}s, Ctrl+C to abort)[/dim]"
            )
            next_report = now + report_interval
        time.sleep(0.5)


def _terminate_procs(procs: list[subprocess.Popen[str]]) -> None:
    """SIGTERM all spawned process groups, then SIGKILL after 5 s."""
    for p in procs:
        if p.poll() is None:
            _signal_process_group(p.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    for p in procs:
        remaining = max(0, deadline - time.monotonic())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _signal_process_group(p.pid, signal.SIGKILL)


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    """Signal pid's process group, falling back to the process itself."""
    try:
        pgid = os.getpgid(pid)
    except OSError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
        return
    if pgid == os.getpgrp():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
        return
    try:
        os.killpg(pgid, sig)
    except OSError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)


def _drain_pipe(pipe: IO[Any], log_path: Path | None = None) -> None:
    """Read lines from a pipe, optionally writing to a log file."""
    try:
        fh = open(log_path, "a", encoding="utf-8") if log_path else None  # noqa: SIM115
        for line in pipe:
            if fh:
                fh.write(line if isinstance(line, str) else line.decode())
        if fh:
            fh.close()
    except (OSError, ValueError):
        pass


def _vite_health_check(port: int, *, timeout: float = 2.0) -> bool:
    """Return *True* if the Vite dev server on *port* responds to a health check."""
    try:
        req = urllib.request.Request(f"http://localhost:{port}/_dev/health")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _read_vite_port_from_log(log_path: Path, timeout: float, default: int) -> int:
    """Parse the Vite ``Local:`` URL from a log file, polling until *timeout*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8")
        except OSError:
            time.sleep(0.3)
            continue
        match = re.search(r"Local:\s+https?://[^:]+:(\d+)", text)
        if match:
            return int(match.group(1))
        time.sleep(0.3)
    return default


def _capture_vite_port(proc: subprocess.Popen[str], default: int, timeout: float) -> int:
    """Read Vite stdout lines looking for the ``Local:`` URL.

    Returns the parsed port, or *default* if not found within *timeout*.
    """
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Non-blocking readline via select-like polling isn't trivial;
        # Vite prints the Local line quickly so a short blocking read is fine.
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.2)
            continue
        # Vite prints:  ➜  Local:   http://localhost:5173/
        match = re.search(r"Local:\s+https?://[^:]+:(\d+)", line)
        if match:
            return int(match.group(1))
    return default


# ---------------------------------------------------------------------------
# stop command
# ---------------------------------------------------------------------------


@app.command()
def stop() -> None:
    """Stop all G2 OpenClaw processes (agents, MCP servers, gateway, Vite, simulator)."""

    targets = [
        ("OpenClaw daemon", ["openclaw.*daemon", "openclaw.*gateway"]),
        ("OpenClaw agent", ["openclaw-agent", "openclaw.*agent", "codex app-server"]),
        ("MemPalace MCP server", ["python.*mempalace"]),
        ("Gateway", ["python.*-m.*gateway"]),
        ("Vite dev server", ["node.*vite"]),
        ("EvenHub simulator", ["evenhub-simulator"]),
    ]

    own_pid = os.getpid()
    parent_pid = os.getppid()
    exclude_pids = {own_pid, parent_pid}
    killed_any = False

    console.print("[bold]Stopping G2 OpenClaw services…[/bold]\n")

    for name, patterns in targets:
        pids: set[int] = set()
        for pattern in patterns:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    pid = int(line.strip())
                    if pid not in exclude_pids:
                        pids.add(pid)

        if not pids:
            console.print(f"  [dim]●[/dim] {name}: not running")
            continue

        # SIGTERM first
        for pid in list(pids):
            try:
                _signal_process_group(pid, signal.SIGTERM)
            except ProcessLookupError:
                pids.discard(pid)

        # Wait for graceful exit (up to 5 s)
        deadline = time.monotonic() + 5
        remaining = set(pids)
        while remaining and time.monotonic() < deadline:
            time.sleep(0.3)
            for pid in list(remaining):
                try:
                    os.kill(pid, 0)  # check if still alive
                except ProcessLookupError:
                    remaining.discard(pid)

        # SIGKILL survivors
        for pid in remaining:
            with contextlib.suppress(ProcessLookupError):
                _signal_process_group(pid, signal.SIGKILL)

        pid_str = ", ".join(str(p) for p in sorted(pids))
        console.print(f"  [green]✓[/green] {name}: stopped (PID {pid_str})")
        killed_any = True

    if killed_any:
        console.print("\n[green]All services stopped.[/green]")
    else:
        console.print("\n[dim]No G2 OpenClaw services were running.[/dim]")

    # Clean up PID file
    _remove_pid_file()


# ---------------------------------------------------------------------------
# push-config command
# ---------------------------------------------------------------------------

_no_restart_option = typer.Option(
    False,
    "--no-restart",
    help="Only push config — don't restart the OpenClaw daemon.",
)


@app.command()
def push_config(
    no_restart: bool = _no_restart_option,
) -> None:
    """Push repo OpenClaw config and restart the daemon.

    Runs ``scripts/push-openclaw-config.sh`` (merge provider config, resolve
    API key, copy SOUL.md + preload) then restarts the OpenClaw daemon with
    the Azure api-version preload injected via NODE_OPTIONS.
    """

    push_script = _PROJECT_ROOT / "scripts" / "push-openclaw-config.sh"
    if not push_script.is_file():
        console.print(f"[red]✗[/red] Push script not found: {push_script}")
        raise typer.Exit(code=1)

    try:
        openclaw = _require_openclaw_binary()
    except (_OpenClawResolutionError, _OpenClawVersionError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # -- 1. Run the push script ------------------------------------------------
    console.print("[bold]1/2 Pushing OpenClaw config…[/bold]")
    console.print(f"  [dim]OpenClaw: {openclaw.path} (version {openclaw.version_text})[/dim]")
    result = subprocess.run(
        ["bash", str(push_script)],
        cwd=str(_PROJECT_ROOT),
        env={**os.environ, "OPENCLAW_BIN": str(openclaw.path)},
    )
    if result.returncode != 0:
        console.print(f"[red]✗[/red] Push script failed (exit code {result.returncode})")
        raise typer.Exit(code=1)

    # -- 2. Restart the OpenClaw daemon ----------------------------------------
    daemon_restarted = False
    _, openclaw_port = _read_openclaw_config()

    if no_restart:
        console.print("[bold]2/2 Daemon restart[/bold]")
        console.print("  [dim]Skipped (--no-restart)[/dim]")
    else:
        console.print("[bold]2/2 Restarting OpenClaw daemon…[/bold]")

        # Ensure NODE_OPTIONS is in the systemd service file
        service_path = Path.home() / ".config/systemd/user/openclaw-gateway.service"
        preload_path = Path.home() / ".openclaw" / "azure-api-version-preload.cjs"
        if service_path.is_file() and preload_path.is_file():
            service_text = service_path.read_text(encoding="utf-8")
            node_options_line = f'Environment="NODE_OPTIONS=--require {preload_path}"'
            if "NODE_OPTIONS" not in service_text:
                service_text = service_text.replace(
                    "[Service]\n",
                    f"[Service]\n{node_options_line}\n",
                )
                service_path.write_text(service_text, encoding="utf-8")
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    check=False,
                    capture_output=True,
                )
                console.print("  [green]✓[/green] Injected Azure preload into systemd service")
        elif preload_path.is_file() and not service_path.is_file():
            console.print(f"  [yellow]⚠[/yellow] Systemd service not found at {service_path}")
        elif not preload_path.is_file():
            console.print(
                "  [yellow]⚠[/yellow] Azure preload not found at "
                f"{preload_path} — api-version injection disabled"
            )

        # Restart via openclaw CLI
        console.print(
            "  Restarting OpenClaw daemon on port "
            f"{openclaw_port} via {openclaw.path} (version {openclaw.version_text})…"
        )
        subprocess.run(
            [str(openclaw.path), "daemon", "restart"],
            check=False,
            capture_output=True,
            env=_mempalace_env(),
        )
        _wait_for_port(openclaw_port, label="OpenClaw daemon")
        daemon_restarted = True

    # -- Summary ---------------------------------------------------------------
    port_ok = _is_port_open(openclaw_port)
    rows = [
        "[bold]Config pushed:[/bold]      [green]✓[/green]",
        "[bold]Daemon restarted:[/bold]   "
        + (
            "[green]✓[/green]"
            if daemon_restarted
            else "[dim]skipped[/dim]"
            if no_restart
            else "[red]✗[/red]"
        ),
        f"[bold]Port {openclaw_port}:[/bold]         "
        + ("[green]open[/green]" if port_ok else "[red]closed[/red]"),
    ]
    console.print(Panel("\n".join(rows), title="push-config", border_style="green"))


# ---------------------------------------------------------------------------
# launch command
# ---------------------------------------------------------------------------

_audio_device_option = typer.Option(
    None,
    "--audio-device",
    help="Audio input device ID for the simulator (passed as --aid).",
)
_no_simulator_option = typer.Option(
    False,
    "--no-simulator",
    help="Skip launching the EvenHub simulator.",
)
_no_openclaw_daemon_option = typer.Option(
    False,
    "--no-openclaw",
    help="Skip launching the OpenClaw daemon.",
)
_list_audio_devices_option = typer.Option(
    False,
    "--list-audio-devices",
    help="List available audio input devices and exit.",
)
_local_audio_option = typer.Option(
    False,
    "--local-audio",
    help="Capture audio from the local mic instead of receiving it over WebSocket.",
)
_restart_option = typer.Option(
    False,
    "--restart",
    help="Stop all running services before launching.",
)
_daemon_option = typer.Option(
    False,
    "--daemon",
    "-d",
    help="Detach after services start (write PIDs to logs/.sim.pid).",
)


@app.command()
def launch(
    audio_device: str | None = _audio_device_option,
    no_simulator: bool = _no_simulator_option,
    no_openclaw: bool = _no_openclaw_daemon_option,
    list_audio_devices: bool = _list_audio_devices_option,
    local_audio: bool = _local_audio_option,
    restart: bool = _restart_option,
    daemon: bool = _daemon_option,
) -> None:
    """Start the gateway, G2 dev server, and simulator together."""

    # -- Restart: stop everything first ---------------------------------------
    if restart:
        console.print("[bold]Stopping existing services…[/bold]\n")
        stop()
        console.print()  # blank line before launch output

    # -- List audio devices shortcut -------------------------------------------
    if list_audio_devices:
        subprocess.run(
            ["evenhub-simulator", "--list-audio-input-devices"],
            check=False,
        )
        raise typer.Exit()

    if not no_openclaw:
        console.print("[bold]MemPalace health[/bold]")
        if not _check_mempalace_health():
            raise typer.Exit(code=1)
    openclaw: _ResolvedOpenClaw | None = None
    if not no_openclaw:
        try:
            openclaw = _require_openclaw_binary()
        except (_OpenClawResolutionError, _OpenClawVersionError) as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from exc

    # -- OTel init (before any server import) ---------------------------------
    from gateway.otel_setup import init_otel

    otel_shutdown = init_otel()

    spawned: list[subprocess.Popen[Any]] = []
    log_files: list[Any] = []  # Track opened log file handles for cleanup
    _log_dir = _PROJECT_ROOT / "logs"
    _log_dir.mkdir(exist_ok=True)

    # Clear per-session logs (not gateway.log — managed by RotatingFileHandler)
    for _name in ("gateway-stderr.log", "vite.log", "simulator.log"):
        (_log_dir / _name).write_text("", encoding="utf-8")

    def _cleanup(*_: object) -> None:
        console.print("\n[bold yellow]Shutting down…[/bold yellow]")
        _terminate_procs(spawned)
        for fh in log_files:
            with contextlib.suppress(Exception):
                fh.close()
        _remove_pid_file()
        otel_shutdown()
        console.print("[green]All processes stopped.[/green]")

    gateway_port = _read_gateway_port()
    _, openclaw_port = _read_openclaw_config()
    vite_default_port = 5173
    vite_port = vite_default_port
    gateway_url = f"ws://127.0.0.1:{gateway_port}"
    gateway_started_by_us = False
    vite_started_by_us = False
    simulator_started = False

    try:
        # -- 1. OpenClaw daemon (systemd-managed) --------------------------------
        console.print("[bold]1/4 OpenClaw daemon[/bold]")
        _needs_openclaw_restart = False
        _needs_openclaw_start = False
        if no_openclaw:
            console.print("  [dim]Skipped (--no-openclaw)[/dim]")
        else:
            if openclaw is None:
                raise typer.Exit(code=1)
            console.print(f"  [dim]Using {openclaw.path} (version {openclaw.version_text})[/dim]")
            if _is_port_open(openclaw_port):
                # Check whether the running daemon has NODE_OPTIONS set
                preload_path = Path.home() / ".openclaw" / "azure-api-version-preload.cjs"
                _oc_pid = _find_pid_on_port(openclaw_port)
                if (
                    preload_path.is_file()
                    and _oc_pid is not None
                    and not _proc_has_env(_oc_pid, "NODE_OPTIONS")
                ):
                    console.print(
                        f"  [yellow]⚠[/yellow] OpenClaw (PID {_oc_pid}) running "
                        "without Azure api-version preload — restarting…"
                    )
                    _needs_openclaw_restart = True
                else:
                    console.print(f"  [green]✓[/green] Already running on port {openclaw_port}")
            else:
                _needs_openclaw_start = True

        if (_needs_openclaw_start or _needs_openclaw_restart) and not no_openclaw:
            # Step 1: Ensure NODE_OPTIONS is in the systemd service file
            service_path = Path.home() / ".config/systemd/user/openclaw-gateway.service"
            preload_path = Path.home() / ".openclaw" / "azure-api-version-preload.cjs"
            if service_path.is_file() and preload_path.is_file():
                service_text = service_path.read_text(encoding="utf-8")
                # Systemd splits unquoted values at spaces — quote the value
                node_options_line = f'Environment="NODE_OPTIONS=--require {preload_path}"'
                if "NODE_OPTIONS" not in service_text:
                    service_text = service_text.replace(
                        "[Service]\n",
                        f"[Service]\n{node_options_line}\n",
                    )
                    service_path.write_text(service_text, encoding="utf-8")
                    subprocess.run(
                        ["systemctl", "--user", "daemon-reload"],
                        check=False,
                        capture_output=True,
                    )
                    console.print("  [green]✓[/green] Injected Azure preload into systemd service")
            elif preload_path.is_file() and not service_path.is_file():
                console.print(f"  [yellow]⚠[/yellow] Systemd service not found at {service_path}")
            elif not preload_path.is_file():
                console.print(
                    "  [yellow]⚠[/yellow] Azure preload not found at "
                    f"{preload_path} — api-version injection disabled"
                )

            # Step 2: Start or restart via systemd
            if openclaw is None:
                raise typer.Exit(code=1)
            if _needs_openclaw_restart:
                console.print(
                    "  Restarting OpenClaw daemon on port "
                    f"{openclaw_port} via {openclaw.path} (version {openclaw.version_text})…"
                )
                subprocess.run(
                    [str(openclaw.path), "daemon", "restart"],
                    check=False,
                    capture_output=True,
                    env=_mempalace_env(),
                )
            else:
                console.print(
                    "  Starting OpenClaw daemon on port "
                    f"{openclaw_port} via {openclaw.path} (version {openclaw.version_text})…"
                )
                subprocess.run(
                    [str(openclaw.path), "daemon", "start"],
                    check=False,
                    capture_output=True,
                    env=_mempalace_env(),
                )
            _wait_for_port(openclaw_port, label="OpenClaw daemon")

        # -- 2. Gateway ------------------------------------------------------------
        console.print("[bold]2/4 Gateway[/bold]")
        if _is_port_open(gateway_port):
            console.print(f"  [green]✓[/green] Already running on port {gateway_port}")
        else:
            console.print(f"  Starting gateway on port {gateway_port}…")
            _gw_log = open(_log_dir / "gateway-stderr.log", "a", encoding="utf-8")  # noqa: SIM115
            log_files.append(_gw_log)
            gw_env = {**os.environ}
            if local_audio:
                gw_env["G2_LOCAL_AUDIO"] = "true"
            # NOTE: Do NOT modify LD_LIBRARY_PATH here — the gateway server
            # loads CUDA libs via ctypes.CDLL in _setup_cuda_library_paths().
            # Setting LD_LIBRARY_PATH with uv-cached .so paths causes SIGBUS
            # on startup due to library version conflicts.
            gw_proc = subprocess.Popen(
                [sys.executable, "-m", "gateway"],
                cwd=str(_PROJECT_ROOT),
                stdout=_gw_log,
                stderr=_gw_log,
                env=gw_env,
                start_new_session=True,
            )
            spawned.append(gw_proc)
            gateway_started_by_us = True
            _wait_for_port(gateway_port, label="Gateway")

        # -- 3. Vite dev server ----------------------------------------------------
        console.print("[bold]3/4 Vite dev server[/bold]")
        g2_app_dir = _PROJECT_ROOT / "g2_app"
        _vite_already_running = False
        if _is_port_open(vite_default_port):
            if _vite_health_check(vite_default_port):
                console.print(f"  [green]✓[/green] Already running on port {vite_default_port}")
                _vite_already_running = True
            else:
                console.print("  [yellow]⚠[/yellow] Stale process on port 5173 — killing…")
                stale_pid = _find_pid_on_port(vite_default_port)
                if stale_pid:
                    os.kill(stale_pid, signal.SIGKILL)
                    time.sleep(0.5)
        if not _vite_already_running:
            console.print("  Starting Vite dev server…")
            _vite_log = open(_log_dir / "vite.log", "a", encoding="utf-8")  # noqa: SIM115
            log_files.append(_vite_log)
            vite_proc: subprocess.Popen[str]
            if daemon:
                # Daemon mode: send stdout straight to the log file so the
                # process survives after the launcher exits (no broken pipe).
                # Redirect stdin from /dev/null to prevent EIO on TTY read.
                vite_proc = subprocess.Popen(
                    ["npm", "run", "dev:network"],
                    cwd=str(g2_app_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=_vite_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ},
                    start_new_session=True,
                )
                spawned.append(vite_proc)
                vite_started_by_us = True
                vite_port = _read_vite_port_from_log(
                    _log_dir / "vite.log", timeout=15, default=vite_default_port
                )
                if not _is_port_open(vite_port):
                    _wait_for_port(vite_port, label="Vite dev server")
                else:
                    console.print(f"  [green]✓[/green] Vite dev server ready on port {vite_port}")
            else:
                # Foreground mode: capture stdout to parse the port, then
                # drain the rest into the log via a daemon thread.
                vite_proc = subprocess.Popen(
                    ["npm", "run", "dev:network"],
                    cwd=str(g2_app_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ},
                    start_new_session=True,
                )
                spawned.append(vite_proc)
                vite_started_by_us = True
                vite_port = _capture_vite_port(vite_proc, default=vite_default_port, timeout=15)
                if vite_proc.stdout:
                    threading.Thread(
                        target=_drain_pipe,
                        args=(vite_proc.stdout, _log_dir / "vite.log"),
                        daemon=True,
                    ).start()
                if not _is_port_open(vite_port):
                    _wait_for_port(vite_port, label="Vite dev server")
                else:
                    console.print(f"  [green]✓[/green] Vite dev server ready on port {vite_port}")

        # -- 4. Simulator ----------------------------------------------------------
        console.print("[bold]4/4 EvenHub simulator[/bold]")
        if no_simulator:
            console.print("  [dim]Skipped (--no-simulator)[/dim]")
        else:
            sim_cmd: list[str] = ["evenhub-simulator"]
            if audio_device:
                sim_cmd += ["--aid", audio_device]
            sim_cmd.append(f"http://localhost:{vite_port}")
            try:
                _sim_log = open(_log_dir / "simulator.log", "a", encoding="utf-8")  # noqa: SIM115
                log_files.append(_sim_log)
                sim_env = {
                    **os.environ,
                    "RUST_LOG": "debug",
                }
                sim_proc = subprocess.Popen(
                    sim_cmd,
                    cwd=str(_PROJECT_ROOT),
                    stdout=_sim_log,
                    stderr=_sim_log,
                    env=sim_env,
                    start_new_session=True,
                )
                spawned.append(sim_proc)
                simulator_started = True
                console.print("  [green]✓[/green] Simulator launched")
            except FileNotFoundError:
                console.print("  [red]✗[/red] evenhub-simulator not found on PATH — skipping")

        # -- Summary ---------------------------------------------------------------
        rows = [
            f"[bold]OpenClaw:[/bold]    ws://127.0.0.1:{openclaw_port}  (systemd)",
            f"[bold]Gateway:[/bold]     {gateway_url}"
            f"  ({'spawned' if gateway_started_by_us else 'pre-existing'})",
            f"[bold]Dev server:[/bold]  http://localhost:{vite_port}"
            f"  ({'spawned' if vite_started_by_us else 'pre-existing'})",
            f"[bold]Simulator:[/bold]   {'running' if simulator_started else 'off'}"
            + (f"  (device: {audio_device})" if audio_device else ""),
        ]
        if local_audio:
            rows.append("[bold]Local audio:[/bold]  [green]enabled[/green]")

        console.print(Panel("\n".join(rows), title="G2 OpenClaw", border_style="green"))
        console.print(f"[dim]Logs → {_log_dir.relative_to(_PROJECT_ROOT)}/[/dim]")
        console.print("Press [bold]Ctrl+C[/bold] to stop all services.")

        # -- Write PID file --------------------------------------------------------
        pids: dict[str, int] = {}
        for p in spawned:
            args_list = p.args if isinstance(p.args, list | tuple) else []
            cmd_str = " ".join(str(a) for a in args_list)
            if "gateway" in cmd_str:
                pids["gateway"] = p.pid
            elif "vite" in cmd_str or "npm" in cmd_str:
                pids["vite"] = p.pid
            elif "simulator" in cmd_str:
                pids["simulator"] = p.pid
        if pids:
            _write_pid_file(pids)

        if daemon:
            console.print("[dim]Daemon mode — detaching.[/dim]")
            return

        # -- Wait for interrupt ----------------------------------------------------
        signal.signal(signal.SIGTERM, _cleanup)
        while True:
            # Check that our children are still alive
            for p in spawned:
                if p.poll() is not None:
                    spawned.remove(p)
            if not spawned:
                console.print("[dim]All spawned processes have exited.[/dim]")
                break
            time.sleep(1)

    except KeyboardInterrupt:
        _cleanup()
