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
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from ..config import ServiceConfig, ShutdownConfig
from ..config.input_checks import ready_format_error
from ..platform import get_platform_handler
from .health import HealthManager, ServiceState
from .logs import LogCapture, LogManager, StreamReader
from .service_environment import ServiceEnvironmentFile, service_process_environment
from .service_locks import ServiceLockRegistry
from .stale_instance import (
    PortInUseError,
    StaleInstanceCleaner,
    extract_ready_port,
    pid_matches_service,
)

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


class PidProcessHandle:
    """Popen-like handle for a process this Haniel did not spawn.

    A service adopted across a Haniel self-update is not a child of the new
    Haniel, so ``Popen.wait()`` cannot see it exit. This handle exposes the
    small Popen surface the manager uses (``pid``, ``poll``, ``wait``,
    ``terminate``, ``kill``, ``returncode``) and judges liveness by PID with
    the platform's ``is_pid_running`` and ``is_pid_zombie``. The exit code of
    a process that is not our child cannot be read, so a vanished process
    reports ``EXIT_CODE_UNKNOWN``.
    """

    EXIT_CODE_UNKNOWN = -1
    # Windows ``is_pid_running`` starts powershell, so poll it far less often.
    MONITOR_INTERVAL = 10.0 if os.name == "nt" else 2.0
    WAIT_INTERVAL = 1.0 if os.name == "nt" else 0.1

    def __init__(self, pid: int, platform) -> None:
        self.pid = pid
        self.platform = platform
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self.platform.is_pid_running(self.pid) and not self.platform.is_pid_zombie(
            self.pid
        ):
            return None
        self.returncode = self.EXIT_CODE_UNKNOWN
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the PID to disappear.

        Without a timeout (the crash monitor) the PID is checked every
        ``MONITOR_INTERVAL``; with one (stop paths) every ``WAIT_INTERVAL``.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        interval = self.MONITOR_INTERVAL if timeout is None else self.WAIT_INTERVAL
        while True:
            returncode = self.poll()
            if returncode is not None:
                return returncode
            if deadline is None:
                time.sleep(interval)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout)
            time.sleep(min(interval, remaining))

    def terminate(self) -> None:
        self.platform.terminate_pid(self.pid)

    def kill(self) -> None:
        self.platform.kill_pid(self.pid)


