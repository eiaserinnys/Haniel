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
import os
import re
import signal
import sys
from pathlib import Path
from uuid import uuid4

import click

from haniel import __version__, EXIT_SELF_UPDATE, EXIT_RESTART
from haniel.commands.common import (
    input_issues,
    load_and_validate,
    validate_config_file,
)
from haniel.config.input_checks import log_config_issues
from haniel.commands.handover import handover_command
from haniel.commands.install import install_command
from haniel.commands.install import print_dry_run_install as print_dry_run_install
from haniel.commands.lifecycle import lifecycle_group
from haniel.commands.status import collect_status
from haniel.config import HanielConfig
from haniel.core.lifecycle_control import LifecycleControl
from haniel.core.lifecycle_locks import LifecycleConflict


ACTIVE_SELF_HEAD_ENV = "HANIEL_ACTIVE_SELF_HEAD"


def _normalize_active_self_head(value: str, *, source: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) is None:
        raise click.BadParameter(
            f"{source} must be a full hexadecimal commit ID",
            param_hint="active self head",
        )
    return value.lower()


def _validate_active_self_head(
    _context: click.Context,
    _parameter: click.Parameter,
    value: str | None,
) -> str | None:
    if value is None:
        return None
    return _normalize_active_self_head(value, source="--active-self-head")


def _resolve_active_self_head(option_value: str | None) -> str | None:
    environment_value = os.environ.get(ACTIVE_SELF_HEAD_ENV)
    option_head = (
        _normalize_active_self_head(option_value, source="--active-self-head")
        if option_value is not None
        else None
    )
    environment_head = (
        _normalize_active_self_head(environment_value, source=ACTIVE_SELF_HEAD_ENV)
        if environment_value is not None
        else None
    )
    if (
        option_head is not None
        and environment_head is not None
        and option_head != environment_head
    ):
        raise click.BadParameter(
            f"--active-self-head conflicts with {ACTIVE_SELF_HEAD_ENV}",
            param_hint="active self head",
        )
    return option_head or environment_head


