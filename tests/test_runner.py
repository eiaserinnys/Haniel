"""Tests for the haniel runner module."""

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import (
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    BackoffConfig,
    HooksConfig,
)
from haniel.core.runner import (
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
            "bot": ServiceConfig(run="cmd", after=["mcp-app"]),
            "mcp-app": ServiceConfig(run="cmd"),
            "mcp-slack": ServiceConfig(run="cmd"),
            "rescue-bot": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)
        order = graph.topological_sort()

        assert order.index("mcp-app") < order.index("bot")
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

    def test_runner_shutdown_order(
        self, config_with_deps: HanielConfig, tmp_path: Path
    ):
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
                "both-deps": ServiceConfig(
                    run="cmd", repo="main-repo", after=["main-service"]
                ),
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

    @patch("haniel.core.process.ProcessManager.start_service")
    @patch("subprocess.run")
    def test_pre_start_hook_success_allows_start(
        self, mock_run: MagicMock, mock_start: MagicMock, tmp_path: Path
    ):
        """pre_start 훅 exit 0 → 서비스 정상 기동."""
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test-service": ServiceConfig(
                    run="sleep 100",
                    hooks=HooksConfig(pre_start="echo hi"),
                ),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        result = runner._start_service("test-service")

        mock_run.assert_called_once()
        mock_start.assert_called_once()
        assert result is True

    @patch("haniel.core.process.ProcessManager.start_service")
    @patch("subprocess.run")
    def test_pre_start_hook_failure_aborts_start(
        self, mock_run: MagicMock, mock_start: MagicMock, tmp_path: Path
    ):
        """pre_start 훅 exit 1 → _start_service() False 반환, process_manager 미호출."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "false")
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test-service": ServiceConfig(
                    run="sleep 100",
                    hooks=HooksConfig(pre_start="false"),
                ),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        result = runner._start_service("test-service")

        mock_start.assert_not_called()
        assert result is False

    @patch("haniel.core.process.ProcessManager.start_service")
    def test_no_pre_start_hook_starts_normally(
        self, mock_start: MagicMock, tmp_path: Path
    ):
        """pre_start 훅 없을 때 → 서비스 정상 기동."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test-service": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        result = runner._start_service("test-service")

        mock_start.assert_called_once()
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


# --- Extended Runner Tests ---


class TestDependencyGraphExtended:
    """Extended tests for DependencyGraph."""

    def test_get_nonexistent_dependents(self):
        """Get dependents for nonexistent service."""
        services = {
            "web": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)

        dependents = graph.get_dependents("nonexistent")
        assert dependents == []

    def test_get_nonexistent_dependencies(self):
        """Get dependencies for nonexistent service."""
        services = {
            "web": ServiceConfig(run="cmd"),
        }
        graph = DependencyGraph(services)

        deps = graph.get_dependencies("nonexistent")
        assert deps == []

    def test_get_all_dependents_transitive(self):
        """Get all transitive dependents."""
        services = {
            "db": ServiceConfig(run="cmd"),
            "cache": ServiceConfig(run="cmd", after=["db"]),
            "api": ServiceConfig(run="cmd", after=["cache"]),
            "web": ServiceConfig(run="cmd", after=["api"]),
        }
        graph = DependencyGraph(services)

        all_deps = graph.get_all_dependents("db")
        assert all_deps == {"cache", "api", "web"}


