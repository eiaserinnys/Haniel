"""
Static file serving for the Haniel dashboard.

Registers the Vite build output (dashboard/dist/) on the aiohttp app.
All paths not matching /api/*, /ws, or /sse fall back to index.html
so React Router works in production.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Paths that should not fall back to index.html
_API_PREFIXES = ("/api/", "/ws", "/sse")


def _find_dist() -> Path | None:
    """Locate the Vite build output relative to this file."""
    # src/haniel/dashboard/static.py → project root / dashboard / dist
    # .parent×3: dashboard/ → haniel/ → src/ → .self/
    here = Path(__file__).parent
    candidate = here.parent.parent.parent / "dashboard" / "dist"
    if candidate.is_dir():
        return candidate
    return None


async def _spa_fallback(request: web.Request) -> web.Response:
    """Return index.html for all non-API routes (SPA fallback)."""
    dist = request.app["_dashboard_dist"]
    index = dist / "index.html"
    if not index.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(index)


def setup_static(app: web.Application) -> None:
    """Register static file routes on the aiohttp application.

    Must be called after API and WebSocket routes are registered
    so that /api/* and /ws take precedence.

    Args:
        app: The aiohttp Application to add routes to
    """
    dist = _find_dist()
    if dist is None:
        logger.warning(
            "Dashboard dist not found — run `pnpm build` in dashboard/. "
            "Static serving is disabled."
        )
        return

    assets_dir = dist / "assets"
    if not assets_dir.is_dir():
        logger.warning(
            "Dashboard dist/assets not found — run `pnpm build` in dashboard/. "
            "Static serving is disabled."
        )
        return

    app["_dashboard_dist"] = dist

    # Serve static assets (JS, CSS, images, …)
    app.router.add_static("/assets", assets_dir, name="dashboard_assets")

    # SPA fallback for all other paths (must come last)
    app.router.add_route("GET", "/{path_info:.*}", _spa_fallback)

    logger.info("Dashboard static serving enabled from: %s", dist)
