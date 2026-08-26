"""Executable compatibility probe for Haniel's low-level MCP wiring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def validate_mcp_runtime_compatibility(
    *,
    server_factory: Callable[..., Any] | None = None,
    session_manager_factory: Callable[..., Any] | None = None,
) -> None:
    """Register every low-level handler shape and build the HTTP manager."""
    if server_factory is None:
        from mcp.server import Server

        server_factory = Server
    if session_manager_factory is None:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        session_manager_factory = StreamableHTTPSessionManager

    server = server_factory("haniel-compatibility-probe")

    async def list_resources() -> list[Any]:
        return []

    async def read_resource(uri: Any) -> str:
        return ""

    async def list_tools() -> list[Any]:
        return []

    async def call_tool(name: str, arguments: dict[str, Any]) -> list[Any]:
        return []

    server.list_resources()(list_resources)
    server.read_resource()(read_resource)
    server.list_tools()(list_tools)
    server.call_tool()(call_tool)
    session_manager_factory(app=server, json_response=True, stateless=False)


def main() -> None:
    """Exercise CLI import and MCP wiring inside a prepared release venv."""
    from haniel.cli import main as cli_main

    assert callable(cli_main)
    validate_mcp_runtime_compatibility()


if __name__ == "__main__":
    main()
