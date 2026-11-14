"""Attempt-independent reporting context for node-owned deployments."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import HanielConfig
from .deployment_state import DeploymentStateStore
from .repo_reconciliation import deterministic_deploy_id
from .repo_reconciliation import RepoReconciliationSnapshot
from .orch_outbox import OrchOutbox
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
    prune_correlations: bool = False,
) -> None:
    """Reconcile the latest terminal journal with the durable outbox."""
    orchestrator = config.orchestrator_client
    if orchestrator_client is None or orchestrator is None or not orchestrator.enabled:
        return

    retained = set()
    all_read = True
    for repo_name, repo_config in config.repos.items():
        if repo_config.release_manifest is None:
            continue
        try:
            journal = journal_store.read(repo_name)
            if journal is None or journal.get("state") not in {
                "success",
                "failed",
                "verification_failed",
            }:
                continue
            node_attempt_id = _required_journal_text(
                journal, "journal_attempt_id", repo_name
            )
            target_head = _required_journal_text(journal, "target_head", repo_name)
            local_head = get_local_head(config_dir / repo_config.path)
            success = journal["state"] == "success"
            registry = getattr(orchestrator_client, "_registry", None)
            outbox = getattr(registry, "outbox", None)
            if not isinstance(outbox, OrchOutbox):
                if not success or local_head != target_head:
                    continue
            attempt = journal.get("orchestrator_attempt_id")
            key = "o:" + attempt if attempt else "n:" + node_attempt_id
            retained.add(key)
            if isinstance(outbox, OrchOutbox) and (
                key in outbox.entries or outbox.was_acked(repo_name, key)
            ):
                continue
            deploy_id = deterministic_deploy_id(
                orchestrator.node_id, repo_name, repo_config.branch, target_head
            )
            error = (
                None
                if success
                else str(
                    journal.get("error")
                    or journal.get("error_code")
                    or "deployment failed"
                )
            )
            if attempt:
                generation = (
                    outbox.correlation(repo_name, key)
                    if isinstance(outbox, OrchOutbox)
                    else None
                )
                if generation is None:
                    logger.warning(
                        "Cannot reconstruct orchestrator attempt %s without its connection generation",
                        attempt,
                    )
                    continue
                snapshot = RepoReconciliationSnapshot(
                    orchestrator.node_id,
                    repo_name,
                    repo_config.branch,
                    local_head,
                    target_head,
                    deploy_id,
                )
                orchestrator_client.enqueue_deploy_result(
                    deploy_id,
                    "success" if success else "failed",
                    error=error,
                    duration_ms=0,
                    orchestrator_attempt_id=attempt,
                    connection_generation=generation,
                    settled_snapshot=snapshot,
                )
                continue
            if success and local_head != target_head:
                logger.warning(
                    "Cannot reconstruct node success for %s: checkout changed",
                    repo_name,
                )
                continue
            orchestrator_client.enqueue_node_deploy_report(
                phase="succeeded" if success else "failed",
                node_attempt_id=node_attempt_id,
                journal_attempt_id=node_attempt_id,
                deploy_id=deploy_id,
                repo=repo_name,
                branch=repo_config.branch,
                target_head=target_head,
                local_head=local_head,
                trigger="startup",
                duration_ms=0,
                **({"error": error} if error else {}),
            )
        except Exception as error:
            all_read = False
            logger.warning(
                "Failed to queue startup retro deploy report for %s: %s",
                repo_name,
                bounded_redact_text(str(error)),
            )
    registry = getattr(orchestrator_client, "_registry", None)
    outbox = getattr(registry, "outbox", None)
    if prune_correlations and all_read and isinstance(outbox, OrchOutbox):
        outbox.prune_correlations(retained)


def _required_journal_text(journal: dict[str, object], key: str, repo_name: str) -> str:
    value = journal.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"successful deployment journal for {repo_name} lacks {key}")
    return value
