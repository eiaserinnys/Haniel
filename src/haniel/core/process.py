"""
Process management for haniel services.

This module handles:
- Process spawning and lifecycle management
- Ready condition detection (port, delay, log, http)
- Graceful shutdown (SIGTERM → timeout → SIGKILL)
- HTTP shutdown support

haniel doesn't care what it runs. It just starts, monitors, and stops processes
as specified in the configuration.
"""

import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from ..config import ServiceConfig, ShutdownConfig
from .health import HealthManager, ServiceState
from .logs import LogCapture, LogManager, StreamReader
from ..platform import get_platform_handler

logger = logging.getLogger(__name__)


class ReadyConditionType(Enum):
    """Types of ready conditions."""

    PORT = "port"
    DELAY = "delay"
    LOG = "log"
    HTTP = "http"


@dataclass
class ReadyCondition:
    """Parsed ready condition."""

    type: ReadyConditionType
    value: str

    @classmethod
    def parse(cls, condition: str) -> "ReadyCondition":
        """Parse a ready condition string.

        Args:
            condition: Condition string like "port:8080", "delay:5", "log:Ready", "http://..."

        Returns:
            ReadyCondition instance

        Raises:
            ValueError: If the condition format is invalid
        """
        if condition.startswith("port:"):
            return cls(ReadyConditionType.PORT, condition[5:])
        elif condition.startswith("delay:"):
            return cls(ReadyConditionType.DELAY, condition[6:])
        elif condition.startswith("log:"):
            return cls(ReadyConditionType.LOG, condition[4:])
        elif condition.startswith("http:"):
            return cls(ReadyConditionType.HTTP, condition[5:])
        else:
            raise ValueError(f"Unknown ready condition format: {condition}")


@dataclass
class ManagedProcess:
    """A process managed by haniel."""

    name: str
    config: ServiceConfig
    process: subprocess.Popen | None = None
    log_capture: LogCapture | None = None
    stdout_reader: StreamReader | None = None
    stderr_reader: StreamReader | None = None
    ready_event: threading.Event | None = None
    _ready_callback_added: bool = False


