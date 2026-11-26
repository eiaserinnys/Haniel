"""
Tests for haniel MCP server.

Tests the MCP server that provides Claude Code integration:
- Resources: status, repos, logs
- Tools: restart, stop, start, pull, enable, reload
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from haniel.config import HanielConfig, McpConfig, ServiceConfig, RepoConfig
from haniel.core.health import ServiceState
from haniel.core.service_lifecycle import PullOutcome
from haniel.integrations.mcp_server import McpToolError
from haniel.core.thread_shutdown import DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS


class TestHanielMcpServer:
    """Test HanielMcpServer class."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="streamable_http", port=3200),
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

    async def test_start_uses_configured_host(self, mcp_server, mock_runner):
        mock_runner.config.mcp = McpConfig(host="192.0.2.1")
        with patch("uvicorn.Config") as config, patch("uvicorn.Server") as server:
            server.return_value.serve = AsyncMock()
            await mcp_server.start()
        assert config.call_args.kwargs["host"] == "192.0.2.1"


class TestMcpPortInUse:
    async def test_start_logs_error_and_does_not_serve_when_port_is_taken(self, caplog):
        import socket

        from haniel.integrations.mcp_server import HanielMcpServer

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            runner = MagicMock()
            runner.config = HanielConfig(
                mcp=McpConfig(enabled=True, host="127.0.0.1", port=port),
                services={},
                repos={},
            )
            server = HanielMcpServer(runner)

            with patch("uvicorn.Server") as uvicorn_server:
                with caplog.at_level("ERROR", logger="haniel.integrations.mcp_server"):
                    await server.start()

        uvicorn_server.assert_not_called()
        assert (
            f"MCP port {port} on 127.0.0.1 is already in use; change mcp.port"
            in caplog.text
        )


class TestMcpResources:
    """Test MCP resource handlers."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="streamable_http", port=3200),
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
            mcp=McpConfig(enabled=True, transport="streamable_http", port=3200),
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
        """Test haniel_restart delegates to one runner restart call."""
        result = await mcp_server.call_tool("haniel_restart", {"service": "web"})
        assert "success" in result.lower() or "restarted" in result.lower()
        mock_runner.restart_service.assert_called_once_with("web")
        mock_runner.process_manager.stop_service.assert_not_called()
        mock_runner._start_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_unknown_service(self, mcp_server, mock_runner):
        """Test restart with unknown service."""
        mock_runner.get_status.return_value = {
            "running": True,
            "services": {},
            "repos": {},
        }
        with pytest.raises(McpToolError, match="Service not found: unknown"):
            await mcp_server.call_tool("haniel_restart", {"service": "unknown"})

    @pytest.mark.asyncio
    async def test_stop_service(self, mcp_server, mock_runner):
        """Test haniel_stop delegates to the runner stop method."""
        result = await mcp_server.call_tool("haniel_stop", {"service": "web"})
        assert "stopped" in result.lower() or "success" in result.lower()
        mock_runner.stop_service.assert_called_once_with("web")

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
        mock_runner.trigger_pull.return_value = PullOutcome.PULLED
        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        assert result == "main: pulled; affected services restarted"
        mock_runner.trigger_pull.assert_called_with("main")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fails", [False, True])
    async def test_update_uses_canonical_repo_pull(
        self, mcp_server, mock_runner, fails
    ):
        mock_runner.get_status.return_value["services"]["web"]["config"] = {
            "repo": "main"
        }
        mock_runner.trigger_pull.return_value = PullOutcome.PULLED
        if fails:
            mock_runner.trigger_pull.side_effect = RuntimeError("pull failed")
            with pytest.raises(McpToolError, match="pull failed"):
                await mcp_server.call_tool("haniel_update", {"service": "web"})
        else:
            result = await mcp_server.call_tool("haniel_update", {"service": "web"})
            assert result.startswith("web (repo main): pulled")

        mock_runner.trigger_pull.assert_called_once_with("main")
        mock_runner._pull_repo.assert_not_called()
        mock_runner.restart_service.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (PullOutcome.NO_CHANGES, "main: no changes to pull"),
            (
                PullOutcome.ALREADY_PULLING,
                "main: already pulling; this request was ignored",
            ),
        ],
    )
    async def test_pull_repo_reports_what_actually_happened(
        self, mcp_server, mock_runner, outcome, expected
    ):
        """haniel_pull must not claim a pull or restart that did not happen."""
        mock_runner.trigger_pull.return_value = outcome
        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        assert result == expected
        assert "restarted" not in result

    @pytest.mark.asyncio
    async def test_pull_self_repo_warns_haniel_restarts(self, mcp_server, mock_runner):
        mock_runner.trigger_pull.return_value = PullOutcome.SELF_UPDATE_REQUESTED
        result = await mcp_server.call_tool("haniel_pull", {"repo": "main"})
        assert result.startswith("main: self-update requested.")
        assert "every service on this node stops" in result
        assert "overlap or adopt" in result

    @pytest.mark.asyncio
    async def test_restart_haniel_warns_node_services_stop(
        self, mcp_server, mock_runner
    ):
        mock_runner.request_restart.return_value = "Restart initiated."
        result = await mcp_server.call_tool("haniel_restart", {"service": "haniel"})
        assert result.startswith("Restart initiated.")
        assert "every service on this node stops" in result

    @pytest.mark.parametrize("tool", ["haniel_restart", "haniel_pull", "haniel_update"])
    def test_tool_descriptions_warn_about_haniel_restart(self, mcp_server, tool):
        description = next(
            t["description"] for t in mcp_server.list_tools() if t["name"] == tool
        )
        assert "every service on this node stops" in description
        assert "overlap or adopt" in description

    @pytest.mark.asyncio
    async def test_pull_unknown_repo(self, mcp_server, mock_runner):
        """Test pull with unknown repo."""
        mock_runner.get_status.return_value = {
            "running": True,
            "services": {},
            "repos": {},
        }
        with pytest.raises(McpToolError, match="Repository not found: unknown"):
            await mcp_server.call_tool("haniel_pull", {"repo": "unknown"})

    @pytest.mark.asyncio
    async def test_enable_service(self, mcp_server, mock_runner):
        """haniel_enable is the pair of haniel_disable, not a circuit reset."""
        with patch("haniel.integrations.mcp_server.enable_service") as enable:
            enable.return_value = {"ok": True, "service": "web", "enabled": True}
            result = await mcp_server.call_tool("haniel_enable", {"service": "web"})

        assert json.loads(result)["enabled"] is True
        enable.assert_called_once_with(mock_runner, "web")
        mock_runner.health_manager.reset_circuit.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_unknown_service_is_an_error(self, mcp_server, mock_runner):
        with patch("haniel.integrations.mcp_server.enable_service") as enable:
            enable.side_effect = KeyError("Service not found: nope")
            with pytest.raises(McpToolError) as error:
                await mcp_server.call_tool("haniel_enable", {"service": "nope"})
        assert str(error.value) == "Service not found: nope"

    @pytest.mark.asyncio
    async def test_reset_circuit_tool(self, mcp_server, mock_runner):
        """haniel_reset_circuit only releases the circuit breaker."""
        mock_runner._snapshot_config_state.return_value.config.services = {"web": 1}
        mock_runner.process_manager.is_running.return_value = False
        result = await mcp_server.call_tool("haniel_reset_circuit", {"service": "web"})
        assert json.loads(result)["circuit_reset"] is True
        mock_runner.health_manager.reset_circuit.assert_called_once_with("web")

    @pytest.mark.asyncio
    async def test_reset_circuit_unknown_service_is_an_error(
        self, mcp_server, mock_runner
    ):
        mock_runner._snapshot_config_state.return_value.config.services = {}
        with pytest.raises(McpToolError, match="Service not found: nope"):
            await mcp_server.call_tool("haniel_reset_circuit", {"service": "nope"})
        mock_runner.health_manager.reset_circuit.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_logs_unknown_service_is_an_error(self, mcp_server, mock_runner):
        mock_runner._snapshot_config_state.return_value.config.services = {"web": 1}
        with pytest.raises(McpToolError, match="Service not found: nope"):
            await mcp_server.call_tool("haniel_read_logs", {"service": "nope"})
        mock_runner.process_manager.log_manager.get_log_tail.assert_not_called()

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
            mcp=McpConfig(enabled=True, transport="streamable_http", port=3200),
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
        assert "haniel_disable" in tool_names
        assert "haniel_reload" in tool_names
        assert "haniel_reload_service" in tool_names
        assert "haniel_register_service" in tool_names


class TestMcpServerExtended:
    """Extended tests for MCP server edge cases."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock ServiceRunner."""
        runner = MagicMock()
        runner.config = HanielConfig(
            poll_interval=60,
            mcp=McpConfig(enabled=True, transport="streamable_http", port=3200),
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
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_restart", {"service": ""})

    @pytest.mark.asyncio
    async def test_stop_empty_service_name(self, mcp_server):
        """Test stop with empty service name."""
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_stop", {"service": ""})

    @pytest.mark.asyncio
    async def test_start_empty_service_name(self, mcp_server):
        """Test start with empty service name."""
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_start", {"service": ""})

    @pytest.mark.asyncio
    async def test_pull_empty_repo_name(self, mcp_server):
        """Test pull with empty repo name."""
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_pull", {"repo": ""})

    @pytest.mark.asyncio
    async def test_enable_empty_service_name(self, mcp_server):
        """Test enable with empty service name."""
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_enable", {"service": ""})

    @pytest.mark.asyncio
    async def test_disable_service_tool(self, mcp_server, mock_runner):
        """Test haniel_disable delegates to service lifecycle core."""
        with patch("haniel.integrations.mcp_server.disable_service") as disable:
            disable.return_value = {"ok": True, "service": "web", "enabled": False}
            result = await mcp_server.call_tool("haniel_disable", {"service": "web"})

        assert json.loads(result)["enabled"] is False
        disable.assert_called_once_with(mock_runner, "web")

    @pytest.mark.asyncio
    async def test_reload_service_tool(self, mcp_server, mock_runner):
        """Test haniel_reload_service delegates to service lifecycle core."""
        with patch(
            "haniel.integrations.mcp_server.reload_service_definition"
        ) as reload_service:
            reload_service.return_value = {
                "ok": True,
                "service": "web",
                "restarted": True,
            }
            result = await mcp_server.call_tool(
                "haniel_reload_service", {"service": "web"}
            )

        assert json.loads(result)["restarted"] is True
        reload_service.assert_called_once_with(mock_runner, "web")

    @pytest.mark.asyncio
    async def test_register_service_tool(self, mcp_server, mock_runner):
        """Test haniel_register_service delegates to service lifecycle core."""
        with patch("haniel.integrations.mcp_server.register_service") as register:
            register.return_value = {"ok": True, "service": "api", "started": True}
            result = await mcp_server.call_tool(
                "haniel_register_service",
                {
                    "name": "api",
                    "service_config": {"run": "python api.py"},
                    "start": True,
                },
            )

        assert json.loads(result)["started"] is True
        register.assert_called_once_with(
            mock_runner,
            name="api",
            service_config={"run": "python api.py"},
            repo=None,
            repo_config=None,
            start=True,
        )

    @pytest.mark.asyncio
    async def test_delete_service_config_accepts_purge(self, mcp_server, mock_runner):
        """Test haniel_delete_service_config passes purge to common deletion."""
        with patch("haniel.integrations.mcp_server.delete_service_config") as delete:
            delete.return_value = {"ok": True, "service": "web", "purged": True}
            result = await mcp_server.call_tool(
                "haniel_delete_service_config", {"service": "web", "purge": True}
            )

        assert json.loads(result)["purged"] is True
        delete.assert_called_once_with(mock_runner, "web", purge=True)

    @pytest.mark.asyncio
    async def test_disable_empty_service_name(self, mcp_server):
        """Test disable with empty service name."""
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_disable", {"service": ""})

    @pytest.mark.asyncio
    async def test_reload_service_empty_name(self, mcp_server):
        """Test reload_service with empty service name."""
        with pytest.raises(McpToolError, match="required"):
            await mcp_server.call_tool("haniel_reload_service", {"service": ""})

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, mcp_server):
        """Test calling unknown tool."""
        with pytest.raises(McpToolError, match="Unknown tool"):
            await mcp_server.call_tool("unknown_tool", {})

    @pytest.mark.asyncio
    async def test_restart_exception_handling(self, mcp_server, mock_runner):
        """Test restart with an exception from the runner restart."""
        mock_runner.restart_service.side_effect = Exception("Start failed")

        with pytest.raises(McpToolError):
            await mcp_server.call_tool("haniel_restart", {"service": "web"})

    @pytest.mark.asyncio
    async def test_stop_exception_handling(self, mcp_server, mock_runner):
        """Test stop with an exception from the runner stop."""
        mock_runner.stop_service.side_effect = Exception("Stop failed")

        with pytest.raises(McpToolError):
            await mcp_server.call_tool("haniel_stop", {"service": "web"})

    @pytest.mark.asyncio
    async def test_start_exception_handling(self, mcp_server, mock_runner):
        """Test start with exception."""
        mock_runner._start_service.side_effect = Exception("Start failed")

        with pytest.raises(McpToolError):
            await mcp_server.call_tool("haniel_start", {"service": "web"})

    @pytest.mark.asyncio
    async def test_pull_failure(self, mcp_server, mock_runner):
        """Test pull when trigger_pull raises (git pull failed)."""
        mock_runner.trigger_pull.side_effect = RuntimeError("git pull failed for main")

        with pytest.raises(McpToolError):
            await mcp_server.call_tool("haniel_pull", {"repo": "main"})

    @pytest.mark.asyncio
    async def test_pull_exception_handling(self, mcp_server, mock_runner):
        """Test pull with unexpected exception from trigger_pull."""
        mock_runner.trigger_pull.side_effect = Exception("Pull failed")

        with pytest.raises(McpToolError):
            await mcp_server.call_tool("haniel_pull", {"repo": "main"})

    @pytest.mark.asyncio
    async def test_enable_exception_handling(self, mcp_server, mock_runner):
        """Test enable with exception."""
        with patch("haniel.integrations.mcp_server.enable_service") as enable:
            enable.side_effect = Exception("Enable failed")
            with pytest.raises(McpToolError, match="Enable failed"):
                await mcp_server.call_tool("haniel_enable", {"service": "web"})

    @pytest.mark.asyncio
    async def test_reload_exception_handling(self, mcp_server, mock_runner):
        """Test reload with exception."""
        mock_runner.reload_config = MagicMock(side_effect=Exception("Load failed"))
        with pytest.raises(McpToolError, match="Load failed"):
            await mcp_server.call_tool("haniel_reload", {})

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

    def test_stop_sync_bounds_server_loop_wait(self, mcp_server):
        class ThreadProbe:
            alive = True
            join_timeout = object()

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout=None) -> None:
                self.join_timeout = timeout
                if timeout is None:
                    self.alive = False

        thread = ThreadProbe()
        mcp_server._server = MagicMock()
        mcp_server._server_thread = thread

        mcp_server.stop_sync()

        assert mcp_server._server.should_exit is True
        assert thread.join_timeout == DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS
        assert thread.is_alive() is True

    def test_stop_sync_records_request_before_server_is_initialized(self, mcp_server):
        """An immediate stop must survive the startup window before uvicorn exists."""
        mcp_server._server = None
        mcp_server._server_thread = None

        mcp_server.stop_sync()

        assert mcp_server._stop_requested.is_set()


