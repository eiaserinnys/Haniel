"""Progress leases and ignored-report observability for deploy attempts."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from .connection_lifecycle import ActiveConnection
from .protocol import DeployProgress, DeployReportAck

logger = logging.getLogger(__name__)

MAX_ATTEMPT_LEASE_MULTIPLIER = 4
DECLARED_BUDGET_SAFETY_FACTOR = 1.5
MAX_DECLARED_ATTEMPT_BUDGET_SEC = 2 * 60 * 60


def attempt_hard_budget_seconds(
    attempt_timeout_sec: float,
    expected_budget_sec: int | None,
) -> float:
    """Preserve the old ×4 floor and cap only node-declared extension."""

    legacy_budget_sec = attempt_timeout_sec * MAX_ATTEMPT_LEASE_MULTIPLIER
    if expected_budget_sec is None:
        return legacy_budget_sec
    declared_budget_sec = min(
        math.ceil(expected_budget_sec * DECLARED_BUDGET_SAFETY_FACTOR),
        MAX_DECLARED_ATTEMPT_BUDGET_SEC,
    )
    return max(legacy_budget_sec, declared_budget_sec)


class DeployAttemptProgress:
    """Coordinator mixin for bounded lease renewal and report acknowledgements."""

    async def handle_progress(self, progress: DeployProgress) -> None:
        """진행 알림은 시도 번호로 견준다(어긋남 5). 글의 시도가 그 요청의 도는 시도이고, 글의 node_id와 연결이
        인증한 node_id와 시도 줄의 node_id가 모두 같을 때만 임대를 늘린다. 허가의 세대는 보지 않으므로 재접속 뒤
        같은 시도의 알림도 받는다(hub가 본문 node_id와 연결의 node_id가 다른 글을 버린다).
        """
        attempt = await self.attempts.get_attempt(progress.orchestrator_attempt_id)
        connection = self._connections.get(progress.node_id)
        if (
            attempt is None
            or attempt["deploy_id"] != progress.deploy_id
            or attempt["node_id"] != progress.node_id
            or not isinstance(connection, ActiveConnection)
        ):
            return
        # 실행 상한은 실제 실행 시작 시각에서 잰다(어긋남 6). 이 알림이 그 시각을 처음 적는 걸음이면
        # 방금 적는 시각(now)에서 잰다. 적기 전 값으로 승인 시각(started_at)을 쓰지 않는다
        now = datetime.now(timezone.utc)
        started = attempt["execution_started_at"]
        base = datetime.fromisoformat(started) if started else now
        hard_deadline = base + timedelta(
            seconds=attempt_hard_budget_seconds(
                self._attempt_timeout_sec,
                attempt["expected_budget_sec"] or progress.expected_budget_sec,
            )
        )
        idle_deadline = now + timedelta(seconds=self._attempt_timeout_sec)
        deadline_at = min(idle_deadline, hard_deadline).isoformat()
        result = await self.attempts.record_progress(
            deploy_id=progress.deploy_id,
            orchestrator_attempt_id=progress.orchestrator_attempt_id,
            node_id=progress.node_id,
            expected_budget_sec=progress.expected_budget_sec,
            deadline_at=deadline_at,
            now=now.isoformat(),
        )
        if result.get("status") == "renewed":
            self._schedule_attempt(
                progress.orchestrator_attempt_id, result["deadline_at"]
            )

    async def _handle_ignored_report(
        self,
        *,
        node_id: str,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        report_type: str,
        summary: str,
    ) -> None:
        attempt = await self.attempts.get_attempt(orchestrator_attempt_id)
        if attempt is None:
            reason = "unknown_attempt"
            outcome = None
        elif attempt["outcome"] != "active":
            reason = "terminal_attempt"
            outcome = attempt["outcome"]
        else:
            reason = "identity_mismatch"
            outcome = attempt["outcome"]
        logger.warning(
            "Ignored deploy report attempt_id=%s outcome=%s report_type=%s "
            "deploy_id=%s generation=%s reason=%s summary=%s",
            orchestrator_attempt_id,
            outcome or "missing",
            report_type,
            deploy_id,
            connection_generation,
            reason,
            summary[:512],
        )
        connection = self._connections.get(node_id)
        if not isinstance(connection, ActiveConnection):
            return
        await self._send(
            node_id,
            connection.generation,
            DeployReportAck(
                deploy_id=deploy_id,
                orchestrator_attempt_id=orchestrator_attempt_id,
                report_type=report_type,
                reason=reason,
                attempt_outcome=outcome,
            ),
        )
