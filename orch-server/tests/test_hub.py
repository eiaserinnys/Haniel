"""Tests for WebSocketHub — node/dashboard WS handling, broadcast, send_to_node."""

import asyncio
import contextlib
import json
import time
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


class TestDeployTimeout:
    """Hub tracks in-flight deploys; broadcasts timeout/orphan-fail."""

    async def _seed_deploying(
        self, store: EventStore, deploy_id: str, node_id: str = "n1"
    ) -> None:
        await store.create_deploy_event(
            deploy_id=deploy_id, node_id=node_id, repo="r", branch="main",
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status(deploy_id, DeployStatus.DEPLOYING)

    async def test_timeout_broadcasts_failure(self, registry, store):
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=0.1)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_deploying(store, "d1")
        await hub.register_pending_deploy("d1", "n1", "r", "main")
        await asyncio.sleep(0.25)

        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        timeouts = [
            p for p in sent
            if p.get("type") == "status_change" and p.get("status") == "failed"
        ]
        assert len(timeouts) == 1
        assert timeouts[0]["deploy_id"] == "d1"
        assert timeouts[0]["node_id"] == "n1"
        assert "d1" not in hub._pending_deploys
        ev = await store.get_deploy_event("d1")
        assert ev["status"] == "failed"
        assert ev["error"] == "timeout"

    async def test_result_arrival_cancels_timeout(self, registry, store):
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_deploying(store, "d1")
        await hub.register_pending_deploy("d1", "n1", "r", "main")
        timeout_task = hub._pending_deploys["d1"].timeout_task

        result = DeployResult(
            deploy_id="d1", node_id="n1", status="success", duration_ms=500,
        )
        await hub._handle_deploy_result(result)

        assert "d1" not in hub._pending_deploys
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_task
        assert timeout_task.cancelled()
        # Single broadcast — the success status_change. No timeout.
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        statuses = [p["status"] for p in sent if p.get("type") == "status_change"]
        assert statuses == ["success"]

    async def test_cleanup_orphan_deploys_via_pending(self, registry, store):
        """orphan deploys tracked in _pending_deploys are cancelled + broadcast on disconnect."""
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_deploying(store, "d_a", node_id="n1")
        await self._seed_deploying(store, "d_b", node_id="n2")
        await hub.register_pending_deploy("d_a", "n1", "r", "main")
        await hub.register_pending_deploy("d_b", "n2", "r", "main")

        await hub._cleanup_orphan_deploys("n1", error="node disconnected")

        assert "d_a" not in hub._pending_deploys
        assert "d_b" in hub._pending_deploys
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        failed = [
            p for p in sent
            if p.get("type") == "status_change" and p.get("status") == "failed"
        ]
        assert len(failed) == 1 and failed[0]["deploy_id"] == "d_a"
        ev = await store.get_deploy_event("d_a")
        assert ev["status"] == "failed"
        assert ev["error"] == "node disconnected"

    async def test_cleanup_orphan_deploys_via_store(self, registry, store):
        """DEPLOYING events that were never registered (e.g., previous server lifecycle)
        are still failed + broadcast on cleanup. Replaces the former
        NodeRegistry.unregister responsibility."""
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_deploying(store, "d_orphan", node_id="n1")

        await hub._cleanup_orphan_deploys("n1", error="node disconnected")

        ev = await store.get_deploy_event("d_orphan")
        assert ev["status"] == "failed"
        assert ev["error"] == "node disconnected"
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        failed = [
            p for p in sent
            if p.get("type") == "status_change" and p.get("status") == "failed"
        ]
        assert len(failed) == 1 and failed[0]["deploy_id"] == "d_orphan"

    async def test_heartbeat_timeout_path_fails_deploys(self, store):
        """Integration: registry.check_stale → unregister (no deploy fail) →
        hub._cleanup_orphan_deploys → DEPLOYING events transition to FAILED.
        Guards against accidental coupling between registry.unregister and
        deploy cleanup."""
        registry = NodeRegistry(store, heartbeat_timeout=0.05)
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1", token="t", hostname="h",
            os="Linux", arch="x86_64", haniel_version="0.1.0",
        )
        await registry.register(ws, hello)
        await self._seed_deploying(store, "d1", node_id="n1")
        await hub.register_pending_deploy("d1", "n1", "r", "main")

        # Force the heartbeat to be older than the timeout
        registry.get_node("n1").last_heartbeat = time.time() - 1.0

        # `check_stale` calls `unregister` internally. After our refactor,
        # `unregister` no longer touches DEPLOYING events; the hub's cleanup
        # is the single source of truth.
        stale = await registry.check_stale()
        assert stale == ["n1"]
        ev_before = await store.get_deploy_event("d1")
        assert ev_before["status"] == "deploying"  # registry didn't fail it

        # _check_loop would then call cleanup_orphan_deploys; simulate that.
        await hub._cleanup_orphan_deploys(
            "n1", error="node disconnected (heartbeat timeout)"
        )

        ev_after = await store.get_deploy_event("d1")
        assert ev_after["status"] == "failed"
        assert ev_after["error"] == "node disconnected (heartbeat timeout)"


class TestSupersedePending:
    """supersede_pending rejects older PENDING deploys in the same (node, repo, branch)."""

    async def _seed_pending(
        self,
        store: EventStore,
        deploy_id: str,
        node_id: str = "n1",
        repo: str = "r",
        branch: str = "main",
    ) -> None:
        await store.create_deploy_event(
            deploy_id=deploy_id, node_id=node_id, repo=repo, branch=branch,
            commits=["h"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

    async def test_marks_others_rejected(self, hub: WebSocketHub, store):
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_pending(store, "d1")
        await asyncio.sleep(0.005)
        await self._seed_pending(store, "d2")
        await asyncio.sleep(0.005)
        await self._seed_pending(store, "d3")

        result = await hub.supersede_pending("n1", "r", "main", "d3")

        assert set(result) == {"d1", "d2"}
        for did in ("d1", "d2"):
            ev = await store.get_deploy_event(did)
            assert ev["status"] == "rejected"
            assert ev["reject_reason"] == "superseded by d3"
        ev3 = await store.get_deploy_event("d3")
        assert ev3["status"] == "pending"

        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        rejected = [
            p for p in sent
            if p.get("type") == "status_change" and p.get("status") == "rejected"
        ]
        assert {p["deploy_id"] for p in rejected} == {"d1", "d2"}
        for p in rejected:
            assert p.get("reject_reason") == "superseded by d3"

    async def test_skips_other_branches(self, hub: WebSocketHub, store):
        # Only the dev-branch deploy exists; supersede_pending on main → no-op.
        # Verifies that a PENDING entry on a different branch is untouched.
        await self._seed_pending(store, "d_dev", branch="dev")

        result = await hub.supersede_pending("n1", "r", "main", "d_new")

        assert result == []
        ev_dev = await store.get_deploy_event("d_dev")
        assert ev_dev["status"] == "pending"
        assert ev_dev["reject_reason"] is None

    async def test_returns_empty_when_no_others(self, hub: WebSocketHub, store):
        await self._seed_pending(store, "d_alone")
        result = await hub.supersede_pending("n1", "r", "main", "d_alone")
        assert result == []
        ev = await store.get_deploy_event("d_alone")
        assert ev["status"] == "pending"
