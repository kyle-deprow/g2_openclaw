"""Workspace validation helpers for autoresearch artifacts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from gateway.autoresearch.constants import (
    DEFAULT_ALLOWED_TARGET_STATUS_LINES as DEFAULT_ALLOWED_TARGET_STATUS_LINES,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.gitops import (
    _render_literal as _render_literal,
)
from gateway.autoresearch.gitops import (
    _run_git as _run_git,
)


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
