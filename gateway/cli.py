"""CLI commands for the G2 OpenClaw gateway.

Provides ``gateway-cli init-env`` to auto-generate a ``.env`` file
by detecting system capabilities and reading OpenClaw configuration.
Campaign stalls are resumed only through the explicit
``autoresearch-acknowledge-campaign-review`` command.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import typer
from rich.console import Console
from rich.panel import Panel

from gateway.autoresearch import constants as autoresearch_constants
from gateway.autoresearch_readiness import (
    DEFAULT_PLATFORM_READINESS_PATH,
    load_platform_readiness,
)
from gateway.autoresearch_shared import AUTORESEARCH_OWNER_SESSION_KEY
from gateway.autoresearch_systemd import (
    SystemdUnitStateError,
    systemd_unit_is_active,
)

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
_REQUIRED_OPENCLAW_VERSION = (2026, 7, 1)
_REQUIRED_OPENCLAW_VERSION_TEXT = "2026.7.1-2"
DEFAULT_AUTORESEARCH_DIR = Path("/home/dev/.openclaw/autoresearch")
DEFAULT_AUTORESEARCH_STATE_PATH = DEFAULT_AUTORESEARCH_DIR / "quantipy-state.json"
DEFAULT_AUTORESEARCH_CHECKPOINT_PATH = DEFAULT_AUTORESEARCH_DIR / "owner-recovery.json"
DEFAULT_AUTORESEARCH_ARTIFACTS_PATH = DEFAULT_AUTORESEARCH_DIR / "artifacts"
DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH = DEFAULT_AUTORESEARCH_DIR / "stage-inbox"
DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH = Path(
    "/home/dev/.openclaw/agents/autoresearch-pm/sessions/sessions.json"
)
DEFAULT_OPENCLAW_GATEWAY_SERVICE = "openclaw-gateway.service"
DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE = "quantipy-autoresearch-supervisor.service"
DEFAULT_OPENCLAW_STATE_DB_PATH = Path("/home/dev/.openclaw/state/openclaw.sqlite")
DEFAULT_OPENCLAW_CONFIG_PATH = Path("/home/dev/.openclaw/openclaw.json")
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


class _SimulatorLaunchError(RuntimeError):
    """Raised when the simulator cannot be launched safely in this environment."""


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


def _openclaw_daemon_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a daemon environment that keeps Azure preload out of Codex runs."""
    env = _mempalace_env(base_env)
    if env.get("OPENCLAW_PROVIDER", "codex") != "azure":
        node_options = env.get("NODE_OPTIONS")
        if node_options and "azure-api-version-preload.cjs" in node_options:
            env.pop("NODE_OPTIONS")
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
    autoresearch_constants.DEFAULT_OPENCLAW_CONFIG_PATH,
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
_readiness_manifest_option = typer.Option(
    DEFAULT_PLATFORM_READINESS_PATH,
    "--readiness-manifest",
    dir_okay=False,
    readable=True,
    help="Operator-owned platform readiness manifest.",
)
_output_path_option = typer.Option(
    ...,
    "--output",
    dir_okay=False,
    help="Path to write the updated autoresearch state JSON.",
)
_instruction_manifest_sha256_option = typer.Option(
    None,
    "--instruction-manifest-sha256",
    help="Dispatch source_manifest_sha256 from autoresearch-next.",
)
_state_reference_sha256_option = typer.Option(
    None,
    "--state-reference-sha256",
    help="Dispatch state_reference_sha256 from autoresearch-next.",
)
_readiness_build_manifest_argument = typer.Argument(
    ..., help="Schema-v3 platform-readiness manifest output."
)
_readiness_expected_commit_option = typer.Option(
    ..., "--expected-quantipy-commit", help="Full Quantipy commit to attest."
)
_readiness_xnys_calendar_option = typer.Option(
    ..., "--xnys-calendar", help="Existing operator-owned XNYS calendar evidence."
)
_readiness_campaign_xnys_start_option = typer.Option(
    ...,
    "--campaign-xnys-start",
    help="Required first XNYS session for the campaign.",
)
_readiness_campaign_xnys_end_option = typer.Option(
    ...,
    "--campaign-xnys-end",
    help="Required last XNYS session for the campaign.",
)
_readiness_evidence_output_option = typer.Option(
    None,
    "--quantipy-evidence",
    help="Quantipy data-contract evidence output; defaults beside the manifest.",
)
_command_output_path_option = typer.Option(
    ...,
    "--output",
    help="Absolute output path for the one-time private command file.",
)


class _OperatorCommandError(RuntimeError):
    """Raised when an operator-only diagnostic or repair cannot be proven safe."""


class _PartialArchiveError(_OperatorCommandError):
    """Raised when residue restoration leaves one or more archive paths stranded."""

    def __init__(self, failures: list[str]) -> None:
        details = "\n".join(f"  stranded path: {failure}" for failure in failures)
        super().__init__(f"PARTIAL ARCHIVE: residue restoration failed\n{details}")


@dataclass(frozen=True, slots=True)
class _CampaignArchive:
    """Completed campaign archive plus the information needed to undo it."""

    path: Path
    notes: tuple[str, ...]
    moved: tuple[tuple[Path, Path], ...]
    owner_sessions_path: Path
    owner_sessions_store_without_key: dict[str, object] | None
    owner_session_entry: dict[str, object] | None


