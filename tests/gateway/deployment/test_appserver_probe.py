"""Tests for the Codex app-server doctor probe."""

from __future__ import annotations

from pathlib import Path

import pytest
from gateway.deployment import appserver_probe


def _write_process(
    proc_root: Path,
    pid: int,
    *,
    cmdline: bytes,
    environ: bytes,
    status: bytes | None = None,
    cgroup: bytes | None = None,
) -> Path:
    process_dir = proc_root / str(pid)
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(cmdline)
    (process_dir / "environ").write_bytes(environ)
    if status is not None:
        (process_dir / "status").write_bytes(status)
    if cgroup is not None:
        (process_dir / "cgroup").write_bytes(cgroup)
    return process_dir


def test_probe_detects_real_child_arg0_topology_and_missing_root(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stale_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0stale"
    _write_process(
        proc_root,
        1000,
        cmdline=b"/opt/codex\0app-server\0",
        environ=b"PATH=/usr/bin\0",
    )
    _write_process(
        proc_root,
        1001,
        cmdline=b"codex-code-mode-host\0",
        environ=f"PATH={stale_dir}:/usr/bin\0OTHER=value\0".encode(),
        status=b"Name:\tcodex-code-mode-host\nPPid:\t1000\n",
    )
    missing_root = tmp_path / "missing-root"

    result = appserver_probe.probe_appserver(
        proc_root=proc_root,
        writable_roots=(missing_root,),
    )

    assert result.missing_writable_roots == (missing_root,)
    assert result.stale_arg0_directories == (appserver_probe.StaleArg0Directory(1000, stale_dir),)


def test_probe_is_silent_when_child_arg0_directory_exists(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    existing_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0live"
    existing_dir.mkdir(parents=True)
    _write_process(
        proc_root,
        2000,
        cmdline=b"node\0/opt/.bin/codex\0app-server\0",
        environ=b"PATH=/usr/bin\0",
    )
    _write_process(
        proc_root,
        2001,
        cmdline=b"codex-code-mode-host\0",
        environ=f"PATH=/usr/bin:{existing_dir}\0".encode(),
        status=b"Name:\tcodex-code-mode-host\nPPid:\t2000\n",
    )

    result = appserver_probe.probe_appserver(
        proc_root=proc_root,
        writable_roots=(),
    )

    assert result.missing_writable_roots == ()
    assert result.stale_arg0_directories == ()


def test_probe_falls_back_to_child_pid_without_appserver_ancestor(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stale_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0orphan"
    _write_process(
        proc_root,
        3000,
        cmdline=b"codex-linux-sandbox\0",
        environ=f"PATH={stale_dir}\0".encode(),
        status=b"Name:\tcodex-linux-sandbox\nPPid:\t1\n",
        cgroup=b"0::/user.slice/user-1000.slice/openclaw-gateway.service\n",
    )

    assert appserver_probe.find_stale_arg0_directories(proc_root) == (
        appserver_probe.StaleArg0Directory(3000, stale_dir),
    )


def test_probe_fallback_ignores_process_in_unrelated_cgroup(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stale_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0operator"
    _write_process(
        proc_root,
        3001,
        cmdline=b"codex-linux-sandbox\0",
        environ=f"PATH={stale_dir}\0".encode(),
        status=b"Name:\tcodex-linux-sandbox\nPPid:\t1\n",
        cgroup=b"0::/user.slice/user-1000.slice/session-4.scope\n",
    )

    assert appserver_probe.find_stale_arg0_directories(proc_root) == ()


def test_probe_fallback_ignores_process_without_readable_cgroup(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stale_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0unknown"
    _write_process(
        proc_root,
        3002,
        cmdline=b"codex-linux-sandbox\0",
        environ=f"PATH={stale_dir}\0".encode(),
        status=b"Name:\tcodex-linux-sandbox\nPPid:\t1\n",
    )

    assert appserver_probe.find_stale_arg0_directories(proc_root) == ()


@pytest.mark.parametrize(
    "cgroup",
    [
        b"0::/user.slice/user-1000.slice/openclaw-gateway.service.d\n",
        b"0::/user.slice/user-1000.slice/openclaw-gateway.service-worker\n",
    ],
)
def test_probe_fallback_requires_exact_gateway_cgroup_unit(
    tmp_path: Path,
    cgroup: bytes,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stale_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0boundary"
    _write_process(
        proc_root,
        3003,
        cmdline=b"codex-linux-sandbox\0",
        environ=f"PATH={stale_dir}\0".encode(),
        status=b"Name:\tcodex-linux-sandbox\nPPid:\t1\n",
        cgroup=cgroup,
    )

    assert appserver_probe.find_stale_arg0_directories(proc_root) == ()


def test_probe_ignores_non_codex_processes_that_inherited_arg0_path(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stale_dir = tmp_path / "codex-home" / "tmp" / "arg0" / "codex-arg0inherited"
    _write_process(
        proc_root,
        4000,
        cmdline=b"next-server (v15.0.0)\0",
        environ=f"PATH={stale_dir}:/usr/bin\0".encode(),
        status=b"Name:\tnext-server\nPPid:\t1\n",
    )

    assert appserver_probe.find_stale_arg0_directories(proc_root) == ()


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (b"/opt/codex\0app-server\0", True),
        (b"node\0/opt/.bin/codex\0app-server\0", True),
        (b"/bin/sh\0-c\0codex app-server\0", False),
    ],
)
def test_codex_app_server_matching_uses_argv_structure(cmdline: bytes, expected: bool) -> None:
    assert appserver_probe._is_codex_app_server(cmdline) is expected


@pytest.mark.parametrize("environ", [b"HOME=/tmp\0", b"PATH=\0HOME=/tmp\0"])
def test_probe_ignores_processes_without_a_path_value(
    tmp_path: Path,
    environ: bytes,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        4000,
        cmdline=b"codex-code-mode-host\0",
        environ=environ,
        status=b"Name:\tchild\nPPid:\t1\n",
    )

    assert appserver_probe.find_stale_arg0_directories(proc_root) == ()


def test_probe_tolerates_unreadable_environ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    process_dir = _write_process(
        proc_root,
        5000,
        cmdline=b"codex-code-mode-host\0",
        environ=b"PATH=/tmp/arg0/codex-arg0missing\0",
        status=b"Name:\tchild\nPPid:\t1\n",
    )
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == process_dir / "environ":
            raise PermissionError(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    assert appserver_probe.find_stale_arg0_directories(proc_root) == ()
