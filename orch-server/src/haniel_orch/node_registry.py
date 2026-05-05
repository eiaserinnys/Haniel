"""Node registry — tracks connected nodes and their heartbeat status."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from starlette.websockets import WebSocket

from .event_store import EventStore
from .protocol import NodeHello

logger = logging.getLogger(__name__)


@dataclass
class ConnectedNode:
    """Runtime state for a connected node. Not persisted directly."""

    node_id: str
    websocket: WebSocket
    hello: NodeHello
    last_heartbeat: float = field(default_factory=time.time)
    connected_at: float = field(default_factory=time.time)
    services: list[dict] | None = None  # latest service state (updated by heartbeat)


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
            services=hello.services,
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
        """Unregister a node. Marks as disconnected.

        NOTE: in-flight deploy failure (DEPLOYING → FAILED) is handled by
        :meth:`WebSocketHub._cleanup_orphan_deploys` — single source of truth
        for ws-disconnect, heartbeat-timeout, and shutdown paths. The hub
        invokes both ``unregister`` and ``_cleanup_orphan_deploys`` after
        observing a disconnect, so DEPLOYING events still transition to
        FAILED + broadcast.
        """
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

        logger.info(f"Node unregistered: {node_id}")

    async def heartbeat(self, node_id: str, services: list[dict] | None = None) -> None:
        """Update heartbeat timestamp and optionally service state for a node."""
        node = self._nodes.get(node_id)
        if node:
            node.last_heartbeat = time.time()
            if services is not None:
                node.services = services
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
