"""승인 화면이 읽는 값(카드 #1896): 적용 방식, 저장소별 fetch 시각, 서빙 커밋, 진행 단계, 자기 갱신 끊김."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from state_support import to_deploying, to_success
from test_api import _register_node
from test_deploy_attempt_coordinator import Harness
from haniel_orch.api import create_api_routes
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import (
    ChangeNotification,
    DeployProgress,
    DeployResult,
    NodeHello,
    NodeStatus,
    RepoFetchStatus,
    parse_node_message,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
async def registry(store: EventStore):
    return NodeRegistry(store)


@pytest.fixture
async def hub(registry: NodeRegistry, store: EventStore):
    return WebSocketHub(registry, store, token="test-token")


@pytest.fixture
def client(hub: WebSocketHub, store: EventStore):
    return TestClient(Starlette(routes=create_api_routes(hub, store)))


def change(deploy_id: str = "n1:r:main:" + SHA_A, **extra) -> ChangeNotification:
    node_id, repo, branch, head = deploy_id.split(":", 3)
    return ChangeNotification(
        deploy_id=deploy_id,
        node_id=node_id,
        repo=repo,
        branch=branch,
        commits=[f"{head[:7]} change"],
        affected_services=["web"],
        detected_at="2026-11-26T00:00:00+00:00",
        target_head=head,
        **extra,
    )


def hello(node_id: str = "n1", **extra) -> NodeHello:
    return NodeHello(
        node_id=node_id,
        token="t",
        hostname="host",
        os="Linux",
        arch="x86_64",
        haniel_version="1.0",
        **extra,
    )


class TestWireCompatibility:
    def test_old_node_messages_parse_without_the_new_fields(self):
        old_change = {
            "type": "change_notification",
            "deploy_id": "n:r:main:abc",
            "node_id": "n",
            "repo": "r",
            "branch": "main",
            "commits": ["abc c"],
            "affected_services": [],
            "detected_at": "2026-11-26T00:00:00+00:00",
        }
        assert parse_node_message(json.dumps(old_change)).apply_policy is None
        status = parse_node_message(json.dumps({"type": "node_status", "node_id": "n"}))
        assert status.repos is None and status.poll_interval is None

    def test_new_fields_parse_and_unknown_fields_are_ignored(self):
        status = parse_node_message(
            json.dumps(
                {
                    "type": "node_status",
                    "node_id": "n",
                    "poll_interval": 60,
                    "repos": [{"repo": "r", "last_fetch": "2026-11-26T00:00:00+00:00"}],
                    "future_field": 1,
                }
            )
        )
        assert isinstance(status, NodeStatus)
        assert status.poll_interval == 60
        assert status.repos[0].repo == "r"

    def test_apply_policy_accepts_only_the_two_values(self):
        assert change(apply_policy="auto").apply_policy == "auto"
        assert change(apply_policy="approval").apply_policy == "approval"
        with pytest.raises(ValueError):
            change(apply_policy="manual")


class TestApplyPolicy:
    async def test_pending_item_carries_the_policy_the_node_sent(
        self, hub, store, client
    ):
        await hub._handle_change_notification(change(apply_policy="approval"))

        item = client.get("/api/orch/pending").json()["deploys"][0]
        assert item["apply_policy"] == "approval"

    async def test_old_node_leaves_it_null(self, hub, store, client):
        await hub._handle_change_notification(change())

        assert client.get("/api/orch/pending").json()["deploys"][0]["apply_policy"] is None

    async def test_a_repeat_notification_updates_a_pending_row(
        self, hub, store, client
    ):
        """옛 노드가 만든 줄도, 설정이 바뀐 줄도 노드가 다시 알리면 맞춘다."""
        await hub._handle_change_notification(change())
        await hub._handle_change_notification(change(apply_policy="approval"))
        assert client.get("/api/orch/pending").json()["deploys"][0]["apply_policy"] == "approval"

        await hub._handle_change_notification(change(apply_policy="auto"))
        assert client.get("/api/orch/pending").json()["deploys"][0]["apply_policy"] == "auto"

        await hub._handle_change_notification(change())
        assert client.get("/api/orch/pending").json()["deploys"][0]["apply_policy"] == "auto"


class TestNodeFetchInfo:
    async def test_hello_and_heartbeat_values_reach_the_nodes_list(
        self, hub, registry, store, client
    ):
        await _register_node(
            hub,
            registry,
            AsyncMock(),
            hello(
                repos=[{"repo": "r", "last_fetch": "2026-11-26T00:00:00+00:00"}],
                poll_interval=60,
            ),
        )
        node = client.get("/api/orch/nodes").json()["nodes"][0]
        assert node["poll_interval"] == 60
        assert node["repo_fetches"] == [
            {"repo": "r", "last_fetch": "2026-11-26T00:00:00+00:00"}
        ]

        await registry.heartbeat(
            "n1",
            repos=[
                RepoFetchStatus(repo="r", last_fetch="2026-11-26T00:01:00+00:00")
            ],
            poll_interval=30,
        )
        node = client.get("/api/orch/nodes").json()["nodes"][0]
        assert node["poll_interval"] == 30
        assert node["repo_fetches"][0]["last_fetch"] == "2026-11-26T00:01:00+00:00"

    async def test_old_node_and_disconnected_node_report_null(
        self, hub, registry, store, client
    ):
        await _register_node(hub, registry, AsyncMock(), hello())
        await store.upsert_node("n2", "host-2", "Linux", "x86_64", "1.0")

        nodes = {n["node_id"]: n for n in client.get("/api/orch/nodes").json()["nodes"]}
        for node_id in ("n1", "n2"):
            assert nodes[node_id]["poll_interval"] is None
            assert nodes[node_id]["repo_fetches"] is None


class TestServingCommits:
    async def _succeed(self, hub, store, deploy_id, repo_head):
        await hub._handle_change_notification(change(deploy_id))
        event = await store.get_deploy_event(deploy_id)
        await to_deploying(store, deploy_id)
        await to_success(store, deploy_id)
        return event

    async def test_latest_success_per_node_and_repo(self, hub, store, client):
        await store.upsert_node("n1", "host", "Linux", "x86_64", "1.0")
        await self._succeed(hub, store, f"n1:r:main:{SHA_A}", SHA_A)
        await self._succeed(hub, store, f"n1:r:main:{SHA_B}", SHA_B)
        await self._succeed(hub, store, f"n1:other:main:{SHA_A}", SHA_A)
        await hub._handle_change_notification(change(f"n1:r:main:{'c' * 40}"))

        node = client.get("/api/orch/nodes").json()["nodes"][0]
        serving = {row["repo"]: row for row in node["serving"]}
        assert set(serving) == {"r", "other"}
        assert serving["r"]["target_head"] == SHA_B
        assert serving["r"]["deploy_id"] == f"n1:r:main:{SHA_B}"
        assert serving["r"]["deployed_at"]
        assert serving["other"]["target_head"] == SHA_A

    async def test_node_without_success_has_an_empty_list(self, store, client):
        await store.upsert_node("n1", "host", "Linux", "x86_64", "1.0")

        assert client.get("/api/orch/nodes").json()["nodes"][0]["serving"] == []


class TestSelfUpdateDisconnect:
    async def _deploying_self_update(self, hub, store, node_id="n1"):
        deploy_id = f"{node_id}:haniel:main:{SHA_A}"
        await hub._handle_change_notification(change(deploy_id, is_self_update=True))
        await to_deploying(store, deploy_id)
        return deploy_id

    async def test_disconnect_broadcast_names_a_deploying_self_update(
        self, hub, store
    ):
        deploy_id = await self._deploying_self_update(hub, store)
        dashboard = AsyncMock()
        hub._dashboard_connections.add(dashboard)

        await hub._on_current_node_disconnect("n1", "ws_closed", "node disconnected")

        event = json.loads(dashboard.send_text.call_args_list[0].args[0])
        assert event == {
            "type": "node_disconnected",
            "node_id": "n1",
            "reason": "ws_closed",
            "self_update_deploying": True,
            "self_update_deploy_id": deploy_id,
        }

    async def test_a_pending_self_update_is_not_deploying(self, hub, store):
        await hub._handle_change_notification(
            change(f"n1:haniel:main:{SHA_A}", is_self_update=True)
        )
        dashboard = AsyncMock()
        hub._dashboard_connections.add(dashboard)

        await hub._on_current_node_disconnect("n1", "ws_closed", "node disconnected")

        event = json.loads(dashboard.send_text.call_args_list[0].args[0])
        assert event["self_update_deploying"] is False
        assert event["self_update_deploy_id"] is None

    async def test_lookup_failure_still_broadcasts_and_cleans_up(
        self, hub, store, monkeypatch
    ):
        async def broken_lookup(node_id=None):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(store, "get_deploying_self_updates", broken_lookup)
        cleanup = AsyncMock()
        monkeypatch.setattr(hub, "_cleanup_orphan_commands", cleanup)
        dashboard = AsyncMock()
        hub._dashboard_connections.add(dashboard)

        await hub._on_current_node_disconnect("n1", "ws_closed", "node disconnected")

        event = json.loads(dashboard.send_text.call_args_list[0].args[0])
        assert event["type"] == "node_disconnected"
        assert event["self_update_deploying"] is False
        assert event["self_update_deploy_id"] is None
        cleanup.assert_awaited_once_with("n1", error="node disconnected")

    async def test_nodes_list_marks_only_the_node_with_a_deploying_self_update(
        self, hub, store, client
    ):
        await store.upsert_node("n1", "host-1", "Linux", "x86_64", "1.0")
        await store.upsert_node("n2", "host-2", "Linux", "x86_64", "1.0")
        deploy_id = await self._deploying_self_update(hub, store, "n1")

        nodes = {n["node_id"]: n for n in client.get("/api/orch/nodes").json()["nodes"]}
        assert nodes["n1"]["self_update_deploying"] is True
        assert nodes["n1"]["self_update_deploy_id"] == deploy_id
        assert nodes["n2"]["self_update_deploying"] is False
        assert nodes["n2"]["self_update_deploy_id"] is None


class TestProgressStage:
    async def _deploying(self, store: EventStore):
        deploy_id = "n:r:main:target"
        await store.create_deploy_event(
            deploy_id=deploy_id,
            node_id="n",
            repo="r",
            branch="main",
            commits=["target change"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-11-26T00:00:00Z",
            target_head="target",
        )
        await store.attempts.begin_normal_attempt(
            orchestrator_attempt_id="a1",
            deploy_id=deploy_id,
            connection_generation="g1",
            current_generation="g1",
            source="manual_single",
            approved_by="director",
            deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        )
        return deploy_id

    def _progress(self, stage: str) -> DeployProgress:
        return DeployProgress(
            deploy_id="n:r:main:target",
            node_id="n",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            stage=stage,
        )

    async def test_latest_stage_is_stored_broadcast_and_listed(self, store):
        deploy_id = await self._deploying(store)
        harness = Harness(store)
        try:
            await harness.coordinator.handle_progress(self._progress("build"))
            await harness.coordinator.handle_progress(self._progress("build"))
            await harness.coordinator.handle_progress(self._progress("starting"))

            assert (await store.attempts.get_attempt("a1"))["progress_stage"] == "starting"
            (item,) = await store.get_active_deploys()
            assert item["deploy_id"] == deploy_id
            assert item["progress_stage"] == "starting"
            assert [b["stage"] for b in harness.broadcasts if b["type"] == "deploy_progress"] == [
                "build",
                "starting",
            ]
            assert {
                "type": "deploy_progress",
                "deploy_id": deploy_id,
                "node_id": "n",
                "orchestrator_attempt_id": "a1",
                "stage": "starting",
            } in harness.broadcasts
        finally:
            await harness.close()

    async def test_pending_item_and_unreported_deploy_have_no_stage(self, store):
        deploy_id = "n:r:main:other"
        await store.create_deploy_event(
            deploy_id=deploy_id,
            node_id="n",
            repo="r",
            branch="main",
            commits=["c"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-11-26T00:00:00Z",
            target_head="other",
        )
        (item,) = await store.get_active_deploys()
        assert item["progress_stage"] is None

        await to_deploying(store, deploy_id)
        (item,) = await store.get_active_deploys()
        assert item["progress_stage"] is None


class TestFailedStage:
    _deploying = TestProgressStage._deploying
    _progress = TestProgressStage._progress

    async def _fail_after(self, store, harness, *stages: str):
        await self._deploying(store)
        for stage in stages:
            await harness.coordinator.handle_progress(self._progress(stage))
        await harness.coordinator.handle_result(
            DeployResult(
                deploy_id="n:r:main:target",
                node_id="n",
                status="failed",
                error="deployment failed: boom",
                orchestrator_attempt_id="a1",
                connection_generation="g1",
            )
        )
        rows = await store.get_deploy_history()
        return next(row for row in rows if row["deploy_id"] == "attempt:a1")

    async def test_rollback_keeps_the_stage_it_failed_in(self, store):
        harness = Harness(store)
        try:
            row = await self._fail_after(
                store, harness, "build", "starting", "recovering", "recovering"
            )
            assert row["failed_stage"] == "starting"
            assert row["recovery_attempted"] is True
            assert (await store.attempts.get_attempt("a1"))["progress_stage"] == "recovering"
        finally:
            await harness.close()

    async def test_failure_without_recovering_reports_the_last_stage(self, store):
        harness = Harness(store)
        try:
            row = await self._fail_after(store, harness, "build", "preflight")
            assert row["failed_stage"] == "preflight"
            assert row["recovery_attempted"] is False
        finally:
            await harness.close()

    async def test_failure_without_any_progress_reports_null(self, store):
        harness = Harness(store)
        try:
            row = await self._fail_after(store, harness)
            assert row["failed_stage"] is None
            assert row["recovery_attempted"] is None
        finally:
            await harness.close()

    async def test_recovering_as_the_first_report_has_no_failed_stage(self, store):
        harness = Harness(store)
        try:
            row = await self._fail_after(store, harness, "recovering")
            assert row["failed_stage"] is None
            assert row["recovery_attempted"] is True
        finally:
            await harness.close()

    async def test_first_recovering_wins_over_later_stages(self, store):
        harness = Harness(store)
        try:
            row = await self._fail_after(
                store, harness, "migrating", "recovering", "starting"
            )
            assert row["failed_stage"] == "migrating"
            assert row["recovery_attempted"] is True
        finally:
            await harness.close()

    async def test_probe_and_node_owned_rows_carry_null_fields(self, store):
        await store._db.execute(
            "INSERT INTO node_deploy_attempts (node_attempt_id, deploy_id, node_id, repo, branch, "
            "target_head, trigger, outcome, started_local_head, started_at, completed_at, error) "
            "VALUES ('na1','n:r:main:t','n','r','main','t','startup','failed','p',"
            "'2026-11-26T00:00:00Z','2026-11-26T00:01:00Z','boom')"
        )
        await store._db.commit()
        rows = await store.get_deploy_history()
        node_row = next(r for r in rows if r["deploy_id"] == "node-attempt:na1")
        assert node_row["failed_stage"] is None
        assert node_row["recovery_attempted"] is None


class TestBlockedBySelfUpdate:
    async def test_message_follows_the_self_update_status(self, hub, store, client):
        self_id = f"n1:haniel:main:{SHA_A}"
        other_id = f"n1:r:main:{SHA_B}"
        await hub._handle_change_notification(change(self_id, is_self_update=True))
        await hub._handle_change_notification(change(other_id))

        pending = client.post("/api/orch/approve", json={"deploy_id": other_id})
        assert pending.status_code == 409
        assert pending.json()["error"] == (
            f"self-update {self_id} must be approved or postponed "
            "before other deployments on node n1"
        )
        assert pending.json()["blocking_self_update"]["status"] == "pending"

        await to_deploying(store, self_id)
        deploying = client.post("/api/orch/approve", json={"deploy_id": other_id})
        assert deploying.status_code == 409
        assert deploying.json()["error"] == (
            f"self-update {self_id} is deploying on node n1; "
            "approve other deployments after it finishes"
        )
        assert deploying.json()["blocking_self_update"]["status"] == "deploying"
