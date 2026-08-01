"""Tests for REST API routes — approve, reject, approve-all, queries."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from haniel_orch.api import create_api_routes
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import DeployApproval, DeployStatus, NodeHello


@pytest.fixture
async def registry(store: EventStore):
    return NodeRegistry(store)


@pytest.fixture
async def hub(registry: NodeRegistry, store: EventStore):
    return WebSocketHub(registry, store, token="test-token")


@pytest.fixture
def routes(hub: WebSocketHub, store: EventStore):
    return create_api_routes(hub, store)


async def _seed_pending(
    store: EventStore,
    deploy_id: str = "d1",
    node_id: str = "n1",
    repo: str = "myrepo",
    branch: str = "main",
):
    """Helper: create a pending deploy event."""
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node_id,
        repo=repo,
        branch=branch,
        commits=["abc1234 fix: something"],
        affected_services=["bot"],
        diff_stat="+10 -3",
        detected_at="2026-01-01T00:00:00Z",
    )


class TestGetPending:
    async def test_returns_pending_deploys(self, hub, store, routes):
        await _seed_pending(store, "d1", branch="main")
        await _seed_pending(store, "d2", branch="dev")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["deploys"]) == 2

    async def test_repairs_stale_pending_deploys(self, hub, store, routes):
        await _seed_pending(store, "old")
        await asyncio.sleep(0.005)
        await _seed_pending(store, "new")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        assert [d["deploy_id"] for d in resp.json()["deploys"]] == ["new"]

        old = await store.get_deploy_event("old")
        assert old["status"] == "rejected"
        assert old["reject_reason"] == "superseded by new"

    async def test_empty_when_none(self, hub, store, routes):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        assert resp.json()["deploys"] == []
        assert set(resp.json()) == {"deploys"}

    async def test_failure_detail_is_history_only(
        self, hub, store, routes
    ):
        await _seed_pending(store, "failed")
        generation = hub.deploy_coordinator.register_connection("n1")
        await store.attempts.begin_normal_attempt(
            orchestrator_attempt_id="attempt-failed",
            deploy_id="failed",
            connection_generation=generation,
            source="manual_single",
            approved_by="dashboard",
            deadline_at="2099-01-01T00:00:00+00:00",
        )
        await store.attempts.record_result(
            deploy_id="failed",
            orchestrator_attempt_id="attempt-failed",
            connection_generation=generation,
            status="failed",
            error="post-pull failed",
            duration_ms=12,
        )

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        client = TestClient(Starlette(routes=routes))
        data = client.get("/api/orch/pending").json()

        assert [row["deploy_id"] for row in data["deploys"]] == ["failed"]
        assert data["deploys"][0]["error"] is None
        history = client.get("/api/orch/history").json()["deploys"]
        assert any(row["error"] == "post-pull failed" for row in history)

    async def test_includes_deploying(self, hub, store, routes):
        """/api/orch/pending returns active (pending + deploying), not just pending."""
        await _seed_pending(store, "d_pending")
        await _seed_pending(store, "d_deploying", branch="dev")
        await _seed_pending(store, "d_rejected", branch="feature")
        await store.update_deploy_status("d_deploying", DeployStatus.DEPLOYING)
        await store.update_deploy_status(
            "d_rejected", DeployStatus.REJECTED, reject_reason="not ready"
        )

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        ids = {d["deploy_id"] for d in resp.json()["deploys"]}
        assert ids == {"d_pending", "d_deploying"}

    async def test_rejects_pending_for_renamed_node(
        self, hub, registry, store, routes
    ):
        """A disconnected node_id with the same hostname as a connected node
        is a renamed node; its stale pending deploys should not keep asking
        for approval."""
        await store.upsert_node(
            "old-node", "same-host", "Linux", "x86_64", "0.14.2", connected=False
        )
        await registry.register(
            AsyncMock(),
            NodeHello(
                node_id="new-node",
                token="t",
                hostname="same-host",
                os="Linux",
                arch="x86_64",
                haniel_version="0.14.2",
            ),
        )
        await _seed_pending(
            store,
            "old-node:myrepo:main:aaaaaaa",
            node_id="old-node",
            repo="myrepo",
            branch="main",
        )
        await asyncio.sleep(0.005)
        await _seed_pending(
            store,
            "new-node:myrepo:main:bbbbbbb",
            node_id="new-node",
            repo="myrepo",
            branch="main",
        )
        # Node connection lifecycle owns this repair. The GET route is a pure read.
        await hub.cleanup_pending_for_renamed_nodes()

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        assert [d["deploy_id"] for d in resp.json()["deploys"]] == [
            "new-node:myrepo:main:bbbbbbb"
        ]

        old = await store.get_deploy_event("old-node:myrepo:main:aaaaaaa")
        assert old["status"] == "rejected"
        assert old["reject_reason"] == "node renamed to new-node"


class TestGetNodes:
    async def test_returns_registered_nodes(self, hub, store, routes):
        await store.upsert_node("n1", "host-1", "Linux", "x86_64", "0.14.2")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/nodes")
        assert resp.status_code == 200
        assert len(resp.json()["nodes"]) == 1

    async def test_connected_reflects_runtime_registry(self, hub, registry, store, routes):
        await store.upsert_node("n1", "host-1", "Linux", "x86_64", "0.14.2")
        await store.upsert_node("n2", "host-2", "Linux", "x86_64", "0.14.2")
        await registry.register(
            AsyncMock(),
            NodeHello(
                node_id="n2",
                token="t",
                hostname="host-2",
                os="Linux",
                arch="x86_64",
                haniel_version="0.14.2",
            ),
        )

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/nodes")

        assert resp.status_code == 200
        nodes = {node["node_id"]: node for node in resp.json()["nodes"]}
        assert nodes["n1"]["connected"] == 0
        assert nodes["n2"]["connected"] == 1


class TestGetHistory:
    async def test_returns_history_with_limit(self, hub, store, routes):
        for i in range(5):
            await _seed_pending(store, f"d{i}", branch=f"branch-{i}")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/history?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()["deploys"]) == 3

    async def test_history_excludes_superseded_by_default(
        self, hub, store, routes
    ):
        """GET /api/orch/history (no query) must filter out auto-supersede
        rows so the dashboard's HistoryView is not drowned by them."""
        for did in ("d_old", "d_new"):
            await _seed_pending(store, did)
        await store.update_deploy_status(
            "d_old", DeployStatus.REJECTED,
            reject_reason="superseded by d_new",
        )

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/history")
        assert resp.status_code == 200
        ids = {d["deploy_id"] for d in resp.json()["deploys"]}
        assert "d_old" not in ids
        assert "d_new" in ids

    async def test_history_includes_superseded_with_query(
        self, hub, store, routes
    ):
        """GET /api/orch/history?include_superseded=1 must return supersede
        rows so the operator can audit chains."""
        for did in ("d_old", "d_new"):
            await _seed_pending(store, did)
        await store.update_deploy_status(
            "d_old", DeployStatus.REJECTED,
            reject_reason="superseded by d_new",
        )

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/history?include_superseded=1")
        assert resp.status_code == 200
        ids = {d["deploy_id"] for d in resp.json()["deploys"]}
        assert ids == {"d_old", "d_new"}


