"""Rich console utilities for the Azure Infrastructure CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.status import Status

if TYPE_CHECKING:
    from infra.azure_ops import CommandResult

console = Console()


def print_success(message: str) -> None:
    """Print a green success message with a checkmark."""
    console.print(f"[bold green]✔[/bold green] {message}")


def print_error(message: str) -> None:
    """Print a red error message with an X."""
    console.print(f"[bold red]✘[/bold red] {message}")


def print_warning(message: str) -> None:
    """Print a yellow warning message."""
    console.print(f"[bold yellow]⚠[/bold yellow] {message}")


def print_step(message: str) -> None:
    """Print a blue step indicator."""
    console.print(f"[bold blue]→[/bold blue] {message}")


def create_status(message: str) -> Status:
    """Return a Rich Status context manager for long-running operations.

    Usage::

        with create_status("Deploying..."):
            run_long_operation()
    """
    return console.status(f"[bold cyan]{message}[/bold cyan]", spinner="dots")


def print_deployment_result(result: CommandResult) -> None:
    """Display a deployment result in a formatted Rich panel."""
    if result.success:
        title = "[bold green]Deployment Succeeded[/bold green]"
        border_style = "green"
    else:
        title = "[bold red]Deployment Failed[/bold red]"
        border_style = "red"

    body_parts: list[str] = []

    if result.stdout:
        body_parts.append(result.stdout)

    if result.stderr:
        label = "Warnings" if result.success else "Errors"
        body_parts.append(f"[dim]── {label} ──[/dim]\n{result.stderr}")

    body = "\n\n".join(body_parts) if body_parts else "[dim]No output[/dim]"

    console.print(Panel(body, title=title, border_style=border_style, expand=False))


def confirm_action(message: str) -> bool:
    """Prompt the user for confirmation using Rich."""
    return Confirm.ask(f"[bold yellow]{message}[/bold yellow]")
