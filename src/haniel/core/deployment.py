"""Auditable, migration-aware repository deployment orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .deployment_command_runner import (
    CommandResult,
    CommandRunner,
    CommandSpec,
    subprocess_command_runner as subprocess_command_runner,
)
from .deployment_state import DeploymentStateStore
from .release_manifest import MigrationSpec, ReleaseManifest


@dataclass(frozen=True)
class DeploymentCallbacks:
    """ServiceRunner boundary used by the state machine."""

    build: Callable[[], None]
    stop: Callable[[], dict[str, Any] | None]
    start_and_wait: Callable[[], None]
    rollback: Callable[[], None]
    prepare_roll_forward: Callable[[], None]
    stop_partial: Callable[[], None] | None = None
    writer_services: tuple[str, ...] = ()
    owner_instance: str | None = None
    quiescence_nonce: str | None = None
    acknowledge_quiesced: Callable[[dict[str, Any]], None] | None = None


@dataclass(frozen=True)
class DeploymentResult:
    status: Literal["success", "failed"]
    recovered: bool
    skipped: bool = False


class DeploymentError(RuntimeError):
    """A deployment failed after compensation was attempted."""

    def __init__(
        self,
        message: str,
        *,
        recovered: bool,
        recovery_error: Exception | None = None,
    ) -> None:
        self.recovered = recovered
        self.recovery_error = recovery_error
        super().__init__(message)


class DeploymentCoordinator:
    """Execute one manifest release and compensate every failed handover."""

    def __init__(
        self,
        *,
        state_store: DeploymentStateStore,
        command_runner: CommandRunner,
    ) -> None:
        self.state_store = state_store
        self.command_runner = command_runner

    def execute(
        self,
        *,
        repo_name: str,
        previous_head: str,
        target_head: str,
        manifest: ReleaseManifest,
        callbacks: DeploymentCallbacks,
        orchestrator_attempt_id: str | None = None,
        node_id: str | None = None,
        branch: str | None = None,
        manifest_identity: str | None = None,
        manifest_digest: str | None = None,
        journal_attempt_id: str | None = None,
        expected_operation: Literal["fresh_install", "upgrade"] | None = None,
        request_id: str | None = None,
    ) -> DeploymentResult:
        if self.state_store.is_success(repo_name, target_head, manifest.release_id):
            return DeploymentResult(status="success", recovered=False, skipped=True)

        environment = {
            "HANIEL_DEPLOY_REPO": repo_name,
            "HANIEL_RELEASE_ID": manifest.release_id,
            "HANIEL_PREVIOUS_HEAD": previous_head,
            "HANIEL_TARGET_HEAD": target_head,
            "HANIEL_PREVIOUS_RELEASE_PRESENT": str(
                expected_operation != "fresh_install"
            ).lower(),
            "HANIEL_DEPLOYMENT_JOURNAL": str(self.state_store.path(repo_name)),
        }
        if request_id is not None:
            environment["HANIEL_REQUEST_ID"] = request_id
        if expected_operation is not None:
            environment["HANIEL_DATABASE_OPERATION"] = expected_operation
        migration_started = False
        target_start_attempted = False
        operation = expected_operation
        contract_mode = bool(manifest.migration and manifest.migration.operation)
        self.state_store.begin(
            repo_name,
            previous_head,
            target_head,
            manifest.release_id,
            orchestrator_attempt_id=orchestrator_attempt_id,
            node_id=node_id,
            branch=branch,
            manifest_identity=manifest_identity,
            manifest_digest=manifest_digest,
            journal_attempt_id=journal_attempt_id,
            request_id=request_id,
            expected_operation=expected_operation,
        )

        try:
            callbacks.build()
            self.state_store.transition(repo_name, "preflight")
            if manifest.migration:
                preflight = self._run_database(
                    repo_name,
                    "preflight",
                    manifest.migration.preflight,
                    environment,
                )
                if contract_mode:
                    operation, database_journal_path = self._preflight_contract(
                        manifest.migration,
                        preflight,
                        expected_operation=expected_operation,
                    )
                    environment["HANIEL_DATABASE_OPERATION"] = operation
                    self.state_store.link_release_contract(
                        repo_name,
                        request_id=request_id,
                        operation=operation,
                        database_journal_path=database_journal_path,
                    )

            quiescence_receipt: dict[str, Any] | None = None
            if not contract_mode or operation == "upgrade":
                quiescence_receipt = callbacks.stop()
            if contract_mode and operation == "upgrade":
                self._validate_quiescence_receipt(
                    quiescence_receipt,
                    request_id=request_id,
                    repo_name=repo_name,
                    target_head=target_head,
                    callbacks=callbacks,
                )
                assert quiescence_receipt is not None
                if callbacks.acknowledge_quiesced is not None:
                    callbacks.acknowledge_quiesced(quiescence_receipt)
                receipt_path = self.state_store.write_quiescence_receipt(
                    repo_name, quiescence_receipt
                )
                environment["HANIEL_QUIESCENCE_RECEIPT"] = str(receipt_path)
                assert manifest.migration is not None
                assert operation is not None
                self.state_store.link_release_contract(
                    repo_name,
                    request_id=request_id,
                    operation=operation,
                    database_journal_path=str(
                        self.state_store.read(repo_name)["database_journal_path"]
                    ),
                    quiescence_receipt=quiescence_receipt,
                )

            if (
                manifest.migration
                and operation != "fresh_install"
                and (manifest.migration.backup or manifest.migration.verify_backup)
            ):
                self.state_store.transition(repo_name, "backing_up")
                if manifest.migration.backup:
                    self._run_database(
                        repo_name,
                        "backup",
                        manifest.migration.backup,
                        environment,
                    )
                if manifest.migration.verify_backup:
                    self._run_database(
                        repo_name,
                        "verify_backup",
                        manifest.migration.verify_backup,
                        environment,
                    )

            self.state_store.transition(repo_name, "migrating")
            if manifest.migration:
                migration_started = True
                self._run_database(
                    repo_name,
                    "apply",
                    manifest.migration.apply,
                    environment,
                )

            self.state_store.transition(repo_name, "starting")
            target_start_attempted = True
            callbacks.start_and_wait()
            self.state_store.transition(repo_name, "verifying")
            self._verify(manifest, environment)
            self.state_store.transition(repo_name, "success")
            return DeploymentResult(status="success", recovered=False)
        except Exception as deployment_error:
            return self._recover_and_raise(
                repo_name=repo_name,
                manifest=manifest,
                environment=environment,
                callbacks=callbacks,
                migration_started=migration_started,
                target_start_attempted=target_start_attempted,
                deployment_error=deployment_error,
                contract_mode=contract_mode,
                operation=operation,
            )

    def _recover_and_raise(
        self,
        *,
        repo_name: str,
        manifest: ReleaseManifest,
        environment: dict[str, str],
        callbacks: DeploymentCallbacks,
        migration_started: bool,
        target_start_attempted: bool,
        deployment_error: Exception,
        contract_mode: bool,
        operation: str | None,
    ) -> DeploymentResult:
        self.state_store.transition(
            repo_name, "recovering", message=str(deployment_error)
        )
        if operation == "fresh_install":
            if target_start_attempted:
                try:
                    if callbacks.stop_partial is None:
                        raise RuntimeError(
                            "partial target stop callback is unavailable"
                        )
                    callbacks.stop_partial()
                except Exception as cleanup_error:
                    self.state_store.transition(
                        repo_name,
                        "failed",
                        message=(
                            "fresh_install failed and partial target cleanup failed: "
                            f"{cleanup_error}"
                        ),
                        recovered=False,
                    )
                    raise DeploymentError(
                        "RECOVERY_FAILED: fresh target processes could not be stopped",
                        recovered=False,
                        recovery_error=cleanup_error,
                    ) from deployment_error
            self.state_store.transition(
                repo_name,
                "failed",
                message=f"fresh_install failed without destructive recovery: {deployment_error}",
                recovered=False,
            )
            raise DeploymentError(
                f"deployment failed: {deployment_error}", recovered=False
            ) from deployment_error

        try:
            if contract_mode:
                if migration_started:
                    recovery_environment = {
                        **environment,
                        "HANIEL_DATABASE_OPERATION": "recovery",
                        "HANIEL_FAILED_DATABASE_OPERATION": str(operation),
                    }
                    self._run_database(
                        repo_name,
                        "recovery",
                        manifest.recovery.command,
                        recovery_environment,
                    )
                callbacks.rollback()
                self.state_store.transition(
                    repo_name,
                    "failed",
                    message=(
                        "deployment failed but database and repository recovery completed: "
                        f"{deployment_error}"
                    ),
                    recovered=True,
                )
                raise DeploymentError(
                    f"deployment failed but availability recovered: {deployment_error}",
                    recovered=True,
                ) from deployment_error
            if migration_started:
                try:
                    if manifest.recovery.strategy == "roll_forward":
                        callbacks.prepare_roll_forward()
                    self._run(manifest.recovery.command, environment)
                    if manifest.recovery.strategy == "roll_forward":
                        callbacks.start_and_wait()
                        self._verify(manifest, environment)
                    else:
                        callbacks.rollback()
                except Exception:
                    if manifest.recovery.fallback is None:
                        raise
                    callbacks.prepare_roll_forward()
                    self._run(manifest.recovery.fallback, environment)
                    callbacks.rollback()
            else:
                callbacks.rollback()
        except DeploymentError:
            raise
        except Exception as recovery_error:
            self.state_store.transition(
                repo_name,
                "failed",
                message=f"recovery failed: {recovery_error}",
                recovered=False,
            )
            raise DeploymentError(
                f"deployment failed: {deployment_error}; recovery failed: {recovery_error}",
                recovered=False,
                recovery_error=recovery_error,
            ) from deployment_error

        self.state_store.transition(
            repo_name,
            "failed",
            message=f"deployment failed but availability recovered: {deployment_error}",
            recovered=True,
        )
        raise DeploymentError(
            f"deployment failed but availability recovered: {deployment_error}",
            recovered=True,
        ) from deployment_error

    @staticmethod
    def _validate_quiescence_receipt(
        receipt: dict[str, Any] | None,
        *,
        request_id: str | None,
        repo_name: str,
        target_head: str,
        callbacks: DeploymentCallbacks,
    ) -> None:
        if not isinstance(receipt, dict):
            raise RuntimeError(
                "QUIESCENCE_REQUIRED: resident owner did not acknowledge stop"
            )
        expected_fields = {
            "request_id": request_id,
            "repo": repo_name,
            "target_head": target_head,
            "owner_instance": callbacks.owner_instance,
            "quiescence_nonce": callbacks.quiescence_nonce,
        }
        mismatched = [
            key
            for key, expected in expected_fields.items()
            if not isinstance(expected, str)
            or not expected
            or receipt.get(key) != expected
        ]
        writer_services = set(callbacks.writer_services)
        stopped = receipt.get("stopped_services")
        already_stopped = receipt.get("already_stopped_services")
        quiesced = receipt.get("quiesced_services")
        if not all(
            isinstance(value, list)
            and all(isinstance(item, str) and item for item in value)
            for value in (stopped, already_stopped, quiesced)
        ):
            mismatched.append("service_sets")
        else:
            stopped_set = set(stopped)
            already_stopped_set = set(already_stopped)
            quiesced_set = set(quiesced)
            if (
                not writer_services
                or stopped_set & already_stopped_set
                or stopped_set | already_stopped_set != writer_services
                or quiesced_set != writer_services
                or len(stopped) != len(stopped_set)
                or len(already_stopped) != len(already_stopped_set)
                or len(quiesced) != len(quiesced_set)
            ):
                mismatched.append("service_sets")
        if mismatched:
            raise RuntimeError(
                "QUIESCENCE_REQUIRED: receipt does not match current deployment intent"
            )

    def _verify(self, manifest: ReleaseManifest, environment: dict[str, str]) -> None:
        for command in manifest.post_start_verify:
            try:
                self._run(command, environment)
            except Exception as error:
                raise RuntimeError(f"POST_VERIFY_FAILED: {error}") from error

    def _run(
        self, command: CommandSpec, environment: dict[str, str]
    ) -> CommandResult | None:
        return self.command_runner(command, environment)

    def _run_database(
        self,
        repo_name: str,
        phase: str,
        command: CommandSpec,
        environment: dict[str, str],
    ) -> CommandResult | None:
        error_code = {
            "preflight": "PREFLIGHT_FAILED",
            "backup": "BACKUP_CREATE_FAILED",
            "verify_backup": "BACKUP_VERIFY_FAILED",
            "apply": "APPLY_FAILED",
            "recovery": "RECOVERY_FAILED",
        }.get(phase, "DATABASE_COMMAND_FAILED")
        try:
            result = self._run(command, environment)
        except Exception as error:
            message = str(error)
            preserved_codes = (
                "JOURNAL_GATE_FAILED",
                "AMBIGUOUS_COMMIT_STATE",
                "OPERATION_MISMATCH",
            )
            if any(code in message for code in preserved_codes):
                raise
            raise RuntimeError(f"{error_code}: {message}") from error
        if result is not None and result.json_data is not None:
            self.state_store.record_database_result(
                repo_name,
                phase=phase,
                result=result.json_data,
            )
        return result

    @staticmethod
    def _preflight_contract(
        migration: MigrationSpec,
        result: CommandResult | None,
        *,
        expected_operation: Literal["fresh_install", "upgrade"] | None,
    ) -> tuple[Literal["fresh_install", "upgrade"], str]:
        payload = result.json_data if result is not None else None
        if not isinstance(payload, dict):
            raise RuntimeError("PREFLIGHT_FAILED: expected a JSON result object")
        if (
            migration.result_contract
            and payload.get("schema_version") != migration.result_contract
        ):
            raise RuntimeError("PREFLIGHT_FAILED: result contract mismatch")
        if payload.get("ok") is not True:
            raise RuntimeError("PREFLIGHT_FAILED: preflight did not return ok=true")
        operation = payload.get("operation")
        if operation not in ("fresh_install", "upgrade"):
            raise RuntimeError("PREFLIGHT_FAILED: invalid operation")
        if expected_operation is not None and operation != expected_operation:
            raise RuntimeError(
                f"OPERATION_MISMATCH: expected {expected_operation}, got {operation}"
            )
        journal_path = payload.get("journal_path")
        if not isinstance(journal_path, str) or not journal_path:
            raise RuntimeError("PREFLIGHT_FAILED: journal_path is required")
        return operation, journal_path
