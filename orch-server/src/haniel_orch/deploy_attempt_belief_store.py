"""믿음 글을 적는 저장소 조각(카드 #1259 PR 2b-2): 받았다, 끊김, hello 대조, 믿음별 마감의 복원과 만기.

모든 판단은 받기 함수(`deploy_attempt_intake`)가 (사건, 사실)로 바꾸고 요청 정의의 한 걸음이 적는다. 여기서는 신원 확인과
마감 칸 쓰기, 트랜잭션만 맡는다. 판 없는 시도(node_protocol NULL)는 받았다와 hello 대조와 유예 마감이 없다(wire-v1 4절).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .deploy_attempt_intake import (
    DEADLINE_ACK,
    DEADLINE_GRACE,
    DEADLINE_LEASE,
    DEADLINE_NONE,
    closure_of,
    intake_deadline,
    intake_hello,
    intake_received,
    is_legacy,
)
from .statechart import attempt_steps, steps
from .statechart.restore import NoTransition

logger = logging.getLogger(__name__)

# 닿았는지 모름의 짧은 마감(wire-v1 5절). 노드 핑의 기본값(간격 10초, 답 기다림 10초)보다 길다
ACK_DEADLINE_SEC = 30.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plus(now: datetime, seconds: float) -> str:
    return (now + timedelta(seconds=seconds)).isoformat()


class DeployAttemptBeliefStore:
    """Mixin: 믿음을 옮기는 글과 믿음별 마감을 저장 잠금 안에서 적는다."""

    async def request_belief(self, deploy_id: str) -> str | None:
        return await self._request_belief(deploy_id)

    async def record_received(
        self, *, deploy_id: str, orchestrator_attempt_id: str, node_id: str
    ) -> dict[str, Any]:
        """받았다(deploy_attempt_received). 지금 시도의 것만 믿음을 옮긴다(시도 번호, 요청, 노드가 같고 도는 시도).

        닿았는지 모름에서 줄 서 있음으로 가면 짧은 마감을 멈춘다(마감 종류 none). 닫힌 시도의 것은 버린다.
        """
        async with self._mutation_lock:
            try:
                attempt = await self._attempt(orchestrator_attempt_id)
                if (
                    attempt is None
                    or attempt["deploy_id"] != deploy_id
                    or attempt["node_id"] != node_id
                ):
                    return {"status": "ignored", "reason": "identity_mismatch"}
                intake = intake_received(
                    node_protocol=attempt["node_protocol"], active=attempt["outcome"] == "active"
                )
                if intake is None:
                    reason = "legacy_attempt" if is_legacy(attempt["node_protocol"]) else "terminal_attempt"
                    return {"status": "ignored", "reason": reason}
                try:
                    step = await steps.step_request(self._db, deploy_id, intake.event, facts=intake.facts)
                except NoTransition as exc:
                    exc.warn(intake.event)
                    return {"status": "ignored", "reason": "no_transition"}
                if step.belief == "b_queued" and step.belief_previous == "b_unknown":
                    await attempt_steps.set_deadline(self._db, orchestrator_attempt_id, kind=DEADLINE_NONE)
                await self._db.commit()
                return {
                    "status": "acked",
                    "deploy_id": deploy_id,
                    "orchestrator_attempt_id": orchestrator_attempt_id,
                    "belief_previous": step.belief_previous,
                    "belief": step.belief,
                }
            except Exception:
                await self._db.rollback()
                raise

    async def _active_attempts_of_node(self, node_id: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM deploy_attempts WHERE node_id = ? AND outcome = 'active'", (node_id,)
        )
        return self._rows(cursor, await cursor.fetchall())

    async def mark_link_down(self, node_id: str, *, grace_sec: float) -> list[dict[str, Any]]:
        """오케스트레이터가 끊김을 안다. 판 1 시도마다 link_down을 넣고, 줄 서 있음이면 유예 마감을 건다(wire-v1 5절).

        돌려주는 목록은 유예 마감을 건 시도(타이머를 걸 호출부용)다.
        """
        async with self._mutation_lock:
            try:
                now = _now()
                graced: list[dict[str, Any]] = []
                for attempt in await self._active_attempts_of_node(node_id):
                    if is_legacy(attempt["node_protocol"]):
                        continue
                    deploy_id = attempt["deploy_id"]
                    try:
                        step = await steps.step_request(self._db, deploy_id, "link_down", now=now.isoformat())
                    except NoTransition as exc:
                        exc.warn("link_down")
                        continue
                    if step.belief == "b_queued":
                        deadline_at = _plus(now, grace_sec)
                        await attempt_steps.set_deadline(
                            self._db,
                            attempt["orchestrator_attempt_id"],
                            kind=DEADLINE_GRACE,
                            deadline_at=deadline_at,
                        )
                        graced.append(
                            {
                                "orchestrator_attempt_id": attempt["orchestrator_attempt_id"],
                                "deploy_id": deploy_id,
                                "deadline_at": deadline_at,
                            }
                        )
                await self._db.commit()
                return graced
            except Exception:
                await self._db.rollback()
                raise

    async def reconcile_hello(self, node_id: str, held: set[str] | None) -> list[dict[str, Any]]:
        """다시 붙은 노드의 hello 목록과 그 노드의 판 1 시도를 견준다(design.md:141-145, 7항).

        목록이 None(옛 노드)이면 대조하지 않는다. 시도마다 결과 한 줄을 돌려준다(호출부가 INFO 로그와 타이머를 맡는다).
        """
        async with self._mutation_lock:
            try:
                out: list[dict[str, Any]] = []
                if held is None:
                    return out
                for attempt in await self._active_attempts_of_node(node_id):
                    deploy_id = attempt["deploy_id"]
                    belief = await self._request_belief(deploy_id)
                    intake = intake_hello(
                        node_protocol=attempt["node_protocol"],
                        belief=belief or "",
                        held=held,
                        attempt_id=attempt["orchestrator_attempt_id"],
                        newer=False,
                    )
                    if intake is None:
                        continue
                    entry = {
                        "orchestrator_attempt_id": attempt["orchestrator_attempt_id"],
                        "deploy_id": deploy_id,
                        "belief": belief,
                        "event": intake.event,
                    }
                    if intake.event == "hello_holds":
                        try:
                            step = await steps.step_request(self._db, deploy_id, "hello_holds")
                        except NoTransition as exc:
                            exc.warn("hello_holds")
                            continue
                        if step.belief == "b_queued":
                            await attempt_steps.set_deadline(
                                self._db, attempt["orchestrator_attempt_id"], kind=DEADLINE_NONE
                            )
                        out.append({**entry, "status": "held", "belief_after": step.belief})
                        continue
                    running = belief == "b_running"
                    result = await self._close_attempt_unlocked(
                        attempt,
                        event="hello_missing",
                        facts={},
                        kind="hello_missing",
                        stage="hello",
                        reason="node_restarted" if running else "execution_unknown",
                        error=(
                            "node reconnected without this attempt after it had started"
                            if running
                            else "node reconnected without this attempt before it had started"
                        ),
                        closure=closure_of("hello_missing", belief or ""),
                        move_source=True,
                        node_glyph=False,
                    )
                    out.append({**entry, **result})
                await self._db.commit()
                return out
            except Exception:
                await self._db.rollback()
                raise

    async def expire_deadline(
        self,
        orchestrator_attempt_id: str,
        *,
        expected_deadline_at: str,
        kind: str,
        stage: str,
        reason: str,
        error: str,
    ) -> dict[str, Any]:
        """마감 타이머가 울렸다. 저장된 마감이 타이머 때의 값이고 지났으며, 마감 종류와 믿음이 맞을 때만 닫는다(8항).

        맞지 않으면 낡은 타이머다(stale). 판 없는 시도는 지금처럼 attempt_failed, 판 1은 종류별 사건이다.
        """
        async with self._mutation_lock:
            try:
                attempt = await self._attempt(orchestrator_attempt_id)
                if attempt is None or attempt["outcome"] != "active":
                    return {"status": "ignored"}
                deadline_kind = attempt["deadline_kind"] or DEADLINE_LEASE
                if deadline_kind == DEADLINE_NONE:
                    return {"status": "stale", "deadline_kind": deadline_kind}
                deadline_at = attempt["deadline_at"]
                if deadline_at != expected_deadline_at or datetime.fromisoformat(
                    deadline_at
                ) > _now():
                    return {"status": "deadline_changed", "deadline_at": deadline_at}
                belief = await self._request_belief(attempt["deploy_id"]) or ""
                intake = intake_deadline(
                    node_protocol=attempt["node_protocol"],
                    deadline_kind=deadline_kind,
                    belief=belief,
                    newer=False,
                )
                if intake is None:
                    return {"status": "stale", "deadline_kind": deadline_kind, "belief": belief}
                if intake.event == "attempt_failed":
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt, kind=kind, stage=stage, reason=reason, error=error, self_closed=True
                    )
                else:
                    result = await self._close_attempt_unlocked(
                        attempt,
                        event=intake.event,
                        facts={},
                        kind=kind,
                        stage=stage,
                        reason=reason,
                        error=error,
                        closure=closure_of(intake.event, belief),
                        move_source=True,
                        node_glyph=False,
                    )
                await self._db.commit()
                return {**result, "event": intake.event}
            except Exception:
                await self._db.rollback()
                raise

    async def restore_belief_deadlines(self, *, grace_sec: float) -> list[dict[str, Any]]:
        """재기동 때 도는 시도의 마감을 되살린다(wire-v1 5절). 모든 노드가 끊긴 것으로 본다.

        판 1: 줄 서 있음은 복원 시각부터 유예, 닿았는지 모름은 저장된 짧은 마감과 복원 시각 더하기 30초 가운데 늦은 쪽,
        시작했다는 저장된 실행 임대 그대로. 판 없음은 저장된 마감 그대로. 돌려주는 목록은 타이머를 걸 시도들이다.
        """
        async with self._mutation_lock:
            try:
                now = _now()
                out: list[dict[str, Any]] = []
                for attempt in await self.get_active_attempts():
                    kind = attempt["deadline_kind"] or DEADLINE_LEASE
                    deadline_at = attempt["deadline_at"]
                    if not is_legacy(attempt["node_protocol"]):
                        belief = await self._request_belief(attempt["deploy_id"])
                        if belief == "b_queued":
                            kind, deadline_at = DEADLINE_GRACE, _plus(now, grace_sec)
                        elif belief == "b_unknown":
                            kind = DEADLINE_ACK
                            deadline_at = max(deadline_at, _plus(now, ACK_DEADLINE_SEC))
                        else:
                            kind = DEADLINE_LEASE
                        if (kind, deadline_at) != (attempt["deadline_kind"], attempt["deadline_at"]):
                            await attempt_steps.set_deadline(
                                self._db, attempt["orchestrator_attempt_id"], kind=kind, deadline_at=deadline_at
                            )
                    out.append({**attempt, "deadline_kind": kind, "deadline_at": deadline_at})
                await self._db.commit()
                return out
            except Exception:
                await self._db.rollback()
                raise
