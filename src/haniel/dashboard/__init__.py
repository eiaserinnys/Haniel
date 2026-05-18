"""
haniel built-in web dashboard.

Provides REST API and WebSocket event stream for service management.
Integrated into the Starlette server used by the MCP Streamable HTTP transport.
"""

import hmac
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import create_api_routes
from .config_api import create_config_api_routes
from .ws import DashboardWebSocket
from .static import setup_static

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner
    from ..core.claude_session import ClaudeSessionManager
    from ..integrations.slack_bot import SlackBot

logger = logging.getLogger(__name__)


class AuthMiddleware:
    """Pure ASGI middleware for Bearer token authentication on /api/* routes.

    Pure-ASGI (not BaseHTTPMiddleware) so that the `scope["type"]` check below
    can short-circuit WebSocket upgrades — BaseHTTPMiddleware only sees HTTP
    requests, so a previous /ws guard via path-prefix was *bypassed* by every
    real WebSocket. WS authentication is now enforced inside ``handle_ws()``
    via query-param token (matches the orchestrator-server pattern).

    When ``token`` is empty/None, all requests pass through (auth disabled —
    backward compat for noauth deployments and existing tests).
    """

    def __init__(self, app: ASGIApp, token: str = "") -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Skip WS (handled by handle_ws) and noauth mode
        if scope["type"] != "http" or not self._token:
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Only guard /api/* routes. /ws/*, /auth/*, /dashboard SPA assets
        # and root pass through.
        if not path.startswith("/api/"):
            await self._app(scope, receive, send)
            return

        # Check Authorization: Bearer header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        if auth_value.startswith("Bearer "):
            token = auth_value[7:]
            if hmac.compare_digest(token, self._token):
                await self._app(scope, receive, send)
                return
            # Token present but wrong — 403
            response = JSONResponse(
                {"error": "forbidden"}, status_code=403
            )
            await response(scope, receive, send)
            return

        # No (or malformed) Authorization header — 401
        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)


def _login_handler(request: Request) -> FileResponse:
    """Serve the static token-entry page.

    Path is resolved relative to this file so the response works regardless
    of the process cwd (cf. spec-reviewer 5-1 — no cwd-relative paths).
    """
    return FileResponse(Path(__file__).parent / "static" / "auth" / "login.html")


def setup_dashboard(
    runner: "ServiceRunner",
    token: str | None = None,
    claude_session_manager: "ClaudeSessionManager | None" = None,
    slack_bot: "SlackBot | None" = None,
) -> tuple[list[Route | WebSocketRoute], list[Middleware], DashboardWebSocket]:
    """Create dashboard routes, middleware, and WebSocket handler.

    Args:
        runner: ServiceRunner instance to expose via API
        token: Bearer token for authentication. If None/empty, dashboard is
               accessible without auth (a warning is logged) — backward compat
               with existing noauth deployments and tests.
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

    # token=None when auth disabled — DashboardWebSocket.handle_ws skips the
    # query-token check in that case (mirrors AuthMiddleware short-circuit).
    ws_handler = DashboardWebSocket(runner, token=token)

    api_routes = create_api_routes(runner)
    config_routes = create_config_api_routes(runner)

    routes: list[Route | WebSocketRoute] = []
    routes.extend(api_routes)
    routes.extend(config_routes)
    routes.append(WebSocketRoute("/ws", ws_handler.handle_ws))
    # /auth/login serves the token-entry page (login.html bundled via
    # pyproject.toml force-include). MUST be registered before
    # setup_static()'s SPA fallback `Route("/{path:path}")`, otherwise the
    # catch-all swallows the path.
    routes.append(Route("/auth/login", _login_handler, methods=["GET"]))

    if claude_session_manager is not None:
        from .chat_ws import ChatWebSocket
        from .chat_broadcast import ChatBroadcaster

        broadcaster = ChatBroadcaster()
        chat_ws_handler = ChatWebSocket(
            claude_session_manager,
            slack_bot=slack_bot,
            broadcaster=broadcaster,
            token=token,
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

    # Static file routes (SPA fallback must come last — catch-all)
    static_routes = setup_static()
    routes.extend(static_routes)

    return routes, middleware, ws_handler


__all__ = ["setup_dashboard", "DashboardWebSocket", "AuthMiddleware"]
