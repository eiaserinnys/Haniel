"""Tests for WebSocketHub — node/dashboard WS handling, broadcast, send_to_node."""

import asyncio
import contextlib
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import (
    ChangeNotification,
    DeployApproval,
    DeployResult,
    DeployStatus,
    NodeHello,
    RepoReconciliation,
)


def approval(deploy_id: str = "d1", approved_by: str = "dashboard") -> DeployApproval:
    return DeployApproval(
        deploy_id=deploy_id,
        orchestrator_attempt_id=f"attempt-{deploy_id}",
        execution_mode="execute",
        connection_generation="g1",
        approved_by=approved_by,
    )


def deploy_result(
    deploy_id: str,
    *,
    node_id: str = "n1",
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
) -> DeployResult:
    return DeployResult(
        deploy_id=deploy_id,
        node_id=node_id,
        status=status,
        error=error,
        duration_ms=duration_ms,
        orchestrator_attempt_id=f"attempt-{deploy_id}",
        connection_generation="g1",
    )


async def begin_attempt(
    store: EventStore,
    deploy_id: str,
    *,
    generation: str = "g1",
    deadline_at: str | None = None,
) -> None:
    assert store.attempts is not None
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=f"attempt-{deploy_id}",
        deploy_id=deploy_id,
        connection_generation=generation,
        source="manual_single",
        approved_by="test",
        deadline_at=deadline_at
        or (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )


def settled(
    deploy_id: str,
    *,
    node_id: str = "n1",
    local_head: str | None = None,
    remote_head: str | None = None,
) -> RepoReconciliation:
    target = deploy_id.rsplit(":", 1)[-1]
    return RepoReconciliation(
        phase="settled",
        deploy_id=deploy_id,
        node_id=node_id,
        repo="r" if ":repo:" not in deploy_id else "repo",
        branch="main",
        local_head=local_head or target,
        remote_head=remote_head or target,
        orchestrator_attempt_id=f"attempt-{deploy_id}",
        connection_generation="g1",
    )


def bind_generation(
    hub: WebSocketHub, *, node_id: str = "n1", generation: str = "g1"
) -> None:
    hub.deploy_coordinator._generations[node_id] = generation


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

        msg = approval(approved_by="test")
        result = await hub.send_to_node("n1", msg)

        assert result is True
        ws.send_text.assert_called_once_with(msg.model_dump_json())

    async def test_returns_false_for_unknown_node(self, hub: WebSocketHub):
        msg = approval()
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

        msg = approval()
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

        result = await hub.send_to_node("n1", approval())

        assert result is False
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        assert {
            "type": "node_disconnected",
            "node_id": "n1",
            "reason": "send_failed",
        } in sent


