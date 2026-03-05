"""
Tests for haniel process management.

Tests cover:
- Process spawning and lifecycle
- Ready conditions (port, delay, log, http)
- Graceful shutdown
- Circuit breaker and backoff
"""

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import ServiceConfig, ServiceShutdownConfig
from haniel.core.health import HealthManager, ServiceState
from haniel.core.logs import LogManager, LogCapture
from haniel.platform import get_platform_handler
from haniel.core.process import ProcessManager, ReadyCondition, ReadyConditionType


# Path to the dummy server
DUMMY_SERVER = Path(__file__).parent / "fixtures" / "dummy_server.py"


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    """Create a temporary log directory."""
    log_path = tmp_path / "logs"
    log_path.mkdir()
    return log_path


@pytest.fixture
def process_manager(tmp_path: Path, log_dir: Path) -> ProcessManager:
    """Create a ProcessManager for testing."""
    return ProcessManager(
        config_dir=tmp_path,
        log_dir=log_dir,
    )


class TestReadyCondition:
    """Tests for ReadyCondition parsing."""

    def test_parse_port_condition(self):
        """Should parse port conditions correctly."""
        cond = ReadyCondition.parse("port:8080")
        assert cond.type == ReadyConditionType.PORT
        assert cond.value == "8080"

    def test_parse_delay_condition(self):
        """Should parse delay conditions correctly."""
        cond = ReadyCondition.parse("delay:5")
        assert cond.type == ReadyConditionType.DELAY
        assert cond.value == "5"

    def test_parse_log_condition(self):
        """Should parse log conditions correctly."""
        cond = ReadyCondition.parse("log:Server started")
        assert cond.type == ReadyConditionType.LOG
        assert cond.value == "Server started"

    def test_parse_http_condition(self):
        """Should parse http conditions correctly."""
        cond = ReadyCondition.parse("http:localhost:8080/health")
        assert cond.type == ReadyConditionType.HTTP
        assert cond.value == "localhost:8080/health"

    def test_parse_invalid_condition(self):
        """Should raise ValueError for invalid conditions."""
        with pytest.raises(ValueError):
            ReadyCondition.parse("invalid:condition")


class TestPlatformHandler:
    """Tests for platform-specific handlers."""

    def test_get_platform_handler(self):
        """Should return a valid platform handler."""
        handler = get_platform_handler()
        assert handler is not None

    def test_is_port_listening_false(self):
        """Should return False for non-listening port."""
        handler = get_platform_handler()
        # Use a high port that's unlikely to be in use
        assert handler.is_port_listening(59999) is False

    def test_subprocess_kwargs(self):
        """Should return valid subprocess kwargs."""
        handler = get_platform_handler()
        kwargs = handler.get_subprocess_kwargs()
        assert isinstance(kwargs, dict)


class TestLogCapture:
    """Tests for log capture functionality."""

    def test_log_capture_creates_file(self, log_dir: Path):
        """Should create a log file when started."""
        capture = LogCapture("test-service", log_dir)
        capture.start()
        try:
            assert capture.log_path is not None
            assert capture.log_path.exists()
        finally:
            capture.stop()

    def test_log_capture_writes_lines(self, log_dir: Path):
        """Should write lines to the buffer and file."""
        capture = LogCapture("test-service", log_dir)
        capture.start()
        try:
            capture.write_line("Test line 1")
            capture.write_line("Test line 2")

            lines = capture.get_recent_lines()
            assert len(lines) == 2
            assert "Test line 1" in lines[0]
            assert "Test line 2" in lines[1]
        finally:
            capture.stop()

    def test_log_capture_pattern_callback(self, log_dir: Path):
        """Should call callback when pattern is matched."""
        capture = LogCapture("test-service", log_dir)
        capture.start()

        matched = []
        def callback(line: str):
            matched.append(line)

        capture.add_pattern_callback("Ready", callback)

        try:
            capture.write_line("Starting...")
            capture.write_line("Server Ready!")
            capture.write_line("Done")

            # Give callback thread time to execute
            time.sleep(0.1)

            assert len(matched) == 1
            assert "Ready" in matched[0]
        finally:
            capture.stop()


