"""Immutable config and repository observations for lock-free runner I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import HanielConfig, RepoConfig, ServiceConfig


@dataclass(frozen=True)
class RunnerConfigSnapshot:
    generation: int
    config: HanielConfig
    enabled_services: dict[str, ServiceConfig]
    repo_identity: dict[str, RepoConfig]
    self_repo: str | None
    startup_order: tuple[str, ...]
    shutdown_order: tuple[str, ...]


@dataclass(frozen=True)
class RepoRuntimeSnapshot:
    generation: int
    repo_name: str
    config: RepoConfig
    last_head: str | None
    last_fetch: datetime | None
    fetch_error: str | None
    pending_changes: dict[str, Any] | None
    pull_lock: Any
    affected_services: tuple[str, ...]


@dataclass(frozen=True)
class RepoObservation:
    generation: int
    repo_name: str
    repo_config: RepoConfig
    last_fetch: datetime | None
    fetch_error: str | None
    last_head: str | None
    pending_changes: dict[str, Any] | None
    changed: bool = False
