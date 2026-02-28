"""
haniel CLI - Command-line interface for the service runner.

Commands:
    install  - Set up the execution environment
    run      - Start services and poll loop
    status   - Show current service status
    validate - Check configuration validity
"""

import sys
from pathlib import Path

import click

from haniel import __version__


def validate_config_file(ctx: click.Context, param: click.Parameter, value: str | None) -> Path | None:
    """Validate that a config file exists and return its Path."""
    if value is None:
        return None

    path = Path(value)
    if not path.exists():
        raise click.BadParameter(f"Config file not found: {value}")
    return path


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx: click.Context, version: bool) -> None:
    """haniel - Configuration-based, intentionally ignorant service runner.

    haniel doesn't know what it runs. It checks git repos, pulls changes,
    and starts processes as specified in the config file.
    """
    if version:
        click.echo(f"haniel {__version__}")
        return

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option("--dry-run", is_flag=True, help="Show what would be done without executing.")
def install(config: Path | None, dry_run: bool) -> None:
    """Set up the execution environment from a configuration file.

    This command:
    1. Validates system requirements (Python, Node, etc.)
    2. Creates required directories
    3. Clones git repositories
    4. Sets up virtual environments
    5. Guides you through interactive configuration via Claude Code
    """
    if config is None:
        click.echo(click.get_current_context().get_help())
        return

    if dry_run:
        click.echo(f"[dry-run] Would install from: {config}")
        click.echo("[dry-run] Phase 1: Mechanical installation")
        click.echo("[dry-run] Phase 2: Interactive setup (Claude Code)")
        click.echo("[dry-run] Phase 3: Finalization")
        return

    click.echo(f"Installing from: {config}")
    click.echo("Installation complete (skeleton)")


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize).")
def run(config: Path | None, foreground: bool) -> None:
    """Start services and begin the poll loop.

    This command:
    1. Loads the configuration
    2. Starts all enabled services in order
    3. Enters the poll loop (git fetch, restart on changes)
    """
    if config is None:
        click.echo(click.get_current_context().get_help())
        return

    click.echo(f"Running with config: {config}")
    if foreground:
        click.echo("Running in foreground mode")
    click.echo("Run complete (skeleton)")


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option("--json", "as_json", is_flag=True, help="Output status as JSON.")
def status(config: Path | None, as_json: bool) -> None:
    """Show current service and repository status.

    Displays:
    - Service status (running/stopped/crashed)
    - Repository status (HEAD, last fetch time)
    - MCP server status (if enabled)
    """
    if as_json:
        click.echo('{"status": "not_running", "services": [], "repos": []}')
        return

    click.echo("haniel status")
    click.echo("Status: Not running")
    if config:
        click.echo(f"Config: {config}")


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
def validate(config: Path | None) -> None:
    """Validate configuration file.

    Checks:
    - YAML syntax
    - Required fields
    - Circular dependencies (after fields)
    - Port conflicts (ready: port:*)
    - Duplicate repository paths
    """
    if config is None:
        click.echo(click.get_current_context().get_help())
        return

    click.echo(f"Validating: {config}")
    # Skeleton: just check if file exists and is valid YAML
    try:
        import yaml
        with open(config) as f:
            yaml.safe_load(f)
        click.echo("Configuration is valid (basic check only)")
        click.echo("OK")
    except yaml.YAMLError as e:
        click.echo(f"YAML error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
