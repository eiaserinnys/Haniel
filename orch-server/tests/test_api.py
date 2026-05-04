"""Tests for REST API routes — approve, reject, approve-all, queries."""

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


async def _seed_pending(store: EventStore, deploy_id: str = "d1", node_id: str = "n1"):
    """Helper: create a pending deploy event."""
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node_id,
        repo="myrepo",
        branch="main",
        commits=["abc1234 fix: something"],
        affected_services=["bot"],
        diff_stat="+10 -3",
        detected_at="2026-01-01T00:00:00Z",
    )


class TestGetPending:
    async def test_returns_pending_deploys(self, hub, store, routes):
        await _seed_pending(store, "d1")
        await _seed_pending(store, "d2")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["deploys"]) == 2

    async def test_empty_when_none(self, hub, store, routes):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/pending")
        assert resp.status_code == 200
        assert resp.json()["deploys"] == []


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


class TestGetHistory:
    async def test_returns_history_with_limit(self, hub, store, routes):
        for i in range(5):
            await _seed_pending(store, f"d{i}")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/api/orch/history?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()["deploys"]) == 3


class TestApproveDeploy:
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
        await _seed_pending(store, "d1", "n1")

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post(
            "/api/orch/approve", json={"deploy_id": "d1"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert "warning" in data

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
        await _seed_pending(store, "d1", "n1")
        await _seed_pending(store, "d2", "n1")

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
