"""
MCP server for haniel.

Provides Claude Code integration through the Model Context Protocol:
- Resources: status, repos, logs (read-only queries)
- Tools: restart, stop, start, pull, enable, reload (control operations)

haniel doesn't care what queries it - it just exposes its state and accepts commands
through a standardized MCP interface.
"""

import asyncio
import json
import logging
import threading
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
        self._mcp_app: Optional[Any] = None
        self._app_runner: Optional[Any] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._server_thread: Optional[threading.Thread] = None

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

        Returns:
            List of resource definitions
        """
        return [
            {
                "uri": "haniel://status",
                "name": "Overall Status",
                "description": "Get overall status of haniel runner including all services and repos",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://status/{service}",
                "name": "Service Status",
                "description": "Get status of a specific service",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://repos",
                "name": "Repository Status",
                "description": "Get status of all tracked repositories",
                "mimeType": "application/json",
            },
            {
                "uri": "haniel://logs/{service}",
                "name": "Service Logs",
                "description": "Get recent logs for a service (use ?lines=N for count)",
                "mimeType": "text/plain",
            },
        ]

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
                "name": "haniel_approve_update",
                "description": "Approve a pending haniel self-update. Shuts down all services and restarts with updated code.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
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
            return await self._restart_service(arguments.get("service", ""))
        elif name == "haniel_stop":
            return await self._stop_service(arguments.get("service", ""))
        elif name == "haniel_start":
            return await self._start_service(arguments.get("service", ""))
        elif name == "haniel_pull":
            return await self._pull_repo(arguments.get("repo", ""))
        elif name == "haniel_enable":
            return await self._enable_service(arguments.get("service", ""))
        elif name == "haniel_reload":
            return await self._reload_config()
        elif name == "haniel_approve_update":
            return await self._approve_self_update()
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
            loop = asyncio.get_event_loop()

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
            loop = asyncio.get_event_loop()
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

        loop = asyncio.get_event_loop()
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

        Uses run_in_executor to avoid blocking the event loop.
        """
        if not repo:
            return json.dumps({"error": "Repository name is required"})

        if repo not in self._get_repo_names():
            return json.dumps({"error": f"Repository not found: {repo}"})

        try:
            loop = asyncio.get_event_loop()

            # Get affected services
            affected = await loop.run_in_executor(
                None, self.runner.get_affected_services, repo
            )

            # Stop affected services (reverse dependency order)
            shutdown_order = await loop.run_in_executor(
                None, self.runner.get_shutdown_order
            )
            shutdown_order = [s for s in shutdown_order if s in affected]

            for service in shutdown_order:
                is_running = await loop.run_in_executor(
                    None, self.runner.process_manager.is_running, service
                )
                if is_running:
                    await loop.run_in_executor(
                        None, self.runner.process_manager.stop_service, service
                    )

            # Pull the repo
            success = await loop.run_in_executor(None, self.runner._pull_repo, repo)
            if not success:
                return json.dumps({"error": f"Failed to pull repository '{repo}'"})

            # Restart affected services (startup order)
            startup_order = await loop.run_in_executor(
                None, self.runner.get_startup_order
            )
            startup_order = [s for s in startup_order if s in affected]

            for service in startup_order:
                await loop.run_in_executor(None, self.runner._start_service, service)

            return f"Success: Repository '{repo}' pulled, {len(affected)} service(s) restarted"
        except Exception as e:
            logger.error(f"Failed to pull {repo}: {e}")
            return json.dumps({"error": f"Failed to pull '{repo}': {e}"})

    async def _enable_service(self, service: str) -> str:
        """Reset circuit breaker for a service."""
        if not service:
            return json.dumps({"error": "Service name is required"})

        try:
            loop = asyncio.get_event_loop()
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
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.runner.reload_config)
            return "Success: Configuration reloaded"
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
            return json.dumps({"error": f"Failed to reload configuration: {e}"})

    async def _approve_self_update(self) -> str:
        """Approve a pending self-update.

        Delegates to runner.approve_self_update() which signals the main
        thread to exit with code 10 for the wrapper script to handle.
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.runner.approve_self_update)
        return result

    async def start(self) -> None:
        """Start the MCP server.

        This starts an SSE-based MCP server on the configured port.
        """
        if not self.enabled:
            logger.info("MCP server is disabled")
            return

        try:
            from mcp.server import Server
            from mcp.server.sse import SseServerTransport
            from mcp.types import Resource, Tool, TextContent
            from aiohttp import web

            # Create stop event for graceful shutdown
            self._stop_event = asyncio.Event()

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
            async def handle_read_resource(uri: str):
                content = await self.read_resource(uri)
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

            # Create SSE transport
            sse = SseServerTransport("/sse")

            # Create aiohttp app
            app = web.Application()
            # mcp >= 1.0 removed handle_sse (replaced with ASGI-based connect_sse).
            # Fall back to dashboard-only mode if SSE transport is unavailable.
            if hasattr(sse, "handle_sse"):
                app.router.add_route("GET", "/sse", sse.handle_sse)
                app.router.add_route("POST", "/sse", sse.handle_post_message)
            else:
                logger.warning(
                    "MCP SSE transport unavailable (mcp >= 1.0 removed handle_sse). "
                    "Starting in dashboard-only mode."
                )

            # Attach dashboard routes only when explicitly enabled in config
            dashboard_cfg = self.runner.config.dashboard
            if dashboard_cfg is not None and dashboard_cfg.enabled:
                try:
                    from ..dashboard import setup_dashboard
                    from ..core.claude_session import ClaudeSessionManager

                    loop = asyncio.get_event_loop()

                    # Initialise Claude session manager (None if claude not in PATH)
                    session_manager = None
                    try:
                        import os
                        import shutil
                        claude_path = shutil.which("claude")
                        if claude_path is None:
                            # Windows service PATH may not include user-local bin.
                            # CLAUDE_CLI_DIR can be set in haniel.yaml service.environment.
                            cli_dir = os.environ.get("CLAUDE_CLI_DIR")
                            if cli_dir:
                                for name in ("claude.cmd", "claude"):
                                    candidate = os.path.join(cli_dir, name)
                                    if os.path.exists(candidate):
                                        claude_path = candidate
                                        break
                        if claude_path is not None:
                            session_manager = ClaudeSessionManager(self.runner)
                        else:
                            logger.warning("claude CLI not found — chat panel disabled. "
                                           "Set CLAUDE_CLI_DIR in haniel.yaml service.environment.")
                    except Exception as sm_err:
                        logger.warning("Failed to initialise ClaudeSessionManager: %s", sm_err)

                    ws_handler = setup_dashboard(
                        app,
                        self.runner,
                        loop,
                        token=dashboard_cfg.token,
                        claude_session_manager=session_manager,
                    )
                    self.runner._ws_handler = ws_handler
                    logger.info("Dashboard routes attached to MCP server")
                except Exception as e:
                    logger.warning(f"Failed to set up dashboard: {e}")

            # Store for shutdown
            self._mcp_app = app

            # Start server
            runner = web.AppRunner(app)
            await runner.setup()
            self._app_runner = runner

            site = web.TCPSite(runner, "0.0.0.0", self.port)
            await site.start()

            logger.info(f"MCP server started on port {self.port}")

            # Wait until stop event is set
            try:
                await self._stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                await runner.cleanup()
                logger.info("MCP server cleaned up")

        except ImportError as e:
            logger.warning(f"MCP dependencies not available: {e}")
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            raise

    async def stop(self) -> None:
        """Stop the MCP server."""
        if self._stop_event:
            self._stop_event.set()

        if self._app_runner:
            try:
                await self._app_runner.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up MCP server: {e}")
            self._app_runner = None

        logger.info("MCP server stopped")

    def stop_sync(self) -> None:
        """Stop the MCP server synchronously.

        Used when the ServiceRunner stops.
        """
        if self._stop_event:
            # Signal the async stop event from sync code
            loop = None
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                pass

            if loop and loop.is_running():
                # Schedule stop on the running loop
                loop.call_soon_threadsafe(self._stop_event.set)
            else:
                # Create new event loop for cleanup
                try:
                    asyncio.run(self.stop())
                except Exception as e:
                    logger.warning(f"Error stopping MCP server: {e}")

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
