"""Canonical config identity and atomic resident handover reload planning."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..config import HanielConfig
from .path_identity import canonical_path_text
from .service_environment import (
    ServiceEnvironmentFile,
    read_service_environment_file,
)

if TYPE_CHECKING:
    from .runner import ServiceRunner


class HandoverConfigError(RuntimeError):
    """The current config cannot safely satisfy a handover request."""


_NON_RELOADABLE_CONFIG_FIELDS = (
    "shutdown",
    "backoff",
    "webhooks",
    "slack",
    "mcp",
    "dashboard",
    "install",
    "orchestrator_client",
)


@dataclass(frozen=True)
class BoundServiceEnvironment:
    """Secret-free env-file identity captured by the request config digest."""

    service: str
    path: str
    sha256: str
    snapshot: ServiceEnvironmentFile = field(repr=False, compare=False)


@dataclass(frozen=True)
class HandoverReloadPlan:
    """Immutable evidence produced while replacing the resident config snapshot."""

    config_digest: str
    old_affected: tuple[str, ...]
    new_affected: tuple[str, ...]
    quiesce_services: tuple[str, ...]
    service_environments: tuple[BoundServiceEnvironment, ...]

    def service_environment_map(self) -> dict[str, BoundServiceEnvironment]:
        return {binding.service: binding for binding in self.service_environments}


def handover_config_digest(config_path: Path) -> str:
    """Hash validated config semantics and referenced env-file content.

    Only the digest leaves this function. Config and env-file values are never
    returned, persisted, or formatted into an exception.
    """

    resolved = config_path.expanduser().resolve(strict=False)
    try:
        _config, digest, _bindings = _load_config_identity(resolved)
    except HandoverConfigError:
        raise
    except Exception as error:
        raise HandoverConfigError(
            "CONFIG_RELOAD_FAILED: configuration could not be loaded"
        ) from error
    return digest


def load_handover_config(config_path: Path) -> tuple[HanielConfig, str]:
    """Load one validated config snapshot and calculate its bound digest."""

    resolved = config_path.expanduser().resolve(strict=False)
    try:
        config, digest, _bindings = _load_config_identity(resolved)
    except HandoverConfigError:
        raise
    except Exception as error:
        raise HandoverConfigError(
            "CONFIG_RELOAD_FAILED: configuration could not be loaded"
        ) from error
    return config, digest


def require_handover_config_digest(config_path: Path, expected_digest: str) -> None:
    """Fail closed when config or referenced env-file content has drifted."""

    _config, actual_digest = load_handover_config(config_path)
    if actual_digest != expected_digest:
        raise HandoverConfigError(
            "CONFIG_DIGEST_MISMATCH: resident config changed during handover"
        )


def affected_services_for_config(
    config: HanielConfig, repo_name: str
) -> tuple[str, ...]:
    """Return enabled direct and transitive dependents for a repository."""

    enabled = {
        name: service for name, service in config.services.items() if service.enabled
    }
    affected = {name for name, service in enabled.items() if service.repo == repo_name}
    changed = True
    while changed:
        changed = False
        for name, service in enabled.items():
            if name not in affected and any(dep in affected for dep in service.after):
                affected.add(name)
                changed = True
    return tuple(sorted(affected))


def prepare_runner_handover_config(
    runner: "ServiceRunner", repo_name: str, expected_digest: str
) -> HandoverReloadPlan:
    """Atomically validate and replace a resident runner's config snapshot."""

    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise HandoverConfigError("CONFIG_DIGEST_REQUIRED: config digest is required")
    if runner.config_path is None:
        raise HandoverConfigError(
            "CONFIG_RELOAD_FAILED: resident owner has no config path"
        )

    with runner._config_reload_lock:
        try:
            candidate, actual_digest, service_environments = _load_config_identity(
                runner.config_path.expanduser().resolve(strict=False)
            )
        except HandoverConfigError:
            raise
        except Exception as error:
            raise HandoverConfigError(
                "CONFIG_RELOAD_FAILED: configuration could not be loaded"
            ) from error
        if actual_digest != expected_digest:
            raise HandoverConfigError(
                "CONFIG_DIGEST_MISMATCH: resident config changed before handover"
            )
        old_repo = runner.config.repos.get(repo_name)
        new_repo = candidate.repos.get(repo_name)
        if old_repo is None or new_repo is None:
            raise HandoverConfigError(
                "CONFIG_RELOAD_UNSAFE: handover repository is not present"
            )
        old_repo_path = (runner.config_dir / old_repo.path).resolve(strict=False)
        new_repo_path = (runner.config_dir / new_repo.path).resolve(strict=False)
        old_manifest_path = (
            old_repo_path / old_repo.release_manifest
            if old_repo.release_manifest is not None
            else None
        )
        new_manifest_path = (
            new_repo_path / new_repo.release_manifest
            if new_repo.release_manifest is not None
            else None
        )
        if (
            canonical_path_text(old_repo_path) != canonical_path_text(new_repo_path)
            or (
                canonical_path_text(old_manifest_path)
                if old_manifest_path is not None
                else None
            )
            != (
                canonical_path_text(new_manifest_path)
                if new_manifest_path is not None
                else None
            )
            or old_repo.url != new_repo.url
            or old_repo.branch != new_repo.branch
        ):
            raise HandoverConfigError(
                "CONFIG_RELOAD_UNSAFE: repository fetch identity, path, or "
                "manifest changed"
            )

        changed_non_reloadable = [
            field
            for field in _NON_RELOADABLE_CONFIG_FIELDS
            if getattr(runner.config, field) != getattr(candidate, field)
        ]
        if changed_non_reloadable:
            raise HandoverConfigError(
                "CONFIG_RELOAD_UNSAFE: resident subsystems require restart for: "
                + ", ".join(changed_non_reloadable)
            )

        old_affected = tuple(
            sorted(
                set(affected_services_for_config(runner.config, repo_name))
                | set(
                    runner.process_manager.running_services_affected_by_repo(repo_name)
                )
            )
        )
        new_affected = affected_services_for_config(candidate, repo_name)
        runner._apply_config_snapshot(candidate)
        return HandoverReloadPlan(
            config_digest=actual_digest,
            old_affected=old_affected,
            new_affected=new_affected,
            quiesce_services=tuple(sorted(set(old_affected) | set(new_affected))),
            service_environments=service_environments,
        )


