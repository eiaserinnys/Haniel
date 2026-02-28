"""Tests for the haniel runner module."""

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from haniel.config import (
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    BackoffConfig,
    ShutdownConfig,
    HooksConfig,
)
from haniel.health import HealthManager, ServiceState
from haniel.runner import (
    ServiceRunner,
    DependencyGraph,
    topological_sort,
    CyclicDependencyError,
)


# --- DependencyGraph Tests ---


class TestDependencyGraph:
    """Tests for the DependencyGraph class."""

    def test_empty_graph(self):
        """Empty graph should return empty list."""
        graph = DependencyGraph({})
        order = graph.topological_sort()
        assert order == []

    def test_single_service_no_deps(self):
        """Single service with no dependencies."""
        services = {
            "web": ServiceConfig(run="python server.py"),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort()
        assert order == ["web"]

    def test_linear_dependencies(self):
        """Linear chain: a -> b -> c."""
        services = {
            "c": ServiceConfig(run="cmd", after=["b"]),
            "b": ServiceConfig(run="cmd", after=["a"]),
            "a": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort()

        # a must come before b, b must come before c
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_multiple_dependencies(self):
        """Service with multiple dependencies."""
        services = {
            "web": ServiceConfig(run="cmd", after=["db", "cache"]),
            "db": ServiceConfig(run="cmd"),
            "cache": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort()

        # db and cache must come before web
        assert order.index("db") < order.index("web")
        assert order.index("cache") < order.index("web")

    def test_complex_dependencies(self):
        """More complex dependency graph."""
        services = {
            "bot": ServiceConfig(run="cmd", after=["mcp-seosoyoung"]),
            "mcp-seosoyoung": ServiceConfig(run="cmd"),
            "mcp-slack": ServiceConfig(run="cmd"),
            "rescue-bot": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort()

        assert order.index("mcp-seosoyoung") < order.index("bot")
        # mcp-slack and rescue-bot have no deps, can be anywhere

    def test_cyclic_dependency_detected(self):
        """Cyclic dependencies should raise error."""
        services = {
            "a": ServiceConfig(run="cmd", after=["b"]),
            "b": ServiceConfig(run="cmd", after=["a"]),
        }
        graph = DependencyGraph(services)

        with pytest.raises(CyclicDependencyError):
            graph.topological_sort()

    def test_missing_dependency_ignored(self):
        """Missing dependencies should be ignored (validated elsewhere)."""
        services = {
            "web": ServiceConfig(run="cmd", after=["nonexistent"]),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort()
        assert order == ["web"]

    def test_reverse_order(self):
        """Reverse topological order for shutdown."""
        services = {
            "c": ServiceConfig(run="cmd", after=["b"]),
            "b": ServiceConfig(run="cmd", after=["a"]),
            "a": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort(reverse=True)

        # Reverse: c, b, a (shutdown order)
        assert order.index("c") < order.index("b")
        assert order.index("b") < order.index("a")

    def test_get_dependents(self):
        """Get all services that depend on a given service."""
        services = {
            "bot": ServiceConfig(run="cmd", after=["mcp"]),
            "api": ServiceConfig(run="cmd", after=["mcp"]),
            "mcp": ServiceConfig(run="cmd"),
            "standalone": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)

        dependents = graph.get_dependents("mcp")
        assert set(dependents) == {"bot", "api"}

    def test_get_dependencies(self):
        """Get all dependencies of a service."""
        services = {
            "web": ServiceConfig(run="cmd", after=["db", "cache"]),
            "db": ServiceConfig(run="cmd"),
            "cache": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)

        deps = graph.get_dependencies("web")
        assert set(deps) == {"db", "cache"}


# --- Topological Sort Standalone Function Tests ---


class TestTopologicalSort:
    """Tests for the topological_sort function."""

    def test_simple_sort(self):
        """Simple topological sort."""
        services = {
            "b": ServiceConfig(run="cmd", after=["a"]),
            "a": ServiceConfig(run="cmd"),
        }
        order = topological_sort(services)
        assert order.index("a") < order.index("b")


# --- ServiceRunner Tests ---


class TestServiceRunner:
    """Tests for the ServiceRunner class."""

    @pytest.fixture
    def basic_config(self, tmp_path: Path) -> HanielConfig:
        """Create a basic config for testing."""
        return HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test-service": ServiceConfig(
                    run="python -c 'import time; time.sleep(100)'",
                    enabled=True,
                ),
            },
        )

    @pytest.fixture
    def config_with_deps(self, tmp_path: Path) -> HanielConfig:
        """Create a config with service dependencies."""
        return HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "mcp": ServiceConfig(run="sleep 100", enabled=True),
                "bot": ServiceConfig(run="sleep 100", after=["mcp"], enabled=True),
            },
        )

    @pytest.fixture
    def config_with_repo(self, tmp_path: Path) -> HanielConfig:
        """Create a config with a repository."""
        return HanielConfig(
            poll_interval=5,
            repos={
                "test-repo": RepoConfig(
                    url="git@github.com:test/test.git",
                    branch="main",
                    path="./test-repo",
                ),
            },
            services={
                "test-service": ServiceConfig(
                    run="echo hello",
                    repo="test-repo",
                    enabled=True,
                ),
            },
        )

    def test_runner_initialization(self, basic_config: HanielConfig, tmp_path: Path):
        """Test runner initialization."""
        runner = ServiceRunner(basic_config, config_dir=tmp_path)

        assert runner.config == basic_config
        assert runner.config_dir == tmp_path
        assert runner.poll_interval == 5
        assert not runner.is_running

    def test_runner_startup_order(self, config_with_deps: HanielConfig, tmp_path: Path):
        """Test that services start in correct dependency order."""
        runner = ServiceRunner(config_with_deps, config_dir=tmp_path)

        startup_order = runner.get_startup_order()
        assert startup_order.index("mcp") < startup_order.index("bot")

    def test_runner_shutdown_order(self, config_with_deps: HanielConfig, tmp_path: Path):
        """Test that services stop in reverse dependency order."""
        runner = ServiceRunner(config_with_deps, config_dir=tmp_path)

        shutdown_order = runner.get_shutdown_order()
        assert shutdown_order.index("bot") < shutdown_order.index("mcp")

    def test_disabled_services_excluded(self, tmp_path: Path):
        """Disabled services should not be started."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "enabled": ServiceConfig(run="sleep 100", enabled=True),
                "disabled": ServiceConfig(run="sleep 100", enabled=False),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        order = runner.get_startup_order()
        assert "enabled" in order
        assert "disabled" not in order

    def test_get_affected_services(self, tmp_path: Path):
        """Test finding services affected by repo changes."""
        config = HanielConfig(
            poll_interval=5,
            repos={
                "main-repo": RepoConfig(
                    url="git@github.com:test/main.git",
                    branch="main",
                    path="./main",
                ),
                "other-repo": RepoConfig(
                    url="git@github.com:test/other.git",
                    branch="main",
                    path="./other",
                ),
            },
            services={
                "main-service": ServiceConfig(run="cmd", repo="main-repo"),
                "other-service": ServiceConfig(run="cmd", repo="other-repo"),
                "both-deps": ServiceConfig(run="cmd", repo="main-repo", after=["main-service"]),
                "no-repo": ServiceConfig(run="cmd"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        affected = runner.get_affected_services("main-repo")
        # main-service depends on main-repo
        # both-deps depends on main-repo AND depends on main-service
        assert "main-service" in affected
        assert "both-deps" in affected
        assert "other-service" not in affected
        assert "no-repo" not in affected


# --- Hook Execution Tests ---


class TestHookExecution:
    """Tests for hook execution in the runner."""

    @pytest.fixture
    def config_with_hooks(self, tmp_path: Path) -> HanielConfig:
        """Create a config with hooks."""
        return HanielConfig(
            poll_interval=5,
            repos={
                "test-repo": RepoConfig(
                    url="git@github.com:test/test.git",
                    branch="main",
                    path="./test-repo",
                ),
            },
            services={
                "test-service": ServiceConfig(
                    run="sleep 100",
                    repo="test-repo",
                    hooks=HooksConfig(post_pull="echo 'post pull executed'"),
                ),
            },
        )

    @patch("subprocess.run")
    def test_post_pull_hook_executed(
        self, mock_run: MagicMock, config_with_hooks: HanielConfig, tmp_path: Path
    ):
        """Test that post_pull hook is executed after pull."""
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

        runner = ServiceRunner(config_with_hooks, config_dir=tmp_path)
        result = runner.execute_hook("test-service", "post_pull")

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "echo" in call_args[0][0] or call_args[0][0][0] == "echo"

    @patch("subprocess.run")
    def test_hook_failure_reported(
        self, mock_run: MagicMock, config_with_hooks: HanielConfig, tmp_path: Path
    ):
        """Test that hook failures are reported but don't stop execution."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd")

        runner = ServiceRunner(config_with_hooks, config_dir=tmp_path)
        result = runner.execute_hook("test-service", "post_pull")

        assert result is False

    def test_no_hook_returns_true(self, tmp_path: Path):
        """Service without hook should return True."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "no-hooks": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)
        result = runner.execute_hook("no-hooks", "post_pull")

        assert result is True


# --- Status Tests ---


class TestRunnerStatus:
    """Tests for runner status reporting."""

    def test_get_status_when_stopped(self, tmp_path: Path):
        """Test status when runner is stopped."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        status = runner.get_status()
        assert status["running"] is False
        assert "services" in status
        assert "repos" in status

    def test_get_status_includes_services(self, tmp_path: Path):
        """Test that status includes service information."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "svc1": ServiceConfig(run="sleep 100"),
                "svc2": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        status = runner.get_status()
        assert "svc1" in status["services"]
        assert "svc2" in status["services"]

    def test_get_status_includes_repos(self, tmp_path: Path):
        """Test that status includes repo information."""
        config = HanielConfig(
            poll_interval=5,
            repos={
                "repo1": RepoConfig(
                    url="git@github.com:test/test.git",
                    branch="main",
                    path="./repo1",
                ),
            },
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        status = runner.get_status()
        assert "repo1" in status["repos"]
