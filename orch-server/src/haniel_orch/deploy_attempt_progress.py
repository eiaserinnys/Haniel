"""Progress leases and ignored-report observability for deploy attempts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .connection_lifecycle import ActiveConnection
from .protocol import DeployProgress, DeployReportAck

logger = logging.getLogger(__name__)

MAX_ATTEMPT_LEASE_MULTIPLIER = 4


class DeployAttemptProgress:
    """Coordinator mixin for bounded lease renewal and report acknowledgements."""

    async def handle_progress(self, progress: DeployProgress) -> None:
        attempt = await self.attempts.get_attempt(progress.orchestrator_attempt_id)
        connection = self._connections.get(progress.node_id)
        if (
            attempt is None
            or attempt["node_id"] != progress.node_id
            or not isinstance(connection, ActiveConnection)
            or connection.generation != progress.connection_generation
        ):
            return
        started_at = datetime.fromisoformat(attempt["started_at"])
        hard_deadline = started_at + timedelta(
            seconds=self._attempt_timeout_sec * MAX_ATTEMPT_LEASE_MULTIPLIER
        )
        idle_deadline = datetime.now(timezone.utc) + timedelta(
            seconds=self._attempt_timeout_sec
        )
        deadline_at = min(idle_deadline, hard_deadline).isoformat()
        result = await self.attempts.renew_active_attempt(
            deploy_id=progress.deploy_id,
            orchestrator_attempt_id=progress.orchestrator_attempt_id,
            connection_generation=progress.connection_generation,
            deadline_at=deadline_at,
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
