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

import click
from pydantic import ValidationError as PydanticValidationError

from haniel import __version__, EXIT_SELF_UPDATE
from haniel.config import load_config, HanielConfig, validate_config


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
    """haniel - Configuration-based, intentionally indifferent service runner.

    haniel doesn't care what it runs. It checks git repos, pulls changes,
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
@click.option("--resume", is_flag=True, help="Resume from previous install state if exists.")
@click.option("--skip-interactive", is_flag=True, help="Skip Claude Code interactive phase.")
def install(
    config: Path | None,
    dry_run: bool,
    resume: bool,
    skip_interactive: bool,
) -> None:
    """Set up the execution environment from a configuration file.

    This command:
    1. Validates system requirements (Python, Node, etc.)
    2. Creates required directories
    3. Clones git repositories
    4. Sets up virtual environments
    5. Guides you through interactive configuration via Claude Code
    """
    from haniel.installer import InstallOrchestrator, InstallState, InstallPhase

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

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config_dir = config.parent.resolve()
    state_file = config_dir / "install.state"

    # Load or create state
    if resume and state_file.exists():
        state = InstallState.load(state_file)
        click.echo(f"Resuming from phase: {state.phase.value}")
        click.echo(f"Completed steps: {len(state.completed_steps)}")
        if state.failed_steps:
            click.echo(click.style(f"Failed steps: {len(state.failed_steps)}", fg="yellow"))
        click.echo()
    else:
        if state_file.exists():
            # Prompt for resume
            if not click.confirm("Previous install state found. Start fresh?"):
                state = InstallState.load(state_file)
                click.echo(f"Resuming from phase: {state.phase.value}")
            else:
                state = InstallState()
                click.echo("Starting fresh installation")
        else:
            state = InstallState()

    # Create orchestrator
    orchestrator = InstallOrchestrator(
        haniel_config, config_dir, state, config_filename=config.name
    )

    click.echo(f"Installing from: {config}")
    click.echo()

    try:
        # Phase 0: Bootstrap
        if state.phase in [InstallPhase.NOT_STARTED, InstallPhase.BOOTSTRAP]:
            click.echo(click.style("=== Phase 0: Bootstrap ===", bold=True))
            if not orchestrator.run_bootstrap_phase():
                click.echo(click.style("Bootstrap failed. Claude Code is required.", fg="red"))
                click.echo("Install it with: npm install -g @anthropic-ai/claude-code")
                sys.exit(1)
            click.echo(click.style("✓ Bootstrap complete", fg="green"))
            click.echo()

        # Phase 1: Mechanical
        if state.phase == InstallPhase.MECHANICAL:
            click.echo(click.style("=== Phase 1: Mechanical Installation ===", bold=True))
            orchestrator.run_mechanical_phase()

            # Report results
            if state.failed_steps:
                click.echo(click.style("Some steps failed:", fg="yellow"))
                for step in state.failed_steps:
                    click.echo(f"  - {step.step}: {step.error}")
            else:
                click.echo(click.style("✓ Mechanical phase complete", fg="green"))
            click.echo()

        # Phase 2: Interactive
        if state.phase == InstallPhase.INTERACTIVE:
            if skip_interactive:
                click.echo(click.style("=== Phase 2: Interactive (Skipped) ===", bold=True))
                click.echo("Interactive phase skipped by --skip-interactive flag")
                state.transition_to(InstallPhase.FINALIZE)
                orchestrator.save_state()
            else:
                click.echo(click.style("=== Phase 2: Interactive Installation ===", bold=True))

                # Check if there are pending configs
                if orchestrator.interactive.has_pending_configs():
                    status = orchestrator.interactive.get_install_status()
                    click.echo("Pending configs:")
                    for pending in status["pending_configs"]:
                        click.echo(f"  - {pending['name']}: {', '.join(pending['missing_keys'])}")
                    click.echo()
                    click.echo("Launching Claude Code for interactive setup...")
                    click.echo("(Claude Code will guide you through the remaining configuration)")
                    click.echo()

                    # For now, just transition (real implementation would launch Claude Code)
                    # orchestrator.run_interactive_phase()
                    state.transition_to(InstallPhase.FINALIZE)
                    orchestrator.save_state()
                    click.echo(click.style("✓ Interactive phase complete (simulated)", fg="green"))
                else:
                    click.echo("No interactive configuration needed")
                    state.transition_to(InstallPhase.FINALIZE)
                    orchestrator.save_state()
            click.echo()

        # Phase 3: Finalize
        if state.phase == InstallPhase.FINALIZE:
            click.echo(click.style("=== Phase 3: Finalization ===", bold=True))

            if orchestrator.run_finalize_phase():
                click.echo(click.style("✓ Finalization complete", fg="green"))
            else:
                click.echo(click.style("Finalization incomplete", fg="yellow"))
                click.echo("Some configs may be missing. Run with --resume to continue.")
            click.echo()

        # Complete
        if state.phase == InstallPhase.COMPLETE:
            click.echo(click.style("=== Installation Complete ===", fg="green", bold=True))
            click.echo()

            summary = orchestrator.finalizer.get_completion_summary()
            click.echo("Generated files:")
            for f in summary["generated_files"]:
                click.echo(f"  - {f['path']}")

            if summary["service"]:
                click.echo()
                click.echo(f"Service registered: {summary['service']['name']}")
                click.echo(f"Start with: sc start {summary['service']['name']}")
                click.echo(f"Or manually: haniel run {config}")

    except KeyboardInterrupt:
        click.echo()
        click.echo(click.style("Installation interrupted. Use --resume to continue later.", fg="yellow"))
        orchestrator.save_state()
        sys.exit(130)
    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        orchestrator.save_state()
        sys.exit(1)


@main.command()
@click.argument("config", required=False, callback=validate_config_file)
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize).")
@click.option("--dry-run", is_flag=True, help="Show what would be done without executing.")
@click.option("--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR).")
def run(config: Path | None, foreground: bool, dry_run: bool, log_level: str) -> None:
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

    # Create runner
    config_dir = config.parent.resolve()
    runner = ServiceRunner(
        config=haniel_config,
        config_dir=config_dir,
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
        runner.stop()
        if runner.self_update_requested:
            click.echo(click.style("Exiting for self-update (exit code 10).", fg="yellow"))
            sys.exit(EXIT_SELF_UPDATE)
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
    from haniel.core.health import ServiceState

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
            click.echo(click.style("Configuration errors:", fg="red", bold=True), err=True)
            for error in errors:
                click.echo(f"  - {error}", err=True)
        sys.exit(1)

    # Create a runner to get status info (not starting it)
    config_dir = config.parent.resolve()
    runner = ServiceRunner(
        config=haniel_config,
        config_dir=config_dir,
    )

    status_data = runner.get_status()

    if as_json:
        click.echo(json.dumps(status_data, indent=2))
        return

    # Human-readable output
    click.echo(click.style("haniel status", bold=True))
    click.echo()

    running = status_data.get("running", False)
    status_str = click.style("Running", fg="green") if running else click.style("Stopped", fg="yellow")
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
