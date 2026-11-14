"""노드 보고(결과, 정착, 증거 회수, deploy_attempt_terminal)를 저장소에 넘기고 뒷일을 하는 코디네이터 조각.

종결 묶음(결과, 정착, 증거 회수)에는 그 시도의 증거가 종결됐을 때(증거 정의의 terminal_ok, terminal_fail) 판 1 연결에
「종결을 받았다」를 보내고, 이미 종결된 뒤 같은 글이 또 오면(dup) 같은 답을 다시 보낸다(wire-v1 3절). 반쪽 묶음에는 보내지
않는다. deploy_attempt_terminal에는 보내지 않는다(wire-v1 2절). 옛 노드에는 지금의 deploy_report_ack(ignored) 길 그대로다.
"""

from __future__ import annotations

from .protocol import DeployAttemptTerminal, DeployResult, ManifestRecoveryEvidence, RepoReconciliation

_SETTLED = frozenset({"terminal_ok", "terminal_fail"})


class DeployAttemptReportHandlers:
    """Coordinator mixin: one handler per node report, then answer, broadcast, and callback."""

    async def _after_bundle(
        self,
        *,
        node_id: str,
        deploy_id: str,
        orchestrator_attempt_id: str,
        connection_generation: str,
        report_type: str,
        summary: str,
        result: dict,
    ) -> None:
        todo = set(result.get("todo") or ())
        if result.get("status") == "duplicate":
            # 이미 종결된 증거에 같은 글이 또 왔다. 판 1 연결에는 같은 답을, 옛 노드에는 지금처럼 ignored 알림을
            if await self._answer_terminal(
                node_id,
                deploy_id=deploy_id,
                orchestrator_attempt_id=orchestrator_attempt_id,
                closed_now=False,
            ):
                return
            await self._handle_ignored_report(
                node_id=node_id,
                deploy_id=deploy_id,
                orchestrator_attempt_id=orchestrator_attempt_id,
                connection_generation=connection_generation,
                report_type=report_type,
                summary=summary,
            )
            return
        if result.get("status") == "ignored":
            await self._handle_ignored_report(
                node_id=node_id,
                deploy_id=deploy_id,
                orchestrator_attempt_id=orchestrator_attempt_id,
                connection_generation=connection_generation,
                report_type=report_type,
                summary=summary,
            )
            return
        if todo & _SETTLED:
            await self._answer_terminal(
                node_id,
                deploy_id=deploy_id,
                orchestrator_attempt_id=orchestrator_attempt_id,
                closed_now=bool(result.get("closed_now")),
            )
        await self._after_store_terminal(node_id, orchestrator_attempt_id, result)

    async def handle_result(self, msg: DeployResult) -> None:
        result = await self.attempts.record_result(
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            status=msg.status,
            error=msg.error,
            duration_ms=msg.duration_ms,
        )
        await self._after_bundle(
            node_id=msg.node_id,
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            report_type="deploy_result",
            summary=f"status={msg.status} duration_ms={msg.duration_ms} error={msg.error!r}",
            result=result,
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
        await self._after_bundle(
            node_id=msg.node_id,
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            report_type="repo_reconciliation",
            summary=f"local_head={msg.local_head} remote_head={msg.remote_head}",
            result=result,
        )

    async def handle_evidence(self, evidence: ManifestRecoveryEvidence) -> None:
        result = await self.attempts.record_recovery_evidence(evidence)
        todo = set(result.get("todo") or ())
        if result.get("status") == "duplicate" or todo & _SETTLED:
            await self._answer_terminal(
                evidence.node_id,
                deploy_id=evidence.deploy_id,
                orchestrator_attempt_id=evidence.orchestrator_attempt_id,
                closed_now=bool(result.get("closed_now")),
            )
        await self._after_store_terminal(
            evidence.node_id, evidence.orchestrator_attempt_id, result
        )

    async def handle_terminal(self, terminal: DeployAttemptTerminal) -> None:
        """「실행하지 않음」은 빚이 아니라 답하지 않는다(wire-v1 2절)."""
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
        for cancelled_id in result.get("cancelled_orchestrator_attempt_ids") or ():
            self._cancel_timer(f"attempt:{cancelled_id}")
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
