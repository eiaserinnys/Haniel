"""Durable deploy probe and attempt deadline scheduling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


class DeployAttemptDeadlines:
    """Timer mechanics for the coordinator's durable DB-owned deadlines."""

    async def shutdown(self) -> None:
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

    def _schedule_probe(self, probe_id: str) -> None:
        self._timers[f"probe:{probe_id}"] = asyncio.create_task(
            self._probe_timeout(probe_id)
        )

    def _schedule_attempt(self, orchestrator_attempt_id: str, deadline_at: str) -> None:
        delay = max(
            0.0,
            (datetime.fromisoformat(deadline_at) - datetime.now(timezone.utc)).total_seconds(),
        )
        self._timers[f"attempt:{orchestrator_attempt_id}"] = asyncio.create_task(
            self._attempt_timeout(orchestrator_attempt_id, delay)
        )

    async def _probe_timeout(self, probe_id: str) -> None:
        try:
            await asyncio.sleep(self._probe_timeout_sec)
        except asyncio.CancelledError:
            return
        if await self.attempts.terminalize_preflight(
            probe_id,
            kind="preflight_timeout",
            stage="deadline",
            reason="probe_timeout",
            error="deploy plan probe timed out before begin",
        ):
            self._resolve_probe(probe_id, self._plan_rejected("preflight timeout", 504))

    async def _attempt_timeout(self, orchestrator_attempt_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        attempt = await self.attempts.get_attempt(orchestrator_attempt_id)
        if attempt is None or attempt["outcome"] != "active":
            return
        if attempt["execution_mode"] == "evidence_recovery":
            reason = "recovery_evidence_timeout"
            error = "manifest recovery evidence was not received before the attempt deadline"
        elif attempt["result_status"] == "success":
            reason = "settled_head_timeout"
            error = "settled HEAD evidence was not received after a successful deploy result"
        elif attempt["settled_at"] is not None:
            reason = "deploy_result_timeout"
            error = "deploy result was not received after settled HEAD evidence"
        else:
            reason = "attempt_evidence_timeout"
            error = "deploy result and settled HEAD evidence were not received before the attempt deadline"
        result = await self.attempts.fail_active_attempt(
            orchestrator_attempt_id,
            kind="attempt_timeout",
            stage="deadline",
            reason=reason,
            error=error,
        )
        if result.get("deploy_id"):
            await self._after_store_terminal(
                attempt["node_id"], orchestrator_attempt_id, result
            )

    def _cancel_timer(self, key: str) -> None:
        task = self._timers.pop(key, None)
        if task is not None:
            task.cancel()

    @staticmethod
    def _deadline(seconds: float) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
