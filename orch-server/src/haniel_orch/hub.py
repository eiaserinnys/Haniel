"""WebSocket hub — routes messages between nodes and dashboards."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from .deploy_attempt_coordinator import DeployAttemptCoordinator
from .event_store import EventStore
from .hub_canonicals import HubCanonicalFlows
from .hub_commands import HubServiceCommands, PendingCommand  # noqa: F401 (재노출)
from .node_disconnect_workflow import NodeDisconnectWorkflow
from .node_registry import NodeRegistry
from .push import NullPushService, PushService
from .protocol import (
    ChangeNotification,
    DeployAttemptReceived,
    DeployAttemptTerminal,
    DeployPlanProposal,
    DeployProgress,
    DeployResult,
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

# 시도에 묶인 노드 글 가운데 본문에 node_id가 있는 것(deploy_attempt_terminal에는 node_id 칸이 없다).
# RepoReconciliation은 정착(settled)만 시도에 묶인다
_ATTEMPT_BOUND = (
    DeployProgress,
    DeployResult,
    RepoReconciliation,
    ManifestRecoveryEvidence,
    DeployAttemptReceived,
)


class WebSocketHub(HubCanonicalFlows, HubServiceCommands):
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
        self_update_postpone_sec: float = 21600.0,
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
        self._self_update_postpone_sec = self_update_postpone_sec
        self._repo_reconciler = RepoReconciler(store, self.broadcast_to_dashboards)
        self.deploy_coordinator = DeployAttemptCoordinator(
            store,
            self.send_to_node_generation,
            lambda event: self.broadcast_to_dashboards(event),
            self._registry.has_connection_identity,
            probe_timeout_sec=min(deploy_timeout_sec, 30.0),
            attempt_timeout_sec=deploy_timeout_sec,
            terminal_callback=self._on_deploy_terminal,
            node_protocol=self._registry.deploy_protocol,
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

    @property
    def self_update_postpone_sec(self) -> float:
        """How long a rejected self-update stays postponed before revival."""
        return self._self_update_postpone_sec

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

        # 2b. hello 대조: 메시지 고리에 들어가기 전에 이 노드의 판 1 시도를 쥔 시도 목록과 견준다(wire-v1 1절, 7항)
        await self.deploy_coordinator.reconcile_hello(
            node_id, msg.deploy_protocol, msg.held_attempts
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

                if (
                    isinstance(incoming, _ATTEMPT_BOUND)
                    and incoming.node_id != node_id
                    and not (
                        isinstance(incoming, RepoReconciliation)
                        and incoming.phase != "settled"
                    )
                ):
                    # 시도에 묶인 노드 글의 소유권: 본문 node_id가 연결이 인증한 node_id와 다르면 버린다(design.md:110)
                    logger.warning(
                        "Ignoring %s with connection identity mismatch: connected=%s reported=%s attempt=%s",
                        incoming.type,
                        node_id,
                        incoming.node_id,
                        incoming.orchestrator_attempt_id,
                    )
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
                elif isinstance(incoming, DeployAttemptReceived):
                    await self.deploy_coordinator.handle_received(incoming)
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
