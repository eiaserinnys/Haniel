"""Pending orch deploy_id marker (read/write/consume) for self_repo deploys.

Written by OrchestratorClient.deploy_approval only after pre-stage succeeds,
consumed by the new runner on start() and used to send
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
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKER_RELPATH = Path(".local") / "orch_pending_deploy.json"
SCHEMA_VERSION = 2


@dataclass
class OrchPendingDeploy:
    version: int
    deploy_id: str
    started_at: str  # ISO 8601 timestamp
    orchestrator_attempt_id: str
    connection_generation: str
    execution_mode: str
    probe_id: str | None
    preflight_fingerprint: str | None

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
            orchestrator_attempt_id=str(data["orchestrator_attempt_id"]),
            connection_generation=str(data["connection_generation"]),
            execution_mode=str(data["execution_mode"]),
            probe_id=data.get("probe_id"),
            preflight_fingerprint=data.get("preflight_fingerprint"),
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


def write(
    config_dir: Path,
    deploy_id: str,
    started_at: str,
    *,
    orchestrator_attempt_id: str,
    connection_generation: str,
    execution_mode: str,
    probe_id: str | None,
    preflight_fingerprint: str | None,
) -> None:
    """Atomically write a pre-staged deploy correlation marker.

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
        orchestrator_attempt_id=orchestrator_attempt_id,
        connection_generation=connection_generation,
        execution_mode=execution_mode,
        probe_id=probe_id,
        preflight_fingerprint=preflight_fingerprint,
    ).to_dict()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def discard(config_dir: Path, *, expected_deploy_id: str) -> bool:
    """Delete a pending marker only when it belongs to the expected deploy."""
    path = _marker_path(config_dir)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("deploy_id") != expected_deploy_id:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