class TestHealthManager:
    """Tests for health management."""

    def test_initial_state(self):
        """Should start in STOPPED state."""
        manager = HealthManager()
        health = manager.get_health("test-service")
        assert health.state == ServiceState.STOPPED

    def test_state_transitions(self):
        """Should track state transitions correctly."""
        manager = HealthManager()

        manager.record_start("test-service")
        assert manager.get_health("test-service").state == ServiceState.STARTING

        manager.record_ready("test-service")
        assert manager.get_health("test-service").state == ServiceState.READY

        manager.record_stop("test-service")
        assert manager.get_health("test-service").state == ServiceState.STOPPED

    def test_crash_increments_failures(self):
        """Should increment failure count on crash."""
        manager = HealthManager()

        manager.record_start("test-service")
        manager.record_crash("test-service", exit_code=1)

        health = manager.get_health("test-service")
        assert health.consecutive_failures == 1
        assert health.restart_count == 1

    def test_ready_resets_failures(self):
        """Should reset failure count when ready."""
        manager = HealthManager()

        # Simulate some failures
        manager.record_start("test-service")
        manager.record_crash("test-service", exit_code=1)
        manager.record_start("test-service")
        manager.record_crash("test-service", exit_code=1)

        assert manager.get_health("test-service").consecutive_failures == 2

        # Now succeed
        manager.record_start("test-service")
        manager.record_ready("test-service")

        assert manager.get_health("test-service").consecutive_failures == 0

    def test_circuit_breaker_trips(self):
        """Should trip circuit breaker after threshold failures."""
        manager = HealthManager(
            circuit_breaker_threshold=3,
            circuit_breaker_window=300,
        )

        # Trigger multiple failures
        for _ in range(3):
            manager.record_start("test-service")
            manager.record_crash("test-service", exit_code=1)

        health = manager.get_health("test-service")
        assert health.state == ServiceState.CIRCUIT_OPEN
        assert not manager.should_restart("test-service")

    def test_reset_circuit(self):
        """Should be able to reset circuit breaker."""
        manager = HealthManager(circuit_breaker_threshold=2)

        # Trip the circuit
        manager.record_start("test-service")
        manager.record_crash("test-service", exit_code=1)
        manager.record_start("test-service")
        manager.record_crash("test-service", exit_code=1)

        assert manager.get_health("test-service").state == ServiceState.CIRCUIT_OPEN

        # Reset it
        manager.reset_circuit("test-service")

        assert manager.get_health("test-service").state == ServiceState.STOPPED
        assert manager.should_restart("test-service")

    def test_exponential_backoff(self):
        """Should apply exponential backoff to restart delays."""
        # Use high threshold to prevent circuit breaker from tripping
        manager = HealthManager(base_delay=1, max_delay=60, circuit_breaker_threshold=10)

        delays = []
        for _ in range(5):
            manager.record_start("test-service")
            delay = manager.record_crash("test-service", exit_code=1)
            delays.append(delay)

        # Check exponential growth: 1, 2, 4, 8, 16
        assert delays == [1, 2, 4, 8, 16]

    def test_backoff_capped_at_max(self):
        """Should cap backoff at max_delay."""
        manager = HealthManager(base_delay=10, max_delay=30)

        for _ in range(5):
            manager.record_start("test-service")
            delay = manager.record_crash("test-service", exit_code=1)

        health = manager.get_health("test-service")
        assert health.current_backoff <= 30


class TestProcessManager:
    """Tests for ProcessManager."""

    def test_start_simple_process(self, process_manager: ProcessManager, tmp_path: Path):
        """Should start a simple process."""
        config = ServiceConfig(
            run=f"{sys.executable} -c \"import time; print('Hello'); time.sleep(60)\"",
        )

        managed = process_manager.start_service("test", config)

        try:
            assert managed.process is not None
            assert managed.process.poll() is None  # Still running
            assert process_manager.is_running("test")
        finally:
            process_manager.stop_service("test", force=True)

    def test_stop_process(self, process_manager: ProcessManager, tmp_path: Path):
        """Should stop a running process."""
        config = ServiceConfig(
            run=f"{sys.executable} -c \"import time; time.sleep(60)\"",
        )

        process_manager.start_service("test", config)
        assert process_manager.is_running("test")

        result = process_manager.stop_service("test", timeout=2)

        assert result is True
        assert not process_manager.is_running("test")

    def test_ready_condition_delay(self, process_manager: ProcessManager):
        """Should wait for delay condition."""
        config = ServiceConfig(
            run=f"{sys.executable} -c \"import time; time.sleep(60)\"",
            ready="delay:0.1",
        )

        managed = process_manager.start_service("test", config)

        try:
            # Should become ready almost immediately
            is_ready = process_manager.wait_for_ready("test", timeout=2)
            assert is_ready
        finally:
            process_manager.stop_service("test", force=True)

    @pytest.mark.skipif(
        not DUMMY_SERVER.exists(),
        reason="Dummy server not found",
    )
    def test_ready_condition_port(self, process_manager: ProcessManager):
        """Should detect port listening condition."""
        # Use a random high port
        port = 18080

        config = ServiceConfig(
            run=f"{sys.executable} {DUMMY_SERVER} --port {port}",
            ready=f"port:{port}",
        )

        managed = process_manager.start_service("test", config)

        try:
            is_ready = process_manager.wait_for_ready("test", timeout=10)
            assert is_ready

            # Verify port is actually listening
            handler = get_platform_handler()
            assert handler.is_port_listening(port)
        finally:
            process_manager.stop_service("test", timeout=2)

    @pytest.mark.skipif(
        not DUMMY_SERVER.exists(),
        reason="Dummy server not found",
    )
    def test_ready_condition_log(self, process_manager: ProcessManager):
        """Should detect log pattern condition."""
        port = 18081

        config = ServiceConfig(
            run=f"{sys.executable} {DUMMY_SERVER} --port {port} --ready-message 'Service is READY'",
            ready="log:READY",
        )

        managed = process_manager.start_service("test", config)

        try:
            is_ready = process_manager.wait_for_ready("test", timeout=10)
            assert is_ready
        finally:
            process_manager.stop_service("test", timeout=2)

    @pytest.mark.skipif(
        not DUMMY_SERVER.exists(),
        reason="Dummy server not found",
    )
    def test_ready_condition_http(self, process_manager: ProcessManager):
        """Should detect HTTP ready condition."""
        port = 18082

        config = ServiceConfig(
            run=f"{sys.executable} {DUMMY_SERVER} --port {port}",
            ready=f"http:localhost:{port}/health",
        )

        managed = process_manager.start_service("test", config)

        try:
            is_ready = process_manager.wait_for_ready("test", timeout=10)
            assert is_ready
        finally:
            process_manager.stop_service("test", timeout=2)

    def test_graceful_shutdown(self, process_manager: ProcessManager):
        """Should perform graceful shutdown with SIGTERM."""
        # Use a script that handles SIGTERM
        script = """
import signal
import sys
import time

def handler(signum, frame):
    print("Received SIGTERM, cleaning up...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handler)
print("Ready")
while True:
    time.sleep(0.1)
"""
        config = ServiceConfig(
            run=f"{sys.executable} -c \"{script}\"",
        )

        process_manager.start_service("test", config)
        time.sleep(0.5)  # Let it start

        result = process_manager.stop_service("test", timeout=5)

        assert result is True
        assert not process_manager.is_running("test")

    def test_force_kill(self, process_manager: ProcessManager):
        """Should force kill when graceful shutdown fails."""
        # Use a script that ignores SIGTERM (only works on POSIX)
        script = """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.1)
"""
        config = ServiceConfig(
            run=f"{sys.executable} -c \"{script}\"",
        )

        process_manager.start_service("test", config)
        time.sleep(0.5)

        result = process_manager.stop_service("test", force=True)

        assert result is True
        assert not process_manager.is_running("test")

    def test_stop_all(self, process_manager: ProcessManager):
        """Should stop all running services."""
        for i in range(3):
            config = ServiceConfig(
                run=f"{sys.executable} -c \"import time; time.sleep(60)\"",
            )
            process_manager.start_service(f"test-{i}", config)

        assert process_manager.is_running("test-0")
        assert process_manager.is_running("test-1")
        assert process_manager.is_running("test-2")

        process_manager.stop_all(timeout=2)

        assert not process_manager.is_running("test-0")
        assert not process_manager.is_running("test-1")
        assert not process_manager.is_running("test-2")

    def test_crash_detection(self, process_manager: ProcessManager):
        """Should detect when a process crashes."""
        crashed = []

        def on_crash(exit_code):
            crashed.append(exit_code)

        config = ServiceConfig(
            run=f"{sys.executable} -c \"import sys; sys.exit(1)\"",
        )

        process_manager.start_service("test", config, on_crash=on_crash)

        # Wait for crash
        time.sleep(0.5)

        assert len(crashed) == 1
        assert crashed[0] == 1

    def test_ready_callback(self, process_manager: ProcessManager):
        """Should call ready callback when service is ready."""
        ready_called = []

        def on_ready():
            ready_called.append(True)

        config = ServiceConfig(
            run=f"{sys.executable} -c \"import time; time.sleep(60)\"",
            ready="delay:0.1",
        )

        process_manager.start_service("test", config, on_ready=on_ready)

        try:
            process_manager.wait_for_ready("test", timeout=2)
            assert len(ready_called) == 1
        finally:
            process_manager.stop_service("test", force=True)

    def test_working_directory(self, process_manager: ProcessManager, tmp_path: Path):
        """Should use the specified working directory."""
        # Create a subdirectory
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()

        # Create a marker file
        (work_dir / "marker.txt").write_text("test")

        config = ServiceConfig(
            run=f"{sys.executable} -c \"import os; print(os.getcwd()); import time; time.sleep(60)\"",
            cwd="workdir",
        )

        managed = process_manager.start_service("test", config)

        try:
            time.sleep(0.5)
            lines = managed.log_capture.get_recent_lines()
            # Check that the cwd was set correctly
            assert any("workdir" in line for line in lines)
        finally:
            process_manager.stop_service("test", force=True)

    def test_already_running_raises(self, process_manager: ProcessManager):
        """Should raise error when starting already running service."""
        config = ServiceConfig(
            run=f"{sys.executable} -c \"import time; time.sleep(60)\"",
        )

        process_manager.start_service("test", config)

        try:
            with pytest.raises(RuntimeError):
                process_manager.start_service("test", config)
        finally:
            process_manager.stop_service("test", force=True)


class TestLogManager:
    """Tests for LogManager."""

    def test_get_capture(self, log_dir: Path):
        """Should create and return log captures."""
        manager = LogManager(log_dir)

        capture1 = manager.get_capture("service-a")
        capture2 = manager.get_capture("service-b")

        assert capture1 is not capture2
        assert manager.get_capture("service-a") is capture1

    def test_start_capture(self, log_dir: Path):
        """Should start log capture for a service."""
        manager = LogManager(log_dir)

        capture = manager.start_capture("test-service")

        assert capture.log_path is not None
        assert capture.log_path.exists()

        manager.stop_all()

    def test_stop_all(self, log_dir: Path):
        """Should stop all captures."""
        manager = LogManager(log_dir)

        manager.start_capture("service-a")
        manager.start_capture("service-b")

        manager.stop_all()

        # Files should still exist
        assert (log_dir / "service-a.log").exists()
        assert (log_dir / "service-b.log").exists()


class TestPosixHandler:
    """Tests for PosixHandler."""

    def test_terminate_process_already_terminated(self):
        """Should handle already terminated process gracefully."""
        from haniel.platform.posix import PosixHandler

        handler = PosixHandler()

        # Create a mock process that has already terminated
        mock_process = MagicMock()
        mock_process.poll.return_value = 0  # Already exited

        # Should not raise
        handler.terminate_process(mock_process)

    def test_kill_process_already_terminated(self):
        """Should handle already terminated process gracefully."""
        from haniel.platform.posix import PosixHandler

        handler = PosixHandler()

        mock_process = MagicMock()
        mock_process.poll.return_value = 0  # Already exited

        # Should not raise
        handler.kill_process(mock_process)

    def test_setup_process_group(self):
        """Should do nothing (process group set via Popen kwargs)."""
        from haniel.platform.posix import PosixHandler

        handler = PosixHandler()
        mock_process = MagicMock()

        # Should not raise
        handler.setup_process_group(mock_process)

    def test_is_port_listening_socket_error(self):
        """Should return False on socket error."""
        from haniel.platform.posix import PosixHandler
        import socket

        handler = PosixHandler()

        with patch("socket.socket") as mock_socket_class:
            mock_socket = MagicMock()
            mock_socket.connect_ex.side_effect = socket.error("Connection refused")
            mock_socket_class.return_value = mock_socket

            result = handler.is_port_listening(8080)

            assert result is False
            mock_socket.close.assert_called_once()


