"""WebSocket hub — routes messages between nodes and dashboards."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from .deploy_attempt_coordinator import DeployAttemptCoordinator
from .event_store import EventStore
from .node_disconnect_workflow import NodeDisconnectWorkflow
from .node_registry import NodeRegistry
from .push import NullPushService, PushService
from .protocol import (
    ChangeNotification,
    DeployAttemptTerminal,
    DeployPlanProposal,
    DeployProgress,
    DeployResult,
    DeployStatus,
    ManifestRecoveryEvidence,
    NodeDeployReport,
    NodeHello,
    NodeStatus,
    OrchestratorMessage,
    RepoReconciliation,
    ServiceCommandResult,
    parse_node_message,
)
from .reconciliation import RepoReconciler

logger = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    """In-flight service command tracked by the hub."""

    node_id: str
    service_name: str
    action: str
    timeout_task: asyncio.Task[None]


class WebSocketHub:
    """Central hub managing node and dashboard WebSocket connections."""

    def __init__(
        self,
        registry: NodeRegistry,
        store: EventStore,
        token: str,
        push_service: PushService | None = None,
        auth_bearer_token: str = "",
        command_timeout_sec: float = 30.0,
        deploy_timeout_sec: float = 300.0,
    ) -> None:
        self._registry = registry
        self._store = store
        self._token = token
        self._push: PushService = push_service or NullPushService()
        self._auth_bearer_token = auth_bearer_token
        self._dashboard_connections: set[WebSocket] = set()
        self._heartbeat_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        # In-flight service commands: command_id → PendingCommand.
        # Single source of truth for command tracking — pop transfers ownership for race-safety.
        # Assumes a single asyncio event loop (no cross-thread access). For multi-loop/thread
        # deployments, an asyncio.Lock would be required.
        self._command_timeout_sec = command_timeout_sec
        self._pending_commands: dict[str, PendingCommand] = {}
        self._deploy_timeout_sec = deploy_timeout_sec
        self._repo_reconciler = RepoReconciler(store, self.broadcast_to_dashboards)
        self.deploy_coordinator = DeployAttemptCoordinator(
            store,
            self.send_to_node_generation,
            lambda event: self.broadcast_to_dashboards(event),
            self._registry.has_connection_identity,
            probe_timeout_sec=min(deploy_timeout_sec, 30.0),
            attempt_timeout_sec=deploy_timeout_sec,
            terminal_callback=self._on_deploy_terminal,
        )
        self._disconnects = NodeDisconnectWorkflow(
            self._registry,
            self.deploy_coordinator,
            self._on_current_node_disconnect,
        )

    @property
    def registry(self) -> NodeRegistry:
        """Public accessor for the node registry."""
        return self._registry

    def _verify_dashboard_token(self, token: str | None) -> bool:
        """Verify dashboard WebSocket token. Empty auth_bearer_token = auth disabled."""
        if not self._auth_bearer_token:
            return True  # auth disabled — backward compat for tests
        if not token:
            return False
        return hmac.compare_digest(token, self._auth_bearer_token)

    async def handle_node_ws(self, websocket: WebSocket) -> None:
        """Handle a node WebSocket connection lifecycle."""
        await websocket.accept()

        # 1. First message must be NodeHello with valid token
        try:
            raw = await websocket.receive_text()
            msg = parse_node_message(raw)
        except Exception as e:
            logger.warning(f"Node WS: invalid first message: {e}")
            await websocket.close(code=4001, reason="invalid hello")
            return

        if not isinstance(msg, NodeHello):
            await websocket.close(code=4001, reason="expected node_hello")
            return

        if msg.token != self._token:
            await websocket.close(code=4001, reason="auth failed")
            return

        # 2. Register node
        node_id = msg.node_id
        previous = self._registry.get_node(node_id)
        connection_lease = await self.deploy_coordinator.register_connection(
            node_id,
            activate=lambda lease: self._registry.register(
                websocket,
                msg,
                lease.generation,
                lease.connection_token,
            ),
        )
        if previous is not None and previous.websocket is not websocket:
            try:
                await previous.websocket.close(code=4000, reason="node reconnected")
            except Exception as e:
                logger.debug(
                    "Failed to close replaced node %s websocket: %s", node_id, e
                )

        # 3. Broadcast node_connected
        await self.broadcast_to_dashboards(
            {"type": "node_connected", "node_id": node_id, "hostname": msg.hostname}
        )
        await self.cleanup_pending_for_renamed_nodes()

        # 4. Message loop
        try:
            while True:
                raw = await websocket.receive_text()
                if not self._registry.is_current_connection(
                    node_id,
                    websocket,
                    connection_lease.generation,
                    connection_lease.connection_token,
                ):
                    logger.debug(
                        "Ignoring message from stale node connection: %s", node_id
                    )
                    break
                try:
                    incoming = parse_node_message(raw)
                except Exception as e:
                    logger.warning(f"Node {node_id}: invalid message: {e}")
                    continue

                if isinstance(incoming, ChangeNotification):
                    await self._handle_change_notification(incoming)
                elif isinstance(incoming, NodeStatus):
                    await self._registry.heartbeat(
                        incoming.node_id,
                        services=incoming.services,
                        websocket=websocket,
                    )
                elif isinstance(incoming, DeployResult):
                    await self._handle_deploy_result(incoming)
                elif isinstance(incoming, NodeDeployReport):
                    if incoming.node_id != node_id:
                        logger.warning(
                            "Ignoring node deploy report with connection identity "
                            "mismatch: connected=%s reported=%s",
                            node_id,
                            incoming.node_id,
                        )
                        continue
                    try:
                        await self._handle_node_deploy_report(incoming)
                    except Exception as e:
                        logger.warning(
                            "Ignoring failed node deploy report: "
                            "node=%s node_attempt_id=%s error=%s",
                            node_id,
                            incoming.node_attempt_id,
                            e,
                        )
                        continue
                elif isinstance(incoming, DeployProgress):
                    await self.deploy_coordinator.handle_progress(incoming)
                elif isinstance(incoming, RepoReconciliation):
                    if incoming.phase == "observed":
                        await self._repo_reconciler.handle(incoming)
                    elif incoming.phase == "attempt_started":
                        await self.deploy_coordinator.handle_auto_request(incoming)
                    elif incoming.phase == "settled":
                        await self.deploy_coordinator.handle_settled(incoming)
                elif isinstance(incoming, DeployPlanProposal):
                    await self.deploy_coordinator.handle_proposal(
                        incoming,
                        connection_token=connection_lease.connection_token,
                    )
                elif isinstance(incoming, ManifestRecoveryEvidence):
                    await self.deploy_coordinator.handle_evidence(incoming)
                elif isinstance(incoming, DeployAttemptTerminal):
                    await self.deploy_coordinator.handle_terminal(incoming)
                elif isinstance(incoming, ServiceCommandResult):
                    await self._handle_service_command_result(incoming)

        except WebSocketDisconnect:
            pass
        finally:
            # 5. Cleanup on disconnect
            await self._disconnect_node(
                node_id,
                reason="ws_closed",
                error="node disconnected",
                close_ws=False,
                websocket=websocket,
                expected_generation=connection_lease.generation,
                expected_connection_token=connection_lease.connection_token,
            )

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

    async def _handle_deploy_result(self, msg: DeployResult) -> None:
        """Delegate result correlation to the durable attempt coordinator."""
        await self.deploy_coordinator.handle_result(msg)

    async def _handle_node_deploy_report(self, msg: NodeDeployReport) -> None:
        """Reconcile an attempt-independent node-owned deployment report."""
        result = await self._store.record_node_deploy_report(msg)
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

    async def _handle_service_command_result(self, msg: ServiceCommandResult) -> None:
        """Process a ServiceCommandResult: cancel timeout task, broadcast to dashboards."""
        # Race-safe ownership transfer: pop ensures the timeout task can no longer broadcast.
        pending = self._pending_commands.pop(msg.command_id, None)
        if pending is not None:
            pending.timeout_task.cancel()
        await self.broadcast_to_dashboards(
            {
                "type": "service_command_result",
                "command_id": msg.command_id,
                "node_id": msg.node_id,
                "service_name": msg.service_name,
                "action": msg.action,
                "success": msg.success,
                "error": msg.error,
                "result": msg.result,
            }
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

    async def handle_dashboard_ws(self, websocket: WebSocket) -> None:
        """Handle a dashboard WebSocket connection with optional token auth."""
        token = websocket.query_params.get("token")
        if not self._verify_dashboard_token(token):
            await websocket.close(code=4001, reason="auth failed")
            return

        await websocket.accept()
        self._dashboard_connections.add(websocket)
        logger.info(f"Dashboard connected ({len(self._dashboard_connections)} total)")

        try:
            while True:
                # Keep-alive: just wait for client messages (ping/pong handled by framework)
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            self._dashboard_connections.discard(websocket)
            logger.info(
                f"Dashboard disconnected ({len(self._dashboard_connections)} total)"
            )

    async def register_pending_command(
        self,
        command_id: str,
        node_id: str,
        service_name: str,
        action: str,
    ) -> None:
        """Track an in-flight service command and schedule a timeout broadcast.

        On timeout (after `command_timeout_sec`), broadcasts a service_command_result
        with success=False, error='timeout'. Cancelled when a real ServiceCommandResult
        arrives or the target node disconnects.
        """
        timeout_task = asyncio.create_task(self._command_timeout(command_id))
        self._pending_commands[command_id] = PendingCommand(
            node_id=node_id,
            service_name=service_name,
            action=action,
            timeout_task=timeout_task,
        )

    async def _command_timeout(self, command_id: str) -> None:
        """Wait `command_timeout_sec`; broadcast timeout if command is still pending."""
        try:
            await asyncio.sleep(self._command_timeout_sec)
        except asyncio.CancelledError:
            return
        # Race-safe: if the result already arrived, pop returns None and we no-op.
        pending = self._pending_commands.pop(command_id, None)
        if pending is None:
            return
        await self.broadcast_to_dashboards(
            {
                "type": "service_command_result",
                "command_id": command_id,
                "node_id": pending.node_id,
                "service_name": pending.service_name,
                "action": pending.action,
                "success": False,
                "error": "timeout",
            }
        )

    async def _cleanup_orphan_commands(self, node_id: str, *, error: str) -> None:
        """Cancel & broadcast for any in-flight commands targeted at the given node.

        Single source of truth for both ws-disconnect (handle_node_ws.finally) and
        heartbeat-timeout (_check_loop) paths so that the dashboard releases the
        matching button rather than waiting for the per-command timeout.
        """
        orphans = [
            cid
            for cid, pending in self._pending_commands.items()
            if pending.node_id == node_id
        ]
        for cid in orphans:
            pending = self._pending_commands.pop(cid)
            pending.timeout_task.cancel()
            await self.broadcast_to_dashboards(
                {
                    "type": "service_command_result",
                    "command_id": cid,
                    "node_id": node_id,
                    "service_name": pending.service_name,
                    "action": pending.action,
                    "success": False,
                    "error": error,
                }
            )

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

    async def broadcast_to_dashboards(self, event: dict[str, Any]) -> None:
        """Send event to all connected dashboards. Individual failures are logged and ignored."""
        if not self._dashboard_connections:
            return

        payload = json.dumps(event)
        disconnected: list[WebSocket] = []

        for ws in self._dashboard_connections:
            try:
                await ws.send_text(payload)
            except Exception as e:
                logger.warning(f"Dashboard broadcast failed: {e}")
                disconnected.append(ws)

        for ws in disconnected:
            self._dashboard_connections.discard(ws)

    async def send_to_node(self, node_id: str, message: OrchestratorMessage) -> bool:
        """Send a message to a specific node. Returns False if node not connected."""
        node = self._registry.get_node(node_id)
        if node is None:
            return False
        try:
            await node.websocket.send_text(message.model_dump_json())
            return True
        except Exception as e:
            logger.warning(f"Failed to send to node {node_id}: {e}")
            await self._disconnect_node(
                node_id,
                reason="send_failed",
                error="node disconnected (send failed)",
                close_ws=True,
                websocket=node.websocket,
                expected_generation=node.connection_generation,
                expected_connection_token=node.connection_token,
            )
            return False

    async def send_to_node_generation(
        self, node_id: str, generation: str, message: OrchestratorMessage
    ) -> bool:
        """Send a deploy permit only through its coordinator-owned socket generation."""
        connection = self.deploy_coordinator.current_connection(node_id)
        if connection is None or connection.generation != generation:
            return False
        node = self._registry.get_node(node_id)
        if (
            node is None
            or node.connection_generation != generation
            or node.connection_token != connection.connection_token
        ):
            return False
        try:
            await node.websocket.send_text(message.model_dump_json())
            return True
        except Exception as exc:
            logger.warning("Failed generation-bound send to node %s: %s", node_id, exc)
            task = asyncio.create_task(
                self._disconnect_node(
                    node_id,
                    reason="send_failed",
                    error="node disconnected (generation-bound send failed)",
                    close_ws=True,
                    websocket=node.websocket,
                    expected_generation=generation,
                    expected_connection_token=connection.connection_token,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return False

    async def _disconnect_node(
        self,
        node_id: str,
        *,
        reason: str,
        error: str,
        close_ws: bool,
        websocket: WebSocket,
        expected_generation: str,
        expected_connection_token: str,
    ) -> None:
        await self._disconnects.disconnect(
            node_id,
            reason=reason,
            error=error,
            close_ws=close_ws,
            websocket=websocket,
            expected_generation=expected_generation,
            expected_connection_token=expected_connection_token,
        )

    async def _on_current_node_disconnect(
        self, node_id: str, reason: str, error: str
    ) -> None:
        await self.broadcast_to_dashboards(
            {"type": "node_disconnected", "node_id": node_id, "reason": reason}
        )
        await self._cleanup_orphan_commands(node_id, error=error)

    async def start_heartbeat_checker(self) -> None:
        """Start periodic heartbeat check task (30s interval)."""
        # One explicit startup repair closes duplicate pending canonicals left by
        # pre-generation-time servers. Read APIs stay mutation-free.
        await self._store.supersede_stale_pending_deploys()
        await self.deploy_coordinator.restore_deadlines()

        async def _check_loop() -> None:
            while True:
                await asyncio.sleep(30)
                stale_nodes = await self._registry.check_stale_connections()
                for node in stale_nodes:
                    await self._disconnect_node(
                        node.node_id,
                        reason="heartbeat_timeout",
                        error="node disconnected (heartbeat timeout)",
                        close_ws=False,
                        websocket=node.websocket,
                        expected_generation=node.connection_generation,
                        expected_connection_token=node.connection_token,
                    )

        self._heartbeat_task = asyncio.create_task(_check_loop())

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel pending timeouts, close all connections."""
        await self.deploy_coordinator.shutdown()
        # Cancel pending timeout tasks first; await for graceful unwind to avoid
        # RuntimeWarning: "coroutine was never awaited" on event-loop teardown.
        pending_tasks = [
            pending.timeout_task for pending in self._pending_commands.values()
        ]
        for t in pending_tasks:
            t.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._pending_commands.clear()

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Close all dashboard connections
        for ws in list(self._dashboard_connections):
            try:
                await ws.close(code=1001, reason="server shutdown")
            except Exception:
                pass
        self._dashboard_connections.clear()

        # Close all node connections
        for node in self._registry.get_connected_nodes():
            try:
                await node.websocket.close(code=1001, reason="server shutdown")
            except Exception:
                pass
