"""WebSocket hub — routes messages between nodes and dashboards."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
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


class WebSocketHub:
    """Central hub managing node and dashboard WebSocket connections."""

    def __init__(
        self,
        registry: NodeRegistry,
        store: EventStore,
        token: str,
        push_service: PushService | None = None,
        auth_bearer_token: str = "",
    ) -> None:
        self._registry = registry
        self._store = store
        self._token = token
        self._push: PushService = push_service or NullPushService()
        self._auth_bearer_token = auth_bearer_token
        self._dashboard_connections: set[WebSocket] = set()
        self._heartbeat_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

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
        """Process a DeployResult: update status + broadcast."""
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
        """Process a ServiceCommandResult: broadcast to dashboards."""
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

        self._heartbeat_task = asyncio.create_task(_check_loop())

    async def shutdown(self) -> None:
        """Graceful shutdown: close all connections, cancel heartbeat task."""
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
