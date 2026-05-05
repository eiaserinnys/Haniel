"""Tests for WebSocketHub — node/dashboard WS handling, broadcast, send_to_node."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import ConnectedNode, NodeRegistry
from haniel_orch.protocol import (
    ChangeNotification,
    DeployApproval,
    DeployResult,
    DeployStatus,
    NodeHello,
    NodeStatus,
)


@pytest.fixture
async def registry(store: EventStore):
    return NodeRegistry(store)


@pytest.fixture
async def hub(registry: NodeRegistry, store: EventStore):
    return WebSocketHub(registry, store, token="test-token")


class TestBroadcastToDashboards:
    async def test_sends_to_all_dashboards(self, hub: WebSocketHub):
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        hub._dashboard_connections = {ws1, ws2}

        await hub.broadcast_to_dashboards({"type": "test", "data": 123})

        expected = json.dumps({"type": "test", "data": 123})
        ws1.send_text.assert_called_once_with(expected)
        ws2.send_text.assert_called_once_with(expected)

    async def test_removes_failed_connections(self, hub: WebSocketHub):
        ws_good = AsyncMock()
        ws_bad = AsyncMock()
        ws_bad.send_text.side_effect = Exception("disconnected")
        hub._dashboard_connections = {ws_good, ws_bad}

        await hub.broadcast_to_dashboards({"type": "test"})

        assert ws_bad not in hub._dashboard_connections
        assert ws_good in hub._dashboard_connections

    async def test_noop_when_no_dashboards(self, hub: WebSocketHub):
        # Should not raise
        await hub.broadcast_to_dashboards({"type": "test"})


class TestSendToNode:
    async def test_sends_message_to_connected_node(
        self, hub: WebSocketHub, registry: NodeRegistry, store: EventStore
    ):
        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        msg = DeployApproval(deploy_id="d1", approved_by="test")
        result = await hub.send_to_node("n1", msg)

        assert result is True
        ws.send_text.assert_called_once_with(msg.model_dump_json())

    async def test_returns_false_for_unknown_node(self, hub: WebSocketHub):
        msg = DeployApproval(deploy_id="d1")
        result = await hub.send_to_node("nonexistent", msg)
        assert result is False

    async def test_returns_false_on_send_error(
        self, hub: WebSocketHub, registry: NodeRegistry, store: EventStore
    ):
        ws = AsyncMock()
        ws.send_text.side_effect = Exception("broken pipe")
        hello = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        msg = DeployApproval(deploy_id="d1")
        result = await hub.send_to_node("n1", msg)
        assert result is False


class TestHandleChangeNotification:
    async def test_stores_and_broadcasts(self, hub: WebSocketHub, store: EventStore):
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        notification = ChangeNotification(
            deploy_id="n1:repo:main:abc1234",
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["abc1234 fix: something"],
            affected_services=["bot"],
            diff_stat="+10 -3",
            detected_at="2026-05-05T00:00:00Z",
        )

        await hub._handle_change_notification(notification)

        # Verify stored
        event = await store.get_deploy_event("n1:repo:main:abc1234")
        assert event is not None
        assert event["status"] == "pending"
        assert event["repo"] == "repo"

        # Verify broadcast
        ws_dash.send_text.assert_called_once()
        broadcast_data = json.loads(ws_dash.send_text.call_args[0][0])
        assert broadcast_data["type"] == "new_pending"
        assert broadcast_data["deploy_id"] == "n1:repo:main:abc1234"


class TestHandleDeployResult:
    async def test_success_result(self, hub: WebSocketHub, store: EventStore):
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        # Create the deploy event first
        await store.create_deploy_event(
            deploy_id="d1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("d1", DeployStatus.DEPLOYING)

        result = DeployResult(
            deploy_id="d1", node_id="n1", status="success", duration_ms=5000
        )
        await hub._handle_deploy_result(result)

        event = await store.get_deploy_event("d1")
        assert event["status"] == "success"
        assert event["duration_ms"] == 5000

        broadcast_data = json.loads(ws_dash.send_text.call_args[0][0])
        assert broadcast_data["type"] == "status_change"
        assert broadcast_data["status"] == "success"

    async def test_failed_result(self, hub: WebSocketHub, store: EventStore):
        await store.create_deploy_event(
            deploy_id="d2",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("d2", DeployStatus.DEPLOYING)

        result = DeployResult(
            deploy_id="d2",
            node_id="n1",
            status="failed",
            error="exit code 1",
            duration_ms=3400,
        )
        await hub._handle_deploy_result(result)

        event = await store.get_deploy_event("d2")
        assert event["status"] == "failed"
        assert event["error"] == "exit code 1"


class TestHeartbeatChecker:
    async def test_start_and_shutdown(self, hub: WebSocketHub):
        await hub.start_heartbeat_checker()
        assert hub._heartbeat_task is not None
        assert not hub._heartbeat_task.done()

        await hub.shutdown()
        assert hub._heartbeat_task.done()


class TestShutdown:
    async def test_closes_dashboard_connections(self, hub: WebSocketHub):
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        hub._dashboard_connections = {ws1, ws2}

        await hub.shutdown()

        ws1.close.assert_called_once_with(code=1001, reason="server shutdown")
        ws2.close.assert_called_once_with(code=1001, reason="server shutdown")
        assert len(hub._dashboard_connections) == 0

    async def test_closes_node_connections(
        self, hub: WebSocketHub, registry: NodeRegistry, store: EventStore
    ):
        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        await hub.shutdown()

        ws.close.assert_called_once_with(code=1001, reason="server shutdown")


class TestPushIntegration:
    """Tests for push notification integration in WebSocketHub."""

    async def test_change_notification_fires_push(self, store: EventStore):
        """ChangeNotification triggers push_service.notify with new_pending data."""
        push = AsyncMock()
        registry = NodeRegistry(store)
        hub = WebSocketHub(registry, store, token="t", push_service=push)

        notification = ChangeNotification(
            deploy_id="n1:repo:main:abc",
            node_id="n1",
            repo="myrepo",
            branch="main",
            commits=["abc fix"],
            affected_services=["svc"],
            detected_at="2026-05-05T00:00:00Z",
        )

        await hub._handle_change_notification(notification)
        # Let the fire-and-forget task complete
        await asyncio.sleep(0.05)

        push.notify.assert_called_once()
        args, kwargs = push.notify.call_args
        title = kwargs.get("title", args[0])
        data = kwargs.get("data", args[2])
        assert "myrepo" in title
        assert data["type"] == "new_pending"
        assert data["deploy_id"] == "n1:repo:main:abc"

    async def test_deploy_result_success_fires_push(self, store: EventStore):
        """DeployResult(success) triggers push notification."""
        push = AsyncMock()
        registry = NodeRegistry(store)
        hub = WebSocketHub(registry, store, token="t", push_service=push)

        await store.create_deploy_event(
            deploy_id="d1", node_id="n1", repo="r", branch="main",
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("d1", DeployStatus.DEPLOYING)

        result = DeployResult(deploy_id="d1", node_id="n1", status="success", duration_ms=5000)
        await hub._handle_deploy_result(result)
        await asyncio.sleep(0.05)

        push.notify.assert_called_once()
        args, kwargs = push.notify.call_args
        data = kwargs.get("data", args[2])
        assert data["status"] == "success"

    async def test_deploy_result_failed_fires_push(self, store: EventStore):
        """DeployResult(failed) triggers push notification."""
        push = AsyncMock()
        registry = NodeRegistry(store)
        hub = WebSocketHub(registry, store, token="t", push_service=push)

        await store.create_deploy_event(
            deploy_id="d2", node_id="n1", repo="r", branch="main",
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("d2", DeployStatus.DEPLOYING)

        result = DeployResult(deploy_id="d2", node_id="n1", status="failed", error="exit 1")
        await hub._handle_deploy_result(result)
        await asyncio.sleep(0.05)

        push.notify.assert_called_once()
        args, kwargs = push.notify.call_args
        data = kwargs.get("data", args[2])
        assert data["status"] == "failed"

    async def test_push_failure_does_not_break_broadcast(self, store: EventStore):
        """Push failure does not prevent dashboard broadcast."""
        push = AsyncMock()
        push.notify = AsyncMock(side_effect=Exception("relay down"))
        registry = NodeRegistry(store)
        hub = WebSocketHub(registry, store, token="t", push_service=push)

        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        notification = ChangeNotification(
            deploy_id="d1:repo:main:abc",
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["abc fix"],
            affected_services=["svc"],
            detected_at="2026-05-05T00:00:00Z",
        )

        await hub._handle_change_notification(notification)
        await asyncio.sleep(0.05)

        # Dashboard broadcast should succeed even if push fails
        ws_dash.send_text.assert_called_once()

    async def test_null_push_service_is_noop(self, hub: WebSocketHub, store: EventStore):
        """Default hub (no push_service arg) uses NullPushService — no errors."""
        # hub fixture has push_service=None → auto-injected NullPushService
        notification = ChangeNotification(
            deploy_id="d1:repo:main:abc",
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["abc fix"],
            affected_services=["svc"],
            detected_at="2026-05-05T00:00:00Z",
        )
        # Should not raise any errors — NullPushService.notify is no-op
        await hub._handle_change_notification(notification)
        await asyncio.sleep(0.05)  # let fire-and-forget complete
