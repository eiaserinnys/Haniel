"""Durable lease mutations for active deploy attempts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class DeployAttemptProgressStore:
    """Mixin that renews and expires attempt leases under the store lock."""

    async def renew_active_attempt(
        self,
        *,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        expected_budget_sec: int | None,
        deadline_at: str,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                declared_budget_sec = attempt["expected_budget_sec"]
                if declared_budget_sec is None:
                    declared_budget_sec = expected_budget_sec
                await self._db.execute(
                    "UPDATE deploy_attempts SET deadline_at = ?, "
                    "expected_budget_sec = ? "
                    "WHERE orchestrator_attempt_id = ? AND outcome = 'active'",
                    (deadline_at, declared_budget_sec, orchestrator_attempt_id),
                )
                await self._db.commit()
                return {
                    "status": "renewed",
                    "deadline_at": deadline_at,
                    "expected_budget_sec": declared_budget_sec,
                }
            except Exception:
                await self._db.rollback()
                raise

    async def fail_attempt_if_deadline_due(
        self,
        orchestrator_attempt_id: str,
        *,
        expected_deadline_at: str,
        kind: str,
        stage: str,
        reason: str,
        error: str,
    ) -> dict[str, Any]:
        """Fail only if the durable deadline still matches the timer snapshot."""
        async with self._mutation_lock:
            try:
                attempt = await self._attempt(orchestrator_attempt_id)
                if attempt is None or attempt["outcome"] != "active":
                    return {"status": "ignored"}
                deadline_at = attempt["deadline_at"]
                if deadline_at != expected_deadline_at or datetime.fromisoformat(
                    deadline_at
                ) > datetime.now(timezone.utc):
                    return {"status": "deadline_changed", "deadline_at": deadline_at}
                result = await self._fail_attempt_unlocked(
                    attempt=attempt,
                    kind=kind,
                    stage=stage,
                    reason=reason,
                    error=error,
                )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise
