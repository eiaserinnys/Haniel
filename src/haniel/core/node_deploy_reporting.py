"""Attempt-independent reporting context for node-owned deployments."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeDeployReportContext:
    """Immutable identity retained from start through terminal reporting."""

    node_attempt_id: str
    journal_attempt_id: str | None
    deploy_id: str
    node_id: str
    repo: str
    branch: str
    target_head: str
    started_local_head: str
    trigger: str
    started_monotonic: float
