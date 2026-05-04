"""Node registry — tracks connected nodes and their heartbeat status."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from starlette.websockets import WebSocket

from .event_store import EventStore
from .protocol import DeployStatus, NodeHello

logger = logging.getLogger(__name__)


@dataclass
class ConnectedNode:
    """Runtime state for a connected node. Not persisted directly."""

    node_id: str
    websocket: WebSocket
    hello: NodeHello
    last_heartbeat: float = field(default_factory=time.time)
    connected_at: float = field(default_factory=time.time)


class NodeRegistry:
    """Manages connected nodes in memory, backed by EventStore for persistence."""

    def __init__(self, store: EventStore, heartbeat_timeout: float = 90.0) -> None:
        self._nodes: dict[str, ConnectedNode] = {}
        self._store = store
        self._heartbeat_timeout = heartbeat_timeout

    async def register(self, ws: WebSocket, hello: NodeHello) -> None:
        """Register a node. Upserts in DB and adds to memory."""
        node = ConnectedNode(
            node_id=hello.node_id,
            websocket=ws,
            hello=hello,
        )
        self._nodes[hello.node_id] = node
        await self._store.upsert_node(
            node_id=hello.node_id,
            hostname=hello.hostname,
            os=hello.os,
            arch=hello.arch,
            haniel_version=hello.haniel_version,
            connected=True,
        )
        logger.info(f"Node registered: {hello.node_id} ({hello.hostname})")

    async def unregister(self, node_id: str) -> None:
        """Unregister a node. Marks as disconnected + fails deploying events."""
        self._nodes.pop(node_id, None)

        # Mark node as disconnected in DB
        await self._store.upsert_node(
            node_id=node_id,
            hostname="",
            os="",
            arch="",
            haniel_version="",
            connected=False,
        )

        # Fail any in-flight deploys for this node
        deploying = await self._store.get_deploying_events_for_node(node_id)
        for event in deploying:
            await self._store.update_deploy_status(
                event["deploy_id"],
                DeployStatus.FAILED,
                error="node disconnected",
            )
            logger.warning(
                f"Deploy {event['deploy_id']} marked as failed: node {node_id} disconnected"
            )

        logger.info(f"Node unregistered: {node_id}")

    async def heartbeat(self, node_id: str) -> None:
        """Update heartbeat timestamp for a node."""
        node = self._nodes.get(node_id)
        if node:
            node.last_heartbeat = time.time()
        await self._store.update_node_heartbeat(node_id)

    def get_node(self, node_id: str) -> ConnectedNode | None:
        """Get a connected node by ID."""
        return self._nodes.get(node_id)

    def get_connected_nodes(self) -> list[ConnectedNode]:
        """Get all currently connected nodes."""
        return list(self._nodes.values())

    async def check_stale(self) -> list[str]:
        """Identify and unregister nodes that exceeded heartbeat timeout.

        Returns list of node_ids that were unregistered.
        """
        now = time.time()
        stale_ids = [
            node_id
            for node_id, node in self._nodes.items()
            if (now - node.last_heartbeat) > self._heartbeat_timeout
        ]

        for node_id in stale_ids:
            logger.warning(f"Node {node_id} heartbeat timeout, unregistering")
            await self.unregister(node_id)

        return stale_ids
