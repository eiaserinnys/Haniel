"""macOS LaunchAgent registration for Haniel services."""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shlex
import sys
from pathlib import Path

from ..config import HanielConfig
from ..config.model import ServiceDefinitionConfig
from .utils import get_tool_search_path

logger = logging.getLogger(__name__)

_LABEL_PREFIX = "com.haniel."
_SERVICE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _absolute(path: Path) -> Path:
    """Make a path absolute without resolving symlinks."""
    return path if path.is_absolute() else path.absolute()


def _working_directory(config_dir: Path, service_cfg: ServiceDefinitionConfig) -> Path:
    """Resolve the LaunchAgent WorkingDirectory from the service config."""
    resolved = service_cfg.working_directory.replace("{root}", str(config_dir))
    path = Path(resolved)
    if not path.is_absolute():
        path = config_dir / path
    return _absolute(path)


def _require_executable(path: Path, description: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{description} not found: {path}")
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"{description} is not executable: {path}")


def _self_update_runner(config: HanielConfig, config_dir: Path) -> Path:
    """Return the absolute wrapper script path for a self-updating install."""
    self_update = config.self_update
    assert self_update is not None
    repo_config = config.repos.get(self_update.repo)
    if repo_config is None:
        raise RuntimeError(
            f"Self-update repo is not configured in repos: {self_update.repo}"
        )
    repo_path = Path(repo_config.path)
    if not repo_path.is_absolute():
        repo_path = config_dir / repo_path
    runner_path = _absolute(repo_path) / "haniel-runner.sh"
    helper_path = runner_path.parent / "scripts" / "haniel_atomic_release.py"
    if not runner_path.is_file():
        raise FileNotFoundError(f"Self-update wrapper not found: {runner_path}")
    if not helper_path.is_file():
        raise FileNotFoundError(f"Atomic release helper not found: {helper_path}")
    return runner_path


def _launchd_environment(
    config_dir: Path, service_cfg: ServiceDefinitionConfig
) -> dict[str, str]:
    """Build the plist environment from the service config."""
    environment = {
        key: value.replace("{root}", str(config_dir))
        for key, value in (service_cfg.environment or {}).items()
    }
    if "PATH" not in environment:
        environment["PATH"] = get_tool_search_path()
    return environment


def _launchd_label(service_cfg: ServiceDefinitionConfig) -> str:
    name = service_cfg.name
    if not _SERVICE_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid service name for a LaunchAgent label: {name}")
    return _LABEL_PREFIX + name


def generate_launchd_plist(
    config: HanielConfig,
    config_dir: Path,
    config_filename: str,
    service_cfg: ServiceDefinitionConfig,
) -> bytes:
    """Generate the LaunchAgent property list for a Haniel service.

    Raises:
        RuntimeError: root or service-account installs, missing self repo
        ValueError: service name unusable in a LaunchAgent label
        FileNotFoundError: missing config, working directory or runner path
    """
    if os.geteuid() == 0:
        raise RuntimeError(
            "Install the LaunchAgent as a normal user; do not run the "
            "installer with sudo"
        )
    if service_cfg.service_account is not None:
        raise RuntimeError(
            "service_account is not supported for a macOS LaunchAgent; "
            "the agent runs as the logged-in user"
        )

    label = _launchd_label(service_cfg)
    config_dir = _absolute(config_dir)
    config_path = config_dir / config_filename
    if not config_path.is_file():
        raise FileNotFoundError(f"Haniel configuration not found: {config_path}")

    working_directory = _working_directory(config_dir, service_cfg)
    if not working_directory.is_dir():
        raise FileNotFoundError(f"Working directory not found: {working_directory}")

    if config.self_update is not None:
        runner_path = _self_update_runner(config, config_dir)
        program_arguments = [
            "/bin/bash",
            str(runner_path),
            str(config_dir / "haniel-runner.conf"),
        ]
    else:
        interpreter = Path(sys.executable)
        _require_executable(interpreter, "Haniel's Python interpreter")
        program_arguments = [
            str(_absolute(interpreter)),
            "-m",
            "haniel.cli",
            "run",
            str(config_path),
        ]

    log_dir = config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "Label": label,
        "WorkingDirectory": str(working_directory),
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "EnvironmentVariables": _launchd_environment(config_dir, service_cfg),
        "StandardOutPath": str(log_dir / "haniel-runner.stdout.log"),
        "StandardErrorPath": str(log_dir / "haniel-runner.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def register_launchd_service(
    config: HanielConfig,
    config_dir: Path,
    config_filename: str,
    service_cfg: ServiceDefinitionConfig,
) -> None:
    """Install the LaunchAgent plist without loading it in this session."""
    plist_bytes = generate_launchd_plist(
        config, config_dir, config_filename, service_cfg
    )
    label = _launchd_label(service_cfg)
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{label}.plist"
    plist_path.write_bytes(plist_bytes)
    plist_path.chmod(0o644)

    uid = os.getuid()
    logger.info("LaunchAgent file installed; not loaded in this session")
    logger.info("  File: %s", plist_path)
    logger.info(
        "  Start: launchctl bootstrap gui/%s %s",
        uid,
        shlex.quote(str(plist_path)),
    )
    logger.info("  Status: launchctl print gui/%s/%s", uid, label)
    logger.info("  Restart loaded job: launchctl kickstart -k gui/%s/%s", uid, label)
    logger.info("  Stop and unload: launchctl bootout gui/%s/%s", uid, label)
    logger.info("  Apply a reinstalled plist: bootout, then bootstrap again")
