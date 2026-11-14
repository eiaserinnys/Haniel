"""허브의 요청 줄 흐름(새 커밋 알림, 보류 자기 갱신 되살림, 노드 스스로 한 배포 보고, 푸시, 밀어내기, 이름 바뀐 노드 정리). 2b-2에서 허브 모듈의 크기를 지키려 옮겼다. 내용은 그대로다."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .protocol import ChangeNotification, DeployResult, DeployStatus, NodeDeployReport

logger = logging.getLogger(__name__)


class HubCanonicalFlows:
    """Mixin: 요청 줄을 바꾸는 허브 흐름과 그 방송, 푸시."""

    async def _handle_change_notification(self, msg: ChangeNotification) -> None:
        """Process a ChangeNotification: store + supersede older PENDING + broadcast.

        Generation-time supersede is the primary gate for "newest PENDING per
        (node, repo, branch)" — older PENDING entries for the same group are
        auto-rejected with reject_reason='superseded by ${msg.deploy_id}'.
        DEPLOYING is preserved because generation-time supersede mutates only
        canonical rows that are still PENDING.
        approve-time supersede in api.approve_deploy/approve_all remains as a
        defensive secondary gate.
        """
        is_self_update = (
            msg.is_self_update
            if msg.is_self_update is not None
            else msg.repo.casefold() == "haniel"
        )
        inserted = await self._store.create_deploy_event(
            deploy_id=msg.deploy_id,
            node_id=msg.node_id,
            repo=msg.repo,
            branch=msg.branch,
            commits=msg.commits,
            affected_services=msg.affected_services,
            diff_stat=msg.diff_stat,
            detected_at=msg.detected_at,
            target_head=msg.target_head,
            deployment_kind=msg.deployment_kind,
            expected_manifest_identity=msg.expected_manifest_identity,
            expected_manifest_digest=msg.expected_manifest_digest,
            is_self_update=is_self_update,
        )
        if not inserted:
            await self._revive_postponed_self_update(msg)
            return

        inserted_event = await self._store.get_deploy_event(msg.deploy_id)
        if inserted_event is None:
            raise RuntimeError("inserted canonical disappeared")
        if inserted_event["status"] != DeployStatus.PENDING.value:
            await self.broadcast_to_dashboards(
                {
                    "type": "status_change",
                    "deploy_id": msg.deploy_id,
                    "status": DeployStatus.REJECTED.value,
                    "node_id": msg.node_id,
                    "reject_reason": inserted_event["reject_reason"],
                }
            )
            return

        superseded_rows = await self._store.get_deploys_superseded_by(msg.deploy_id)
        await self.deploy_coordinator.resolve_terminal_canonicals(
            {row["deploy_id"] for row in superseded_rows},
            message="canonical superseded during preflight",
        )
        for superseded in superseded_rows:
            await self.broadcast_to_dashboards(
                {
                    "type": "status_change",
                    "deploy_id": superseded["deploy_id"],
                    "status": DeployStatus.REJECTED.value,
                    "node_id": superseded["node_id"],
                    "reject_reason": superseded["reject_reason"],
                }
            )

        await self.broadcast_to_dashboards(
            {
                "type": "new_pending",
                "deploy_id": msg.deploy_id,
                "node_id": msg.node_id,
                "repo": msg.repo,
                "branch": msg.branch,
                "detected_at": msg.detected_at,
            }
        )

        # Fire-and-forget push notification
        self._spawn_push(
            title=f"배포 대기: {msg.repo}",
            body=f"{msg.node_id}에서 {msg.repo} ({msg.branch}) 변경 감지",
            data={
                "deploy_id": msg.deploy_id,
                "type": "new_pending",
                "node_id": msg.node_id,
            },
        )

    async def _revive_postponed_self_update(self, msg: ChangeNotification) -> None:
        """Return a due postponed self-update to pending on a repeat notification.

        Exceptions are logged and swallowed: a raise here would disconnect the
        node that sent the notification.
        """
        try:
            if not await self._store.reopen_postponed_self_update(msg.deploy_id):
                return
            await self.broadcast_to_dashboards(
                {
                    "type": "new_pending",
                    "deploy_id": msg.deploy_id,
                    "node_id": msg.node_id,
                    "repo": msg.repo,
                    "branch": msg.branch,
                    "detected_at": msg.detected_at,
                }
            )
        except Exception:
            logger.exception(
                "Failed to revive postponed self-update %s", msg.deploy_id
            )

    async def _handle_deploy_result(self, msg: DeployResult) -> None:
        """Delegate result correlation to the durable attempt coordinator."""
        await self.deploy_coordinator.handle_result(msg)

    async def _handle_node_deploy_report(self, msg: NodeDeployReport) -> None:
        """Reconcile an attempt-independent node-owned deployment report.

        종결 보고(성공, 실패)에는 언제나 「종결을 받았다」로 답한다(design.md:120, 판 1 연결만). 요청 줄이 없거나 닫혀 있거나
        이미 적은 노드 시도여도 같다. outcome은 이 글이 노드 시도나 요청을 닫았으면 closed_now, 아니면 already_closed다.
        """
        result = await self._store.record_node_deploy_report(msg)
        if msg.phase in ("succeeded", "failed"):
            await self.deploy_coordinator._answer_terminal(
                msg.node_id,
                deploy_id=msg.deploy_id,
                node_attempt_id=msg.node_attempt_id,
                closed_now=bool(result.get("closed_now")),
            )
        if result["status"] in {"ignored", "started"}:
            return
        cancelled = result.get("cancelled_orchestrator_attempt_ids", [])
        if cancelled:
            await self.deploy_coordinator.cancel_superseded_attempts(
                msg.deploy_id, cancelled
            )
        await self.broadcast_to_dashboards(
            {
                "type": "status_change",
                "deploy_id": msg.deploy_id,
                "status": result["canonical_status"],
                "node_id": msg.node_id,
            }
        )
        if result["status"] == "success" or (
            result["status"] == "failed"
            and result["canonical_status"] == DeployStatus.PENDING.value
        ):
            await self._on_deploy_terminal({**result, "node_id": msg.node_id})

    async def _on_deploy_terminal(self, result: dict[str, Any]) -> None:
        """Preserve terminal push UX without making it a state authority."""
        if result["status"] not in {"success", "failed"}:
            return
        status_text = "성공" if result["status"] == "success" else "실패"
        self._spawn_push(
            title=f"배포 {status_text}: {result['node_id']}",
            body=f"{result['node_id']}의 배포가 {status_text}했습니다",
            data={
                "deploy_id": result["deploy_id"],
                "type": "status_change",
                "status": result["status"],
            },
        )

    def _spawn_push(self, title: str, body: str, data: dict[str, Any]) -> None:
        """Spawn a fire-and-forget push task. Task ref is held to prevent GC."""
        task = asyncio.create_task(self._fire_push(title, body, data))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _fire_push(self, title: str, body: str, data: dict[str, Any]) -> None:
        """Send push notification. Failures are logged and ignored."""
        try:
            await self._push.notify(title, body, data)
        except Exception as e:
            logger.warning(f"Push notification failed: {e}")

    async def supersede_pending(
        self, node_id: str, repo: str, branch: str
    ) -> list[str]:
        """Reject pending rows older than the transaction-time latest canonical.

        Used by approve_deploy/approve_all so that approving a newer deploy
        automatically supersedes older PENDING deploys in the same branch.
        Each superseded deploy gets status=REJECTED + reject_reason
        ='superseded by ${latest_deploy_id}' and a status_change broadcast
        with the reject_reason field.

        Returns the list of superseded deploy_ids. Generation-time creation is
        already transactional; this remains the approval-time defensive gate.
        """
        rejected = await self._store.supersede_pending_for_branch(node_id, repo, branch)
        await self.deploy_coordinator.resolve_terminal_canonicals(
            {event["deploy_id"] for event in rejected},
            message="canonical superseded during preflight",
        )
        superseded: list[str] = []
        for ev in rejected:
            did = ev["deploy_id"]
            reason = ev["reject_reason"]
            await self.broadcast_to_dashboards(
                {
                    "type": "status_change",
                    "deploy_id": did,
                    "status": DeployStatus.REJECTED.value,
                    "node_id": node_id,
                    "reject_reason": reason,
                }
            )
            superseded.append(did)
        return superseded

    async def cleanup_pending_for_renamed_nodes(self) -> list[str]:
        """Reject PENDING deploys for stale node IDs replaced by a new name.

        A node rename changes the configured node_id, so deploy_id changes too
        and normal (node, repo, branch) supersede cannot connect the old and
        new rows. If exactly one connected node reports a hostname, any other
        stored node_id for that hostname is treated as a stale previous name.
        """
        by_hostname: dict[str, list[str]] = {}
        for node in self._registry.get_connected_nodes():
            hostname = node.hello.hostname
            if not hostname:
                continue
            by_hostname.setdefault(hostname, []).append(node.node_id)

        rejected_ids: list[str] = []
        for hostname, connected_ids in by_hostname.items():
            if len(connected_ids) != 1:
                continue
            active_node_id = connected_ids[0]
            known_node_ids = await self._store.get_node_ids_by_hostname(hostname)
            stale_node_ids = [
                node_id
                for node_id in known_node_ids
                if node_id != active_node_id and node_id not in connected_ids
            ]
            if not stale_node_ids:
                continue

            reason = f"node renamed to {active_node_id}"
            rejected = await self._store.reject_pending_deploys_for_nodes(
                stale_node_ids, reason
            )
            await self.deploy_coordinator.resolve_terminal_canonicals(
                {event["deploy_id"] for event in rejected},
                message="canonical node identity changed during preflight",
            )
            for event in rejected:
                await self.broadcast_to_dashboards(
                    {
                        "type": "status_change",
                        "deploy_id": event["deploy_id"],
                        "status": DeployStatus.REJECTED.value,
                        "node_id": event["node_id"],
                        "reject_reason": reason,
                    }
                )
                rejected_ids.append(event["deploy_id"])

        return rejected_ids
