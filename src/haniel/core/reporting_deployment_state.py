"""Deployment journal adapter that reports committed progress."""

from __future__ import annotations

import logging
from typing import Any, Callable

from .deployment_state import DeploymentStateStore

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]
_PROGRESS_STATES = frozenset(
    {
        "build",
        "preflight",
        "backing_up",
        "migrating",
        "starting",
        "verifying",
        "recovering",
    }
)


class ReportingDeploymentStateStore(DeploymentStateStore):
    """Persist journal truth, then report nonterminal phase changes."""

    def __init__(self, directory, progress_callback: ProgressCallback | None) -> None:
        super().__init__(directory)
        self._progress_callback = progress_callback

    def begin(self, *args: Any, **kwargs: Any) -> str:
        journal_attempt_id = super().begin(*args, **kwargs)
        self._report("build")
        return journal_attempt_id

    def transition(self, repo_name: str, state: str, **kwargs: Any) -> None:
        super().transition(repo_name, state, **kwargs)
        self._report(state)

    def _report(self, state: str) -> None:
        if self._progress_callback is None or state not in _PROGRESS_STATES:
            return
        try:
            self._progress_callback(state)
        except Exception as exc:
            logger.warning("Failed to report deploy progress %s: %s", state, exc)
