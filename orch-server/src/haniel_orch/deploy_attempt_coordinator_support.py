"""코디네이터의 작은 도우미(탐침 글 만들기, 거절 답, 방송, 연결 세대 확인). 2b-2에서 코디네이터 모듈의 크기를 지키려 옮겼다. 내용은 그대로다."""

from __future__ import annotations

from typing import Any, Literal

from .connection_lifecycle import ActiveConnection
from .protocol import DeployPlanProbe, RejectedDeployAttemptAck


class PlanRejected(RuntimeError):
    """Manual retry preflight ended before begin."""

    def __init__(
        self,
        message: str,
        status_code: int,
        *,
        blocking_self_update: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.blocking_self_update = blocking_self_update
        super().__init__(message)


class DeployAttemptCoordinatorSupport:
    """Mixin: 탐침 글, 거절 답, 방송, 연결 세대 확인 같은 작은 도우미."""

    async def resolve_terminal_canonicals(
        self,
        deploy_ids: set[str],
        *,
        message: str = "canonical became terminal during preflight",
    ) -> None:
        """Release manual waiters after the store terminalizes their probes."""
        for probe_id in list(self._probe_context):
            probe = await self.attempts.get_probe(probe_id)
            if (
                probe is not None
                and probe["deploy_id"] in deploy_ids
                and probe["status"] == "terminal"
            ):
                self._resolve_probe(probe_id, PlanRejected(message, 409))

    async def cancel_superseded_attempts(
        self, deploy_id: str, orchestrator_attempt_ids: list[str]
    ) -> None:
        """Release timers and preflight waiters superseded by node-owned success."""
        await self.resolve_terminal_canonicals(
            {deploy_id}, message="canonical succeeded through node-owned deployment"
        )
        for orchestrator_attempt_id in orchestrator_attempt_ids:
            self._cancel_timer(f"attempt:{orchestrator_attempt_id}")

    async def _requested_id(self, probe_id: str) -> str | None:
        for probe in await self.attempts.get_active_probes():
            if probe["probe_id"] == probe_id:
                return probe["requested_orchestrator_attempt_id"]
        return None

    def _probe_message(self, probe: dict) -> DeployPlanProbe:
        return DeployPlanProbe(
            probe_id=probe["probe_id"],
            connection_generation=probe["connection_generation"],
            deploy_id=probe["deploy_id"],
            node_id=probe["node_id"],
            repo=probe["repo"],
            branch=probe["branch"],
            target_head=probe["target_head"],
            source_orchestrator_attempt_id=probe["source_orchestrator_attempt_id"],
            retry_lineage=list(__import__("json").loads(probe["retry_lineage_json"])),
            expected_manifest_identity=probe["expected_manifest_identity"],
            expected_manifest_digest=probe["expected_manifest_digest"],
            deadline_at=probe["deadline_at"],
            requested_orchestrator_attempt_id=probe[
                "requested_orchestrator_attempt_id"
            ],
        )

    async def _close_unsent_attempt(
        self, node_id: str, orchestrator_attempt_id: str
    ) -> None:
        """허가가 노드에 닿지 못하면 시도를 그 자리에서 실패로 닫는다(어긋남 9)."""
        result = await self.attempts.fail_attempt_not_sent(orchestrator_attempt_id)
        await self._after_store_terminal(node_id, orchestrator_attempt_id, result)

    async def _send_auto_rejection(
        self,
        node_id: str,
        deploy_id: str,
        requested: str,
        generation: str,
        error: Literal["retry_requires_manual_approval", "self_update_pending"],
    ) -> None:
        await self._send(
            node_id,
            generation,
            RejectedDeployAttemptAck(
                accepted=False,
                requested_orchestrator_attempt_id=requested,
                deploy_id=deploy_id,
                connection_generation=generation,
                error=error,
            ),
        )

    async def _broadcast_status(self, event: dict, status: str) -> None:
        await self._broadcast(
            {
                "type": "status_change",
                "deploy_id": event["deploy_id"],
                "status": status,
                "node_id": event["node_id"],
            }
        )

    def _resolve_probe(self, probe_id: str, error: Exception) -> None:
        context = self._probe_context.pop(probe_id, None)
        self._cancel_timer(f"probe:{probe_id}")
        if context is not None and context[2] is not None and not context[2].done():
            context[2].set_exception(error)

    def _require_generation(self, node_id: str) -> str:
        state = self._connections.get(node_id)
        if not (
            isinstance(state, ActiveConnection)
            and self._connection_present(
                node_id, state.generation, state.connection_token
            )
        ):
            raise PlanRejected("node not connected", 503)
        return state.generation

    @staticmethod
    def _plan_rejected(message: str, status_code: int) -> PlanRejected:
        return PlanRejected(message, status_code)
