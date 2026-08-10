"""
haniel runner module.

Implements the poll → pull → restart cycle:
- Phase 1: Change detection (git fetch)
- Phase 2: Change application (shutdown → pull → hooks → restart)
- Phase 3: Health check (process survival)

haniel doesn't care what it runs. It polls, pulls, and restarts as configured.

This legacy orchestration module remains above 500 lines while its runtime
surfaces are extracted incrementally. New release lifecycle logic belongs in
one_shot_handover.py; this file only wires existing callers to that boundary.
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
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from uuid import uuid4

from ..config import (
    BackoffConfig,
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    ShutdownConfig,
)
from ..config.readiness import ready_port
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
    activate_repo_target,
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
from .deployment_errors import (
    StableDeploymentError,
    stable_deployment_error_code,
)
from .runner_config_snapshot import (
    RepoObservation,
    RepoRuntimeSnapshot,
    RunnerConfigSnapshot,
)
from .repo_reconciliation import (
    RepoReconciliationSnapshot,
    capture_repo_snapshot,
)
from .runner_deployment import run_manifest_deployment
from .one_shot_handover import probe_manifest_target
from .release_staging import ReleaseStagingError
from .handover_config import (
    HandoverConfigError,
    require_handover_config_digest,
)
from .safety_redaction import bounded_redact_text, redact_text, sensitive_values
from .lifecycle_control import DeploymentLease, LifecycleConflict, LifecycleControl
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
from ..integrations.deploy_attempt_gate import DeployPermissionError

if TYPE_CHECKING:
    from ..integrations.orchestrator_client import OrchestratorClient
    from ..integrations.slack_bot import SlackBot
    from .handover_config import HandoverReloadPlan
    from .service_environment import ServiceEnvironmentFile

logger = logging.getLogger(__name__)

_OPERATIONAL_DEPLOYMENT_ERRORS = (
    ApprovalRevalidationError,
    DeploymentError,
    DeployPermissionError,
    GitError,
    HandoverConfigError,
    LifecycleConflict,
    ReleaseManifestActivationRequired,
    StableDeploymentError,
)


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
        self.lifecycle_control = LifecycleControl(
            config_path or (config_dir / "haniel.yaml")
        )
        self.lifecycle_instance_id: str | None = None

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
        self._config_reload_lock = threading.RLock()
        self._config_generation = 0
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
        self._startup_manifest_request_ids: dict[str, str] = {}
        self._startup_manifest_operations: dict[str, str] = {}
        self._startup_manifest_reload_plans: dict[str, "HandoverReloadPlan"] = {}
        self._startup_manifest_config_snapshots: dict[str, RunnerConfigSnapshot] = {}
        self._startup_deployment_leases: dict[str, DeploymentLease] = {}
        self._startup_repo_locks: set[str] = set()
        self._startup_pull_locks: dict[str, threading.Lock] = {}

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
        from ..config import load_config, require_valid_config
        from .service_lifecycle import config_file_transaction

        if not self.config_path:
            raise RuntimeError("config_path is not set — cannot reload configuration")

        with config_file_transaction(self.config_path):
            snapshot = self._snapshot_config_state()
            new_config = load_config(self.config_path)
            require_valid_config(new_config)
            self._replace_config_snapshot(new_config, snapshot.generation)

        logger.info("Configuration reloaded from %s", self.config_path)

    def prepare_handover_config(self, repo_name: str, expected_digest: str):
        """Reload exactly the config snapshot bound to a handover request."""

        from .handover_config import prepare_runner_handover_config

        return prepare_runner_handover_config(self, repo_name, expected_digest)

    def _prepare_current_manifest_config(
        self, repo_name: str
    ) -> "HandoverReloadPlan | None":
        """Bind resident manifest callers to the current config byte snapshot."""

        if self.config_path is None:
            return None
        from .handover_config import handover_config_digest

        digest = handover_config_digest(self.config_path)
        return self.prepare_handover_config(repo_name, digest)

    def _snapshot_config_state(self) -> RunnerConfigSnapshot:
        """Copy config-derived memory while holding the resident lock briefly."""

        with self._config_reload_lock:
            return RunnerConfigSnapshot(
                generation=self._config_generation,
                config=self.config.model_copy(deep=True),
                enabled_services=deepcopy(self._enabled_services),
                repo_identity={
                    name: state.config.model_copy(deep=True)
                    for name, state in self._repo_states.items()
                },
                self_repo=self._self_repo,
                startup_order=tuple(self._dependency_graph.topological_sort()),
                shutdown_order=tuple(
                    self._dependency_graph.topological_sort(reverse=True)
                ),
            )

    def _snapshot_repo_runtime(self, name: str) -> RepoRuntimeSnapshot:
        """Copy one repo state and its stable pull-lock reference."""

        with self._config_reload_lock:
            state = self._repo_states.get(name)
            if state is None:
                raise ValueError(f"Unknown repo: {name}")
            directly_affected = {
                service
                for service, config in self._enabled_services.items()
                if config.repo == name
            }
            affected = set(directly_affected)
            for service in directly_affected:
                affected.update(self._dependency_graph.get_all_dependents(service))
            return RepoRuntimeSnapshot(
                generation=self._config_generation,
                repo_name=name,
                config=state.config.model_copy(deep=True),
                last_head=state.last_head,
                last_fetch=state.last_fetch,
                fetch_error=state.fetch_error,
                pending_changes=deepcopy(state.pending_changes),
                pull_lock=self._pull_locks[name],
                affected_services=tuple(affected),
            )

    def _snapshot_repo_and_config(
        self, name: str
    ) -> tuple[RunnerConfigSnapshot, RepoRuntimeSnapshot]:
        """Capture config identity and one repo from the same generation."""

        with self._config_reload_lock:
            return self._snapshot_config_state(), self._snapshot_repo_runtime(name)

    def _commit_repo_observation(self, observation: RepoObservation) -> bool:
        """CAS one completed external observation into resident memory."""

        with self._config_reload_lock:
            state = self._repo_states.get(observation.repo_name)
            if (
                observation.generation != self._config_generation
                or state is None
                or state.config != observation.repo_config
            ):
                return False
            state.last_fetch = observation.last_fetch
            state.fetch_error = observation.fetch_error
            state.last_head = observation.last_head
            state.pending_changes = deepcopy(observation.pending_changes)
            return True

    def _replace_config_snapshot(
        self,
        candidate: HanielConfig,
        expected_generation: int,
    ) -> int:
        """Prepare outside the lock, then atomically replace one generation."""

        enabled_services = {
            name: svc for name, svc in candidate.services.items() if svc.enabled
        }
        dependency_graph = DependencyGraph(enabled_services)
        resident_identity = self._snapshot_config_state().repo_identity
        prepared_identity_states: dict[str, RepoState] = {}
        prepared_new_locks: dict[str, threading.Lock] = {}
        for name, repo_cfg in candidate.repos.items():
            old_config = resident_identity.get(name)
            checkout_identity_unchanged = old_config is not None and (
                old_config.url,
                old_config.path,
                old_config.branch,
            ) == (repo_cfg.url, repo_cfg.path, repo_cfg.branch)
            if checkout_identity_unchanged:
                continue
            state = RepoState(name=name, config=repo_cfg.model_copy(deep=True))
            repo_path = self.config_dir / repo_cfg.path
            if repo_path.exists():
                try:
                    state.last_head = get_head(repo_path)
                except GitError as error:
                    state.fetch_error = bounded_redact_text(str(error))
            prepared_identity_states[name] = state
            if old_config is None:
                prepared_new_locks[name] = threading.Lock()
        self_update_repo = candidate.self_update.repo if candidate.self_update else None
        with self._config_reload_lock:
            if self._config_generation != expected_generation:
                raise StableDeploymentError(
                    "CONFIG_GENERATION_CHANGED",
                    "resident config changed while preparing a replacement",
                )
            repo_states: dict[str, RepoState] = {}
            pull_locks: dict[str, threading.Lock] = {}
            for name, repo_cfg in candidate.repos.items():
                current = self._repo_states.get(name)
                prepared = prepared_identity_states.get(name)
                if prepared is not None:
                    repo_states[name] = prepared
                    pull_locks[name] = (
                        prepared_new_locks[name]
                        if current is None
                        else self._pull_locks[name]
                    )
                    continue
                assert current is not None
                repo_states[name] = RepoState(
                    name=name,
                    config=repo_cfg.model_copy(deep=True),
                    last_head=current.last_head,
                    last_fetch=current.last_fetch,
                    fetch_error=current.fetch_error,
                    pending_changes=deepcopy(current.pending_changes),
                )
                pull_locks[name] = self._pull_locks[name]
            self.config = candidate
            self.poll_interval = candidate.poll_interval
            self._enabled_services = enabled_services
            self._dependency_graph = dependency_graph
            self._repo_states = repo_states
            self._pull_locks = pull_locks
            self._self_repo = self_update_repo
            self._config_generation += 1
            return self._config_generation

    def _require_config_generation(self, snapshot: RunnerConfigSnapshot) -> None:
        """Fail closed before an irreversible action after config drift."""

        with self._config_reload_lock:
            if snapshot.generation != self._config_generation:
                raise StableDeploymentError(
                    "CONFIG_GENERATION_CHANGED",
                    "resident config changed during release preparation",
                )

    @staticmethod
    def _restore_failed_activation(repo_path: Path, previous_head: str) -> None:
        """Restore and verify the checkout before propagating activation failure."""

        try:
            if get_head(repo_path) != previous_head:
                reset_repo_to(repo_path, previous_head)
            if get_head(repo_path) != previous_head:
                raise RuntimeError("previous HEAD equality verification failed")
        except Exception as recovery_error:
            raise StableDeploymentError(
                "RECOVERY_FAILED",
                f"failed to restore checkout after activation error: {recovery_error}",
            ) from recovery_error

    def get_startup_order(self) -> list[str]:
        """Get the order in which services should start.

        Returns:
            List of service names in startup order
        """
        return list(self._snapshot_config_state().startup_order)

    def get_shutdown_order(self) -> list[str]:
        """Get the order in which services should stop.

        Returns:
            List of service names in shutdown order (reverse of startup)
        """
        return list(self._snapshot_config_state().shutdown_order)

    def get_affected_services(self, repo_name: str) -> list[str]:
        """Get services affected by changes to a repository.

        Args:
            repo_name: Name of the repository

        Returns:
            List of service names that depend on this repo
        """
        return list(self._snapshot_repo_runtime(repo_name).affected_services)

    def execute_hook(self, service_name: str, hook_name: str) -> bool:
        """Execute a lifecycle hook for a service.

        Args:
            service_name: Name of the service
            hook_name: Name of the hook (e.g., "post_pull")

        Returns:
            True if hook executed successfully or doesn't exist
        """
        snapshot = self._snapshot_config_state()
        if service_name not in snapshot.enabled_services:
            return True

        config = snapshot.enabled_services[service_name]
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

        redaction_values = sensitive_values(os.environ)
        logger.info(
            "Executing %s hook for %s: %s",
            hook_name,
            service_name,
            redact_text(hook_cmd, redaction_values),
        )

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
                "Hook %s for %s failed with exit code %s: %s",
                hook_name,
                service_name,
                e.returncode,
                redact_text(str(e.stderr or ""), redaction_values),
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"Hook {hook_name} for {service_name} timed out")
            return False
        except Exception as e:
            logger.error(
                "Hook %s for %s failed: %s",
                hook_name,
                service_name,
                redact_text(str(e), redaction_values),
            )
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
        config_snapshot = self._snapshot_config_state()
        enabled_services = config_snapshot.enabled_services
        startup_order = list(config_snapshot.startup_order)
        logger.info(f"Starting services in order: {startup_order}")

        # Run legacy post_pull hooks on first start only for repos updated at startup.
        if not self._post_pull_executed:
            self._post_pull_executed = True
            for name in startup_order:
                service = enabled_services[name]
                if service.repo in self._startup_updated_repos:
                    self.execute_hook(name, "post_pull")

        manifest_services: dict[str, list[str]] = {}
        for repo_name in self._startup_manifest_updates:
            manifest_services[repo_name] = [
                name
                for name in startup_order
                if enabled_services[name].repo == repo_name
            ]

        handled_manifest_services: set[str] = set()
        for name in startup_order:
            if name in handled_manifest_services:
                continue
            if self.process_manager.is_running(name):
                logger.info("Service already running after handover: %s", name)
                continue
            repo_name = enabled_services[name].repo
            if repo_name not in self._startup_manifest_updates:
                self._start_service(name)
                continue

            reload_plan = self._startup_manifest_reload_plans.get(repo_name)
            affected = (
                list(reload_plan.new_affected)
                if reload_plan is not None
                else manifest_services[repo_name]
            )
            handled_manifest_services.update(affected)
            config_kwargs = (
                {
                    "quiesce_services": list(reload_plan.quiesce_services),
                    "config_digest": reload_plan.config_digest,
                    "service_environment_bindings": (
                        reload_plan.service_environment_map()
                    ),
                }
                if reload_plan is not None
                else {}
            )
            try:
                startup_snapshot = self._startup_manifest_config_snapshots.get(
                    repo_name
                )
                if startup_snapshot is not None:
                    self._require_config_generation(startup_snapshot)
                snapshot_kwargs = (
                    {"config_snapshot": startup_snapshot}
                    if startup_snapshot is not None
                    else {}
                )
                run_manifest_deployment(
                    self,
                    repo_name,
                    affected,
                    self._startup_manifest_updates[repo_name],
                    desired_running=set(affected),
                    expected_operation=self._startup_manifest_operations.get(
                        repo_name, "upgrade"
                    ),
                    request_id=self._startup_manifest_request_ids.get(
                        repo_name, f"startup-resume-{repo_name}"
                    ),
                    **snapshot_kwargs,
                    **config_kwargs,
                )
            except _OPERATIONAL_DEPLOYMENT_ERRORS as error:
                code = stable_deployment_error_code(error)
                request_id = self._startup_manifest_request_ids.get(
                    repo_name, f"startup-resume-{repo_name}"
                )
                try:
                    self._deployment_state_store().fail_handover_if_current(
                        repo_name,
                        request_id,
                        code,
                        str(error),
                    )
                except Exception as journal_error:
                    logger.error(
                        "Failed to persist startup deployment failure for %s [%s]: %s",
                        repo_name,
                        code,
                        journal_error,
                    )
                logger.error(
                    "Startup deployment failed for %s [%s]; continuing service "
                    "lifecycle: %s",
                    repo_name,
                    code,
                    bounded_redact_text(str(error)),
                )
                if code == "CONFIG_GENERATION_CHANGED":
                    current_services = self._snapshot_config_state().enabled_services
                    for service_name in affected:
                        if (
                            service_name in current_services
                            and not self.process_manager.is_running(service_name)
                        ):
                            if not self._start_service(service_name):
                                raise RuntimeError(
                                    "failed to restore startup service after "
                                    f"config generation drift: {service_name}"
                                )

    def _release_startup_repo_locks(self) -> None:
        """Release repo leases retained across startup manifest handover."""
        for repo_name, lease in tuple(self._startup_deployment_leases.items()):
            lease.__exit__(None, None, None)
            self._startup_deployment_leases.pop(repo_name, None)
            self._startup_manifest_reload_plans.pop(repo_name, None)
            self._startup_manifest_config_snapshots.pop(repo_name, None)
        for repo_name in tuple(self._startup_repo_locks):
            lock = self._startup_pull_locks.pop(repo_name, None)
            if lock is None:
                try:
                    lock = self._snapshot_repo_runtime(repo_name).pull_lock
                except ValueError:
                    lock = None
            if lock is not None and lock.locked():
                lock.release()
            self._startup_repo_locks.discard(repo_name)

    def _start_service(
        self,
        name: str,
        *,
        expected_env_path: str | None = None,
        expected_env_sha256: str | None = None,
        approved_env_snapshot: "ServiceEnvironmentFile | None" = None,
        propagate_failure: bool = False,
    ) -> bool:
        """Start a single service.

        Args:
            name: Service name

        Returns:
            True if started successfully
        """
        snapshot = self._snapshot_config_state()
        if name not in snapshot.enabled_services:
            return False

        config = snapshot.enabled_services[name]
        logger.info(f"Starting service: {name}")

        try:
            from ..config.validators import require_valid_service_readiness

            require_valid_service_readiness(name, config)
            if not self.execute_hook(name, "pre_start"):
                logger.error(f"pre_start hook failed for {name}, aborting start")
                self._record_start_failure(name, "pre_start hook failed")
                return False
            self.process_manager.start_service(
                name=name,
                config=config,
                on_ready=lambda n=name: self._on_service_ready(n),
                on_crash=lambda exit_code, n=name: self._on_service_crash(n, exit_code),
                expected_env_path=expected_env_path,
                expected_env_sha256=expected_env_sha256,
                approved_env_snapshot=approved_env_snapshot,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start service {name}: {e}")
            if propagate_failure:
                raise
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
        snapshot = self._snapshot_config_state()
        dependency_graph = DependencyGraph(snapshot.enabled_services)
        blockers = []
        for dependency in dependency_graph.get_dependencies(name):
            if (
                dependency in snapshot.enabled_services
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
        config = self._snapshot_config_state().config
        if not config.mcp or not config.mcp.enabled:
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
        config = self._snapshot_config_state().config
        if not config.slack or not config.slack.enabled:
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
                config=config.slack,
                approve_callback=self.trigger_pull,
                app_home_controller=self,
            )
            self._slack_bot.start()
        except Exception as e:
            logger.error("Failed to start Slack bot: %s", e)
            self._slack_bot = None

    def _start_orch_client(self) -> None:
        """Start orchestrator client if configured."""
        orch_cfg = self._snapshot_config_state().config.orchestrator_client
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
        snapshot = self._snapshot_config_state()
        services = []
        for name, svc_config in snapshot.config.services.items():
            health = self.health_manager.get_health(name)
            port = ready_port(svc_config.ready)
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
        config_snapshot, runtime = self._snapshot_repo_and_config(repo)
        configured_branch = runtime.config.branch
        is_self_repo = config_snapshot.self_repo == repo
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

        if is_self_repo:
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
        state = self._snapshot_repo_runtime(repo)
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
        config_snapshot, state = self._snapshot_repo_and_config(repo)
        orchestrator = config_snapshot.config.orchestrator_client
        if orchestrator is None:
            raise RuntimeError("orchestrator client is not configured")
        snapshot = capture_repo_snapshot(
            node_id=orchestrator.node_id,
            repo=repo,
            branch=state.config.branch,
            path=self.config_dir / state.config.path,
            deploy_id=deploy_id,
        )
        self._require_config_generation(config_snapshot)
        return snapshot

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
        config_snapshot = self._snapshot_config_state()
        self_repo = config_snapshot.self_repo
        if self_repo is not None:
            config_snapshot, self_runtime = self._snapshot_repo_and_config(self_repo)
        else:
            self_runtime = None
        self._orch_client.enqueue_deploy_result(
            pending.deploy_id,
            status=status,
            error=error,
            duration_ms=duration_ms,
            orchestrator_attempt_id=pending.orchestrator_attempt_id,
            connection_generation=pending.connection_generation,
            settled_snapshot=self._capture_orchestrator_repo_snapshot(
                self_repo,
                self_runtime.config.branch,
                pending.deploy_id,
            )
            if self_repo is not None and self_runtime is not None
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

        if action == "reload":
            from .service_lifecycle import reload_service_definition

            return reload_service_definition(self, service_name)

        if not service_name:
            raise ValueError("Service name is required")
        if service_name not in self._snapshot_config_state().config.services:
            raise ValueError(f"Unknown service: {service_name}")

        if action == "restart":
            message = self.restart_service(service_name)
            return {"ok": True, "service": service_name, "message": message}
        if action == "stop":
            self.stop_service(service_name)
            return {"ok": True, "service": service_name, "action": action}
        if action == "start":
            if not self._start_service(service_name):
                raise RuntimeError(f"Failed to start service: {service_name}")
            return {"ok": True, "service": service_name, "action": action}
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
        runtime = self._snapshot_repo_runtime(repo_name)
        pull_lock = runtime.pull_lock
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
                raise StableDeploymentError(
                    "DEPLOYMENT_LEASE_CONFLICT", f"already pulling {repo_name}"
                )
            logger.info("Already pulling %s, ignoring duplicate request", repo_name)
            return

        captured_changes: dict | None = None
        deployment_lease: DeploymentLease | None = None
        lifecycle_request_id = orchestrator_attempt_id or f"runtime-{uuid4()}"
        try:
            deployment_lease = self.lifecycle_control.acquire_deployment(
                repo_name, lifecycle_request_id
            )
            runtime = self._snapshot_repo_runtime(repo_name)
            if branch is not None and branch != runtime.config.branch:
                raise ApprovalRevalidationError(
                    f"approval branch {branch!r} differs from configured "
                    f"branch {runtime.config.branch!r} for {repo_name!r}"
                )

            # Guard: skip if no pending changes (e.g. pull just completed, re-entry)
            if not runtime.pending_changes:
                logger.info("No pending changes for %s, skipping", repo_name)
                return

            # _pull_repo() clears state.pending_changes → capture before any ops
            captured_changes = runtime.pending_changes

            if self._ws_handler is not None:
                self._ws_handler.broadcast_repo_pulling(repo_name, True)

            if self._slack_bot:
                self._slack_bot.notify_pulling(repo_name, auto=auto)

            self._ensure_release_manifest_activation(repo_name)
            runtime = self._snapshot_repo_runtime(repo_name)
            reload_plan = (
                self._prepare_current_manifest_config(repo_name)
                if runtime.config.release_manifest
                else None
            )
            config_snapshot, runtime = self._snapshot_repo_and_config(repo_name)
            affected = (
                list(reload_plan.new_affected)
                if reload_plan is not None
                else list(runtime.affected_services)
            )
            quiesce_services = (
                list(reload_plan.quiesce_services)
                if reload_plan is not None
                else affected
            )
            self._suppress_pending_restarts(quiesce_services)
            try:
                for svc in quiesce_services:
                    self._cancel_pending_restart(svc)

                repo_path = self.config_dir / runtime.config.path
                previous_head = (
                    get_head(repo_path)
                    if (runtime.config.release_manifest or target_head is not None)
                    else None
                )
                if runtime.config.release_manifest:
                    assert previous_head is not None
                    resolved_branch = branch or runtime.config.branch
                    intended_target = target_head or get_remote_head(
                        repo_path, runtime.config.branch
                    )
                    manifest_identity = runtime.config.release_manifest
                    journal_store = self._deployment_state_store()
                    journal_attempt_id = journal_store.begin_handover(
                        repo_name,
                        previous_head=previous_head,
                        target_ref=intended_target,
                        manifest_identity=manifest_identity,
                        request_id=lifecycle_request_id,
                        expected_operation="upgrade",
                        config_digest=(
                            reload_plan.config_digest
                            if reload_plan is not None
                            else None
                        ),
                    )
                    staged = probe_manifest_target(
                        self,
                        repo_name,
                        target_ref=intended_target,
                        expected_operation="upgrade",
                        request_id=lifecycle_request_id,
                        config_digest=(
                            reload_plan.config_digest
                            if reload_plan is not None
                            else None
                        ),
                        service_environment_bindings=(
                            reload_plan.service_environment_map()
                            if reload_plan is not None
                            else None
                        ),
                    )
                    journal_store.bind_handover_target(
                        repo_name,
                        request_id=lifecycle_request_id,
                        target_head=staged.target_head,
                        release_id=staged.manifest.release_id,
                        manifest_digest=staged.manifest_digest,
                    )
                    if target_head is not None and staged.target_head != target_head:
                        raise ApprovalRevalidationError(
                            "approved target changed during staging: "
                            f"approved={target_head} staged={staged.target_head}"
                        )
                else:
                    journal_attempt_id = None
                if runtime.config.release_manifest:
                    assert previous_head is not None
                    try:
                        self._require_config_generation(config_snapshot)
                        discarded = activate_repo_target(
                            repo_path,
                            staged.target_head,
                            strategy=runtime.config.pull_strategy or "merge",
                        )
                        actual_head = get_head(repo_path)
                        if actual_head != staged.target_head:
                            raise StableDeploymentError(
                                "PULL_FAILED",
                                "activated checkout differs from staged target",
                            )
                        if not self._commit_repo_observation(
                            RepoObservation(
                                generation=runtime.generation,
                                repo_name=repo_name,
                                repo_config=runtime.config,
                                last_fetch=runtime.last_fetch,
                                fetch_error=None,
                                last_head=actual_head,
                                pending_changes=None,
                                changed=True,
                            )
                        ):
                            raise StableDeploymentError(
                                "CONFIG_GENERATION_CHANGED",
                                "config changed before activation commit for "
                                f"{repo_name}",
                            )
                    except Exception:
                        self._restore_failed_activation(repo_path, previous_head)
                        raise
                else:
                    self._require_config_generation(config_snapshot)
                    success, discarded = self._pull_repo(repo_name)
                    if not success:
                        raise StableDeploymentError(
                            "PULL_FAILED", f"git pull failed for {repo_name}"
                        )

                if runtime.config.release_manifest:
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
                        expected_operation="upgrade",
                        request_id=lifecycle_request_id,
                        quiesce_services=quiesce_services,
                        config_digest=(
                            reload_plan.config_digest
                            if reload_plan is not None
                            else None
                        ),
                        service_environment_bindings=(
                            reload_plan.service_environment_map()
                            if reload_plan is not None
                            else None
                        ),
                        config_snapshot=config_snapshot,
                        **progress_kwargs,
                    )
                else:
                    self._restart_after_pull_legacy(repo_name, affected)
            finally:
                self._release_restart_suppression(quiesce_services)

            if repo_name == config_snapshot.self_repo:
                # Self-update: signal wrapper to restart Haniel with new code.
                # notify_done is skipped — startup notification fires after restart.
                self._require_config_generation(config_snapshot)
                self._prepare_self_update_shutdown()
                self._require_config_generation(config_snapshot)
                self._notify_self_update_approved()
                self._require_config_generation(config_snapshot)
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
            code = stable_deployment_error_code(e)
            if code != "HANDOVER_FAILED":
                logger.error(
                    "Pull failed for %s [%s]: %s",
                    repo_name,
                    code,
                    bounded_redact_text(str(e)),
                )
                try:
                    self._deployment_state_store().fail_handover_if_current(
                        repo_name,
                        lifecycle_request_id,
                        code,
                        str(e),
                    )
                except Exception as journal_error:
                    logger.error(
                        "Failed to persist pull failure for %s [%s]: %s",
                        repo_name,
                        code,
                        journal_error,
                    )
            if self._slack_bot:
                self._slack_bot.notify_done(
                    repo_name,
                    success=False,
                    pending_changes=captured_changes,
                    error=bounded_redact_text(str(e)),
                )
            raise
        finally:
            # Broadcast before release to preserve state propagation order
            if self._ws_handler is not None:
                self._ws_handler.broadcast_repo_pulling(repo_name, False)
            # Clear pending hash so new changes after this pull trigger fresh notifications
            self._last_pending_hash.pop(repo_name, None)
            if deployment_lease is not None:
                deployment_lease.__exit__(None, None, None)
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
            raise StableDeploymentError(
                "APPLY_FAILED", f"post_pull hook failed for: {failed}"
            )

        shutdown_order = [s for s in self.get_shutdown_order() if s in affected]
        for service in shutdown_order:
            self._cancel_pending_restart(service)
            if self.process_manager.is_running(service):
                logger.info("Stopping %s for post-pull restart", service)
                if not self.process_manager.stop_service(service):
                    raise StableDeploymentError(
                        "QUIESCENCE_REQUIRED",
                        f"failed to stop {service} after pull",
                    )
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
                    raise StableDeploymentError(
                        "QUIESCENCE_REQUIRED",
                        f"failed to stop already-running {service} after pull",
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
            raise StableDeploymentError(
                "APPLY_FAILED",
                "failed to restart services after pull for "
                f"{repo_name}: {'; '.join(details)}",
            )

    @staticmethod
    def _hash_pending(pending: dict) -> str:
        """Return a stable SHA-256 hex digest of a pending_changes dict."""
        return hashlib.sha256(json.dumps(pending, sort_keys=True).encode()).hexdigest()

    def _init_repo_states(self) -> None:
        """Initialize repo states with current HEAD."""
        for name in self._snapshot_config_state().repo_identity:
            self._initialize_repo_state(name, self._snapshot_repo_runtime(name))

    def _initialize_repo_state(self, name: str, state: RepoRuntimeSnapshot) -> None:
        """Set a repository baseline from an existing local checkout."""
        repo_path = self.config_dir / state.config.path
        if repo_path.exists():
            try:
                head = get_head(repo_path)
                self._commit_repo_observation(
                    RepoObservation(
                        generation=state.generation,
                        repo_name=name,
                        repo_config=state.config,
                        last_fetch=state.last_fetch,
                        fetch_error=None,
                        last_head=head,
                        pending_changes=state.pending_changes,
                    )
                )
                logger.info("Repo %s at HEAD: %s", name, head[:8])
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

        self_repo = self._snapshot_config_state().self_repo
        if self_repo is None:
            return
        try:
            state = self._snapshot_repo_runtime(self_repo)
        except ValueError:
            return
        self._commit_repo_observation(
            RepoObservation(
                generation=state.generation,
                repo_name=self_repo,
                repo_config=state.config,
                last_fetch=state.last_fetch,
                fetch_error=state.fetch_error,
                last_head=state.last_head,
                pending_changes=None,
            )
        )
        self._last_pending_hash.pop(self_repo, None)

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
        self._startup_manifest_request_ids.clear()
        self._startup_manifest_operations.clear()
        self._startup_manifest_reload_plans.clear()
        self._startup_manifest_config_snapshots.clear()
        for lease in self._startup_deployment_leases.values():
            lease.__exit__(None, None, None)
        self._startup_deployment_leases.clear()

        startup_repo_names = tuple(self._snapshot_config_state().repo_identity)

        for name in startup_repo_names:
            if name == self._snapshot_config_state().self_repo:
                continue

            try:
                runtime = self._snapshot_repo_runtime(name)
            except ValueError:
                continue
            pull_lock = runtime.pull_lock

            self._startup_repo_locks.add(name)
            self._startup_pull_locks[name] = pull_lock
            pull_lock.acquire()
            retain_for_manifest_handover = False
            deployment_lease: DeploymentLease | None = None
            startup_request_id = f"startup-{name}-{uuid4()}"
            try:
                deployment_lease = self.lifecycle_control.acquire_deployment(
                    name, startup_request_id
                )
                runtime = self._snapshot_repo_runtime(name)
                repo_path = self.config_dir / runtime.config.path
                if not repo_path.exists():
                    logger.warning(
                        "Repo %s path does not exist: %s, skipping",
                        name,
                        repo_path,
                    )
                    continue
                has_updates = fetch_repo(
                    path=repo_path,
                    branch=runtime.config.branch,
                )
                observed_at = datetime.now()

                activated = self._ensure_release_manifest_activation(name)
                runtime = self._snapshot_repo_runtime(name)
                reload_plan = (
                    self._prepare_current_manifest_config(name)
                    if runtime.config.release_manifest
                    else None
                )
                config_snapshot, runtime = self._snapshot_repo_and_config(name)
                repo_path = self.config_dir / runtime.config.path
                if has_updates:
                    logger.info("Startup update: pulling %s", name)
                    previous_head = get_head(repo_path)
                    if runtime.config.release_manifest:
                        startup_target = get_remote_head(
                            repo_path, runtime.config.branch
                        )
                        manifest_identity = runtime.config.release_manifest
                        assert manifest_identity is not None
                        journal_store = self._deployment_state_store()
                        journal_store.begin_handover(
                            name,
                            previous_head=previous_head,
                            target_ref=startup_target,
                            manifest_identity=manifest_identity,
                            request_id=startup_request_id,
                            expected_operation="upgrade",
                            config_digest=(
                                reload_plan.config_digest
                                if reload_plan is not None
                                else None
                            ),
                        )
                        staged = probe_manifest_target(
                            self,
                            name,
                            target_ref=startup_target,
                            expected_operation="upgrade",
                            request_id=startup_request_id,
                            config_digest=(
                                reload_plan.config_digest
                                if reload_plan is not None
                                else None
                            ),
                            service_environment_bindings=(
                                reload_plan.service_environment_map()
                                if reload_plan is not None
                                else None
                            ),
                        )
                        journal_store.bind_handover_target(
                            name,
                            request_id=startup_request_id,
                            target_head=staged.target_head,
                            release_id=staged.manifest.release_id,
                            manifest_digest=staged.manifest_digest,
                        )
                    if runtime.config.release_manifest:
                        try:
                            if reload_plan is not None:
                                assert self.config_path is not None
                                require_handover_config_digest(
                                    self.config_path, reload_plan.config_digest
                                )
                            self._require_config_generation(config_snapshot)
                            activate_repo_target(
                                repo_path,
                                staged.target_head,
                                strategy=runtime.config.pull_strategy or "merge",
                            )
                            actual_head = get_head(repo_path)
                            if actual_head != staged.target_head:
                                raise StableDeploymentError(
                                    "PULL_FAILED",
                                    "activated checkout differs from staged target",
                                )
                            if not self._commit_repo_observation(
                                RepoObservation(
                                    generation=runtime.generation,
                                    repo_name=name,
                                    repo_config=runtime.config,
                                    last_fetch=observed_at,
                                    fetch_error=None,
                                    last_head=actual_head,
                                    pending_changes=None,
                                    changed=True,
                                )
                            ):
                                raise StableDeploymentError(
                                    "CONFIG_GENERATION_CHANGED",
                                    "config changed before startup result commit for "
                                    f"{name}",
                                )
                        except Exception:
                            assert previous_head is not None
                            self._restore_failed_activation(repo_path, previous_head)
                            raise
                    else:
                        try:
                            self._require_config_generation(config_snapshot)
                            pull_repo(
                                path=repo_path,
                                branch=runtime.config.branch,
                                strategy=runtime.config.pull_strategy or "merge",
                            )
                            actual_head = get_head(repo_path)
                            if not self._commit_repo_observation(
                                RepoObservation(
                                    generation=runtime.generation,
                                    repo_name=name,
                                    repo_config=runtime.config,
                                    last_fetch=observed_at,
                                    fetch_error=None,
                                    last_head=actual_head,
                                    pending_changes=None,
                                    changed=True,
                                )
                            ):
                                raise StableDeploymentError(
                                    "CONFIG_GENERATION_CHANGED",
                                    "config changed before startup result commit for "
                                    f"{name}",
                                )
                        except Exception:
                            self._restore_failed_activation(repo_path, previous_head)
                            raise
                    updated.append(name)
                    if runtime.config.release_manifest:
                        assert previous_head is not None
                        self._startup_manifest_updates[name] = previous_head
                        self._startup_manifest_request_ids[name] = startup_request_id
                        self._startup_manifest_operations[name] = "upgrade"
                        if reload_plan is not None:
                            self._startup_manifest_reload_plans[name] = reload_plan
                        self._startup_manifest_config_snapshots[name] = config_snapshot
                    else:
                        self._startup_updated_repos.add(name)
                else:
                    self._commit_repo_observation(
                        RepoObservation(
                            generation=runtime.generation,
                            repo_name=name,
                            repo_config=runtime.config,
                            last_fetch=observed_at,
                            fetch_error=None,
                            last_head=runtime.last_head,
                            pending_changes=runtime.pending_changes,
                        )
                    )
                    logger.debug("Startup update: %s is up to date", name)
                    self._queue_interrupted_manifest_deployment(
                        name,
                        activated,
                        request_id=startup_request_id,
                        reload_plan=reload_plan,
                        config_snapshot=config_snapshot,
                    )

                if name in self._startup_manifest_updates:
                    retain_for_manifest_handover = True
                    if deployment_lease is not None:
                        self._startup_deployment_leases[name] = deployment_lease
                        deployment_lease = None

            except _OPERATIONAL_DEPLOYMENT_ERRORS as e:
                code = stable_deployment_error_code(e, default="STARTUP_UPDATE_FAILED")
                logger.error(
                    "Startup update failed for %s [%s]: %s",
                    name,
                    code,
                    bounded_redact_text(str(e)),
                )
                try:
                    failed_runtime = self._snapshot_repo_runtime(name)
                    self._commit_repo_observation(
                        RepoObservation(
                            generation=failed_runtime.generation,
                            repo_name=name,
                            repo_config=failed_runtime.config,
                            last_fetch=failed_runtime.last_fetch,
                            fetch_error=bounded_redact_text(str(e)),
                            last_head=failed_runtime.last_head,
                            pending_changes=failed_runtime.pending_changes,
                        )
                    )
                except ValueError:
                    pass
                if isinstance(e, StableDeploymentError):
                    try:
                        self._deployment_state_store().fail_handover_if_current(
                            name,
                            startup_request_id,
                            code,
                            str(e),
                        )
                    except Exception as journal_error:
                        logger.error(
                            "Failed to persist startup failure for %s [%s]: %s",
                            name,
                            code,
                            journal_error,
                        )
                failed.append(name)
            finally:
                if not retain_for_manifest_handover:
                    pull_lock.release()
                    self._startup_repo_locks.discard(name)
                    self._startup_pull_locks.pop(name, None)
                if deployment_lease is not None:
                    deployment_lease.__exit__(None, None, None)

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
        runtime = self._snapshot_repo_runtime(repo_name)
        if runtime.config.release_manifest:
            return False
        repo_path = self.config_dir / runtime.config.path
        discovered = discover_remote_release_manifest(repo_path, runtime.config.branch)
        if discovered is None:
            return False
        if self.config_path is None:
            raise ReleaseManifestActivationRequired(
                f"release manifest discovered for {repo_name}, but config_path is unavailable"
            )
        from .service_lifecycle import config_file_transaction

        with config_file_transaction(self.config_path):
            plan = plan_release_manifest_activation(
                self.config_path, repo_name, discovered
            )
            result = activate_release_manifest(
                self.config_path,
                plan=plan,
            )
            self.reload_config()
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
        for repo_name in self._snapshot_config_state().repo_identity:
            try:
                runtime = self._snapshot_repo_runtime(repo_name)
            except ValueError:
                continue
            if not runtime.config.release_manifest:
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
        self,
        repo_name: str,
        activated: bool,
        *,
        request_id: str,
        reload_plan: "HandoverReloadPlan | None",
        config_snapshot: RunnerConfigSnapshot,
    ) -> None:
        """Queue current-head manifest work under caller-owned startup locks."""

        runtime = self._snapshot_repo_runtime(repo_name)
        if not runtime.config.release_manifest:
            return
        repo_path = self.config_dir / runtime.config.path
        current_head = get_head(repo_path)
        store = self._deployment_state_store()
        journal = store.read(repo_name)
        if activated:
            previous_head = current_head
        else:
            if journal is None or journal.get("state") == "success":
                return
            previous_head = journal.get("previous_head")
            if not isinstance(previous_head, str) or current_head == previous_head:
                return
        manifest_identity = runtime.config.release_manifest
        assert manifest_identity is not None
        operation = journal.get("expected_operation") if journal is not None else None
        if operation not in {"fresh_install", "upgrade"}:
            operation = "upgrade"
        store.begin_handover(
            repo_name,
            previous_head=previous_head,
            target_ref=current_head,
            manifest_identity=manifest_identity,
            request_id=request_id,
            expected_operation=operation,
            config_digest=(
                reload_plan.config_digest if reload_plan is not None else None
            ),
        )
        staged = probe_manifest_target(
            self,
            repo_name,
            target_ref=current_head,
            expected_operation=operation,
            request_id=request_id,
            config_digest=(
                reload_plan.config_digest if reload_plan is not None else None
            ),
            service_environment_bindings=(
                reload_plan.service_environment_map()
                if reload_plan is not None
                else None
            ),
        )
        if staged.target_head != current_head:
            raise ReleaseStagingError(
                "startup resume target differs from detached staging evidence"
            )
        if reload_plan is not None:
            assert self.config_path is not None
            require_handover_config_digest(self.config_path, reload_plan.config_digest)
        store.bind_handover_target(
            repo_name,
            request_id=request_id,
            target_head=staged.target_head,
            release_id=staged.manifest.release_id,
            manifest_digest=staged.manifest_digest,
        )
        logger.warning(
            "Resuming staged manifest deployment for %s from %s at %s",
            repo_name,
            previous_head[:8],
            current_head[:8],
        )
        self._startup_manifest_updates[repo_name] = previous_head
        self._startup_manifest_request_ids[repo_name] = request_id
        self._startup_manifest_operations[repo_name] = operation
        self._startup_manifest_config_snapshots[repo_name] = config_snapshot
        if reload_plan is not None:
            self._startup_manifest_reload_plans[repo_name] = reload_plan

    def _poll_loop(self) -> None:
        """Main poll loop."""
        while not self._stop_event.is_set():
            try:
                self._poll_cycle()
            except _OPERATIONAL_DEPLOYMENT_ERRORS as error:
                code = stable_deployment_error_code(error, default="POLL_CYCLE_FAILED")
                logger.error(
                    "Poll cycle failed [%s]: %s",
                    code,
                    bounded_redact_text(str(error)),
                )

            # Wait for next poll interval (interruptible)
            interval = self._snapshot_config_state().config.poll_interval
            self._stop_event.wait(timeout=interval)

    def _poll_cycle(self) -> None:
        """Execute one poll cycle.

        Phase 1: Check for changes (git fetch)
        Phase 2: Apply changes (shutdown → pull → hooks → restart)
        Phase 3: Health check (process pending restarts)
        """
        with self._state_lock:
            self._state.last_poll = datetime.now()
            self._state.poll_count += 1

        # Phase 1: external observation and generation-checked memory commit
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
        cycle_snapshot = self._snapshot_config_state()

        for name in cycle_snapshot.repo_identity:
            try:
                config_snapshot, runtime = self._snapshot_repo_and_config(name)
            except ValueError:
                continue
            repo_path = self.config_dir / runtime.config.path

            if (
                name == config_snapshot.self_repo
                and self._self_update_requested.is_set()
            ):
                self._clear_self_update_pending_state()
                continue

            if not repo_path.exists():
                logger.warning(f"Repo {name} path does not exist: {repo_path}")
                continue

            try:
                # Fetch from remote (don't rely on return value)
                fetch_repo(
                    path=repo_path,
                    branch=runtime.config.branch,
                )
                observed_at = datetime.now()

                # Read current HEAD (may differ from last_head if externally pulled)
                current_head = get_head(repo_path)
                pending_changes = runtime.pending_changes
                repo_changed = False
                should_notify_pending = False

                if current_head != runtime.last_head:
                    # External pull or other process advanced HEAD
                    previous_head = runtime.last_head
                    self_checkout_matches_remote = False
                    if name == config_snapshot.self_repo and previous_head:
                        remote_head = get_remote_head(repo_path, runtime.config.branch)
                        self_checkout_matches_remote = current_head == remote_head
                    last_short = runtime.last_head[:8] if runtime.last_head else "None"
                    logger.info(
                        f"Changes detected in repo: {name} "
                        f"(last_head={last_short} → current={current_head[:8]})"
                    )
                    changed.append(name)
                    repo_changed = True
                    if (
                        name == config_snapshot.self_repo
                        and previous_head
                        and self_checkout_matches_remote
                    ):
                        pending_changes = get_applied_change_evidence(
                            repo_path, previous_head, current_head
                        )
                        should_notify_pending = True
                    else:
                        pending_changes = get_pending_changes(
                            path=repo_path,
                            branch=runtime.config.branch,
                        )
                else:
                    # current == last_head, check if remote has new commits
                    remote_head = get_remote_head(repo_path, runtime.config.branch)
                    if remote_head != current_head:
                        logger.info(
                            f"Remote changes available for repo: {name} "
                            f"(current={current_head[:8]} → remote={remote_head[:8]})"
                        )
                        changed.append(name)
                        repo_changed = True
                        should_notify_pending = True
                        pending_changes = get_pending_changes(
                            path=repo_path,
                            branch=runtime.config.branch,
                        )
                    else:
                        # A self checkout can already match origin while the running
                        # process still uses the previous code. Preserve the restart
                        # evidence so reconnecting orchestrators can recover the same
                        # deterministic Pending until approval restarts Haniel.
                        if not (
                            name == config_snapshot.self_repo
                            and self._state.self_update_pending
                        ):
                            pending_changes = None

                observation = RepoObservation(
                    generation=runtime.generation,
                    repo_name=name,
                    repo_config=runtime.config,
                    last_fetch=observed_at,
                    fetch_error=None,
                    last_head=current_head,
                    pending_changes=pending_changes,
                    changed=repo_changed,
                )
                if not self._commit_repo_observation(observation):
                    logger.info(
                        "Discarded stale repo observation for %s at generation %s",
                        name,
                        runtime.generation,
                    )
                    if name in changed:
                        changed.remove(name)
                    continue

                _committed_config, committed = self._snapshot_repo_and_config(name)
                if committed.generation != runtime.generation:
                    if name in changed:
                        changed.remove(name)
                    continue
                if repo_changed and self._ws_handler is not None:
                    self._ws_handler.broadcast_repo_change(
                        name, committed.pending_changes or {}
                    )
                if (
                    should_notify_pending
                    and self._slack_bot
                    and committed.pending_changes
                    and not committed.pull_lock.locked()
                ):
                    content_hash = self._hash_pending(committed.pending_changes)
                    if self._last_pending_hash.get(name) != content_hash:
                        self._last_pending_hash[name] = content_hash
                        self._slack_bot.notify_pending(name, committed.pending_changes)

                self._notify_orchestrator_change(name)

            except GitError as e:
                safe_error = bounded_redact_text(str(e))
                logger.error("Failed to fetch %s: %s", name, safe_error)
                self._commit_repo_observation(
                    RepoObservation(
                        generation=runtime.generation,
                        repo_name=name,
                        repo_config=runtime.config,
                        last_fetch=runtime.last_fetch,
                        fetch_error=safe_error,
                        last_head=runtime.last_head,
                        pending_changes=runtime.pending_changes,
                    )
                )

        return changed

    def _notify_orchestrator_change(self, name: str) -> None:
        if not self._orch_client:
            return
        config_snapshot, state = self._snapshot_repo_and_config(name)
        orchestrator = config_snapshot.config.orchestrator_client
        if orchestrator is None:
            return
        snapshot = capture_repo_snapshot(
            node_id=orchestrator.node_id,
            repo=name,
            branch=state.config.branch,
            path=self.config_dir / state.config.path,
        )
        self_restart_required = name == config_snapshot.self_repo and (
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
                try:
                    self._require_config_generation(config_snapshot)
                except StableDeploymentError:
                    logger.info(
                        "Discarded stale orchestrator change report for %s", name
                    )
                    return
                self._orch_client.notify_change(
                    repo=name,
                    branch=state.config.branch,
                    commits=commits,
                    affected_services=list(state.affected_services),
                    diff_stat=state.pending_changes.get("stat"),
                    deploy_id=snapshot.deploy_id,
                    target_head=snapshot.remote_head,
                    deployment_kind="manifest" if manifest_identity else "legacy",
                    expected_manifest_identity=manifest_identity,
                    expected_manifest_digest=manifest_digest,
                    is_self_update=name == config_snapshot.self_repo,
                    wait=True,
                )
        if self_restart_required:
            return
        try:
            self._require_config_generation(config_snapshot)
        except StableDeploymentError:
            logger.info("Discarded stale reconciliation report for %s", name)
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
        config_snapshot = self._snapshot_config_state()
        self_repo = config_snapshot.self_repo

        # Check for self-update repo
        if self_repo and self_repo in changed_repos:
            self._initiate_self_update(config_snapshot)
            # Remove self-repo from list; remaining repos still get normal treatment
            changed_repos = [r for r in changed_repos if r != self_repo]
            if not changed_repos:
                return

        # auto_apply=false: detection only, skip stop→pull→restart
        if not config_snapshot.config.auto_apply:
            logger.info("auto_apply=false, skipping apply for: %s", changed_repos)
            return

        repo_auto_apply: dict[str, bool] = {}
        for repo in changed_repos:
            try:
                repo_auto_apply[repo] = self._snapshot_repo_runtime(
                    repo
                ).config.auto_apply
            except ValueError:
                repo_auto_apply[repo] = True
        manual_repos = [repo for repo in changed_repos if not repo_auto_apply[repo]]
        changed_repos = [repo for repo in changed_repos if repo_auto_apply[repo]]
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
            except _OPERATIONAL_DEPLOYMENT_ERRORS as error:
                code = stable_deployment_error_code(error, default="AUTO_DEPLOY_FAILED")
                logger.error(
                    "Auto-deploy failed for %s [%s]: %s",
                    repo,
                    code,
                    bounded_redact_text(str(error)),
                )

    def _run_auto_deploy(self, repo: str) -> None:
        """Run auto_apply through the same settled-HEAD reporting contract."""
        config_snapshot, runtime = self._snapshot_repo_and_config(repo)
        has_orchestrator = not (
            getattr(self, "_orch_client", None) is None
            or config_snapshot.config.orchestrator_client is None
        )
        configured_branch = runtime.config.branch
        if not has_orchestrator:
            self.trigger_pull(repo, auto=True)
            return
        before = self._capture_orchestrator_repo_snapshot(repo, configured_branch)
        permit = self._orch_client.request_auto_attempt(before)
        progress = self._orch_client.start_deploy_progress(permit)
        started = time.monotonic()
        operation_error: str | None = None
        caught_error: Exception | None = None
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
            caught_error = exc
        finally:
            progress.stop()
        settled = self._capture_orchestrator_repo_snapshot(
            repo, configured_branch, before.deploy_id
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if self._orch_client:
            self._orch_client.report_deploy_attempt(
                settled, operation_error, duration_ms, permit
            )
        if caught_error is not None:
            raise caught_error

    def _initiate_self_update(
        self, snapshot: RunnerConfigSnapshot | None = None
    ) -> None:
        """Handle detection of changes in haniel's own repo.

        If auto_update is true, immediately signals the main thread to exit
        for update. Otherwise, sets pending state and sends a webhook notification.
        The actual update is deferred until approve_self_update() is called.

        This method is called from the poll thread, so it cannot raise
        SelfUpdateExit directly (SystemExit in a daemon thread terminates
        only that thread). Instead, it signals the main thread via an event.

        See ADR-0002 for the full self-update architecture.
        """
        snapshot = snapshot or self._snapshot_config_state()
        self._require_config_generation(snapshot)
        self_update = snapshot.config.self_update
        if self_update is None:
            raise RuntimeError("self_update config required for self-update")

        if self_update.auto_update:
            logger.info("Self-update: auto_update=true, exiting for update")
            self._require_config_generation(snapshot)
            self._notify_self_update_detected(auto=True)
            self._require_config_generation(snapshot)
            self._prepare_self_update_shutdown()
            self._require_config_generation(snapshot)
            self._self_update_requested.set()
            self.stop()
            return

        # Manual approval mode
        self._require_config_generation(snapshot)
        with self._state_lock:
            if self._state.self_update_pending:
                logger.debug("Self-update already pending, skipping duplicate")
                return
            self._state.self_update_pending = True

        logger.info("Self-update: changes detected, awaiting approval")
        self._require_config_generation(snapshot)
        self._notify_self_update_detected(auto=False)

        if self._ws_handler is not None and snapshot.self_repo:
            self._require_config_generation(snapshot)
            self._ws_handler.broadcast_self_update_pending(snapshot.self_repo)

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
        self_repo = self._snapshot_config_state().self_repo
        slack_bot = getattr(self, "_slack_bot", None)
        if slack_bot is not None and self_repo:
            slack_bot.notify_pulling(self_repo, auto=False)
        self._notify_self_update_approved()
        self._self_update_requested.set()
        self._clear_self_update_pending_state()
        # Notify dashboard that the update work is now starting (server about
        # to shut down). This is the canonical signal for the dashboard's
        # 'Updating…' overlay — the API response alone is insufficient.
        # ws_handler가 None이면(대시보드 비활성) broadcast 스킵 — 의도된 동작.
        if self._ws_handler is not None and self_repo:
            self._ws_handler.broadcast_self_update_started(self_repo)
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

            webhooks = self._snapshot_config_state().config.webhooks
            if not webhooks:
                return

            event_type = EventType[event_type_name]
            msg = WebhookMessage(
                event_type=event_type,
                service_name="haniel",
                message=message,
                details=details or {},
            )
            notifier = WebhookNotifier(webhooks)
            notifier.notify_sync(msg)
        except Exception as e:
            logger.warning(f"Failed to send self-update notification: {e}")

    def _notify_self_update_detected(self, *, auto: bool) -> None:
        """Send webhook notification for self-update detection."""
        mode = "auto-updating" if auto else "awaiting approval"
        self_repo = self._snapshot_config_state().self_repo
        self._notify_self_update(
            "SELF_UPDATE_DETECTED",
            f"Changes detected in haniel's own repository. {mode.capitalize()}.",
            {"repo": self_repo, "mode": mode},
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
        try:
            state = self._snapshot_repo_runtime(repo_name)
        except ValueError:
            return False, []
        self._ensure_release_manifest_activation(repo_name)
        config_snapshot, state = self._snapshot_repo_and_config(repo_name)
        repo_path = self.config_dir / state.config.path
        strategy = state.config.pull_strategy or "merge"

        try:
            previous_head = get_head(repo_path)
            self._require_config_generation(config_snapshot)
            discarded = pull_repo(
                path=repo_path,
                branch=state.config.branch,
                strategy=strategy,
            )
            head = get_head(repo_path)
            if not self._commit_repo_observation(
                RepoObservation(
                    generation=state.generation,
                    repo_name=repo_name,
                    repo_config=state.config,
                    last_fetch=state.last_fetch,
                    fetch_error=None,
                    last_head=head,
                    pending_changes=None,
                    changed=True,
                )
            ):
                self._restore_failed_activation(repo_path, previous_head)
                raise StableDeploymentError(
                    "CONFIG_GENERATION_CHANGED",
                    f"config changed before pull result commit for {repo_name}",
                )
            head_short = head[:8] if head else "unknown"
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
        config_snapshot = self._snapshot_config_state()
        enabled_services = config_snapshot.enabled_services
        dependency_model = DependencyGraph(enabled_services)

        # Service states from health manager — includes config for dashboard
        service_status = {}
        for name, svc_config in enabled_services.items():
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
        for name in enabled_services:
            dependency_graph[name] = {
                "dependencies": sorted(dependency_model.get_dependencies(name)),
                "dependents": sorted(dependency_model.get_dependents(name)),
            }

        # Repo states
        repo_status = {}
        for name in config_snapshot.repo_identity:
            try:
                runtime = self._snapshot_repo_runtime(name)
            except ValueError:
                continue
            head_short = runtime.last_head[:8] if runtime.last_head else None
            repo_status[name] = {
                "path": str(runtime.config.path),
                "branch": runtime.config.branch,
                "last_head": head_short,
                "last_fetch": runtime.last_fetch.isoformat()
                if runtime.last_fetch
                else None,
                "fetch_error": runtime.fetch_error,
                "pending_changes": runtime.pending_changes,
                "pulling": runtime.pull_lock.locked(),
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
                "poll_interval": config_snapshot.config.poll_interval,
                "services": service_status,
                "pending_restarts": pending_restarts,
                "dependency_graph": dependency_graph,
                "repos": repo_status,
            }
            if config_snapshot.self_repo:
                last_result = (
                    self._last_self_update_result.to_dict()
                    if self._last_self_update_result is not None
                    else None
                )
                result["self_update"] = {
                    "repo": config_snapshot.self_repo,
                    "pending": self._state.self_update_pending,
                    "auto_update": config_snapshot.config.self_update.auto_update
                    if config_snapshot.config.self_update
                    else False,
                    "last_result": last_result,
                }
            return result
