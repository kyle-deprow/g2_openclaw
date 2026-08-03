"""White-box and bash differential tests for guarded filesystem primitives."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.deployment.guarded_fs import (
    guard_destination_parent_path_chain,
    guard_destination_path_chain,
    guard_no_hardlinked_regular_file,
    guarded_copy_path_topology_preserving_final_symlink_topology,
    path_owned_by_effective_user,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDED_FS_MODULE = "gateway.deployment.guarded_fs"


def _run_module(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", GUARDED_FS_MODULE, *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def test_destination_chain_accepts_deep_existing_path(tmp_path: Path) -> None:
    destination = tmp_path
    for index in range(32):
        destination /= f"level-{index}"
        destination.mkdir()
    destination /= "new-file"

    guard_destination_path_chain(str(destination), "checking deep destination")


def test_destination_chain_rejects_symlink_at_leaf(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_text("outside\n", encoding="utf-8")
    destination = tmp_path / "managed"
    destination.symlink_to(target)

    with pytest.raises(
        RuntimeError,
        match=rf"Destination path chain contains symlink while writing: {destination} -> {target}",
    ):
        guard_destination_path_chain(str(destination), "writing")


def test_destination_chain_rejects_symlink_at_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    ancestor = tmp_path / "managed"
    ancestor.symlink_to(target, target_is_directory=True)
    destination = ancestor / "nested" / "file"

    result = _run_module(["guard-destination-path-chain", str(destination), "writing"])

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: Destination path chain contains symlink while writing: {ancestor} -> {target}\n"
        f"       Full destination: {destination}\n"
        "       Refusing before mutation to avoid following nested symlinks outside the "
        "managed root.\n"
    )


def test_destination_parent_chain_allows_final_symlink_but_not_ancestor(tmp_path: Path) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    target = tmp_path / "outside"
    target.write_text("old\n", encoding="utf-8")
    destination = parent / "config"
    destination.symlink_to(target)
    source = tmp_path / "source"
    source.write_text("new\n", encoding="utf-8")

    guard_destination_parent_path_chain(str(destination), "publishing")
    assert (
        guarded_copy_path_topology_preserving_final_symlink_topology(
            str(source), str(destination), "publishing"
        )
        == 0
    )
    assert destination.read_text(encoding="utf-8") == "new\n"
    assert target.read_text(encoding="utf-8") == "new\n"
    assert destination.is_symlink()


def test_destination_parent_chain_strips_trailing_slashes_before_dirname(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_text("outside\n", encoding="utf-8")
    final_component = tmp_path / "leaf"
    final_component.symlink_to(target)

    guard_destination_parent_path_chain(f"{final_component}/", "publishing")


@pytest.mark.parametrize(
    "error", [OSError(20, "not a directory"), OSError(13, "permission denied")]
)
def test_destination_chain_treats_any_lstat_failure_as_missing(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    def fail_lstat(path: str) -> os.stat_result:
        raise error

    monkeypatch.setattr(os, "lstat", fail_lstat)

    guard_destination_path_chain("/managed/config", "checking")


def test_literal_help_is_guarded_as_a_path() -> None:
    result = _run_module(["guard-destination-path-chain", "--", "--help", "checking"])

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "ERROR: Destination path is not absolute while checking: --help\n"
        "       Refusing before mutation.\n"
    )


def test_hardlinked_regular_file_is_rejected_before_mutation(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    alias = tmp_path / "external-alias"
    alias.write_bytes(b"external\n")
    destination.hardlink_to(alias)

    with pytest.raises(
        RuntimeError,
        match=(
            rf"Destination path is a hard-linked regular file while copying: {destination} "
            r"\(link count 2\)"
        ),
    ):
        guard_no_hardlinked_regular_file(str(destination), "copying")
    assert alias.read_bytes() == b"external\n"


def test_hardlink_guard_does_not_follow_symlink_to_hardlinked_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    alias = tmp_path / "alias"
    target.write_bytes(b"shared\n")
    alias.hardlink_to(target)
    link = tmp_path / "link"
    link.symlink_to(target)

    guard_no_hardlinked_regular_file(str(link), "checking")


def test_hardlink_guard_silently_proceeds_when_nlink_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "destination"
    path.write_bytes(b"content\n")
    real_lstat = os.lstat
    call_count = 0

    def lstat_with_failed_nlink_read(candidate: str) -> os.stat_result:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError(13, "permission denied")
        return real_lstat(candidate)

    monkeypatch.setattr(os, "lstat", lstat_with_failed_nlink_read)

    guard_no_hardlinked_regular_file(str(path), "checking")
    assert call_count == 2


@pytest.mark.skipif(os.geteuid() != 0, reason="wrong-owner test requires root to chown safely")
def test_effective_owner_predicate_matches_bash_o_test(tmp_path: Path) -> None:
    path = tmp_path / "owned"
    path.write_text("owned\n", encoding="utf-8")
    assert path_owned_by_effective_user(str(path))

    wrong_uid = 65534 if os.geteuid() != 65534 else 0
    try:
        os.chown(path, wrong_uid, -1)
    except PermissionError:
        pytest.skip("changing file owner requires elevated test privileges")
    assert not path_owned_by_effective_user(str(path))


def _committed_bash_guard_functions() -> str:
    completed = subprocess.run(
        # HEAD contains the P0 Python wrappers; compare against the immediate
        # pre-P0 Bash implementation for the differential guard contract.
        ["git", "show", "HEAD^:scripts/push-openclaw-config.sh"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    source = completed.stdout
    start = source.index("file_sha256() {")
    end = source.index("\nprepare_repo_config_preflight_copy() {", start)
    return source[start:end]


def _run_committed_bash_guard(
    function_name: str, arguments: list[str]
) -> subprocess.CompletedProcess[str]:
    command = (
        "set +e\n"
        f"{_committed_bash_guard_functions()}\n"
        f'{function_name} "$@"\n'
        "status=$?\n"
        "printf '__GUARD_STATUS__=%s\\n' \"$status\"\n"
    )
    environment = os.environ.copy()
    environment["PYTHON_BIN"] = str(REPO_ROOT / ".venv/bin/python")
    return subprocess.run(
        ["bash", "-c", command, "guard-differential", *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


@pytest.mark.parametrize(
    ("bash_function", "module_command"),
    [
        ("guarded_cp_file", "guarded-cp-file"),
        ("guarded_rm_rf", "guarded-rm-rf"),
        ("guarded_mv_replace", "guarded-mv-replace"),
    ],
)
def test_multi_fault_guard_order_matches_committed_bash(
    tmp_path: Path, bash_function: str, module_command: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target"
    target.write_text("target\n", encoding="utf-8")
    ancestor = tmp_path / "managed"
    ancestor.symlink_to(outside, target_is_directory=True)
    destination = ancestor / "nested" / "destination"
    source = tmp_path / "source"
    source.write_text("source\n", encoding="utf-8")
    context = "multi-fault guard order"

    if bash_function == "guarded_rm_rf":
        bash_arguments = [str(destination), context]
        module_arguments = [module_command, str(destination), context]
    else:
        bash_arguments = [str(source), str(destination), context]
        module_arguments = [module_command, str(source), str(destination), context]
    bash_result = _run_committed_bash_guard(bash_function, bash_arguments)
    module_result = _run_module(module_arguments)

    bash_stderr = bash_result.stderr
    assert bash_result.stdout.endswith("__GUARD_STATUS__=1\n")
    assert module_result.returncode == 1
    assert module_result.stdout == ""
    assert module_result.stderr == bash_stderr
    assert "chain contains symlink" in module_result.stderr
    assert str(ancestor) in module_result.stderr
