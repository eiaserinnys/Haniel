"""Execution sequencer for orchestrator deploy approvals (node side).

Plain code, not a state machine (design.md 1절 「정의 밖에 두는 것」). It holds
at most one running execution and a first-in-first-out line of approvals that
were received but not yet started, so approvals keep executing one at a time
exactly as they did when the receive loop ran them inline.

The worker lives on the OrchestratorClient's event loop for the client's whole
lifetime, not for one socket: a dropped connection neither cancels the running
execution nor empties the line. Repository locks, startup-deployment waits, and
DEPLOYMENT_LEASE_CONFLICT stay inside the execution itself as before.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

Execute = Callable[[dict[str, Any]], Awaitable[None]]


class DeployExecutionSequencer:
    """Run submitted approvals strictly in submission order, one at a time."""

    def __init__(self, execute: Execute) -> None:
        self._execute = execute
        self._queue: deque[dict[str, Any]] = deque()
        self._wakeup: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._executing: dict[str, Any] | None = None

    @property
    def executing(self) -> dict[str, Any] | None:
        """The approval whose execution is running now, if any."""
        return self._executing

    @property
    def queued(self) -> int:
        """Approvals received but not yet started."""
        return len(self._queue)

    def start(self) -> None:
        """Start the single worker on the running event loop."""
        if self._worker is not None:
            return
        self._executing = None
        self._wakeup = asyncio.Event()
        self._worker = asyncio.get_running_loop().create_task(
            self._run(), name="deploy-execution-sequencer"
        )

    def submit(self, approval: dict[str, Any]) -> bool:
        """Line up one approval. Returns False (and logs) when not accepting work."""
        if self._worker is None or self._wakeup is None:
            logger.warning(
                "Dropping deploy approval %s attempt=%s: sequencer is not running",
                approval.get("deploy_id"),
                approval.get("orchestrator_attempt_id"),
            )
            return False
        self._queue.append(approval)
        self._wakeup.set()
        return True

    async def close(self) -> None:
        """Stop accepting work, drop what never started, leave running threads alone."""
        worker, self._worker = self._worker, None
        while self._queue:
            approval = self._queue.popleft()
            logger.warning(
                "Discarding queued deploy approval %s attempt=%s: client is stopping",
                approval.get("deploy_id"),
                approval.get("orchestrator_attempt_id"),
            )
        if worker is None:
            return
        executing = self._executing
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        if executing is not None:
            logger.warning(
                "Deploy approval %s attempt=%s keeps running in its thread; "
                "this client can no longer report its result",
                executing.get("deploy_id"),
                executing.get("orchestrator_attempt_id"),
            )

    async def _run(self) -> None:
        assert self._wakeup is not None
        while True:
            while not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
            approval = self._queue.popleft()
            self._executing = approval
            try:
                await self._execute(approval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Deploy approval %s attempt=%s execution failed: %s",
                    approval.get("deploy_id"),
                    approval.get("orchestrator_attempt_id"),
                    exc,
                )
            finally:
                self._executing = None
