"""
haniel built-in web dashboard.

Provides REST API and WebSocket event stream for service management.
Integrated into the Starlette server used by the MCP Streamable HTTP transport.
"""

import logging
from typing import TYPE_CHECKING

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute

from .api import create_api_routes
from .config_api import create_config_api_routes
from .ws import DashboardWebSocket
from .static import setup_static

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner
    from ..core.claude_session import ClaudeSessionManager
    from ..integrations.slack_bot import SlackBot

logger = logging.getLogger(__name__)

DASHBOARD_PATHS_PREFIX = ("/api/", "/ws")


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforces Bearer token authentication on /api/* and /ws requests."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(DASHBOARD_PATHS_PREFIX):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return Response(
                    content="Authorization: Bearer <token> required",
                    status_code=401,
                )
            if auth_header[len("Bearer ") :] != self._token:
                return Response(
                    content="Invalid token",
                    status_code=403,
                )
        return await call_next(request)


def setup_dashboard(
    runner: "ServiceRunner",
    token: str | None = None,
    claude_session_manager: "ClaudeSessionManager | None" = None,
    slack_bot: "SlackBot | None" = None,
) -> tuple[list[Route | WebSocketRoute], list[Middleware], DashboardWebSocket]:
    """Create dashboard routes, middleware, and WebSocket handler.

    Args:
        runner: ServiceRunner instance to expose via API
        token: Bearer token for authentication. If None, dashboard is
               accessible without auth (a warning is logged).
        claude_session_manager: Optional ClaudeSessionManager for the chat panel

    Returns:
        Tuple of (routes, middleware_list, ws_handler)
    """
    middleware: list[Middleware] = []
    if token:
        middleware.append(Middleware(AuthMiddleware, token=token))
    else:
        logger.warning(
            "Dashboard is running without authentication. "
            "Set dashboard.token in haniel.yaml to restrict access."
        )

    ws_handler = DashboardWebSocket(runner)

    api_routes = create_api_routes(runner)
    config_routes = create_config_api_routes(runner)

    routes: list[Route | WebSocketRoute] = []
    routes.extend(api_routes)
    routes.extend(config_routes)
    routes.append(WebSocketRoute("/ws", ws_handler.handle_ws))

    if claude_session_manager is not None:
        from .chat_ws import ChatWebSocket
        from .chat_broadcast import ChatBroadcaster

        broadcaster = ChatBroadcaster()
        chat_ws_handler = ChatWebSocket(
            claude_session_manager,
            slack_bot=slack_bot,
            broadcaster=broadcaster,
        )
        routes.append(WebSocketRoute("/ws/chat", chat_ws_handler.handle_ws))

        # Bind chat deps to DashboardWebSocket for deferred DM handler registration
        ws_handler.configure_chat(
            slack_bot=slack_bot,
            broadcaster=broadcaster,
            session_manager=claude_session_manager,
        )

        logger.info(
            "Dashboard routes registered: %d API + %d config API + WebSocket + Chat WebSocket",
            len(api_routes),
            len(config_routes),
        )
    else:
        logger.info(
            "Dashboard routes registered: %d API + %d config API + WebSocket",
            len(api_routes),
            len(config_routes),
        )

    # Static file routes (SPA fallback must come last)
    static_routes = setup_static()
    routes.extend(static_routes)

    return routes, middleware, ws_handler


__all__ = ["setup_dashboard", "DashboardWebSocket"]
