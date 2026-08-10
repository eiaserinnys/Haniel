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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from ..config import ServiceConfig, ShutdownConfig
from ..config.readiness import ReadyCondition, ReadyConditionType
from ..config.validators import require_valid_service_readiness
from ..platform import get_platform_handler
from .health import HealthManager, ServiceState
from .logs import (
    LogCapture,
    LogManager,
    PatternCallbackHandle,
    StreamReader,
)
from .service_environment import ServiceEnvironmentFile, service_process_environment
from .stale_instance import PortInUseError, StaleInstanceCleaner, extract_ready_port

logger = logging.getLogger(__name__)


class ReadinessState(Enum):
    """Terminal-monotonic readiness state for one process generation."""

    PENDING = "pending"
    UNCONFIGURED = "unconfigured"
    READY = "ready"
    TIMED_OUT = "timed_out"
    EXITED = "exited"
    STOPPED = "stopped"
    REPLACED = "replaced"


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
    intentional_stop: bool = False
    generation: int = 0
    readiness_state: ReadinessState = ReadinessState.PENDING
    readiness_started_at: float | None = None
    readiness_deadline: float | None = None
    ready_condition: ReadyCondition | None = None
    marker_observed_at: float | None = None
    marker_evidence_lock: threading.Lock = field(default_factory=threading.Lock)
    marker_event: threading.Event = field(default_factory=threading.Event)
    readiness_done_event: threading.Event = field(default_factory=threading.Event)
    ready_callback_handle: PatternCallbackHandle | None = None
    ready_monitor: threading.Thread | None = None
    on_ready: Callable[[], None] | None = None
    crash_committed: bool = False
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
    STALE_INSTANCE_GRACE_TIMEOUT = 5  # seconds
    IMMEDIATE_EXIT_WINDOW = 1.0  # seconds
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
        self.stale_instance_grace_timeout = self.STALE_INSTANCE_GRACE_TIMEOUT
        self.immediate_exit_window = self.IMMEDIATE_EXIT_WINDOW

        self._processes: dict[str, ManagedProcess] = {}
        self._lock = threading.RLock()
        self._next_generation = 1

    def start_service(
        self,
        name: str,
        config: ServiceConfig,
        ready_timeout: float | None = None,
        on_ready: Callable[[], None] | None = None,
        on_crash: Callable[[int | None], None] | None = None,
        expected_env_path: str | None = None,
        expected_env_sha256: str | None = None,
        approved_env_snapshot: ServiceEnvironmentFile | None = None,
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
        # This is the direct-call boundary. It must fail before stale-process
        # cleanup, log files, health mutation, environment reads, or spawn.
        require_valid_service_readiness(name, config)
        condition = (
            ReadyCondition.parse(config.ready) if config.ready is not None else None
        )

        with self._lock:
            if name in self._processes and self._processes[name].process:
                if self._processes[name].process.poll() is None:
                    raise RuntimeError(f"Service {name} is already running")

        # Resolve working directory
        cwd = self.config_dir
        if config.cwd:
            cwd = self.config_dir / config.cwd

        try:
            self._cleanup_stale_instance_before_start(name, config, cwd)
        except PortInUseError as e:
            logger.error(str(e))
            raise RuntimeError(str(e)) from e

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

        child_env = service_process_environment(
            self.config_dir,
            config,
            expected_env_path=expected_env_path,
            expected_env_sha256=expected_env_sha256,
            approved_snapshot=approved_env_snapshot,
        )
        process = self._spawn_process(name, cmd, cwd, popen_kwargs, child_env)

        # Set up process group
        self.platform.setup_process_group(process)

        process = self._retry_after_immediate_port_exit(
            name=name,
            config=config,
            cwd=cwd,
            cmd=cmd,
            popen_kwargs=popen_kwargs,
            child_env=child_env,
            process=process,
            log_capture=log_capture,
        )

        # Create managed process
        managed = ManagedProcess(
            name=name,
            config=config,
            process=process,
            log_capture=log_capture,
            ready_event=threading.Event(),
            ready_condition=condition,
            on_ready=on_ready,
        )

        timeout = self.DEFAULT_READY_TIMEOUT if ready_timeout is None else ready_timeout
        managed.readiness_started_at = time.monotonic()
        managed.readiness_deadline = managed.readiness_started_at + timeout

        # Install current generation and arm log evidence before either stream
        # reader can consume the first child line.
        with self._lock:
            previous = self._processes.get(name)
            if previous is not None:
                self._set_terminal_locked(previous, ReadinessState.REPLACED)
            managed.generation = self._next_generation
            self._next_generation += 1
            self._processes[name] = managed
            self._arm_log_evidence_locked(managed)

            if condition is None:
                managed.readiness_state = ReadinessState.UNCONFIGURED
                managed.readiness_done_event.set()
                self.health_manager.record_running(name)

        try:
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

            # Start ready condition monitoring
            if condition is not None:
                self._start_ready_monitor(managed, timeout, on_ready)

            # Start crash monitor
            self._start_crash_monitor(managed, on_crash)
        except Exception as error:
            logger.exception("Failed to initialize service monitors for %s", name)
            with self._lock:
                self._set_terminal_locked(managed, ReadinessState.STOPPED)
            try:
                if process.poll() is None:
                    self.platform.kill_process(process)
                    process.wait(timeout=5)
            except Exception as cleanup_error:
                logger.error(
                    "Failed to reap %s after monitor initialization error: %s",
                    name,
                    cleanup_error,
                )
            finally:
                self._cleanup_managed(managed)
                self.health_manager.record_crash(
                    name, exit_code=process.poll(), reason=str(error)
                )
            raise

        return managed

    def _spawn_process(
        self,
        name: str,
        cmd: str | list[str],
        cwd: Path,
        popen_kwargs: dict,
        child_env: dict[str, str],
    ) -> subprocess.Popen:
        """Spawn a process, preserving Windows breakaway fallback behavior."""
        try:
            return subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
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
                    retry_kwargs = dict(popen_kwargs)
                    retry_kwargs["creationflags"] = flags & ~CREATE_BREAKAWAY_FROM_JOB
                    logger.debug("Retrying %s without CREATE_BREAKAWAY_FROM_JOB", name)
                    try:
                        return subprocess.Popen(
                            cmd,
                            cwd=cwd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            env=child_env,
                            **retry_kwargs,
                        )
                    except (OSError, subprocess.SubprocessError) as e:
                        self.health_manager.record_crash(
                            name, exit_code=None, reason=str(e)
                        )
                        raise RuntimeError(
                            f"Failed to start service {name}: {e}"
                        ) from e

            self.health_manager.record_crash(
                name, exit_code=None, reason="PermissionError"
            )
            raise RuntimeError(f"Failed to start service {name}: PermissionError")
        except (OSError, subprocess.SubprocessError) as e:
            self.health_manager.record_crash(name, exit_code=None, reason=str(e))
            raise RuntimeError(f"Failed to start service {name}: {e}") from e

    def _cleanup_stale_instance_before_start(
        self,
        name: str,
        config: ServiceConfig,
        cwd: Path,
    ) -> None:
        StaleInstanceCleaner(self.platform).cleanup_before_start(
            service_name=name,
            config=config,
            grace_timeout=self._shutdown_timeout_for(config),
        )

    def _shutdown_timeout_for(self, config: ServiceConfig) -> float:
        if config.shutdown:
            return float(config.shutdown.timeout)
        if self.stale_instance_grace_timeout is not None:
            return float(self.stale_instance_grace_timeout)
        return float(self.shutdown_config.timeout)

    def _retry_after_immediate_port_exit(
        self,
        *,
        name: str,
        config: ServiceConfig,
        cwd: Path,
        cmd: str | list[str],
        popen_kwargs: dict,
        child_env: dict[str, str],
        process: subprocess.Popen,
        log_capture: LogCapture,
    ) -> subprocess.Popen:
        if extract_ready_port(config) is None:
            return process

        exit_code = self._wait_for_immediate_exit(process)
        if exit_code is None:
            return process

        self._capture_exited_process_output(process, log_capture)
        cleaner = StaleInstanceCleaner(self.platform)
        if not cleaner.has_ready_port_occupants(config):
            return process

        logger.warning(
            "Service %s exited within %.1fs while its ready port remains in use; "
            "diagnosing as possible EADDRINUSE and retrying after stale cleanup",
            name,
            self.immediate_exit_window,
        )
        self.health_manager.record_crash(
            name,
            exit_code=exit_code,
            reason="immediate exit with ready port still in use",
        )

        try:
            self._cleanup_stale_instance_before_start(name, config, cwd)
        except PortInUseError as e:
            logger.error(str(e))
            raise RuntimeError(str(e)) from e

        retry = self._spawn_process(name, cmd, cwd, popen_kwargs, child_env)
        self.platform.setup_process_group(retry)
        return retry

    def _wait_for_immediate_exit(self, process: subprocess.Popen) -> int | None:
        deadline = time.monotonic() + max(self.immediate_exit_window, 0)
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                return exit_code
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(self.POLL_INTERVAL, max(deadline - time.monotonic(), 0)))

    @staticmethod
    def _capture_exited_process_output(
        process: subprocess.Popen,
        log_capture: LogCapture,
    ) -> None:
        try:
            stdout, stderr = process.communicate(timeout=0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return

        for line in (stdout or "").splitlines():
            log_capture.write_line(line, "stdout")
        for line in (stderr or "").splitlines():
            log_capture.write_line(line, "stderr")

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

        managed.intentional_stop = True
        self.health_manager.record_stopping(name)

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

    def running_services_affected_by_repo(self, repo_name: str) -> tuple[str, ...]:
        """Derive affected live processes from their immutable start snapshots."""

        with self._lock:
            started = {
                name: managed.config for name, managed in self._processes.items()
            }
            running = {
                name
                for name, managed in self._processes.items()
                if managed.process is not None and managed.process.poll() is None
            }
        affected = {
            name for name, config in started.items() if config.repo == repo_name
        }
        changed = True
        while changed:
            changed = False
            for name, config in started.items():
                if name not in affected and any(
                    dependency in affected for dependency in config.after
                ):
                    affected.add(name)
                    changed = True
        return tuple(sorted(affected & running))

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

        if managed.readiness_state is ReadinessState.UNCONFIGURED:
            return False

        timeout = self.DEFAULT_READY_TIMEOUT if timeout is None else timeout
        managed.readiness_done_event.wait(timeout=timeout)
        return managed.readiness_state is ReadinessState.READY

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
        thread = threading.Thread(
            target=self._ready_monitor_loop,
            args=(managed,),
            daemon=True,
            name=f"haniel-ready-{managed.name}-{managed.generation}",
        )
        with self._lock:
            if self._is_current_pending_locked(managed):
                managed.ready_monitor = thread
        thread.start()

    def _arm_log_evidence_locked(self, managed: ManagedProcess) -> None:
        condition = managed.ready_condition
        if condition is None or condition.type is not ReadyConditionType.LOG:
            return
        if managed.log_capture is None:
            return

        def record_marker(_line: str) -> None:
            observed_at = time.monotonic()
            # Persist the evidence before contending for the manager lock. A timeout
            # commit can then distinguish a timely marker from a late callback even
            # when the callback has not yet entered the state transition section.
            with managed.marker_evidence_lock:
                if managed.marker_observed_at is None:
                    managed.marker_observed_at = observed_at
                managed.marker_event.set()

        managed.ready_callback_handle = managed.log_capture.add_pattern_callback(
            condition.value, record_marker
        )
        managed._ready_callback_added = True

    def _ready_monitor_loop(
        self,
        managed: ManagedProcess,
    ) -> None:
        """The only authority that may commit READY for this generation."""
        condition = managed.ready_condition
        assert condition is not None
        try:
            while True:
                with self._lock:
                    if not self._is_current_pending_locked(managed):
                        return
                    process = managed.process
                    deadline = managed.readiness_deadline
                    started_at = managed.readiness_started_at
                with managed.marker_evidence_lock:
                    marker_observed_at = managed.marker_observed_at

                if process is None or process.poll() is not None:
                    self._commit_terminal(managed, ReadinessState.EXITED)
                    return

                now = time.monotonic()
                assert deadline is not None
                assert started_at is not None

                if condition.type is ReadyConditionType.LOG:
                    if marker_observed_at is not None:
                        self._commit_ready(
                            managed,
                            observed_at=marker_observed_at,
                            deadline=deadline,
                        )
                        return
                elif condition.type is ReadyConditionType.DELAY:
                    observed_at = started_at + float(condition.value)
                    if now >= observed_at:
                        self._commit_ready(
                            managed,
                            observed_at=observed_at,
                            deadline=deadline,
                        )
                        return
                elif self._check_ready_condition(condition, managed):
                    self._commit_ready(
                        managed,
                        observed_at=time.monotonic(),
                        deadline=deadline,
                    )
                    return

                now = time.monotonic()
                if now >= deadline:
                    self._commit_terminal(
                        managed, ReadinessState.TIMED_OUT, record_running=True
                    )
                    return
                managed.marker_event.wait(min(self.POLL_INTERVAL, deadline - now))
                managed.marker_event.clear()
        except Exception:
            logger.exception("Readiness monitor failed for %s", managed.name)
            self._commit_terminal(
                managed, ReadinessState.TIMED_OUT, record_running=True
            )
        finally:
            with self._lock:
                if managed.ready_monitor is threading.current_thread():
                    managed.ready_monitor = None

    def _is_current_pending_locked(self, managed: ManagedProcess) -> bool:
        return (
            self._processes.get(managed.name) is managed
            and managed.readiness_state is ReadinessState.PENDING
        )

    def _detach_ready_callback_locked(self, managed: ManagedProcess) -> None:
        handle = managed.ready_callback_handle
        if handle is not None and managed.log_capture is not None:
            managed.log_capture.remove_pattern_callback(handle)
        managed.ready_callback_handle = None
        managed._ready_callback_added = False

    def _set_terminal_locked(
        self, managed: ManagedProcess, state: ReadinessState
    ) -> bool:
        current = managed.readiness_state
        if state is ReadinessState.TIMED_OUT:
            if current is not ReadinessState.PENDING:
                return False
        elif state in (
            ReadinessState.EXITED,
            ReadinessState.STOPPED,
            ReadinessState.REPLACED,
        ):
            if current in (
                ReadinessState.TIMED_OUT,
                ReadinessState.EXITED,
                ReadinessState.STOPPED,
                ReadinessState.REPLACED,
            ):
                return False
        elif current is not ReadinessState.PENDING:
            return False
        managed.readiness_state = state
        self._detach_ready_callback_locked(managed)
        managed.marker_event.set()
        managed.readiness_done_event.set()
        return True

    def _commit_terminal(
        self,
        managed: ManagedProcess,
        state: ReadinessState,
        *,
        record_running: bool = False,
    ) -> bool:
        callback: Callable[[], None] | None = None
        committed_ready = False
        changed = False
        with self._lock:
            if self._processes.get(managed.name) is not managed:
                return False
            process = managed.process
            with managed.marker_evidence_lock:
                marker_observed_at = managed.marker_observed_at
            if (
                state is ReadinessState.TIMED_OUT
                and managed.ready_condition is not None
                and managed.ready_condition.type is ReadyConditionType.LOG
                and marker_observed_at is not None
                and managed.readiness_deadline is not None
                and marker_observed_at <= managed.readiness_deadline
                and process is not None
                and process.poll() is None
                and self._is_current_pending_locked(managed)
            ):
                callback = self._set_ready_locked(managed)
                committed_ready = True
            else:
                changed = self._set_terminal_locked(managed, state)
                if changed and record_running:
                    self.health_manager.record_running(managed.name)
        if callback is not None:
            callback()
        if committed_ready:
            return True
        return changed

    def _set_ready_locked(self, managed: ManagedProcess) -> Callable[[], None] | None:
        managed.readiness_state = ReadinessState.READY
        self._detach_ready_callback_locked(managed)
        if managed.ready_event is not None:
            managed.ready_event.set()
        managed.readiness_done_event.set()
        self.health_manager.record_ready(managed.name)
        return managed.on_ready

    def _commit_ready(
        self,
        managed: ManagedProcess,
        *,
        observed_at: float | None = None,
        deadline: float | None = None,
    ) -> bool:
        """Atomically commit READY only for live, current, nonterminal evidence."""

        callback: Callable[[], None] | None = None
        with self._lock:
            if not self._is_current_pending_locked(managed):
                return False
            process = managed.process
            if process is None or process.poll() is not None:
                self._set_terminal_locked(managed, ReadinessState.EXITED)
                return False
            evidence_time = time.monotonic() if observed_at is None else observed_at
            effective_deadline = (
                managed.readiness_deadline if deadline is None else deadline
            )
            if effective_deadline is not None and evidence_time > effective_deadline:
                self._set_terminal_locked(managed, ReadinessState.TIMED_OUT)
                return False
            callback = self._set_ready_locked(managed)
        if callback is not None:
            callback()
        return True

    def _commit_crash(self, managed: ManagedProcess, exit_code: int | None) -> bool:
        """Publish an exit only if this generation still owns the service."""

        with self._lock:
            if self._processes.get(managed.name) is not managed:
                return False
            if managed.crash_committed or managed.readiness_state in (
                ReadinessState.STOPPED,
                ReadinessState.REPLACED,
            ):
                return False
            if managed.readiness_state not in (
                ReadinessState.TIMED_OUT,
                ReadinessState.EXITED,
            ) and not self._set_terminal_locked(managed, ReadinessState.EXITED):
                return False
            health = self.health_manager.get_health(managed.name)
            if managed.intentional_stop or health.state is ServiceState.STOPPING:
                return False
            managed.crash_committed = True
            self.health_manager.record_crash(managed.name, exit_code)
            return True

    def _check_ready_condition(
        self,
        condition: ReadyCondition,
        managed: ManagedProcess | None = None,
    ) -> bool:
        """Check if a ready condition is met.

        Args:
            condition: The ready condition to check

        Returns:
            True if the condition is met
        """
        if condition.type == ReadyConditionType.PORT:
            if managed is not None and managed.process is not None:
                return self._check_port_ready(condition, managed)
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
            endpoint = condition.endpoint
            assert endpoint is not None
            return self._check_http_ready(endpoint)

        return False

    def _check_port_ready(
        self,
        condition: ReadyCondition,
        managed: ManagedProcess,
    ) -> bool:
        """Check that a ready port belongs to this managed process tree."""
        try:
            port = int(condition.value)
        except ValueError:
            return False

        process = managed.process
        if process is None or process.poll() is not None:
            return False

        if self.platform.is_port_owned_by_process_tree(port, process.pid):
            return True

        if self.platform.is_port_listening(port):
            logger.debug(
                "Port %s is listening but is not owned by %s process tree",
                port,
                managed.name,
            )
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

        # Close readiness and publish crash health in the same generation order.
        if not self._commit_crash(managed, exit_code):
            return

        if on_crash:
            on_crash(exit_code)

    def _cleanup_managed(self, managed: ManagedProcess) -> None:
        """Clean up a managed process.

        Args:
            managed: The managed process to clean up
        """
        with self._lock:
            self._set_terminal_locked(managed, ReadinessState.STOPPED)
            monitor = managed.ready_monitor
            process = managed.process

        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=5)

        # Stop stream readers
        if managed.stdout_reader:
            managed.stdout_reader.stop()
        if managed.stderr_reader:
            managed.stderr_reader.stop()

        for reader in (managed.stdout_reader, managed.stderr_reader):
            if reader is None or reader is threading.current_thread():
                continue
            if reader.ident is None:
                reader.close_before_start()
                continue
            reader.join(timeout=5)
            if reader.is_alive():
                logger.warning("Stream reader did not stop for %s", managed.name)

        for reader, stream in (
            (managed.stdout_reader, None if process is None else process.stdout),
            (managed.stderr_reader, None if process is None else process.stderr),
        ):
            if reader is None and stream is not None and not stream.closed:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        # Stop log capture
        self.log_manager.stop_capture(managed.name)

        # Clear process reference
        managed.process = None
