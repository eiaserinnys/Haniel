"""Thread-safe deploy progress heartbeats shared by manual and auto execution."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

PROGRESS_STAGES = frozenset(
    {
        "preparing",
        "build",
        "preflight",
        "backing_up",
        "migrating",
        "starting",
        "verifying",
        "recovering",
    }
)

Emit = Callable[[dict], None]
ProgressCallback = Callable[[str], None]


class DeployProgressEmitter:
    """Emit transitions immediately and repeat the current stage while alive."""

    def __init__(
        self,
        *,
        node_id: str,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        emit: Emit,
        expected_budget_sec: int | None = None,
        interval_sec: float = 30.0,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("progress interval must be positive")
        self._payload = {
            "type": "deploy_progress",
            "deploy_id": deploy_id,
            "node_id": node_id,
            "orchestrator_attempt_id": orchestrator_attempt_id,
            "connection_generation": connection_generation,
        }
        self._expected_budget_sec = expected_budget_sec
        self._emit = emit
        self._interval_sec = interval_sec
        self._stage = "preparing"
        self._stage_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Declare the optional extension once. Later legacy-shaped heartbeats
        # still renew an older hub even if it rejects this first new field.
        self._safe_emit("preparing", declare_budget=True)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="haniel-deploy-progress",
            daemon=True,
        )
        self._thread.start()

    def transition(self, stage: str) -> None:
        if stage not in PROGRESS_STAGES:
            raise ValueError(f"unknown deploy progress stage: {stage}")
        with self._stage_lock:
            self._stage = stage
        self._safe_emit(stage)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._interval_sec):
            with self._stage_lock:
                stage = self._stage
            self._safe_emit(stage)

    def _safe_emit(self, stage: str, *, declare_budget: bool = False) -> None:
        try:
            message = {**self._payload, "stage": stage}
            if declare_budget and self._expected_budget_sec is not None:
                message["expected_budget_sec"] = self._expected_budget_sec
            self._emit(message)
        except Exception as exc:
            logger.warning("Failed to queue deploy progress: %s", exc)
