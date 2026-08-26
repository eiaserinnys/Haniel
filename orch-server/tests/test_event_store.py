"""Tests for EventStore CRUD operations."""

import asyncio
import ast
import inspect
import textwrap

import aiosqlite

from haniel_orch.event_store import EventStore
from haniel_orch.protocol import DeployStatus, NodeDeployReport


def node_report(
    phase: str,
    *,
    target_head: str = "target",
    local_head: str | None = None,
    error: str | None = None,
) -> NodeDeployReport:
    return NodeDeployReport(
        phase=phase,
        node_attempt_id="node-attempt-1",
        journal_attempt_id="journal-1",
        deploy_id=f"n1:repo:main:{target_head}",
        node_id="n1",
        repo="repo",
        branch="main",
        target_head=target_head,
        local_head=local_head or ("old" if phase == "started" else target_head),
        trigger="local",
        error=error,
        duration_ms=None if phase == "started" else 25,
    )


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

    async def test_self_update_marker_round_trips_as_boolean(self, store: EventStore):
        await store.create_deploy_event(
            deploy_id="self-update",
            node_id="n1",
            repo="custom-runner",
            branch="main",
            commits=["h update"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
            is_self_update=True,
        )

        event = await store.get_deploy_event("self-update")
        assert event["is_self_update"] is True
        assert (await store.get_active_deploys())[0]["is_self_update"] is True

    async def test_schema_backfills_active_legacy_haniel_rows(self, tmp_path):
        db_path = tmp_path / "legacy.sqlite"
        db = await aiosqlite.connect(db_path)
        await db.execute(
            """CREATE TABLE deploy_events (
                deploy_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
                repo TEXT NOT NULL, branch TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                commits_json TEXT NOT NULL, affected_services_json TEXT NOT NULL,
                diff_stat TEXT, detected_at TEXT NOT NULL, approved_by TEXT,
                reject_reason TEXT, error TEXT, duration_ms INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        await db.execute(
            """INSERT INTO deploy_events
               (deploy_id, node_id, repo, branch, status, commits_json,
                affected_services_json, detected_at, created_at, updated_at)
               VALUES ('legacy-self-update', 'n1', 'haniel', 'main', 'pending',
                       '[\"h update\"]', '[]', '2026-01-01T00:00:00Z',
                       '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
        )
        await db.commit()
        await db.close()

        migrated = EventStore(str(db_path))
        await migrated.initialize()
        try:
            event = await migrated.get_deploy_event("legacy-self-update")
            assert event["is_self_update"] is True
        finally:
            await migrated.close()

    async def test_schema_does_not_overwrite_explicit_false_on_restart(self, tmp_path):
        db_path = tmp_path / "current.sqlite"
        store = EventStore(str(db_path))
        await store.initialize()
        await store.create_deploy_event(
            deploy_id="explicit-regular",
            node_id="n1",
            repo="haniel",
            branch="main",
            commits=["h regular"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
            is_self_update=False,
        )
        await store.close()

        reopened = EventStore(str(db_path))
        await reopened.initialize()
        try:
            event = await reopened.get_deploy_event("explicit-regular")
            assert event["is_self_update"] is False
        finally:
            await reopened.close()

    async def test_schema_restores_missing_expected_budget_column(self, tmp_path):
        db_path = tmp_path / "legacy-attempt.sqlite"
        initial = EventStore(str(db_path))
        await initial.initialize()
        await initial.close()

        db = await aiosqlite.connect(db_path)
        await db.execute("ALTER TABLE deploy_attempts DROP COLUMN expected_budget_sec")
        await db.commit()
        await db.close()

        migrated = EventStore(str(db_path))
        await migrated.initialize()
        try:
            cursor = await migrated._db.execute("PRAGMA table_info(deploy_attempts)")
            columns = {row[1] for row in await cursor.fetchall()}
            assert "expected_budget_sec" in columns
        finally:
            await migrated.close()


class TestMutationBoundary:
    PUBLIC_MUTATIONS = {
        "initialize",
        "create_deploy_event",
        "record_node_deploy_report",
        "transition_deploy_status",
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


class TestNodeDeployReportReconciliation:
    async def _seed(self, store: EventStore, target_head: str = "target") -> str:
        deploy_id = f"n1:repo:main:{target_head}"
        await store.create_deploy_event(
            deploy_id=deploy_id,
            node_id="n1",
            repo="repo",
            branch="main",
            commits=[f"{target_head} change"],
            affected_services=["svc"],
            diff_stat=None,
            detected_at="2026-08-11T00:00:00Z",
            target_head=target_head,
        )
        return deploy_id

    async def test_started_then_success_closes_exact_pending(self, store: EventStore):
        deploy_id = await self._seed(store)

        started = await store.record_node_deploy_report(node_report("started"))
        assert started["status"] == "started"
        assert (await store.get_deploy_event(deploy_id))["status"] == "pending"

        succeeded = await store.record_node_deploy_report(node_report("succeeded"))
        assert succeeded["status"] == "success"
        assert (await store.get_deploy_event(deploy_id))["status"] == "success"

    async def test_retro_terminal_without_started_closes_exact_pending(
        self, store: EventStore
    ):
        deploy_id = await self._seed(store)

        succeeded = await store.record_node_deploy_report(
            node_report("succeeded").model_copy(
                update={"trigger": "startup", "duration_ms": 0}
            )
        )

        assert succeeded["status"] == "success"
        assert (await store.get_deploy_event(deploy_id))["status"] == "success"

    async def test_terminal_trigger_may_reflect_startup_recovery(
        self, store: EventStore
    ):
        deploy_id = await self._seed(store)
        await store.record_node_deploy_report(node_report("started"))

        succeeded = await store.record_node_deploy_report(
            node_report("succeeded").model_copy(update={"trigger": "startup"})
        )

        assert succeeded["status"] == "success"
        event = next(
            row
            for row in await store.get_deploy_history()
            if row["deploy_id"] == deploy_id
        )
        assert event["node_deploy_trigger"] == "local"

    async def test_failure_reopens_pending_and_adds_history(self, store: EventStore):
        deploy_id = await self._seed(store)
        await store.record_node_deploy_report(node_report("started"))

        failed = await store.record_node_deploy_report(
            node_report("failed", local_head="old", error="verify failed")
        )

        assert failed["status"] == "failed"
        assert (await store.get_deploy_event(deploy_id))["status"] == "pending"
        history = await store.get_deploy_history()
        attempt = next(
            row for row in history if row["deploy_id"] == "node-attempt:node-attempt-1"
        )
        assert attempt["status"] == "failed"
        assert attempt["error"] == "verify failed"

    async def test_other_target_success_does_not_touch_pending(self, store: EventStore):
        deploy_id = await self._seed(store, "target-a")

        result = await store.record_node_deploy_report(
            node_report("succeeded", target_head="target-b")
        )

        assert result["status"] == "ignored"
        assert (await store.get_deploy_event(deploy_id))["status"] == "pending"

    async def test_success_supersedes_active_orchestrator_attempt(
        self, store: EventStore
    ):
        deploy_id = await self._seed(store)
        assert store.attempts is not None
        await store.attempts.begin_normal_attempt(
            orchestrator_attempt_id="orch-1",
            deploy_id=deploy_id,
            connection_generation="g1",
            current_generation="g1",
            source="manual_single",
            approved_by="test",
            deadline_at="2099-01-01T00:00:00Z",
        )

        result = await store.record_node_deploy_report(node_report("succeeded"))

        assert result["cancelled_orchestrator_attempt_ids"] == ["orch-1"]
        assert (await store.attempts.get_attempt("orch-1"))["outcome"] == "superseded"
        assert (await store.get_deploy_event(deploy_id))["status"] == "success"

    async def test_failure_does_not_overwrite_active_orchestrator_owner(
        self, store: EventStore
    ):
        deploy_id = await self._seed(store)
        assert store.attempts is not None
        await store.attempts.begin_normal_attempt(
            orchestrator_attempt_id="orch-1",
            deploy_id=deploy_id,
            connection_generation="g1",
            current_generation="g1",
            source="manual_single",
            approved_by="test",
            deadline_at="2099-01-01T00:00:00Z",
        )

        result = await store.record_node_deploy_report(
            node_report("failed", local_head="old", error="local attempt failed")
        )

        assert result["canonical_status"] == "deploying"
        assert (await store.get_deploy_event(deploy_id))["status"] == "deploying"
        assert (await store.attempts.get_attempt("orch-1"))["outcome"] == "active"


class TestGetPendingDeploys:
    async def test_returns_only_pending(self, store: EventStore):
        # Create 2 pending + 1 approved
        for i in range(3):
            await store.create_deploy_event(
                deploy_id=f"d{i}",
                node_id="n1",
                repo="r",
                branch=f"branch-{i}",
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

    async def test_orders_by_detected_canonical_time_before_insert_time(
        self, store: EventStore
    ):
        await store.create_deploy_event(
            "new",
            "n1",
            "r",
            "main",
            ["new"],
            [],
            None,
            "2026-08-02T00:00:00Z",
        )
        await store.create_deploy_event(
            "late-old",
            "n1",
            "r",
            "main",
            ["old"],
            [],
            None,
            "2026-08-01T00:00:00Z",
        )

        assert [row["deploy_id"] for row in await store.get_pending_deploys()] == [
            "new"
        ]

    async def test_startup_cleanup_keeps_latest_detected_not_latest_inserted(
        self, store: EventStore
    ):
        await store.create_deploy_event(
            "detected-new",
            "n1",
            "r",
            "new-branch",
            ["new"],
            [],
            None,
            "2026-08-02T00:00:00Z",
        )
        await store.create_deploy_event(
            "inserted-late-old",
            "n1",
            "r",
            "old-branch",
            ["old"],
            [],
            None,
            "2026-08-01T00:00:00Z",
        )
        await store._db.execute(
            "UPDATE deploy_events SET branch = 'main', status = 'pending' "
            "WHERE deploy_id IN ('detected-new', 'inserted-late-old')"
        )
        await store._db.commit()

        superseded = await store.supersede_stale_pending_deploys()

        assert superseded == ["inserted-late-old"]
        assert (await store.get_deploy_event("detected-new"))["status"] == "pending"
        assert (await store.get_deploy_event("inserted-late-old"))[
            "status"
        ] == "rejected"


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

    async def test_late_older_canonical_cannot_supersede_latest_pending(
        self, store: EventStore
    ):
        await store.create_deploy_event(
            deploy_id="new",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["new"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-02T00:00:00Z",
        )
        await store.create_deploy_event(
            deploy_id="old-late",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["old"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )

        assert [row["deploy_id"] for row in await store.get_active_deploys()] == ["new"]
        stale = await store.get_deploy_event("old-late")
        assert stale["status"] == "rejected"
        assert stale["reject_reason"] == "superseded by new"

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

    async def test_active_self_update_is_scoped_to_node(self, store: EventStore):
        await store.create_deploy_event(
            "self",
            "n1",
            "haniel",
            "main",
            ["h"],
            [],
            None,
            "2026-01-01T00:00:00Z",
            is_self_update=True,
        )
        await store.create_deploy_event(
            "other",
            "n2",
            "repo",
            "main",
            ["h"],
            [],
            None,
            "2026-01-01T00:00:00Z",
        )

        assert (await store.get_active_self_update_for_node("n1"))[
            "deploy_id"
        ] == "self"
        assert await store.get_active_self_update_for_node("n2") is None


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
                branch=f"history-{i}",
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

    async def test_globally_sorts_canonical_and_attempt_rows_before_limit(
        self, store: EventStore
    ):
        for deploy_id, branch in (("d1", "one"), ("d2", "two")):
            await store.create_deploy_event(
                deploy_id=deploy_id,
                node_id="n1",
                repo="r",
                branch=branch,
                commits=[deploy_id],
                affected_services=[],
                diff_stat=None,
                detected_at="2026-08-01T00:00:00Z",
                target_head=deploy_id,
            )
        attempts = store.attempts
        assert attempts is not None
        await attempts.begin_normal_attempt(
            orchestrator_attempt_id="a1",
            deploy_id="d1",
            connection_generation="g1",
            current_generation="g1",
            source="manual_single",
            approved_by="director",
            deadline_at="2099-01-01T00:00:00+00:00",
        )
        await attempts.fail_active_attempt(
            "a1",
            kind="failed",
            stage="execution",
            reason="test_failure",
            error="boom",
        )
        await store._db.execute(
            "UPDATE deploy_attempts SET completed_at = ? WHERE orchestrator_attempt_id = ?",
            ("2026-08-03T00:00:00Z", "a1"),
        )
        await store._db.execute(
            "UPDATE deploy_events SET updated_at = ? WHERE deploy_id = ?",
            ("2026-08-02T00:00:00Z", "d2"),
        )
        await store._db.execute(
            "UPDATE deploy_events SET updated_at = ? WHERE deploy_id = ?",
            ("2026-08-01T00:00:00Z", "d1"),
        )
        await store._db.commit()

        history = await store.get_deploy_history(limit=2)

        assert [row["deploy_id"] for row in history] == ["attempt:a1", "d2"]

    async def test_excludes_superseded_by_default(self, store: EventStore):
        """superseded rows (reject_reason starting with 'superseded by ') are
        excluded from history by default — they would otherwise drown out
        actionable deploys."""
        for index, did in enumerate(("d_old", "d_new", "d_manual")):
            await store.create_deploy_event(
                deploy_id=did,
                node_id="n1",
                repo="r",
                branch=f"branch-{index}",
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
