"""ServiceRunner adapter for migration-aware repository deployments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .deployment import (
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentStateStore,
    ReleaseManifest,
    subprocess_command_runner,
)
from .git import get_head, reset_repo_to, sha256_file_at_commit

if TYPE_CHECKING:
    from ..config import ServiceConfig
    from .runner import ServiceRunner

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]
_PROGRESS_STATES = frozenset(
    {
        "build",
        "preflight",
        "backing_up",
        "migrating",
        "starting",
        "verifying",
        "recovering",
    }
)


class ReportingDeploymentStateStore(DeploymentStateStore):
    """Persist journal truth, then report nonterminal phase changes."""

    def __init__(
        self, directory: Path, progress_callback: ProgressCallback | None
    ) -> None:
        super().__init__(directory)
        self._progress_callback = progress_callback

    def begin(self, *args: Any, **kwargs: Any) -> str:
        journal_attempt_id = super().begin(*args, **kwargs)
        self._report("build")
        return journal_attempt_id

    def transition(self, repo_name: str, state: str, **kwargs: Any) -> None:
        super().transition(repo_name, state, **kwargs)
        self._report(state)

    def _report(self, state: str) -> None:
        if self._progress_callback is None or state not in _PROGRESS_STATES:
            return
        try:
            self._progress_callback(state)
        except Exception as exc:
            logger.warning("Failed to report deploy progress %s: %s", state, exc)


class RunnerDeploymentAdapter:
    """Translate deployment phases into ServiceRunner process operations."""

    def __init__(
        self,
        runner: "ServiceRunner",
        repo_name: str,
        affected: list[str],
        repo_path: Path,
        previous_head: str,
        desired_running: set[str] | None = None,
    ) -> None:
        self.runner = runner
        self.repo_name = repo_name
        self.affected = affected
        self.repo_path = repo_path
        self.previous_head = previous_head
        self.desired_running = desired_running
        self.handover_started = False

    def callbacks(self) -> DeploymentCallbacks:
        return DeploymentCallbacks(
            build=self.build,
            stop=self.stop,
            start_and_wait=lambda: self.start_and_wait(set(self.affected)),
            rollback=self.rollback,
            prepare_roll_forward=self.prepare_roll_forward,
        )

    def service_cwd(self, service_name: str) -> Path:
        if service_name not in self.affected:
            raise ValueError(
                f"manifest environment_service is not affected: {service_name}"
            )
        service = self.runner._enabled_services.get(service_name)
        if service is None:
            raise ValueError(
                f"manifest environment_service is not enabled: {service_name}"
            )
        return self._resolve_service_cwd(service)

    def derive_service_cwd(self) -> Path | None:
        service_cwds = {
            self._resolve_service_cwd(service)
            for service in self.runner._enabled_services.values()
            if service.repo == self.repo_name
        }
        if len(service_cwds) != 1:
            return None
        return next(iter(service_cwds))

    def _resolve_service_cwd(self, service: "ServiceConfig") -> Path:
        return (
            (self.runner.config_dir / service.cwd).resolve()
            if service.cwd
            else self.runner.config_dir.resolve()
        )

    def build(self) -> None:
        failed = [
            service
            for service in self.affected
            if not self.runner.execute_hook(service, "post_pull")
        ]
        if failed:
            raise RuntimeError(
                f"post_pull hook failed for: {', '.join(sorted(failed))}"
            )

    def stop(self) -> None:
        self.handover_started = True
        self._stop_running()

    def start_and_wait(self, selected: set[str]) -> None:
        startup_order = [
            service
            for service in self.runner.get_startup_order()
            if service in selected
        ]
        for service in startup_order:
            self.runner._cancel_pending_restart(service)
            blockers = self.runner._blocked_start_dependencies(service)
            if blockers:
                raise RuntimeError(
                    f"{service} blocked by dependencies: {', '.join(blockers)}"
                )
            if self.runner.process_manager.is_running(service):
                if not self.runner.process_manager.stop_service(service):
                    raise RuntimeError(f"failed to stop already-running {service}")
                self.runner._cancel_pending_restart(service)
            if not self.runner._start_service(service):
                raise RuntimeError(f"failed to start {service}")
            if not self.runner.process_manager.wait_for_ready(service):
                raise RuntimeError(f"readiness timeout for {service}")
            if not self.runner.process_manager.is_running(service):
                raise RuntimeError(f"{service} exited before readiness verification")

    def rollback(self) -> None:
        if self.handover_started:
            self._stop_running()
        reset_repo_to(self.repo_path, self.previous_head)
        state = self.runner._repo_states[self.repo_name]
        state.last_head = get_head(self.repo_path)
        self.build()
        recovery_services = (
            self.desired_running
            if self.desired_running is not None
            else set(self.affected)
        )
        if self.handover_started or self.desired_running is not None:
            try:
                self.start_and_wait(recovery_services)
            except Exception as error:
                raise RuntimeError(
                    f"availability down after rollback: {error}"
                ) from error
        self._assert_recovery_availability(recovery_services)

    def prepare_roll_forward(self) -> None:
        self.handover_started = True
        self._stop_running()

    def _stop_running(self) -> None:
        shutdown_order = [
            service
            for service in self.runner.get_shutdown_order()
            if service in self.affected
        ]
        for service in shutdown_order:
            self.runner._cancel_pending_restart(service)
            if self.runner.process_manager.is_running(service):
                if not self.runner.process_manager.stop_service(service):
                    raise RuntimeError(f"failed to stop {service}")
            self.runner._cancel_pending_restart(service)

    def _assert_recovery_availability(self, services: set[str]) -> None:
        down: list[str] = []
        for name in sorted(services):
            pid = self.runner.process_manager.get_pid(name)
            if pid is None:
                down.append(f"{name} (process not running)")
                continue
            service = self.runner._enabled_services[name]
            if service.ready and service.ready.startswith("port:"):
                try:
                    port = int(service.ready.removeprefix("port:"))
                except ValueError:
                    down.append(f"{name} (invalid ready port: {service.ready})")
                    continue
                if not self.runner.process_manager.platform.is_port_owned_by_process_tree(
                    port, pid
                ):
                    down.append(f"{name} (port {port} not owned by process {pid})")
        if down:
            raise RuntimeError("availability down after rollback: " + ", ".join(down))


def run_manifest_deployment(
    runner: "ServiceRunner",
    repo_name: str,
    affected: list[str],
    previous_head: str,
    *,
    desired_running: set[str] | None = None,
    orchestrator_attempt_id: str | None = None,
    node_id: str | None = None,
    branch: str | None = None,
    journal_attempt_id: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Load the pulled release manifest and run the auditable handover."""

    repo_config = runner._repo_states[repo_name].config
    if repo_config.release_manifest is None:
        raise ValueError(f"release manifest is not configured for {repo_name}")
    repo_path = (runner.config_dir / repo_config.path).resolve()
    manifest_path = (repo_path / repo_config.release_manifest).resolve()
    if not manifest_path.is_relative_to(repo_path):
        raise ValueError(f"release manifest escapes repository: {manifest_path}")

    adapter = RunnerDeploymentAdapter(
        runner,
        repo_name,
        affected,
        repo_path,
        previous_head,
        desired_running,
    )
    state_store = ReportingDeploymentStateStore(
        runner.config_dir / ".haniel" / "deployments", progress_callback
    )
    target_head = get_head(repo_path)

    try:
        manifest = ReleaseManifest.load(manifest_path)
    except Exception as error:
        live = state_store.read(repo_name)
        identity = repo_config.release_manifest
        digest = (
            live.get("manifest_digest")
            if live is not None and live.get("journal_attempt_id") == journal_attempt_id
            else None
        )
        state_store.begin(
            repo_name,
            previous_head,
            target_head,
            "invalid-manifest",
            orchestrator_attempt_id=orchestrator_attempt_id,
            node_id=node_id,
            branch=branch,
            manifest_identity=identity,
            manifest_digest=digest,
            journal_attempt_id=journal_attempt_id,
        )
        state_store.transition(repo_name, "recovering", message=str(error))
        try:
            adapter.rollback()
        except Exception as recovery_error:
            state_store.transition(
                repo_name,
                "failed",
                message=f"manifest recovery failed: {recovery_error}",
                recovered=False,
            )
            raise DeploymentError(
                f"invalid manifest and recovery failed: {recovery_error}",
                recovered=False,
                recovery_error=recovery_error,
            ) from error
        state_store.transition(
            repo_name,
            "failed",
            message=f"invalid manifest; previous release restored: {error}",
            recovered=True,
        )
        raise DeploymentError(
            f"invalid manifest; previous release restored: {error}",
            recovered=True,
        ) from error

    base_runner = subprocess_command_runner(repo_path)

    def run_command(command, environment):
        backup_dir = (
            runner.config_dir / ".haniel" / "backups" / repo_name / manifest.release_id
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        command_environment = {
            **environment,
            "HANIEL_BACKUP_DIR": str(backup_dir),
        }
        service_cwd = (
            adapter.service_cwd(manifest.environment_service)
            if manifest.environment_service
            else adapter.derive_service_cwd()
        )
        if service_cwd is not None:
            command_environment["HANIEL_SERVICE_CWD"] = str(service_cwd)
        base_runner(
            command,
            command_environment,
        )

    coordinator = DeploymentCoordinator(
        state_store=state_store,
        command_runner=run_command,
    )
    coordinator.execute(
        repo_name=repo_name,
        previous_head=previous_head,
        target_head=target_head,
        manifest=manifest,
        callbacks=adapter.callbacks(),
        orchestrator_attempt_id=orchestrator_attempt_id,
        node_id=node_id,
        branch=branch,
        manifest_identity=repo_config.release_manifest,
        manifest_digest=sha256_file_at_commit(
            repo_path, target_head, repo_config.release_manifest
        ),
        journal_attempt_id=journal_attempt_id,
    )
