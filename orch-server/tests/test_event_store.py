"""Tests for EventStore CRUD operations."""

import asyncio
import ast
import inspect
import textwrap

from haniel_orch.event_store import EventStore
from haniel_orch.protocol import DeployStatus


class TestCreateDeployEvent:
    async def test_create_and_get(self, store: EventStore):
        inserted = await store.create_deploy_event(
            deploy_id="n1:repo:main:abc1234",
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["abc1234 fix: something"],
            affected_services=["bot", "mcp"],
            diff_stat="+10 -3",
            detected_at="2026-05-05T00:00:00Z",
        )
        assert inserted is True

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
        inserted = await store.create_deploy_event(
            deploy_id="dup-id",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h1 original"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        assert inserted is True
        # Second insert with same deploy_id — should be silently ignored
        inserted = await store.create_deploy_event(
            deploy_id="dup-id",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h2 duplicate attempt"],
            affected_services=["new-svc"],
            diff_stat="+100 -0",
            detected_at="2026-01-02T00:00:00Z",
        )
        assert inserted is False

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

    async def test_failed_terminal_reopens_with_snapshot(self, store: EventStore):
        deploy_id = "n1:repo:main:remote"
        await store.create_deploy_event(
            deploy_id=deploy_id,
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["remote change"],
            affected_services=["svc"],
            diff_stat="1 file",
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status(
            deploy_id,
            DeployStatus.DEPLOYING,
            approved_by="dashboard",
        )
        assert await store.apply_deploy_result(
            deploy_id,
            DeployStatus.FAILED,
            error="hook failed",
            duration_ms=123,
        )
        failed = await store.get_deploy_event(deploy_id)

        assert await store.reopen_failed_deploy(deploy_id)
        reopened = await store.get_deploy_event(deploy_id)
        assert reopened["status"] == "pending"
        assert reopened["approved_by"] is None
        assert reopened["error"] is None
        assert reopened["duration_ms"] is None

        latest = await store.get_latest_failed_deploy()
        assert latest is not None
        assert latest["deploy_id"].startswith("attempt:")
        assert latest["error"] == "hook failed"
        assert latest["duration_ms"] == 123
        assert latest["approved_by"] == "dashboard"
        assert latest["created_at"] == failed["updated_at"]
        assert latest["updated_at"] == failed["updated_at"]

    async def test_late_result_cannot_overwrite_reopened_pending(
        self, store: EventStore
    ):
        deploy_id = "n1:repo:main:remote"
        await store.create_deploy_event(
            deploy_id=deploy_id,
            node_id="n1",
            repo="repo",
            branch="main",
            commits=["remote change"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status(deploy_id, DeployStatus.DEPLOYING)
        assert await store.apply_deploy_result(
            deploy_id, DeployStatus.FAILED, error="timeout"
        )
        assert await store.reopen_failed_deploy(deploy_id)

        assert not await store.apply_deploy_result(
            deploy_id, DeployStatus.SUCCESS, duration_ms=999
        )
        event = await store.get_deploy_event(deploy_id)
        assert event["status"] == "pending"

    async def test_latest_failed_uses_updated_at_then_deploy_id(
        self, store: EventStore
    ):
        for deploy_id in ("failed-a", "failed-b"):
            await store.create_deploy_event(
                deploy_id=deploy_id,
                node_id="n1",
                repo="repo",
                branch=deploy_id,
                commits=["h msg"],
                affected_services=[],
                diff_stat=None,
                detected_at="2026-01-01T00:00:00Z",
            )
            await store.update_deploy_status(deploy_id, DeployStatus.DEPLOYING)
            await store.apply_deploy_result(
                deploy_id, DeployStatus.FAILED, error=deploy_id
            )
        await store._db.execute(
            "UPDATE deploy_events SET updated_at = ? WHERE deploy_id IN (?, ?)",
            ("2026-07-01T00:00:00Z", "failed-a", "failed-b"),
        )
        await store._db.commit()

        latest = await store.get_latest_failed_deploy()
        assert latest["deploy_id"] == "failed-b"


class TestMutationBoundary:
    PUBLIC_MUTATIONS = {
        "initialize",
        "create_deploy_event",
        "reopen_failed_deploy",
        "apply_deploy_result",
        "transition_deploy_status",
        "resolve_pending_branch",
        "supersede_stale_pending_deploys",
        "update_deploy_status",
        "reject_pending_deploys_for_nodes",
        "upsert_node",
        "update_node_heartbeat",
        "mark_node_disconnected",
    }

    def test_every_public_mutation_acquires_lock_once_without_public_nesting(self):
        for name in self.PUBLIC_MUTATIONS:
            source = textwrap.dedent(inspect.getsource(getattr(EventStore, name)))
            assert source.count("async with self._mutation_lock") == 1, name
            tree = ast.parse(source)
            nested = {
                call.func.attr
                for call in ast.walk(tree)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr in self.PUBLIC_MUTATIONS
            }
            assert nested == set(), (name, nested)

    async def test_composite_reopen_does_not_deadlock_non_reentrant_lock(
        self, store: EventStore
    ):
        deploy_id = "n1:repo:main:remote"
        await store.create_deploy_event(
            deploy_id,
            "n1",
            "repo",
            "main",
            ["remote change"],
            [],
            None,
            "2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status(deploy_id, DeployStatus.DEPLOYING)
        await store.apply_deploy_result(deploy_id, DeployStatus.FAILED, error="boom")

        assert await asyncio.wait_for(
            store.reopen_failed_deploy(deploy_id),
            timeout=0.5,
        )


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
                detected_at=f"2026-01-0{i + 1}T00:00:00Z",
            )
        await store.update_deploy_status(
            "d2", DeployStatus.APPROVED, approved_by="dash"
        )

        pending = await store.get_pending_deploys()
        assert len(pending) == 2
        ids = {p["deploy_id"] for p in pending}
        assert ids == {"d0", "d1"}

    async def test_empty_when_none_pending(self, store: EventStore):
        pending = await store.get_pending_deploys()
        assert pending == []


class TestGetActiveDeploys:
    """get_active_deploys returns PENDING + DEPLOYING (used by /api/orch/pending)."""

    async def test_includes_pending_and_deploying(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="d_pending",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="d_deploying",
            node_id="n1",
            repo="r",
            branch="dev",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="d_rejected",
            node_id="n1",
            repo="r",
            branch="feature",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("d_deploying", DeployStatus.DEPLOYING)
        await store.update_deploy_status(
            "d_rejected", DeployStatus.REJECTED, reject_reason="not ready"
        )

        active = await store.get_active_deploys()
        ids = {d["deploy_id"] for d in active}
        assert ids == {"d_pending", "d_deploying"}

    async def test_ordered_newest_first(self, store: EventStore):
        for i in range(3):
            await store.create_deploy_event(
                deploy_id=f"d{i}",
                node_id="n1",
                repo="r",
                branch="main",
                commits=[f"h{i} msg"],
                affected_services=[],
                diff_stat=None,
                detected_at=f"2026-01-0{i + 1}T00:00:00Z",
            )
            # Distinct created_at timestamps so ORDER BY is deterministic.
            await asyncio.sleep(0.005)

        active = await store.get_active_deploys()
        # same group is repaired to newest only
        assert [d["deploy_id"] for d in active] == ["d2"]

    async def test_repairs_existing_stale_pending_rows(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="old",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h old"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await asyncio.sleep(0.005)
        await store.create_deploy_event(
            deploy_id="new",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h new"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-02T00:00:00Z",
        )

        active = await store.get_active_deploys()
        assert [d["deploy_id"] for d in active] == ["new"]

        old = await store.get_deploy_event("old")
        assert old["status"] == "rejected"
        assert old["reject_reason"] == "superseded by new"

    async def test_empty_when_none_active(self, store: EventStore):
        active = await store.get_active_deploys()
        assert active == []


class TestGetPendingDeploysForBranch:
    """get_pending_deploys_for_branch returns PENDING for the matching (node, repo, branch)."""

    async def test_returns_only_matching_branch(self, store: EventStore):
        # same node + repo, different branches; same branch + different node
        await store.create_deploy_event(
            deploy_id="m1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="d1",
            node_id="n1",
            repo="r",
            branch="dev",
            commits=["h"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="m2",
            node_id="n2",
            repo="r",
            branch="main",
            commits=["h"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        result = await store.get_pending_deploys_for_branch("n1", "r", "main")
        assert {d["deploy_id"] for d in result} == {"m1"}

    async def test_includes_only_pending(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="m1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="m2",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("m1", DeployStatus.DEPLOYING)

        result = await store.get_pending_deploys_for_branch("n1", "r", "main")
        assert {d["deploy_id"] for d in result} == {"m2"}

    async def test_empty_when_no_match(self, store: EventStore):
        result = await store.get_pending_deploys_for_branch("nope", "r", "main")
        assert result == []


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
                detected_at=f"2026-01-0{i + 1}T00:00:00Z",
            )

        history = await store.get_deploy_history(limit=3)
        assert len(history) == 3

    async def test_default_limit(self, store: EventStore):
        history = await store.get_deploy_history()
        assert isinstance(history, list)

    async def test_excludes_superseded_by_default(self, store: EventStore):
        """superseded rows (reject_reason starting with 'superseded by ') are
        excluded from history by default — they would otherwise drown out
        actionable deploys."""
        for did in ("d_old", "d_new", "d_manual"):
            await store.create_deploy_event(
                deploy_id=did,
                node_id="n1",
                repo="r",
                branch="main",
                commits=["h msg"],
                affected_services=[],
                diff_stat=None,
                detected_at="2026-01-01T00:00:00Z",
            )
        # d_old: auto-superseded by d_new
        await store.update_deploy_status(
            "d_old",
            DeployStatus.REJECTED,
            reject_reason="superseded by d_new",
        )
        # d_manual: rejected by operator (non-supersede)
        await store.update_deploy_status(
            "d_manual",
            DeployStatus.REJECTED,
            reject_reason="not ready yet",
        )

        history = await store.get_deploy_history()
        ids = {d["deploy_id"] for d in history}
        assert "d_old" not in ids, "superseded row should be excluded by default"
        assert "d_new" in ids
        assert "d_manual" in ids

    async def test_includes_superseded_when_requested(self, store: EventStore):
        """include_superseded=True returns supersede rows alongside others
        (audit view)."""
        for did in ("d_old", "d_new"):
            await store.create_deploy_event(
                deploy_id=did,
                node_id="n1",
                repo="r",
                branch="main",
                commits=["h msg"],
                affected_services=[],
                diff_stat=None,
                detected_at="2026-01-01T00:00:00Z",
            )
        await store.update_deploy_status(
            "d_old",
            DeployStatus.REJECTED,
            reject_reason="superseded by d_new",
        )

        history = await store.get_deploy_history(include_superseded=True)
        ids = {d["deploy_id"] for d in history}
        assert ids == {"d_old", "d_new"}

    async def test_non_superseded_rejected_still_visible(self, store: EventStore):
        """Manual rejects (reject_reason not starting with 'superseded by ')
        must remain visible in the default response — they carry operator
        intent and should not be hidden."""
        await store.create_deploy_event(
            deploy_id="d_man",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status(
            "d_man",
            DeployStatus.REJECTED,
            reject_reason="rolled back during incident",
        )

        history = await store.get_deploy_history()
        assert any(d["deploy_id"] == "d_man" for d in history)


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

        await store.update_deploy_status("suc1", DeployStatus.SUCCESS, duration_ms=8200)

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
        await store.upsert_node(
            "n2", "h2", "Windows", "x86_64", "0.1.0", connected=False
        )

        nodes = await store.get_nodes()
        assert len(nodes) == 2
        ids = {n["node_id"] for n in nodes}
        assert ids == {"n1", "n2"}

    async def test_empty_when_no_nodes(self, store: EventStore):
        nodes = await store.get_nodes()
        assert nodes == []
