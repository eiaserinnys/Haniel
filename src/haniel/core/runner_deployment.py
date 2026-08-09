"""ServiceRunner adapter for migration-aware repository deployments."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from .deployment import (
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    ReleaseManifest,
    subprocess_command_runner,
)
from .git import get_head, reset_repo_to, sha256_file_at_commit
from .handover_config import BoundServiceEnvironment, require_handover_config_digest
from .reporting_deployment_state import (
    ProgressCallback,
    ReportingDeploymentStateStore,
)
from .runner_deployment_identity import (
    deployment_error_code,
    require_resident_owner,
    validate_lifecycle_request,
)
from .release_manifest import (
    ManifestServiceEnvironment,
    resolve_manifest_service_cwd,
    resolve_manifest_service_environment,
)
from .service_environment import ServiceEnvironmentFile
from .safety_redaction import bounded_redact_text

if TYPE_CHECKING:
    from .runner import ServiceRunner

logger = logging.getLogger(__name__)


class RunnerDeploymentAdapter:
    """Translate deployment phases into ServiceRunner process operations."""

    def __init__(
        self,
        runner: "ServiceRunner",
        repo_name: str,
        affected: list[str],
        repo_path: Path,
        previous_head: str,
        target_head: str | None = None,
        request_id: str | None = None,
        quiescence_callback: Callable[[dict[str, Any]], None] | None = None,
        desired_running: set[str] | None = None,
        quiesce_services: list[str] | None = None,
        config_digest: str | None = None,
        service_environment_bindings: (
            dict[str, BoundServiceEnvironment] | None
        ) = None,
    ) -> None:
        self.runner = runner
        self.repo_name = repo_name
        self.affected = affected
        self.repo_path = repo_path
        self.previous_head = previous_head
        self.target_head = target_head
        self.request_id = request_id
        self.quiescence_callback = quiescence_callback
        self.desired_running = desired_running
        self.quiesce_services = sorted(set(quiesce_services or affected))
        self.config_digest = config_digest
        self.service_environment_bindings = service_environment_bindings
        self.handover_started = False
        self.quiescence_nonce = uuid4().hex

    def callbacks(self) -> DeploymentCallbacks:
        return DeploymentCallbacks(
            build=self.build,
            stop=self.stop,
            start_and_wait=lambda: self.start_and_wait(set(self.affected)),
            rollback=self.rollback,
            prepare_roll_forward=self.prepare_roll_forward,
            stop_partial=self.stop_partial,
            writer_services=tuple(self.quiesce_services),
            owner_instance=self._owner_instance(),
            quiescence_nonce=self.quiescence_nonce,
            config_digest=self.config_digest,
            acknowledge_quiesced=self.quiescence_callback,
        )

    def service_cwd(self, service_name: str) -> Path:
        resolved = resolve_manifest_service_cwd(
            self.runner, self.repo_name, self.affected, service_name
        )
        assert resolved is not None
        return resolved

    def derive_service_cwd(self) -> Path | None:
        return resolve_manifest_service_cwd(
            self.runner, self.repo_name, self.affected, None
        )

    def service_environment(
        self,
        service_name: str | None,
        *,
        requires_env_file: bool,
    ) -> ManifestServiceEnvironment:
        binding = (
            self.service_environment_bindings.get(service_name)
            if self.service_environment_bindings is not None
            and service_name is not None
            else None
        )
        if (
            self.config_digest is not None
            and requires_env_file
            and binding is None
        ):
            raise ValueError(
                "SERVICE_ENV_FILE_CHANGED: request config has no bound service env file"
            )
        if binding is not None:
            cwd = resolve_manifest_service_cwd(
                self.runner,
                self.repo_name,
                self.affected,
                service_name,
            )
            return ManifestServiceEnvironment(
                cwd=cwd,
                env_file=Path(binding.path),
                env_file_sha256=binding.sha256,
            )
        return resolve_manifest_service_environment(
            self.runner,
            self.repo_name,
            self.affected,
            service_name,
            requires_env_file=requires_env_file,
            expected_env_path=binding.path if binding is not None else None,
            expected_env_sha256=binding.sha256 if binding is not None else None,
        )

    def approved_service_environment(
        self, service_name: str | None
    ) -> ServiceEnvironmentFile | None:
        if self.service_environment_bindings is None or service_name is None:
            return None
        binding = self.service_environment_bindings.get(service_name)
        return binding.snapshot if binding is not None else None

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

    def stop(self) -> dict[str, Any]:
        if self.config_digest is not None:
            assert self.runner.config_path is not None
            require_handover_config_digest(self.runner.config_path, self.config_digest)
        self.handover_started = True
        stopped, already_stopped = self._stop_running()
        receipt = {
            "request_id": self.request_id,
            "repo": self.repo_name,
            "target_head": self.target_head,
            "stopped_services": stopped,
            "already_stopped_services": already_stopped,
            "quiesced_services": self.quiesce_services,
            "owner_instance": self._owner_instance(),
            "quiescence_nonce": self.quiescence_nonce,
            "quiesced_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.config_digest is not None:
            receipt["config_digest"] = self.config_digest
        return receipt

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
            binding = (
                self.service_environment_bindings.get(service)
                if self.service_environment_bindings is not None
                else None
            )
            if (
                self.config_digest is not None
                and self.runner._enabled_services[service].release_env_file is not None
                and binding is None
            ):
                raise RuntimeError(
                    "SERVICE_ENV_FILE_CHANGED: request has no approved runtime env snapshot"
                )
            started = (
                self.runner._start_service(service)
                if binding is None
                else self.runner._start_service(
                    service,
                    expected_env_path=binding.path,
                    expected_env_sha256=binding.sha256,
                    approved_env_snapshot=binding.snapshot,
                    propagate_failure=True,
                )
            )
            if not started:
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

    def stop_partial(self) -> None:
        """Stop only target processes; never mutate DB or repository state."""
        self._stop_running()

    def _stop_running(self) -> tuple[list[str], list[str]]:
        stopped: list[str] = []
        already_stopped: list[str] = []
        shutdown_order = [
            service
            for service in self.runner.get_shutdown_order()
            if service in self.quiesce_services
        ]
        shutdown_order.extend(
            service
            for service in sorted(self.quiesce_services, reverse=True)
            if service not in shutdown_order
        )
        for service in shutdown_order:
            self.runner._cancel_pending_restart(service)
            if self.runner.process_manager.is_running(service):
                if not self.runner.process_manager.stop_service(service):
                    raise RuntimeError(f"failed to stop {service}")
                stopped.append(service)
            else:
                already_stopped.append(service)
            self.runner._cancel_pending_restart(service)
        remaining = [
            service
            for service in self.quiesce_services
            if self.runner.process_manager.is_running(service)
        ]
        if remaining:
            raise RuntimeError(
                "writer services remain running: " + ", ".join(sorted(remaining))
            )
        return sorted(stopped), sorted(already_stopped)

    def _owner_instance(self) -> str | None:
        return getattr(self.runner, "lifecycle_instance_id", None)

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
    expected_operation: str | None = None,
    request_id: str | None = None,
    quiescence_callback: Callable[[dict[str, Any]], None] | None = None,
    quiesce_services: list[str] | None = None,
    config_digest: str | None = None,
    service_environment_bindings: dict[str, BoundServiceEnvironment] | None = None,
) -> None:
    """Load the pulled release manifest and run the auditable handover."""

    repo_config = runner._repo_states[repo_name].config
    if repo_config.release_manifest is None:
        raise ValueError(f"release manifest is not configured for {repo_name}")
    repo_path = (runner.config_dir / repo_config.path).resolve()
    manifest_path = (repo_path / repo_config.release_manifest).resolve()
    if not manifest_path.is_relative_to(repo_path):
        raise ValueError(f"release manifest escapes repository: {manifest_path}")

    state_store = ReportingDeploymentStateStore(
        runner.config_dir / ".haniel" / "deployments", progress_callback
    )
    target_head = get_head(repo_path)
    if journal_attempt_id is None and request_id is not None:
        existing = state_store.read(repo_name)
        if (
            existing is not None
            and existing.get("request_id") == request_id
            and existing.get("target_head") == target_head
            and existing.get("state") not in state_store.TERMINAL_STATES
        ):
            journal_attempt_id = existing.get("journal_attempt_id")
    adapter = RunnerDeploymentAdapter(
        runner=runner,
        repo_name=repo_name,
        affected=affected,
        repo_path=repo_path,
        previous_head=previous_head,
        target_head=target_head,
        request_id=request_id,
        quiescence_callback=quiescence_callback,
        desired_running=desired_running,
        quiesce_services=quiesce_services,
        config_digest=config_digest,
        service_environment_bindings=service_environment_bindings,
    )
    lifecycle = getattr(runner, "lifecycle_control", None)
    owner_instance = getattr(runner, "lifecycle_instance_id", None)
    if expected_operation is not None:
        require_resident_owner(lifecycle, owner_instance, request_id)
    if lifecycle is not None and request_id is not None:
        validate_lifecycle_request(
            lifecycle,
            state_store,
            request_id=request_id,
            repo_name=repo_name,
            target_head=target_head,
            expected_operation=expected_operation or "upgrade",
            config_digest=config_digest,
        )

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
        if expected_operation == "fresh_install":
            state_store.transition(
                repo_name,
                "failed",
                message=f"invalid fresh-install manifest; target preserved: {error}",
                recovered=False,
            )
            raise DeploymentError(
                f"invalid manifest: {error}", recovered=False
            ) from error
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

    if manifest.requires_service_env_file and config_digest is None:
        raise RuntimeError(
            "CONFIG_DIGEST_REQUIRED: manifest service environment requires "
            "the one-shot config identity boundary"
        )

    contract_mode = bool(manifest.migration and manifest.migration.operation)
    resolved_operation = expected_operation or ("upgrade" if contract_mode else None)
    if contract_mode and expected_operation is None:
        require_resident_owner(lifecycle, owner_instance, request_id)

    approved_environment = adapter.approved_service_environment(
        manifest.environment_service
    )
    base_runner = subprocess_command_runner(
        repo_path,
        approved_service_environment=approved_environment,
    )
    apply_started = False

    def run_command(command, environment):
        nonlocal apply_started
        is_recovery = environment.get("HANIEL_DATABASE_OPERATION") == "recovery"
        if config_digest is not None and not apply_started and not is_recovery:
            assert runner.config_path is not None
            require_handover_config_digest(runner.config_path, config_digest)
        if manifest.migration is not None and command is manifest.migration.apply:
            apply_started = True
        backup_dir = (
            runner.config_dir / ".haniel" / "backups" / repo_name / manifest.release_id
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        command_environment = {
            **environment,
            "HANIEL_BACKUP_DIR": str(backup_dir),
        }
        service_environment = adapter.service_environment(
            manifest.environment_service,
            requires_env_file=manifest.requires_service_env_file,
        )
        command_environment.update(service_environment.child_environment())
        return base_runner(
            command,
            command_environment,
        )

    coordinator = DeploymentCoordinator(
        state_store=state_store,
        command_runner=run_command,
    )
    owns_spool_request = False
    lease = None
    try:
        if lifecycle is not None and request_id is not None:
            if not lifecycle.request_path(request_id).exists():
                runtime_payload = {
                    "kind": "runtime-handover",
                    "repo": repo_name,
                    "target_ref": target_head,
                    "expected_operation": expected_operation or "upgrade",
                    "executor_instance": owner_instance,
                }
                if config_digest is not None:
                    runtime_payload["config_digest"] = config_digest
                lifecycle.submit_request(request_id, runtime_payload)
                lifecycle.ack(
                    request_id,
                    "accepted",
                    {
                        "repo": repo_name,
                        "owner_instance": getattr(
                            runner, "lifecycle_instance_id", None
                        ),
                        "config_digest": config_digest,
                    },
                )
                owns_spool_request = True
            if quiescence_callback is None:
                adapter.quiescence_callback = lambda receipt: lifecycle.ack(
                    request_id, "quiesced", receipt
                )
            lease = lifecycle.acquire_deployment(repo_name, request_id)
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
            expected_operation=(resolved_operation),
            request_id=request_id,
            config_digest=config_digest,
        )
    except Exception as error:
        if owns_spool_request:
            lifecycle.ack(
                request_id,
                "terminal",
                {
                    "schema_version": "haniel.runtime-handover.result.v1",
                    "ok": False,
                    "request_id": request_id,
                    "error": {
                        "code": deployment_error_code(error),
                        "message": bounded_redact_text(str(error)),
                    },
                    "config_digest": config_digest,
                },
            )
        raise
    else:
        if owns_spool_request:
            lifecycle.ack(
                request_id,
                "terminal",
                {
                    "schema_version": "haniel.runtime-handover.result.v1",
                    "ok": True,
                    "request_id": request_id,
                    "repo": repo_name,
                    "target_head": target_head,
                    "config_digest": config_digest,
                },
            )
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)