class TestServiceRunnerExtended:
    """Extended tests for ServiceRunner."""

    @pytest.fixture
    def runner_with_repo(self, tmp_path: Path):
        """Create a runner with a repo."""
        # Create a fake git repo
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        git_dir = repo_path / ".git"
        git_dir.mkdir()

        config = HanielConfig(
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
        return ServiceRunner(config, config_dir=tmp_path)

    @patch("subprocess.run")
    def test_hook_timeout(self, mock_run: MagicMock, tmp_path: Path):
        """Test hook timeout handling."""
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 300)

        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test-service": ServiceConfig(
                    run="sleep 100",
                    hooks=HooksConfig(post_pull="slow_command"),
                ),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)
        result = runner.execute_hook("test-service", "post_pull")

        assert result is False

    @patch("subprocess.run")
    def test_hook_generic_exception(self, mock_run: MagicMock, tmp_path: Path):
        """Test hook generic exception handling."""
        mock_run.side_effect = Exception("Something went wrong")

        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test-service": ServiceConfig(
                    run="sleep 100",
                    hooks=HooksConfig(post_pull="bad_command"),
                ),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)
        result = runner.execute_hook("test-service", "post_pull")

        assert result is False

    def test_execute_hook_disabled_service(self, tmp_path: Path):
        """Test hook execution for disabled service."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "disabled-service": ServiceConfig(
                    run="sleep 100",
                    enabled=False,
                    hooks=HooksConfig(post_pull="echo test"),
                ),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)
        result = runner.execute_hook("disabled-service", "post_pull")

        # Should return True because service is not in enabled services
        assert result is True

    def test_get_status_structure(self, tmp_path: Path):
        """Test that get_status returns proper structure."""
        config = HanielConfig(
            poll_interval=60,
            repos={
                "repo1": RepoConfig(
                    url="git@github.com:test/test.git",
                    branch="main",
                    path="./repo1",
                ),
            },
            services={
                "svc1": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        status = runner.get_status()

        assert "running" in status
        assert "start_time" in status
        assert "last_poll" in status
        assert "poll_count" in status
        assert "poll_interval" in status
        assert "services" in status
        assert "repos" in status

    def test_runner_is_running_property(self, tmp_path: Path):
        """Test is_running property."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        assert runner.is_running is False

    @patch("haniel.core.runner.ServiceRunner._start_mcp_server")
    @patch("haniel.core.runner.ServiceRunner.start_services")
    def test_runner_start_stop(self, mock_start_services, mock_mcp, tmp_path: Path):
        """Test starting and stopping the runner."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Start
        runner.start()
        assert runner.is_running is True

        # Stop
        runner.stop()
        assert runner.is_running is False

    @patch("haniel.core.runner.ServiceRunner._start_mcp_server")
    @patch("haniel.core.runner.ServiceRunner.start_services")
    def test_runner_start_already_running(
        self, mock_start_services, mock_mcp, tmp_path: Path
    ):
        """Test starting when already running."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        runner.start()
        mock_start_services.reset_mock()

        # Start again - should not start services again
        runner.start()
        mock_start_services.assert_not_called()

        runner.stop()

    def test_runner_stop_not_running(self, tmp_path: Path):
        """Test stopping when not running."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Should not raise
        runner.stop()


class TestCyclicDependencyError:
    """Tests for CyclicDependencyError."""

    def test_error_message(self):
        """Test error message format."""
        cycle = ["a", "b", "a"]
        error = CyclicDependencyError(cycle)

        assert "a -> b -> a" in str(error)
        assert error.cycle == cycle


class TestServiceRunnerPollCycle:
    """Tests for ServiceRunner poll cycle."""

    @pytest.fixture
    def runner_with_mock_repo(self, tmp_path: Path):
        """Create a runner with a mock repo."""
        # Create a fake git repo
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        git_dir = repo_path / ".git"
        git_dir.mkdir()

        config = HanielConfig(
            poll_interval=1,
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
        return ServiceRunner(config, config_dir=tmp_path)

    @patch("haniel.core.runner.fetch_repo")
    @patch("haniel.core.runner.get_head")
    @patch("haniel.core.runner.get_remote_head")
    def test_detect_changes_no_changes(
        self, mock_remote_head, mock_head, mock_fetch, runner_with_mock_repo
    ):
        """Test detecting no changes in repos."""
        mock_fetch.return_value = False  # No changes
        mock_head.return_value = "abc1234"
        mock_remote_head.return_value = "abc1234"  # Remote == current, no new commits

        runner_with_mock_repo._init_repo_states()
        changed = runner_with_mock_repo._detect_changes()

        assert changed == []

    @patch("haniel.core.runner.get_pending_changes", return_value=None)
    @patch("haniel.core.runner.get_remote_head")
    @patch("haniel.core.runner.fetch_repo")
    @patch("haniel.core.runner.get_head")
    def test_detect_changes_with_changes(
        self, mock_head, mock_fetch, mock_remote_head, mock_pending, runner_with_mock_repo
    ):
        """Test detecting changes in repos."""
        mock_fetch.return_value = True
        mock_head.return_value = "abc1234"
        mock_remote_head.return_value = "def5678"  # Remote ahead of current

        runner_with_mock_repo._init_repo_states()
        changed = runner_with_mock_repo._detect_changes()

        assert "test-repo" in changed

    @patch("haniel.core.runner.fetch_repo")
    @patch("haniel.core.runner.get_head")
    def test_detect_changes_fetch_error(
        self, mock_head, mock_fetch, runner_with_mock_repo
    ):
        """Test handling fetch errors."""
        from haniel.core.git import GitError

        mock_fetch.side_effect = GitError("Fetch failed")
        mock_head.return_value = "abc1234"

        runner_with_mock_repo._init_repo_states()
        changed = runner_with_mock_repo._detect_changes()

        assert changed == []
        # Check error was recorded
        state = runner_with_mock_repo._repo_states["test-repo"]
        assert state.fetch_error is not None

    def test_schedule_restart(self, tmp_path: Path):
        """Test scheduling a service restart."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        runner._schedule_restart("test", 5.0)

        with runner._restart_lock:
            assert "test" in runner._pending_restarts

    @patch("haniel.core.runner.ServiceRunner._start_service")
    def test_process_pending_restarts(self, mock_start, tmp_path: Path):
        """Test processing pending restarts."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Schedule restart in the past
        runner._pending_restarts["test"] = time.time() - 1

        runner._process_pending_restarts()

        mock_start.assert_called_with("test")

    @patch("haniel.core.runner.pull_repo")
    @patch("haniel.core.runner.get_head")
    def test_pull_repo_success(self, mock_head, mock_pull, runner_with_mock_repo):
        """Test pulling a repo successfully."""
        mock_head.return_value = "new_commit"

        result = runner_with_mock_repo._pull_repo("test-repo")

        assert result is True
        mock_pull.assert_called_once()

    @patch("haniel.core.runner.pull_repo")
    def test_pull_repo_failure(self, mock_pull, runner_with_mock_repo):
        """Test pulling a repo with failure."""
        from haniel.core.git import GitError

        mock_pull.side_effect = GitError("Pull failed")

        result = runner_with_mock_repo._pull_repo("test-repo")

        assert result is False

    def test_pull_repo_unknown(self, tmp_path: Path):
        """Test pulling unknown repo."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        result = runner._pull_repo("unknown")

        assert result is False


