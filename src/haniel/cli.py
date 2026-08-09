"""
haniel CLI - Command-line interface for the service runner.

Commands:
    install  - Set up the execution environment
    run      - Start services and poll loop
    status   - Show current service status
    validate - Check configuration validity
"""

import json
import logging
import signal
import sys
from pathlib import Path
from uuid import uuid4

import click

from haniel import __version__, EXIT_SELF_UPDATE, EXIT_RESTART
from haniel.commands.common import load_and_validate, validate_config_file
from haniel.commands.handover import handover_command
from haniel.commands.install import install_command
from haniel.commands.install import print_dry_run_install as print_dry_run_install
from haniel.commands.lifecycle import lifecycle_group
from haniel.config import HanielConfig


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
                    after_str = (
                        f" (after: {', '.join(service.after)})" if service.after else ""
                    )
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
    """haniel - Configuration-based, intentionally indifferent service runner.

    haniel doesn't care what it runs. It checks git repos, pulls changes,
    and starts processes as specified in the config file.
    """
    if version:
        click.echo(f"haniel {__version__}")
        return

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


main.add_command(handover_command)
main.add_command(install_command)
main.add_command(lifecycle_group)


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option(
    "--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize)."
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without executing."
)
@click.option(
    "--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)."
)
@click.option("--initial-request-id", hidden=True)
def run(
    config: Path | None,
    foreground: bool,
    dry_run: bool,
    log_level: str,
    initial_request_id: str | None,
) -> None:
    """Start services and begin the poll loop.

    This command:
    1. Loads the configuration
    2. Starts all enabled services in order
    3. Enters the poll loop (git fetch, restart on changes)
    """
    from haniel.core.runner import ServiceRunner

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

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("haniel")

    click.echo(f"Starting haniel with config: {config}")
    click.echo(f"Poll interval: {haniel_config.poll_interval}s")
    click.echo(f"Services: {len(haniel_config.services)}")
    click.echo(f"Repositories: {len(haniel_config.repos)}")
    click.echo()

    # Add file handler for haniel's own log
    config_dir = config.parent.resolve()
    log_file = config_dir / "logs" / "haniel.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.getLogger().addHandler(file_handler)

    # Create runner
    runner = ServiceRunner(
        config=haniel_config,
        config_dir=config_dir,
        config_path=config.resolve(),
    )
    from haniel.core.lifecycle_control import LifecycleControl
    from haniel.core.lifecycle_request_server import LifecycleRequestServer

    lifecycle = LifecycleControl(config.resolve())
    instance_id = str(uuid4())
    owner = lifecycle.acquire_owner(instance_id)
    runner.lifecycle_instance_id = instance_id
    runner.lifecycle_control = lifecycle
    lifecycle_server = LifecycleRequestServer(
        control=lifecycle,
        runner=runner,
        instance_id=instance_id,
    )

    # Signal handlers for graceful shutdown
    def handle_signal(signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        click.echo(f"\nReceived {sig_name}, shutting down...")
        runner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        if initial_request_id is not None:
            lifecycle_server.handle_request(initial_request_id)
            initial_terminal = lifecycle.read_result(initial_request_id).get("terminal")
            if not initial_terminal:
                raise RuntimeError(
                    "initial lifecycle request did not reach terminal state"
                )
            if not initial_terminal.get("ok") and not initial_terminal.get(
                "recovered", False
            ):
                error = initial_terminal.get("error") or {}
                raise RuntimeError(
                    str(error.get("message", "initial lifecycle request failed"))
                )

        # Print startup order
        startup_order = runner.get_startup_order()
        click.echo("Startup order:")
        for i, name in enumerate(startup_order, 1):
            svc = haniel_config.services[name]
            after_str = f" (after: {', '.join(svc.after)})" if svc.after else ""
            click.echo(f"  {i}. {name}{after_str}")
        click.echo()

        # Start the runner
        runner.start()
        lifecycle_server.start()
        click.echo(click.style("Services started. Entering poll loop.", fg="green"))
        click.echo("Press Ctrl+C to stop.")
        click.echo()

        # Keep main thread alive
        while runner.is_running:
            try:
                # Sleep in small intervals to allow signal handling
                import time

                time.sleep(1)
            except KeyboardInterrupt:
                break

    except Exception as e:
        logger.exception(f"Runner error: {e}")
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        lifecycle_server.close()
        runner.stop()
        owner.__exit__(None, None, None)
        if runner.self_update_requested:
            click.echo(
                click.style("Exiting for self-update (exit code 10).", fg="yellow")
            )
            sys.exit(EXIT_SELF_UPDATE)
        if runner.restart_requested:
            click.echo(click.style("Exiting for restart (exit code 11).", fg="yellow"))
            sys.exit(EXIT_RESTART)
        click.echo("Shutdown complete.")


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
    from haniel.core.runner import ServiceRunner

    # If no config provided, show basic status
    if config is None:
        if as_json:
            click.echo(json.dumps({"running": False, "services": {}, "repos": {}}))
        else:
            click.echo("haniel status")
            click.echo("Status: Not running (no config specified)")
        return

    # Load config to show configured services
    haniel_config, errors = load_and_validate(config)
    if errors:
        if as_json:
            click.echo(json.dumps({"error": "Invalid config", "errors": errors}))
        else:
            click.echo(
                click.style("Configuration errors:", fg="red", bold=True), err=True
            )
            for error in errors:
                click.echo(f"  - {error}", err=True)
        sys.exit(1)

    # Create a runner to get status info (not starting it)
    config_dir = config.parent.resolve()
    runner = ServiceRunner(
        config=haniel_config,
        config_dir=config_dir,
        config_path=config.resolve(),
    )

    status_data = runner.get_status()

    if as_json:
        click.echo(json.dumps(status_data, indent=2))
        return

    # Human-readable output
    click.echo(click.style("haniel status", bold=True))
    click.echo()

    running = status_data.get("running", False)
    status_str = (
        click.style("Running", fg="green")
        if running
        else click.style("Stopped", fg="yellow")
    )
    click.echo(f"Status: {status_str}")

    if status_data.get("start_time"):
        click.echo(f"Started: {status_data['start_time']}")
    if status_data.get("last_poll"):
        click.echo(f"Last poll: {status_data['last_poll']}")
    if status_data.get("poll_count"):
        click.echo(f"Poll count: {status_data['poll_count']}")

    click.echo(f"Poll interval: {status_data.get('poll_interval', 'N/A')}s")
    click.echo()

    # Services
    services = status_data.get("services", {})
    if services:
        click.echo(click.style("Services:", bold=True))
        for name, svc_status in services.items():
            state = svc_status.get("state", "unknown")

            # Color based on state
            if state == "running" or state == "ready":
                state_str = click.style(state, fg="green")
            elif state == "stopped":
                state_str = click.style(state, fg="yellow")
            elif state == "crashed" or state == "circuit_open":
                state_str = click.style(state, fg="red")
            else:
                state_str = click.style(state, fg="white")

            uptime = svc_status.get("uptime")
            uptime_str = f" (uptime: {int(uptime)}s)" if uptime else ""

            restarts = svc_status.get("restart_count", 0)
            restart_str = f" [restarts: {restarts}]" if restarts > 0 else ""

            click.echo(f"  {name}: {state_str}{uptime_str}{restart_str}")
        click.echo()

    # Repos
    repos = status_data.get("repos", {})
    if repos:
        click.echo(click.style("Repositories:", bold=True))
        for name, repo_status in repos.items():
            head = repo_status.get("last_head", "unknown")
            branch = repo_status.get("branch", "?")
            last_fetch = repo_status.get("last_fetch")
            error = repo_status.get("fetch_error")

            if error:
                status_str = click.style(f"ERROR: {error}", fg="red")
            elif last_fetch:
                status_str = f"HEAD: {head} (fetched: {last_fetch})"
            else:
                status_str = f"HEAD: {head or 'N/A'}"

            click.echo(f"  {name} ({branch}): {status_str}")


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
