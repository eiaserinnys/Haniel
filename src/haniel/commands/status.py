"""`haniel status`: report the resident instance's real state, or say why not."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from haniel.config import HanielConfig
from haniel.core.lifecycle_control import LifecycleControl
from haniel.core.lifecycle_locks import LifecycleConflict

STATUS_TIMEOUT_SEC = 3.0
_ANY_ADDRESS_TO_LOOPBACK = {"0.0.0.0": "127.0.0.1", "::": "[::1]"}


@dataclass(frozen=True)
class StatusReport:
    running: bool
    pid: int | None
    owner_note: str | None
    runtime: dict[str, Any] | None
    unavailable_reason: str | None

    def to_json(self, config: HanielConfig) -> dict[str, Any]:
        if self.runtime is not None:
            return {
                **self.runtime,
                "running": True,
                "pid": self.pid,
                "source": "runtime",
            }
        return {
            "running": self.running,
            "pid": self.pid,
            "source": "config",
            "runtime_status": "unavailable",
            "reason": self.unavailable_reason,
            "services": {
                name: {"enabled": service.enabled, "run": service.run}
                for name, service in config.services.items()
            },
            "repos": {
                name: {"branch": repo.branch, "path": repo.path}
                for name, repo in config.repos.items()
            },
        }


def collect_status(config_path: Path, config: HanielConfig) -> StatusReport:
    running, pid, owner_note = _find_owner(config_path)
    if not running:
        return StatusReport(
            running=False,
            pid=None,
            owner_note=owner_note,
            runtime=None,
            unavailable_reason="no running haniel owns this config",
        )
    runtime, reason = _fetch_runtime(config)
    return StatusReport(
        running=True,
        pid=pid,
        owner_note=owner_note,
        runtime=runtime,
        unavailable_reason=reason,
    )


def _find_owner(config_path: Path) -> tuple[bool, int | None, str | None]:
    """Read the lifecycle owner record that `haniel run` holds for this config."""
    try:
        owner = LifecycleControl(config_path.resolve()).read_active_owner()
    except LifecycleConflict as error:
        if error.code == "LIFECYCLE_OWNER_MISSING":
            return False, None, None
        return True, None, f"owner record is unreadable ({error.detail})"
    pid = owner.get("pid")
    return True, pid if isinstance(pid, int) else None, None


def _fetch_runtime(config: HanielConfig) -> tuple[dict[str, Any] | None, str | None]:
    """Ask the running haniel's dashboard API for its real service status."""
    mcp = config.mcp
    dashboard = config.dashboard
    if mcp is None or not mcp.enabled:
        return None, "mcp disabled so runtime status is unavailable"
    if dashboard is None or not dashboard.enabled:
        return None, "dashboard disabled so runtime status is unavailable"

    host = _ANY_ADDRESS_TO_LOOPBACK.get(mcp.host, mcp.host)
    target = f"{host}:{mcp.port}"
    headers: dict[str, str] = {}
    try:
        token = config.resolve_secret(dashboard.token, "dashboard.token")
    except Exception as error:
        return None, f"could not resolve dashboard.token: {error}"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = httpx.get(
            f"http://{target}/api/status",
            headers=headers,
            timeout=STATUS_TIMEOUT_SEC,
        )
    except httpx.HTTPError as error:
        return None, (
            f"could not reach running haniel at {target} ({type(error).__name__})"
        )
    if response.status_code in (401, 403):
        return None, (
            f"running haniel at {target} rejected the dashboard token "
            f"(HTTP {response.status_code})"
        )
    if response.status_code != 200:
        return None, (
            f"running haniel at {target} answered HTTP {response.status_code} "
            "to GET /api/status"
        )
    try:
        return response.json(), None
    except ValueError:
        return None, f"running haniel at {target} sent a non-JSON status"
