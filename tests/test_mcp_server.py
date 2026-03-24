"""
Tests for haniel MCP server.

Tests the MCP server that provides Claude Code integration:
- Resources: status, repos, logs
- Tools: restart, stop, start, pull, enable, reload
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from haniel.config import HanielConfig, McpConfig, ServiceConfig, RepoConfig
from haniel.core.health import ServiceState


class TestHanielMcpServer:
    """Test HanielMcpServer class."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="sse", port=3200),
            services={
                "web": ServiceConfig(run="python -m http.server"),
                "worker": ServiceConfig(run="python worker.py"),
            },
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo"),
            },
        )
        runner.config_dir = Path("/tmp/test")

        # Mock get_status
        runner.get_status.return_value = {
            "running": True,
            "start_time": "2026-02-28T10:00:00",
            "last_poll": "2026-02-28T14:00:00",
            "poll_count": 100,
            "poll_interval": 60,
            "services": {
                "web": {
                    "state": "running",
                    "uptime": 3600,
                    "restart_count": 0,
                    "consecutive_failures": 0,
                },
                "worker": {
                    "state": "running",
                    "uptime": 3600,
                    "restart_count": 0,
                    "consecutive_failures": 0,
                },
            },
            "repos": {
                "main": {
                    "path": "./repo",
                    "branch": "main",
                    "last_head": "abc12345",
                    "last_fetch": "2026-02-28T14:00:00",
                    "fetch_error": None,
                },
            },
        }

        # Mock health manager
        runner.health_manager = MagicMock()
        runner.health_manager.get_health.return_value = MagicMock(
            state=ServiceState.RUNNING,
            get_uptime=lambda: 3600,
            restart_count=0,
            consecutive_failures=0,
        )
        runner.health_manager.reset_circuit = MagicMock()

        # Mock process manager
        runner.process_manager = MagicMock()
        runner.process_manager.stop_service = MagicMock(return_value=True)
        runner.process_manager.is_running = MagicMock(return_value=True)
        runner.process_manager.log_manager = MagicMock()
        runner.process_manager.log_manager.get_log_tail = MagicMock(
            return_value=[
                "[14:00:00] [stdout] Server started",
                "[14:00:01] [stdout] Ready",
            ]
        )

        # Mock service methods
        runner._start_service = MagicMock(return_value=True)
        runner._pull_repo = MagicMock(return_value=True)
        runner.get_affected_services = MagicMock(return_value=["web"])
        runner.get_startup_order = MagicMock(return_value=["web", "worker"])
        runner.get_shutdown_order = MagicMock(return_value=["worker", "web"])

        # Mock enabled services
        runner._enabled_services = runner.config.services

        return runner

    @pytest.fixture
    def mcp_server(self, mock_runner):
        """Create HanielMcpServer instance."""
        from haniel.integrations.mcp_server import HanielMcpServer

        return HanielMcpServer(mock_runner)

    def test_server_creation(self, mcp_server, mock_runner):
        """Test MCP server can be created."""
        assert mcp_server is not None
        assert mcp_server.runner is mock_runner

    def test_get_port(self, mcp_server):
        """Test getting MCP port from config."""
        assert mcp_server.port == 3200