class TestApproveDeploy:
    async def test_deploying_state_exists_before_node_can_return_result(
        self, hub, store, routes
    ):
        await _seed_pending(store, "d1", "n1")
        hub.deploy_coordinator.register_connection("n1")

        async def send_after_transition(node_id, message):
            assert node_id == "n1"
            assert (await store.get_deploy_event("d1"))["status"] == "deploying"
            assert any(
                row["deploy_id"] == "d1"
                for row in await store.attempts.get_active_attempts()
            )
            return True

        hub.send_to_node = AsyncMock(side_effect=send_after_transition)
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        response = TestClient(Starlette(routes=routes)).post(
            "/api/orch/approve", json={"deploy_id": "d1"}
        )
        assert response.status_code == 200

    async def test_approve_success_node_connected(self, hub, registry, store, routes):
        await _seed_pending(store, "d1", "n1")

        # Register node so send_to_node succeeds
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

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/approve", json={"deploy_id": "d1"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deploying"

        # Verify DB state
        event = await store.get_deploy_event("d1")
        assert event["status"] == "deploying"
        assert event["approved_by"] == "dashboard"

    async def test_approve_node_disconnected(self, hub, store, routes):
        """Offline node approve: 503 + status revert to PENDING (no transient APPROVED leak).

        Policy (analysis cache F1): rather than leaving APPROVED on the deploy
        with no resend path, fail the approve outright and keep PENDING so the
        operator can retry once the node reconnects.
        """
        await _seed_pending(store, "d1", "n1")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/approve", json={"deploy_id": "d1"}
        )
        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert data["deploy_id"] == "d1"
        # DB must be reverted to PENDING — no transient APPROVED leak.
        event = await store.get_deploy_event("d1")
        assert event["status"] == "pending"

    async def test_approve_missing_deploy_id(self, hub, store, routes):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve", json={})
        assert resp.status_code == 400

    async def test_approve_not_found(self, hub, store, routes):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/approve", json={"deploy_id": "nonexistent"}
        )
        assert resp.status_code == 404

    async def test_approve_already_approved(self, hub, store, routes):
        await _seed_pending(store, "d1")
        await store.update_deploy_status("d1", DeployStatus.APPROVED)

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/approve", json={"deploy_id": "d1"}
        )
        assert resp.status_code == 409

    async def test_approve_supersedes_older_pending(
        self, hub, registry, store, routes
    ):
        """Approving the latest deploy supersedes older PENDING entries
        on the same (node, repo, branch)."""
        # 3 PENDING deploys on the same branch — d1 oldest, d3 newest (we approve d3).
        # asyncio.sleep separates created_at timestamps so reject_reason
        # message ("superseded by d3") is deterministic regardless of which
        # deploy_id was supplied to kept_deploy_id.
        await _seed_pending(store, "d1", "n1")
        await asyncio.sleep(0.005)
        await _seed_pending(store, "d2", "n1")
        await asyncio.sleep(0.005)
        await _seed_pending(store, "d3", "n1")

        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1", token="t", hostname="h",
            os="Linux", arch="x86_64", haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve", json={"deploy_id": "d3"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "deploying"

        # Each change atomically supersedes the then-current canonical. Approval
        # does not rewrite the audit chain after the fact.
        ev3 = await store.get_deploy_event("d3")
        assert ev3["status"] == "deploying"
        ev1 = await store.get_deploy_event("d1")
        ev2 = await store.get_deploy_event("d2")
        assert (ev1["status"], ev1["reject_reason"]) == (
            "rejected", "superseded by d2"
        )
        assert (ev2["status"], ev2["reject_reason"]) == (
            "rejected", "superseded by d3"
        )

        # Rows were created directly through the store, so their creation-time
        # broadcasts are intentionally outside this approval request.
        sent = [json.loads(c.args[0]) for c in ws_dash.send_text.call_args_list]
        assert not any(p.get("status") == "rejected" for p in sent)
        assert any(p.get("status") == "deploying" for p in sent)


