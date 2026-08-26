"""Pure deployment budget and repository hook selection helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping

from ..config import ServiceConfig

if TYPE_CHECKING:
    from .release_manifest import ReleaseManifest


@dataclass(frozen=True)
class DeploymentBudget:
    """Expected upper bound components declared by one Haniel node."""

    build_hooks_sec: int
    pre_start_hooks_sec: int
    readiness_sec: int
    verification_sec: int
    recovery_sec: int
    total_sec: int

    def breakdown(self) -> str:
        """Return the calculation basis used in one stable log line."""

        return (
            f"build={self.build_hooks_sec}s + "
            f"pre_start={self.pre_start_hooks_sec}s + "
            f"readiness={self.readiness_sec}s + "
            f"verification={self.verification_sec}s + "
            f"recovery={self.recovery_sec}s"
        )


def repo_owned_services(
    repo_name: str,
    affected: Iterable[str],
    services: Mapping[str, ServiceConfig],
) -> tuple[str, ...]:
    """Keep affected ordering while excluding dependency-only services."""

    return tuple(
        service_name
        for service_name in affected
        if (service := services.get(service_name)) is not None
        and service.repo == repo_name
    )


def effective_ready_timeout(
    service: ServiceConfig,
    process_default: float,
) -> float:
    """Preserve the legacy runtime default unless YAML declares an override."""

    if "ready_timeout" not in service.model_fields_set:
        return process_default
    return service.ready_timeout


def expected_deployment_budget(
    *,
    repo_name: str,
    affected: Iterable[str],
    services: Mapping[str, ServiceConfig],
    manifest: "ReleaseManifest",
) -> DeploymentBudget:
    """Calculate the node-owned budget that the hub may trust in protocol v2."""

    affected_services = tuple(affected)
    owned_services = repo_owned_services(repo_name, affected_services, services)
    one_build_attempt_sec = sum(
        service.hooks.timeout
        for service_name in owned_services
        if (service := services[service_name]).hooks is not None
        and service.hooks.post_pull is not None
    )
    build_hooks_sec = one_build_attempt_sec * manifest.build_retry.max_attempts
    pre_start_hooks_sec = sum(
        service.hooks.timeout
        for service_name in affected_services
        if (service := services.get(service_name)) is not None
        and service.hooks is not None
        and service.hooks.pre_start is not None
    )
    readiness_sec = sum(
        services[service_name].ready_timeout
        for service_name in affected_services
        if service_name in services
    )
    verification_sec = sum(
        command.timeout_seconds for command in manifest.post_start_verify
    )
    recovery_sec = one_build_attempt_sec + readiness_sec
    return DeploymentBudget(
        build_hooks_sec=build_hooks_sec,
        pre_start_hooks_sec=pre_start_hooks_sec,
        readiness_sec=readiness_sec,
        verification_sec=verification_sec,
        recovery_sec=recovery_sec,
        total_sec=(
            build_hooks_sec
            + pre_start_hooks_sec
            + readiness_sec
            + verification_sec
            + recovery_sec
        ),
    )