@dataclass
class ManagedProcess:
    """A process managed by haniel."""

    name: str
    config: ServiceConfig
    process: subprocess.Popen | PidProcessHandle | None = None
    log_capture: LogCapture | None = None
    stdout_reader: StreamReader | None = None
    stderr_reader: StreamReader | None = None
    ready_event: threading.Event | None = None
    ready_failure_event: threading.Event | None = None
    ready_exit_code: int | None = None
    intentional_stop: bool = False
    relay_processes: list[subprocess.Popen] = field(default_factory=list)
    # True for an adopted process: its relays belong to the previous Haniel,
    # but its output still goes through relays, so it can be handed over again.
    relay_backed: bool = False
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
    READY_RECHECK_INTERVAL = 10  # seconds between checks past the ready limit
    STABLE_RUNNING_SECONDS = 10  # a service with no ready condition counts as
    # working once its process has stayed up this long

    def __init__(
        self,
        config_dir: Path,
        log_dir: Path | None = None,
        shutdown_config: ShutdownConfig | None = None,
        health_manager: HealthManager | None = None,
        service_locks: ServiceLockRegistry | None = None,
    ):
        """Initialize the process manager.

        Args:
            config_dir: Base directory for resolving relative paths
            log_dir: Directory for log files (default: config_dir/logs)
            shutdown_config: Global shutdown configuration
            health_manager: Health manager for state tracking
            service_locks: Per-service lifecycle locks shared with the runner
        """
        self.config_dir = config_dir
        self.log_dir = log_dir or config_dir / "logs"
        self.shutdown_config = shutdown_config or ShutdownConfig()
        self.health_manager = health_manager or HealthManager()
        self.service_locks = service_locks or ServiceLockRegistry()
        self.log_manager = LogManager(self.log_dir)
        self.platform = get_platform_handler()
        self.stale_instance_grace_timeout = self.STALE_INSTANCE_GRACE_TIMEOUT
        self.immediate_exit_window = self.IMMEDIATE_EXIT_WINDOW
        self.ready_recheck_interval = self.READY_RECHECK_INTERVAL
        self.stable_running_seconds = self.STABLE_RUNNING_SECONDS

        self._processes: dict[str, ManagedProcess] = {}
        self._pending_processes: dict[str, ManagedProcess] = {}
        self._lock = threading.Lock()

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
        log_relay: bool = False,
    ) -> ManagedProcess:
        """Start a service process.

        The service's lifecycle lock covers the running check through Popen,
        the immediate-exit retry, and registration, so two starts cannot both
        spawn a child.

        Args:
            name: Service name
            config: Service configuration
            ready_timeout: Timeout for ready condition (default: 60s)
            on_ready: Callback when service is ready
            on_crash: Callback when service crashes
            log_relay: Send stdout/stderr through log_sink relays so the log
                stream survives a Haniel replacement

        Returns:
            ManagedProcess instance

        Raises:
            RuntimeError: If the service is already running
        """
        with self.service_locks.hold(name):
            return self._start_service_locked(
                name,
                config,
                ready_timeout=ready_timeout,
                on_ready=on_ready,
                on_crash=on_crash,
                expected_env_path=expected_env_path,
                expected_env_sha256=expected_env_sha256,
                approved_env_snapshot=approved_env_snapshot,
                log_relay=log_relay,
            )

    def _start_service_locked(
        self,
        name: str,
        config: ServiceConfig,
        ready_timeout: float | None,
        on_ready: Callable[[], None] | None,
        on_crash: Callable[[int | None], None] | None,
        expected_env_path: str | None,
        expected_env_sha256: str | None,
        approved_env_snapshot: ServiceEnvironmentFile | None,
        overlap: bool = False,
        log_relay: bool = False,
    ) -> ManagedProcess:
        with self._lock:
            if (
                not overlap
                and name in self._processes
                and self._processes[name].process
            ):
                if self._processes[name].process.poll() is None:
                    raise RuntimeError(f"Service {name} is already running")

        # Resolve working directory
        cwd = self.config_dir
        if config.cwd:
            cwd = self.config_dir / config.cwd

        if not overlap:
            try:
                self._cleanup_stale_instance_before_start(name, config, cwd)
            except PortInUseError as e:
                logger.error(str(e))
                raise RuntimeError(str(e)) from e

        # Start log capture
        log_capture = (
            LogCapture(name, self.log_dir)
            if overlap
            else self.log_manager.start_capture(name)
        )
        if overlap:
            log_capture.start()

        # Record service starting
        if not overlap:
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

        relay_processes = self._start_log_relays(name) if log_relay else []
        for relay in relay_processes:
            self._start_relay_reaper(relay)

        try:
            child_env = service_process_environment(
                self.config_dir,
                config,
                expected_env_path=expected_env_path,
                expected_env_sha256=expected_env_sha256,
                approved_snapshot=approved_env_snapshot,
            )
            process = self._spawn_process(
                name,
                cmd,
                cwd,
                popen_kwargs,
                child_env,
                record_failure=not overlap,
                stdout=(
                    relay_processes[0].stdin if relay_processes else subprocess.PIPE
                ),
                stderr=(
                    relay_processes[1].stdin if relay_processes else subprocess.PIPE
                ),
            )
        except Exception:
            self._abort_log_relays(relay_processes)
            if overlap:
                log_capture.stop()
            raise

        self._close_log_relay_stdin(relay_processes)

        # Set up process group
        self.platform.setup_process_group(process)

        if not overlap:
            process, relay_processes = self._retry_after_immediate_port_exit(
                name=name,
                config=config,
                cwd=cwd,
                cmd=cmd,
                popen_kwargs=popen_kwargs,
                child_env=child_env,
                process=process,
                log_capture=log_capture,
                relay_processes=relay_processes,
            )

        # Create managed process
        managed = ManagedProcess(
            name=name,
            config=config,
            process=process,
            log_capture=log_capture,
            ready_event=threading.Event(),
            ready_failure_event=threading.Event(),
            relay_processes=relay_processes,
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
            (self._pending_processes if overlap else self._processes)[name] = managed

        # Start ready condition monitoring
        timeout = ready_timeout or self.DEFAULT_READY_TIMEOUT
        self._start_ready_monitor(managed, timeout, on_ready)

        # Start crash monitor
        if not overlap:
            self._start_crash_monitor(managed, on_crash)

        return managed

    def overlap_service(
        self,
        name: str,
        config: ServiceConfig,
        *,
        ready_timeout: float,
        exit_timeout: float,
        on_ready: Callable[[], None] | None = None,
        on_crash: Callable[[int | None], None] | None = None,
        expected_env_path: str | None = None,
        expected_env_sha256: str | None = None,
        approved_env_snapshot: ServiceEnvironmentFile | None = None,
        log_relay: bool = False,
    ) -> bool:
        """Prepare a replacement before retiring the current service process."""
        with self.service_locks.hold(name):
            with self._lock:
                old = self._processes.get(name)
            if old is None or old.process is None or old.process.poll() is not None:
                return False
            # The old process may exit as soon as the new process announces itself.
            old.intentional_stop = True
            started = time.monotonic()
            try:
                new = self._start_service_locked(
                    name,
                    config,
                    ready_timeout,
                    on_ready,
                    on_crash,
                    expected_env_path,
                    expected_env_sha256,
                    approved_env_snapshot,
                    overlap=True,
                    log_relay=log_relay,
                )
            except Exception:
                old.intentional_stop = False
                raise
            if (
                not self._wait_for_managed_ready(new, ready_timeout)
                or new.process.poll() is not None
            ):
                self._retire_overlap_process(new, 0)
                with self._lock:
                    self._pending_processes.pop(name, None)
                old.intentional_stop = False
                raise RuntimeError(f"readiness failed for {name} replacement")
            logger.info(
                "Overlap ready for %s: pid=%s elapsed=%.2fs",
                name,
                new.process.pid,
                time.monotonic() - started,
            )
            with self._lock:
                self._processes[name] = new
                self._pending_processes.pop(name, None)
            self.log_manager.replace_capture(name, new.log_capture)
            self._start_crash_monitor(new, on_crash)
            try:
                self._retire_overlap_process(old, exit_timeout)
            except Exception:
                logger.exception(
                    "Failed to retire old overlap process for %s: pid=%s",
                    name,
                    old.process.pid,
                )
            return True

    def _retire_overlap_process(self, managed: ManagedProcess, wait: float) -> None:
        process = managed.process
        assert process is not None
        managed.intentional_stop = True
        started = time.monotonic()
        method = "self"
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            method = "SIGTERM"
            self.platform.terminate_process(process)
            timeout = (
                managed.config.shutdown.timeout
                if managed.config.shutdown
                else self.shutdown_config.timeout
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                method = "SIGKILL"
                self.platform.kill_process(process)
                process.wait(timeout=self.shutdown_config.kill_timeout)
        logger.info(
            "Overlap exit for %s: pid=%s method=%s elapsed=%.2fs",
            managed.name,
            process.pid,
            method,
            time.monotonic() - started,
        )
        if managed.stdout_reader:
            managed.stdout_reader.stop()
        if managed.stderr_reader:
            managed.stderr_reader.stop()
        if managed.log_capture:
            managed.log_capture.stop()

    def adopt_handover_service(
        self,
        name: str,
        config: ServiceConfig,
        *,
        previous_pid: int,
        exit_timeout: float,
        ready_timeout: float,
        on_ready: Callable[[], None] | None = None,
        on_crash: Callable[[int | None], None] | None = None,
        expected_env_path: str | None = None,
        expected_env_sha256: str | None = None,
        approved_env_snapshot: ServiceEnvironmentFile | None = None,
        log_relay: bool = False,
    ) -> bool:
        """Replace a service process inherited from the previous Haniel.

        The inherited process is not a child here, so it is watched by PID
        instead of ``Popen.wait()``. Once the replacement is ready it becomes
        the managed process and the old PID is retired: wait for a self-exit,
        then SIGTERM after re-checking the command line, then SIGKILL. A
        replacement that never becomes ready is stopped together with the old
        PID, and the caller falls back to the ordinary start path.
        """
        with self.service_locks.hold(name):
            if not self._inherited_pid_running(previous_pid):
                return False
            started = time.monotonic()
            new = self._start_service_locked(
                name,
                config,
                ready_timeout,
                on_ready,
                on_crash,
                expected_env_path,
                expected_env_sha256,
                approved_env_snapshot,
                overlap=True,
                log_relay=log_relay,
            )
            if (
                not self._wait_for_managed_ready(new, ready_timeout)
                or new.process.poll() is not None
            ):
                logger.warning(
                    "Self-update handover for %s did not complete: replacement "
                    "failed readiness; starting normally",
                    name,
                )
                self._retire_overlap_process(new, 0)
                with self._lock:
                    self._pending_processes.pop(name, None)
                self._stop_inherited_pid(config, previous_pid)
                return False
            with self._lock:
                self._processes[name] = new
                self._pending_processes.pop(name, None)
            self.log_manager.replace_capture(name, new.log_capture)
            self._start_crash_monitor(new, on_crash)
            method = self._retire_inherited_pid(
                name, config, previous_pid, exit_timeout
            )
            logger.info(
                "Self-update handover for %s: pid=%s previous_pid=%s method=%s "
                "elapsed=%.2fs",
                name,
                new.process.pid,
                previous_pid,
                method,
                time.monotonic() - started,
            )
            return True

    def adopt_inherited_pid(
        self,
        name: str,
        config: ServiceConfig,
        *,
        pid: int,
        on_ready: Callable[[], None] | None = None,
        on_crash: Callable[[int | None], None] | None = None,
    ) -> bool:
        """Take over a service the previous Haniel left running, without a restart.

        No replacement is started. The inherited PID becomes the managed
        process behind a ``PidProcessHandle`` and the service's ready
        condition is checked once. When the PID is gone or the check fails the
        PID is retired by the ordinary handover retirement and False is
        returned so the caller starts the service normally.
        """
        with self.service_locks.hold(name):
            handle = PidProcessHandle(pid, self.platform)
            if handle.poll() is not None:
                return False
            managed = ManagedProcess(
                name=name,
                config=config,
                process=handle,
                ready_event=threading.Event(),
                ready_failure_event=threading.Event(),
                relay_backed=True,
            )
            if not self._adopted_service_ready(managed):
                logger.warning(
                    "Self-update handover for %s did not complete: pid=%s failed "
                    "the ready check; retiring it and starting normally",
                    name,
                    pid,
                )
                self._retire_inherited_pid(name, config, pid, 0)
                return False
            with self._lock:
                self._processes[name] = managed
            self.health_manager.record_start(name)
            if config.ready and self._parsed_ready(config.ready) is not None:
                self.health_manager.record_ready(name)
            else:
                self.health_manager.record_running(name)
            if managed.ready_event:
                managed.ready_event.set()
            self._start_crash_monitor(managed, on_crash)
            logger.info("Adopted %s (pid %s) without restart", name, pid)
            if on_ready:
                on_ready()
            return True

    def retire_inherited_pid(self, name: str, config: ServiceConfig, pid: int) -> str:
        """Stop a PID inherited from the previous Haniel that cannot be adopted."""
        with self.service_locks.hold(name):
            return self._retire_inherited_pid(name, config, pid, 0)

    @staticmethod
    def _parsed_ready(ready: str) -> ReadyCondition | None:
        try:
            return ReadyCondition.parse(ready)
        except ValueError:
            return None

    def _adopted_service_ready(self, managed: ManagedProcess) -> bool:
        """Run the ready check once; the service has been up, so it is not awaited."""
        condition = self._parsed_ready(managed.config.ready or "")
        if condition is None or condition.type not in (
            ReadyConditionType.PORT,
            ReadyConditionType.HTTP,
        ):
            return True
        return self._check_ready_condition(condition, managed)

    def relay_backed_processes(self) -> list[tuple[str, ManagedProcess]]:
        """Return running services whose output goes through relay processes."""
        with self._lock:
            managed = [
                (name, item)
                for name, item in self._processes.items()
                if item.relay_processes or getattr(item, "relay_backed", False)
            ]
        return [
            (name, item)
            for name, item in managed
            if item.process is not None and item.process.poll() is None
        ]

    def _inherited_pid_running(self, pid: int) -> bool:
        return self.platform.is_pid_running(pid) and not self.platform.is_pid_zombie(
            pid
        )

    def _wait_for_pid_exit(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            if not self._inherited_pid_running(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(self.POLL_INTERVAL, max(deadline - time.monotonic(), 0)))

    def _retire_inherited_pid(
        self,
        name: str,
        config: ServiceConfig,
        pid: int,
        exit_timeout: float,
    ) -> str:
        if self._wait_for_pid_exit(pid, exit_timeout):
            return "self"
        if not self._inherited_pid_running(pid):
            return "self"
        if not pid_matches_service(self.platform, pid, config):
            logger.warning(
                "Self-update handover for %s: pid=%s no longer matches the "
                "service; not terminating it",
                name,
                pid,
            )
            return "self"
        self.platform.terminate_pid(pid)
        shutdown_timeout = (
            config.shutdown.timeout if config.shutdown else self.shutdown_config.timeout
        )
        if self._wait_for_pid_exit(pid, shutdown_timeout):
            return "term"
        if not pid_matches_service(self.platform, pid, config):
            logger.warning(
                "Self-update handover for %s: pid=%s no longer matches the "
                "service; not killing it",
                name,
                pid,
            )
            return "term"
        self.platform.kill_pid(pid)
        self._wait_for_pid_exit(pid, self.shutdown_config.kill_timeout)
        return "kill"

    def _stop_inherited_pid(self, config: ServiceConfig, pid: int) -> None:
        self.platform.terminate_pid(pid)
        shutdown_timeout = (
            config.shutdown.timeout if config.shutdown else self.shutdown_config.timeout
        )
        if not self._wait_for_pid_exit(pid, shutdown_timeout):
            self.platform.kill_pid(pid)
            self._wait_for_pid_exit(pid, self.shutdown_config.kill_timeout)

    def _start_log_relays(self, name: str) -> list[subprocess.Popen]:
        """Start one stand-in relay per stream for a relay-backed service."""
        log_path = self.log_dir / f"{name}.log"
        relays: list[subprocess.Popen] = []
        try:
            for source in ("stdout", "stderr"):
                relays.append(self._spawn_log_relay(name, source, log_path))
        except BaseException:
            self._abort_log_relays(relays)
            raise
        return relays

    def _spawn_log_relay(
        self,
        name: str,
        source: str,
        log_path: Path,
    ) -> subprocess.Popen:
        command = [
            sys.executable,
            "-m",
            "haniel.core.log_sink",
            "--service",
            name,
            "--source",
            source,
            "--log-file",
            str(log_path),
        ]
        environment = _log_relay_environment()
        popen_kwargs = self.platform.get_subprocess_kwargs()
        try:
            return _popen_log_relay(command, environment, popen_kwargs)
        except PermissionError:
            # CREATE_BREAKAWAY_FROM_JOB requires specific Job Object
            # permissions. Retry without breakaway flag if denied.
            if os.name == "nt" and "creationflags" in popen_kwargs:
                from haniel.platform.windows import CREATE_BREAKAWAY_FROM_JOB

                flags = popen_kwargs["creationflags"]
                if flags & CREATE_BREAKAWAY_FROM_JOB:
                    retry_kwargs = dict(popen_kwargs)
                    retry_kwargs["creationflags"] = flags & ~CREATE_BREAKAWAY_FROM_JOB
                    return _popen_log_relay(command, environment, retry_kwargs)
            raise

    @staticmethod
    def _start_relay_reaper(relay: subprocess.Popen) -> None:
        """Reap a relay process so it never stays a zombie."""

        def reap() -> None:
            try:
                relay.wait()
            except Exception:
                pass

        threading.Thread(target=reap, daemon=True, name="haniel-log-relay").start()

    @staticmethod
    def _close_log_relay_stdin(relays: list[subprocess.Popen]) -> None:
        """Drop the parent's write ends so EOF follows the service exit."""
        for relay in relays:
            stream = relay.stdin
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _abort_log_relays(self, relays: list[subprocess.Popen]) -> None:
        self._close_log_relay_stdin(relays)
        for relay in relays:
            if relay.poll() is None:
                relay.terminate()

    def _spawn_process(
        self,
        name: str,
        cmd: str | list[str],
        cwd: Path,
        popen_kwargs: dict,
        child_env: dict[str, str],
        *,
        record_failure: bool = True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) -> subprocess.Popen:
        """Spawn a process, preserving Windows breakaway fallback behavior."""
        try:
            return subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
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
                            stdout=stdout,
                            stderr=stderr,
                            text=True,
                            env=child_env,
                            **retry_kwargs,
                        )
                    except (OSError, subprocess.SubprocessError) as e:
                        if record_failure:
                            self.health_manager.record_crash(
                                name, exit_code=None, reason=str(e)
                            )
                        raise self._start_failure(name, cmd, cwd, e) from e

            error = PermissionError("permission denied")
            if record_failure:
                self.health_manager.record_crash(
                    name, exit_code=None, reason="PermissionError"
                )
            raise self._start_failure(name, cmd, cwd, error)
        except (OSError, subprocess.SubprocessError) as e:
            if record_failure:
                self.health_manager.record_crash(name, exit_code=None, reason=str(e))
            raise self._start_failure(name, cmd, cwd, e) from e

    @staticmethod
    def _start_failure(
        name: str, cmd: str | list[str], cwd: Path, error: Exception
    ) -> RuntimeError:
        """Say which service, folder, and executable failed, and what to check."""
        if isinstance(cmd, str):
            parts = cmd.split()
            executable = parts[0] if parts else cmd
        else:
            executable = str(cmd[0]) if cmd else ""
        message = (
            f"Failed to start service {name} (cwd: {cwd}, executable: {executable}): "
            f"{type(error).__name__}: {error}"
        )
        if not Path(cwd).is_dir():
            message += (
                f". services.{name}.cwd path does not exist: {cwd}; "
                "does the repository need to be cloned?"
            )
        elif isinstance(error, PermissionError):
            message += f". Check that {executable} has execute permission"
        return RuntimeError(message)

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
        relay_processes: list[subprocess.Popen],
    ) -> tuple[subprocess.Popen, list[subprocess.Popen]]:
        if extract_ready_port(config) is None:
            return process, relay_processes

        exit_code = self._wait_for_immediate_exit(process)
        if exit_code is None:
            return process, relay_processes

        self._capture_exited_process_output(process, log_capture)
        cleaner = StaleInstanceCleaner(self.platform)
        if not cleaner.has_ready_port_occupants(config):
            return process, relay_processes

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

        retry_relays = self._start_log_relays(name) if relay_processes else []
        for relay in retry_relays:
            self._start_relay_reaper(relay)
        try:
            retry = self._spawn_process(
                name,
                cmd,
                cwd,
                popen_kwargs,
                child_env,
                stdout=retry_relays[0].stdin if retry_relays else subprocess.PIPE,
                stderr=retry_relays[1].stdin if retry_relays else subprocess.PIPE,
            )
        except Exception:
            self._abort_log_relays(retry_relays)
            raise
        self._close_log_relay_stdin(retry_relays)
        self.platform.setup_process_group(retry)
        return retry, retry_relays

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
        """Stop a service process under the service's lifecycle lock.

        Args:
            name: Service name
            timeout: Graceful shutdown timeout (default: from config)
            force: If True, skip graceful shutdown and kill immediately

        Returns:
            True if the service was stopped successfully
        """
        with self.service_locks.hold(name):
            return self._stop_service_locked(name, timeout=timeout, force=force)

    def _stop_service_locked(
        self,
        name: str,
        timeout: float | None,
        force: bool,
    ) -> bool:
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

    def wait_for_ready(
        self,
        name: str,
        timeout: float | None = None,
    ) -> bool:
        """Wait for a service to become ready.

        A process that exits while it is still being waited on fails the wait
        instead of holding it for the whole timeout. Use
        readiness_failure_detail() to tell that failure from a timeout.

        Args:
            name: Service name
            timeout: Maximum time to wait (default: 60s)

        Returns:
            True if the service is ready, False if timeout or process exit
        """
        with self._lock:
            if name not in self._processes:
                return False
            managed = self._processes[name]

        return self._wait_for_managed_ready(managed, timeout)

    def _wait_for_managed_ready(
        self, managed: ManagedProcess, timeout: float | None
    ) -> bool:
        if managed.ready_event is None:
            return True

        timeout = timeout or self.DEFAULT_READY_TIMEOUT
        deadline = time.monotonic() + timeout
        failure_event = managed.ready_failure_event
        if failure_event is None:
            return managed.ready_event.wait(timeout=timeout)
        while True:
            remaining = deadline - time.monotonic()
            wait_for = min(self.POLL_INTERVAL, max(remaining, 0.0))
            if managed.ready_event.wait(timeout=wait_for):
                return True
            if failure_event.is_set():
                return False
            if remaining <= 0:
                return False

    def readiness_failure_detail(self, name: str) -> str | None:
        """Describe a readiness wait that ended because the process exited."""
        with self._lock:
            managed = self._processes.get(name)
        if managed is None or managed.ready_failure_event is None:
            return None
        if not managed.ready_failure_event.is_set():
            return None
        if managed.ready_exit_code is None:
            return f"{name} process exited during readiness wait"
        return (
            f"{name} process exited during readiness wait "
            f"(exit code {managed.ready_exit_code})"
        )

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
            # No ready condition: record RUNNING (not READY); the heartbeat
            # reports ready=true for a running service with no ready condition.
            if managed.ready_event:
                managed.ready_event.set()
            self.health_manager.record_running(managed.name, confirmed=False)
            self._start_stable_running_monitor(managed)
            if on_ready:
                on_ready()
            return

        problem = ready_format_error(ready_str)
        if problem:
            logger.warning("Service %s: %s", managed.name, problem)

        try:
            condition = ReadyCondition.parse(ready_str)
        except ValueError:
            # Invalid condition: record RUNNING; the heartbeat keeps ready=false
            # because the service does have a ready condition.
            if managed.ready_event:
                managed.ready_event.set()
            self.health_manager.record_running(managed.name, confirmed=False)
            self._start_stable_running_monitor(managed)
            return

        # Start ready monitor thread
        thread = threading.Thread(
            target=self._ready_monitor_loop,
            args=(managed, condition, timeout, on_ready),
            daemon=True,
        )
        thread.start()

    def _start_stable_running_monitor(self, managed: ManagedProcess) -> None:
        """Count a service with no usable ready condition as working once it stays up."""
        threading.Thread(
            target=self._stable_running_loop,
            args=(managed,),
            daemon=True,
        ).start()

    def _stable_running_loop(self, managed: ManagedProcess) -> None:
        deadline = time.monotonic() + self.stable_running_seconds
        while True:
            if managed.intentional_stop or (
                managed.process is not None and managed.process.poll() is not None
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.POLL_INTERVAL, remaining))
        self.health_manager.record_stable(managed.name)

    def _ready_monitor_loop(
        self,
        managed: ManagedProcess,
        condition: ReadyCondition,
        timeout: float,
        on_ready: Callable[[], None] | None,
    ) -> None:
        """Monitor loop for ready condition.

        Past the ready limit the same condition keeps being checked at a
        slower interval, so a slow service is still recorded ready when its
        condition finally matches.
        """
        start_time = time.time()

        # Special handling for delay condition
        if condition.type == ReadyConditionType.DELAY:
            try:
                delay = float(condition.value)
                remaining = min(delay, timeout) if delay > 0 else 0.0
                while remaining > 0:
                    if self._ready_monitor_should_stop(managed):
                        return
                    step = min(self.POLL_INTERVAL, remaining)
                    time.sleep(step)
                    remaining -= step
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

            deadline = start_time + timeout
            warned = False
            while True:
                now = time.time()
                if now >= deadline and not warned:
                    self._warn_ready_timeout(managed, timeout)
                    warned = True
                wait_for = (
                    self.ready_recheck_interval
                    if now >= deadline
                    else min(self.ready_recheck_interval, max(deadline - now, 0.0))
                )
                if log_ready_event.wait(timeout=min(wait_for, self.POLL_INTERVAL)):
                    self._record_ready(managed, start_time, timeout, on_ready)
                    return
                if self._ready_monitor_should_stop(managed):
                    return

        # Polling-based conditions
        deadline = start_time + timeout
        warned = False
        while True:
            if self._ready_monitor_should_stop(managed):
                return

            if self._check_ready_condition(condition, managed):
                self._record_ready(managed, start_time, timeout, on_ready)
                return

            now = time.time()
            if now >= deadline and not warned:
                self._warn_ready_timeout(managed, timeout)
                warned = True
            wait_for = (
                self.ready_recheck_interval
                if now >= deadline
                else min(self.POLL_INTERVAL, max(deadline - now, 0.0))
            )
            time.sleep(wait_for)

    def _ready_monitor_should_stop(self, managed: ManagedProcess) -> bool:
        """Stop watching when the process or its ownership is gone.

        A process that exits on its own fails the pending readiness wait; an
        intentional stop or a replaced owner only ends the watch.
        """
        if managed.intentional_stop:
            return True
        if managed.process is not None and managed.process.poll() is not None:
            self._record_ready_failure(managed)
            return True
        return (
            self._processes.get(managed.name) is not managed
            and self._pending_processes.get(managed.name) is not managed
        )

    def _record_ready_failure(self, managed: ManagedProcess) -> None:
        """Wake readiness waiters with the exit code of a dead process."""
        if managed.ready_failure_event is None:
            return
        managed.ready_exit_code = (
            managed.process.poll() if managed.process is not None else None
        )
        managed.ready_failure_event.set()

    def _record_ready(
        self,
        managed: ManagedProcess,
        start_time: float,
        timeout: float,
        on_ready: Callable[[], None] | None,
    ) -> None:
        """Record ready and note when it happened past the limit."""
        if managed.ready_event:
            managed.ready_event.set()
        self.health_manager.record_ready(managed.name)
        if on_ready:
            on_ready()
        elapsed = time.time() - start_time
        if elapsed > timeout:
            logger.info(
                "Ready condition for %s satisfied after the %.0fs limit (%.1fs)",
                managed.name,
                timeout,
                elapsed,
            )

    @staticmethod
    def _warn_ready_timeout(managed: ManagedProcess, timeout: float) -> None:
        logger.warning(
            "Ready condition not confirmed within %.0fs for %s; continuing to check",
            timeout,
            managed.name,
        )

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
            return self._check_http_ready(condition.value)

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
        exit_code: int | None = process.wait()
        exit_code_known = not isinstance(process, PidProcessHandle)
        if not exit_code_known:
            exit_code = None

        # Check if this was a graceful stop
        health = self.health_manager.get_health(managed.name)
        if managed.intentional_stop or health.state == ServiceState.STOPPING:
            # Intentional stop, not a crash
            return

        # This is a crash
        if not exit_code_known:
            logger.warning(
                "Adopted process of %s (pid %s) disappeared; its exit code is unknown",
                managed.name,
                process.pid,
            )
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


def _popen_log_relay(
    command: list[str],
    environment: dict[str, str],
    popen_kwargs: dict,
) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        env=environment,
        **popen_kwargs,
    )


def _log_relay_environment() -> dict[str, str]:
    """Make the haniel package importable by ``-m haniel.core.log_sink``."""
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2])
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not existing else source_root + os.pathsep + existing
    )
    return environment


def relay_capable(config: ServiceConfig) -> bool:
    """Whether this service's output can go through a stand-in relay.

    ``ready: log:`` conditions read lines through LogCapture, so those
    services keep the direct pipe.
    """
    return not (config.ready or "").startswith("log:")