class TestServiceRunnerMcp:
    """Tests for ServiceRunner MCP integration."""

    @patch("haniel.core.runner.ServiceRunner._start_mcp_server")
    @patch("haniel.core.runner.ServiceRunner.start_services")
    def test_start_with_mcp_disabled(
        self, mock_start_services, mock_mcp, tmp_path: Path
    ):
        """Test starting runner with MCP disabled."""
        from haniel.config import McpConfig

        config = HanielConfig(
            poll_interval=5,
            mcp=McpConfig(enabled=False),
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        runner.start()
        runner.stop()

        mock_mcp.assert_called_once()

    def test_start_mcp_server_disabled(self, tmp_path: Path):
        """Test _start_mcp_server when disabled."""
        from haniel.config import McpConfig

        config = HanielConfig(
            poll_interval=5,
            mcp=McpConfig(enabled=False),
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Should not raise
        runner._start_mcp_server()

        # MCP server should not be set
        assert runner._mcp_server is None

    @patch("haniel.integrations.mcp_server.HanielMcpServer")
    def test_start_mcp_server_enabled(self, mock_mcp_class, tmp_path: Path):
        """Test _start_mcp_server when enabled."""
        from haniel.config import McpConfig

        config = HanielConfig(
            poll_interval=5,
            mcp=McpConfig(enabled=True, port=3200),
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        mock_server = MagicMock()
        mock_mcp_class.return_value = mock_server

        runner._start_mcp_server()

        mock_server.start_background.assert_called_once()

    @patch("haniel.integrations.mcp_server.HanielMcpServer")
    def test_start_mcp_server_import_error(self, mock_mcp_class, tmp_path: Path):
        """Test _start_mcp_server with import error."""
        from haniel.config import McpConfig

        config = HanielConfig(
            poll_interval=5,
            mcp=McpConfig(enabled=True, port=3200),
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        mock_mcp_class.side_effect = ImportError("No module")

        # Should not raise
        runner._start_mcp_server()

    @patch("haniel.integrations.mcp_server.HanielMcpServer")
    def test_start_mcp_server_exception(self, mock_mcp_class, tmp_path: Path):
        """Test _start_mcp_server with exception."""
        from haniel.config import McpConfig

        config = HanielConfig(
            poll_interval=5,
            mcp=McpConfig(enabled=True, port=3200),
            repos={},
            services={},
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        mock_mcp_class.side_effect = Exception("Server failed")

        # Should not raise
        runner._start_mcp_server()


class TestServiceRunnerServices:
    """Tests for ServiceRunner service management."""

    @patch("haniel.core.process.ProcessManager.start_service")
    def test_start_services_order(self, mock_start, tmp_path: Path):
        """Test starting services in order."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "db": ServiceConfig(run="sleep 100"),
                "api": ServiceConfig(run="sleep 100", after=["db"]),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        runner.start_services()

        # Should be called for both services
        assert mock_start.call_count == 2

    @patch("haniel.core.process.ProcessManager.stop_service")
    @patch("haniel.core.process.ProcessManager.is_running")
    def test_stop_services_order(self, mock_running, mock_stop, tmp_path: Path):
        """Test stopping services in order."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "db": ServiceConfig(run="sleep 100"),
                "api": ServiceConfig(run="sleep 100", after=["db"]),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        mock_running.return_value = True

        runner.stop_services()

        # Should be called in reverse order
        assert mock_stop.call_count == 2

    @patch("haniel.core.process.ProcessManager.start_service")
    def test_start_service_failure(self, mock_start, tmp_path: Path):
        """Test handling service start failure."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        mock_start.side_effect = Exception("Start failed")

        result = runner._start_service("test")

        assert result is False

    def test_start_service_not_enabled(self, tmp_path: Path):
        """Test starting a service that doesn't exist in enabled services."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "disabled": ServiceConfig(run="sleep 100", enabled=False),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        result = runner._start_service("disabled")

        assert result is False


class TestServiceRunnerCallbacks:
    """Tests for ServiceRunner callbacks."""

    def test_on_service_ready_callback(self, tmp_path: Path):
        """Test on_service_ready callback."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Should not raise
        runner._on_service_ready("test")

    @patch("haniel.core.runner.ServiceRunner._schedule_restart")
    def test_on_service_crash_with_restart(self, mock_schedule, tmp_path: Path):
        """Test on_service_crash when restart is allowed."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
            backoff=BackoffConfig(base_delay=1.0, max_delay=10.0),
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Mock should_restart to return True
        runner.health_manager.should_restart = MagicMock(return_value=True)
        runner.health_manager.get_health = MagicMock(
            return_value=MagicMock(get_restart_delay=MagicMock(return_value=1.0))
        )

        runner._on_service_crash("test", 1)

        mock_schedule.assert_called_once()

    def test_on_service_crash_circuit_breaker_open(self, tmp_path: Path):
        """Test on_service_crash when circuit breaker is open."""
        config = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "test": ServiceConfig(run="sleep 100"),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        # Mock should_restart to return False (circuit breaker open)
        runner.health_manager.should_restart = MagicMock(return_value=False)

        # Should not raise
        runner._on_service_crash("test", 1)


# --- reload_config Tests ---


class TestReloadConfig:
    """Tests for ServiceRunner.reload_config()."""

    def _write_yaml(self, path: Path, config: HanielConfig) -> None:
        import yaml

        data = config.model_dump(by_alias=True, exclude_none=True, mode="python")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
            )

    def test_raises_when_config_path_not_set(self, tmp_path: Path):
        """reload_config() raises RuntimeError when config_path is None."""
        config = HanielConfig(poll_interval=5, repos={}, services={})
        runner = ServiceRunner(config, config_dir=tmp_path)  # no config_path
        with pytest.raises(RuntimeError, match="config_path is not set"):
            runner.reload_config()

    def test_updates_poll_interval(self, tmp_path: Path):
        """reload_config() picks up a changed poll_interval."""
        config_file = tmp_path / "haniel.yaml"
        original = HanielConfig(poll_interval=60, repos={}, services={})
        self._write_yaml(config_file, original)

        runner = ServiceRunner(original, config_dir=tmp_path, config_path=config_file)
        assert runner.poll_interval == 60

        updated = HanielConfig(poll_interval=30, repos={}, services={})
        self._write_yaml(config_file, updated)

        runner.reload_config()
        assert runner.poll_interval == 30

    def test_adds_new_service_to_enabled(self, tmp_path: Path):
        """reload_config() includes a newly added service in _enabled_services."""
        config_file = tmp_path / "haniel.yaml"
        original = HanielConfig(
            poll_interval=5,
            repos={},
            services={"web": ServiceConfig(run="python -m http.server")},
        )
        self._write_yaml(config_file, original)

        runner = ServiceRunner(original, config_dir=tmp_path, config_path=config_file)
        assert "worker" not in runner._enabled_services

        updated = HanielConfig(
            poll_interval=5,
            repos={},
            services={
                "web": ServiceConfig(run="python -m http.server"),
                "worker": ServiceConfig(run="python worker.py", after=["web"]),
            },
        )
        self._write_yaml(config_file, updated)

        runner.reload_config()
        assert "worker" in runner._enabled_services

    def test_removes_deleted_repo_from_states(self, tmp_path: Path):
        """reload_config() removes a repo that was deleted from config."""
        from haniel.config import RepoConfig

        config_file = tmp_path / "haniel.yaml"
        original = HanielConfig(
            poll_interval=5,
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo")
            },
            services={},
        )
        self._write_yaml(config_file, original)

        runner = ServiceRunner(original, config_dir=tmp_path, config_path=config_file)
        assert "main" in runner._repo_states

        updated = HanielConfig(poll_interval=5, repos={}, services={})
        self._write_yaml(config_file, updated)

        runner.reload_config()
        assert "main" not in runner._repo_states

    def test_preserves_repo_fetch_state(self, tmp_path: Path):
        """reload_config() preserves last_head / last_fetch for surviving repos."""
        from datetime import datetime
        from haniel.config import RepoConfig

        config_file = tmp_path / "haniel.yaml"
        original = HanielConfig(
            poll_interval=5,
            repos={
                "main": RepoConfig(url="git@github.com:test/repo.git", path="./repo")
            },
            services={},
        )
        self._write_yaml(config_file, original)

        runner = ServiceRunner(original, config_dir=tmp_path, config_path=config_file)
        # Simulate a fetch having occurred
        runner._repo_states["main"].last_head = "abc12345"
        runner._repo_states["main"].last_fetch = datetime(2026, 1, 1)

        # Reload with same repo (branch changed)
        updated = HanielConfig(
            poll_interval=5,
            repos={
                "main": RepoConfig(
                    url="git@github.com:test/repo.git", path="./repo", branch="develop"
                )
            },
            services={},
        )
        self._write_yaml(config_file, updated)

        runner.reload_config()

        assert runner._repo_states["main"].last_head == "abc12345"
        assert runner._repo_states["main"].config.branch == "develop"
