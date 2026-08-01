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

    def is_current_connection(self, node_id: str, websocket: WebSocket) -> bool:
        """Return True when websocket is the active connection for node_id."""
        node = self._nodes.get(node_id)
        return node is not None and node.websocket is websocket

    async def unregister(
        self, node_id: str, websocket: WebSocket | None = None
    ) -> bool:
        """Unregister a node. Marks as disconnected.

        When websocket is provided, unregister only if that exact connection is
        still current. This prevents a late close from a superseded connection
        from deleting a freshly reconnected node with the same node_id.

        Durable deploy attempts are not failed here. Their DB deadline remains
        the single terminal authority across disconnects and server restarts.
        """
        node = self._nodes.get(node_id)
        if websocket is not None and (node is None or node.websocket is not websocket):
            logger.debug("Ignoring stale unregister for node: %s", node_id)
            return False

        self._nodes.pop(node_id, None)

        # Mark node as disconnected in DB without erasing the last known
        # hostname/version shown in the dashboard.
        await self._store.mark_node_disconnected(node_id)

        logger.info(f"Node unregistered: {node_id}")
        return True

    async def heartbeat(
        self,
        node_id: str,
        services: list[dict] | None = None,
        websocket: WebSocket | None = None,
    ) -> None:
        """Update heartbeat timestamp and optionally service state for a node."""
        node = self._nodes.get(node_id)
        if node is None:
            return
        if websocket is not None and node.websocket is not websocket:
            logger.debug("Ignoring stale heartbeat for node: %s", node_id)
            return
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
        """Identify nodes that exceeded heartbeat timeout.

        The hub owns disconnect side effects so WebSocket close, heartbeat
        timeout, and outbound send failure share one cleanup path.
        """
        stale_nodes = await self.check_stale_connections()
        return [node.node_id for node in stale_nodes]

    async def check_stale_connections(self) -> list[ConnectedNode]:
        """Return stale node records with their exact WebSocket connection."""
        now = time.time()
        stale_nodes = [
            node
            for node in self._nodes.values()
            if (now - node.last_heartbeat) > self._heartbeat_timeout
        ]

        for node in stale_nodes:
            logger.warning(f"Node {node.node_id} heartbeat timeout")

        return stale_nodes