class ProcessManager:
    """Manages the lifecycle of service processes.

    Responsibilities:
    - Start/stop processes
    - Monitor process health
    - Handle ready conditions
    - Graceful shutdown
    """

    DEFAULT_READY_TIMEOUT = 60  # seconds
    DEFAULT_SHUTDOWN_TIMEOUT = 10  # seconds
    DEFAULT_KILL_TIMEOUT = 30  # seconds
    POLL_INTERVAL = 0.1  # seconds

    def __init__(
        self,
        config_dir: Path,
        log_dir: Path | None = None,
        shutdown_config: ShutdownConfig | None = None,
        health_manager: HealthManager | None = None,
    ):
        """Initialize the process manager.

        Args:
            config_dir: Base directory for resolving relative paths
            log_dir: Directory for log files (default: config_dir/logs)
            shutdown_config: Global shutdown configuration
            health_manager: Health manager for state tracking
        """
        self.config_dir = config_dir
        self.log_dir = log_dir or config_dir / "logs"
        self.shutdown_config = shutdown_config or ShutdownConfig()
        self.health_manager = health_manager or HealthManager()
        self.log_manager = LogManager(self.log_dir)
        self.platform = get_platform_handler()

        self._processes: dict[str, ManagedProcess] = {}
        self._lock = threading.Lock()

    def start_service(
        self,
        name: str,
        config: ServiceConfig,
        ready_timeout: float | None = None,
        on_ready: Callable[[], None] | None = None,
        on_crash: Callable[[int | None], None] | None = None,
    ) -> ManagedProcess:
        """Start a service process.

        Args:
            name: Service name
            config: Service configuration
            ready_timeout: Timeout for ready condition (default: 60s)
            on_ready: Callback when service is ready
            on_crash: Callback when service crashes

        Returns:
            ManagedProcess instance

        Raises:
            RuntimeError: If the service is already running
        """
        with self._lock:
            if name in self._processes and self._processes[name].process:
                if self._processes[name].process.poll() is None:
                    raise RuntimeError(f"Service {name} is already running")

        # Resolve working directory
        cwd = self.config_dir
        if config.cwd:
            cwd = self.config_dir / config.cwd

        # Start log capture
        log_capture = self.log_manager.start_capture(name)

        # Record service starting
        self.health_manager.record_start(name)

        # Parse command
        # On Windows, subprocess.Popen accepts a string directly and delegates
        # argument parsing to CreateProcess, avoiding shlex.split() mishandling
        # backslash path separators. On POSIX, shlex.split() is correct.
        if os.name == "nt":
            cmd = config.run
        else:
            cmd = shlex.split(config.run)

        # Get platform-specific subprocess kwargs
        popen_kwargs = self.platform.get_subprocess_kwargs()

        # On Windows, use shell=True for .cmd/.bat commands (pnpm, npx, etc.)
        # but not for direct .exe paths, where shell=True causes issues with
        # ./ relative paths ("'.' is not recognized as a command").
        if os.name == "nt":
            first_token = config.run.split()[0] if config.run else ""
            resolved = shutil.which(first_token)
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                popen_kwargs["shell"] = True

        # Start the process
        try:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **popen_kwargs,
            )
        except PermissionError:
            # CREATE_BREAKAWAY_FROM_JOB requires specific Job Object
            # permissions. Retry without breakaway flag if denied.
            if os.name == "nt" and "creationflags" in popen_kwargs:
                from haniel.platform.windows import (
                    CREATE_BREAKAWAY_FROM_JOB,
                )

                flags = popen_kwargs["creationflags"]
                if flags & CREATE_BREAKAWAY_FROM_JOB:
                    popen_kwargs["creationflags"] = flags & ~CREATE_BREAKAWAY_FROM_JOB
                    logger.debug("Retrying %s without CREATE_BREAKAWAY_FROM_JOB", name)
                    try:
                        process = subprocess.Popen(
                            cmd,
                            cwd=cwd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            **popen_kwargs,
                        )
                    except (OSError, subprocess.SubprocessError) as e:
                        self.health_manager.record_crash(
                            name, exit_code=None, reason=str(e)
                        )
                        raise RuntimeError(
                            f"Failed to start service {name}: {e}"
                        ) from e
                else:
                    self.health_manager.record_crash(
                        name, exit_code=None, reason="PermissionError"
                    )
                    raise RuntimeError(
                        f"Failed to start service {name}: PermissionError"
                    )
            else:
                raise
        except (OSError, subprocess.SubprocessError) as e:
            self.health_manager.record_crash(name, exit_code=None, reason=str(e))
            raise RuntimeError(f"Failed to start service {name}: {e}") from e

        # Set up process group
        self.platform.setup_process_group(process)

        # Create managed process
        managed = ManagedProcess(
            name=name,
            config=config,
            process=process,
            log_capture=log_capture,
            ready_event=threading.Event(),
        )

        # Start stream readers
        if process.stdout:
            managed.stdout_reader = StreamReader(
                process.stdout,
                log_capture,
                source="stdout",
            )
            managed.stdout_reader.start()

        if process.stderr:
            managed.stderr_reader = StreamReader(
                process.stderr,
                log_capture,
                source="stderr",
            )
            managed.stderr_reader.start()

        # Store the managed process
        with self._lock:
            self._processes[name] = managed

        # Start ready condition monitoring
        timeout = ready_timeout or self.DEFAULT_READY_TIMEOUT
        self._start_ready_monitor(managed, timeout, on_ready)

        # Start crash monitor
        self._start_crash_monitor(managed, on_crash)

        return managed

    def stop_service(
        self,
        name: str,
        timeout: float | None = None,
        force: bool = False,
    ) -> bool:
        """Stop a service process.

        Args:
            name: Service name
            timeout: Graceful shutdown timeout (default: from config)
            force: If True, skip graceful shutdown and kill immediately

        Returns:
            True if the service was stopped successfully
        """
        with self._lock:
            if name not in self._processes:
                return True
            managed = self._processes[name]

        process = managed.process
        if process is None or process.poll() is not None:
            # Already stopped
            self._cleanup_managed(managed)
            return True

        config = managed.config
        shutdown_timeout = timeout
        if shutdown_timeout is None:
            if config.shutdown:
                shutdown_timeout = config.shutdown.timeout
            else:
                shutdown_timeout = self.shutdown_config.timeout

        if force:
            # Force kill
            self.platform.kill_process(process)
            process.wait(timeout=5)
            self._cleanup_managed(managed)
            self.health_manager.record_stop(name)
            return True

        # Try graceful shutdown
        if config.shutdown and config.shutdown.method == "http":
            # HTTP shutdown
            success = self._http_shutdown(config.shutdown.endpoint or "/shutdown")
            if success:
                # Wait for process to exit
                try:
                    process.wait(timeout=shutdown_timeout)
                    self._cleanup_managed(managed)
                    self.health_manager.record_stop(name)
                    return True
                except subprocess.TimeoutExpired:
                    pass

        # Signal-based shutdown
        self.platform.terminate_process(process)

        # Wait for graceful shutdown
        try:
            process.wait(timeout=shutdown_timeout)
            self._cleanup_managed(managed)
            self.health_manager.record_stop(name)
            return True
        except subprocess.TimeoutExpired:
            pass

        # Force kill
        kill_timeout = self.shutdown_config.kill_timeout
        self.platform.kill_process(process)

        try:
            process.wait(timeout=kill_timeout)
            self._cleanup_managed(managed)
            self.health_manager.record_stop(name)
            return True
        except subprocess.TimeoutExpired:
            # Process refuses to die
            return False

    def stop_all(self, timeout: float | None = None) -> None:
        """Stop all managed services.

        Args:
            timeout: Timeout per service
        """
        with self._lock:
            names = list(self._processes.keys())

        # Stop in reverse order (last started first)
        for name in reversed(names):
            self.stop_service(name, timeout=timeout)

    def get_pid(self, name: str) -> int | None:
        """Get the PID of a running service process, or None if not running."""
        with self._lock:
            managed = self._processes.get(name)
            if managed and managed.process and managed.process.poll() is None:
                return managed.process.pid
            return None

    def is_running(self, name: str) -> bool:
        """Check if a service is running.

        Args:
            name: Service name

        Returns:
            True if the service is running
        """
        with self._lock:
            if name not in self._processes:
                return False
            managed = self._processes[name]

        if managed.process is None:
            return False
        return managed.process.poll() is None

    def get_state(self, name: str) -> ServiceState:
        """Get the current state of a service.

        Args:
            name: Service name

        Returns:
            Current ServiceState
        """
        health = self.health_manager.get_health(name)
        return health.state

    def wait_for_ready(
        self,
        name: str,
        timeout: float | None = None,
    ) -> bool:
        """Wait for a service to become ready.

        Args:
            name: Service name
            timeout: Maximum time to wait (default: 60s)

        Returns:
            True if the service is ready, False if timeout
        """
        with self._lock:
            if name not in self._processes:
                return False
            managed = self._processes[name]

        if managed.ready_event is None:
            return True

        timeout = timeout or self.DEFAULT_READY_TIMEOUT
        return managed.ready_event.wait(timeout=timeout)

    def _start_ready_monitor(
        self,
        managed: ManagedProcess,
        timeout: float,
        on_ready: Callable[[], None] | None,
    ) -> None:
        """Start monitoring for ready condition.

        Args:
            managed: The managed process
            timeout: Maximum time to wait for ready
            on_ready: Callback when ready
        """
        ready_str = managed.config.ready
        if not ready_str:
            # No ready condition, mark as ready immediately
            if managed.ready_event:
                managed.ready_event.set()
            self.health_manager.record_running(managed.name)
            if on_ready:
                on_ready()
            return

        try:
            condition = ReadyCondition.parse(ready_str)
        except ValueError:
            # Invalid condition, mark as ready
            if managed.ready_event:
                managed.ready_event.set()
            self.health_manager.record_running(managed.name)
            return

        # Start ready monitor thread
        thread = threading.Thread(
            target=self._ready_monitor_loop,
            args=(managed, condition, timeout, on_ready),
            daemon=True,
        )
        thread.start()

    def _ready_monitor_loop(
        self,
        managed: ManagedProcess,
        condition: ReadyCondition,
        timeout: float,
        on_ready: Callable[[], None] | None,
    ) -> None:
        """Monitor loop for ready condition."""
        start_time = time.time()

        # Special handling for delay condition
        if condition.type == ReadyConditionType.DELAY:
            try:
                delay = float(condition.value)
                if delay > 0:
                    time.sleep(min(delay, timeout))
            except ValueError:
                pass
            # Mark as ready after delay
            if managed.ready_event:
                managed.ready_event.set()
            self.health_manager.record_ready(managed.name)
            if on_ready:
                on_ready()
            return

        # Special handling for log pattern
        if condition.type == ReadyConditionType.LOG:
            log_ready_event = threading.Event()

            def log_callback(line: str) -> None:
                log_ready_event.set()

            if managed.log_capture:
                managed.log_capture.add_pattern_callback(
                    condition.value,
                    log_callback,
                )
                managed._ready_callback_added = True

            # Wait for log pattern or timeout
            if log_ready_event.wait(timeout=timeout):
                if managed.ready_event:
                    managed.ready_event.set()
                self.health_manager.record_ready(managed.name)
                if on_ready:
                    on_ready()
            return

        # Polling-based conditions
        while time.time() - start_time < timeout:
            # Check if process is still running
            if managed.process and managed.process.poll() is not None:
                return  # Process exited

            if self._check_ready_condition(condition):
                if managed.ready_event:
                    managed.ready_event.set()
                self.health_manager.record_ready(managed.name)
                if on_ready:
                    on_ready()
                return

            time.sleep(self.POLL_INTERVAL)

        # Timeout - still mark as running but not ready
        # Service will continue, but ready wasn't detected

    def _check_ready_condition(self, condition: ReadyCondition) -> bool:
        """Check if a ready condition is met.

        Args:
            condition: The ready condition to check

        Returns:
            True if the condition is met
        """
        if condition.type == ReadyConditionType.PORT:
            try:
                port = int(condition.value)
                return self.platform.is_port_listening(port)
            except ValueError:
                return False

        elif condition.type == ReadyConditionType.DELAY:
            # Delay is handled by timing, not polling
            # If we reach here in the poll loop, the delay has passed
            try:
                delay = float(condition.value)
                return delay <= 0
            except ValueError:
                return True

        elif condition.type == ReadyConditionType.HTTP:
            return self._check_http_ready(condition.value)

        return False

    def _check_http_ready(self, url: str) -> bool:
        """Check if an HTTP endpoint returns 2xx.

        Args:
            url: URL to check

        Returns:
            True if the response is 2xx
        """
        try:
            # Add http:// if not present
            if not url.startswith("http://") and not url.startswith("https://"):
                url = f"http://{url}"

            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _http_shutdown(self, endpoint: str, port: int | None = None) -> bool:
        """Send HTTP shutdown request.

        Args:
            endpoint: Shutdown endpoint path
            port: Port to send request to (extracted from ready condition if not specified)

        Returns:
            True if the request was successful
        """
        try:
            # Build URL
            url = endpoint
            if not url.startswith("http://") and not url.startswith("https://"):
                # Assume localhost
                port_str = f":{port}" if port else ""
                url = f"http://localhost{port_str}{endpoint}"

            request = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _start_crash_monitor(
        self,
        managed: ManagedProcess,
        on_crash: Callable[[int | None], None] | None,
    ) -> None:
        """Start monitoring for process crashes.

        Args:
            managed: The managed process
            on_crash: Callback when the process crashes
        """
        thread = threading.Thread(
            target=self._crash_monitor_loop,
            args=(managed, on_crash),
            daemon=True,
        )
        thread.start()

    def _crash_monitor_loop(
        self,
        managed: ManagedProcess,
        on_crash: Callable[[int | None], None] | None,
    ) -> None:
        """Monitor loop for process crashes."""
        process = managed.process
        if process is None:
            return

        # Wait for process to exit
        exit_code = process.wait()

        # Check if this was a graceful stop
        health = self.health_manager.get_health(managed.name)
        if health.state == ServiceState.STOPPING:
            # Graceful stop, not a crash
            return

        # This is a crash
        self.health_manager.record_crash(managed.name, exit_code)

        if on_crash:
            on_crash(exit_code)

    def _cleanup_managed(self, managed: ManagedProcess) -> None:
        """Clean up a managed process.

        Args:
            managed: The managed process to clean up
        """
        # Stop stream readers
        if managed.stdout_reader:
            managed.stdout_reader.stop()
        if managed.stderr_reader:
            managed.stderr_reader.stop()

        # Remove pattern callbacks
        if managed.log_capture and managed._ready_callback_added:
            ready_str = managed.config.ready
            if ready_str and ready_str.startswith("log:"):
                pattern = ready_str[4:]
                managed.log_capture.remove_pattern_callback(pattern)

        # Stop log capture
        self.log_manager.stop_capture(managed.name)

        # Clear process reference
        managed.process = None
