"""계획 묻기를 닫는 규칙(저장소 조각, 2b-2): 계획 답의 실패 저널 규칙(10항)과 계획 묻기 닫힘의 믿음 되돌림. 저장소 모듈의 크기를 지키려 새 모듈에 둔다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .deploy_attempt_intake import CLOSURE_NO
from .protocol import DeployPlanProposal
from .statechart import steps
from .statechart.restore import NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptPlanRules:
    """Mixin: 계획 묻기를 닫을 때의 규칙과 믿음 되돌림."""

    async def _plan_failed_journal_unlocked(
        self, probe: dict[str, Any], proposal: DeployPlanProposal, source: str
    ) -> dict[str, Any] | None:
        """자동 탐침의 계획 답에 「아니오」로 닫힌 계보 시도의 실패 저널이 실려 왔으면 허가를 보내지 않고 점검 필요를 세운다(10항).

        조건은 판으로 거르지 않는다(판 없는 요청은 「아니오」 닫힘이 없어 휴면이다).
        """
        is_auto = probe["requested_orchestrator_attempt_id"] is not None or source == "auto"
        linked = proposal.linked_orchestrator_attempt_id
        if not is_auto or linked is None or proposal.journal_status not in ("failed", "verification_failed"):
            return None
        retry = await self._retry_unlocked(probe["deploy_id"])
        if retry is None or linked not in retry["lineage"]:
            return None
        linked_attempt = await self._attempt(linked)
        if linked_attempt is None or linked_attempt["closure"] not in CLOSURE_NO:
            return None
        now = _now_iso()
        await self._terminalize_probe_unlocked(
            {**probe, "status": "proposed"},
            "preflight_fail_closed",
            "proposal",
            "retry_requires_manual_approval",
            f"journal {proposal.journal_attempt_id} records a failure of attempt {linked}, "
            f"which closed as {linked_attempt['closure']} without inspection",
        )
        todo: list[str] = []
        try:
            step = await steps.step_request(
                self._db,
                probe["deploy_id"],
                "plan_failed_journal",
                data={
                    "failed_attempt_id": linked,
                    "stable_source": linked,
                    "marker_created_at": retry["created_at"],
                },
                now=now,
            )
            todo = list(step.todo)
        except NoTransition as exc:
            exc.warn("plan_failed_journal")
        return {
            "status": "terminal",
            "kind": "preflight_fail_closed",
            "reason": "retry_requires_manual_approval",
            "todo": todo,
        }

    async def _close_probe_unlocked(
        self,
        probe: dict[str, Any],
        kind: str,
        stage: str,
        reason: str,
        error: str,
        *,
        source: str,
    ) -> list[str]:
        """계획 묻기를 닫고 믿음을 되돌린다. 점검 필요가 자동 탐침을 막은 것이면 plan_rejected(auto_reject), 그 밖은 plan_closed."""
        await self._terminalize_probe_unlocked(probe, kind, stage, reason, error)
        is_auto = probe["requested_orchestrator_attempt_id"] is not None or source == "auto"
        belief_event = (
            "plan_rejected" if (is_auto and reason == "retry_requires_manual_approval") else "plan_closed"
        )
        return list(await self._settle_probe_belief_unlocked(probe["deploy_id"], belief_event))
