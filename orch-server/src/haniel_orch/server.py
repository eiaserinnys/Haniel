"""Orchestrator server assembly — config + Starlette application."""

from __future__ import annotations

import logging

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute

from .api import create_api_routes
from .event_store import EventStore
from .hub import WebSocketHub
from .node_registry import NodeRegistry

logger = logging.getLogger(__name__)


class OrchestratorConfig(BaseModel):
    """Server configuration. All fields are required — no hidden defaults."""

    token: str  # shared secret for node authentication
    db_path: str = "orchestrator.db"
    host: str = "0.0.0.0"
    port: int = 9300
    heartbeat_timeout: float = 90.0


class OrchestratorServer:
    """Assembles and runs the orchestrator Starlette application."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._store = EventStore(config.db_path)
        self._registry = NodeRegistry(
            self._store, heartbeat_timeout=config.heartbeat_timeout
        )
        self._hub = WebSocketHub(self._registry, self._store, config.token)
        self._app: Starlette | None = None

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def registry(self) -> NodeRegistry:
        return self._registry

    @property
    def hub(self) -> WebSocketHub:
        return self._hub

    def build_app(self) -> Starlette:
        """Build the Starlette application with all routes."""
        api_routes = create_api_routes(self._hub, self._store)
        ws_routes = [
            WebSocketRoute("/ws/node", self._hub.handle_node_ws),
            WebSocketRoute("/ws/dashboard", self._hub.handle_dashboard_ws),
        ]

        self._app = Starlette(
            routes=api_routes + ws_routes,
            on_startup=[self._on_startup],
            on_shutdown=[self._on_shutdown],
        )
        return self._app

    async def _on_startup(self) -> None:
        """Initialize store and start heartbeat checker."""
        await self._store.initialize()
        await self._hub.start_heartbeat_checker()
        logger.info(
            f"Orchestrator started on {self._config.host}:{self._config.port}"
        )

    async def _on_shutdown(self) -> None:
        """Graceful shutdown: hub + store."""
        await self._hub.shutdown()
        await self._store.close()
        logger.info("Orchestrator shut down")
