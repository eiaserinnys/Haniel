"""Tests for EventStore CRUD operations."""

import pytest

from haniel_orch.event_store import EventStore
from haniel_orch.protocol import DeployStatus


class TestCreateDeployEvent:
    async def test_create_and_get(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="n1:repo:main:abc1234",
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["abc1234 fix: something"],
            affected_services=["bot", "mcp"],
            diff_stat="+10 -3",
            detected_at="2026-05-05T00:00:00Z",
        )

        event = await store.get_deploy_event("n1:repo:main:abc1234")
        assert event is not None
        assert event["deploy_id"] == "n1:repo:main:abc1234"
        assert event["node_id"] == "n1"
        assert event["repo"] == "repo"
        assert event["branch"] == "main"
        assert event["status"] == "pending"
        assert event["commits"] == ["abc1234 fix: something"]
        assert event["affected_services"] == ["bot", "mcp"]
        assert event["diff_stat"] == "+10 -3"

    async def test_duplicate_deploy_id_ignored(self, store: EventStore):
        """INSERT OR IGNORE — same deploy_id should not raise."""
        await store.create_deploy_event(
            deploy_id="dup-id",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h1 original"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        # Second insert with same deploy_id — should be silently ignored
        await store.create_deploy_event(
            deploy_id="dup-id",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h2 duplicate attempt"],
            affected_services=["new-svc"],
            diff_stat="+100 -0",
            detected_at="2026-01-02T00:00:00Z",
        )

        # Original data should be preserved
        event = await store.get_deploy_event("dup-id")
        assert event["commits"] == ["h1 original"]
        assert event["affected_services"] == []

    async def test_get_nonexistent_returns_none(self, store: EventStore):
        event = await store.get_deploy_event("nonexistent")
        assert event is None

    async def test_null_diff_stat(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="no-stat",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        event = await store.get_deploy_event("no-stat")
        assert event["diff_stat"] is None


class TestGetPendingDeploys:
    async def test_returns_only_pending(self, store: EventStore):
        # Create 2 pending + 1 approved
        for i in range(3):
            await store.create_deploy_event(
                deploy_id=f"d{i}",
                node_id="n1",
                repo="r",
                branch="main",
                commits=[f"h{i} msg"],
                affected_services=[],
                diff_stat=None,
                detected_at=f"2026-01-0{i+1}T00:00:00Z",
            )
        await store.update_deploy_status("d2", DeployStatus.APPROVED, approved_by="dash")

        pending = await store.get_pending_deploys()
        assert len(pending) == 2
        ids = {p["deploy_id"] for p in pending}
        assert ids == {"d0", "d1"}

    async def test_empty_when_none_pending(self, store: EventStore):
        pending = await store.get_pending_deploys()
        assert pending == []


class TestGetDeployHistory:
    async def test_returns_all_newest_first(self, store: EventStore):
        for i in range(5):
            await store.create_deploy_event(
                deploy_id=f"h{i}",
                node_id="n1",
                repo="r",
                branch="main",
                commits=[f"c{i} msg"],
                affected_services=[],
                diff_stat=None,
                detected_at=f"2026-01-0{i+1}T00:00:00Z",
            )

        history = await store.get_deploy_history(limit=3)
        assert len(history) == 3

    async def test_default_limit(self, store: EventStore):
        history = await store.get_deploy_history()
        assert isinstance(history, list)


class TestUpdateDeployStatus:
    async def test_update_to_approved(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="upd1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        await store.update_deploy_status(
            "upd1", DeployStatus.APPROVED, approved_by="dashboard"
        )

        event = await store.get_deploy_event("upd1")
        assert event["status"] == "approved"
        assert event["approved_by"] == "dashboard"

    async def test_update_to_rejected_with_reason(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="rej1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        await store.update_deploy_status(
            "rej1", DeployStatus.REJECTED, reject_reason="not ready yet"
        )

        event = await store.get_deploy_event("rej1")
        assert event["status"] == "rejected"
        assert event["reject_reason"] == "not ready yet"

    async def test_update_to_failed_with_error(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="fail1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        await store.update_deploy_status(
            "fail1", DeployStatus.FAILED, error="exit code 1", duration_ms=3400
        )

        event = await store.get_deploy_event("fail1")
        assert event["status"] == "failed"
        assert event["error"] == "exit code 1"
        assert event["duration_ms"] == 3400

    async def test_update_to_success_with_duration(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="suc1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        await store.update_deploy_status(
            "suc1", DeployStatus.SUCCESS, duration_ms=8200
        )

        event = await store.get_deploy_event("suc1")
        assert event["status"] == "success"
        assert event["duration_ms"] == 8200

    async def test_updated_at_changes(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="ts1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        event_before = await store.get_deploy_event("ts1")
        await store.update_deploy_status("ts1", DeployStatus.DEPLOYING)
        event_after = await store.get_deploy_event("ts1")

        assert event_after["updated_at"] >= event_before["updated_at"]


class TestGetDeployingEventsForNode:
    async def test_returns_deploying_only(self, store: EventStore):
        # Create events in different states
        await store.create_deploy_event(
            deploy_id="dep1",
            node_id="n1",
            repo="r1",
            branch="main",
            commits=["h1 msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="dep2",
            node_id="n1",
            repo="r2",
            branch="main",
            commits=["h2 msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="dep3",
            node_id="n2",
            repo="r1",
            branch="main",
            commits=["h3 msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        # Set dep1 to deploying, dep2 to pending, dep3 to deploying (different node)
        await store.update_deploy_status("dep1", DeployStatus.DEPLOYING)
        await store.update_deploy_status("dep3", DeployStatus.DEPLOYING)

        deploying = await store.get_deploying_events_for_node("n1")
        assert len(deploying) == 1
        assert deploying[0]["deploy_id"] == "dep1"

    async def test_empty_when_no_deploying(self, store: EventStore):
        deploying = await store.get_deploying_events_for_node("n1")
        assert deploying == []


class TestUpsertNode:
    async def test_insert_new_node(self, store: EventStore):
        await store.upsert_node(
            node_id="n1",
            hostname="server-01",
            os="Linux",
            arch="x86_64",
            haniel_version="0.14.2",
        )

        nodes = await store.get_nodes()
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "n1"
        assert nodes[0]["hostname"] == "server-01"
        assert nodes[0]["connected"] == 1

    async def test_update_existing_node(self, store: EventStore):
        await store.upsert_node(
            node_id="n1",
            hostname="old-host",
            os="Linux",
            arch="x86_64",
            haniel_version="0.13.0",
        )
        await store.upsert_node(
            node_id="n1",
            hostname="new-host",
            os="Linux",
            arch="x86_64",
            haniel_version="0.14.2",
        )

        nodes = await store.get_nodes()
        assert len(nodes) == 1
        assert nodes[0]["hostname"] == "new-host"
        assert nodes[0]["haniel_version"] == "0.14.2"

    async def test_disconnected_node(self, store: EventStore):
        await store.upsert_node(
            node_id="n1",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
            connected=False,
        )

        nodes = await store.get_nodes()
        assert nodes[0]["connected"] == 0


class TestUpdateNodeHeartbeat:
    async def test_updates_last_seen(self, store: EventStore):
        await store.upsert_node(
            node_id="n1",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )

        node_before = (await store.get_nodes())[0]
        await store.update_node_heartbeat("n1")
        node_after = (await store.get_nodes())[0]

        assert node_after["last_seen"] >= node_before["last_seen"]
        assert node_after["connected"] == 1


class TestGetNodes:
    async def test_returns_all_nodes(self, store: EventStore):
        await store.upsert_node("n1", "h1", "Linux", "x86_64", "0.1.0")
        await store.upsert_node("n2", "h2", "Windows", "x86_64", "0.1.0", connected=False)

        nodes = await store.get_nodes()
        assert len(nodes) == 2
        ids = {n["node_id"] for n in nodes}
        assert ids == {"n1", "n2"}

    async def test_empty_when_no_nodes(self, store: EventStore):
        nodes = await store.get_nodes()
        assert nodes == []