class TestMcpResources:
    """Test MCP resource handlers."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="sse", port=3200),
            services={
                "web": ServiceConfig(run="python -m http.server"),
            },
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo"),
            },
        )
        runner.config_dir = Path("/tmp/test")

        runner.get_status.return_value = {
            "running": True,
            "services": {
                "web": {"state": "running", "uptime": 3600},
            },
            "repos": {
                "main": {"path": "./repo", "branch": "main", "last_head": "abc12345"},
            },
        }

        runner.health_manager = MagicMock()
        runner.health_manager.get_health.return_value = MagicMock(
            state=ServiceState.RUNNING,
            get_uptime=lambda: 3600,
            restart_count=0,
            consecutive_failures=0,
        )

        runner.process_manager = MagicMock()
        runner.process_manager.log_manager = MagicMock()
        runner.process_manager.log_manager.get_log_tail = MagicMock(
            return_value=["[14:00:00] Log line 1", "[14:00:01] Log line 2"]
        )

        runner._enabled_services = runner.config.services

        return runner

    @pytest.fixture
    def mcp_server(self, mock_runner):
        """Create HanielMcpServer instance."""
        from haniel.integrations.mcp_server import HanielMcpServer

        return HanielMcpServer(mock_runner)

    @pytest.mark.asyncio
    async def test_read_status_resource(self, mcp_server):
        """Test reading haniel://status resource."""
        result = await mcp_server.read_resource("haniel://status")
        assert result is not None
        data = json.loads(result)
        assert data["running"] is True
        assert "services" in data

    @pytest.mark.asyncio
    async def test_read_status_service_resource(self, mcp_server, mock_runner):
        """Test reading haniel://status/{service} resource."""
        # Update mock to include service in status
        mock_runner.get_status.return_value = {
            "running": True,
            "services": {
                "web": {"state": "running", "uptime": 3600},
            },
            "repos": {},
        }
        result = await mcp_server.read_resource("haniel://status/web")
        assert result is not None
        data = json.loads(result)
        assert data["state"] == "running"

    @pytest.mark.asyncio
    async def test_read_status_unknown_service(self, mcp_server, mock_runner):
        """Test reading status for unknown service returns error."""
        mock_runner.get_status.return_value = {
            "running": True,
            "services": {},  # No services
            "repos": {},
        }
        result = await mcp_server.read_resource("haniel://status/unknown")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_read_repos_resource(self, mcp_server):
        """Test reading haniel://repos resource."""
        result = await mcp_server.read_resource("haniel://repos")
        assert result is not None
        data = json.loads(result)
        assert "main" in data
        assert data["main"]["path"] == "./repo"

    @pytest.mark.asyncio
    async def test_read_logs_resource(self, mcp_server, mock_runner):
        """Test reading haniel://logs/{service} resource."""
        result = await mcp_server.read_resource("haniel://logs/web?lines=50")
        assert result is not None
        assert "Log line 1" in result

    @pytest.mark.asyncio
    async def test_read_logs_default_lines(self, mcp_server, mock_runner):
        """Test reading logs with default line count."""
        result = await mcp_server.read_resource("haniel://logs/web")
        assert result is not None
        mock_runner.process_manager.log_manager.get_log_tail.assert_called_with(
            "web", 50
        )


