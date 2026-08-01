"""Git worktree validation helpers for autoresearch."""

from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse

from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.secure_io import (
    _require_private_directory as _require_private_directory,
)


def _render_literal(value: str) -> str:
    """Render untrusted prompt/error values as a single JSON string literal."""
    return json.dumps(value, ensure_ascii=True)


def _run_git(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=False,
            capture_output=True,
            cwd=working_directory,
            text=True,
        )
    except (OSError, RuntimeError) as exc:
        raise AutoresearchValidationError(
            f"Git {operation} could not run in {_render_literal(str(working_directory))}"
        ) from exc
    return result


def _require_git_output(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> str:
    result = _run_git(working_directory, arguments, operation=operation)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git {operation} failed in {_render_literal(str(working_directory))}"
        )
    return result.stdout.strip()


def _require_git_worktree_root(path: Path, *, label: str) -> Path:
    if not path.is_dir():
        raise AutoresearchValidationError(f"{label} {_render_literal(str(path))} does not exist")
    resolved_path = path.resolve()
    top_level = Path(
        _require_git_output(
            resolved_path,
            ("rev-parse", "--show-toplevel"),
            operation=f"worktree check for {label}",
        )
    ).resolve()
    if top_level != resolved_path:
        raise AutoresearchValidationError(
            f"{label} {_render_literal(str(path))} must be the root of a Git worktree"
        )
    return resolved_path


def _require_isolated_git_clone_root(path: Path, *, label: str) -> Path:
    root = _require_git_worktree_root(path, label=label)
    git_metadata = root / ".git"
    try:
        metadata = git_metadata.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} must contain private .git directory metadata"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AutoresearchValidationError(
            f"{label} must be an isolated clone with a private .git directory"
        )
    _require_private_directory(git_metadata, label=f"{label} .git metadata")
    return root


def _require_artifact_origin_matches_target(
    workspace: Path,
    target_checkout: Path,
    *,
    label: str,
) -> None:
    result = _run_git(
        workspace,
        ("config", "--get", "remote.origin.url"),
        operation=f"origin check for {label}",
    )
    origin = result.stdout.strip()
    if result.returncode != 0 or not origin:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(workspace))}"
        )
    parsed = urlparse(origin)
    if parsed.scheme and parsed.scheme != "file":
        raise AutoresearchValidationError(
            f"{label} remote.origin.url must be the authoritative local target_repo"
        )
    origin_path = Path(unquote(parsed.path if parsed.scheme == "file" else origin)).expanduser()
    try:
        resolved_origin = origin_path.resolve(strict=True)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} remote.origin.url does not resolve to authoritative target_repo"
        ) from exc
    if resolved_origin != target_checkout:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(workspace))}"
        )


def _require_strict_canonical_workspace_path(value: str, *, label: str) -> Path:
    declared_path = Path(value).expanduser()
    try:
        resolved_path = declared_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AutoresearchValidationError(
            f"{label} {_render_literal(value)} does not exist or is not a directory"
        ) from exc
    if not resolved_path.is_dir():
        raise AutoresearchValidationError(
            f"{label} {_render_literal(value)} does not exist or is not a directory"
        )
    if value != str(resolved_path):
        raise AutoresearchValidationError(f"{label} must be its strict canonical resolved path")
    return resolved_path


def _path_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_workspace_under_autoresearch_worktree_root(
    workspace: Path,
    *,
    label: str,
    worktree_root: Path,
) -> None:
    try:
        workspace.relative_to(worktree_root)
    except ValueError as exc:
        raise AutoresearchValidationError(
            f"{label} must be under the canonical autoresearch worktree root"
        ) from exc


def _resolve_git_commit(worktree: Path, commit_sha: str, *, label: str) -> str:
    result = _run_git(
        worktree,
        ("rev-parse", "--verify", f"{commit_sha}^{{commit}}"),
        operation=f"commit lookup for {label}",
    )
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"{label} {_render_literal(commit_sha)} does not exist in the artifact worktree"
        )
    return result.stdout.strip()


def _require_git_descends_from(
    worktree: Path,
    ancestor: str,
    descendant: str,
    *,
    label: str,
) -> None:
    result = _run_git(
        worktree,
        ("merge-base", "--is-ancestor", ancestor, descendant),
        operation=f"readiness ancestry check for {label}",
    )
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"{label} must descend from the exact readiness-pinned Quantipy commit"
        )


def _require_git_success(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> None:
    result = _run_git(working_directory, arguments, operation=operation)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git {operation} failed in {_render_literal(str(working_directory))}"
        )


def _require_clean_git_worktree(worktree: Path) -> None:
    status = _require_git_output(
        worktree,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        operation="status check",
    )
    if status:
        raise AutoresearchValidationError(
            f"artifact worktree {_render_literal(str(worktree))} must be clean"
        )
