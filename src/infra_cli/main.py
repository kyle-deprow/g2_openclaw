"""Azure Infrastructure CLI — deploy, validate, and manage Bicep templates."""

from __future__ import annotations

from typing import Annotated

import typer

from infra_cli import azure_ops
from infra_cli.console import (
    confirm_action,
    console,
    create_status,
    print_deployment_result,
    print_error,
    print_step,
    print_success,
    print_warning,
)

app = typer.Typer(
    name="azure-infra-cli",
    help="CLI tool for deploying Azure infrastructure via Bicep templates.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# Shared option types
# ---------------------------------------------------------------------------

SubscriptionOpt = Annotated[
    str,
    typer.Option(
        "--subscription-id", "-s", help="Azure subscription ID.", envvar="AZURE_SUBSCRIPTION_ID"
    ),
]
LocationOpt = Annotated[
    str,
    typer.Option("--location", "-l", help="Azure region for the deployment."),
]
ParamFileOpt = Annotated[
    str,
    typer.Option("--param-file", "-p", help="Path to the .bicepparam parameter file."),
]
ConfirmOpt = Annotated[
    bool,
    typer.Option("--confirm/--no-confirm", help="Prompt for confirmation before executing."),
]

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def deploy(
    subscription_id: SubscriptionOpt,
    location: LocationOpt = "eastus",
    param_file: ParamFileOpt = "infra/parameters/dev.bicepparam",
    confirm: ConfirmOpt = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run validate + what-if only, skip actual deployment."),
    ] = False,
) -> None:
    """Deploy the Azure infrastructure (subscription-scoped)."""
    # 1. Prerequisites
    print_step("Checking prerequisites…")
    prereq = azure_ops.check_prerequisites()
    if not prereq.success:
        print_error(prereq.stderr)
        raise typer.Exit(code=1)
    print_success("Prerequisites satisfied")

    # 2. Validate
    print_step("Validating Bicep templates…")
    with create_status("Validating…"):
        validation = azure_ops.validate_template(subscription_id, location, param_file)
    if not validation.success:
        print_error("Template validation failed")
        print_deployment_result(validation)
        raise typer.Exit(code=1)
    print_success("Template validation passed")

    # 3. What-if
    print_step("Running what-if analysis…")
    with create_status("Analysing changes…"):
        whatif_result = azure_ops.run_deployment(
            subscription_id, location, param_file, what_if=True
        )
    print_deployment_result(whatif_result)
    if not whatif_result.success:
        print_error("What-if analysis failed")
        raise typer.Exit(code=1)

    # 4. Stop here on dry run
    if dry_run:
        print_success("Dry run complete — no resources were modified")
        raise typer.Exit(code=0)

    # 5. Confirm
    if confirm and not confirm_action("Proceed with deployment?"):
        print_warning("Deployment cancelled by user")
        raise typer.Exit(code=0)

    # 6. Deploy
    print_step("Deploying infrastructure…")
    with create_status("Deploying — this may take several minutes…"):
        result = azure_ops.run_deployment(subscription_id, location, param_file)
    print_deployment_result(result)

    if result.success:
        print_success("Deployment completed successfully")
    else:
        print_error("Deployment failed")
        raise typer.Exit(code=1)


@app.command(name="what-if")
def what_if(
    subscription_id: SubscriptionOpt,
    location: LocationOpt = "eastus",
    param_file: ParamFileOpt = "infra/parameters/dev.bicepparam",
) -> None:
    """Run a what-if analysis to preview infrastructure changes."""
    prereq = azure_ops.check_prerequisites()
    if not prereq.success:
        print_error(prereq.stderr)
        raise typer.Exit(code=1)

    print_step("Running what-if analysis…")
    with create_status("Analysing changes…"):
        result = azure_ops.run_deployment(subscription_id, location, param_file, what_if=True)

    print_deployment_result(result)
    if not result.success:
        raise typer.Exit(code=1)


@app.command()
def destroy(
    subscription_id: SubscriptionOpt,
    resource_group: Annotated[
        str,
        typer.Option("--resource-group", "-g", help="Name of the resource group to delete."),
    ],
    confirm: ConfirmOpt = True,
) -> None:
    """Tear down infrastructure by deleting the resource group."""
    if confirm and not confirm_action(
        f"This will permanently delete resource group [bold]{resource_group}[/bold]. Continue?"
    ):
        print_warning("Destroy cancelled by user")
        raise typer.Exit(code=0)

    print_step(f"Deleting resource group [bold]{resource_group}[/bold]…")
    with create_status("Deleting…"):
        result = azure_ops.delete_resource_group(subscription_id, resource_group)

    if result.success:
        print_success(f"Resource group '{resource_group}' deletion initiated (--no-wait)")
    else:
        print_error(f"Failed to delete resource group '{resource_group}'")
        print_deployment_result(result)
        raise typer.Exit(code=1)


@app.command()
def validate(
    subscription_id: SubscriptionOpt,
    location: LocationOpt = "eastus",
    param_file: ParamFileOpt = "infra/parameters/dev.bicepparam",
) -> None:
    """Validate Bicep templates without deploying."""
    prereq = azure_ops.check_prerequisites()
    if not prereq.success:
        print_error(prereq.stderr)
        raise typer.Exit(code=1)

    print_step("Validating Bicep templates…")
    with create_status("Validating…"):
        result = azure_ops.validate_template(subscription_id, location, param_file)

    if result.success:
        print_success("Template validation passed")
        if result.stdout:
            console.print(result.stdout)
    else:
        print_error("Template validation failed")
        print_deployment_result(result)
        raise typer.Exit(code=1)


@app.command()
def lint(
    template_path: Annotated[
        str,
        typer.Option("--template-path", "-t", help="Path to the .bicep template to lint."),
    ] = "infra/main.bicep",
) -> None:
    """Lint Bicep templates using ``az bicep build``."""
    prereq = azure_ops.check_prerequisites()
    if not prereq.success:
        print_error(prereq.stderr)
        raise typer.Exit(code=1)

    print_step(f"Linting {template_path}…")
    with create_status("Linting…"):
        result = azure_ops.lint_bicep(template_path)

    if result.success:
        print_success("Bicep linting passed — no errors found")
    else:
        print_error("Bicep linting found issues")
        print_deployment_result(result)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
