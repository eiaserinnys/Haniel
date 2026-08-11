"""Attempt-independent reporting context for node-owned deployments."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import HanielConfig
from .deployment_state import DeploymentStateStore
from .repo_reconciliation import deterministic_deploy_id
from .safety_redaction import bounded_redact_text

if TYPE_CHECKING:
    from ..integrations.orchestrator_client import OrchestratorClient

logger = logging.getLogger(__name__)


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


def enqueue_startup_retro_reports(
    *,
    config: HanielConfig,
    config_dir: Path,
    orchestrator_client: OrchestratorClient | None,
    journal_store: DeploymentStateStore,
    get_local_head: Callable[[Path], str],
) -> None:
    """Replay safe terminal evidence from successful deployment journals."""
    orchestrator = config.orchestrator_client
    if orchestrator_client is None or orchestrator is None or not orchestrator.enabled:
        return

    for repo_name, repo_config in config.repos.items():
        if repo_config.release_manifest is None:
            continue
        try:
            journal = journal_store.read(repo_name)
            if journal is None or journal.get("state") != "success":
                continue
            node_attempt_id = _required_journal_text(
                journal, "journal_attempt_id", repo_name
            )
            target_head = _required_journal_text(journal, "target_head", repo_name)
            local_head = get_local_head(config_dir / repo_config.path)
            if local_head != target_head:
                continue
            orchestrator_client.enqueue_node_deploy_report(
                phase="succeeded",
                node_attempt_id=node_attempt_id,
                journal_attempt_id=node_attempt_id,
                deploy_id=deterministic_deploy_id(
                    orchestrator.node_id,
                    repo_name,
                    repo_config.branch,
                    target_head,
                ),
                repo=repo_name,
                branch=repo_config.branch,
                target_head=target_head,
                local_head=local_head,
                trigger="startup",
                duration_ms=0,
            )
        except Exception as error:
            logger.warning(
                "Failed to queue startup retro deploy report for %s: %s",
                repo_name,
                bounded_redact_text(str(error)),
            )


def _required_journal_text(journal: dict[str, object], key: str, repo_name: str) -> str:
    value = journal.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"successful deployment journal for {repo_name} lacks {key}")
    return value
