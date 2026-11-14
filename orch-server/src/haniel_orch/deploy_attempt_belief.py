"""믿음 글의 코디네이터 조각(카드 #1259 PR 2b-2): 받았다, hello 대조, 끊김의 유예 마감, 「종결을 받았다」 답.

저장소가 정의로 적은 결과를 받아 타이머와 로그와 노드로 가는 답만 맡는다. 답은 판 1 연결(hello의 deploy_protocol이
1 이상)에만 보내고, 옛 노드에는 지금의 deploy_report_ack(ignored) 길을 그대로 둔다(wire-v1 3절).
"""

from __future__ import annotations

import logging
from typing import Any

from .connection_lifecycle import ActiveConnection
from .deploy_attempt_belief_store import ACK_DEADLINE_SEC
from .deploy_attempt_intake import is_legacy
from .protocol import DeployAttemptReceived, DeployTerminalReceived, HeldAttempt

logger = logging.getLogger(__name__)


class DeployAttemptBelief:
    """Coordinator mixin: 믿음 글을 저장소에 넘기고 타이머, 로그, 답을 맡는다."""

    def _permit_deadline_sec(self, node_protocol: int | None) -> float:
        """허가를 보낸 뒤의 첫 마감: 판 1은 닿았는지 모름의 짧은 마감, 판 없음은 지금 실행 임대(wire-v1 5절)."""
        return self._attempt_timeout_sec if is_legacy(node_protocol) else ACK_DEADLINE_SEC

    async def handle_received(self, msg: DeployAttemptReceived) -> None:
        """받았다. 지금 시도의 것만 믿음을 줄 서 있음으로 옮기고 짧은 마감을 멈춘다."""
        result = await self.attempts.record_received(
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            node_id=msg.node_id,
        )
        if result["status"] != "acked":
            logger.debug(
                "deploy_attempt_received ignored: node=%s attempt=%s deploy=%s reason=%s",
                msg.node_id,
                msg.orchestrator_attempt_id,
                msg.deploy_id,
                result.get("reason"),
            )
            return
        logger.info(
            "deploy_attempt_received: node=%s attempt=%s deploy=%s belief %s -> %s",
            msg.node_id,
            msg.orchestrator_attempt_id,
            msg.deploy_id,
            result["belief_previous"],
            result["belief"],
        )
        if result["belief_previous"] == "b_unknown" and result["belief"] == "b_queued":
            self._cancel_timer(f"attempt:{msg.orchestrator_attempt_id}")

    async def reconcile_hello(
        self,
        node_id: str,
        deploy_protocol: int | None,
        held_attempts: list[HeldAttempt] | None,
    ) -> list[dict[str, Any]]:
        """등록 직후, 메시지 고리에 들어가기 전에 그 노드의 판 1 시도를 hello 목록과 견준다(7항).

        목록이 None이면 대조하지 않는다. 시도마다 INFO 로그 한 줄(노드, 시도 번호, 믿음, 있음과 없음)을 남긴다.
        """
        if held_attempts is None:
            return []
        held = {
            item.orchestrator_attempt_id
            for item in held_attempts
            if item.orchestrator_attempt_id is not None
        }
        results = await self.attempts.reconcile_hello(node_id, held)
        for entry in results:
            holds = entry["event"] == "hello_holds"
            logger.info(
                "hello reconcile: node=%s protocol=%s attempt=%s deploy=%s belief=%s %s",
                node_id,
                deploy_protocol,
                entry["orchestrator_attempt_id"],
                entry["deploy_id"],
                entry["belief"],
                "held" if holds else "missing",
            )
            if holds:
                if entry.get("belief_after") == "b_queued":
                    self._cancel_timer(f"attempt:{entry['orchestrator_attempt_id']}")
                continue
            await self._after_store_terminal(node_id, entry["orchestrator_attempt_id"], entry)
        return results

    async def _on_link_down(self, node_id: str) -> None:
        """끊김을 안 때. 줄 서 있는 판 1 시도에 유예 마감을 걸고 타이머를 세운다(wire-v1 5절)."""
        for graced in await self.attempts.mark_link_down(
            node_id, grace_sec=self._attempt_timeout_sec
        ):
            logger.info(
                "grace deadline armed: node=%s attempt=%s deploy=%s until=%s",
                node_id,
                graced["orchestrator_attempt_id"],
                graced["deploy_id"],
                graced["deadline_at"],
            )
            self._schedule_attempt(graced["orchestrator_attempt_id"], graced["deadline_at"])

    async def _answer_terminal(
        self,
        node_id: str,
        *,
        deploy_id: str | None,
        orchestrator_attempt_id: str | None = None,
        node_attempt_id: str | None = None,
        closed_now: bool,
    ) -> bool:
        """「종결을 받았다」(wire-v1 3절). 판 1 연결에만 보낸다. 보냈으면 True."""
        if is_legacy(self._node_protocol(node_id)):
            return False
        connection = self._connections.get(node_id)
        if not isinstance(connection, ActiveConnection):
            return False
        message = DeployTerminalReceived(
            deploy_id=deploy_id,
            orchestrator_attempt_id=orchestrator_attempt_id,
            node_attempt_id=node_attempt_id,
            outcome="closed_now" if closed_now else "already_closed",
        )
        sent = await self._send(node_id, connection.generation, message)
        logger.info(
            "deploy_terminal_received %s: node=%s deploy=%s attempt=%s node_attempt=%s outcome=%s",
            "sent" if sent else "not sent",
            node_id,
            deploy_id,
            orchestrator_attempt_id,
            node_attempt_id,
            message.outcome,
        )
        return sent
