"""
Installation orchestrator.

Controls the flow between installation phases:
- Phase 0 (Bootstrap): Check Claude Code availability
- Phase 1 (Mechanical): Directories, git clone, venv, npm
- Phase 2 (Interactive): Claude Code session for secrets
- Phase 3 (Finalize): Config generation, service registration

haniel doesn't care what each phase does - it just coordinates the flow.
"""

import logging
import shutil
from pathlib import Path
from typing import Callable

from ..config import HanielConfig
from .state import InstallState, InstallPhase

logger = logging.getLogger(__name__)


class InstallOrchestrator:
    """Orchestrates the installation process."""

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        state: InstallState,
    ):
        """Initialize the orchestrator.

        Args:
            config: Haniel configuration
            config_dir: Directory containing haniel.yaml
            state: Installation state
        """
        self.config = config
        self.config_dir = config_dir
        self.state = state
        self._state_file = config_dir / "install.state"

        # Lazy import to avoid circular dependencies
        self._mechanical: "MechanicalInstaller | None" = None
        self._interactive: "InteractiveInstaller | None" = None
        self._finalizer: "Finalizer | None" = None

    @property
    def mechanical(self) -> "MechanicalInstaller":
        """Get the mechanical installer."""
        if self._mechanical is None:
            from .mechanical import MechanicalInstaller

            self._mechanical = MechanicalInstaller(
                self.config, self.config_dir, self.state
            )
        return self._mechanical

    @property
    def interactive(self) -> "InteractiveInstaller":
        """Get the interactive installer."""
        if self._interactive is None:
            from .interactive import InteractiveInstaller

            self._interactive = InteractiveInstaller(
                self.config, self.config_dir, self.state
            )
        return self._interactive

    @property
    def finalizer(self) -> "Finalizer":
        """Get the finalizer."""
        if self._finalizer is None:
            from .finalize import Finalizer

            self._finalizer = Finalizer(self.config, self.config_dir, self.state)
        return self._finalizer

    def check_claude_code(self) -> bool:
        """Check if Claude Code is available.

        Returns:
            True if Claude Code is installed
        """
        claude_path = shutil.which("claude")
        if claude_path:
            logger.info(f"Claude Code found at: {claude_path}")
            return True
        logger.warning("Claude Code not found in PATH")
        return False

    def save_state(self) -> None:
        """Save the current state."""
        self.state.save(self._state_file)

    def run_bootstrap_phase(self) -> bool:
        """Run Phase 0: Bootstrap checks.

        Returns:
            True if bootstrap passed, False if Claude Code is missing
        """
        logger.info("=== Phase 0: Bootstrap ===")

        if self.state.phase == InstallPhase.NOT_STARTED:
            self.state.start_installation()
            self.save_state()

        # Check Claude Code (required for Phase 2)
        if not self.check_claude_code():
            logger.error("Claude Code is required but not found")
            logger.error(
                "Please install it: npm install -g @anthropic-ai/claude-code"
            )
            return False

        self.state.mark_complete("claude-code-check")
        self.state.transition_to(InstallPhase.MECHANICAL)
        self.save_state()
        return True

    def run_mechanical_phase(self) -> bool:
        """Run Phase 1: Mechanical installation.

        Returns:
            True if all mechanical steps passed
        """
        logger.info("=== Phase 1: Mechanical Installation ===")

        if self.state.phase != InstallPhase.MECHANICAL:
            self.state.transition_to(InstallPhase.MECHANICAL)

        # Check requirements
        if not self.state.is_step_complete("requirements"):
            logger.info("Checking requirements...")
            results = self.mechanical.check_requirements()
            all_passed = all(r.get("installed", False) for r in results)
            if all_passed:
                self.state.mark_complete("requirements")
            else:
                # Record failures but continue
                for r in results:
                    if not r.get("installed", False):
                        self.state.mark_failed(
                            f"requirements:{r['name']}",
                            r.get("error", "Not installed"),
                        )
            self.save_state()

        # Create directories
        if not self.state.is_step_complete("directories"):
            logger.info("Creating directories...")
            try:
                self.mechanical.create_directories()
                # mark_complete is called inside create_directories
            except Exception as e:
                self.state.mark_failed("directories", str(e))
            self.save_state()

        # Clone repositories
        if not self.state.is_step_complete("repos"):
            logger.info("Cloning repositories...")
            try:
                self.mechanical.clone_repos()
            except Exception as e:
                self.state.mark_failed("repos", str(e))
            self.save_state()

        # Create environments
        if not self.state.is_step_complete("environments"):
            logger.info("Creating environments...")
            try:
                self.mechanical.create_environments()
            except Exception as e:
                self.state.mark_failed("environments", str(e))
            self.save_state()

        # Create static configs
        if not self.state.is_step_complete("static-configs"):
            logger.info("Creating static configs...")
            try:
                self.mechanical.create_static_configs()
                self.state.mark_complete("static-configs")
            except Exception as e:
                self.state.mark_failed("static-configs", str(e))
            self.save_state()

        # Determine pending interactive configs
        self.mechanical.determine_pending_configs()
        self.save_state()

        # Transition to interactive phase
        self.state.transition_to(InstallPhase.INTERACTIVE)
        self.save_state()

        logger.info("Mechanical phase complete")
        return True

    def run_interactive_phase(
        self,
        on_status: Callable[[dict], None] | None = None,
    ) -> bool:
        """Run Phase 2: Interactive installation.

        This starts the MCP server for install mode and launches Claude Code.

        Args:
            on_status: Optional callback for status updates

        Returns:
            True if interactive phase completed successfully
        """
        logger.info("=== Phase 2: Interactive Installation ===")

        if self.state.phase != InstallPhase.INTERACTIVE:
            self.state.transition_to(InstallPhase.INTERACTIVE)
            self.save_state()

        # Check if there are pending configs
        if not self.interactive.has_pending_configs():
            logger.info("No interactive configs needed, skipping Phase 2")
            self.state.transition_to(InstallPhase.FINALIZE)
            self.save_state()
            return True

        # Launch Claude Code session
        success = self.interactive.launch_claude_code_session()

        if success:
            self.state.transition_to(InstallPhase.FINALIZE)
            self.save_state()

        return success

    def run_finalize_phase(self) -> bool:
        """Run Phase 3: Finalization.

        Returns:
            True if finalization completed successfully
        """
        logger.info("=== Phase 3: Finalization ===")

        if self.state.phase != InstallPhase.FINALIZE:
            self.state.transition_to(InstallPhase.FINALIZE)
            self.save_state()

        # Check all configs are filled
        if not self.finalizer.check_all_configs_filled():
            logger.error("Not all required configs are filled")
            return False

        # Generate config files
        try:
            self.finalizer.generate_config_files()
            self.state.mark_complete("config-generation")
        except Exception as e:
            logger.error(f"Failed to generate config files: {e}")
            self.state.mark_failed("config-generation", str(e))
            self.save_state()
            return False

        # Register service (Windows only)
        try:
            self.finalizer.register_service()
            self.state.mark_complete("service-registration")
        except Exception as e:
            logger.warning(f"Service registration failed: {e}")
            self.state.mark_failed("service-registration", str(e))
            # Don't fail the whole installation for this

        self.state.transition_to(InstallPhase.COMPLETE)
        self.save_state()

        logger.info("=== Installation Complete ===")
        return True

    def run_full_install(
        self,
        resume: bool = False,
        on_status: Callable[[dict], None] | None = None,
    ) -> bool:
        """Run the full installation process.

        Args:
            resume: If True and state exists, resume from last phase
            on_status: Optional callback for status updates

        Returns:
            True if installation completed successfully
        """
        # Load existing state if resuming
        if resume and self._state_file.exists():
            self.state = InstallState.load(self._state_file)
            logger.info(f"Resuming from phase: {self.state.phase}")
        else:
            self.state = InstallState()
            logger.info("Starting fresh installation")

        # Run phases based on current state
        if self.state.phase == InstallPhase.NOT_STARTED:
            if not self.run_bootstrap_phase():
                return False

        if self.state.phase == InstallPhase.BOOTSTRAP:
            if not self.run_bootstrap_phase():
                return False

        if self.state.phase == InstallPhase.MECHANICAL:
            if not self.run_mechanical_phase():
                return False

        if self.state.phase == InstallPhase.INTERACTIVE:
            if not self.run_interactive_phase(on_status):
                return False

        if self.state.phase == InstallPhase.FINALIZE:
            if not self.run_finalize_phase():
                return False

        return self.state.is_complete()

    def retry_step(self, step_name: str) -> dict:
        """Retry a failed step.

        Args:
            step_name: Name of the step to retry

        Returns:
            Result dict with success status
        """
        self.state.clear_failure(step_name)

        # Handle requirement retries
        if step_name.startswith("requirements:"):
            req_name = step_name.split(":")[1]
            results = self.mechanical.check_requirements()
            for r in results:
                if r["name"] == req_name:
                    if r.get("installed", False):
                        self.state.mark_complete(step_name)
                        self.save_state()
                        return {"success": True, "result": r}
                    else:
                        self.state.mark_failed(step_name, r.get("error", "Not installed"))
                        self.save_state()
                        return {"success": False, "error": r.get("error")}
            return {"success": False, "error": f"Unknown requirement: {req_name}"}

        # Handle other retries by re-running the relevant phase
        if step_name == "directories":
            try:
                self.mechanical.create_directories()
                self.save_state()
                return {"success": True}
            except Exception as e:
                self.state.mark_failed(step_name, str(e))
                self.save_state()
                return {"success": False, "error": str(e)}

        if step_name == "repos":
            try:
                self.mechanical.clone_repos()
                self.save_state()
                return {"success": True}
            except Exception as e:
                self.state.mark_failed(step_name, str(e))
                self.save_state()
                return {"success": False, "error": str(e)}

        return {"success": False, "error": f"Unknown step: {step_name}"}
