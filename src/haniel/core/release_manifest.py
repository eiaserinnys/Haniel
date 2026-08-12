"""Validated release-manifest models, separate from execution policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ServiceConfig
from .service_environment import read_service_environment_file
from .path_identity import canonical_path_text

from .deployment_command_runner import CommandSpec


class ProvenanceProbeSpec(BaseModel):
    """Commands allowed in a detached target checkout before live mutation."""

    model_config = ConfigDict(extra="forbid")

    prepare: CommandSpec
    probe: CommandSpec


class MigrationSpec(BaseModel):
    """Migration commands executed around the process handover."""

    model_config = ConfigDict(extra="forbid")

    destructive: bool = False
    operation: Literal["discover"] | None = None
    result_contract: str | None = Field(default=None, min_length=1)
    provenance_probe: ProvenanceProbeSpec | None = None
    preflight: CommandSpec
    backup: CommandSpec | None = None
    verify_backup: CommandSpec | None = None
    apply: CommandSpec

    @model_validator(mode="after")
    def validate_destructive_gate(self) -> "MigrationSpec":
        if self.destructive and (self.backup is None or self.verify_backup is None):
            raise ValueError(
                "destructive migration requires both backup and verify_backup"
            )
        return self


class RecoverySpec(BaseModel):
    """Automatic compensation after a failed process handover."""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["rollback", "roll_forward"]
    command: CommandSpec
    fallback: CommandSpec | None = None

    @model_validator(mode="after")
    def validate_availability_fallback(self) -> "RecoverySpec":
        if self.strategy == "roll_forward" and self.fallback is None:
            raise ValueError(
                "roll_forward recovery requires a previous-release fallback"
            )
        return self


class RetrySpec(BaseModel):
    """Bounded retry policy for one deployment phase."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=4, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    max_backoff_seconds: float = Field(default=20.0, ge=0.0, le=60.0)
    total_grace_seconds: float = Field(default=60.0, ge=0.0, le=300.0)

    @model_validator(mode="after")
    def validate_backoff_bounds(self) -> "RetrySpec":
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be greater than or equal to "
                "initial_backoff_seconds"
            )
        return self


# Preserve the PR #38 import surface while the policy becomes phase-generic.
VerifyRetrySpec = RetrySpec


class ReleaseManifest(BaseModel):
    """Repository-provided contract consumed by Haniel."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["haniel.release.v1"]
    release_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    environment_service: str | None = Field(default=None, min_length=1)
    requires_service_env_file: bool = False
    migration: MigrationSpec | None = None
    build_retry: RetrySpec = Field(default_factory=RetrySpec)
    post_start_verify: list[CommandSpec] = Field(min_length=1)
    post_start_verify_retry: RetrySpec = Field(default_factory=RetrySpec)
    recovery: RecoverySpec

    @classmethod
    def load(cls, path: Path) -> "ReleaseManifest":
        try:
            payload = path.read_text(encoding="utf-8")
            return cls.model_validate_json(payload)
        except Exception as exc:
            raise ValueError(
                f"invalid release manifest {path}; expected haniel.release.v1: {exc}"
            ) from exc


def resolve_manifest_service_cwd(
    runner: Any,
    repo_name: str,
    affected: list[str],
    environment_service: str | None,
    *,
    enabled_services: dict[str, ServiceConfig] | None = None,
) -> Path | None:
    """Resolve the same service environment for detached and live commands."""

    if enabled_services is None:
        enabled_services = (
            runner._snapshot_config_state().enabled_services
            if hasattr(runner, "_snapshot_config_state")
            else runner._enabled_services
        )

    def resolve(service: Any) -> Path:
        return (
            (runner.config_dir / service.cwd).resolve()
            if service.cwd
            else runner.config_dir.resolve()
        )

    if environment_service is not None:
        service_name = environment_service
        if service_name not in affected:
            raise ValueError(
                f"manifest environment_service is not affected: {service_name}"
            )
        service = enabled_services.get(service_name)
        if service is None:
            raise ValueError(
                f"manifest environment_service is not enabled: {service_name}"
            )
        return resolve(service)

    service_cwds = {
        resolve(service)
        for service in enabled_services.values()
        if service.repo == repo_name
    }
    if len(service_cwds) != 1:
        return None
    return next(iter(service_cwds))


@dataclass(frozen=True)
class ManifestServiceEnvironment:
    """Validated service-owned paths supplied to release children."""

    cwd: Path | None
    env_file: Path | None
    env_file_sha256: str | None = None

    def child_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        if self.cwd is not None:
            environment["HANIEL_SERVICE_CWD"] = str(self.cwd)
        if self.env_file is not None:
            environment["HANIEL_SERVICE_ENV_FILE"] = str(self.env_file)
            assert self.env_file_sha256 is not None
            environment["HANIEL_SERVICE_ENV_FILE_SHA256"] = self.env_file_sha256
        return environment


def resolve_manifest_service_environment(
    runner: Any,
    repo_name: str,
    affected: list[str],
    environment_service: str | None,
    *,
    requires_env_file: bool,
    expected_env_path: str | None = None,
    expected_env_sha256: str | None = None,
    enabled_services: dict[str, ServiceConfig] | None = None,
) -> ManifestServiceEnvironment:
    """Resolve cwd and the config-declared env file from one service identity."""

    if enabled_services is None:
        enabled_services = (
            runner._snapshot_config_state().enabled_services
            if hasattr(runner, "_snapshot_config_state")
            else runner._enabled_services
        )

    cwd = resolve_manifest_service_cwd(
        runner,
        repo_name,
        affected,
        environment_service,
        enabled_services=enabled_services,
    )
    if environment_service is None:
        if requires_env_file:
            raise ValueError(
                "SERVICE_ENV_FILE_REQUIRED: manifest environment_service is required"
            )
        return ManifestServiceEnvironment(cwd=cwd, env_file=None)

    service = enabled_services[environment_service]
    declared = service.release_env_file
    if declared is None:
        if requires_env_file:
            raise ValueError(
                "SERVICE_ENV_FILE_REQUIRED: environment service has no release env file"
            )
        return ManifestServiceEnvironment(cwd=cwd, env_file=None)

    try:
        snapshot = read_service_environment_file(runner.config_dir / declared)
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    if expected_env_path is not None and canonical_path_text(
        snapshot.path
    ) != canonical_path_text(Path(expected_env_path)):
        raise ValueError(
            "SERVICE_ENV_FILE_CHANGED: service env file path changed after binding"
        )
    if expected_env_sha256 is not None and snapshot.sha256 != expected_env_sha256:
        raise ValueError(
            "SERVICE_ENV_FILE_CHANGED: service env file content changed after binding"
        )
    return ManifestServiceEnvironment(
        cwd=cwd,
        env_file=snapshot.path,
        env_file_sha256=snapshot.sha256,
    )
