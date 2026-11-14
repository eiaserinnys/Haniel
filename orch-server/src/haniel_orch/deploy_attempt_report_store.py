"""노드 보고(결과, 정착, 증거 회수, deploy_attempt_terminal)를 시도 줄에 적는 저장소 조각.

결과와 정착과 증거 회수는 시도가 닫힌 뒤에 와도 그 시도의 증거로 적는다(design.md:119). 증거가 성공이나 실패로 종결되면
요청에 늦은 사건을 넣는다. 이미 종결된 증거에 같은 글이 또 오면 아무것도 쓰지 않고 dup 기호만 돌려준다(design.md:81).
결과 모양의 todo(증거 정의의 terminal_ok, terminal_fail, dup)와 closed_now(이 글이 시도나 요청을 닫았는가)로 호출부가
판 1 연결에 「종결을 받았다」를 보낸다. deploy_attempt_terminal의 늦은 글은 지금처럼 ignored다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .deploy_attempt_intake import CLOSURE_NOT_EXECUTED, intake_terminal
from .protocol import DeployAttemptTerminal, ManifestRecoveryEvidence
from .statechart import attempt_steps, steps
from .statechart.restore import NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptReportStore:
    """Mixin that records node reports under the store lock."""

    async def _mark_started_unlocked(self, attempt: dict[str, Any], now: str) -> None:
        """실제 실행의 증거가 닿았다. 시작 시각을 처음 한 번 적고, 도는 시도면 믿음을 시작했다로 옮긴다."""
        await attempt_steps.mark_execution_started(self._db, attempt["orchestrator_attempt_id"], now)
        if attempt["outcome"] != "active":
            return
        try:
            await steps.step_request(self._db, attempt["deploy_id"], "started", now=now)
        except NoTransition as exc:
            exc.warn("started")

    async def _record_evidence_unlocked(
        self,
        attempt: dict[str, Any],
        event: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """증거 사건 하나를 넣는다. 도는 시도의 성공 종결은 요청을 닫고, 닫힌 시도의 종결은 늦은 사건으로 요청에 넣는다."""
        try:
            step = await attempt_steps.step_attempt(
                self._db,
                attempt["orchestrator_attempt_id"],
                event,
                data=data,
            )
        except NoTransition as exc:
            exc.warn(event)
            return {"status": "ignored"}
        todo = list(step.todo)
        if "dup" in todo:
            return {"status": "duplicate", "deploy_id": attempt["deploy_id"], "todo": todo, "closed_now": False}
        if attempt["outcome"] != "active":
            late = await self._apply_late_evidence_unlocked(attempt, step.leaf)
            return {**late, "todo": todo}
        if step.leaf == "ev_both":
            closed = await self._close_request_after_success(attempt)
            return {**closed, "todo": todo}
        return {"status": "recorded", "todo": todo, "closed_now": False}

    async def _late_fail_unlocked(
        self, attempt: dict[str, Any], *, reason: str, error: str, extra: dict[str, Any]
    ) -> dict[str, Any]:
        """닫힌 시도에 닿은 실패 증거(실패 결과, 어긋난 정착, 어긋난 증거 회수). 닫힘 칸은 그대로고 증거만 종결한다."""
        return await self._record_evidence_unlocked(
            attempt,
            "fail",
            data={"kind": None, "stage": None, "reason": reason, "error": error, "extra": extra},
        )

    async def record_result(
        self,
        *,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        status: str,
        error: str | None,
        duration_ms: int | None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                attempt = await self._owned_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                active = attempt["outcome"] == "active"
                failed_extra = {
                    "result_status": status,
                    "result_error": error,
                    "duration_ms": duration_ms,
                }
                if attempt["execution_mode"] != "execute":
                    if not active:
                        return {"status": "ignored"}  # 닫힌 증거 회수 시도에 온 결과는 모순되는 글이다
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="execution_mode_mismatch",
                        stage="message_validation",
                        reason="result_forbidden_for_mode",
                        error="raw DeployResult is forbidden for evidence_recovery mode",
                    )
                    await self._db.commit()
                    return result
                # 결과는 실제 실행의 증거다. 닫힌 시도에도 사실만 적는다(임대를 늘리거나 시도를 되살리지 않는다)
                await self._mark_started_unlocked(attempt, _now_iso())
                if status == "failed" and active:
                    result = await self._fail_attempt_unlocked(
                        attempt={**attempt, "duration_ms": duration_ms},
                        kind="deploy_result_failed",
                        stage="execution",
                        reason="deploy_result_failed",
                        error=error or "deployment operation failed",
                        extra=failed_extra,
                    )
                elif status == "failed":
                    result = await self._late_fail_unlocked(
                        attempt,
                        reason="deploy_result_failed",
                        error=error or "deployment operation failed",
                        extra=failed_extra,
                    )
                else:
                    result = await self._record_evidence_unlocked(
                        attempt,
                        "result_ok",
                        data={"duration_ms": duration_ms},
                    )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def record_settled(
        self,
        *,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        local_head: str,
        remote_head: str,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            try:
                attempt = await self._owned_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                settled_data = {"local_head": local_head, "remote_head": remote_head}
                mismatch_error = (
                    "settled HEAD mismatch: "
                    f"local={local_head} remote={remote_head} target={attempt['target_head']}"
                )
                mismatch_extra = {
                    "settled_local_head": local_head,
                    "settled_remote_head": remote_head,
                    "settled_at": _now_iso(),
                }
                if attempt["execution_mode"] != "execute":
                    # 증거 회수 방식 시도의 정착은 실행 시작으로 적지 않는다(design.md 68)
                    result = await self._record_evidence_unlocked(
                        attempt, "settled_noted", data=settled_data
                    )
                    await self._db.commit()
                    return result
                # 실행 방식의 정착은 실제 실행의 증거다. 닫힌 시도에도 사실만 적는다
                await self._mark_started_unlocked(attempt, _now_iso())
                if local_head != attempt["target_head"] and attempt["outcome"] == "active":
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="settled_head_mismatch",
                        stage="reconciliation",
                        reason="settled_head_mismatch",
                        error=mismatch_error,
                        extra=mismatch_extra,
                    )
                elif local_head != attempt["target_head"]:
                    result = await self._late_fail_unlocked(
                        attempt,
                        reason="settled_head_mismatch",
                        error=mismatch_error,
                        extra=mismatch_extra,
                    )
                else:
                    result = await self._record_evidence_unlocked(
                        attempt, "settled_ok", data=settled_data
                    )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def record_recovery_evidence(
        self, evidence: ManifestRecoveryEvidence
    ) -> dict[str, Any]:
        """증거 회수 보고. 닫힌 시도의 늦은 보고도 증거로 적는다(2b-2 6항): 검증을 통과하면 성공 종결, 아니면 실패 종결."""
        async with self._mutation_lock:
            try:
                attempt = await self._owned_attempt(
                    evidence.deploy_id,
                    evidence.orchestrator_attempt_id,
                    evidence.connection_generation,
                )
                if attempt is None:
                    return {"status": "ignored"}
                active = attempt["outcome"] == "active"
                if attempt["execution_mode"] != "evidence_recovery":
                    if not active:
                        return {"status": "ignored"}  # 닫힌 실행 시도에 온 증거 회수는 모순되는 글이다
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="execution_mode_mismatch",
                        stage="message_validation",
                        reason="evidence_forbidden_for_mode",
                        error="ManifestRecoveryEvidence is forbidden for execute mode",
                    )
                else:
                    error = await self._validate_recovery_evidence(attempt, evidence)
                    if error and active:
                        result = await self._fail_attempt_unlocked(
                            attempt=attempt,
                            kind="recovery_evidence_invalid",
                            stage="evidence_validation",
                            reason="recovery_evidence_invalid",
                            error=error,
                        )
                    elif error:
                        result = await self._late_fail_unlocked(
                            attempt, reason="recovery_evidence_invalid", error=error, extra={}
                        )
                    else:
                        result = await self._record_evidence_unlocked(
                            attempt, "recovered"
                        )
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def record_node_terminal(
        self, terminal: DeployAttemptTerminal
    ) -> dict[str, Any]:
        """노드의 「실행하지 않음」. 판 1 시도가 시작하기 전이면 실행하지 않음 닫힘(점검 필요를 세우지 않음, source를 옮기지 않음),
        시작했다 뒤나 판 없는 시도면 지금처럼 실패 닫힘이다(4e). 닫힌 시도에 온 것은 ignored다."""
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    terminal.deploy_id,
                    terminal.orchestrator_attempt_id,
                    terminal.connection_generation,
                )
                if attempt is None:
                    return {"status": "ignored"}
                belief = await self._request_belief(attempt["deploy_id"]) or ""
                intake = intake_terminal(node_protocol=attempt["node_protocol"], belief=belief, newer=False)
                if intake.event == "declined":
                    result = await self._close_attempt_unlocked(
                        attempt,
                        event="declined",
                        facts={},
                        kind=terminal.kind,
                        stage=terminal.stage,
                        reason=terminal.reason,
                        error=terminal.error,
                        closure=CLOSURE_NOT_EXECUTED,
                        move_source=False,
                        node_glyph=True,
                    )
                else:
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind=terminal.kind,
                        stage=terminal.stage,
                        reason=terminal.reason,
                        error=terminal.error,
                    )
                await self._db.commit()
                return {**result, "node_id": attempt["node_id"], "event": intake.event}
            except Exception:
                await self._db.rollback()
                raise
