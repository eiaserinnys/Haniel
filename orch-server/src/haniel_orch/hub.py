"""WebSocket hub — routes messages between nodes and dashboards."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from .event_store import EventStore
from .node_registry import NodeRegistry
from .push import NullPushService, PushService
from .protocol import (
    ChangeNotification,
    DeployResult,
    DeployStatus,
    NodeHello,
    NodeStatus,
    OrchestratorMessage,
    ServiceCommandResult,
    parse_node_message,
)

logger = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    """In-flight service command tracked by the hub."""

    node_id: str
    service_name: str
    action: str
    timeout_task: asyncio.Task[None]


@dataclass
class PendingDeploy:
    """In-flight deploy tracked by the hub (post-approval, pre-result)."""

    node_id: str
    repo: str
    branch: str
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
        # In-flight deploys: deploy_id → PendingDeploy.
        # Single source of truth for deploy tracking (timeout + orphan cleanup);
        # mirrors the _pending_commands pattern. Race-safe via pop ownership transfer.
        self._deploy_timeout_sec = deploy_timeout_sec
        self._pending_deploys: dict[str, PendingDeploy] = {}

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
        await self._registry.register(websocket, msg)
        node_id = msg.node_id

        # 3. Broadcast node_connected
        await self.broadcast_to_dashboards(
            {"type": "node_connected", "node_id": node_id, "hostname": msg.hostname}
        )

        # 4. Message loop
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    incoming = parse_node_message(raw)
                except Exception as e:
                    logger.warning(f"Node {node_id}: invalid message: {e}")
                    continue

                if isinstance(incoming, ChangeNotification):
                    await self._handle_change_notification(incoming)
                elif isinstance(incoming, NodeStatus):
                    await self._registry.heartbeat(incoming.node_id, services=incoming.services)
                elif isinstance(incoming, DeployResult):
                    await self._handle_deploy_result(incoming)
                elif isinstance(incoming, ServiceCommandResult):
                    await self._handle_service_command_result(incoming)

        except WebSocketDisconnect:
            pass
        finally:
            # 5. Cleanup on disconnect
            await self._registry.unregister(node_id)
            await self.broadcast_to_dashboards(
                {"type": "node_disconnected", "node_id": node_id, "reason": "ws_closed"}
            )
            await self._cleanup_orphan_commands(node_id, error="node disconnected")
            await self._cleanup_orphan_deploys(node_id, error="node disconnected")

    async def _handle_change_notification(self, msg: ChangeNotification) -> None:
        """Process a ChangeNotification: store + broadcast."""
        await self._store.create_deploy_event(
            deploy_id=msg.deploy_id,
            node_id=msg.node_id,
            repo=msg.repo,
            branch=msg.branch,
            commits=msg.commits,
            affected_services=msg.affected_services,
            diff_stat=msg.diff_stat,
            detected_at=msg.detected_at,
        )
        await self.broadcast_to_dashboards({
            "type": "new_pending",
            "deploy_id": msg.deploy_id,
            "node_id": msg.node_id,
            "repo": msg.repo,
            "branch": msg.branch,
            "detected_at": msg.detected_at,
        })

        # Fire-and-forget push notification
        self._spawn_push(
            title=f"배포 대기: {msg.repo}",
            body=f"{msg.node_id}에서 {msg.repo} ({msg.branch}) 변경 감지",
            data={"deploy_id": msg.deploy_id, "type": "new_pending", "node_id": msg.node_id},
        )

    async def _handle_deploy_result(self, msg: DeployResult) -> None:
        """Process a DeployResult: cancel timeout, update status, broadcast."""
        # Race-safe ownership transfer: pop ensures the timeout task can no longer broadcast.
        pending = self._pending_deploys.pop(msg.deploy_id, None)
        if pending is not None:
            pending.timeout_task.cancel()
        status = DeployStatus[msg.status.upper()]
        await self._store.update_deploy_status(
            msg.deploy_id,
            status,
            error=msg.error,
            duration_ms=msg.duration_ms,
        )
        await self.broadcast_to_dashboards({
            "type": "status_change",
            "deploy_id": msg.deploy_id,
            "status": status.value,
            "node_id": msg.node_id,
        })

        # Fire-and-forget push for terminal states
        if status in (DeployStatus.SUCCESS, DeployStatus.FAILED):
            status_text = "성공" if status == DeployStatus.SUCCESS else "실패"
            self._spawn_push(
                title=f"배포 {status_text}: {msg.node_id}",
                body=f"{msg.node_id}의 배포가 {status_text}했습니다",
                data={"deploy_id": msg.deploy_id, "type": "status_change", "status": status.value},
            )

    async def _handle_service_command_result(self, msg: ServiceCommandResult) -> None:
        """Process a ServiceCommandResult: cancel timeout task, broadcast to dashboards."""
        # Race-safe ownership transfer: pop ensures the timeout task can no longer broadcast.
        pending = self._pending_commands.pop(msg.command_id, None)
        if pending is not None:
            pending.timeout_task.cancel()
        await self.broadcast_to_dashboards({
            "type": "service_command_result",
            "command_id": msg.command_id,
            "node_id": msg.node_id,
            "service_name": msg.service_name,
            "action": msg.action,
            "success": msg.success,
            "error": msg.error,
        })

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
            logger.info(f"Dashboard disconnected ({len(self._dashboard_connections)} total)")

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
        await self.broadcast_to_dashboards({
            "type": "service_command_result",
            "command_id": command_id,
            "node_id": pending.node_id,
            "service_name": pending.service_name,
            "action": pending.action,
            "success": False,
            "error": "timeout",
        })

    async def _cleanup_orphan_commands(self, node_id: str, *, error: str) -> None:
        """Cancel & broadcast for any in-flight commands targeted at the given node.

        Single source of truth for both ws-disconnect (handle_node_ws.finally) and
        heartbeat-timeout (_check_loop) paths so that the dashboard releases the
        matching button rather than waiting for the per-command timeout.
        """
        orphans = [
            cid for cid, pending in self._pending_commands.items()
            if pending.node_id == node_id
        ]
        for cid in orphans:
            pending = self._pending_commands.pop(cid)
            pending.timeout_task.cancel()
            await self.broadcast_to_dashboards({
                "type": "service_command_result",
                "command_id": cid,
                "node_id": node_id,
                "service_name": pending.service_name,
                "action": pending.action,
                "success": False,
                "error": error,
            })

    async def register_pending_deploy(
        self, deploy_id: str, node_id: str, repo: str, branch: str
    ) -> None:
        """Track an in-flight deploy and schedule a timeout broadcast.

        On timeout (after `deploy_timeout_sec`), marks status=FAILED with
        error='timeout' in the store and broadcasts status_change. Cancelled
        when DeployResult arrives, the node disconnects, or the deploy is
        superseded.
        """
        timeout_task = asyncio.create_task(self._deploy_timeout(deploy_id))
        self._pending_deploys[deploy_id] = PendingDeploy(
            node_id=node_id,
            repo=repo,
            branch=branch,
            timeout_task=timeout_task,
        )

    async def _deploy_timeout(self, deploy_id: str) -> None:
        """Wait `deploy_timeout_sec`; broadcast failure if deploy is still in-flight."""
        try:
            await asyncio.sleep(self._deploy_timeout_sec)
        except asyncio.CancelledError:
            return
        # Race-safe: if the result already arrived (or supersede consumed the
        # entry), pop returns None and we no-op.
        pending = self._pending_deploys.pop(deploy_id, None)
        if pending is None:
            return
        await self._store.update_deploy_status(
            deploy_id, DeployStatus.FAILED, error="timeout"
        )
        await self.broadcast_to_dashboards({
            "type": "status_change",
            "deploy_id": deploy_id,
            "status": DeployStatus.FAILED.value,
            "node_id": pending.node_id,
        })

    async def _cleanup_orphan_deploys(self, node_id: str, *, error: str) -> None:
        """Fail in-flight deploys for a disconnected node + broadcast status_change.

        Single source of truth for in-flight deploy cleanup (replaces the
        former NodeRegistry.unregister DEPLOYING→FAILED path). Called from
        handle_node_ws.finally, _check_loop, and shutdown so that ws-disconnect,
        heartbeat-timeout, and graceful shutdown all flow through the same code.

        Two responsibilities:
        1. Cancel any outstanding _pending_deploys timeout tasks for this node.
        2. Mark all DEPLOYING events for this node as FAILED + broadcast
           status_change. Covers both deploys we registered and any DEPLOYING
           rows from a previous server lifecycle.
        """
        # 1. Cancel in-flight (post-approval) deploys for this node.
        orphan_ids = [
            did for did, pending in self._pending_deploys.items()
            if pending.node_id == node_id
        ]
        for did in orphan_ids:
            pending = self._pending_deploys.pop(did)
            pending.timeout_task.cancel()

        # 2. FAIL + broadcast for all DEPLOYING events on this node.
        deploying = await self._store.get_deploying_events_for_node(node_id)
        for event in deploying:
            await self._store.update_deploy_status(
                event["deploy_id"], DeployStatus.FAILED, error=error
            )
            await self.broadcast_to_dashboards({
                "type": "status_change",
                "deploy_id": event["deploy_id"],
                "status": DeployStatus.FAILED.value,
                "node_id": node_id,
            })

    async def supersede_pending(
        self, node_id: str, repo: str, branch: str, kept_deploy_id: str
    ) -> list[str]:
        """Reject all PENDING deploys for the same (node, repo, branch) except `kept_deploy_id`.

        Used by approve_deploy/approve_all so that approving a newer deploy
        automatically supersedes older PENDING deploys in the same branch.
        Each superseded deploy gets status=REJECTED + reject_reason
        ='superseded by ${kept_deploy_id}' and a status_change broadcast
        with the reject_reason field.

        Returns the list of superseded deploy_ids. Defensive: cancels any
        outstanding _pending_deploys timeout task for the superseded id
        (PENDING deploys typically aren't tracked yet, so this is a no-op
        in normal operation).
        """
        same_branch = await self._store.get_pending_deploys_for_branch(
            node_id, repo, branch
        )
        superseded: list[str] = []
        for ev in same_branch:
            did = ev["deploy_id"]
            if did == kept_deploy_id:
                continue
            reason = f"superseded by {kept_deploy_id}"
            await self._store.update_deploy_status(
                did, DeployStatus.REJECTED, reject_reason=reason
            )
            # Defensive: cancel a timeout task if one exists (rare for PENDING).
            pending = self._pending_deploys.pop(did, None)
            if pending is not None:
                pending.timeout_task.cancel()
            await self.broadcast_to_dashboards({
                "type": "status_change",
                "deploy_id": did,
                "status": DeployStatus.REJECTED.value,
                "node_id": node_id,
                "reject_reason": reason,
            })
            superseded.append(did)
        return superseded

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
            return False

    async def start_heartbeat_checker(self) -> None:
        """Start periodic heartbeat check task (30s interval)."""

        async def _check_loop() -> None:
            while True:
                await asyncio.sleep(30)
                stale_ids = await self._registry.check_stale()
                for node_id in stale_ids:
                    await self.broadcast_to_dashboards({
                        "type": "node_disconnected",
                        "node_id": node_id,
                        "reason": "heartbeat_timeout",
                    })
                    # Mirror the ws-disconnect path so dashboards stop waiting on
                    # buttons whose target node has gone silent.
                    await self._cleanup_orphan_commands(
                        node_id, error="node disconnected (heartbeat timeout)"
                    )
                    await self._cleanup_orphan_deploys(
                        node_id, error="node disconnected (heartbeat timeout)"
                    )

        self._heartbeat_task = asyncio.create_task(_check_loop())

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel pending timeouts, close all connections."""
        # Cancel pending timeout tasks first; await for graceful unwind to avoid
        # RuntimeWarning: "coroutine was never awaited" on event-loop teardown.
        pending_tasks = [pending.timeout_task for pending in self._pending_commands.values()]
        pending_tasks += [pending.timeout_task for pending in self._pending_deploys.values()]
        for t in pending_tasks:
            t.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._pending_commands.clear()
        self._pending_deploys.clear()

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
