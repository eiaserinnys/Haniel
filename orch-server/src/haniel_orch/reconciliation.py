"""Reconcile orchestrator deploy rows with node-observed repository HEADs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .event_store import EventStore
from .protocol import DeployStatus, RepoReconciliation

Broadcast = Callable[[dict], Awaitable[None]]


class RepoReconciler:
    """Repair only non-retry stale Pending rows from observed Git truth."""

    def __init__(
        self,
        store: EventStore,
        broadcast: Broadcast,
    ) -> None:
        self._store = store
        self._broadcast = broadcast

    async def handle(self, msg: RepoReconciliation) -> None:
        if msg.deploy_id.startswith("attempt:"):
            raise ValueError("attempt snapshot IDs are server-only")

        if msg.phase != "observed":
            return
        resolved = await self._store.resolve_observed_pending(
            deploy_id=msg.deploy_id,
            node_id=msg.node_id,
            repo=msg.repo,
            branch=msg.branch,
            local_head=msg.local_head,
            remote_head=msg.remote_head,
        )
        if not resolved:
            return
        await self._broadcast(
            {
                "type": "status_change",
                "deploy_id": msg.deploy_id,
                "status": DeployStatus.SUCCESS.value,
                "node_id": msg.node_id,
            }
        )
