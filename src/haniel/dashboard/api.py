"""
REST API routes for the haniel dashboard.

Provides HTTP endpoints for service management, repo control, and status queries.
These routes are added to the Starlette app used by the MCP server.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 1000


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_api_routes(runner: "ServiceRunner") -> list[Route]:
    """Create Starlette route definitions for the dashboard REST API.

    Args:
        runner: ServiceRunner instance to control via API

    Returns:
        List of Starlette Route objects ready to be included in an app
    """

    def _get_service_names() -> set[str]:
        status = runner.get_status()
        return set(status.get("services", {}).keys())

    def _get_repo_names() -> set[str]:
        status = runner.get_status()
        return set(status.get("repos", {}).keys())

    # ── GET /api/status ──────────────────────────────────────────────────────

    async def get_status(request: Request) -> JSONResponse:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, runner.get_status)
        return JSONResponse(status)

    # ── GET /api/services ────────────────────────────────────────────────────

    async def get_services(request: Request) -> JSONResponse:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, runner.get_status)
        return JSONResponse(status["services"])

    # ── POST /api/services/{name}/start ─────────────────────────────────────

    async def service_start(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in _get_service_names():
            return _error(f"Service not found: {name}", status=404)
        loop = asyncio.get_event_loop()
        is_running = await loop.run_in_executor(
            None, runner.process_manager.is_running, name
        )
        if is_running:
            return JSONResponse(
                {"ok": False, "service": name, "message": "already running"}
            )
        try:
            await loop.run_in_executor(None, runner._start_service, name)
            return JSONResponse({"ok": True, "service": name, "action": "start"})
        except Exception as e:
            logger.error(f"Failed to start {name}: {e}")
            return _error(str(e))

    # ── POST /api/services/{name}/stop ──────────────────────────────────────

    async def service_stop(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in _get_service_names():
            return _error(f"Service not found: {name}", status=404)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, runner.process_manager.stop_service, name
            )
            return JSONResponse({"ok": True, "service": name, "action": "stop"})
        except Exception as e:
            logger.error(f"Failed to stop {name}: {e}")
            return _error(str(e))

    # ── POST /api/services/{name}/restart ────────────────────────────────────

    async def service_restart(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in _get_service_names():
            return _error(f"Service not found: {name}", status=404)
        try:
            loop = asyncio.get_event_loop()
            is_running = await loop.run_in_executor(
                None, runner.process_manager.is_running, name
            )
            if is_running:
                await loop.run_in_executor(
                    None, runner.process_manager.stop_service, name
                )
            await loop.run_in_executor(None, runner._start_service, name)
            return JSONResponse({"ok": True, "service": name, "action": "restart"})
        except Exception as e:
            logger.error(f"Failed to restart {name}: {e}")
            return _error(str(e))

    # ── POST /api/services/{name}/enable ─────────────────────────────────────

    async def service_enable(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in _get_service_names():
            return _error(f"Service not found: {name}", status=404)
        try:
            runner.health_manager.reset_circuit(name)
            return JSONResponse({"ok": True, "service": name, "action": "enable"})
        except Exception as e:
            return _error(str(e))

    # ── GET /api/services/{name}/logs ─────────────────────────────────────────

    async def service_logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in _get_service_names():
            return _error(f"Service not found: {name}", status=404)
        try:
            lines = int(request.query_params.get("lines", "100"))
            lines = min(lines, MAX_LOG_LINES)
        except ValueError:
            return _error("lines must be an integer")

        try:
            log_lines = runner.process_manager.log_manager.get_log_tail(name, lines)
            return JSONResponse({"service": name, "lines": log_lines})
        except Exception as e:
            return _error(str(e))

    # ── GET /api/repos ────────────────────────────────────────────────────────

    async def get_repos(request: Request) -> JSONResponse:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, runner.get_status)
        return JSONResponse(status["repos"])

    # ── POST /api/repos/{name}/pull ───────────────────────────────────────────

    async def repo_pull(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in _get_repo_names():
            return _error(f"Repository not found: {name}", status=404)
        try:
            loop = asyncio.get_event_loop()

            affected = await loop.run_in_executor(
                None, runner.get_affected_services, name
            )
            shutdown_order = await loop.run_in_executor(None, runner.get_shutdown_order)
            shutdown_order = [s for s in shutdown_order if s in affected]

            for svc in shutdown_order:
                is_running = await loop.run_in_executor(
                    None, runner.process_manager.is_running, svc
                )
                if is_running:
                    await loop.run_in_executor(
                        None, runner.process_manager.stop_service, svc
                    )

            success = await loop.run_in_executor(None, runner._pull_repo, name)
            if not success:
                return _error(f"Failed to pull repository '{name}'")

            startup_order = await loop.run_in_executor(None, runner.get_startup_order)
            startup_order = [s for s in startup_order if s in affected]
            for svc in startup_order:
                await loop.run_in_executor(None, runner._start_service, svc)

            # Include updated head commit for immediate client-side state update
            repo_state = runner._repo_states.get(name)
            new_head = repo_state.last_head if repo_state else None

            return JSONResponse(
                {
                    "ok": True,
                    "repo": name,
                    "action": "pull",
                    "restarted": startup_order,
                    "head": new_head,
                }
            )
        except Exception as e:
            logger.error(f"Failed to pull {name}: {e}")
            return _error(str(e))

    # ── POST /api/self-update/approve ─────────────────────────────────────────

    async def self_update_approve(request: Request) -> JSONResponse:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, runner.approve_self_update)
            return JSONResponse({"ok": True, "message": result})
        except Exception as e:
            return _error(str(e))

    # ── POST /api/reload ──────────────────────────────────────────────────────

    async def reload(request: Request) -> JSONResponse:
        if not hasattr(runner, "reload_config"):
            return _error("reload_config not supported", status=501)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, runner.reload_config)
            return JSONResponse({"ok": True, "action": "reload"})
        except Exception as e:
            return _error(str(e))

    return [
        Route("/api/status", get_status, methods=["GET"]),
        Route("/api/services", get_services, methods=["GET"]),
        Route("/api/services/{name}/start", service_start, methods=["POST"]),
        Route("/api/services/{name}/stop", service_stop, methods=["POST"]),
        Route("/api/services/{name}/restart", service_restart, methods=["POST"]),
        Route("/api/services/{name}/enable", service_enable, methods=["POST"]),
        Route("/api/services/{name}/logs", service_logs, methods=["GET"]),
        Route("/api/repos", get_repos, methods=["GET"]),
        Route("/api/repos/{name}/pull", repo_pull, methods=["POST"]),
        Route("/api/self-update/approve", self_update_approve, methods=["POST"]),
        Route("/api/reload", reload, methods=["POST"]),
    ]
