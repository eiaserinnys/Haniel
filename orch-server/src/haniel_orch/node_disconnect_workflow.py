"""Three-phase node disconnect workflow outside the WebSocket hub."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from starlette.websockets import WebSocket

from .deploy_attempt_coordinator import DeployAttemptCoordinator
from .node_registry import NodeRegistry

logger = logging.getLogger(__name__)

DisconnectedCallback = Callable[[str, str, str], Awaitable[None]]


class NodeDisconnectWorkflow:
    """Revoke execution first, then conditionally clean registry and DB state."""

    def __init__(
        self,
        registry: NodeRegistry,
        coordinator: DeployAttemptCoordinator,
        on_disconnected: DisconnectedCallback,
    ) -> None:
        self._registry = registry
        self._coordinator = coordinator
        self._on_disconnected = on_disconnected
        self._completions: dict[str, asyncio.Future[None]] = {}

    async def disconnect(
        self,
        node_id: str,
        *,
        reason: str,
        error: str,
        close_ws: bool,
        websocket: WebSocket,
        expected_generation: str,
        expected_connection_token: str,
    ) -> None:
        disconnecting = await self._coordinator.begin_disconnect(
            node_id,
            expected_generation,
            expected_connection_token,
        )
        if disconnecting is None:
            logger.debug("Ignoring stale disconnect for node: %s", node_id)
            return

        completion = self._completions.get(disconnecting.disconnect_token)
        if completion is not None:
            await completion
            return
        completion = asyncio.get_running_loop().create_future()
        self._completions[disconnecting.disconnect_token] = completion

        node = self._registry.get_node(node_id)
        if (
            close_ws
            and node is not None
            and node.websocket is websocket
            and node.connection_generation == expected_generation
            and node.connection_token == expected_connection_token
        ):
            try:
                await node.websocket.close(code=1011, reason=reason)
            except Exception as exc:
                logger.debug("Failed to close node %s websocket: %s", node_id, exc)

        try:
            await self._registry.unregister_if_current(
                node_id,
                websocket=websocket,
                expected_generation=expected_generation,
                expected_connection_token=expected_connection_token,
            )

            async def finalize_current_disconnect() -> None:
                await self._on_disconnected(node_id, reason, error)

            await self._coordinator.finalize_disconnect(
                node_id,
                disconnecting,
                on_current_disconnect=finalize_current_disconnect,
            )
        finally:
            if not completion.done():
                completion.set_result(None)
            self._completions.pop(disconnecting.disconnect_token, None)
