"""Orchestrator server assembly — config + Starlette application."""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Literal

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from .api import create_api_routes
from .event_store import EventStore
from .hub import WebSocketHub
from .node_registry import NodeRegistry
from .push import NullPushService, PushService, RelayPushService

logger = logging.getLogger(__name__)


class PushConfig(BaseModel):
    """Push notification configuration. Relay mode sends via CF Workers."""

    mode: Literal["relay"]  # future: add "direct"
    relay_url: str | None = None
    instance_key: str | None = None


class OrchestratorConfig(BaseModel):
    """Server configuration. All fields are required — no hidden defaults."""

    token: str  # shared secret for node authentication
    db_path: str = "orchestrator.db"
    host: str = "0.0.0.0"
    port: int = 9300
    heartbeat_timeout: float = 90.0
    dashboard_dir: str | None = None  # override dashboard static path
    push: PushConfig | None = None  # None = push disabled


class OrchestratorServer:
    """Assembles and runs the orchestrator Starlette application."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._store = EventStore(config.db_path)
        self._registry = NodeRegistry(
            self._store, heartbeat_timeout=config.heartbeat_timeout
        )
        self._push = self._create_push_service(config)
        self._hub = WebSocketHub(
            self._registry, self._store, config.token, push_service=self._push
        )
        self._app: Starlette | None = None

    @staticmethod
    def _create_push_service(config: OrchestratorConfig) -> PushService:
        """Create push service based on configuration."""
        if config.push and config.push.mode == "relay":
            if not config.push.relay_url or not config.push.instance_key:
                raise ValueError(
                    "push.relay_url and push.instance_key required for relay mode"
                )
            return RelayPushService(config.push.relay_url, config.push.instance_key)
        return NullPushService()

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
        ws_routes: list[Route | WebSocketRoute | Mount] = [
            WebSocketRoute("/ws/node", self._hub.handle_node_ws),
            WebSocketRoute("/ws/dashboard", self._hub.handle_dashboard_ws),
        ]

        # Dashboard SPA (mount only when build artifacts exist)
        # Config override (e.g. production deployment) or auto-detect via __file__
        if self._config.dashboard_dir:
            dashboard_dir = pathlib.Path(self._config.dashboard_dir)
        else:
            # Path: __file__ = src/haniel_orch/server.py
            #   .parent = src/haniel_orch/  .parent = src/  .parent = orch-server/
            #   / "dashboard" / "dist" = orch-server/dashboard/dist/
            dashboard_dir = pathlib.Path(__file__).parent.parent.parent / "dashboard" / "dist"
        dashboard_routes: list[Route | Mount] = []
        if dashboard_dir.exists():
            async def serve_dashboard(request: Request) -> Response:
                """SPA fallback: serve static file if exists, else index.html."""
                path = request.path_params.get("path", "")
                file_path = dashboard_dir / path
                if path and file_path.is_file():
                    return FileResponse(str(file_path))
                return FileResponse(str(dashboard_dir / "index.html"))

            dashboard_routes = [
                Route("/dashboard", serve_dashboard),
                Route("/dashboard/{path:path}", serve_dashboard),
            ]
            logger.info(f"Dashboard mounted from {dashboard_dir}")

        self._app = Starlette(
            routes=api_routes + ws_routes + dashboard_routes,
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
        """Graceful shutdown: hub + push + store."""
        await self._hub.shutdown()
        await self._push.close()
        await self._store.close()
        logger.info("Orchestrator shut down")


def create_app() -> Starlette:
    """Uvicorn factory entry point.

    Reads configuration from environment variables:
        TOKEN            — shared secret for node authentication (required)
        DB_PATH          — SQLite database path
        HOST             — bind address
        PORT             — bind port
        HEARTBEAT_TIMEOUT — seconds before a node is considered disconnected
        DASHBOARD_DIR    — override dashboard static file path
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.environ.get("TOKEN")
    if not token:
        raise RuntimeError("TOKEN environment variable is required")

    config = OrchestratorConfig(
        token=token,
        db_path=os.environ.get("DB_PATH", "orchestrator.db"),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "9300")),
        heartbeat_timeout=float(os.environ.get("HEARTBEAT_TIMEOUT", "90")),
        dashboard_dir=os.environ.get("DASHBOARD_DIR"),
    )

    server = OrchestratorServer(config)
    return server.build_app()