def _load_config_identity(
    resolved: Path,
) -> tuple[HanielConfig, str, tuple[BoundServiceEnvironment, ...]]:
    config, projection, environment_snapshots = _load_config_projection(resolved)
    serialized = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    bindings = tuple(
        BoundServiceEnvironment(
            service=service,
            path=evidence["path"],
            sha256=evidence["sha256"],
            snapshot=environment_snapshots[service],
        )
        for service, evidence in sorted(
            projection.get("release_env_evidence", {}).items()
        )
    )
    return config, hashlib.sha256(serialized).hexdigest(), bindings


def _canonical_projection(
    config: HanielConfig, config_dir: Path
) -> tuple[dict[str, Any], dict[str, ServiceEnvironmentFile]]:
    projection = deepcopy(config.model_dump(mode="json", by_alias=True))

    for repo in projection.get("repos", {}).values():
        repo["path"] = canonical_path_text(config_dir / repo["path"])

    env_evidence: dict[str, dict[str, str]] = {}
    environment_snapshots: dict[str, ServiceEnvironmentFile] = {}
    for name, service in config.services.items():
        rendered = projection["services"][name]
        if service.cwd is not None:
            rendered["cwd"] = canonical_path_text(config_dir / service.cwd)
        if service.release_env_file is None:
            continue
        path = (
            (config_dir / service.release_env_file).expanduser().resolve(strict=False)
        )
        if not path.is_file():
            raise HandoverConfigError(
                "SERVICE_ENV_FILE_INVALID: declared release env file is not a file"
            )
        snapshot = read_service_environment_file(path)
        rendered["release_env_file"] = canonical_path_text(snapshot.path)
        environment_snapshots[name] = snapshot
        env_evidence[name] = {
            "path": canonical_path_text(snapshot.path),
            "sha256": snapshot.sha256,
        }

    install = projection.get("install")
    if isinstance(install, dict):
        for item in (install.get("configs") or {}).values():
            item["path"] = canonical_path_text(config_dir / item["path"])
        install["directories"] = [
            canonical_path_text(config_dir / path)
            for path in (install.get("directories") or [])
        ]
    projection["release_env_evidence"] = env_evidence
    return projection, environment_snapshots


def _load_config_projection(
    resolved: Path,
) -> tuple[
    HanielConfig,
    dict[str, Any],
    dict[str, ServiceEnvironmentFile],
]:
    """Parse and project one exact config-file byte snapshot."""

    raw = resolved.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    config = HanielConfig.model_validate(data or {})
    projection, environment_snapshots = _canonical_projection(
        config, resolved.parent
    )
    if resolved.read_bytes() != raw:
        raise HandoverConfigError(
            "CONFIG_RELOAD_FAILED: configuration changed while it was read"
        )
    return config, projection, environment_snapshots
