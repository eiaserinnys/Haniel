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
from pydantic import ValidationError as PydanticValidationError

from haniel import __version__
from haniel.config import load_config, HanielConfig
from haniel.validators import validate_config, ValidationError


def validate_config_file(ctx: click.Context, param: click.Parameter, value: str | None) -> Path | None:
    """Validate that a config file exists and return its Path."""
    if value is None:
        return None

    path = Path(value)
    if not path.exists():
        raise click.BadParameter(f"Config file not found: {value}")
    return path


def load_and_validate(config_path: Path) -> tuple[HanielConfig | None, list[str]]:
    """Load and validate a config file.

    Returns:
        Tuple of (config, errors). If config is None, errors contains schema errors.
    """
    errors: list[str] = []

    try:
        config = load_config(config_path)
    except PydanticValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"Schema error at {loc}: {err['msg']}")
        return None, errors
    except Exception as e:
        errors.append(f"Failed to load config: {e}")
        return None, errors

    # Run semantic validation
    validation_errors = validate_config(config)
    for err in validation_errors:
        errors.append(str(err))

    return config, errors


def print_dry_run_install(config: HanielConfig) -> None:
    """Print what install would do without executing."""
    click.echo(click.style("[dry-run] Phase 1: Mechanical installation", bold=True))

    # Requirements
    if config.install and config.install.requirements:
        click.echo("  - Requirements check:")
        for req, version in config.install.requirements.items():
            click.echo(f"      {req}: {version}")

    # Directories
    if config.install and config.install.directories:
        click.echo("  - Directories to create:")
        for d in config.install.directories:
            click.echo(f"      {d}")

    # Repos
    if config.repos:
        click.echo("  - Repositories to clone:")
        for name, repo in config.repos.items():
            click.echo(f"      {name} -> {repo.path}")

    # Environments
    if config.install and config.install.environments:
        click.echo("  - Environments to set up:")
        for name, env in config.install.environments.items():
            click.echo(f"      {name} ({env.type})")

    # Static configs
    if config.install and config.install.configs:
        static_configs = {k: v for k, v in config.install.configs.items() if v.content}
        if static_configs:
            click.echo("  - Config files (static):")
            for name, cfg in static_configs.items():
                click.echo(f"      {name} -> {cfg.path}")

    click.echo()
    click.echo(click.style("[dry-run] Phase 2: Interactive setup (Claude Code)", bold=True))

    # Interactive configs
    if config.install and config.install.configs:
        interactive_configs = {k: v for k, v in config.install.configs.items() if v.keys}
        if interactive_configs:
            click.echo("  - Config files (interactive):")
            for name, cfg in interactive_configs.items():
                click.echo(f"      {name} -> {cfg.path}")
                if cfg.keys:
                    missing = [k.key for k in cfg.keys if not k.default]
                    defaults = [k.key for k in cfg.keys if k.default]
                    if missing:
                        click.echo(f"        - Collect: {', '.join(missing)}")
                    if defaults:
                        click.echo(f"        - Defaults: {', '.join(defaults)}")

    click.echo()
    click.echo(click.style("[dry-run] Phase 3: Finalization", bold=True))

    if config.install and config.install.service:
        click.echo(f"  - Register service: {config.install.service.name}")
        if config.install.service.display:
            click.echo(f"      Display name: {config.install.service.display}")


def print_dry_run_run(config: HanielConfig) -> None:
    """Print what run would do without executing."""
    click.echo(click.style("[dry-run] Service startup plan", bold=True))
    click.echo(f"  Poll interval: {config.poll_interval}s")
    click.echo()

    if config.repos:
        click.echo("  Repositories to monitor:")
        for name, repo in config.repos.items():
            click.echo(f"    - {name}: {repo.branch} @ {repo.path}")
        click.echo()

    if config.services:
        click.echo("  Services to start (in order):")
        # Sort by dependencies (simple topological hint)
        started: set[str] = set()
        pending = list(config.services.keys())

        while pending:
            for name in pending[:]:
                service = config.services[name]
                deps = set(service.after)
                if deps <= started:
                    after_str = f" (after: {', '.join(service.after)})" if service.after else ""
                    ready_str = f" [ready: {service.ready}]" if service.ready else ""
                    enabled_str = "" if service.enabled else " (DISABLED)"
                    click.echo(f"    - {name}{after_str}{ready_str}{enabled_str}")
                    click.echo(f"        {service.run}")
                    started.add(name)
                    pending.remove(name)
                    break
            else:
                # Remaining have unmet deps (possibly circular)
                for name in pending:
                    click.echo(f"    - {name} (UNMET DEPENDENCIES)")
                break


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

    # Load and validate
    haniel_config, errors = load_and_validate(config)
    if errors:
        click.echo(click.style("Configuration errors:", fg="red", bold=True), err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"[dry-run] Configuration: {config}")
        click.echo()
        print_dry_run_install(haniel_config)
        return

    click.echo(f"Installing from: {config}")
    click.echo("Installation complete (skeleton)")


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize).")
@click.option("--dry-run", is_flag=True, help="Show what would be done without executing.")
def run(config: Path | None, foreground: bool, dry_run: bool) -> None:
    """Start services and begin the poll loop.

    This command:
    1. Loads the configuration
    2. Starts all enabled services in order
    3. Enters the poll loop (git fetch, restart on changes)
    """
    if config is None:
        click.echo(click.get_current_context().get_help())
        return

    # Load and validate
    haniel_config, errors = load_and_validate(config)
    if errors:
        click.echo(click.style("Configuration errors:", fg="red", bold=True), err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"[dry-run] Configuration: {config}")
        click.echo()
        print_dry_run_run(haniel_config)
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
    - Schema compliance (required fields, types)
    - Circular dependencies (after fields)
    - Port conflicts (ready: port:*)
    - Duplicate repository paths
    - Missing references (non-existent services/repos)
    """
    if config is None:
        click.echo(click.get_current_context().get_help())
        return

    click.echo(f"Validating: {config}")
    click.echo()

    haniel_config, errors = load_and_validate(config)

    if errors:
        click.echo(click.style("Validation FAILED", fg="red", bold=True))
        click.echo()
        for error in errors:
            click.echo(f"  {click.style('✗', fg='red')} {error}")
        sys.exit(1)

    # Print summary
    click.echo(click.style("Validation passed!", fg="green", bold=True))
    click.echo()
    click.echo("Configuration summary:")
    click.echo(f"  - Poll interval: {haniel_config.poll_interval}s")
    click.echo(f"  - Repositories: {len(haniel_config.repos)}")
    click.echo(f"  - Services: {len(haniel_config.services)}")

    if haniel_config.webhooks:
        click.echo(f"  - Webhooks: {len(haniel_config.webhooks)}")

    if haniel_config.mcp:
        status = "enabled" if haniel_config.mcp.enabled else "disabled"
        click.echo(f"  - MCP server: {status}")
        if haniel_config.mcp.enabled:
            click.echo(f"      Transport: {haniel_config.mcp.transport}")
            click.echo(f"      Port: {haniel_config.mcp.port}")

    if haniel_config.install:
        click.echo("  - Install configuration: present")

    click.echo()
    click.echo("OK")


if __name__ == "__main__":
    main()
