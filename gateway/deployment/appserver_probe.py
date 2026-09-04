"""Read-only probes for Codex app-server process and sandbox prerequisites."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .codex_agents import CODEX_WRITABLE_ROOTS

APP_SERVER_RESTART_REMEDIATION = (
    "restart the gateway: systemctl --user restart openclaw-gateway.service"
)


@dataclass(frozen=True, slots=True)
class StaleArg0Directory:
    """A process using a vanished Codex arg0 shim directory."""

    pid: int
    path: Path


@dataclass(frozen=True, slots=True)
class AppServerProbeResult:
    """Read-only app-server and writable-root health findings."""

    missing_writable_roots: tuple[Path, ...]
    stale_arg0_directories: tuple[StaleArg0Directory, ...]


def _is_codex_app_server(cmdline: bytes) -> bool:
    """Return whether argv has a direct or node-wrapped codex app-server shape."""

    arguments = [part.decode("utf-8", errors="replace") for part in cmdline.split(b"\0") if part]
    if len(arguments) >= 2 and Path(arguments[0]).name == "codex" and arguments[1] == "app-server":
        return True
    return (
        len(arguments) >= 3
        and Path(arguments[0]).name in {"node", "nodejs"}
        and Path(arguments[1]).name == "codex"
        and arguments[2] == "app-server"
    )


def _path_from_environ(environ: bytes) -> str | None:
    """Extract PATH from a NUL-separated process environment."""

    for item in environ.split(b"\0"):
        if item.startswith(b"PATH="):
            return item[5:].decode("utf-8", errors="replace")
    return None


def _arg0_directory(path_entry: str) -> Path | None:
    """Return a PATH entry matching the Codex arg0 shim directory layout."""

    candidate = Path(path_entry)
    if (
        candidate.parent.name != "arg0"
        or candidate.parent.parent.name != "tmp"
        or not candidate.name.startswith("codex-arg0")
    ):
        return None
    return candidate


def _is_codex_runtime_process(cmdline: bytes) -> bool:
    """Return whether argv[0] is a Codex runtime binary (not an env inheritor)."""

    arguments = [part.decode("utf-8", errors="replace") for part in cmdline.split(b"\0") if part]
    if not arguments:
        return False
    return Path(arguments[0]).name.startswith("codex")


def _ppid_from_status(status: bytes) -> int | None:
    """Extract PPid from a Linux /proc status file."""

    for line in status.splitlines():
        if not line.startswith(b"PPid:"):
            continue
        fields = line.split()
        if len(fields) != 2:
            return None
        try:
            return int(fields[1])
        except ValueError:
            return None
    return None


def _cgroup_path(proc_root: Path, pid: int) -> str | None:
    """Return the cgroup v2 path for a process, if it can be parsed."""

    try:
        cgroup = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in cgroup.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "" and fields[2].startswith("/"):
            return fields[2]
    return None


def _appserver_ancestor_pid(proc_root: Path, child_pid: int) -> int | None:
    """Find the nearest codex app-server in a process's PPid chain."""

    current_pid = child_pid
    visited: set[int] = set()
    while current_pid > 0 and current_pid not in visited:
        visited.add(current_pid)
        process_dir = proc_root / str(current_pid)
        try:
            if _is_codex_app_server((process_dir / "cmdline").read_bytes()):
                return current_pid
            parent_pid = _ppid_from_status((process_dir / "status").read_bytes())
        except OSError:
            return None
        if parent_pid is None or parent_pid == current_pid:
            return None
        current_pid = parent_pid
    return None


def find_stale_arg0_directories(proc_root: Path = Path("/proc")) -> tuple[StaleArg0Directory, ...]:
    """Find vanished arg0 directories and attribute them to app-server ancestors."""

    try:
        process_dirs = tuple(proc_root.iterdir())
    except OSError:
        return ()

    findings: list[StaleArg0Directory] = []
    for process_dir in process_dirs:
        if not process_dir.name.isdecimal():
            continue
        try:
            path_value = _path_from_environ((process_dir / "environ").read_bytes())
        except OSError:
            continue
        if not path_value:
            continue

        seen_paths: set[Path] = set()
        for path_entry in path_value.split(os.pathsep):
            arg0_directory = _arg0_directory(path_entry)
            if arg0_directory is None or arg0_directory in seen_paths:
                continue
            seen_paths.add(arg0_directory)
            if arg0_directory.is_dir():
                continue
            child_pid = int(process_dir.name)
            owner_pid = _appserver_ancestor_pid(proc_root, child_pid)
            if owner_pid is None:
                try:
                    cmdline = (process_dir / "cmdline").read_bytes()
                except OSError:
                    continue
                if not _is_codex_runtime_process(cmdline):
                    continue
                cgroup_path = _cgroup_path(proc_root, child_pid)
                if cgroup_path is None or "openclaw-gateway.service" not in cgroup_path.split("/"):
                    continue
                owner_pid = child_pid
            findings.append(StaleArg0Directory(owner_pid, arg0_directory))
    return tuple(findings)


def probe_appserver(
    *,
    proc_root: Path = Path("/proc"),
    writable_roots: Sequence[Path] = CODEX_WRITABLE_ROOTS,
) -> AppServerProbeResult:
    """Probe declared writable roots and running Codex app-server arg0 paths."""

    missing_writable_roots = tuple(root for root in writable_roots if not root.is_dir())
    return AppServerProbeResult(
        missing_writable_roots=missing_writable_roots,
        stale_arg0_directories=find_stale_arg0_directories(proc_root),
    )
