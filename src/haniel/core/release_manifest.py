"""Validated release-manifest models, separate from execution policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ReleaseManifest(BaseModel):
    """Repository-provided contract consumed by Haniel."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["haniel.release.v1"]
    release_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    environment_service: str | None = Field(default=None, min_length=1)
    migration: MigrationSpec | None = None
    post_start_verify: list[CommandSpec] = Field(min_length=1)
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
) -> Path | None:
    """Resolve the same service environment for detached and live commands."""

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
        service = runner._enabled_services.get(service_name)
        if service is None:
            raise ValueError(
                f"manifest environment_service is not enabled: {service_name}"
            )
        return resolve(service)

    service_cwds = {
        resolve(service)
        for service in runner._enabled_services.values()
        if service.repo == repo_name
    }
    if len(service_cwds) != 1:
        return None
    return next(iter(service_cwds))
