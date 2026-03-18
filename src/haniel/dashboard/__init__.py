"""
haniel built-in web dashboard.

Provides REST API and WebSocket event stream for service management.
Integrated into the existing aiohttp server used by the MCP SSE transport.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from .api import create_api_routes
from .config_api import create_config_api_routes
from .ws import DashboardWebSocket
from .static import setup_static

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner

logger = logging.getLogger(__name__)

DASHBOARD_PATHS_PREFIX = ("/api/", "/ws")


def _make_auth_middleware(token: str):
    """Create an aiohttp middleware that enforces Bearer token authentication
    on all /api/* and /ws requests.
    """

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path.startswith(DASHBOARD_PATHS_PREFIX):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                raise web.HTTPUnauthorized(
                    reason="Authorization: Bearer <token> required"
                )
            if auth_header[len("Bearer "):] != token:
                raise web.HTTPForbidden(reason="Invalid token")
        return await handler(request)

    return auth_middleware


def setup_dashboard(
    app: web.Application,
    runner: "ServiceRunner",
    loop: asyncio.AbstractEventLoop,
    token: str | None = None,
) -> DashboardWebSocket:
    """Register dashboard routes on an existing aiohttp Application.

    Must be called before the app runner is set up (i.e., before
    AppRunner.setup() is awaited).

    Args:
        app: The aiohttp Application to add routes to
        runner: ServiceRunner instance to expose via API
        loop: The event loop the aiohttp server runs on
        token: Bearer token for authentication. If None, dashboard is
               accessible without auth (a warning is logged).

    Returns:
        DashboardWebSocket instance (for testing / shutdown use)
    """
    if token:
        app.middlewares.append(_make_auth_middleware(token))  # type: ignore[arg-type]
    else:
        logger.warning(
            "Dashboard is running without authentication. "
            "Set dashboard.token in haniel.yaml to restrict access."
        )

    ws_handler = DashboardWebSocket(runner)
    ws_handler.setup(loop)

    api_routes = create_api_routes(runner)
    config_routes = create_config_api_routes(runner)
    app.router.add_routes(api_routes)
    app.router.add_routes(config_routes)
    app.router.add_route("GET", "/ws", ws_handler.handle_ws)

    logger.info(
        "Dashboard routes registered: %d API + %d config API + WebSocket",
        len(api_routes),
        len(config_routes),
    )

    # Serve built frontend (must come after API/WS routes)
    setup_static(app)

    return ws_handler


__all__ = ["setup_dashboard", "DashboardWebSocket"]
