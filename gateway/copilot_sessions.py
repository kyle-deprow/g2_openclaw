"""Discover running Copilot CLI sessions from ~/.copilot/session-state/."""

from __future__ import annotations

import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_STATE_DIR = Path.home() / ".copilot" / "session-state"

# Pattern: inuse.<PID>.lock
_LOCK_RE = re.compile(r"^inuse\.(\d+)\.lock$")

# Single-slot TTL cache for list_copilot_sessions().  Designed for the one
# call site in server.py that polls on each copilot_session_list_request.
#   (monotonic_ts, state_dir, running_only, cached_list) or None
_copilot_cache: tuple[float, Path, bool, list[CopilotSessionInfo]] | None = None
_COPILOT_CACHE_TTL: float = 3.0  # seconds


def invalidate_copilot_cache() -> None:
    """Clear the copilot session list cache, forcing a fresh scan on next call."""
    global _copilot_cache
    _copilot_cache = None


@dataclass(frozen=True, slots=True)
class CopilotSessionInfo:
    """Metadata for a single Copilot CLI session."""

    session_id: str
    cwd: str
    dir_name: str
    git_root: str | None
    repository: str | None
    branch: str | None
    summary: str
    created_at: str
    updated_at: str
    pid: int
    is_running: bool


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive via /proc/<pid>/stat (Linux) or os.kill signal 0."""
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        return True
    # Fallback for non-Linux or /proc not mounted
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def _find_lock_pid(session_dir: Path) -> int | None:
    """Find a live lock file PID in the session directory. Return None if no live lock."""
    for entry in session_dir.iterdir():
        m = _LOCK_RE.match(entry.name)
        if m:
            return int(m.group(1))
    return None


def _parse_workspace_yaml(session_dir: Path) -> dict[str, str | None] | None:
    """Parse workspace.yaml and return key fields. Return None on failure."""
    ws_path = session_dir / "workspace.yaml"
    if not ws_path.exists():
        return None
    try:
        with ws_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        git_root = data.get("git_root")
        repository = data.get("repository")
        branch = data.get("branch")
        return {
            "id": str(data.get("id", "")),
            "cwd": str(data.get("cwd", "")),
            "git_root": str(git_root) if git_root is not None else None,
            "repository": str(repository) if repository is not None else None,
            "branch": str(branch) if branch is not None else None,
            "summary": str(data.get("summary", "")),
            "created_at": str(data.get("created_at", "")),
            "updated_at": str(data.get("updated_at", "")),
        }
    except Exception:
        logger.debug("Failed to parse workspace.yaml in %s", session_dir, exc_info=True)
        return None


def list_copilot_sessions(
    state_dir: Path | None = None,
    running_only: bool = True,
) -> list[CopilotSessionInfo]:
    """Scan the Copilot session-state directory for sessions.

    Args:
        state_dir: Override the default ``~/.copilot/session-state/`` path.
        running_only: If True, only return sessions with a live lock file PID.

    Returns:
        List of ``CopilotSessionInfo`` sorted by ``updated_at`` descending.
    """
    global _copilot_cache

    root = state_dir or _DEFAULT_STATE_DIR

    # Check TTL cache (must match same call parameters)
    if _copilot_cache is not None:
        cache_ts, cache_dir, cache_ro, cached_list = _copilot_cache
        if (
            cache_dir == root
            and cache_ro == running_only
            and time.monotonic() - cache_ts < _COPILOT_CACHE_TTL
        ):
            return cached_list
    if not root.is_dir():
        logger.info("Copilot session-state dir not found: %s", root)
        return []

    sessions: list[CopilotSessionInfo] = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue

        pid = _find_lock_pid(entry)
        is_running = pid is not None and _pid_alive(pid)

        if running_only and not is_running:
            continue

        meta = _parse_workspace_yaml(entry)
        if meta is None:
            continue

        cwd = meta["cwd"] or ""
        sessions.append(
            CopilotSessionInfo(
                session_id=meta["id"] or entry.name,
                cwd=cwd,
                dir_name=Path(cwd).name if cwd else entry.name,
                git_root=meta.get("git_root"),
                repository=meta.get("repository"),
                branch=meta.get("branch"),
                summary=(meta["summary"] or "")[:200],
                created_at=meta["created_at"] or "",
                updated_at=meta["updated_at"] or "",
                pid=pid or 0,
                is_running=is_running,
            )
        )

    # Sort newest first
    sessions.sort(key=lambda s: s.updated_at, reverse=True)

    # Update cache
    _copilot_cache = (time.monotonic(), root, running_only, sessions)

    return sessions


def kill_copilot_session(
    session_id: str,
    state_dir: Path | None = None,
) -> bool:
    """Kill a running Copilot CLI session by sending SIGTERM to its PID.

    Args:
        session_id: The session UUID to kill.
        state_dir: Override the default ``~/.copilot/session-state/`` path.

    Returns:
        True if the signal was sent successfully, False otherwise.
    """
    root = state_dir or _DEFAULT_STATE_DIR
    if "/" in session_id or ".." in session_id:
        logger.warning("Invalid session_id (path traversal attempt): %s", session_id)
        return False
    session_dir = root / session_id
    if not session_dir.is_dir():
        logger.warning("Copilot session dir not found: %s", session_dir)
        return False

    pid = _find_lock_pid(session_dir)
    if pid is None:
        logger.warning("No lock file found for session %s", session_id)
        return False

    if not _pid_alive(pid):
        logger.warning("PID %d for session %s is not alive", pid, session_id)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to PID %d (session %s)", pid, session_id)
        invalidate_copilot_cache()
        return True
    except (ProcessLookupError, PermissionError) as exc:
        logger.warning("Failed to kill PID %d for session %s: %s", pid, session_id, exc)
        return False
