"""Recover explicit evidence for checkout changes already applied locally."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .git import GitError, _run_git

logger = logging.getLogger(__name__)


def get_changes_between(path: Path, previous_head: str, current_head: str) -> dict:
    """Return commits and diff stat for one explicit local HEAD transition."""
    if not path.exists():
        raise GitError(f"Path does not exist: {path}")

    revision_range = f"{previous_head}..{current_head}"
    try:
        log_result = _run_git(["log", "--oneline", revision_range], cwd=path)
        commits = [line for line in log_result.stdout.strip().splitlines() if line]
        if not commits:
            return {"commits": [], "stat": None}
        stat_result = _run_git(["diff", "--stat", revision_range], cwd=path)
        return {"commits": commits, "stat": stat_result.stdout.strip() or None}
    except subprocess.CalledProcessError as exc:
        raise GitError(
            f"Failed to inspect git range {revision_range} in {path}: "
            f"{exc.stderr.strip()}"
        ) from exc


def get_applied_change_evidence(
    path: Path, previous_head: str, current_head: str
) -> dict:
    """Return actual range evidence or an explicit target-HEAD fallback event."""
    try:
        changes = get_changes_between(path, previous_head, current_head)
    except Exception as error:
        logger.warning(
            "Could not recover applied git range %s..%s: %s",
            previous_head[:8],
            current_head[:8],
            error,
        )
        changes = {"commits": [], "stat": None}
    if changes["commits"]:
        return changes
    return {
        "commits": [
            f"{current_head} self-update already applied externally "
            f"from {previous_head}"
        ],
        "stat": None,
    }
