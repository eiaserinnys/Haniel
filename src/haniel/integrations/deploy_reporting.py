"""Deploy attempt reporting derived from settled repository HEAD truth."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from ..core.repo_reconciliation import RepoReconciliationSnapshot

logger = logging.getLogger(__name__)

ApprovalHandler = Callable[[str, str, str], str | None]
SnapshotHandler = Callable[[str, str, str], RepoReconciliationSnapshot]
SendJson = Callable[[dict], Awaitable[None]]


def parse_deploy_id(deploy_id: str) -> tuple[str, str, str, str] | None:
    """Parse the four-part canonical node deploy ID."""
    if not isinstance(deploy_id, str) or not deploy_id:
        return None
    parts = deploy_id.split(":", 3)
    if len(parts) != 4:
        return None
    return tuple(parts)  # type: ignore[return-value]


def classify_deploy_result(
    snapshot: RepoReconciliationSnapshot,
    operation_error: str | None,
) -> tuple[str, str | None]:
    """Classify an attempt without conflating command return with Git truth."""
    mismatch = None if snapshot.in_sync else snapshot.mismatch_error()
    if operation_error and mismatch:
        return "failed", f"{operation_error}; {mismatch}"
    if operation_error:
        return "failed", operation_error
    if mismatch:
        return "failed", mismatch
    return "success", None


class DeployReporter:
    """Execute approvals and emit result before settled reconciliation."""

    def __init__(
        self,
        *,
        node_id: str,
        approval_handler: ApprovalHandler | None,
        snapshot_handler: SnapshotHandler | None,
        send_json: SendJson,
    ) -> None:
        self._node_id = node_id
        self._approval_handler = approval_handler
        self._snapshot_handler = snapshot_handler
        self._send_json = send_json

    async def handle_approval(self, msg: dict) -> None:
        deploy_id = msg.get("deploy_id", "")
        parsed = parse_deploy_id(deploy_id)
        if parsed is None:
            await self.send_result(
                deploy_id, "failed", error=f"invalid deploy_id format: {deploy_id!r}"
            )
            return
        node_id, repo, branch, _remote_head = parsed
        if node_id != self._node_id:
            await self.send_result(
                deploy_id,
                "failed",
                error=f"deploy_id node mismatch: {node_id} != {self._node_id}",
            )
            return
        if self._approval_handler is None:
            await self.send_result(
                deploy_id,
                "failed",
                error="no deploy_approval handler registered",
            )
            return

        started = time.monotonic()
        operation_error: str | None = None
        try:
            result = await asyncio.to_thread(
                self._approval_handler, deploy_id, repo, branch
            )
        except Exception as exc:
            operation_error = str(exc)
            logger.warning("Deploy %s failed: %s", deploy_id, exc)
            result = None
        if result == "deferred":
            logger.info(
                "Deploy %s deferred; result will be sent after restart", deploy_id
            )
            return

        if self._snapshot_handler is None:
            await self.send_result(
                deploy_id,
                "failed",
                error="no repository snapshot handler registered",
            )
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            snapshot = await asyncio.to_thread(
                self._snapshot_handler, repo, branch, deploy_id
            )
        except Exception as exc:
            error = f"{operation_error}; snapshot failed: {exc}" if operation_error else (
                f"snapshot failed: {exc}"
            )
            await self.send_result(
                deploy_id, "failed", error=error, duration_ms=duration_ms
            )
            return
        await self.send_settled_result(snapshot, operation_error, duration_ms)

    async def send_settled_result(
        self,
        snapshot: RepoReconciliationSnapshot,
        operation_error: str | None,
        duration_ms: int,
    ) -> None:
        status, error = classify_deploy_result(snapshot, operation_error)
        await self.send_result(
            snapshot.deploy_id,
            status,
            error=error,
            duration_ms=duration_ms,
        )
        await self.send_reconciliation(snapshot, "settled")

    async def send_result(
        self,
        deploy_id: str,
        status: str,
        *,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await self._send_json(
            {
                "type": "deploy_result",
                "deploy_id": deploy_id,
                "node_id": self._node_id,
                "status": status,
                "error": error,
                "duration_ms": duration_ms,
            }
        )

    async def send_reconciliation(
        self,
        snapshot: RepoReconciliationSnapshot,
        phase: str,
    ) -> None:
        if snapshot.deploy_id.startswith("attempt:"):
            raise ValueError("attempt snapshot IDs are server-only")
        await self._send_json(
            {
                "type": "repo_reconciliation",
                "phase": phase,
                "deploy_id": snapshot.deploy_id,
                "node_id": snapshot.node_id,
                "repo": snapshot.repo,
                "branch": snapshot.branch,
                "local_head": snapshot.local_head,
                "remote_head": snapshot.remote_head,
            }
        )
