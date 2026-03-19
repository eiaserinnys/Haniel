"""
Tests for the config management REST API (config_api.py).

Covers:
- GET /api/config, /api/config/services, /api/config/repos
- PUT /api/config/services/{name}  — update existing service, reload called
- POST /api/config/services        — add service, reload called
- DELETE /api/config/services/{name} — remove service, 400 when has dependents
- PUT /api/config/repos/{name}
- POST /api/config/repos           — add repo
- DELETE /api/config/repos/{name}  — 400 when referenced by services
- 501 when config_path is None
- 400 on validation failure
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.dashboard import setup_dashboard


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_yaml(path: Path, config: HanielConfig) -> None:
    """Write a HanielConfig to a YAML file (by_alias=True)."""
    data = config.model_dump(by_alias=True, exclude_none=True, mode="python")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def base_config() -> HanielConfig:
    """A minimal HanielConfig used as the starting point for most tests."""
    return HanielConfig(
        poll_interval=60,
        services={
            "web": ServiceConfig(run="python -m http.server"),
            "worker": ServiceConfig(run="python worker.py", after=["web"]),
        },
        repos={
            "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo"),
        },
    )


@pytest.fixture
def config_file(tmp_path: Path, base_config: HanielConfig) -> Path:
    """Write base_config to a temporary haniel.yaml and return its path."""
    path = tmp_path / "haniel.yaml"
    _write_yaml(path, base_config)
    return path


@pytest.fixture
def mock_runner(config_file: Path, base_config: HanielConfig):
    """Mock ServiceRunner with a real config_path pointing to a temp YAML."""
    runner = MagicMock()
    runner.config = base_config
    runner.config_dir = config_file.parent
    runner.config_path = config_file

    runner.get_status.return_value = {
        "running": True,
        "start_time": "2026-01-01T00:00:00",
        "last_poll": "2026-01-01T01:00:00",
        "poll_count": 1,
        "poll_interval": 60,
        "services": {
            "web": {
                "state": "running",
                "uptime": 100.0,
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
                "uptime": 100.0,
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

    runner.process_manager = MagicMock()
    runner.process_manager.is_running.return_value = False
    runner.process_manager.log_manager = MagicMock()
    runner.health_manager = MagicMock()

    # reload_config is a real no-op by default in mock; tests can override
    runner.reload_config.return_value = None

    return runner


@pytest.fixture
def dashboard_app(mock_runner):
    """aiohttp app with config API routes registered (via setup_dashboard)."""
    app = web.Application()
    loop = asyncio.new_event_loop()
    setup_dashboard(app, mock_runner, loop)
    yield app
    loop.close()


# ── GET /api/config ────────────────────────────────────────────────────────────


class TestGetConfig:
    @pytest.mark.asyncio
    async def test_returns_200_with_full_config(self, dashboard_app, mock_runner):
        """GET /api/config returns 200 and JSON representation of config."""
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.get("/api/config")
            assert resp.status == 200
            data = await resp.json()
            assert "services" in data
            assert "repos" in data
            assert "web" in data["services"]
            assert "main" in data["repos"]

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        """GET /api/config returns 501 when runner.config_path is None."""
        mock_runner.config_path = None
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.get("/api/config")
            assert resp.status == 501


# ── GET /api/config/services ──────────────────────────────────────────────────


class TestGetConfigServices:
    @pytest.mark.asyncio
    async def test_returns_services_dict(self, dashboard_app, mock_runner):
        """GET /api/config/services returns the services section."""
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.get("/api/config/services")
            assert resp.status == 200
            data = await resp.json()
            assert "web" in data
            assert "worker" in data

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.get("/api/config/services")
            assert resp.status == 501


# ── GET /api/config/repos ─────────────────────────────────────────────────────


class TestGetConfigRepos:
    @pytest.mark.asyncio
    async def test_returns_repos_dict(self, dashboard_app, mock_runner):
        """GET /api/config/repos returns the repos section."""
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.get("/api/config/repos")
            assert resp.status == 200
            data = await resp.json()
            assert "main" in data

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.get("/api/config/repos")
            assert resp.status == 501


# ── PUT /api/config/services/{name} ──────────────────────────────────────────


class TestPutService:
    @pytest.mark.asyncio
    async def test_updates_service_and_calls_reload(
        self, dashboard_app, mock_runner, config_file
    ):
        """PUT /api/config/services/{name} updates the service and calls reload_config."""
        payload = {"run": "python -m http.server 9090"}
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.put(
                "/api/config/services/web",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        mock_runner.reload_config.assert_called()

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_service(
        self, dashboard_app, mock_runner
    ):
        """PUT /api/config/services/nonexistent returns 404."""
        payload = {"run": "python app.py"}
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.put(
                "/api/config/services/nonexistent",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_400_on_validation_failure(self, dashboard_app, mock_runner):
        """PUT with missing required 'run' field returns 400."""
        payload = {"cwd": "./somewhere"}  # missing 'run'
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.put(
                "/api/config/services/web",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        payload = {"run": "python app.py"}
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.put(
                "/api/config/services/web",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 501


# ── POST /api/config/services ─────────────────────────────────────────────────


class TestPostService:
    @pytest.mark.asyncio
    async def test_adds_service_and_calls_reload(
        self, dashboard_app, mock_runner, config_file
    ):
        """POST /api/config/services adds a new service and calls reload_config."""
        payload = {
            "name": "cache",
            "config": {"run": "redis-server"},
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.post(
                "/api/config/services",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        mock_runner.reload_config.assert_called()

    @pytest.mark.asyncio
    async def test_returns_400_on_duplicate_service(self, dashboard_app, mock_runner):
        """POST for an already existing service name returns 400."""
        payload = {
            "name": "web",  # already exists
            "config": {"run": "python -m http.server"},
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.post(
                "/api/config/services",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        payload = {"name": "cache", "config": {"run": "redis-server"}}
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.post(
                "/api/config/services",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 501


# ── DELETE /api/config/services/{name} ───────────────────────────────────────


class TestDeleteService:
    @pytest.mark.asyncio
    async def test_removes_service(self, dashboard_app, mock_runner, config_file):
        """DELETE /api/config/services/{name} removes the service from YAML."""
        # 'worker' depends on 'web', so we delete 'worker' first
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/services/worker")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        mock_runner.reload_config.assert_called()

    @pytest.mark.asyncio
    async def test_returns_400_when_has_dependents(self, dashboard_app, mock_runner):
        """DELETE /api/config/services/web returns 400 because worker depends on it."""
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/services/web")
            assert resp.status == 400
            data = await resp.json()
            assert "error" in data
            assert "worker" in data["error"]

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_service(
        self, dashboard_app, mock_runner
    ):
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/services/nonexistent")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/services/worker")
            assert resp.status == 501


# ── POST /api/config/repos ────────────────────────────────────────────────────


class TestPostRepo:
    @pytest.mark.asyncio
    async def test_adds_repo_and_calls_reload(
        self, dashboard_app, mock_runner, config_file
    ):
        """POST /api/config/repos adds a new repo and calls reload_config."""
        payload = {
            "name": "extra",
            "config": {
                "url": "git@github.com:test/extra.git",
                "path": "./extra",
            },
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.post(
                "/api/config/repos",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        mock_runner.reload_config.assert_called()

    @pytest.mark.asyncio
    async def test_returns_400_on_duplicate_repo(self, dashboard_app, mock_runner):
        """POST for an already existing repo name returns 400."""
        payload = {
            "name": "main",  # already exists
            "config": {
                "url": "git@github.com:test/repo.git",
                "path": "./repo",
            },
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.post(
                "/api/config/repos",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        payload = {
            "name": "extra",
            "config": {"url": "git@github.com:x/y.git", "path": "./y"},
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.post(
                "/api/config/repos",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 501


# ── DELETE /api/config/repos/{name} ──────────────────────────────────────────


class TestDeleteRepo:
    @pytest.mark.asyncio
    async def test_returns_400_when_referenced_by_services(
        self, dashboard_app, mock_runner, config_file, tmp_path
    ):
        """DELETE /api/config/repos/{name} returns 400 when a service uses it."""
        # Create a config with a service that references a repo
        config_with_ref = HanielConfig(
            poll_interval=60,
            services={
                "web": ServiceConfig(run="python app.py", repo="main"),
            },
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo"),
            },
        )
        _write_yaml(config_file, config_with_ref)

        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/repos/main")
            assert resp.status == 400
            data = await resp.json()
            assert "error" in data
            assert "web" in data["error"]

    @pytest.mark.asyncio
    async def test_removes_unreferenced_repo(
        self, dashboard_app, mock_runner, config_file
    ):
        """DELETE /api/config/repos/main succeeds when no service references it."""
        # base_config has no service referencing 'main' repo
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/repos/main")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        mock_runner.reload_config.assert_called()

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_repo(self, dashboard_app, mock_runner):
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/repos/nonexistent")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_501_when_no_config_path(self, dashboard_app, mock_runner):
        mock_runner.config_path = None
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.delete("/api/config/repos/main")
            assert resp.status == 501


# ── PUT /api/config/repos/{name} ─────────────────────────────────────────────


class TestPutRepo:
    @pytest.mark.asyncio
    async def test_updates_repo_and_calls_reload(
        self, dashboard_app, mock_runner, config_file
    ):
        """PUT /api/config/repos/{name} updates a repo and calls reload_config."""
        payload = {
            "url": "git@github.com:test/repo.git",
            "branch": "develop",
            "path": "./repo",
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.put(
                "/api/config/repos/main",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        mock_runner.reload_config.assert_called()

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_repo(self, dashboard_app, mock_runner):
        payload = {
            "url": "git@github.com:x/y.git",
            "path": "./y",
        }
        async with TestClient(TestServer(dashboard_app)) as client:
            resp = await client.put(
                "/api/config/repos/nonexistent",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 404
