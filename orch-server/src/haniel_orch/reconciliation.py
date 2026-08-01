"""Reconcile orchestrator deploy rows with node-observed repository HEADs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .event_store import EventStore
from .protocol import DeployStatus, RepoReconciliation

Broadcast = Callable[[dict], Awaitable[None]]
RegisterAttempt = Callable[[str, str, str, str], Awaitable[None]]


class RepoReconciler:
    """Keep active rows derived from Git truth, independently of attempt outcome."""

    def __init__(
        self,
        store: EventStore,
        broadcast: Broadcast,
        register_attempt: RegisterAttempt,
    ) -> None:
        self._store = store
        self._broadcast = broadcast
        self._register_attempt = register_attempt

    async def handle(self, msg: RepoReconciliation) -> None:
        if msg.deploy_id.startswith("attempt:"):
            raise ValueError("attempt snapshot IDs are server-only")

        in_sync = msg.local_head == msg.remote_head
        if msg.phase == "attempt_started":
            if in_sync:
                return
            started = await self._store.transition_deploy_status(
                msg.deploy_id, DeployStatus.PENDING, DeployStatus.DEPLOYING
            )
            if started:
                await self._register_attempt(
                    msg.deploy_id, msg.node_id, msg.repo, msg.branch
                )
                await self._broadcast(
                    {
                        "type": "status_change",
                        "deploy_id": msg.deploy_id,
                        "status": DeployStatus.DEPLOYING.value,
                        "node_id": msg.node_id,
                    }
                )
            return

        if in_sync:
            resolved = await self._store.resolve_pending_branch(
                msg.node_id, msg.repo, msg.branch
            )
            for deploy_id in resolved:
                await self._broadcast(
                    {
                        "type": "status_change",
                        "deploy_id": deploy_id,
                        "status": DeployStatus.SUCCESS.value,
                        "node_id": msg.node_id,
                    }
                )
            return

        event = await self._store.get_deploy_event(msg.deploy_id)
        if event is None:
            return
        try:
            terminal = DeployStatus(event["status"])
        except ValueError:
            return
        if terminal not in (DeployStatus.FAILED, DeployStatus.SUCCESS):
            return
        if await self._store.reopen_terminal_deploy(msg.deploy_id, terminal):
            await self._broadcast(
                {
                    "type": "new_pending",
                    "deploy_id": msg.deploy_id,
                    "node_id": msg.node_id,
                    "repo": msg.repo,
                    "branch": msg.branch,
                }
            )
