"""Orchestrator server assembly — config + Starlette application."""

from __future__ import annotations

import hmac
import logging
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import create_api_routes
from .auth import AuthConfig, create_auth_routes
from .event_store import EventStore
from .hub import WebSocketHub
from .node_registry import NodeRegistry
from .push import NullPushService, PushService, RelayPushService

logger = logging.getLogger(__name__)


class AuthMiddleware:
    """Pure ASGI middleware for Bearer token authentication on /api/* routes.

    Skips auth for:
      - /auth/* routes (OAuth flow)
      - /ws/* routes (handled by hub with query param token)
      - /dashboard/* routes (static SPA files)
      - Non-API paths (root, favicon, etc.)

    When auth_bearer_token is empty, all requests pass through (auth disabled).
    """

    def __init__(self, app: ASGIApp, auth_bearer_token: str = "") -> None:
        self._app = app
        self._auth_bearer_token = auth_bearer_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._auth_bearer_token:
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Only guard /api/* routes
        if not path.startswith("/api/"):
            await self._app(scope, receive, send)
            return

        # Check Authorization: Bearer header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        if auth_value.startswith("Bearer "):
            token = auth_value[7:]
            if hmac.compare_digest(token, self._auth_bearer_token):
                await self._app(scope, receive, send)
                return

        # Reject
        response = JSONResponse(
            {"error": "unauthorized"}, status_code=401
        )
        await response(scope, receive, send)


class PushConfig(BaseModel):
    """Push notification configuration. Relay mode sends via CF Workers."""

    mode: Literal["relay"]  # future: add "direct"
    relay_url: str | None = None
    instance_key: str | None = None


class OrchestratorConfig(BaseModel):
    """Server configuration. All fields are required — no hidden defaults."""

    token: str  # shared secret for node authentication
    auth_bearer_token: str = ""  # dashboard auth token; empty = auth disabled
    db_path: str = "orchestrator.db"
    host: str = "0.0.0.0"
    port: int = 9300
    heartbeat_timeout: float = 90.0
    dashboard_dir: str | None = None  # override dashboard static path
    push: PushConfig | None = None  # None = push disabled
    command_timeout_sec: float = 30.0  # service-command 응답 대기 타임아웃


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
            self._registry,
            self._store,
            config.token,
            push_service=self._push,
            auth_bearer_token=config.auth_bearer_token,
            command_timeout_sec=config.command_timeout_sec,
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
        auth_routes: list[Route] = []
        if self._config.auth_bearer_token:
            try:
                auth_config = AuthConfig()
                auth_routes = create_auth_routes(auth_config)
            except (KeyError, ValueError) as e:
                logger.warning(f"Auth routes disabled (missing env): {e}")

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

        @asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
            # startup
            await self._store.initialize()
            await self._hub.start_heartbeat_checker()
            logger.info(
                f"Orchestrator started on {self._config.host}:{self._config.port}"
            )
            yield
            # shutdown
            await self._hub.shutdown()
            await self._push.close()
            await self._store.close()
            logger.info("Orchestrator shut down")

        self._app = Starlette(
            routes=auth_routes + api_routes + ws_routes + dashboard_routes,
            lifespan=lifespan,
        )

        # Wrap with AuthMiddleware (pure ASGI — supports both HTTP and WebSocket passthrough)
        if self._config.auth_bearer_token:
            self._app = AuthMiddleware(self._app, self._config.auth_bearer_token)  # type: ignore[assignment]

        return self._app


def create_app() -> Starlette:
    """Uvicorn factory entry point.

    Reads configuration from environment variables:
        TOKEN              — shared secret for node authentication (required)
        AUTH_BEARER_TOKEN  — dashboard auth token; empty/unset = auth disabled
        DB_PATH            — SQLite database path
        HOST               — bind address
        PORT               — bind port
        HEARTBEAT_TIMEOUT  — seconds before a node is considered disconnected
        DASHBOARD_DIR      — override dashboard static file path
        ORCH_COMMAND_TIMEOUT_SEC — optional override, default 30.0s (service-command 응답 대기)
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
        auth_bearer_token=os.environ.get("AUTH_BEARER_TOKEN", ""),
        db_path=os.environ.get("DB_PATH", "orchestrator.db"),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "9300")),
        heartbeat_timeout=float(os.environ.get("HEARTBEAT_TIMEOUT", "90")),
        dashboard_dir=os.environ.get("DASHBOARD_DIR"),
    )
    # ORCH_COMMAND_TIMEOUT_SEC: optional override.
    # 기본값(30.0)은 OrchestratorConfig.command_timeout_sec 필드가 정본으로 보유
    # (env-variables.md §1: 코드에 fallback 기본값을 두지 않음).
    if "ORCH_COMMAND_TIMEOUT_SEC" in os.environ:
        timeout_override = float(os.environ["ORCH_COMMAND_TIMEOUT_SEC"])
        if timeout_override <= 0:
            raise ValueError(
                f"ORCH_COMMAND_TIMEOUT_SEC must be > 0, got {timeout_override}"
            )
        config = config.model_copy(update={"command_timeout_sec": timeout_override})

    server = OrchestratorServer(config)
    return server.build_app()
