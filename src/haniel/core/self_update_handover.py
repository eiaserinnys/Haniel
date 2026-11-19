"""Durable handover state for relay-backed services crossing a self-update.

The exiting Haniel leaves every relay-backed service running and records what
the next process needs to adopt it: the service identity, its PID, the
repository commit it was started for, and the overlap exit timeout. The next
Haniel consumes the file once during startup and deletes it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_RELPATH = Path(".local") / "self_update_handover.json"
STATE_VERSION = 1
HANDOVER_MODES = ("overlap", "adopt")


@dataclass(frozen=True)
class HandoverServiceState:
    """One relay-backed service that must survive a self-update."""

    name: str
    pid: int
    repo: str
    head: str
    exit_timeout_sec: int
    mode: str = "overlap"


def write(config_dir: Path, services: list[HandoverServiceState]) -> Path:
    """Atomically record the services the next Haniel should adopt."""
    payload = {
        "version": STATE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "services": [_service_payload(service) for service in services],
    }
    path = config_dir / STATE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)
    return path


def _service_payload(service: HandoverServiceState) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": service.name,
        "pid": service.pid,
        "repo": service.repo,
        "head": service.head,
        "exit_timeout_sec": service.exit_timeout_sec,
    }
    # A missing mode means overlap, so overlap state files keep their old shape.
    if service.mode != "overlap":
        payload["mode"] = service.mode
    return payload


def read_and_clear(config_dir: Path) -> list[HandoverServiceState]:
    """Read and delete the handover state, tolerating a missing or bad file."""
    path = config_dir / STATE_RELPATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        logger.warning("Cannot read self-update handover state %s: %s", path, error)
        _discard_path(path)
        return []
    try:
        payload = json.loads(raw)
        services = _parse_services(payload)
    except ValueError as error:
        logger.warning(
            "Ignoring invalid self-update handover state %s: %s", path, error
        )
        _discard_path(path)
        return []
    _discard_path(path)
    return services


def discard(config_dir: Path) -> None:
    """Remove the handover state after a cancelled self-update."""
    _discard_path(config_dir / STATE_RELPATH)


def _discard_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Cannot remove self-update handover state %s: %s", path, error)


def _parse_services(payload: object) -> list[HandoverServiceState]:
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise ValueError("unsupported handover state version")
    raw_services = payload.get("services")
    if not isinstance(raw_services, list):
        raise ValueError("services must be a list")
    services: list[HandoverServiceState] = []
    for raw in raw_services:
        if not isinstance(raw, dict):
            raise ValueError("service entry must be an object")
        try:
            name = raw["name"]
            pid = raw["pid"]
            repo = raw["repo"]
            head = raw["head"]
            exit_timeout_sec = raw["exit_timeout_sec"]
        except KeyError as error:
            raise ValueError(f"service entry is missing {error.args[0]}") from error
        if not isinstance(name, str) or not name:
            raise ValueError("service name must be a non-empty string")
        if type(pid) is not int or pid <= 0:
            raise ValueError("service pid must be a positive integer")
        if not isinstance(repo, str) or not repo:
            raise ValueError("service repo must be a non-empty string")
        if not isinstance(head, str) or not head:
            raise ValueError("service head must be a non-empty string")
        if type(exit_timeout_sec) is not int or exit_timeout_sec <= 0:
            raise ValueError("service exit_timeout_sec must be a positive integer")
        mode = raw.get("mode", "overlap")
        if mode not in HANDOVER_MODES:
            raise ValueError(f"service mode must be one of {HANDOVER_MODES}")
        services.append(
            HandoverServiceState(
                name=name,
                pid=pid,
                repo=repo,
                head=head,
                exit_timeout_sec=exit_timeout_sec,
                mode=mode,
            )
        )
    return services
