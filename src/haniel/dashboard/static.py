"""
Static file serving for the Haniel dashboard.

Registers the Vite build output (dashboard/dist/) as static files.
All paths not matching /api/*, /ws, /mcp, or other registered routes
fall back to index.html so React Router works in production.
"""

import logging
from pathlib import Path

from starlette.responses import FileResponse, Response
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


def _find_dist() -> Path | None:
    """Locate the Vite build output relative to this file."""
    # src/haniel/dashboard/static.py -> project root / dashboard / dist
    # .parent*3: dashboard/ -> haniel/ -> src/ -> .self/
    here = Path(__file__).parent
    candidate = here.parent.parent.parent / "dashboard" / "dist"
    if candidate.is_dir():
        return candidate
    return None


def setup_static() -> list[Mount | Route]:
    """Create static file routes for the dashboard.

    Returns a list of Starlette routes for static assets and SPA fallback.
    The SPA fallback route should be placed last in the app's route list
    so that API/WS/MCP routes take precedence.

    Returns:
        List of Mount/Route objects, or empty list if dist not found
    """
    dist = _find_dist()
    if dist is None:
        logger.warning(
            "Dashboard dist not found — run `pnpm build` in dashboard/. "
            "Static serving is disabled."
        )
        return []

    async def spa_fallback(request: Request) -> Response:
        """Return index.html for all non-API routes (SPA fallback)."""
        index = dist / "index.html"
        if not index.exists():
            return Response(status_code=404)
        return FileResponse(index)

    routes: list[Mount | Route] = [
        Mount("/assets", StaticFiles(directory=str(dist / "assets")), name="dashboard_assets"),
        Route("/{path:path}", spa_fallback, methods=["GET"]),
    ]

    logger.info("Dashboard static serving enabled from: %s", dist)
    return routes