class TestMcpTools:
    """Test MCP tool handlers."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="sse", port=3200),
            services={
                "web": ServiceConfig(run="python -m http.server"),
            },
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo"),
            },
        )
        runner.config_dir = Path("/tmp/test")
        runner.log_dir = Path("/tmp/test/logs")

        # Mock get_status to include services and repos
        runner.get_status.return_value = {
            "running": True,
            "services": {
                "web": {"state": "running", "uptime": 3600},
            },
            "repos": {
                "main": {"path": "./repo", "branch": "main", "last_head": "abc12345"},
            },
        }

        runner.health_manager = MagicMock()
        runner.health_manager.reset_circuit = MagicMock()

        runner.process_manager = MagicMock()
        runner.process_manager.stop_service = MagicMock(return_value=True)
        runner.process_manager.is_running = MagicMock(return_value=True)

        runner._start_service = MagicMock(return_value=True)
        runner._pull_repo = MagicMock(return_value=True)
        runner.get_affected_services = MagicMock(return_value=["web"])
        runner.get_startup_order = MagicMock(return_value=["web"])
        runner.get_shutdown_order = MagicMock(return_value=["web"])

        runner._enabled_services = runner.config.services
        runner._repo_states = {
            "main": MagicMock(config=runner.config.repos["main"]),
        }

        return runner

    @pytest.fixture
    def mcp_server(self, mock_runner):
        """Create HanielMcpServer instance."""
        from haniel.integrations.mcp_server import HanielMcpServer

        return HanielMcpServer(mock_runner)

    @pytest.mark.asyncio
    async def test_restart_service(self, mcp_server, mock_runner):
        """Test haniel_restart tool."""
        result = await mcp_server.call_tool("haniel_restart", {"service": "web"})
        assert "success" in result.lower() or "restarted" in result.lower()
        mock_runner.process_manager.stop_service.assert_called_with("web")
        mock_runner._start_service.assert_called_with("web")

    @pytest.mark.asyncio
    async def test_restart_unknown_service(self, mcp_server, mock_runner):
        """Test restart with unknown service."""
        mock_runner.get_status.return_value = {
            "running": True,
            "services": {},
            "repos": {},
        }
        result = await mcp_server.call_tool("haniel_restart", {"service": "unknown"})
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_service(self, mcp_server, mock_runner):
        """Test haniel_stop tool."""
        result = await mcp_server.call_tool("haniel_stop", {"service": "web"})
        assert "stopped" in result.lower() or "success" in result.lower()
        mock_runner.process_manager.stop_service.assert_called_with("web")

    @pytest.mark.asyncio
    async def test_start_service(self, mcp_server, mock_runner):
        """Test haniel_start tool."""
        mock_runner.process_manager.is_running = MagicMock(return_value=False)
        result = await mcp_server.call_tool("haniel_start", {"service": "web"})
        assert "started" in result.lower() or "success" in result.lower()
        mock_runner._start_service.assert_called_with("web")

    @pytest.mark.asyncio
    async def test_start_already_running(self, mcp_server, mock_runner):
        """Test start when service already running."""
        mock_runner.process_manager.is_running = MagicMock(return_value=True)
        result = await mcp_server.call_tool("haniel_start", {"service": "web"})
        assert "already running" in result.lower()

    @pytest.mark.asyncio
    async def test_pull_repo(self, mcp_server, mock_runner):
        """Test haniel_pull tool delegates to runner.trigger_pull."""
        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        assert "pulled" in result.lower() or "success" in result.lower()
        mock_runner.trigger_pull.assert_called_with("main")

    @pytest.mark.asyncio
    async def test_pull_repo_while_pulling(self, mcp_server, mock_runner):
        """Test haniel_pull returns success even when is_pulling guard silently returns.

        trigger_pull returns None (no exception) when is_pulling is True.
        MCP should still return a success message in this case.
        """
        mock_runner.trigger_pull.return_value = None  # simulates is_pulling guard
        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        assert "success" in result.lower() or "pulled" in result.lower()

    @pytest.mark.asyncio
    async def test_pull_unknown_repo(self, mcp_server, mock_runner):
        """Test pull with unknown repo."""
        mock_runner.get_status.return_value = {
            "running": True,
            "services": {},
            "repos": {},
        }
        result = await mcp_server.call_tool("haniel_pull", {"repo": "unknown"})
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_enable_service(self, mcp_server, mock_runner):
        """Test haniel_enable tool (circuit breaker reset)."""
        result = await mcp_server.call_tool("haniel_enable", {"service": "web"})
        assert "enabled" in result.lower() or "reset" in result.lower()
        mock_runner.health_manager.reset_circuit.assert_called_with("web")

    @pytest.mark.asyncio
    async def test_reload_config(self, mcp_server, mock_runner):
        """Test haniel_reload tool."""
        mock_runner.reload_config = MagicMock()
        result = await mcp_server.call_tool("haniel_reload", {})
        assert "reloaded" in result.lower() or "success" in result.lower()
        mock_runner.reload_config.assert_called_once()


class TestMcpServerIntegration:
    """Integration tests for MCP server."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="sse", port=3200),
            services={},
            repos={},
        )
        runner.config_dir = Path("/tmp/test")
        runner.get_status.return_value = {"running": True, "services": {}, "repos": {}}
        runner._enabled_services = {}
        runner.health_manager = MagicMock()
        runner.process_manager = MagicMock()
        runner.process_manager.log_manager = MagicMock()
        return runner

    @pytest.fixture
    def mcp_server(self, mock_runner):
        """Create HanielMcpServer instance."""
        from haniel.integrations.mcp_server import HanielMcpServer

        return HanielMcpServer(mock_runner)

    def test_list_resources(self, mcp_server):
        """Test listing available resources."""
        resources = mcp_server.list_resources()
        resource_uris = [r["uri"] for r in resources]
        assert "haniel://status" in resource_uris
        assert "haniel://repos" in resource_uris
        assert "haniel://config" in resource_uris

    def test_list_resources_with_services(self, mock_runner):
        """Test that per-service resources are generated dynamically."""
        from haniel.integrations.mcp_server import HanielMcpServer

        mock_runner.get_status.return_value = {
            "running": True,
            "services": {"web": {"state": "running"}},
            "repos": {},
        }
        server = HanielMcpServer(mock_runner)
        resources = server.list_resources()
        resource_uris = [r["uri"] for r in resources]
        assert "haniel://logs/web" in resource_uris
        assert "haniel://status/web" in resource_uris

    def test_list_tools(self, mcp_server):
        """Test listing available tools."""
        tools = mcp_server.list_tools()
        assert len(tools) >= 6
        tool_names = [t["name"] for t in tools]
        assert "haniel_restart" in tool_names
        assert "haniel_stop" in tool_names
        assert "haniel_start" in tool_names
        assert "haniel_pull" in tool_names
        assert "haniel_enable" in tool_names
        assert "haniel_reload" in tool_names


