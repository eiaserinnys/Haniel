"""노드 보고(결과, 정착, 증거 회수, deploy_attempt_terminal)를 시도 줄에 적는 저장소 조각."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .protocol import DeployAttemptTerminal, ManifestRecoveryEvidence
from .statechart import attempt_steps
from .statechart.restore import NoTransition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptReportStore:
    """Mixin that records node reports under the store lock."""

    async def _record_evidence_unlocked(
        self,
        attempt: dict[str, Any],
        event: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """성공 쪽 증거 사건 하나를 넣고, 성공으로 끝났으면 요청 줄도 닫는다."""
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
        if step.leaf == "ev_both":
            return await self._close_request_after_success(attempt)
        return {"status": "recorded"}

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
                attempt = await self._current_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                if attempt["execution_mode"] != "execute":
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="execution_mode_mismatch",
                        stage="message_validation",
                        reason="result_forbidden_for_mode",
                        error="raw DeployResult is forbidden for evidence_recovery mode",
                    )
                elif status == "failed":
                    result = await self._fail_attempt_unlocked(
                        attempt={**attempt, "duration_ms": duration_ms},
                        kind="deploy_result_failed",
                        stage="execution",
                        reason="deploy_result_failed",
                        error=error or "deployment operation failed",
                        extra={
                            "result_status": status,
                            "result_error": error,
                            "duration_ms": duration_ms,
                        },
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
                attempt = await self._current_attempt(
                    deploy_id, orchestrator_attempt_id, connection_generation
                )
                if attempt is None:
                    return {"status": "ignored"}
                settled_data = {"local_head": local_head, "remote_head": remote_head}
                if attempt["execution_mode"] != "execute":
                    result = await self._record_evidence_unlocked(
                        attempt, "settled_noted", data=settled_data
                    )
                elif local_head != attempt["target_head"]:
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="settled_head_mismatch",
                        stage="reconciliation",
                        reason="settled_head_mismatch",
                        error="settled HEAD mismatch: "
                        f"local={local_head} remote={remote_head} target={attempt['target_head']}",
                        extra={
                            "settled_local_head": local_head,
                            "settled_remote_head": remote_head,
                            "settled_at": _now_iso(),
                        },
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
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    evidence.deploy_id,
                    evidence.orchestrator_attempt_id,
                    evidence.connection_generation,
                )
                if attempt is None:
                    return {"status": "ignored"}
                if attempt["execution_mode"] != "evidence_recovery":
                    result = await self._fail_attempt_unlocked(
                        attempt=attempt,
                        kind="execution_mode_mismatch",
                        stage="message_validation",
                        reason="evidence_forbidden_for_mode",
                        error="ManifestRecoveryEvidence is forbidden for execute mode",
                    )
                else:
                    error = await self._validate_recovery_evidence(attempt, evidence)
                    if error:
                        result = await self._fail_attempt_unlocked(
                            attempt=attempt,
                            kind="recovery_evidence_invalid",
                            stage="evidence_validation",
                            reason="recovery_evidence_invalid",
                            error=error,
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
        """Apply the only node-authorized terminal to its exact active owner."""
        async with self._mutation_lock:
            try:
                attempt = await self._current_attempt(
                    terminal.deploy_id,
                    terminal.orchestrator_attempt_id,
                    terminal.connection_generation,
                )
                if attempt is None:
                    return {"status": "ignored"}
                result = await self._fail_attempt_unlocked(
                    attempt=attempt,
                    kind=terminal.kind,
                    stage=terminal.stage,
                    reason=terminal.reason,
                    error=terminal.error,
                )
                await self._db.commit()
                return {**result, "node_id": attempt["node_id"]}
            except Exception:
                await self._db.rollback()
                raise