def _exit_immediately(exit_code: int) -> None:
    """Flush user-visible streams, then bypass interpreter finalization."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


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
@click.option("--foreground", "-f", is_flag=True, hidden=True, expose_value=False)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without executing."
)
@click.option(
    "--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)."
)
@click.option(
    "--active-self-head",
    hidden=True,
    callback=_validate_active_self_head,
)
@click.option("--initial-request-id", hidden=True)
def run(
    config: Path | None,
    dry_run: bool,
    log_level: str,
    active_self_head: str | None,
    initial_request_id: str | None,
) -> None:
    """Start services and begin the poll loop.

    Without CONFIG, ./haniel.yaml is used.

    This command:
    1. Loads the configuration
    2. Starts all enabled services in order
    3. Enters the poll loop (git fetch, restart on changes)
    """
    from haniel.core.runner import ServiceRunner

    if config is None:
        config = Path("haniel.yaml")
        if not config.exists():
            click.echo(
                "No CONFIG given and ./haniel.yaml was not found. "
                "Usage: haniel run [CONFIG]",
                err=True,
            )
            sys.exit(2)

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

    lifecycle = LifecycleControl(config.resolve())
    instance_id = str(uuid4())
    try:
        owner = lifecycle.acquire_owner(instance_id)
    except LifecycleConflict as conflict:
        if conflict.code != "LIFECYCLE_OWNER_CONFLICT":
            raise
        pid = lifecycle.read_owner(optional=True).get("pid", "unknown")
        click.echo(
            f"haniel is already running with this config ({config.resolve()}, "
            f"pid {pid})",
            err=True,
        )
        sys.exit(1)

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger = logging.getLogger("haniel")
    log_config_issues(config, haniel_config)

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
    try:
        runner = ServiceRunner(
            config=haniel_config,
            config_dir=config_dir,
            config_path=config.resolve(),
            active_self_head=_resolve_active_self_head(active_self_head),
        )
    except BaseException:
        owner.__exit__(None, None, None)
        raise
    from haniel.core.lifecycle_request_server import LifecycleRequestServer

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
        while runner.is_running and not runner.self_update_requested:
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
        self_update_requested = runner.self_update_requested
        if self_update_requested:
            from haniel.core.self_update_exit_intent import write

            click.echo(
                click.style("Exiting for self-update (exit code 10).", fg="yellow")
            )
            write(config_dir)
        lifecycle_server.close()
        runner.stop()
        owner.__exit__(None, None, None)
        if self_update_requested:
            _exit_immediately(EXIT_SELF_UPDATE)
        if runner.restart_requested:
            click.echo(click.style("Exiting for restart (exit code 11).", fg="yellow"))
            sys.exit(EXIT_RESTART)
        click.echo("Shutdown complete.")


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option("--json", "as_json", is_flag=True, help="Output status as JSON.")
def status(config: Path | None, as_json: bool) -> None:
    """Show whether haniel is running and what its services are doing.

    Reads the running instance's owner record, then asks its dashboard API
    (needs mcp and dashboard enabled in CONFIG) for the real service status.
    When that is not possible it says why and lists services from the config.
    """
    if config is None:
        if as_json:
            click.echo(json.dumps({"running": None, "services": {}, "repos": {}}))
        else:
            click.echo("haniel status")
            click.echo("Status: unknown (no config specified; pass haniel.yaml)")
        return

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

    report = collect_status(config, haniel_config)

    if as_json:
        click.echo(json.dumps(report.to_json(haniel_config), indent=2))
        return

    click.echo(click.style("haniel status", bold=True))
    click.echo()

    if report.running:
        pid_str = f" (pid {report.pid})" if report.pid is not None else ""
        click.echo(f"Status: {click.style('Running', fg='green')}{pid_str}")
        if report.owner_note:
            click.echo(f"Note: {report.owner_note}")
    else:
        click.echo(f"Status: {click.style('Not running', fg='yellow')}")

    runtime = report.runtime
    if runtime is None:
        click.echo(f"Runtime status unavailable: {report.unavailable_reason}")
        click.echo()
        _echo_configured_status(haniel_config)
        return

    if runtime.get("start_time"):
        click.echo(f"Started: {runtime['start_time']}")
    if runtime.get("last_poll"):
        click.echo(f"Last poll: {runtime['last_poll']}")
    if runtime.get("poll_count"):
        click.echo(f"Poll count: {runtime['poll_count']}")

    click.echo(f"Poll interval: {runtime.get('poll_interval', 'N/A')}s")
    click.echo()

    # Services
    services = runtime.get("services", {})
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
    repos = runtime.get("repos", {})
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


def _echo_configured_status(haniel_config: HanielConfig) -> None:
    """List what the config declares; this is not runtime state."""
    if haniel_config.services:
        click.echo(click.style("Services (from config; not runtime state):", bold=True))
        for name, service in haniel_config.services.items():
            click.echo(f"  {name}: {'enabled' if service.enabled else 'disabled'}")
        click.echo()
    if haniel_config.repos:
        click.echo(click.style("Repositories (from config):", bold=True))
        for name, repo in haniel_config.repos.items():
            click.echo(f"  {name} ({repo.branch}): {repo.path}")


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option(
    "--no-plain-secrets", is_flag=True, help="Reject plain secrets in config."
)
def validate(config: Path | None, no_plain_secrets: bool) -> None:
    """Validate configuration file.

    Checks:
    - YAML syntax
    - Schema compliance (required fields, types)
    - Circular dependencies (after fields)
    - Port conflicts (ready: port:*)
    - Duplicate repository paths
    - Missing references (non-existent services/repos)
    - Unknown keys and unusable values (ready, signal, delays, empty url/token)
    """
    if config is None:
        click.echo(click.get_current_context().get_help())
        return

    click.echo(f"Validating: {config}")
    click.echo()

    haniel_config, errors = load_and_validate(config)
    warnings = []
    for issue in input_issues(config, haniel_config):
        (errors if issue.severity == "error" else warnings).append(str(issue))

    if not errors and no_plain_secrets:
        from .config.secrets import plain_secret_errors

        errors.extend(
            plain_secret_errors(haniel_config, config.read_text(encoding="utf-8"))
        )

    if errors:
        click.echo(click.style("Validation FAILED", fg="red", bold=True))
        click.echo()
        for error in errors:
            click.echo(f"  {click.style('✗', fg='red')} {error}")
        for warning in warnings:
            click.echo(f"  {click.style('!', fg='yellow')} {warning}")
        sys.exit(1)

    # Print summary
    click.echo(click.style("Validation passed!", fg="green", bold=True))
    click.echo()
    for warning in warnings:
        click.echo(f"  {click.style('!', fg='yellow')} {warning}")
    if warnings:
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