class TestCliDryRunInstall:
    """Tests for CLI dry-run install functionality."""

    def test_print_dry_run_install_requirements(self):
        """Test dry-run install shows requirements."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, InstallConfig

        config = HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.10", "node": ">=18.0"}
            )
        )

        # Should not raise
        print_dry_run_install(config)

    def test_print_dry_run_install_directories(self):
        """Test dry-run install shows directories."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, InstallConfig

        config = HanielConfig(
            install=InstallConfig(
                directories=["./logs", "./data"]
            )
        )

        # Should not raise
        print_dry_run_install(config)

    def test_print_dry_run_install_repos(self):
        """Test dry-run install shows repositories."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, RepoConfig

        config = HanielConfig(
            repos={
                "myrepo": RepoConfig(
                    url="https://github.com/test/test.git",
                    path="./repos/myrepo"
                )
            }
        )

        # Should not raise
        print_dry_run_install(config)

    def test_print_dry_run_install_environments(self):
        """Test dry-run install shows environments."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, InstallConfig, EnvironmentConfig

        config = HanielConfig(
            install=InstallConfig(
                environments={
                    "myenv": EnvironmentConfig(type="python", path="./.venv"),
                    "nodeenv": EnvironmentConfig(type="npm", path="./node_modules")
                }
            )
        )

        # Should not raise
        print_dry_run_install(config)

    def test_print_dry_run_install_static_configs(self):
        """Test dry-run install shows static config files."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, InstallConfig, ConfigFileConfig

        config = HanielConfig(
            install=InstallConfig(
                configs={
                    "myconfig": ConfigFileConfig(
                        path="./config.yaml",
                        content="key: value"
                    )
                }
            )
        )

        # Should not raise
        print_dry_run_install(config)

    def test_print_dry_run_install_interactive_configs(self):
        """Test dry-run install shows interactive config files."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, InstallConfig, ConfigFileConfig, ConfigKeyConfig

        config = HanielConfig(
            install=InstallConfig(
                configs={
                    "env": ConfigFileConfig(
                        path="./.env",
                        keys=[
                            ConfigKeyConfig(key="API_KEY", prompt="Enter API key"),
                            ConfigKeyConfig(key="DEBUG", default="false")
                        ]
                    )
                }
            )
        )

        # Should not raise
        print_dry_run_install(config)

    def test_print_dry_run_install_service(self):
        """Test dry-run install shows service registration."""
        from haniel.cli import print_dry_run_install
        from haniel.config import HanielConfig, InstallConfig, ServiceDefinitionConfig

        config = HanielConfig(
            install=InstallConfig(
                service=ServiceDefinitionConfig(
                    name="myservice",
                    display="My Service"
                )
            )
        )

        # Should not raise
        print_dry_run_install(config)


