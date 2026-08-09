"""
Config management REST API for the haniel dashboard.

Provides endpoints for reading and mutating haniel.yaml configuration
at runtime. All mutating operations follow the pattern:
  read -> modify -> semantic validate -> backup -> write -> reload
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..config.model import HanielConfig, RepoConfig, ServiceConfig
from ..config.validators import validate_config
from ..config.io import backup_config, read_config, restore_config, write_config
from ..core.service_lifecycle import (
    config_write_transaction,
    delete_service_config,
    register_repo,
    register_service,
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


def _commit_config(config_path, config: HanielConfig, runner: "ServiceRunner") -> None:
    """Atomically backup, write, and reload config.

    Must be called inside config_write_transaction(runner).

    Raises:
        RuntimeError: If writing the config file fails (restores from backup first)
    """
    _ = backup_config(config_path)
    try:
        write_config(config_path, config)
    except Exception as write_err:
        restore_config(config_path)
        raise RuntimeError(f"Write failed: {write_err}") from write_err
    runner.reload_config()


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

    # ── PUT /api/config/services/{name} ───────────────────────────────────────

    async def put_service(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.path_params["name"]
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        loop = asyncio.get_running_loop()

        def _do_put():
            try:
                new_svc = ServiceConfig.model_validate(body)
            except ValidationError as e:
                raise ValueError(str(e)) from e

            with config_write_transaction(runner):
                config = read_config(config_path)

                if name not in config.services:
                    raise KeyError(f"Service not found: {name}")

                config.services[name] = new_svc

                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))

                _commit_config(config_path, config, runner)

        try:
            await loop.run_in_executor(None, _do_put)
            return JSONResponse({"ok": True})
        except KeyError as e:
            return _error(str(e), status=404)
        except (ValueError, RuntimeError) as e:
            return _error(str(e), status=400)
        except Exception as e:
            logger.error("PUT /api/config/services/%s failed: %s", name, e)
            return _error(str(e), status=500)

    # ── POST /api/config/services ─────────────────────────────────────────────

    async def post_service(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        loop = asyncio.get_running_loop()

        def _do_post():
            svc_name = body.get("name")
            svc_data = body.get("config")
            if not svc_name or svc_data is None:
                raise ValueError("Body must contain 'name' and 'config' fields")

            try:
                new_svc = ServiceConfig.model_validate(svc_data)
            except ValidationError as e:
                raise ValueError(str(e)) from e

            with config_write_transaction(runner):
                config = read_config(config_path)

                if svc_name in config.services:
                    raise ValueError(f"Service already exists: {svc_name}")

                config.services[svc_name] = new_svc

                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))

                _commit_config(config_path, config, runner)

        try:
            await loop.run_in_executor(None, _do_post)
            return JSONResponse({"ok": True})
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("POST /api/config/services failed: %s", e)
            return _error(str(e), status=500)

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
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.path_params["name"]
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        loop = asyncio.get_running_loop()

        def _do_put():
            try:
                new_repo = RepoConfig.model_validate(body)
            except ValidationError as e:
                raise ValueError(str(e)) from e

            with config_write_transaction(runner):
                config = read_config(config_path)

                if name not in config.repos:
                    raise KeyError(f"Repo not found: {name}")

                config.repos[name] = new_repo

                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))

                _commit_config(config_path, config, runner)

        try:
            await loop.run_in_executor(None, _do_put)
            return JSONResponse({"ok": True})
        except KeyError as e:
            return _error(str(e), status=404)
        except (ValueError, RuntimeError) as e:
            return _error(str(e), status=400)
        except Exception as e:
            logger.error("PUT /api/config/repos/%s failed: %s", name, e)
            return _error(str(e), status=500)

    # ── POST /api/config/repos ────────────────────────────────────────────────

    async def post_repo(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")

        loop = asyncio.get_running_loop()

        def _do_post():
            repo_name = body.get("name")
            repo_data = body.get("config")
            if not repo_name or repo_data is None:
                raise ValueError("Body must contain 'name' and 'config' fields")

            try:
                return register_repo(runner, name=repo_name, repo_config=repo_data)
            except ValidationError as e:
                raise ValueError(str(e)) from e

        try:
            result = await loop.run_in_executor(None, _do_post)
            return JSONResponse(result)
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("POST /api/config/repos failed: %s", e)
            return _error(str(e), status=500)

    # ── DELETE /api/config/repos/{name} ───────────────────────────────────────

    async def delete_repo(request: Request) -> JSONResponse:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.path_params["name"]
        loop = asyncio.get_running_loop()

        def _do_delete():
            with config_write_transaction(runner):
                config = read_config(config_path)

                if name not in config.repos:
                    raise KeyError(f"Repo not found: {name}")

                # Reference check: services whose `repo` field points to this repo
                using_services = [
                    svc_name
                    for svc_name, svc_cfg in config.services.items()
                    if svc_cfg.repo == name
                ]
                if using_services:
                    raise ValueError(
                        f"Cannot delete repo '{name}': used by services {using_services}"
                    )

                del config.repos[name]

                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))

                _commit_config(config_path, config, runner)

        try:
            await loop.run_in_executor(None, _do_delete)
            return JSONResponse({"ok": True})
        except KeyError as e:
            return _error(str(e), status=404)
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("DELETE /api/config/repos/%s failed: %s", name, e)
            return _error(str(e), status=500)

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
