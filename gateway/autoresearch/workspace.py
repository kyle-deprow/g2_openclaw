"""Workspace validation helpers for autoresearch artifacts."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch import constants
from gateway.autoresearch.constants import (
    DEFAULT_ALLOWED_TARGET_STATUS_LINES as DEFAULT_ALLOWED_TARGET_STATUS_LINES,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.gitops import (
    _path_under_root as _path_under_root,
)
from gateway.autoresearch.gitops import (
    _render_literal as _render_literal,
)
from gateway.autoresearch.gitops import (
    _require_artifact_origin_matches_target as _require_artifact_origin_matches_target,
)
from gateway.autoresearch.gitops import (
    _require_clean_git_worktree as _require_clean_git_worktree,
)
from gateway.autoresearch.gitops import (
    _require_git_output as _require_git_output,
)
from gateway.autoresearch.gitops import (
    _require_git_success as _require_git_success,
)
from gateway.autoresearch.gitops import (
    _require_git_worktree_root as _require_git_worktree_root,
)
from gateway.autoresearch.gitops import (
    _require_isolated_git_clone_root as _require_isolated_git_clone_root,
)
from gateway.autoresearch.gitops import (
    _resolve_git_commit as _resolve_git_commit,
)
from gateway.autoresearch.gitops import (
    _run_git as _run_git,
)
from gateway.autoresearch.secure_io import (
    _require_private_directory as _require_private_directory,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)

if TYPE_CHECKING:
    from gateway.autoresearch.policy import AutoresearchPolicy as AutoresearchPolicy


def validate_target_worktree_clean(
    status_lines: Sequence[str],
    *,
    allowed_status_lines: Sequence[str] = DEFAULT_ALLOWED_TARGET_STATUS_LINES,
) -> None:
    """Fail if the target repo has unapproved dirty files.

    The autoresearch loop may choose any strategy, but each stage must start
    from an uncontaminated target repo. Known persistent local docs can be
    allowlisted explicitly; crash residue and late writer output cannot.
    """
    allowed = set(allowed_status_lines)
    unexpected = tuple(line for line in status_lines if line and line not in allowed)
    if unexpected:
        details = "\n".join(f"- {line}" for line in unexpected)
        raise AutoresearchValidationError(
            "target repo worktree is dirty with unapproved changes:\n"
            f"{details}\n"
            "Stop stale writers and clean or commit the target repo before "
            "launching the next autoresearch stage."
        )


def _require_ancestor(
    worktree: Path,
    ancestor: str,
    descendant: str,
    *,
    error_message: str,
    missing_is_not_ancestor: bool = False,
) -> None:
    result = _run_git(
        worktree,
        ("merge-base", "--is-ancestor", ancestor, descendant),
        operation="ancestry check",
    )
    if result.returncode == 1 or (missing_is_not_ancestor and result.returncode != 0):
        raise AutoresearchValidationError(error_message)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(worktree))}"
        )


def _common_git_base(worktree: Path, first: str, second: str, *, label: str) -> str:
    result = _run_git(
        worktree,
        ("merge-base", first, second),
        operation=f"common ancestry check for {label}",
    )
    base = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{7,64}", base) is None:
        raise AutoresearchValidationError(f"Git common ancestry check failed for {label}")
    _require_ancestor(
        worktree,
        base,
        first,
        error_message=f"readiness base is not an ancestor of {label} runtime commit",
    )
    _require_ancestor(
        worktree,
        base,
        second,
        error_message=f"readiness base is not an ancestor of {label} implementation commit",
    )
    return base


def state_has_legacy_autoresearch_workspace(state: AutoresearchState) -> bool:
    """Return whether persisted state still points at the retired linked-worktree root."""
    workspaces: list[str] = []
    if state.implementation_result is not None:
        workspaces.append(state.implementation_result.workspace_path)
    workspaces.extend(fix.workspace_path for fix in state.fix_history)
    for value in workspaces:
        try:
            path = Path(value).expanduser().resolve(strict=False)
        except RuntimeError:
            return True
        if _path_under_root(path, constants.LEGACY_AUTORESEARCH_WORKTREE_ROOT):
            return True
    return False


def _rewrite_workspace_prefix(value: str, *, old_root: Path, new_root: Path) -> str:
    path = Path(value).expanduser().resolve(strict=False)
    try:
        relative = path.relative_to(old_root)
    except ValueError as exc:
        raise AutoresearchValidationError(
            "legacy workspace migration can rewrite only paths under the retired worktree root"
        ) from exc
    return str(new_root / relative)


def _clone_legacy_workspace_for_state(
    *,
    legacy_workspace: Path,
    authoritative_checkout: Path,
    destination: Path,
    commit_sha: str,
) -> None:
    def validate_destination() -> None:
        workspace = _require_isolated_git_clone_root(
            destination,
            label="legacy workspace migration destination",
        )
        _require_artifact_origin_matches_target(
            workspace,
            authoritative_checkout,
            label="legacy workspace migration destination",
        )
        head = _resolve_git_commit(workspace, "HEAD", label="migrated workspace HEAD")
        if head != _resolve_git_commit(
            workspace,
            commit_sha,
            label="migrated workspace commit_sha",
        ):
            raise AutoresearchValidationError(
                "legacy workspace migration destination exists with a different HEAD"
            )
        authoritative_head = _resolve_git_commit(
            authoritative_checkout,
            "HEAD",
            label="authoritative target_repo HEAD",
        )
        _require_ancestor(
            workspace,
            authoritative_head,
            head,
            error_message=(
                "authoritative target_repo HEAD is not an ancestor of migrated workspace commit"
            ),
            missing_is_not_ancestor=True,
        )
        _require_clean_git_worktree(workspace)

    if destination.exists():
        validate_destination()
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    _require_private_directory(destination.parent, label="legacy workspace migration root")
    _require_git_success(
        authoritative_checkout.parent,
        (
            "clone",
            "--no-hardlinks",
            "--no-local",
            str(authoritative_checkout),
            str(destination),
        ),
        operation="clone legacy autoresearch workspace",
    )
    destination.chmod(0o700)
    (destination / ".git").chmod(0o700)
    _require_artifact_origin_matches_target(
        destination,
        authoritative_checkout,
        label="legacy workspace migration destination",
    )
    _require_git_success(
        destination,
        ("fetch", "origin", commit_sha),
        operation="fetch migrated autoresearch workspace commit",
    )
    _require_git_success(
        destination,
        ("checkout", "--detach", commit_sha),
        operation="checkout migrated autoresearch workspace commit",
    )
    _remove_group_world_write_bits(destination)
    destination.chmod(0o700)
    (destination / ".git").chmod(0o700)
    validate_destination()


def _remove_group_world_write_bits(path: Path) -> None:
    for current_root, directory_names, file_names in os.walk(path):
        root_path = Path(current_root)
        if root_path.name == ".git":
            directory_names[:] = []
            continue
        root_mode = stat.S_IMODE(root_path.lstat().st_mode)
        root_path.chmod(root_mode & ~0o022)
        for file_name in file_names:
            file_path = root_path / file_name
            file_metadata = file_path.lstat()
            if stat.S_ISLNK(file_metadata.st_mode):
                continue
            file_mode = stat.S_IMODE(file_metadata.st_mode)
            file_path.chmod(file_mode & ~0o022)


def _require_legacy_linked_worktree_from_authoritative_checkout(
    legacy_workspace: Path,
    authoritative_checkout: Path,
) -> None:
    git_file = legacy_workspace / ".git"
    try:
        git_metadata = git_file.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            "legacy workspace migration requires linked worktree .git metadata"
        ) from exc
    if stat.S_ISLNK(git_metadata.st_mode) or not stat.S_ISREG(git_metadata.st_mode):
        raise AutoresearchValidationError(
            "legacy workspace migration requires source linked worktree metadata"
        )
    common_dir = Path(
        _require_git_output(
            legacy_workspace,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            operation="legacy linked worktree common git dir check",
        )
    ).resolve(strict=True)
    authoritative_git_dir = Path(
        _require_git_output(
            authoritative_checkout,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            operation="authoritative target_repo common git dir check",
        )
    ).resolve(strict=True)
    if common_dir != authoritative_git_dir:
        raise AutoresearchValidationError(
            "legacy workspace migration source must share authoritative target_repo Git metadata"
        )


def migrate_legacy_autoresearch_workspace_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Move a pre-isolated-clone active state onto the controller-owned workspace root.

    This is intentionally not exposed as a model artifact transition. It exists so a
    live state carrying the retired linked-worktree path can resume without allowing
    another model write to that legacy workspace.
    """
    from gateway.autoresearch import persistence as persistence_module
    from gateway.autoresearch import transitions as transitions_module

    resolved_state_path = state_path.expanduser().resolve(strict=False)
    with persistence_module._exclusive_state_lock(resolved_state_path):
        state = persistence_module.load_state_file(resolved_state_path)
        if not state_has_legacy_autoresearch_workspace(state):
            return state
        implementation = state.implementation_result
        if implementation is None:
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation_result"
            )
        old_root = constants.LEGACY_AUTORESEARCH_WORKTREE_ROOT.resolve(strict=False)
        old_workspace = Path(implementation.workspace_path).expanduser().resolve(strict=True)
        if not _path_under_root(old_workspace, old_root):
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation_result under retired root"
            )
        legacy_root_metadata = old_root.lstat()
        if old_root.is_symlink() or not stat.S_ISDIR(legacy_root_metadata.st_mode):
            raise AutoresearchValidationError("legacy workspace root must be a plain directory")
        old_workspace = _require_git_worktree_root(
            old_workspace,
            label="legacy implementation_result workspace_path",
        )
        if state.setup is None:
            raise AutoresearchValidationError(
                "legacy workspace migration requires setup target_repo"
            )
        authoritative_checkout = _require_git_worktree_root(
            Path(state.setup.target_repo).expanduser(),
            label="authoritative target_repo",
        )
        _require_legacy_linked_worktree_from_authoritative_checkout(
            old_workspace,
            authoritative_checkout,
        )
        _require_clean_git_worktree(old_workspace)
        implementation_commit = _resolve_git_commit(
            old_workspace,
            implementation.commit_sha,
            label="legacy implementation_result commit_sha",
        )
        _resolve_git_commit(
            authoritative_checkout,
            implementation_commit,
            label="authoritative target_repo object database implementation commit",
        )
        if _resolve_git_commit(old_workspace, "HEAD", label="legacy workspace HEAD") != (
            implementation_commit
        ):
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation commit at HEAD"
            )
        new_root = constants.DEFAULT_AUTORESEARCH_WORKTREE_ROOT.resolve(strict=False)
        new_workspace = new_root / old_workspace.relative_to(old_root)
        _clone_legacy_workspace_for_state(
            legacy_workspace=old_workspace,
            authoritative_checkout=authoritative_checkout,
            destination=new_workspace,
            commit_sha=implementation_commit,
        )
        migrated_implementation = replace(
            implementation,
            workspace_path=str(new_workspace),
            experiment_manifest_path=_rewrite_workspace_prefix(
                implementation.experiment_manifest_path,
                old_root=old_root,
                new_root=new_root,
            ),
        )
        migrated_fixes = tuple(
            replace(
                fix,
                workspace_path=_rewrite_workspace_prefix(
                    fix.workspace_path,
                    old_root=old_root,
                    new_root=new_root,
                ),
            )
            for fix in state.fix_history
        )
        migrated = replace(
            state,
            implementation_result=migrated_implementation,
            fix_history=migrated_fixes,
        )
        transitions_module.validate_artifact_workspace(
            replace(migrated, implementation_result=None, fix_history=()),
            migrated_implementation,
        )
        for fix in migrated.fix_history:
            transitions_module.validate_artifact_workspace(migrated, fix)
        transitions_module._validate_state(migrated, policy, validation_context)
        persistence_module._atomic_save_state_file(resolved_state_path, migrated)
        return migrated
