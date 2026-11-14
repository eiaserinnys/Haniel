"""Durable lease mutations for active deploy attempts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .statechart import attempt_steps, steps
from .statechart.restore import NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptProgressStore:
    """Mixin that renews and expires attempt leases under the store lock."""

    async def record_progress(
        self,
        *,
        deploy_id: str,
        orchestrator_attempt_id: str,
        node_id: str,
        expected_budget_sec: int | None,
        deadline_at: str,
        now: str,
    ) -> dict[str, Any]:
        """진행 알림을 적는다. 글의 시도가 그 요청의 도는 시도이고 노드가 같을 때만 임대를 늘린다(세대는 보지 않는다).

        `now`는 이 알림이 실행 시작 시각(`execution_started_at`)을 처음 적을 때 쓰는 시각이다. 호출부가 `deadline_at`의
        상한을 같은 시각에서 재므로 한 값을 넘겨 받는다.
        닫힌 시도에 온 알림은 실행 시작 사실만 적는다(임대를 늘리거나 시도를 되살리지 않는다).
        도는 시도의 첫 알림은 「시작했다」다(wire-v1 2절): 믿음을 시작했다로 옮기고 마감 종류를 실행 임대로 바꾼다.
        """
        async with self._mutation_lock:
            try:
                attempt = await self._attempt(orchestrator_attempt_id)
                if (
                    attempt is None
                    or attempt["deploy_id"] != deploy_id
                    or attempt["node_id"] != node_id
                ):
                    return {"status": "ignored"}
                await attempt_steps.mark_execution_started(
                    self._db, orchestrator_attempt_id, now
                )
                if attempt["outcome"] != "active":
                    await self._db.commit()
                    return {"status": "closed"}
                belief = None
                try:
                    step = await steps.step_request(self._db, deploy_id, "started", now=now)
                    belief = step.belief
                except NoTransition as exc:
                    exc.warn("started")
                declared_budget_sec = attempt["expected_budget_sec"]
                if declared_budget_sec is None:
                    declared_budget_sec = expected_budget_sec
                await attempt_steps.renew_deadline(
                    self._db, orchestrator_attempt_id, deadline_at, declared_budget_sec
                )
                await self._db.commit()
                return {
                    "status": "renewed",
                    "deadline_at": deadline_at,
                    "expected_budget_sec": declared_budget_sec,
                    "belief": belief,
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
        """Fail only if the durable deadline still matches the timer snapshot.

        마감은 오케스트레이터가 스스로 내는 닫힘이다. 증거 잎은 옮기지 않는다. 마감 종류와 믿음이 맞을 때만 닫고
        (판 1: 짧은 마감, 유예 마감, 실행 임대. 판 없음: 실행 임대 하나), 맞지 않으면 stale이다(`expire_deadline`).
        """
        return await self.expire_deadline(
            orchestrator_attempt_id,
            expected_deadline_at=expected_deadline_at,
            kind=kind,
            stage=stage,
            reason=reason,
            error=error,
        )