class TestConfigSaveRejections:
    """Saving an entry with unknown keys or bad values is a tool error."""

    @pytest.fixture
    def real_server(self, tmp_path: Path):
        from haniel.core.runner import ServiceRunner
        from haniel.integrations.mcp_server import HanielMcpServer
        from haniel.config import load_config

        path = tmp_path / "haniel.yaml"
        path.write_text("services:\n  web:\n    run: python app.py\n", encoding="utf-8")
        runner = ServiceRunner(load_config(path), config_dir=tmp_path, config_path=path)
        return HanielMcpServer(runner), path

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            (
                "haniel_update_service_config",
                {"service": "web", "config": {"run": "x", "redy": "port:80"}},
            ),
            (
                "haniel_create_service_config",
                {"name": "api", "config": {"run": "x", "redy": "port:80"}},
            ),
            (
                "haniel_register_service",
                {"name": "api", "service_config": {"run": "x", "redy": "port:80"}},
            ),
        ],
    )
    async def test_unknown_key_is_a_tool_error_and_writes_nothing(
        self, real_server, tool, arguments
    ):
        server, path = real_server
        before = path.read_text(encoding="utf-8")

        with pytest.raises(McpToolError, match=r"unknown key services\.\w+\.redy"):
            await server.call_tool(tool, arguments)

        assert path.read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    async def test_repo_with_empty_url_is_a_tool_error(self, real_server):
        server, _ = real_server

        with pytest.raises(McpToolError, match=r"repos\.r\.url must not be empty"):
            await server.call_tool(
                "haniel_create_repo_config",
                {"name": "r", "config": {"url": "", "path": "./r"}},
            )
