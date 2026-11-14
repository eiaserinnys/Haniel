"""노드 보고(결과, 정착, 증거 회수, deploy_attempt_terminal)를 저장소에 넘기고 뒷일을 하는 코디네이터 조각."""

from __future__ import annotations

from .protocol import DeployAttemptTerminal, DeployResult, ManifestRecoveryEvidence, RepoReconciliation


class DeployAttemptReportHandlers:
    """Coordinator mixin: one handler per node report, then broadcast and callback."""

    async def handle_result(self, msg: DeployResult) -> None:
        result = await self.attempts.record_result(
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            status=msg.status,
            error=msg.error,
            duration_ms=msg.duration_ms,
        )
        if result.get("status") == "ignored":
            await self._handle_ignored_report(
                node_id=msg.node_id,
                deploy_id=msg.deploy_id,
                orchestrator_attempt_id=msg.orchestrator_attempt_id,
                connection_generation=msg.connection_generation,
                report_type="deploy_result",
                summary=(
                    f"status={msg.status} duration_ms={msg.duration_ms} "
                    f"error={msg.error!r}"
                ),
            )
            return
        await self._after_store_terminal(
            msg.node_id, msg.orchestrator_attempt_id, result
        )

    async def handle_settled(self, msg: RepoReconciliation) -> None:
        if msg.orchestrator_attempt_id is None or msg.connection_generation is None:
            return
        result = await self.attempts.record_settled(
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            local_head=msg.local_head,
            remote_head=msg.remote_head,
        )
        if result.get("status") == "ignored":
            await self._handle_ignored_report(
                node_id=msg.node_id,
                deploy_id=msg.deploy_id,
                orchestrator_attempt_id=msg.orchestrator_attempt_id,
                connection_generation=msg.connection_generation,
                report_type="repo_reconciliation",
                summary=f"local_head={msg.local_head} remote_head={msg.remote_head}",
            )
            return
        await self._after_store_terminal(
            msg.node_id, msg.orchestrator_attempt_id, result
        )

    async def handle_evidence(self, evidence: ManifestRecoveryEvidence) -> None:
        result = await self.attempts.record_recovery_evidence(evidence)
        await self._after_store_terminal(
            evidence.node_id, evidence.orchestrator_attempt_id, result
        )

    async def handle_terminal(self, terminal: DeployAttemptTerminal) -> None:
        result = await self.attempts.record_node_terminal(terminal)
        if result.get("status") == "ignored":
            return
        await self._after_store_terminal(
            result["node_id"],
            terminal.orchestrator_attempt_id,
            result,
        )

    async def _after_store_terminal(
        self, node_id: str, orchestrator_attempt_id: str, result: dict
    ) -> None:
        if result.get("status") == "late_evidence":
            # 닫힌 시도의 늦은 증거. 요청이 바뀌었으면(점검 필요가 섰거나 꼬리표가 남았다) 화면만 맞춘다.
            # 노드에는 받아들인 결과와 정착에 보내는 것과 같은 것(없음)을 보낸다
            if result.get("request_changed") and result.get("request_status"):
                await self._broadcast(
                    {
                        "type": "status_change",
                        "deploy_id": result["deploy_id"],
                        "status": result["request_status"],
                        "node_id": node_id,
                    }
                )
            return
        if result.get("status") not in {"success", "failed", "superseded"}:
            return
        await self.resolve_terminal_canonicals(
            {result["deploy_id"]},
            message="attempt lineage changed during preflight",
        )
        self._cancel_timer(f"attempt:{orchestrator_attempt_id}")
        status = (
            "success"
            if result["status"] == "success"
            else ("rejected" if result["status"] == "superseded" else "pending")
        )
        await self._broadcast(
            {
                "type": "status_change",
                "deploy_id": result["deploy_id"],
                "status": status,
                "node_id": node_id,
            }
        )
        if self._terminal_callback is not None:
            await self._terminal_callback(
                {
                    **result,
                    "node_id": node_id,
                    "orchestrator_attempt_id": orchestrator_attempt_id,
                }
            )
