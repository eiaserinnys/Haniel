"""Pending orch deploy_id marker (read/write/consume) for self_repo deploys.

Written by OrchestratorClient.deploy_approval handler before triggering
self-update, consumed by the new runner on start() and used to send
DeployResult to orch-server after self-update completes.

Pairs with self_update_marker.py: self_update_marker is written by the
PowerShell wrapper after Update-HanielRepo and reports update steps;
this marker is written by Python before signaling the wrapper and only
carries the pending deploy_id so the new runner can correlate the
update result with the originating orch deploy.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKER_RELPATH = Path(".local") / "orch_pending_deploy.json"
SCHEMA_VERSION = 1


@dataclass
class OrchPendingDeploy:
    version: int
    deploy_id: str
    started_at: str  # ISO 8601 timestamp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _marker_path(config_dir: Path) -> Path:
    return config_dir / MARKER_RELPATH


def read_and_consume(config_dir: Path) -> OrchPendingDeploy | None:
    """Read marker if present, validate schema, delete it, return parsed.

    On any I/O or schema error: warn, attempt deletion, return None.
    The caller treats None as "no pending deploy correlation available".
    """
    path = _marker_path(config_dir)
    if not path.exists():
        return None
    result: OrchPendingDeploy | None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("marker must be a JSON object")
        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported marker version: {version!r}")
        result = OrchPendingDeploy(
            version=version,
            deploy_id=str(data["deploy_id"]),
            started_at=str(data["started_at"]),
        )
    except Exception as e:
        logger.warning("Orch pending deploy marker malformed at %s: %s", path, e)
        result = None
    # Always delete (consume) — even on parse error so we don't loop on a bad file
    try:
        path.unlink()
    except Exception as e:
        logger.warning("Failed to delete marker %s: %s", path, e)
    return result


def write(config_dir: Path, deploy_id: str, started_at: str) -> None:
    """Write marker (called by deploy_approval handler before self-update).

    Args:
        config_dir: Haniel config directory (the marker is placed under .local/).
        deploy_id: Orch-server deploy_id ("{node_id}:{repo}:{branch}:{first_hash}").
        started_at: ISO 8601 UTC timestamp.
    """
    path = _marker_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = OrchPendingDeploy(
        version=SCHEMA_VERSION,
        deploy_id=deploy_id,
        started_at=started_at,
    ).to_dict()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
