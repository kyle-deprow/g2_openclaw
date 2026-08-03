"""White-box and black-box tests for deployment file identity guards."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.deployment.identity import (
    file_bytes,
    file_sha256,
    guarded_regular_file_identity,
    verify_guarded_regular_file_identity_unchanged,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
IDENTITY_MODULE = "gateway.deployment.identity"


def _run_identity(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", IDENTITY_MODULE, *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def test_file_measurements_match_shell_tools(tmp_path: Path) -> None:
    path = tmp_path / "payload"
    payload = b"same bytes\0with a newline\n"
    path.write_bytes(payload)

    assert file_sha256(str(path)) == hashlib.sha256(payload).hexdigest()
    assert file_bytes(str(path)) == len(payload)
    assert _run_identity(["file-sha256", str(path)]).stdout == (
        hashlib.sha256(payload).hexdigest() + "\n"
    )
    assert _run_identity(["file-bytes", str(path)]).stdout == str(len(payload))


def test_file_bytes_rejects_directory(tmp_path: Path) -> None:
    result = _run_identity(["file-bytes", str(tmp_path)])

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: Could not read guarded file while measuring bytes: {tmp_path}\n"
        "       Refusing before mutation.\n"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="mode-000 read test is ineffective for root")
def test_file_bytes_rejects_mode_zero_file(tmp_path: Path) -> None:
    path = tmp_path / "unreadable"
    path.write_bytes(b"secret\n")
    path.chmod(0)

    try:
        result = _run_identity(["file-bytes", str(path)])
    finally:
        path.chmod(0o600)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: Could not read guarded file while measuring bytes: {path}\n"
        "       Refusing before mutation.\n"
    )


def test_guarded_regular_file_identity_captures_gnu_stat_shape(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("{}\n", encoding="utf-8")

    identity = guarded_regular_file_identity(str(path), "capturing config")
    stat_fields = identity.split(":")
    path_stat = os.lstat(path)

    assert len(stat_fields) == 4
    assert stat_fields == [
        str(path_stat.st_dev),
        str(path_stat.st_ino),
        f"{path_stat.st_mode:x}",
        "1",
    ]


def test_guarded_regular_file_identity_rejects_leaf_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("target\n", encoding="utf-8")
    link = tmp_path / "config"
    link.symlink_to(target)

    with pytest.raises(
        RuntimeError,
        match=(
            rf"ERROR: Guarded file is a symlink while reading config: {link} -> {target}\n"
            r"       Refusing to trust followed bytes for guarded config publication\."
        ),
    ):
        guarded_regular_file_identity(str(link), "reading config")


def test_guarded_regular_file_identity_rejects_hardlink(tmp_path: Path) -> None:
    path = tmp_path / "config"
    alias = tmp_path / "alias"
    path.write_text("same\n", encoding="utf-8")
    alias.hardlink_to(path)

    with pytest.raises(
        RuntimeError, match=rf"Guarded file is hard-linked.*{path} \(link count 2\)"
    ):
        guarded_regular_file_identity(str(path), "reading config")


def test_identity_reverification_detects_recreated_same_bytes(tmp_path: Path) -> None:
    path = tmp_path / "config"
    replacement = tmp_path / "replacement"
    path.write_bytes(b"identical bytes\n")
    expected = guarded_regular_file_identity(str(path), "capturing config")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)

    with pytest.raises(
        RuntimeError,
        match=rf"ERROR: Guarded file identity/topology changed during validation: {path}\.\n",
    ):
        verify_guarded_regular_file_identity_unchanged(str(path), expected, "validation")


def test_identity_reverification_accepts_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("unchanged\n", encoding="utf-8")
    expected = guarded_regular_file_identity(str(path), "capturing config")

    verify_guarded_regular_file_identity_unchanged(str(path), expected, "validation")
