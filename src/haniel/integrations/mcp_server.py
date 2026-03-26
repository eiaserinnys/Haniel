"""
MCP server for haniel.

Provides Claude Code integration through the Model Context Protocol:
- Resources: status, repos, logs (read-only queries)
- Tools: restart, stop, start, pull, enable, reload (control operations)

haniel doesn't care what queries it - it just exposes its state and accepts commands
through a standardized MCP interface.
"""

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING, Optional

from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner

logger = logging.getLogger(__name__)

# Default values
DEFAULT_MCP_PORT = 3200
DEFAULT_MCP_ENABLED = True
MAX_LOG_LINES = 10000


class HanielMcpServer:
    """MCP server for haniel service runner.

    Exposes haniel functionality through MCP resources and tools,
    allowing Claude Code to query status and control services.
    """

    def __init__(self, runner: "ServiceRunner"):
        """Initialize the MCP server.

        Args:
            runner: ServiceRunner instance to expose via MCP
        """
        self.runner = runner
        self._server: Optional[Any] = None  # uvicorn.Server
        self._server_thread: Optional[threading.Thread] = None
        self._session_manager: Optional[Any] = None

    @property
    def port(self) -> int:
        """Get the MCP server port from config."""
        if self.runner.config.mcp:
            return self.runner.config.mcp.port
        return DEFAULT_MCP_PORT

    @property
    def enabled(self) -> bool:
        """Check if MCP server is enabled."""
        if self.runner.config.mcp:
            return self.runner.config.mcp.enabled
        return DEFAULT_MCP_ENABLED

    def list_resources(self) -> list[dict[str, Any]]:
        """List available MCP resources.

        Returns dynamically generated per-service URIs so callers can
        discover available services without prior knowledge.
        """
        resources = [
            {
                "uri": "haniel://status",
                "name": "Overall Status",
                "description": "Get overall status of haniel runner including all services and repos",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://config",
                "name": "Configuration",
                "description": "Full haniel.yaml configuration",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://config/services",
                "name": "Service Configs",
                "description": "All service configurations from haniel.yaml",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://config/repos",
                "name": "Repo Configs",
                "description": "All repository configurations from haniel.yaml",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://repos",
                "name": "Repository Status",
                "description": "Get status of all tracked repositories",
                "mimeType": "application/json",
            },
        ]

        # Dynamic per-service resources
        try:
            status = self.runner.get_status()
            service_names = sorted(status.get("services", {}).keys())
        except Exception:
            service_names = []

        for name in service_names:
            resources.append(
                {
                    "uri": f"haniel://status/{name}",
                    "name": f"Service: {name}",
                    "description": f"Status of {name} service",
                    "mimeType": "application/json",
                }
            )
            resources.append(
                {
                    "uri": f"haniel://logs/{name}",
                    "name": f"Logs: {name}",
                    "description": f"Recent 50 lines of {name} logs",
                    "mimeType": "text/plain",
                }
            )

        return resources

    def list_tools(self) -> list[dict[str, Any]]:
        """List available MCP tools.

        Returns:
            List of tool definitions
        """
        return [
            {
                "name": "haniel_restart",
                "description": "Restart a service (stop + start)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Name of the service to restart",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_stop",
                "description": "Stop a service",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Name of the service to stop",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_start",
                "description": "Start a service",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Name of the service to start",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_pull",
                "description": "Pull a repository and restart dependent services",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "Name of the repository to pull",
                        },
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "haniel_enable",
                "description": "Reset circuit breaker for a service (re-enable after repeated failures)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Name of the service to enable",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_reload",
                "description": "Reload haniel.yaml configuration (processes continue running)",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "haniel_update",
                "description": "Update a service (git pull + restart). For service='haniel', pulls haniel repo and restarts itself.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Name of the service to update. Use 'haniel' for self-update.",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_check_updates",
                "description": "Check all repos (including haniel itself) for pending changes. Results are from last poll snapshot — may be up to poll_interval seconds stale.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "haniel_read_logs",
                "description": "Read service logs with line count and optional grep filter",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Name of the service",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "Number of lines to return (default 100, max 1000)",
                        },
                        "grep": {
                            "type": "string",
                            "description": "Filter lines containing this pattern (case-insensitive)",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_update_service_config",
                "description": "Update an existing service configuration in haniel.yaml. Validates, backs up, writes atomically, and reloads.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string", "description": "Service name"},
                        "config": {
                            "type": "object",
                            "description": "Full service config object (run, cwd, repo, ready, etc.)",
                        },
                    },
                    "required": ["service", "config"],
                },
            },
            {
                "name": "haniel_create_service_config",
                "description": "Add a new service to haniel.yaml.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "New service name"},
                        "config": {
                            "type": "object",
                            "description": "Service config object",
                        },
                    },
                    "required": ["name", "config"],
                },
            },
            {
                "name": "haniel_delete_service_config",
                "description": "Remove a service from haniel.yaml. Fails if other services depend on it.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name to delete",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "haniel_update_repo_config",
                "description": "Update an existing repo configuration in haniel.yaml.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repo name"},
                        "config": {
                            "type": "object",
                            "description": "Full repo config object (url, branch, path, hooks)",
                        },
                    },
                    "required": ["repo", "config"],
                },
            },
            {
                "name": "haniel_create_repo_config",
                "description": "Add a new repo to haniel.yaml.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "New repo name"},
                        "config": {
                            "type": "object",
                            "description": "Repo config object",
                        },
                    },
                    "required": ["name", "config"],
                },
            },
            {
                "name": "haniel_delete_repo_config",
                "description": "Remove a repo from haniel.yaml. Fails if services reference it.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "Repo name to delete",
                        },
                    },
                    "required": ["repo"],
                },
            },
        ]

    async def read_resource(self, uri: str) -> str:
        """Read a resource by URI.

        Args:
            uri: Resource URI (e.g., haniel://status, haniel://logs/web?lines=50)

        Returns:
            Resource content as string
        """
        parsed = urlparse(uri)

        if parsed.scheme != "haniel":
            return json.dumps({"error": f"Unknown scheme: {parsed.scheme}"})

        path = parsed.netloc + parsed.path
        query = parse_qs(parsed.query)

        # haniel://config
        if path == "config":
            return await self._get_config()
        if path == "config/services":
            return await self._get_config_services()
        if path == "config/repos":
            return await self._get_config_repos()

        # haniel://status
        if path == "status":
            return await self._get_overall_status()

        # haniel://status/{service}
        if path.startswith("status/"):
            service = path[7:]  # Remove "status/"
            return await self._get_service_status(service)

        # haniel://repos
        if path == "repos":
            return await self._get_repos_status()

        # haniel://logs/{service}
        if path.startswith("logs/"):
            service = path[5:]  # Remove "logs/"
            # Validate and bound the lines parameter
            lines_raw = query.get("lines", ["50"])[0]
            try:
                lines = int(lines_raw)
                lines = max(1, min(lines, MAX_LOG_LINES))
            except (ValueError, TypeError):
                lines = 50
            return await self._get_service_logs(service, lines)

        return json.dumps({"error": f"Unknown resource: {uri}"})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by name.

        Args:
            name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result as string
        """
        if name == "haniel_restart":
            service = arguments.get("service", "")
            if service == "haniel":
                return await self._self_restart()
            return await self._restart_service(service)
        elif name == "haniel_stop":
            service = arguments.get("service", "")
            if service == "haniel":
                return "Error: Cannot stop haniel itself via MCP"
            return await self._stop_service(service)
        elif name == "haniel_start":
            return await self._start_service(arguments.get("service", ""))
        elif name == "haniel_pull":
            return await self._pull_repo(arguments.get("repo", ""))
        elif name == "haniel_enable":
            return await self._enable_service(arguments.get("service", ""))
        elif name == "haniel_reload":
            return await self._reload_config()
        elif name == "haniel_update":
            return await self._update_service(arguments)
        elif name == "haniel_check_updates":
            return await self._check_updates()
        elif name == "haniel_read_logs":
            return await self._read_logs_tool(arguments)
        elif name == "haniel_update_service_config":
            return await self._update_service_config(arguments)
        elif name == "haniel_create_service_config":
            return await self._create_service_config(arguments)
        elif name == "haniel_delete_service_config":
            return await self._delete_service_config(arguments)
        elif name == "haniel_update_repo_config":
            return await self._update_repo_config(arguments)
        elif name == "haniel_create_repo_config":
            return await self._create_repo_config(arguments)
        elif name == "haniel_delete_repo_config":
            return await self._delete_repo_config(arguments)
        else:
            return f"Error: Unknown tool '{name}'"

    # Resource handlers

    async def _get_overall_status(self) -> str:
        """Get overall runner status."""
        status = self.runner.get_status()
        return json.dumps(status, indent=2)

    async def _get_service_status(self, service: str) -> str:
        """Get status of a specific service.

        Uses thread-safe get_status() to avoid race conditions.
        """
        # Use thread-safe get_status() method
        status = self.runner.get_status()
        services = status.get("services", {})

        if service not in services:
            return json.dumps({"error": f"Service not found: {service}"})

        return json.dumps({"service": service, **services[service]}, indent=2)

    async def _get_repos_status(self) -> str:
        """Get status of all repositories."""
        status = self.runner.get_status()
        return json.dumps(status.get("repos", {}), indent=2)

    async def _get_service_logs(self, service: str, lines: int = 50) -> str:
        """Get recent logs for a service."""
        log_lines = self.runner.process_manager.log_manager.get_log_tail(service, lines)
        return "\n".join(log_lines)

    async def _read_logs_tool(self, arguments: dict[str, Any]) -> str:
        """Read service logs with line count and optional grep filter."""
        service = arguments["service"]
        lines = min(arguments.get("lines", 100), 1000)
        grep_pattern = arguments.get("grep")

        log_lines = self.runner.process_manager.log_manager.get_log_tail(service, lines)
        if grep_pattern:
            log_lines = [
                line for line in log_lines if grep_pattern.lower() in line.lower()
            ]
        return json.dumps(
            {"service": service, "lines": log_lines, "count": len(log_lines)}
        )

    # Tool handlers

    def _get_service_names(self) -> set[str]:
        """Get enabled service names in a thread-safe way."""
        status = self.runner.get_status()
        return set(status.get("services", {}).keys())

    def _get_repo_names(self) -> set[str]:
        """Get repo names in a thread-safe way."""
        status = self.runner.get_status()
        return set(status.get("repos", {}).keys())

    async def _restart_service(self, service: str) -> str:
        """Restart a service.

        Uses run_in_executor to avoid blocking the event loop.
        """
        if not service:
            return json.dumps({"error": "Service name is required"})

        if service not in self._get_service_names():
            return json.dumps({"error": f"Service not found: {service}"})

        try:
            loop = asyncio.get_running_loop()

            # Stop the service
            is_running = await loop.run_in_executor(
                None, self.runner.process_manager.is_running, service
            )
            if is_running:
                await loop.run_in_executor(
                    None, self.runner.process_manager.stop_service, service
                )

            # Start the service
            await loop.run_in_executor(None, self.runner._start_service, service)
            return f"Success: Service '{service}' restarted"
        except Exception as e:
            logger.error(f"Failed to restart {service}: {e}")
            return json.dumps({"error": f"Failed to restart '{service}': {e}"})

    async def _stop_service(self, service: str) -> str:
        """Stop a service.

        Uses run_in_executor to avoid blocking the event loop.
        """
        if not service:
            return json.dumps({"error": "Service name is required"})

        if service not in self._get_service_names():
            return json.dumps({"error": f"Service not found: {service}"})

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self.runner.process_manager.stop_service, service
            )
            return f"Success: Service '{service}' stopped"
        except Exception as e:
            logger.error(f"Failed to stop {service}: {e}")
            return json.dumps({"error": f"Failed to stop '{service}': {e}"})

    async def _start_service(self, service: str) -> str:
        """Start a service.

        Uses run_in_executor to avoid blocking the event loop.
        """
        if not service:
            return json.dumps({"error": "Service name is required"})

        if service not in self._get_service_names():
            return json.dumps({"error": f"Service not found: {service}"})

        loop = asyncio.get_running_loop()
        is_running = await loop.run_in_executor(
            None, self.runner.process_manager.is_running, service
        )
        if is_running:
            return f"Warning: Service '{service}' is already running"

        try:
            await loop.run_in_executor(None, self.runner._start_service, service)
            return f"Success: Service '{service}' started"
        except Exception as e:
            logger.error(f"Failed to start {service}: {e}")
            return json.dumps({"error": f"Failed to start '{service}': {e}"})

    async def _pull_repo(self, repo: str) -> str:
        """Pull a repository and restart dependent services.

        Delegates to runner.trigger_pull(), which is the single canonical pull path
        shared by all triggers (Dashboard, Slack button, auto_apply, MCP).
        This ensures the is_pulling guard, Slack notifications, WebSocket broadcasts,
        and post_pull hooks are all applied consistently.
        """
        if not repo:
            return json.dumps({"error": "Repository name is required"})

        if repo not in self._get_repo_names():
            return json.dumps({"error": f"Repository not found: {repo}"})

        try:
            # Pre-fetch affected service count for the return message.
            # trigger_pull internally calls get_affected_services again, but
            # a discrepancy only affects the count in the message, not behavior.
            affected = await asyncio.to_thread(self.runner.get_affected_services, repo)

            # trigger_pull handles: is_pulling guard, Slack notify, WebSocket broadcast,
            # service stop/start, and post_pull hooks.
            # If is_pulling is already True, trigger_pull returns silently (no exception),
            # so MCP returns a success message (duplicate-pull prevention is the intent).
            await asyncio.to_thread(self.runner.trigger_pull, repo)

            return f"Success: Repository '{repo}' pulled, {len(affected)} service(s) restarted"
        except Exception as e:
            logger.error(f"Failed to pull {repo}: {e}")
            return json.dumps({"error": f"Failed to pull '{repo}': {e}"})

    async def _enable_service(self, service: str) -> str:
        """Reset circuit breaker for a service."""
        if not service:
            return json.dumps({"error": "Service name is required"})

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self.runner.health_manager.reset_circuit, service
            )
            return f"Success: Circuit breaker reset for '{service}', service enabled"
        except Exception as e:
            logger.error(f"Failed to enable {service}: {e}")
            return json.dumps({"error": f"Failed to enable '{service}': {e}"})

    async def _reload_config(self) -> str:
        """Reload configuration."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.runner.reload_config)
            return "Success: Configuration reloaded"
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
            return json.dumps({"error": f"Failed to reload configuration: {e}"})

    async def _update_service(self, arguments: dict[str, Any]) -> str:
        """Update a service: pull its repo, then restart."""
        service = arguments.get("service", "")
        if service == "haniel":
            return await self._self_update()

        # Find repo for this service
        svc_names = self._get_service_names()
        if service not in svc_names:
            return f"Error: Service not found: {service}"

        status = self.runner.get_status()
        svc_info = status.get("services", {}).get(service, {})
        repo_name = svc_info.get("config", {}).get("repo")
        if not repo_name:
            return f"Error: Service '{service}' has no associated repo"

        # Pull
        loop = asyncio.get_running_loop()
        pull_ok = await loop.run_in_executor(None, self.runner._pull_repo, repo_name)
        if not pull_ok:
            return f"Error: Pull failed for repo '{repo_name}'"

        # Restart
        return await self._restart_service(service)

    async def _self_update(self) -> str:
        """Self-update: pull haniel repo, then deferred exit(10)."""
        loop = asyncio.get_running_loop()

        # Pull haniel's own repo first
        self_repo = getattr(self.runner, "_self_repo", None)
        if not self_repo:
            return "Error: No self repo configured"

        pull_ok = await loop.run_in_executor(None, self.runner._pull_repo, self_repo)
        if not pull_ok:
            return f"Error: Pull failed for haniel repo '{self_repo}'"

        # Signal self-update and deferred stop
        self.runner._self_update_requested.set()

        async def _deferred_stop():
            await asyncio.sleep(0.5)
            await loop.run_in_executor(None, self.runner.stop)

        asyncio.ensure_future(_deferred_stop())
        return "Self-update: pull succeeded, restarting haniel..."

    async def _check_updates(self) -> str:
        """Check all repos for pending changes."""
        status = self.runner.get_status()
        repos = status.get("repos", {})
        updates = {}
        for name, repo in repos.items():
            if repo.get("pending_changes"):
                updates[name] = repo["pending_changes"]
        return json.dumps(
            {
                "repos_with_updates": updates,
                "total": len(updates),
                "note": f"Snapshot from last poll (interval={status.get('poll_interval', '?')}s)",
            },
            indent=2,
            ensure_ascii=False,
        )

    # --- Config resource handlers ---

    async def _get_config(self) -> str:
        from ..dashboard.config_io import read_config

        config = read_config(self.runner.config_path)
        return json.dumps(
            config.model_dump(by_alias=True, mode="json"), indent=2, ensure_ascii=False
        )

    async def _get_config_services(self) -> str:
        from ..dashboard.config_io import read_config

        config = read_config(self.runner.config_path)
        data = config.model_dump(by_alias=True, mode="json")
        return json.dumps(data.get("services", {}), indent=2, ensure_ascii=False)

    async def _get_config_repos(self) -> str:
        from ..dashboard.config_io import read_config

        config = read_config(self.runner.config_path)
        data = config.model_dump(by_alias=True, mode="json")
        return json.dumps(data.get("repos", {}), indent=2, ensure_ascii=False)

    # --- Config CRUD tool handlers ---

    _config_lock = None  # Lazy-init threading.Lock

    def _get_config_lock(self):
        if self._config_lock is None:
            import threading

            self._config_lock = threading.Lock()
        return self._config_lock

    async def _update_service_config(self, arguments: dict[str, Any]) -> str:
        from ..dashboard.config_io import (
            read_config,
            write_config,
            backup_config,
            restore_config,
        )
        from ..config.model import ServiceConfig
        from ..config.validators import validate_config

        service = arguments["service"]
        config_data = arguments["config"]
        config_path = self.runner.config_path
        loop = asyncio.get_running_loop()

        def _do():
            new_svc = ServiceConfig.model_validate(config_data)
            with self._get_config_lock():
                config = read_config(config_path)
                if service not in config.services:
                    raise KeyError(f"Service not found: {service}")
                config.services[service] = new_svc
                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))
                backup_config(config_path)
                try:
                    write_config(config_path, config)
                except Exception:
                    restore_config(config_path)
                    raise
                self.runner.reload_config()

        try:
            await loop.run_in_executor(None, _do)
            return json.dumps({"ok": True})
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})

    async def _create_service_config(self, arguments: dict[str, Any]) -> str:
        from ..dashboard.config_io import (
            read_config,
            write_config,
            backup_config,
            restore_config,
        )
        from ..config.model import ServiceConfig
        from ..config.validators import validate_config

        name = arguments["name"]
        config_data = arguments["config"]
        config_path = self.runner.config_path
        loop = asyncio.get_running_loop()

        def _do():
            new_svc = ServiceConfig.model_validate(config_data)
            with self._get_config_lock():
                config = read_config(config_path)
                if name in config.services:
                    raise ValueError(f"Service already exists: {name}")
                config.services[name] = new_svc
                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))
                backup_config(config_path)
                try:
                    write_config(config_path, config)
                except Exception:
                    restore_config(config_path)
                    raise
                self.runner.reload_config()

        try:
            await loop.run_in_executor(None, _do)
            return json.dumps({"ok": True})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    async def _delete_service_config(self, arguments: dict[str, Any]) -> str:
        from ..dashboard.config_io import (
            read_config,
            write_config,
            backup_config,
            restore_config,
        )
        from ..config.validators import validate_config

        service = arguments["service"]
        config_path = self.runner.config_path
        loop = asyncio.get_running_loop()

        def _do():
            with self._get_config_lock():
                config = read_config(config_path)
                if service not in config.services:
                    raise KeyError(f"Service not found: {service}")
                dependents = [
                    n
                    for n, s in config.services.items()
                    if n != service and service in s.after
                ]
                if dependents:
                    raise ValueError(f"Cannot delete: referenced by {dependents}")
                del config.services[service]
                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))
                backup_config(config_path)
                try:
                    write_config(config_path, config)
                except Exception:
                    restore_config(config_path)
                    raise
                self.runner.reload_config()

        try:
            await loop.run_in_executor(None, _do)
            return json.dumps({"ok": True})
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})

    async def _update_repo_config(self, arguments: dict[str, Any]) -> str:
        from ..dashboard.config_io import (
            read_config,
            write_config,
            backup_config,
            restore_config,
        )
        from ..config.model import RepoConfig
        from ..config.validators import validate_config

        repo = arguments["repo"]
        config_data = arguments["config"]
        config_path = self.runner.config_path
        loop = asyncio.get_running_loop()

        def _do():
            new_repo = RepoConfig.model_validate(config_data)
            with self._get_config_lock():
                config = read_config(config_path)
                if repo not in config.repos:
                    raise KeyError(f"Repo not found: {repo}")
                config.repos[repo] = new_repo
                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))
                backup_config(config_path)
                try:
                    write_config(config_path, config)
                except Exception:
                    restore_config(config_path)
                    raise
                self.runner.reload_config()

        try:
            await loop.run_in_executor(None, _do)
            return json.dumps({"ok": True})
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})

    async def _create_repo_config(self, arguments: dict[str, Any]) -> str:
        from ..dashboard.config_io import (
            read_config,
            write_config,
            backup_config,
            restore_config,
        )
        from ..config.model import RepoConfig
        from ..config.validators import validate_config

        name = arguments["name"]
        config_data = arguments["config"]
        config_path = self.runner.config_path
        loop = asyncio.get_running_loop()

        def _do():
            new_repo = RepoConfig.model_validate(config_data)
            with self._get_config_lock():
                config = read_config(config_path)
                if name in config.repos:
                    raise ValueError(f"Repo already exists: {name}")
                config.repos[name] = new_repo
                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))
                backup_config(config_path)
                try:
                    write_config(config_path, config)
                except Exception:
                    restore_config(config_path)
                    raise
                self.runner.reload_config()

        try:
            await loop.run_in_executor(None, _do)
            return json.dumps({"ok": True})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    async def _delete_repo_config(self, arguments: dict[str, Any]) -> str:
        from ..dashboard.config_io import (
            read_config,
            write_config,
            backup_config,
            restore_config,
        )
        from ..config.validators import validate_config

        repo = arguments["repo"]
        config_path = self.runner.config_path
        loop = asyncio.get_running_loop()

        def _do():
            with self._get_config_lock():
                config = read_config(config_path)
                if repo not in config.repos:
                    raise KeyError(f"Repo not found: {repo}")
                using = [n for n, s in config.services.items() if s.repo == repo]
                if using:
                    raise ValueError(f"Cannot delete: used by services {using}")
                del config.repos[repo]
                errors = validate_config(config)
                if errors:
                    raise ValueError(str(errors[0]))
                backup_config(config_path)
                try:
                    write_config(config_path, config)
                except Exception:
                    restore_config(config_path)
                    raise
                self.runner.reload_config()

        try:
            await loop.run_in_executor(None, _do)
            return json.dumps({"ok": True})
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})

    async def _self_restart(self) -> str:
        """Restart haniel without performing a git update.

        Delegates to runner.request_restart() which signals the main
        thread to exit with code 11 for the wrapper script to handle.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self.runner.request_restart)
        return result

    async def start(self) -> None:
        """Start the MCP server.

        This starts a Starlette + uvicorn server with MCP Streamable HTTP transport.
        """
        if not self.enabled:
            logger.info("MCP server is disabled")
            return

        try:
            from mcp.server import Server
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
            from mcp.types import Resource, Tool, TextContent
            from starlette.applications import Starlette
            from starlette.routing import Mount
            import uvicorn

            # Create MCP server
            mcp = Server("haniel")

            # Register resource list handler
            @mcp.list_resources()
            async def handle_list_resources():
                resources = []
                for r in self.list_resources():
                    resources.append(
                        Resource(
                            uri=r["uri"],
                            name=r["name"],
                            description=r.get("description"),
                            mimeType=r.get("mimeType"),
                        )
                    )
                return resources

            # Register resource read handler
            @mcp.read_resource()
            async def handle_read_resource(uri):
                content = await self.read_resource(str(uri))
                return content

            # Register tool list handler
            @mcp.list_tools()
            async def handle_list_tools():
                tools = []
                for t in self.list_tools():
                    tools.append(
                        Tool(
                            name=t["name"],
                            description=t.get("description"),
                            inputSchema=t.get("inputSchema", {}),
                        )
                    )
                return tools

            # Register tool call handler
            @mcp.call_tool()
            async def handle_call_tool(name: str, arguments: dict):
                result = await self.call_tool(name, arguments)
                return [TextContent(type="text", text=result)]

            # Create StreamableHTTP session manager
            session_manager = StreamableHTTPSessionManager(
                app=mcp,
                json_response=True,
                stateless=True,
            )

            # Build routes
            all_routes = [
                Mount("/mcp", app=session_manager.handle_request),
            ]

            # Attach dashboard routes only when explicitly enabled in config
            dashboard_cfg = self.runner.config.dashboard
            ws_handler = None
            middleware = []
            if dashboard_cfg is not None and dashboard_cfg.enabled:
                try:
                    from ..dashboard import setup_dashboard
                    from ..core.claude_session import ClaudeSessionManager

                    # Initialise Claude session manager.
                    # The SDK bundles its own CLI, so no PATH detection needed.
                    sm = None
                    try:
                        sm = ClaudeSessionManager(self.runner)
                        self._session_manager = sm
                    except Exception as sm_err:
                        logger.warning(
                            "Failed to initialise ClaudeSessionManager: %s", sm_err
                        )

                    dashboard_routes, dashboard_middleware, ws_handler = (
                        setup_dashboard(
                            self.runner,
                            token=dashboard_cfg.token,
                            claude_session_manager=sm,
                            slack_bot=self.runner._slack_bot,
                        )
                    )
                    all_routes.extend(dashboard_routes)
                    middleware = dashboard_middleware

                    if ws_handler:
                        self.runner._ws_handler = ws_handler
                    logger.info("Dashboard routes attached to MCP server")
                except Exception as e:
                    logger.warning(f"Failed to set up dashboard: {e}")

            # Create lifespan for session manager
            @contextlib.asynccontextmanager
            async def lifespan(app: Starlette) -> AsyncIterator[None]:
                async with session_manager.run():
                    # Set up WebSocket event loop binding after the event loop is running
                    if ws_handler is not None:
                        loop = asyncio.get_running_loop()
                        ws_handler.setup(loop)
                    yield

            # Create Starlette app
            app = Starlette(
                routes=all_routes,
                middleware=middleware,
                lifespan=lifespan,
            )

            logger.info(f"MCP server starting on port {self.port}")

            # Start uvicorn server
            config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=self.port,
                log_level="warning",
            )
            self._server = uvicorn.Server(config)
            await self._server.serve()

        except ImportError as e:
            logger.warning(f"MCP dependencies not available: {e}")
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            raise

    async def stop(self) -> None:
        """Stop the MCP server."""
        # Disconnect all cached Claude SDK clients first
        if self._session_manager is not None:
            try:
                await self._session_manager.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down session manager: {e}")

        if self._server:
            self._server.should_exit = True

        logger.info("MCP server stopped")

    def stop_sync(self) -> None:
        """Stop the MCP server synchronously.

        Used when the ServiceRunner stops.
        """
        if self._server:
            self._server.should_exit = True

        logger.info("MCP server stop requested")

    def start_background(self) -> None:
        """Start the MCP server in a background thread.

        This is useful when integrating with synchronous code.
        """

        def run_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.start())
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"MCP server error: {e}")
            finally:
                loop.close()

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()
        logger.info("MCP server started in background thread")