def _run_systemd_probe(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Run one read-only systemd probe for the sanctioned systemd leaf."""
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SystemdUnitStateError(f"failed to execute systemd probe: {exc}") from exc


def _is_systemd_unit_active(unit: str) -> bool:
    """Return a unit's state only when the strict systemd helper proves it."""
    return systemd_unit_is_active(unit, run_command=_run_systemd_probe)


def _new_utc_path(parent: Path, stem: str, suffix: str = "") -> Path:
    """Allocate a unique private path using a UTC timestamp."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1000):
        numbered = "" if attempt == 0 else f"-{attempt}"
        candidate = parent / f"{stem}-{timestamp}{numbered}{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise _OperatorCommandError(f"could not allocate a unique path below {parent}")


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a regular file on the same filesystem."""
    if path.is_symlink():
        raise _OperatorCommandError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise _OperatorCommandError(f"failed to atomically write {path}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _archive_json(path: Path, payload: object) -> None:
    """Write one archive metadata file without exposing a half-written JSON file."""
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _load_owner_session_archive_plan(
    sessions_path: Path,
    archive_path: Path,
) -> tuple[list[tuple[Path, Path]], dict[str, object] | None, list[str], dict[str, object] | None]:
    """Resolve the owner transcript from the PM sessions mapping before moving anything."""
    notes: list[str] = []
    if not sessions_path.exists() and not sessions_path.is_symlink():
        notes.append(f"missing owner session mapping: {sessions_path}")
        return [], None, notes, None
    if sessions_path.is_symlink():
        raise _OperatorCommandError(f"refusing symlinked owner session mapping: {sessions_path}")
    try:
        raw: object = json.loads(sessions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _OperatorCommandError(
            f"cannot safely read owner session mapping {sessions_path}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise _OperatorCommandError(f"owner session mapping is not a JSON object: {sessions_path}")
    store: dict[str, object] = dict(raw)
    entry_raw = store.get(AUTORESEARCH_OWNER_SESSION_KEY)
    if entry_raw is None:
        notes.append(f"missing owner session mapping entry: {AUTORESEARCH_OWNER_SESSION_KEY}")
        return [], None, notes, store
    if not isinstance(entry_raw, Mapping):
        raise _OperatorCommandError(
            f"owner session mapping entry is malformed: {AUTORESEARCH_OWNER_SESSION_KEY}"
        )
    entry: dict[str, object] = dict(entry_raw)
    session_id = entry.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        raise _OperatorCommandError(
            f"owner session mapping entry has no usable sessionId: {AUTORESEARCH_OWNER_SESSION_KEY}"
        )

    sessions_dir = sessions_path.parent.resolve(strict=False)
    candidates = [sessions_dir / f"{session_id}.jsonl"]
    session_file_raw = entry.get("sessionFile")
    if isinstance(session_file_raw, str) and session_file_raw.strip():
        session_file = Path(session_file_raw).expanduser()
        if not session_file.is_absolute():
            session_file = sessions_dir / session_file
        candidates.append(session_file)
    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved != sessions_dir and sessions_dir not in resolved.parents:
            raise _OperatorCommandError(
                f"owner session file escapes the PM sessions directory: {candidate}"
            )
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(resolved)

    moves: list[tuple[Path, Path]] = []
    for candidate in unique_candidates:
        if not candidate.exists() and not candidate.is_symlink():
            notes.append(f"missing owner session file: {candidate}")
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise _OperatorCommandError(f"owner session file is not a regular file: {candidate}")
        moves.append((candidate, archive_path / "sessions" / candidate.name))

    updated_store = dict(store)
    del updated_store[AUTORESEARCH_OWNER_SESSION_KEY]
    return moves, entry, notes, updated_store


def _restore_campaign_archive(archive: _CampaignArchive) -> None:
    """Restore every moved item and remove the archive, reporting stranded paths."""
    failures: list[str] = []
    for source, destination in reversed(archive.moved):
        if not destination.exists() and not destination.is_symlink():
            if source.exists() or source.is_symlink():
                continue
            failures.append(f"{source} (archive location {destination} is also missing)")
            continue
        try:
            destination.rename(source)
        except OSError as exc:
            failures.append(f"{destination} -> {source}: {exc}")

    if archive.owner_session_entry is not None:
        if archive.owner_sessions_store_without_key is None:
            failures.append(
                f"{archive.path / 'sessions.json'} -> {archive.owner_sessions_path} "
                "(original owner mapping was unavailable)"
            )
        else:
            restored_store = dict(archive.owner_sessions_store_without_key)
            restored_store[AUTORESEARCH_OWNER_SESSION_KEY] = archive.owner_session_entry
            try:
                _atomic_write_text(
                    archive.owner_sessions_path,
                    json.dumps(restored_store, indent=2, sort_keys=True) + "\n",
                )
            except _OperatorCommandError as exc:
                failures.append(
                    f"{archive.path / 'sessions.json'} -> {archive.owner_sessions_path} "
                    f"(owner mapping restoration failed: {exc})"
                )

    mapping_archive = archive.path / "sessions.json"
    if mapping_archive.exists() or mapping_archive.is_symlink():
        try:
            mapping_archive.unlink()
        except OSError as exc:
            failures.append(f"{mapping_archive} (archive mapping cleanup failed: {exc})")

    sessions_archive = archive.path / "sessions"
    if sessions_archive.exists() or sessions_archive.is_symlink():
        try:
            sessions_archive.rmdir()
        except OSError as exc:
            failures.append(f"{sessions_archive} (archive directory cleanup failed: {exc})")
    if archive.path.exists() or archive.path.is_symlink():
        try:
            archive.path.rmdir()
        except OSError as exc:
            failures.append(f"{archive.path} (archive cleanup failed: {exc})")
    if failures:
        raise _PartialArchiveError(failures)


def _archive_fresh_campaign() -> _CampaignArchive:
    """Move campaign residue into a unique UTC archive, rolling back on failure."""
    archive_parent = DEFAULT_AUTORESEARCH_DIR / "campaign-archives"
    if archive_parent.is_symlink():
        raise _OperatorCommandError(
            f"refusing symlinked campaign archive directory: {archive_parent}"
        )
    archive_parent.mkdir(parents=True, exist_ok=True)
    archive_path = _new_utc_path(archive_parent, "campaign")

    source_specs = (
        (DEFAULT_AUTORESEARCH_ARTIFACTS_PATH, archive_path / "artifacts", "artifacts directory"),
        (
            DEFAULT_AUTORESEARCH_STAGE_INBOX_PATH,
            archive_path / "stage-inbox",
            "stage-inbox directory",
        ),
        (
            DEFAULT_AUTORESEARCH_CHECKPOINT_PATH,
            archive_path / "owner-recovery.json",
            "owner-recovery checkpoint",
        ),
        (
            DEFAULT_AUTORESEARCH_STATE_PATH,
            archive_path / "quantipy-state.json",
            "campaign state file",
        ),
    )
    moves: list[tuple[Path, Path]] = []
    notes: list[str] = []
    for source, destination, label in source_specs:
        if not source.exists() and not source.is_symlink():
            notes.append(f"missing {label}: {source}")
            continue
        if source.is_symlink():
            raise _OperatorCommandError(f"refusing symlinked {label}: {source}")
        expected_directory = label.endswith("directory")
        if source.is_dir() != expected_directory:
            raise _OperatorCommandError(f"unexpected {label} type: {source}")
        moves.append((source, destination))

    session_moves, removed_mapping, session_notes, updated_store = _load_owner_session_archive_plan(
        DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH,
        archive_path,
    )
    moves.extend(session_moves)
    notes.extend(session_notes)
    for source, destination in moves:
        try:
            if source.stat().st_dev != archive_parent.stat().st_dev:
                raise _OperatorCommandError(
                    f"archive source is on a different filesystem: {source}"
                )
        except OSError as exc:
            raise _OperatorCommandError(f"cannot inspect archive source {source}: {exc}") from exc
        if destination.exists() or destination.is_symlink():
            raise _OperatorCommandError(f"archive destination already exists: {destination}")

    mapping_archive = archive_path / "sessions.json"
    moved: list[tuple[Path, Path]] = []
    archive_created = False
    try:
        archive_path.mkdir(mode=0o700)
        archive_created = True
        if removed_mapping is not None:
            _archive_json(mapping_archive, {AUTORESEARCH_OWNER_SESSION_KEY: removed_mapping})
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            moved.append((source, destination))
        if updated_store is not None and removed_mapping is not None:
            _atomic_write_text(
                DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH,
                json.dumps(updated_store, indent=2, sort_keys=True) + "\n",
            )
    except (OSError, _OperatorCommandError) as exc:
        if archive_created:
            archive = _CampaignArchive(
                path=archive_path,
                notes=tuple(notes),
                moved=tuple(moved),
                owner_sessions_path=DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH,
                owner_sessions_store_without_key=updated_store,
                owner_session_entry=removed_mapping,
            )
            try:
                _restore_campaign_archive(archive)
            except _PartialArchiveError:
                raise
        if isinstance(exc, _OperatorCommandError):
            raise
        raise _OperatorCommandError(f"campaign residue archive failed: {exc}") from exc
    return _CampaignArchive(
        path=archive_path,
        notes=tuple(notes),
        moved=tuple(moved),
        owner_sessions_path=DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH,
        owner_sessions_store_without_key=updated_store,
        owner_session_entry=removed_mapping,
    )


class _DoctorSupervisorController:
    """Inject a previously proven supervisor state into ControlStatus."""

    def __init__(self, active: bool | None) -> None:
        self._active = active

    def ensure_started(self) -> None:
        raise RuntimeError("doctor supervisor controller is read-only")

    def stop(self) -> None:
        raise RuntimeError("doctor supervisor controller is read-only")

    def is_active(self) -> bool:
        if self._active is None:
            raise _OperatorCommandError("supervisor service probe-error")
        return self._active


@app.command("autoresearch-doctor")
def autoresearch_doctor() -> None:
    """Report autoresearch service, state, control, and recovery health."""
    from gateway.autoresearch.persistence import load_state_file
    from gateway.autoresearch_checkpoint import SupervisorCheckpoint
    from gateway.autoresearch_control import AutoresearchControl, ControlConfig

    service_states: dict[str, bool | None] = {}
    service_errors: dict[str, str] = {}
    for unit in (DEFAULT_OPENCLAW_GATEWAY_SERVICE, DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE):
        try:
            service_states[unit] = _is_systemd_unit_active(unit)
        except Exception as exc:
            service_states[unit] = None
            service_errors[unit] = str(exc)

    state = None
    state_error: str | None = None
    try:
        state = load_state_file(DEFAULT_AUTORESEARCH_STATE_PATH)
    except Exception as exc:
        state_error = str(exc)

    checkpoint = None
    checkpoint_error: str | None = None
    try:
        checkpoint = SupervisorCheckpoint.load(DEFAULT_AUTORESEARCH_CHECKPOINT_PATH)
    except Exception as exc:
        checkpoint_error = str(exc)

    status = None
    status_error: str | None = None
    try:
        supervisor_active = service_states.get(DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE)
        control = AutoresearchControl(
            ControlConfig(
                state_path=DEFAULT_AUTORESEARCH_STATE_PATH,
                owner_sessions_path=DEFAULT_AUTORESEARCH_OWNER_SESSIONS_PATH,
                checkpoint_path=DEFAULT_AUTORESEARCH_CHECKPOINT_PATH,
                supervisor_service_name=DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE,
            ),
            service_controller=_DoctorSupervisorController(supervisor_active),
        )
        status = control.status()
    except Exception as exc:
        status_error = str(exc)

    now = time.time()
    degraded: list[str] = []
    console.print("autoresearch doctor")
    console.print("===================")
    console.print("systemd")
    for unit in (DEFAULT_OPENCLAW_GATEWAY_SERVICE, DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE):
        activity = service_states[unit]
        if activity is True:
            console.print(f"  {unit:<42} ACTIVE")
        elif activity is False:
            console.print(f"  {unit:<42} INACTIVE")
            degraded.append(f"{unit} inactive")
        else:
            console.print(
                f"  {unit:<42} ERROR probe-error: {service_errors.get(unit, 'unknown')}",
                markup=False,
                soft_wrap=True,
            )
            degraded.append(f"{unit} probe-error")

    console.print("state")
    if state is None:
        console.print(
            f"  ERROR: {state_error or 'state unavailable'}",
            markup=False,
            soft_wrap=True,
        )
        degraded.append("state unavailable")
    else:
        counters = state.campaign_counters
        console.print(
            f"  schema={autoresearch_constants.AUTORESEARCH_STATE_SCHEMA_VERSION}"
            f" phase={state.phase.value} iteration={state.iteration}"
        )
        console.print(f"  suspended={state.suspended} reason={state.suspension_reason or '-'}")
        console.print(
            "  campaign_review="
            f"{state.campaign_review_required} reason={state.campaign_review_reason or '-'}"
        )
        console.print(
            "  counters="
            f"non_keep:{counters.consecutive_non_keep},"
            f"no_consensus:{counters.consecutive_no_consensus},"
            f"since_keep:{counters.iterations_since_last_keep}"
        )
        console.print(f"  registry_size={len(state.hypothesis_registry)}")
        if state.suspended:
            degraded.append("state suspended")
        if state.campaign_review_required:
            degraded.append("campaign review required")

    console.print("control")
    if status is None:
        console.print(
            f"  ERROR: {status_error or 'control status unavailable'}",
            markup=False,
            soft_wrap=True,
        )
        degraded.append("control status unavailable")
    else:
        console.print(f"  owner={status.owner_agent_id} session={status.owner_session_key}")
        cycle_at = status.supervisor_last_cycle_at
        console.print(
            f"  phase={status.phase} iteration={status.iteration}"
            f" owner_lifecycle={status.owner_lifecycle_status or '-'}"
            f" task_count={len(status.tasks)}"
        )
        console.print(
            "  supervisor_last_outcome="
            f"{status.supervisor_last_outcome or '-'}"
            f" detail={status.supervisor_last_detail or '-'}"
            f" cycle_at={cycle_at if cycle_at is not None else '-'}"
        )

    console.print("checkpoint")
    if checkpoint is None:
        console.print(
            f"  ERROR: {checkpoint_error or 'checkpoint unavailable'}",
            markup=False,
            soft_wrap=True,
        )
        degraded.append("checkpoint unavailable")
    else:
        alerted_keys = sorted(
            key for key, record in checkpoint.recovery_records.items() if record.alerted
        )
        nudge_times = [
            record.last_nudge_at
            for record in checkpoint.recovery_records.values()
            if record.last_nudge_at is not None
        ]
        last_nudge_at = max(nudge_times) if nudge_times else None
        if last_nudge_at is None:
            nudge_recency = "missing"
        else:
            nudge_recency = f"{max(0.0, now - last_nudge_at):.0f}s ago"
        console.print(f"  recovery_records={len(checkpoint.recovery_records)}")
        console.print(f"  alerted_keys={','.join(alerted_keys) if alerted_keys else 'none'}")
        console.print(f"  last_nudge_at={nudge_recency}")
        if alerted_keys:
            degraded.append("alerted recovery key")

    services_active = all(
        service_states.get(unit) is True
        for unit in (DEFAULT_OPENCLAW_GATEWAY_SERVICE, DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE)
    )
    if services_active and status is not None:
        if status.supervisor_last_cycle_at is None:
            degraded.append("last supervisor cycle unavailable while services are active")
        elif now - status.supervisor_last_cycle_at > 600:
            degraded.append("last supervisor cycle is older than 10 minutes")

    if degraded:
        console.print("health=DEGRADED", markup=False)
        console.print(
            f"issues={'; '.join(dict.fromkeys(degraded))}",
            markup=False,
            soft_wrap=True,
        )
        raise typer.Exit(code=1)
    console.print("health=HEALTHY")


@app.command("autoresearch-init-state")
def autoresearch_init_state(
    output_path: Path = _output_path_option,
    readiness_manifest: Path = _readiness_manifest_option,
    fresh_campaign: bool = typer.Option(
        False,
        "--fresh-campaign",
        help="Archive campaign runtime residue before writing pristine schema-v5 state.",
    ),
) -> None:
    """Initialize a pristine schema-v5 campaign pinned to platform readiness."""
    from gateway.autoresearch.persistence import (
        initialize_state,
        provision_quantipy_experiment_runs_root,
        save_state_file,
    )

    if not fresh_campaign:
        try:
            readiness = load_platform_readiness(readiness_manifest)
            provision_quantipy_experiment_runs_root()
            state = initialize_state(readiness)
            save_state_file(output_path, state)
        except ValueError as exc:
            console.print(f"autoresearch-init-state failed: {exc}", markup=False)
            raise typer.Exit(code=1) from exc
        console.print(f"wrote pristine autoresearch state v5: {output_path}", markup=False)
        return

    try:
        try:
            if _is_systemd_unit_active(DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE):
                raise _OperatorCommandError(
                    "refusing fresh campaign while "
                    f"{DEFAULT_AUTORESEARCH_SUPERVISOR_SERVICE} is active; stop it first"
                )
        except SystemdUnitStateError as exc:
            raise _OperatorCommandError(f"cannot prove supervisor is inactive: {exc}") from exc
        readiness = load_platform_readiness(readiness_manifest)
        state = initialize_state(readiness)
        provision_quantipy_experiment_runs_root()
        archive = _archive_fresh_campaign()
        try:
            save_state_file(output_path, state)
        except Exception as exc:
            try:
                _restore_campaign_archive(archive)
            except _PartialArchiveError:
                raise
            raise _OperatorCommandError(
                f"fresh campaign state save failed after archive: {exc}"
            ) from exc
    except Exception as exc:
        console.print(f"autoresearch-init-state failed: {exc}", markup=False)
        raise typer.Exit(code=1) from exc

    console.print(f"archived campaign residue: {archive.path}", markup=False)
    for note in archive.notes:
        console.print(f"  note: {note}", markup=False)
    console.print(f"wrote pristine autoresearch state v5: {output_path}", markup=False)


def _backup_openclaw_state_db(database_path: Path) -> Path:
    """Create a consistent WAL-aware SQLite backup and publish it atomically."""
    backup_path = _new_utc_path(
        database_path.parent,
        "openclaw.sqlite.rebaseline",
        ".bak",
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{backup_path.name}.",
        dir=database_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with (
            contextlib.closing(sqlite3.connect(database_path)) as source,
            contextlib.closing(sqlite3.connect(temporary_path)) as backup,
        ):
            source.backup(backup)
            backup.commit()
        os.replace(temporary_path, backup_path)
    except (OSError, sqlite3.Error) as exc:
        raise _OperatorCommandError(f"failed to back up {database_path}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
    return backup_path


@app.command("deployment-rebaseline-config-health")
def deployment_rebaseline_config_health() -> None:
    """Clear one stale config-health fingerprint after a safe database backup."""
    database_path = DEFAULT_OPENCLAW_STATE_DB_PATH
    config_path = str(DEFAULT_OPENCLAW_CONFIG_PATH)
    try:
        if _is_systemd_unit_active(DEFAULT_OPENCLAW_GATEWAY_SERVICE):
            raise _OperatorCommandError(
                f"refusing config-health rebaseline while {DEFAULT_OPENCLAW_GATEWAY_SERVICE} "
                "is active; stop it first"
            )
        if database_path.is_symlink() or not database_path.is_file():
            raise _OperatorCommandError(f"missing database: {database_path}")
        console.print(
            "About to rebaseline config health: back up "
            f"{database_path}, then delete the entry for exactly {config_path}."
        )
        backup_path = _backup_openclaw_state_db(database_path)
        try:
            with contextlib.closing(sqlite3.connect(database_path)) as connection, connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'config_health_entries'"
                ).fetchone()
                if table is None:
                    raise _OperatorCommandError(
                        "config-health schema error: table config_health_entries does not exist"
                    )
                column_names = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(config_health_entries)")
                }
                if "config_path" not in column_names:
                    raise _OperatorCommandError(
                        "config-health schema error: table config_health_entries "
                        "does not have the expected config_path column"
                    )
                cursor = connection.execute(
                    "DELETE FROM config_health_entries WHERE config_path = ?",
                    (config_path,),
                )
                deleted = cursor.rowcount
                if deleted == 0:
                    raise _OperatorCommandError(
                        f"no matching config-health row for {config_path}; nothing rebaselined"
                    )
        except sqlite3.Error as exc:
            raise _OperatorCommandError(f"config-health database update failed: {exc}") from exc
    except Exception as exc:
        console.print(
            f"deployment-rebaseline-config-health failed: {exc}",
            markup=False,
            soft_wrap=True,
        )
        raise typer.Exit(code=1) from exc

    console.print(f"backup={backup_path}", markup=False)
    console.print(f"rows_deleted={deleted}", markup=False)
    console.print("Follow-up: re-run `openclaw config validate`.", markup=False)


@app.command("autoresearch-create-command-file")
def autoresearch_create_command_file(
    output_path: Path = _command_output_path_option,
) -> None:
    """Create a private detached-command file from the schema-v1 stdin protocol."""
    from gateway.autoresearch_runs import (
        AutoresearchRunRecordError,
        create_command_input_file_from_stdin,
    )

    try:
        create_command_input_file_from_stdin(
            output_path=output_path,
            payload=sys.stdin.buffer.read(),
        )
    except AutoresearchRunRecordError as exc:
        console.print(f"[red]autoresearch-create-command-file failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(str(output_path))


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
    readiness_manifest: Path = _readiness_manifest_option,
) -> None:
    """Validate autoresearch state/config and print the deterministic next action."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.engine import (
        next_action,
    )
    from gateway.autoresearch.manifest_runtime import (
        build_receipt_catalog,
    )
    from gateway.autoresearch.persistence import (
        load_state_file,
    )
    from gateway.autoresearch.workspace import (
        validate_target_worktree_clean,
    )

    try:
        state = load_state_file(state_path)
        policy = load_autoresearch_policy(openclaw_config)
        readiness = load_platform_readiness(readiness_manifest)
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
        action = next_action(
            state,
            policy,
            receipts,
            readiness=readiness,
            state_path=state_path,
        )
        reloaded_state = load_state_file(state_path)
        confirmed_action = next_action(
            reloaded_state,
            policy,
            receipts,
            readiness=readiness,
            state_path=state_path,
        )
        if (
            confirmed_action.source_manifest_sha256 != action.source_manifest_sha256
            or confirmed_action.state_reference_sha256 != action.state_reference_sha256
        ):
            raise ValueError(
                "autoresearch dispatch input changed after action construction; "
                "rerun autoresearch-next"
            )
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
    quantipy_root: Path = _quantipy_root_option,
    readiness_manifest: Path = _readiness_manifest_option,
    instruction_manifest_sha256: str | None = _instruction_manifest_sha256_option,
    state_reference_sha256: str | None = _state_reference_sha256_option,
) -> None:
    """Advance autoresearch state with a validated artifact and persist the result."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.manifest_runtime import (
        build_receipt_catalog,
        expected_instruction_manifest_sha256,
    )
    from gateway.autoresearch.persistence import (
        advance_artifact_state_file,
        load_state_file,
    )
    from gateway.autoresearch.state import (
        AutoresearchValidationContext,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        readiness = load_platform_readiness(readiness_manifest)
        validation_context = AutoresearchValidationContext.from_readiness(readiness)
        validation_context.validate_for_state(state)
        if instruction_manifest_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", instruction_manifest_sha256
        ):
            raise ValueError("instruction_manifest_sha256 must be a SHA-256 hex digest")
        if state_reference_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", state_reference_sha256
        ):
            raise ValueError("state_reference_sha256 must be a SHA-256 hex digest")
        if instruction_manifest_sha256 is None:
            receipts = build_receipt_catalog(quantipy_root)
            source_manifest_sha256 = expected_instruction_manifest_sha256(
                state,
                policy,
                receipts,
                state_path=state_path,
            )
        else:
            source_manifest_sha256 = instruction_manifest_sha256
        advance_artifact_state_file(
            state_path=state_path,
            output_path=output_path,
            artifact_path=artifact_path,
            instruction_manifest_sha256=source_manifest_sha256,
            policy=policy,
            validation_context=validation_context,
            state_reference_sha256=state_reference_sha256,
        )
    except ValueError as exc:
        console.print(f"[red]autoresearch-advance failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


@app.command("autoresearch-submit-stage")
def autoresearch_submit_stage(
    state_path: Path = _state_path_argument,
    artifact_path: Path = _artifact_path_argument,
    openclaw_config: Path = _openclaw_config_option,
    quantipy_root: Path = _quantipy_root_option,
    readiness_manifest: Path = _readiness_manifest_option,
    instruction_manifest_sha256: str | None = _instruction_manifest_sha256_option,
    state_reference_sha256: str | None = _state_reference_sha256_option,
) -> None:
    """Submit a validated stage artifact to the supervisor-owned inbox."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.constants import (
        DEFAULT_AUTORESEARCH_STAGE_INBOX,
    )
    from gateway.autoresearch.manifest_runtime import (
        build_receipt_catalog,
        expected_instruction_manifest_sha256,
    )
    from gateway.autoresearch.persistence import (
        load_state_file,
        submit_stage_artifact_file,
    )
    from gateway.autoresearch.state import (
        AutoresearchValidationContext,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        readiness = load_platform_readiness(readiness_manifest)
        validation_context = AutoresearchValidationContext.from_readiness(readiness)
        validation_context.validate_for_state(state)
        if instruction_manifest_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", instruction_manifest_sha256
        ):
            raise ValueError("instruction_manifest_sha256 must be a SHA-256 hex digest")
        if state_reference_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", state_reference_sha256
        ):
            raise ValueError("state_reference_sha256 must be a SHA-256 hex digest")
        if instruction_manifest_sha256 is None:
            receipts = build_receipt_catalog(quantipy_root)
            instruction_manifest_sha256 = expected_instruction_manifest_sha256(
                state,
                policy,
                receipts,
                state_path=state_path,
            )
        submission_path = submit_stage_artifact_file(
            state_path=state_path,
            artifact_path=artifact_path,
            inbox_path=DEFAULT_AUTORESEARCH_STAGE_INBOX,
            instruction_manifest_sha256=instruction_manifest_sha256,
            policy=policy,
            validation_context=validation_context,
            state_reference_sha256=state_reference_sha256,
        )
    except ValueError as exc:
        console.print(f"[red]autoresearch-submit-stage failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]submitted stage artifact:[/green] {submission_path}")


@app.command("autoresearch-pin-readiness")
def autoresearch_pin_readiness(
    state_path: Path = _state_path_argument,
    output_path: Path = _output_path_option,
    readiness_manifest: Path = _readiness_manifest_option,
) -> None:
    """Initialize readiness or explicitly repin an active state to the same IDs."""
    from gateway.autoresearch.lifecycle import (
        pin_platform_readiness,
    )
    from gateway.autoresearch.persistence import (
        load_state_file,
        persist_derived_state,
    )

    try:
        state = load_state_file(state_path)
        readiness = load_platform_readiness(readiness_manifest)
        next_state = pin_platform_readiness(state, readiness)
        persist_derived_state(state_path, output_path, state, next_state)
    except ValueError as exc:
        console.print(f"[red]autoresearch-pin-readiness failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


@app.command("autoresearch-build-readiness")
def autoresearch_build_readiness(
    manifest_path: Path = _readiness_build_manifest_argument,
    quantipy_root: Path = _quantipy_root_option,
    expected_quantipy_commit: str = _readiness_expected_commit_option,
    xnys_calendar: Path = _readiness_xnys_calendar_option,
    campaign_xnys_start: str = _readiness_campaign_xnys_start_option,
    campaign_xnys_end: str = _readiness_campaign_xnys_end_option,
    quantipy_evidence: Path | None = _readiness_evidence_output_option,
) -> None:
    """Build and revalidate strict Quantipy evidence plus a schema-v3 readiness manifest."""
    from datetime import date

    from gateway.autoresearch_readiness import build_quantipy_readiness

    evidence_path = quantipy_evidence or manifest_path.with_name("quantipy-data-contract.json")
    try:
        parsed_campaign_start = date.fromisoformat(campaign_xnys_start)
        parsed_campaign_end = date.fromisoformat(campaign_xnys_end)
        manifest = build_quantipy_readiness(
            manifest_path=manifest_path,
            quantipy_evidence_path=evidence_path,
            quantipy_root=quantipy_root,
            expected_quantipy_commit=expected_quantipy_commit,
            xnys_calendar_path=xnys_calendar,
            campaign_xnys_start=parsed_campaign_start,
            campaign_xnys_end=parsed_campaign_end,
        )
    except ValueError as exc:
        console.print(f"[red]autoresearch-build-readiness failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]wrote platform readiness v{manifest.schema_version}:[/green] {manifest_path}"
    )


@app.command("autoresearch-resume")
def autoresearch_resume(
    state_path: Path = _state_path_argument,
    output_path: Path = _output_path_option,
    openclaw_config: Path = _openclaw_config_option,
    readiness_manifest: Path = _readiness_manifest_option,
) -> None:
    """Explicitly recheck readiness and resume a suspended iteration."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.lifecycle import (
        resume_suspended_iteration,
    )
    from gateway.autoresearch.persistence import (
        load_state_file,
        persist_derived_state,
    )
    from gateway.autoresearch.transitions import (
        validate_state,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        validate_state(state, policy)
        readiness = load_platform_readiness(readiness_manifest)
        next_state = resume_suspended_iteration(state, readiness)
        persist_derived_state(state_path, output_path, state, next_state, policy=policy)
    except ValueError as exc:
        console.print(f"[red]autoresearch-resume failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


@app.command("autoresearch-acknowledge-campaign-review")
def autoresearch_acknowledge_campaign_review(
    state_path: Path = _state_path_argument,
    acknowledgement: str = typer.Option(
        ...,
        "--acknowledgement",
        help="Operator acknowledgement, stripped to 32-1024 characters.",
    ),
    output_path: Path = _output_path_option,
    openclaw_config: Path = _openclaw_config_option,
) -> None:
    """Acknowledge a pending campaign review and persist its repeat-phase state."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.lifecycle import (
        acknowledge_campaign_review,
    )
    from gateway.autoresearch.persistence import (
        load_state_file,
        persist_derived_state,
    )
    from gateway.autoresearch.transitions import (
        validate_state,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        validate_state(state, policy)
        next_state = acknowledge_campaign_review(state, acknowledgement)
        persist_derived_state(state_path, output_path, state, next_state, policy=policy)
    except ValueError as exc:
        console.print(f"[red]autoresearch-acknowledge-campaign-review failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]wrote autoresearch state:[/green] {output_path}")


@app.command("autoresearch-retry-external-verification")
def autoresearch_retry_external_verification(
    state_path: Path = _state_path_argument,
    reason: str = typer.Option(
        ..., "--reason", help="Exact non-empty operator infrastructure repair reason."
    ),
    openclaw_config: Path = _openclaw_config_option,
    quantipy_root: Path = _quantipy_root_option,
    readiness_manifest: Path = _readiness_manifest_option,
) -> None:
    """Operator-only bounded retry for the current local panel HTTP 413 verification failure."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.manifest_runtime import (
        build_receipt_catalog,
    )
    from gateway.autoresearch.operator_recovery import (
        retry_external_verification_state_file,
    )
    from gateway.autoresearch.state import (
        AutoresearchValidationContext,
    )
    from gateway.autoresearch_readiness import (
        EXTERNAL_VERIFICATION_RETRY_OPERATOR_ENV_VAR,
        EXTERNAL_VERIFICATION_RETRY_OPERATOR_VALUE,
        load_platform_readiness,
        probe_research_panel_for_external_verification_retry,
    )

    try:
        if os.environ.get(EXTERNAL_VERIFICATION_RETRY_OPERATOR_ENV_VAR) != (
            EXTERNAL_VERIFICATION_RETRY_OPERATOR_VALUE
        ):
            raise ValueError(
                "operator capability is required; set "
                f"{EXTERNAL_VERIFICATION_RETRY_OPERATOR_ENV_VAR}=1 in the human/Codex shell"
            )
        policy = load_autoresearch_policy(openclaw_config)
        readiness = load_platform_readiness(readiness_manifest)
        build_receipt_catalog(quantipy_root)
        probe = probe_research_panel_for_external_verification_retry()
        state = retry_external_verification_state_file(
            state_path,
            probe,
            operator_reason=reason,
            policy=policy,
            validation_context=AutoresearchValidationContext.from_readiness(readiness),
        )
    except ValueError as exc:
        console.print(f"[red]autoresearch-retry-external-verification failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    receipt = state.external_verification_retry_receipt
    assert receipt is not None
    console.print(
        f"[green]external verification retry authorized:[/green] {receipt.expected_run_id}"
    )


@app.command("autoresearch-recover-interrupted-verification")
def autoresearch_recover_interrupted_verification(
    state_path: Path = _state_path_argument,
    reason: str = typer.Option(
        ..., "--reason", help="Exact non-empty operator reason for the detached v3 stop."
    ),
    openclaw_config: Path = _openclaw_config_option,
    quantipy_root: Path = _quantipy_root_option,
    readiness_manifest: Path = _readiness_manifest_option,
) -> None:
    """Operator-only recovery for the one sealed, stopped detached v3 verification run."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.constants import (
        INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_ENV_VAR,
        INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_VALUE,
    )
    from gateway.autoresearch.manifest_runtime import (
        build_receipt_catalog,
    )
    from gateway.autoresearch.operator_recovery import (
        recover_interrupted_verification_state_file,
    )
    from gateway.autoresearch.state import (
        AutoresearchValidationContext,
    )
    from gateway.autoresearch_readiness import load_platform_readiness

    try:
        if os.environ.get(INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_ENV_VAR) != (
            INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_VALUE
        ):
            raise ValueError(
                "operator capability is required; set "
                f"{INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_ENV_VAR}=1 in the human/Codex shell"
            )
        policy = load_autoresearch_policy(openclaw_config)
        readiness = load_platform_readiness(readiness_manifest)
        state = recover_interrupted_verification_state_file(
            state_path,
            operator_reason=reason,
            policy=policy,
            receipts=build_receipt_catalog(quantipy_root),
            validation_context=AutoresearchValidationContext.from_readiness(readiness),
        )
    except ValueError as exc:
        console.print(f"[red]autoresearch-recover-interrupted-verification failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    receipt = state.external_verification_retry_receipt
    assert receipt is not None
    console.print(
        f"[green]interrupted verification recovery authorized:[/green] {receipt.expected_run_id}"
    )


@app.command("autoresearch-recover-platform-runtime")
def autoresearch_recover_platform_runtime(
    state_path: Path = _state_path_argument,
    reason: str = typer.Option(
        ..., "--reason", help="Exact non-empty operator reason for canonical runtime recovery."
    ),
    openclaw_config: Path = _openclaw_config_option,
    readiness_manifest: Path = _readiness_manifest_option,
) -> None:
    """Operator-only exact recovery of the sealed v4 panel receipt failure into v5."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.constants import (
        PLATFORM_RUNTIME_RECOVERY_OPERATOR_ENV_VAR,
        PLATFORM_RUNTIME_RECOVERY_OPERATOR_VALUE,
    )
    from gateway.autoresearch.operator_recovery import (
        recover_platform_runtime_state_file,
    )
    from gateway.autoresearch.state import (
        AutoresearchValidationContext,
    )
    from gateway.autoresearch_readiness import (
        load_platform_readiness,
        probe_research_panel_for_external_verification_retry,
    )

    try:
        if os.environ.get(PLATFORM_RUNTIME_RECOVERY_OPERATOR_ENV_VAR) != (
            PLATFORM_RUNTIME_RECOVERY_OPERATOR_VALUE
        ):
            raise ValueError(
                "operator capability is required; set "
                f"{PLATFORM_RUNTIME_RECOVERY_OPERATOR_ENV_VAR}=1 in the human/Codex shell"
            )
        policy = load_autoresearch_policy(openclaw_config)
        readiness = load_platform_readiness(readiness_manifest)
        state = recover_platform_runtime_state_file(
            state_path,
            probe=probe_research_panel_for_external_verification_retry(),
            operator_reason=reason,
            policy=policy,
            validation_context=AutoresearchValidationContext.from_readiness(readiness),
        )
    except ValueError as exc:
        console.print(f"[red]autoresearch-recover-platform-runtime failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    receipt = state.external_verification_retry_receipt
    assert receipt is not None
    console.print(f"[green]platform runtime recovery authorized:[/green] {receipt.expected_run_id}")


@app.command("autoresearch-suspend-infra")
def autoresearch_suspend_infra(
    state_path: Path = _state_path_argument,
    reason: str = typer.Option(
        ..., "--reason", help="Exact non-empty operator infrastructure reason."
    ),
    output_path: Path = _output_path_option,
    openclaw_config: Path = _openclaw_config_option,
) -> None:
    """Durably suspend an active alpha iteration for operator-owned infrastructure repair."""
    from gateway.autoresearch.configuration import (
        load_autoresearch_policy,
    )
    from gateway.autoresearch.lifecycle import (
        suspend_for_infrastructure,
    )
    from gateway.autoresearch.persistence import (
        load_state_file,
        persist_derived_state,
    )
    from gateway.autoresearch.transitions import (
        validate_state,
    )

    try:
        policy = load_autoresearch_policy(openclaw_config)
        state = load_state_file(state_path)
        validate_state(state, policy)
        next_state = suspend_for_infrastructure(state, reason)
        validate_state(next_state, policy)
        persist_derived_state(state_path, output_path, state, next_state, policy=policy)
    except ValueError as exc:
        console.print(f"[red]autoresearch-suspend-infra failed:[/red] {exc}")
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
    """Return whether Vite exposes the exact simulator automation health contract."""
    try:
        req = urllib.request.Request(f"http://localhost:{port}/_dev/health")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return False
            if response.headers.get_content_type() != "application/json":
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and set(payload) == {"ok"} and payload["ok"] is True
    except (urllib.error.URLError, OSError, UnicodeDecodeError, ValueError):
        return False


def _vite_launch_command() -> list[str]:
    """Return the loopback-only Vite command with simulator controls enabled."""
    return ["npm", "run", "dev:sim"]


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


def _simulator_launch_command(
    sim_cmd: list[str], *, env: dict[str, str] | None = None
) -> list[str]:
    """Return a simulator command that has a display backend in headless shells."""
    launch_env = os.environ if env is None else env
    if launch_env.get("DISPLAY") or launch_env.get("WAYLAND_DISPLAY"):
        return sim_cmd
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        raise _SimulatorLaunchError(
            "EvenHub simulator needs DISPLAY/WAYLAND_DISPLAY or xvfb-run in PATH"
        )
    return [xvfb_run, "-a", *sim_cmd]


def _require_simulator_backend(env: dict[str, str] | None = None) -> None:
    """Fail before launching services if the native simulator cannot render."""
    launch_env = os.environ if env is None else env
    if launch_env.get("DISPLAY") or launch_env.get("WAYLAND_DISPLAY"):
        return
    if shutil.which("xvfb-run") is None:
        raise _SimulatorLaunchError(
            "EvenHub simulator needs DISPLAY/WAYLAND_DISPLAY or xvfb-run in PATH"
        )


def _require_simulator_still_running(
    proc: subprocess.Popen[Any], *, log_path: Path, timeout: float = 2.0
) -> None:
    """Catch immediate native simulator startup failures before reporting success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            detail = _tail_file_text(log_path, bytes_limit=4096)
            raise _SimulatorLaunchError(
                f"EvenHub simulator exited during startup with code {exit_code}: "
                f"{detail or 'no log output'}"
            )
        time.sleep(0.1)


def _tail_file_text(path: Path, *, bytes_limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > bytes_limit:
                handle.seek(size - bytes_limit)
                handle.readline()
            return handle.read().decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise _SimulatorLaunchError(f"failed to read simulator log {path}: {exc}") from exc


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
    auth, copy agent bootstrap files, and install managed service drop-ins),
    then restarts the OpenClaw daemon.
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

        # Restart via openclaw CLI
        console.print(
            "  Restarting OpenClaw daemon on port "
            f"{openclaw_port} via {openclaw.path} (version {openclaw.version_text})…"
        )
        restart_result = subprocess.run(
            [str(openclaw.path), "daemon", "restart"],
            check=False,
            capture_output=True,
            env=_openclaw_daemon_env(),
        )
        if restart_result.returncode != 0:
            console.print(
                f"[red]✗[/red] Daemon restart failed (exit code {restart_result.returncode})"
            )
            raise typer.Exit(code=1)
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

    if not no_simulator:
        try:
            _require_simulator_backend()
        except _SimulatorLaunchError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from exc
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
                console.print(f"  [green]✓[/green] Already running on port {openclaw_port}")
            else:
                _needs_openclaw_start = True

        if (_needs_openclaw_start or _needs_openclaw_restart) and not no_openclaw:
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
                    env=_openclaw_daemon_env(),
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
                    env=_openclaw_daemon_env(),
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
                    _vite_launch_command(),
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
                    _vite_launch_command(),
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
                launch_cmd = _simulator_launch_command(sim_cmd, env=sim_env)
                sim_proc = subprocess.Popen(
                    launch_cmd,
                    cwd=str(_PROJECT_ROOT),
                    stdout=_sim_log,
                    stderr=_sim_log,
                    env=sim_env,
                    start_new_session=True,
                )
                spawned.append(sim_proc)
                _require_simulator_still_running(sim_proc, log_path=_log_dir / "simulator.log")
                simulator_started = True
                console.print("  [green]✓[/green] Simulator launched")
            except FileNotFoundError:
                console.print("  [red]✗[/red] evenhub-simulator not found on PATH — skipping")
            except _SimulatorLaunchError as exc:
                console.print(f"  [red]✗[/red] {exc}")
                raise typer.Exit(code=1) from exc

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
    except Exception:
        _cleanup()
        raise
