"""Shared service lifecycle operations for MCP and REST adapters."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .child_env import sanitized_child_env

from ..config.io import backup_config, read_config, restore_config, write_config
from ..config.model import HanielConfig, RepoConfig, ServiceConfig
from ..config.validators import validate_config
from .git import clone_repo
from .runner import DependencyGraph

if TYPE_CHECKING:
    from .runner import ServiceRunner

logger = logging.getLogger(__name__)

CONFIG_WRITE_LOCK = threading.Lock()


def _require_config_path(runner: "ServiceRunner") -> Path:
    config_path = getattr(runner, "config_path", None)
    if not config_path:
        raise RuntimeError("config_path is not set")
    return Path(config_path)


def _commit_config(
    config_path: Path,
    config: HanielConfig,
    runner: "ServiceRunner",
) -> None:
    backup_config(config_path)
    try:
        write_config(config_path, config)
    except Exception:
        restore_config(config_path)
        raise
    runner.reload_config()


def _validate_or_raise(config: HanielConfig) -> None:
    errors = validate_config(config)
    if errors:
        raise ValueError(str(errors[0]))


def _repo_path(config_dir: Path, repo_config: RepoConfig) -> Path:
    return config_dir / repo_config.path


def _with_default_repo_path(repo_name: str, repo_config: RepoConfig) -> RepoConfig:
    if repo_config.path.strip():
        return repo_config
    return repo_config.model_copy(update={"path": f"./{repo_name}"})


def _remove_created_clone(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _ensure_repo_clone(repo_name: str, repo_config: RepoConfig, path: Path) -> str:
    if path.exists():
        if (path / ".git").is_dir():
            logger.info("Repository %s already exists at %s", repo_name, path)
            return "existing"
        raise ValueError(f"Repository path exists but is not a git repo: {path}")

    logger.info("Cloning repository %s from %s to %s", repo_name, repo_config.url, path)
    clone_repo(
        url=repo_config.url,
        branch=repo_config.branch,
        path=path,
    )
    return "created"


def _run_shell_hook(
    name: str, hook_name: str, command: str, cwd: Path, root: Path
) -> None:
    command = command.replace("{root}", str(root))
    if os.name == "nt":
        config_prefix = str(root).replace("\\", "/") + "/"
        command = re.sub(r"(?<![.\w])\./", config_prefix, command)

    shell_operators = re.search(r"&&|\|\||[;|]", command)
    if os.name == "nt" or shell_operators:
        run_cmd: str | list[str] = command
        shell = True
    else:
        run_cmd = shlex.split(command)
        shell = False

    try:
        subprocess.run(
            run_cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            shell=shell,
            env=sanitized_child_env(),
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"{hook_name} hook failed for {name}: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{hook_name} hook timed out for {name}") from e


def _maybe_run_repo_post_pull(
    repo_name: str,
    repo_config: RepoConfig,
    repo_path: Path,
    config_dir: Path,
) -> bool:
    if not repo_config.hooks or not repo_config.hooks.post_pull:
        return False
    _run_shell_hook(
        repo_name,
        "post_pull",
        repo_config.hooks.post_pull,
        repo_path,
        config_dir,
    )
    return True


def _rebuild_service_indexes(runner: "ServiceRunner") -> None:
    runner._enabled_services = {
        name: service
        for name, service in runner.config.services.items()
        if service.enabled
    }
    runner._dependency_graph = DependencyGraph(runner._enabled_services)


def _stop_if_running(runner: "ServiceRunner", service: str) -> bool:
    if not runner.process_manager.is_running(service):
        return False
    if not runner.process_manager.stop_service(service):
        raise RuntimeError(f"Failed to stop service: {service}")
    runner._cancel_pending_restart(service)
    return True


def _start_enabled_service(runner: "ServiceRunner", service: str) -> bool:
    if service not in runner._enabled_services:
        return False
    if not runner._start_service(service):
        raise RuntimeError(f"Failed to start service: {service}")
    return True


def _resolve_repo_for_registration(
    config: HanielConfig,
    service: ServiceConfig,
    repo: str | None,
    repo_config: dict[str, Any] | None,
) -> tuple[ServiceConfig, str | None, RepoConfig | None]:
    repo_name = repo or service.repo
    if not repo_name:
        return service, None, None

    if service.repo and service.repo != repo_name:
        raise ValueError(
            f"service_config.repo ({service.repo}) does not match repo ({repo_name})"
        )
    if not service.repo:
        service = service.model_copy(update={"repo": repo_name})

    supplied_repo = RepoConfig.model_validate(repo_config) if repo_config else None
    if repo_name in config.repos:
        existing = config.repos[repo_name]
        if not existing.path.strip():
            replacement_path = (
                supplied_repo.path
                if supplied_repo is not None and supplied_repo.path.strip()
                else f"./{repo_name}"
            )
            existing = existing.model_copy(update={"path": replacement_path})
            config.repos[repo_name] = existing
        return service, repo_name, config.repos[repo_name]

    if supplied_repo is None:
        raise ValueError(f"Repo not found: {repo_name}")

    supplied_repo = _with_default_repo_path(repo_name, supplied_repo)
    config.repos[repo_name] = supplied_repo
    return service, repo_name, supplied_repo


def register_service(
    runner: "ServiceRunner",
    *,
    name: str,
    service_config: dict[str, Any],
    repo: str | None = None,
    repo_config: dict[str, Any] | None = None,
    start: bool = True,
) -> dict[str, Any]:
    """Add config, ensure source checkout, run initial hooks, and start service."""
    if not name:
        raise ValueError("Service name is required")

    config_path = _require_config_path(runner)
    created_clone_path: Path | None = None
    repo_post_pull = False

    with CONFIG_WRITE_LOCK:
        config = read_config(config_path)
        if name in config.services:
            raise ValueError(f"Service already exists: {name}")

        service = ServiceConfig.model_validate(service_config)
        service, repo_name, resolved_repo = _resolve_repo_for_registration(
            config,
            service,
            repo,
            repo_config,
        )
        config.services[name] = service
        _validate_or_raise(config)

        clone_status = "skipped"
        repo_path_value = None
        if resolved_repo is not None:
            repo_path = _repo_path(runner.config_dir, resolved_repo)
            repo_path_value = resolved_repo.path
            repo_path_preexisted = repo_path.exists()
            try:
                clone_status = _ensure_repo_clone(
                    repo_name or "",
                    resolved_repo,
                    repo_path,
                )
                if clone_status == "created":
                    created_clone_path = repo_path
                repo_post_pull = _maybe_run_repo_post_pull(
                    repo_name or "",
                    resolved_repo,
                    repo_path,
                    runner.config_dir,
                )
            except Exception:
                if not repo_path_preexisted and repo_path.exists():
                    _remove_created_clone(repo_path)
                raise

        try:
            _commit_config(config_path, config, runner)
        except Exception:
            if created_clone_path is not None:
                _remove_created_clone(created_clone_path)
            raise

    service_post_pull = False
    if service.hooks and service.hooks.post_pull:
        if not runner.execute_hook(name, "post_pull"):
            raise RuntimeError(f"post_pull hook failed for service: {name}")
        service_post_pull = True

    started = False
    if start and service.enabled:
        started = _start_enabled_service(runner, name)

    return {
        "ok": True,
        "service": name,
        "repo": repo_name,
        "repo_path": repo_path_value,
        "clone": clone_status,
        "repo_post_pull": "executed" if repo_post_pull else "skipped",
        "post_pull": "executed" if service_post_pull else "skipped",
        "started": started,
    }


def register_repo(
    runner: "ServiceRunner",
    *,
    name: str,
    repo_config: dict[str, Any],
) -> dict[str, Any]:
    """Add a repo definition to config and reload runner state."""
    if not name:
        raise ValueError("Repo name is required")

    config_path = _require_config_path(runner)
    with CONFIG_WRITE_LOCK:
        config = read_config(config_path)
        if name in config.repos:
            raise ValueError(f"Repo already exists: {name}")

        config.repos[name] = RepoConfig.model_validate(repo_config)
        _validate_or_raise(config)
        _commit_config(config_path, config, runner)

    return {"ok": True, "repo": name}


def reload_service_definition(runner: "ServiceRunner", service: str) -> dict[str, Any]:
    """Reload one service definition from disk and restart only that service."""
    if not service:
        raise ValueError("Service name is required")

    config_path = _require_config_path(runner)
    new_config = read_config(config_path)
    if service not in new_config.services:
        raise KeyError(f"Service not found: {service}")
    _validate_or_raise(new_config)

    new_service = new_config.services[service]
    if new_service.repo and new_service.repo not in runner._repo_states:
        raise ValueError(
            f"Service '{service}' references repo '{new_service.repo}', "
            "but that repo is not loaded; run full reload or register_service"
        )

    runner.config.services[service] = new_service
    _rebuild_service_indexes(runner)

    stopped = _stop_if_running(runner, service)
    restarted = False
    if new_service.enabled:
        restarted = _start_enabled_service(runner, service)

    return {
        "ok": True,
        "service": service,
        "enabled": new_service.enabled,
        "stopped": stopped,
        "restarted": restarted,
        "dependents_policy": "dependents_not_restarted",
    }


def disable_service(runner: "ServiceRunner", service: str) -> dict[str, Any]:
    """Persistently disable a service and stop it if it is running."""
    if not service:
        raise ValueError("Service name is required")

    config_path = _require_config_path(runner)
    with CONFIG_WRITE_LOCK:
        config = read_config(config_path)
        if service not in config.services:
            raise KeyError(f"Service not found: {service}")

        config.services[service] = config.services[service].model_copy(
            update={"enabled": False}
        )
        _validate_or_raise(config)
        stopped = _stop_if_running(runner, service)
        _commit_config(config_path, config, runner)

    return {
        "ok": True,
        "service": service,
        "enabled": False,
        "stopped": stopped,
    }


def _purge_allowed(config_dir: Path, path: Path) -> None:
    resolved = path.resolve()
    root = config_dir.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"Refusing to purge path outside config_dir: {path}")


def delete_service_config(
    runner: "ServiceRunner",
    service: str,
    *,
    purge: bool = False,
) -> dict[str, Any]:
    """Delete a service config after graceful stop, optionally purging its repo dir."""
    if not service:
        raise ValueError("Service name is required")

    config_path = _require_config_path(runner)
    purge_path: Path | None = None

    with CONFIG_WRITE_LOCK:
        config = read_config(config_path)
        if service not in config.services:
            raise KeyError(f"Service not found: {service}")

        dependents = [
            name
            for name, svc in config.services.items()
            if name != service and service in svc.after
        ]
        if dependents:
            raise ValueError(
                f"Cannot delete service '{service}': referenced by {dependents}"
            )

        target = config.services[service]

        if purge and target.repo:
            shared = [
                name
                for name, svc in config.services.items()
                if name != service and svc.repo == target.repo
            ]
            if shared:
                raise ValueError(
                    f"Cannot purge repo '{target.repo}': still used by services {shared}"
                )
            repo_config = config.repos.get(target.repo)
            if repo_config is not None:
                purge_path = _repo_path(runner.config_dir, repo_config)
                _purge_allowed(runner.config_dir, purge_path)

        stopped = _stop_if_running(runner, service)
        del config.services[service]
        _validate_or_raise(config)
        _commit_config(config_path, config, runner)

    purged = False
    if purge_path is not None and purge_path.exists():
        shutil.rmtree(purge_path)
        purged = True

    return {
        "ok": True,
        "service": service,
        "stopped": stopped,
        "purged": purged,
        "purge_path": str(purge_path) if purge_path is not None else None,
    }
