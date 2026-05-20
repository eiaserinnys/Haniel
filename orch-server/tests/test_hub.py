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
        assert registry.get_node("n1") is None
        ws.close.assert_awaited_once()
        nodes = await store.get_nodes()
        assert nodes[0]["connected"] == 0

    async def test_send_error_broadcasts_disconnect(
        self, hub: WebSocketHub, registry: NodeRegistry, store: EventStore
    ):
        ws = AsyncMock()
        ws.send_text.side_effect = Exception("broken pipe")
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await registry.register(
            ws,
            NodeHello(
                node_id="n1",
                token="t",
                hostname="h",
                os="Linux",
                arch="x86_64",
                haniel_version="0.1.0",
            ),
        )

        result = await hub.send_to_node("n1", DeployApproval(deploy_id="d1"))

        assert result is False
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        assert {
            "type": "node_disconnected",
            "node_id": "n1",
            "reason": "send_failed",
        } in sent


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


class TestHandleChangeNotificationSupersede:
    """change_notification 수신 시점 supersede — 같은 (node, repo, branch)
    이전 PENDING은 즉시 REJECTED('superseded by ${new}'). DEPLOYING은 무관.

    Primary gate (generation-time). approve-time supersede in
    api.approve_deploy/approve_all remains as defensive backup.
    """

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
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

    def _notification(
        self,
        deploy_id: str,
        node_id: str = "n1",
        repo: str = "r",
        branch: str = "main",
    ) -> ChangeNotification:
        return ChangeNotification(
            deploy_id=deploy_id, node_id=node_id, repo=repo, branch=branch,
            commits=["hN msgN"], affected_services=[], diff_stat=None,
            detected_at="2026-05-19T00:00:00Z",
        )

    async def test_supersedes_prior_pending_same_branch(
        self, hub: WebSocketHub, store: EventStore
    ):
        """d_old PENDING 존재 → d_new change_notification → d_old=REJECTED,
        d_new=PENDING."""
        await self._seed_pending(store, "d_old")

        await hub._handle_change_notification(self._notification("d_new"))

        ev_old = await store.get_deploy_event("d_old")
        ev_new = await store.get_deploy_event("d_new")
        assert ev_old["status"] == "rejected"
        assert ev_old["reject_reason"] == "superseded by d_new"
        assert ev_new["status"] == "pending"

    async def test_does_not_touch_deploying(
        self, hub: WebSocketHub, store: EventStore
    ):
        """d_running DEPLOYING + d_new change_notification → d_running 그대로
        (이미 실행 중인 작업은 보호)."""
        await self._seed_pending(store, "d_running")
        await store.update_deploy_status(
            "d_running", DeployStatus.DEPLOYING
        )

        await hub._handle_change_notification(self._notification("d_new"))

        ev_running = await store.get_deploy_event("d_running")
        ev_new = await store.get_deploy_event("d_new")
        assert ev_running["status"] == "deploying"
        assert ev_running["reject_reason"] is None
        assert ev_new["status"] == "pending"

    async def test_other_branch_untouched(
        self, hub: WebSocketHub, store: EventStore
    ):
        """다른 branch의 PENDING은 영향 없음."""
        await self._seed_pending(store, "d_dev", branch="dev")

        await hub._handle_change_notification(
            self._notification("d_main", branch="main")
        )

        ev_dev = await store.get_deploy_event("d_dev")
        assert ev_dev["status"] == "pending"
        assert ev_dev["reject_reason"] is None

    async def test_broadcasts_supersede_status_change(
        self, hub: WebSocketHub, store: EventStore
    ):
        """supersede 발생 시 status_change broadcast가 reject_reason과 함께
        대시보드로 전달되어야 한다 (App.tsx의 startsWith('superseded') 분기가
        토스트와 setPending refetch를 트리거)."""
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_pending(store, "d_old")

        await hub._handle_change_notification(self._notification("d_new"))

        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        rejected = [
            p for p in sent
            if p.get("type") == "status_change"
            and p.get("status") == "rejected"
        ]
        assert len(rejected) == 1
        assert rejected[0]["deploy_id"] == "d_old"
        assert rejected[0]["reject_reason"] == "superseded by d_new"
        # new_pending도 함께 broadcast됨 (기존 동작)
        new_pendings = [p for p in sent if p.get("type") == "new_pending"]
        assert len(new_pendings) == 1
        assert new_pendings[0]["deploy_id"] == "d_new"

    async def test_duplicate_change_notification_is_ignored(
        self, hub: WebSocketHub, store: EventStore
    ):
        """Duplicate deterministic deploy_id should not rebroadcast or
        re-run supersede."""
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        notification = self._notification("d_same")

        await hub._handle_change_notification(notification)
        await hub._handle_change_notification(notification)

        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        new_pendings = [p for p in sent if p.get("type") == "new_pending"]
        assert len(new_pendings) == 1


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
        """Integration: heartbeat timeout goes through the hub disconnect policy."""
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

        stale = await registry.check_stale()
        assert stale == ["n1"]
        assert registry.get_node("n1") is not None
        ev_before = await store.get_deploy_event("d1")
        assert ev_before["status"] == "deploying"

        await hub._disconnect_node(
            "n1",
            reason="heartbeat_timeout",
            error="node disconnected (heartbeat timeout)",
            close_ws=False,
        )

        assert registry.get_node("n1") is None
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
