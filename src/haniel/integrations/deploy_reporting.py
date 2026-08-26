"""Deploy attempt reporting derived from settled repository HEAD truth."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .deploy_progress import DeployProgressEmitter, ProgressCallback

if TYPE_CHECKING:
    from ..core.repo_reconciliation import RepoReconciliationSnapshot

logger = logging.getLogger(__name__)

ApprovalHandler = Callable[[dict, ProgressCallback], str | dict | None]
SnapshotHandler = Callable[[str, str, str], "RepoReconciliationSnapshot"]
ExpectedBudgetHandler = Callable[[str], int | None]
SendJson = Callable[[dict], Awaitable[None]]


class ApprovalRevalidationError(RuntimeError):
    """Node-local evidence changed after the server fixed the execution mode."""


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
        expected_budget_handler: ExpectedBudgetHandler | None = None,
        send_json: SendJson,
        progress_interval_sec: float = 30.0,
    ) -> None:
        self._node_id = node_id
        self._approval_handler = approval_handler
        self._snapshot_handler = snapshot_handler
        self._expected_budget_handler = expected_budget_handler
        self._send_json = send_json
        self._progress_interval_sec = progress_interval_sec

    async def handle_approval(self, msg: dict) -> None:
        deploy_id = msg.get("deploy_id", "")
        orchestrator_attempt_id = msg.get("orchestrator_attempt_id", "")
        connection_generation = msg.get("connection_generation", "")
        parsed = parse_deploy_id(deploy_id)
        if parsed is None:
            await self.send_result(
                deploy_id,
                orchestrator_attempt_id,
                connection_generation,
                "failed",
                error=f"invalid deploy_id format: {deploy_id!r}",
            )
            return
        node_id, repo, branch, _remote_head = parsed
        if node_id != self._node_id:
            await self.send_result(
                deploy_id,
                orchestrator_attempt_id,
                connection_generation,
                "failed",
                error=f"deploy_id node mismatch: {node_id} != {self._node_id}",
            )
            return
        if self._approval_handler is None:
            await self.send_result(
                deploy_id,
                orchestrator_attempt_id,
                connection_generation,
                "failed",
                error="no deploy_approval handler registered",
            )
            return

        started = time.monotonic()
        operation_error: str | None = None
        loop = asyncio.get_running_loop()
        progress_tasks: set[asyncio.Task[None]] = set()

        def emit_progress(message: dict) -> None:
            def spawn() -> None:
                progress_tasks.add(asyncio.create_task(self._send_json(message)))

            loop.call_soon_threadsafe(spawn)

        expected_budget_sec = None
        if self._expected_budget_handler is not None:
            try:
                expected_budget_sec = await asyncio.to_thread(
                    self._expected_budget_handler, deploy_id
                )
            except Exception as exc:
                logger.warning(
                    "Failed to calculate expected budget for %s: %s",
                    deploy_id,
                    exc,
                )

        emitter = DeployProgressEmitter(
            node_id=self._node_id,
            deploy_id=deploy_id,
            orchestrator_attempt_id=orchestrator_attempt_id,
            connection_generation=connection_generation,
            emit=emit_progress,
            expected_budget_sec=expected_budget_sec,
            interval_sec=self._progress_interval_sec,
        )
        emitter.start()
        try:
            try:
                result = await asyncio.to_thread(
                    self._approval_handler, msg, emitter.transition
                )
            except ApprovalRevalidationError as exc:
                await self._send_json(
                    {
                        "type": "deploy_attempt_terminal",
                        "kind": "approval_revalidation_failed",
                        "deploy_id": deploy_id,
                        "orchestrator_attempt_id": orchestrator_attempt_id,
                        "connection_generation": connection_generation,
                        "stage": "approval_revalidation",
                        "reason": "approval_revalidation_failed",
                        "error": str(exc),
                    }
                )
                return
            except Exception as exc:
                operation_error = str(exc)
                logger.warning("Deploy %s failed: %s", deploy_id, exc)
                result = None
        finally:
            emitter.stop()
            await asyncio.sleep(0)
            if progress_tasks:
                outcomes = await asyncio.gather(*progress_tasks, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        logger.warning(
                            "Failed to send deploy progress for %s: %s",
                            deploy_id,
                            outcome,
                        )
        if result == "deferred":
            logger.info(
                "Deploy %s deferred; result will be sent after restart", deploy_id
            )
            return
        if (
            isinstance(result, dict)
            and result.get("type") == "manifest_recovery_evidence"
        ):
            await self._send_json(result)
            return

        if self._snapshot_handler is None:
            await self.send_result(
                deploy_id,
                orchestrator_attempt_id,
                connection_generation,
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
            error = (
                f"{operation_error}; snapshot failed: {exc}"
                if operation_error
                else (f"snapshot failed: {exc}")
            )
            await self.send_result(
                deploy_id,
                orchestrator_attempt_id,
                connection_generation,
                "failed",
                error=error,
                duration_ms=duration_ms,
            )
            return
        await self.send_settled_result(
            snapshot,
            operation_error,
            duration_ms,
            orchestrator_attempt_id,
            connection_generation,
        )

    async def send_settled_result(
        self,
        snapshot: RepoReconciliationSnapshot,
        operation_error: str | None,
        duration_ms: int,
        orchestrator_attempt_id: str,
        connection_generation: str,
    ) -> None:
        status = "failed" if operation_error else "success"
        error = operation_error
        await self.send_result(
            snapshot.deploy_id,
            orchestrator_attempt_id,
            connection_generation,
            status,
            error=error,
            duration_ms=duration_ms,
        )
        await self.send_reconciliation(
            snapshot,
            "settled",
            orchestrator_attempt_id=orchestrator_attempt_id,
            connection_generation=connection_generation,
        )

    async def send_result(
        self,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
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
                "orchestrator_attempt_id": orchestrator_attempt_id,
                "connection_generation": connection_generation,
            }
        )

    async def send_reconciliation(
        self,
        snapshot: RepoReconciliationSnapshot,
        phase: str,
        *,
        orchestrator_attempt_id: str | None = None,
        connection_generation: str | None = None,
    ) -> None:
        if snapshot.deploy_id.startswith("attempt:"):
            raise ValueError("attempt snapshot IDs are server-only")
        payload = {
            "type": "repo_reconciliation",
            "phase": phase,
            "deploy_id": snapshot.deploy_id,
            "node_id": snapshot.node_id,
            "repo": snapshot.repo,
            "branch": snapshot.branch,
            "local_head": snapshot.local_head,
            "remote_head": snapshot.remote_head,
            "orchestrator_attempt_id": orchestrator_attempt_id,
        }
        if connection_generation is not None:
            payload["connection_generation"] = connection_generation
        await self._send_json(payload)
