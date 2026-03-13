"""
Install-mode MCP server for haniel.

A minimal MCP server that runs during installation to allow Claude Code
to interact with the install process through MCP tools.

This is separate from the main MCP server (mcp_server.py) which runs
during normal operation with ServiceRunner.
"""

import asyncio
import logging
import threading
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .interactive import InteractiveInstaller

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_MCP_PORT = 3201


class InstallMcpServer:
    """MCP server for install mode.

    Exposes install-specific tools for Claude Code to use during
    interactive configuration collection.
    """

    def __init__(
        self,
        installer: "InteractiveInstaller",
        port: int = DEFAULT_INSTALL_MCP_PORT,
    ):
        """Initialize the install MCP server.

        Args:
            installer: InteractiveInstaller instance for tool callbacks
            port: Port to run the server on
        """
        self.installer = installer
        self.port = port
        self._app_runner: Optional[Any] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._server_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def list_tools(self) -> list[dict[str, Any]]:
        """List available MCP tools for install mode.

        Returns:
            List of tool definitions
        """
        return self.installer.get_mcp_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by name.

        Args:
            name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result as string
        """
        return await self.installer.call_mcp_tool(name, arguments)

    async def start(self) -> None:
        """Start the MCP server.

        This starts an SSE-based MCP server on the configured port.
        """
        try:
            from mcp.server import Server
            from mcp.server.sse import SseServerTransport
            from mcp.types import Tool, TextContent
            from aiohttp import web

            # Create stop event for graceful shutdown
            self._stop_event = asyncio.Event()

            # Create MCP server
            mcp = Server("haniel-install")

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
            app.router.add_route("GET", "/sse", sse.handle_sse)
            app.router.add_route("POST", "/sse", sse.handle_post_message)

            # Add health check endpoint
            async def health_handler(request):
                return web.json_response({"status": "ok", "mode": "install"})

            app.router.add_route("GET", "/health", health_handler)

            # Start server
            runner = web.AppRunner(app)
            await runner.setup()
            self._app_runner = runner

            site = web.TCPSite(runner, "0.0.0.0", self.port)
            await site.start()

            logger.info(f"Install MCP server started on port {self.port}")

            # Wait until stop event is set
            try:
                await self._stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                await runner.cleanup()
                logger.info("Install MCP server cleaned up")

        except ImportError as e:
            logger.warning(f"MCP dependencies not available: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to start install MCP server: {e}")
            raise

    async def stop(self) -> None:
        """Stop the MCP server."""
        if self._stop_event:
            self._stop_event.set()

        if self._app_runner:
            try:
                await self._app_runner.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up install MCP server: {e}")
            self._app_runner = None

        logger.info("Install MCP server stopped")

    def start_background(self) -> None:
        """Start the MCP server in a background thread.

        This allows the main thread to continue running Claude Code.
        """

        def run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self.start())
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Install MCP server error: {e}")
            finally:
                self._loop.close()

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()

        # Wait a moment for the server to start
        import time

        time.sleep(0.5)

        logger.info("Install MCP server started in background thread")

    def stop_background(self) -> None:
        """Stop the background MCP server.

        Call this from the main thread to stop the server running
        in the background thread.
        """
        if self._stop_event and self._loop:
            # Signal the stop event from the main thread
            self._loop.call_soon_threadsafe(self._stop_event.set)

            # Wait for thread to finish
            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=5.0)
                if self._server_thread.is_alive():
                    logger.warning("Install MCP server thread did not stop cleanly")

        logger.info("Install MCP server stopped from main thread")

    def is_running(self) -> bool:
        """Check if the server is running.

        Returns:
            True if the server is running
        """
        return self._server_thread is not None and self._server_thread.is_alive()
