"""
haniel runner module.

Implements the poll → pull → restart cycle:
- Phase 1: Change detection (git fetch)
- Phase 2: Change application (shutdown → pull → hooks → restart)
- Phase 3: Health check (process survival)

haniel doesn't care what it runs. It polls, pulls, and restarts as configured.
"""

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import (
    BackoffConfig,
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    ShutdownConfig,
)
from ..config.release_activation import (
    ReleaseManifestActivationRequired,
    activate_release_manifest,
    discover_remote_release_manifest,
    plan_release_manifest_activation,
)
from .child_env import sanitized_child_env
from .change_evidence import get_applied_change_evidence
from .git import (
    GitError,
    fetch_repo,
    get_head,
    get_pending_changes,
    get_remote_head,
    reset_repo_to,
    pull_repo,
    sha256_file_at_commit,
)
from .health import HealthManager, ServiceState
from .process import ProcessManager
from .deployment import DeploymentError, DeploymentStateStore
from .repo_reconciliation import (
    RepoReconciliationSnapshot,
    capture_repo_snapshot,
)
from .runner_deployment import run_manifest_deployment
from .deploy_retry_planner import DeployRetryPlanner
from .orchestrated_deploy_execution import (
    OrchestratedDeployRegistry,
    assert_remote_target,
    build_recovery_evidence,
    execute_approved_plan,
    validate_approved_plan,
)
from .self_update_marker import (
    SelfUpdateResult,
    read_and_consume as _read_self_update_marker,
)
from .orch_pending_deploy import (
    read_and_consume as _read_orch_pending_deploy,
    write as _write_orch_pending_deploy,
)
from ..integrations.deploy_reporting import ApprovalRevalidationError

if TYPE_CHECKING:
    from ..integrations.orchestrator_client import OrchestratorClient
    from ..integrations.slack_bot import SlackBot

logger = logging.getLogger(__name__)


class CyclicDependencyError(Exception):
    """Raised when a cyclic dependency is detected in service dependencies."""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"Cyclic dependency detected: {' -> '.join(cycle)}")


class DependencyGraph:
    """Represents service dependency relationships.

    Used for:
    - Topological sort for startup order
    - Reverse sort for shutdown order
    - Finding affected services when a repo changes
    """

    def __init__(self, services: dict[str, ServiceConfig]):
        """Initialize the dependency graph.

        Args:
            services: Dict of service name to config
        """
        self._services = services
        self._build_graph()

    def _build_graph(self) -> None:
        """Build adjacency lists for the graph."""
        # Forward edges: service -> services it depends on
        self._dependencies: dict[str, set[str]] = {}
        # Reverse edges: service -> services that depend on it
        self._dependents: dict[str, set[str]] = {}

        for name in self._services:
            self._dependencies[name] = set()
            self._dependents[name] = set()

        for name, config in self._services.items():
            for dep in config.after:
                if dep in self._services:  # Only track existing services
                    self._dependencies[name].add(dep)
                    self._dependents[dep].add(name)

    def topological_sort(self, reverse: bool = False) -> list[str]:
        """Return services in topological order.

        Args:
            reverse: If True, return reverse order (for shutdown)

        Returns:
            List of service names in dependency order

        Raises:
            CyclicDependencyError: If a cycle is detected
        """
        if not self._services:
            return []

        # Kahn's algorithm
        in_degree: dict[str, int] = {name: 0 for name in self._services}
        for name in self._services:
            for dep in self._dependencies[name]:
                in_degree[name] += 1

        # Pre-compute order indices for O(1) lookup in sorting
        order_index = {name: i for i, name in enumerate(self._services.keys())}

        # Start with nodes that have no dependencies
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result: list[str] = []

        while queue:
            # Sort queue for deterministic order (YAML order preserved for ties)
            queue.sort(key=lambda x: order_index[x])
            node = queue.pop(0)
            result.append(node)

            for dependent in self._dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._services):
            # Cycle detected - find it for error message
            remaining = [n for n in self._services if n not in result]
            raise CyclicDependencyError(remaining)

        if reverse:
            result.reverse()

        return result

    def get_dependents(self, service: str) -> list[str]:
        """Get all services that depend on the given service.

        Args:
            service: Service name

        Returns:
            List of dependent service names
        """
        if service not in self._dependents:
            return []
        return list(self._dependents[service])

    def get_dependencies(self, service: str) -> list[str]:
        """Get all services that the given service depends on.

        Args:
            service: Service name

        Returns:
            List of dependency service names
        """
        if service not in self._dependencies:
            return []
        return list(self._dependencies[service])

    def get_all_dependents(self, service: str) -> set[str]:
        """Get all services that transitively depend on the given service.

        Args:
            service: Service name

        Returns:
            Set of all dependent service names (transitive closure)
        """
        result: set[str] = set()
        queue = list(self._dependents.get(service, set()))

        while queue:
            dep = queue.pop(0)
            if dep not in result:
                result.add(dep)
                queue.extend(self._dependents.get(dep, set()))

        return result


def topological_sort(
    services: dict[str, ServiceConfig], reverse: bool = False
) -> list[str]:
    """Standalone topological sort function.

    Args:
        services: Dict of service name to config
        reverse: If True, return reverse order

    Returns:
        List of service names in dependency order
    """
    graph = DependencyGraph(services)
    return graph.topological_sort(reverse=reverse)


@dataclass
class RepoState:
    """Tracks the state of a repository."""

    name: str
    config: RepoConfig
    last_head: str | None = None
    last_fetch: datetime | None = None
    fetch_error: str | None = None
    pending_changes: dict | None = None  # {"commits": [...], "stat": "..."}


@dataclass
class RunnerState:
    """Overall runner state."""

    running: bool = False
    start_time: datetime | None = None
    last_poll: datetime | None = None
    poll_count: int = 0
    self_update_pending: bool = False


