"""Install state-policy selection and the install command boundary."""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import click

from haniel.config import HanielConfig
from haniel.installer import InstallOrchestrator, InstallPhase
from haniel.installer.state import InstallState

from .common import load_and_validate, validate_config_file

StatePolicy = Literal["resume", "start-fresh"]
InstallProvenance = Literal["initial", "existing"]
ExpectedOperation = Literal["fresh_install", "upgrade"]


class InstallStatePolicyError(RuntimeError):
    """A stable install policy error suitable for CLI and automation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def select_install_state(
    *,
    state_file: Path,
    config_path: Path,
    state_policy: StatePolicy | None,
    resume_alias: bool,
    noninteractive: bool,
    prompt: Callable[[str], bool],
    defer_manifest_handover: bool = False,
    provenance: InstallProvenance | None = None,
    expected_operation: ExpectedOperation | None = None,
) -> InstallState:
    """Select resume/start-fresh without allowing automation to imply consent."""
    if resume_alias and state_policy == "start-fresh":
        raise InstallStatePolicyError(
            "STATE_POLICY_CONFLICT",
            "--resume cannot be combined with --state-policy start-fresh",
        )
    try:
        InstallState().bind_handover(
            config_path=config_path,
            deferred=defer_manifest_handover,
            provenance=provenance,
            expected_operation=expected_operation,
        )
    except ValueError as error:
        raise InstallStatePolicyError(
            "INSTALL_STATE_IDENTITY_MISMATCH", str(error)
        ) from error

    policy: StatePolicy | None = "resume" if resume_alias else state_policy
    exists = state_file.exists()

    if exists and policy is None:
        if noninteractive:
            raise InstallStatePolicyError(
                "STATE_POLICY_REQUIRED",
                "install.state exists; choose resume or start-fresh",
            )
        policy = (
            "start-fresh"
            if prompt("Previous install state found. Start fresh?")
            else "resume"
        )

    if policy == "resume" and not exists:
        raise InstallStatePolicyError(
            "STATE_NOT_FOUND", "cannot resume because install.state does not exist"
        )

    if policy == "resume":
        try:
            state = InstallState.load(state_file, strict=True)
        except Exception as error:
            raise InstallStatePolicyError(
                "INSTALL_STATE_INVALID",
                "install.state is malformed or schema-invalid; choose start-fresh",
            ) from error
    else:
        if exists:
            _archive_state(state_file)
        state = InstallState()

    try:
        state.bind_handover(
            config_path=config_path,
            deferred=defer_manifest_handover,
            provenance=provenance,
            expected_operation=expected_operation,
        )
    except ValueError as error:
        raise InstallStatePolicyError(
            "INSTALL_STATE_IDENTITY_MISMATCH", str(error)
        ) from error
    return state


def is_noninteractive(*, skip_interactive: bool) -> bool:
    """Return whether prompting is forbidden for this invocation."""
    return skip_interactive or not os.isatty(0)


def _archive_state(state_file: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = state_file.with_name(f"{state_file.name}.{stamp}.bak")
    os.replace(state_file, destination)
    return destination


def print_dry_run_install(config: HanielConfig) -> None:
    """Print the mechanical, interactive, and finalization install plan."""
    click.echo(click.style("[dry-run] Phase 1: Mechanical installation", bold=True))
    if config.install and config.install.requirements:
        click.echo("  - Requirements check:")
        for requirement, version in config.install.requirements.items():
            click.echo(f"      {requirement}: {version}")
    if config.install and config.install.directories:
        click.echo("  - Directories to create:")
        for directory in config.install.directories:
            click.echo(f"      {directory}")
    if config.repos:
        click.echo("  - Repositories to clone:")
        for name, repo in config.repos.items():
            click.echo(f"      {name} -> {repo.path}")
    if config.install and config.install.environments:
        click.echo("  - Environments to set up:")
        for name, environment in config.install.environments.items():
            click.echo(f"      {name} ({environment.type})")
    if config.install and config.install.configs:
        static = {
            name: item for name, item in config.install.configs.items() if item.content
        }
        if static:
            click.echo("  - Config files (static):")
            for name, item in static.items():
                click.echo(f"      {name} -> {item.path}")

    click.echo()
    click.echo(
        click.style("[dry-run] Phase 2: Interactive setup (Claude Code)", bold=True)
    )
    if config.install and config.install.configs:
        interactive = {
            name: item for name, item in config.install.configs.items() if item.keys
        }
        if interactive:
            click.echo("  - Config files (interactive):")
            for name, item in interactive.items():
                click.echo(f"      {name} -> {item.path}")
                missing = [key.key for key in item.keys or [] if not key.default]
                defaults = [key.key for key in item.keys or [] if key.default]
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


@click.command("install")
@click.argument("config", required=False, callback=validate_config_file)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without executing."
)
@click.option(
    "--resume", is_flag=True, help="Deprecated alias for --state-policy resume."
)
@click.option(
    "--state-policy",
    type=click.Choice(["resume", "start-fresh"]),
    help="Required for non-interactive reuse of install.state.",
)
@click.option(
    "--skip-interactive", is_flag=True, help="Skip Claude Code interactive phase."
)
@click.option(
    "--defer-manifest-handover",
    is_flag=True,
    help="Clone/validate manifest repositories without pull, build, or start.",
)
@click.option("--provenance", type=click.Choice(["initial", "existing"]))
@click.option("--expected-operation", type=click.Choice(["fresh_install", "upgrade"]))
def install_command(
    config: Path | None,
    dry_run: bool,
    resume: bool,
    state_policy: StatePolicy | None,
    skip_interactive: bool,
    defer_manifest_handover: bool,
    provenance: InstallProvenance | None,
    expected_operation: ExpectedOperation | None,
) -> None:
    """Set up the execution environment from a configuration file."""
    if config is None:
        click.echo(click.get_current_context().get_help())
        return
    haniel_config, errors = load_and_validate(config)
    if errors or haniel_config is None:
        click.echo(click.style("Configuration errors:", fg="red", bold=True), err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        raise click.exceptions.Exit(1)
    if dry_run:
        click.echo(f"[dry-run] Configuration: {config}\n")
        print_dry_run_install(haniel_config)
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config_dir = config.parent.resolve()
    state_file = config_dir / "install.state"
    try:
        state = select_install_state(
            state_file=state_file,
            config_path=config.resolve(),
            state_policy=state_policy,
            resume_alias=resume,
            noninteractive=is_noninteractive(skip_interactive=skip_interactive),
            prompt=click.confirm,
            defer_manifest_handover=defer_manifest_handover,
            provenance=provenance,
            expected_operation=expected_operation,
        )
    except InstallStatePolicyError as error:
        click.echo(str(error), err=True)
        raise click.exceptions.Exit(1) from error

    if (resume or state_policy == "resume") and state_file.exists():
        click.echo(f"Resuming from phase: {state.phase.value}")
        click.echo(f"Completed steps: {len(state.completed_steps)}")
        if state.failed_steps:
            click.echo(
                click.style(f"Failed steps: {len(state.failed_steps)}", fg="yellow")
            )
        click.echo()
    elif state_policy == "start-fresh":
        click.echo("Starting fresh installation")

    orchestrator = InstallOrchestrator(
        haniel_config,
        config_dir,
        state,
        config_filename=config.name,
        defer_manifest_handover=defer_manifest_handover,
    )
    click.echo(f"Installing from: {config}\n")
    try:
        _run_install_phases(
            orchestrator=orchestrator,
            state=state,
            config=config,
            haniel_config=haniel_config,
            skip_interactive=skip_interactive,
        )
    except KeyboardInterrupt:
        click.echo(
            "\n"
            + click.style(
                "Installation interrupted. Use --resume to continue later.", fg="yellow"
            )
        )
        orchestrator.save_state()
        raise click.exceptions.Exit(130)
    except Exception as error:
        click.echo(click.style(f"Error: {error}", fg="red"), err=True)
        orchestrator.save_state()
        raise click.exceptions.Exit(1) from error


def _run_install_phases(
    *,
    orchestrator: InstallOrchestrator,
    state: InstallState,
    config: Path,
    haniel_config: HanielConfig,
    skip_interactive: bool,
) -> None:
    if state.phase in (InstallPhase.NOT_STARTED, InstallPhase.BOOTSTRAP):
        click.echo(click.style("=== Phase 0: Bootstrap ===", bold=True))
        if not orchestrator.run_bootstrap_phase():
            raise RuntimeError("Bootstrap failed. Claude Code is required.")
        click.echo(click.style("✓ Bootstrap complete\n", fg="green"))

    if state.phase == InstallPhase.MECHANICAL:
        click.echo(click.style("=== Phase 1: Mechanical Installation ===", bold=True))
        orchestrator.run_mechanical_phase()
        if state.failed_steps:
            click.echo(click.style("Some steps failed:", fg="yellow"))
            for step in state.failed_steps:
                click.echo(f"  - {step.step}: {step.error}")
        else:
            click.echo(click.style("✓ Mechanical phase complete", fg="green"))
        click.echo()

    if state.phase == InstallPhase.INTERACTIVE:
        _run_interactive(orchestrator, state, skip_interactive)

    if state.phase == InstallPhase.FINALIZE:
        click.echo(click.style("=== Phase 3: Finalization ===", bold=True))
        if orchestrator.run_finalize_phase():
            click.echo(click.style("✓ Finalization complete", fg="green"))
        else:
            click.echo(click.style("Finalization incomplete", fg="yellow"))
            click.echo("Some configs may be missing. Run with --resume to continue.")
        click.echo()

    if state.phase == InstallPhase.COMPLETE:
        _print_completion(orchestrator, config, haniel_config)


def _run_interactive(
    orchestrator: InstallOrchestrator, state: InstallState, skip: bool
) -> None:
    if skip:
        click.echo(click.style("=== Phase 2: Interactive (Skipped) ===", bold=True))
        click.echo("Interactive phase skipped by --skip-interactive flag")
        state.transition_to(InstallPhase.FINALIZE)
        orchestrator.save_state()
        click.echo()
        return
    click.echo(click.style("=== Phase 2: Interactive Installation ===", bold=True))
    if not orchestrator.interactive.has_pending_configs():
        click.echo("No interactive configuration needed")
        state.transition_to(InstallPhase.FINALIZE)
        orchestrator.save_state()
        click.echo()
        return
    status = orchestrator.interactive.get_install_status()
    click.echo("Pending configs:")
    for pending in status["pending_configs"]:
        click.echo(f"  - {pending['name']}: {', '.join(pending['missing_keys'])}")
    click.echo("\nLaunching Claude Code for interactive setup...\n")
    if not orchestrator.run_interactive_phase():
        raise RuntimeError("Interactive phase failed; run with --resume to retry.")
    click.echo(click.style("✓ Interactive phase complete\n", fg="green"))


def _print_completion(
    orchestrator: InstallOrchestrator, config: Path, haniel_config: HanielConfig
) -> None:
    click.echo(click.style("=== Installation Complete ===\n", fg="green", bold=True))
    summary = orchestrator.finalizer.get_completion_summary()
    click.echo("Generated files:")
    for generated in summary["generated_files"]:
        click.echo(f"  - {generated['path']}")
    service_registration_deferred = orchestrator.state.is_step_complete(
        "service-registration-deferred"
    )
    if summary["service"] and not service_registration_deferred:
        click.echo(f"\nService registered: {summary['service']['name']}")
        click.echo(f"Start with: sc start {summary['service']['name']}")
        click.echo(f"Or manually: haniel run {config}")
    elif summary["service"]:
        click.echo("\nService registration deferred to manifest handover")
    if haniel_config.services:
        click.echo("\nService endpoints:")
        for name, service in haniel_config.services.items():
            if service.ready and service.ready.startswith("port:"):
                click.echo(
                    f"  {name}: http://localhost:{service.ready.split(':', 1)[1]}"
                )
