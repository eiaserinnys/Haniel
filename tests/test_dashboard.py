"""
Tests for the haniel dashboard module.

Covers:
- REST API response format (api.py)
- WebSocket event broadcasts (ws.py)
- git pending_changes helper (git.py)
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route, WebSocketRoute

from haniel.config import (
    DashboardConfig,
    HanielConfig,
    McpConfig,
    RepoConfig,
    ServiceConfig,
)
from haniel.core.health import ServiceState


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_runner():
    """Create a mock ServiceRunner suitable for dashboard tests."""
    runner = MagicMock()
    runner.config = HanielConfig(
        poll_interval=60,
        mcp=McpConfig(enabled=True, transport="streamable_http", port=3200),
        services={
            "web": ServiceConfig(run="python -m http.server"),
            "worker": ServiceConfig(run="python worker.py", after=["web"]),
        },
        repos={
            "main": RepoConfig(
                url="git@github.com:test/repo.git", path="./repo"
            ),
        },
    )
    runner.config_dir = Path("/tmp/test")

    runner.get_status.return_value = {
        "running": True,
        "start_time": "2026-01-01T00:00:00",
        "last_poll": "2026-01-01T01:00:00",
        "poll_count": 10,
        "poll_interval": 60,
        "services": {
            "web": {
                "state": "running",
                "uptime": 3600.0,
                "restart_count": 0,
                "consecutive_failures": 0,
                "config": {
                    "run": "python -m http.server",
                    "cwd": None,
                    "repo": None,
                    "after": [],
                    "ready": None,
                    "enabled": True,
                },
            },
            "worker": {
                "state": "running",
                "uptime": 3600.0,
                "restart_count": 0,
                "consecutive_failures": 0,
                "config": {
                    "run": "python worker.py",
                    "cwd": None,
                    "repo": None,
                    "after": ["web"],
                    "ready": None,
                    "enabled": True,
                },
            },
        },
        "pending_restarts": [],
        "dependency_graph": {
            "web": {"dependencies": [], "dependents": ["worker"]},
            "worker": {"dependencies": ["web"], "dependents": []},
        },
        "repos": {
            "main": {
                "path": "./repo",
                "branch": "main",
                "last_head": "abc12345",
                "last_fetch": "2026-01-01T01:00:00",
                "fetch_error": None,
                "pending_changes": None,
            }
        },
    }

    # Mock process_manager
    runner.process_manager = MagicMock()
    runner.process_manager.is_running.return_value = True
    runner.process_manager.log_manager = MagicMock()
    runner.process_manager.log_manager.get_log_tail.return_value = [
        "line 1",
        "line 2",
    ]

    # Mock health_manager
    runner.health_manager = MagicMock()

    return runner


@pytest.fixture
def dashboard_app(mock_runner):
    """Create a Starlette app with dashboard routes registered."""
    from haniel.dashboard import setup_dashboard

    routes, middleware, ws_handler = setup_dashboard(mock_runner)
    # Set up ws_handler with a mock loop for broadcast tests
    loop = asyncio.new_event_loop()
    ws_handler.setup(loop)
    app = Starlette(routes=routes, middleware=middleware)
    yield app
    loop.close()


# ── REST API Tests ────────────────────────────────────────────────────────────


class TestDashboardApi:
    """Test the dashboard REST API endpoints."""

    def test_get_status(self, dashboard_app, mock_runner):
        """GET /api/status returns full status dict."""
        client = TestClient(dashboard_app)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        assert "repos" in data
        assert "running" in data

    def test_get_services(self, dashboard_app, mock_runner):
        """GET /api/services returns services dict."""
        client = TestClient(dashboard_app)
        resp = client.get("/api/services")
        assert resp.status_code == 200
        data = resp.json()
        assert "web" in data
        assert "worker" in data

    def test_service_stop_calls_process_manager(
        self, dashboard_app, mock_runner
    ):
        """POST /api/services/{name}/stop calls process_manager.stop_service."""
        client = TestClient(dashboard_app)
        resp = client.post("/api/services/web/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["action"] == "stop"
        mock_runner.process_manager.stop_service.assert_called_once_with("web")

    def test_service_not_found_returns_404(self, dashboard_app, mock_runner):
        """POST /api/services/{name}/stop with unknown name returns 404."""
        client = TestClient(dashboard_app)
        resp = client.post("/api/services/nonexistent/stop")
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data

    def test_service_enable_resets_circuit(self, dashboard_app, mock_runner):
        """POST /api/services/{name}/enable calls health_manager.reset_circuit."""
        client = TestClient(dashboard_app)
        resp = client.post("/api/services/web/enable")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_runner.health_manager.reset_circuit.assert_called_once_with("web")

    def test_service_logs(self, dashboard_app, mock_runner):
        """GET /api/services/{name}/logs returns log lines."""
        client = TestClient(dashboard_app)
        resp = client.get("/api/services/web/logs?lines=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "lines" in data
        assert isinstance(data["lines"], list)
        mock_runner.process_manager.log_manager.get_log_tail.assert_called_once_with(
            "web", 10
        )

    def test_service_logs_invalid_lines_param(
        self, dashboard_app, mock_runner
    ):
        """GET /api/services/{name}/logs?lines=abc returns 400."""
        client = TestClient(dashboard_app)
        resp = client.get("/api/services/web/logs?lines=abc")
        assert resp.status_code == 400

    def test_get_repos(self, dashboard_app, mock_runner):
        """GET /api/repos returns repos dict."""
        client = TestClient(dashboard_app)
        resp = client.get("/api/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert "main" in data
        assert "pending_changes" in data["main"]

    def test_self_update_approve(self, dashboard_app, mock_runner):
        """POST /api/self-update/approve calls runner.approve_self_update."""
        mock_runner.approve_self_update.return_value = "update scheduled"
        client = TestClient(dashboard_app)
        resp = client.post("/api/self-update/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_runner.approve_self_update.assert_called_once()

    def test_reload_not_supported(self, dashboard_app, mock_runner):
        """POST /api/reload returns 501 when runner has no reload_config."""
        # Remove reload_config attribute from mock
        del mock_runner.reload_config
        client = TestClient(dashboard_app)
        resp = client.post("/api/reload")
        assert resp.status_code == 501


# ── WebSocket Tests ───────────────────────────────────────────────────────────


class TestDashboardWebSocket:
    """Test WebSocket event stream."""

    def test_ws_connect_receives_init(self, dashboard_app, mock_runner):
        """WebSocket connection receives initial status on connect."""
        client = TestClient(dashboard_app)
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            assert data["type"] == "init"
            assert "status" in data
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_state_change_broadcast(self, mock_runner):
        """State change events are broadcast to connected WebSocket clients.

        This test verifies the DashboardWebSocket._broadcast method directly,
        since the Starlette TestClient's event loop is separate from the one
        that ws_handler schedules broadcasts on.
        """
        from haniel.dashboard.ws import DashboardWebSocket

        ws_handler = DashboardWebSocket(mock_runner)

        # Create a mock WebSocket
        mock_ws = MagicMock()
        mock_ws.send_text = MagicMock(side_effect=lambda t: asyncio.coroutine(lambda: None)())

        ws_handler._clients.add(mock_ws)

        event = {
            "type": "state_change",
            "service": "web",
            "old": ServiceState.STARTING.value,
            "new": ServiceState.RUNNING.value,
            "timestamp": "2026-01-01T00:00:00",
        }
        await ws_handler._broadcast(event)

        mock_ws.send_text.assert_called_once()
        sent_data = json.loads(mock_ws.send_text.call_args[0][0])
        assert sent_data["type"] == "state_change"
        assert sent_data["service"] == "web"
        assert sent_data["old"] == ServiceState.STARTING.value
        assert sent_data["new"] == ServiceState.RUNNING.value


# ── git.get_pending_changes Tests ─────────────────────────────────────────────


class TestGetPendingChanges:
    """Tests for get_pending_changes in git.py."""

    def test_no_changes_returns_empty(self, tmp_path):
        """When HEAD == remote HEAD, returns empty commits and None stat."""
        from haniel.core.git import get_pending_changes

        with patch("haniel.core.git._run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="", returncode=0)
            result = get_pending_changes(tmp_path, "main")

        assert result["commits"] == []
        assert result["stat"] is None

    def test_with_changes_returns_commit_list(self, tmp_path):
        """When there are pending commits, returns commit list and stat."""
        from haniel.core.git import get_pending_changes

        log_output = "abc1234 fix: bug fix\ndef5678 feat: new feature\n"
        stat_output = " src/foo.py | 10 +++++\n 1 file changed, 10 insertions(+)"

        call_count = 0

        def side_effect(args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if "log" in args:
                result.stdout = log_output
            else:
                result.stdout = stat_output
            return result

        with patch("haniel.core.git._run_git", side_effect=side_effect):
            result = get_pending_changes(tmp_path, "main")

        assert len(result["commits"]) == 2
        assert result["commits"][0] == "abc1234 fix: bug fix"
        assert result["stat"] is not None

    def test_exception_returns_empty(self, tmp_path):
        """On any exception, returns empty result (does not propagate)."""
        from haniel.core.git import get_pending_changes

        with patch("haniel.core.git._run_git", side_effect=RuntimeError("git error")):
            result = get_pending_changes(tmp_path, "main")

        assert result["commits"] == []
        assert result["stat"] is None


# ── DashboardConfig model test ─────────────────────────────────────────────────


class TestDashboardConfig:
    """Tests for DashboardConfig model."""

    def test_defaults(self):
        """DashboardConfig has sensible defaults."""
        cfg = DashboardConfig()
        assert cfg.enabled is True
        assert cfg.port is None

    def test_custom_port(self):
        """DashboardConfig accepts a custom port."""
        cfg = DashboardConfig(enabled=True, port=8080)
        assert cfg.port == 8080

    def test_haniel_config_has_dashboard_field(self):
        """HanielConfig includes an optional dashboard field."""
        cfg = HanielConfig()
        assert cfg.dashboard is None

        cfg2 = HanielConfig(dashboard=DashboardConfig(enabled=False))
        assert cfg2.dashboard is not None
        assert cfg2.dashboard.enabled is False