class TestRejectDeploy:
    async def test_reject_success(self, hub, store, routes):
        await _seed_pending(store, "d1", "n1")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/reject",
            json={"deploy_id": "d1", "reason": "not ready"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rejected"

        event = await store.get_deploy_event("d1")
        assert event["status"] == "rejected"
        assert event["reject_reason"] == "not ready"

    async def test_reject_not_found(self, hub, store, routes):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/reject", json={"deploy_id": "x"}
        )
        assert resp.status_code == 404

    async def test_reject_not_pending(self, hub, store, routes):
        await _seed_pending(store, "d1")
        await store.update_deploy_status("d1", DeployStatus.DEPLOYING)

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/reject", json={"deploy_id": "d1"}
        )
        assert resp.status_code == 409


class TestApproveAll:
    async def test_approve_all_with_connected_nodes(
        self, hub, registry, store, routes
    ):
        # Two deploys on different branches → distinct (node, repo, branch)
        # groups, so both should be approved (no auto-supersede).
        await _seed_pending(store, "d1", "n1", branch="main")
        await _seed_pending(store, "d2", "n1", branch="dev")

        # Register node
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

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve-all")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["approved"]) == {"d1", "d2"}
        assert data["failed"] == []

    async def test_approve_all_no_pending(self, hub, store, routes):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve-all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == []
        assert data["failed"] == []
        assert data["message"] == "no pending deploys"

    async def test_approve_all_node_disconnected(self, hub, store, routes):
        """Offline node in approve_all: response shape unchanged,
        but DB status reverts to PENDING (analysis cache F2)."""
        await _seed_pending(store, "d1", "n1")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve-all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["deploy_id"] == "d1"
        # Response shape preserved. DB status must revert to PENDING — no
        # transient APPROVED leak so the operator can retry once the node
        # reconnects.
        event = await store.get_deploy_event("d1")
        assert event["status"] == "pending"

    async def test_approve_all_no_supersede_no_key(
        self, hub, registry, store, routes
    ):
        """When no group has multiple PENDING entries, response has no 'superseded' key."""
        await _seed_pending(store, "d1", "n1", branch="main")
        await _seed_pending(store, "d2", "n1", branch="dev")

        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1", token="t", hostname="h",
            os="Linux", arch="x86_64", haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve-all")
        assert resp.status_code == 200
        data = resp.json()
        assert "superseded" not in data
        assert set(data["approved"]) == {"d1", "d2"}

    async def test_approve_all_groups_per_branch(
        self, hub, registry, store, routes
    ):
        """approve_all approves only the latest per (node, repo, branch);
        older entries in the same group are auto-superseded."""
        # 2 deploys on (n1, myrepo, main) — d2 newest, d1 older
        # 1 deploy on (n1, myrepo, dev)
        # 1 deploy on (n2, myrepo, main)
        # asyncio.sleep separates created_at within the (n1, myrepo, main)
        # group so that d2 is the deterministic latest.
        await _seed_pending(store, "d1", "n1", branch="main")
        await asyncio.sleep(0.005)
        await _seed_pending(store, "d2", "n1", branch="main")
        await _seed_pending(store, "d3", "n1", branch="dev")
        await _seed_pending(store, "d4", "n2", branch="main")

        ws_n1 = AsyncMock()
        ws_n2 = AsyncMock()
        for nid, ws in (("n1", ws_n1), ("n2", ws_n2)):
            hello = NodeHello(
                node_id=nid, token="t", hostname="h",
                os="Linux", arch="x86_64", haniel_version="0.1.0",
            )
            await registry.register(ws, hello)

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/api/orch/approve-all")
        assert resp.status_code == 200
        data = resp.json()

        # `pending` is ordered created_at DESC, so d2 ranks before d1 within
        # its group → d2 is approved, d1 is superseded. d3 and d4 are alone
        # in their groups → both approved.
        assert set(data["approved"]) == {"d2", "d3", "d4"}
        assert "superseded" not in data
        assert (await store.get_deploy_event("d1"))["reject_reason"] == "superseded by d2"
        assert data["failed"] == []

        ev_d1 = await store.get_deploy_event("d1")
        assert ev_d1["status"] == "rejected"
        assert ev_d1["reject_reason"] == "superseded by d2"
