"""닫힌 시도의 늦은 증거를 요청에 넣는 저장소 조각(카드 #1259 PR 2b-1).

오케스트레이터가 스스로 닫은 시도(실행 임대 마감, 보내기 실패)의 결과와 정착이 뒤늦게 닿아 증거가 종결되면,
요청 정의에 늦은 사건(success_evidence, late_failed)을 넣는다. 요청을 바꾸는 것은 정의의 전이뿐이다.
판 1은 뒤 시도가 실행을 시작했는지, 판 없음은 뒤 시도를 열었는지 확인한다(design.md:123, 431).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .deploy_attempt_intake import is_legacy
from .statechart import attempt_steps, steps
from .statechart.restore import NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptLateEvidence:
    """Mixin: late evidence of a closed attempt, applied to the request through the definition."""

    async def _no_later_attempt(self, attempt: dict[str, Any]) -> bool:
        """판 1은 뒤 시도의 실행 시작을, 판 없음은 뒤 시도를 연 이력을 확인한다."""
        if attempt["attempt_seq"] is None:
            return False
        started = "" if is_legacy(attempt.get("node_protocol")) else " AND execution_started_at IS NOT NULL"
        cursor = await self._db.execute(
            "SELECT 1 FROM deploy_attempts WHERE deploy_id = ? AND attempt_seq > ?" + started + " LIMIT 1",
            (attempt["deploy_id"], attempt["attempt_seq"]),
        )
        return await cursor.fetchone() is None

    async def _apply_late_evidence_unlocked(
        self, attempt: dict[str, Any], leaf: str
    ) -> dict[str, Any]:
        """닫힌 시도의 증거가 종결된 뒤 요청에 늦은 사건을 넣는다. 요청을 바꾸는 것은 정의의 전이뿐이다."""
        deploy_id = attempt["deploy_id"]
        now = _now_iso()
        changed = False
        if leaf == "ev_both":
            no_later = await self._no_later_attempt(attempt)
            try:
                step = await steps.step_request(
                    self._db,
                    deploy_id,
                    "success_evidence",
                    facts={"no_later": no_later},
                    data={
                        "attempt_id": attempt["orchestrator_attempt_id"],
                        "duration_ms": attempt["duration_ms"],
                    },
                    now=now,
                )
                changed = True
            except NoTransition as exc:
                exc.warn("success_evidence")
            else:
                if step.leaf == "success":
                    cursor = await self._db.execute(
                        """SELECT orchestrator_attempt_id FROM deploy_attempts
                           WHERE deploy_id = ? AND outcome = 'active'
                           ORDER BY orchestrator_attempt_id""",
                        (deploy_id,),
                    )
                    active_ids = [row[0] for row in await cursor.fetchall()]
                    for active_id in active_ids:
                        await attempt_steps.step_attempt(self._db, active_id, "superseded_by_node", now=now)
                    await self._terminalize_sibling_probes_unlocked(
                        deploy_id, "canonical succeeded through late evidence"
                    )
                    return {
                        "status": "success",
                        "deploy_id": deploy_id,
                        "closed_now": True,
                        "cancelled_orchestrator_attempt_ids": active_ids,
                    }
        elif leaf == "ev_failed":
            current_retry = await self._retry_unlocked(deploy_id)
            try:
                await steps.step_request(
                    self._db,
                    deploy_id,
                    "late_failed",
                    data={
                        "failed_attempt_id": attempt["orchestrator_attempt_id"],
                        "stable_source": attempt["orchestrator_attempt_id"],
                        "marker_created_at": current_retry["created_at"] if current_retry else None,
                    },
                    now=now,
                )
                changed = True
            except NoTransition as exc:
                exc.warn("late_failed")
        else:
            return {"status": "recorded", "closed_now": False}
        after = await self._event(deploy_id)
        return {
            "status": "late_evidence",
            "deploy_id": deploy_id,
            "evidence": "success" if leaf == "ev_both" else "failed",
            "request_changed": changed,
            "request_status": after["status"] if after else None,
            "closed_now": False,
        }
