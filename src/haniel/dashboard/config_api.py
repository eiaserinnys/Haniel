"""
Config management REST API for the haniel dashboard.

Provides endpoints for reading and mutating haniel.yaml configuration
at runtime. All mutating operations follow the pattern:
  read → modify → semantic validate → backup → write → reload
"""

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING

from aiohttp import web
from pydantic import ValidationError

from ..config.model import HanielConfig, RepoConfig, ServiceConfig
from ..config.validators import validate_config
from .config_io import backup_config, read_config, restore_config, write_config

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner

logger = logging.getLogger(__name__)

# Module-level write lock — shared across all config mutation requests.
# Prevents concurrent read-modify-write races on the YAML file.
_write_lock = threading.Lock()


def _json_response(data, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data),
        status=status,
        content_type="application/json",
    )


def _error(message: str, status: int = 400) -> web.Response:
    return _json_response({"error": message}, status=status)


def _config_to_response(config: HanielConfig) -> dict:
    """Serialize HanielConfig to a JSON-safe dict.

    Uses by_alias=True so that the self_update field appears as 'self',
    matching the original YAML key.
    """
    return config.model_dump(by_alias=True, mode="json")


def _commit_config(config_path, config: HanielConfig, runner: "ServiceRunner") -> None:
    """Atomically backup, write, and reload config.

    Must be called while holding _write_lock.

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


def create_config_api_routes(runner: "ServiceRunner") -> list[web.RouteDef]:
    """Create aiohttp route definitions for the config management API.

    Args:
        runner: ServiceRunner instance whose config is exposed and mutated

    Returns:
        List of aiohttp RouteDef objects ready to be added to an app.router
    """

    def _get_config_path():
        """Return config_path or None."""
        return getattr(runner, "config_path", None)

    # ── GET /api/config ───────────────────────────────────────────────────────

    async def get_config(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)
        try:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, read_config, config_path)
            return _json_response(_config_to_response(config))
        except Exception as e:
            logger.error("Failed to read config: %s", e)
            return _error(str(e), status=500)

    # ── GET /api/config/services ──────────────────────────────────────────────

    async def get_config_services(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)
        try:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, read_config, config_path)
            data = _config_to_response(config)
            return _json_response(data.get("services", {}))
        except Exception as e:
            logger.error("Failed to read config services: %s", e)
            return _error(str(e), status=500)

    # ── GET /api/config/repos ─────────────────────────────────────────────────

    async def get_config_repos(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)
        try:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, read_config, config_path)
            data = _config_to_response(config)
            return _json_response(data.get("repos", {}))
        except Exception as e:
            logger.error("Failed to read config repos: %s", e)
            return _error(str(e), status=500)

    # ── PUT /api/config/services/{name} ───────────────────────────────────────

    async def put_service(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.match_info["name"]
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

            with _write_lock:
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
            return _json_response({"ok": True})
        except KeyError as e:
            return _error(str(e), status=404)
        except (ValueError, RuntimeError) as e:
            return _error(str(e), status=400)
        except Exception as e:
            logger.error("PUT /api/config/services/%s failed: %s", name, e)
            return _error(str(e), status=500)

    # ── POST /api/config/services ─────────────────────────────────────────────

    async def post_service(request: web.Request) -> web.Response:
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

            with _write_lock:
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
            return _json_response({"ok": True})
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("POST /api/config/services failed: %s", e)
            return _error(str(e), status=500)

    # ── DELETE /api/config/services/{name} ────────────────────────────────────

    async def delete_service(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.match_info["name"]
        loop = asyncio.get_running_loop()

        def _do_delete():
            with _write_lock:
                config = read_config(config_path)

                if name not in config.services:
                    raise KeyError(f"Service not found: {name}")

                # Dependency check: other services whose `after` list contains this name
                dependents = [
                    svc_name
                    for svc_name, svc_cfg in config.services.items()
                    if svc_name != name and name in svc_cfg.after
                ]
                if dependents:
                    raise ValueError(
                        f"Cannot delete service '{name}': referenced by {dependents}"
                    )

                # Stop service if currently running
                if runner.process_manager.is_running(name):
                    runner.process_manager.stop_service(name)

                del config.services[name]

                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))

                _commit_config(config_path, config, runner)

        try:
            await loop.run_in_executor(None, _do_delete)
            return _json_response({"ok": True})
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

    async def put_repo(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.match_info["name"]
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

            with _write_lock:
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
            return _json_response({"ok": True})
        except KeyError as e:
            return _error(str(e), status=404)
        except (ValueError, RuntimeError) as e:
            return _error(str(e), status=400)
        except Exception as e:
            logger.error("PUT /api/config/repos/%s failed: %s", name, e)
            return _error(str(e), status=500)

    # ── POST /api/config/repos ────────────────────────────────────────────────

    async def post_repo(request: web.Request) -> web.Response:
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
                new_repo = RepoConfig.model_validate(repo_data)
            except ValidationError as e:
                raise ValueError(str(e)) from e

            with _write_lock:
                config = read_config(config_path)

                if repo_name in config.repos:
                    raise ValueError(f"Repo already exists: {repo_name}")

                config.repos[repo_name] = new_repo

                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))

                _commit_config(config_path, config, runner)

        try:
            await loop.run_in_executor(None, _do_post)
            return _json_response({"ok": True})
        except ValueError as e:
            return _error(str(e), status=400)
        except RuntimeError as e:
            return _error(str(e), status=500)
        except Exception as e:
            logger.error("POST /api/config/repos failed: %s", e)
            return _error(str(e), status=500)

    # ── DELETE /api/config/repos/{name} ───────────────────────────────────────

    async def delete_repo(request: web.Request) -> web.Response:
        config_path = _get_config_path()
        if not config_path:
            return _error("config_path not set", status=501)

        name = request.match_info["name"]
        loop = asyncio.get_running_loop()

        def _do_delete():
            with _write_lock:
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
            return _json_response({"ok": True})
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
        web.get("/api/config", get_config),
        web.get("/api/config/services", get_config_services),
        web.get("/api/config/repos", get_config_repos),
        web.put("/api/config/services/{name}", put_service),
        web.post("/api/config/services", post_service),
        web.delete("/api/config/services/{name}", delete_service),
        web.put("/api/config/repos/{name}", put_repo),
        web.post("/api/config/repos", post_repo),
        web.delete("/api/config/repos/{name}", delete_repo),
    ]
