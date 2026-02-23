"""Azure operations module — wraps az cli commands via subprocess."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result returned by every Azure CLI operation."""

    success: bool
    stdout: str
    stderr: str
    return_code: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEFAULT_TEMPLATE: Path = Path("infra/main.bicep")


def _run_az(args: list[str], *, timeout: int = 600) -> CommandResult:
    """Execute an ``az`` CLI command and return a :class:`CommandResult`.

    Parameters
    ----------
    args:
        Arguments to pass **after** ``az`` (e.g. ``["deployment", "sub", "create", ...]``).
    timeout:
        Maximum seconds to wait before killing the process.
    """
    completed = subprocess.run(
        ["az", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        success=completed.returncode == 0,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
        return_code=completed.returncode,
    )


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def check_prerequisites() -> CommandResult:
    """Verify that the ``az`` CLI and Bicep extension are installed.

    Returns a successful :class:`CommandResult` when both are available, or a
    failure result describing what is missing.
    """
    if shutil.which("az") is None:
        return CommandResult(
            success=False,
            stdout="",
            stderr="Azure CLI (az) is not installed or not on PATH.",
            return_code=1,
        )

    bicep_check = _run_az(["bicep", "version"])
    if not bicep_check.success:
        return CommandResult(
            success=False,
            stdout="",
            stderr="Bicep CLI is not installed. Run: az bicep install",
            return_code=1,
        )

    return CommandResult(
        success=True,
        stdout=f"az CLI found. {bicep_check.stdout}",
        stderr="",
        return_code=0,
    )


def run_deployment(
    subscription_id: str,
    location: str,
    param_file: str,
    *,
    what_if: bool = False,
) -> CommandResult:
    """Create (or what-if) a subscription-scoped deployment.

    Parameters
    ----------
    subscription_id:
        Azure subscription ID.
    location:
        Azure region, e.g. ``"eastus"``.
    param_file:
        Path to the ``.bicepparam`` file.
    what_if:
        If ``True`` run a what-if analysis instead of a real deployment.
    """
    args = [
        "deployment",
        "sub",
        "create",
        "--subscription",
        subscription_id,
        "--location",
        location,
        "--template-file",
        str(_DEFAULT_TEMPLATE),
        "--parameters",
        param_file,
    ]

    if what_if:
        args.append("--what-if")

    return _run_az(args)


def validate_template(
    subscription_id: str,
    location: str,
    param_file: str,
) -> CommandResult:
    """Validate Bicep templates without deploying.

    Parameters
    ----------
    subscription_id:
        Azure subscription ID.
    location:
        Azure region.
    param_file:
        Path to the ``.bicepparam`` file.
    """
    return _run_az(
        [
            "deployment",
            "sub",
            "validate",
            "--subscription",
            subscription_id,
            "--location",
            location,
            "--template-file",
            str(_DEFAULT_TEMPLATE),
            "--parameters",
            param_file,
        ]
    )


def delete_resource_group(
    subscription_id: str,
    resource_group_name: str,
) -> CommandResult:
    """Delete an Azure resource group.

    Parameters
    ----------
    subscription_id:
        Azure subscription ID.
    resource_group_name:
        Name of the resource group to delete.
    """
    return _run_az(
        [
            "group",
            "delete",
            "--subscription",
            subscription_id,
            "--name",
            resource_group_name,
            "--yes",
            "--no-wait",
        ]
    )


def lint_bicep(template_path: str | None = None) -> CommandResult:
    """Run the Bicep linter (``az bicep build``) on a template.

    Parameters
    ----------
    template_path:
        Path to the ``.bicep`` file to lint.  Defaults to ``infra/main.bicep``.
    """
    path = template_path or str(_DEFAULT_TEMPLATE)
    return _run_az(["bicep", "build", "--file", path])