class ServiceRunner:
    """Manages the poll → pull → restart cycle.

    Responsibilities:
    - Start services in dependency order
    - Poll repositories for changes
    - Restart affected services when repos change
    - Handle process crashes (via HealthManager)
    """

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        log_dir: Path | None = None,
        config_path: Path | None = None,
    ):
        """Initialize the runner.

        Args:
            config: Haniel configuration
            config_dir: Base directory for resolving relative paths
            log_dir: Directory for log files (default: config_dir/logs)
            config_path: Absolute path to the haniel.yaml file. When set, the
                dashboard config API can read/write the file and reload_config()
                is operational. When None, config API returns 501.
        """
        self.config = config
        self.config_dir = config_dir
        self.log_dir = log_dir or config_dir / "logs"
        self.config_path = config_path

        self.poll_interval = config.poll_interval

        # Extract backoff config
        backoff = config.backoff or BackoffConfig()
        shutdown = config.shutdown or ShutdownConfig()

        # Initialize managers
        self.health_manager = HealthManager(
            base_delay=backoff.base_delay,
            max_delay=backoff.max_delay,
            circuit_breaker_threshold=backoff.circuit_breaker,
            circuit_breaker_window=backoff.circuit_window,
        )
        self.process_manager = ProcessManager(
            config_dir=config_dir,
            log_dir=self.log_dir,
            shutdown_config=shutdown,
            health_manager=self.health_manager,
        )

        # Build dependency graph for enabled services only
        self._enabled_services = {
            name: svc for name, svc in config.services.items() if svc.enabled
        }
        self._dependency_graph = DependencyGraph(self._enabled_services)

        # Initialize repo states
        self._repo_states: dict[str, RepoState] = {}
        for name, repo_config in config.repos.items():
            self._repo_states[name] = RepoState(name=name, config=repo_config)

        # Runner state
        self._state = RunnerState()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

        # Restart scheduling
        self._pending_restarts: dict[str, float] = {}  # service -> restart_time
        self._restart_suppressed: set[str] = set()
        self._restart_lock = threading.Lock()

        # MCP server (lazy initialized)
        self._mcp_server = None

        # WebSocket handler (set by MCP server after dashboard setup)
        self._ws_handler = None

        # Slack bot (initialized in start() if configured)
        self._slack_bot: "SlackBot | None" = None

        # Orchestrator client (initialized in start() if configured)
        self._orch_client: "OrchestratorClient | None" = None
        self._orchestrated_deploys = OrchestratedDeployRegistry()

        # Per-repo pull locks: acquire(blocking=False) for atomic duplicate guard
        self._pull_locks: dict[str, threading.Lock] = {
            name: threading.Lock() for name in self._repo_states
        }

        # Deduplication: track last notified pending_changes hash per repo
        # to avoid spamming Slack on every poll when content hasn't changed.
        self._last_pending_hash: dict[str, str] = {}

        # Track whether startup post_pull hooks have been considered.
        self._post_pull_executed = False
        self._startup_updated_repos: set[str] = set()
        self._startup_manifest_updates: dict[str, str] = {}
        self._startup_repo_locks: set[str] = set()

        # Self-update (see ADR-0002)
        self._self_repo: str | None = (
            config.self_update.repo if config.self_update else None
        )
        self._self_update_requested = threading.Event()
        self._restart_requested = threading.Event()

        # Self-update result from previous wrapper iteration (consumed on start)
        self._last_self_update_result: SelfUpdateResult | None = None

    @property
    def is_running(self) -> bool:
        """Check if the runner is active."""
        return self._state.running

    def reload_config(self) -> None:
        """Reload configuration from disk and apply changes.

        Re-reads haniel.yaml, updates poll_interval, enabled services,
        dependency graph, and repo states. Running processes are not stopped;
        the new config takes effect on the next poll cycle.

        Raises:
            RuntimeError: If config_path was not provided at construction time.
        """
        from ..config import load_config

        if not self.config_path:
            raise RuntimeError("config_path is not set — cannot reload configuration")

        new_config = load_config(self.config_path)
        self.config = new_config
        self.poll_interval = new_config.poll_interval

        # Rebuild enabled-services index and dependency graph
        self._enabled_services = {
            name: svc for name, svc in new_config.services.items() if svc.enabled
        }
        self._dependency_graph = DependencyGraph(self._enabled_services)

        # Merge repo states — preserve last_fetch / last_head for existing repos
        existing: dict[str, RepoState] = dict(self._repo_states)
        self._repo_states = {}
        for name, repo_cfg in new_config.repos.items():
            if name in existing:
                existing[name].config = repo_cfg
                self._repo_states[name] = existing[name]
            else:
                self._repo_states[name] = RepoState(name=name, config=repo_cfg)

        # Sync pull locks — preserve existing locks, create new ones, drop removed
        self._pull_locks = {
            name: self._pull_locks.get(name, threading.Lock())
            for name in self._repo_states
        }

        # Update self-update repo reference
        self._self_repo = (
            new_config.self_update.repo if new_config.self_update else None
        )

        logger.info("Configuration reloaded from %s", self.config_path)

    def get_startup_order(self) -> list[str]:
        """Get the order in which services should start.

        Returns:
            List of service names in startup order
        """
        return self._dependency_graph.topological_sort()

    def get_shutdown_order(self) -> list[str]:
        """Get the order in which services should stop.

        Returns:
            List of service names in shutdown order (reverse of startup)
        """
        return self._dependency_graph.topological_sort(reverse=True)

    def get_affected_services(self, repo_name: str) -> list[str]:
        """Get services affected by changes to a repository.

        Args:
            repo_name: Name of the repository

        Returns:
            List of service names that depend on this repo
        """
        # Find services that directly depend on this repo
        directly_affected: set[str] = set()
        for name, config in self._enabled_services.items():
            if config.repo == repo_name:
                directly_affected.add(name)

        # Include transitively dependent services
        all_affected: set[str] = set(directly_affected)
        for service in directly_affected:
            all_affected.update(self._dependency_graph.get_all_dependents(service))

        return list(all_affected)

    def execute_hook(self, service_name: str, hook_name: str) -> bool:
        """Execute a lifecycle hook for a service.

        Args:
            service_name: Name of the service
            hook_name: Name of the hook (e.g., "post_pull")

        Returns:
            True if hook executed successfully or doesn't exist
        """
        if service_name not in self._enabled_services:
            return True

        config = self._enabled_services[service_name]
        if not config.hooks:
            return True

        hook_cmd = getattr(config.hooks, hook_name, None)
        if not hook_cmd:
            return True

        # Determine working directory
        cwd = self.config_dir
        if config.cwd:
            cwd = self.config_dir / config.cwd

        # Substitute {root} placeholder with the absolute config directory path,
        # mirroring the same pattern used in installer/mechanical.py _apply_config_template
        hook_cmd = hook_cmd.replace("{root}", str(self.config_dir))

        # On Windows, cmd.exe cannot parse "./path" — it treats "." as a command
        # name and fails with "'.' is not recognized".  Resolve all "./" prefixes
        # to the absolute config directory so cmd.exe receives a valid path.
        if os.name == "nt":
            config_prefix = str(self.config_dir).replace("\\", "/") + "/"
            hook_cmd = re.sub(r"(?<![.\w])\./", config_prefix, hook_cmd)

        logger.info(f"Executing {hook_name} hook for {service_name}: {hook_cmd}")

        try:
            # Use shell=True when the command contains shell operators (&&, ||,
            # ;, |) so they are interpreted correctly on all platforms.
            # On Windows this is always needed for .cmd/.bat executables too.
            shell_operators = re.search(r"&&|\|\||[;|]", hook_cmd)
            if os.name == "nt" or shell_operators:
                run_cmd: str | list[str] = hook_cmd
                shell = True
            else:
                run_cmd = shlex.split(hook_cmd)
                shell = False

            subprocess.run(
                run_cmd,
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for hooks
                shell=shell,
                env=sanitized_child_env(),
            )
            logger.info(f"Hook {hook_name} for {service_name} completed successfully")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Hook {hook_name} for {service_name} failed with exit code {e.returncode}: {e.stderr}"
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"Hook {hook_name} for {service_name} timed out")
            return False
        except Exception as e:
            logger.error(f"Hook {hook_name} for {service_name} failed: {e}")
            return False

    def start_services(self) -> None:
        """Start services and always release startup deployment leases."""
        try:
            self._start_services_in_dependency_order()
        finally:
            self._release_startup_repo_locks()

    def _start_services_in_dependency_order(self) -> None:
        """Start all enabled services in dependency order.

        On first start, executes post_pull hooks only for services whose repos
        were actually updated during the startup pull pass. Restarting Haniel is
        a controlled downtime window for pulling all repos, but unchanged repos
        should not pay rebuild cost.
        """
        startup_order = self.get_startup_order()
        logger.info(f"Starting services in order: {startup_order}")

        # Run legacy post_pull hooks on first start only for repos updated at startup.
        if not self._post_pull_executed:
            self._post_pull_executed = True
            for name in startup_order:
                service = self._enabled_services[name]
                if service.repo in self._startup_updated_repos:
                    self.execute_hook(name, "post_pull")

        manifest_services: dict[str, list[str]] = {}
        for repo_name in self._startup_manifest_updates:
            manifest_services[repo_name] = [
                name
                for name in startup_order
                if self._enabled_services[name].repo == repo_name
            ]

        handled_manifest_services: set[str] = set()
        for name in startup_order:
            if name in handled_manifest_services:
                continue
            repo_name = self._enabled_services[name].repo
            if repo_name not in self._startup_manifest_updates:
                self._start_service(name)
                continue

            affected = manifest_services[repo_name]
            handled_manifest_services.update(affected)
            try:
                run_manifest_deployment(
                    self,
                    repo_name,
                    affected,
                    self._startup_manifest_updates[repo_name],
                    desired_running=set(affected),
                )
            except DeploymentError as error:
                if not error.recovered:
                    raise
                logger.error(
                    "Startup deployment failed for %s but availability recovered: %s",
                    repo_name,
                    error,
                )

    def _release_startup_repo_locks(self) -> None:
        """Release repo leases retained across startup manifest handover."""
        for repo_name in tuple(self._startup_repo_locks):
            lock = self._pull_locks.get(repo_name)
            if lock is not None and lock.locked():
                lock.release()
            self._startup_repo_locks.discard(repo_name)

    def _start_service(self, name: str) -> bool:
        """Start a single service.

        Args:
            name: Service name

        Returns:
            True if started successfully
        """
        if name not in self._enabled_services:
            return False

        config = self._enabled_services[name]
        logger.info(f"Starting service: {name}")

        try:
            if not self.execute_hook(name, "pre_start"):
                logger.error(f"pre_start hook failed for {name}, aborting start")
                self._record_start_failure(name, "pre_start hook failed")
                return False
            self.process_manager.start_service(
                name=name,
                config=config,
                on_ready=lambda n=name: self._on_service_ready(n),
                on_crash=lambda exit_code, n=name: self._on_service_crash(n, exit_code),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start service {name}: {e}")
            self._schedule_start_failure_retry(name, str(e))
            return False

    def _on_service_ready(self, name: str) -> None:
        """Called when a service becomes ready."""
        logger.info(f"Service {name} is ready")

    def _record_start_failure(self, name: str, reason: str) -> None:
        """Record a pre-process start failure and schedule retry via backoff."""
        delay = self.health_manager.record_crash(name, exit_code=None, reason=reason)
        if delay > 0 and self.health_manager.should_restart(name):
            logger.info(
                "Scheduling restart of %s in %ss after start failure",
                name,
                delay,
            )
            self._schedule_restart(name, delay)
        else:
            logger.error("Circuit breaker open for %s, not restarting", name)

    def _schedule_start_failure_retry(self, name: str, reason: str) -> None:
        health = self.health_manager.get_health(name)
        if health.state != ServiceState.CRASHED:
            self._record_start_failure(name, reason)
            return

        if self.health_manager.should_restart(name):
            delay = health.get_restart_delay()
            logger.info(
                "Scheduling restart of %s in %ss after start failure",
                name,
                delay,
            )
            self._schedule_restart(name, delay)
        else:
            logger.error("Circuit breaker open for %s, not restarting", name)

    def _blocked_start_dependencies(self, name: str) -> list[str]:
        blockers = []
        for dependency in self._dependency_graph.get_dependencies(name):
            if (
                dependency in self._enabled_services
                and not self.process_manager.is_running(dependency)
            ):
                blockers.append(dependency)
        return sorted(blockers)

    def _schedule_restart_after_dependency_block(
        self,
        name: str,
        blockers: list[str],
    ) -> None:
        circuit_open = []
        for dependency in blockers:
            health = self.health_manager.get_health(dependency)
            if health.state == ServiceState.CIRCUIT_OPEN:
                circuit_open.append(dependency)
        if circuit_open:
            logger.error(
                "Skipping restart of %s because dependency circuit is open: %s",
                name,
                ", ".join(circuit_open),
            )
            return

        with self._restart_lock:
            pending_recovery = [
                dependency
                for dependency in blockers
                if dependency in self._pending_restarts
                or dependency in self._restart_suppressed
            ]

        if not pending_recovery:
            logger.error(
                "Skipping restart of %s because dependencies are not running and "
                "not queued for recovery: %s",
                name,
                ", ".join(blockers),
            )
            return

        dependency_delays = [
            self.health_manager.get_health(dependency).get_restart_delay()
            for dependency in pending_recovery
        ]
        delay = max([self.health_manager.base_delay, *dependency_delays])
        logger.warning(
            "Skipping restart of %s until dependencies recover: %s. Retrying in %ss",
            name,
            ", ".join(blockers),
            delay,
        )
        self._schedule_restart(name, delay)

    def _on_service_crash(self, name: str, exit_code: int | None) -> None:
        """Called when a service crashes.

        Args:
            name: Service name
            exit_code: Exit code (None if signal)
        """
        logger.warning(f"Service {name} crashed with exit code {exit_code}")

        if self.health_manager.get_health(name).state == ServiceState.STOPPING:
            logger.info("Skipping restart schedule for intentionally stopping %s", name)
            return

        # record_crash returns the delay and handles circuit breaker atomically
        # Note: The crash is already recorded by ProcessManager's crash monitor,
        # so we just need to check if we should restart
        if self.health_manager.should_restart(name):
            health = self.health_manager.get_health(name)
            delay = health.get_restart_delay()
            logger.info(f"Scheduling restart of {name} in {delay}s")
            self._schedule_restart(name, delay)
        else:
            logger.error(f"Circuit breaker open for {name}, not restarting")

    def _schedule_restart(self, name: str, delay: float) -> None:
        """Schedule a service restart after a delay.

        Args:
            name: Service name
            delay: Delay in seconds
        """
        with self._restart_lock:
            restart_time = time.time() + delay
            self._pending_restarts[name] = restart_time

    def _cancel_pending_restart(self, name: str) -> bool:
        """Cancel a pending service restart if one is queued.

        Returns:
            True when a queued restart was removed.
        """
        with self._restart_lock:
            if name not in self._pending_restarts:
                return False
            del self._pending_restarts[name]

        logger.info("Cancelled pending restart for %s", name)
        return True

    def _suppress_pending_restarts(self, names: list[str]) -> None:
        """Prevent scheduled restarts from racing a pull-owned restart."""
        if not names:
            return

        with self._restart_lock:
            for name in names:
                self._pending_restarts.pop(name, None)
            self._restart_suppressed.update(names)

    def _release_restart_suppression(self, names: list[str]) -> None:
        """Allow scheduled restarts again after pull restart ownership ends."""
        if not names:
            return

        with self._restart_lock:
            for name in names:
                self._restart_suppressed.discard(name)

    def stop_services(self) -> bool:
        """Stop all services in reverse dependency order."""
        shutdown_order = self.get_shutdown_order()
        logger.info(f"Stopping services in order: {shutdown_order}")
        all_stopped = True

        for name in shutdown_order:
            self._cancel_pending_restart(name)
            if self.process_manager.is_running(name):
                logger.info(f"Stopping service: {name}")
                if not self.process_manager.stop_service(name):
                    logger.error("Failed to stop service: %s", name)
                    all_stopped = False

        return all_stopped

    def _prepare_self_update_shutdown(self) -> None:
        """Stop managed services before allowing Haniel itself to update."""
        if not self.stop_services():
            raise RuntimeError("Failed to stop all services for self-update")

    def start(self) -> None:
        """Start the runner (services + poll loop + MCP server)."""
        if self._state.running:
            return

        logger.info("Starting ServiceRunner")
        self._state.running = True
        self._state.start_time = datetime.now()
        self._stop_event.clear()

        # Initialize repo states (get current HEAD)
        self._init_repo_states()
        self._recover_interrupted_deployment_journals()

        # Consume self-update result from previous wrapper iteration (if any).
        # Done before MCP server starts so ws_handler.setup(loop) can broadcast it.
        self._last_self_update_result = _read_self_update_marker(self.config_dir)
        if self._last_self_update_result is not None:
            logger.info(
                "Loaded self-update result: ok=%s err=%s",
                self._last_self_update_result.ok,
                self._last_self_update_result.error,
            )

        # Start MCP server if enabled
        self._start_mcp_server()

        # Start Slack bot if configured
        self._start_slack_bot()

        # Start orchestrator client if configured
        self._start_orch_client()

        # Apply pending updates before starting services
        self._apply_startup_updates()

        # Start services
        self.start_services()

        # Start poll loop in background
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
        )
        self._poll_thread.start()

    def stop(self) -> None:
        """Stop the runner (poll loop + services + MCP server)."""
        if not self._state.running:
            return

        logger.info("Stopping ServiceRunner")
        with self._state_lock:
            self._state.running = False
        self._stop_event.set()

        # Stop Slack bot
        if self._slack_bot:
            self._slack_bot.notify_shutdown()  # best-effort, exceptions handled internally
            try:
                self._slack_bot.stop()
            except Exception as e:
                logger.warning("Error stopping Slack bot: %s", e)

        # Stop MCP server
        if self._mcp_server:
            try:
                self._mcp_server.stop_sync()
            except Exception as e:
                logger.warning(f"Error stopping MCP server: {e}")

        # Wait for poll thread
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

        # Stop orchestrator client (after poll thread to avoid race with notify_change)
        if self._orch_client:
            self._orch_client.stop()

        # Stop services
        self.stop_services()

    def _start_mcp_server(self) -> None:
        """Start the MCP server if enabled."""
        if not self.config.mcp or not self.config.mcp.enabled:
            logger.info("MCP server is disabled")
            return

        try:
            from ..integrations.mcp_server import HanielMcpServer

            self._mcp_server = HanielMcpServer(self)
            self._mcp_server.start_background()
            logger.info(f"MCP server starting on port {self._mcp_server.port}")
        except ImportError as e:
            logger.warning(f"MCP dependencies not available: {e}")
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")

    def _start_slack_bot(self) -> None:
        """Start the Slack bot if configured and enabled."""
        if not self.config.slack or not self.config.slack.enabled:
            logger.info("Slack bot is disabled")
            return

        try:
            from ..integrations.slack_bot import SlackBot
        except ImportError:
            logger.error(
                "slack-bolt package is required for Slack integration. "
                "Install it with: pip install slack-bolt"
            )
            self._slack_bot = None
            return

        try:
            self._slack_bot = SlackBot(
                config=self.config.slack,
                approve_callback=self.trigger_pull,
                app_home_controller=self,
            )
            self._slack_bot.start()
        except Exception as e:
            logger.error("Failed to start Slack bot: %s", e)
            self._slack_bot = None

    def _start_orch_client(self) -> None:
        """Start orchestrator client if configured."""
        orch_cfg = self.config.orchestrator_client
        if orch_cfg is None or not orch_cfg.enabled:
            return
        try:
            from ..integrations.orchestrator_client import OrchestratorClient
            import haniel

            self._orch_client = OrchestratorClient(
                config=orch_cfg,
                haniel_version=haniel.__version__,
                get_services_info=self._collect_services_info,
                service_command_handler=self._handle_service_command,
                deploy_approval_handler=self._handle_deploy_approval,
                deploy_plan_probe_handler=self._handle_deploy_plan_probe,
                repo_snapshot_handler=self._capture_orchestrator_repo_snapshot,
            )
            # Map any self-update result from the previous wrapper iteration
            # into a buffered DeployResult before the client connects. The
            # buffer is flushed on the first successful WS connection so the
            # orch-server gets a definitive result instead of a stuck DEPLOYING.
            self._enqueue_pending_self_deploy_result()
            self._orch_client.start()
            logger.info("Orchestrator client started")
        except Exception as e:
            logger.warning(f"Failed to start orchestrator client: {e}")
            self._orch_client = None

    def _collect_services_info(self) -> list[dict]:
        """Collect service info for orchestrator reporting.

        Status fields are split into three axes so the dashboard can show
        process state and readiness independently — the previous single
        ``status`` field conflated ``running`` (HealthState) with the
        action-button trigger and hid edge cases like
        ``pid=None`` + ``health.state=ready`` (race window).

        - ``process_status``: ``'running'`` when pid is reported by the
          process manager, ``'stopped'`` when absent.
        - ``health_status``: ``HealthManager`` state value
          (``ready`` / ``running`` / ``starting`` / ``stopping`` /
          ``crashed`` / ``stopped`` / ``circuit_open``).
        - ``ready``: True iff process is running AND health state is ready.

        The legacy ``status`` key is removed; orch-server's dashboard now
        consumes ``process_status`` (action buttons) and ``health_status``
        (display) separately. See atom 260519.01.
        """
        services = []
        for name, svc_config in self.config.services.items():
            health = self.health_manager.get_health(name)
            # Extract port from ready condition (format: "port:N")
            port = None
            if svc_config.ready and svc_config.ready.startswith("port:"):
                try:
                    port = int(svc_config.ready.split(":")[1])
                except (ValueError, IndexError):
                    pass
            # Get PID via public method
            pid = self.process_manager.get_pid(name)
            process_status = "running" if pid is not None else "stopped"
            health_status = health.state.value
            services.append(
                {
                    "name": name,
                    "port": port,
                    "pid": pid,
                    "process_status": process_status,
                    "health_status": health_status,
                    "ready": (process_status == "running" and health_status == "ready"),
                    "role": svc_config.repo or "",
                    "uptime_ms": int(health.get_uptime() * 1000)
                    if health.get_uptime()
                    else 0,
                    "enabled": svc_config.enabled,
                    "deps": svc_config.after,
                }
            )
        return services

    def _handle_deploy_approval(
        self,
        approval: dict,
        progress_callback: Callable[[str], None] | None = None,
    ) -> str | dict | None:
        """Revalidate the immutable plan, then enter its sole allowed mode."""
        deploy_id = approval["deploy_id"]
        _node_id, repo, branch, _target = deploy_id.split(":", 3)
        if repo not in self._repo_states:
            raise ValueError(f"Unknown repo: {repo}")
        configured_branch = self._repo_states[repo].config.branch
        if branch != configured_branch:
            raise ApprovalRevalidationError(
                f"approval branch {branch!r} differs from configured "
                f"branch {configured_branch!r} for {repo!r}"
            )

        probe = self._orchestrated_deploys.consume_approval(approval)
        planner = self._deploy_retry_planner(repo)
        if probe is not None:
            plan = validate_approved_plan(planner, probe, approval)
            if plan.mode == "evidence_recovery":
                return build_recovery_evidence(approval, probe, plan)

        if self._self_repo and repo == self._self_repo:
            # Self-update path. trigger_pull(self_repo) calls self.stop()
            # which calls self._orch_client.stop(), which joins the
            # orch_client thread. We are running on an orch_client executor
            # thread, so calling trigger_pull here would deadlock the join.
            # Instead: persist the deploy_id so the next runner can
            # correlate the marker result with this deploy, signal the
            # self-update via approve_self_update, and stop on a
            # separate daemon thread after a small delay (gives the
            # caller coroutine time to return "deferred" and let the
            # orch_client coroutine finish cleanly).
            assert_remote_target(self, repo, _target)
            _write_orch_pending_deploy(
                self.config_dir,
                deploy_id=deploy_id,
                started_at=datetime.now(timezone.utc).isoformat(),
                orchestrator_attempt_id=approval["orchestrator_attempt_id"],
                connection_generation=approval["connection_generation"],
                execution_mode=approval["execution_mode"],
                probe_id=approval.get("probe_id"),
                preflight_fingerprint=approval.get("preflight_fingerprint"),
            )
            # Mark self-update pending so approve_self_update() proceeds
            # even if the polling loop hasn't yet detected the change.
            with self._state_lock:
                self._state.self_update_pending = True
            self.approve_self_update()
            threading.Thread(
                target=self._deferred_stop_for_self_update,
                name="haniel-deferred-stop",
                daemon=True,
            ).start()
            return "deferred"
        return execute_approved_plan(
            self,
            approval,
            probe,
            planner,
            progress_callback=progress_callback,
        )

    def _handle_deploy_plan_probe(self, probe: dict) -> dict:
        """Return a side-effect-free proposal and retain its immutable snapshot."""
        repo = probe["repo"]
        planner = self._deploy_retry_planner(repo)
        plan = planner.plan(probe)
        self._orchestrated_deploys.record_probe(probe)
        return plan.proposal(probe)

    def _deploy_retry_planner(self, repo: str) -> DeployRetryPlanner:
        state = self._repo_states.get(repo)
        if state is None:
            raise ValueError(f"Unknown repo: {repo}")
        return DeployRetryPlanner(
            repo_path=(self.config_dir / state.config.path).resolve(),
            manifest_path=state.config.release_manifest,
            journal_store=self._deployment_state_store(),
        )

    def _capture_orchestrator_repo_snapshot(
        self,
        repo: str,
        branch: str,
        deploy_id: str | None = None,
    ) -> RepoReconciliationSnapshot:
        """Capture the one Git-truth contract shared by every trigger."""
        state = self._repo_states.get(repo)
        if state is None:
            raise ValueError(f"Unknown repo: {repo}")
        return capture_repo_snapshot(
            node_id=self.config.orchestrator_client.node_id,
            repo=repo,
            branch=state.config.branch,
            path=self.config_dir / state.config.path,
            deploy_id=deploy_id,
        )

    def _deferred_stop_for_self_update(self) -> None:
        """Helper: small delay then stop. Allows the orch_client coroutine
        that returned 'deferred' to finish before orch_client.stop() joins
        the orch_client thread. Runs on a daemon thread.
        """
        time.sleep(0.5)
        try:
            self.stop()
        except Exception as e:
            logger.warning("Deferred stop for self-update failed: %s", e)

    def _enqueue_pending_self_deploy_result(self) -> None:
        """Map a consumed self-update marker to a buffered DeployResult.

        Called from _start_orch_client after _read_self_update_marker so
        self._last_self_update_result is already populated. The buffered
        result is flushed when orch_client connects.

        Skips silently in these cases:
          - no orch_pending_deploy marker → nothing to report
          - orch_client is None → orchestrator integration disabled
          - marker present but self_update_result missing → defensive
            failed report (the wrapper didn't write a marker, so the
            update either never ran or crashed before completing)
        """
        pending = _read_orch_pending_deploy(self.config_dir)
        if pending is None:
            return
        if self._orch_client is None:
            logger.info(
                "orch_pending_deploy marker present but orch_client is "
                "disabled — discarding deploy_id=%s",
                pending.deploy_id,
            )
            return
        last = self._last_self_update_result
        if last is None:
            logger.warning(
                "orch_pending_deploy marker present but self_update_result "
                "missing — sending failed for deploy_id=%s",
                pending.deploy_id,
            )
            self._orch_client.enqueue_deploy_result(
                pending.deploy_id,
                status="failed",
                error="self-update result marker missing after restart",
                orchestrator_attempt_id=pending.orchestrator_attempt_id,
                connection_generation=pending.connection_generation,
            )
            return

        duration_ms: int | None = None
        try:
            t0 = datetime.fromisoformat(pending.started_at)
            t1 = datetime.fromisoformat(last.finished_at)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
            if duration_ms < 0:
                duration_ms = None
        except Exception:
            duration_ms = None

        status = "success" if last.ok else "failed"
        error = None if last.ok else (last.error or "self-update reported failure")
        self._orch_client.enqueue_deploy_result(
            pending.deploy_id,
            status=status,
            error=error,
            duration_ms=duration_ms,
            orchestrator_attempt_id=pending.orchestrator_attempt_id,
            connection_generation=pending.connection_generation,
            settled_snapshot=self._capture_orchestrator_repo_snapshot(
                self._self_repo,
                self._repo_states[self._self_repo].config.branch,
                pending.deploy_id,
            )
            if self._self_repo
            else None,
        )
        logger.info(
            "Enqueued DeployResult for self-update: deploy_id=%s status=%s",
            pending.deploy_id,
            status,
        )

    def _handle_service_command(
        self,
        service_name: str,
        action: str,
        payload: dict | None = None,
    ) -> dict | str | None:
        """Handle remote service command from orchestrator.

        Raises ValueError for unknown service or action (caught by OrchestratorClient).
        """
        payload = payload or {}
        if action == "reload-config":
            self.reload_config()
            return {"ok": True, "action": action}
        if action == "register-service":
            from .service_lifecycle import register_service

            name = payload.get("name") or service_name
            service_config = payload.get("service_config", payload.get("config"))
            if not name:
                raise ValueError("Service name is required")
            if service_config is None:
                raise ValueError("service_config is required")
            return register_service(
                self,
                name=name,
                service_config=service_config,
                repo=payload.get("repo"),
                repo_config=payload.get("repo_config"),
                start=payload.get("start", True),
            )
        if action == "register-repo":
            from .service_lifecycle import register_repo

            name = payload.get("name") or service_name
            repo_config = payload.get("repo_config", payload.get("config"))
            if not name:
                raise ValueError("Repo name is required")
            if repo_config is None:
                raise ValueError("repo_config is required")
            return register_repo(self, name=name, repo_config=repo_config)

        if not service_name:
            raise ValueError("Service name is required")
        if service_name not in self.config.services:
            raise ValueError(f"Unknown service: {service_name}")

        if action == "restart":
            message = self.restart_service(service_name)
            return {"ok": True, "service": service_name, "message": message}
        elif action == "stop":
            self.stop_service(service_name)
            return {"ok": True, "service": service_name, "action": action}
        elif action == "start":
            if not self._start_service(service_name):
                raise RuntimeError(f"Failed to start service: {service_name}")
            return {"ok": True, "service": service_name, "action": action}
        elif action == "reload":
            from .service_lifecycle import reload_service_definition

            return reload_service_definition(self, service_name)
        else:
            raise ValueError(f"Unknown action: {action}")

    def trigger_pull(
        self,
        repo_name: str,
        auto: bool = False,
        *,
        orchestrator_attempt_id: str | None = None,
        node_id: str | None = None,
        branch: str | None = None,
        target_head: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Pull changes for a repository and restart affected services.

        This is the single code path used by all three triggers:
        - Dashboard "Pull" button (via api.py run_in_executor)
        - Slack "배포 승인" button (via approve_callback in Phase 2)
        - auto_apply=True (via _apply_changes)

        Thread safety: uses one per-repo lock. Non-orchestrated duplicates return;
        orchestrated attempts wait only for the bounded startup deployment owner.

        Args:
            repo_name: Repository to pull
            auto: True if triggered automatically (affects Slack message wording)
        """
        if repo_name not in self._pull_locks:
            raise ValueError(f"Unknown repo: {repo_name}")

        pull_lock = self._pull_locks[repo_name]
        if (
            orchestrator_attempt_id is not None
            and repo_name in self._startup_repo_locks
        ):
            if pull_lock.locked():
                logger.info(
                    "Waiting for startup deployment of %s before orchestrated attempt %s",
                    repo_name,
                    orchestrator_attempt_id,
                )
            pull_lock.acquire()
        elif not pull_lock.acquire(blocking=False):
            if orchestrator_attempt_id is not None:
                raise RuntimeError(f"already pulling {repo_name}")
            logger.info("Already pulling %s, ignoring duplicate request", repo_name)
            return

        captured_changes: dict | None = None
        try:
            state = self._repo_states[repo_name]

            # Guard: skip if no pending changes (e.g. pull just completed, re-entry)
            if not state.pending_changes:
                logger.info("No pending changes for %s, skipping", repo_name)
                return

            # _pull_repo() clears state.pending_changes → capture before any ops
            captured_changes = state.pending_changes

            if self._ws_handler is not None:
                self._ws_handler.broadcast_repo_pulling(repo_name, True)

            if self._slack_bot:
                self._slack_bot.notify_pulling(repo_name, auto=auto)

            affected = self.get_affected_services(repo_name)
            self._suppress_pending_restarts(affected)
            try:
                for svc in affected:
                    self._cancel_pending_restart(svc)

                self._ensure_release_manifest_activation(repo_name)
                repo_path = self.config_dir / state.config.path
                previous_head = (
                    get_head(repo_path)
                    if (state.config.release_manifest or target_head is not None)
                    else None
                )
                if state.config.release_manifest:
                    assert previous_head is not None
                    resolved_branch = branch or state.config.branch
                    intended_target = target_head or get_remote_head(
                        repo_path, state.config.branch
                    )
                    manifest_identity = state.config.release_manifest
                    manifest_digest = sha256_file_at_commit(
                        repo_path, intended_target, manifest_identity
                    )
                    journal_attempt_id = self._record_manifest_deployment_intent(
                        repo_name,
                        previous_head,
                        "approved-pull-pending",
                        target_head=intended_target,
                        orchestrator_attempt_id=orchestrator_attempt_id,
                        node_id=node_id,
                        branch=resolved_branch,
                        manifest_identity=manifest_identity,
                        manifest_digest=manifest_digest,
                    )
                else:
                    journal_attempt_id = None
                success, discarded = self._pull_repo(repo_name)
                if not success:
                    raise RuntimeError(f"git pull failed for {repo_name}")
                if target_head is not None:
                    actual_head = get_head(repo_path)
                    if actual_head != target_head:
                        assert previous_head is not None
                        reset_repo_to(repo_path, previous_head)
                        state.last_head = get_head(repo_path)
                        raise ApprovalRevalidationError(
                            "approved target changed during pull: "
                            f"approved={target_head} pulled={actual_head}"
                        )

                if state.config.release_manifest:
                    assert previous_head is not None
                    progress_kwargs = (
                        {"progress_callback": progress_callback}
                        if progress_callback is not None
                        else {}
                    )
                    run_manifest_deployment(
                        self,
                        repo_name,
                        affected,
                        previous_head,
                        orchestrator_attempt_id=orchestrator_attempt_id,
                        node_id=node_id,
                        branch=resolved_branch,
                        journal_attempt_id=journal_attempt_id,
                        **progress_kwargs,
                    )
                else:
                    self._restart_after_pull_legacy(repo_name, affected)
            finally:
                self._release_restart_suppression(affected)

            if self._self_repo and repo_name == self._self_repo:
                # Self-update: signal wrapper to restart Haniel with new code.
                # notify_done is skipped — startup notification fires after restart.
                self._prepare_self_update_shutdown()
                self._notify_self_update_approved()
                self._self_update_requested.set()
                self._clear_self_update_pending_state()
                self.stop()
                return

            if self._slack_bot:
                self._slack_bot.notify_done(
                    repo_name,
                    success=True,
                    pending_changes=captured_changes,
                    discarded_changes=discarded,
                )

        except Exception as e:
            if self._slack_bot:
                self._slack_bot.notify_done(
                    repo_name,
                    success=False,
                    pending_changes=captured_changes,
                    error=str(e),
                )
            raise
        finally:
            # Broadcast before release to preserve state propagation order
            if self._ws_handler is not None:
                self._ws_handler.broadcast_repo_pulling(repo_name, False)
            # Clear pending hash so new changes after this pull trigger fresh notifications
            self._last_pending_hash.pop(repo_name, None)
            pull_lock.release()

    def _restart_after_pull_legacy(self, repo_name: str, affected: list[str]) -> None:
        """Preserve the pre-manifest restart contract for unrelated repositories."""

        failed_hooks = [
            service
            for service in affected
            if not self.execute_hook(service, "post_pull")
        ]
        if failed_hooks:
            failed = ", ".join(sorted(failed_hooks))
            logger.error(
                "Skipping restart after pull for repo %s because post_pull "
                "failed for: %s. Existing service processes were left running.",
                repo_name,
                failed,
            )
            raise RuntimeError(f"post_pull hook failed for: {failed}")

        shutdown_order = [s for s in self.get_shutdown_order() if s in affected]
        for service in shutdown_order:
            self._cancel_pending_restart(service)
            if self.process_manager.is_running(service):
                logger.info("Stopping %s for post-pull restart", service)
                if not self.process_manager.stop_service(service):
                    raise RuntimeError(f"failed to stop {service} after pull")
            self._cancel_pending_restart(service)

        failed_starts: list[str] = []
        skipped_starts: dict[str, list[str]] = {}
        startup_order = [s for s in self.get_startup_order() if s in affected]
        for service in startup_order:
            self._cancel_pending_restart(service)
            blockers = self._blocked_start_dependencies(service)
            if blockers:
                logger.error(
                    "Skipping restart of %s after pull because dependencies "
                    "are not running: %s",
                    service,
                    ", ".join(blockers),
                )
                skipped_starts[service] = blockers
                self._schedule_restart_after_dependency_block(service, blockers)
                continue

            if self.process_manager.is_running(service):
                logger.warning(
                    "%s is already running before post-pull restart; "
                    "stopping it so the new artifacts are started",
                    service,
                )
                if not self.process_manager.stop_service(service):
                    raise RuntimeError(
                        f"failed to stop already-running {service} after pull"
                    )
                self._cancel_pending_restart(service)

            logger.info("Restarting %s after pull", service)
            if not self._start_service(service):
                failed_starts.append(service)

        if failed_starts or skipped_starts:
            details = []
            if failed_starts:
                details.append(f"failed: {', '.join(sorted(failed_starts))}")
            if skipped_starts:
                skipped = ", ".join(
                    f"{service} blocked by {', '.join(blockers)}"
                    for service, blockers in sorted(skipped_starts.items())
                )
                details.append(f"skipped: {skipped}")
            raise RuntimeError(
                "failed to restart services after pull for "
                f"{repo_name}: {'; '.join(details)}"
            )

    @staticmethod
    def _hash_pending(pending: dict) -> str:
        """Return a stable SHA-256 hex digest of a pending_changes dict."""
        return hashlib.sha256(json.dumps(pending, sort_keys=True).encode()).hexdigest()

    def _init_repo_states(self) -> None:
        """Initialize repo states with current HEAD."""
        for name, state in self._repo_states.items():
            repo_path = self.config_dir / state.config.path
            if repo_path.exists():
                try:
                    state.last_head = get_head(repo_path)
                    logger.info(f"Repo {name} at HEAD: {state.last_head[:8]}")
                except GitError as e:
                    logger.warning(f"Failed to get HEAD for {name}: {e}")

    def _clear_self_update_pending_state(self) -> None:
        """Clear user-visible self-update pending state.

        After approval the wrapper owns the update. The old process can still
        live long enough to run another poll cycle, so pending UI/Slack state
        must be cleared before the process actually exits.
        """
        with self._state_lock:
            self._state.self_update_pending = False

        if self._self_repo is None:
            return

        state = self._repo_states.get(self._self_repo)
        if state is not None:
            state.pending_changes = None
        self._last_pending_hash.pop(self._self_repo, None)

    def _apply_startup_updates(self) -> None:
        """Fetch and pull all repos that have pending remote changes.

        Called once during start(), before start_services().
        Self-update repo is excluded (handled by haniel-runner.ps1).
        Repo-level auto_apply is intentionally ignored here: when Haniel
        restarts, all services are already down, so pulling every managed repo is
        the least surprising recovery/deploy behavior.
        Individual repo failures are logged but do not block other repos.
        """
        logger.info("Checking for pending updates on startup...")
        updated: list[str] = []
        failed: list[str] = []
        self._startup_updated_repos.clear()
        self._startup_manifest_updates.clear()

        for name, state in self._repo_states.items():
            # Skip self-update repo (haniel-runner.ps1 handles it)
            if name == self._self_repo:
                continue

            repo_path = self.config_dir / state.config.path
            if not repo_path.exists():
                logger.warning(
                    "Repo %s path does not exist: %s, skipping", name, repo_path
                )
                continue

            pull_lock = self._pull_locks[name]
            self._startup_repo_locks.add(name)
            pull_lock.acquire()
            retain_for_manifest_handover = False
            try:
                has_updates = fetch_repo(
                    path=repo_path,
                    branch=state.config.branch,
                )
                state.last_fetch = datetime.now()
                state.fetch_error = None

                activated = self._ensure_release_manifest_activation(name)
                if has_updates:
                    logger.info("Startup update: pulling %s", name)
                    previous_head = (
                        get_head(repo_path) if state.config.release_manifest else None
                    )
                    if previous_head is not None:
                        self._record_manifest_deployment_intent(
                            name, previous_head, "startup-pull-pending"
                        )
                    pull_repo(
                        path=repo_path,
                        branch=state.config.branch,
                        strategy=state.config.pull_strategy or "merge",
                    )
                    state.last_head = get_head(repo_path)
                    state.pending_changes = None
                    updated.append(name)
                    if state.config.release_manifest:
                        assert previous_head is not None
                        self._startup_manifest_updates[name] = previous_head
                    else:
                        self._startup_updated_repos.add(name)
                else:
                    logger.debug("Startup update: %s is up to date", name)
                    self._queue_interrupted_manifest_deployment(name, activated)

                if name in self._startup_manifest_updates:
                    retain_for_manifest_handover = True

            except (GitError, ReleaseManifestActivationRequired) as e:
                logger.error("Startup update failed for %s: %s", name, e)
                state.fetch_error = str(e)
                failed.append(name)
            finally:
                if not retain_for_manifest_handover:
                    pull_lock.release()
                    self._startup_repo_locks.discard(name)

        if updated:
            logger.info(
                "Startup update complete: %d repos updated (%s)",
                len(updated),
                ", ".join(updated),
            )
        else:
            logger.info("Startup update complete: all repos up to date")

        if failed:
            logger.warning(
                "Startup update: %d repos failed (%s)",
                len(failed),
                ", ".join(failed),
            )

    def _ensure_release_manifest_activation(self, repo_name: str) -> bool:
        """Activate a conventional remote manifest before any new code is pulled."""
        state = self._repo_states[repo_name]
        if state.config.release_manifest:
            return False
        repo_path = self.config_dir / state.config.path
        discovered = discover_remote_release_manifest(repo_path, state.config.branch)
        if discovered is None:
            return False
        if self.config_path is None:
            raise ReleaseManifestActivationRequired(
                f"release manifest discovered for {repo_name}, but config_path is unavailable"
            )
        plan = plan_release_manifest_activation(self.config_path, repo_name, discovered)
        result = activate_release_manifest(
            self.config_path,
            repo_name,
            discovered,
            expected_sha256=plan.config_sha256,
        )
        state.config.release_manifest = discovered
        logger.warning(
            "Activated release manifest for %s before pull (changed=%s, backup=%s)",
            repo_name,
            result.changed,
            result.backup_path,
        )
        return result.changed

    def _deployment_state_store(self) -> DeploymentStateStore:
        return DeploymentStateStore(self.config_dir / ".haniel" / "deployments")

    def _recover_interrupted_deployment_journals(self) -> None:
        """Close attempts left nonterminal by a previous runner process."""
        store = self._deployment_state_store()
        reason = "runner restarted before deployment completed"
        for repo_name, state in self._repo_states.items():
            if not state.config.release_manifest:
                continue
            journal = store.read(repo_name)
            if journal is None:
                continue
            prior_state = journal.get("state")
            if store.mark_interrupted(repo_name, reason=reason):
                logger.warning(
                    "Closed interrupted deployment journal for %s from %s",
                    repo_name,
                    prior_state,
                )

    def _record_manifest_deployment_intent(
        self,
        repo_name: str,
        previous_head: str,
        release_id: str,
        *,
        target_head: str | None = None,
        orchestrator_attempt_id: str | None = None,
        node_id: str | None = None,
        branch: str | None = None,
        manifest_identity: str | None = None,
        manifest_digest: str | None = None,
    ) -> str:
        """Persist the rollback head before a pull can make startup resumable."""
        return self._deployment_state_store().begin(
            repo_name,
            previous_head,
            target_head or previous_head,
            release_id,
            orchestrator_attempt_id=orchestrator_attempt_id,
            node_id=node_id,
            branch=branch,
            manifest_identity=manifest_identity,
            manifest_digest=manifest_digest,
        )

    def _queue_interrupted_manifest_deployment(
        self, repo_name: str, activated: bool
    ) -> None:
        state = self._repo_states[repo_name]
        if not state.config.release_manifest:
            return
        repo_path = self.config_dir / state.config.path
        current_head = get_head(repo_path)
        journal = self._deployment_state_store().read(repo_name)
        if activated:
            self._record_manifest_deployment_intent(
                repo_name, current_head, "startup-activation-pending"
            )
            self._startup_manifest_updates[repo_name] = current_head
            return
        if journal is None or journal.get("state") == "success":
            return
        previous_head = journal.get("previous_head")
        release_id = journal.get("release_id")
        activation_pending = release_id == "startup-activation-pending"
        if isinstance(previous_head, str) and (
            activation_pending or current_head != previous_head
        ):
            logger.warning(
                "Resuming interrupted manifest deployment for %s from %s",
                repo_name,
                previous_head[:8],
            )
            self._startup_manifest_updates[repo_name] = previous_head

    def _poll_loop(self) -> None:
        """Main poll loop."""
        while not self._stop_event.is_set():
            try:
                self._poll_cycle()
            except Exception as e:
                logger.exception(f"Error in poll cycle: {e}")

            # Wait for next poll interval (interruptible)
            self._stop_event.wait(timeout=self.poll_interval)

    def _poll_cycle(self) -> None:
        """Execute one poll cycle.

        Phase 1: Check for changes (git fetch)
        Phase 2: Apply changes (shutdown → pull → hooks → restart)
        Phase 3: Health check (process pending restarts)
        """
        with self._state_lock:
            self._state.last_poll = datetime.now()
            self._state.poll_count += 1

        # Phase 1: Change detection
        changed_repos = self._detect_changes()

        # Phase 2: Apply changes
        if changed_repos:
            self._apply_changes(changed_repos)

        # Phase 3: Process pending restarts
        self._process_pending_restarts()

    def _detect_changes(self) -> list[str]:
        """Detect changes in all repositories.

        Uses a three-way comparison: last_head (haniel's last processed HEAD)
        vs current_head (repo's actual HEAD, which may have been advanced by
        an external process) vs remote_head (origin after fetch).

        This ensures changes are detected even when an external process
        (e.g. Claude Code session) pulls the repo directly.

        Returns:
            List of repo names that have changes
        """
        changed: list[str] = []

        for name, state in self._repo_states.items():
            repo_path = self.config_dir / state.config.path

            if name == self._self_repo and self._self_update_requested.is_set():
                self._clear_self_update_pending_state()
                continue

            if not repo_path.exists():
                logger.warning(f"Repo {name} path does not exist: {repo_path}")
                continue

            try:
                # Fetch from remote (don't rely on return value)
                fetch_repo(
                    path=repo_path,
                    branch=state.config.branch,
                )
                state.last_fetch = datetime.now()
                state.fetch_error = None

                # Read current HEAD (may differ from last_head if externally pulled)
                current_head = get_head(repo_path)

                if current_head != state.last_head:
                    # External pull or other process advanced HEAD
                    previous_head = state.last_head
                    self_checkout_matches_remote = False
                    if name == self._self_repo and previous_head:
                        remote_head = get_remote_head(repo_path, state.config.branch)
                        self_checkout_matches_remote = current_head == remote_head
                    last_short = state.last_head[:8] if state.last_head else "None"
                    logger.info(
                        f"Changes detected in repo: {name} "
                        f"(last_head={last_short} → current={current_head[:8]})"
                    )
                    changed.append(name)
                    state.last_head = current_head
                    if (
                        name == self._self_repo
                        and previous_head
                        and self_checkout_matches_remote
                    ):
                        state.pending_changes = get_applied_change_evidence(
                            repo_path, previous_head, current_head
                        )
                    else:
                        state.pending_changes = get_pending_changes(
                            path=repo_path,
                            branch=state.config.branch,
                        )
                    if self._ws_handler is not None:
                        self._ws_handler.broadcast_repo_change(
                            name, state.pending_changes or {}
                        )
                    # Self-repo: even after an external pull, Haniel needs a restart to
                    # use the new code. Notify Slack so the user can approve the restart.
                    if (
                        name == self._self_repo
                        and self._slack_bot
                        and state.pending_changes
                        and not self._pull_locks[name].locked()
                    ):
                        self._slack_bot.notify_pending(name, state.pending_changes)
                else:
                    # current == last_head, check if remote has new commits
                    remote_head = get_remote_head(repo_path, state.config.branch)
                    if remote_head != current_head:
                        logger.info(
                            f"Remote changes available for repo: {name} "
                            f"(current={current_head[:8]} → remote={remote_head[:8]})"
                        )
                        changed.append(name)
                        state.pending_changes = get_pending_changes(
                            path=repo_path,
                            branch=state.config.branch,
                        )
                        if self._ws_handler is not None:
                            self._ws_handler.broadcast_repo_change(
                                name, state.pending_changes or {}
                            )
                        # Notify Slack only when remote has new commits (not already pulling)
                        # and only when the pending content has actually changed.
                        if (
                            self._slack_bot
                            and state.pending_changes
                            and not self._pull_locks[name].locked()
                        ):
                            content_hash = self._hash_pending(state.pending_changes)
                            if self._last_pending_hash.get(name) != content_hash:
                                self._last_pending_hash[name] = content_hash
                                self._slack_bot.notify_pending(
                                    name, state.pending_changes
                                )
                    else:
                        # A self checkout can already match origin while the running
                        # process still uses the previous code. Preserve the restart
                        # evidence so reconnecting orchestrators can recover the same
                        # deterministic Pending until approval restarts Haniel.
                        if not (
                            name == self._self_repo and self._state.self_update_pending
                        ):
                            state.pending_changes = None

                self._notify_orchestrator_change(name, state)

            except GitError as e:
                logger.error(f"Failed to fetch {name}: {e}")
                state.fetch_error = str(e)

        return changed

    def _notify_orchestrator_change(self, name: str, state: RepoState) -> None:
        if not self._orch_client:
            return
        snapshot = self._capture_orchestrator_repo_snapshot(name, state.config.branch)
        self_restart_required = name == self._self_repo and (
            self._state.self_update_pending
            or (snapshot.in_sync and bool(state.pending_changes))
        )
        if state.pending_changes and (not snapshot.in_sync or self_restart_required):
            commits = state.pending_changes.get("commits", [])
            if commits:
                manifest_identity = state.config.release_manifest
                manifest_digest = None
                if manifest_identity:
                    try:
                        manifest_digest = sha256_file_at_commit(
                            self.config_dir / state.config.path,
                            snapshot.remote_head,
                            manifest_identity,
                        )
                    except GitError as exc:
                        logger.warning(
                            "Cannot snapshot release manifest for %s at %s: %s",
                            name,
                            snapshot.remote_head,
                            exc,
                        )
                self._orch_client.notify_change(
                    repo=name,
                    branch=state.config.branch,
                    commits=commits,
                    affected_services=self.get_affected_services(name),
                    diff_stat=state.pending_changes.get("stat"),
                    deploy_id=snapshot.deploy_id,
                    target_head=snapshot.remote_head,
                    deployment_kind="manifest" if manifest_identity else "legacy",
                    expected_manifest_identity=manifest_identity,
                    expected_manifest_digest=manifest_digest,
                    is_self_update=name == self._self_repo,
                    wait=True,
                )
        if self_restart_required:
            return
        self._orch_client.notify_repo_reconciliation(snapshot, "observed", wait=True)

    def _apply_changes(self, changed_repos: list[str]) -> None:
        """Apply changes from the specified repositories.

        If the self-update repo is among the changed repos, it is handled
        separately via _initiate_self_update() instead of the normal
        pull → restart flow. See ADR-0002 for architecture details.

        Args:
            changed_repos: List of repo names with changes
        """
        # Check for self-update repo
        if self._self_repo and self._self_repo in changed_repos:
            self._initiate_self_update()
            # Remove self-repo from list; remaining repos still get normal treatment
            changed_repos = [r for r in changed_repos if r != self._self_repo]
            if not changed_repos:
                return

        # auto_apply=false: detection only, skip stop→pull→restart
        if not self.config.auto_apply:
            logger.info("auto_apply=false, skipping apply for: %s", changed_repos)
            return

        manual_repos = [
            repo
            for repo in changed_repos
            if self._repo_states.get(repo) is not None
            and not self._repo_states[repo].config.auto_apply
        ]
        changed_repos = [
            repo
            for repo in changed_repos
            if self._repo_states.get(repo) is None
            or self._repo_states[repo].config.auto_apply
        ]
        if manual_repos:
            logger.info(
                "repo auto_apply=false, pending manual approval for: %s",
                manual_repos,
            )
        if not changed_repos:
            return

        for repo in changed_repos:
            try:
                self._run_auto_deploy(repo)
            except Exception as e:
                logger.error("Auto-deploy failed for %s: %s", repo, e)

    def _run_auto_deploy(self, repo: str) -> None:
        """Run auto_apply through the same settled-HEAD reporting contract."""
        if (
            getattr(self, "_orch_client", None) is None
            or self.config.orchestrator_client is None
        ):
            self.trigger_pull(repo, auto=True)
            return
        state = self._repo_states[repo]
        before = self._capture_orchestrator_repo_snapshot(repo, state.config.branch)
        permit = self._orch_client.request_auto_attempt(before)
        progress = self._orch_client.start_deploy_progress(permit)
        started = time.monotonic()
        operation_error: str | None = None
        try:
            approval = {
                "deploy_id": permit["deploy_id"],
                "orchestrator_attempt_id": permit["begun_orchestrator_attempt_id"],
                "execution_mode": permit["execution_mode"],
                "probe_id": permit["probe_id"],
                "connection_generation": permit["connection_generation"],
                "preflight_fingerprint": permit["preflight_fingerprint"],
            }
            probe = self._orchestrated_deploys.consume_approval(approval)
            execute_approved_plan(
                self,
                approval,
                probe,
                self._deploy_retry_planner(repo),
                progress_callback=progress.transition,
            )
        except Exception as exc:
            operation_error = str(exc)
        finally:
            progress.stop()
        settled = self._capture_orchestrator_repo_snapshot(
            repo, state.config.branch, before.deploy_id
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if self._orch_client:
            self._orch_client.report_deploy_attempt(
                settled, operation_error, duration_ms, permit
            )
        if operation_error:
            raise RuntimeError(operation_error)

    def _initiate_self_update(self) -> None:
        """Handle detection of changes in haniel's own repo.

        If auto_update is true, immediately signals the main thread to exit
        for update. Otherwise, sets pending state and sends a webhook notification.
        The actual update is deferred until approve_self_update() is called.

        This method is called from the poll thread, so it cannot raise
        SelfUpdateExit directly (SystemExit in a daemon thread terminates
        only that thread). Instead, it signals the main thread via an event.

        See ADR-0002 for the full self-update architecture.
        """
        if self.config.self_update is None:
            raise RuntimeError("self_update config required for self-update")

        if self.config.self_update.auto_update:
            logger.info("Self-update: auto_update=true, exiting for update")
            self._notify_self_update_detected(auto=True)
            self._prepare_self_update_shutdown()
            self._self_update_requested.set()
            self.stop()
            return

        # Manual approval mode
        with self._state_lock:
            if self._state.self_update_pending:
                logger.debug("Self-update already pending, skipping duplicate")
                return
            self._state.self_update_pending = True

        logger.info("Self-update: changes detected, awaiting approval")
        self._notify_self_update_detected(auto=False)

        if self._ws_handler is not None and self._self_repo:
            self._ws_handler.broadcast_self_update_pending(self._self_repo)

    def approve_self_update(self) -> str:
        """Approve a pending self-update.

        Sets the self_update_requested signal but does NOT call stop().
        The caller (API handler / MCP handler) is responsible for scheduling
        stop() after sending the HTTP response, to avoid the race condition
        where stop() kills the connection before the response reaches the client.

        Returns:
            Status message
        """
        with self._state_lock:
            if not self._state.self_update_pending:
                return "No self-update pending."

        logger.info("Self-update approved, shutting down for update")
        self._prepare_self_update_shutdown()
        slack_bot = getattr(self, "_slack_bot", None)
        if slack_bot is not None and self._self_repo:
            slack_bot.notify_pulling(self._self_repo, auto=False)
        self._notify_self_update_approved()
        self._self_update_requested.set()
        self._clear_self_update_pending_state()
        # Notify dashboard that the update work is now starting (server about
        # to shut down). This is the canonical signal for the dashboard's
        # 'Updating…' overlay — the API response alone is insufficient.
        # ws_handler가 None이면(대시보드 비활성) broadcast 스킵 — 의도된 동작.
        if self._ws_handler is not None and self._self_repo:
            self._ws_handler.broadcast_self_update_started(self._self_repo)
        return "Self-update approved. Shutting down for update."

    @property
    def self_update_requested(self) -> bool:
        """Check if self-update exit has been requested."""
        return self._self_update_requested.is_set()

    def request_restart(self) -> str:
        """Request a clean restart without update.

        Signals the main thread to exit with code 11, which tells the
        wrapper script to restart without performing a git pull.

        Returns:
            Status message
        """
        logger.info("Restart requested, shutting down for restart")
        self._restart_requested.set()
        self.stop()
        return "Restart initiated. Shutting down..."

    # ── AppHomeController interface methods ──────────────────────────────────

    def restart_service(self, name: str) -> str:
        """Restart a managed service (or request haniel restart).

        Satisfies AppHomeController protocol (duck-typed, no import needed).
        """
        if name == "haniel":
            return self.request_restart()
        self.process_manager.stop_service(name)
        self._start_service(name)
        return f"Service '{name}' restarted"

    def start_service(self, name: str) -> None:
        """Start a managed service.

        Satisfies AppHomeController protocol (duck-typed, no import needed).
        """
        self._start_service(name)

    def stop_service(self, name: str) -> None:
        """Stop a managed service.

        Satisfies AppHomeController protocol (duck-typed, no import needed).
        """
        self.process_manager.stop_service(name)

    def enable_service(self, name: str) -> str:
        """Reset circuit breaker for a service.

        The poll loop will restart the service on its next cycle.
        Satisfies AppHomeController protocol (duck-typed, no import needed).
        """
        self.health_manager.reset_circuit(name)
        return f"Circuit reset for '{name}'. Poll loop will restart the service."

    @property
    def restart_requested(self) -> bool:
        """Check if restart exit has been requested."""
        return self._restart_requested.is_set()

    def _notify_self_update(
        self,
        event_type_name: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        """Send a self-update webhook notification.

        Args:
            event_type_name: EventType enum value name (e.g., "SELF_UPDATE_DETECTED")
            message: Human-readable message
            details: Optional details dict
        """
        try:
            from ..integrations.webhook import (
                EventType,
                WebhookMessage,
                WebhookNotifier,
            )

            if not self.config.webhooks:
                return

            event_type = EventType[event_type_name]
            msg = WebhookMessage(
                event_type=event_type,
                service_name="haniel",
                message=message,
                details=details or {},
            )
            notifier = WebhookNotifier(self.config.webhooks)
            notifier.notify_sync(msg)
        except Exception as e:
            logger.warning(f"Failed to send self-update notification: {e}")

    def _notify_self_update_detected(self, *, auto: bool) -> None:
        """Send webhook notification for self-update detection."""
        mode = "auto-updating" if auto else "awaiting approval"
        self._notify_self_update(
            "SELF_UPDATE_DETECTED",
            f"Changes detected in haniel's own repository. {mode.capitalize()}.",
            {"repo": self._self_repo, "mode": mode},
        )

    def _notify_self_update_approved(self) -> None:
        """Send webhook notification for self-update approval."""
        self._notify_self_update(
            "SELF_UPDATE_APPROVED",
            "Self-update approved. Shutting down for update.",
        )

    def _pull_repo(self, repo_name: str) -> tuple[bool, list[str]]:
        """Pull changes for a repository.

        Args:
            repo_name: Name of the repository

        Returns:
            Tuple of (success, discarded_files). discarded_files is non-empty only
            when pull_strategy is 'force' and local changes were discarded.
        """
        if repo_name not in self._repo_states:
            return False, []

        state = self._repo_states[repo_name]
        self._ensure_release_manifest_activation(repo_name)
        repo_path = self.config_dir / state.config.path
        strategy = state.config.pull_strategy or "merge"

        try:
            discarded = pull_repo(
                path=repo_path,
                branch=state.config.branch,
                strategy=strategy,
            )
            state.last_head = get_head(repo_path)
            state.pending_changes = None
            head_short = state.last_head[:8] if state.last_head else "unknown"
            logger.info(f"Pulled {repo_name}, new HEAD: {head_short}")
            return True, discarded
        except GitError as e:
            logger.error(f"Failed to pull {repo_name}: {e}")
            return False, []

    def _process_pending_restarts(self) -> None:
        """Process any pending service restarts."""
        now = time.time()

        with self._restart_lock:
            ready = [
                name
                for name, restart_time in self._pending_restarts.items()
                if restart_time <= now and name not in self._restart_suppressed
            ]

            for name in ready:
                del self._pending_restarts[name]

        for name in ready:
            if not self.process_manager.is_running(name):
                blockers = self._blocked_start_dependencies(name)
                if blockers:
                    self._schedule_restart_after_dependency_block(name, blockers)
                    continue
                logger.info(f"Executing scheduled restart for {name}")
                if not self._start_service(name):
                    logger.error("Scheduled restart for %s failed", name)

    def get_status(self) -> dict:
        """Get current status of the runner.

        Returns:
            Status dict with runner state, services, repos, and dependency graph
        """
        # Service states from health manager — includes config for dashboard
        service_status = {}
        for name, svc_config in self._enabled_services.items():
            health = self.health_manager.get_health(name)
            service_status[name] = {
                "state": health.state.value,
                "uptime": health.get_uptime(),
                "restart_count": health.restart_count,
                "consecutive_failures": health.consecutive_failures,
                # Config info for dashboard
                "config": {
                    "run": svc_config.run,
                    "cwd": svc_config.cwd,
                    "repo": svc_config.repo,
                    "after": svc_config.after,
                    "ready": svc_config.ready,
                    "enabled": svc_config.enabled,
                },
            }

        # Pending restarts — snapshot under lock
        with self._restart_lock:
            pending_restarts = list(self._pending_restarts.keys())

        # Dependency graph
        dependency_graph = {}
        for name in self._enabled_services:
            dependency_graph[name] = {
                "dependencies": sorted(self._dependency_graph.get_dependencies(name)),
                "dependents": sorted(self._dependency_graph.get_dependents(name)),
            }

        # Repo states
        repo_status = {}
        for name, state in self._repo_states.items():
            head_short = state.last_head[:8] if state.last_head else None
            repo_status[name] = {
                "path": str(state.config.path),
                "branch": state.config.branch,
                "last_head": head_short,
                "last_fetch": state.last_fetch.isoformat()
                if state.last_fetch
                else None,
                "fetch_error": state.fetch_error,
                "pending_changes": state.pending_changes,
                "pulling": self._pull_locks[name].locked(),
            }

        # Read runner state with lock for thread safety
        with self._state_lock:
            result = {
                "running": self._state.running,
                "start_time": self._state.start_time.isoformat()
                if self._state.start_time
                else None,
                "last_poll": self._state.last_poll.isoformat()
                if self._state.last_poll
                else None,
                "poll_count": self._state.poll_count,
                "poll_interval": self.poll_interval,
                "services": service_status,
                "pending_restarts": pending_restarts,
                "dependency_graph": dependency_graph,
                "repos": repo_status,
            }
            if self._self_repo:
                last_result = (
                    self._last_self_update_result.to_dict()
                    if self._last_self_update_result is not None
                    else None
                )
                result["self_update"] = {
                    "repo": self._self_repo,
                    "pending": self._state.self_update_pending,
                    "auto_update": self.config.self_update.auto_update
                    if self.config.self_update
                    else False,
                    "last_result": last_result,
                }
            return result
