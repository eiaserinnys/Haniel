"""MCP authentication on the assembled server."""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from haniel.config import DashboardConfig, HanielConfig, McpConfig
from haniel.core.runner import ServiceRunner
from haniel.integrations.mcp_server import HanielMcpServer


@pytest.mark.parametrize("dashboard_enabled", [True, False])
async def test_mcp_bearer_auth(tmp_path, dashboard_enabled):
    config = HanielConfig(
        mcp=McpConfig(),
        dashboard=DashboardConfig(enabled=dashboard_enabled, token="${secret:MCP}"),
    )
    config._secret_values = {"MCP": "resolved-test-token"}
    runner = ServiceRunner(config, config_dir=tmp_path)
    with (
        patch("uvicorn.Server") as server,
        patch("haniel.core.claude_session.ClaudeSessionManager"),
    ):
        server.return_value.serve = AsyncMock()
        await HanielMcpServer(runner).start()
        app = server.call_args.args[0].app

    with TestClient(app) as client:
        assert client.get("/mcp", follow_redirects=False).status_code == 401
        assert client.get("/mcp/extra").status_code == 401
        assert client.get("/mcpx").status_code != 401
        assert client.get("/mcp", headers={"Authorization": "Basic wrong"}).status_code == 401
        assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 403
        assert client.get(
            "/mcp", headers={"Authorization": "Bearer resolved-test-token"}
        ).status_code not in (401, 403)


async def test_mcp_without_token_passes_through(tmp_path):
    runner = ServiceRunner(HanielConfig(mcp=McpConfig()), config_dir=tmp_path)
    with patch("uvicorn.Server") as server:
        server.return_value.serve = AsyncMock()
        await HanielMcpServer(runner).start()
        app = server.call_args.args[0].app
    with TestClient(app) as client:
        assert client.get("/mcp").status_code not in (401, 403)
