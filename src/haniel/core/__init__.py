"""
haniel core — runtime engine.

The poll → pull → restart cycle: change detection, process management,
health monitoring, git operations, and log capture.
"""

from .git import (
    GitCloneError,
    GitError,
    GitFetchError,
    GitPullError,
    GitTimeoutError,
    clone_repo,
    fetch_repo,
    get_head,
    get_remote_head,
    has_changes,
    pull_repo,
)
from .health import HealthManager, ServiceState
from .logs import LogCapture, LogManager, StreamReader
from .process import ProcessManager, ReadyCondition, ReadyConditionType
from .runner import DependencyGraph, ServiceRunner

__all__ = [
    # runner
    "ServiceRunner",
    "DependencyGraph",
    # process
    "ProcessManager",
    "ReadyCondition",
    "ReadyConditionType",
    # health
    "HealthManager",
    "ServiceState",
    # git
    "clone_repo",
    "fetch_repo",
    "pull_repo",
    "get_head",
    "get_remote_head",
    "has_changes",
    "GitError",
    "GitCloneError",
    "GitFetchError",
    "GitPullError",
    "GitTimeoutError",
    # logs
    "LogCapture",
    "LogManager",
    "StreamReader",
]