class TestReconnectReplacement:
    async def test_reconnect_closes_replaced_socket(
        self, hub: WebSocketHub, registry: NodeRegistry
    ):
        old_ws = AsyncMock()
        hello = NodeHello(
            node_id="n1",
            token="test-token",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(old_ws, hello)

        new_ws = AsyncMock()
        new_ws.receive_text.side_effect = [
            hello.model_dump_json(),
            WebSocketDisconnect(code=1000),
        ]

        await hub.handle_node_ws(new_ws)

        old_ws.close.assert_awaited_once_with(code=4000, reason="node reconnected")

    async def test_stale_disconnect_does_not_remove_reconnected_node(
        self, hub: WebSocketHub, registry: NodeRegistry, store: EventStore
    ):
        old_ws = AsyncMock()
        new_ws = AsyncMock()
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        hello = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(old_ws, hello)
        await registry.register(new_ws, hello)

        await hub._disconnect_node(
            "n1",
            reason="ws_closed",
            error="node disconnected",
            close_ws=False,
            websocket=old_ws,
        )

        node = registry.get_node("n1")
        assert node is not None
        assert node.websocket is new_ws
        ws_dash.send_text.assert_not_called()
        nodes = await store.get_nodes()
        assert nodes[0]["connected"] == 1


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
            deploy_id=deploy_id,
            node_id=node_id,
            repo=repo,
            branch=branch,
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

    def _notification(
        self,
        deploy_id: str,
        node_id: str = "n1",
        repo: str = "r",
        branch: str = "main",
        detected_at: str = "2026-05-19T00:00:00Z",
    ) -> ChangeNotification:
        return ChangeNotification(
            deploy_id=deploy_id,
            node_id=node_id,
            repo=repo,
            branch=branch,
            commits=["hN msgN"],
            affected_services=[],
            diff_stat=None,
            detected_at=detected_at,
        )

    async def test_late_older_notification_does_not_open_or_push_pending(
        self, hub: WebSocketHub, store: EventStore
    ):
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await hub._handle_change_notification(
            self._notification("new", detected_at="2026-05-20T00:00:00Z")
        )
        ws_dash.reset_mock()

        await hub._handle_change_notification(
            self._notification("old-late", detected_at="2026-05-19T00:00:00Z")
        )

        assert [row["deploy_id"] for row in await store.get_active_deploys()] == [
            "new"
        ]
        sent = [json.loads(call.args[0]) for call in ws_dash.send_text.call_args_list]
        assert not any(item.get("type") == "new_pending" for item in sent)
        assert sent == [
            {
                "type": "status_change",
                "deploy_id": "old-late",
                "status": "rejected",
                "node_id": "n1",
                "reject_reason": "superseded by new",
            }
        ]

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

    async def test_does_not_touch_deploying(self, hub: WebSocketHub, store: EventStore):
        """d_running DEPLOYING + d_new change_notification → d_running 그대로
        (이미 실행 중인 작업은 보호)."""
        await self._seed_pending(store, "d_running")
        await begin_attempt(store, "d_running")

        await hub._handle_change_notification(self._notification("d_new"))

        ev_running = await store.get_deploy_event("d_running")
        ev_new = await store.get_deploy_event("d_new")
        assert ev_running["status"] == "deploying"
        assert ev_running["reject_reason"] is None
        assert ev_new["status"] == "pending"

    async def test_other_branch_untouched(self, hub: WebSocketHub, store: EventStore):
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
            p
            for p in sent
            if p.get("type") == "status_change" and p.get("status") == "rejected"
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
        await begin_attempt(store, "d1")

        result = deploy_result("d1", status="success", duration_ms=5000)
        await hub._handle_deploy_result(result)

        event = await store.get_deploy_event("d1")
        assert event["status"] == "deploying"

        bind_generation(hub)
        await hub.deploy_coordinator.handle_settled(settled("d1"))
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
        await begin_attempt(store, "d2")

        result = deploy_result(
            "d2", status="failed", error="exit code 1", duration_ms=3400
        )
        await hub._handle_deploy_result(result)

        event = await store.get_deploy_event("d2")
        assert event["status"] == "pending"
        assert event["error"] is None
        retry = await store.attempts.get_retry_requirement("d2")
        assert retry["source_orchestrator_attempt_id"] == "attempt-d2"
        history = await store.get_deploy_history()
        failed = next(row for row in history if row["deploy_id"] == "attempt:attempt-d2")
        assert failed["error"] == "exit code 1"
        assert failed["duration_ms"] == 3400


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
            deploy_id="d1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await begin_attempt(store, "d1")

        result = deploy_result("d1", status="success", duration_ms=5000)
        await hub._handle_deploy_result(result)
        push.notify.assert_not_called()
        bind_generation(hub)
        await hub.deploy_coordinator.handle_settled(settled("d1"))
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
            deploy_id="d2",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await begin_attempt(store, "d2")

        result = deploy_result("d2", status="failed", error="exit 1")
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

    async def test_null_push_service_is_noop(
        self, hub: WebSocketHub, store: EventStore
    ):
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


class TestRepoReconciliation:
    DEPLOY_ID = "n1:repo:main:remote"

    async def _seed(self, store: EventStore) -> None:
        await store.create_deploy_event(
            deploy_id=self.DEPLOY_ID,
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["remote change"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

    def _message(self, phase: str, *, in_sync: bool) -> RepoReconciliation:
        return RepoReconciliation(
            phase=phase,
            deploy_id=self.DEPLOY_ID,
            node_id="n1",
            repo="repo",
            branch="main",
            local_head="remote" if in_sync else "local",
            remote_head="remote",
            orchestrator_attempt_id=(
                f"attempt-{self.DEPLOY_ID}" if phase != "observed" else None
            ),
            connection_generation=("g1" if phase == "settled" else None),
        )

    async def test_auto_success_equal_removes_stale_pending(self, hub, store):
        await self._seed(store)

        await hub._repo_reconciler.handle(self._message("observed", in_sync=True))

        assert await store.get_active_deploys() == []
        event = await store.get_deploy_event(self.DEPLOY_ID)
        assert event["status"] == "success"

    async def test_failed_equal_observation_stays_retryable_and_late_result_is_ignored(
        self, hub, store
    ):
        await self._seed(store)
        await begin_attempt(store, self.DEPLOY_ID)
        await hub._handle_deploy_result(
            deploy_result(self.DEPLOY_ID, status="failed", error="hook failed")
        )
        await hub._repo_reconciler.handle(self._message("observed", in_sync=True))

        reopened = await store.get_deploy_event(self.DEPLOY_ID)
        assert reopened["status"] == "pending"
        history = await store.get_deploy_history()
        assert any(row.get("error") == "hook failed" for row in history)

        await hub._handle_deploy_result(
            deploy_result(self.DEPLOY_ID, status="success")
        )
        assert (await store.get_deploy_event(self.DEPLOY_ID))["status"] == "pending"

    async def test_raw_success_then_settled_mismatch_reopens_retryable(self, hub, store):
        await self._seed(store)
        await begin_attempt(store, self.DEPLOY_ID)
        await hub._handle_deploy_result(
            deploy_result(self.DEPLOY_ID, status="success")
        )

        bind_generation(hub)
        await hub.deploy_coordinator.handle_settled(
            self._message("settled", in_sync=False)
        )

        event = await store.get_deploy_event(self.DEPLOY_ID)
        assert event["status"] == "pending"
        history = await store.get_deploy_history()
        failure = next(
            row for row in history
            if row["deploy_id"] == f"attempt:attempt-{self.DEPLOY_ID}"
        )
        assert failure["terminal_kind"] == "settled_head_mismatch"
        assert "local=local" in failure["error"]


class TestDeployTimeout:
    """Persisted orchestration IDs, not hub memory, own deploy deadlines."""

    async def _seed_deploying(
        self, store: EventStore, deploy_id: str, node_id: str = "n1"
    ) -> None:
        await store.create_deploy_event(
            deploy_id=deploy_id,
            node_id=node_id,
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await begin_attempt(store, deploy_id)

    async def test_timeout_reopens_retryable_and_broadcasts_pending(self, registry, store):
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=0.05)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await store.create_deploy_event(
            deploy_id="d1", node_id="n1", repo="r", branch="main",
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await begin_attempt(
            store,
            "d1",
            deadline_at=(datetime.now(timezone.utc) + timedelta(milliseconds=30)).isoformat(),
        )
        await hub.deploy_coordinator.restore_deadlines()
        await asyncio.sleep(0.12)

        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        timeouts = [
            p
            for p in sent
            if p.get("type") == "status_change" and p.get("status") == "pending"
        ]
        assert len(timeouts) == 1
        assert timeouts[0]["deploy_id"] == "d1"
        assert timeouts[0]["node_id"] == "n1"
        ev = await store.get_deploy_event("d1")
        assert ev["status"] == "pending"
        history = await store.get_deploy_history()
        timeout = next(row for row in history if row["deploy_id"] == "attempt:attempt-d1")
        assert timeout["terminal_kind"] == "attempt_timeout"
        assert "deploy result and settled HEAD evidence" in timeout["error"]

    async def test_raw_success_keeps_deadline_until_settled(self, registry, store):
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}
        await self._seed_deploying(store, "d1")
        await hub.deploy_coordinator.restore_deadlines()
        timer_key = "attempt:attempt-d1"
        timeout_task = hub.deploy_coordinator._timers[timer_key]

        result = deploy_result("d1", status="success", duration_ms=500)
        await hub._handle_deploy_result(result)

        assert timer_key in hub.deploy_coordinator._timers
        assert (await store.get_deploy_event("d1"))["status"] == "deploying"
        bind_generation(hub)
        await hub.deploy_coordinator.handle_settled(settled("d1"))
        assert timer_key not in hub.deploy_coordinator._timers
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_task
        assert timeout_task.done()
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        statuses = [p["status"] for p in sent if p.get("type") == "status_change"]
        assert statuses == ["success"]

    async def test_disconnect_keeps_active_attempt_for_result_or_deadline(self, registry, store):
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        await store.create_deploy_event(
            deploy_id="d1", node_id="n1", repo="r", branch="main",
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        generation = hub.deploy_coordinator.register_connection("n1")
        await begin_attempt(store, "d1", generation=generation)
        await hub.deploy_coordinator.restore_deadlines()

        await hub.deploy_coordinator.disconnect("n1", generation)

        active = await store.attempts.get_active_attempts()
        assert [row["orchestrator_attempt_id"] for row in active] == ["attempt-d1"]
        assert (await store.get_deploy_event("d1"))["status"] == "deploying"

    async def test_restart_closes_old_probe_without_opening_attempt(self, registry, store):
        hub = WebSocketHub(registry, store, token="t", deploy_timeout_sec=10.0)
        await store.create_deploy_event(
            deploy_id="d1", node_id="n1", repo="r", branch="main",
            commits=["h msg"], affected_services=[], diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.attempts.create_probe(
            probe_id="p1", deploy_id="d1", connection_generation="old-generation",
            deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            manual_retry=False,
        )

        await hub.deploy_coordinator.restore_deadlines()

        assert await store.attempts.get_active_probes() == []
        assert await store.attempts.get_active_attempts() == []
        assert (await store.get_deploy_event("d1"))["status"] == "pending"
        history = await store.get_deploy_history()
        assert next(row for row in history if row["deploy_id"] == "preflight:p1")[
            "terminal_kind"
        ] == "preflight_disconnected"


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
            deploy_id=deploy_id,
            node_id=node_id,
            repo=repo,
            branch=branch,
            commits=["h"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

    async def test_creation_time_supersede_chain_is_not_rewritten_on_approval(
        self, hub: WebSocketHub, store
    ):
        await self._seed_pending(store, "d1")
        await asyncio.sleep(0.005)
        await self._seed_pending(store, "d2")
        await asyncio.sleep(0.005)
        await self._seed_pending(store, "d3")

        result = await hub.supersede_pending("n1", "r", "main")

        assert result == []
        ev1 = await store.get_deploy_event("d1")
        ev2 = await store.get_deploy_event("d2")
        assert ev1["status"] == "rejected"
        assert ev1["reject_reason"] == "superseded by d2"
        assert ev2["status"] == "rejected"
        assert ev2["reject_reason"] == "superseded by d3"
        ev3 = await store.get_deploy_event("d3")
        assert ev3["status"] == "pending"

    async def test_skips_other_branches(self, hub: WebSocketHub, store):
        # Only the dev-branch deploy exists; supersede_pending on main → no-op.
        # Verifies that a PENDING entry on a different branch is untouched.
        await self._seed_pending(store, "d_dev", branch="dev")

        result = await hub.supersede_pending("n1", "r", "main")

        assert result == []
        ev_dev = await store.get_deploy_event("d_dev")
        assert ev_dev["status"] == "pending"
        assert ev_dev["reject_reason"] is None

    async def test_returns_empty_when_no_others(self, hub: WebSocketHub, store):
        await self._seed_pending(store, "d_alone")
        result = await hub.supersede_pending("n1", "r", "main")
        assert result == []
        ev = await store.get_deploy_event("d_alone")
        assert ev["status"] == "pending"

    async def test_old_approval_cleanup_never_rejects_newer_pending(
        self, hub: WebSocketHub, store
    ):
        await self._seed_pending(store, "old")
        await begin_attempt(store, "old")
        await asyncio.sleep(0.005)
        await self._seed_pending(store, "new")

        result = await hub.supersede_pending("n1", "r", "main")

        assert result == []
        assert (await store.get_deploy_event("old"))["status"] == "deploying"
        assert (await store.get_deploy_event("new"))["status"] == "pending"
