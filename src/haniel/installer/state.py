"""
Install state management.

Tracks installation progress and supports resumption:
- Completed/failed/pending steps
- Config values collected during interactive phase
- Phase transitions

haniel doesn't care what the state means - it just persists and loads JSON.
"""

import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InstallPhase(str, Enum):
    """Installation phases."""

    NOT_STARTED = "not_started"
    BOOTSTRAP = "bootstrap"  # Phase 0: Check Claude Code
    MECHANICAL = "mechanical"  # Phase 1: Directories, clone, venv
    INTERACTIVE = "interactive"  # Phase 2: Claude Code session
    FINALIZE = "finalize"  # Phase 3: Config generation, WinSW
    COMPLETE = "complete"


class StepStatus(BaseModel):
    """Status of a failed step."""

    step: str = Field(..., description="Step name")
    error: str = Field(..., description="Error message")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="When the failure occurred",
    )


class InstallState(BaseModel):
    """Installation state for resumption support."""

    phase: InstallPhase = Field(
        default=InstallPhase.NOT_STARTED, description="Current installation phase"
    )
    completed_steps: list[str] = Field(
        default_factory=list, description="Successfully completed steps"
    )
    failed_steps: list[StepStatus] = Field(
        default_factory=list, description="Failed steps with errors"
    )
    pending_configs: dict[str, list[str]] = Field(
        default_factory=dict, description="Config name -> list of missing keys"
    )
    config_values: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="Config name -> key -> value"
    )
    started_at: str | None = Field(
        default=None, description="When installation started"
    )
    updated_at: str | None = Field(
        default=None, description="Last update time"
    )

    def save(self, path: Path) -> None:
        """Save state to a JSON file.

        Args:
            path: Path to state file
        """
        self.updated_at = datetime.now().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2)
        logger.debug(f"Saved install state to {path}")

    @classmethod
    def load(cls, path: Path) -> "InstallState":
        """Load state from a JSON file.

        Args:
            path: Path to state file

        Returns:
            Loaded state, or new state if file doesn't exist
        """
        if not path.exists():
            logger.debug(f"No state file at {path}, creating new state")
            return cls()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = cls.model_validate(data)
            logger.debug(f"Loaded install state from {path}: phase={state.phase}")
            return state
        except Exception as e:
            logger.warning(f"Failed to load state from {path}: {e}, creating new state")
            return cls()

    def mark_complete(self, step: str) -> None:
        """Mark a step as complete.

        Args:
            step: Step name
        """
        if step not in self.completed_steps:
            self.completed_steps.append(step)
            logger.debug(f"Marked step complete: {step}")

    def mark_failed(self, step: str, error: str) -> None:
        """Mark a step as failed.

        Args:
            step: Step name
            error: Error message
        """
        # Remove from completed if it was there
        if step in self.completed_steps:
            self.completed_steps.remove(step)

        # Add to failed (remove old failure for same step first)
        self.failed_steps = [s for s in self.failed_steps if s.step != step]
        self.failed_steps.append(StepStatus(step=step, error=error))
        logger.debug(f"Marked step failed: {step} - {error}")

    def clear_failure(self, step: str) -> None:
        """Clear a failure from a step (for retry).

        Args:
            step: Step name
        """
        self.failed_steps = [s for s in self.failed_steps if s.step != step]
        logger.debug(f"Cleared failure for step: {step}")

    def is_step_complete(self, step: str) -> bool:
        """Check if a step is complete.

        Args:
            step: Step name

        Returns:
            True if step is complete
        """
        return step in self.completed_steps

    def get_failed_step(self, step: str) -> StepStatus | None:
        """Get failure info for a step.

        Args:
            step: Step name

        Returns:
            StepStatus if step failed, None otherwise
        """
        for s in self.failed_steps:
            if s.step == step:
                return s
        return None

    def set_config_value(self, config_name: str, key: str, value: str) -> None:
        """Set a config value.

        Args:
            config_name: Name of the config (e.g., "workspace-env")
            key: Key name
            value: Value
        """
        if config_name not in self.config_values:
            self.config_values[config_name] = {}
        self.config_values[config_name][key] = value
        logger.debug(f"Set config value: {config_name}.{key}")

    def get_config_value(self, config_name: str, key: str) -> str | None:
        """Get a config value.

        Args:
            config_name: Name of the config
            key: Key name

        Returns:
            Value if set, None otherwise
        """
        if config_name in self.config_values:
            return self.config_values[config_name].get(key)
        return None

    def start_installation(self) -> None:
        """Mark installation as started."""
        self.started_at = datetime.now().isoformat()
        self.phase = InstallPhase.BOOTSTRAP

    def transition_to(self, phase: InstallPhase) -> None:
        """Transition to a new phase.

        Args:
            phase: Target phase
        """
        logger.info(f"Transitioning from {self.phase} to {phase}")
        self.phase = phase
        self.updated_at = datetime.now().isoformat()

    def is_complete(self) -> bool:
        """Check if installation is complete."""
        return self.phase == InstallPhase.COMPLETE

    def is_incomplete(self) -> bool:
        """Check if installation was started but not completed."""
        return self.phase not in [InstallPhase.NOT_STARTED, InstallPhase.COMPLETE]

    def to_summary(self) -> dict[str, Any]:
        """Get a summary of the current state.

        Returns:
            Summary dict suitable for display or logging
        """
        return {
            "phase": self.phase.value,
            "completed_steps": len(self.completed_steps),
            "failed_steps": len(self.failed_steps),
            "configs_filled": sum(
                len(v) for v in self.config_values.values()
            ),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }
