"""닫힌 시도의 늦은 증거를 요청에 넣는 저장소 조각(카드 #1259 PR 2b-1).

오케스트레이터가 스스로 닫은 시도(실행 임대 마감, 보내기 실패)의 결과와 정착이 뒤늦게 닿아 증거가 종결되면,
요청 정의에 늦은 사건(success_evidence, late_failed)을 넣는다. 요청을 바꾸는 것은 정의의 전이뿐이다.
「뒤 시도를 연 적이 없음」은 같은 요청에 이 시도보다 큰 attempt_seq가 있는가로 가린다(design.md:123, 431).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .protocol import DeployStatus
from .statechart import steps
from .statechart.restore import NoTransition

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptLateEvidence:
    """Mixin: late evidence of a closed attempt, applied to the request through the definition."""

    async def _no_later_attempt(self, attempt: dict[str, Any]) -> bool:
        """이 시도보다 뒤 시도를 연 적이 없는가. 순서 칸이 비었으면 견줄 수 없으므로 「있다」로 본다(요청을 닫지 않는 쪽)."""
        if attempt["attempt_seq"] is None:
            return False
        cursor = await self._db.execute(
            "SELECT 1 FROM deploy_attempts WHERE deploy_id = ? AND attempt_seq > ? LIMIT 1",
            (attempt["deploy_id"], attempt["attempt_seq"]),
        )
        return await cursor.fetchone() is None

    async def _apply_late_evidence_unlocked(
        self, attempt: dict[str, Any], leaf: str
    ) -> dict[str, Any]:
        """닫힌 시도의 증거가 종결된 뒤 요청에 늦은 사건을 넣는다. 요청을 바꾸는 것은 정의의 전이뿐이다."""
        deploy_id = attempt["deploy_id"]
        now = _now_iso()
        before = await self._event(deploy_id)
        changed = False
        if leaf == "ev_both":
            no_later = await self._no_later_attempt(attempt)
            if no_later and before is not None and before["status"] == DeployStatus.DEPLOYING.value:
                logger.warning(
                    "늦은 성공의 no_later가 배포 중인 요청에서 참이다(도는 시도 없는 배포 중): deploy_id=%s attempt=%s",
                    deploy_id,
                    attempt["orchestrator_attempt_id"],
                )
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
                    await self._terminalize_sibling_probes_unlocked(
                        deploy_id, "canonical succeeded through late evidence"
                    )
                    return {"status": "success", "deploy_id": deploy_id, "closed_now": True}
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
