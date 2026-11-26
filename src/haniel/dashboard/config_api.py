"""
Config management REST API for the haniel dashboard.

Provides endpoints for reading and mutating haniel.yaml configuration
at runtime. All mutating operations follow the pattern:
  read -> modify -> semantic validate -> backup -> write -> reload
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..config.model import HanielConfig
from ..config.io import read_config
from ..core.service_lifecycle import (
    create_service_config,
    delete_repo_config,
    delete_service_config,
    register_repo,
    register_service,
    update_repo_config,
    update_service_config,
)

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner

logger = logging.getLogger(__name__)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _config_to_response(config: HanielConfig) -> dict:
    """Serialize HanielConfig to a JSON-safe dict.

    Uses by_alias=True so that the self_update field appears as 'self',
    matching the original YAML key.
    """
    return config.model_dump(by_alias=True, mode="json")


def _write_failed(error: Exception) -> RuntimeError:
    return RuntimeError(f"Write failed: {error}")


def create_config_api_routes(runner: "ServiceRunner") -> list[Route]:
    """Create Starlette route definitions for the config management API.

    Args:
        runner: ServiceRunner instance whose config is exposed and mutated

    Returns:
        List of Starlette Route objects ready to be included in an app
    """

    def _get_config_path():
        """Return config_path or None."""
        return getattr(runner, "config_path", None)

    # ── GET /api/config ───────────────────────────────────────────────────────

    async def get_config(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)
        try:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, read_config, config_path)
            return JSONResponse(_config_to_response(config))
        except Exception as e:
            logger.error("Failed to read config: %s", e)
            return _error(str(e), status=500)

    # ── GET /api/config/services ──────────────────────────────────────────────

    async def get_config_services(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)
        try:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, read_config, config_path)
            data = _config_to_response(config)
            return JSONResponse(data.get("services", {}))
        except Exception as e:
            logger.error("Failed to read config services: %s", e)
            return _error(str(e), status=500)

    # ── GET /api/config/repos ─────────────────────────────────────────────────

    async def get_config_repos(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)
        try:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, read_config, config_path)
            data = _config_to_response(config)
            return JSONResponse(data.get("repos", {}))
        except Exception as e:
            logger.error("Failed to read config repos: %s", e)
            return _error(str(e), status=500)

    async def _apply_change(
        call, label: str, *, runtime_status: int, not_found: bool = True
    ) -> JSONResponse:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, call)
            return JSONResponse(result)
        except KeyError as e:
            if not_found:
                return _error(str(e), status=404)
            return _internal_error(label, e)
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=runtime_status)
        except Exception as e:
            return _internal_error(label, e)

    def _internal_error(label: str, error: Exception) -> JSONResponse:
        logger.error("%s failed: %s", label, error)
        return _error(str(error), status=500)

    # ── PUT /api/config/services/{name} ───────────────────────────────────────

    async def put_service(request: Request) -> JSONResponse:
        if not _get_config_path():
            return _error("config_path not set", status=501)

        name = request.path_params["name"]
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        def _do_put():
            update_service_config(runner, name, body, wrap_write_error=_write_failed)
            return {"ok": True}

        return await _apply_change(
            _do_put, f"PUT /api/config/services/{name}", runtime_status=400
        )

    # ── POST /api/config/services ─────────────────────────────────────────────

    async def post_service(request: Request) -> JSONResponse:
        if not _get_config_path():
            return _error("config_path not set", status=501)

        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        def _do_post():
            svc_name = body.get("name")
            svc_data = body.get("config")
            if not svc_name or svc_data is None:
                raise ValueError("Body must contain 'name' and 'config' fields")

            create_service_config(
                runner, svc_name, svc_data, wrap_write_error=_write_failed
            )
            return {"ok": True}

        return await _apply_change(
            _do_post, "POST /api/config/services", runtime_status=500, not_found=False
        )

    # ── POST /api/config/services/register ───────────────────────────────────

    async def register_service_route(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        name = body.get("name")
        service_config = body.get("service_config", body.get("config"))
        if not name or service_config is None:
            return _error("Body must contain 'name' and 'service_config' fields")

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: register_service(
                    runner,
                    name=name,
                    service_config=service_config,
                    repo=body.get("repo"),
                    repo_config=body.get("repo_config"),
                    start=body.get("start", True),
                ),
            )
            return JSONResponse(result)
        except KeyError as e:
            return _error(str(e), status=404)
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("POST /api/config/services/register failed: %s", e)
            return _error(str(e), status=500)

    # ── DELETE /api/config/services/{name} ────────────────────────────────────

    async def delete_service(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.path_params["name"]
        purge = request.query_params.get("purge", "").lower() == "true"
        loop = asyncio.get_running_loop()

        try:
            result = await loop.run_in_executor(
                None,
                lambda: delete_service_config(runner, name, purge=purge),
            )
            return JSONResponse(result)
        except KeyError as e:
            return _error(str(e), status=404)
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("DELETE /api/config/services/%s failed: %s", name, e)
            return _error(str(e), status=500)

    # ── PUT /api/config/repos/{name} ──────────────────────────────────────────

    async def put_repo(request: Request) -> JSONResponse:
        if not _get_config_path():
            return _error("config_path not set", status=501)

        name = request.path_params["name"]
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        def _do_put():
            update_repo_config(runner, name, body, wrap_write_error=_write_failed)
            return {"ok": True}

        return await _apply_change(
            _do_put, f"PUT /api/config/repos/{name}", runtime_status=400
        )

    # ── POST /api/config/repos ────────────────────────────────────────────────

    async def post_repo(request: Request) -> JSONResponse:
        if not _get_config_path():
            return _error("config_path not set", status=501)

        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        def _do_post():
            repo_name = body.get("name")
            repo_data = body.get("config")
            if not repo_name or repo_data is None:
                raise ValueError("Body must contain 'name' and 'config' fields")

            return register_repo(runner, name=repo_name, repo_config=repo_data)

        return await _apply_change(
            _do_post, "POST /api/config/repos", runtime_status=500, not_found=False
        )

    # ── DELETE /api/config/repos/{name} ───────────────────────────────────────

    async def delete_repo(request: Request) -> JSONResponse:
        if not _get_config_path():
            return _error("config_path not set", status=501)

        name = request.path_params["name"]

        def _do_delete():
            delete_repo_config(runner, name, wrap_write_error=_write_failed)
            return {"ok": True}

        return await _apply_change(
            _do_delete, f"DELETE /api/config/repos/{name}", runtime_status=500
        )

    return [
        Route("/api/config", get_config, methods=["GET"]),
        Route("/api/config/services", get_config_services, methods=["GET"]),
        Route("/api/config/repos", get_config_repos, methods=["GET"]),
        Route("/api/config/services/{name}", put_service, methods=["PUT"]),
        Route("/api/config/services", post_service, methods=["POST"]),
        Route(
            "/api/config/services/register",
            register_service_route,
            methods=["POST"],
        ),
        Route("/api/config/services/{name}", delete_service, methods=["DELETE"]),
        Route("/api/config/repos/{name}", put_repo, methods=["PUT"]),
        Route("/api/config/repos", post_repo, methods=["POST"]),
        Route("/api/config/repos/{name}", delete_repo, methods=["DELETE"]),
    ]