class TestCliDryRunRun:
    """Tests for CLI dry-run run functionality."""

    def test_print_dry_run_run_basic(self):
        """Test dry-run run shows basic config."""
        from haniel.cli import print_dry_run_run
        from haniel.config import HanielConfig

        config = HanielConfig(poll_interval=60)

        # Should not raise
        print_dry_run_run(config)

    def test_print_dry_run_run_repos(self):
        """Test dry-run run shows repositories."""
        from haniel.cli import print_dry_run_run
        from haniel.config import HanielConfig, RepoConfig

        config = HanielConfig(
            repos={
                "repo1": RepoConfig(
                    url="https://github.com/test/test1.git",
                    path="./repos/test1",
                    branch="main"
                ),
                "repo2": RepoConfig(
                    url="https://github.com/test/test2.git",
                    path="./repos/test2"
                )
            }
        )

        # Should not raise
        print_dry_run_run(config)

    def test_print_dry_run_run_services_with_deps(self):
        """Test dry-run run shows services with dependencies."""
        from haniel.cli import print_dry_run_run
        from haniel.config import HanielConfig, ServiceConfig

        config = HanielConfig(
            services={
                "db": ServiceConfig(run="start-db"),
                "api": ServiceConfig(run="start-api", after=["db"]),
                "worker": ServiceConfig(run="start-worker", after=["db", "api"])
            }
        )

        # Should not raise
        print_dry_run_run(config)

    def test_print_dry_run_run_disabled_service(self):
        """Test dry-run run shows disabled services."""
        from haniel.cli import print_dry_run_run
        from haniel.config import HanielConfig, ServiceConfig

        config = HanielConfig(
            services={
                "enabled": ServiceConfig(run="start-enabled"),
                "disabled": ServiceConfig(run="start-disabled", enabled=False)
            }
        )

        # Should not raise
        print_dry_run_run(config)


class TestCliHelpers:
    """Tests for CLI helper functions."""

    def test_validate_config_file_none(self):
        """Test validate_config_file returns None for None input."""
        from haniel.cli import validate_config_file

        result = validate_config_file(None, None, None)
        assert result is None

    def test_validate_config_file_not_found(self):
        """Test validate_config_file raises for non-existent file."""
        import click
        from haniel.cli import validate_config_file

        with pytest.raises(click.BadParameter):
            validate_config_file(None, None, "/nonexistent/config.yaml")

    def test_validate_config_file_exists(self, tmp_path: Path):
        """Test validate_config_file returns Path for existing file."""
        from haniel.cli import validate_config_file

        config_file = tmp_path / "config.yaml"
        config_file.write_text("poll_interval: 30")

        result = validate_config_file(None, None, str(config_file))

        assert result == config_file

    def test_load_and_validate_pydantic_error(self, tmp_path: Path):
        """Test load_and_validate returns errors for invalid schema."""
        from haniel.cli import load_and_validate

        config_file = tmp_path / "config.yaml"
        config_file.write_text("poll_interval: not_a_number")

        config, errors = load_and_validate(config_file)

        assert config is None
        assert len(errors) > 0
        assert any("Schema error" in e for e in errors)

    def test_load_and_validate_load_error(self, tmp_path: Path):
        """Test load_and_validate handles load errors."""
        from haniel.cli import load_and_validate

        config_file = tmp_path / "config.yaml"
        config_file.write_text("invalid: yaml: content: [")

        config, errors = load_and_validate(config_file)

        assert config is None
        assert len(errors) > 0

    def test_load_and_validate_semantic_error(self, tmp_path: Path):
        """Test load_and_validate returns semantic validation errors."""
        from haniel.cli import load_and_validate

        # Create config with circular dependency
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
services:
  svc-a:
    run: echo a
    after: [svc-b]
  svc-b:
    run: echo b
    after: [svc-a]
""")

        config, errors = load_and_validate(config_file)

        # Should have config but with semantic errors
        assert config is not None
        assert len(errors) > 0
