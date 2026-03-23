"""
haniel runner module.

Implements the poll → pull → restart cycle:
- Phase 1: Change detection (git fetch)
- Phase 2: Change application (shutdown → pull → hooks → restart)
- Phase 3: Health check (process survival)

haniel doesn't care what it runs. It polls, pulls, and restarts as configured.
"""

import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import (
    BackoffConfig,
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    ShutdownConfig,
)
from ..integrations.slack_bot import SlackBot
from .git import (
    GitError,
    fetch_repo,
    get_head,
    get_pending_changes,
    get_remote_head,
    pull_repo,
)
from .health import HealthManager
from .process import ProcessManager


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
    is_pulling: bool = False  # True while trigger_pull() is running


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
        self._restart_lock = threading.Lock()

        # MCP server (lazy initialized)
        self._mcp_server = None

        # WebSocket handler (set by MCP server after dashboard setup)
        self._ws_handler = None

        # Slack bot (initialized in start() if configured)
        self._slack_bot: SlackBot | None = None

        # Track whether post_pull hooks have been run on first start
        self._post_pull_executed = False

        # Self-update (see ADR-0002)
        self._self_repo: str | None = (
            config.self_update.repo if config.self_update else None
        )
        self._self_update_requested = threading.Event()
        self._restart_requested = threading.Event()

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
            # On Windows, use shell=True to support .cmd/.bat executables (pnpm,
            # npx, etc.) and shell operators (&&, ||).  Pass the command as a
            # string so CreateProcess / cmd.exe handle it natively.
            if os.name == "nt":
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
        """Start all enabled services in dependency order.

        On first start, executes post_pull hooks for all services before
        starting them. This ensures build steps (npm build, pip install -e, etc.)
        run after initial clone, just as they would after a git pull update.
        """
        startup_order = self.get_startup_order()
        logger.info(f"Starting services in order: {startup_order}")

        # Run post_pull hooks on first start (initial install has same semantics as pull)
        if not self._post_pull_executed:
            self._post_pull_executed = True
            for name in startup_order:
                self.execute_hook(name, "post_pull")

        for name in startup_order:
            self._start_service(name)

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
            return False

    def _on_service_ready(self, name: str) -> None:
        """Called when a service becomes ready."""
        logger.info(f"Service {name} is ready")

    def _on_service_crash(self, name: str, exit_code: int | None) -> None:
        """Called when a service crashes.

        Args:
            name: Service name
            exit_code: Exit code (None if signal)
        """
        logger.warning(f"Service {name} crashed with exit code {exit_code}")

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

    def stop_services(self) -> None:
        """Stop all services in reverse dependency order."""
        shutdown_order = self.get_shutdown_order()
        logger.info(f"Stopping services in order: {shutdown_order}")

        for name in shutdown_order:
            if self.process_manager.is_running(name):
                logger.info(f"Stopping service: {name}")
                self.process_manager.stop_service(name)

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

        # Start MCP server if enabled
        self._start_mcp_server()

        # Start Slack bot if configured
        self._start_slack_bot()

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
            self._slack_bot = SlackBot(
                config=self.config.slack,
                approve_callback=self.trigger_pull,
            )
            self._slack_bot.start()
        except Exception as e:
            logger.error("Failed to start Slack bot: %s", e)
            self._slack_bot = None

    def trigger_pull(self, repo_name: str, auto: bool = False) -> None:
        """Pull changes for a repository and restart affected services.

        This is the single code path used by all three triggers:
        - Dashboard "Pull" button (via api.py run_in_executor)
        - Slack "배포 승인" button (via approve_callback in Phase 2)
        - auto_apply=True (via _apply_changes)

        Args:
            repo_name: Repository to pull
            auto: True if triggered automatically (affects Slack message wording)
        """
        if repo_name not in self._repo_states:
            raise ValueError(f"Unknown repo: {repo_name}")

        state = self._repo_states[repo_name]
        if state.is_pulling:
            logger.info("Already pulling %s, ignoring duplicate request", repo_name)
            return
        state.is_pulling = True

        if self._ws_handler is not None:
            self._ws_handler.broadcast_repo_pulling(repo_name, True)

        if self._slack_bot:
            self._slack_bot.notify_pulling(repo_name, auto=auto)

        try:
            affected = self.get_affected_services(repo_name)
            shutdown_order = [s for s in self.get_shutdown_order() if s in affected]
            for svc in shutdown_order:
                if self.process_manager.is_running(svc):
                    logger.info("Stopping %s for pull", svc)
                    self.process_manager.stop_service(svc)

            success = self._pull_repo(repo_name)
            if not success:
                raise RuntimeError(f"git pull failed for {repo_name}")

            for svc in affected:
                self.execute_hook(svc, "post_pull")

            startup_order = [s for s in self.get_startup_order() if s in affected]
            for svc in startup_order:
                logger.info("Restarting %s after pull", svc)
                self._start_service(svc)

            if self._slack_bot:
                self._slack_bot.notify_done(repo_name, success=True)

        except Exception as e:
            if self._slack_bot:
                self._slack_bot.notify_done(repo_name, success=False, error=str(e))
            raise
        finally:
            state.is_pulling = False
            if self._ws_handler is not None:
                self._ws_handler.broadcast_repo_pulling(repo_name, False)

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
                    last_short = state.last_head[:8] if state.last_head else "None"
                    logger.info(
                        f"Changes detected in repo: {name} "
                        f"(last_head={last_short} → current={current_head[:8]})"
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
                        if self._slack_bot and state.pending_changes and not state.is_pulling:
                            self._slack_bot.notify_pending(name, state.pending_changes)
                    else:
                        state.pending_changes = None

            except GitError as e:
                logger.error(f"Failed to fetch {name}: {e}")
                state.fetch_error = str(e)

        return changed

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

        # Collect all affected services
        all_affected: set[str] = set()
        for repo in changed_repos:
            all_affected.update(self.get_affected_services(repo))

        if not all_affected:
            # No services affected, just pull
            for repo in changed_repos:
                self._pull_repo(repo)
            return

        logger.info(f"Services affected by changes: {all_affected}")

        # Trigger pull for each changed repo via the unified method
        for repo in changed_repos:
            try:
                self.trigger_pull(repo, auto=True)
            except Exception as e:
                logger.error("Auto-deploy failed for %s: %s", repo, e)

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
        self._notify_self_update_approved()
        self._self_update_requested.set()
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

    def _pull_repo(self, repo_name: str) -> bool:
        """Pull changes for a repository.

        Args:
            repo_name: Name of the repository

        Returns:
            True if pull succeeded
        """
        if repo_name not in self._repo_states:
            return False

        state = self._repo_states[repo_name]
        repo_path = self.config_dir / state.config.path

        try:
            pull_repo(
                path=repo_path,
                branch=state.config.branch,
            )
            state.last_head = get_head(repo_path)
            state.pending_changes = None
            head_short = state.last_head[:8] if state.last_head else "unknown"
            logger.info(f"Pulled {repo_name}, new HEAD: {head_short}")
            return True
        except GitError as e:
            logger.error(f"Failed to pull {repo_name}: {e}")
            return False

    def _process_pending_restarts(self) -> None:
        """Process any pending service restarts."""
        now = time.time()

        with self._restart_lock:
            ready = [
                name
                for name, restart_time in self._pending_restarts.items()
                if restart_time <= now
            ]

            for name in ready:
                del self._pending_restarts[name]

        for name in ready:
            if not self.process_manager.is_running(name):
                logger.info(f"Executing scheduled restart for {name}")
                self._start_service(name)

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
                result["self_update"] = {
                    "repo": self._self_repo,
                    "pending": self._state.self_update_pending,
                    "auto_update": self.config.self_update.auto_update
                    if self.config.self_update
                    else False,
                }
            return result
