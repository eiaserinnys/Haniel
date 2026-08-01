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
    connection_generation: str
    connection_token: str
    last_heartbeat: float = field(default_factory=time.time)
    connected_at: float = field(default_factory=time.time)
    services: list[dict] | None = None  # latest service state (updated by heartbeat)


class NodeRegistry:
    """Manages connected nodes in memory, backed by EventStore for persistence."""

    def __init__(self, store: EventStore, heartbeat_timeout: float = 90.0) -> None:
        self._nodes: dict[str, ConnectedNode] = {}
        self._store = store
        self._heartbeat_timeout = heartbeat_timeout

    async def register(
        self,
        ws: WebSocket,
        hello: NodeHello,
        connection_generation: str,
        connection_token: str,
    ) -> None:
        """Register a node. Upserts in DB and adds to memory."""
        node = ConnectedNode(
            node_id=hello.node_id,
            websocket=ws,
            hello=hello,
            connection_generation=connection_generation,
            connection_token=connection_token,
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
            connection_generation=connection_generation,
            connection_token=connection_token,
        )
        logger.info(f"Node registered: {hello.node_id} ({hello.hostname})")

    def is_current_connection(
        self,
        node_id: str,
        websocket: WebSocket,
        expected_generation: str | None = None,
        expected_connection_token: str | None = None,
    ) -> bool:
        """Return True when websocket and optional generation are current."""
        node = self._nodes.get(node_id)
        return (
            node is not None
            and node.websocket is websocket
            and (
                expected_generation is None
                or node.connection_generation == expected_generation
            )
            and (
                expected_connection_token is None
                or node.connection_token == expected_connection_token
            )
        )

    def has_connection_identity(
        self, node_id: str, generation: str, connection_token: str
    ) -> bool:
        """Check the server-owned identity without exposing the WebSocket object."""
        node = self._nodes.get(node_id)
        return (
            node is not None
            and node.connection_generation == generation
            and node.connection_token == connection_token
        )

    async def unregister_if_current(
        self,
        node_id: str,
        *,
        websocket: WebSocket,
        expected_generation: str,
        expected_connection_token: str,
    ) -> bool:
        """Conditionally unregister one immutable connection generation.

        Both the in-memory removal and durable offline write are compare-and-set.
        A reconnect that lands during the durable await therefore cannot be
        erased by the older socket's cleanup.

        Durable deploy attempts are not failed here. Their DB deadline remains
        the single terminal authority across disconnects and server restarts.
        """
        node = self._nodes.get(node_id)
        if (
            node is None
            or node.websocket is not websocket
            or node.connection_generation != expected_generation
            or node.connection_token != expected_connection_token
        ):
            logger.debug("Ignoring stale unregister for node: %s", node_id)
            return False

        self._nodes.pop(node_id, None)

        # Mark node as disconnected in DB without erasing the last known
        # hostname/version shown in the dashboard.
        durable_removed = await self._store.mark_node_disconnected(
            node_id, expected_generation, expected_connection_token
        )
        if not durable_removed:
            logger.debug(
                "Ignoring generation superseded during unregister for node: %s",
                node_id,
            )
            return False

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
        await self._store.update_node_heartbeat(
            node_id,
            expected_generation=node.connection_generation,
            expected_connection_token=node.connection_token,
        )

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
