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
from .ws import DashboardWebSocket

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner

logger = logging.getLogger(__name__)


def setup_dashboard(
    app: web.Application,
    runner: "ServiceRunner",
    loop: asyncio.AbstractEventLoop,
) -> DashboardWebSocket:
    """Register dashboard routes on an existing aiohttp Application.

    Must be called before the app runner is set up (i.e., before
    AppRunner.setup() is awaited).

    Args:
        app: The aiohttp Application to add routes to
        runner: ServiceRunner instance to expose via API
        loop: The event loop the aiohttp server runs on

    Returns:
        DashboardWebSocket instance (for testing / shutdown use)
    """
    ws_handler = DashboardWebSocket(runner)
    ws_handler.setup(loop)

    api_routes = create_api_routes(runner)
    app.router.add_routes(api_routes)
    app.router.add_route("GET", "/ws", ws_handler.handle_ws)

    logger.info("Dashboard routes registered: %d API + WebSocket", len(api_routes))
    return ws_handler


__all__ = ["setup_dashboard", "DashboardWebSocket"]