class TestMcpServerExtended:
    """Extended tests for MCP server edge cases."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="sse", port=3200),
            services={
                "web": ServiceConfig(run="python -m http.server"),
            },
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo"),
            },
        )
        runner.config_dir = Path("/tmp/test")
        runner.get_status.return_value = {
            "running": True,
            "services": {
                "web": {"state": "running", "uptime": 3600},
            },
            "repos": {
                "main": {"path": "./repo", "branch": "main", "last_head": "abc12345"},
            },
        }
        runner.health_manager = MagicMock()
        runner.process_manager = MagicMock()
        runner.process_manager.log_manager = MagicMock()
        runner.process_manager.log_manager.get_log_tail = MagicMock(return_value=[])
        runner.process_manager.is_running = MagicMock(return_value=False)
        runner.process_manager.stop_service = MagicMock()
        runner._start_service = MagicMock()
        runner._pull_repo = MagicMock(return_value=True)
        runner.get_affected_services = MagicMock(return_value=["web"])
        runner.get_startup_order = MagicMock(return_value=["web"])
        runner.get_shutdown_order = MagicMock(return_value=["web"])
        return runner

    @pytest.fixture
    def mcp_server(self, mock_runner):
        """Create HanielMcpServer instance."""
        from haniel.integrations.mcp_server import HanielMcpServer

        return HanielMcpServer(mock_runner)

    @pytest.mark.asyncio
    async def test_read_unknown_scheme(self, mcp_server):
        """Test reading resource with unknown scheme."""
        result = await mcp_server.read_resource("unknown://status")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_read_unknown_resource(self, mcp_server):
        """Test reading unknown resource path."""
        result = await mcp_server.read_resource("haniel://unknown/path")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_logs_invalid_lines_param(self, mcp_server, mock_runner):
        """Test reading logs with invalid lines parameter."""
        await mcp_server.read_resource("haniel://logs/web?lines=invalid")
        # Should default to 50
        mock_runner.process_manager.log_manager.get_log_tail.assert_called_with(
            "web", 50
        )

    @pytest.mark.asyncio
    async def test_logs_bounded_lines(self, mcp_server, mock_runner):
        """Test that lines parameter is bounded."""
        await mcp_server.read_resource("haniel://logs/web?lines=100000")
        # Should be capped at MAX_LOG_LINES (10000)
        call_args = mock_runner.process_manager.log_manager.get_log_tail.call_args
        assert call_args[0][1] <= 10000

    @pytest.mark.asyncio
    async def test_restart_empty_service_name(self, mcp_server):
        """Test restart with empty service name."""
        result = await mcp_server.call_tool("haniel_restart", {"service": ""})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_empty_service_name(self, mcp_server):
        """Test stop with empty service name."""
        result = await mcp_server.call_tool("haniel_stop", {"service": ""})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_start_empty_service_name(self, mcp_server):
        """Test start with empty service name."""
        result = await mcp_server.call_tool("haniel_start", {"service": ""})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_pull_empty_repo_name(self, mcp_server):
        """Test pull with empty repo name."""
        result = await mcp_server.call_tool("haniel_pull", {"repo": ""})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_enable_empty_service_name(self, mcp_server):
        """Test enable with empty service name."""
        result = await mcp_server.call_tool("haniel_enable", {"service": ""})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, mcp_server):
        """Test calling unknown tool."""
        result = await mcp_server.call_tool("unknown_tool", {})
        assert "unknown" in result.lower()

    @pytest.mark.asyncio
    async def test_restart_exception_handling(self, mcp_server, mock_runner):
        """Test restart with exception during _start_service."""
        # Make _start_service raise an exception
        mock_runner._start_service.side_effect = Exception("Start failed")

        result = await mcp_server.call_tool("haniel_restart", {"service": "web"})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_stop_exception_handling(self, mcp_server, mock_runner):
        """Test stop with exception."""
        mock_runner.process_manager.stop_service.side_effect = Exception("Stop failed")

        result = await mcp_server.call_tool("haniel_stop", {"service": "web"})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_start_exception_handling(self, mcp_server, mock_runner):
        """Test start with exception."""
        mock_runner._start_service.side_effect = Exception("Start failed")

        result = await mcp_server.call_tool("haniel_start", {"service": "web"})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_pull_failure(self, mcp_server, mock_runner):
        """Test pull when trigger_pull raises (git pull failed)."""
        mock_runner.trigger_pull.side_effect = RuntimeError("git pull failed for main")

        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_pull_exception_handling(self, mcp_server, mock_runner):
        """Test pull with unexpected exception from trigger_pull."""
        mock_runner.trigger_pull.side_effect = Exception("Pull failed")

        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_enable_exception_handling(self, mcp_server, mock_runner):
        """Test enable with exception."""
        mock_runner.health_manager.reset_circuit.side_effect = Exception("Reset failed")

        result = await mcp_server.call_tool("haniel_enable", {"service": "web"})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_reload_exception_handling(self, mcp_server, mock_runner):
        """Test reload with exception."""
        mock_runner.reload_config = MagicMock(side_effect=Exception("Load failed"))
        result = await mcp_server.call_tool("haniel_reload", {})
        data = json.loads(result)
        assert "error" in data

    def test_enabled_property_no_mcp_config(self):
        """Test enabled property when MCP config is not set."""
        from haniel.integrations.mcp_server import HanielMcpServer

        runner = MagicMock()
        runner.config = HanielConfig(poll_interval=60, services={}, repos={})  # No mcp
        runner.config_dir = Path("/tmp/test")

        server = HanielMcpServer(runner)
        # Should default to True
        assert server.enabled is True

    def test_port_property_no_mcp_config(self):
        """Test port property when MCP config is not set."""
        from haniel.integrations.mcp_server import HanielMcpServer

        runner = MagicMock()
        runner.config = HanielConfig(poll_interval=60, services={}, repos={})  # No mcp
        runner.config_dir = Path("/tmp/test")

        server = HanielMcpServer(runner)
        # Should default to 3200
        assert server.port == 3200


class TestMcpServerLifecycle:
    """Tests for MCP server lifecycle methods."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=False, port=3200),  # Disabled
            services={},
            repos={},
        )
        runner.config_dir = Path("/tmp/test")
        return runner

    @pytest.fixture
    def mcp_server(self, mock_runner):
        """Create HanielMcpServer instance."""
        from haniel.integrations.mcp_server import HanielMcpServer

        return HanielMcpServer(mock_runner)

    @pytest.mark.asyncio
    async def test_stop_without_start(self, mcp_server):
        """Test stop without starting."""
        # Should not raise
        await mcp_server.stop()

    def test_stop_sync_without_start(self, mcp_server):
        """Test stop_sync without starting."""
        # Should not raise
        mcp_server.stop_sync()
